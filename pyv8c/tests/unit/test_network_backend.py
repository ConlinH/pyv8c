import sys
import types
from typing import TypedDict

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from _http_fixtures import resolved, submit

from pyv8c.async_api import Browser
from pyv8c.network.models import NetworkRequest
from pyv8c.network.registry import accepted_option_keys
from pyv8c.web.fetch.response import Response


class FakeResponse:
    status_code = 200
    reason = "OK"
    headers = {"content-type": "text/plain"}
    content = b"cronet-ok"
    url = "http://example.test/data"
    history = ()


class FakeClient:
    """An ``AsyncCronetClient`` stand-in: request and close are coroutines."""

    def __init__(self, **options):
        self.options = options
        self.closed = False

    async def request(self, method, url, **kwargs):
        return FakeResponse()

    async def close(self):
        self.closed = True


def install_fake_cycronet(monkeypatch):
    monkeypatch.setitem(sys.modules, "cycronet",
                        types.SimpleNamespace(AsyncCronetClient=FakeClient))


class DeclaringClient:
    """A cycronet client that names its options, so an engine's keys are knowable."""

    def __init__(self, *, timeout_ms=1000, verify=True):
        self.options = {"timeout_ms": timeout_ms, "verify": verify}

    async def request(self, method, url, **kwargs):
        return FakeResponse()

    async def close(self):
        pass


def install_declaring_cycronet(monkeypatch):
    monkeypatch.setitem(sys.modules, "cycronet",
                        types.SimpleNamespace(AsyncCronetClient=DeclaringClient))


class FakeSocket:
    """One fixture socket: what the page sends comes back as a message."""

    protocol = "fixture"

    def __init__(self, callbacks, tasks):
        self._callbacks = dict(callbacks)
        self._tasks = tasks
        self.closed = False

    def _deliver(self, name, *arguments):
        """Hand one event to the page the way a real engine does: on the queue."""
        handler = self._callbacks.get(name)
        if handler is not None:
            self._tasks.post_task(lambda: handler(*arguments))

    def open(self):
        self._deliver("open", self)

    def send(self, message):
        self._deliver("message", self, message, True)

    def send_bytes(self, data):
        self._deliver("message", self, data, False)

    def close(self, code=1000, reason=""):
        if self.closed:
            return
        self.closed = True
        self._deliver("close", self, int(code), str(reason), True)


class FakeWebSocketEngine:
    """A socket engine with nothing behind it, for the page-level tests."""

    def __init__(self, *, options=None):
        self.options = dict(options or {})
        self.cookies = None
        self.sockets = []

    def websocket(self, tasks, url, protocols, callbacks, options=None):
        socket = FakeSocket(callbacks, tasks)
        self.sockets.append(socket)
        socket.open()
        return socket

    def close_websocket(self, websocket):
        websocket.close(1000, "page closed")

    def close(self):
        for socket in self.sockets:
            socket.close(1000, "context closed")


def install_fake_ws_engine(monkeypatch):
    """Register the socket engine the page-level WebSocket tests name."""
    from pyv8c import register_ws_transport

    register_ws_transport(
        "fixture-sockets", lambda *, options=None: FakeWebSocketEngine(options=options),
    )


async def test_real_network_requires_optional_dependency(monkeypatch):
    """The default HTTP engine is curl_cffi, and its absence is named as such."""
    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr("pyv8c.network.curl_cffi.importlib.import_module", missing)
    with pytest.raises(RuntimeError, match="curl_cffi"):
        (await Browser(real_network=True).__aenter__()).new_profile()


async def test_cronet_client_observes_its_configured_timeout(monkeypatch):
    class StrictClient:
        def __init__(self, *, timeout_ms):
            self.options = {"timeout_ms": timeout_ms}

        async def request(self, method, url, **kwargs):
            return FakeResponse()

        async def close(self):
            pass

    monkeypatch.setitem(sys.modules, "cycronet",
                        types.SimpleNamespace(AsyncCronetClient=StrictClient))
    browser = (await Browser(
        real_network=True, http_engine="cronet",
        http_options={"timeout_ms": 5000},
    ).__aenter__())
    engine = browser.new_profile().network_backend.http
    assert engine.session.options == {"timeout_ms": 5000}
    (await browser.close())


async def test_the_contexts_proxy_reaches_cronet_as_proxies(monkeypatch):
    """cycronet names its proxy option ``proxies``, so the bridge renames the context's."""
    install_fake_cycronet(monkeypatch)
    browser = (await Browser(real_network=True, http_engine="cronet", proxy="http://127.0.0.1:3128").__aenter__())
    try:
        engine = browser.new_profile().network_backend.http
        assert engine.session.options.get("proxies") == "http://127.0.0.1:3128"
    finally:
        (await browser.close())


