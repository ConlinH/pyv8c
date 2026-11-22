"""Host waits and HTTP answers end with their operation or document."""

import asyncio

import gc
import threading
import weakref
from types import SimpleNamespace

import pytest
from _http_fixtures import GatedTransport

from pyv8c.foundation.host_errors import HostStateError, RealmBusyError

from pyv8c import NetworkResponse, pyv8
from pyv8c.async_api import Browser

SYNC_XHR = """let xhr=new XMLHttpRequest();
xhr.open('GET','/slow',false);xhr.send();'finished'"""


@pytest.fixture
async def pending_loader():
    entered, release = threading.Event(), threading.Event()

    def loader(url, request):
        entered.set()
        assert release.wait(5)
        return {"body": "offline answer", "headers": {"content-type": "text/plain"}}

    browser = (await Browser(
        real_network=False, stub_logging="off", threaded_loaders=True, resource_loader=loader
    ).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", "https://offline.test/", wait_until="commit"))
    try:
        yield page, entered, release
    finally:
        release.set()
        (await browser.close())


@pytest.mark.parametrize("background", [False, True])
async def test_sync_loader_releases_owner_when_operation_budget_expires(pending_loader, background):
    page, entered, release = pending_loader
    if background:
        page.execution_group.executor.default_timeout = 0.1
        page.tasks.post(lambda: page.scripts.evaluate(SYNC_XHR))
        assert await asyncio.to_thread(entered.wait, 2)
    else:
        with pytest.raises((RealmBusyError, pyv8.JavaScriptTerminated)):
            page.evaluate(SYNC_XHR, timeout=0.1)
        assert entered.is_set()
    # Recovery must not depend on the loader's answer.
    assert page.evaluate("42", timeout=1) == 42
    assert not release.is_set()


async def test_close_interrupts_sync_loader_without_waiting_for_its_answer(pending_loader):
    page, entered, release = pending_loader
    errors = []
    def interrupt():
        try:
            assert entered.wait(2)
            page.execution_group.request_stop()
        except BaseException as exc:
            errors.append(exc)
    control = threading.Thread(target=interrupt)
    control.start()
    try:
        with pytest.raises(pyv8.JavaScriptTerminated):
            page.evaluate(SYNC_XHR, timeout=None)
    finally:
        await asyncio.to_thread(control.join, 2)
    assert not control.is_alive() and not errors
    (await page.close())
    assert page.closed and page.execution_group.executor.closed
    assert not release.is_set()


async def test_sync_loader_keeps_its_shorter_request_timeout(pending_loader):
    page, entered, release = pending_loader
    with pytest.raises(TimeoutError, match="loader did not answer"):
        page.executor.call(
            page.resource_fetcher.load_resource,
            "https://offline.test/slow",
            {"timeout": 0.03},
            timeout=1,
        )
    assert entered.is_set()
    assert page.evaluate("42", timeout=1) == 42
    assert not release.is_set()


@pytest.mark.parametrize("method", ["start_fetch", "start_xhr"])
@pytest.mark.parametrize("transition", ["close", "navigate", "cancel", "complete"])
async def test_host_http_future_has_one_terminal_outcome(pending_loader, method, transition):
    page, entered, release = pending_loader
    controller = page.net._js_http
    future = getattr(controller, method)("https://offline.test/slow")
    assert (await asyncio.to_thread(entered.wait, 2))
    assert not future.done()
    if transition == "close":
        (await page.close())
    elif transition == "navigate":
        (await page.set_content("<body>next</body>", "https://offline.test/next", wait_until="commit"))
    elif transition == "cancel":
        assert future.cancel()
    else:
        release.set()
        assert (await asyncio.to_thread(future.result, 2)).body == "offline answer"
    assert future.done()
    if transition in ("close", "navigate"):
        with pytest.raises(HostStateError, match="replaced or closed"):
            (await asyncio.to_thread(future.result, 0))
    assert not controller._pending
    if transition in ("navigate", "cancel"):
        release.set()
        assert (
            (await asyncio.to_thread(getattr(controller, method)("https://offline.test/next-data").result, 2)).body
            == "offline answer"
        )
        assert page.evaluate("42") == 42
        assert not controller._pending


async def test_http_completion_callback_cannot_start_work_in_retiring_document(pending_loader):
    page, entered, release = pending_loader
    future = page.net._js_http.start_fetch("https://offline.test/slow")
    assert (await asyncio.to_thread(entered.wait, 2))
    restarted = []
    future.add_done_callback(lambda done: restarted.append(page.net._js_http.start_fetch("/other")))
    (await page.set_content("<body>next</body>", "https://offline.test/next", wait_until="commit"))
    assert future.done() and len(restarted) == 1 and restarted[0].done()
    with pytest.raises(HostStateError):
        restarted[0].result(0)
    assert not page.net._js_http._pending


async def test_completed_http_future_does_not_retain_its_controller():
    from pyv8c.platform.js_http import JsHttpController

    page = SimpleNamespace(
        realm=SimpleNamespace(executor=SimpleNamespace(call=lambda callback, **kwargs: callback()))
    )
    controller = JsHttpController(page)
    controller._start = lambda kind, url, init, deliver, reject: deliver(
        SimpleNamespace(to_python=lambda: {"ok": True, "text": "answer"}))
    future = controller.start_fetch("https://offline.test/")
    assert (await asyncio.to_thread(future.result)).body == "answer"
    reference = weakref.ref(controller)
    del controller
    gc.collect()
    assert (reference()) is None


@pytest.mark.parametrize("method", ["fetch", "xhr"])
@pytest.mark.parametrize("transition", ["close", "navigate", "cancel", "complete"])
async def test_public_http_wait_releases_pending_operation(pending_loader, method, transition):
    page, entered, release = pending_loader
    pending = asyncio.create_task(getattr(page.net, method)("/slow", timeout=2))
    assert await asyncio.to_thread(entered.wait, 1)
    assert not pending.done()
    if transition == "close":
        (await page.close())
    elif transition == "navigate":
        (await page.set_content("<body>next</body>", "https://offline.test/next", wait_until="commit"))
    elif transition == "cancel":
        pending.cancel()
    else:
        release.set()
    if transition in ("close", "navigate"):
        with pytest.raises(HostStateError, match="replaced or closed"):
            await pending
    elif transition == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        assert (await pending).body == "offline answer"
    assert not page.net._js_http._pending


@pytest.mark.parametrize("method", ["fetch", "xhr"])
async def test_public_http_timeout_does_not_depend_on_loader_answer(pending_loader, method):
    page, entered, release = pending_loader
    with pytest.raises(TimeoutError, match="did not settle"):
        await getattr(page.net, method)("/slow", timeout=0.03)
    assert entered.is_set() and not release.is_set()
    assert not page.net._js_http._pending
    assert page.evaluate("42") == 42


async def test_public_http_preserves_request_timeout_identity(pending_loader, monkeypatch):
    from concurrent.futures import Future

    page, _, _ = pending_loader
    error = TimeoutError("transport timeout")
    source = Future()
    source.set_exception(error)
    monkeypatch.setattr(page.net._js_http, "start_fetch", lambda *args, **kwargs: source)
    with pytest.raises(TimeoutError) as caught:
        await page.net.fetch("/slow", timeout=1)
    assert caught.value is error


@pytest.mark.parametrize("pool_kind", ["net", "loader"])
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_runtime_joins_io_cooperatively_after_page_close(pool_kind, cancel_waiter):
    from pyv8c.network.backend import NetworkBackend

    entered, release = threading.Event(), threading.Event()
    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    def blocked_request():
        entered.set()
        assert release.wait(3)
        return 42
    try:
        page = runtime.new_page()
        if pool_kind == "net":
            from concurrent.futures import ThreadPoolExecutor

            pool = ThreadPoolExecutor(max_workers=1)
            page.profile.network_backend = NetworkBackend(
                http=GatedTransport(lambda request: NetworkResponse(
                    request.url, body=str(blocked_request())), executor=pool))
            active = asyncio.create_task(page.net.get("http://fixture.test/"))
        else:
            pool = runtime.loader_pool
            active = pool.submit(blocked_request)
        assert await asyncio.to_thread(entered.wait, 1)
        page_closing = asyncio.create_task(page.close())
        await asyncio.sleep(.01)
        assert not active.done(), "owner must remain cooperative while I/O is active"
        if pool_kind == "net":
            assert not page_closing.done()
        closing = asyncio.create_task(((runtime.close())))
        await asyncio.sleep(0.01)
        assert not closing.done() and not release.is_set()
        if cancel_waiter:
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert not runtime._close_task.done()
        release.set()
        await runtime.close()
        await page_closing

        assert runtime.closed
        assert active.result().text == "42" if pool_kind == "net" else active.result() == 42
        assert not any(thread.is_alive() for thread in pool._threads)
        assert not runtime._runtime._io_threads
    finally:
        release.set()
        await runtime.close()


async def test_io_pool_shutdown_failure_remains_owned_for_retry(monkeypatch):
    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    entered, release = threading.Event(), threading.Event()
    pool = runtime.loader_pool
    def blocked():
        entered.set()
        assert release.wait(3)
    active = pool.submit(blocked)
    assert await asyncio.to_thread(entered.wait, 1)
    shutdown = pool.shutdown
    def fail_shutdown(**options):
        raise RuntimeError("pool shutdown failed")
    monkeypatch.setattr(pool, "shutdown", fail_shutdown)
    try:
        with pytest.raises(RuntimeError, match="pool shutdown failed"):
            await runtime.close()
        assert pool in runtime._runtime._retired_io_pools
        assert not runtime.closed and not active.done()
        monkeypatch.setattr(pool, "shutdown", shutdown)
        release.set()
        await runtime.close()
        assert runtime.closed and active.done()
        assert not any(thread.is_alive() for thread in pool._threads)
        assert not runtime._runtime._retired_io_pools
    finally:
        release.set()
        monkeypatch.setattr(pool, "shutdown", shutdown)
        await runtime.close()


async def test_close_network_budget_expiry_remains_owned_for_retry(monkeypatch):
    """A wedged in-flight count must not make page.close() wait forever."""
    from pyv8c.foundation import background

    monkeypatch.setattr(background, "_NETWORK_WAIT_SECONDS", 0.05)
    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    try:
        page = runtime.new_page()
        profile = page.profile
        sibling = profile.new_page()
        ledger = page._background
        # An in-flight count with no tracked future and no host delivery is
        # the one shape that parks on a waiter instead of a future.
        ledger.begin_network()
        with pytest.raises(HostStateError, match="did not finish teardown"):
            await page.close()
        assert page in profile.pages, "a failed teardown keeps ownership"
        assert ledger._network == 1, "the budget never releases the count"
        assert not ledger._waiters, "the timed-out waiter is removed"
        assert sibling.evaluate("42") == 42, "a sibling Page is unaffected"
        ledger.complete_network()
        await page.close()
        assert page not in profile.pages
    finally:
        await runtime.close()


async def test_close_inside_the_network_budget_still_waits_for_the_wakeup(monkeypatch):
    """A count that settles inside the budget closes normally."""
    from pyv8c.foundation import background

    monkeypatch.setattr(background, "_NETWORK_WAIT_SECONDS", 2)
    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    try:
        page = runtime.new_page()
        ledger = page._background
        ledger.begin_network()
        closing = asyncio.create_task(page.close())
        await asyncio.sleep(.05)
        assert not closing.done(), "close still waits while the count is held"
        ledger.complete_network()
        await closing
        assert ledger._network == 0
        assert not ledger._waiters
    finally:
        await runtime.close()


@pytest.mark.parametrize("network", [False, True])
async def test_background_progress_renews_the_close_budget(monkeypatch, network):
    from concurrent.futures import Future
    from pyv8c.foundation import background

    monkeypatch.setattr(background, "_NETWORK_WAIT_SECONDS", .3)
    work = background.BackgroundWork()
    loop = asyncio.get_running_loop()
    for index in range(1, 4):
        if network:
            work.begin_network()
            loop.call_later(.15 * index, work.complete_network)
        else:
            future = Future()
            work.track(future, kind="test")
            loop.call_later(.15 * index, future.set_result, None)
    await work.wait()
    assert not work._waiters


async def test_an_interrupt_hook_does_not_bypass_the_close_budget(monkeypatch):
    """A successful abort is not a completion, even on an interruptible engine."""
    from pyv8c.foundation import background
    from concurrent.futures import Future
    from pyv8c import NetworkRequest
    from pyv8c.network.backend import NetworkBackend

    monkeypatch.setattr(background, "_NETWORK_WAIT_SECONDS", 0.05)
    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    try:
        page = runtime.new_page()
        profile = page.profile
        ledger = page._background
        response = Future()
        stopped = []

        def submit_request(request, handle, remaining):
            handle.attach_abort(lambda: stopped.append(True))
            return response

        backend = NetworkBackend(http=SimpleNamespace(submit_request=submit_request))
        job = backend.submit(ledger, id(page), NetworkRequest("http://fixture.test/"), lambda: None)
        job.cancel()
        assert stopped == [True]
        assert ledger._network == 1, "an abort never releases the count"

        with pytest.raises(HostStateError, match="did not finish teardown"):
            await page.close()
        assert page in profile.pages, "a failed teardown keeps ownership"
        assert ledger._network == 1
        response.set_result(None)
        await page.close()
        assert page not in profile.pages
    finally:
        await runtime.close()
