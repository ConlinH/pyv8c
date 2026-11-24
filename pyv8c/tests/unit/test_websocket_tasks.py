"""WebSocket owner events, buffering and asynchronous transport teardown."""
import asyncio
import datetime
import importlib.util
import socket
import ssl
import threading
from collections import deque
from pathlib import Path

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from pyv8c.foundation.tasks import TaskSource
from pyv8c.network import websockets as transport_module
from pyv8c.network.proxy import PROXY_OPTION
from pyv8c.network.websocket_io import WebSocketIO
from pyv8c.network.websockets import WebSocketsTransport, _Socket


class RecordingTasks:
    def __init__(self):
        self.pending = deque()
        self.handshakes = 0
        self.completions = []

    def post_task(self, callback, source=TaskSource.DOM, *, document_bound=False):
        self.pending.append((source, document_bound, callback))
        return True

    def begin_network(self):
        self.handshakes += 1

    def complete_network(self):
        self.handshakes -= 1

    def track_websocket(self, future, cancel):
        self.completions.append(future)

    def drain(self):
        while self.pending:
            source, _, callback = self.pending.popleft()
            assert source is TaskSource.NETWORKING
            callback()


async def until(condition):
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.005)


def url(server):
    return f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"


def recorded_socket(tasks, events):
    callbacks = {name: lambda *args, name=name: events.append((name, args[1:]))
                 for name in ("open", "message", "error", "close")}
    return _Socket(None, tasks, "ws://example.test/", [], callbacks, {})


def test_events_use_separate_document_networking_tasks_in_order():
    tasks, events = RecordingTasks(), []
    socket = recorded_socket(tasks, events)
    for name in ("open", "message", "message", "close"):
        socket._deliver(name, socket, "payload")
    assert events == []
    assert len(tasks.pending) == 4
    assert all(bound for _, bound, _ in tasks.pending)
    tasks.drain()
    assert events == [(name, ("payload",)) for name in ("open", "message", "message", "close")]
    assert not socket._callbacks


def test_dispose_suppresses_already_queued_events():
    tasks, events = RecordingTasks(), []
    socket = recorded_socket(tasks, events)
    socket._deliver("open", socket)
    socket.dispose()
    tasks.drain()
    assert events == []


@pytest.mark.parametrize("finished", [False, True])
def test_close_before_open_delivery_fails_without_open_or_message(finished):
    tasks, events = RecordingTasks(), []
    socket = recorded_socket(tasks, events)
    socket._deliver("open", socket)
    socket._deliver("message", socket, "late", True)
    if finished:
        socket._deliver("close", socket, 1000, "remote", True)
    socket.close()
    if not finished:
        socket._deliver("close", socket, 1000, "remote", True)
    for _ in range(3):
        tasks.pending.popleft()[2]()
    assert [name for name, _ in events] == ["error"]
    assert len(tasks.pending) == 1
    tasks.drain()
    assert events[-1] == ("close", (1006, "", False))
    assert not socket._callbacks


def test_pre_open_failure_callback_exception_still_schedules_close():
    tasks, events = RecordingTasks(), []
    socket = recorded_socket(tasks, events)

    def fail(*args):
        raise ValueError("error callback failed")

    socket._callbacks["error"] = fail
    socket._deliver("close", socket, 1000, "", True)
    socket.close()
    with pytest.raises(ValueError, match="error callback failed"):
        tasks.pending.popleft()[2]()
    tasks.drain()
    assert events == [("close", (1006, "", False))]


def test_close_callback_failure_still_releases_callbacks():
    tasks = RecordingTasks()

    def fail(*args):
        raise ValueError("listener failed")

    socket = _Socket(None, tasks, "ws://example.test/", [], {"close": fail}, {})
    socket._deliver("close", socket, 1000, "", True)
    with pytest.raises(ValueError, match="listener failed"):
        tasks.drain()
    assert not socket._callbacks


def test_socket_start_failure_releases_all_registrations(monkeypatch):
    def fail_start(self, lifetime):
        raise RuntimeError("cannot start loop")

    monkeypatch.setattr(_Socket, "start", fail_start)
    engine = WebSocketsTransport()
    tasks = RecordingTasks()
    with pytest.raises(RuntimeError, match="cannot start loop"):
        engine.websocket(tasks, "ws://example.test/", [], {})
    assert not engine._sockets
    completion, = tasks.completions
    assert completion.done()
    engine.close()


