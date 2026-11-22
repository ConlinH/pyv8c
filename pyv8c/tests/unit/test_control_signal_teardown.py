"""Execution control signals must not be swallowed by teardown or error reporting."""

import pytest

from pyv8c.foundation.lifecycle import LifecycleState, ResourceScope


def _raise(exc):
    raise exc


async def test_resource_scope_propagates_control_exception_and_settles_state():
    scope = ResourceScope()
    scope.add(lambda: _raise(KeyboardInterrupt("stop")))
    with pytest.raises(KeyboardInterrupt):
        scope.close()
    assert scope.state is LifecycleState.CLOSED


async def test_resource_scope_propagates_system_exit_and_settles_state():
    scope = ResourceScope()
    scope.add(lambda: _raise(SystemExit(3)))
    with pytest.raises(SystemExit) as caught:
        scope.close()
    assert caught.value.code == 3
    assert scope.state is LifecycleState.CLOSED


async def test_resource_scope_still_continues_after_ordinary_failure():
    scope = ResourceScope()
    calls = []
    scope.add(lambda: calls.append("last"))
    scope.add(lambda: _raise(RuntimeError("ordinary")))
    scope.close()
    assert calls == ["last"]
    assert scope.state is LifecycleState.CLOSED


@pytest.mark.native
async def test_page_close_stops_page_callbacks_after_control_signal(monkeypatch):
    pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
    from pyv8c.async_api import Browser

    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.navigation.has_navigation = True
        calls = []
        monkeypatch.setattr(
            page.lifecycle, "dispatch_beforeunload",
            lambda: _raise(KeyboardInterrupt("stop")),
        )
        monkeypatch.setattr(
            page.lifecycle, "dispatch_unload",
            lambda: calls.append("unload"),
        )
        with pytest.raises(KeyboardInterrupt):
            (await page.close())
        assert calls == []
    finally:
        monkeypatch.undo()
        (await browser.close())


@pytest.mark.native
async def test_exception_reporting_propagates_control_signal(monkeypatch):
    pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
    from pyv8c.async_api import Browser

    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        monkeypatch.setattr(
            page.lifecycle, "dispatch_script_error",
            lambda *a, **k: _raise(KeyboardInterrupt("stop")),
        )
        with pytest.raises(KeyboardInterrupt):
            page.report_exception(message="x")
        assert page._reporting_error is False
    finally:
        monkeypatch.undo()
        (await browser.close())


@pytest.mark.native
async def test_rejection_reporting_propagates_control_signal(monkeypatch):
    pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
    from pyv8c.async_api import Browser

    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        monkeypatch.setattr(
            page.lifecycle, "dispatch_rejection",
            lambda *a, **k: _raise(KeyboardInterrupt("stop")),
        )
        with pytest.raises(KeyboardInterrupt):
            page.report_rejection("unhandledrejection", None, None)
    finally:
        monkeypatch.undo()
        (await browser.close())
