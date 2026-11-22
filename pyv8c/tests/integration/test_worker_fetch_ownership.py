"""Worker fetch crosses owners as data and belongs to the worker lifetime."""

import asyncio
import threading
import json
from contextlib import asynccontextmanager
from urllib.parse import quote

import pytest

pytest.importorskip('pyv8c.pyv8')
from _http_fixtures import GatedTransport
from pyv8c.async_api import Browser
from pyv8c.network.models import NetworkResponse
from pyv8c.platform.resources import session
from pyv8c.web.workers.runtime import WorkerRuntime


class _AbortHookEngine(GatedTransport):
    def __init__(self, respond, *, on_abort=None):
        self.sent = []
        self.aborted = []

        def send(request):
            self.sent.append(request.url)
            return respond(request)

        def interrupt(request):
            self.aborted.append(request.url)
            if on_abort is not None:
                on_abort()

        super().__init__(send, interrupt=interrupt)


async def _start_worker_fetch(browser):
    page = browser.new_page()
    await page.set_content("<p>local</p>", "http://fixture.test/")
    page.evaluate("""globalThis.worker = new Worker(URL.createObjectURL(new Blob([
        "fetch(location.origin + '/first').catch(() => {})"
    ], {type:'text/javascript'})));""")
    return page


async def test_worker_close_reaches_custom_transport_cancellation():
    entered, released = threading.Event(), threading.Event()

    def respond(request):
        entered.set()
        assert released.wait(3)
        return NetworkResponse(request.url)

    engine = _AbortHookEngine(respond, on_abort=released.set)
    async with Browser(http_engine=engine, stub_logging="off") as browser:
        await _start_worker_fetch(browser)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(browser.close(), 2)
            assert engine.sent == engine.aborted == ["http://fixture.test/first"]
        finally:
            released.set()


async def test_terminating_between_redirect_hops_does_not_abort_the_completed_hop(monkeypatch):
    deciding, resume, stopped = (threading.Event() for _ in range(3))
    original_next_hop = session.next_hop
    original_teardown = WorkerRuntime._teardown_owner

    def next_hop(**kwargs):
        deciding.set()
        assert resume.wait(5)
        return original_next_hop(**kwargs)

    def teardown(runtime):
        original_teardown(runtime)
        stopped.set()

    monkeypatch.setattr(session, "next_hop", next_hop)
    monkeypatch.setattr(WorkerRuntime, "_teardown_owner", teardown)
    engine = _AbortHookEngine(lambda request: NetworkResponse(
        request.url, status=302, headers={"location": "/second"}))
    async with Browser(http_engine=engine, stub_logging="off") as browser:
        page = await _start_worker_fetch(browser)
        try:
            assert await asyncio.to_thread(deciding.wait, 2)
            page.evaluate("worker.terminate()")
            assert await asyncio.to_thread(stopped.wait, 2)
        finally:
            resume.set()
        await browser.close()
        assert engine.aborted == []
        assert engine.sent == ["http://fixture.test/first"]


@asynccontextmanager
async def running_worker(monkeypatch, source, loader, *, data=False):
    ready, runtimes = threading.Event(), []
    def started(runtime):
        runtimes.append(runtime)
        ready.set()
    monkeypatch.setattr(WorkerRuntime, '_started', started)
    def resources(url, request):
        return source if url.endswith('/worker.js') else loader(url, request)
    browser = (await Browser(real_network=False, stub_logging='off', resource_loader=resources).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://fixture.test/', wait_until="commit"))
        url = 'data:text/javascript,' + quote(source, safe='') if data else '/worker.js'
        page.evaluate("globalThis.messages=[];globalThis.worker=new Worker("+json.dumps(url)+");"
                      "worker.onmessage=e=>messages.push(e.data)")
        assert (await asyncio.to_thread(ready.wait, 3))
        yield page, runtimes[0]
    finally:
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 2))
            assert not runtime.executor._thread.is_alive()


