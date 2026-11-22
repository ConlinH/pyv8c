"""The two built-in HTTP engines, measured on the same local wire.

Chrome's absolute header baselines belong to ``test_request_headers_on_the_wire.py``
and the repros harness.  Here the question is narrower and complementary: does
the *same* scene, recorded by a real server, reach both engines with the same
framework headers, the same CookieJar truth, the same bodies and the same
negotiation capabilities?  Transport-placed headers, Cronet's injected empty
``Cookie`` and TLS GREASE values are normalised away rather than pinned.

Every recorder is fixture-local, so ``--dist loadscope`` can place this module
on any worker.  Neither engine name touches an external host: origins are two
addresses in the loopback /8 block and the TLS listener never answers.
"""
import asyncio
import json
import os
import threading
import time
from concurrent.futures import Future
from contextlib import asynccontextmanager
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from _http_fixtures import submit
from pyv8c import RequestTimeout

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import LoadFailure
from pyv8c.foundation.load_handle import LoadHandle
from pyv8c.network import registry
from pyv8c.network.async_delivery import AsyncTransport
from pyv8c.network.http_scheduler import HttpScheduler
from pyv8c.network.models import NetworkRequest, NetworkResponse
from pyv8c.platform.resources.cache import ResourceCache
from pyv8c.platform.resources.session import ResourceRequestSession
from pyv8c.platform.services import _TaskCapability
from pyv8c.web.fetch.xhr import XMLHttpRequest
from wire_parity_harness import (
    CAPABILITIES_REQUIRED,
    HelloRecorder,
    RecordingOrigins,
    RecordingProxy,
    framework_headers,
)

ENGINES = ("curl_cffi", "cronet")

#: Framework headers a navigation must put on the wire, regardless of engine.
NAVIGATION_HEADERS = frozenset({
    "user-agent", "accept", "accept-language", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "upgrade-insecure-requests",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user",
    "accept-encoding",
})


def _require(name):
    """Skip just this engine's cases when its optional client is absent."""
    if name == "cronet":
        pytest.importorskip("cycronet", reason="the Cronet transport is optional")
    else:
        pytest.importorskip("curl_cffi", reason="the curl_cffi engine is optional")


class LocalHttpTransport(AsyncTransport):
    """A custom single-hop transport for the loopback GET and OPTIONS cases."""

    def _open_session(self):
        pass

    async def _close_session(self):
        pass

    async def _single_call(self, request):
        url = urlsplit(request.url)
        assert url.scheme == "http" and url.hostname == "127.0.0.1"
        assert request.body is None
        reader, writer = await asyncio.open_connection(url.hostname, url.port)
        try:
            target = url.path or "/"
            if url.query:
                target += "?" + url.query
            lines = [f"{request.method} {target} HTTP/1.1", f"Host: {url.netloc}", "Connection: close"]
            lines.extend(f"{name}: {value}" for name, value in request.headers.items()
                         if name.lower() not in {"host", "connection"})
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            await writer.drain()
            status_line, *lines = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").splitlines()
            _, status, reason = status_line.split(" ", 2)
            headers = dict((name.lower(), value.strip()) for name, value in
                           (line.split(":", 1) for line in lines if line))
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            return NetworkResponse(request.url, int(status), reason, headers, body)
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.fixture(params=(*ENGINES, LocalHttpTransport), ids=(*ENGINES, "custom"))
def submission_engine(request):
    if isinstance(request.param, str):
        _require(request.param)
    return request.param


@pytest.fixture(params=ENGINES)
def engine_name(request):
    _require(request.param)
    return request.param


@pytest.fixture
def origins():
    with RecordingOrigins() as pair:
        yield pair


@asynccontextmanager
async def _page(engine_name, origins, **options):
    """A navigated page on one engine; the browser is closed before returning."""
    browser = await Browser(
        stub_logging="off", http_engine=engine_name,
        default_headers={"X-Parity": "kept"}, **options,
    ).__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.goto(f"{origins.same.url}/page", wait_until="load")
        yield page
    finally:
        await browser.close()


@lru_cache(maxsize=2)
def _hello_for(name):
    """One real ClientHello per engine; the frozen result is safe to reuse."""
    _require(name)
    recorder = HelloRecorder()
    transport = None
    try:
        transport = registry.create_transport(name)
        try:
            submit(transport, NetworkRequest(recorder.url, timeout=5)).result(timeout=6)
        except Exception:
            # The listener never answers; the ClientHello is the evidence.
            pass
        return recorder.wait(6)
    finally:
        if transport is not None:
            transport.close()
        recorder.close()


async def test_navigation_and_host_headers_survive_the_engine(engine_name, origins):
    async with _page(engine_name, origins) as page:
        await page.net.get("/echo")
    navigation = origins.same.sent("/page")
    hosted = origins.same.sent("/echo")
    framework = framework_headers(navigation)

    assert NAVIGATION_HEADERS <= set(framework), "a framework header was dropped"
    assert all(framework.values()), "a framework header arrived empty"
    assert {"host", "connection"} <= set(navigation.names()), (
        "the transport still has to place its own headers")
    assert framework_headers(hosted)["x-parity"] == "kept", (
        "a caller-supplied default header must not be lost")


async def test_both_engines_agree_on_framework_headers(origins):
    """The normalised comparison the Chrome absolute baselines cannot express."""
    snapshots = {}
    for name in ENGINES:
        _require(name)
        origins.same.clear()
        async with _page(name, origins) as page:
            await page.net.get("/echo")
        snapshots[name] = (
            framework_headers(origins.same.sent("/page")),
            framework_headers(origins.same.sent("/echo")),
        )
    assert snapshots["curl_cffi"] == snapshots["cronet"], (
        "the two engines must not change the headers the framework owns")


async def test_the_context_cookie_jar_is_the_only_truth(engine_name, origins):
    async with _page(engine_name, origins) as page:
        await page.net.get("/set")
        await page.net.get("/many")
        cookies = set(page.evaluate("document.cookie").split("; "))
        echoed = (await page.net.get("/echo")).json()

    assert cookies == {"a=1", "b=2", "m1=1", "m2=2"}, (
        "both Set-Cookie headers of each response must reach the one jar")
    assert set((echoed["cookie"] or "").split("; ")) == cookies, (
        "the wire carries the jar's value, not a second client-side jar")


