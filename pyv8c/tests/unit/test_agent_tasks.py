import gc
import threading
import weakref

import pytest

from pyv8c.foundation.agent_tasks import AgentTaskQueue, TaskContext
from pyv8c.foundation.tasks import TaskQueue, TaskSource
from pyv8c.foundation.executor import RealmExecutor


@pytest.mark.parametrize("transition", [
    "cancel", "invalidate", "rebind", "migrate", "close_lane", "close_agent", "prune", "turn",
])
def test_cancelled_callback_finalizer_can_reenter_owner(transition):
    class ClosingHost(Host):
        closed = False

        def is_closed(self):
            return self.closed

    host = ClosingHost()
    lane = TaskQueue(host)
    executor = RealmExecutor()
    outcomes = []

    class Callback:
        def __call__(self):
            pytest.fail("cancelled callback executed")

        def __del__(self):
            try:
                executor.call(lambda: (lane.pending(), lane.post(lambda: None)), timeout=0.2)
            except BaseException as error:
                outcomes.append(type(error).__name__)
            else:
                outcomes.append("completed")

    try:
        ticket = lane.post_task(Callback(), document_bound=True)
        if transition == "cancel":
            assert ticket.cancel()
        elif transition == "invalidate":
            lane.set_task_context(TaskContext())
        elif transition == "rebind":
            lane.bind_scheduler(lane._scheduler, None, TaskContext())
        elif transition == "migrate":
            lane.bind_scheduler(AgentTaskQueue(), None, TaskContext())
        elif transition == "close_lane":
            lane.close()
        elif transition == "close_agent":
            lane._scheduler.close()
        else:
            host.closed = True
            if transition == "prune":
                assert not lane._scheduler.has_runnable_tasks()
            else:
                assert not lane._scheduler.run_turn()
        assert not ticket.pending
        assert outcomes == ["completed"]
    finally:
        lane.close()
        executor.close()


class Host:
    blocked = False
    def is_closed(self): return False
    def parser_blocked(self): return self.blocked


def lanes():
    agent = AgentTaskQueue()
    left, right = TaskQueue(Host()), TaskQueue(Host())
    for lane in (left, right):
        lane.bind_scheduler(agent, lambda callback, *args: callback(*args), TaskContext())
    return agent, left, right


def test_source_fifo_and_source_fairness_are_agent_owned():
    agent, left, right = lanes()
    seen = []
    left.post(lambda: seen.append('left1'), TaskSource.POSTED_MESSAGE)
    right.post(lambda: seen.append('right'), TaskSource.POSTED_MESSAGE)
    left.post(lambda: seen.append('left2'), TaskSource.POSTED_MESSAGE)
    right.post(lambda: seen.append('network'), TaskSource.NETWORKING)
    while agent.run_turn(): pass
    assert seen == ['left1', 'network', 'right', 'left2']


def test_media_tasks_have_their_own_fifo_and_keep_document_cancellation():
    agent, left, right = lanes()
    seen = []
    for index in range(4):
        left.post(lambda index=index: seen.append(('dom', index)), TaskSource.DOM)
    left.post(lambda: seen.append('media1'), TaskSource.MEDIA_ELEMENT)
    left.post(lambda: seen.append('media2'), TaskSource.MEDIA_ELEMENT)
    stale = right.post_task(lambda: seen.append('stale'), TaskSource.MEDIA_ELEMENT,
                            document_bound=True)
    right.set_task_context(TaskContext(generation=1))
    left.schedule(lambda: seen.append('timer'), 0)
    while agent.run_turn():
        pass
    assert seen == [('dom', 0), 'media1', 'timer', ('dom', 1), 'media2',
                    ('dom', 2), ('dom', 3)]
    assert not stale.pending
    right.post(lambda: seen.append('after-close'), TaskSource.MEDIA_ELEMENT)
    right.close()
    assert not agent.run_turn() and 'after-close' not in seen


def test_parser_blocking_is_per_document_and_does_not_reorder_other_work():
    agent, left, right = lanes()
    left._host.blocked = True
    seen = []
    left.post(lambda: seen.append('blocked'), TaskSource.PARSER)
    right.post(lambda: seen.append('parser'), TaskSource.PARSER)
    left.post(lambda: seen.append('blocked-second'), TaskSource.PARSER)
    left.post(lambda: seen.append('network'), TaskSource.NETWORKING)
    while agent.run_turn(): pass
    assert seen == ['parser', 'network']
    assert not agent.has_runnable_tasks()
    left._host.blocked = False
    assert agent.run_turn()
    assert seen[-1] == 'blocked'
    assert agent.run_turn() and seen[-1] == 'blocked-second'


