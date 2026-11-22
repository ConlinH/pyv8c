"""The default WebSocket engine. Protocol I/O stays off the Runtime owner."""
import asyncio
import importlib
import inspect
import time
from collections import deque
from concurrent.futures import Future, wait
from contextvars import Context
from functools import cache
from threading import RLock

from ..foundation.logging import logger
from ..foundation.tasks import TaskSource
from ..foundation.urls import header_set as _header_set
from .models import NetworkUnavailableError
from .proxy import PROXY_OPTION, websockets_proxy
from .websocket_io import TEARDOWN_TIMEOUT, WebSocketIO

_BRIDGE_KEYS = frozenset({"cookies", "headers", PROXY_OPTION})
_MANAGED_KEYS = frozenset({"uri", "subprotocols", "additional_headers", "logger", "origin"})
# asyncio passes these through **kwargs to its connection constructors.
_SOCKET_KEYS = frozenset({"sock", "ssl", "server_hostname", "proxy_ssl", "proxy_server_hostname"})
_MAX_CONNECTIONS = 256
_MAX_HANDSHAKES = 32
_MAX_PENDING_MESSAGES = 256
_MAX_PENDING_BYTES = 16 * 1024 * 1024
_OWNER_CLOSE_GRACE = 0.5
_ABNORMAL_CLOSE = (1006, "", False)


@cache
def _connect_client():
    try:
        client = importlib.import_module("websockets.asyncio.client")
    except ImportError as exc:
        raise NetworkUnavailableError(
            "the websockets network engine requires the 'websockets' package"
        ) from exc

    class BrowserConnect(client.connect):
        def process_redirect(self, exc):
            # Fetch uses redirect mode "error" for WebSocket handshakes.
            # In particular, never forward this handshake's Origin or cookies.
            return exc

    return BrowserConnect


@cache
def _connection_type():
    """A client connection whose closing callback survives a failed TLS handshake."""
    client = importlib.import_module("websockets.asyncio.client")

    class ClientConnection(client.ClientConnection):
        def connection_lost(self, exc):
            # A proxied wss:// calls start_tls before connection_made. The
            # library then closes recv_messages, which only connection_made
            # creates, so a certificate failure crashes the event-loop callback.
            if not hasattr(self, "recv_messages"):
                self.set_connection_exc(exc)
                if self.keepalive_task is not None:
                    self.keepalive_task.cancel()
                    self.keepalive_task = None
                if not self.connection_lost_waiter.done():
                    self.connection_lost_waiter.set_result(None)
                return
            super().connection_lost(exc)

    return ClientConnection


def _http_url(url):
    return "http" + url[2:]


def _size(message):
    if isinstance(message, str) and not message.isascii():
        return len(message.encode("utf-8"))
    return len(message)


def _close_details(connection):
    received = connection.protocol.close_rcvd
    if received is None:
        return _ABNORMAL_CLOSE
    return int(received.code), received.reason, connection.protocol.close_sent is not None


