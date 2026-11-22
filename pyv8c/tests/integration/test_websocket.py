"""``new WebSocket`` on the real ``websockets`` engine.

The unit tests drive the page's socket API with a fixture engine; these run a
real server, because what a socket engine is for is exactly what a fixture would
hide: the handshake a browser sends, the frames that cross the wire, and the
close handshake both sides have to agree on.
"""

import asyncio
import threading
import time
import base64
import hashlib
import socket
import ssl
import datetime
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytest.importorskip("websockets", reason="the default socket engine needs websockets")

from websockets.sync.server import serve

from pyv8c.async_api import Browser


def document(url, request):
    """Serve the page itself; the sockets are the point of these tests."""
    return {"body": "<body></body>", "headers": {"content-type": "text/html"}}


async def wait_for(page, expression, timeout=5):
    """Turn the page's loop until *expression* holds, or give up.

    An open socket is not pending work, so a message arriving on one cannot be
    waited for by an idle wait alone.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        (await page.run_until_idle(timeout=0.1))
        if page.evaluate(expression):
            return True
    return False


class EchoServer:
    """A WebSocket server that echoes, and remembers what it was asked."""

    def __init__(self, subprotocols=None):
        self.handshakes = []
        self.handshake_ready = threading.Event()
        self.closed = []
        self.close_ready = threading.Event()
        self._server = serve(self._handler, "127.0.0.1", 0, subprotocols=subprotocols)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/socket"

    def _serve(self):
        try:
            self._server.serve_forever()
        except Exception:  # shutdown closes the socket underneath it
            pass

    def _handler(self, connection):
        self.handshakes.append(dict(connection.request.headers))
        self.handshake_ready.set()
        try:
            for message in connection:
                connection.send(message)
        finally:
            self.closed.append(connection.close_code)
            self.close_ready.set()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def echo():
    with EchoServer() as server:
        yield server


def test_sync_browser_drives_socket_events_and_waits_for_shutdown(echo):
    from pyv8c.sync_api import Browser as SyncBrowser

    with SyncBrowser(http_engine=None) as browser:
        page = browser.new_page()
        page.evaluate(f"""
            globalThis.result = null;
            globalThis.ws = new WebSocket('{echo.url}');
            ws.onopen = () => ws.send('sync');
            ws.onmessage = event => result = event.data;
            void 0;
        """)
        page.wait_for('result === "sync"', timeout=5)
        assert page.evaluate("result") == "sync"
    assert echo.close_ready.wait(1)
    assert echo.closed == [1000]


async def test_a_page_socket_talks_to_a_real_server(echo):
    browser = (await Browser(ws_engine="websockets", resource_loader=document).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto("http://example.test/")
        page.evaluate(f"""
            window.__events = [];
            const ws = new WebSocket('{echo.url}');
            ws.onopen = () => {{ window.__events.push(['open', ws.protocol]); ws.send('hello'); }};
            ws.onmessage = e => {{ window.__events.push(['message', e.data, typeof e.data]); ws.close(1000, 'done'); }};
            ws.onclose = e => window.__events.push(['close', e.code, e.reason, e.wasClean]);
            window.__ws = ws;
        """)
        assert (await wait_for(page, "__events.length >= 3")), page.evaluate("__events")
        assert page.evaluate("__events") == [
            ["open", ""], ["message", "hello", "string"], ["close", 1000, "done", True],
        ]
        assert page.evaluate("__ws.readyState") == 3
        assert not page.services.websocket_services._sockets
    finally:
        (await browser.close())


async def test_binary_frames_survive_the_round_trip(echo):
    browser = (await Browser(ws_engine="websockets", resource_loader=document).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto("http://example.test/")
        # A Blob built from a string carries its bytes; a Blob or a send built
        # from a bare typed array does not -- ``bytes_from_value`` falls through
        # to ``bytes(value)``, which iterates a JavaScript object: its indices,
        # not its elements.  That defect is the Blob constructor's, not this
        # engine's, and it is not what these tests are about.
        page.evaluate(f"""
            window.__events = [];
            const ws = new WebSocket('{echo.url}');
            ws.binaryType = 'arraybuffer';
            ws.onopen = () => ws.send(new Blob(['abc']));
            ws.onmessage = e => window.__events.push([e.data instanceof ArrayBuffer, Array.from(new Uint8Array(e.data))]);
            window.__ws = ws;
        """)
        assert (await wait_for(page, "__events.length >= 1")), page.evaluate("__events")
        assert page.evaluate("__events") == [[True, [97, 98, 99]]]
    finally:
        (await browser.close())


async def test_a_handshake_carries_the_browser_headers_and_the_jar(echo):
    browser = (await Browser(ws_engine="websockets", resource_loader=document).__aenter__())
    try:
        context = browser.new_profile()
        origin = 'http://' + urlsplit(echo.url).netloc
        page = context.new_page()
        await page.goto(origin + '/')
        # A page sets cookies for its own host only, so the jar is filled the way
        # a response from the socket's host would have filled it.
        context.cookies.set_from_document(echo.url.replace("ws://", "http://"), "sid=1; path=/")
        page.evaluate(f"window.__ws = new WebSocket('{echo.url}')")
        assert (await wait_for(page, "__ws.readyState === 1")), page.evaluate("__ws.readyState")
        assert (await asyncio.to_thread(echo.handshake_ready.wait, 5))
        headers = echo.handshakes[-1]
        # A browser's handshake, not Python's: the document's origin, the
        # environment's headers and the context's cookies for the target.
        assert headers["origin"] == origin
        assert headers["user-agent"].startswith("Mozilla/5.0")
        assert "zh-CN" in headers["accept-language"]
        assert headers["cookie"] == "sid=1"
    finally:
        (await browser.close())


async def test_cross_site_websocket_omits_lax_cookie(echo):
    browser = (await Browser(ws_engine='websockets', resource_loader=document).__aenter__())
    try:
        context = browser.new_profile()
        page = context.new_page()
        await page.goto('http://example.test/')
        context.cookies.set_from_document(echo.url.replace('ws://', 'http://'), 'sid=1; path=/; SameSite=Lax')
        page.evaluate(f"window.__ws = new WebSocket('{echo.url}')")
        assert (await wait_for(page, '__ws.readyState === 1'))
        assert (await asyncio.to_thread(echo.handshake_ready.wait, 5))
        assert 'cookie' not in echo.handshakes[-1]
    finally:
        (await browser.close())


async def test_the_handshake_can_leave_the_cookies_behind(echo):
    browser = (await Browser(
        ws_engine="websockets", ws_options={"cookies": False}, resource_loader=document,
    ).__aenter__())
    try:
        context = browser.new_profile()
        page = context.new_page()
        await page.goto("http://example.test/")
        context.cookies.set_from_document(echo.url.replace("ws://", "http://"), "sid=1; path=/")
        page.evaluate(f"window.__ws = new WebSocket('{echo.url}')")
        assert (await wait_for(page, "__ws.readyState === 1")), page.evaluate("__ws.readyState")
        assert (await asyncio.to_thread(echo.handshake_ready.wait, 5))
        assert "cookie" not in echo.handshakes[-1]
    finally:
        (await browser.close())


async def test_extra_handshake_headers_come_from_the_options(echo):
    browser = (await Browser(
        ws_engine="websockets", ws_options={"headers": {"X-Page": "socket"}}, resource_loader=document,
    ).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto("http://example.test/")
        page.evaluate(f"window.__ws = new WebSocket('{echo.url}')")
        assert (await wait_for(page, "__ws.readyState === 1")), page.evaluate("__ws.readyState")
        assert (await asyncio.to_thread(echo.handshake_ready.wait, 5))
        assert echo.handshakes[-1]["x-page"] == "socket"
    finally:
        (await browser.close())


async def test_a_subprotocol_is_negotiated():
    with EchoServer(subprotocols=["chat"]) as server:
        browser = (await Browser(ws_engine="websockets", resource_loader=document).__aenter__())
        try:
            page = browser.new_profile().new_page()
            await page.goto("http://example.test/")
            page.evaluate(f"window.__ws = new WebSocket('{server.url}', ['chat'])")
            assert (await wait_for(page, "__ws.readyState === 1")), page.evaluate("__ws.readyState")
            assert page.evaluate("__ws.protocol") == "chat"
        finally:
            (await browser.close())


async def test_a_socket_that_cannot_connect_reports_error_then_close():
    browser = (await Browser(ws_engine="websockets", resource_loader=document).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto("http://example.test/")
        page.evaluate("""
            window.__events = [];
            const ws = new WebSocket('ws://127.0.0.1:1/');
            ws.onerror = () => window.__events.push('error');
            ws.onclose = e => window.__events.push(['close', e.code, e.wasClean]);
            window.__ws = ws;
        """)
        assert (await wait_for(page, "__events.length >= 2")), page.evaluate("__events")
        assert page.evaluate("__events") == ["error", ["close", 1006, False]]
    finally:
        (await browser.close())


async def test_closing_the_context_closes_the_socket(echo):
    browser = (await Browser(ws_engine="websockets", resource_loader=document).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto("http://example.test/")
        page.evaluate(f"window.__ws = new WebSocket('{echo.url}')")
        assert (await wait_for(page, "__ws.readyState === 1")), page.evaluate("__ws.readyState")
    finally:
        (await browser.close())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not echo.closed:
        (await asyncio.sleep(0.05))
    # The engine's own closing handshake, not a dropped socket.
    assert echo.closed == [1000]


async def test_close_while_handshake_is_pending_never_reopens_socket(monkeypatch):
    entered, cancelled = threading.Event(), threading.Event()

    async def connect(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr('pyv8c.network.websockets._connect_client', lambda: connect)
    browser = (await Browser(ws_engine='websockets', resource_loader=document).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto('http://example.test/')
        page.evaluate("""
            globalThis.events = [];
            globalThis.ws = new WebSocket('ws://example.test/');
            ws.onopen = () => events.push('open');
            ws.onerror = () => events.push('error');
            ws.onclose = e => events.push(['close', e.code, e.wasClean]);
        """)
        assert (await asyncio.to_thread(entered.wait, 3))
        page.evaluate("ws.close(1000, 'cancelled')")
        assert page.evaluate('ws.readyState') == 2
        (await page.wait_for('events.length === 2', timeout=5))
        assert page.evaluate('events') == ['error', ['close', 1006, False]]
        assert page.evaluate('ws.readyState') == 3
        assert cancelled.is_set()
    finally:
        (await browser.close())


async def test_close_after_real_handshake_before_open_task(echo, monkeypatch):
    from pyv8c.network.websockets import _Socket

    posted = threading.Event()
    original = _Socket._deliver

    def observe(self, name, *args):
        original(self, name, *args)
        if name == 'open':
            posted.set()

    monkeypatch.setattr(_Socket, '_deliver', observe)
    browser = (await Browser(ws_engine='websockets', resource_loader=document).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto('http://example.test/')
        page.executor.call(lambda: page.v8_context.expose(
            wait_for_transport=lambda: posted.wait(3)))
        assert page.evaluate(f"""
            globalThis.events=[];
            globalThis.ws=new WebSocket('{echo.url}');
            ws.onopen=()=>events.push('open');
            ws.onmessage=()=>events.push('message');
            ws.onerror=()=>{{events.push(['error',ws.readyState]);
                queueMicrotask(()=>events.push('microtask'));}};
            ws.onclose=e=>events.push(['close',e.code,e.reason,e.wasClean,ws.readyState]);
            const connected=wait_for_transport();
            events.push(['before',ws.readyState]);
            ws.close(1000,'cancel');
            events.push(['after',ws.readyState]);
            connected;
        """)
        (await page.wait_for('events.length===5', timeout=5))
        assert page.evaluate('events') == [
            ['before', 0], ['after', 2], ['error', 3], 'microtask',
            ['close', 1006, '', False, 3],
        ]
        assert not page.services.websocket_services._sockets
    finally:
        (await browser.close())


async def test_queued_message_is_suppressed_after_ready_state_becomes_closing(echo, monkeypatch):
    from pyv8c.network.websockets import _Socket

    posted = threading.Event()
    original = _Socket._deliver

    def observe(self, name, *args, **kwargs):
        original(self, name, *args, **kwargs)
        if name == "message":
            posted.set()

    monkeypatch.setattr(_Socket, "_deliver", observe)
    async with Browser(http_engine=None) as browser:
        page = browser.new_page()
        page.executor.call(lambda: page.v8_context.expose(wait_message=lambda: posted.wait(3)))
        page.evaluate(f"""
            globalThis.events = [];
            globalThis.ws = new WebSocket('{echo.url}');
            ws.onmessage = e => events.push([ws.readyState, e.data]);
            ws.onclose = e => events.push([e.code, e.wasClean]);
            void 0;
        """)
        await page.wait_for("ws.readyState === WebSocket.OPEN", timeout=5)
        transport, = page.services.websocket_services._sockets
        assert page.evaluate("""
            ws.send('already queued');
            const queued = wait_message();
            ws.close();
            [queued, ws.readyState];
        """) == [True, 2]
        await page.wait_for("ws.readyState === WebSocket.CLOSED", timeout=5)
        assert page.evaluate("events") == [[1000, True]]
        assert transport._pending_reads == transport._pending_read_bytes == 0


async def test_slow_socket_writer_does_not_block_page_tasks(echo, monkeypatch):
    from websockets.asyncio.client import ClientConnection

    entered, release = threading.Event(), threading.Event()
    original = ClientConnection.send

    async def slow_send(self, data, *args, **kwargs):
        entered.set()
        async with asyncio.timeout(5):
            while not release.is_set():
                await asyncio.sleep(0.005)
        return await original(self, data, *args, **kwargs)

    monkeypatch.setattr(ClientConnection, 'send', slow_send)
    browser = (await Browser(ws_engine='websockets', resource_loader=document).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto('http://example.test/')
        page.evaluate(f"globalThis.ws=new WebSocket('{echo.url}'); 'created'")
        (await page.wait_for('ws.readyState===1', timeout=5))
        assert page.evaluate("ws.send('中');ws.bufferedAmount") == 3
        assert (await asyncio.to_thread(entered.wait, 3))
        page.evaluate('globalThis.timerRan=false;setTimeout(()=>timerRan=true,0)')
        (await page.wait_for('timerRan', timeout=2))
        assert page.evaluate('ws.bufferedAmount') == 3
        release.set()
        (await page.wait_for('ws.bufferedAmount===0', timeout=5))
    finally:
        release.set()
        (await browser.close())


@pytest.mark.parametrize('close_first', [False, True])
@pytest.mark.parametrize('secure', [False, True])
async def test_page_close_interrupts_real_unread_websocket(monkeypatch, close_first, secure, tmp_path):
    from websockets.asyncio.client import ClientConnection

    if secure:
        pytest.importorskip('cryptography')

    release, entered, finished = threading.Event(), threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            accept = base64.b64encode(hashlib.sha1(key.encode('ascii')).digest()).decode('ascii')
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            self.wfile.flush()
            release.wait(30)  # Complete the handshake, then deliberately stop reading.
            self.close_connection = True

        def log_message(self, *args):
            pass

    original = ClientConnection.send

    async def observed_send(self, data, *args, **kwargs):
        self.transport.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 0)
        self.transport.set_write_buffer_limits(high=1024, low=0)
        entered.set()
        try:
            if secure:
                # SSL can pass one large write into the TCP transport before
                # backpressure propagates. Exercise real fragmented writes.
                view = memoryview(data)
                data = (view[offset:offset + 16384] for offset in range(0, len(view), 16384))
            return await original(self, data, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(ClientConnection, 'send', observed_send)
    options = {}
    server_context = None
    if secure:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
        cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                             serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.verify_mode = ssl.CERT_NONE  # Only this local self-signed fixture.
        options['ssl'] = client_context
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    if server_context is not None:
        try:
            server.socket = server_context.wrap_socket(server.socket, server_side=True)
        except BaseException:
            server.server_close()
            raise
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    browser = (await Browser(ws_engine='websockets', ws_options=options, resource_loader=document).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto('http://example.test/')
        scheme = 'wss' if secure else 'ws'
        page.evaluate(f"globalThis.ws=new WebSocket('{scheme}://127.0.0.1:{server.server_port}/');'created'")
        (await page.wait_for('ws.readyState===1', timeout=5))
        engine = page.profile.network_backend.ws
        with engine._lock:
            transport, = engine._sockets
        connection = transport._connection
        page.evaluate("ws.send(new Blob(['x'.repeat(4*1024*1024)]));'sent'")
        assert (await asyncio.to_thread(entered.wait, 3))
        assert not (await asyncio.to_thread(finished.wait, 0.1)), 'fixture did not block the actual WebSocket write'
        if close_first:
            page.evaluate('ws.close()')
        (await page.close())
        assert finished.is_set()
        assert transport.completion.done()
        assert connection.connection_lost_waiter.done()
        if connection.keepalive_task is not None:
            assert connection.keepalive_task.done()
        assert not transport._callbacks
        assert not engine._sockets
        assert not page.services.websocket_services._sockets
    finally:
        release.set()
        (await browser.close())
        server.shutdown()
        server.server_close()
        (await asyncio.to_thread(server_thread.join, 5))
        assert not server_thread.is_alive()
