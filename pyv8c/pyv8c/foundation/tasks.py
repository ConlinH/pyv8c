"""Per-page task, timer, and pending-work scheduler.

:meth:`TaskQueue.run_turn` selects one runnable task, rotating between sources.
Microtasks are checkpoints owned by the execution agent, never a task source.
"""

from __future__ import annotations

import enum
import heapq
import math
import traceback
import itertools
import threading
import time
from collections.abc import Callable
from typing import Protocol

from .error_policy import control_exceptions, is_host_failure
from .logging import logger

#: Timed on the finest monotonic clock Python exposes: Windows' ``time.monotonic()``
#: moves once per 64 Hz tick, so a 5 ms timer could only be seen 15.6 ms late; on
#: platforms where ``monotonic`` is already fine grained ``perf_counter`` is the same clock.
_now = time.perf_counter


class TaskSource(enum.Enum):
    """FIFO task sources; enum order seeds fair rotation, not priority."""

    # Enum equality is identity; use its C-level identity hash in queue indexes.
    __hash__ = object.__hash__

    PARSER = "parser"         # parser resumption, gated by the parser-blocking state
    NAVIGATION = "navigation"
    NETWORKING = "networking"
    TIMER = "timer"
    POSTED_MESSAGE = "posted-message"
    USER_INTERACTION = "user-interaction"
    DOM = "dom-manipulation"
    MEDIA_ELEMENT = "media-element"
    EMBEDDER = "embedder"


class TaskHost(Protocol):
    """The only thing :class:`TaskQueue` may ask of its owning realm.

    ``TaskQueue`` lives in ``foundation`` and must never reach back into ``platform``,
    so the page supplies this protocol and the queue can be built and tested alone.
    """

    def is_closed(self) -> bool: ...
    def parser_blocked(self) -> bool: ...


class _NullTaskHost:
    """Stand-in host used when a queue is built without an owning realm."""

    def is_closed(self) -> bool:
        return False

    def parser_blocked(self) -> bool:
        return False


class _HTMLTimer:
    __slots__ = ('callback', 'nesting', 'delay')

    def __init__(self, callback, delay, nesting):
        self.callback = callback
        self.nesting = nesting + 1
        self.delay = max(0.0, float(delay))

    def timeout(self):
        # The algorithm clamps before incrementing the task's stored level.
        return max(self.delay, 0.004) if self.nesting > 6 else self.delay

    def __call__(self):
        return self.callback()


