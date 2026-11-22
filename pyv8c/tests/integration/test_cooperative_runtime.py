"""Contract tests for caller ownership and cooperative waits, using real controllers."""
from pyv8c.async_api import run

import asyncio
import functools
import gc
import json
import threading
import time
import weakref
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from pyv8c.foundation import timer_resolution
from pyv8c.foundation.host_errors import HostStateError

pytest.importorskip("pyv8c.pyv8")
from pyv8c.foundation.futures import bridge_future
from pyv8c.foundation.owner_executor import OwnerExecutor

from pyv8c.async_api import Browser

run_owned = run
from websockets.asyncio.client import connect

from pyv8c import DevToolsOptions, pyv8


def run_async(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        async def checked():
            loop = asyncio.get_running_loop()
            errors = []
            loop.set_exception_handler(lambda _, context: errors.append(context))
            await asyncio.wait_for(function(*args, **kwargs), 15)
            await asyncio.sleep(0)
            assert not errors, errors

        run_owned(checked())

    return run


def loader(url, request):
    path = urlsplit(url).path
    body, kind = {
        "/doc": (
            "<title>loaded</title><body><script>globalThis.loaded=42</script></body>",
            "text/html",
        ),
        "/fetch": ("hello", "text/plain"),
        "/worker.js": (
            "onmessage=e=>{postMessage(e.data*2);setTimeout(()=>postMessage('timer'),2)}",
            "text/javascript",
        ),
        "/slow-script.js": ("globalThis.externalScript=42", "text/javascript"),
    }.get(path, ("<body></body>", "text/html"))
    return {"status": 200, "url": url, "body": body, "headers": {"content-type": kind}}


@run_async
async def test_page_and_vm_are_on_the_application_thread_without_an_owner_thread():
    before = set(threading.enumerate())
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        driver = page.execution_group.executor
        assert isinstance(driver, OwnerExecutor)
        assert driver._owner_ident == threading.get_ident()
        assert not hasattr(driver, "_thread")
        assert page.evaluate("6*7") == 42
        assert not any(
            t not in before and t.name == "pyv8c-window-group" for t in threading.enumerate()
        )


@run_async
async def test_public_page_evaluation_rejects_a_foreign_thread():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        with pytest.raises(HostStateError, match="owner thread"):
            await asyncio.to_thread(page.evaluate, "1+1")
        assert page.evaluate("1+1") == 2


@run_async
async def test_internal_cross_thread_delivery_is_marshaled_without_moving_the_vm():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        driver = page.execution_group.executor
        result = await asyncio.to_thread(driver.call, threading.get_ident)
        assert result == threading.get_ident()


@run_async
async def test_two_application_threads_can_each_own_a_runtime():
    async def other():
        async with Browser(real_network=False, stub_logging="off") as runtime:
            page = runtime.new_page()
            return threading.get_ident(), page.evaluate("6*7")

    async with Browser(real_network=False, stub_logging="off") as runtime:
        local = runtime.new_page()
        owner, result = await asyncio.to_thread(lambda: run_owned(other()))
        assert owner != threading.get_ident() and result == 42
        assert local.evaluate("1+1") == 2


@run_async
async def test_multiple_runtimes_on_one_loop_keep_lifecycles_isolated():
    async with Browser(real_network=False, stub_logging="off") as first:
        page = first.new_page()
        async with Browser(real_network=False, stub_logging="off") as second:
            other = second.new_page()
            assert page.execution_group is not other.execution_group
            assert other.evaluate("42") == 42
        assert other.closed
        assert page.evaluate("6*7") == 42
        assert not first.closed


@run_async
async def test_timer_runs_while_the_business_coroutine_is_awaiting():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,5)")
        await asyncio.sleep(0.03)
        driver = page.execution_group.executor
        assert page.evaluate("ready") is True, {
            "soon": repr(driver._soon),
            "timer": repr(driver._timer),
            "deadline": driver._next_deadline(),
            "notified": driver._notified,
            "ready": repr(driver._task_queues),
            "clock_resolution": runtime._runtime.loop._clock_resolution,
        }


@run_async
async def test_blocking_the_owner_does_not_pretend_to_make_background_progress():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,5)")
        time.sleep(0.02)
        assert page.evaluate("ready") is False
        await page.wait_for("ready", timeout=1)
        assert page.evaluate("ready") is True


