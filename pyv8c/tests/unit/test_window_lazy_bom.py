"""Low-frequency Window modules are allocated on first observable access."""
import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c.async_api import Browser


def _loader(url, request):
    return {"url": url, "body": "", "headers": {"content-type": "text/html"}}


async def test_window_defers_low_frequency_bom_modules_and_keeps_identity():
    browser = (await Browser(stub_logging="off", resource_loader=_loader).__aenter__())
    try:
        context = browser.new_profile()
        page = context.new_page()
        await page.goto("http://lazy.test/start")
        window = page.window
        assert window.location.href == "http://lazy.test/start"

        assert page.evaluate("navigator === navigator")
        assert "navigator" in browser.bindings.runtime.loaded_features
        assert page.evaluate("screen === screen")
        assert page.evaluate("localStorage === localStorage")
        assert page.evaluate("sessionStorage === sessionStorage")
        assert page.evaluate("history === history")
        assert page.evaluate("indexedDB === indexedDB")
        assert "indexeddb" in browser.bindings.runtime.loaded_features
        assert page.evaluate("history.length") == 1
    finally:
        (await browser.close())


async def test_lazy_history_starts_at_the_current_document_url():
    browser = (await Browser(stub_logging="off", resource_loader=_loader).__aenter__())
    try:
        context = browser.new_profile()
        page = context.new_page()
        await page.goto("http://lazy.test/original")
        window = page.window
        window.location.href = "http://lazy.test/current"
        assert page.evaluate("history.length") == 1
        # History is created on first access, so its single entry describes the
        # document that is actually loaded; fragment navigation and
        # back/forward stay coherent because no stale about:blank entry is kept
        # (a History materialized *before* a commit is re-pointed by
        # ``Window.sync_history_url``).
        assert window.history._entries == [("http://lazy.test/current", None)]
    finally:
        (await browser.close())
