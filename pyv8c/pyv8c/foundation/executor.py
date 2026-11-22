"""Dedicated-thread driver for Worker runtimes."""

from __future__ import annotations

import threading
import time

from . import execution_state as state
from . import timer_resolution
from .logging import logger


class RealmExecutor(state.ExecutionState):
    """Advance shared execution state on one dedicated owner thread."""

    def __init__(self, name="pyv8c-realm", *, auto_pump=True, default_timeout=state._UNSET):
        super().__init__(auto_pump=auto_pump, default_timeout=default_timeout)
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def wakeup(self):
        """Publish a wake after publishing work; the sequence survives early signals."""
        with self._wake_condition:
            self._wake_sequence += 1
            self._wake_condition.notify()

    def _sleep(self, observed):
        """Park only if no wake has arrived since the pump began its work scan.

        No queue lock is acquired while holding the wake condition. Publication
        racing any scan is retained in the sequence, and Condition.wait releases
        the lock atomically with entering the wait. No signal is ever cleared.
        """
        # The pump already tried every work source. Any later publication
        # changes the sequence; elapsed timers are covered by the deadline.
        # Do not repeat the entire readiness scan before parking.
        seconds = self._next_deadline()
        deadline = None if seconds is None else time.monotonic() + max(0.0, seconds)
        with self._wake_condition:
            if self.closed or self._wake_sequence != observed:
                return
            with timer_resolution.hold(enabled=deadline is not None):
                while not self.closed and self._wake_sequence == observed:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        break
                    self._wake_condition.wait(remaining)

    def _run(self):
        self._owner_ident = threading.get_ident()
        try:
            self._drive()
        finally:
            # A thread that went away for any reason must take its callers with it:
            # a closed executor fails fast instead of blocking them until timeout.
            self._closed.set()
            self._fail_pending()

    def _drive(self):
        while not self._closed.is_set():
            self._pump()

    def close(self, timeout=5):
        self._close_commands()
        if not self.is_owner_thread():
            self._thread.join(timeout)
            if self._thread.is_alive():
                # A leaked owner thread keeps the isolate alive; say so.
                logger.warning(f"{self._thread.name} did not exit within {timeout}s")


__all__ = ["RealmExecutor"]