class TaskQueue:
    """One realm's task sources, timers and pending-work counters."""

    def __init__(self, host=None, *, error_sink: Callable[[BaseException], None] | None = None):
        from .agent_tasks import AgentTaskQueue, TaskContext
        self._routing_lock = threading.RLock()
        self._runner = None
        self._task_context = TaskContext()
        self._timer_queued = False
        self._timer_ticket = None
        self._host = host if host is not None else _NullTaskHost()
        self._error_sink = error_sink
        self.timers: list[tuple[float, int, Callable[..., object], float | None]] = []
        self.counter = itertools.count(1)
        self.cancelled: set[int] = set()
        self._timer_ids: set[int] = set()
        self._timer_nesting = 0
        self.errors: list[BaseException] = []
        # Guards the errors and the in-flight counters, which other threads touch.
        self._lock = threading.RLock()
        self.network_pending = 0
        self.worker_pending = 0
        self.loader_pending = 0
        self._closed = False
        self._boundary = None
        self._scheduler = AgentTaskQueue()
        self._scheduler.attach(self)

    @property
    def closed(self):
        return self._closed or self._host.is_closed()

    def set_error_sink(self, sink):
        """Install the reporter notified for every task exception."""
        self._error_sink = sink

    def set_boundary(self, boundary):
        self._boundary = boundary

    def set_wakeup(self, wakeup):
        """Install the callback that rouses the realm executor.

        Without it a task posted from a worker or network thread would only be
        noticed when the next command arrives.
        """
        self._scheduler.set_wakeup(wakeup)

    def _signal(self):
        """Nudge the realm executor, which owns the sleeping."""
        self._scheduler._signal()

    def run_callback(self, callback, *args):
        """Run one queued task, keeping a faulty task from aborting the rest."""
        if self._runner is not None:
            # The native ticket covers error listeners and callback cleanup too,
            # not just the callback's direct JavaScript invocation.
            return self._runner(self._run_callback, callback, *args)
        return self._run_callback(callback, *args)

    def _run_callback(self, callback, *args):
        try:
            return callback(*args)
        except control_exceptions():
            raise
        except BaseException as exc:
            self._report_error(exc, callback)
            return None
        finally:
            if self._boundary is not None:
                self._boundary()

    def report_error(self, exc, callback=None):
        """Record an exception against this realm and notify the sink.

        Public counterpart of :meth:`_report_error`, for collaborators such as the
        executor that need the same bookkeeping without a private method.
        """
        self._report_error(exc, callback)

    def _report_error(self, exc, callback=None):
        """Record a task exception and hand it to the realm's reporter.

        A failure of the host's own scheduling is not a task's: an ``error`` event is
        what a script does wrong, so the reason is logged and the realm is not told.
        """
        if is_host_failure(exc):
            #: An error raised across the native boundary often arrives with no Python
            #: frames at all, so the traceback below can be empty; the task's own name
            #: is then the only thing that says where the failure happened.
            who = getattr(callback, "__qualname__", None) or repr(callback)
            logger.warning("the host could not finish: {} (task: {})", exc, who)
            #: The one line above rarely says where it came from; the traceback, at
            #: debug, does -- a host failure is diagnosed in the log or not at all.
            logger.debug(
                "the host could not finish, from:\n{}",
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip(),
            )
            return
        #: What is left is a declared error or a JavaScript exception: both are the
        #: realm's to see as they are, so nothing stands between them and the sink.
        with self._lock:
            self.errors.append(exc)
        sink = self._error_sink
        if sink is not None:
            try:
                sink(exc)
            except control_exceptions():
                raise
            except BaseException:  # reporting must never break the turn
                pass

    def drain_task_errors(self):
        """Take and clear the task exceptions collected so far."""
        with self._lock:
            errors = tuple(self.errors)
            self.errors.clear()
        return errors

    def begin_network(self):
        with self._lock:
            self.network_pending += 1

    def complete_network(self):
        with self._lock:
            self.network_pending = max(0, self.network_pending - 1)
        self._signal()

    def begin_worker(self):
        with self._lock:
            self.worker_pending += 1

    def complete_worker(self):
        with self._lock:
            self.worker_pending = max(0, self.worker_pending - 1)
        self._signal()

    def begin_loader(self):
        with self._lock:
            self.loader_pending += 1

    def complete_loader(self):
        with self._lock:
            self.loader_pending = max(0, self.loader_pending - 1)
        self._signal()

    def bind_scheduler(self, scheduler, runner, context):
        """Move internal continuations to the current Agent, preserving lane ownership.

        Document-bound records retain their original identity and are discarded
        when that identity is no longer active. Internal cleanup may survive a
        navigation and therefore is explicitly not document-bound by default.
        """
        with self._routing_lock:
            if self.closed or scheduler.closed:
                raise RuntimeError('cannot bind a closed task queue')
            old = self._scheduler
            previous_runner, previous_context = self._runner, self._task_context
            self._runner = runner
            self._task_context = context
            if old is scheduler:
                retired = scheduler.invalidate_document(self)
            else:
                records = old.detach(self)
                scheduler.attach(self)
                self._scheduler = scheduler
                retired = scheduler.adopt(records)
        del retired, previous_runner, previous_context

    def set_task_context(self, context):
        """Replace this lane's identity and invalidate old document work."""
        with self._routing_lock:
            # Worker identity is installed before its V8 context becomes live.
            # Changing identity does not start work or reopen a closed queue.
            previous_context = self._task_context
            self._task_context = context
            retired = self._scheduler.invalidate_document(self)
        del retired, previous_context

    def post_task(self, callback, source: TaskSource = TaskSource.DOM, *, label=None, document_bound=False):
        """Return a cancellation ticket, or None when this lane no longer accepts work."""
        with self._routing_lock:
            return self._scheduler.post(self, callback, source, label=label, document_bound=document_bound)

    def post(self, callback, source: TaskSource = TaskSource.DOM):
        """Queue *callback* on the given task source.

        Parser work is held separately so a parser-blocking script can gate resumption
        without freezing network delivery or timers.
        """
        return self.post_task(callback, source) is not None

    def post_managed(self, callback, source=TaskSource.EMBEDDER):
        """Publish and wake for an operation owning its budget and cleanup."""
        with self._routing_lock:
            return self._scheduler.post(self, callback, source, label='embedder operation', managed=True)

    def _timers_changed(self):
        # Heap mutations finish before acquiring routing/Agent locks. A bind
        # racing this notification either imports the heap or receives it here.
        with self._routing_lock:
            self._scheduler.refresh_timers(self)
        self._signal()

    def _has_timers(self):
        # A running interval is temporarily absent from the heap but remains
        # registered. Keep its lane indexed across callback-side cancellation.
        with self._lock:
            self._discard_cancelled_timers()
            return not self.closed and bool(self._timer_ids)

    def schedule(self, callback, delay, repeat=False):
        try:
            delay = max(0.0, float(delay))
        except (TypeError, ValueError):
            delay = 0.0
        with self._lock:
            if self.closed:
                return 0
            ident = next(self.counter)
            self._timer_ids.add(ident)
            heapq.heappush(self.timers, (_now() + delay, ident, callback, delay if repeat else None))
        self._timers_changed()
        return ident

    def schedule_html_timer(self, callback, delay, repeat=False):
        """Apply HTML timer nesting without changing internal timeout users."""
        timer = _HTMLTimer(callback, delay, self._timer_nesting)
        return self.schedule(timer, timer.timeout(), repeat)

    def cancel(self, ident):
        ident = int(ident)
        with self._lock:
            if ident in self._timer_ids:
                self._timer_ids.discard(ident)
                self.cancelled.add(ident)
        self._timers_changed()

    def cancel_timers(self):
        """Drop every pending timer.

        A timer belongs to the document that set it, and this queue belongs to the page,
        so a document being replaced has to clear its own: otherwise the callbacks run
        into a realm that is gone. Posted tasks are left alone -- the engine's own
        plumbing travels that way and is still wanted.
        """
        with self._lock:
            ticket, self._timer_ticket = self._timer_ticket, None
            self._timer_queued = False
            self.timers.clear()
            self.cancelled.clear()
            self._timer_ids.clear()
        if ticket is not None:
            ticket.cancel()
        self._timers_changed()

    def pending(self):
        """Whether this lane has a valid queued task."""
        return self._scheduler.pending(self)

    def has_runnable_tasks(self):
        return self._scheduler.has_runnable_tasks(self)

    def _has_due_timer(self):
        with self._lock:
            self._discard_cancelled_timers()
            return bool(self.timers and self.timers[0][0] <= _now())

    def _timer_deadline(self):
        with self._lock:
            return self.timers[0][0] if self.timers else float('inf')

    def _deliver_timer(self):
        self._timer_queued = False
        self._timer_ticket = None
        self._run_due_timers()

    def next_deadline(self):
        """Seconds until the next timer is due, or ``None`` when none exists.

        The executor uses this as its idle wait timeout so timers fire without polling.
        """
        with self._lock:
            self._discard_cancelled_timers()
            if self.timers:
                return max(0.0, self.timers[0][0] - _now())
            return None

    def _discard_cancelled_timers(self):
        """Cancelled heap entries are bookkeeping, never an event-loop turn."""
        while self.timers and self.timers[0][1] in self.cancelled:
            _, ident, _, _ = heapq.heappop(self.timers)
            self.cancelled.discard(ident)

    def _run_due_timers(self):
        with self._lock:
            self._discard_cancelled_timers()
            if not self.timers or self.timers[0][0] > _now():
                return False
            due, ident, callback, interval = heapq.heappop(self.timers)
            if interval is None:
                self._timer_ids.discard(ident)
        next_due = None
        if interval is not None:
            now = _now()
            # Blink re-arms repeating timers before invoking their handler. A late
            # wake skips missed grid slots, leaving at least interval / 20 before
            # the next target; a long callback can make that target overdue.
            next_due = (due + math.ceil((now + interval / 20 - due) / interval) * interval
                        if interval else now)
            if isinstance(callback, _HTMLTimer):
                following = max(callback.delay, 0.004) if callback.nesting >= 6 else callback.delay
                next_due += following - interval
                interval = following
        previous = self._timer_nesting
        self._timer_nesting = callback.nesting if isinstance(callback, _HTMLTimer) else 0
        try:
            self.run_callback(callback)
        finally:
            self._timer_nesting = previous
            with self._lock:
                if interval is not None:
                    if ident in self._timer_ids and not self.closed:
                        if isinstance(callback, _HTMLTimer):
                            callback.nesting += 1
                        heapq.heappush(self.timers, (next_due, ident, callback, interval))
                    else:
                        self.cancelled.discard(ident)
                empty = not self.timers
            # A nonempty heap was already indexed before this delivery. Only
            # removing its final entry changes membership; avoid bookkeeping
            # and self-wakeup for every item in a dense timer batch.
            if empty:
                self._timers_changed()
        return True

    def run_turn(self, *, allow_parser: bool = True) -> bool:
        """Select one task through the Agent's sole source selector."""
        return self._scheduler.run_turn(allow_parser=allow_parser)

    def close(self):
        with self._lock:
            self._closed = True
            retired_timers, self.timers = self.timers, []
            self.cancelled.clear()
            self._timer_ids.clear()
            # Completions may arrive after close; no task may land after this clear.
            self.network_pending = 0
            self.worker_pending = 0
            self.loader_pending = 0
        with self._routing_lock:
            records = self._scheduler.detach(self)
            retired = [record.ticket._cancel_pending() for record in records]
            self._timer_queued = False
        del retired, records, retired_timers
        self._signal()


__all__ = ["TaskHost", "TaskQueue", "TaskSource"]
