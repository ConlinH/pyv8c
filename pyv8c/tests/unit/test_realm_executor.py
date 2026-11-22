
import asyncio
import threading
import time

import pytest
from loguru import logger

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import RealmBusyError
from pyv8c.foundation.executor import RealmExecutor


async def test_terminated_task_is_not_reported_as_a_page_error():
    from pyv8c import pyv8

    class Queue:
        def has_runnable_tasks(self): return True
        def run_turn(self): raise pyv8.JavaScriptTerminated()
        def report_error(self, error): raise AssertionError("termination was reported as a page error")

    executor = object.__new__(RealmExecutor)
    executor.auto_pump = True
    executor._task_queues = {Queue(): False}
    executor._queue_cursor = 0
    executor._checkpoint = None
    assert executor._run_turn() is True


async def test_close_settles_waiters_without_holding_submission_lock():
    from pyv8c.foundation.execution_state import Command, _Completion

    executor = RealmExecutor(name="test-close-callback")
    entered, release = threading.Event(), threading.Event()
    checked, acquired = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(5)

    def inspect_lock():
        with executor._submit_lock:
            acquired.set()

    class Completion(_Completion):
        def finish(self, value=None, error=None):
            thread = threading.Thread(target=inspect_lock)
            thread.start()
            if acquired.wait(1):
                checked.set()
            thread.join(1)
            super().finish(value, error)

    try:
        assert executor.post(block)
        assert (await asyncio.to_thread(entered.wait, 2))
        completion = Completion()
        executor._submit_command(Command(completion, lambda: None, (), {}, time.perf_counter(), None, 999), None)
        executor.close(timeout=0)
        assert checked.is_set(), 'completion ran while the submission lock was held'
        with pytest.raises(RuntimeError, match='closed'):
            (await asyncio.to_thread(completion.wait, None, None))
    finally:
        release.set()
        executor.close()


def _late_failure():
    time.sleep(0.3)
    raise RuntimeError("late failure")


def _late_success():
    time.sleep(0.2)
    return "late"


def sink(messages):
    """Capture log lines, so a warning about a command that outlived its caller is readable."""
    return logger.add(lambda message: messages.append(str(message).strip()), format="{message}")


def wait_for_lines(messages, count=1, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(messages) < count:
        time.sleep(0.02)


async def test_executor_runs_calls_on_one_owner_thread_and_closes_waiters():
    executor = RealmExecutor(name="test-realm")
    try:
        caller = threading.get_ident()
        owner = executor.call(threading.get_ident)
        assert owner != caller
        assert executor.call(lambda: executor.is_owner_thread()) is True
    finally:
        executor.close()
    with pytest.raises(RuntimeError, match="closed"):
        executor.call(lambda: None)


async def test_page_background_timer_runs_without_manual_queue_pump():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        page.evaluate("window.__executorTimer = 0; setTimeout(() => __executorTimer = 1, 10)")
        (await asyncio.sleep(0.2))
        assert page.evaluate("__executorTimer") == 1
    finally:
        (await browser.close())


async def test_page_close_stops_realm_executor():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.close())
    assert page.realm.executor.closed
    (await browser.close())


async def test_timed_out_queued_command_is_not_executed_later():
    executor = RealmExecutor(name="test-cancel")
    entered = threading.Event()
    release = threading.Event()
    side_effect = []
    try:
        executor.post(lambda: (entered.set(), release.wait(2)))
        assert (await asyncio.to_thread(entered.wait, 1))
        with pytest.raises(RealmBusyError, match="command cancelled"):
            executor.call(lambda: side_effect.append("ghost"), timeout=0.01)
        release.set()
        (await asyncio.sleep(0.05))
        assert side_effect == []
    finally:
        release.set()
        executor.close()


async def test_a_command_that_outlives_its_caller_reports_what_it_ended_with():
    """The reason a wedged realm finally gives up is logged, not swallowed."""
    messages = []
    token = sink(messages)
    executor = RealmExecutor(name="test-late-failure")
    try:
        with pytest.raises(RealmBusyError, match="its outcome is logged"):
            executor.call(_late_failure, timeout=0.05)
        wait_for_lines(messages)
    finally:
        logger.remove(token)
        executor.close()
    assert len(messages) == 1
    assert "'_late_failure' failed" in messages[0]
    assert "RuntimeError: late failure" in messages[0]
    assert "after it was submitted" in messages[0]
    # The caller's own budget is repeated: ending just past it means the command
    # started near the deadline rather than being slow itself.
    assert "its caller waited 0.05s" in messages[0]


async def test_a_command_that_finishes_late_is_reported_too():
    messages = []
    token = sink(messages)
    executor = RealmExecutor(name="test-late-success")
    try:
        with pytest.raises(RealmBusyError):
            executor.call(_late_success, timeout=0.05)
        wait_for_lines(messages)
    finally:
        logger.remove(token)
        executor.close()
    assert len(messages) == 1
    assert "'_late_success' finished" in messages[0]
    assert "failed" not in messages[0]
