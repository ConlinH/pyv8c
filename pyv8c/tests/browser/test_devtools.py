"""Real CDP coverage for the browser adapter and shared service."""

import json
import asyncio
import functools
import threading
from concurrent.futures import Future
from urllib.request import urlopen

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from pyv8c import DevToolsOptions
from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import HostStateError

#: How long a client waits for something the browser has to answer.  The answer is
#: produced in-process, but it can still queue behind a busy realm or a machine
#: running the suite twice over: replies come back inside 250 ms on an idle machine
#: and took over 4 s under load, which is what a 4 s budget used to fail on.  A test
#: that really hangs is caught by the per-test timeout, so this only has to be
#: generous.
REPLY_TIMEOUT = 30


class Client:
    def __init__(self, handle, path="/"):
        self.ws = connect(f"ws://{handle.host}:{handle.port}{path}", close_timeout=1)
        self.sequence = 0
        self.inbox = []

    def send(self, method, params=None, session=None, request_id=None):
        self.sequence += 1
        payload = {"id": self.sequence if request_id is None else request_id, "method": method}
        if params is not None:
            payload["params"] = params
        if session is not None:
            payload["sessionId"] = session
        self.ws.send(json.dumps(payload))
        return payload["id"]

    def matching(self, predicate):
        for index, message in enumerate(self.inbox):
            if predicate(message):
                return self.inbox.pop(index)
        while True:
            message = json.loads(self.ws.recv(timeout=REPLY_TIMEOUT))
            if predicate(message):
                return message
            self.inbox.append(message)

    def request(self, method, params=None, session=None):
        request_id = self.send(method, params, session)
        message = self.matching(lambda item: item.get("id") == request_id and item.get("sessionId") == session)
        assert "error" not in message, message
        return message["result"]

    def event(self, method, session=None):
        return self.matching(lambda item: item.get("method") == method and item.get("sessionId") == session)["params"]

    def ready(self, session=None):
        self.request("Runtime.enable", session=session)
        self.request("Debugger.enable", session=session)
        self.request("Runtime.runIfWaitingForDebugger", session=session)

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.ws.close()