async def test_graceful_close_is_fifo_with_pending_sends_and_owner_acknowledges_bytes():
    received = []
    tasks = RecordingTasks()
    engine = WebSocketsTransport()

    async def reader(connection):
        try:
            async for message in connection:
                received.append(message)
        except ConnectionClosed:
            pass
        received.append((connection.close_code, connection.close_reason))

    try:
        async with serve(reader, "127.0.0.1", 0) as server:
            socket = engine.websocket(tasks, url(server), [], {"open": lambda *_: None})
            await until(lambda: tasks.pending)
            tasks.drain()
            socket.send("中")
            socket.send_bytes(b"ab")
            socket.close(4001, "done")
            socket.close(4002, "again")
            await asyncio.wrap_future(socket.completion)
            assert not engine._sockets
            assert tasks.handshakes == 0
            await until(lambda: len(received) == 3)
            assert received == ["中", b"ab", (4001, "done")]
            assert socket.buffered_amount == 5
            assert [bound for _, bound, _ in tasks.pending] == [False]
            tasks.drain()
            assert socket.buffered_amount == 0
    finally:
        await asyncio.to_thread(engine.close)


@pytest.mark.parametrize("limit", ["messages", "bytes"])
async def test_sent_messages_release_admission_before_owner_acknowledgement(monkeypatch, limit):
    tasks, received = RecordingTasks(), []
    engine = WebSocketsTransport()

    async def reader(connection):
        async for message in connection:
            received.append(message)

    monkeypatch.setattr(transport_module, "_MAX_PENDING_MESSAGES", 1 if limit == "messages" else 32)
    monkeypatch.setattr(transport_module, "_MAX_PENDING_BYTES", 32 if limit == "messages" else 3)
    try:
        async with serve(reader, "127.0.0.1", 0) as server:
            socket = engine.websocket(tasks, url(server), [], {"open": lambda *_: None})
            await until(lambda: tasks.pending)
            tasks.drain()
            for count, data in enumerate(("中", b"abc", "字"), 1):
                if isinstance(data, str):
                    socket.send(data)
                else:
                    socket.send_bytes(data)
                await until(lambda: socket._sent_bytes == count * 3)
                assert socket.buffered_amount == count * 3
            await until(lambda: len(received) == 3)
            assert received == ["中", b"abc", "字"]
            assert [bound for _, bound, _ in tasks.pending] == [False]
            tasks.drain()
            assert socket.buffered_amount == 0
    finally:
        await asyncio.to_thread(engine.close)


async def test_handshake_and_connection_limits_release_on_cancellation(monkeypatch):
    dialed = []
    tasks, rejected = RecordingTasks(), []

    async def dial(address, **kwargs):
        dialed.append(address)
        await asyncio.Future()

    monkeypatch.setattr(transport_module, "_connect_client", lambda: dial)
    monkeypatch.setattr(transport_module, "_MAX_HANDSHAKES", 1)
    monkeypatch.setattr(transport_module, "_MAX_CONNECTIONS", 2)
    engine = WebSocketsTransport()
    try:
        first = engine.websocket(tasks, "ws://first.test/", [], {})
        second = engine.websocket(tasks, "ws://second.test/", [], {})
        third = engine.websocket(tasks, "ws://third.test/", [], {"error": lambda *args: rejected.append(args)})
        await until(lambda: dialed)
        assert dialed == ["ws://first.test/"]
        assert third.completion.done()
        tasks.drain()
        assert len(rejected) == 1
        engine.close_websocket(first)
        await asyncio.wrap_future(first.completion)
        await until(lambda: len(dialed) == 2)
        assert dialed[-1] == "ws://second.test/"
        engine.close_websocket(second)
        await asyncio.wrap_future(second.completion)
        assert not engine._sockets and tasks.handshakes == 0
    finally:
        await asyncio.to_thread(engine.close)


@pytest.mark.parametrize("limit", ["messages", "bytes"])
async def test_full_send_queue_aborts_once_with_unsent_bytes(monkeypatch, limit):
    tasks, events = RecordingTasks(), []
    engine = WebSocketsTransport()

    async def block(*args):
        await asyncio.Future()

    async def reader(connection):
        await connection.wait_closed()

    monkeypatch.setattr(ClientConnection, "send", block)
    monkeypatch.setattr(transport_module, "_MAX_PENDING_MESSAGES", 2 if limit == "messages" else 32)
    monkeypatch.setattr(transport_module, "_MAX_PENDING_BYTES", 32 if limit == "messages" else 6)
    try:
        async with serve(reader, "127.0.0.1", 0) as server:
            socket = engine.websocket(tasks, url(server), [], {
                name: lambda *args, name=name: events.append((name, args[1:]))
                for name in ("open", "error", "close")
            })
            await until(lambda: tasks.pending)
            tasks.drain()
            for _ in range(4):
                socket.send("中")
            await asyncio.wrap_future(socket.completion)
            tasks.drain()
            assert [name for name, _ in events] == ["open", "error", "close"]
            assert isinstance(events[1][1][0], BufferError)
            assert events[2][1] == (1006, "", False)
            assert socket.buffered_amount == 12
    finally:
        await asyncio.to_thread(engine.close)


