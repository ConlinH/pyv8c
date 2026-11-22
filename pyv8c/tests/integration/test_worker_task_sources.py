"""Worker commands and messages participate in source selection."""

import asyncio
import threading
from concurrent.futures import Future
import pytest
from pyv8c import pyv8

from pyv8c.async_api import Browser
from pyv8c.web.workers.runtime import WorkerRuntime


@pytest.mark.parametrize('entry', ['ordinary', 'control', 'error'])
async def test_host_result_observes_microtasks_even_on_error_and_control_paths(monkeypatch, entry):
    ready, runtimes = threading.Event(), []
    def started(runtime):
        runtimes.append(runtime)
        ready.set()
    monkeypatch.setattr(WorkerRuntime, '_started', started)
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("globalThis.worker=new Worker('data:text/javascript,0')")
        assert (await asyncio.to_thread(ready.wait, 2))
        runtime = runtimes[0]
        source = "globalThis.order=['sync'];queueMicrotask(()=>order.push('micro'));"
        if entry == 'error':
            with pytest.raises(pyv8.JSException):
                runtime.executor.call(runtime._context.exec_js, source + "throw Error('fixture')")
        else:
            options = {'_task_lane': None} if entry == 'control' else {}
            runtime.executor.call(runtime._context.exec_js, source, **options)
        assert runtime.executor.call(runtime._context.exec_js, 'JSON.stringify(order)') == '["sync","micro"]'
    finally:
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 3))
            assert not runtime.executor._thread.is_alive()


@pytest.mark.parametrize('delivery', ['host', 'message'])
async def test_worker_delivery_does_not_bypass_queued_document_task(monkeypatch, delivery):
    ready, release, done = threading.Event(), threading.Event(), threading.Event()
    runtimes, seen = [], []

    def started(runtime):
        runtimes.append(runtime)
        def block():
            runtime._realm.tasks.post(lambda: seen.append('dom'))
            ready.set()
            assert release.wait(5)
        runtime._realm.tasks.post(block)

    def message(runtime, value, ports=()):
        seen.append(value)
        done.set()

    monkeypatch.setattr(WorkerRuntime, '_started', started)
    monkeypatch.setattr(WorkerRuntime, '_dispatch_message', message)
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("globalThis.worker=new Worker(URL.createObjectURL(new Blob(['0'])))")
        assert (await asyncio.to_thread(ready.wait, 2))
        runtime = runtimes[0]
        if delivery == 'host':
            assert runtime.executor.post(lambda: (seen.append('host'), done.set()))
        else:
            runtime.post_from_parent('message')
        release.set()
        assert (await asyncio.to_thread(done.wait, 2))
        assert seen == ['dom', delivery]
    finally:
        release.set()
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 3))
            assert not runtime.executor._thread.is_alive()


async def test_failed_worker_source_releases_its_executor(monkeypatch):
    runtimes = []
    def fail(runtime):
        runtimes.append(runtime)
        raise OSError('local fixture: worker source unavailable')
    monkeypatch.setattr(WorkerRuntime, '_source_of_worker', fail)
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("globalThis.failed=false;globalThis.worker=new Worker('data:text/javascript,0');"
                      "worker.onerror=e=>{failed=true;e.preventDefault()}")
        (await page.wait_for('failed', timeout=2))
        runtime = runtimes[0]
        (await asyncio.to_thread(runtime.executor._thread.join, 0.5))
        assert not runtime.executor._thread.is_alive()
        assert runtime._realm is None
        assert (await page.run_until_idle(timeout=1))
        assert page.tasks.worker_pending == 0
        assert page.evaluate('42') == 42
    finally:
        (await browser.close())


async def test_termination_interrupts_an_unbounded_host_operation(monkeypatch):
    ready, finished = threading.Event(), Future()
    runtimes = []
    def started(runtime):
        runtime.executor.default_timeout = None
        runtimes.append(runtime)
        ready.set()
    monkeypatch.setattr(WorkerRuntime, '_started', started)
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    thread = None
    try:
        page = browser.new_profile().new_page()
        page.evaluate("globalThis.entered=false;globalThis.worker=new Worker('data:text/javascript,0');"
                      "worker.onmessage=()=>entered=true")
        assert (await asyncio.to_thread(ready.wait, 2))
        runtime = runtimes[0]
        def execute():
            try:
                finished.set_result(runtime.executor.call(runtime._context.exec_js,
                    'postMessage(1);while(true){}', timeout=None))
            except BaseException as error:
                finished.set_exception(error)
        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        (await page.wait_for('entered', timeout=2))
        runtime.stop()
        with pytest.raises(pyv8.JavaScriptTerminated):
            (await asyncio.to_thread(finished.result, 2))
        (await asyncio.to_thread(runtime.executor._thread.join, 2))
        assert not runtime.executor._thread.is_alive()
        assert runtime._agent_tasks is None
    finally:
        (await browser.close())
        if thread is not None:
            (await asyncio.to_thread(thread.join, 3))
            assert not thread.is_alive()


async def test_startup_messages_keep_fifo_and_microtask_boundaries(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runtimes = []
    original = WorkerRuntime._source_of_worker
    def source(runtime):
        runtimes.append(runtime)
        entered.set()
        assert release.wait(5)
        return original(runtime)
    monkeypatch.setattr(WorkerRuntime, '_source_of_worker', source)
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("globalThis.messages=[];globalThis.worker=new Worker('data:text/javascript,'+"
            "encodeURIComponent(\"onmessage=e=>{postMessage(e.data);"
            "queueMicrotask(()=>postMessage('micro'+e.data))}\"));"
            "worker.onmessage=e=>messages.push(e.data)")
        assert (await asyncio.to_thread(entered.wait, 2))
        page.evaluate('worker.postMessage(1);worker.postMessage(2)')
        release.set()
        (await page.wait_for('messages.length===4', timeout=2))
        assert page.evaluate('messages') == [1, 'micro1', 2, 'micro2']
    finally:
        release.set()
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 3))
            assert not runtime.executor._thread.is_alive()


async def test_messages_across_realm_publication_keep_fifo(monkeypatch):
    loading, loaded, ready, release = (threading.Event() for _ in range(4))
    runtimes = []
    original = WorkerRuntime._source_of_worker
    def source(runtime):
        runtimes.append(runtime)
        loading.set()
        assert loaded.wait(5)
        return original(runtime)
    def started(runtime):
        ready.set()
        assert release.wait(5)
    monkeypatch.setattr(WorkerRuntime, '_source_of_worker', source)
    monkeypatch.setattr(WorkerRuntime, '_started', started)
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("globalThis.messages=[];globalThis.worker=new Worker('data:text/javascript,'+"
                      "encodeURIComponent('onmessage=e=>postMessage(e.data)'));"
                      "worker.onmessage=e=>messages.push(e.data)")
        assert (await asyncio.to_thread(loading.wait, 2))
        page.evaluate('worker.postMessage(1)')
        loaded.set()
        assert (await asyncio.to_thread(ready.wait, 2))
        page.evaluate('worker.postMessage(2)')
        release.set()
        (await page.wait_for('messages.length===2', timeout=2))
        assert page.evaluate('messages') == [1, 2]
    finally:
        loaded.set()
        release.set()
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 3))
            assert not runtime.executor._thread.is_alive()