@run_async
async def test_bounded_sources_deliver_microtasks_before_the_next_timer():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        page.evaluate(
            "globalThis.events=[];setTimeout(()=>{events.push('timer');queueMicrotask(()=>events.push('micro'))},1);setTimeout(()=>events.push('next'),2)"
        )
        await page.wait_for("events.length===3", timeout=1)
        assert page.evaluate("events") == ["timer", "micro", "next"]


@run_async
async def test_cooperative_callers_do_not_starve_a_timer():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,5)")

        async def caller():
            for _ in range(1000):
                assert page.evaluate("1+1") == 2
                await asyncio.sleep(0)

        await asyncio.gather(*(caller() for _ in range(8)))
        assert page.evaluate("ready") is True


@pytest.mark.parametrize("state", ["commit", "domcontentloaded", "load"])
@run_async
async def test_navigation_waits_for_the_requested_lifecycle(state):
    async with Browser(real_network=False, resource_loader=loader, stub_logging="off") as runtime:
        page = runtime.new_page()
        response = await page.goto("https://runtime.test/doc", wait_until=state)
        assert response.status == 200
        await page.wait_for_load_state("load")
        assert page.evaluate("[location.href,document.title,loaded,document.readyState]") == [
            "https://runtime.test/doc",
            "loaded",
            42,
            "complete",
        ]


@run_async
async def test_navigation_replaces_the_realm_and_retires_old_handles_on_page_close():
    async with Browser(real_network=False, resource_loader=loader, stub_logging="off") as runtime:
        page = runtime.new_page()
        await page.goto("https://runtime.test/doc")
        old = page.evaluate_handle("({old:42})")
        previous = page.v8_context
        page.evaluate("globalThis.onlyInPreviousDocument=42")
        await page.goto("https://runtime.test/doc?next=1")
        assert page.evaluate("location.search") == "?next=1"
        assert page.v8_context is not previous
        assert page.evaluate("typeof onlyInPreviousDocument") == "undefined"
        (await page.close())
        assert previous.live_handles == 0
        with pytest.raises(RuntimeError):
            old.to_python()


@run_async
async def test_navigation_preserves_a_loader_timeout_exception():
    error = TimeoutError("loader failed")

    def fail(url, request):
        raise error

    async with Browser(real_network=False, resource_loader=fail, stub_logging="off") as runtime:
        with pytest.raises(TimeoutError) as caught:
            await runtime.new_page().goto("https://runtime.test/doc", timeout=1)
        assert caught.value is error


