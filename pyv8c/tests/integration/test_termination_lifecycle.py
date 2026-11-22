"""Termination unwinds callbacks; only the ticket owner classifies cancellation."""

import asyncio
import threading

import pytest
from pyv8c import pyv8
from loguru import logger

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import RealmBusyError


async def test_rejected_native_stop_is_not_classified_as_close_cancellation():
    from types import SimpleNamespace
    from pyv8c.platform.execution import WindowExecutionGroup
    group = WindowExecutionGroup.__new__(WindowExecutionGroup)
    group._control_lock = threading.RLock()
    group._stopped_operations = set()
    endpoint = object()
    group._operation = (endpoint, SimpleNamespace(terminate_operation=lambda ticket: False), 17)
    assert group.request_stop(endpoint) is False
    assert not group._stopped_operations


@pytest.mark.parametrize('close_browser', [False, True])
async def test_close_during_load_does_not_report_failure_or_finish_load(close_browser, monkeypatch):
    entered = threading.Event()
    events, messages = [], []
    token = logger.add(lambda message: messages.append(str(message)))
    html = '''<script>
      addEventListener('load',()=>{entered();while(true){}});
      addEventListener('load',()=>record('second-load'));
      addEventListener('pageshow',()=>record('pageshow'));
      addEventListener('pagereveal',()=>record('pagereveal'));
      addEventListener('unload',()=>record('unload'));
    </script>'''
    browser = (await Browser(real_network=False, stub_logging='off',
                      resource_loader=lambda url, request: html).__aenter__())
    try:
        profile = browser.new_profile()
        page, other = profile.new_page(), profile.new_page()
        execute = page.scripts.execute_script
        def execute_script(*args, **kwargs):
            page.v8_context.expose(entered=entered.set, record=events.append)
            return execute(*args, **kwargs)
        monkeypatch.setattr(page.scripts, 'execute_script', execute_script)
        errors = []
        def interrupt_load():
            try:
                assert entered.wait(2)
                page.execution_group.request_stop()
            except BaseException as exc:
                errors.append(exc)
        control = threading.Thread(target=interrupt_load)
        control.start()
        try:
            await page.goto('http://local.test/', wait_until='domcontentloaded')
            waiting = page.lifecycle.wait_for_state('load')
            await asyncio.to_thread(control.join, 3)
            assert entered.is_set() and not control.is_alive() and not errors
        finally:
            await asyncio.to_thread(control.join, 3)
        if close_browser:
            await browser.close()
        else:
            (await page.close())
        with pytest.raises((pyv8.JavaScriptTerminated, RuntimeError)):
            (await asyncio.to_thread(waiting.result, 1))
        assert events == ['unload']
        assert page.closed
        assert not page.lifecycle._state_waiters
        assert not page.execution_group._stopped_operations
        assert not any('the host could not finish' in message for message in messages)
        if not close_browser:
            assert other.evaluate('1+1') == 2
    finally:
        (await browser.close())
        logger.remove(token)


async def test_listener_budget_failure_propagates_and_next_operation_recovers():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        with pytest.raises((pyv8.JavaScriptTerminated, RealmBusyError)):
            page.evaluate('''globalThis.seen=[];globalThis.target=new EventTarget();
              target.addEventListener('x',()=>{while(true){}});
              target.addEventListener('x',()=>seen.push('second'));
              target.dispatchEvent(new Event('x'));seen.push('after');''', timeout=0.05)
        assert page.evaluate('seen') == []
        assert page.evaluate('target.dispatchEvent(new Event("other"))') is True
        assert page.evaluate('1+1') == 2
    finally:
        (await browser.close())


async def test_close_during_synchronous_dispatch_still_fails_the_host_operation():
    entered = threading.Event()
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.executor.call(page.v8_context.expose, entered=entered.set)
        errors = []
        def interrupt_dispatch():
            try:
                assert entered.wait(2)
                page.execution_group.request_stop()
            except BaseException as exc:
                errors.append(exc)
        control = threading.Thread(target=interrupt_dispatch)
        control.start()
        try:
            with pytest.raises((pyv8.JavaScriptTerminated, RealmBusyError)):
                page.evaluate("let t=new EventTarget(); t.addEventListener('x',()=>{entered();while(true){}});t.dispatchEvent(new Event('x'));'success';", timeout=3)
        finally:
            await asyncio.to_thread(control.join, 3)
        assert not control.is_alive() and not errors
        (await page.close())
    finally:
        (await browser.close())


