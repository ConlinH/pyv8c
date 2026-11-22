"""Device motion: the empty readings Chrome delivers to a page that listens."""
import json

from pyv8c.async_api import Browser

PAGE = """<head><script>
window.__events = [];
const note = (e) => window.__events.push([
    e.type, e.interval === undefined ? null : e.interval,
    typeof e.alpha === 'undefined' ? null : e.alpha,
    typeof e.absolute === 'undefined' ? null : e.absolute,
    e.acceleration ? [e.acceleration.x, e.acceleration.y, e.acceleration.z] : null]);
for (const type of ['devicemotion', 'deviceorientation', 'deviceorientationabsolute'])
    addEventListener(type, note);
</script></head><body>body</body>"""


async def test_a_page_that_listens_receives_one_empty_reading_per_motion_type():
    """Chrome starts as soon as a page listens; nothing moves here, so the reading is
    the empty one -- every field null, the interval it reports, and only the absolute
    orientation type saying so."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle())
        assert json.loads(page.evaluate("JSON.stringify(__events)")) == [
            ["devicemotion", 16, None, None, [None, None, None]],
            ["deviceorientation", None, None, False, None],
            ["deviceorientationabsolute", None, None, True, None],
        ]
    finally:
        (await browser.close())


async def test_a_listener_added_after_load_is_handed_one_reading():
    """A listener that arrives late gets one straight away, as Chrome's does."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body>body</body>", url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle())
        page.evaluate("window.__late = [];"
                      " addEventListener('devicemotion', () => __late.push('motion'));"
                      " addEventListener('deviceorientation', () => __late.push('orientation'))")
        (await page.run_until_idle())
        assert page.evaluate("__late") == ["motion", "orientation"]
    finally:
        (await browser.close())


async def test_an_insecure_origin_has_the_interfaces_taken_away():
    """Chrome exposes no motion interface outside a secure context: the names go, and
    return with a secure document."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle())
        assert page.evaluate("typeof DeviceMotionEvent") == "function"

        (await page.set_content(PAGE, url="http://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle())
        assert page.evaluate("""[typeof DeviceMotionEvent, typeof DeviceMotionEventAcceleration,
            typeof DeviceMotionEventRotationRate, typeof DeviceOrientationEvent,
            isSecureContext]""") == ["undefined", "undefined", "undefined", "undefined", False]
        assert page.evaluate("__events") == []

        (await page.set_content(PAGE, url="https://repro.test/again", wait_until="commit"))
        (await page.run_until_idle())
        assert page.evaluate("typeof DeviceOrientationEvent") == "function"
    finally:
        (await browser.close())
