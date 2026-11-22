"""Loopback regressions for WebSocket I/O and document ownership."""
import asyncio
import json
import threading
import time

import pytest
from websockets.asyncio.server import serve

from pyv8c.async_api import Browser


def address(server):
    return f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"


async def connect(page, url, count=1):
    page.evaluate(f"""
        globalThis.echoes = 0;
        globalThis.sockets = Array.from({{length: {count}}}, () => {{
            const ws = new WebSocket({json.dumps(url)});
            ws.onopen = () => ws.send("ping");
            ws.onmessage = () => echoes++;
            return ws;
        }});
        void 0;
    """)
    await page.wait_for(f"echoes === {count}", timeout=5)


async def echo(connection):
    async for message in connection:
        await connection.send(message)


async def test_connections_share_one_io_thread_across_profiles():
    async with serve(echo, "127.0.0.1", 0) as server, Browser(http_engine=None) as browser:
        pages = [browser.new_profile().new_page() for _ in range(2)]
        for page in pages:
            await page.set_content()
        before = set(threading.enumerate())
        await asyncio.gather(*(connect(page, address(server), 6) for page in pages))
        clients = [thread for thread in threading.enumerate() if thread not in before]
        assert len(clients) == 1, [thread.name for thread in clients]
        await pages[0].profile.close()
        pages[1].evaluate('sockets[0].send("ping")')
        await pages[1].wait_for("echoes === 7", timeout=5)
        await browser.close()
        assert not clients[0].is_alive()


async def test_document_replacement_retires_its_socket():
    closed = asyncio.Event()

    async def track(connection):
        try:
            await echo(connection)
        finally:
            closed.set()

    async with serve(track, "127.0.0.1", 0) as server, Browser(http_engine=None) as browser:
        page = browser.new_page()
        await page.set_content("<p>old</p>", "http://example.test/old")
        await connect(page, address(server))
        page.evaluate('location.hash = "same-document"')
        assert page.evaluate("location.hash") == "#same-document"
        page.evaluate('sockets[0].send("ping")')
        await page.wait_for("echoes === 2", timeout=5)
        assert not closed.is_set()
        await page.set_content("<p>new</p>", "http://example.test/new")
        await asyncio.wait_for(closed.wait(), 0.75)
        await connect(page, address(server))


async def test_first_content_replacement_closes_the_retained_websocket_object():
    disconnected = asyncio.Event()

    async def handler(connection):
        await connection.wait_closed()
        disconnected.set()

    async with serve(handler, "127.0.0.1", 0) as server, Browser(http_engine=None) as browser:
        page = browser.new_page()
        page.evaluate(f"""
            globalThis.ws = new WebSocket({json.dumps(address(server))});
            globalThis.events = [];
            ws.onerror = () => events.push("error");
            ws.onclose = () => events.push("close");
            void 0;
        """)
        await page.wait_for("ws.readyState === WebSocket.OPEN", timeout=5)
        await page.set_content("<p>new document</p>", "http://example.test/")
        assert page.evaluate("ws.readyState") == 3
        await asyncio.wait_for(disconnected.wait(), 1)
        assert page.evaluate("ws.send('中'); [ws.bufferedAmount, events]") == [3, []]


