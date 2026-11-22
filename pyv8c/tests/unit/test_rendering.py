import pytest

from pyv8c.foundation.rendering import RenderingScheduler


def setup_frames():
    now = [10.0]
    scheduler = RenderingScheduler(frame_rate=10, now=lambda: now[0])
    results = []
    def lane(active=lambda: True, order=lambda: ()):
        return scheduler.add(invoke=lambda callback, *args: callback(*args),
                             timestamp=lambda stamp: stamp, active=active,
                             report_error=results.append, order=order)
    return now, scheduler, lane, results


def test_shared_grid_and_late_frames_do_not_replay_missed_frames():
    now, scheduler, lane, results = setup_frames()
    a, b = lane(), lane()
    a.request(lambda stamp: results.append(('a', stamp)))
    now[0] += .05
    b.request(lambda stamp: results.append(('b', stamp)))
    assert not scheduler.run_frame()
    now[0] += .50
    assert scheduler.run_frame()
    assert [name for name, stamp in results] == ['a', 'b']
    assert results[0][1] == results[1][1] == pytest.approx(10.5)
    assert scheduler.next_deadline() is None
    a.request(lambda stamp: results.append(('next', stamp)))
    assert 0 < scheduler.next_deadline() <= .1
    assert not scheduler.run_frame()


def test_hidden_pending_frames_do_not_wake_and_resume_on_grid():
    now, scheduler, lane, results = setup_frames()
    visible = [False]
    a = lane(active=lambda: visible[0])
    a.request(results.append)
    assert scheduler.next_deadline() is None
    now[0] += 5
    assert not scheduler.run_frame()
    visible[0] = True
    scheduler.changed()
    assert scheduler.next_deadline() > 0
    now[0] += .11
    assert scheduler.run_frame()
    assert len(results) == 1


def test_document_snapshot_order_and_late_child_requests():
    now, scheduler, lane, results = setup_frames()
    child = lane(order=lambda: (1,))
    parent = lane(order=lambda: ())
    child.request(lambda stamp: results.append('child'))
    def first(stamp):
        results.append('parent')
        child.request(lambda stamp: results.append('child-added'))
        parent.request(lambda stamp: results.append('parent-next'))
    parent.request(first)
    now[0] += .11
    scheduler.run_frame()
    assert results == ['parent', 'child', 'child-added']
    now[0] += .1
    scheduler.run_frame()
    assert results[-1] == 'parent-next'


def test_idle_child_first_requested_by_parent_waits_for_next_frame():
    now, scheduler, lane, results = setup_frames()
    child = lane(order=lambda: (1,))
    parent = lane()
    parent.request(lambda stamp: child.request(results.append))
    now[0] += .11
    scheduler.run_frame()
    assert results == []
    now[0] += .1
    scheduler.run_frame()
    assert results == [10.2]


def test_cancel_and_close_during_frame_remove_callbacks_and_deadlines():
    now, scheduler, lane, results = setup_frames()
    a, b = lane(), lane()
    def first(stamp):
        a.cancel(second)
        b.close()
    a.request(first)
    second = a.request(results.append)
    b.request(results.append)
    now[0] += .11
    scheduler.run_frame()
    assert results == []
    assert scheduler.next_deadline() is None
    scheduler.close()
    assert not scheduler._lanes
    assert a.request(results.append) == 0
    with pytest.raises(RuntimeError, match='closed'):
        lane()


def test_host_idle_query_does_not_rewrite_deadlines():
    now, scheduler, lane, results = setup_frames()
    owner = object()
    a = scheduler.add(invoke=lambda callback, *args: callback(*args),
                      active=lambda: True, timestamp=lambda stamp: stamp,
                      report_error=results.append, owner=owner)
    a.request(results.append)
    deadline = scheduler._due
    assert not scheduler.has_runnable_tasks({owner})
    assert scheduler._due == deadline
    now[0] += .11
    assert scheduler.has_runnable_tasks({owner})
    assert scheduler._due == deadline


def test_request_index_survives_reentry_and_cleans_terminal_paths():
    now, scheduler, lane, results = setup_frames()
    a, b = lane(), lane()
    def first(stamp):
        assert a not in scheduler._pending_lanes
        a.request(results.append)
        b.cancel(second)
    a.request(first)
    second = b.request(results.append)
    now[0] += .11
    scheduler.run_frame()
    assert set(scheduler._pending_lanes) == {a}
    now[0] += .1
    scheduler.run_frame()
    assert not scheduler._pending_lanes
    a.request(results.append)
    scheduler.cancel_owner(a.owner)
    assert not scheduler._pending_lanes
    scheduler.close()
    assert not scheduler._pending_lanes


@pytest.mark.parametrize('rate', [0, -1, float('inf'), float('nan')])
def test_invalid_frame_rate(rate):
    with pytest.raises(ValueError, match='frame_rate'):
        RenderingScheduler(frame_rate=rate)