@pytest.mark.parametrize("limit", ["messages", "bytes"])
async def test_slow_owner_applies_receive_backpressure_in_order(monkeypatch, limit):
    tasks, received = RecordingTasks(), []
    engine = WebSocketsTransport()
    messages = ["中", "二", "三", "四", "五", "六"]

    async def send_burst(connection):
        for message in messages:
            await connection.send(message)
        await connection.wait_closed()

    monkeypatch.setattr(transport_module, "_MAX_PENDING_MESSAGES", 2 if limit == "messages" else 32)
    monkeypatch.setattr(transport_module, "_MAX_PENDING_BYTES", 32 if limit == "messages" else 6)
    try:
        async with serve(send_burst, "127.0.0.1", 0) as server:
            socket = engine.websocket(tasks, url(server), [], {
                "open": lambda *_: None,
                "message": lambda _, message, is_text: received.append(message),
            })
            await until(lambda: socket._read_paused)
            assert len(tasks.pending) == 3  # open plus exactly two messages
            assert socket._pending_read_bytes == 6
            while len(received) < len(messages):
                await until(lambda: tasks.pending)
                tasks.drain()
            assert received == messages
            engine.close_websocket(socket)
            await asyncio.wrap_future(socket.completion)
            assert socket._pending_reads == socket._pending_read_bytes == 0
    finally:
        await asyncio.to_thread(engine.close)


def test_connect_kwargs_keep_a_caller_connection_class():
    marker = type("Marker", (), {})
    engine = WebSocketsTransport(options={"create_connection": marker})
    try:
        kwargs = engine._connect_kwargs("wss://example.test/socket", None)
    finally:
        engine.close()
    assert kwargs["create_connection"] is marker


def test_connect_kwargs_use_a_connection_that_tolerates_an_unopened_handshake():
    engine = WebSocketsTransport()
    try:
        kind = engine._connect_kwargs("wss://example.test/socket", None)["create_connection"]
    finally:
        engine.close()
    assert issubclass(kind, ClientConnection)
    assert kind is not ClientConnection


def _recording_proxy():
    path = Path(__file__).resolve().parents[1] / "integration" / "proxy_harness.py"
    spec = importlib.util.spec_from_file_location("pyv8c_test_proxy_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RecordingProxy()


def _self_signed_context(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


class _TlsPort:
    """One localhost TLS listener. The client is expected to reject the certificate."""

    def __init__(self, context):
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, args=(context,), daemon=True)
        self._thread.start()

    def close(self):
        self._socket.close()
        self._thread.join(timeout=2)

    def _serve(self, context):
        try:
            client, _address = self._socket.accept()
        except OSError:
            return
        try:
            tls = context.wrap_socket(client, server_side=True)
            tls.recv(1)
            tls.close()
        except OSError:
            pass
        finally:
            client.close()


async def test_a_proxied_wss_certificate_failure_stays_a_certificate_error(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    callback_errors = []
    original_start = WebSocketIO.start

    def start(self):
        loop = original_start(self)
        loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))
        return loop

    monkeypatch.setattr(WebSocketIO, "start", start)
    proxy = _recording_proxy()
    server = _TlsPort(_self_signed_context(tmp_path))
    tasks, errors = RecordingTasks(), []
    engine = WebSocketsTransport(options={PROXY_OPTION: proxy.url})
    try:
        socket = engine.websocket(
            tasks, f"wss://127.0.0.1:{server.port}/", [],
            {"error": lambda _socket, error: errors.append(error), "close": lambda *_args: None},
        )
        async with asyncio.timeout(5):
            await asyncio.wrap_future(socket.completion)
        tasks.drain()
        assert proxy.tunnels == [f"127.0.0.1:{server.port}"]
        assert callback_errors == []
        error, = errors
        assert isinstance(error, ssl.SSLCertVerificationError)
    finally:
        await asyncio.to_thread(engine.close)
        proxy.close()
        server.close()
