"""The cancellable delivery path shared by both built-in HTTP engines."""
import asyncio
import threading
import time
from concurrent.futures import Future, wait
from dataclasses import replace
from threading import RLock

from ..foundation.host_errors import HostStateError, LoadFailure, RequestTimeout
from ..foundation.logging import logger
from .cookies import CookieJar
from .models import permits_credentials, with_cookie_header

_CLOSE_TIMEOUT = 5.0


class _AsyncAbortToken:
    """Interrupt the loop's Task without completing its result Future."""

    def __init__(self):
        self._lock = RLock()
        self._reason = None
        self._task = None

    @property
    def reason(self):
        with self._lock:
            return self._reason

    def attach(self, task):
        with self._lock:
            self._task = task
            if self._reason is not None:
                task.cancel()

    def detach(self):
        with self._lock:
            self._task = None

    def cancel(self):
        self._interrupt("cancelled")

    def expire(self):
        self._interrupt("deadline")

    def _interrupt(self, reason):
        with self._lock:
            task = self._task
            if self._reason is not None or (task is not None and task.done()):
                return
            self._reason = reason
            if task is not None:
                task.get_loop().call_soon_threadsafe(task.cancel)


class AsyncTransport:
    """Transport running cancellable coroutines on a dedicated I/O loop."""

    FOLLOWS_ITSELF = False

    def __init__(self, *, options=None, loop_name="pyv8c-async-loop"):
        self.options = dict(options or {})
        self.cookies = CookieJar()
        self._lock = RLock()
        self._inflight_lock = RLock()
        self._inflight = {}
        self._closed = False
        self._closing = False
        self._failed_close = False
        self.loop = None
        self._thread = None
        self.session = None
        self._loop_started = False
        self._session_closed = False
        self._loop_stopped = False
        self._shutdown = None
        self._stop_requested = False
        # The loop thread starts on the first call, not at construction.
        try:
            loop = self._create_loop()
            self.loop = loop
            self._thread = threading.Thread(
                target=loop.run_forever, daemon=True, name=loop_name)
            self._open_session()
        except BaseException:
            if self.loop is not None:
                self.loop.close()
            raise

    def _create_loop(self):
        return asyncio.new_event_loop()

    def _start_loop(self):
        with self._inflight_lock:
            if not self._loop_started:
                self._thread.start()
                self._loop_started = True

    def _open_session(self):
        """Create ``self.session`` using ``self.options``."""
        raise NotImplementedError

    async def _single_call(self, request):
        """Send one hop. The client must not follow redirects."""
        raise NotImplementedError

    async def _send(self, request):
        request = with_cookie_header(request, self.cookies)
        logger.debug("network download: {} {}", request.method, request.url)
        response = await self._single_call(request)
        set_cookie = response.headers.get("set-cookie")
        if set_cookie and permits_credentials(request):
            self.cookies.add_from_response(response.url, set_cookie)
        return response

    def _map_error(self, request, exc):
        """The failure a transport error becomes; ``None`` keeps it unchanged."""
        return LoadFailure(f"{request.method} {request.url} failed: {exc}")

    def submit_request(self, request, handle, remaining):
        """Enqueue directly on the I/O loop and settle after cleanup."""
        token = _AsyncAbortToken()
        handle.attach_abort(token.cancel)
        future = Future()
        future.set_running_or_notify_cancel()
        # Register under the lock close() uses so its cancel sweep never misses a call.
        with self._inflight_lock:
            if self._closing or self._closed or self._failed_close:
                raise HostStateError("network backend is closed")
            if token.reason is not None:
                raise LoadFailure(f"{request.method} {request.url} was aborted")
            self._start_loop()
            self._inflight[future] = token
            try:
                self.loop.call_soon_threadsafe(self._start_call, request, token, future, remaining)
            except BaseException:
                del self._inflight[future]
                raise
        return future

    def _start_call(self, request, token, future, remaining):
        def settle(task):
            token.detach()
            try:
                result = task.result()
            except asyncio.CancelledError as exc:
                if token.reason == "deadline":
                    error = RequestTimeout("HTTP request exceeded its deadline")
                else:
                    error = LoadFailure(f"{request.method} {request.url} was aborted")
                error.__cause__ = exc
                future.set_exception(error)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                with self._inflight_lock:
                    del self._inflight[future]

        async def run():
            if token.reason is not None:
                raise asyncio.CancelledError
            budget = remaining()
            current = replace(request, timeout=budget)
            deadline = None if budget is None else self.loop.time() + budget
            timer = None if deadline is None else self.loop.call_at(deadline, token.expire)
            try:
                response = await self._send(current)
                if token.reason == "deadline":
                    raise RequestTimeout("HTTP request exceeded its deadline")
                return response
            except RequestTimeout:
                raise
            except Exception as exc:
                if token.reason == "deadline":
                    raise RequestTimeout("HTTP request exceeded its deadline") from exc
                mapped = self._map_error(current, exc)
                if mapped is None:
                    raise
                raise mapped from exc
            finally:
                if timer is not None:
                    timer.cancel()

        coroutine = run()
        try:
            task = self.loop.create_task(coroutine)
        except BaseException as exc:
            coroutine.close()
            future.set_exception(exc)
            with self._inflight_lock:
                del self._inflight[future]
            return
        token.attach(task)
        task.add_done_callback(settle)

    async def _drain(self):
        pending = [task for task in asyncio.all_tasks()
                   if task is not asyncio.current_task()]
        for task in pending:
            if not task.cancelling():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _close_session(self):
        await self.session.close()

    async def _shutdown_session(self):
        if not self._session_closed:
            await self._close_session()
            self._session_closed = True
        await self._drain()
        await self.loop.shutdown_asyncgens()
        await self.loop.shutdown_default_executor()

    def _stop_loop(self):
        if self._loop_stopped:
            return
        deadline = time.monotonic() + _CLOSE_TIMEOUT

        def remaining():
            return max(0, deadline - time.monotonic())

        if not self._stop_requested:
            with self._inflight_lock:
                in_flight = dict(self._inflight)
            for token in in_flight.values():
                token.cancel()
            if in_flight and wait(in_flight, timeout=remaining()).not_done:
                raise TimeoutError("HTTP requests did not finish teardown")

            self._start_loop()
            if self._shutdown is None:
                self._shutdown = asyncio.run_coroutine_threadsafe(self._shutdown_session(), self.loop)
            try:
                self._shutdown.result(remaining())
            except BaseException:
                if self._shutdown.cancelled() or (
                        self._shutdown.done() and self._shutdown.exception() is not None):
                    self._shutdown = None
                raise
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._stop_requested = True
        self._thread.join(remaining())
        if self._thread.is_alive():
            raise TimeoutError("the HTTP event loop did not stop")
        self._loop_stopped = True

    def close(self):
        """Release every resource; a failure is reported and stays retryable."""
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            self._failed_close = False
        try:
            self._stop_loop()
            self.loop.close()
            with self._lock:
                self._closed = True
        finally:
            with self._lock:
                self._closing = False
                self._failed_close = not self._closed


__all__ = ["AsyncTransport"]