class _Socket:
    """One document's socket. Only the I/O loop touches the protocol connection."""

    def __init__(self, engine, tasks, url, protocols, callbacks, connect_kwargs):
        self._engine = engine
        self._tasks = tasks
        self._url = url
        self._protocols = protocols
        self._callbacks = dict(callbacks)
        self._connect_kwargs = connect_kwargs
        self.completion = Future()
        self.completion.set_running_or_notify_cancel()
        self.protocol = ""
        self._lock = RLock()
        self._disposed = False
        self._open_delivered = False
        self._error_delivered = False
        self._cancelled_before_open = False
        self._close_requested = None
        self._overflow = None
        self._writes = deque()
        self._queued_write_bytes = 0
        self._buffered_amount = self._sent_bytes = 0
        self._writing = None
        self._pending_reads = self._pending_read_bytes = 0
        self._read_paused = False
        # The remaining fields are I/O loop state, initialized before publication.
        self._loop = self._task = self._connection = None
        self._lifetime = None
        self._writable = self._readable = None
        self._retirement_timer = None
        self._interrupted = self._cleaning = False
        self._handshake_pending = False

    @property
    def buffered_amount(self):
        with self._lock:
            return self._buffered_amount

    def send(self, message):
        self._send(message)

    def send_bytes(self, message):
        self._send(message)

    def _send(self, message):
        size = _size(message)
        with self._lock:
            # Each non-throwing send contributes to the owner-visible amount.
            self._buffered_amount += size
            if self._disposed or not self._open_delivered or self._close_requested or self._loop is None:
                return
            if self._overflow is not None:
                return
            if (len(self._writes) + (self._writing is not None) >= _MAX_PENDING_MESSAGES or
                    self._queued_write_bytes + (self._writing or 0) + size > _MAX_PENDING_BYTES):
                self._overflow = BufferError("WebSocket send queue is full")
                self._signal()
                return
            wake = not self._writes
            self._writes.append((message, size))
            self._queued_write_bytes += size
            if wake:
                self._loop.call_soon_threadsafe(self._writable.set, context=Context())

    def close(self, code=1000, reason=""):
        with self._lock:
            if self._disposed or self._close_requested is not None:
                return
            self._close_requested = (int(code), str(reason))
            self._cancelled_before_open = not self._open_delivered
            self._signal()

    def dispose(self):
        """Revoke callbacks immediately and interrupt this socket on its I/O loop."""
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
            self._callbacks.clear()
            self._writes.clear()
            self._queued_write_bytes = 0
            self._signal()

    def _signal(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._apply_commands, context=Context())

    def _apply_commands(self):
        if self._task is None or self._task.done() or self._interrupted or self._cleaning:
            return
        with self._lock:
            disposed = self._disposed
            stop = (self._overflow is not None or self._cancelled_before_open or
                    (disposed and (self._connection is None or self._writing is not None)))
        if stop:
            self._interrupt()
            return
        if disposed and self._retirement_timer is None:
            self._retirement_timer = self._loop.call_later(_OWNER_CLOSE_GRACE, self._interrupt)
            self._readable.set()
        self._writable.set()

    def _interrupt(self):
        if self._task is not None and not self._task.done() and not (self._interrupted or self._cleaning):
            self._interrupted = True
            self._task.cancel()

    def start(self, lifetime):
        with self._lock:
            self._lifetime = lifetime
            if self._disposed or (lifetime is not None and lifetime.closed):
                self.dispose()
                self._finish(None, _ABNORMAL_CLOSE)
                return
            self._loop = self._engine._io.start()
            self._tasks.begin_network()
            self._handshake_pending = True
            try:
                self._loop.call_soon_threadsafe(self._start_on_loop, context=Context())
            except BaseException:
                self._complete_handshake()
                raise

    def _start_on_loop(self):
        self._writable, self._readable = asyncio.Event(), asyncio.Event()
        coroutine = self._run()
        try:
            self._task = self._loop.create_task(coroutine)
        except Exception as error:
            coroutine.close()
            self._finish(error, _ABNORMAL_CLOSE)
            return
        self._task.add_done_callback(self._settle)
        self._apply_commands()

    def _complete_handshake(self):
        if self._handshake_pending:
            self._handshake_pending = False
            self._tasks.complete_network()

    async def _open(self):
        async with asyncio.timeout(self._connect_kwargs.get("open_timeout", 10)):
            async with self._engine._handshakes:
                with self._lock:
                    if (self._disposed or self._cancelled_before_open or
                            (self._lifetime is not None and self._lifetime.closed)):
                        raise asyncio.CancelledError
                connector = _connect_client()(
                    self._url, subprotocols=self._protocols or None, **self._connect_kwargs)
                try:
                    self._connection = await connector
                finally:
                    if self._connection is None:
                        # websockets aborts a partially opened connection before raising.
                        # Join its connection_lost callback before settling ownership.
                        self._connection = getattr(connector, "connection", None)

    async def _run(self):
        pumps = ()
        try:
            await self._open()
            connection = self._connection
            self.protocol = connection.subprotocol or ""
            self._deliver("open", self)
            self._complete_handshake()
            reader = self._loop.create_task(self._read(connection))
            writer = self._loop.create_task(self._write(connection))
            pumps = (reader, writer)
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            if writer in done:
                writer.result()
            await reader
        finally:
            self._cleaning = True
            connection = self._connection
            if self._retirement_timer is not None:
                self._retirement_timer.cancel()
            if connection is not None and connection.close_code is None:
                connection.transport.abort()
            for pump in pumps:
                if not pump.done():
                    pump.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            if connection is not None:
                await connection.wait_closed()
                if connection.keepalive_task is not None:
                    await asyncio.gather(connection.keepalive_task, return_exceptions=True)

    async def _write(self, connection):
        while True:
            await self._writable.wait()
            with self._lock:
                if self._disposed:
                    close = self._close_requested or (1000, "owner closed")
                elif self._writes:
                    close = None
                    message, size = self._writes.popleft()
                    self._queued_write_bytes -= size
                    self._writing = size
                elif self._close_requested:
                    close = self._close_requested
                else:
                    self._writable.clear()
                    continue
            if close is not None:
                await connection.close(*close)
                return
            sent_bytes = 0
            try:
                await connection.send(message)
                sent_bytes = size
            finally:
                self._write_finished(sent_bytes)
            del message

    def _write_finished(self, size):
        # Release admission on the I/O thread. The owner publishes the
        # completed bytes together at its next acknowledgement task.
        with self._lock:
            self._writing = None
            notify = size > 0 and self._sent_bytes == 0
            self._sent_bytes += size
        if notify:
            # This is bookkeeping on the socket, even if its document was replaced.
            self._tasks.post_task(self._acknowledge, TaskSource.NETWORKING)

    def _acknowledge(self):
        with self._lock:
            self._buffered_amount -= self._sent_bytes
            self._sent_bytes = 0

    async def _read(self, connection):
        while True:
            message = await connection.recv()
            size = _size(message)
            if not await self._reserve_message(size):
                continue
            self._deliver("message", self, message, isinstance(message, str), message_size=size)
            del message

    async def _reserve_message(self, size):
        if size > _MAX_PENDING_BYTES:
            raise BufferError("WebSocket message exceeds the receive budget")
        while True:
            with self._lock:
                if self._disposed:
                    return False
                if (self._pending_reads < _MAX_PENDING_MESSAGES and
                        self._pending_read_bytes + size <= _MAX_PENDING_BYTES):
                    self._pending_reads += 1
                    self._pending_read_bytes += size
                    return True
                self._read_paused = True
                self._readable.clear()
            await self._readable.wait()

    def _release_message(self, size):
        with self._lock:
            self._pending_reads -= 1
            self._pending_read_bytes -= size
            if self._read_paused and not self._disposed and self._loop is not None:
                self._read_paused = False
                self._loop.call_soon_threadsafe(self._readable.set, context=Context())

    def _settle(self, task):
        """Publish terminal state only after reader, writer, and transport exited."""
        self._task = None
        connection, self._connection = self._connection, None
        error = None
        details = _ABNORMAL_CLOSE
        try:
            task.result()
        except asyncio.CancelledError:
            error = self._overflow or ConnectionAbortedError("WebSocket opening was cancelled")
        except Exception as exc:
            from websockets.exceptions import ConnectionClosed
            if self._handshake_pending or not isinstance(exc, ConnectionClosed):
                error = exc
            else:
                details = _close_details(connection)
                if not details[2]:
                    error = exc
        else:
            details = _close_details(connection)
        self._finish(error, details)

    def _finish(self, error, details):
        try:
            if error is not None:
                logger.debug("websocket on {} failed: {!r}", self._url, error)
                self._deliver("error", self, error)
            self._deliver("close", self, *details)
        except Exception:
            with self._lock:
                self._callbacks.clear()
            logger.exception("failed to post WebSocket terminal events")
        finally:
            self._complete_handshake()
            with self._lock:
                self._writes.clear()
                self._queued_write_bytes = 0
                self._connect_kwargs = None
                self._lifetime = None
                self._loop = self._writable = self._readable = self._retirement_timer = None
            engine, self._engine = self._engine, None
            engine._discard_socket(self)
            self.completion.set_result(None)

    def _deliver(self, name, *arguments, message_size=None):
        """One owner task per event, with a microtask checkpoint between events."""
        with self._lock:
            wanted = name in self._callbacks
        if not wanted:
            if message_size is not None:
                self._release_message(message_size)
            return

        def deliver():
            try:
                self._handle_event(name, arguments)
            finally:
                if message_size is not None:
                    self._release_message(message_size)

        try:
            posted = self._tasks.post_task(deliver, TaskSource.NETWORKING, document_bound=True)
        except BaseException:
            if message_size is not None:
                self._release_message(message_size)
            raise
        if posted is None:
            if message_size is not None:
                self._release_message(message_size)
            if name == "close":
                with self._lock:
                    self._callbacks.clear()

    def _handle_event(self, name, arguments):
        with self._lock:
            cancelled = self._cancelled_before_open
            if cancelled and name in ("open", "message"):
                return
            handler = self._callbacks.get(name)
            if handler is None:
                return
            if name == "open":
                self._open_delivered = True
            if name == "error":
                self._error_delivered = True
            inject_error = name == "close" and cancelled and not self._error_delivered
            error_handler = self._callbacks.get("error") if inject_error else None
            if inject_error:
                self._error_delivered = True
        if name == "close" and cancelled:
            arguments = (self, *_ABNORMAL_CLOSE)
        if inject_error:
            # close() may run after transport completion but before the open event.
            try:
                if error_handler is not None:
                    error_handler(self, ConnectionAbortedError("WebSocket closed before open delivery"))
            finally:
                self._deliver("close", *arguments)
            return
        try:
            handler(*arguments)
        finally:
            if name == "close":
                with self._lock:
                    self._callbacks.clear()


