"""Application-owned Runtime and cooperative resource lifetime."""

import asyncio
import threading
from enum import Enum, auto

from ..foundation.executor import RealmExecutor
from ..foundation.futures import consume_future
from ..foundation.host_errors import HostStateError
from ..foundation.owner_executor import OwnerExecutor
from .execution import WindowExecutionGroup


class RuntimeState(Enum):
    NEW = auto()
    OPEN = auto()
    CLOSING = auto()
    CLOSE_FAILED = auto()
    CLOSED = auto()


class Runtime:
    """Internal execution ownership; browser configuration lives in Browser."""

    def __init__(self):
        self._state = RuntimeState.NEW
        self._lifecycle_lock = threading.RLock()
        self._executors = set()
        self._worker_threads = set()
        self._io_threads = set()
        self._retired_io_pools = set()
        self._workers = set()
        self._close_task = None
        self._owner = None

    @property
    def state(self):
        return self._state

    @property
    def closed(self):
        return self._state is RuntimeState.CLOSED

    @property
    def owner(self):
        return self._owner

    def open(self):
        if self._state is not RuntimeState.NEW:
            raise HostStateError("Browser can only be entered once")
        self.loop = asyncio.get_running_loop()
        self._owner = threading.get_ident()
        self._state = RuntimeState.OPEN

    def begin_close(self):
        self.check_owner(allow_closing=True)
        self._state = RuntimeState.CLOSING

    def fail_close(self):
        self._state = RuntimeState.CLOSE_FAILED

    def check_loop(self):
        if asyncio.get_running_loop() is not self.loop:
            raise HostStateError("operation belongs to the Browser event loop")

    def check_owner(self, *, allow_closing=False):
        self.check_thread()
        allowed = (
            (RuntimeState.OPEN, RuntimeState.CLOSING, RuntimeState.CLOSE_FAILED)
            if allow_closing
            else (RuntimeState.OPEN,)
        )
        if self._state not in allowed:
            raise HostStateError("Browser is closed or not accepting work")

    def check_thread(self):
        if threading.get_ident() != self._owner:
            raise HostStateError("Browser APIs belong to their owner thread")

    def create_execution_group(self, frame_rate):
        self.check_owner()
        executor = OwnerExecutor(self.loop, on_closed=self._executor_closed,
                                 on_failed=self._executor_failed)
        self._executors.add(executor)
        try:
            return WindowExecutionGroup(frame_rate=frame_rate, executor=executor)
        except BaseException:
            executor.close()
            raise

    def create_worker_executor(self, name):
        with self._lifecycle_lock:
            if self.state is not RuntimeState.OPEN:
                raise HostStateError("Runtime is not accepting Workers")
            executor = RealmExecutor(name=name, auto_pump=True)
            self._worker_threads = {thread for thread in self._worker_threads if thread.is_alive()}
            self._worker_threads.add(executor._thread)
            return executor

    def _executor_closed(self, executor):
        self._executors.discard(executor)

    def _executor_failed(self, executor):
        with self._lifecycle_lock:
            if self._state is RuntimeState.OPEN:
                self._state = RuntimeState.CLOSE_FAILED

    def register_worker(self, worker):
        with self._lifecycle_lock:
            if worker.executor.closed:
                return
            self._workers.add(worker)
            stopping = self._state is not RuntimeState.OPEN
        if stopping:
            worker.stop()

    def discard_worker(self, worker):
        with self._lifecycle_lock:
            self._workers.discard(worker)

    def _retire_io_pool(self, pool):
        """Stop admission now; join transport and loader threads asynchronously."""
        with self._lifecycle_lock:
            self._retired_io_pools = {
                item for item in self._retired_io_pools
                if any(thread.is_alive() for thread in item._threads)
            }
            self._retired_io_pools.add(pool)
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        finally:
            # A cancellation callback can raise during shutdown. Retain both
            # the pool and its threads so a later close can finish the stop.
            with self._lifecycle_lock:
                self._io_threads = {thread for thread in self._io_threads if thread.is_alive()}
                self._io_threads.update(pool._threads)

    async def close(self):
        if threading.get_ident() != self._owner:
            raise HostStateError("Browser teardown belongs to its owner thread")
        self.check_loop()
        if self.closed:
            return
        if self._close_task is None or (
            self._close_task.done() and (self._close_task.cancelled() or self._close_task.exception() is not None)
        ):
            self.begin_close()
            self._close_task = asyncio.create_task(self._close_owner())
            self._close_task.add_done_callback(consume_future)
        await asyncio.shield(self._close_task)

    async def _close_owner(self):
        try:
            # JS termination can retire the Worker wrapper even when a host
            # stop failed. Runtime retains cleanup ownership independently.
            with self._lifecycle_lock:
                workers = tuple(self._workers)
                pools = tuple(self._retired_io_pools)
            for pool in pools:
                self._retire_io_pool(pool)
            for worker in workers:
                worker.stop()
            joined = set()
            while True:
                with self._lifecycle_lock:
                    pending = (self._worker_threads | self._io_threads) - joined
                if not pending:
                    break
                for thread in pending:
                    await asyncio.to_thread(thread.join, 5)
                    if thread.is_alive():
                        raise HostStateError("Worker or I/O thread did not finish Runtime teardown")
                    joined.add(thread)
            for executor in tuple(self._executors):
                executor.close()
        except BaseException:
            self._state = RuntimeState.CLOSE_FAILED
            raise
        self._state = RuntimeState.CLOSED
        self._worker_threads.clear()
        self._io_threads.clear()
        self._retired_io_pools.clear()
        self._workers.clear()
        await asyncio.sleep(0)
