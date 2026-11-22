"""A transport owns completion even after an observer cancels."""

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from pyv8c.network.assembly import assemble_backend
from pyv8c.network.backend import NetworkBackend
from pyv8c.network.models import NetworkRequest, RequestHandle
from pyv8c.foundation.load_handle import LoadHandle


@pytest.mark.parametrize("early", [False, True])
def test_cancellation_reaches_the_registered_hook_once(early):
    stopped = []
    handle = RequestHandle()
    if early:
        assert not handle.cancel()
    handle.attach_abort(lambda: stopped.append(True))
    if not early:
        assert handle.cancel()
    assert not handle.cancel()
    assert handle.cancelled and stopped == [True]


def test_completed_hop_releases_its_cancel_hook():
    stopped = []

    def submit(request, handle, remaining):
        handle.attach_abort(lambda: stopped.append(True))
        future = Future()
        future.set_result("reply")
        return future

    tasks = SimpleNamespace(begin_network=lambda stop: None, complete_network=lambda stop: None)
    backend = NetworkBackend(http=SimpleNamespace(submit_request=submit))
    job = backend.submit(tasks, "page", NetworkRequest("http://fixture.test/"), lambda: None)
    assert job.result.result() == "reply"
    assert not job.handle.cancel()
    assert not stopped


@pytest.mark.parametrize("raises", [False, True])
def test_interruption_leaves_completion_owned_until_the_transport_exits(raises):
    counts, stopped = [], []
    tasks = SimpleNamespace(
        begin_network=lambda stop: counts.append(1),
        complete_network=lambda stop: counts.append(-1),
    )
    future = Future()

    def submit(request, handle, remaining):
        def interrupt():
            stopped.append(True)
            if raises:
                raise OSError("interrupt failed")

        handle.attach_abort(interrupt)
        return future

    backend = NetworkBackend(http=SimpleNamespace(submit_request=submit))
    job = backend.submit(tasks, "page", NetworkRequest("http://fixture.test/"), lambda: None)
    job.cancel()
    job.cancel()
    assert stopped == [True]
    assert counts == [1]
    assert not job.result.done()

    future.set_result("reply")
    assert counts == [1, -1]
    assert job.result.result() == "reply"


def test_nested_load_handles_pass_late_cancellation_to_the_future():
    inner, outer = LoadHandle(), LoadHandle()
    outer.bind_transport(inner)
    assert outer.cancel()
    future = Future()
    inner.bind_transport(future)
    assert future.cancelled()
    assert not outer.cancel()


def test_assembly_rejects_an_engine_without_async_submission_and_closes_the_factory_instance():
    closed = []

    class Engine:
        def close(self):
            closed.append(True)

    browser = SimpleNamespace(
        real_network=True, http_engine=Engine, ws_engine=None, http_options={}, ws_options={},
    )
    with pytest.raises(TypeError, match="submit_request"):
        assemble_backend(browser)
    assert closed == [True]


@pytest.mark.parametrize("kind", ["missing", "coroutine", "async-generator"])
def test_assembly_requires_a_synchronous_http_close(kind):
    async def async_close():
        raise AssertionError("closing a rejected engine should not start async cleanup")

    async def async_generator():
        yield None

    engine = SimpleNamespace(submit_request=lambda request, handle, remaining: Future())
    if kind != "missing":
        engine.close = async_close if kind == "coroutine" else async_generator
    browser = SimpleNamespace(
        real_network=True, http_engine=lambda: engine, ws_engine=None, http_options={}, ws_options={},
    )
    with pytest.raises(TypeError, match=r"synchronous close\(\)"):
        assemble_backend(browser)