@pytest.mark.parametrize("transition", ["timeout", "cancel", "close"])
@run_async
async def test_waiting_navigation_has_one_terminal_outcome_and_ignores_late_answers(transition):
    entered, release = threading.Event(), threading.Event()

    def slow(url, request):
        entered.set()
        assert release.wait(3)
        return {"body": "<title>late</title>", "headers": {"content-type": "text/html"}}

    async with Browser(
        real_network=False, resource_loader=slow, threaded_loaders=True, stub_logging="off"
    ) as runtime:
        page = runtime.new_page()
        task = asyncio.create_task(
            page.goto("https://runtime.test/slow", timeout=0.04 if transition == "timeout" else 2)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            if transition == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                if transition == "close":
                    (await page.close())
                with pytest.raises(HostStateError):
                    await task
            if not page.closed:
                assert page.evaluate("location.href") == "about:blank"
        finally:
            release.set()
        await asyncio.sleep(0.03)
        if not page.closed:
            assert page.evaluate("location.href") == "about:blank"
            assert page.evaluate("42") == 42


@run_async
async def test_external_scripts_finish_before_load():
    def resources(url, request):
        if urlsplit(url).path == "/doc":
            return {
                "body": "<script src='/slow-script.js'></script>",
                "headers": {"content-type": "text/html"},
            }
        return loader(url, request)

    async with Browser(
        real_network=False, resource_loader=resources, threaded_loaders=True, stub_logging="off"
    ) as runtime:
        page = runtime.new_page()
        await page.goto("https://runtime.test/doc")
        assert page.evaluate("externalScript") == 42


@run_async
async def test_script_requested_navigation_keeps_using_the_navigation_task_source():
    async with Browser(real_network=False, resource_loader=loader, stub_logging="off") as runtime:
        page = (await runtime.new_page().set_content("<body>initial</body>", "https://runtime.test/", wait_until="commit"))
        page.evaluate("location.href='/doc'")
        await page.wait_for(
            "location.pathname==='/doc' && document.readyState==='complete'", timeout=1
        )
        assert page.evaluate("loaded") == 42


@run_async
async def test_fetch_and_xhr_progress_without_evaluation_polling():
    async with Browser(real_network=False, resource_loader=loader, stub_logging="off") as runtime:
        page = (await runtime.new_page().set_content("<body></body>", "https://runtime.test/", wait_until="commit"))
        page.evaluate(
            "globalThis.answer='pending';fetch('/fetch').then(r=>r.text()).then(t=>answer=t)"
        )
        await asyncio.sleep(0.03)
        assert page.evaluate("answer") == "hello"
        page.evaluate(
            "globalThis.xhrAnswer='pending';let x=new XMLHttpRequest();x.open('GET','/fetch');x.onload=()=>xhrAnswer=x.responseText;x.send()"
        )
        await page.wait_for("xhrAnswer==='hello'", timeout=1)


@pytest.fixture
def http_origin():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<body>network</body>" if self.path == "/" else b"http answer"
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path == "/" else "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


@run_async
async def test_real_http_navigation_and_fetch(http_origin):
    async with Browser(stub_logging="off") as runtime:
        page = runtime.new_page()
        response = await page.goto(http_origin + "/", timeout=2)
        assert response.status == 200
        page.evaluate(
            "globalThis.answer='pending';fetch('/data').then(r=>r.text()).then(t=>answer=t)"
        )
        await page.wait_for("answer==='http answer'", timeout=2)


@run_async
async def test_worker_keeps_an_independent_owner_and_delivers_messages():
    async with Browser(real_network=False, resource_loader=loader, stub_logging="off") as runtime:
        page = (await runtime.new_page().set_content("<body></body>", "https://runtime.test/", wait_until="commit"))
        page.evaluate(
            "globalThis.messages=[];globalThis.worker=new Worker('/worker.js');worker.onmessage=e=>messages.push(e.data);worker.postMessage(21)"
        )
        await page.wait_for("messages.length===2", timeout=2)
        assert page.evaluate("messages") == [42, "timer"]
        worker = next(iter(page._owned.workers))
        assert worker._runtime.executor._owner_ident != threading.get_ident()
        assert not isinstance(worker._runtime.executor, OwnerExecutor)
        (await page.close())
        await asyncio.to_thread(worker._runtime.executor._thread.join, 1)
        assert not worker._runtime.executor._thread.is_alive()


@run_async
async def test_wasm_foreground_work_wakes_the_asyncio_owner():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        page.evaluate(
            "globalThis.wasmDone=false;WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0])).then(()=>wasmDone=true)"
        )
        await asyncio.sleep(0.05)
        assert page.evaluate("wasmDone") is True


@run_async
async def test_js_timeout_terminates_without_poisoning_the_next_call():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        with pytest.raises(pyv8.JavaScriptTerminated):
            page.evaluate("while(true){}", timeout=0.03)
        assert page.evaluate("42") == 42


@run_async
async def test_a_blocking_python_callback_delays_host_return_but_not_budget_expiry():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        page.v8_context.expose(blocking=lambda: time.sleep(0.06))
        started = time.perf_counter()
        with pytest.raises(pyv8.JavaScriptTerminated):
            page.evaluate("blocking();42", timeout=0.01)
        # Same-thread execution cannot return while Python's blocking callback
        # is still on this stack. This is an explicit new-contract limit.
        assert time.perf_counter() - started >= 0.05
        assert page.evaluate("42") == 42


@run_async
async def test_predicate_errors_and_wait_timeouts_remain_distinct():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        with pytest.raises(pyv8.JSException):
            await page.wait_for("missing.value", timeout=1)
        with pytest.raises(TimeoutError):
            await page.wait_for("false", timeout=0.02)
        assert page.evaluate("42") == 42


@run_async
async def test_iframe_uses_the_same_owner_without_extra_threads():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = (await runtime.new_page().set_content(
            "<iframe srcdoc='<script>globalThis.answer=42</script>'></iframe>",
            "https://runtime.test/",
         wait_until="commit"))
        await page.wait_for("frames.length===1 && frames[0].answer===42", timeout=1)
        child = next(iter(page._owned.frames))
        assert child.execution_group is page.execution_group
        assert child.execution_group.executor._owner_ident == threading.get_ident()


@run_async
async def test_close_is_idempotent_and_cancels_timer_and_scheduled_turns():
    before = timer_resolution._holders
    runtime = Browser(real_network=False, stub_logging="off")
    async with runtime:
        page = runtime.new_page()
        driver = page.execution_group.executor
        page.evaluate("setInterval(()=>{},5)")
        await asyncio.sleep(0.01)
        (await page.close())
        (await page.close())
        assert driver.closed and driver._soon is None and driver._timer is None
    await runtime.close()
    (await page.close())
    assert not runtime._runtime._executors
    assert timer_resolution._holders == before


@run_async
async def test_a_failed_close_can_be_retried(monkeypatch):
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        original = page._close_owner

        def fail():
            raise RuntimeError("cleanup failed")

        monkeypatch.setattr(page, "_close_owner", fail)
        with pytest.raises(RuntimeError, match="cleanup failed"):
            (await page.close())
        assert not page.closed and not runtime.closed
        monkeypatch.setattr(page, "_close_owner", original)
        (await page.close())
        assert page.closed


@run_async
async def test_completed_future_delivery_does_not_retain_its_destination():
    source = Future()
    destination = bridge_future(source)
    reference = weakref.ref(destination)
    source.set_result(42)
    assert await destination == 42
    del destination
    await asyncio.sleep(0)
    assert reference() is None


@run_async
async def test_profile_creation_failure_does_not_publish_partial_state(monkeypatch):
    from pyv8c.platform.browser import Browser
    original = Browser.new_profile
    error = RuntimeError("profile creation failed")
    def fail(self, *args, **kwargs):
        raise error
    async with Browser(real_network=False, stub_logging="off") as runtime:
        monkeypatch.setattr(Browser, "new_profile", fail)
        with pytest.raises(RuntimeError) as caught:
            runtime.new_page()
        assert caught.value is error and runtime._default_profile is None
        assert not runtime.profiles
        monkeypatch.setattr(Browser, "new_profile", original)
        assert runtime.new_page().evaluate("42") == 42


@run_async
async def test_cancelling_a_close_waiter_does_not_cancel_runtime_cleanup():
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    runtime.new_page()
    runtime._runtime._worker_threads.add(thread)
    closing = asyncio.create_task(((runtime.close())))
    try:
        await asyncio.sleep(0.01)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not runtime.closed
    finally:
        release.set()
        await runtime.close()
    assert runtime.closed and not thread.is_alive()


@run_async
async def test_repeated_page_trees_workers_and_handles_finish_runtime_teardown():
    for _ in range(8):
        async with Browser(real_network=False, stub_logging="off") as runtime:
            page = (await runtime.new_page().set_content("<iframe srcdoc='<body>child</body>'></iframe>", wait_until="commit"))
            await page.wait_for("frames.length===1", timeout=1)
            page.evaluate(
                "globalThis.reply=0;globalThis.w=new Worker(URL.createObjectURL(new Blob(['onmessage=e=>postMessage(e.data)'])));w.onmessage=e=>reply=e.data;w.postMessage(42)"
            )
            await page.wait_for("reply===42", timeout=2)
            context, isolate = page.v8_context, page.isolate
            handle = page.evaluate_handle("({node:document.body,fn:()=>42})")
            (await page.close())
        assert context.live_handles == 0 and isolate.live_v8_handles == 0
        assert not any(thread.is_alive() for thread in runtime._runtime._worker_threads)
        assert not runtime._runtime._executors
        with pytest.raises(RuntimeError):
            handle.to_python()
    gc.collect()


async def cdp_request(ws, ident, method, params=None, session=None):
    payload = {"id": ident, "method": method}
    if params is not None:
        payload["params"] = params
    if session:
        payload["sessionId"] = session
    await ws.send(json.dumps(payload))
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), 3))
        if message.get("id") == ident:
            assert "error" not in message, message
            return message.get("result", {})