@pytest.mark.parametrize("close_owner", ["page", "profile"])
async def test_close_interrupts_stalled_handshake(close_owner):
    entered, disconnected = asyncio.Event(), asyncio.Event()

    async def hold_upgrade(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            entered.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(hold_upgrade, "127.0.0.1", 0) as server:
        async with Browser(http_engine=None, ws_options={"open_timeout": 1.5}) as browser:
            page = browser.new_page()
            page.evaluate(f"new WebSocket({json.dumps(address(server))}); void 0")
            await asyncio.wait_for(entered.wait(), 5)
            start = time.perf_counter()
            owner = page if close_owner == "page" else page.profile
            await owner.close()
            assert time.perf_counter() - start < 0.75
            await asyncio.wait_for(disconnected.wait(), 0.75)


@pytest.mark.parametrize("abort", ["js-close", "profile-close", "deadline"])
async def test_stalled_proxy_connect_releases_tcp_on_cancel_or_deadline(abort):
    request = asyncio.get_running_loop().create_future()
    disconnected = asyncio.Event()
    peers = []

    async def hold_connect(reader, writer):
        peers.append(writer)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            request.set_result(header.splitlines()[0])
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(hold_connect, "127.0.0.1", 0) as proxy:
        port = proxy.sockets[0].getsockname()[1]
        open_timeout = 1 if abort == "deadline" else 5
        async with Browser(http_engine=None, proxy=f"http://127.0.0.1:{port}",
                           ws_options={"open_timeout": open_timeout}) as browser:
            try:
                page = browser.new_page()
                page.evaluate("""
                    globalThis.events = [];
                    globalThis.ws = new WebSocket('ws://127.0.0.1:32100/');
                    ws.onerror = () => events.push('error');
                    ws.onclose = e => events.push([e.code, e.wasClean]);
                    void 0;
                """)
                assert await asyncio.wait_for(request, 3) == b"CONNECT 127.0.0.1:32100 HTTP/1.1"
                if abort == "profile-close":
                    await page.profile.close()
                else:
                    if abort == "js-close":
                        page.evaluate("ws.close()")
                    await page.wait_for("events.length === 2", timeout=3)
                    assert page.evaluate("events") == ["error", [1006, False]]
                await asyncio.wait_for(disconnected.wait(), 1)
            finally:
                for writer in peers:
                    writer.close()
                    await writer.wait_closed()


@pytest.mark.parametrize("release_peer", [True, False])
async def test_profile_close_waits_for_established_transport(release_peer):
    peer_ready, release = asyncio.Event(), asyncio.Event()

    async def hold_close(connection):
        await connection.send("ping")
        connection.transport.pause_reading()
        peer_ready.set()
        try:
            await release.wait()
        finally:
            if not connection.transport.is_closing():
                connection.transport.resume_reading()
        await connection.wait_closed()

    async with serve(hold_close, "127.0.0.1", 0) as server, Browser(http_engine=None) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        page.evaluate(f"globalThis.ws = new WebSocket({json.dumps(address(server))}); void 0")
        await page.wait_for("ws.readyState === WebSocket.OPEN", timeout=5)
        await asyncio.wait_for(peer_ready.wait(), 5)
        closing = asyncio.create_task(profile.close())
        try:
            await asyncio.sleep(0.05)
            assert not closing.done(), "Profile returned before the WebSocket transport shut down"
            if not release_peer:
                await asyncio.wait_for(asyncio.shield(closing), 1.5)
        finally:
            release.set()
            await closing


async def test_navigation_revokes_events_from_a_custom_socket_engine(monkeypatch):
    from pyv8c.web.websocket import WebSocket

    delivered = []
    monkeypatch.setattr(WebSocket, "_on_message", lambda self, *args: delivered.append(args))

    class DelayedEngine:
        def __init__(self):
            self.closed = []

        def websocket(self, tasks, url, protocols, callbacks, options=None):
            websocket = object()
            self.callbacks = callbacks
            tasks.post_task(lambda: callbacks["open"](websocket))
            return websocket

        def close_websocket(self, websocket):
            self.closed.append(websocket)

        def close(self):
            pass

    engine = DelayedEngine()
    async with Browser(http_engine=None, ws_engine=engine) as browser:
        page = browser.new_page()
        await page.set_content("", "http://example.test/old")
        page.evaluate("globalThis.ws = new WebSocket('ws://example.test/socket'); void 0")
        await page.wait_for("ws.readyState === WebSocket.OPEN", timeout=5)
        await page.set_content("", "http://example.test/new")
        socket, = engine.closed
        # A custom engine may still hold a completion queued on a generic lane.
        # Its callback must never re-enter the old V8 document.
        engine.callbacks["message"](socket, "stale", True)
        engine.callbacks["error"](socket, RuntimeError("stale"))
        engine.callbacks["close"](socket, 1000, "stale", True)
        assert delivered == []
        assert page.evaluate("typeof ws") == "undefined"


async def test_failed_socket_retirement_keeps_navigation_state_and_cleanup_ownership():
    class SometimesFailingEngine:
        def __init__(self):
            self.opened = []
            self.attempts = []
            self.fail = True

        def websocket(self, tasks, url, protocols, callbacks, options=None):
            socket = object()
            self.opened.append(socket)
            tasks.post_task(lambda: callbacks["open"](socket))
            return socket

        def close_websocket(self, websocket):
            self.attempts.append(websocket)
            if self.fail:
                raise RuntimeError("WebSocket teardown failed")

        def close(self):
            pass

    engine = SometimesFailingEngine()
    lifecycle = []
    async with Browser(http_engine=None, ws_engine=engine) as browser:
        page = browser.new_page()
        await page.set_content("<p>old</p>", "http://old.fixture/")
        page.executor.call(lambda: page.v8_context.expose(unloaded=lambda: lifecycle.append("unload")))
        page.evaluate("""
            globalThis.ws = new WebSocket('ws://old.fixture/');
            addEventListener('unload', () => {
                unloaded();
                new WebSocket('ws://old.fixture/unloading');
            });
        """)
        await page.wait_for("ws.readyState === WebSocket.OPEN", timeout=3)
        old_document, old_realm = page.document, page.realm
        old_generation, old_origin = page.navigation.generation, page.origin
        try:
            with pytest.raises(RuntimeError, match="WebSocket teardown failed"):
                await page.set_content("<p>new</p>", "http://new.fixture/")
            assert page.document is old_document and page.realm is old_realm
            assert page.navigation.generation == old_generation and page.origin == old_origin
            assert page.evaluate("[location.href, document.body.textContent, document.hidden]") == [
                "http://old.fixture/", "old", False,
            ]
            assert lifecycle == []
            old_socket, = engine.opened
            assert list(page.services.websocket_services._sockets) == [old_socket]
        finally:
            engine.fail = False
        await page.set_content(
            "<script>globalThis.fresh = new WebSocket('ws://new.fixture/')</script>",
            "http://new.fixture/",
        )
        await page.wait_for("fresh.readyState === WebSocket.OPEN", timeout=3)
        assert page.origin == ("http", "new.fixture", 80)
        assert page.evaluate("location.href") == "http://new.fixture/"
        assert lifecycle == ["unload"]
        assert engine.attempts == [old_socket, old_socket]
        assert len(engine.opened) == 2


async def test_handshake_redirect_never_reaches_target():
    target_connections = []

    async def target(connection):
        target_connections.append(connection)
        await connection.wait_closed()

    async def redirect(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                f"HTTP/1.1 302 Found\r\nLocation: {address(destination)}\r\n"
                "Content-Length: 0\r\n\r\n".encode("ascii"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with serve(target, "127.0.0.1", 0) as destination:
        async with await asyncio.start_server(redirect, "127.0.0.1", 0) as server:
            async with Browser(http_engine=None) as browser:
                page = browser.new_page()
                page.evaluate(f"""
                    globalThis.events = [];
                    globalThis.ws = new WebSocket({json.dumps(address(server))});
                    ws.onopen = () => events.push("open");
                    ws.onerror = () => events.push("error");
                    ws.onclose = e => events.push([e.code, e.wasClean]);
                    void 0;
                """)
                await page.wait_for("events.length === 2", timeout=5)
                assert page.evaluate("events") == ["error", [1006, False]]
                assert target_connections == []


@pytest.mark.parametrize("compression", ["deflate", None])
async def test_compression_option_controls_the_wire_handshake(compression):
    negotiated = []

    async def handler(connection):
        negotiated.append(connection.response.headers.get("Sec-WebSocket-Extensions", ""))
        await echo(connection)

    async with serve(handler, "127.0.0.1", 0) as server:
        async with Browser(http_engine=None, ws_options={"compression": compression}) as browser:
            await connect(browser.new_page(), address(server))
            extension, = negotiated
            assert ("permessage-deflate" in extension) == (compression == "deflate")


@pytest.mark.parametrize("retire", ["terminate", "navigate", "profile"])
async def test_worker_socket_uses_profile_and_retires_with_worker(retire):
    handshakes, closed = [], asyncio.Event()

    async def handler(connection):
        handshakes.append(connection.request.headers)
        try:
            await echo(connection)
        finally:
            closed.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        def document(url, request):
            return f"""
                const ws = new WebSocket({json.dumps(address(server))});
                ws.onopen = () => ws.send("worker");
                ws.onmessage = e => postMessage(e.data);
            """

        async with Browser(http_engine=None, resource_loader=document) as browser:
            page = browser.new_page()
            await page.set_content("", "http://example.test/")
            page.evaluate("""
                globalThis.answer = [];
                globalThis.worker = new Worker("/worker.js");
                worker.onmessage = e => answer.push(e.data);
            """)
            await page.wait_for("answer.length === 1", timeout=5)
            assert page.evaluate("answer") == ["worker"]
            assert handshakes[0]["Origin"] == "http://example.test"
            if retire == "terminate":
                page.evaluate("worker.terminate()")
            elif retire == "navigate":
                await page.set_content("", "http://example.test/next")
            else:
                await page.profile.close()
            await asyncio.wait_for(closed.wait(), 0.75)


@pytest.mark.parametrize("action", ["terminate", "navigate"])
async def test_worker_stop_interrupts_active_and_queued_socket_handshakes(monkeypatch, action):
    from pyv8c.network import websockets as transport_module

    monkeypatch.setattr(transport_module, "_MAX_HANDSHAKES", 1)
    requested, disconnected = asyncio.Event(), asyncio.Event()
    host_entered, release_worker = threading.Event(), threading.Event()
    requests = []
    peers = []

    async def hold_upgrade(reader, writer):
        peers.append(writer)
        try:
            handshake = await reader.readuntil(b"\r\n\r\n")
            requests.append(handshake.splitlines()[0])
            requested.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    def occupied_worker():
        host_entered.set()
        release_worker.wait(5)

    async with await asyncio.start_server(hold_upgrade, "127.0.0.1", 0) as server:
        url = address(server)
        script = f"""
            globalThis.first = new WebSocket({json.dumps(url + 'first')});
            globalThis.queued = new WebSocket({json.dumps(url + 'queued')});
            postMessage('created');
        """
        async with Browser(http_engine=None, resource_loader=lambda *_: script) as browser:
            try:
                page = browser.new_page()
                await page.set_content("", "http://worker.fixture/")
                page.evaluate("""
                    globalThis.created = false;
                    globalThis.worker = new Worker('/worker.js');
                    worker.onmessage = () => created = true;
                """)
                await page.wait_for("created", timeout=3)
                await asyncio.wait_for(requested.wait(), 3)
                runtime, = browser._runtime._workers
                runtime.executor.post(occupied_worker, _task_lane=None)
                assert await asyncio.to_thread(host_entered.wait, 2)
                engine = page.profile.network_backend.ws
                with engine._lock:
                    sockets = tuple(engine._sockets)
                assert len(sockets) == 2
                if action == "terminate":
                    page.evaluate("worker.terminate()")
                else:
                    await page.set_content("<p>new</p>", "http://worker.fixture/next")
                await asyncio.wait_for(disconnected.wait(), 1)
                await asyncio.wait_for(asyncio.gather(
                    *(asyncio.wrap_future(socket.completion) for socket in sockets)), 1)
                assert requests == [b"GET /first HTTP/1.1"]
            finally:
                release_worker.set()
                for writer in peers:
                    writer.close()
                    await writer.wait_closed()
