"""Public synchronous entry: real scheduler, transports, Worker and CDP.

No live websites are used. Async lifecycle/cancellation coverage lives beside
this file; these cases exercise the persistent-loop observation boundary.
"""

import asyncio
import gc
import inspect
import json
import signal
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from urllib.parse import urlsplit

import pytest
from websockets.sync.client import connect

pytest.importorskip("pyv8c.pyv8")
from pyv8c import DevToolsOptions, NetworkResponse, pyv8
from pyv8c import async_api, sync_api
from _http_fixtures import GatedTransport

from pyv8c.foundation.host_errors import HostStateError, RealmBusyError
from pyv8c.platform.runtime import Runtime


def loader(url, request):
    body, kind = {
        "/doc": ("<title>loaded</title><script>globalThis.answer=42</script>", "text/html"),
        "/fetch": ('{"answer":42}', "application/json"),
        "/worker.js": ("onmessage=e=>{postMessage(e.data*2);setTimeout(()=>postMessage('timer'),2)}", "text/javascript"),
    }.get(urlsplit(url).path, ("<body></body>", "text/html"))
    return {"url": url, "status": 200, "body": body, "headers": {"content-type": kind}}


@pytest.fixture
def set_sigint_handler():
    previous = signal.getsignal(signal.SIGINT)
    try:
        yield lambda handler: signal.signal(signal.SIGINT, handler)
    finally:
        signal.signal(signal.SIGINT, previous)


def test_explicit_namespaces_and_shared_kernel():
    import pyv8c
    from pyv8c.platform.runtime import Runtime
    for name in ("Browser", "Profile", "Page", "run", "Runtime"):
        assert name not in pyv8c.__all__
        assert not hasattr(pyv8c, name)
    assert inspect.iscoroutinefunction(async_api.Page.goto)
    assert not inspect.iscoroutinefunction(sync_api.Page.goto)
    assert not inspect.iscoroutinefunction(async_api.Page.evaluate)
    with sync_api.Browser(real_network=False) as browser:
        assert isinstance(browser._impl._runtime, Runtime)
        assert browser._impl._runtime.owner == threading.get_ident()
        assert browser._driver._runner.get_loop() is browser._impl._runtime.loop
        profile = browser.new_profile()
        page = profile.new_page()
        assert isinstance(profile, sync_api.Profile) and isinstance(page, sync_api.Page)
        assert profile.browser is browser and page.profile is profile
        assert browser.profiles == [profile] and profile.pages == [page]
        assert browser.new_page().profile is browser.new_page().profile
        assert not hasattr(page._impl.execution_group.executor, "_thread")
    page.close()
    profile.close()
    browser.close()
    assert page.profile is profile and profile.browser is browser
    with pytest.raises(HostStateError):
        browser.__enter__()


def test_context_managers_cleanup_even_when_the_application_raises():
    class ApplicationFailure(Exception):
        pass
    with pytest.raises(ApplicationFailure):
        with sync_api.Browser(real_network=False) as browser:
            with browser.new_profile() as profile:
                with profile.new_page() as page:
                    raise ApplicationFailure()
    assert browser.closed and profile.closed and page.closed
    assert browser._driver._runner is None


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_startup_interrupt_cleans_up_an_open_runtime(monkeypatch, set_sigint_handler, cleanup_fails):
    browser = sync_api.Browser(real_network=False)
    open_runtime = Runtime.open
    original_close = browser._impl._finish_close

    def interrupt_startup(runtime):
        open_runtime(runtime)
        runtime.loop.call_soon(signal.raise_signal, signal.SIGINT)

    async def fail_close(*args):
        raise RuntimeError("injected startup cleanup failure")

    monkeypatch.setattr(Runtime, "open", interrupt_startup)
    set_sigint_handler(signal.default_int_handler)
    if cleanup_fails:
        monkeypatch.setattr(browser._impl, "_finish_close", fail_close)

    with pytest.raises(KeyboardInterrupt) as interrupted:
        browser.__enter__()
    loop = browser._impl._runtime.loop
    if cleanup_fails:
        assert not loop.is_closed() and not browser.closed
        assert any("injected startup cleanup failure" in note for note in interrupted.value.__notes__)
        monkeypatch.setattr(browser._impl, "_finish_close", original_close)
    else:
        assert browser.closed and loop.is_closed()
    browser.close()
    assert browser.closed and loop.is_closed()


