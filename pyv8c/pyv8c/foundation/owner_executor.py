"""Advance shared execution state through its caller's asyncio loop."""

import threading

from . import execution_state as state
from . import timer_resolution
from .agent_tasks import AgentTaskQueue
from .host_errors import HostStateError

_UNSCANNED = object()


class OwnerExecutor(state.ExecutionState):
    def __init__(self, loop, *, on_closed=None, on_failed=None, auto_pump=True, default_timeout=state._UNSET):
        super().__init__(auto_pump=auto_pump, default_timeout=default_timeout)
        self._loop = loop
        self._owner_ident = threading.get_ident()
        self._on_closed = on_closed
        self._on_failed = on_failed
        self.failed = False
        self._soon = self._timer = None
        self._in_turn = self._notified = self._fine_timer = False
        self._sleep_hint = _UNSCANNED

    def wakeup(self):
        with self._wake_condition:
            self._wake_sequence += 1
        if self.closed or self.failed:
            return
        if self.is_owner_thread():
            self._request_turn()
        else:
            try:
                self._loop.call_soon_threadsafe(self._request_turn)
            except RuntimeError:
                if not self.closed:
                    raise

    def _drop_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._fine_timer:
            self._fine_timer = False
            timer_resolution.release()

    def _request_turn(self):
        if self.closed or self.failed:
            return
        if self._in_turn:
            self._notified = True
            return
        self._drop_timer()
        if self._soon is None:
            self._soon = self._loop.call_soon(self._turn)

    def _sleep(self, observed):
        # There is no nested event loop or blocking owner wait. asyncio owns
        # parking, while the existing deadline sources remain authoritative.
        self._sleep_hint = self._next_deadline()

    def _turn(self):
        self._soon = None
        self._drop_timer()
        if self.closed or self.failed:
            return
        self._sleep_hint = _UNSCANNED
        self._notified = False
        self._in_turn = True
        try:
            super()._pump()
            if self.closed:
                return
            if self._sleep_hint is _UNSCANNED:
                ready = not self._commands.empty()
                if self.auto_pump:
                    for tasks in tuple(self._task_queues):
                        if isinstance(tasks, AgentTaskQueue) and not tasks._ready:
                            continue
                        if tasks.has_runnable_tasks():
                            ready = True
                            break
                delay = 0 if ready else self._next_deadline()
            else:
                delay = self._sleep_hint
        except BaseException:
            # Stop scheduling/admission, but keep owner-side resource teardown
            # reachable. Only actual disposal relinquishes Runtime ownership.
            with self._submit_lock:
                self.failed = True
            try:
                if self._on_failed is not None:
                    self._on_failed(self)
            finally:
                # Retiring arguments may run host finalizers. Publish the
                # Runtime failure before those finalizers can reenter it.
                self._fail_pending()
                self._drop_timer()
            raise
        finally:
            self._in_turn = False
        if self._notified or delay == 0:
            self._request_turn()
        elif delay is not None:
            timer_resolution.acquire()
            self._fine_timer = True
            self._timer = self._loop.call_later(delay, self._request_turn)

    def _check_admission(self):
        if self.failed:
            raise RuntimeError("owner executor failed")
        super()._check_admission()

    def close(self):
        if not self.is_owner_thread():
            raise HostStateError("Runtime teardown belongs to its owner thread")
        self._close_commands()
        if self._soon is not None:
            self._soon.cancel()
            self._soon = None
        self._drop_timer()
        callback, self._on_closed = self._on_closed, None
        self._on_failed = None
        if callback is not None:
            callback(self)