async def test_a_context_without_a_proxy_gives_cronet_none(monkeypatch):
    install_fake_cycronet(monkeypatch)
    browser = (await Browser(real_network=True, http_engine="cronet").__aenter__())
    try:
        engine = browser.new_profile().network_backend.http
        assert "proxies" not in engine.session.options
    finally:
        (await browser.close())


async def test_cronet_declares_the_proxy_key_for_its_one_role(monkeypatch):
    """A client that names its options is checked against them before it is built."""
    install_declaring_cycronet(monkeypatch)
    assert accepted_option_keys("cronet", "http") == {
        "timeout_ms", "verify", "proxy"}
    # cycronet is an HTTP engine only; sockets belong to ``websockets``.
    assert accepted_option_keys("cronet", "ws") is None


async def test_real_network_fetch_is_delivered_on_page_queue(monkeypatch):
    install_fake_cycronet(monkeypatch)
    browser = (await Browser(real_network=True, http_engine="cronet", http_options={"verify": True}).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    # The response is delivered on the page task queue, never inline: even a
    # fixture-short round trip resolves after the current script has ended.
    assert page.evaluate(
        "fetch('/data').then(r => r.text()).then(v => window.__result = v), typeof window.__result"
    ) == "undefined"
    (await page.wait_for("typeof __result !== 'undefined'", timeout=2))
    assert page.evaluate("window.__result") == "cronet-ok"
    (await browser.close())


async def test_the_bridge_follows_only_a_follow_request(monkeypatch):
    """The load path inspects every hop itself, so only "follow" asks the engine to
    run a chain; "error" used to be followed by mistake."""
    install_fake_curl_cffi(monkeypatch)
    browser = (await Browser(real_network=True, stub_logging="off").__aenter__())
    try:
        engine = browser.new_profile().network_backend.http
        followed = {}
        for mode in ("follow", "manual", "error"):
            submit(engine, NetworkRequest("http://example.test/x", redirect=mode)).result(timeout=6)
            followed[mode] = engine.session.calls[-1][2]["allow_redirects"]
    finally:
        (await browser.close())
    assert followed == {"follow": False, "manual": False, "error": False}


async def test_allow_redirects_is_a_field_not_an_options_key(monkeypatch):
    """Client-side redirect following is always disabled."""
    from pyv8c.network.request_builder import finalize_request

    install_fake_curl_cffi(monkeypatch)
    browser = (await Browser(real_network=True, stub_logging="off").__aenter__())
    try:
        engine = browser.new_profile().network_backend.http
        submit(engine, NetworkRequest("http://example.test/x", redirect="follow",
                                           allow_redirects=False)).result(timeout=6)
        assert engine.session.calls[-1][2]["allow_redirects"] is False
        submit(engine, NetworkRequest("http://example.test/x", redirect="manual",
                                           allow_redirects=True)).result(timeout=6)
        assert engine.session.calls[-1][2]["allow_redirects"] is False
        legacy = finalize_request(url="http://example.test/x", redirect="follow",
                                  options={"allow_redirects": False})
        submit(engine, legacy).result(timeout=6)
        assert legacy.allow_redirects is False
        assert "allow_redirects" not in legacy.options
        assert engine.session.calls[-1][2]["allow_redirects"] is False
    finally:
        (await browser.close())


async def test_an_engine_that_cannot_stop_following_refuses_a_manual_redirect(monkeypatch):
    """A client that ignores allow_redirects=False cannot publish the followed result."""
    calls = []
    class FollowingClient(FakeClient):
        async def request(self, method, url, **kwargs):
            calls.append(url)
            response = FakeResponse()
            response.url = url
            response.history = ("http://example.test/first",)
            return response

    monkeypatch.setitem(sys.modules, "cycronet",
                        types.SimpleNamespace(AsyncCronetClient=FollowingClient))
    browser = (await Browser(real_network=True, http_engine="cronet", stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.evaluate(
            "window.__m = null;"
            "fetch('/x', {redirect: 'manual'}).then(() => { window.__m = 'resolved'; })"
            ".catch((e) => { window.__m = e.name + ': ' + e.message; });"
        )
        (await page.wait_for("window.__m !== null", timeout=3))
        # A page hears Chrome's wording for any network failure, and nothing else.
        assert page.evaluate("window.__m") == "TypeError: Failed to fetch"

        # The reason stays host-side, where a caller can act on it.
        from pyv8c.network.models import NetworkUnavailableError

        with pytest.raises(NetworkUnavailableError) as refused:
            page.services.resource_services.load_resource("/x", {"redirect": "manual"})
        assert "followed a redirect" in str(refused.value)
        assert len(calls) == 2
    finally:
        (await browser.close())


async def test_network_download_url_is_logged_at_debug_level(monkeypatch):
    install_fake_curl_cffi(monkeypatch)
    messages = []
    monkeypatch.setattr(
        "pyv8c.network.async_delivery.logger.debug",
        lambda message, *values: messages.append((message, values)),
    )
    browser = (await Browser(real_network=True).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("window.__downloadFinished=false; fetch('/download').then(r => r.text()).then(() => window.__downloadFinished=true)")
    (await page.wait_for("window.__downloadFinished", timeout=2))
    assert ("network download: {} {}", ("GET", "http://example.test/download")) in messages
    (await browser.close())


async def test_resource_loader_takes_precedence_over_real_network(monkeypatch):
    install_fake_curl_cffi(monkeypatch)
    browser = (await Browser(real_network=True, resource_loader=lambda url, request: Response("fixture", {"url": url})).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    assert page.evaluate("fetch('/data').then(r => r.text()).then(v => window.__result = v)") is not None
    (await page.wait_for("typeof __result !== 'undefined'", timeout=2))
    assert page.evaluate("window.__result") == "fixture"
    (await browser.close())


async def test_websocket_events_are_delivered_on_page_queue(monkeypatch):
    install_fake_ws_engine(monkeypatch)
    browser = (await Browser(stub_logging="off", ws_engine="fixture-sockets").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("window.__events=[]; const ws=new WebSocket('ws://example.test/'); ws.onopen=()=>__events.push('open'); ws.onmessage=e=>__events.push(e.data); ws.onclose=()=>__events.push('close'); window.__ws=ws")
    (await page.run_until_idle())
    assert page.evaluate("__events") == ["open"]
    page.evaluate("__ws.send('hello')")
    (await page.run_until_idle())
    assert page.evaluate("__events") == ["open", "hello"]
    page.evaluate("__ws.close(1000, 'done')")
    (await page.run_until_idle())
    assert page.evaluate("__events") == ["open", "hello", "close"]
    assert page.evaluate("[WebSocket.prototype.__proto__ === EventTarget.prototype, WebSocket.OPEN, WebSocket.CLOSED]") == [True, 1, 3]
    (await browser.close())


async def test_websocket_binary_types_and_blob_api(monkeypatch):
    install_fake_ws_engine(monkeypatch)
    browser = (await Browser(stub_logging="off", ws_engine="fixture-sockets").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("window.__binary=[]; const ws=new WebSocket('ws://example.test/'); ws.binaryType='blob'; ws.onopen=()=>ws.send(new Blob(['abc'], {type:'application/test'})); ws.onmessage=e=>__binary.push([e.data instanceof Blob,e.data instanceof ArrayBuffer,e.data instanceof Blob ? e.data.size : e.data.byteLength]); window.__ws=ws")
    (await page.run_until_idle())
    assert page.evaluate("__binary") == [[True, False, 3]]
    assert page.evaluate("new Blob(['hello'], {type:'text/plain'}).slice(1,4).text().then(v=>window.__blob=v)") is not None
    (await page.run_until_idle())
    assert page.evaluate("__blob") == "ell"
    page.evaluate("new Blob(['abc']).arrayBuffer().then(v=>window.__buffer=Array.from(new Uint8Array(v)).join(','))")
    (await page.run_until_idle())
    assert page.evaluate("__buffer") == "97,98,99"
    page.evaluate("__ws.binaryType='arraybuffer'; __ws.send(new Uint8Array([1,2,255]))")
    (await page.run_until_idle())
    assert page.evaluate("__binary[1]") == [False, True, 3]
    (await browser.close())


async def test_header_deduped_cookies_are_restored_from_raw_headers(monkeypatch):
    """Read repeated raw headers, preserving each cookie's security attributes."""

    class FakeCookieEntry:
        def __init__(self, name, value, path="/"):
            self.name, self.value, self.path = name, value, path

    class FakeResponseJar:
        def __init__(self, entries): self._entries = entries
        def iter_cookies(self): return iter(self._entries)

    class DedupedResponse:
        status_code = 202
        reason = ""
        headers = {"set-cookie": "FSSBBIl1UgzbN7N80T=first; Path=/"}
        _headers = {"set-cookie": ["FSSBBIl1UgzbN7N80T=first; Path=/", "FSSBBIl1UgzbN7N80S=second; Path=/"]}
        content = b""
        url = "http://example.test/page"
        history = ()
        cookies = FakeResponseJar([FakeCookieEntry("FSSBBIl1UgzbN7N80T", "first"),
                                   FakeCookieEntry("FSSBBIl1UgzbN7N80S", "second")])

    class DedupedClient(FakeClient):
        requests_sent = []
        async def request(self, method, url, **kwargs):
            DedupedClient.requests_sent.append(kwargs.get("headers") or {})
            return DedupedResponse()

    monkeypatch.setitem(sys.modules, "cycronet",
                        types.SimpleNamespace(AsyncCronetClient=DedupedClient))
    browser = (await Browser(real_network=True, http_engine="cronet", stub_logging="off").__aenter__())
    context = browser.new_profile()
    page = context.new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("window.__downloadFinished=false; fetch('/page').then(r => r.text()).then(() => window.__downloadFinished=true)")
    (await page.wait_for("window.__downloadFinished", timeout=2))
    header = context.network_backend.cookies.header_for("http://example.test/page")
    assert "FSSBBIl1UgzbN7N80T=first" in header
    assert "FSSBBIl1UgzbN7N80S=second" in header
    page.evaluate("window.__downloadFinished=false; fetch('/page2').then(r => r.text()).then(() => window.__downloadFinished=true)")
    (await page.wait_for("window.__downloadFinished", timeout=2))
    cookie = DedupedClient.requests_sent[-1].get("Cookie", "")
    assert "FSSBBIl1UgzbN7N80T=first" in cookie
    assert "FSSBBIl1UgzbN7N80S=second" in cookie
    (await browser.close())


async def test_cronet_suppresses_client_cookie_jar_and_preserves_raw_attributes(monkeypatch):
    """The client jar cannot bypass credentials or reconstruct security attributes."""
    calls = []

    class Client(FakeClient):
        async def request(self, method, url, **kwargs):
            calls.append(kwargs)
            response = FakeResponse()
            response.url = url
            response._headers = {'set-cookie': ['sid=bad; Secure; SameSite=Strict; Path=/']}
            return response

    monkeypatch.setitem(sys.modules, 'cycronet',
                        types.SimpleNamespace(AsyncCronetClient=Client))
    from pyv8c.network.cronet import CronetTransport
    transport = CronetTransport()
    try:
        submit(transport, NetworkRequest(url='http://example.test/', credentials='omit', redirect='manual')).result(timeout=6)
        assert calls[-1]['headers']['Cookie'] == ''
        assert calls[-1]['allow_redirects'] is False
        submit(transport, NetworkRequest(url='http://example.test/', credentials='include', redirect='manual')).result(timeout=6)
        assert transport.cookies.header_for('https://example.test/') == ''
    finally:
        transport.close()


async def test_cross_origin_cors_and_no_cors(monkeypatch):
    install_fake_cycronet(monkeypatch)
    class CORSClient(FakeClient):
        async def request(self, method, url, **kwargs):
            response = FakeResponse(); response.url = url
            response.headers = {"access-control-allow-origin": "http://example.test"}
            return response
    monkeypatch.setitem(sys.modules, "cycronet",
                        types.SimpleNamespace(AsyncCronetClient=CORSClient))
    browser = (await Browser(real_network=True, http_engine="cronet").__aenter__())
    page = browser.new_profile().new_page(); (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("fetch('http://other.test/data').then(r => r.text()).then(v => window.__cors = v)")
    (await page.wait_for("typeof __cors !== 'undefined'", timeout=2))
    assert page.evaluate("__cors") == "cronet-ok"
    page.evaluate("fetch('http://other.test/data', {mode:'no-cors'}).then(r => window.__opaque = [r.type, r.status])")
    (await page.wait_for("typeof __opaque !== 'undefined'", timeout=2))
    assert page.evaluate("__opaque") == ["opaque", 0]
    (await browser.close())


async def test_a_worker_navigator_comes_from_its_feature():
    """The worker global asks the realm for Navigator instead of importing it."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.evaluate("""(() => {
            const source = new Blob([
                "postMessage([navigator === navigator, navigator.userAgent.length > 0]);"
            ], {type: 'text/javascript'});
            const worker = new Worker(URL.createObjectURL(source));
            worker.onmessage = event => window.__workerNavigator = event.data;
        })()""")
        (await page.wait_for("typeof __workerNavigator !== 'undefined'", timeout=2))
        assert page.evaluate("__workerNavigator") == [True, True]
        assert "navigator" in browser.bindings.runtime.loaded_features
    finally:
        (await browser.close())


async def test_worker_executes_blob_script_and_uses_independent_queue():
    browser = (await Browser().__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    result = page.evaluate("(() => { const src = new Blob([\"self.onmessage = e => self.postMessage({value:e.data.value + 1, bytes:e.data.bytes, helper:typeof __pyv8c_worker_value, windowProps:typeof WindowProperties});\"], {type:'text/javascript'}); const url=URL.createObjectURL(src); const w=new Worker(url); window.__workerInstance=w; w.onmessage=e=>window.__worker=e.data; w.onerror=e=>window.__workerError=e.message; w.postMessage({value:4, bytes:new Uint8Array([1,2,3])}); return [url.startsWith('blob:'), w instanceof Worker]; })()")
    assert result == [True, True]
    (await page.wait_for("typeof __worker !== 'undefined' || typeof __workerError !== 'undefined'", timeout=2))
    assert page.evaluate("[typeof __worker, typeof __workerError]") == ["object", "undefined"]
    assert page.evaluate("__worker.value") == 5
    assert page.evaluate("[__worker.helper, __worker.windowProps]") == ["undefined", "undefined"]
    assert page.evaluate("Array.from(new Uint8Array(__worker.bytes))") == [1, 2, 3]
    page.evaluate("URL.revokeObjectURL(URL.createObjectURL(new Blob(['x'])))")
    page.evaluate("window.__workerBeforeTerminate = __worker; window.__workerInstance.terminate()")
    (await page.run_until_idle())
    assert page.evaluate("__worker === __workerBeforeTerminate") is True
    (await browser.close())


async def test_worker_self_is_the_context_global_without_explicit_expose():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("""(() => {
      const source = new Blob([
        "postMessage([self === globalThis, typeof self, typeof globalThis, typeof window, Object.prototype.hasOwnProperty.call(globalThis, 'self')]);"
      ], {type: 'text/javascript'});
      const worker = new Worker(URL.createObjectURL(source));
      worker.onmessage = event => window.__workerGlobals = event.data;
    })()""")
    (await page.wait_for("typeof __workerGlobals !== 'undefined'", timeout=2))
    assert page.evaluate("__workerGlobals") == [True, "object", "object", "undefined", False]
    (await browser.close())


# -- choosing, composing and extending engines --------------------------


class FakeCurlHeaders(dict):
    """curl_cffi's header view: a mapping that still knows repeated names."""

    def __init__(self, items):
        super().__init__()
        self._items = list(items)

    def multi_items(self):
        return list(self._items)


class FakeCurlResponse:
    status_code = 200
    reason = "OK"
    url = "http://example.test/data"
    history = ()
    elapsed = None
    content = b"curl-ok"
    text = "curl-ok"

    def __init__(self, items=None):
        self.headers = FakeCurlHeaders(items or [("content-type", "text/plain")])


class FakeCurlSession:
    """What the curl_cffi bridge talks to; one instance per context."""

    def __init__(self, **options):
        self.options = options
        self.calls = []
        self.closed = False
        self.items = [("content-type", "text/plain")]

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeCurlResponse(self.items)

    async def close(self):
        self.closed = True


def install_fake_curl_cffi(monkeypatch, session_type=FakeCurlSession):
    """Put a fake ``curl_cffi.requests`` in place of the optional package."""
    requests = types.SimpleNamespace(AsyncSession=session_type)
    monkeypatch.setitem(sys.modules, "curl_cffi", types.SimpleNamespace(requests=requests))
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", requests)


class DeclaringCurlSession(FakeCurlSession):
    """A session whose signature names the options it takes."""

    def __init__(self, *, impersonate=None, default_headers=False, loop=None):
        super().__init__(impersonate=impersonate, default_headers=default_headers,
                         loop=loop)


class SelfCheckingCurlSession:
    """What curl_cffi really does: no key list, but it refuses the key itself."""

    _KNOWN = frozenset({"impersonate", "default_headers", "loop"})

    def __init__(self, **options):
        unknown = sorted(set(options) - self._KNOWN)
        if unknown:
            raise TypeError(
                f"BaseSession.__init__() got an unexpected keyword argument {unknown[0]!r}"
            )
        self.options = options
        self.calls = []
        self.closed = False
        self.items = [("content-type", "text/plain")]

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeCurlResponse(self.items)

    async def close(self):
        self.closed = True


async def test_the_default_stack_is_curl_cffi_and_websockets(monkeypatch):
    install_fake_curl_cffi(monkeypatch)
    browser = (await Browser(real_network=True, stub_logging="off").__aenter__())
    try:
        backend = browser.new_profile().network_backend
        assert type(backend).__name__ == "NetworkBackend"
        assert type(backend.http).__name__ == "CurlCffiTransport"
        assert type(backend.ws).__name__ == "WebSocketsTransport"
        assert (backend.http_available, backend.ws_available) == (True, True)
    finally:
        (await browser.close())


async def test_naming_the_engines_selects_each_role(monkeypatch):
    install_fake_cycronet(monkeypatch)
    install_fake_curl_cffi(monkeypatch)
    install_fake_ws_engine(monkeypatch)
    browser = (await Browser(stub_logging="off", http_engine="curl_cffi", ws_engine="fixture-sockets").__aenter__())
    try:
        backend = browser.new_profile().network_backend
        assert type(backend.http).__name__ == "CurlCffiTransport"
        assert type(backend.ws).__name__ == "FakeWebSocketEngine"
    finally:
        (await browser.close())


async def test_naming_one_role_assembles_exactly_that_role(monkeypatch):
    install_fake_curl_cffi(monkeypatch)
    browser = (await Browser(stub_logging="off", http_engine="curl_cffi", ws_engine=None).__aenter__())
    try:
        backend = browser.new_profile().network_backend
        assert (backend.http_available, backend.ws_available) == (True, False)
    finally:
        (await browser.close())

    install_fake_ws_engine(monkeypatch)
    browser = (await Browser(stub_logging="off", http_engine=None, ws_engine="fixture-sockets").__aenter__())
    try:
        backend = browser.new_profile().network_backend
        assert (backend.http_available, backend.ws_available) == (False, True)
    finally:
        (await browser.close())


async def test_a_context_without_an_http_engine_says_so(monkeypatch):
    from pyv8c.foundation.host_errors import LoadFailure
    from pyv8c.network.models import NetworkRequest, NetworkUnavailableError

    install_fake_ws_engine(monkeypatch)
    browser = (await Browser(stub_logging="off", http_engine=None, ws_engine="fixture-sockets").__aenter__())
    try:
        context = browser.new_profile()
        # The host API reports it as the host's own failure to load, not as a crash
        # somewhere deeper in a page's fetch.
        with pytest.raises(LoadFailure, match="HTTP engine"):
            (await context.new_page().net.get("http://example.test/"))
        # And the assembled backend refuses a request itself: there is no engine
        # behind it to hand the work to.
        with pytest.raises(NetworkUnavailableError, match="no HTTP engine"):
            context.network_backend.submit(None, None, NetworkRequest("http://example.test/"), lambda: None)
    finally:
        (await browser.close())


async def test_curl_cffi_transport_forwards_options_and_keeps_every_cookie(monkeypatch):
    """The bridge honours transport options and owns cookies itself."""
    install_fake_curl_cffi(monkeypatch)
    browser = (await Browser(
        stub_logging="off",
        http_engine="curl_cffi",
        http_options={"impersonate": "chrome", "max_clients": 3},
    ).__aenter__())
    try:
        context = browser.new_profile()
        session = context.network_backend.http.session
        assert session.options["impersonate"] == "chrome"
        # The transport adds no headers of its own: the caller decides them all.
        assert session.options["default_headers"] is False
        assert session.options["max_clients"] == 3

        session.items = [
            ("content-type", "text/plain"),
            ("set-cookie", "first=1; Path=/"),
            ("set-cookie", "second=2; Path=/"),
        ]
        page = context.new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.evaluate("fetch('/data').then(r => r.text()).then(v => window.__body = v)")
        (await page.wait_for("typeof __body !== 'undefined'", timeout=2))

        assert page.evaluate("__body") == "curl-ok"
        method, url, kwargs = session.calls[-1]
        assert (method, url) == ("GET", "http://example.test/data")
        # curl's own jar is discarded: the context's jar is the single store.
        assert kwargs["discard_cookies"] is True

        # Repeated Set-Cookie headers arrive through ``multi_items``; a client
        # that folds them into a dict would silently keep only the first.
        jar = context.network_backend.cookies.header_for("http://example.test/data")
        assert "first=1" in jar and "second=2" in jar

        page.evaluate("window.__downloadFinished=false; fetch('/again').then(r => r.text()).then(() => window.__downloadFinished=true)")
        (await page.wait_for("window.__downloadFinished", timeout=2))
        sent = session.calls[-1][2]["headers"]
        assert "first=1" in sent["Cookie"] and "second=2" in sent["Cookie"]
    finally:
        (await browser.close())


async def test_unknown_engine_names_what_is_registered(monkeypatch):
    from pyv8c import NetworkUnavailableError, transport_names

    browser = (await Browser(stub_logging="off", http_engine="does-not-exist").__aenter__())
    try:
        with pytest.raises(NetworkUnavailableError) as failure:
            browser.new_profile()
    finally:
        (await browser.close())
    message = str(failure.value)
    assert "does-not-exist" in message
    for name in transport_names():
        assert name in message


async def test_missing_curl_cffi_names_the_optional_package(monkeypatch):
    from pyv8c import NetworkUnavailableError

    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr("pyv8c.network.curl_cffi.importlib.import_module", missing)
    browser = (await Browser(stub_logging="off", http_engine="curl_cffi").__aenter__())
    try:
        with pytest.raises(NetworkUnavailableError, match="curl_cffi"):
            browser.new_profile()
    finally:
        (await browser.close())


async def test_a_registered_transport_serves_the_context(monkeypatch):
    """A host-supplied engine is reachable by name, which is the extension point."""
    from pyv8c import register_transport

    class StubBackend:
        def __init__(self):
            self.cookies = None
            self.requests = []

        def submit_request(self, request, handle, remaining):
            self.requests.append(request)
            return resolved(FakeResponse())

        def close(self):
            return None

    register_transport("fixture-engine", lambda options: StubBackend())
    # An engine that declares no key set is not validated: what its options mean
    # is the host's business, and the registry has no way to know.
    browser = (await Browser(stub_logging="off", http_engine="fixture-engine", http_options={"anything": 1}).__aenter__())
    try:
        backend = browser.new_profile().network_backend
        assert type(backend.http).__name__ == "StubBackend"
    finally:
        (await browser.close())


# -- network options -----------------------------------------------------


async def test_option_keys_come_from_the_client_signature():
    from pyv8c.network.registry import signature_option_keys

    class SessionParams(TypedDict, total=False):
        impersonate: str
        verify: bool

    class Named:
        def __init__(self, *, timeout_ms=1000, **kwargs: SessionParams):
            self.options = kwargs

    class Swallows:
        def __init__(self, **options):
            self.options = options

    # The client is the authority on its own options, and a TypedDict is how it
    # names the ones it takes through ``**kwargs``.
    assert signature_option_keys(Named) == frozenset({"timeout_ms", "impersonate", "verify"})
    # Naming nothing is not a list of no keys: there is nothing to check against,
    # so the engine stays unvalidated instead of rejecting everything.
    assert signature_option_keys(Swallows) is None


async def test_a_misspelt_option_is_refused_when_the_context_is_created(monkeypatch):
    """Each role's options are checked against the engine that reads them."""
    install_declaring_cycronet(monkeypatch)
    browser = (await Browser(
        real_network=True, http_engine="cronet",
        http_options={"timeout_ms": 5000, "impersonete": "chrome"},
    ).__aenter__())
    try:
        with pytest.raises(TypeError) as failure:
            browser.new_profile()
    finally:
        (await browser.close())
    message = str(failure.value)
    assert "'impersonete'" in message
    assert "cronet" in message
    # The keys that do exist are listed, so the typo is easy to correct.
    assert "timeout_ms" in message and "verify" in message


async def test_an_option_belonging_to_another_engine_is_refused(monkeypatch):
    install_declaring_cycronet(monkeypatch)
    browser = (await Browser(real_network=True, http_engine="cronet", http_options={"impersonate": "chrome"}).__aenter__())
    try:
        with pytest.raises(TypeError, match="'impersonate'"):
            browser.new_profile()
    finally:
        (await browser.close())


async def test_a_missing_dependency_is_reported_before_the_options_are_checked(monkeypatch):
    from pyv8c import NetworkUnavailableError

    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr("pyv8c.network.cronet.importlib.import_module", missing)
    browser = (await Browser(real_network=True, http_engine="cronet", http_options={"impersonete": "chrome"}).__aenter__())
    try:
        with pytest.raises(NetworkUnavailableError, match="cycronet"):
            browser.new_profile()
    finally:
        (await browser.close())


async def test_each_role_is_handed_its_own_options(monkeypatch):
    install_fake_curl_cffi(monkeypatch)
    install_fake_ws_engine(monkeypatch)
    browser = (await Browser(
        stub_logging="off",
        http_engine="curl_cffi", http_options={"impersonate": "chrome", "max_clients": 3},
        ws_engine="fixture-sockets", ws_options={"fixture": "socket"},
    ).__aenter__())
    try:
        backend = browser.new_profile().network_backend
        # Each engine saw its own dictionary and nothing of the other's.
        assert backend.http.session.options["impersonate"] == "chrome"
        assert backend.http.session.options["max_clients"] == 3
        assert backend.ws.options == {"fixture": "socket"}
    finally:
        (await browser.close())


async def test_options_without_an_engine_to_read_them_are_refused():
    browser = (await Browser(stub_logging="off", real_network=False, http_options={"max_workers": 2}).__aenter__())
    try:
        with pytest.raises(TypeError, match="no network engine"):
            browser.new_profile()
    finally:
        (await browser.close())


class InjectedBackend:
    """Just enough of an HTTP engine for the injection path to accept it."""

    def submit_request(self, request, handle, remaining):
        raise NotImplementedError


    def close(self):
        return None


async def test_options_cannot_configure_a_ready_engine(monkeypatch):
    install_fake_ws_engine(monkeypatch)
    for ready in (InjectedBackend(), lambda: InjectedBackend()):
        browser = (await Browser(stub_logging="off", http_engine=ready, http_options={"max_workers": 2}).__aenter__())
        try:
            with pytest.raises(TypeError, match="ready"):
                browser.new_profile()
        finally:
            (await browser.close())


async def test_a_ready_engine_fills_its_own_role(monkeypatch):
    install_fake_ws_engine(monkeypatch)
    ready = InjectedBackend()
    browser = (await Browser(stub_logging="off", http_engine=ready, ws_engine="fixture-sockets").__aenter__())
    try:
        backend = browser.new_profile().network_backend
        assert backend.http is ready
        # The role it was not given keeps its own engine.
        assert type(backend.ws).__name__ == "FakeWebSocketEngine"
    finally:
        (await browser.close())


async def test_a_factory_builds_one_engine_per_context():
    made = []

    def factory():
        engine = InjectedBackend()
        made.append(engine)
        return engine

    browser = (await Browser(stub_logging="off", http_engine=factory).__aenter__())
    try:
        first = browser.new_profile().network_backend.http
        second = browser.new_profile().network_backend.http
        assert first is not second
        assert made == [first, second]
    finally:
        (await browser.close())


async def test_an_object_in_both_roles_is_used_once():
    class BothBackend(InjectedBackend):
        def websocket(self, tasks, url, protocols, callbacks, options=None):
            raise NotImplementedError

    shared = BothBackend()
    browser = (await Browser(stub_logging="off", http_engine=shared, ws_engine=shared).__aenter__())
    try:
        backend = browser.new_profile().network_backend
        assert backend.http is shared and backend.ws is shared
    finally:
        (await browser.close())


async def test_a_ready_engine_must_serve_the_role_it_is_given():
    for role in ("http_engine", "ws_engine"):
        browser = (await Browser(stub_logging="off", **{role: object()}).__aenter__())
        try:
            with pytest.raises(TypeError, match=f"{role} must be"):
                browser.new_profile()
        finally:
            (await browser.close())


async def test_real_network_false_refuses_a_ready_engine():
    browser = (await Browser(stub_logging="off", real_network=False, http_engine=InjectedBackend()).__aenter__())
    try:
        with pytest.raises(TypeError, match="real_network=False"):
            browser.new_profile()
    finally:
        (await browser.close())

    # A name is only a preference: it is simply unused.
    browser = (await Browser(stub_logging="off", real_network=False, http_engine="cronet").__aenter__())
    try:
        assert browser.new_profile().network_backend is None
    finally:
        (await browser.close())


async def test_curl_cffi_options_are_validated_when_the_session_declares_them(monkeypatch):
    install_fake_curl_cffi(monkeypatch, session_type=DeclaringCurlSession)
    browser = (await Browser(
        stub_logging="off", http_engine="curl_cffi",
        http_options={"impersonate": "chrome"},
    ).__aenter__())
    try:
        session = browser.new_profile().network_backend.http.session
        assert session.options["impersonate"] == "chrome"
        assert session.options["default_headers"] is False
    finally:
        (await browser.close())

    browser = (await Browser(stub_logging="off", http_engine="curl_cffi", http_options={"impersonete": "chrome"}).__aenter__())
    try:
        with pytest.raises(TypeError, match="'impersonete'"):
            browser.new_profile()
    finally:
        (await browser.close())


async def test_a_session_without_a_key_list_is_left_to_refuse_its_own_options(monkeypatch):
    """curl_cffi keeps its parameter TypedDict for type checkers only."""
    install_fake_curl_cffi(monkeypatch, session_type=SelfCheckingCurlSession)
    browser = (await Browser(stub_logging="off", http_engine="curl_cffi", http_options={"impersonete": "chrome"}).__aenter__())
    try:
        with pytest.raises(TypeError) as failure:
            browser.new_profile()
    finally:
        (await browser.close())
    message = str(failure.value)
    # The session's own refusal is kept, and now says where the option came from.
    assert "http_options" in message
    assert "impersonete" in message


async def test_a_registered_engine_may_declare_the_keys_it_accepts():
    from pyv8c import register_transport

    seen = {}

    class KeyedBackend(InjectedBackend):
        def __init__(self, options):
            seen["options"] = options

    register_transport(
        "keyed-engine", lambda options: KeyedBackend(options),
        option_keys=lambda: frozenset({"flavour"}),
    )
    browser = (await Browser(stub_logging="off", http_engine="keyed-engine", http_options={"flavour": "mint"}).__aenter__())
    try:
        browser.new_profile()
        assert seen["options"] == {"flavour": "mint"}
    finally:
        (await browser.close())

    browser = (await Browser(stub_logging="off", http_engine="keyed-engine", http_options={"flavor": "mint"}).__aenter__())
    try:
        with pytest.raises(TypeError, match="'flavor'"):
            browser.new_profile()
    finally:
        (await browser.close())
