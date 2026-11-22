"""Deterministic regressions for the four concurrency/lifecycle defects.

Functional tests cannot catch these defects because reproducing them needs a
specific interleaving, which almost never occurs in a single happy-path run:

- A1 -- ``DevToolsTarget.close()`` used to resolve its realm owner with a
  single attribute lookup that never succeeds for a Page (the executor lives
  on ``Page.realm``).  ``Debugger.disconnect`` therefore ran on whichever
  thread happened to close the target, breaking the single-owner invariant.
- A2 -- the execution driver must wake when a delivery arrives as it parks.
- A3 -- an exception raised by a ``RealmExecutor.post()`` command has no
  Future to mirror onto and used to vanish inside the owner thread.
- A4 -- a fetch/XHR or document-subresource response that completed before its
  handle was registered left the cancel callback registered for the lifetime
  of the page.

These are marked ``race``; CI re-runs them with ``pytest-repeat --count`` so a
scheduling change cannot hide behind one lucky pass.
"""
from __future__ import annotations

import asyncio

import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.race

from pyv8c.foundation.executor import RealmExecutor
from pyv8c.foundation.load_handle import LoadHandle

pytestmark = [pytest.mark.race, pytest.mark.native]


class _RecordingDebugger:
    """Stand-in for a pyv8.Debugger that records where calls ran."""

    def __init__(self):
        self.disconnect_calls = []
        self.send_calls = []

    def disconnect(self):
        self.disconnect_calls.append(threading.get_ident())

    def cancel_pause_on_next_statement(self):
        pass

    def resume(self):
        pass

    def send(self, data):
        self.send_calls.append(data)


class _PageLike:
    """Mimics a Page: its executor is reachable through ``realm.executor``."""

    def __init__(self, executor):
        self.realm = SimpleNamespace(executor=executor)


def _fake_context(executor):
    def try_inspector(callback, wake):
        callback()
        return True
    return SimpleNamespace(isolate_id=id(executor), _try_inspector=try_inspector)


class _DummyNode:
    """Minimal stand-in for a connected element in the resource path."""

    attributes = {}

    def __init__(self):
        self.events = []

    def fn_dispatchEvent(self, event):
        self.events.append(event.type)
        return True


def _response_for(url):
    return {"status": 200, "url": str(url), "body": "ok", "headers": {"content-type": "text/plain"}}


# -- A1: Debugger.disconnect must run on (or never run off) the owner thread -


async def test_devtools_close_disconnects_on_the_realm_owner_thread():
    """A1: a page target must detach its inspector on the realm owner thread.

    Previously ``close()`` looked for the owner only at ``realm.executor``,
    which never resolves for a Page, so the disconnect ran on the closing
    thread instead of the owner thread that owns V8.
    """
    from pyv8c.pyv8.devtools import DevToolsOptions
    from pyv8c.pyv8.devtools.router import Router
    from pyv8c.pyv8.devtools.scheduler import Host
    from pyv8c.pyv8.devtools.session import Session

    executor = RealmExecutor(name="race-dev-owner")
    try:
        owner_ident = executor.call(threading.get_ident)
        debugger = _RecordingDebugger()
        router = Router(DevToolsOptions())
        target = router.register(Host(_fake_context(executor), executor, lambda: "about:blank", lambda: None))
        session = Session(router, target, None, 1)
        session.native = debugger
        for future in target.close():
            (await asyncio.to_thread(future.result, 2))

        assert debugger.disconnect_calls == [owner_ident], (
            "Debugger.disconnect did not run on the realm owner thread"
        )
        assert not target.diagnostics
    finally:
        executor.close()


async def test_devtools_requires_an_explicit_host_adapter():
    from pyv8c.pyv8.devtools.scheduler import Host

    with pytest.raises(TypeError):
        Host(object())


async def test_devtools_close_skips_disconnect_when_owner_is_already_closed():
    """A1: closing after the owner died must record a diagnostic, not crash."""
    from pyv8c.pyv8.devtools import DevToolsOptions
    from pyv8c.pyv8.devtools.router import Router
    from pyv8c.pyv8.devtools.scheduler import Host
    from pyv8c.pyv8.devtools.session import Session

    executor = RealmExecutor(name="race-dev-dead-owner")
    executor.close()

    debugger = _RecordingDebugger()
    router = Router(DevToolsOptions())
    target = router.register(Host(_fake_context(executor), executor, lambda: "about:blank", lambda: None))
    session = Session(router, target, None, 1)
    session.native = debugger
    futures = target.close()
    for future in futures:
        router.track_cleanup(future)
    with pytest.raises(RuntimeError, match="cleanup failed.*executor is closed"):
        router.finish_cleanup()

    assert debugger.disconnect_calls == []
    assert len(router.diagnostics) == 1