def test_blocking_operations_capture_the_callers_current_context():
    request_id = ContextVar("request_id", default="unset")
    seen = []

    def trace(*event):
        seen.append(request_id.get())

    request_id.set("at entry")
    with sync_api.Browser(real_network=False, trace=True, trace_callback=trace) as browser:
        page = browser.new_page()
        for value in ("first call", "second call"):
            request_id.set(value)
            seen.clear()
            page.evaluate('document.createElement("div").id="direct"')
            assert seen and set(seen) == {value}
            seen.clear()
            page.set_content('<script>document.createElement("div").id="waiting"</script>')
            assert seen and set(seen) == {value}
            assert request_id.get() == value


@pytest.mark.parametrize("scope", ["browser", "profile", "page"])
def test_sync_close_retires_ready_page_tasks_before_pumping(scope):
    events = []
    with sync_api.Browser(real_network=False, trace=True,
                          trace_callback=lambda *event: events.append(event)) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        page.evaluate("setTimeout(() => navigator.userAgent, 0)")
        events.clear()
        {"browser": browser, "profile": profile, "page": page}[scope].close()
        assert page.closed
        assert not any("userAgent" in repr(event) for event in events)


def test_direct_evaluation_handles_microtasks_and_exception_identity():
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        runner = browser._driver._runner
        # Evaluation cannot enter Runner: its fast path stays direct.
        original = runner.run
        runner.run = lambda *a, **k: pytest.fail("evaluate must not enter the loop")
        try:
            assert page.evaluate("Promise.resolve().then(()=>globalThis.answer=42);globalThis.answer") != 42
            assert page.evaluate("answer") == 42
            assert page.evaluate("({items:[1,2],answer:42})") == {"items": [1, 2], "answer": 42}
            assert page.evaluate_handle("({answer:42})").to_python() == {"answer": 42}
            with pytest.raises(pyv8.JSException):
                page.evaluate("throw new Error('test')")
        finally:
            runner.run = original
        assert page.evaluate("6*7") == 42


def test_waits_use_one_loop_and_host_sleep_does_not_pump_timers():
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        loop = browser._impl._runtime.loop
        page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,2)")
        time.sleep(.02)
        assert page.evaluate("ready") is False
        page.wait_for("ready", timeout=1)
        assert page.evaluate("ready") is True
        assert page.run_until_idle(timeout=1)
        assert browser._driver._runner.get_loop() is loop
        with pytest.raises(TimeoutError):
            page.wait_for("false", timeout=.015)
        assert page.evaluate("42") == 42


def test_page_sleep_pumps_page_tasks_without_entering_js():
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        loop = browser._impl._runtime.loop
        page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,2)")
        time.sleep(.02)
        assert page.evaluate("ready") is False
        page.sleep(.02)
        assert page.evaluate("ready") is True
        assert browser._driver._runner.get_loop() is loop
        # Invalid durations are clamped to a single cooperative yield.
        page.sleep(-1)
        assert page.evaluate("42") == 42


def test_native_execution_timeout_recovers_on_the_same_page():
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        with pytest.raises((pyv8.JavaScriptTerminated, RealmBusyError)):
            page.evaluate("while(true){}", timeout=.02)
        assert page.evaluate("42", timeout=1) == 42


def test_sync_browsers_have_independent_persistent_loops():
    with sync_api.Browser(real_network=False) as outer:
        page = outer.new_page()
        page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,1)")
        with sync_api.Browser(real_network=False) as inner:
            second = inner.new_page()
            second.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,10)")
            second.wait_for("ready", timeout=1)
            assert page.evaluate("ready") is False
            assert outer._impl._runtime.loop is not inner._impl._runtime.loop
        page.wait_for("ready", timeout=1)


