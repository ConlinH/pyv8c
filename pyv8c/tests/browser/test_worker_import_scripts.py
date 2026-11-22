"""Worker dependencies use the entry URL, and failed loads are script-visible errors.

Synchronous execution, coercion and thrown script errors belong to corpus
worker_import_scripts_execute_synchronously_and_stop_on_exception.
"""

import asyncio
import json
import time

import pytest

from pyv8c.async_api import Browser


@pytest.mark.parametrize('kind', ['classic', 'module'])
async def test_import_scripts_resolves_against_entry_and_attributes_the_request(kind):
    seen = []
    sources = {
        'https://worker.test/dir/entry.js': '''
            try { importScripts('sub/first.js'); postMessage(self.answer); }
            catch (e) { postMessage(e.name); }
        ''',
        'https://worker.test/dir/sub/first.js': "importScripts('second.js');",
        'https://worker.test/dir/second.js': "self.answer = 'imported';",
    }

    def load(url, request):
        seen.append((url, request))
        return {'body': sources[url], 'headers': {'content-type': 'text/javascript'}}

    browser = (await Browser(real_network=False, resource_loader=load).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://worker.test/', wait_until="commit"))
        page.evaluate('''const worker = new Worker('/dir/entry.js', {type: %s});
            worker.onmessage = event => window.result = event.data;''' % json.dumps(kind))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = page.evaluate('window.result')
            if isinstance(result, str):
                break
            (await asyncio.sleep(.01))
        assert result == ('imported' if kind == 'classic' else 'TypeError')
        if kind == 'classic':
            assert [url for url, _ in seen] == list(sources)
            for _, request in seen[1:]:
                assert request['initiator_url'] == 'https://worker.test/dir/entry.js'
                assert request['destination'] == 'script'
        else:
            assert len(seen) == 1
    finally:
        (await browser.close())


async def test_import_scripts_reports_a_failed_http_status_without_executing_its_body():
    def load(url, request):
        return {'status': 404, 'body': "self.ran = true;",
                'headers': {'content-type': 'text/javascript'}}

    browser = (await Browser(real_network=False, resource_loader=load).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://worker.test/', wait_until="commit"))
        source = '''try { importScripts('https://worker.test/missing.js'); }
            catch(e) { postMessage([e.name, !!self.ran]); }'''
        page.evaluate('''const worker = new Worker(URL.createObjectURL(new Blob([%s],
            {type:'text/javascript'}))); worker.onmessage = e => window.result = e.data;
        ''' % json.dumps(source))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = page.evaluate('window.result')
            if isinstance(result, list):
                break
            (await asyncio.sleep(.01))
        assert result == ['NetworkError', False]
    finally:
        (await browser.close())


async def test_import_scripts_uses_final_entry_url_after_redirect():
    def loader(url, request):
        if url.endswith('/start.js'):
            return {'status': 302, 'headers': {'location': '/dir/entry.js'}}
        if url.endswith('/dir/entry.js'):
            return "try{importScripts('dependency.js');postMessage(answer)}catch(e){postMessage(e.name)}"
        if url.endswith('/dir/dependency.js'):
            return 'self.answer=42'
        return {'status': 404, 'body': ''}
    browser = (await Browser(real_network=False, resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://worker.test/', wait_until="commit"))
        page.evaluate("globalThis.result=null;globalThis.worker=new Worker('/start.js');worker.onmessage=e=>result=e.data")
        (await page.wait_for('result!==null', timeout=3))
        assert page.evaluate('result') == 42
    finally:
        (await browser.close())


async def test_imported_script_stack_uses_final_response_url():
    def loader(url, request):
        if url.endswith('/entry.js'):
            return "try{importScripts('/redirect.js')}catch(e){postMessage(e.stack.includes('/dir/dependency.js'))}"
        if url.endswith('/redirect.js'):
            return {'status': 302, 'headers': {'location': '/dir/dependency.js'}}
        return "throw new Error('dependency failure')"
    browser = (await Browser(real_network=False, resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://worker.test/', wait_until="commit"))
        page.evaluate("globalThis.result=null;globalThis.worker=new Worker('/entry.js');worker.onmessage=e=>result=e.data")
        (await page.wait_for('result!==null', timeout=3))
        assert page.evaluate('result') is True
    finally:
        (await browser.close())


async def test_import_scripts_does_not_wait_for_busy_creator_owner(monkeypatch):
    import threading
    from pyv8c.web.workers.runtime import WorkerRuntime
    started, blocked, release = threading.Event(), threading.Event(), threading.Event()
    runtimes = []
    def ready(runtime):
        runtimes.append(runtime)
        started.set()
    monkeypatch.setattr(WorkerRuntime, '_started', ready)
    def loader(url, request):
        return 'self.imported=42;' if url.endswith('dependency.js') else '0'
    browser = (await Browser(real_network=False, resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>',url='https://worker.test/', wait_until="commit"))
        page.evaluate("globalThis.worker=new Worker('/entry.js')")
        assert (await asyncio.to_thread(started.wait, 2))
        def hold_creator():
            blocked.set()
            assert release.wait(5)
        page.tasks.post_task(hold_creator)
        assert (await asyncio.to_thread(blocked.wait, 2))
        runtime = runtimes[0]
        assert runtime.executor.call(runtime._invoke, runtime._context.exec_js,
            "importScripts('dependency.js');imported", timeout=2) == 42
    finally:
        release.set()
        (await browser.close())


@pytest.mark.race
@pytest.mark.parametrize('action', ['terminate', 'close'])
async def test_sync_import_wait_is_cancelled_without_running_late_source(monkeypatch, action):
    import threading
    from pyv8c.web.workers.runtime import WorkerRuntime
    ready, entered, release = threading.Event(), threading.Event(), threading.Event()
    runtimes = []
    def started(runtime):
        runtimes.append(runtime)
        ready.set()
    monkeypatch.setattr(WorkerRuntime, '_started', started)
    def loader(url, request):
        if url.endswith('entry.js'):
            return "onmessage=()=>{importScripts('slow.js');postMessage('late')}"
        entered.set()
        assert release.wait(5)
        return "postMessage('dependency-ran')"
    browser = (await Browser(real_network=False, resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>',url='https://worker.test/', wait_until="commit"))
        page.evaluate("globalThis.worker=new Worker('/entry.js');worker.onmessage=e=>{}")
        assert (await asyncio.to_thread(ready.wait, 2))
        runtime = runtimes[0]
        ownership = runtime._realm.resource_ownership
        page.evaluate("worker.postMessage('start')")
        assert (await asyncio.to_thread(entered.wait, 2))
        if action == 'terminate':
            page.evaluate('worker.terminate()')
        else:
            (await page.close())
        (await asyncio.to_thread(runtime.executor._thread.join, 2))
        assert not runtime.executor._thread.is_alive()
        assert ownership.active_count == 0
    finally:
        release.set()
        (await browser.close())