async def test_devtools_page_close_records_no_disconnect_diagnostics(browser):
    """A1: closing a live devtools page detaches on the owner without noise."""
    context = browser.new_profile()
    page = context.new_page()
    handle = context.start_devtools(page)
    try:
        target = context._devtools.targets[page]

        (await page.close())

        assert target.closed
        assert not handle._router.diagnostics
    finally:
        handle.close()


# -- A2: a delivery racing the pump's wait must not be slept past -------


async def test_owner_wait_does_not_sleep_out_a_delivery_that_races_the_wait():
    """A delivery must wake the driver without a nested event-loop pump."""
    executor = RealmExecutor(name="test-race-a2", auto_pump=True)
    from pyv8c.foundation.tasks import TaskQueue

    queue = TaskQueue()
    consumed = []
    asleep = threading.Event()
    finished = threading.Event()
    wait = executor._sleep

    def recording_wait(seconds=None):
        asleep.set()
        wait(seconds)

    executor._sleep = recording_wait
    try:
        executor.call(executor.attach_task_queue, queue)
        assert (await asyncio.to_thread(asleep.wait, 1.0)), "the owner never went to sleep"
        queue.post(lambda: (consumed.append("ok"), finished.set()))
        assert (await asyncio.to_thread(finished.wait, 1.0)), "the delivery did not wake the owner"
        assert consumed == ["ok"]
    finally:
        executor.close()


# -- A3: post() exceptions must surface instead of vanishing -------