async def test_a_cross_domain_chain_drops_cookies_and_intermediate_bodies(
        engine_name, origins):
    async with _page(engine_name, origins) as page:
        await page.net.get("/set")
        hop = await page.net.get("/hop1")

    assert hop.status == 200 and hop.url.endswith("/hop2"), (
        "the chain really reached the second origin")
    assert "first-hop-body-must-not-leak" not in hop.text, (
        "only the final hop's body is delivered")
    first_hop = origins.same.sent("/hop1").sent().get("cookie") or ""
    cross_hop = origins.cross.sent("/hop2").sent().get("cookie") or ""
    assert first_hop.strip(), "the first hop belongs to the jar's domain"
    if origins.cross_domain:
        assert not cross_hop.strip(), (
            "a genuinely cross-domain hop must not carry its cookies")
    # Ports do not isolate cookies; the fallback origin may be the same host.


async def test_compressed_and_empty_bodies_are_readable(engine_name, origins):
    async with _page(engine_name, origins) as page:
        compressed = await page.net.get("/gzip")
        empty = await page.net.get("/nocontent")
    assert compressed.text == "compressed body", "gzip stays decoded"
    assert empty.status == 204
    assert empty.content == b""


async def test_post_bodies_and_redirect_chains(engine_name, origins, monkeypatch):
    async with _page(engine_name, origins) as page:
        posted = (await page.net.post("/echo", json={"a": 1})).json()
        backend = page.profile.network_backend
        submit_hop = backend.submit
        hops = []

        def record_hop(tasks, group, request, remaining, **options):
            hops.append((urlsplit(request.url).path, remaining, options["host"]))
            return submit_hop(tasks, group, request, remaining, **options)

        monkeypatch.setattr(backend, "submit", record_hop)
        redirected = await page.net.get("/redirect")

    assert [path for path, _, _ in hops] == ["/redirect", "/echo"]
    assert hops[0][1] is hops[1][1] and all(host for _, _, host in hops)
    assert json.loads(posted["body"]) == {"a": 1}
    assert posted["content_type"] == "application/json"
    record = origins.same.sent("/echo", "POST")
    assert record.body == b'{"a": 1}'
    assert "content-length" in record.names()

    assert redirected.status == 200
    assert redirected.url.endswith("/echo")
    assert "redirect-body-must-not-leak" not in redirected.text, (
        "only the final hop's body is delivered")
    assert any(item.path == "/redirect" for item in origins.same.records()), (
        "the intermediate hop really happened on the wire")


async def test_redirect_recomputes_path_cookies_and_stores_the_intermediate_response(engine_name, origins):
    async with _page(engine_name, origins) as page:
        page.evaluate("document.cookie='scoped=1; Path=/scoped'")
        response = await page.net.get("/scoped/start")
        assert response.redirected and response.json()["cookie"] == "fresh=1"
        assert origins.same.sent("/scoped/start").sent()["cookie"] == "scoped=1"
        assert (await page.net.get("/scoped/other")).status == 200
        cookies = origins.same.sent("/scoped/other").sent()["cookie"]
        assert set(cookies.split("; ")) == {"scoped=1", "fresh=1", "fresh_scoped=1"}


async def test_disabling_redirects_does_not_send_the_second_hop(engine_name, origins):
    async with _page(engine_name, origins) as page:
        response = await page.net.get("/scoped/start", options={"allow_redirects": False})
        assert response.status == 302 and not response.redirected
        assert [record.path for record in origins.same.records()] == ["/page", "/scoped/start"]
        assert (await page.net.get("/echo")).json()["cookie"] == "fresh=1"


async def test_profile_backend_owns_navigation_window_worker_and_host_http(
        submission_engine, origins, monkeypatch):
    address = origins.same.url
    sent = []
    async with Browser(http_engine=submission_engine, ws_engine=None, stub_logging="off") as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        assert not hasattr(page, "network_backend")
        backend = profile.network_backend
        engine = backend.http
        start = backend._start

        def capture(job):
            sent.append((urlsplit(job.request.url).path, job.lane, job.host, job.group))
            return start(job)

        monkeypatch.setattr(backend, "_start", capture)
        await page.goto(address + "/page", wait_until="commit")
        assert (await page.net.get("/echo")).status == 200
        assert (await page.net.fetch("/final")).status == 200
        script = f"importScripts({json.dumps(address + '/worker-import.js')}); postMessage(imported)"
        page.evaluate(f"globalThis.worker = new Worker(URL.createObjectURL(new Blob([{json.dumps(script)}], "
                      "{type:'text/javascript'})));"
                      "worker.onmessage = event => globalThis.workerResult = event.data;")
        await page.wait_for("globalThis.workerResult === 42", timeout=3)
        await profile.close()
        assert profile.network_backend is None and backend.http is None and engine._closed

    assert sent == [
        ("/page", "navigation", False, id(page)),
        ("/echo", "ordinary", True, id(page)),
        ("/final", "ordinary", False, id(page)),
        ("/worker-import.js", "blocking", False, id(page)),
    ]


