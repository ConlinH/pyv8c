"""``page.net``: host requests over the context's transport.

These run against a real local server with the real built-in engines, because
what they are about is exactly what a fake would hide: that a host request and a
page request are the same session as far as the server is concerned -- one cookie
jar, the browser's headers, and the same transport underneath both.
"""

import asyncio
import gzip
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytest.importorskip("curl_cffi", reason="these exercise the curl_cffi engine")

from _http_fixtures import GatedTransport, resolved

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import LoadFailure
from pyv8c.network.backend import NetworkBackend
from pyv8c.network.models import NetworkRequest, NetworkResponse

#: The streaming endpoint keeps writing until the test releases it.  Both are
#: reset by the test that uses them, so a failed test cannot strand the server.
_STREAM_STARTED = Event()
_STREAM_STOP = Event()
#: A connected request that never sends a response header or a body chunk.
_HANG_STOP = Event()


class _EchoHandler(BaseHTTPRequestHandler):
    """A tiny server: sets cookies, and reports back what it was sent."""

    def _reply(self, body, content_type="application/json", cookies=()):
        self.send_response(200)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Server", "echo")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, cookies=()):
        self._reply(json.dumps(payload).encode("utf-8"), cookies=cookies)

    def _redirect(self):
        body = b"redirect-body-must-not-leak"
        self.send_response(302)
        self.send_header("Location", "/echo")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while not _STREAM_STOP.is_set():
                self.wfile.write(b"x" * 4096)
                self.wfile.flush()
                _STREAM_STARTED.set()
                time.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _hang(self):
        # The socket is accepted and the request is read, but no response byte
        # is written.  Only cancelling the request coroutine can stop this call.
        _HANG_STOP.wait(30)
        self.close_connection = True

    def do_GET(self):
        if self.path.startswith("/hang"):
            self._hang()
            return
        if self.path.startswith("/set"):
            self._json({"set": True}, cookies=["a=1; Path=/", "b=2; Path=/"])
            return
        if self.path.startswith("/redirect"):
            self._redirect()
            return
        if self.path.startswith("/nocontent"):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/gzip"):
            body = gzip.compress(b"compressed body")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/stream"):
            self._stream()
            return
        if self.path.startswith("/echo"):
            self._json({
                "cookie": self.headers.get("Cookie"),
                "ua": self.headers.get("User-Agent"),
                "language": self.headers.get("Accept-Language"),
                "custom": self.headers.get("X-Custom"),
                "referer": self.headers.get("Referer"),
                "accept": self.headers.get("Accept"),
                "encoding": self.headers.get("Accept-Encoding"),
                "destination": self.headers.get("Sec-Fetch-Dest"),
                "mode": self.headers.get("Sec-Fetch-Mode"),
                "site": self.headers.get("Sec-Fetch-Site"),
                "hints": self.headers.get("Sec-CH-UA"),
            })
            return
        self._reply(b"<html><body>page</body></html>", content_type="text/html; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        self._json({"type": self.headers.get("Content-Type"), "body": body})

    def log_message(self, _format, *_args):
        pass


@pytest.fixture
def origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
async def page(origin):
    browser = (await Browser(
        stub_logging="off",
        http_engine="curl_cffi",
        default_headers={"X-Custom": "browser"},
    ).__aenter__())
    instance = browser.new_profile().new_page()
    await instance.goto(origin + "/")
    try:
        yield instance
    finally:
        (await browser.close())


@pytest.fixture
async def cronet_page(origin):
    """The same host API on the second built-in, also a cancellable coroutine."""
    pytest.importorskip("cycronet", reason="these exercise the cronet engine")
    browser = (await Browser(
        stub_logging="off",
        http_engine="cronet",
        default_headers={"X-Custom": "browser"},
    ).__aenter__())
    instance = browser.new_profile().new_page()
    await instance.goto(origin + "/")
    try:
        yield instance
    finally:
        _HANG_STOP.set()
        (await browser.close())


async def test_page_net_sends_the_environment_headers_and_the_jar(page, origin):
    (await page.net.get("/set"))
    payload = (await page.net.get("/echo")).json()

    assert payload["ua"].startswith("Mozilla/5.0")
    assert payload["language"] == "zh-CN,zh;q=0.9"
    assert payload["custom"] == "browser", "Runtime(default_headers=...) applies"
    assert payload["cookie"] == "a=1; b=2", "both cookies the response set are sent back"
    # A host request now carries what the page's own fetch would carry.
    assert payload["accept"] == "*/*"
    assert payload["encoding"] == "gzip, deflate, br, zstd"
    assert payload["destination"] == "empty"
    assert payload["mode"] == "cors"
    assert payload["site"] == "same-origin"
    assert payload["hints"] == '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"'


async def test_host_and_page_requests_share_one_cookie_baseline(page):
    """Both sending paths finalise the same Cookie header at build time."""
    (await page.net.get("/set"))
    host = (await page.net.get("/echo")).json()["cookie"]
    snapshot = (await page.net.fetch("/echo")).json()
    assert host == "a=1; b=2"
    assert snapshot["cookie"] == host


async def test_custom_engine_completion_stays_owned_after_page_close_times_out(monkeypatch):
    from pyv8c.foundation import background
    from pyv8c.foundation.host_errors import HostStateError

    class Engine:
        def __init__(self):
            self.calls = {}

        def submit_request(self, request, handle, remaining):
            future = Future()
            future.set_running_or_notify_cancel()
            self.calls[request.url] = handle, future
            return future

        def close(self):
            pass
    engine = Engine()
    monkeypatch.setattr(background, "_NETWORK_WAIT_SECONDS", 0.05)
    async with Browser(http_engine=engine, stub_logging="off") as browser:
        profile = browser.new_profile()
        pages = [profile.new_page() for _ in range(2)]
        urls = [f"https://fixture.test/{i}" for i in range(2)]
        for page, url in zip(pages, urls):
            await page.set_content("<p>ready</p>", url)
            page.evaluate("fetch(location.href).catch(() => {})")
        try:
            assert set(engine.calls) == set(urls)
            with pytest.raises(HostStateError, match="1 in flight"):
                await pages[0].close()
            assert pages[0] in profile.pages
            assert engine.calls[urls[0]][0].cancelled
            assert not engine.calls[urls[1]][0].cancelled
        finally:
            for url, (_, complete) in engine.calls.items():
                complete.set_result(NetworkResponse(url))
        await pages[0].close()
        assert pages[0] not in profile.pages
        assert not pages[1].closed


async def test_headers_override_in_three_layers(page):
    page.net.headers["X-Custom"] = "page"
    assert (await page.net.get("/echo")).json()["custom"] == "page"
    assert (await page.net.get("/echo", headers={"X-Custom": "call"})).json()["custom"] == "call"
    # A caller's own spelling of a default header wins over the derived one.
    assert (await page.net.get("/echo", headers={"user-agent": "custom-agent"})).json()["ua"] == "custom-agent"


async def test_page_net_resolves_relative_urls_and_query_parameters(page, origin):
    response = (await page.net.get("/echo", params={"q": "1", "r": "2"}))
    assert response.url == f"{origin}/echo?q=1&r=2"
    assert response.ok is True
    assert response.headers["x-server"] == "echo"


async def test_page_net_posts_json_and_answers_asynchronously(page):
    posted = (await page.net.post("/echo", json={"a": 1}))
    assert posted.json()["type"] == "application/json"
    assert json.loads(posted.json()["body"]) == {"a": 1}

    future = asyncio.create_task(page.net.get("/echo"))
    assert (await future).status == 200


async def test_page_net_can_drop_or_override_cookies(page):
    (await page.net.get("/set"))
    assert (await page.net.get("/echo", cookies=False)).json()["cookie"] is None
    overridden = (await page.net.get("/echo", cookies={"a": "override"})).json()["cookie"]
    assert overridden == "a=override; b=2"


async def test_cookies_are_shared_with_the_page_in_both_directions(page):
    # A host request's cookies are visible to the document...
    (await page.net.get("/set"))
    assert page.evaluate("document.cookie") == "a=1; b=2"

    # ...a page fetch sends them through the same transport...
    page.evaluate("fetch('/echo').then(r => r.json()).then(v => window.__fromPage = v)")
    (await page.wait_for("typeof window.__fromPage !== 'undefined'", timeout=10))
    assert page.evaluate("__fromPage")["cookie"] == "a=1; b=2"

    # ...and a cookie a page script sets is sent by the next host request.
    page.evaluate("document.cookie = 'c=3; path=/'")
    assert (await page.net.get("/echo")).json()["cookie"] == "a=1; b=2; c=3"


async def test_the_host_api_stays_out_of_the_realm():
    """Nothing of ``page.net`` is visible to page code.

    A browser has no ``net`` global, so neither may this host API appear as one:
    a page that goes looking must find it undefined, along with the bridge global
    that used to carry the extension's native function.
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("[typeof window.net, typeof globalThis.net]") == ["undefined", "undefined"]
        assert page.evaluate("typeof globalThis.__pyv8cNetHost__") == "undefined"
    finally:
        (await browser.close())


async def test_page_net_without_a_transport_says_so():
    browser = (await Browser(stub_logging="off", real_network=False).__aenter__())
    try:
        instance = browser.new_profile().new_page()
        # A missing engine is the host's own failure to load, so the host sees it.
        with pytest.raises(LoadFailure) as failure:
            (await instance.net.get("http://example.test/"))
        assert instance.net.available is False
    finally:
        (await browser.close())
    assert "real_network" in str(failure.value)


async def test_host_request_uses_the_shared_in_flight_accounting(monkeypatch):
    """A host request is visible to the same network ledger as resource loads."""
    import threading

    entered, release = threading.Event(), threading.Event()

    def request(value):
        entered.set()
        assert release.wait(3)
        return NetworkResponse(value.url)

    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        net = page.net
        page.profile.network_backend = NetworkBackend(http=GatedTransport(request))
        monkeypatch.setattr(net, "build",
                            lambda method, url, **kw: NetworkRequest(url=url, timeout=None))
        task = asyncio.create_task(net.request("GET", "https://slow.test/"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert page._background._network == 1
        finally:
            release.set()
            assert (await task).url == "https://slow.test/"
        assert page._background._network == 0


async def test_host_http_preserves_each_callers_context_in_a_shared_dispatch():
    identity = ContextVar("host_http_test_identity")

    class Engine:
        def submit_request(self, request, handle, remaining):
            captured = identity.get()
            return resolved(NetworkResponse(request.url, body=f"{captured}:{identity.get()}"))

        def close(self):
            pass
    async with Browser(http_engine=Engine(), ws_engine=None, stub_logging="off") as browser:
        profile = browser.new_profile()

        async def get(page, label):
            reset = identity.set(label)
            try:
                return (await page.net.get(f"http://fixture.test/{label}")).text
            finally:
                identity.reset(reset)

        assert await asyncio.gather(get(profile.new_page(), "A"),
                                    get(profile.new_page(), "B")) == ["A:A", "B:B"]


@pytest.mark.parametrize("action", ["cancel", "close"])
async def test_host_http_discards_an_unstarted_custom_post(action):

    initialized, release = Event(), Event()
    sent = []

    def initializer():
        initialized.set()
        assert release.wait(5)

    executor = ThreadPoolExecutor(max_workers=1, initializer=initializer)
    engine = GatedTransport(lambda request: sent.append(request.url), executor=executor)
    async with Browser(http_engine=engine, ws_engine=None, stub_logging="off") as browser:
        page = browser.new_page()
        request = asyncio.create_task(page.net.post("http://fixture.test/queued"))
        try:
            assert await asyncio.to_thread(initialized.wait, 2)
            if action == "close":
                await asyncio.wait_for(page.close(), 2)
            else:
                request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            if action == "cancel":
                await asyncio.wait_for(page.close(), 2)
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
    assert sent == []


async def test_host_request_future_tracks_running_and_queued_cancellation(monkeypatch):
    """Cancelling queued observation prevents work; active transport stays owned."""
    import threading
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    calls = []
    def request(value):
        calls.append(value.url)
        entered[int(value.url.rsplit('/', 1)[-1])].set()
        assert release.wait(3)
        return NetworkResponse(value.url)
    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        net = page.net
        page.profile.network_backend = NetworkBackend(http=GatedTransport(request))
        monkeypatch.setattr(net, "build", lambda method, url, **kw: NetworkRequest(url=url, timeout=None))
        active = [asyncio.create_task(net.request("GET", f"https://queue.test/{i}")) for i in (0, 1)]
        try:
            assert all([await asyncio.to_thread(event.wait, 1) for event in entered])
            queued = asyncio.create_task(net.request("GET", "https://queue.test/2"))
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            release.set()
            assert [response.url for response in await asyncio.gather(*active)] == [
                "https://queue.test/0", "https://queue.test/1"]
            assert sorted(calls) == ["https://queue.test/0", "https://queue.test/1"]
        finally:
            release.set()
            await asyncio.gather(*active, return_exceptions=True)


async def test_host_request_deadline_attempts_abort_and_reports_a_refusal(monkeypatch):
    """A host deadline now asks for an interrupt; no hook means honest failure."""
    import threading

    from pyv8c.network.models import RequestHandle

    entered, release = threading.Event(), threading.Event()

    def request(value):
        entered.set()
        assert release.wait(3)
        return value.url

    attempts = []
    original = RequestHandle.cancel

    def spy(submission):
        answer = original(submission)
        attempts.append(answer)
        return answer

    monkeypatch.setattr(RequestHandle, "cancel", spy)

    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        net = page.net
        page.profile.network_backend = NetworkBackend(http=GatedTransport(request))
        monkeypatch.setattr(net, "build",
                            lambda method, url, **kw: NetworkRequest(url=url, timeout=None))
        task = asyncio.create_task(net.request("GET", "https://slow.test/"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, 0.05)
            assert attempts == [False], "the transport had already started"
            assert page._background._network == 1, "a refused abort never releases the count"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            for _ in range(300):
                if page._background._network == 0:
                    break
                await asyncio.sleep(0.01)
        assert page._background._network == 0, "the transport future still settles once"


async def test_host_request_shutdown_completes_cancelled_queue(monkeypatch):
    import threading
    from pyv8c.foundation import background
    from pyv8c.foundation.host_errors import HostStateError

    monkeypatch.setattr(background, "_NETWORK_WAIT_SECONDS", 0.05)
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    def request(value):
        entered[int(value.url.rsplit('/', 1)[-1])].set()
        assert release.wait(3)
        return NetworkResponse(value.url)
    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        net = page.net
        page.profile.network_backend = NetworkBackend(http=GatedTransport(request))
        monkeypatch.setattr(net, "build", lambda method, url, **kw: NetworkRequest(url=url, timeout=None))
        active = [asyncio.create_task(net.request("GET", f"https://queue.test/{i}")) for i in (0, 1)]
        try:
            assert all([await asyncio.to_thread(event.wait, 1) for event in entered])
            queued = asyncio.create_task(net.request("GET", "https://queue.test/2"))
            await asyncio.sleep(0)
            closing = asyncio.create_task(page.close())
            with pytest.raises(asyncio.CancelledError):
                await queued
            with pytest.raises(HostStateError, match="2 in flight"):
                await closing
            assert page in page.profile.pages
            release.set()
            await asyncio.gather(*active)
            await page.close()
        finally:
            release.set()
            await asyncio.gather(*active, return_exceptions=True)


async def test_host_request_really_aborts_a_streaming_call(page, monkeypatch):
    """A host deadline cancels the request coroutine, not a no-op."""
    attempts = _spy_on_aborts(monkeypatch)
    _STREAM_STARTED.clear()
    _STREAM_STOP.clear()
    task = None
    try:
        task = asyncio.create_task(page.net.get("/stream", timeout=5))
        assert await asyncio.to_thread(_STREAM_STARTED.wait, 2)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, 0.05)
        assert attempts == [True], "the host path registered a real interrupt hook"
        # Without the interrupt the call would run until its 5s transport
        # timeout; with it, the cancelled coroutine settles the future at once.
        deadline = time.perf_counter() + 2
        while page._background._network and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)
        assert page._background._network == 0, "the aborted transfer settled its count"
        assert (await page.net.get("/echo")).ok, "the session survives an aborted call"
    finally:
        _STREAM_STOP.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def test_host_request_keeps_only_the_final_body_when_it_follows_a_redirect(page):
    """A followed chain delivers the last hop only, never the redirect's body."""
    response = await page.net.get("/redirect")
    assert response.status == 200
    assert response.json()["custom"] == "browser"


async def test_host_request_keeps_an_empty_body_empty(page):
    response = await page.net.get("/nocontent")
    assert response.status == 204
    assert response.content == b""


async def test_host_request_decodes_a_compressed_body(page):
    """An interruptible call still receives libcurl's decoded bytes."""
    response = await page.net.get("/gzip")
    assert response.text == "compressed body"


def _spy_on_aborts(monkeypatch):
    from pyv8c.network.models import RequestHandle

    attempts = []
    original = RequestHandle.cancel

    def spy(submission):
        answer = original(submission)
        attempts.append(answer)
        return answer

    monkeypatch.setattr(RequestHandle, "cancel", spy)
    return attempts


async def _assert_stalled_connect_is_interrupted(page, monkeypatch):
    """No response header and no body chunk: only a coroutine cancel reaches it."""
    attempts = _spy_on_aborts(monkeypatch)
    _HANG_STOP.clear()
    task = None
    try:
        task = asyncio.create_task(page.net.get("/hang", timeout=30))
        await asyncio.sleep(0.2)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, 0.1)
        assert attempts == [True], "the host path registered a real interrupt hook"
        deadline = time.perf_counter() + 2
        while page._background._network and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)
        assert page._background._network == 0, (
            "the cancelled transfer settled its count")
        assert (await page.net.get("/echo")).json()["custom"] == "browser", (
            "the session survives an interrupted connect")
    finally:
        _HANG_STOP.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def test_the_default_engine_interrupts_a_stalled_connect(page, monkeypatch):
    await _assert_stalled_connect_is_interrupted(page, monkeypatch)


async def test_cronet_interrupts_a_stalled_connect(cronet_page, monkeypatch):
    await _assert_stalled_connect_is_interrupted(cronet_page, monkeypatch)