async def test_post_exception_reaches_the_realm_error_sink():
    """A3: a fire-and-forget command must not swallow its own exception.

    A declared error reaches the realm's sink; a failure outside the error family is
    the host's own, so it reaches the log instead.  Either way it does not vanish.
    """
    from loguru import logger

    from pyv8c.web.page_errors import PageTypeError
    from pyv8c.foundation.tasks import TaskQueue

    queue = TaskQueue()
    executor = RealmExecutor(name="race-error-sink")
    messages = []
    token = logger.add(lambda message: messages.append(str(message).strip()), format="{message}")
    try:
        executor.attach_task_queue(queue)
        seen = []
        queue.set_error_sink(seen.append)
        assert executor.post(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not messages:
            (await asyncio.sleep(0.01))

        assert any("boom" in line for line in messages), "the host's failure was swallowed"
        assert seen == [], seen

        assert executor.post(lambda: (_ for _ in ()).throw(PageTypeError("boom")))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not seen:
            (await asyncio.sleep(0.01))

        assert isinstance(seen[0], PageTypeError), seen
        assert "boom" in str(seen[0])
    finally:
        logger.remove(token)
        executor.close()


async def test_post_control_exception_marks_the_executor_closed():
    """A3: SystemExit must interrupt the owner loop instead of being logged."""
    caught = []

    def capture(args):
        caught.append(args.exc_value)

    previous_hook = threading.excepthook
    threading.excepthook = capture
    executor = RealmExecutor(name="race-control-exit")
    try:
        assert executor.post(lambda: (_ for _ in ()).throw(SystemExit(3)))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not executor.closed:
            (await asyncio.sleep(0.01))
        assert executor.closed, "owner thread swallowed SystemExit and kept running"
        assert len(caught) == 1 and isinstance(caught[0], SystemExit), (
            "SystemExit was logged away instead of escaping the owner thread"
        )
    finally:
        threading.excepthook = previous_hook
        executor.close()


# -- A4: a handle must never be left registered after its delivery ------


async def test_inline_fetch_completion_releases_ownership(browser, monkeypatch):
    """Inline delivery releases ownership even for a pending handle."""
    page = browser.new_profile().new_page()
    try:
        responses = []
        def synchronous(handle, url, request, callback):
            callback(_response_for(url), None)
        monkeypatch.setattr(page.resource_fetcher, "_start_async", synchronous)
        handle = page.services.resource_services.load_resource_async(
            "http://127.0.0.1:9/x", {"method": "GET"}, lambda r, e: responses.append((r, e)))
        assert not handle.done
        assert len(responses) == 1 and responses[0][1] is None
        assert page.resource_ownership.active_count == 0
    finally:
        (await page.close())


async def test_document_resource_is_tracked_before_its_request_is_issued(browser, monkeypatch):
    """A4: document subresources register first so delivery can untrack.

    The old order issued the request and only then registered the handle, so a
    synchronous completion called ``_untrack_resource`` before the handle had a
    scope callback and silently stranded the registration.
    """
    context = browser.new_profile()
    page = context.new_page()
    try:
        controller = page.document_resources
        order = []
        real_track = page.resource_ownership.track

        def spied_track(handle):
            order.append("track")
            real_track(handle)

        def recording_issue(url, request, callback):
            order.append("issue")
            return LoadHandle()

        monkeypatch.setattr(page.resource_ownership, "track", spied_track)
        monkeypatch.setattr(page.resource_fetcher, "load_resource_async", recording_issue)

        controller.track_document_resource(
            _DummyNode(), "http://127.0.0.1:9/doc.js", {"method": "GET"}, "script"
        )

        assert order == ["track", "issue"], f"document resource registered after issue: {order}"
    finally:
        (await page.close())


async def test_race_teardown_returns_to_thread_baseline(thread_leak_check, browser):
    """A1+A4 teardown must not leak owner, server, or network threads."""
    for _ in range(3):
        context = browser.new_profile()
        page = context.new_page()
        handle = context.start_devtools(page)
        handle.close()
        (await page.close())

    thread_leak_check(tolerance=0, timeout=15)


async def test_browser_close_refuses_new_profiles_while_teardown_is_running(monkeypatch):
    from pyv8c.async_api import Browser
    from pyv8c.foundation.host_errors import HostStateError
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    context = browser.new_profile()
    original = context.close
    observed = []
    async def gated_close():
        with pytest.raises(HostStateError):
            browser.new_profile()
        observed.append(True)
        await original()
    monkeypatch.setattr(context, "close", gated_close)
    await browser.close()
    assert observed == [True] and browser.profiles == []
    with pytest.raises(HostStateError):
        _ = browser.loader_pool
    assert browser._loader_pool is None


async def test_browser_close_retry_keeps_the_loader_for_failed_context(monkeypatch):
    from pyv8c.async_api import Browser
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    context = browser.new_profile()
    pool = browser.loader_pool
    original = context.close
    def fail(): raise RuntimeError('fixture cleanup failed')
    monkeypatch.setattr(context, 'close', fail)
    with pytest.raises(RuntimeError, match='fixture cleanup failed'):
        (await browser.close())
    assert browser.loader_pool is pool
    assert pool.submit(lambda:42).result(3) == 42
    monkeypatch.setattr(context, 'close', original)
    (await browser.close())
    assert browser._closed


async def test_page_close_retries_failed_retired_realm_cleanup():
    from pyv8c.async_api import Browser
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    attempts = []
    def close():
        attempts.append(1)
        if len(attempts) == 1: raise RuntimeError('retired cleanup failed')
    retired = SimpleNamespace(close=close)
    page._retired_realms.append(retired)
    try:
        with pytest.raises(RuntimeError, match='retired cleanup failed'):
            (await page.close())
        assert not page.closed and not page.executor.closed
        assert page.realm.v8_context is not None
        assert page._retired_realms == [retired]
        (await page.close())
        assert page.closed and attempts == [1, 1]
    finally:
        (await browser.close())


async def test_page_close_retries_failed_owned_isolate_cleanup(monkeypatch):
    from pyv8c.async_api import Browser
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    isolate = page.isolate
    original = type(isolate).close
    attempts = []
    def close(current):
        if current is not isolate:
            return original(current)
        attempts.append(1)
        if len(attempts) == 1: raise RuntimeError('isolate cleanup failed')
        original(current)
    monkeypatch.setattr(type(isolate), 'close', close)
    try:
        with pytest.raises(RuntimeError, match='isolate cleanup failed'):
            (await page.close())
        assert not page.closed and not page.executor.closed
        (await page.close())
        assert page.closed and page.executor.closed and attempts == [1, 1]
    finally:
        (await browser.close())
