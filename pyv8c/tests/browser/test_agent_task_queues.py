
import asyncio
import threading
import subprocess
import sys
from pyv8c import pyv8

from pyv8c.async_api import Browser
from pyv8c.foundation.tasks import TaskSource


async def test_same_agent_source_preserves_enqueue_order_across_documents():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe></iframe>', wait_until="commit"))
        child = page.document.fn_querySelector('iframe')._child_services._page_realm.page
        log, done = [], threading.Event()

        def enqueue():
            page.tasks.post(lambda: log.append('parent-first'), TaskSource.POSTED_MESSAGE)
            child.tasks.post(lambda: log.append('child'), TaskSource.POSTED_MESSAGE)
            page.tasks.post(lambda: (log.append('parent-last'), done.set()), TaskSource.POSTED_MESSAGE)

        page.executor.call(enqueue)
        assert (await asyncio.to_thread(done.wait, 3))
        assert log == ['parent-first', 'child', 'parent-last']
    finally:
        (await browser.close())


async def test_stop_between_operations_prevents_new_background_execution():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        group = page.execution_group
        group.executor.call(group.request_stop)
        assert group.stopping
        assert not page.tasks.post(lambda: None)
    finally:
        (await browser.close())


async def test_stop_during_a_task_does_not_silently_skip_nested_host_initialization():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        done = threading.Event()
        results = []
        def active_task():
            page.execution_group.request_stop()
            results.append(page.executor.call(lambda: 'initialized'))
            done.set()
        page.tasks.post(active_task)
        assert (await asyncio.to_thread(done.wait, 3))
        assert results == ['initialized']
    finally:
        (await browser.close())


async def test_background_error_listener_and_microtasks_share_the_task_budget():
    program = '\nfrom pyv8c.async_api import Browser, run\nasync def _program():\n\n    from pyv8c.async_api import Browser, run\n    from loguru import logger\n    logger.disable(\'pyv8c\')\n    browser=(await Browser(real_network=False,stub_logging=\'off\').__aenter__())\n    try:\n        page=browser.new_profile().new_page()\n        page.execution_group.executor.default_timeout=0.03\n        page.evaluate("globalThis.finished=false;globalThis.errorSeen=false;onerror=()=>{errorSeen=true;queueMicrotask(function spin(){queueMicrotask(spin)});return true}")\n        def bad_task():page.v8_context.exec_js("throw new Error(\'background failure\')")\n        page.tasks.post(bad_task)\n        page.tasks.post(lambda:page.v8_context.exec_js(\'finished=true\'))\n        (await page.wait_for(\'finished\',timeout=2))\n        assert page.evaluate(\'errorSeen\') is True\n        assert page.evaluate(\'42\',timeout=1)==42\n    finally:\n        # The 30ms budget is for the background task, not native heap teardown.\n        if \'page\' in locals():page.execution_group.executor.default_timeout=2\n        (await browser.close())\nrun(_program())\n'
    result = (await asyncio.to_thread(subprocess.run, [sys.executable, '-c', program], capture_output=True,
                            text=True, timeout=8))
    assert result.returncode == 0, result.stdout + result.stderr


async def test_posted_host_errors_are_reported_to_their_own_window():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe></iframe>', wait_until="commit"))
        child = page.document.fn_querySelector('iframe')._child_services._page_realm.page
        page.evaluate("globalThis.errors=[];onerror=message=>{errors.push(message);return true};"
                      "frames[0].eval(\"globalThis.errors=[];onerror=message=>{errors.push(message);return true}\")")
        def fail(message):raise pyv8.JsError(message)
        page.executor.post(fail, 'parent failure')
        child.executor.post(fail, 'child failure')
        (await page.wait_for('errors.length === 1 && frames[0].errors.length === 1', timeout=0.5))
        assert page.evaluate('[errors,frames[0].errors]') == [['parent failure'], ['child failure']]
    finally:
        (await browser.close())


async def test_terminated_background_task_does_not_fire_error_or_stop_siblings():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe></iframe>', wait_until="commit"))
        child = page.document.fn_querySelector('iframe')._child_services._page_realm.page
        page.evaluate("globalThis.errorCount=0; onerror=()=>{errorCount++}")
        done = threading.Event()
        def terminate():
            raise pyv8.JavaScriptTerminated()
        page.tasks.post(terminate)
        child.tasks.post(done.set)
        assert (await asyncio.to_thread(done.wait, 3))
        assert page.evaluate('errorCount') == 0
        assert child.evaluate('6 * 7') == 42
    finally:
        (await browser.close())


async def wait_for_owner_park(driver, monkeypatch):
    """Observe a cooperative idle turn without a condition-variable waiter."""
    observed = driver._loop.create_future()
    driver.wakeup()
    driver._loop.call_soon(lambda: observed.set_result(driver._soon is None))
    assert await observed


async def test_timer_submitted_from_another_thread_wakes_a_parked_agent(monkeypatch):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        driver = page.execution_group.executor
        done = threading.Event()
        assert await page.run_until_idle(timeout=1)
        await wait_for_owner_park(driver, monkeypatch)
        await asyncio.to_thread(page.tasks.schedule, done.set, 0.01)
        assert (await asyncio.to_thread(done.wait, 1))
    finally:
        (await browser.close())
