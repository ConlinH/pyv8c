"""Both built-in HTTP engines share one cancellable delivery skeleton."""
import asyncio
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pyv8c.foundation.host_errors import HostStateError, LoadFailure
from pyv8c.network import registry
from pyv8c.network.assembly import assemble_backend
from pyv8c.network.async_delivery import AsyncTransport, _AsyncAbortToken
from pyv8c.network.backend import NetworkBackend
from pyv8c.network.cronet import CronetTransport
from pyv8c.network.curl_cffi import CurlCffiTransport
from pyv8c.network.models import NetworkRequest, NetworkUnavailableError, RequestHandle

ENGINES = ("curl_cffi", "cronet")


def _raw(url):
    body = b'{"custom":"browser"}'
    return SimpleNamespace(content=body, text=body.decode(), status_code=200,
                           reason="OK", url=url, headers={}, history=(), elapsed=None)


def _stalled_session(calls, sessions):
    """One async client whose ``/hang`` call never produces a response byte."""

    class Client:
        def __init__(self, **options):
            self.options, self.closed = options, False
            sessions.append(self)

        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/hang"):
                await asyncio.Event().wait()
            return _raw(url)

        async def close(self):
            self.closed = True

    return Client


@pytest.fixture
def fake_curl(monkeypatch):
    calls, sessions = [], []
    module = SimpleNamespace(AsyncSession=_stalled_session(calls, sessions))
    monkeypatch.setitem(sys.modules, "curl_cffi", SimpleNamespace(requests=module))
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", module)
    return calls, sessions


@pytest.fixture
def fake_cronet(monkeypatch):
    calls, sessions = [], []
    monkeypatch.setitem(
        sys.modules, "cycronet",
        SimpleNamespace(AsyncCronetClient=_stalled_session(calls, sessions)))
    return calls, sessions


@pytest.fixture(params=ENGINES)
def engine(request):
    """Each test below runs against both engine names and their own fake client."""
    name = request.param
    calls, sessions = (request.getfixturevalue(
        "fake_curl" if name == "curl_cffi" else "fake_cronet"))
    transport = registry.create_transport(name)
    try:
        yield name, transport, calls, sessions
    finally:
        transport.close()


def _backend(name):
    browser = SimpleNamespace(
        real_network=True, http_engine=name, ws_engine=None,
        http_options={}, ws_options={},
    )
    return assemble_backend(browser, proxy=None)


def _tasks():
    counts = []
    tasks = SimpleNamespace(
        begin_network=lambda stop: counts.append(1),
        complete_network=lambda stop: counts.append(-1),
        closed=False,
    )
    return tasks, counts


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_the_two_names_are_the_only_built_in_http_engines(fake_curl, fake_cronet):
    # A host may register probe engines in the same process; only the built-in
    # names and the absence of the removed opt-in siblings are ours to assert.
    http = set(registry.transport_names("http"))
    assert set(ENGINES) <= http
    assert not {"curl_cffi_async", "cronet_async"} & http
    assert "websockets" in registry.transport_names("ws")
    assert "cronet" not in registry.transport_names("ws")
    assert registry.accepts_proxy("curl_cffi") is True
    assert registry.accepts_proxy("cronet") is True
    assert not hasattr(registry, "supports_abort")
    # One skeleton, two protocol implementations; no opt-in sibling remains.
    assert issubclass(CurlCffiTransport, AsyncTransport)
    assert issubclass(CronetTransport, AsyncTransport)
    assert CurlCffiTransport.submit_request is AsyncTransport.submit_request
    assert CronetTransport.submit_request is AsyncTransport.submit_request
    assert CronetTransport.FOLLOWS_ITSELF is False


@pytest.mark.parametrize("name", ENGINES)
def test_a_missing_client_names_the_optional_dependency(name, monkeypatch):
    module = "curl_cffi.requests" if name == "curl_cffi" else "cycronet"
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(NetworkUnavailableError, match="optional"):
        registry.create_transport(name)


@pytest.mark.parametrize("name", ENGINES)
def test_an_installed_client_without_its_async_api_is_named(name, monkeypatch):
    module = "curl_cffi.requests" if name == "curl_cffi" else "cycronet"
    monkeypatch.setitem(sys.modules, module, SimpleNamespace())
    expected = "AsyncSession" if name == "curl_cffi" else "AsyncCronetClient"
    with pytest.raises(NetworkUnavailableError, match=expected):
        registry.create_transport(name)


@pytest.mark.parametrize("name", ENGINES)
def test_a_refused_construction_releases_the_loop_thread(name, monkeypatch):
    module = "curl_cffi.requests" if name == "curl_cffi" else "cycronet"
    key = "AsyncSession" if name == "curl_cffi" else "AsyncCronetClient"

    class Refused:
        def __init__(self, **options):
            raise TypeError("loop is not an option")

    monkeypatch.setitem(sys.modules, module, SimpleNamespace(**{key: Refused}))
    loops_before = sum(thread.name.endswith("-loop")
                       for thread in threading.enumerate())
    with pytest.raises(TypeError, match="was refused"):
        registry.create_transport(name)
    loops_after = sum(thread.name.endswith("-loop")
                      for thread in threading.enumerate())
    assert loops_after == loops_before, (
        "a failed constructor must not leave its loop or pool behind")