@pytest.mark.parametrize("replace", ["navigation", "document-open", "worker-navigation"])
async def test_document_cutover_cancels_a_prepared_redirect(submission_engine, origins, monkeypatch, replace):
    next_ready, release_next, next_submitted = (threading.Event() for _ in range(3))
    submit_hop = ResourceRequestSession.submit
    address = origins.same.url

    def gate_redirect(session, tasks, load, request, **options):
        if request.url != address + "/echo":
            return submit_hop(session, tasks, load, request, **options)
        next_ready.set()
        assert release_next.wait(3)
        try:
            return submit_hop(session, tasks, load, request, **options)
        finally:
            next_submitted.set()

    monkeypatch.setattr(ResourceRequestSession, "submit", gate_redirect)
    async with Browser(http_engine=submission_engine, ws_engine=None, stub_logging="off") as browser:
        page = browser.new_page()
        await page.set_content("<p>old</p>", address + "/")
        backend = page.profile.network_backend
        original_start = backend._start
        sent = []

        def start(job):
            sent.append(job.request.url)
            return original_start(job)

        monkeypatch.setattr(backend, "_start", start)
        old_generation = page.navigation.generation
        retire = page.resource_ownership.cancel_document_requests

        def at_cutover(**options):
            if page.navigation.generation != old_generation:
                # Reproduce admission in the window between publishing the new
                # generation and cancelling the old load.
                release_next.set()
                assert next_submitted.wait(2)
            retire(**options)

        monkeypatch.setattr(page.resource_ownership, "cancel_document_requests", at_cutover)
        delivered = []
        handle = None
        if replace == "worker-navigation":
            cancel = page.net.cancel_document_requests

            def after_cutover():
                assert page.navigation.generation != old_generation
                release_next.set()
                assert next_submitted.wait(2)
                cancel()

            monkeypatch.setattr(page.net, "cancel_document_requests", after_cutover)
            script = f"fetch({json.dumps(address + '/redirect')}).catch(() => {{}})"
            page.evaluate(f"globalThis.worker = new Worker(URL.createObjectURL(new Blob([{json.dumps(script)}], "
                          "{type:'text/javascript'})))")
        else:
            handle = page.executor.call(page.resource_fetcher.load_resource_async, "/redirect", {},
                                        lambda *answer: delivered.append(answer))
        try:
            assert await asyncio.to_thread(next_ready.wait, 3)
            if replace == "document-open":
                page.evaluate("document.open(); void 0")
            else:
                await page.set_content("<p>new</p>", address + "/new")
            if handle is not None:
                assert handle.cancelled
        finally:
            release_next.set()
        assert await asyncio.to_thread(next_submitted.wait, 3)
        await page.run_until_idle(timeout=3)
        assert delivered == [] and sent == [address + "/redirect"]
    assert [record.path for record in origins.same.records()] == ["/redirect"]


async def test_completed_host_error_survives_a_busy_runtime(submission_engine, origins, monkeypatch):
    failure = OSError("real transport failure")
    started, fail_now, settled = (threading.Event() for _ in range(3))
    errors = []
    settle = HttpScheduler._settle

    async def fail(_request):
        started.set()
        assert await asyncio.to_thread(fail_now.wait, 3)
        raise failure

    def capture(scheduler, job, *, result=None, error=None):
        settle(scheduler, job, result=result, error=error)
        if job.request.url.endswith("/fail"):
            errors.append((error, job.remaining()))
            settled.set()

    async with Browser(http_engine=submission_engine, ws_engine=None, stub_logging="off") as browser:
        page = browser.new_page()
        await page.set_content("<p>local</p>", origins.same.url + "/")
        await page.net.get("/page")
        monkeypatch.setattr(page.profile.network_backend.http, "_single_call", fail)
        monkeypatch.setattr(HttpScheduler, "_settle", capture)
        observer = asyncio.create_task(page.net.get("/fail", timeout=.5))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            fail_now.set()
            # Keep the Runtime busy even though transport settlement is independent.
            assert settled.wait(2)
            original, remaining = errors[0]
            assert remaining > 0
            time.sleep(remaining + .05)
            with pytest.raises(type(original)) as observed:
                await observer
            assert observed.value is original
            assert len(errors) == 1
        finally:
            fail_now.set()
            await asyncio.gather(observer, return_exceptions=True)


@pytest.mark.parametrize("first", ["transport", "cache"])
async def test_profile_close_waits_for_cleanup_without_publishing_cancelled_cache(
        submission_engine, origins, monkeypatch, tmp_path, first):
    cache_ready, release_cache, cache_finished = (threading.Event() for _ in range(3))
    transport_started, in_cleanup, release_transport = (threading.Event() for _ in range(3))
    claim = LoadHandle.claim_publication
    put = ResourceCache.put

    def gate_publication(handle):
        cache_ready.set()
        assert release_cache.wait(5)
        return claim(handle)

    def track_write(cache, *args):
        try:
            put(cache, *args)
        finally:
            cache_finished.set()

    monkeypatch.setattr(LoadHandle, "claim_publication", gate_publication)
    monkeypatch.setattr(ResourceCache, "put", track_write)
    async with Browser(http_engine=submission_engine, ws_engine=None, stub_logging="off",
                       cache_dir=tmp_path) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        await page.set_content("<p>local</p>", origins.same.url + "/")
        original_call = page.profile.network_backend.http._single_call

        async def hold_cleanup(request):
            if not request.url.endswith("/hold"):
                return await original_call(request)
            transport_started.set()
            try:
                await asyncio.Future()
            finally:
                in_cleanup.set()
                assert await asyncio.to_thread(release_transport.wait, 5)

        monkeypatch.setattr(page.profile.network_backend.http, "_single_call", hold_cleanup)
        delivered = []
        handle = page.executor.call(page.resource_fetcher.load_resource_async, "/echo", {},
                                    lambda *answer: delivered.append(answer))
        observer = closing = None
        try:
            assert await asyncio.to_thread(cache_ready.wait, 3)
            observer = asyncio.create_task(page.net.get("/hold"))
            assert await asyncio.to_thread(transport_started.wait, 3)
            closing = asyncio.create_task(profile.close())
            assert await asyncio.to_thread(in_cleanup.wait, 3)
            assert handle.cancelled
            if first == "transport":
                release_transport.set()
                with pytest.raises(LoadFailure):
                    await asyncio.wait_for(observer, 3)
            else:
                release_cache.set()
                assert await asyncio.to_thread(cache_finished.wait, 3)
            assert not (await asyncio.wait({closing}, timeout=.05))[0]
            assert not profile.closed
        finally:
            release_cache.set()
            release_transport.set()
            if closing is not None:
                await asyncio.wait_for(closing, 3)
            if observer is not None:
                await asyncio.gather(observer, return_exceptions=True)
        assert profile.closed and delivered == []
        assert not any(path.is_file() for path in tmp_path.rglob("*"))


