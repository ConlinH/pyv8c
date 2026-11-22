"""Shared execution state for single-owner V8 realms.

One thread owns the realm. Drivers choose how to wake and park; command budgets,
publication, cancellation and bounded task selection have one implementation.
"""

from __future__ import annotations

import itertools
import math
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .error_policy import control_exceptions
from .host_errors import RealmBusyError
from .logging import logger

#: Exceptions that must interrupt the process rather than be logged.
_CONTROL_EXCEPTIONS = (KeyboardInterrupt, SystemExit)


#: A queued command: its optional synchronous completion and owner operation.
@dataclass(slots=True)
class Command:
    completion: _Completion | None
    callback: Callable
    args: tuple
    kwargs: dict
    submitted: float
    deadline: float | None
    identity: int
    owner: object = None
    started: bool = False
    ticket: object = None
    discarded: bool = False
    owns_checkpoint: bool = False
    runner: Callable | None = None

    def __call__(self):
        # The task record holds the command directly, rather than a closure
        # holding both the command and its executor. Cancellation retires runner.
        runner = self.runner
        if runner is not None:
            runner(self)


def _default_call_timeout():
    """Seconds a synchronous ``call()`` may wait before raising.

    ``PYV8C_CALL_TIMEOUT`` overrides it; ``0`` or a negative value disables the
    bound.  The default sits far above every documented operation, so it only
    trips on a genuinely wedged realm.
    """
    raw = os.environ.get("PYV8C_CALL_TIMEOUT")
    if raw is None:
        return 60.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 60.0
    return None if value <= 0 else value


_UNSET = object()


def _diagnostic_text(value, convert):
    try:
        return convert(value)
    except control_exceptions():
        raise
    except Exception:
        return "<unprintable>"


def _report_late(outcome, name, submitted, timeout):
    """What a command that outlived its caller ended with.

    The caller is gone, so the answer has nowhere to go but the log: without this a
    network timeout or a failure inside a command is lost, which is what makes a
    realm that is merely waiting look like one that is silent.

    The caller's own budget is repeated here because the two durations read
    differently: ending just past it means the command only started near the
    deadline, so what took the time was whatever held the owner thread before it.
    """
    try:
        seconds = time.perf_counter() - submitted
        note = f" after it was submitted (its caller waited {timeout:g}s)"
        label = _diagnostic_text(name, repr)
        if outcome is None:
            logger.warning(f"{label} finished {seconds:.1f}s{note}")
            return
        detail = _diagnostic_text(outcome, str)
        logger.warning(f"{label} failed {seconds:.1f}s{note}: {type(outcome).__name__}: {detail}")
    except control_exceptions():
        raise
    except Exception:
        # A diagnostic sink must not retire the owner after its caller has left.
        pass


class _CallExpired(Exception):
    def __init__(self, cancelled):
        self.cancelled = cancelled


class _Completion:
    """One synchronous caller and one owner, with atomic start-versus-timeout.

    No callback list or Command reference is retained. Queued expiry prevents
    execution; running expiry leaves the owner responsible for cleanup and logs
    its eventual outcome. Result publication always happens after that cleanup.
    """

    __slots__ = ("_condition", "_state", "_value", "_error", "_late")

    def __init__(self):
        self._condition = threading.Condition()
        self._state = "queued"
        self._value = self._error = self._late = None

    def start(self):
        with self._condition:
            if self._state != "queued":
                return False
            self._state = "running"
            return True

    def wait(self, deadline, description):
        with self._condition:
            while self._state in ("queued", "running"):
                remaining = None if deadline is None else deadline - time.perf_counter()
                if remaining is not None and remaining <= 0:
                    cancelled = self._state == "queued"
                    if cancelled:
                        self._state = "cancelled"
                    else:
                        self._late = description
                    raise _CallExpired(cancelled)
                self._condition.wait(remaining)
            value, error = self._value, self._error
            self._value = self._error = None
        if error is not None:
            raise error
        return value

    def finish(self, value=None, error=None):
        with self._condition:
            if self._state in ("finished", "cancelled"):
                return
            self._state = "finished"
            late, self._late = self._late, None
            if late is None:
                self._value, self._error = value, error
            self._condition.notify()
        if late is not None:
            _report_late(error, *late)