@pytest.mark.parametrize("name", ENGINES)
def test_assembly_installs_the_engine_and_closes_it_once(name, request):
    calls, _ = request.getfixturevalue(
        "fake_curl" if name == "curl_cffi" else "fake_cronet")
    backend = _backend(name)
    transport = backend.http
    assert isinstance(transport, AsyncTransport)

    backend.close()
    assert backend._closed is True and backend.http is None
    assert transport.loop.is_closed()
    assert not transport._thread.is_alive()
    backend.close()
    assert transport._closed is True


@pytest.mark.parametrize("name", ENGINES)
def test_the_contexts_proxy_reaches_the_client(name, request):
    calls, sessions = request.getfixturevalue(
        "fake_curl" if name == "curl_cffi" else "fake_cronet")
    browser = SimpleNamespace(
        real_network=True, http_engine=name, ws_engine=None,
        http_options={}, ws_options={},
    )
    backend = assemble_backend(browser, proxy="http://proxy.test:3128")
    try:
        transport = backend.http
        if name == "curl_cffi":
            assert transport.proxies == {"http": "http://proxy.test:3128",
                                         "https": "http://proxy.test:3128"}
        else:
            assert transport.session.options["proxies"] == "http://proxy.test:3128"
    finally:
        backend.close()


def test_both_engines_share_one_delivery_skeleton(fake_curl, fake_cronet):
    curl = registry.create_transport("curl_cffi")
    cronet = registry.create_transport("cronet")
    try:
        for transport in (curl, cronet):
            assert type(transport).submit_request is AsyncTransport.submit_request
            assert type(transport).close is AsyncTransport.close
        assert curl.loop is not cronet.loop, "each context owns its own loop"
    finally:
        curl.close()
        cronet.close()


@pytest.mark.parametrize("before_attach", [True, False])
async def test_a_cancel_before_or_after_attach_interrupts_the_task(before_attach):
    async def blocked():
        await asyncio.Future()

    task = asyncio.create_task(blocked())
    token = _AsyncAbortToken()
    if before_attach:
        token.cancel()
    token.attach(task)
    if not before_attach:
        token.cancel()
    assert token.reason == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await task


def test_an_early_interrupt_never_starts_a_request(engine):
    _, transport, calls, _ = engine
    request = NetworkRequest(url="http://fixture.test/echo")
    handle = RequestHandle()
    handle.cancel()
    with pytest.raises(LoadFailure, match="was aborted"):
        transport.submit_request(request, handle, lambda: None)
    assert calls == [], "no request is fabricated after a cancel"


def test_a_stalled_socket_wait_is_cancelled_by_abort(engine):
    """No response-body chunk exists; only a real coroutine cancel can stop this."""
    _, transport, calls, sessions = engine
    backend = NetworkBackend(http=transport)
    tasks, counts = _tasks()
    request = NetworkRequest(url="http://fixture.test/hang", timeout=30)
    job = backend.submit(tasks, 1, request, lambda: None)
    handle = job.handle
    assert _wait_for(lambda: bool(calls)), "the call reached the client"
    assert handle.cancel() is True
    error = job.result.exception(timeout=2)
    assert isinstance(error, LoadFailure)
    assert "was aborted" in str(error) and "failed:" not in str(error), (
        "a concurrent CancelledError is an abort, not a transport failure")
    assert counts == [1, -1]
    assert not sessions[-1].closed, "the session survives a cancelled call"


@pytest.mark.parametrize("first", ["cancel", "deadline"])
def test_interrupt_keeps_the_request_owned_until_coroutine_cleanup_finishes(engine, monkeypatch, first):
    _, transport, _, _ = engine
    running, cleaning, release, cleaned = (threading.Event() for _ in range(4))

    async def hold(request):
        running.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            cleaned.set()

    monkeypatch.setattr(transport, "_single_call", hold)
    tasks, counts = _tasks()
    backend = NetworkBackend(http=transport)
    job = backend.submit(tasks, 1, NetworkRequest(url="http://fixture.test/"), lambda: .15)
    try:
        assert running.wait(2)
        if first == "cancel":
            job.cancel()
        assert cleaning.wait(2)
        if first == "deadline":
            job.cancel()
        asyncio.run_coroutine_threadsafe(asyncio.sleep(.2), transport.loop).result(2)
        assert counts == [1]
        assert not job.result.done()
    finally:
        release.set()
    assert _wait_for(lambda: counts == [1, -1])
    error = job.result.exception(timeout=2)
    from pyv8c import RequestTimeout
    assert isinstance(error, RequestTimeout if first == "deadline" else LoadFailure)
    assert cleaned.is_set()


