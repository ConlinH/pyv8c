"""Synchronous results preserve deadlines, cancellation and callback lifetime."""

import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest
from pyv8c.foundation.execution_state import Command
from pyv8c.foundation.executor import RealmExecutor
from pyv8c.foundation.host_errors import RealmBusyError
from pyv8c.foundation.tasks import TaskQueue


def test_callback_timeout_error_is_not_mistaken_for_caller_expiry():
    executor = RealmExecutor()
    error = TimeoutError("raised by callback")

    def fail():
        raise error

    try:
        with pytest.raises(TimeoutError) as caught:
            executor.call(fail)
        assert caught.value is error
        assert executor.call(lambda: 42) == 42
    finally:
        executor.close()


def test_result_is_published_after_deadline_cleanup():
    executor = RealmExecutor()
    entered, release = threading.Event(), threading.Event()

    class Deadline:
        def set_deadline(self, seconds):
            pass

        def clear_deadline(self):
            entered.set()
            assert release.wait(2)

    try:
        executor.attach_deadline_source(Deadline())
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(executor.call, lambda: 42)
            assert entered.wait(2)
            assert not result.done()
            release.set()
            assert result.result(2) == 42
    finally:
        release.set()
        executor.close()


@pytest.mark.parametrize("routed", [False, True])
def test_completed_callbacks_are_released_without_cyclic_gc(routed):
    executor = RealmExecutor()
    queue = TaskQueue()
    refs = []

    class Callback:
        def __call__(self):
            return 42

    enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        executor.attach_task_queue(queue)
        before = sum(type(obj) is Command for obj in gc.get_objects())
        for _ in range(50):
            callback = Callback()
            refs.append(weakref.ref(callback))
            assert executor.call(callback, _task_lane=queue if routed else None) == 42
        del callback
        executor.call(lambda: None, _task_lane=queue if routed else None)
        assert all(ref() is None for ref in refs)
        # The owner may still be unwinding the last operation. It must not
        # retain one Command per successful call when cyclic GC is disabled.
        assert sum(type(obj) is Command for obj in gc.get_objects()) <= before + 2
    finally:
        executor.close()
        if enabled:
            gc.enable()


def test_cancelled_lane_command_does_not_run_or_retain_its_callback():
    executor = RealmExecutor()
    queue = TaskQueue()
    entered, release = threading.Event(), threading.Event()

    class Callback:
        def __call__(self):
            pytest.fail("cancelled command ran")

    callback = Callback()
    ref = weakref.ref(callback)
    try:
        executor.attach_task_queue(queue)
        executor.post(lambda: (entered.set(), release.wait(2)))
        assert entered.wait(2)
        with pytest.raises(RealmBusyError, match="command cancelled"):
            executor.call(callback, timeout=0.01, _task_lane=queue)
        del callback
        assert ref() is None
        release.set()
        assert executor.call(lambda: 42) == 42
    finally:
        release.set()
        executor.close()


def test_concurrent_callers_receive_their_own_values():
    executor = RealmExecutor()
    try:
        with ThreadPoolExecutor(8) as pool:
            values = list(pool.map(lambda i: executor.call(lambda: i), range(200)))
        assert values == list(range(200))
    finally:
        executor.close()


@pytest.mark.parametrize("failure", ["exception_text", "callback_repr", "log_sink"])
def test_late_diagnostic_failure_does_not_close_the_executor(monkeypatch, failure):
    from pyv8c.foundation import execution_state as module

    entered, release = threading.Event(), threading.Event()
    messages = []

    class BrokenError(Exception):
        def __str__(self):
            raise ValueError("exception cannot be formatted")

    class Callback:
        def __call__(self):
            entered.set()
            assert release.wait(2)
            if failure == "exception_text":
                raise BrokenError()
            return 7

        def __repr__(self):
            if failure == "callback_repr":
                raise ValueError("callback cannot be formatted")
            return "late_callback"

    class Log:
        def warning(self, message):
            messages.append(message)
            if failure == "log_sink":
                raise OSError("log destination unavailable")

    monkeypatch.setattr(module, "logger", Log())
    executor = RealmExecutor()
    try:
        with ThreadPoolExecutor(1) as callers:
            result = callers.submit(executor.call, Callback(), timeout=0.1)
            assert entered.wait(2)
            with pytest.raises(RealmBusyError):
                result.result(2)
            release.set()
            assert executor.call(lambda: 42, timeout=2) == 42
        assert not executor.closed
        assert len(messages) == 1
        if failure == "exception_text":
            assert "BrokenError" in messages[0]
            assert "unprintable" in messages[0]
        elif failure == "callback_repr":
            assert "unprintable" in messages[0]
    finally:
        release.set()
        executor.close()


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(1)])
def test_late_diagnostics_preserve_execution_control(monkeypatch, error):
    from pyv8c.foundation import execution_state as module

    class Log:
        def warning(self, message):
            raise error

    monkeypatch.setattr(module, "logger", Log())
    with pytest.raises(type(error)) as caught:
        module._report_late(None, "callback", 0.0, 1.0)
    assert caught.value is error