def test_owner_idle_queries_preserve_source_order_and_find_timers_behind_blocked_parser():
    agent, left, right = lanes()
    seen = []
    left._host.blocked = True
    left.post(lambda: seen.append('parser'), TaskSource.PARSER)
    right.post(lambda: seen.append('right'), TaskSource.POSTED_MESSAGE)
    ticket = left.post_task(lambda: seen.append('left'), TaskSource.POSTED_MESSAGE)
    cancelled = right.post_task(lambda: seen.append('cancelled'), TaskSource.NETWORKING)
    assert cancelled.cancel()
    for _ in range(3):
        assert agent.has_runnable_tasks(left) and agent.has_runnable_tasks(right)
    assert agent.run_turn() and agent.run_turn()
    assert seen == ['right', 'left'] and not ticket.pending
    assert not agent.has_runnable_tasks(left)
    left.schedule(lambda: seen.append('timer'), 0)
    assert agent.has_runnable_tasks(left) and not agent.has_runnable_tasks(right)
    assert agent.run_turn() and seen[-1] == 'timer'
    left._host.blocked = False
    assert agent.has_runnable_tasks(left)
    assert agent.run_turn() and seen == ['right', 'left', 'timer', 'parser']


def test_cancel_releases_payload_and_cannot_cancel_started_work():
    agent, left, _ = lanes()
    class Callback:
        def __call__(self): raise AssertionError('cancelled callback ran')
    callback = Callback()
    ref = weakref.ref(callback)
    ticket = left.post_task(callback)
    assert ticket.cancel() and not ticket.cancel()
    del callback
    gc.collect()
    assert ref() is None
    assert not agent.has_runnable_tasks() and not agent.run_turn()
    started = []
    ticket = left.post_task(lambda: started.append(ticket.cancel()))
    assert agent.run_turn()
    assert started == [False] and not ticket.pending


def test_navigation_invalidates_document_work_but_preserves_internal_cleanup():
    agent, left, right = lanes()
    seen = []
    stale = left.post_task(lambda: seen.append('stale'), document_bound=True)
    left.post(lambda: seen.append('cleanup'))
    right.post(lambda: seen.append('sibling'))
    left.bind_scheduler(agent, None, TaskContext(generation=1))
    while agent.run_turn(): pass
    assert seen == ['cleanup', 'sibling'] and not stale.pending


def test_migration_and_lane_close_preserve_sibling_work_and_timer_cleanup():
    first, left, right = lanes()
    second = AgentTaskQueue()
    seen = []
    left.post(lambda: seen.append('moved'))
    right.post(lambda: seen.append('sibling'))
    left.bind_scheduler(second, None, TaskContext(generation=1))
    assert first.run_turn() and second.run_turn()
    assert seen == ['sibling', 'moved']
    left.schedule(lambda: seen.append('cancelled timer'), 0)
    assert second.has_runnable_tasks()
    left.close()
    assert not second.has_runnable_tasks()
    assert not second.run_turn()
    right.post(lambda: seen.append('survived'))
    assert first.run_turn() and seen[-1] == 'survived'


def test_timer_promotion_keeps_owner_and_cancelled_delivery_cannot_steal_new_timer():
    agent, left, right = lanes()
    seen = []
    left.schedule(lambda: seen.append('old'), 0)
    right.post(lambda: seen.append('network'), TaskSource.NETWORKING)
    assert agent.run_turn() and seen == ['network']
    left.cancel_timers()
    right.post(lambda: seen.append('before-new'), TaskSource.TIMER)
    left.schedule(lambda: seen.append('new'), 0)
    while agent.run_turn(): pass
    assert seen == ['network', 'before-new', 'new']


def test_concurrent_post_close_has_no_residual_records():
    for _ in range(5):
        agent, lane, _ = lanes()
        barrier = threading.Barrier(2)
        def producer():
            barrier.wait()
            for _ in range(100): lane.post(lambda: None)
        thread = threading.Thread(target=producer)
        thread.start();barrier.wait();lane.close();thread.join(2)
        assert not thread.is_alive()
        assert not agent.has_runnable_tasks() and not agent.pending()
        assert not lane.post(lambda: None)


def test_standalone_queue_tickets_cancel_and_release_on_close():
    lane = TaskQueue()
    class Callback:
        def __call__(self): raise AssertionError('closed callback ran')
    callback = Callback()
    ref = weakref.ref(callback)
    ticket = lane.post_task(callback)
    del callback
    lane.close();gc.collect()
    assert not ticket.pending and ref() is None
    assert not lane.run_turn()


def test_initial_identity_can_be_set_before_host_activation_without_reopening_close():
    class StartingHost(Host):
        active = False

        def is_closed(self):
            return not self.active

    host = StartingHost()
    lane = TaskQueue(host)
    identity = TaskContext(generation=1)
    lane.set_task_context(identity)
    assert not lane.post(lambda: None)
    host.active = True
    seen = []
    lane.post_task(lambda: seen.append('stale'), document_bound=True)
    lane.set_task_context(TaskContext(generation=2))
    lane.post_task(lambda: seen.append('live'), document_bound=True)
    assert lane.run_turn() and seen == ['live']
    assert not lane.run_turn()
    lane.close()
    lane.set_task_context(identity)
    assert not lane.post(lambda: None) and not lane.run_turn()