def test_navigation_timeout_cannot_overwrite_a_replacement_document():
    release, entered = threading.Event(), threading.Event()
    def slow_loader(url, request):
        entered.set()
        assert release.wait(3)
        return "<title>stale document</title>"
    with sync_api.Browser(real_network=False, resource_loader=slow_loader, threaded_loaders=True) as browser:
        page = browser.new_page()
        try:
            with pytest.raises(HostStateError):
                page.goto("https://fixture.test/slow", timeout=.02)
            assert entered.is_set()
            page.set_content("<title>replacement</title>")
            release.set()
            page.evaluate("globalThis.settled=false;setTimeout(()=>settled=true,30)")
            page.wait_for("settled", timeout=1)
            assert page.evaluate("document.title") == "replacement"
        finally:
            release.set()


@pytest.mark.parametrize("operation", ["evaluate", "goto", "wait", "click", "net", "new_page", "close"])
def test_foreign_thread_calls_fail_before_creating_coroutines(operation):
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        calls = {
            "evaluate": lambda: page.evaluate("42"),
            "goto": lambda: page.goto("about:blank"),
            "wait": lambda: page.wait_for("true"),
            "click": lambda: page.click("button"),
            "net": lambda: page.net.get("https://fixture.test/"),
            "new_page": browser.new_page,
            "close": browser.close,
        }
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(HostStateError, match="owner thread"):
                pool.submit(calls[operation]).result(timeout=2)
        assert page.evaluate("42") == 42


def test_sync_api_rejects_running_loops_without_coroutine_leaks():
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        async def application():
            for call in (lambda: sync_api.Browser(real_network=False), browser.new_page,
                         lambda: page.goto("about:blank"), lambda: page.evaluate("42"), browser.close):
                with pytest.raises(HostStateError, match="async_api"):
                    call()
        asyncio.run(application())
        assert page.evaluate("42") == 42


def test_js_callbacks_cannot_start_a_nested_wait_in_any_page(monkeypatch):
    with sync_api.Browser(real_network=False) as browser:
        page, sibling = browser.new_page(), browser.new_page()
        failures = []
        def callback():
            for operation in (lambda: page.wait_for("true"), lambda: sibling.wait_for("true"), browser.close):
                with pytest.raises(HostStateError, match="running JS stack"):
                    operation()
                failures.append(True)
        page._impl.v8_context.expose(host_wait=callback)
        original = browser._driver._runner.run
        monkeypatch.setattr(browser._driver._runner, "run", lambda *a: pytest.fail("nested Runner.run"))
        try:
            assert page.evaluate("host_wait();42") == 42
            assert len(failures) == 3
        finally:
            monkeypatch.setattr(browser._driver._runner, "run", original)


def test_closed_pages_and_profiles_cannot_be_reentered():
    with sync_api.Browser(real_network=False) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        page.close()
        with pytest.raises(HostStateError):
            page.__enter__()
        profile.close()
        with pytest.raises(HostStateError):
            profile.__enter__()


def test_cancelled_wait_does_not_poison_the_persistent_loop():
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        loop = browser._impl._runtime.loop
        def cancel_wait():
            for task in asyncio.all_tasks(loop):
                if task.get_coro().__qualname__ == "SyncDriver._run.<locals>.invoke":
                    task.cancel()
        loop.call_soon(cancel_wait)
        with pytest.raises(asyncio.CancelledError):
            page.wait_for("false", timeout=1)
        page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,2)")
        page.wait_for("ready", timeout=1)
        assert page.evaluate("42") == 42


