"""The application API has one object model and explicit completion boundaries."""

import asyncio
import inspect
import threading

import pytest

from _http_fixtures import GatedTransport

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import HostStateError
from pyv8c.network.backend import NetworkBackend


async def test_public_browser_composes_a_private_execution_kernel():
    import pyv8c
    from pyv8c.platform.runtime import Runtime

    assert "Runtime" not in pyv8c.__all__
    assert not hasattr(pyv8c, "Runtime")
    async with Browser(real_network=False) as browser:
        assert not isinstance(browser, Runtime)
        assert isinstance(browser._runtime, Runtime)
        for name in ("new_page", "new_profile", "profiles", "environment"):
            assert not hasattr(browser._runtime, name)
        async with browser.new_profile() as profile:
            async with profile.new_page() as page:
                assert page.profile is profile and profile.browser is browser
                assert not hasattr(page, "runtime")
                assert page.evaluate("Promise.resolve().then(()=>globalThis.answer=42);globalThis.answer") != 42
                assert page.evaluate("answer") == 42
                assert not inspect.isawaitable(page.evaluate("1+1"))
        await profile.close()
        await page.close()
    await browser.close()
    await profile.close()
    await page.close()


async def test_standard_asyncio_run_preserves_the_existing_loop_clock():
    async def application():
        loop = asyncio.get_running_loop()
        clock, resolution = loop.time, loop._clock_resolution
        handle = loop.call_later(100, lambda: None)
        due = handle.when()
        try:
            async with Browser(real_network=False) as browser:
                page = browser.new_page()
                page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,5)")
                await page.wait_for("ready", timeout=1)
                assert loop.time == clock and loop._clock_resolution == resolution
                assert handle.when() == due
                assert page.evaluate("ready") is True
        finally:
            handle.cancel()
    await asyncio.to_thread(lambda: asyncio.run(application()))


async def test_page_sleep_pumps_tasks_without_entering_js():
    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,2)")
        await page.sleep(.02)
        assert page.evaluate("ready") is True
        await page.sleep(-1)  # clamped to a single cooperative yield
        assert page.evaluate("42") == 42


@pytest.mark.parametrize("wait_until", ["commit", "domcontentloaded", "load"])
async def test_content_injection_has_an_explicit_observation_boundary(wait_until):
    entered, release = threading.Event(), threading.Event()
    def loader(url, request):
        entered.set()
        assert release.wait(3)
        return {"body": "globalThis.externalScriptValue=42", "headers": {"content-type": "text/javascript"}}
    async with Browser(real_network=False, resource_loader=loader, threaded_loaders=True) as browser:
        page = browser.new_page()
        pending = asyncio.create_task(page.set_content('<script src="/a.js"></script>',
            "https://content.test/", wait_until=wait_until))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.sleep(0)
            assert pending.done() is (wait_until == "commit")
            release.set()
            assert await pending is page
            await page.wait_for_load_state()
            assert page.evaluate("externalScriptValue") == 42
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_page_close_waits_for_its_transport_without_waiting_for_siblings(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def request(value):
        entered.set()
        assert release.wait(3)
        return 42
    async with Browser(real_network=False) as browser:
        profile = browser.new_profile()
        page, sibling = profile.new_page(), profile.new_page()
        profile.network_backend = NetworkBackend(http=GatedTransport(request))
        request_task = asyncio.create_task(page.net.get("https://slow.test/"))
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            closing = asyncio.create_task(page.close())
            await asyncio.sleep(.01)
            assert not closing.done()
            assert sibling.evaluate("42") == 42
            await asyncio.wait_for(sibling.close(), .5)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert page in profile.pages
            release.set()
            await page.close()
            assert page not in profile.pages
            assert profile.network_backend._http_scheduler.active_count == 0
        finally:
            release.set()
            await page.close()


async def test_paced_input_yields_and_cancellation_does_not_invent_a_click():
    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        await page.set_content('<button id="go" style="width:100px;height:100px">go</button>')
        page.evaluate("globalThis.clicked=0;globalThis.tick=0;document.querySelector('#go').onclick=()=>clicked++;setTimeout(()=>tick++,5)")
        await page.hover("#go", duration=.03)
        assert page.evaluate("tick") == 1
        pending = asyncio.create_task(page.click("#go", delay=1))
        await asyncio.sleep(.01)
        assert page.mouse.pressed == frozenset({"left"})
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert page.mouse.pressed == frozenset()
        assert page.evaluate("clicked") == 0
        await page.click("#go")
        assert page.evaluate("clicked") == 1


async def test_document_replacement_stops_a_suspended_gesture():
    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        await page.set_content('<button id="go">go</button>')
        pending = asyncio.create_task(page.click("#go", delay=.05))
        await asyncio.sleep(.01)
        await page.set_content('<button id="new">new</button>')
        page.evaluate("globalThis.clicked=false;onclick=()=>clicked=true")
        with pytest.raises(HostStateError, match="document changed"):
            await pending
        assert page.evaluate("clicked") is False
        await page.click("#new")
        assert page.evaluate("clicked") is True


async def test_a_queued_gesture_cannot_use_an_element_from_the_old_document():
    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        await page.set_content('<button id="go">go</button>')
        first = asyncio.create_task(page.click("#go", delay=.04))
        await asyncio.sleep(.005)
        second = asyncio.create_task(page.click("#go"))
        await asyncio.sleep(0)
        await page.set_content('<button id="next">next</button>')
        page.evaluate("globalThis.clicked=false;onclick=()=>clicked=true")
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, HostStateError) for result in results)
        assert page.evaluate("clicked") is False
        await page.click("#next")
        assert page.evaluate("clicked") is True


async def test_failed_page_retirement_stops_public_admission_and_remains_retryable(monkeypatch):
    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        original = page._close_owner
        monkeypatch.setattr(page, "_close_owner", lambda: (_ for _ in ()).throw(RuntimeError("retry")))
        with pytest.raises(RuntimeError, match="retry"):
            await page.close()
        with pytest.raises(HostStateError, match="closing"):
            page.evaluate("42")
        monkeypatch.setattr(page, "_close_owner", original)
        await page.close()


async def test_page_close_owns_raw_transport_delivery_before_profile_disposal():
    from concurrent.futures import ThreadPoolExecutor
    from pyv8c import NetworkResponse

    entered, release = threading.Event(), threading.Event()
    class Transport(GatedTransport):
        def __init__(self):
            super().__init__(self._call, executor=ThreadPoolExecutor(max_workers=1))
            self._lock = threading.RLock()
            self._closed = False

        def _call(self, request):
            entered.set()
            assert release.wait(3)
            return NetworkResponse(url=request.url, status=200, body="done")

        def close(self):
            self._closed = True
            super().close()

    transport = Transport()
    try:
        async with Browser(http_engine=transport, ws_engine=None) as browser:
            profile = browser.new_profile()
            page = profile.new_page()
            await page.set_content("<body>local</body>", "https://transport.test/")
            page.evaluate("fetch('/slow').catch(()=>{})")
            assert await asyncio.to_thread(entered.wait, 1)
            closing = asyncio.create_task(page.close())
            try:
                await asyncio.sleep(.01)
                assert not closing.done()
                assert not transport._closed
                release.set()
                await closing
                assert page._background._network == 0
                assert not transport._closed
                await profile.close()
                assert transport._closed
            finally:
                release.set()
                await page.close()
    finally:
        release.set()
        transport.close()
