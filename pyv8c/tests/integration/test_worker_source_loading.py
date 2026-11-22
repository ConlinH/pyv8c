"""Worker startup I/O must not occupy either realm's execution thread."""

import asyncio
import threading

import pytest

pytest.importorskip('pyv8c.pyv8')
from pyv8c.async_api import Browser
from pyv8c.web.workers.runtime import WorkerRuntime


@pytest.mark.parametrize('action', ['release', 'terminate', 'navigate', 'cancel-source', 'close', 'document-open'])
async def test_pending_source_leaves_worker_control_and_parent_available(monkeypatch, action):
    entered, release = threading.Event(), threading.Event()
    runtimes = []
    original_init = WorkerRuntime.__init__
    def capture(runtime, *args, **kwargs):
        original_init(runtime, *args, **kwargs)
        runtimes.append(runtime)
    monkeypatch.setattr(WorkerRuntime, '__init__', capture)

    def loader(url, options):
        assert options['destination'] == 'worker'
        entered.set()
        assert release.wait(5)
        return 'onmessage=e=>postMessage(e.data)'

    browser = (await Browser(real_network=False, stub_logging='off', resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://fixture.test/', wait_until="commit"))
        page.evaluate("globalThis.messages=[];globalThis.worker=new Worker('/slow.js');"
                      "worker.onmessage=e=>messages.push(e.data);worker.postMessage('early')")
        assert (await asyncio.to_thread(entered.wait, 2))
        runtime = runtimes[0]
        assert page.evaluate('6*7', timeout=1) == 42
        assert runtime.executor.call(lambda: 42, timeout=.5, _task_lane=None) == 42
        if action in ('release', 'document-open'):
            if action == 'document-open':
                page.evaluate("document.open();document.write('<body>new</body>');document.close()")
            release.set()
            (await page.wait_for("messages.length===1", timeout=3))
            assert page.evaluate('messages') == ['early']
            page.evaluate('worker.terminate()')
        elif action == 'terminate':
            page.evaluate('worker.terminate()')
        elif action == 'cancel-source':
            page.executor.call(page.resource_ownership.close)
        elif action == 'close':
            (await page.close())
        else:
            (await page.set_content('<body>replacement</body>', url='http://fixture.test/next', wait_until="commit"))
        (await asyncio.to_thread(runtime.executor._thread.join, 1))
        assert not runtime.executor._thread.is_alive()
        if action != 'close':
            assert (await page.run_until_idle(timeout=1))
        assert page.tasks.worker_pending == 0
        assert page.resource_ownership.active_count == 0
        release.set()
        if action != 'close':
            assert page.evaluate('21*2') == 42
    finally:
        release.set()
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 2))
            assert not runtime.executor._thread.is_alive()


@pytest.mark.parametrize('kind', ['blob', 'data'])
async def test_local_source_capture_does_not_run_interceptor_on_parent(monkeypatch, kind):
    entered, release = threading.Event(), threading.Event()
    observed = []
    def intercept(url, request):
        if request.get('destination') == 'worker':
            observed.append(page.executor.is_owner_thread())
            entered.set()
            assert release.wait(5)
    browser = (await Browser(real_network=False, stub_logging='off', resource_interceptor=intercept).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://fixture.test/', wait_until="commit"))
        expression = ("URL.createObjectURL(new Blob(['postMessage(42)']))" if kind == 'blob'
                      else "'data:text/javascript,postMessage(42)'")
        page.evaluate("globalThis.answer=0;const url=" + expression + ";"
                      "globalThis.worker=new Worker(url);worker.onmessage=e=>answer=e.data;"
                      "URL.revokeObjectURL(url)", timeout=1)
        assert (await asyncio.to_thread(entered.wait, 2))
        assert page.evaluate('20+22', timeout=1) == 42
        assert observed == [False]
        release.set()
        (await page.wait_for('answer===42', timeout=3))
        page.evaluate('worker.terminate()')
    finally:
        release.set()
        (await browser.close())


@pytest.mark.parametrize('failure', ['status', 'loader', 'interceptor', 'cross-origin'])
async def test_source_failure_reports_once_and_releases_executor(monkeypatch, failure):
    runtimes = []
    original_init = WorkerRuntime.__init__
    def capture(runtime, *args, **kwargs):
        original_init(runtime, *args, **kwargs)
        runtimes.append(runtime)
    monkeypatch.setattr(WorkerRuntime, '__init__', capture)
    def loader(url, request):
        if failure == 'loader':
            raise ValueError('fixture loader failure')
        return {'status': 404, 'body': "postMessage('must not run')"}
    def interceptor(url, request):
        if failure == 'interceptor' and request.get('destination') == 'worker':
            raise ValueError('fixture interceptor failure')
    browser = (await Browser(real_network=False, stub_logging='off', resource_loader=loader,
                      resource_interceptor=interceptor).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://fixture.test/', wait_until="commit"))
        url = 'http://other.test/source.js' if failure == 'cross-origin' else '/source.js'
        page.evaluate("globalThis.errors=[];globalThis.messages=[];"
                      f"globalThis.worker=new Worker('{url}');"
                      "worker.onerror=e=>{errors.push(e.isTrusted);e.preventDefault()};"
                      "worker.onmessage=e=>messages.push(e.data)")
        (await page.wait_for('errors.length===1', timeout=3))
        runtime = runtimes[0]
        (await asyncio.to_thread(runtime.executor._thread.join, 1))
        assert not runtime.executor._thread.is_alive()
        assert (await page.run_until_idle(timeout=1))
        assert page.evaluate('[errors,messages]') == [[True], []]
        assert page.tasks.worker_pending == 0
        assert page.resource_ownership.active_count == 0
    finally:
        (await browser.close())


async def test_configuration_failure_remains_a_host_error_and_cleans_up(monkeypatch):
    failure = RuntimeError('fixture configuration failure')
    reported, runtimes = [], []
    original_init, original_report = WorkerRuntime.__init__, WorkerRuntime._report_error
    def capture(runtime, *args, **kwargs):
        original_init(runtime, *args, **kwargs)
        runtimes.append(runtime)
    def report(runtime, error):
        reported.append(error)
        original_report(runtime, error)
    def configure():
        raise failure
    monkeypatch.setattr(WorkerRuntime, '__init__', capture)
    monkeypatch.setattr(WorkerRuntime, '_report_error', report)
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        monkeypatch.setattr(page.services.worker_services, 'worker_configuration', configure)
        page.evaluate("globalThis.errors=[];globalThis.worker=new Worker('data:text/javascript,0');"
                      "worker.onerror=e=>errors.push(e.message)")
        runtime = runtimes[0]
        (await asyncio.to_thread(runtime.executor._thread.join, 1))
        assert not runtime.executor._thread.is_alive()
        assert (await page.run_until_idle(timeout=1))
        assert reported == [failure]
        assert page.evaluate('errors') == []
        assert page.tasks.worker_pending == 0
        assert page.resource_ownership.active_count == 0
    finally:
        (await browser.close())