async def test_profile_close_keeps_runtime_responsive_during_cache_replace(
        submission_engine, origins, monkeypatch, tmp_path):
    replacing, release = threading.Event(), threading.Event()
    replace = os.replace

    def slow_replace(source, destination):
        replacing.set()
        assert release.wait(5)
        replace(source, destination)

    monkeypatch.setattr("pyv8c.platform.resources.cache.os.replace", slow_replace)
    async with Browser(http_engine=submission_engine, ws_engine=None, stub_logging="off",
                       cache_dir=tmp_path) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        sibling = browser.new_profile().new_page()
        await page.set_content("<p>local</p>", origins.same.url + "/")
        await sibling.set_content("<p>sibling</p>", origins.same.url + "/")
        delivered = []
        handle = page.executor.call(page.resource_fetcher.load_resource_async, "/echo", {},
                                    lambda *answer: delivered.append(answer))
        assert await asyncio.to_thread(replacing.wait, 3)

        # Release the file operation even if a regression blocks the Runtime.
        watchdog = threading.Timer(2, release.set)
        watchdog.start()
        closing = asyncio.create_task(profile.close())
        try:
            await asyncio.sleep(0)
            assert sibling.evaluate("document.body.textContent") == "sibling"
            assert not release.is_set(), "Runtime was blocked by the cache replace"
            assert handle.cancelled and not closing.done()
            assert not any(tmp_path.rglob("*.json"))
        finally:
            release.set()
            watchdog.cancel()
            await asyncio.wait_for(closing, 3)
        assert profile.closed and delivered == []
        assert len(list(tmp_path.rglob("*.json"))) == 1


@pytest.mark.parametrize("body_started", [False, True], ids=["headers", "body"])
async def test_page_close_interrupts_an_active_http_connection(engine_name, body_started):
    entered, disconnected = asyncio.Event(), asyncio.Event()
    connection = None

    async def hold(reader, writer):
        nonlocal connection
        connection = writer
        try:
            await reader.readuntil(b"\r\n\r\n")
            if body_started:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\nx")
                await writer.drain()
            entered.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(hold, "127.0.0.1", 0)
    async with listener, Browser(stub_logging="off", http_engine=engine_name) as browser:
        page = browser.new_page()
        address = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        request = asyncio.create_task(page.net.get(address, timeout=10))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(page.close(), 3)
            with pytest.raises(LoadFailure, match="was aborted"):
                await request
            await asyncio.wait_for(disconnected.wait(), 3)
        finally:
            if connection is not None:
                connection.close()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)


