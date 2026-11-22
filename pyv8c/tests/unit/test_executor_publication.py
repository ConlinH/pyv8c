"""Force publication at each side of the driver's scan-to-wait boundary."""

import queue
import threading

import pytest
from pyv8c.foundation.executor import RealmExecutor


def sleeper():
    executor = object.__new__(RealmExecutor)
    executor._wake_condition = threading.Condition()
    executor._wake_sequence = 0
    executor._closed = threading.Event()
    executor._commands = queue.SimpleQueue()
    executor.auto_pump = False
    return executor


@pytest.mark.parametrize("deadline", [None, 30.0])
@pytest.mark.parametrize("stage", ["before_sleep", "work_scan", "deadline_scan"])
def test_a_published_wake_cannot_be_erased(deadline, stage, monkeypatch):
    executor = sleeper()
    executor._next_deadline = lambda: deadline
    executor._drain_commands = lambda: False
    executor._run_turn = lambda: False
    executor.auto_pump = False
    if stage == "before_sleep":
        sleep = executor._sleep

        def published_before_sleep(observed):
            executor.wakeup()
            return sleep(observed)

        monkeypatch.setattr(executor, "_sleep", published_before_sleep)
    elif stage == "work_scan":

        def scan():
            executor.wakeup()
            return False

        executor._run_turn = scan
    else:

        def scan():
            executor.wakeup()
            return deadline

        executor._next_deadline = scan

    def unexpected_wait(*_):
        pytest.fail("parked after an unconsumed publication")

    monkeypatch.setattr(executor._wake_condition, "wait", unexpected_wait)
    executor._pump()


def test_publication_after_the_final_check_interrupts_the_wait(monkeypatch):
    executor = sleeper()
    executor._next_deadline = lambda: 30.0
    entered = threading.Event()
    wait = executor._wake_condition.wait

    def waiting(timeout):
        # The publisher must acquire the same lock. It can only publish once
        # wait atomically registers this waiter and releases the condition.
        entered.set()
        return wait(timeout)

    monkeypatch.setattr(executor._wake_condition, "wait", waiting)
    owner = threading.Thread(target=executor._sleep, args=(0,))
    owner.start()
    try:
        assert entered.wait(2)
        executor.wakeup()
        owner.join(2)
        assert not owner.is_alive()
    finally:
        executor._closed.set()
        executor.wakeup()
        owner.join(2)


def test_spurious_notifications_do_not_restart_the_deadline(monkeypatch):
    executor = sleeper()
    executor._next_deadline = lambda: 0.01
    waits = []
    clock = iter([10.0, 10.0, 10.004, 10.02])
    monkeypatch.setattr("pyv8c.foundation.executor.time.monotonic", lambda: next(clock))
    monkeypatch.setattr(executor._wake_condition, "wait", lambda timeout: waits.append(timeout))
    executor._sleep(0)
    assert waits == pytest.approx([0.01, 0.006])