def test_navigation_and_fetch_xhr_worker_and_timers():
    with sync_api.Browser(real_network=False, resource_loader=loader) as browser:
        page = browser.new_page()
        assert page.goto("https://fixture.test/doc").status == 200
        assert page.url == "https://fixture.test/doc"
        page.wait_for_load_state()
        assert page.evaluate("answer") == 42
        for operation in (page.net.fetch, page.net.xhr):
            response = operation("/fetch", timeout=2)
            assert response.status == 200 and response.json() == {"answer": 42}
        page.evaluate("globalThis.messages=[];globalThis.worker=new Worker('/worker.js');"
                      "worker.onmessage=e=>messages.push(e.data);worker.postMessage(21)")
        page.wait_for("messages.length===2", timeout=3)
        assert page.evaluate("messages") == [42, "timer"]
        page.evaluate("worker.terminate()")
    assert not browser._impl._runtime._workers


def test_dom_input_and_document_replacement():
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        assert page.set_content('<button id="go" style="width:100px;height:100px">go</button>') is page
        page.evaluate("globalThis.clicks=0;globalThis.ticks=0;document.querySelector('#go').onclick=()=>clicks++;setTimeout(()=>ticks++,2)")
        page.hover("#go", duration=.02)
        page.click("#go", delay=.01)
        assert page.evaluate("[clicks,ticks]") == [1, 1]
        node = page.query_selector("#go")
        assert node is not None
        page.mouse.click_element(node)
        page.mouse.move(10, 10)
        page.mouse.down()
        assert page.mouse.pressed == frozenset({"left"})
        page.mouse.up()
        assert page.mouse.pressed == frozenset()
        assert page.mouse.position == (10, 10)
        page.set_visibility(True)
        assert page.evaluate("document.hidden") is True
        assert page.dispatch_event("custom", selector="#go")
        page.set_content("<p>new</p>")
        assert page.query_selector("#go") is None


@pytest.mark.parametrize("gesture", [
    "click", "dblclick", "hover", "mouse.click", "mouse.dblclick",
    "mouse.move", "mouse.down", "mouse.up", "mouse.move_to_element", "mouse.click_element",
])
def test_sync_input_rejects_document_replaced_before_dispatch(gesture):
    html = '<button id="go" style="width:100px;height:100px" onclick="clicks++">go</button>'
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        page.set_content(html)
        node = page.query_selector("#go")
        page.evaluate("globalThis.clicks=0; setTimeout(() => {"
                      f"document.open(); document.write({json.dumps(html)}); document.close();"
                      "}, 0)")
        if gesture.startswith("mouse."):
            name = gesture.partition(".")[2]
            args = {
                "click": (10, 10), "dblclick": (10, 10), "move": (10, 10),
                "move_to_element": (node,), "click_element": (node,),
            }.get(name, ())
            operation = getattr(page.mouse, name)
        else:
            operation, args = getattr(page, gesture), ("#go",)
        with pytest.raises(HostStateError, match="document changed during input"):
            operation(*args)
        assert page.evaluate("clicks") == 0
        page.click("#go")
        assert page.evaluate("clicks") == 1


class Transport(GatedTransport):
    def __init__(self, release=None):
        super().__init__(self._call, executor=ThreadPoolExecutor(max_workers=1))
        self.release = release
        self.entered = threading.Event()
        self.requests = []
        self.active = False
        self.closed = False

    def _call(self, request):
        self.requests.append(request)
        self.active = True
        try:
            self.entered.set()
            if self.release is not None:
                assert self.release.wait(3)
            return NetworkResponse(url=request.url, status=200, body="ok")
        finally:
            self.active = False

    def close(self):
        assert not self.active
        super().close()
        self.closed = True


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options"])
def test_host_http_helpers_share_request_budget_and_headers(method):
    transport = Transport()
    with sync_api.Browser(http_engine=transport, ws_engine=None) as browser:
        page = browser.new_page()
        page.net.headers["X-Test"] = "sync"
        response = getattr(page.net, method)("https://fixture.test/", timeout=1)
        assert response.status == 200
        assert transport.requests[-1].method == method.upper()
        assert transport.requests[-1].headers["X-Test"] == "sync"
        assert 0 < transport.requests[-1].timeout <= 1
        with pytest.raises(TimeoutError):
            page.net.get("https://fixture.test/expired", timeout=0)
        assert len(transport.requests) == 1
    assert transport.closed


