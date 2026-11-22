"""The document's timeline: Chrome starts it at the navigation, not at the process."""

import asyncio
import time

from pyv8c.async_api import Browser


async def test_the_timeline_restarts_with_each_document():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body>first</body>", url="https://repro.test/first", wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        assert page.evaluate("Math.round(performance.now())") < 1000
        (await asyncio.sleep(0.5))
        assert page.evaluate("Math.round(performance.now())") >= 500

        (await page.set_content("<body>second</body>", url="https://repro.test/second", wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        # The process has been up longer, so a small reading is the document's own clock.
        assert page.evaluate("Math.round(performance.now())") < 1000
    finally:
        (await browser.close())


async def test_the_origin_is_an_epoch_instant_and_navigation_start_agrees():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body>body</body>", url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        assert page.evaluate(
            "Math.abs(performance.timeOrigin + performance.now() - Date.now()) < 50")
        assert page.evaluate(
            "Math.abs(performance.timing.navigationStart - performance.timeOrigin) < 50")
        assert page.evaluate(
            "[performance.timing.redirectStart, performance.timing.redirectEnd]") == [0, 0]
    finally:
        (await browser.close())


async def test_visibility_state_entries_for_initial_document_and_changes():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("performance.getEntriesByType('visibility-state').length") == 1
        (await page.set_content('<body>visible</body>', url='https://example.test/', wait_until="commit"))
        first = page.evaluate("JSON.stringify(performance.getEntriesByType('visibility-state').map(x => [x.constructor.name,x.name,x.entryType,x.startTime,x.duration]))")
        assert first == '[["VisibilityStateEntry","visible","visibility-state",0,0]]'
        assert page.set_visibility(True) is True
        assert page.set_visibility(True) is False
        assert page.set_visibility(False) is True
        assert page.evaluate("JSON.stringify(performance.getEntriesByType('visibility-state').map(x => x.name))") == '["visible","hidden","visible"]'
        (await page.set_content('<body>new document</body>', url='https://example.test/next', wait_until="commit"))
        assert page.evaluate("performance.getEntriesByType('visibility-state').length") == 1
    finally:
        (await browser.close())