async def attach(ws):
    targets = await cdp_request(ws, 1, "Target.getTargets")
    target = targets["targetInfos"][0]["targetId"]
    result = await cdp_request(ws, 2, "Target.attachToTarget", {"targetId": target})
    return result["sessionId"]


@run_async
async def test_devtools_can_close_a_paused_page_through_its_control_channel():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        handle = runtime._default_profile.start_devtools(page, options=DevToolsOptions(pause_on_start=False))
        ready, paused = threading.Event(), threading.Event()
        errors = []

        async def client():
            async with connect(f"ws://{handle.host}:{handle.port}/") as ws:
                targets = await cdp_request(ws, 1, "Target.getTargets")
                target = targets["targetInfos"][0]["targetId"]
                attached = await cdp_request(ws, 2, "Target.attachToTarget", {"targetId": target})
                session = attached["sessionId"]
                await cdp_request(ws, 3, "Debugger.enable", session=session)
                ready.set()
                while True:
                    message = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    if message.get("method") == "Debugger.paused":
                        paused.set()
                        break
                result = await cdp_request(ws, 4, "Target.closeTarget", {"targetId": target})
                assert result.get("success") is True

        def control():
            try:
                asyncio.run(client())
            except BaseException as error:
                errors.append(error)
            finally:
                ready.set()
                target = page._devtools_target
                if target is not None:
                    target.wake_waiting_owner()

        thread = threading.Thread(target=control, daemon=True)
        thread.start()
        try:
            assert await asyncio.to_thread(ready.wait, 3)
            assert not errors
            # Existing CDP closeTarget acknowledges a queued close after
            # resuming the pause; it permits the current JS turn to finish.
            assert page.evaluate("debugger;42", timeout=2) == 42
            await asyncio.to_thread(thread.join, 4)
            assert not thread.is_alive() and not errors
            async with asyncio.timeout(1):
                while not page.closed:
                    await asyncio.sleep(0.001)
            assert paused.is_set() and page.closed
        finally:
            target = page._devtools_target
            if target is not None:
                target.wake_waiting_owner()
            handle.close()


