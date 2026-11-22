"""Public completion, document ownership and end-to-end HTTP budgets."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.client import connect

from pyv8c import DevToolsOptions, NetworkResponse
from _http_fixtures import GatedTransport

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import HostStateError


@pytest.mark.parametrize("action, options", [
    ("click", {}), ("click", {"delay": .01}), ("dblclick", {"duration": .01}),
])
async def test_element_gesture_stops_when_move_replaces_the_document(action, options):
    async with Browser(real_network=False, stub_logging="off") as browser:
        page = browser.new_page()
        await page.set_content('<button id="old" style="width:300px;height:100px">old</button>')
        page.evaluate("""
            globalThis.received = [];
            document.querySelector('#old').addEventListener('mousemove', () => {
                document.open();
                document.write('<button id="new" style="width:300px;height:100px">new</button>');
                document.close();
                for (const type of ['mousedown', 'mouseup', 'click', 'dblclick']) {
                    document.querySelector('#new').addEventListener(type, () => received.push(type));
                }
            }, {once: true});
        """)
        with pytest.raises(HostStateError, match="document changed during input"):
            await getattr(page, action)("#old", **options)
        assert page.evaluate("[!!document.querySelector('#new'), received]") == [True, []]
        assert not page.mouse.pressed


class RecordingTransport(GatedTransport):
    def __init__(self, release=None):
        super().__init__(lambda request: self._call(request), executor=ThreadPoolExecutor(max_workers=2))
        self._lock = threading.RLock()
        self._closed = False
        self.entered = threading.Event()
        self.release = release
        self.active = 0
        self.closed_while_active = False
        self.requests = []

    def _call(self, request):
        with self._lock:
            self.requests.append(request)
            self.active += 1
        try:
            self.entered.set()
            if self.release is not None:
                assert self.release.wait(3), "test transport was not released"
            return NetworkResponse(url=request.url, status=200, body="globalThis.answer=42",
                                   headers={"content-type": "text/javascript"})
        finally:
            with self._lock:
                self.active -= 1

    def close(self):
        with self._lock:
            self.closed_while_active |= self.active != 0
            self._closed = True
        super().close()


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


@pytest.mark.parametrize("source", ["fetch('/slow').catch(()=>{})", "importScripts('/slow')"])
@pytest.mark.parametrize("owner", ["page", "profile", "browser"])
@pytest.mark.parametrize("cancel_observer", [False, True])
async def test_worker_io_finishes_before_its_owner_and_network_session_close(source, owner, cancel_observer):
    release = threading.Event()
    transport = RecordingTransport(release)
    def intercept(url, request):
        if urlsplit(url).path == "/worker.js":
            return {"body": source, "headers": {"content-type": "text/javascript"}}
        return None
    async with Browser(http_engine=transport, ws_engine=None, resource_interceptor=intercept) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        await page.set_content("<body>parent</body>", "https://worker.test/")
        page.evaluate("globalThis.worker=new Worker('/worker.js')")
        target = {"page": page, "profile": profile, "browser": browser}[owner]
        closing = None
        try:
            assert await asyncio.to_thread(transport.entered.wait, 1)
            closing = asyncio.create_task(target.close())
            await until(lambda: page.closed)
            await asyncio.sleep(.01)
            assert not closing.done(), "close must retain running Worker I/O"
            assert not transport._closed
            if cancel_observer:
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
            release.set()
            await target.close()
            await profile.close()
            assert transport._closed and not transport.closed_while_active
        finally:
            release.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)


async def cdp_request(ws, sequence, method, params=None):
    await ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
    async with asyncio.timeout(2):
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == sequence:
                assert "error" not in message, message
                return message["result"]


async def close_target(page, profile):
    handle = profile.start_devtools(page, options=DevToolsOptions(pause_on_start=False))
    try:
        async with connect(f"ws://{handle.host}:{handle.port}/") as ws:
            targets = await cdp_request(ws, 1, "Target.getTargets")
            target = next(item["targetId"] for item in targets["targetInfos"] if item["type"] == "page")
            assert (await cdp_request(ws, 2, "Target.closeTarget", {"targetId": target}))["success"]
    finally:
        handle.close()


@pytest.mark.parametrize("eager", [False, True])
async def test_cdp_retirement_completes_and_removes_pages_from_a_live_profile(eager):
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if eager:
        loop.set_task_factory(asyncio.eager_task_factory)
    try:
        async with Browser(real_network=False) as browser:
            profile = browser.new_profile()
            for _ in range(3):
                page = profile.new_page()
                await close_target(page, profile)
                await until(lambda: page not in profile.pages)
                assert page.closed
                await page.close()
            assert profile.new_page().evaluate("42") == 42
    finally:
        loop.set_task_factory(previous_factory)


async def test_cdp_close_keeps_pending_io_owned_when_a_close_observer_is_cancelled():
    release = threading.Event()
    transport = RecordingTransport(release)
    async with Browser(http_engine=transport, ws_engine=None) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        request = asyncio.create_task(page.net.get("https://slow.test/"))
        try:
            assert await asyncio.to_thread(transport.entered.wait, 1)
            await close_target(page, profile)
            assert page.closed and page in profile.pages
            assert page._close_task is not None
            observer = asyncio.create_task(page.close())
            await asyncio.sleep(0)
            observer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observer
            assert not page._close_task.done()
            release.set()
            await until(lambda: page not in profile.pages)
            assert (await request).status == 200
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


async def test_failed_cdp_async_cleanup_is_retained_and_retryable(monkeypatch):
    transport = RecordingTransport()
    async with Browser(http_engine=transport, ws_engine=None) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        await page.net.get("https://done.test/")
        wait = page._background.wait
        attempts = []
        async def fail_once():
            attempts.append(None)
            if len(attempts) == 1:
                raise RuntimeError("retry async cleanup")
            await wait()
        monkeypatch.setattr(page._background, "wait", fail_once)
        await close_target(page, profile)
        await until(lambda: page._close_task is not None and page._close_task.done())
        assert isinstance(page._close_task.exception(), RuntimeError)
        assert page in profile.pages
        await page.close()
        assert page not in profile.pages and len(attempts) == 2


async def test_parent_internal_close_waits_for_the_child_page_completion():
    release = threading.Event()
    transport = RecordingTransport(release)
    async with Browser(http_engine=transport, ws_engine=None) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        await page.set_content("<iframe></iframe>")
        child = next(iter(page._owned.frames))
        request = asyncio.create_task(child.net.get("https://child.test/"))
        try:
            assert await asyncio.to_thread(transport.entered.wait, 1)
            await close_target(page, profile)
            assert page.closed and child.closed
            assert page in profile.pages and page._child_pages == {child}
            assert child._close_task is not None
            release.set()
            await page.close()
            assert not page._child_pages and not profile.pages
            assert profile.network_backend._http_scheduler.active_count == 0
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("action", ["move", "down", "up", "click", "dblclick"])
@pytest.mark.parametrize("cancel_queued", [False, True])
async def test_coordinate_input_keeps_its_admitted_document_while_queued(action, cancel_queued):
    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        await page.set_content('<button style="width:100px;height:100px">old</button>')
        mouse = page.mouse
        first = asyncio.create_task(mouse.click(20, 20, delay=.05))
        await until(lambda: mouse.pressed == frozenset({"left"}))
        coordinates = (20, 20) if action in ("move", "click", "dblclick") else ()
        queued = asyncio.create_task(getattr(mouse, action)(*coordinates))
        await asyncio.sleep(0)
        if cancel_queued:
            queued.cancel()
        await page.set_content('<button style="width:100px;height:100px">new</button>')
        page.evaluate("globalThis.events=[];for(const name of ['pointermove','pointerdown','pointerup','click'])addEventListener(name,e=>events.push(e.type))")
        results = await asyncio.gather(first, queued, return_exceptions=True)
        assert isinstance(results[0], HostStateError)
        expected = asyncio.CancelledError if cancel_queued else HostStateError
        assert isinstance(results[1], expected)
        assert page.evaluate("events") == []
        assert mouse.pressed == frozenset()
        await mouse.click(20, 20)
        assert "click" in page.evaluate("events")


class SlowHeader:
    def __init__(self, seconds):
        self.seconds = seconds

    def __str__(self):
        time.sleep(self.seconds)
        return "prepared"


async def test_http_budget_expired_during_preparation_prevents_transport_submission():
    transport = RecordingTransport()
    async with Browser(http_engine=transport, ws_engine=None) as browser:
        page = browser.new_page()
        with pytest.raises(TimeoutError):
            await page.net.post("https://timeout.test/", headers={"X-Slow": SlowHeader(.05)}, timeout=.01)
        assert transport.requests == []


async def test_http_transport_receives_only_the_budget_remaining_after_preparation():
    transport = RecordingTransport()
    async with Browser(http_engine=transport, ws_engine=None) as browser:
        page = browser.new_page()
        assert (await page.net.get("https://timeout.test/", headers={"X-Slow": SlowHeader(.025)}, timeout=.2)).status == 200
        assert 0 < transport.requests[0].timeout < .18


@pytest.mark.parametrize("timeout", [0, -.01])
async def test_an_exhausted_http_budget_prevents_submission(timeout):
    transport = RecordingTransport()
    async with Browser(http_engine=transport, ws_engine=None) as browser:
        with pytest.raises(TimeoutError):
            await browser.new_page().net.get("https://timeout.test/", timeout=timeout)
        assert not transport.requests


async def test_http_without_a_deadline_and_upstream_timeout_identity(monkeypatch):
    transport = RecordingTransport()
    async with Browser(http_engine=transport, ws_engine=None) as browser:
        page = browser.new_page()
        assert (await page.net.get("https://timeout.test/", timeout=None)).status == 200
        assert transport.requests[0].timeout is None
        error = TimeoutError("transport timeout")
        def fail(request):
            raise error
        monkeypatch.setattr(transport, "_call", fail)
        with pytest.raises(TimeoutError) as caught:
            await page.net.get("https://timeout.test/", timeout=1)
        assert caught.value is error


async def test_http_queue_budget_is_checked_even_when_the_owner_loop_is_blocked():
    release = threading.Event()
    transport = RecordingTransport(release)
    async with Browser(http_engine=transport, ws_engine=None) as browser:
        page = browser.new_page()
        active = [asyncio.create_task(page.net.get(f"https://queue.test/{i}", timeout=2)) for i in (0, 1)]
        timer = None
        try:
            await until(lambda: transport.active == 2)
            queued = asyncio.create_task(page.net.post("https://queue.test/expired", timeout=.01))
            await asyncio.sleep(0)
            timer = threading.Timer(.04, release.set)
            timer.start()
            # A transport worker becomes free after the deadline while asyncio
            # cannot run its timeout callback. The expired POST must not start.
            time.sleep(.08)
            with pytest.raises(TimeoutError):
                await queued
            assert all(not request.url.endswith("/expired") for request in transport.requests)
        finally:
            release.set()
            if timer is not None:
                await asyncio.to_thread(timer.join)
            await asyncio.gather(*active, return_exceptions=True)


async def test_network_backend_close_failure_keeps_the_profile_retryable(monkeypatch):
    """An engine close failure reaches first_error and keeps the backend owned."""
    from types import SimpleNamespace

    attempts = []

    def close_backend():
        attempts.append(None)
        if len(attempts) == 1:
            raise RuntimeError("engine close failed")

    async with Browser(real_network=False, stub_logging="off") as browser:
        profile = browser.new_profile()
        profile.new_page()
        backend = SimpleNamespace(
            close=close_backend, http_available=False, cookies=None,
        )
        monkeypatch.setattr(profile, "network_backend", backend)
        with pytest.raises(RuntimeError, match="engine close failed"):
            await profile.close()
        assert profile.network_backend is backend, "a failed close keeps the backend owned"
        assert not profile.closed
        assert len(attempts) == 1
        await profile.close()
        assert len(attempts) == 2
        assert profile.network_backend is None
