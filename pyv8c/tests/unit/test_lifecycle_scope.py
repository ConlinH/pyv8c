import threading

from pyv8c.foundation.lifecycle import LifecycleState, ResourceScope


def test_resource_scope_runs_cleanup_in_reverse_order_once():
    scope = ResourceScope()
    calls = []
    scope.add(lambda: calls.append("first"))
    scope.add(lambda: calls.append("second"))
    scope.close()
    scope.close()
    assert calls == ["second", "first"]
    assert scope.state is LifecycleState.CLOSED


def test_resource_scope_registration_after_close_runs_immediately():
    scope = ResourceScope()
    scope.close()
    calls = []
    assert scope.add(lambda: calls.append("late")) is False
    assert calls == ["late"]


def test_resource_scope_begin_close_is_single_winner():
    scope = ResourceScope()
    values = []

    def close():
        values.append(scope.begin_close())

    threads = [threading.Thread(target=close) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(value is not None for value in values) == 1


def test_resource_scope_can_unregister_completed_resource():
    scope = ResourceScope()
    calls = []
    callback = lambda: calls.append("cancel")
    assert scope.add(callback) is True
    assert scope.remove(callback) is True
    assert scope.remove(callback) is False
    scope.close()
    assert calls == []
