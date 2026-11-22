"""Result snapshots and exceptions obey the same script/microtask boundaries."""

import pytest

from pyv8c import pyv8


@pytest.fixture
def settle_page():
    return lambda page: page.run_until_idle(timeout=0.1)


async def test_primitive_evaluation_owns_one_checkpoint(page):
    endpoint = page.realm.executor
    agent = endpoint.agent
    original = agent.microtasks
    calls = []

    class CountingQueue:
        def checkpoint(self, **kwargs):
            calls.append(None)
            return original.checkpoint(**kwargs)

        def __getattr__(self, name):
            return getattr(original, name)

    try:
        endpoint.call(lambda: setattr(agent, "microtasks", CountingQueue()))
        before = len(calls)
        for _ in range(20):
            assert page.evaluate("1+1") == 2
        assert len(calls) - before == 20
    finally:
        endpoint.call(lambda: setattr(agent, "microtasks", original))


async def test_result_is_copied_after_promise_jobs_finish(page):
    assert page.evaluate("""(() => {
      const value={answer:0}; Promise.resolve().then(()=>value.answer=42); return value;
    })()""") == {"answer": 42}


async def test_snapshot_getters_finish_their_jobs_before_returning(page):
    assert page.evaluate("""globalThis.snapshotDone=0; ({get answer(){
      queueMicrotask(()=>snapshotDone=42); return 2;
    }})""") == {"answer": 2}
    # A raw read captures the value before this new host operation checkpoints.
    assert page.executor.call(page.v8_context.exec_js, "snapshotDone") == 42


async def test_throwing_script_still_checkpoints_and_next_call_recovers(page):
    with pytest.raises(pyv8.JSException):
        page.evaluate(
            'globalThis.failedDone=0; queueMicrotask(()=>failedDone=42); throw Error("bad")'
        )
    assert page.executor.call(page.v8_context.exec_js, "failedDone") == 42
    assert page.evaluate("1+1") == 2


async def test_handle_evaluation_runs_its_script_checkpoint(page):
    handle = page.evaluate_handle("""globalThis.handleDone=0;
      queueMicrotask(()=>handleDone=42); ({answer:2})""")
    assert isinstance(handle, pyv8.JSObject)
    assert page.executor.call(page.v8_context.exec_js, "handleDone") == 42


async def test_snapshot_failure_still_finishes_getter_jobs(page, monkeypatch):
    from pyv8c.platform import page as page_module

    def fail_snapshot(value, seen):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(page_module, "_snapshot_data", fail_snapshot)
    with pytest.raises(RuntimeError, match="snapshot failed"):
        page.evaluate("""globalThis.getterDone=0; ({get answer(){
          queueMicrotask(()=>getterDone=42); return 2;
        }})""")
    assert page.executor.call(page.v8_context.exec_js, "getterDone") == 42


async def test_throwing_snapshot_getter_preserves_its_exception_and_finishes_jobs(page):
    with pytest.raises(pyv8.JSException):
        page.evaluate("""globalThis.throwingGetterDone=0; ({get answer(){
          queueMicrotask(()=>throwingGetterDone=42); throw Error('getter failed');
        }})""")
    assert page.executor.call(page.v8_context.exec_js, "throwingGetterDone") == 42


async def test_inspector_cleanup_failure_does_not_skip_script_checkpoint(page):
    class Target:
        def before_script(self):
            pass

        def after_script(self):
            raise RuntimeError("inspector cleanup failed")

    original = page._devtools_target
    try:
        page._devtools_target = Target()
        with pytest.raises(RuntimeError, match="inspector cleanup failed"):
            page.evaluate("globalThis.inspectorDone=0; queueMicrotask(()=>inspectorDone=42)")
    finally:
        page._devtools_target = original
    assert page.executor.call(page.v8_context.exec_js, "inspectorDone") == 42


@pytest.mark.parametrize("method", ["evaluate", "evaluate_handle"])
@pytest.mark.parametrize("failure", ["before_script", "page_closed", "profile_closed"])
async def test_early_script_failure_completes_its_checkpoint(page, method, failure, settle_page):
    from pyv8c.foundation.host_errors import HostStateError

    page.evaluate("window.beforeHookDone=0;window.bodyRan=false")
    endpoint = page.realm.executor
    agent = endpoint.agent
    original_queue = agent.microtasks
    original_target = page._devtools_target
    original_page_closed, original_profile_closed = page.closed, page.profile.closed
    checkpoints = []
    error = TimeoutError("DevTools startup timed out")

    class CountingQueue:
        def checkpoint(self, **kwargs):
            checkpoints.append(None)
            return original_queue.checkpoint(**kwargs)

        def __getattr__(self, name):
            return getattr(original_queue, name)

    class Target:
        def before_script(self):
            page.v8_context.exec_js("queueMicrotask(()=>beforeHookDone=42)")
            raise error

        def after_script(self):
            raise AssertionError("script never started")

    def evaluate_while_closing():
        # Change the state on the owner, after submission, so the task queue
        # cannot reject the command before it reaches the script's guard.
        if failure == "page_closed":
            page.closed = True
        else:
            page.profile.closed = True
        try:
            return getattr(page, method)("window.bodyRan=true;1+1")
        finally:
            page.closed, page.profile.closed = original_page_closed, original_profile_closed

    try:
        endpoint.call(lambda: setattr(agent, "microtasks", CountingQueue()))
        if failure == "before_script":
            page._devtools_target = Target()
        before = len(checkpoints)
        expected = TimeoutError if failure == "before_script" else HostStateError
        with pytest.raises(expected) as caught:
            if failure == "before_script":
                getattr(page, method)("window.bodyRan=true;1+1")
            else:
                endpoint.group.executor.call(
                    endpoint.invoke, evaluate_while_closing, _owns_checkpoint=True
                )
        if failure == "before_script":
            assert caught.value is error
        assert len(checkpoints) - before == 1
        assert not page.execution_group._dirty
        page.closed, page.profile.closed = original_page_closed, original_profile_closed
        page._devtools_target = original_target
        assert await settle_page(page)
        assert page.executor.call(page.v8_context.exec_js, "bodyRan") is False
        if failure == "before_script":
            assert page.executor.call(page.v8_context.exec_js, "beforeHookDone") == 42
        assert page.evaluate("1+1") == 2
    finally:
        page.closed, page.profile.closed = original_page_closed, original_profile_closed
        page._devtools_target = original_target
        endpoint.call(lambda: setattr(agent, "microtasks", original_queue))