@run_async
async def test_devtools_attaches_evaluates_on_owner_and_detaches():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        page.v8_context.expose(host_thread=lambda: threading.get_ident())
        handle = runtime._default_profile.start_devtools(page, options=DevToolsOptions(pause_on_start=False))
        try:
            async with connect(f"ws://{handle.host}:{handle.port}/") as ws:
                session = await attach(ws)
                result = await cdp_request(
                    ws,
                    3,
                    "Runtime.evaluate",
                    {"expression": "host_thread()", "returnByValue": True},
                    session,
                )
                assert result["result"]["value"] == threading.get_ident()
                await cdp_request(ws, 4, "Target.detachFromTarget", {"sessionId": session})
        finally:
            handle.close()


@pytest.mark.parametrize("evaluation_timeout,pause_delay", [(2, 0), (0.05, 0.12)])
@run_async
async def test_devtools_pause_and_resume_use_an_independent_control_thread(
    evaluation_timeout, pause_delay
):
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        handle = runtime._default_profile.start_devtools(page, options=DevToolsOptions(pause_on_start=False))
        ready, paused = threading.Event(), threading.Event()
        errors = []

        async def client():
            async with connect(f"ws://{handle.host}:{handle.port}/") as ws:
                session = await attach(ws)
                await cdp_request(ws, 3, "Debugger.enable", session=session)
                ready.set()
                while True:
                    message = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    if message.get("method") == "Debugger.paused":
                        paused.set()
                        break
                await asyncio.sleep(pause_delay)
                result = await cdp_request(
                    ws, 4, "Runtime.evaluate", {"expression": "6*7", "returnByValue": True}, session
                )
                assert result["result"]["value"] == 42
                await cdp_request(ws, 5, "Debugger.resume", session=session)
                await cdp_request(ws, 6, "Target.detachFromTarget", {"sessionId": session})

        def control():
            try:
                asyncio.run(client())
            except BaseException as error:
                errors.append(error)
            finally:
                ready.set()
                target = page._devtools_target
                if target is not None:
                    target.wake_waiting_owner()

        thread = threading.Thread(target=control, daemon=True)
        thread.start()
        try:
            assert await asyncio.to_thread(ready.wait, 3)
            assert not errors
            assert (
                page.evaluate("globalThis.answer=41;debugger;answer+1", timeout=evaluation_timeout)
                == 42
            )
            await asyncio.to_thread(thread.join, 4)
            assert not thread.is_alive() and not errors
            assert paused.is_set()
        finally:
            page._devtools_target.wake_waiting_owner()
            handle.close()


@run_async
async def test_runtime_close_failure_stops_admission_and_can_be_retried(monkeypatch):
    from pyv8c.platform.runtime import RuntimeState

    runtime = await Browser(real_network=False, stub_logging="off").__aenter__()
    page = runtime.new_page()
    context = page.v8_context
    original = page._close_owner

    def fail():
        raise RuntimeError("retry native cleanup")

    monkeypatch.setattr(page, "_close_owner", fail)
    try:
        with pytest.raises(RuntimeError, match="retry native cleanup"):
            await runtime.close()
        assert runtime._runtime.state is RuntimeState.CLOSE_FAILED
        with pytest.raises(HostStateError):
            runtime.new_page()
        with pytest.raises(HostStateError):
            page.evaluate("42")
    finally:
        monkeypatch.setattr(page, "_close_owner", original)
        await runtime.close()
    assert page.closed and context.live_handles == 0
    assert runtime._runtime.state is RuntimeState.CLOSED
    await page.profile.close()