def test_prebind_records_and_timer_tickets_move_to_shared_selector():
    lane = TaskQueue()
    seen = []
    cancelled = lane.post_task(lambda: seen.append('cancelled'))
    lane.post(lambda: seen.append('moved'))
    lane.schedule(lambda: seen.append('timer'), 0)
    agent = AgentTaskQueue()
    lane.bind_scheduler(agent, None, TaskContext())
    assert cancelled.cancel()
    while agent.run_turn():
        pass
    assert seen == ['moved', 'timer']
    assert not agent.pending() and not lane.has_runnable_tasks()


def test_executor_rechecks_a_deadline_published_during_the_sleep_scan(monkeypatch):
    executor = RealmExecutor(auto_pump=True)
    queue = TaskQueue()
    done, armed = threading.Event(), threading.Event()
    next_deadline = executor._next_deadline
    def scan():
        old = next_deadline()
        if armed.is_set():
            armed.clear()
            # Publish after reading the old hint. The wake generation must
            # prevent parking on that stale hint, including when it was None.
            queue.schedule(done.set, 0.05)
        return old
    try:
        executor.attach_task_queue(queue)
        def install():
            monkeypatch.setattr(executor, '_next_deadline', scan)
            armed.set()
        executor.call(install)
        assert done.wait(1)
    finally:
        queue.close();executor.close()


def test_timer_candidates_skip_empty_lanes_and_keep_attachment_ties(monkeypatch):
    agent, left, right = lanes()
    idle = TaskQueue()
    idle.bind_scheduler(agent, None, TaskContext())
    def empty_scan():
        raise AssertionError('timer-free lane was scanned')
    monkeypatch.setattr(idle, '_has_due_timer', empty_scan)
    monkeypatch.setattr(idle, 'next_deadline', empty_scan)
    monkeypatch.setattr('pyv8c.foundation.tasks._now', lambda: 100.0)
    seen = []
    right.schedule(lambda: seen.append('right'), 0)
    left.schedule(lambda: seen.append('left'), 0)
    assert agent.has_runnable_tasks() and agent.next_deadline() == 0
    assert agent.run_turn() and agent.run_turn()
    assert seen == ['left', 'right']
    assert not agent._timer_lanes
    assert not agent.has_runnable_tasks() and agent.next_deadline() is None


def test_timer_candidates_follow_cancel_rearm_rebind_reset_and_close():
    agent, left, right = lanes()
    seen = []
    first = left.schedule(lambda: seen.append('cancelled'), 0)
    second = left.schedule(lambda: seen.append('later'), 100)
    left.cancel(first)
    assert not agent.has_runnable_tasks() and left in agent._timer_lanes
    left.cancel(second)
    assert not agent._timer_lanes and agent.next_deadline() is None
    def repeat():
        seen.append('repeat')
        left.cancel(ident)
        left.schedule(lambda: seen.append('new'), 0)
    ident = left.schedule(repeat, 0, repeat=True)
    assert agent.run_turn() and left in agent._timer_lanes
    target = AgentTaskQueue()
    left.bind_scheduler(target, None, TaskContext())
    assert left not in agent._timer_lanes and left in target._timer_lanes
    assert target.run_turn() and seen == ['repeat', 'new']
    assert not target._timer_lanes
    left.schedule(lambda: None, 100)
    left.cancel_timers()
    assert not target._timer_lanes
    right.schedule(lambda: None, 100)
    right.close()
    assert not agent._timer_lanes
    left.schedule(lambda: None, 100)
    target.close()
    left.cancel_timers()
    assert not target._timer_lanes


def test_timer_notification_rechecks_heap_after_concurrent_reset_and_rebind(monkeypatch):
    agent, left, _ = lanes()
    target = AgentTaskQueue()
    paused, release = threading.Event(), threading.Event()
    original = left._timers_changed
    def delayed():
        paused.set()
        assert release.wait(2)
        original()
    monkeypatch.setattr(left, '_timers_changed', delayed)
    producer = threading.Thread(target=lambda: left.schedule(lambda: None, 100))
    producer.start()
    try:
        assert paused.wait(2)
        # Attach imports a timer whose producer has not notified yet.
        left.bind_scheduler(target, None, TaskContext())
        assert left in target._timer_lanes and left not in agent._timer_lanes
        monkeypatch.setattr(left, '_timers_changed', original)
        left.cancel_timers()
        release.set()
        producer.join(2)
        assert not producer.is_alive()
        assert not target._timer_lanes and target.next_deadline() is None
    finally:
        release.set()
        producer.join(2)


def test_running_interval_survives_index_refresh_inside_its_callback():
    agent, lane, _ = lanes()
    seen = []
    def callback():
        seen.append('tick')
        lane.cancel(0)  # no heap entry while the live interval is executing
        if len(seen) == 2:
            lane.cancel(ident)
    ident = lane.schedule(callback, 0, repeat=True)
    assert agent.run_turn()
    assert agent.run_turn()
    assert seen == ['tick', 'tick']
    assert not agent._timer_lanes and not agent.run_turn()
