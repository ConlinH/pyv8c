"""The lazy I/O loop shared by a Browser's built-in WebSocket transports."""
import asyncio
import threading
import time

from ..foundation.host_errors import HostStateError

TEARDOWN_TIMEOUT = 5


class WebSocketIO:
    def __init__(self):
        self._lock = threading.Lock()
        self._loop = None
        self._thread = None
        self._shutdown = None
        self._stopping = False
        self._closing = False

    @property
    def started(self):
        with self._lock:
            return self._loop is not None

    def start(self):
        with self._lock:
            if self._closing:
                raise HostStateError("WebSocket I/O is closed")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever, name="pyv8c-websocket-io", daemon=True)
                try:
                    thread.start()
                except BaseException:
                    loop.close()
                    raise
                self._loop, self._thread = loop, thread
            return self._loop

    async def _drain(self, loop):
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            if not task.cancelling():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await loop.shutdown_asyncgens()
        # DNS may have entered the platform resolver before a connect was cancelled.
        await loop.shutdown_default_executor()

    def close(self):
        """Called off the Runtime owner, after the transports have closed."""
        deadline = time.monotonic() + TEARDOWN_TIMEOUT
        with self._lock:
            self._closing = True
            loop = self._loop
            if loop is None:
                return
            if self._shutdown is None:
                self._shutdown = asyncio.run_coroutine_threadsafe(self._drain(loop), loop)
            self._shutdown.result(max(0, deadline - time.monotonic()))
            if not self._stopping:
                self._stopping = True
                loop.call_soon_threadsafe(loop.stop)
            self._thread.join(max(0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise TimeoutError("WebSocket I/O loop did not stop")
            loop.close()
            self._loop = self._thread = self._shutdown = None
