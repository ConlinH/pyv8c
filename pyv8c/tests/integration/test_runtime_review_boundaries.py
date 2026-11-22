"""Failures and suspension never relinquish owner resource ownership."""
import asyncio
import threading
import time

import pytest

from pyv8c import pyv8
from _http_fixtures import GatedTransport

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import HostStateError
from pyv8c.network.backend import NetworkBackend
from pyv8c.network.models import NetworkResponse


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("source", ["task", "command"])
@pytest.mark.parametrize("with_frame", [False, True])
async def test_control_failure_stops_work_but_retains_teardown(control, source, with_frame):
    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    page = runtime.new_page()
    if with_frame:
        (await page.set_content("<iframe srcdoc='<body>child</body>'></iframe>", wait_until="commit"))
        await page.wait_for_load_state("load")
    frames = tuple(page._owned.frames)
    driver = page.execution_group.executor
    context, isolate = page.v8_context, page.isolate
    original = control("stop")
    def interrupt():
        raise original
    if source == "task":
        page.tasks.post(interrupt)
    else:
        driver.post(interrupt, _task_lane=None)
    try:
        # Execute the same callback the loop schedules, catching the control at
        # the host boundary so pytest itself can continue and verify teardown.
        with pytest.raises(control) as caught:
            driver._turn()
        assert caught.value is original
        assert driver.failed and not driver.closed
        assert driver in runtime._runtime._executors
        with pytest.raises(HostStateError):
            page.evaluate("42")
        assert not await asyncio.to_thread(driver.post, lambda: pytest.fail("failed driver ran"))
        await runtime.close()
        assert runtime.closed and page.closed and isolate.closed
        assert all(frame.closed and frame.v8_context is None for frame in frames)
        with pytest.raises(RuntimeError, match="context is closed"):
            context.exec_js("42")
        assert not runtime._runtime._executors
        await runtime.close()
    finally:
        await runtime.close()


async def test_http_cleanup_failure_cannot_orphan_pool(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    entered, release = threading.Event(), threading.Event()
    page = runtime.new_page()
    def request(request):
        entered.set()
        assert release.wait(3)
        return NetworkResponse(request.url, body="42")
    pool = ThreadPoolExecutor(max_workers=1)
    backend = page.profile.network_backend = NetworkBackend(
        http=GatedTransport(request, executor=pool))
    active = asyncio.create_task((page.net.request("GET", "https://offline.test/")))
    close = page.net._js_http.close
    attempts = []
    def fail_once():
        attempts.append(None)
        if len(attempts) == 1:
            raise RuntimeError("HTTP cleanup failed")
        close()
    monkeypatch.setattr(page.net._js_http, "close", fail_once)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(RuntimeError, match="HTTP cleanup failed"):
            await runtime.close()
        assert page.profile.network_backend is backend
        assert not page.net._close_complete
        closing = asyncio.create_task(((runtime.close())))
        await asyncio.sleep(0.01)
        assert not closing.done() and not active.done()
        release.set()
        await closing
        assert len(attempts) == 2 and page.net._close_complete
        assert active.result().text == "42"
        assert not any(thread.is_alive() for thread in pool._threads)
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize("pending", [False, True])
async def test_load_wait_budget_covers_registration_checkpoint(pending):
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        promise = page.evaluate_handle("Promise.resolve(1)")
        def work(value):
            page.evaluate("(()=>{const until=performance.now()+500;while(performance.now()<until){};return 42})()")
        promise.then(work)
        if pending:
            page.lifecycle.document_load_fired = False
        started = time.perf_counter()
        with pytest.raises((TimeoutError, pyv8.JavaScriptTerminated)):
            await page.wait_for_load_state("load", timeout=0.02)
        assert time.perf_counter() - started < 0.3
        assert not page.lifecycle._state_waiters
        assert page.evaluate("42") == 42


@pytest.mark.parametrize("action", ["move", "click", "down", "up", "dblclick"])
async def test_cached_mouse_rejects_foreign_event_calls(action):
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        mouse = page.mouse
        page.evaluate("globalThis.events=[];for(const name of ['mousemove','mousedown','mouseup','click'])addEventListener(name,e=>events.push(e.type))")
        with pytest.raises(HostStateError, match="owner thread"):
            await asyncio.to_thread(lambda: asyncio.run(getattr(mouse, action)(*([10, 10] if action == "move" else []))))
        assert page.evaluate("events") == []
        assert mouse.position == (0, 0) and mouse.pressed == frozenset()
        with pytest.raises(HostStateError, match="owner thread"):
            await asyncio.to_thread(lambda: page.mouse)


@pytest.mark.parametrize("close_runtime", [False, True])
async def test_idle_wait_reports_shutdown_instead_of_success(close_runtime):
    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    page = runtime.new_page()
    def again():
        if not page.closed:
            page.tasks.post(again)
    page.tasks.post(again)
    pending = asyncio.create_task(page.run_until_idle(1))
    try:
        await asyncio.sleep(0.01)
        assert not pending.done()
        if close_runtime:
            await runtime.close()
        else:
            (await page.close())
        with pytest.raises(HostStateError):
            await pending
    finally:
        await runtime.close()