def background(callback):
    result = Future()

    def run():
        try:
            result.set_result(callback())
        except BaseException as error:
            result.set_exception(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return result


class OwnerActions:
    """Explicit application-loop actions for an independent CDP control thread."""
    def __init__(self, loop):
        self.loop = loop

    def call(self, callback, *args, **kwargs):
        result = Future()
        def invoke():
            try:
                value = callback(*args, **kwargs)
                if asyncio.iscoroutine(value):
                    task = asyncio.create_task(value)
                    def completed(done):
                        if done.cancelled():
                            result.cancel()
                        elif done.exception() is not None:
                            result.set_exception(done.exception())
                        else:
                            result.set_result(done.result())
                    task.add_done_callback(completed)
                else:
                    result.set_result(value)
            except BaseException as error:
                result.set_exception(error)
        self.loop.call_soon_threadsafe(invoke)
        return result

    def wait(self, callback, *args, **kwargs):
        return self.call(callback, *args, **kwargs).result(REPLY_TIMEOUT)

    def enter(self, **options):
        return asyncio.run_coroutine_threadsafe(Browser(**options).__aenter__(), self.loop).result(REPLY_TIMEOUT)

    def wait_async(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(REPLY_TIMEOUT)

    def close(self, runtime):
        return asyncio.run_coroutine_threadsafe(runtime.close(), self.loop).result(REPLY_TIMEOUT)


@pytest.fixture
async def owner():
    return OwnerActions(asyncio.get_running_loop())


def in_control_thread(function):
    @functools.wraps(function)
    async def run(*args, **kwargs):
        def control():
            try:
                return function(*args, **kwargs)
            finally:
                # A failed assertion must release a paused application owner.
                for value in (*args, *kwargs.values()):
                    if isinstance(value, tuple) and len(value) == 3 and hasattr(value[2], "_router"):
                        value[2].close()
        return await asyncio.to_thread(control)
    return run


@pytest.fixture
async def devtools_page():
    browser = await Browser(stub_logging="off", resource_loader=lambda url, request: {"url": url, "body": "<script>globalThis.childStarted=42</script>"}).__aenter__()
    context = browser.new_profile()
    page = context.new_page()
    page.evaluate("globalThis.answer = 40")
    handle = context.start_devtools(page)
    try:
        yield context, page, handle
    finally:
        handle.close()
        await browser.close()

@in_control_thread
def test_discovery_and_nonblocking_start(devtools_page, owner):
    _, page, handle = devtools_page
    assert owner.wait(page.evaluate, "answer") == 40
    base = f"http://{handle.host}:{handle.port}"
    with urlopen(base + "/json/list") as response:
        targets = json.load(response)
    with urlopen(base + "/json") as response:
        assert json.load(response) == targets
    with urlopen(base + "/json/version") as response:
        version = json.load(response)
    assert version["Protocol-Version"] == "1.3"
    assert targets[0]["devtoolsFrontendUrl"] == handle.url
    assert version["webSocketDebuggerUrl"].startswith(f"ws://{handle.host}:{handle.port}/devtools/browser/")
    with Client(handle, "/devtools/page/" + targets[0]["id"]) as client:
        assert handle.wait_for_connection(1)
        assert client.request("Runtime.evaluate", {"expression": "answer + 2"})["result"]["value"] == 42


@in_control_thread
def test_only_one_service_and_client(devtools_page, owner):
    context, page, handle = devtools_page
    with pytest.raises(HostStateError):
        owner.wait(context.start_devtools, page)
    with Client(handle) as first:
        first.request("Target.getTargets")
        with Client(handle) as second:
            with pytest.raises(ConnectionClosed) as closed:
                second.ws.recv(timeout=REPLY_TIMEOUT)
            assert closed.value.rcvd.code == 1013


@in_control_thread
def test_first_statement_console_isolation_step_and_once(devtools_page, owner):
    _, page, handle = devtools_page
    with Client(handle) as client:
        client.ready()
        assert handle.wait_for_ready(1)
        client.request("Runtime.evaluate", {"expression": "globalThis.consoleValue = 9"})
        result = owner.call(lambda: page.evaluate("globalThis.debugValue = 42;\nglobalThis.debugValue += 1", filename="example.js"))
        paused = client.event("Debugger.paused")
        assert paused["callFrames"][0]["location"]["lineNumber"] == 0
        assert not result.done()
        client.request("Debugger.stepOver")
        assert client.event("Debugger.paused")["callFrames"][0]["location"]["lineNumber"] == 1
        client.request("Debugger.resume")
        assert result.result(3) == 43
        assert owner.wait(page.evaluate, "debugValue + consoleValue") == 52


@in_control_thread
def test_url_breakpoint_and_independent_session_ids(devtools_page, owner):
    _, page, handle = devtools_page
    with Client(handle) as client:
        target = client.request("Target.getTargets")["targetInfos"][0]["targetId"]
        one = client.request("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        two = client.request("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        client.request("Debugger.enable", session=one)
        client.request("Debugger.enable", session=two)
        client.request("Debugger.disable", session=two)
        client.request("Debugger.setBreakpointByUrl", {"url": "breakpoint.js", "lineNumber": 0}, one)
        for session, value in [(one, 11), (two, 22)]:
            client.send("Runtime.evaluate", {"expression": str(value)}, session, request_id=100)
        for session, value in [(one, 11), (two, 22)]:
            response = client.matching(lambda item: item.get("id") == 100 and item.get("sessionId") == session)
            assert response["result"]["result"]["value"] == value
        result = owner.call(lambda: page.evaluate("40 + 2", filename="breakpoint.js"))
        assert client.event("Debugger.paused", one)["hitBreakpoints"]
        client.request("Debugger.resume", session=one)
        assert result.result(3) == 42
        client.request("Target.detachFromTarget", {"sessionId": one})
        assert client.event("Target.detachedFromTarget")["sessionId"] == one


@pytest.mark.parametrize("phase", ["ready", "paused", "request"])
@in_control_thread
def test_disconnect_reconnect_preserves_host_and_target(devtools_page, phase, owner):
    _, page, handle = devtools_page
    with Client(handle) as client:
        target_id = client.request("Target.getTargets")["targetInfos"][0]["targetId"]
        client.ready()
        if phase == "paused":
            result = owner.call(lambda: page.evaluate("globalThis.disconnected = 1", filename="disconnect.js"))
            client.event("Debugger.paused")
        elif phase == "request":
            client.send("Runtime.evaluate", {"expression": "debugger; 7"}, request_id=900)
            client.event("Debugger.paused")
    if phase == "paused":
        result.result(3)
    with Client(handle) as client:
        assert client.request("Target.getTargets")["targetInfos"][0]["targetId"] == target_id
        client.ready()
        if phase != "paused":
            result = owner.call(lambda: page.evaluate("1"))
            client.event("Debugger.paused")
            client.request("Debugger.resume")
            result.result(3)
        assert owner.wait(page.evaluate, "answer + 2") == 42
        assert not any(message.get("id") == 900 for message in client.inbox)


@pytest.mark.parametrize("disconnect", [False, True])
@in_control_thread
def test_explicit_startup_wait(disconnect, owner):
    browser = owner.enter(stub_logging="off")
    context = owner.wait(browser.new_profile)
    page = owner.wait(context.new_page)
    handle = owner.wait(context.start_devtools, page, options=DevToolsOptions(wait_for_debugger=True, pause_on_start=False, startup_timeout=3))
    try:
        result = owner.call(lambda: page.evaluate("globalThis.started = 42"))
        with Client(handle) as client:
            client.request("Runtime.enable")
            assert not result.done()
            if not disconnect:
                client.request("Runtime.runIfWaitingForDebugger")
        assert result.result(3) == 42
        assert owner.wait(page.evaluate, "started") == 42
    finally:
        handle.close()
        owner.close(browser)


@in_control_thread
def test_timeout_does_not_execute_or_close_page(owner):
    browser = owner.enter(stub_logging="off")
    try:
        context = owner.wait(browser.new_profile)
        page = owner.wait(context.new_page)
        handle = owner.wait(context.start_devtools, page, options=DevToolsOptions(wait_for_debugger=True, startup_timeout=0))
        with pytest.raises(TimeoutError, match="startup timed out"):
            owner.wait(page.evaluate, "globalThis.shouldNotRun = 1")
        assert not page.closed
        handle.close()
        handle.close()
        assert owner.wait(page.evaluate, "typeof shouldNotRun") == "undefined"
    finally:
        owner.close(browser)


@in_control_thread
def test_page_close_removes_target(devtools_page, owner):
    _, page, handle = devtools_page
    with Client(handle) as client:
        client.request("Runtime.enable")
        owner.wait(page.close)
        assert client.event("Target.targetDestroyed")["targetId"]
        assert not handle._router.diagnostics


@in_control_thread
def test_close_waiters_and_port_are_released(devtools_page, owner):
    import socket

    _, _, handle = devtools_page
    ready = background(lambda: handle.wait_for_ready())
    connection = background(lambda: handle.wait_for_connection())
    handle.close()
    assert ready.result(2) is False
    assert connection.result(2) is False
    with socket.socket() as listener:
        listener.bind((handle.host, handle.port))
    assert not handle._router.transport.thread.is_alive()


@in_control_thread
def test_page_compile_failure_does_not_arm_helpers_or_console(devtools_page, owner):
    _, page, handle = devtools_page
    with Client(handle) as client:
        client.ready()
        with pytest.raises(BaseException):
            owner.wait(page.evaluate, "const =", filename="bad.js")
        assert page.realm.executor.call(lambda: page.v8_context.exec_js("21+21")) == 42
        assert client.request("Runtime.evaluate", {"expression": "1+1"})["result"]["value"] == 2
        result = owner.call(lambda: page.evaluate("42", filename="retry.js"))
        client.event("Debugger.paused")
        client.request("Debugger.resume")
        assert result.result(2) == 42


@in_control_thread
def test_rapid_reconnect_does_not_replay_requests(devtools_page, owner):
    _, _, handle = devtools_page
    target_id = None
    for _ in range(10):
        with Client(handle) as client:
            current = client.request("Target.getTargets")["targetInfos"][0]["targetId"]
            if target_id is not None:
                assert current == target_id
            target_id = current
            client.send("Runtime.evaluate", {"expression": "42"}, request_id=700)
    with Client(handle) as client:
        assert client.request("Runtime.evaluate", {"expression": "21"})["result"]["value"] == 21
        assert not any(message.get("id") == 700 for message in client.inbox)


@in_control_thread
def test_existing_iframe_autoattach_and_commands_during_shared_isolate_pause(owner):
    browser = owner.enter(stub_logging="off", resource_loader=lambda url, request: {"url": url, "body": "<script>globalThis.childValue=7</script>"})
    context = owner.wait(browser.new_profile)
    page = owner.wait(context.new_page)
    owner.wait(page.set_content, '<iframe src="http://example.test/child"></iframe>')
    owner.wait_async(page.run_until_idle(timeout=1))
    handle = owner.wait(context.start_devtools, page)
    try:
        with Client(handle) as client:
            client.request("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
            child = client.event("Target.attachedToTarget")
            assert child["targetInfo"]["type"] == "iframe"
            assert not child["waitingForDebugger"]
            sid = child["sessionId"]
            client.ready()
            client.request("Runtime.enable", session=sid)
            result = owner.call(lambda: page.evaluate("1", filename="parent.js"))
            client.event("Debugger.paused")
            assert client.request("Runtime.evaluate", {"expression": "childValue"}, sid)["result"]["value"] == 7
            client.request("Debugger.resume")
            result.result(3)
            client.request("Target.setAutoAttach", {"autoAttach": False, "flatten": True})
            assert client.event("Target.detachedFromTarget")["sessionId"] == sid
    finally:
        handle.close()
        owner.close(browser)


@pytest.mark.parametrize("kind", ["iframe", "worker"])
@in_control_thread
def test_new_children_wait_before_script_and_parent_close(devtools_page, kind, owner):
    _, page, handle = devtools_page
    with Client(handle) as client:
        client.request("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        if kind == "iframe":
            result = owner.call(lambda: page.set_content('<iframe src="http://example.test/child"></iframe>'))
        else:
            result = owner.call(lambda: page.evaluate("globalThis.w = new Worker(URL.createObjectURL(new Blob(['globalThis.childStarted=42'], {type:'text/javascript'})))"))
        child = client.event("Target.attachedToTarget")
        assert child["targetInfo"]["type"] == kind
        assert child["waitingForDebugger"]
        sid = child["sessionId"]
        value = client.request("Runtime.evaluate", {"expression": "typeof childStarted"}, sid)
        assert value["result"]["value"] == "undefined"
        client.ready(sid)
        assert client.event("Debugger.paused", sid)["callFrames"][0]["location"]["lineNumber"] == 0
        client.request("Debugger.resume", session=sid)
        result.result(3)
        assert client.request("Runtime.evaluate", {"expression": "childStarted"}, sid)["result"]["value"] == 42
        owner.wait(page.close)
        assert client.event("Target.detachedFromTarget")["sessionId"] == sid


@pytest.mark.parametrize("pause", [False, True])
@in_control_thread
def test_parent_close_releases_child_startup_and_pause(devtools_page, pause, owner):
    _, page, handle = devtools_page
    with Client(handle) as client:
        client.request("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        navigation = owner.call(lambda: page.set_content('<iframe src="http://example.test/child"></iframe>'))
        child = client.event("Target.attachedToTarget")
        if pause:
            client.ready(child["sessionId"])
            client.event("Debugger.paused", child["sessionId"])
        root_target = next(t["targetId"] for t in client.request("Target.getTargets")["targetInfos"] if t["type"] == "page")
        assert client.request("Target.closeTarget", {"targetId": root_target})["success"]
        with pytest.raises(HostStateError, match="closed"):
            navigation.result(4)
        client.matching(lambda event: event.get("method") == "Target.targetDestroyed" and event.get("params", {}).get("targetId") == root_target)
        assert owner.wait(lambda: page.closed)


@in_control_thread
def test_service_close_while_paused_keeps_host(devtools_page, owner):
    _, page, handle = devtools_page
    with Client(handle) as client:
        client.ready()
        result = owner.call(lambda: page.evaluate("42", filename="close.js"))
        client.event("Debugger.paused")
        handle.close()
        assert result.result(3) == 42
        assert not page.closed
        assert owner.wait(page.evaluate, "21+21") == 42


@in_control_thread
def test_parent_unregister_follows_descendant_close(devtools_page, monkeypatch, owner):
    _, page, handle = devtools_page
    owner.wait(page.set_content, '<iframe></iframe>')
    child = next(iter(page._owned.frames))
    adapter = page.profile._devtools
    unregister = adapter.unregister
    order = []

    def checked(realm):
        if realm is page:
            assert child.closed, "parent mailbox must remain live until descendants close"
        order.append(realm)
        return unregister(realm)

    monkeypatch.setattr(adapter, "unregister", checked)
    owner.wait(page.close)
    assert order == [child, page]
    assert not handle._router.cleanup