async def test_host_http_does_not_starve_a_sibling_and_closes_only_its_own_work(engine_name):
    held, detached = asyncio.Event(), asyncio.Event()
    paths, connections = [], set()
    stalled_connections, disconnected = 0, 0

    async def serve(reader, writer):
        nonlocal stalled_connections, disconnected
        connections.add(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ", 2)[1]
            paths.append(path)
            if path.startswith(b"/held"):
                stalled_connections += 1
                if stalled_connections == 2:
                    held.set()
                if await reader.read() == b"":
                    disconnected += 1
                    if disconnected == 2:
                        detached.set()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n"
                         b"Connection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(writer)

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with listener, Browser(stub_logging="off", http_engine=engine_name) as browser:
        base = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        profile = browser.new_profile()
        busy, sibling = profile.new_page(), profile.new_page()
        running = [asyncio.create_task(busy.net.get(base + f"/held{i}")) for i in range(2)]
        queued = []
        try:
            await asyncio.wait_for(held.wait(), 3)
            for path in ("/cancelled", "/expired", "/closed"):
                queued.append(asyncio.create_task(busy.net.get(
                    base + path, timeout=.05 if path == "/expired" else 3)))
            await asyncio.sleep(0)
            queued[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued[0]
            with pytest.raises(TimeoutError):
                await queued[1]
            await sibling.goto(base + "/navigation", timeout=2)
            assert (await sibling.net.get(base + "/sibling", timeout=2)).content == b"ok"
            await asyncio.wait_for(busy.close(), 2)
            await asyncio.wait_for(detached.wait(), 2)
            with pytest.raises(asyncio.CancelledError):
                await queued[2]
            results = await asyncio.gather(*running, return_exceptions=True)
            assert all(isinstance(result, LoadFailure) for result in results)
            assert (await sibling.net.get(base + "/still-open", timeout=2)).content == b"ok"
        finally:
            for writer in tuple(connections):
                writer.close()
            for task in running + queued:
                task.cancel()
            await asyncio.gather(*running, *queued, return_exceptions=True)
    assert not set(paths) & {b"/cancelled", b"/expired", b"/closed"}


@pytest.mark.parametrize("max_clients", [2, 3])
async def test_small_curl_pool_leaves_a_host_slot_for_a_sibling_page(max_clients):
    held, detached = asyncio.Event(), asyncio.Event()
    connections, paths = set(), []

    async def serve(reader, writer):
        connections.add(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ", 2)[1]
            paths.append(path)
            if path == b"/held":
                held.set()
                if await reader.read() == b"":
                    detached.set()
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(writer)

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with listener, Browser(http_engine="curl_cffi", http_options={"max_clients": max_clients},
                                 stub_logging="off") as browser:
        base = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        profile = browser.new_profile()
        busy, sibling = profile.new_page(), profile.new_page()
        first = asyncio.create_task(busy.net.get(base + "/held"))
        queued = None
        try:
            await asyncio.wait_for(held.wait(), 3)
            queued = asyncio.create_task(busy.net.get(base + "/queued"))
            assert (await sibling.net.get(base + "/sibling", timeout=2)).content == b"ok"
            assert paths == [b"/held", b"/sibling"]
            await asyncio.wait_for(busy.close(), 2)
            await asyncio.wait_for(detached.wait(), 2)
            with pytest.raises(LoadFailure, match="aborted"):
                await first
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert (await sibling.net.get(base + "/still-open", timeout=2)).content == b"ok"
        finally:
            for writer in tuple(connections):
                writer.close()
            jobs = [first] if queued is None else [first, queued]
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
    assert b"/queued" not in paths


async def test_navigation_can_send_when_ordinary_requests_fill_the_origin(engine_name):
    filled = asyncio.Event()
    paths, writers = [], set()

    async def serve(reader, writer):
        writers.add(writer)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            path = request.split(b" ", 2)[1]
            paths.append(path)
            if path == b"/hold":
                if paths.count(b"/hold") >= 6:
                    filled.set()
                await reader.read()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                         b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with listener, Browser(http_engine=engine_name, stub_logging="off") as browser:
        base = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        profile = browser.new_profile()
        pages = [profile.new_page() for _ in range(3)]
        victims = [asyncio.create_task(page.net.get(base + "/hold")) for page in pages for _ in range(2)]
        try:
            await asyncio.wait_for(filled.wait(), 4)
            nav = profile.new_page()
            assert (await nav.goto(base + "/nav", timeout=2)).status == 200
            assert paths.count(b"/nav") == 1
            assert all(not victim.done() for victim in victims)
        finally:
            for victim in victims:
                victim.cancel()
            await asyncio.gather(*victims, return_exceptions=True)
            for writer in tuple(writers):
                writer.close()


@pytest.mark.parametrize("max_clients, ordinary_count", [(4, 1), (5, 2)])
async def test_small_curl_pool_preserves_navigation_and_ordinary_capacity(max_clients, ordinary_count):
    _require("curl_cffi")
    first_navigation, normal_filled = asyncio.Event(), asyncio.Event()
    paths, connections = [], set()

    async def serve(reader, writer):
        connections.add(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ", 2)[1]
            paths.append(path)
            if path == b"/hold":
                if paths.count(path) == ordinary_count:
                    normal_filled.set()
                await reader.read()
            elif path == b"/slow-nav":
                first_navigation.set()
                await reader.read()
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                             b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
                await writer.drain()
        finally:
            writer.close()
            connections.discard(writer)

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with listener, Browser(http_engine="curl_cffi", http_options={"max_clients": max_clients},
                                 stub_logging="off") as browser:
        address = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        ordinary, slow, fast = [browser.new_page() for _ in range(3)]
        await ordinary.set_content("<p>local</p>", address + "/")
        ordinary.evaluate(f"for (let i = 0; i < {ordinary_count}; i++) fetch('/hold').catch(() => {{}})")
        first = asyncio.create_task(slow.goto(address + "/slow-nav", timeout=6))
        try:
            await asyncio.wait_for(first_navigation.wait(), 2)
            await asyncio.wait_for(normal_filled.wait(), 2)
            assert (await fast.goto(address + "/fast-nav", timeout=2)).status == 200
            assert paths.count(b"/fast-nav") == 1
            assert not first.done()
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            for writer in tuple(connections):
                writer.close()


async def test_synchronous_xhr_does_not_wait_for_its_own_owner_loop(submission_engine, origins):
    async with _page(submission_engine, origins) as page:
        assert page.evaluate("""(() => {
            const xhr = new XMLHttpRequest();
            xhr.open("GET", "/echo", false);
            xhr.send();
            return [xhr.status, JSON.parse(xhr.responseText).method];
        })()""", timeout=3) == [200, "GET"]


async def test_worker_import_scripts_executes_before_returning(submission_engine, origins):
    async with _page(submission_engine, origins) as page:
        source = json.dumps("importScripts(location.origin + '/worker-import.js'); postMessage(imported);")
        page.evaluate("globalThis.worker = new Worker(URL.createObjectURL(new Blob(["
                      + source + "], {type:'text/javascript'})));"
                      "worker.onmessage = event => globalThis.imported = event.data;")
        await page.wait_for("globalThis.imported === 42", timeout=3)
    assert origins.same.sent("/worker-import.js").method == "GET"


@pytest.mark.parametrize("owner", ["page", "worker"])
async def test_synchronous_xhr_can_run_when_ordinary_page_slots_are_full(engine_name, owner):
    # The Window's synchronous call also blocks the asyncio owner of this test.
    released = threading.Event()
    filled = threading.Event()
    lock = threading.Lock()
    held = 0

    class Server(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal held
            if self.path == "/hold":
                with lock:
                    held += 1
                    if held == 4:
                        filled.set()
                released.wait(5)
            elif self.path == "/release":
                released.set()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            body = b"ok" if self.path == "/release" else b""
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    xhr = """
        const xhr = new XMLHttpRequest();
        xhr.open("GET", location.origin + "/release", false);
        xhr.send();
    """
    with ThreadingHTTPServer(("127.0.0.1", 0), Server) as listener:
        thread = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            async with Browser(http_engine=engine_name, stub_logging="off") as browser:
                address = f"http://127.0.0.1:{listener.server_port}"
                page = browser.new_page()
                await page.set_content("<p>local</p>", address + "/")
                page.evaluate("for (let i = 0; i < 4; i++) fetch('/hold').catch(() => {})")
                assert await asyncio.to_thread(filled.wait, 3)
                if owner == "page":
                    assert page.evaluate(
                        "(() => {" + xhr + "return [xhr.status, xhr.responseText];})()", timeout=2
                    ) == [200, "ok"]
                else:
                    script = json.dumps(xhr + "postMessage([xhr.status, xhr.responseText]);")
                    page.evaluate("globalThis.worker = new Worker(URL.createObjectURL(new Blob(["
                                  + script + "], {type:'text/javascript'})));"
                                  "worker.onmessage = e => globalThis.workerResponse = e.data;")
                    await page.wait_for("globalThis.workerResponse !== undefined", timeout=2)
                    assert page.evaluate("workerResponse") == [200, "ok"]
        finally:
            released.set()
            listener.shutdown()
            thread.join()


@pytest.mark.parametrize("phase", ["read", "write"])
@pytest.mark.parametrize("owner", ["page", "worker"])
async def test_cache_io_keeps_runtime_available_and_is_joined_on_close(
        engine_name, origins, monkeypatch, tmp_path, phase, owner):
    method = "get" if phase == "read" else "put"
    original = getattr(ResourceCache, method)
    entered, release = threading.Event(), threading.Event()
    threads = []

    def gate(cache, key, *args):
        if key is not None and key[1].endswith("/echo"):
            with cache._disk_lock:
                threads.append(threading.current_thread().name)
                entered.set()
                assert release.wait(5)
        return original(cache, key, *args)

    monkeypatch.setattr(ResourceCache, method, gate)
    async with Browser(http_engine=engine_name, cache=True, cache_dir=tmp_path,
                       stub_logging="off") as browser:
        profile = browser.new_profile()
        page, sibling = profile.new_page(), profile.new_page()
        address = origins.same.url
        await page.set_content("<p>local</p>", address + "/")
        await sibling.set_content("<p>sibling</p>", address + "/")
        closing_owner = page if phase == "write" else profile
        finish_close = type(closing_owner)._finish_close
        close_calls = []

        async def count_close(owner, *args):
            if owner is closing_owner:
                close_calls.append(owner)
            return await finish_close(owner, *args)

        monkeypatch.setattr(type(closing_owner), "_finish_close", count_close)
        source = "fetch(location.origin + '/echo').catch(() => {})"
        if owner == "page":
            page.evaluate(source)
        else:
            page.evaluate("globalThis.worker = new Worker(URL.createObjectURL(new Blob(["
                          + json.dumps(source) + "], {type:'text/javascript'})))")
        closing = []
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert threads[0].startswith("pyv8c-loader")
            sibling.evaluate("globalThis.result = null;"
                             "fetch('/page').then(r => result = r.status).catch(() => {})", timeout=1)
            assert sibling.evaluate("result === null") is True
            closing = [asyncio.create_task(closing_owner.close()) for _ in range(2)]
            done, _ = await asyncio.wait(closing, timeout=.05)
            assert not done, "close returned with disk I/O still running"
            if phase == "write":
                assert (await sibling.net.get(address + "/page", timeout=2)).status == 200
        finally:
            release.set()
            if closing:
                await asyncio.wait_for(asyncio.gather(*closing), 2)
        assert close_calls == [closing_owner]
        if phase == "write":
            await sibling.wait_for("result === 200", timeout=2)


async def test_preflight_cache_shares_page_and_worker_but_partitions_by_profile(engine_name, tmp_path):
    source = "http://source.test"
    seen = []

    class Server(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            seen.append((self.command, self.path, self.headers["Origin"]))
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", source)
            self.send_header("Access-Control-Allow-Methods", "PUT")
            self.send_header("Access-Control-Allow-Headers", "X-Fixture")
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Max-Age", "0" if self.path == "/zero" else "600")
            self.end_headers()

        def do_PUT(self):
            seen.append((self.command, self.path, self.headers["Origin"]))
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", source)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        do_GET = do_PUT

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Server) as listener:
        thread = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            async with Browser(http_engine=engine_name, stub_logging="off", cache_dir=tmp_path) as browser:
                target = f"http://127.0.0.1:{listener.server_port}"
                page = browser.new_profile().new_page()
                await page.set_content("<p>one</p>", source + "/")
                options = {"method": "PUT", "headers": {"X-Fixture": "yes"}}
                assert (await page.net.fetch(target + "/shared", **options)).text == "ok"

                script = ("fetch(" + json.dumps(target + "/shared") + ", " + json.dumps(options)
                          + ").then(r => r.text()).then(postMessage, e => postMessage(String(e)))")
                page.evaluate("globalThis.worker = new Worker(URL.createObjectURL(new Blob(["
                              + json.dumps(script) + "], {type:'text/javascript'})));"
                              "worker.onmessage = e => globalThis.reply = e.data;")
                await page.wait_for("typeof reply !== 'undefined'", timeout=3)
                assert page.evaluate("reply") == "ok"
                assert (await page.net.fetch(target + "/shared", credentials="include", **options)).text == "ok"

                other = browser.new_profile().new_page()
                await other.set_content("<p>two</p>", source + "/")
                assert (await other.net.fetch(target + "/shared", **options)).text == "ok"
                for _ in range(2):
                    assert (await page.net.fetch(target + "/zero", headers=options["headers"])).text == "ok"
        finally:
            listener.shutdown()
            thread.join()
    assert [method for method, path, _ in seen if path == "/shared"] == [
        "OPTIONS", "PUT", "PUT", "OPTIONS", "PUT", "OPTIONS", "PUT"]
    assert [method for method, path, _ in seen if path == "/zero"] == [
        "OPTIONS", "GET", "OPTIONS", "GET"]
    assert all(origin == source for _, _, origin in seen)


async def test_sync_worker_xhr_times_out_during_cache_read(engine_name, origins, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    original = ResourceCache.get

    def gate(cache, key):
        if key is not None and key[1].endswith("/echo"):
            entered.set()
            assert release.wait(5)
        return original(cache, key)

    monkeypatch.setattr(ResourceCache, "get", gate)
    async with Browser(http_engine=engine_name, cache=True, cache_dir=tmp_path,
                       stub_logging="off") as browser:
        page = browser.new_page()
        await page.set_content("<p>local</p>", origins.same.url + "/")
        script = json.dumps("""
            const xhr = new XMLHttpRequest();
            xhr.open('GET', location.origin + '/echo', false);
            xhr.timeout = 100;
            try { xhr.send(); }
            catch (error) { postMessage([error.name, xhr.readyState, xhr.status]); }
        """)
        page.evaluate("globalThis.result = null;"
                      "globalThis.worker = new Worker(URL.createObjectURL(new Blob(["
                      + script + "], {type:'text/javascript'})));"
                      "worker.onmessage = e => result = e.data;")
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await page.wait_for("result !== null", timeout=1)
            assert page.evaluate("result") == ["TimeoutError", 4, 0]
            closing = asyncio.create_task(page.close())
            done, _ = await asyncio.wait({closing}, timeout=.05)
            assert not done
        finally:
            release.set()
            if closing is not None:
                await asyncio.wait_for(closing, 2)
        assert all(record.path != "/echo" for record in origins.same.records())


async def test_page_xhr_times_out_once_during_cache_read(engine_name, origins, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    original = ResourceCache.get

    def gate(cache, key):
        if key is not None and key[1].endswith("/echo"):
            entered.set()
            assert release.wait(5)
        return original(cache, key)

    monkeypatch.setattr(ResourceCache, "get", gate)
    async with Browser(http_engine=engine_name, cache=True, cache_dir=tmp_path,
                       stub_logging="off") as browser:
        page = browser.new_page()
        await page.set_content("<p>local</p>", origins.same.url + "/")
        page.evaluate("""
            globalThis.events = [];
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/echo');
            xhr.timeout = 250;
            for (const name of ['load', 'error', 'abort', 'timeout', 'loadend']) {
                xhr.addEventListener(name, () => events.push(name));
            }
            xhr.send();
        """)
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await page.wait_for("events.length > 0", timeout=1)
            assert page.evaluate("events") == ["loadend"]
        finally:
            release.set()
        await page.run_until_idle(timeout=2)
        assert page.evaluate("events") == ["loadend"]
        assert all(record.path != "/echo" for record in origins.same.records())


@pytest.mark.parametrize("deadline", [False, True], ids=["upstream-timeout", "request-deadline"])
async def test_custom_engine_distinguishes_request_deadline_from_transport_failure(monkeypatch, deadline):
    class Engine:
        def submit_request(self, request, handle, remaining):
            self.remaining = remaining
            self.result = Future()
            handle.attach_abort(self.result.cancel)
            return self.result

        def close(self):
            pass

    original = _TaskCapability.schedule

    def schedule(tasks, callback, delay, repeat=False):
        if getattr(getattr(callback, "func", None), "__func__", None) is XMLHttpRequest._on_timeout:
            delay += .5
        return original(tasks, callback, delay, repeat)

    monkeypatch.setattr(_TaskCapability, "schedule", schedule)
    engine = Engine()
    async with Browser(http_engine=engine, ws_engine=None, stub_logging="off") as browser:
        page = browser.new_page()
        await page.set_content("<p>local</p>", "http://fixture.test/")
        page.evaluate("""
            globalThis.events = [];
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/request');
            xhr.timeout = 500;
            for (const name of ['error', 'abort', 'timeout', 'loadend']) {
                xhr.addEventListener(name, () => events.push(name));
            }
            xhr.send();
        """)
        if deadline:
            await asyncio.sleep(engine.remaining() + .02)
            with pytest.raises(RequestTimeout) as failure:
                engine.remaining()
            engine.result.set_exception(failure.value)
        else:
            engine.result.set_exception(TimeoutError("upstream transport timeout"))
        await page.wait_for("events.includes('loadend')", timeout=2)
        assert page.evaluate("events") == (["loadend"] if deadline else ["error", "loadend"])


async def test_cleanup_error_after_transport_deadline_settles_as_timeout(
        submission_engine, origins, monkeypatch):
    cleanup_error = OSError("error while unwinding timed-out I/O")
    captured = []
    original_schedule = _TaskCapability.schedule
    settle = HttpScheduler._settle

    def delay_xhr_timer(tasks, callback, delay, repeat=False):
        if getattr(getattr(callback, "func", None), "__name__", "") == "_on_timeout":
            delay += .3
        return original_schedule(tasks, callback, delay, repeat)

    async def fail_in_cleanup(_request):
        try:
            await asyncio.Future()
        except asyncio.CancelledError as error:
            raise cleanup_error from error

    def capture(scheduler, job, *, result=None, error=None):
        captured.append(error)
        settle(scheduler, job, result=result, error=error)

    async with Browser(http_engine=submission_engine, ws_engine=None, stub_logging="off") as browser:
        page = browser.new_page()
        await page.set_content("<p>local</p>", origins.same.url + "/")
        await page.net.get("/page")
        monkeypatch.setattr(page.profile.network_backend.http, "_single_call", fail_in_cleanup)
        monkeypatch.setattr(_TaskCapability, "schedule", delay_xhr_timer)
        monkeypatch.setattr(HttpScheduler, "_settle", capture)
        page.evaluate("""
            globalThis.events = [];
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/request'); xhr.timeout = 250;
            for (const name of ['load', 'error', 'abort', 'timeout', 'loadend']) {
                xhr.addEventListener(name, () => events.push(name));
            }
            xhr.send();
        """)
        await page.wait_for("events.length > 0", timeout=2)
        await asyncio.sleep(.35)
        await page.run_until_idle(timeout=2)
        assert page.evaluate("events") == ["loadend"]
        assert len(captured) == 1 and isinstance(captured[0], RequestTimeout)
        assert captured[0].__cause__ is cleanup_error


@pytest.mark.parametrize("phase", ["queued", "wire", "sync", "network-error"])
async def test_worker_xhr_deadline_has_one_timeout_terminal(submission_engine, monkeypatch, phase):
    filled, disconnected = asyncio.Event(), asyncio.Event()
    paths, connections = [], set()

    async def hold(reader, writer):
        connections.add(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ", 2)[1]
            paths.append(path)
            if phase == "network-error":
                return
            if paths.count(b"/hold") == 4:
                filled.set()
            if await reader.read() == b"" and path == b"/xhr":
                disconnected.set()
        finally:
            writer.close()
            connections.discard(writer)

    original = _TaskCapability.schedule

    def schedule(tasks, callback, delay, repeat=False):
        # Force the transport or queue deadline to win the race with the XHR timer.
        if getattr(getattr(callback, "func", None), "__func__", None) is XMLHttpRequest._on_timeout:
            delay += .5
        return original(tasks, callback, delay, repeat)

    monkeypatch.setattr(_TaskCapability, "schedule", schedule)
    network_failures = []
    if phase == "network-error":
        settle = HttpScheduler._settle

        def delay_failure(scheduler, job, *, result=None, error=None):
            if error is not None and job.request.url.endswith("/xhr"):
                try:
                    budget = job.remaining()
                except TimeoutError:
                    budget = None
                network_failures.append(budget)
                if budget is not None:
                    # The transport has already reported failure. Delay publishing
                    # it to the Worker until just after the HTTP deadline.
                    time.sleep(budget + .03)
            settle(scheduler, job, result=result, error=error)

        monkeypatch.setattr(HttpScheduler, "_settle", delay_failure)
    listener = await asyncio.start_server(hold, "127.0.0.1", 0)
    async with listener, Browser(http_engine=submission_engine, stub_logging="off") as browser:
        address = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        page = browser.new_page()
        await page.set_content("<p>local</p>", address + "/")
        if phase == "queued":
            page.evaluate("for (let i = 0; i < 4; i++) fetch('/hold').catch(() => {})")
            await asyncio.wait_for(filled.wait(), 3)
        synchronous = phase == "sync"
        timeout_ms = 650 if phase == "network-error" else 250
        script = json.dumps(f"""
            const xhr = new XMLHttpRequest(), events = [];
            xhr.open('GET', location.origin + '/xhr', {str(not synchronous).lower()});
            xhr.timeout = {timeout_ms};
            for (const name of ['load', 'error', 'timeout', 'abort', 'loadend']) {{
                xhr.addEventListener(name, () => {{
                    events.push(name);
                    if (name === 'loadend') postMessage([events, xhr.readyState, xhr.status]);
                }});
            }}
            try {{ xhr.send(); }} catch (error) {{
                postMessage([error.name, xhr.readyState, xhr.status]);
            }}
            if ({str(not synchronous).lower()}) {{
                setTimeout(() => postMessage(events), {timeout_ms + 600});
            }}
        """)
        page.evaluate("globalThis.results = [];"
                      "globalThis.worker = new Worker(URL.createObjectURL(new Blob(["
                      + script + "], {type:'text/javascript'})));"
                      "worker.onmessage = e => results.push(e.data);")
        try:
            await page.wait_for(f"results.length >= {1 if synchronous else 2}", timeout=3)
            if synchronous:
                assert page.evaluate("results") == [["TimeoutError", 4, 0]]
            else:
                expected = ["error", "loadend"] if phase == "network-error" else ["loadend"]
                assert page.evaluate("results") == [[expected, 4, 0], expected]
            if phase == "queued":
                assert b"/xhr" not in paths
            elif phase == "network-error":
                assert len(network_failures) == 1 and network_failures[0] is not None
            else:
                await asyncio.wait_for(disconnected.wait(), 2)
            await asyncio.wait_for(browser.close(), 2)
        finally:
            for writer in tuple(connections):
                writer.close()


@pytest.mark.parametrize("script", [
    "fetch(location.origin + '/start').catch(() => {})",
    "importScripts(location.origin + '/start')",
], ids=["fetch", "importScripts"])
async def test_close_interrupts_worker_http_on_the_active_redirect_hop(submission_engine, script):
    entered, disconnected = asyncio.Event(), asyncio.Event()
    connections = set()

    async def serve(reader, writer):
        connections.add(writer)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            if request.startswith(b"GET /start "):
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: /hold\r\n"
                             b"Connection: close\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            assert request.startswith(b"GET /hold ")
            entered.set()
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(writer)

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with listener, Browser(stub_logging="off", http_engine=submission_engine) as browser:
        address = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        page = browser.new_page()
        await page.set_content("<p>local</p>", address + "/")
        page.evaluate("globalThis.worker = new Worker(URL.createObjectURL(new Blob(["
                      + json.dumps(script) + "], {type:'text/javascript'})))")
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert browser._loader_pool is None, "Worker HTTP must not hold a loader thread"
            await asyncio.wait_for(browser.close(), 2)
            await asyncio.wait_for(disconnected.wait(), 2)
        finally:
            for writer in tuple(connections):
                writer.close()


@pytest.mark.parametrize("action", ["close", "abort"])
async def test_cancel_interrupts_worker_cors_preflight_without_sending_the_request(submission_engine, action):
    entered, disconnected = asyncio.Event(), asyncio.Event()
    connections, request_lines = set(), []

    async def hold(reader, writer):
        connections.add(writer)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            request_lines.append(headers.split(b"\r\n", 1)[0])
            entered.set()
            if await reader.read() == b"":
                disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(writer)

    listener = await asyncio.start_server(hold, "127.0.0.1", 0)
    async with listener, Browser(stub_logging="off", http_engine=submission_engine) as browser:
        port = listener.sockets[0].getsockname()[1]
        page = browser.new_page()
        await page.set_content("<p>local</p>", "http://127.0.0.1:1/")
        script = (
            "const control = new AbortController(); onmessage = () => control.abort();"
            f"fetch('http://127.0.0.1:{port}/probe', {{method:'PUT', signal:control.signal}})"
            ".catch(error => postMessage(error.name));"
        )
        page.evaluate("globalThis.errorName = null;"
                      "globalThis.worker = new Worker(URL.createObjectURL(new Blob(["
                      + json.dumps(script) + "], {type:'text/javascript'})));"
                      "worker.onmessage = e => errorName = e.data;")
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if action == "close":
                await asyncio.wait_for(browser.close(), 2)
            else:
                page.evaluate("worker.postMessage(null)")
                await page.wait_for("errorName === 'AbortError'", timeout=2)
            await asyncio.wait_for(disconnected.wait(), 2)
        finally:
            for writer in tuple(connections):
                writer.close()
    assert request_lines == [b"OPTIONS /probe HTTP/1.1"]


async def test_proxy_carries_the_navigation(engine_name, origins):
    proxy = RecordingProxy()
    try:
        async with _page(engine_name, origins, proxy=proxy.url):
            pass
        carried = proxy.carried()
        assert carried, "the navigation must leave through the configured proxy"
        assert all(origins.same.url in line for line in carried)
    finally:
        proxy.close()


def test_the_client_hello_advertises_the_required_capabilities(engine_name):
    """Structure, not an absolute ja3/ja4: GREASE and ordering are excluded."""
    hello = _hello_for(engine_name)
    assert CAPABILITIES_REQUIRED <= hello.capabilities(), (
        f"missing negotiation capabilities: "
        f"{sorted(CAPABILITIES_REQUIRED - hello.capabilities())}")
    assert 0x0304 in hello.supported_versions, "TLS 1.3 is advertised"
    assert {"h2", "http/1.1"} <= set(hello.alpn)
    assert hello.record_version == "0301" and hello.client_version == "0303"
    assert set(hello.digest().split("|")) == set(hello.capabilities()), (
        "the digest is the normalised capability set, never raw bytes")


def test_both_engines_share_a_normalised_negotiation_baseline():
    """The baseline is the shared capability set, not an absolute stack fingerprint.

    libcurl/BoringSSL and Chromium's Cronet legitimately differ in cipher order,
    extra extensions and GREASE code points; pinning those would make the test fail
    on any dependency upgrade without any loss of browser capability.
    """
    hellos = {name: _hello_for(name) for name in ENGINES}
    shared = set.intersection(*(set(hello.capabilities()) for hello in hellos.values()))
    assert CAPABILITIES_REQUIRED <= shared, (
        f"the engines no longer agree on the required capabilities: "
        f"{sorted(CAPABILITIES_REQUIRED - shared)}")
    for name, hello in hellos.items():
        digest = hello.digest()
        assert digest == "|".join(sorted(hello.capabilities())), (
            f"{name}: the digest must be the sorted capability set")
        assert "grease" not in digest and "0x" not in digest, (
            f"{name}: a normalised digest must not carry raw TLS code points")
