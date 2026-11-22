"""The executor accepts externally posted foreground turns without polling."""
import threading
import time

from pyv8c.foundation.executor import RealmExecutor


class ForegroundSource:
    def __init__(self):
        self.lock = threading.Lock()
        self.deadline = None
        self.wakeup = None
        self.ran = threading.Event()
        self.thread = None
        self.attempts = 0

    def set_wakeup(self, callback):
        with self.lock:
            self.wakeup = callback

    def post(self, delay=0):
        with self.lock:
            self.deadline = time.monotonic() + delay
            wakeup = self.wakeup
        if wakeup is not None:
            wakeup()

    def next_deadline(self):
        with self.lock:
            return None if self.deadline is None else max(0, self.deadline - time.monotonic())

    def has_runnable_tasks(self):
        deadline = self.next_deadline()
        return deadline is not None and deadline <= 0

    def run_turn(self):
        with self.lock:
            self.attempts += 1
            if self.deadline is None or self.deadline > time.monotonic():
                return False
            self.deadline = None
        self.thread = threading.get_ident()
        self.ran.set()
        return True


def test_foreground_post_wakes_owner_and_detaches():
    executor = RealmExecutor()
    source = ForegroundSource()
    try:
        executor.call(executor.attach_foreground, source)
        source.post()
        assert source.ran.wait(2)
        assert source.thread == executor._owner_ident
        executor.call(executor.detach_foreground, source)
        assert source.wakeup is None
    finally:
        executor.close()


def test_earlier_foreground_deadline_interrupts_sleep():
    executor = RealmExecutor()
    source = ForegroundSource()
    try:
        executor.call(executor.attach_foreground, source)
        source.post(5)
        source.post(.02)
        assert source.ran.wait(2)
    finally:
        executor.call(executor.detach_foreground, source)
        executor.close()


def test_unrelated_commands_do_not_pump_an_idle_foreground_source():
    executor = RealmExecutor()
    source = ForegroundSource()
    try:
        executor.call(executor.attach_foreground, source)
        for _ in range(20):
            executor.call(lambda: None)
        assert executor.call(lambda: source.attempts) == 0
    finally:
        executor.call(executor.detach_foreground, source)
        executor.close()
