"""Source, policy and lifecycle contracts shared by both resource drivers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("pyv8c.pyv8")
from _http_fixtures import resolved
from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import LoadFailure
from pyv8c.foundation.load_handle import LoadHandle
from pyv8c.platform.resources.documents import DocumentResourceController
from pyv8c.platform.resources.tracker import DocumentResource
from pyv8c.web.fetch.response import Response


async def test_inline_interceptor_return_does_not_replace_new_transport(monkeypatch):
    """Interceptor results advance asynchronously without losing the transport."""
    transport = SimpleNamespace(cancel_calls=0)
    def cancel():
        transport.cancel_calls += 1
    transport.cancel = cancel
    calls = []
    started = threading.Event()
    def dispatch(call, tail, *, force_pool=False, kind="host-callback"):
        assert not force_pool
        assert kind == "host-callback"
        calls.append(call)
        if len(calls) == 1:
            tail(call(), None)
            return None
        started.set()
        return transport
    async with Browser(real_network=False, stub_logging="off",
                         resource_interceptor=lambda url, request: None,
                         resource_loader=lambda url, request: "ok") as browser:
        page = browser.new_profile().new_page()
        monkeypatch.setattr(page.resource_fetcher, "callbacks", SimpleNamespace(dispatch=dispatch))
        def issue():
            handle = page.resource_fetcher.load_resource_async(
                "http://local.test/", {}, lambda *args: pytest.fail("cancelled delivery"))
            assert len(calls) == 1, "interceptor must not advance in the issuing stack"
            return handle
        handle = page.executor.call(issue)
        assert (await asyncio.to_thread(started.wait, 3))
        page.executor.call(handle.cancel)
        assert len(calls) == 2
        assert transport.cancel_calls == 1
        assert page.resource_ownership.active_count == 0


async def ask(page, url, options, asynchronous):
    if not asynchronous:
        return page.resource_fetcher.load_resource(url, options)
    answers = []
    arrived = threading.Event()
    def complete(response, error):
        assert page.realm.executor.is_owner_thread()
        answers.append((response, error))
        arrived.set()
    def issue():
        page.resource_fetcher.load_resource_async(url, options, complete)
        assert not answers, "async completion must not run in the issuing turn"
    page.realm.executor.call(issue)
    assert (await asyncio.to_thread(arrived.wait, 3)), "resource did not deliver before its deadline"
    assert len(answers) == 1
    response, error = answers[0]
    if error:
        raise error
    return response


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("threaded", [False, True])
@pytest.mark.parametrize("url,body", [("data:text/plain,local", "local"), ("about:blank", "")])
async def test_local_sources_precede_loader(asynchronous, threaded, url, body):
    calls = []
    async with Browser(real_network=False, threaded_loaders=threaded, stub_logging="off",
                 resource_loader=lambda url, request: calls.append(url) or "unexpected") as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        assert (await ask(page, url, {}, asynchronous)).body_text() == body
        assert not calls


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("threaded", [False, True])
@pytest.mark.parametrize("mode,allowed", [("cors", False), ("cors", True), ("same-origin", True)])
async def test_loader_has_identical_response_policy(asynchronous, threaded, mode, allowed):
    calls = []
    def loader(url, request):
        calls.append(url)
        return {"body": "cross", "headers": {"access-control-allow-origin": "*"} if allowed else {}}
    async with Browser(real_network=False, threaded_loaders=threaded, stub_logging="off", resource_loader=loader) as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        if mode == "cors" and allowed:
            assert (await ask(page, "http://other.test/x", {"mode": mode}, asynchronous)).body_text() == "cross"
        else:
            with pytest.raises(LoadFailure):
                (await ask(page, "http://other.test/x", {"mode": mode}, asynchronous))
        assert calls == ([] if mode == "same-origin" else ["http://other.test/x"])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("allowed", [False, True])
async def test_interceptor_response_obeys_cross_origin_policy(asynchronous, allowed):
    def interceptor(url, request):
        if url == "http://other.test/x":
            return {"body": "cross", "headers":
                    {"access-control-allow-origin": "http://site.test"} if allowed else {}}
        return None

    async with Browser(real_network=False, stub_logging="off",
                         resource_interceptor=interceptor) as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        if allowed:
            response = (await ask(page, "http://other.test/x", {"mode": "cors"}, asynchronous))
            assert response.body_text() == "cross"
        else:
            with pytest.raises(LoadFailure):
                (await ask(page, "http://other.test/x", {"mode": "cors"}, asynchronous))


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_interceptor_keeps_cross_origin_script_bytes_but_hides_no_cors_fetch(asynchronous):
    async with Browser(real_network=False, stub_logging="off",
                         resource_interceptor=lambda url, request: {"body": "window.loaded=true"}) as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        script = (await ask(page, "http://other.test/x.js",
                     {"mode": "no-cors", "resource_type": "script"}, asynchronous))
        assert script.body_text() == "window.loaded=true"
        fetched = (await ask(page, "http://other.test/x.js",
                      {"mode": "no-cors", "resource_type": "fetch"}, asynchronous))
        assert fetched.status == 0
        assert fetched.body_text() == ""


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_cancel_and_transport_binding_converge(cancel_first):
    cancelled = []
    transport = SimpleNamespace(cancel=lambda: cancelled.append(True))
    handle = LoadHandle()
    if cancel_first:
        handle.cancel()
        handle.bind_transport(transport)
    else:
        handle.bind_transport(transport)
        handle.cancel()
    assert cancelled == [True]
    assert handle.cancelled and handle.done
    assert not handle.finish() and not handle.cancel()


@pytest.mark.race
async def test_concurrent_cancel_and_bind():
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(100):
            handle = LoadHandle()
            gate = threading.Barrier(2)
            cancelled = []
            transport = SimpleNamespace(cancel=lambda: cancelled.append(True))
            def cancel():
                gate.wait()
                handle.cancel()
            def bind():
                gate.wait()
                handle.bind_transport(transport)
            first, second = pool.submit(cancel), pool.submit(bind)
            (await asyncio.to_thread(first.result, 3)); (await asyncio.to_thread(second.result, 3))
            assert cancelled == [True]


async def test_finish_is_terminal():
    handle = LoadHandle()
    assert handle.finish()
    assert not handle.finish() and not handle.cancel()
    cancelled = []
    handle.bind_transport(SimpleNamespace(cancel=lambda: cancelled.append(True)))
    assert not cancelled and handle.done and not handle.cancelled


@pytest.mark.parametrize("missing,replaced", [(True, False), (False, True)])
async def test_iframe_completion_needs_live_resource_and_child_identity(missing, replaced):
    events = []
    child = SimpleNamespace(closed=False, navigation=SimpleNamespace(generation=3))
    services = SimpleNamespace(_page_realm=SimpleNamespace(page=child))
    node = SimpleNamespace(_child_services=services)
    page = SimpleNamespace(closed=False, navigation=SimpleNamespace(generation=2))
    frames = SimpleNamespace(dispatch_frame_completion=lambda *args: events.append(args))
    tracker = DocumentResourceController(page, None, frames)
    if not missing:
        tracker.tracker.add(DocumentResource(2, node, "iframe", LoadHandle(),
                                                child_services=object() if replaced else services,
                                                child_generation=3))
    tracker.finish_iframe_resource(1, 2, node, True)
    assert not events


async def test_unfinished_document_does_not_release_its_load_barrier():
    page = SimpleNamespace()
    tracker = DocumentResourceController(page, None, None)
    item = DocumentResource(1, object(), "iframe", LoadHandle())
    tracker.tracker.add(item)
    assert tracker.has_pending and tracker.outstanding() == {"iframe": 1}


@pytest.mark.race
@pytest.mark.parametrize("navigate", [False, True])
async def test_late_loader_cannot_deliver_redirect_or_populate_cache(tmp_path, navigate):
    started, release = threading.Event(), threading.Event()
    calls, answers = [], []
    def loader(url, request):
        calls.append(url)
        started.set()
        assert release.wait(3)
        return Response("late", {"url": url, "status": 302, "headers": {"location": "/next"}})
    async with Browser(real_network=False, resource_loader=loader, threaded_loaders=True,
                 stub_logging="off", cache_dir=tmp_path) as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        handle = page.resource_fetcher.load_resource_async("/start", {}, lambda *args: answers.append(args))
        assert (await asyncio.to_thread(started.wait, 3))
        if navigate:
            (await page.set_content("<body>new</body>", url="http://site.test/new", wait_until="commit"))
        else:
            handle.cancel()
        release.set()
        (await page.run_until_idle(timeout=3))
        assert not answers and calls == ["http://site.test/start"]
        assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_cache_write_failure_does_not_fail_resource(tmp_path, monkeypatch, asynchronous):
    async with Browser(real_network=False, stub_logging="off", cache_dir=tmp_path,
                 resource_loader=lambda *args: "ok") as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        def fail(*args):
            raise OSError("read-only cache volume")
        monkeypatch.setattr("pyv8c.platform.resources.cache.os.replace", fail)
        assert (await ask(page, "/x", {}, asynchronous)).body_text() == "ok"
        assert not list(tmp_path.rglob("*.json"))
        assert not list(tmp_path.rglob("tmp*"))


async def test_cancellation_releases_resource_scope_registration():
    async with Browser(real_network=False, stub_logging="off", resource_loader=lambda *args: "ok") as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        def issue_and_cancel():
            handle = page.services.resource_services.load_resource_async("/x", {}, lambda *args: pytest.fail("cancelled delivery"))
            handle.cancel()
            assert ("resources", id(handle)) not in page.services._registrations
        page.realm.executor.call(issue_and_cancel)
        (await page.run_until_idle(3))


async def test_pool_submission_failure_delivers_and_balances_loader_count(monkeypatch):
    async with Browser(real_network=False, stub_logging="off", threaded_loaders=True,
                         resource_loader=lambda *args: "ok") as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        def reject(*args):
            raise RuntimeError("cannot schedule new futures after shutdown")
        monkeypatch.setattr(browser.loader_pool, "submit", reject)
        with pytest.raises(RuntimeError, match="shutdown"):
            (await ask(page, "/x", {}, True))
        assert page.tasks.loader_pending == 0


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_same_origin_redirect_is_rejected_before_cross_origin_send(asynchronous):
    calls = []
    def loader(url, request):
        calls.append(url)
        return {"status": 302, "headers": {"location": "http://other.test/end"}}
    async with Browser(real_network=False, stub_logging="off", resource_loader=loader) as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        with pytest.raises(LoadFailure, match="same-origin"):
            (await ask(page, "/start", {"mode": "same-origin"}, asynchronous))
        assert calls == ["http://site.test/start"]


@pytest.mark.parametrize("opaque", [False, True])
async def test_request_headers_and_cors_use_realm_origin(opaque):
    calls = []
    def loader(url, request):
        calls.append(request)
        return {"body": "ok", "headers": {"access-control-allow-origin": "null" if opaque else "http://site.test"}}
    async with Browser(real_network=False, stub_logging="off", resource_loader=loader) as browser:
        parent = browser.new_profile().new_page()
        (await parent.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        if opaque:
            parent.evaluate("var f=document.createElement('iframe'); f.sandbox='allow-scripts'; document.body.appendChild(f)")
        else:
            parent.evaluate("var f=document.createElement('iframe'); document.body.appendChild(f)")
        node = parent.document.fn_querySelector("iframe")
        child = node._child_services._page_realm.page
        assert (await ask(child, "http://site.test/x", {}, True)).body_text() == "ok"
        if opaque:
            assert calls[0]["headers"]["Origin"] == "null"
            assert calls[0]["headers"]["Sec-Fetch-Site"] == "cross-site"
        else:
            assert "Origin" not in calls[0]["headers"]
            assert calls[0]["headers"]["Sec-Fetch-Site"] == "same-origin"


@pytest.mark.race
async def test_cancelling_queued_loader_releases_pending_count():
    release = threading.Event()
    gate = threading.Barrier(5)
    calls = []
    async with Browser(real_network=False, stub_logging="off", threaded_loaders=True,
                         resource_loader=lambda *args: calls.append(True)) as browser:
        def occupy():
            gate.wait(3)
            release.wait(3)
        futures = [browser.loader_pool.submit(occupy) for _ in range(4)]
        try:
            (await asyncio.to_thread(gate.wait, 3))
            page = browser.new_profile().new_page()
            (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
            def issue():
                handle = page.services.resource_services.load_resource_async("/x", {}, lambda *args: pytest.fail("cancelled delivery"))
                assert page.tasks.loader_pending == 1
                handle.cancel()
                assert page.tasks.loader_pending == 0
                assert ("resources", id(handle)) not in page.services._registrations
            page.realm.executor.call(issue)
            (await page.run_until_idle(1))
            assert not calls
        finally:
            release.set()
            for future in futures:
                (await asyncio.to_thread(future.result, 3))


async def test_immediate_backend_completion_is_stored_once(tmp_path, monkeypatch):
    from pyv8c.network.models import NetworkResponse
    async with Browser(real_network=False, stub_logging="off", cache_dir=tmp_path) as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        def send(request, handle, remaining):
            raw = NetworkResponse(url=request.url, status=200, body=b"ok")
            return resolved(raw)
        from pyv8c.network.backend import NetworkBackend
        page.profile.network_backend = NetworkBackend(
            http=SimpleNamespace(submit_request=send, close=lambda: None))
        stores = []
        monkeypatch.setattr(page.profile.resource_cache, "put", lambda *args: stores.append(args))
        assert (await ask(page, "/x", {}, True)).body_text() == "ok"
        assert len(stores) == 1


@pytest.mark.race
async def test_direct_document_handle_cancel_releases_load_barrier():
    started, release = threading.Event(), threading.Event()
    def loader(*args):
        started.set()
        assert release.wait(3)
        return "late"
    async with Browser(real_network=False, stub_logging="off", threaded_loaders=True,
                         resource_loader=loader) as browser:
        try:
            page = browser.new_profile().new_page()
            (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
            node = page.document.fn_createElement("img")
            handle = page.realm.executor.call(lambda: page.document_resources.track_document_resource(
                node, "/x", {"resource_type": "image"}, "img"))
            assert (await asyncio.to_thread(started.wait, 3))
            handle.cancel()
            # The owner queue releases document records, including direct cancellation.
            assert (await page.run_until_idle(timeout=3))
            assert page.executor.call(lambda: not page.document_resources.has_pending)
            assert not page.services._registrations
        finally:
            release.set()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["cors", "no-cors", "same-origin"])
@pytest.mark.parametrize("cross_origin", [False, True])
async def test_blob_source_checks_creator_origin(asynchronous, mode, cross_origin):
    async with Browser(real_network=False, stub_logging="off") as browser:
        context = browser.new_profile()
        creator, reader = context.new_page(), context.new_page()
        (await creator.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
        (await reader.set_content("<body></body>", url="http://other.test/" if cross_origin else "http://site.test/reader", wait_until="commit"))
        url = creator.evaluate("URL.createObjectURL(new Blob(['secret']))")
        if cross_origin:
            with pytest.raises(LoadFailure, match="another origin"):
                (await ask(reader, url, {"mode": mode}, asynchronous))
        else:
            assert (await ask(reader, url, {"mode": mode}, asynchronous)).body_text() == "secret"


async def test_closed_page_returns_terminal_async_handle():
    async with Browser(real_network=False, stub_logging="off") as browser:
        page = browser.new_profile().new_page()
        (await page.close())
        handle = page.resource_fetcher.load_resource_async("http://site.test/", {}, lambda *args: pytest.fail("closed owner delivery"))
        assert handle.done and handle.cancelled
