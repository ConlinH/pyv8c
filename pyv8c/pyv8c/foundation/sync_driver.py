"""Persistent event-loop ownership for a synchronous application.

No platform objects or execution state live here. The same-thread Runtime keeps
its existing ownership and deadlines; this driver only observes its coroutines.
"""

import asyncio
import signal
import threading
import time
from contextvars import copy_context
from weakref import WeakValueDictionary, ref

from .event_loop import create_event_loop
from .futures import consume_future
from .host_errors import HostStateError


class SyncDriver:
    def __init__(self, browser, *, before_wait):
        self._browser = ref(browser)
        self._before_wait = before_wait
        self._owner = None
        self._runner = None
        self._entered = False
        self._objects = WeakValueDictionary()

    @staticmethod
    def check_no_loop():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise HostStateError("sync_api cannot run inside an event loop; use async_api")

    def open(self):
        self.check_no_loop()
        if self._entered:
            raise HostStateError("Browser can only be entered once")
        self._owner = threading.get_ident()
        self._runner = asyncio.Runner(loop_factory=create_event_loop)
        self._entered = True

    def check_access(self):
        if threading.get_ident() != self._owner:
            raise HostStateError("Browser APIs belong to their owner thread")
        self.check_no_loop()

    def check(self):
        self.check_access()
        if self._runner is None:
            raise HostStateError("Browser is closed or not entered")

    def call(self, operation, /, *args, **kwargs):
        # Check before constructing a coroutine: rejected calls must not leak it.
        started = time.perf_counter()
        self._check_wait()
        timeout = kwargs.get("timeout")
        deadline = None if timeout is None else started + max(0.0, float(timeout))
        return self._run(operation, args, kwargs, deadline)

    def _run(self, operation, args, kwargs, deadline):
        async def invoke():
            # Ready callbacks can run before this task. Charge that
            # queue time to the same operation budget before starting any work.
            if deadline is not None:
                kwargs["timeout"] = max(0.0, deadline - time.perf_counter())
            return await operation(*args, **kwargs)

        loop = self._runner.get_loop()
        task = None
        interrupted = False

        def interrupt(signum, frame):
            nonlocal interrupted
            if interrupted or (task is not None and task.done()):
                signal.default_int_handler(signum, frame)
            interrupted = True
            if task is not None:
                task.cancel()
                loop.call_soon_threadsafe(lambda: None)

        # Preserve cooperative Ctrl+C with the default handler. Applications
        # with their own signal handling keep their handler.
        main_thread = threading.current_thread() is threading.main_thread()
        install_handler = main_thread and signal.getsignal(signal.SIGINT) is signal.default_int_handler
        try:
            if install_handler:
                try:
                    signal.signal(signal.SIGINT, interrupt)
                except ValueError:
                    # An embedded interpreter may not permit signal registration.
                    install_handler = False
            task = loop.create_task(invoke(), context=copy_context())
            if interrupted and not task.cancelling():
                task.cancel()
            self._drive(loop, task)
            return task.result()
        except BaseException as error:
            if task is None:
                raise
            if not task.done():
                task.cancel()
                # Any shielded cleanup or running transport remains owned by Runtime.
                try:
                    self._drive(loop, task)
                except BaseException as cleanup_error:
                    error.add_note(f"Interrupted while settling the synchronous call: {cleanup_error!r}")
            if task.done():
                consume_future(task)
            else:
                task.add_done_callback(consume_future)
            if isinstance(error, asyncio.CancelledError) and interrupted and task.uncancel() == 0:
                raise KeyboardInterrupt from None
            raise
        finally:
            if install_handler and signal.getsignal(signal.SIGINT) is interrupt:
                signal.signal(signal.SIGINT, signal.default_int_handler)

    @staticmethod
    def _drive(loop, task):
        if task.done():
            return
        active = True

        def stop(_):
            if active:
                loop.stop()

        task.add_done_callback(stop)
        try:
            loop.run_forever()
            if not task.done():
                raise RuntimeError("Event loop stopped before the synchronous call completed")
        finally:
            # Once queued, a done callback cannot be removed from the loop.
            # It must not stop a subsequent call after an interrupted drive.
            active = False
            task.remove_done_callback(stop)

    def _check_wait(self):
        self.check()
        self._before_wait()

    def close_scope(self, start):
        self._check_wait()
        task = start()
        if task is not None:
            self._run(_observe_close, (task,), {}, None)

    def finish(self):
        self.check()
        runner, self._runner = self._runner, None
        runner.close()

    def wrap(self, impl, kind):
        self.check_access()
        wrapper = self._objects.get(impl)
        if wrapper is None:
            wrapper = object.__new__(kind)
            wrapper._impl, wrapper._driver = impl, self
            wrapper._browser = self._browser()
            self._objects[impl] = wrapper
        return wrapper


async def _observe_close(task):
    await asyncio.shield(task)
