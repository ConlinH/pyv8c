"""Completion ownership for one Page's host work."""

import asyncio
import threading
from collections import Counter

from .close_timing import close_phase
from .host_errors import HostStateError
from .logging import logger

#: Maximum time without a background completion during Page teardown.
_NETWORK_WAIT_SECONDS = 60.0


class BackgroundWork:
    def __init__(self):
        self._lock = threading.Lock()
        self._futures = {}
        self._threads = set()
        self._workers = set()
        self._network = 0
        self._requests = set()
        self._waiters = set()
        self._retiring = False

    def submit(self, executor, callback, *args, kind):
        """Register work before retirement can take its completion snapshot."""
        with self._lock:
            if self._retiring:
                raise HostStateError("Page is closing")
            future = executor.submit(callback, *args)
            self._futures[future] = kind
        future.add_done_callback(self._finished)
        return future

    def _finished(self, future):
        with self._lock:
            self._futures.pop(future, None)
            waiters = tuple(self._waiters)
        self._notify_progress(waiters)

    def track(self, future, *, kind):
        """Join an owner-registered transport Future during Page teardown."""
        with self._lock:
            self._futures[future] = kind
        future.add_done_callback(self._finished)

    def create_worker(self, factory):
        """Publish thread ownership before retirement can take its snapshot."""
        with self._lock:
            if self._retiring:
                raise HostStateError("Page is closing")
            executor = factory()
            self._threads = {item for item in self._threads if item.is_alive()}
            self._threads.add(executor._thread)
        return executor

    def begin_retirement(self):
        with self._lock:
            self._retiring = True
            requests = tuple(self._requests)
        for stop in requests:
            try:
                stop()
            except Exception:
                logger.exception("failed to interrupt a Page request")

    def track_worker(self, worker):
        with self._lock:
            self._workers.add(worker)

    def discard_worker(self, worker):
        with self._lock:
            self._workers.discard(worker)

    def begin_network(self, stop=None):
        with self._lock:
            if stop is not None:
                if self._retiring:
                    raise HostStateError("Page is closing")
                self._requests.add(stop)
            self._network += 1

    def complete_network(self, stop=None):
        with self._lock:
            if stop is not None:
                self._requests.remove(stop)
            self._network -= 1
            if self._network < 0:
                raise RuntimeError("unbalanced Page network ownership")
            waiters = tuple(self._waiters)
        self._notify_progress(waiters)

    @staticmethod
    def _notify_progress(waiters):
        for loop, future in waiters:
            def deliver(future=future):
                if not future.done():
                    future.set_result(None)
            loop.call_soon_threadsafe(deliver)

    async def wait(self):
        scope = asyncio.timeout(_NETWORK_WAIT_SECONDS)
        try:
            async with scope:
                await self._join(scope)
        except TimeoutError:
            if not scope.expired():
                raise
            with self._lock:
                pending = self._network
                futures = len(self._futures)
            raise HostStateError(
                f"Page network work did not finish teardown ({pending} in flight, "
                f"{futures} background futures)"
            ) from None

    async def _join(self, scope):
        # Re-snapshot after each join; failed joins retain ownership for a retry.
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                threads = tuple(self._threads)
                workers = tuple(self._workers)
            for worker in workers:
                worker.stop()
            for thread in threads:
                with close_phase(self, "worker-thread"):
                    await asyncio.to_thread(thread.join, 5)
                if thread.is_alive():
                    raise HostStateError("Page Worker did not finish teardown")
                with self._lock:
                    self._threads.discard(thread)
                scope.reschedule(loop.time() + _NETWORK_WAIT_SECONDS)
            with self._lock:
                if self._threads:
                    continue
                self._workers.clear()
                if not self._network and not self._futures:
                    return
                if self._futures:
                    counts = Counter(self._futures.values())
                    kinds = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
                    phase = f"background-futures ({kinds})"
                else:
                    http = len(self._requests)
                    phase = f"network-drain (http={http}, other={self._network - http})"
                changed = loop.create_future()
                waiter = (loop, changed)
                self._waiters.add(waiter)
            try:
                with close_phase(self, phase):
                    await changed
                scope.reschedule(loop.time() + _NETWORK_WAIT_SECONDS)
            finally:
                with self._lock:
                    self._waiters.discard(waiter)