class ExecutionState:
    """Owner-neutral command, operation and task state; no driver or thread."""

    def __init__(self, *, auto_pump=True, default_timeout=_UNSET):
        self._commands: queue.SimpleQueue[Command] = queue.SimpleQueue()
        self._pending_commands = {}
        self._closed = threading.Event()
        # Serialises a submit against close, so a command cannot land in a stopped queue.
        self._submit_lock = threading.RLock()
        self._wake_condition = threading.Condition()
        self._wake_sequence = 0
        self._owner_ident = None
        self._task_queues = {}
        self._queue_cursor = 0
        self._rendering = None
        self._foreground = None
        self._command_route_state = (None, None, False)
        self.auto_pump = bool(auto_pump)
        self.default_timeout = (
            _default_call_timeout() if default_timeout is _UNSET else default_timeout
        )
        self._error_sink = None
        self._deadline_source = None
        self._checkpoint = None
        self._operation_deadline = None
        self._operation_identity = None
        self._identities = itertools.count(1)

    @property
    def closed(self):
        return self._closed.is_set()

    def is_owner_thread(self):
        return threading.get_ident() == self._owner_ident

    def bind_command_queue(self, queue, runner=None, *, owns_checkpoint=False):
        """Route ordinary host operations to an owner-installed source queue.

        Explicit _task_lane=None remains the restricted bootstrap/control path.
        owns_checkpoint means the runner completes callback cleanup, including
        exceptional exits, before returning; raw controls keep the fallback.
        """
        if not self.is_owner_thread():
            raise RuntimeError("command queue binding requires the owner thread")
        if owns_checkpoint and runner is None:
            raise ValueError("checkpoint ownership requires an execution runner")
        self._command_route_state = (queue, runner, owns_checkpoint)

    def _command_route(self, callback, args, lane):
        owns_checkpoint = False
        if lane is _UNSET:
            lane, runner, owns_checkpoint = self._command_route_state
            if runner is not None:
                callback, args = runner, (callback, *args)
        return callback, args, lane, owns_checkpoint

    def attach_task_queue(self, task_queue, *, owns_checkpoint=False):
        """Attach an owner queue; opt out of fallback only if its callbacks own cleanup."""
        self._task_queues[task_queue] = owns_checkpoint
        # The queue is the realm's single reporting entry point for exceptions
        # raised by a fire-and-forget callback.
        if self._error_sink is None:
            self._error_sink = task_queue.report_error
        # Only the execution driver owns sleeping; queues merely wake it.
        task_queue.set_wakeup(self.wakeup)
        self.wakeup()

    def attach_deadline_source(self, source):
        """Attach the realm's deadline holder (its native V8Context).

        Arming it for one command is what lets a command already running be
        preempted, so a wedged realm raises :class:`pyv8.JavaScriptTerminated`
        instead of hanging the caller.
        """
        self._deadline_source = source

    def attach_checkpoint(self, checkpoint):
        self._checkpoint = checkpoint

    def attach_rendering(self, rendering):
        if not self.is_owner_thread():
            raise RuntimeError("rendering binding requires the owner thread")
        self._rendering = rendering
        rendering.set_wakeup(self.wakeup)
        self.wakeup()

    def attach_foreground(self, source):
        """Attach an owner-thread foreground task source to the driver.

        The source supplies ``has_runnable_tasks``, ``next_deadline``,
        ``run_turn`` and ``set_wakeup`` with the same contracts as task queues.
        Posting may happen on other threads; running and detaching stay on the
        owner thread.  One foreground turn is interleaved with other work.
        """
        if not self.is_owner_thread():
            raise RuntimeError("foreground binding requires the owner thread")
        if self._foreground is not None:
            raise RuntimeError("a foreground source is already bound")
        source.set_wakeup(self.wakeup)
        self._foreground = source
        self.wakeup()

    def detach_foreground(self, source):
        if not self.is_owner_thread():
            raise RuntimeError("foreground unbinding requires the owner thread")
        if self._foreground is source:
            self._foreground = None
            source.set_wakeup(None)

    def detach_deadline_source(self, source):
        if self._deadline_source is source:
            self._deadline_source = None

    def call(self, callback, *args, timeout=_UNSET, _task_lane=_UNSET, **kwargs):
        """Run *callback* on the owner thread and return or raise its result.

        Raises :class:`RealmBusyError` when the owner thread does not pick the
        command up within *timeout*: a wedged realm surfaces as an error.
        """
        if timeout is _UNSET:
            if self.is_owner_thread():
                return callback(*args, **kwargs)
            timeout = self.default_timeout
        if timeout is not None:
            timeout = float(timeout)
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("timeout must be finite and non-negative, or None")
        submitted = time.perf_counter()
        deadline = None if timeout is None else submitted + timeout
        if self.is_owner_thread():
            if deadline is None:
                return callback(*args, **kwargs)
            previous = self._operation_deadline, self._operation_identity
            if previous[0] is not None:
                deadline = min(deadline, previous[0])
            if deadline <= submitted:
                raise RealmBusyError("inline operation deadline expired before execution")
            self._operation_deadline = deadline
            self._operation_identity = (
                previous[1] if previous[1] is not None else next(self._identities)
            )
            try:
                return callback(*args, **kwargs)
            finally:
                self._operation_deadline, self._operation_identity = previous
        name = getattr(callback, "__name__", callback)
        callback, args, _task_lane, owns_checkpoint = self._command_route(
            callback, args, _task_lane
        )
        completion = _Completion()
        command = Command(
            completion,
            callback,
            args,
            kwargs,
            submitted,
            deadline,
            next(self._identities),
            owner=getattr(callback, "__self__", None),
            owns_checkpoint=owns_checkpoint,
        )
        self._submit_command(command, _task_lane)
        try:
            return completion.wait(deadline, (name, submitted, timeout))
        except _CallExpired as expired:
            if expired.cancelled:
                self._cancel_queued(command)
            raise RealmBusyError(
                f"realm executor did not complete {_diagnostic_text(name, repr)} within {timeout}s"
                + (
                    " (command cancelled)"
                    if expired.cancelled
                    else " (already running; its outcome is logged when it ends)"
                )
            ) from None

    def post(self, callback, *args, _task_lane=_UNSET, **kwargs):
        """Asynchronously execute a callback on the owner thread."""
        submitted = time.perf_counter()
        deadline = None if self.default_timeout is None else submitted + self.default_timeout
        callback, args, _task_lane, owns_checkpoint = self._command_route(
            callback, args, _task_lane
        )
        command = Command(
            None,
            callback,
            args,
            kwargs,
            submitted,
            deadline,
            next(self._identities),
            owner=getattr(callback, "__self__", None),
            owns_checkpoint=owns_checkpoint,
        )
        try:
            self._submit_command(command, _task_lane)
        except RuntimeError:
            return False
        return True

    def _check_admission(self):
        if self.closed:
            raise RuntimeError("realm executor is closed")

    def _submit_command(self, command, lane):
        with self._submit_lock:
            self._check_admission()
            if lane is not None:
                command.runner = self._execute_routed
            self._pending_commands[command.identity] = command
            if lane is None:
                self._commands.put(command)
        if lane is not None:
            try:
                ticket = lane.post_managed(command)
            except BaseException:
                self._cancel_queued(command)
                raise
            with self._submit_lock:
                command.ticket = ticket
                pending = command.identity in self._pending_commands
            if ticket is None:
                self._fail_commands((command,), "document task queue is closed")
                raise RuntimeError("document task queue is closed")
            if not pending:
                ticket.cancel()
        if lane is None:
            # Managed lanes publish their own wake after enqueueing. Only the
            # driver's bootstrap/control queue needs a separate notification.
            self.wakeup()

    def _cancel_queued(self, command):
        with self._submit_lock:
            if command.started:
                return
            self._pending_commands.pop(command.identity, None)
            ticket = command.ticket
            command.discarded = True
            retired = command.callback, command.args, command.kwargs, command.runner
            command.callback, command.args, command.kwargs, command.runner = None, (), {}, None
        if ticket is not None:
            ticket.cancel()
        del retired  # Finalizers may reenter the executor; never run under its lock.

    def cancel_owner(self, owner):
        """Fail commands belonging to a closing document without stopping siblings."""
        with self._submit_lock:
            commands = tuple(
                command for command in self._pending_commands.values() if command.owner is owner
            )
            for command in commands:
                self._pending_commands.pop(command.identity, None)
        self._fail_commands(commands, "window executor is closed")

    def _execute_routed(self, command):
        with self._submit_lock:
            # Leave closed submissions registered for the close path to settle.
            if self.closed:
                return
            self._pending_commands.pop(command.identity, None)
            if command.discarded:
                return
            if command.completion is not None and not command.completion.start():
                return
            command.started = True
        self._execute(command)

    def _next_command(self):
        with self._submit_lock:
            while not self.closed:
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    return None
                self._pending_commands.pop(command.identity, None)
                if command.discarded:
                    continue
                if command.completion is not None and not command.completion.start():
                    continue
                command.started = True
                return command
        return None

    def has_pending_for(self, owners):
        with self._submit_lock:
            return any(
                any(command.owner is owner for owner in owners) and not command.discarded
                for command in self._pending_commands.values()
            )

    def _execute(self, command):
        completion, callback, args, kwargs = (
            command.completion,
            command.callback,
            command.args,
            command.kwargs,
        )
        source = self._deadline_source
        previous_operation = self._operation_deadline, self._operation_identity
        self._operation_deadline = command.deadline
        self._operation_identity = command.identity
        budget = self.remaining_budget()
        armed = source is not None and bool(budget)
        if armed:
            try:
                source.set_deadline(budget)
            except BaseException:
                # The context may already be gone, which leaves nothing to preempt;
                # the command still has work to do, cleanup above all.
                armed = False
        result = error = None
        try:
            if command.deadline is not None and budget <= 0:
                raise RealmBusyError("operation deadline expired before execution")
            result = callback(*args, **kwargs)
            if self._checkpoint is not None and not command.owns_checkpoint:
                self._checkpoint()
        except BaseException as exc:
            if completion is None:
                # post() has no synchronous caller to receive the exception.
                self._report_exception(exc)
                return
            error = exc
        finally:
            if armed:
                try:
                    source.clear_deadline()
                except BaseException:
                    # The command may have closed the context it borrowed.
                    pass
            self._operation_deadline, self._operation_identity = previous_operation
            command.callback, command.args, command.kwargs, command.runner = None, (), {}, None
        if completion is not None:
            completion.finish(result, error)

    def remaining_budget(self):
        deadline = self._operation_deadline
        return None if deadline is None else max(0.0, deadline - time.perf_counter())

    @property
    def has_operation(self):
        return self._operation_identity is not None

    def _report_exception(self, exc):
        """Handle an exception raised by a fire-and-forget command.

        Control exceptions are re-raised so a ``Ctrl-C`` reaching the realm
        interrupts the process instead of being written to a log.
        """
        if isinstance(exc, _CONTROL_EXCEPTIONS):
            raise exc
        if isinstance(exc, control_exceptions()):
            logger.debug("realm background operation terminated")
            return
        reported = False
        sink = self._error_sink
        if sink is not None:
            try:
                sink(exc)
                reported = True
            except control_exceptions():
                raise
            except BaseException:  # reporting must not break the loop
                reported = False
        if not reported:
            logger.opt(exception=exc).error("realm command failed")

    def detach_task_queue(self, task_queue):
        self._task_queues.pop(task_queue, None)

    def _next_deadline(self):
        if not self.auto_pump:
            return None
        deadlines = [
            value
            for queue in tuple(self._task_queues)
            if not queue.closed and (value := queue.next_deadline()) is not None
        ]
        if self._rendering is not None:
            value = self._rendering.next_deadline()
            if value is not None:
                deadlines.append(value)
        if self._foreground is not None:
            value = self._foreground.next_deadline()
            if value is not None:
                deadlines.append(value)
        return min(deadlines) if deadlines else None

    def _run_turn(self):
        if not self.auto_pump:
            return False
        queues = tuple(self._task_queues.items())
        if not queues:
            return False
        for offset in range(len(queues)):
            index = (self._queue_cursor + offset) % len(queues)
            tasks, owns_checkpoint = queues[index]
            if not tasks.has_runnable_tasks():
                continue
            try:
                worked = tasks.run_turn()
                if worked and self._checkpoint is not None and not owns_checkpoint:
                    self._checkpoint()
            except _CONTROL_EXCEPTIONS:
                raise
            except control_exceptions():
                # A terminated task is not a page error and must not retire the
                # owner thread shared by unrelated live realms.
                logger.debug("realm task terminated")
                worked = True
            except BaseException as exc:
                # Task cleanup and microtask checkpoints are part of this turn.
                # Report against its owner without killing unrelated queued work.
                tasks.report_error(exc)
                worked = True
            if worked:
                self._queue_cursor = (index + 1) % len(queues)
                return True
        return False

    def _pump(self):
        """Alternate bounded command work with page work before considering sleep."""
        with self._wake_condition:
            observed = self._wake_sequence
        commanded = self._drain_commands()
        if self._closed.is_set():
            return
        worked = self._run_turn()
        foreground = False
        if (
            self.auto_pump
            and not self.closed
            and self._foreground is not None
            and self._foreground.has_runnable_tasks()
        ):
            try:
                foreground = self._foreground.run_turn()
            except _CONTROL_EXCEPTIONS:
                raise
            except control_exceptions():
                logger.debug("foreground operation terminated")
                foreground = True
            except BaseException as error:
                self._report_exception(error)
                foreground = True
        rendered = False
        if self.auto_pump and not self.closed and self._rendering is not None:
            try:
                rendered = self._rendering.run_frame()
            except _CONTROL_EXCEPTIONS:
                raise
            except control_exceptions():
                logger.debug("rendering operation terminated")
                rendered = True
            except BaseException as error:
                self._report_exception(error)
                rendered = True
        if worked or foreground or rendered:
            return
        if commanded:
            return
        self._sleep(observed)

    def _drain_commands(self):
        # A command can post its successor. Never drain that chain ahead of tasks.
        if self._closed.is_set():
            return False
        command = self._next_command()
        if command is None:
            return False
        # A fault's lifetime belongs to the driver: a thread exits, whereas
        # an application owner stops scheduling but must retain teardown.
        self._execute(command)
        return True

    def _fail_pending(self):
        """Complete the callers still waiting on commands that will not run."""
        with self._submit_lock:
            commands = tuple(self._pending_commands.values())
            self._pending_commands.clear()
            while True:
                try:
                    self._commands.get_nowait()
                except queue.Empty:
                    break
        self._fail_commands(commands, "realm executor is closed")

    def _fail_commands(self, commands, reason):
        for command in commands:
            with self._submit_lock:
                if command.started or command.discarded:
                    continue
                command.discarded = True
                self._pending_commands.pop(command.identity, None)
                retired = command.callback, command.args, command.kwargs, command.runner
                command.callback, command.args, command.kwargs, command.runner = None, (), {}, None
            if command.ticket is not None:
                command.ticket.cancel()
            if command.completion is not None:
                command.completion.finish(error=RuntimeError(reason))
            del retired

    def _close_commands(self):
        with self._submit_lock:
            if not self.closed:
                self._closed.set()
        self._fail_pending()
        # Let the driver retire a scheduled turn or wake a parked owner.
        self.wakeup()
