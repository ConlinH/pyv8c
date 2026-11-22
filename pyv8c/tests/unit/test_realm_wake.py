"""The owner waits for a deadline or publication without polling."""
import threading
import time

from pyv8c.foundation.executor import RealmExecutor
from pyv8c.foundation.tasks import TaskQueue


def test_a_distant_timer_is_waited_for_in_one_sleep(monkeypatch):
    executor = RealmExecutor(name="test-sleep")
    queue = TaskQueue()
    executor.attach_task_queue(queue)
    sleeps = []
    condition = executor._wake_condition
    wait = condition.wait

    def recording_sleep(seconds=None):
        sleeps.append(seconds)
        return wait(seconds)

    monkeypatch.setattr(condition, 'wait', recording_sleep)
    try:
        queue.schedule(lambda: None, 60)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if any(value and value > 5 for value in sleeps):
                break
            time.sleep(0.01)
        assert any(value and value > 5 for value in sleeps), f"never waited to the deadline: {sleeps}"
        # A distant deadline must not turn into a series of short slices.
        assert [value for value in sleeps if value is not None and value <= 5] == []
    finally:
        executor.close()


def test_a_delivery_wakes_a_driver_parked_on_a_distant_deadline():
    executor = RealmExecutor(name="test-nudge")
    queue = TaskQueue()
    executor.attach_task_queue(queue)
    try:
        queue.schedule(lambda: None, 60)
        time.sleep(0.1)
        ran = threading.Event()
        started = time.perf_counter()
        assert queue.post(lambda: ran.set()) is True
        assert ran.wait(1.0), "the delivery did not wake the driver"
        assert time.perf_counter() - started < 0.5
    finally:
        executor.close()


def test_a_command_or_a_delivery_wakes_a_driver_parked_with_nothing_due():
    executor = RealmExecutor(name="test-idle-nudge")
    queue = TaskQueue()
    executor.attach_task_queue(queue)
    try:
        time.sleep(0.1)
        started = time.perf_counter()
        assert executor.call(lambda: 1) == 1
        assert time.perf_counter() - started < 0.05

        ran = threading.Event()
        started = time.perf_counter()
        assert queue.post(lambda: ran.set()) is True
        assert ran.wait(1.0), "the delivery did not wake the idle driver"
        assert time.perf_counter() - started < 0.5
    finally:
        executor.close()


def test_close_wakes_a_sleeping_driver():
    executor = RealmExecutor(name="test-close")
    time.sleep(0.05)
    started = time.perf_counter()
    executor.close(timeout=2)
    assert executor.closed
    assert not executor._thread.is_alive()
    assert time.perf_counter() - started < 1.0