async def test_worker_fetch_preparation_is_independent_of_parent_owner(monkeypatch):
    browser = (await Browser(real_network=False, stub_logging='off', resource_loader=lambda url, request:
        "fetch('/value').then(r=>r.text()).then(v=>postMessage(v))" if url.endswith('/worker.js') else 'value').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://fixture.test/', wait_until="commit"))
        prepared = []
        original = page.resource_fetcher.prepare
        def prepare(*args, **kwargs):
            prepared.append(page.executor.is_owner_thread())
            return original(*args, **kwargs)
        monkeypatch.setattr(page.resource_fetcher, 'prepare', prepare)
        page.evaluate("globalThis.answer=null;globalThis.worker=new Worker('/worker.js');"
                      "worker.onmessage=e=>answer=e.data")
        (await page.wait_for("answer==='value'", timeout=3))
        assert prepared == [True]  # Only entry loading belongs to the creator owner.
    finally:
        (await browser.close())


async def test_pending_fetch_does_not_block_worker_timer_or_termination(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runtimes = []
    original = WorkerRuntime.__init__
    def capture(runtime, *args):
        original(runtime, *args)
        runtimes.append(runtime)
    monkeypatch.setattr(WorkerRuntime, '__init__', capture)
    def loader(url, request):
        if url.endswith('/worker.js'):
            return "setTimeout(()=>postMessage('pulse'),0);fetch('/slow').then(r=>r.text()).then(postMessage)"
        entered.set()
        assert release.wait(5)
        return 'late'
    browser = (await Browser(real_network=False, stub_logging='off', resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://fixture.test/', wait_until="commit"))
        page.evaluate("globalThis.messages=[];globalThis.worker=new Worker('/worker.js');"
                      "worker.onmessage=e=>messages.push(e.data)")
        assert (await asyncio.to_thread(entered.wait, 2))
        (await page.wait_for("messages.includes('pulse')", timeout=1))
        runtime = runtimes[0]
        page.evaluate('worker.terminate()')
        (await asyncio.to_thread(runtime.executor._thread.join, 1))
        assert not runtime.executor._thread.is_alive()
        assert page.resource_ownership.active_count == 0
        assert page.tasks.worker_pending == 0
        release.set()
        assert page.evaluate('messages') == ['pulse']
    finally:
        release.set()
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 2))
            assert not runtime.executor._thread.is_alive()


@pytest.mark.parametrize('action', ['abort', 'terminate', 'navigate', 'close', 'document-open'])
async def test_pending_request_follows_worker_lifetime(monkeypatch, action):
    entered, release = threading.Event(), threading.Event()
    source = ("const control=new AbortController();const reason={tag:42};"
              "onmessage=e=>{if(e.data==='abort'){control.abort(reason);return}"
              "fetch('/slow',{signal:control.signal}).then(r=>r.text())"
              ".then(v=>postMessage(['ok',v])).catch(e=>postMessage(['abort',e===reason]));"
              "setTimeout(()=>postMessage('pulse'),0)}")
    def loader(url, options):
        entered.set()
        assert release.wait(5)
        return 'late'
    try:
        async with running_worker(monkeypatch, source, loader) as (page, runtime):
            ownership = runtime._realm.resource_ownership
            page.evaluate("worker.postMessage('start')")
            assert (await asyncio.to_thread(entered.wait, 2))
            (await page.wait_for("messages.includes('pulse')", timeout=2))
            if action == 'abort':
                page.evaluate("worker.postMessage('abort')")
                (await page.wait_for('messages.length===2', timeout=2))
                assert page.evaluate('messages') == ['pulse', ['abort', True]]
            elif action == 'document-open':
                page.evaluate("document.open();document.write('<body>new</body>');document.close()")
                release.set()
                (await page.wait_for('messages.length===2', timeout=2))
                assert page.evaluate('messages') == ['pulse', ['ok', 'late']]
            else:
                if action == 'terminate':
                    page.evaluate('worker.terminate()')
                elif action == 'close':
                    (await page.close())
                else:
                    (await page.set_content('<body>next</body>', url='http://fixture.test/next', wait_until="commit"))
                (await asyncio.to_thread(runtime.executor._thread.join, 1))
                assert not runtime.executor._thread.is_alive()
                assert page.tasks.worker_pending == 0
            assert ownership.active_count == 0
            assert page.resource_ownership.active_count == 0
            release.set()
    finally:
        release.set()


@pytest.mark.parametrize('body', ["'AB'", "new Uint8Array([65,66])", "new Uint8Array([65,66]).buffer",
                                  "new Blob(['AB'])", "({toString(){return 'AB'}})",
                                  "new DataView(new Uint8Array([99,65,66,88]).buffer,1,2)",
                                  "new DataView(new ArrayBuffer(4),4,0)"])
async def test_request_is_snapshotted_and_response_is_rebuilt_in_worker(monkeypatch, body):
    seen = []
    def loader(url, request):
        seen.append(request)
        return {'body':'result', 'headers':{'x-answer':'yes'}}
    source = ("onmessage=()=>{const headers=new Headers({'X-Test':'before'});const body="+body+";"
              "fetch('/value',{method:'POST',headers,body}).then(async r=>postMessage(["
              "r instanceof Response,r.headers instanceof Headers,r.constructor===Response,"
              "await r.text(),r.headers.get('x-answer')]));headers.set('X-Test','after');"
              "if(body instanceof Uint8Array)body[0]=90}")
    async with running_worker(monkeypatch, source, loader) as (page, runtime):
        from pyv8c.foundation.tasks import TaskSource
        adapter = runtime._realm.services.resource_services
        original = adapter._deliver
        delivered = []
        def deliver(*args):
            assert runtime.executor.is_owner_thread()
            delivered.append(runtime._agent_tasks._current.source)
            original(*args)
        monkeypatch.setattr(adapter, '_deliver', deliver)
        page.evaluate("worker.postMessage('start')")
        (await page.wait_for('messages.length===1', timeout=3))
        assert page.evaluate('messages') == [[True,True,True,'result','yes']]
        assert delivered == [TaskSource.NETWORKING]
        assert len(seen) == 1
        assert seen[0]['headers']['x-test'] == 'before'
        value = seen[0]['body']
        assert isinstance(value, (str,bytes))
        expected = b'' if body == 'new DataView(new ArrayBuffer(4),4,0)' else b'AB'
        assert (value.encode() if isinstance(value,str) else value) == expected
        assert runtime._realm.resource_ownership.active_count == 0


@pytest.mark.parametrize('allowed', ['*','null','http://fixture.test'])
async def test_data_worker_uses_opaque_origin_for_cors(monkeypatch, allowed):
    seen = []
    def loader(url, request):
        seen.append(request)
        return {'body':'ok','headers':{'access-control-allow-origin':allowed}}
    source = ("onmessage=()=>fetch('http://fixture.test/value').then(r=>r.text())"
              ".then(v=>postMessage(['ok',v])).catch(e=>postMessage(['error',e.name,e.message]))")
    async with running_worker(monkeypatch, source, loader, data=True) as (page, runtime):
        page.evaluate("worker.postMessage('start')")
        (await page.wait_for('messages.length===1', timeout=3))
        expected = ['error','TypeError','Failed to fetch'] if allowed == 'http://fixture.test' else ['ok','ok']
        assert page.evaluate('messages') == [expected]
        assert len(seen) == 1
        assert seen[0]['headers']['Origin'] == 'null'


async def test_preaborted_fetch_never_dispatches_and_rejects_before_following_microtask(monkeypatch):
    calls = []
    def loader(url, request):
        calls.append(url)
        return 'must not load'
    source = ("onmessage=()=>{const c=new AbortController(),reason={tag:42},order=[];"
              "c.abort(reason);fetch('/value',{signal:c.signal})"
              ".catch(e=>order.push(e===reason?'rejected':'wrong'));"
              "queueMicrotask(()=>order.push('micro'));setTimeout(()=>postMessage(order),0)}")
    async with running_worker(monkeypatch, source, loader) as (page, runtime):
        page.evaluate("worker.postMessage('start')")
        (await page.wait_for('messages.length===1', timeout=3))
        assert page.evaluate('messages') == [['rejected','micro']]
        assert not calls
        assert runtime._realm.resource_ownership.active_count == 0


async def test_window_preaborted_fetch_keeps_the_same_microtask_rule():
    calls = []
    browser = (await Browser(real_network=False, stub_logging='off',
                      resource_loader=lambda *args: calls.append(args) or 'unused').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>',url='http://fixture.test/', wait_until="commit"))
        page.evaluate("globalThis.order=[];const c=new AbortController(),reason={tag:42};"
                      "c.abort(reason);fetch('/value',{signal:c.signal})"
                      ".catch(e=>order.push(e===reason?'rejected':'wrong'));"
                      "queueMicrotask(()=>order.push('micro'))")
        (await page.wait_for('order.length===2', timeout=2))
        assert page.evaluate('order') == ['rejected','micro']
        assert not calls
    finally:
        (await browser.close())


@pytest.mark.parametrize('stage', ['submit', 'source-cancel', 'response-reject', 'loader-error'])
async def test_failed_handoff_releases_both_owners(monkeypatch, stage):
    source = ("onmessage=()=>fetch('/value').then(r=>r.text())"
              ".then(v=>postMessage(['ok',v])).catch(e=>postMessage(['error',e.name]))")
    entered, release = threading.Event(), threading.Event()
    def loader(url, request):
        if stage == 'source-cancel':
            entered.set()
            assert release.wait(5)
        if stage == 'loader-error':
            raise RuntimeError('fixture loader failure')
        return 'value'
    try:
        async with running_worker(monkeypatch, source, loader) as (page, runtime):
            ownership = runtime._realm.resource_ownership
            rejected = threading.Event()
            if stage == 'submit':
                def submit(*args, **kwargs):
                    raise RuntimeError('fixture submission failure')
                monkeypatch.setattr(runtime._realm.config.browser.loader_pool, 'submit', submit)
            elif stage == 'response-reject':
                original = runtime._realm.tasks.post_task
                def post(callback, *args, **kwargs):
                    if kwargs.get('label') == 'Worker fetch response':
                        rejected.set()
                        return None
                    return original(callback, *args, **kwargs)
                monkeypatch.setattr(runtime._realm.tasks, 'post_task', post)
            page.evaluate("worker.postMessage('start')")
            if stage == 'source-cancel':
                assert (await asyncio.to_thread(entered.wait, 2))
                runtime.executor.call(ownership.close)
                runtime.executor.call(lambda: None, _task_lane=None)
            elif stage == 'response-reject':
                assert (await asyncio.to_thread(rejected.wait, 2))
                runtime.executor.call(lambda: None, _task_lane=None)
            else:
                (await page.wait_for('messages.length===1',timeout=2))
                assert page.evaluate('messages') == [['error','TypeError']]
            assert ownership.active_count == 0
            assert page.resource_ownership.active_count == 0
            release.set()
    finally:
        release.set()


async def test_worker_callbacks_are_released_on_worker_before_context_close(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    observed, calls = [], []
    try:
        def loader(*args):
            calls.append(args)
            entered.set()
            assert release.wait(5)
            return 'unused'
        async with running_worker(monkeypatch, '0', loader) as (page, runtime):
            class Callback:
                def __init__(self):
                    self.native = runtime._context.exec_js('(value)=>value')
                def __call__(self, response, error):
                    pytest.fail('cancelled callback was delivered')
                def __del__(self):
                    observed.append((runtime.executor.is_owner_thread(),
                                     runtime._realm.v8_context is not None))
            def issue():
                return runtime._realm.services.resource_services.load_resource_async(
                    'http://fixture.test/value', {}, Callback())
            handle = runtime.executor.call(issue)
            assert (await asyncio.to_thread(entered.wait, 2))
            ownership = runtime._realm.resource_ownership
            runtime.stop()
            (await asyncio.to_thread(runtime.executor._thread.join, 1))
            assert not runtime.executor._thread.is_alive()
            assert handle.cancelled
            assert ownership.active_count == 0
            assert observed == [(True, True)]
            release.set()
            assert (await page.run_until_idle(timeout=2))
            assert len(calls) == 1
    finally:
        release.set()