def test_timeout_retains_active_io_until_close():
    release = threading.Event()
    transport = Transport(release)
    with sync_api.Browser(http_engine=transport, ws_engine=None) as browser:
        page = browser.new_page()
        try:
            with pytest.raises(TimeoutError):
                page.net.get("https://fixture.test/slow", timeout=.02)
            assert transport.entered.is_set() and transport.active
            timer = threading.Timer(.04, release.set)
            timer.start()
            try:
                page.close()
                assert not transport.active
            finally:
                release.set()
                timer.join()
        finally:
            release.set()
    assert transport.closed


def test_sync_loop_queue_time_is_part_of_the_http_deadline():
    transport = Transport()
    with sync_api.Browser(http_engine=transport, ws_engine=None) as browser:
        page = browser.new_page()
        browser._impl._runtime.loop.call_soon(time.sleep, .03)
        with pytest.raises(TimeoutError):
            page.net.post("https://fixture.test/expired", timeout=.01)
        assert transport.requests == []
        assert page.net.get("https://fixture.test/valid", timeout=1).status == 200


@pytest.mark.parametrize("custom_handler", [False, True])
def test_sigint_cannot_leave_a_post_queued(set_sigint_handler, custom_handler):
    def interrupt(*args):
        raise KeyboardInterrupt("custom SIGINT")

    set_sigint_handler(interrupt if custom_handler else signal.default_int_handler)
    transport = Transport()
    with sync_api.Browser(http_engine=transport, ws_engine=None) as browser:
        page = browser.new_page()
        loop = browser._impl._runtime.loop
        loop.call_soon(signal.raise_signal, signal.SIGINT)
        with pytest.raises(KeyboardInterrupt):
            page.net.post("https://fixture.test/interrupted", timeout=1)
        assert not asyncio.all_tasks(loop)
        assert page.net.get("https://fixture.test/later", timeout=1).status == 200
        assert [request.method for request in transport.requests] == ["GET"]


def test_custom_sigint_unwinds_an_input_task_already_waiting(set_sigint_handler):
    def interrupt(*args):
        raise KeyboardInterrupt("interrupt while the button is down")

    set_sigint_handler(interrupt)
    with sync_api.Browser(real_network=False) as browser:
        page = browser.new_page()
        page.set_content('<button id="go">go</button>')
        loop = browser._impl._runtime.loop

        def interrupt_soon():
            loop.call_soon(signal.raise_signal, signal.SIGINT)

        page._impl.v8_context.expose(interrupt_soon=interrupt_soon)
        page.evaluate("""
            globalThis.clicks = 0;
            document.querySelector('#go').onmousedown = () => interrupt_soon();
            document.querySelector('#go').onclick = () => clicks++;
        """)
        with pytest.raises(KeyboardInterrupt, match="button is down"):
            page.click("#go", delay=5)
        assert not asyncio.all_tasks(loop)
        assert not page.mouse.pressed
        assert page.evaluate("clicks") == 0
        page.evaluate("document.querySelector('#go').onmousedown = null")
        page.click("#go")
        assert page.evaluate("clicks") == 1


def test_cancelled_close_observer_retains_cleanup_for_retry():
    release = threading.Event()
    transport = Transport(release)
    browser = sync_api.Browser(http_engine=transport, ws_engine=None).__enter__()
    page = browser.new_page()
    try:
        with pytest.raises(TimeoutError):
            page.net.get("https://fixture.test/slow", timeout=.02)
        loop = browser._impl._runtime.loop
        def cancel_observer():
            for task in asyncio.all_tasks(loop):
                if task.get_coro().__qualname__ == "SyncDriver._run.<locals>.invoke":
                    task.cancel()
        loop.call_later(.01, cancel_observer)
        with pytest.raises(asyncio.CancelledError):
            browser.close()
        assert not browser.closed and browser._driver._runner is not None
        assert transport.active and not transport.closed
        release.set()
    finally:
        release.set()
        browser.close()
    assert browser.closed and transport.closed