def test_close_budget_covers_default_executor_and_can_be_retried(engine, monkeypatch):
    from pyv8c.network import async_delivery

    _, transport, _, _ = engine
    running, release = threading.Event(), threading.Event()
    threads = []

    def resolve(*args, **kwargs):
        threads.append(threading.current_thread())
        running.set()
        assert release.wait(3)
        return []

    async def hold(request):
        await transport.loop.getaddrinfo("fixture.test", 80)

    monkeypatch.setattr("socket.getaddrinfo", resolve)
    monkeypatch.setattr(transport, "_single_call", hold)
    backend = NetworkBackend(http=transport)
    tasks, counts = _tasks()
    job = backend.submit(tasks, 1, NetworkRequest("http://fixture.test/"), lambda: None)
    try:
        assert running.wait(2)
        with monkeypatch.context() as short:
            short.setattr(async_delivery, "_CLOSE_TIMEOUT", 0.05)
            with pytest.raises(TimeoutError):
                backend.close()
        assert isinstance(job.result.exception(timeout=2), LoadFailure)
        assert counts == [1, -1]
        shutdown = transport._shutdown
        assert shutdown is not None and not shutdown.done()
        assert backend.http is transport
        assert threads[0].is_alive() and not transport.loop.is_closed()
    finally:
        release.set()
    backend.close()
    assert transport._shutdown is shutdown
    assert transport.loop.is_closed() and not threads[0].is_alive()


def test_engine_close_budget_keeps_running_cleanup_owned_for_retry(engine, monkeypatch):
    from pyv8c.network import async_delivery

    _, transport, _, sessions = engine
    running, cleaning, release = (threading.Event() for _ in range(3))

    async def hold(request):
        running.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            while not release.is_set():
                await asyncio.sleep(0.005)

    monkeypatch.setattr(transport, "_single_call", hold)
    backend = NetworkBackend(http=transport)
    tasks, counts = _tasks()
    backend.submit(tasks, 1, NetworkRequest(url="http://fixture.test/"), lambda: None)
    try:
        assert running.wait(2)
        with monkeypatch.context() as short:
            short.setattr(async_delivery, "_CLOSE_TIMEOUT", 0.05)
            with pytest.raises(TimeoutError, match="requests did not finish"):
                backend.close()
        assert cleaning.wait(2)
        assert counts == [1] and backend.http is transport
        assert not sessions[-1].closed
    finally:
        release.set()
    backend.close()
    assert counts == [1, -1]
    assert sessions[-1].closed


@pytest.mark.parametrize("name", ENGINES)
def test_close_cancels_a_stalled_call_before_stopping_the_loop(name, request):
    calls, _ = request.getfixturevalue(
        "fake_curl" if name == "curl_cffi" else "fake_cronet")
    transport = registry.create_transport(name)
    backend = NetworkBackend(http=transport)
    tasks, counts = _tasks()
    job = backend.submit(tasks, 1, NetworkRequest(url="http://fixture.test/hang", timeout=30), lambda: 30)
    assert _wait_for(lambda: bool(calls))
    started = time.perf_counter()
    backend.close()
    assert time.perf_counter() - started < 3, "close must not wait for the 30s timeout"
    assert counts == [1, -1]
    assert isinstance(job.result.exception(timeout=2), LoadFailure)
    assert transport.loop.is_closed() and not transport._thread.is_alive()
    backend.close()


def test_a_failed_close_stays_retryable_through_the_backend(engine, monkeypatch):
    _, transport, _, _ = engine
    backend = NetworkBackend(http=transport)
    attempts = []
    original = transport._stop_loop

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("loop stop failed")
        return original()

    monkeypatch.setattr(transport, "_stop_loop", flaky)
    with pytest.raises(RuntimeError, match="loop stop failed"):
        backend.close()
    assert backend._closed is False and backend.http is transport
    assert transport._closed is False and not transport.loop.is_closed()

    backend.close()
    assert backend._closed is True and backend.http is None
    assert transport.loop.is_closed() and not transport._thread.is_alive()
    assert len(attempts) == 2, "the retry runs the teardown again"


def test_a_half_stopped_engine_refuses_new_calls(engine, monkeypatch):
    _, transport, calls, _ = engine
    backend = NetworkBackend(http=transport)
    attempts = []
    original = transport._stop_loop

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("loop stop failed")
        return original()

    monkeypatch.setattr(transport, "_stop_loop", flaky)
    with pytest.raises(RuntimeError, match="loop stop failed"):
        backend.close()
    with pytest.raises(HostStateError, match="closed"):
        transport.submit_request(NetworkRequest(url="http://fixture.test/echo"), RequestHandle(), lambda: None)
    assert calls == []

    backend.close()
    assert backend._closed is True and len(attempts) == 2
