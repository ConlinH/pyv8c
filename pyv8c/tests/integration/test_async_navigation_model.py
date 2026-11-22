
import asyncio
import threading

import pytest
from pyv8c import pyv8

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import HostStateError, RealmBusyError


async def test_script_navigation_does_not_queue_behind_entire_dom_backlog(monkeypatch):
    browser = (await Browser(real_network=False, stub_logging='off',
                      resource_loader=lambda url, request: '<body>next</body>').__aenter__())
    reached = threading.Event()
    dom = []
    observed = []
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body>first</body>', url='http://site.test/', wait_until="commit"))
        original = page.navigation._load

        def load(*args, **kwargs):
            observed.append(len(dom))
            reached.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(page.navigation, '_load', load)

        def enqueue():
            for number in range(100):
                page.tasks.post(lambda number=number: dom.append(number))
            page.navigation.request_navigation('/next')

        page.executor.call(enqueue)
        assert (await asyncio.to_thread(reached.wait, 3))
        assert observed[0] < 100, 'navigation was put behind the entire DOM source backlog'
        (await page.wait_for("document.body.textContent === 'next'", timeout=3))
        assert page.evaluate('location.pathname') == '/next'
    finally:
        (await browser.close())


async def test_slow_main_document_does_not_block_evaluate_or_close():
    entered, release = threading.Event(), threading.Event()
    def loader(url, options):
        entered.set()
        assert release.wait(3)
        return {'body': '<body>late</body>'}
    browser = (await Browser(real_network=False, resource_loader=loader, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        navigation = asyncio.create_task(page.goto('https://slow.test/', timeout=2))
        try:
            assert (await asyncio.to_thread(entered.wait, 1))
            assert page.evaluate('1+1', timeout=0.5) == 2
            (await page.close())
            with pytest.raises(HostStateError, match='closed'):
                await asyncio.wait_for(navigation, 1)
        finally:
            release.set()
    finally:
        release.set()
        (await browser.close())


async def test_waiting_for_load_tracks_the_document_and_releases_timeout_waiter():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.wait_for_load_state(timeout=0.5))
        page.executor.call(setattr, page.lifecycle, 'document_load_fired', False)
        with pytest.raises(TimeoutError):
            (await page.wait_for_load_state(timeout=0.02))
        assert not page.lifecycle._state_waiters
        future = page.executor.call(page.lifecycle.wait_for_state, 'load')
        (await page.set_content('<body>new</body>', wait_until="commit"))
        with pytest.raises(HostStateError, match='replaced'):
            (await asyncio.to_thread(future.result, 1))
    finally:
        (await browser.close())


async def test_evaluate_budget_terminates_microtasks_without_terminating_next_operation():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        with pytest.raises((pyv8.JavaScriptTerminated, RealmBusyError)):
            page.evaluate('function again(){Promise.resolve().then(again)};again()', timeout=0.03)
        assert page.evaluate('1+1', timeout=1) == 2
    finally:
        (await browser.close())


async def test_script_and_callback_cleanup_happen_only_with_an_empty_js_stack():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<script>globalThis.log=[];
          Promise.resolve().then(()=>log.push(document.currentScript && document.currentScript.tagName));
          log.push('first');</script><script>log.push('second')</script>''', wait_until="commit"))
        assert page.evaluate('log') == ['first', 'SCRIPT', 'second']
        assert page.evaluate('''log=[];let target=new EventTarget();
          target.addEventListener('x',()=>{log.push('one');queueMicrotask(()=>log.push('micro'))});
          target.addEventListener('x',()=>log.push('two'));
          target.dispatchEvent(new Event('x'));log.push('after');'ok' ''') == 'ok'
        assert page.evaluate('log') == ['one', 'two', 'after', 'micro']
    finally:
        (await browser.close())


async def test_close_terminates_only_its_execution_group_and_does_not_run_queued_jobs():
    entered = threading.Event()
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        profile = browser.new_profile()
        page, other = profile.new_page(), profile.new_page()
        page.executor.call(page.v8_context.expose, entered=entered.set)
        errors = []
        def stop_running_group():
            try:
                assert entered.wait(1)
                page.execution_group.request_stop()
            except BaseException as exc:
                errors.append(exc)
        stopper = threading.Thread(target=stop_running_group)
        stopper.start()
        try:
            with pytest.raises((pyv8.JavaScriptTerminated, RealmBusyError)):
                page.evaluate("entered(); function again(){Promise.resolve().then(again)}; again();while(true){}", timeout=3)
        finally:
            await asyncio.to_thread(stopper.join, 2)
        assert not stopper.is_alive() and not errors
        (await page.close())
        assert other.evaluate('1+1') == 2
    finally:
        (await browser.close())


async def test_idle_ignores_future_timers_and_times_out_on_self_posting_work():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    stop = threading.Event()
    try:
        page = browser.new_profile().new_page()
        page.evaluate('setTimeout(()=>{},60000)')
        assert (await page.run_until_idle(0))
        entered = threading.Event()
        def recurring():
            entered.set()
            if not stop.is_set():
                page.tasks.post(recurring)
        page.tasks.post(recurring)
        assert (await asyncio.to_thread(entered.wait, 1))
        assert (await page.run_until_idle(0.02)) is False
        stop.set()
        assert (await page.run_until_idle(1))
    finally:
        stop.set()
        (await browser.close())