def test_failed_browser_close_keeps_loop_and_cleanup_retryable(monkeypatch):
    browser = sync_api.Browser(real_network=False).__enter__()
    page = browser.new_page()
    wait = page._impl._background.wait
    attempts = []
    failing = [True]
    async def fail_once():
        attempts.append(None)
        if failing[0]:
            raise RuntimeError("retry cleanup")
        await wait()
    monkeypatch.setattr(page._impl._background, "wait", fail_once)
    try:
        with pytest.raises(RuntimeError, match="retry cleanup"):
            browser.close()
        assert not browser.closed and browser._driver._runner is not None
        with pytest.raises(HostStateError):
            page.evaluate("42")
    finally:
        failing[0] = False
        browser.close()
    assert browser.closed and browser._driver._runner is None and len(attempts) >= 2


def test_cdp_can_drive_a_sync_page_during_cooperative_wait():
    with sync_api.Browser(real_network=False) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        page.evaluate("globalThis.cdpDone=false")
        handle = profile.start_devtools(page, options=DevToolsOptions(pause_on_start=False))
        def client():
            with connect(f"ws://{handle.host}:{handle.port}/", close_timeout=1) as ws:
                ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "globalThis.cdpDone=true;42", "returnByValue": True}}))
                while True:
                    message = json.loads(ws.recv(timeout=3))
                    if message.get("id") == 1:
                        assert message["result"]["result"]["value"] == 42
                        return
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(client)
                page.wait_for("cdpDone", timeout=3)
                # Keep pumping until the protocol reply has left the owner.
                deadline = time.perf_counter() + 3
                while not result.done() and time.perf_counter() < deadline:
                    page.run_until_idle(timeout=.1)
                result.result(timeout=3)
        finally:
            handle.close()


def test_sync_debugger_handshake_wait_drives_the_owner_loop():
    release = threading.Event()
    with sync_api.Browser(real_network=False) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        handle = profile.start_devtools(page, options=DevToolsOptions(pause_on_start=False, wait_for_debugger=True))
        assert isinstance(handle, sync_api.DevToolsHandle)
        def client():
            with connect(f"ws://{handle.host}:{handle.port}/", close_timeout=1) as ws:
                for sequence, method in enumerate(("Runtime.enable", "Debugger.enable", "Runtime.runIfWaitingForDebugger"), 1):
                    ws.send(json.dumps({"id": sequence, "method": method}))
                    while True:
                        message = json.loads(ws.recv(timeout=3))
                        if message.get("id") == sequence:
                            assert "error" not in message
                            break
                assert release.wait(3)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client)
                try:
                    assert handle.wait_for_connection(timeout=2)
                    assert handle.wait_for_ready(timeout=2)
                    assert page.evaluate("42") == 42
                finally:
                    release.set()
                pending.result(timeout=3)
        finally:
            release.set()
            handle.close()


def test_sync_debugger_wait_times_out_cooperatively_and_closed_wait_is_false():
    with sync_api.Browser(real_network=False) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        handle = profile.start_devtools(page, options=DevToolsOptions(pause_on_start=False))
        try:
            assert handle.wait_for_connection(timeout=0) is False
            page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,1)")
            assert handle.wait_for_ready(timeout=.02) is False
            assert page.evaluate("ready") is True
        finally:
            handle.close()
        assert handle.wait_for_ready(timeout=None) is False
        assert handle.wait_for_connection(timeout=None) is False


def test_children_keep_their_browser_alive_without_retaining_closed_siblings():
    browser = sync_api.Browser(real_network=False).__enter__()
    page = browser.new_page()
    reference = weakref.ref(browser)
    del browser
    gc.collect()
    assert reference() is page.profile.browser
    page.close()
    reference().close()
    page_ref = weakref.ref(page)
    del page
    gc.collect()
    assert page_ref() is None and reference() is None
