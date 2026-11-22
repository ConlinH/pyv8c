"""Command publication and parking remain safe with one notification and scan."""

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pyv8c.foundation.agent_tasks import AgentTaskQueue, TaskContext
from pyv8c.foundation.executor import RealmExecutor
from pyv8c.foundation.host_errors import RealmBusyError
from pyv8c.foundation.tasks import TaskQueue


@pytest.mark.parametrize("routed", [False, True])
def test_command_publication_wakes_the_owner_once(routed, monkeypatch):
    executor, lane = RealmExecutor(), TaskQueue()
    entered, release, ran = threading.Event(), threading.Event(), threading.Event()
    wakes = []
    try:
        executor.attach_task_queue(lane)
        executor.post(lambda: (entered.set(), release.wait(3)))
        assert entered.wait(2)
        original = executor.wakeup

        def wake():
            wakes.append(None)
            original()

        monkeypatch.setattr(executor, "wakeup", wake)
        lane.set_wakeup(wake)
        assert executor.post(ran.set, _task_lane=lane if routed else None)
        assert len(wakes) == 1
        release.set()
        assert ran.wait(2)
    finally:
        release.set()
        executor.close()
        lane.close()


def test_managed_command_can_complete_before_its_ticket_is_published(monkeypatch):
    executor, lane = RealmExecutor(), TaskQueue()
    finished = threading.Event()
    execute, post = executor._execute, lane.post_managed

    def complete(command):
        try:
            return execute(command)
        finally:
            finished.set()

    def delayed_publication(callback):
        ticket = post(callback)
        assert finished.wait(2)
        return ticket

    try:
        executor.attach_task_queue(lane)
        monkeypatch.setattr(executor, "_execute", complete)
        monkeypatch.setattr(lane, "post_managed", delayed_publication)
        assert executor.call(lambda: 42, _task_lane=lane) == 42
        assert not executor._pending_commands
        assert executor.call(lambda: 43) == 43
    finally:
        executor.close()
        lane.close()


def test_closing_during_ticket_publication_cannot_leave_a_live_command(monkeypatch):
    executor, lane = RealmExecutor(), TaskQueue()
    entered, release = threading.Event(), threading.Event()
    seen = []
    post = lane.post_managed

    def close_during_post(callback):
        ticket = post(callback)
        executor.close(timeout=0)
        return ticket

    try:
        executor.attach_task_queue(lane)
        executor.post(lambda: (entered.set(), release.wait(3)))
        assert entered.wait(2)
        monkeypatch.setattr(lane, "post_managed", close_during_post)
        with pytest.raises(RuntimeError, match="closed"):
            executor.call(lambda: seen.append("must not execute"), _task_lane=lane)
        assert not executor._pending_commands
        assert not lane.has_runnable_tasks()
        release.set()
    finally:
        release.set()
        executor.close()
        lane.close()
    assert not seen


def test_closing_after_registration_cannot_restore_a_retired_runner(monkeypatch):
    executor, lane = RealmExecutor(), TaskQueue()
    entered, release = threading.Event(), threading.Event()
    registered = []

    class ClosingRegistry(dict):
        def __setitem__(self, key, command):
            super().__setitem__(key, command)
            registered.append(command)
            executor.close(timeout=0)

    try:
        executor.attach_task_queue(lane)
        executor.post(lambda: (entered.set(), release.wait(3)))
        assert entered.wait(2)
        monkeypatch.setattr(executor, "_pending_commands", ClosingRegistry())
        with pytest.raises(RuntimeError, match="closed"):
            executor.call(lambda: 42, _task_lane=lane)
        (command,) = registered
        assert command.discarded
        assert command.runner is command.callback is None
        assert not lane.has_runnable_tasks()
    finally:
        release.set()
        executor.close()
        lane.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_migrated_ticket_keeps_its_original_cancellation_lock(monkeypatch, cancel):
    old, new = AgentTaskQueue(), AgentTaskQueue()
    lane = TaskQueue()
    lane.bind_scheduler(old, None, TaskContext())
    ticket = lane.post_task(lambda: None)
    lane.bind_scheduler(new, None, TaskContext())
    entered, finished = threading.Event(), threading.Event()
    valid = new._valid

    def observe(record):
        entered.set()
        return valid(record)

    def read():
        try:
            return new.has_runnable_tasks()
        finally:
            finished.set()

    monkeypatch.setattr(new, "_valid", observe)
    try:
        with ThreadPoolExecutor(1) as pool:
            with ticket._lock:
                pending = pool.submit(read)
                assert entered.wait(2)
                assert not finished.wait(0.03)
                if cancel:
                    assert ticket.cancel()
            assert pending.result(2) is (not cancel)
    finally:
        lane.close()
        old.close()
        new.close()


def test_an_idle_pump_does_not_scan_the_same_ready_sources_twice(monkeypatch):
    executor = object.__new__(RealmExecutor)
    executor._wake_condition = threading.Condition()
    executor._wake_sequence = 0
    executor._closed = threading.Event()
    executor._commands = queue.SimpleQueue()
    executor._queue_cursor = 0
    executor._checkpoint = None
    executor.auto_pump = True
    executor._drain_commands = lambda: False

    class Source:
        closed = False

        def __init__(self):
            self.readiness = self.deadlines = 0

        def has_runnable_tasks(self):
            self.readiness += 1
            return False

        def next_deadline(self):
            self.deadlines += 1
            return None

    class Rendering(Source):
        def run_frame(self):
            return self.has_runnable_tasks()

    tasks, foreground, rendering = Source(), Source(), Rendering()
    executor._task_queues = {tasks: False}
    executor._foreground, executor._rendering = foreground, rendering
    monkeypatch.setattr(executor._wake_condition, "wait", lambda seconds: executor._closed.set())
    executor._pump()
    assert [source.readiness for source in (tasks, foreground, rendering)] == [1, 1, 1]
    assert [source.deadlines for source in (tasks, foreground, rendering)] == [1, 1, 1]


def test_expired_routed_command_cannot_execute_after_the_owner_resumes():
    executor, lane = RealmExecutor(), TaskQueue()
    entered, release = threading.Event(), threading.Event()
    seen = []
    try:
        executor.attach_task_queue(lane)
        executor.post(lambda: (entered.set(), release.wait(3)))
        assert entered.wait(2)
        with pytest.raises(RealmBusyError, match="command cancelled"):
            executor.call(lambda: seen.append("expired"), timeout=0.01, _task_lane=lane)
        release.set()
        assert executor.call(lambda: 42, _task_lane=lane) == 42
        assert not seen
    finally:
        release.set()
        executor.close()
        lane.close()
