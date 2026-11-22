
import asyncio
import threading
import weakref
import gc

import pytest
from pyv8c import pyv8

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import RealmBusyError
from pyv8c.foundation.tasks import TaskSource


async def test_embedder_commands_join_source_rotation_instead_of_bypassing_queued_work():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        seen, done = [], threading.Event()
        def submit():
            page.tasks.post(lambda: seen.append('dom'), TaskSource.DOM)
            page.executor.post(lambda: (seen.append('host'), done.set()))
        page.executor.call(submit)
        assert (await asyncio.to_thread(done.wait, 2))
        assert seen == ['dom', 'host']
    finally:
        (await browser.close())


async def test_queued_timeout_cancels_work_and_releases_payload_before_owner_resumes():
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        finished, errors, references = threading.Event(), [], []
        class Callback:
            def __call__(self):
                raise AssertionError("cancelled command executed")
        def sender():
            callback = Callback()
            references.append(weakref.ref(callback))
            try:
                page.executor.call(callback, timeout=0.02)
            except RealmBusyError as error:
                errors.append(str(error))
            finally:
                del callback
                finished.set()
        thread = threading.Thread(target=sender)
        thread.start()
        # Deliberately starve the application owner while the internal sender's
        # queue budget expires. Public Page APIs are never called off-owner.
        assert finished.wait(1)
        thread.join(1)
        gc.collect()
        assert errors and "cancelled" in errors[0]
        assert references[0]() is None
        assert not page.execution_group.executor.has_pending_for({page.executor})
        assert page.evaluate("42", timeout=1) == 42


async def test_owner_close_cancels_queued_host_operations_before_dom_rotation(monkeypatch):
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        pending, finished, errors = threading.Event(), threading.Event(), []
        driver = page.execution_group.executor
        original_submit = driver._submit_command
        def submit(command, lane):
            result = original_submit(command, lane)
            if command.owner is page.executor:
                pending.set()
            return result
        monkeypatch.setattr(driver, "_submit_command", submit)
        def sender():
            try:
                page.executor.call(page._evaluate_data, "globalThis.mustNotRun=true", None, timeout=2)
            except RuntimeError as error:
                errors.append(str(error))
            finally:
                finished.set()
        thread = threading.Thread(target=sender)
        thread.start()
        assert pending.wait(1)
        (await page.close())
        assert finished.wait(3)
        thread.join(1)
        assert not thread.is_alive() and errors and page.closed


async def test_nested_host_dispatch_restores_the_outer_operation_deadline():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        driver = page.execution_group.executor
        def outer():
            before = driver.remaining_budget()
            page.executor.post(lambda: None)
            assert page.tasks.run_turn()
            assert driver.has_operation
            assert 0 < driver.remaining_budget() <= before
        page.executor.call(outer, timeout=1)
    finally:
        (await browser.close())


async def test_inline_evaluate_respects_its_explicit_budget_and_does_not_extend_the_outer_one():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        def outer():
            return page.evaluate("const start=Date.now();while(Date.now()-start<200){};globalThis.overran=true", timeout=0.03)
        with pytest.raises((pyv8.JavaScriptTerminated, RealmBusyError)):
            page.executor.call(outer, timeout=1)
        assert page.evaluate('typeof overran', timeout=1) == 'undefined'
        assert page.evaluate('42', timeout=1) == 42
    finally:
        (await browser.close())
