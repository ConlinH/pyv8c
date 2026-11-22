from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from pyv8c.network.backend import NetworkBackend
from pyv8c.network.models import NetworkRequest


@pytest.mark.parametrize("outcome", ["result", "failure", "refused"])
def test_transport_submission_balances_the_shared_ledger(outcome):
    counts = []
    tasks = SimpleNamespace(
        begin_network=lambda stop: counts.append(1),
        complete_network=lambda stop: counts.append(-1),
    )
    future = Future()
    error = OSError("HTTP failed")

    def submit(request, handle, remaining):
        if outcome == "refused":
            raise error
        return future

    backend = NetworkBackend(http=SimpleNamespace(submit_request=submit))
    job = backend.submit(tasks, "page", NetworkRequest("http://fixture.test/"), lambda: None)
    if outcome != "refused":
        assert counts == [1]
        if outcome == "result":
            future.set_result("response")
        else:
            future.set_exception(error)
    if outcome == "result":
        assert job.result.result() == "response"
    else:
        assert job.result.exception() is error
    assert counts == [1, -1]


def test_submission_must_return_a_transport_owned_future():
    active = set()
    tasks = SimpleNamespace(begin_network=active.add, complete_network=active.remove)

    async def invalid_submission(request, handle, remaining):
        return None

    backend = NetworkBackend(http=SimpleNamespace(submit_request=invalid_submission))
    job = backend.submit(tasks, "page", NetworkRequest("http://fixture.test/"), lambda: None)
    with pytest.raises(TypeError, match="concurrent.futures.Future"):
        job.result.result()
    assert not active


class _ClosableEngine:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def close(self):
        self.calls += 1
        if self.error is not None:
            raise self.error


def test_backend_close_reports_the_first_failure_and_closes_every_engine():
    http = _ClosableEngine(RuntimeError("first engine failed"))
    ws = _ClosableEngine(ValueError("second engine failed"))
    backend = NetworkBackend(http=http, ws=ws)
    with pytest.raises(RuntimeError, match="first engine failed"):
        backend.close()
    assert http.calls == 1 and ws.calls == 1, "one failure must not strand the other"
    assert backend._closed is False


def test_backend_close_retries_only_the_failed_engine_and_is_then_idempotent():
    good = _ClosableEngine()
    bad = _ClosableEngine(RuntimeError("engine close failed"))
    backend = NetworkBackend(http=good, ws=bad)
    with pytest.raises(RuntimeError, match="engine close failed"):
        backend.close()
    assert backend.http is good and backend.ws is bad, "references stay owned"
    bad.error = None
    backend.close()
    assert good.calls == 1, "a retry does not close the healthy engine again"
    assert bad.calls == 2
    assert backend.http is None and backend.ws is None
    assert backend._closed is True
    backend.close()
    assert good.calls == 1 and bad.calls == 2


def test_backend_close_keeps_a_failed_additional_role_for_retry():
    extra = _ClosableEngine(RuntimeError("extra failed"))
    backend = NetworkBackend(http=_ClosableEngine(), extra=extra)
    with pytest.raises(RuntimeError, match="extra failed"):
        backend.close()
    assert backend.engine("extra") is extra
    extra.error = None
    backend.close()
    assert backend.engine("extra") is None


def test_backend_close_keeps_ownership_when_a_control_exception_interrupts():
    engine = _ClosableEngine(KeyboardInterrupt())
    backend = NetworkBackend(http=engine)
    with pytest.raises(KeyboardInterrupt):
        backend.close()
    assert backend._closed is False
    assert backend.http is engine
    engine.error = None
    backend.close()
    assert engine.calls == 2
    assert backend._closed is True


def test_backend_preserves_ownership_when_close_returns_a_coroutine():
    async def release():
        raise AssertionError("an invalid close must never run as an async hook")

    engine = SimpleNamespace(close=lambda: release())
    backend = NetworkBackend(http=engine)
    with pytest.raises(TypeError, match=r"close\(\) must return None"):
        backend.close()
    assert backend.http is engine
    assert not backend._closed

    engine.close = lambda: None
    backend.close()
    assert backend._closed