@pytest.mark.parametrize('stage', ['load', 'pageshow', 'pagereveal', 'readystatechange'])
async def test_background_load_budget_fails_waiters_and_reports_once(monkeypatch, stage):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    messages = []
    token = logger.add(lambda message: messages.append(str(message)))
    try:
        page = browser.new_profile().new_page()
        target = 'document' if stage == 'readystatechange' else 'window'
        page.evaluate(f"{target}.addEventListener('{stage}',()=>{{while(true){{}}}})")
        def prepare():
            lifecycle = page.lifecycle
            lifecycle.dom_content_loaded = True
            lifecycle.document_load_fired = False
            lifecycle._load_started = False
            page.document.ready_state = 'interactive'
            return lifecycle.wait_for_state('load')
        waiting = page.executor.call(prepare)
        monkeypatch.setattr(page.execution_group.executor, 'default_timeout', 0.1)
        page.executor.post(page.lifecycle.maybe_fire_document_load)
        with pytest.raises(pyv8.JavaScriptTerminated):
            (await asyncio.to_thread(waiting.result, 2))
        assert page.evaluate('1+1', timeout=1) == 2
        with pytest.raises(pyv8.JavaScriptTerminated):
            (await page.wait_for_load_state('load', timeout=1))
        assert not page.lifecycle.document_load_fired
        assert not page.lifecycle._state_waiters
        assert sum('the host could not finish:' in message for message in messages) == 1
    finally:
        (await browser.close())
        logger.remove(token)


async def test_worker_listener_termination_is_cancelled_without_host_failure(monkeypatch):
    from pyv8c.web.workers.runtime import WorkerRuntime
    ready, entered = threading.Event(), threading.Event()
    runtimes, messages, events = [], [], []
    def started(runtime):
        runtime._context.expose(entered=entered.set, record=events.append)
        runtimes.append(runtime)
        ready.set()
    monkeypatch.setattr(WorkerRuntime, '_started', started)
    token = logger.add(lambda message: messages.append(str(message)))
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""globalThis.worker=new Worker(URL.createObjectURL(new Blob([
          "addEventListener('message',()=>{entered();while(true){}});"+
          "addEventListener('message',()=>record('second'));"
        ],{type:'text/javascript'})));""")
        assert (await asyncio.to_thread(ready.wait, 2))
        page.evaluate('worker.postMessage(1)')
        assert (await asyncio.to_thread(entered.wait, 2))
        runtime = runtimes[0]
        runtime.stop()
        (await asyncio.to_thread(runtime.executor._thread.join, 2))
        assert not runtime.executor._thread.is_alive()
        assert runtime._realm is None
        assert not runtime._stopped_operations
        assert not events
        assert not any('failed in the host' in message or 'the host could not finish' in message
                       for message in messages)
        assert page.evaluate('1+1') == 2
    finally:
        (await browser.close())
        logger.remove(token)


@pytest.mark.parametrize('schedule', ['setTimeout', 'requestAnimationFrame'])
async def test_close_during_background_callback_does_not_report_host_failure(schedule):
    entered = threading.Event()
    messages = []
    token = logger.add(lambda message: messages.append(str(message)))
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.executor.call(page.v8_context.expose, entered=entered.set)
        errors = []
        def interrupt_callback():
            try:
                assert entered.wait(2)
                page.execution_group.request_stop()
            except BaseException as exc:
                errors.append(exc)
        control = threading.Thread(target=interrupt_callback)
        control.start()
        try:
            page.evaluate(f'{schedule}(()=>{{entered();while(true){{}}}})')
            await asyncio.to_thread(control.join, 3)
        finally:
            await asyncio.to_thread(control.join, 3)
        assert entered.is_set() and not control.is_alive() and not errors
        (await page.close())
        assert not page.execution_group._stopped_operations
        assert not any('the host could not finish' in message for message in messages)
    finally:
        (await browser.close())
        logger.remove(token)


async def test_worker_stopped_during_startup_does_not_deliver_started(monkeypatch):
    from pyv8c.web.workers.runtime import WorkerRuntime
    entered = threading.Event()
    runtimes, started = [], []
    invoke = WorkerRuntime._invoke
    def invoke_source(runtime, *args, **kwargs):
        if runtime not in runtimes:
            runtimes.append(runtime)
            runtime._context.expose(entered=entered.set)
        return invoke(runtime, *args, **kwargs)
    monkeypatch.setattr(WorkerRuntime, '_invoke', invoke_source)
    monkeypatch.setattr(WorkerRuntime, '_started', lambda runtime: started.append(runtime))
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("globalThis.worker=new Worker('data:text/javascript,entered();while(true){}')")
        assert (await asyncio.to_thread(entered.wait, 2))
        runtime = runtimes[0]
        runtime.stop()
        (await asyncio.to_thread(runtime.executor._thread.join, 2))
        assert not runtime.executor._thread.is_alive()
        assert not started
        assert runtime._realm is None
    finally:
        (await browser.close())