class WebSocketsTransport:
    """One Profile's handshakes and connections on the Browser's shared I/O loop."""

    @classmethod
    def accepted_option_keys(cls):
        parameters = inspect.signature(_connect_client()).parameters
        named = {
            name for name, parameter in parameters.items()
            if parameter.kind not in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL, parameter.POSITIONAL_ONLY)
        }
        return (frozenset(named) - _MANAGED_KEYS) | _BRIDGE_KEYS | _SOCKET_KEYS

    def __init__(self, *, options=None):
        # A missing dependency is a configuration failure even before opening a socket.
        _connect_client()
        self.options = dict(options or {})
        self.cookies = None
        self._io = WebSocketIO()
        self._owns_io = True
        self._handshakes = asyncio.Semaphore(_MAX_HANDSHAKES)
        self._sockets = set()
        self._closed = False
        self._lock = RLock()

    def _share_io(self, io):
        with self._lock:
            if self._io is io:
                return
            if self._io.started or self._closed:
                raise RuntimeError("WebSocket transport already used its own I/O loop")
            self._io, self._owns_io = io, False

    def websocket(self, tasks, url, protocols, callbacks, options=None):
        with self._lock:
            if self._closed:
                raise NetworkUnavailableError("this WebSocket engine is closed")
            url = str(url)
            socket = _Socket(self, tasks, url, list(protocols or []), callbacks,
                             self._connect_kwargs(url, options))
            if len(self._sockets) >= _MAX_CONNECTIONS:
                socket._finish(NetworkUnavailableError("Profile WebSocket connection limit reached"),
                               _ABNORMAL_CLOSE)
                return socket
            self._sockets.add(socket)
            try:
                lifetime = tasks.track_websocket(socket.completion, socket.dispose)
                socket.start(lifetime)
            except BaseException:
                self._sockets.discard(socket)
                socket.dispose()
                socket.completion.set_result(None)
                raise
        return socket

    def _discard_socket(self, socket):
        with self._lock:
            self._sockets.discard(socket)

    def close_websocket(self, websocket):
        websocket.dispose()

    def _connect_kwargs(self, url, given):
        call = dict(given or {})
        kwargs = {name: value for name, value in self.options.items() if name not in _BRIDGE_KEYS}
        # A direct Profile must not pick up an environment proxy.
        kwargs["proxy"] = websockets_proxy(self.options.get(PROXY_OPTION))
        if kwargs.get("max_size") is None:
            kwargs["max_size"] = _MAX_PENDING_BYTES if "max_size" in kwargs else 1024 * 1024
        else:
            size = kwargs["max_size"]
            if isinstance(size, tuple):
                message, fragment = size
                limit = _MAX_PENDING_BYTES if message is None else min(message, _MAX_PENDING_BYTES)
                kwargs["max_size"] = (limit, fragment)
            else:
                kwargs["max_size"] = min(size, _MAX_PENDING_BYTES)
        queue = kwargs.get("max_queue")
        if isinstance(queue, tuple):
            high, low = queue
            kwargs["max_queue"] = (16 if high is None else high, low)
        elif queue is None:
            kwargs["max_queue"] = 16
        user_agent, headers = self._handshake_headers(url, call)
        if user_agent is not None:
            kwargs.setdefault("user_agent_header", user_agent)
        if headers:
            kwargs["additional_headers"] = headers
        kwargs.setdefault("create_connection", _connection_type())
        return kwargs

    def _handshake_headers(self, url, call):
        """Page headers followed by profile options and its cookie policy."""
        headers = {}
        user_agent = None
        for layer in (call.get("headers"), self.options.get("headers")):
            for name, value in dict(layer or {}).items():
                if str(name).lower() == "user-agent":
                    user_agent = str(value)
                else:
                    _header_set(headers, name, value)
        if call.get("origin"):
            _header_set(headers, "Origin", str(call["origin"]))
        if self.options.get("cookies", True) and self.cookies is not None:
            cookie = self.cookies.header_for(
                _http_url(url), site_for_cookies=call.get("site_for_cookies", call.get("origin", "null")))
            if cookie:
                _header_set(headers, "Cookie", cookie)
        return user_agent, headers

    def close(self):
        deadline = time.monotonic() + TEARDOWN_TIMEOUT
        with self._lock:
            self._closed = True
            sockets = tuple(self._sockets)
        for socket in sockets:
            socket.dispose()
        if sockets and wait([socket.completion for socket in sockets],
                            timeout=max(0, deadline - time.monotonic())).not_done:
            raise TimeoutError("WebSocket connections did not finish teardown")
        if self._owns_io:
            self._io.close()


__all__ = ["WebSocketsTransport"]