@run_async
async def test_failed_group_initialization_closes_its_isolate_and_driver(monkeypatch):
    from pyv8c.platform import execution

    captured = []

    def fail(isolate, *args):
        captured.append(isolate)
        raise RuntimeError("foreground construction failed")

    original = execution.ForegroundSource
    async with Browser(real_network=False, stub_logging="off") as runtime:
        monkeypatch.setattr(execution, "ForegroundSource", fail)
        with pytest.raises(RuntimeError, match="foreground construction failed"):
            runtime.new_page()
        assert captured and captured[0].closed
        assert not runtime._runtime._executors and not runtime._default_profile.pages
        monkeypatch.setattr(execution, "ForegroundSource", original)
        assert runtime.new_page().evaluate("42") == 42


@run_async
async def test_profiles_and_frame_factories_use_runtime_pages_without_global_replacement():
    from pyv8c.foundation.executor import RealmExecutor
    from pyv8c.platform.page import Page


    async with Browser(real_network=False, stub_logging="off") as runtime:
        profile = runtime.new_profile()
        page = (await profile.new_page().set_content("<iframe srcdoc='<body>child</body>'></iframe>", wait_until="commit"))
        await page.wait_for("frames.length===1", timeout=1)
        child = next(iter(page._owned.frames))
        assert isinstance(page, Page) and isinstance(child, Page)
        assert child._runtime is runtime._runtime
        assert type(page) is Page and type(child) is Page



@run_async
async def test_cancelled_lifecycle_wait_removes_its_registration():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        # A pending load is represented by the real lifecycle controller.
        page.lifecycle.document_load_fired = False
        waiting = asyncio.create_task(page.wait_for_load_state("load", timeout=2))
        await asyncio.sleep(0)
        assert len(page.lifecycle._state_waiters) == 1
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.sleep(0)
        assert not page.lifecycle._state_waiters


@run_async
async def test_new_page_navigation_and_url_assignment_have_no_sync_fallback():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        with pytest.raises(TypeError):
            runtime.new_page(url="https://runtime.test/doc")
        page = runtime.new_page()
        with pytest.raises(HostStateError, match="await page.goto"):
            page.url = "https://runtime.test/doc"
        assert page.url == "about:blank"


@pytest.mark.parametrize("cleanup_fails", [False, True])
@run_async
async def test_failed_page_bootstrap_keeps_cleanup_owned_until_success(monkeypatch, cleanup_fails):
    from pyv8c.platform.navigation import NavigationController
    from pyv8c.platform.page import Page

    captured = []

    def fail(controller):
        captured.append((controller.page, controller.page.v8_context))
        raise RuntimeError("document bootstrap failed")

    original = Page._close_owner

    def fail_cleanup(page):
        raise RuntimeError("bootstrap cleanup failed")

    async with Browser(real_network=False, stub_logging="off") as runtime:
        monkeypatch.setattr(NavigationController, "initialize_empty_document", fail)
        if cleanup_fails:
            monkeypatch.setattr(Page, "_close_owner", fail_cleanup)
        with pytest.raises(
            RuntimeError,
            match="bootstrap cleanup failed" if cleanup_fails else "document bootstrap failed",
        ):
            runtime.new_page()
        page, context = captured[0]
        if cleanup_fails:
            assert runtime._default_profile.pages == [page]
            assert not page.closed
            monkeypatch.setattr(Page, "_close_owner", original)
        else:
            assert page.closed and not runtime._default_profile.pages and not runtime._runtime._executors
        isolate = page.execution_group.isolate
    assert page.closed and isolate.closed and context.live_handles == 0


@pytest.mark.parametrize("method", ["fetch", "xhr"])
@run_async
async def test_host_http_delivery_is_cooperative(method):
    async with Browser(real_network=False, resource_loader=loader, stub_logging="off") as runtime:
        page = (await runtime.new_page().set_content("<body></body>", "https://runtime.test/", wait_until="commit"))
        response = await getattr(page.net, method)("/fetch")
        assert response.status == 200 and response.body == "hello"
