"""The mouse sequences pyv8c dispatches, next to the ones Chrome dispatches.

Same page, same steps, one log per browser: a scenario is a list of moves and
button steps that both sides run through their own mouse API, and what comes
back is what the page saw.  Only scenarios whose elements are laid out by
explicit size have their coordinates compared -- the layout model is coarser
than Chromium's for inline and positioned boxes, which is where the two
legitimately differ -- while the event order, targets, ``detail`` and
``buttons`` have to match everywhere.

Two clicks in a row are not compared: Playwright reports each one as a fresh
``click_count=1``, so Chrome's ``detail`` would be 1 and 1 where a real mouse
reports 1 and 2.  The double-click scenario drives the same counted pair on
both sides instead.
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyv8c.pyv8", reason="the comparison runs in the native environment")

from harness import ReferenceUnavailable, chrome_reference

URL = "https://mouse.test/"

#: What each side records: the sequence the page sees, with every member as a
#: string so the two engines serialize identically, and coordinates rounded
#: because the layout model measures in fractional pixels.  ``screenX``/``screenY``
#: are left out: they carry the host window's position on the screen, which this
#: environment has no window to have.
RECORDER = """
globalThis.log = [];
const record = (type) => (e) => log.push([type, e.target.id || e.target.localName,
    String(e.detail), String(e.isTrusted), String(e.button), String(e.buttons),
    String(Math.round(e.clientX)), String(Math.round(e.clientY)),
    String(Math.round(e.pageX)), String(Math.round(e.pageY)),
    String(Math.round(e.movementX)), String(Math.round(e.movementY)),
    String(Math.abs(e.timeStamp - performance.now()) < 1000),
    String(e.type.endsWith('move')
        ? (e.getCoalescedEvents ? (() => { const merged = e.getCoalescedEvents();
              return merged.length >= 1 && merged[0] instanceof PointerEvent; })() : false)
        : true)]);
for (const type of ['pointerover', 'pointerout', 'pointerenter', 'pointerleave', 'pointermove',
                    'pointerdown', 'pointerup', 'mouseover', 'mouseout', 'mouseenter',
                    'mouseleave', 'mousemove', 'mousedown', 'mouseup', 'click', 'dblclick',
                    'auxclick', 'contextmenu', 'focus', 'focusin', 'blur', 'focusout',
                    'input', 'change', 'submit'])
  addEventListener(type, record(type), true);
"""

PAGE = """
<!DOCTYPE html><html><body style="margin:0">
<div id="a" style="display:block;width:100px;height:40px"></div>
<div id="b" style="display:block;width:100px;height:40px"></div>
<a id="link" href="#frag" style="display:block;width:120px;height:30px">link</a>
<button id="btn" style="display:block;width:120px;height:30px">go</button>
<input id="box" type="checkbox" style="display:block;margin:0;width:20px;height:20px">
<label id="lab" for="box" style="display:block;width:120px;height:30px">label</label>
<input id="text" type="text" style="display:block;margin:0;width:120px;height:20px">
<form id="form" action="/submitted">
  <button id="submit" type="submit" style="display:block;width:120px;height:30px">send</button>
</form>
<button id="disabled" disabled style="display:block;width:120px;height:30px">no</button>
<div id="outer" style="display:block;width:200px;height:80px">
  <div id="inner" style="display:block;width:80px;height:80px"></div>
</div>
<script>__RECORDER__</script>
</body></html>
"""

#: ``(move|click|dblclick|down|up, selector, button, dx, dy)``: the steps a
#: scenario runs, expressed against the selector rather than a coordinate, so
#: each side resolves the point from its own layout.
SCENARIOS = {
    "move-onto-and-between-elements": [
        ("move", "#a"), ("move", "#b"), ("move", "#b", "", 1, 1),
    ],
    "click-a-button": [("click", "#btn")],
    "double-click-a-button": [("dblclick", "#btn")],
    "right-click-a-button": [("click", "#btn", "right")],
    "click-a-checkbox": [("click", "#box")],
    "click-a-label": [("click", "#lab")],
    "click-a-disabled-button": [("click", "#disabled")],
    "click-a-text-input": [("click", "#text")],
    "click-a-link": [("click", "#link")],
    "drag-from-one-element-to-another": [
        ("move", "#inner"), ("down",), ("move", "#outer", "", 60, 0), ("up",),
    ],
    "click-through-the-script-api": [("script", "document.getElementById('box').click()")],
    "click-a-label-through-the-script-api": [
        ("script", "document.getElementById('lab').click()"),
    ],
    "click-a-link-through-the-script-api": [
        ("script", "document.getElementById('link').click()"),
    ],
}

#: Scenarios whose elements both layout models place in the same spot, so the
#: coordinates in the log have to match as well as the sequence.  The ones left
#: out are sequence-only for a reason the corpus records: a text input's box
#: carries the UA sheet's border and padding in Chromium and the whitespace
#: inside a ``form`` counts towards its height here, which moves every element
#: after it (``a_form_control_box_is_its_attribute_size``).
COMPARED_TO_THE_PIXEL = frozenset({
    "move-onto-and-between-elements", "click-a-button", "double-click-a-button",
    "right-click-a-button", "click-a-checkbox", "click-a-label", "click-a-link",
})


@pytest.fixture(scope="module")
async def reference():
    """One local Chrome for the module, or a skip saying what is missing."""
    try:
        async with chrome_reference() as session:
            yield session
    except ReferenceUnavailable as unavailable:
        pytest.skip(str(unavailable))


#: The events a browser may coalesce, which only the moving scenarios compare.
COALESCED_MOVES = frozenset(("pointermove", "mousemove"))

#: The scenarios whose whole observation is the moving.
MOVE_SCENARIOS = frozenset({"move-onto-and-between-elements"})


def _scenario_ids():
    return sorted(SCENARIOS)


async def _centre(page, selector, dx=0, dy=0, *, reference=False):
    """The centre of the element's box on *page*, offset by ``(dx, dy)``."""
    source = (
        """(() => { const r = document.querySelector('%s').getBoundingClientRect();
             return [r.x + r.width / 2 + %d, r.y + r.height / 2 + %d]; })()"""
        % (selector, int(dx), int(dy))
    )
    values = await page.evaluate(source) if reference else page.evaluate(source)
    return float(values[0]), float(values[1])


async def _drive(page, steps, *, reference=False):
    """Run the steps against a Playwright-style page: ``mouse`` plus ``evaluate``."""
    for step in steps:
        action = step[0]
        if action == "script":
            (await page.evaluate(step[1])) if reference else page.evaluate(step[1])
            continue
        if action == "down":
            (await page.mouse.down()) if reference else (await page.mouse.down())
            continue
        if action == "up":
            (await page.mouse.up()) if reference else (await page.mouse.up())
            continue
        selector = step[1]
        button = step[2] if len(step) > 2 and step[2] else "left"
        dx = step[3] if len(step) > 3 else 0
        dy = step[4] if len(step) > 4 else 0
        point = await _centre(page, selector, dx, dy, reference=reference)
        if action == "move":
            (await page.mouse.move(*point)) if reference else (await page.mouse.move(*point))
        elif action == "click":
            (await page.mouse.click(*point, button=button)) if reference else (await page.mouse.click(*point, button=button))
        elif action == "dblclick":
            (await page.mouse.dblclick(*point, button=button)) if reference else (await page.mouse.dblclick(*point, button=button))
        else:  # pragma: no cover - a typo in SCENARIOS
            raise AssertionError(f"unknown step {action!r}")


async def _chrome_log(reference, steps):
    page = (await reference.browser.new_page(viewport={"width": 1280, "height": 720}))
    try:
        (await page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body=PAGE.replace("__RECORDER__", RECORDER))))
        (await page.goto(URL))
        await _drive(page, steps, reference=True)
        return (await page.evaluate("log"))
    finally:
        (await page.close())


async def _here_log(steps):
    from pyv8c.async_api import Browser

    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE.replace("__RECORDER__", RECORDER), url=URL, wait_until="commit"))
        await _drive(page, steps)
        return page.evaluate("log")
    finally:
        (await browser.close())


@pytest.fixture(scope="module")
async def compared(request, reference):
    """Both browsers' logs for one scenario, or a skip when Chrome cannot run.

    Move events are dropped for every scenario that is not only about moving:
    a browser coalesces them by design (that is what ``getCoalescedEvents`` is
    for), so how many of them one cursor step produces is not a contract this
    comparison can hold either side to.
    """
    name = request.param
    steps = SCENARIOS[name]
    chrome, here = (await _chrome_log(reference, steps)), (await _here_log(steps))
    if name in MOVE_SCENARIOS:
        return chrome, here
    return ([row for row in chrome if row[0] not in COALESCED_MOVES],
            [row for row in here if row[0] not in COALESCED_MOVES])


@pytest.mark.parametrize("compared", _scenario_ids(), indirect=True)
async def test_the_two_browsers_dispatch_the_same_sequence(compared):
    chrome, here = compared
    assert [row[:6] for row in here] == [row[:6] for row in chrome]


@pytest.mark.parametrize("compared",
                         sorted(COMPARED_TO_THE_PIXEL & set(SCENARIOS)), indirect=True)
async def test_the_same_scenarios_agree_on_the_coordinates_too(compared):
    chrome, here = compared
    assert here == chrome


@pytest.fixture(scope="module")
async def timestamped(reference):
    """One scenario's logs, for the question the sequence comparison does not ask."""
    steps = SCENARIOS["click-a-button"]
    return (await _chrome_log(reference, steps)), (await _here_log(steps))


async def test_both_browsers_stamp_events_on_the_documents_own_clock(timestamped):
    """A page can compare an event's timeStamp with performance.now() on either side."""
    chrome, here = timestamped
    assert here and {row[12] for row in here} == {"true"}
    assert {row[12] for row in chrome} == {"true"}


async def test_both_browsers_report_the_same_shape_of_merged_samples(timestamped):
    """A pointer event's merged samples read as a non-empty list of pointer events.

    Only that much is compared: Chrome hands back a copy with coordinates of its own
    where this one reports the event itself, and how many samples an input step
    carries is not a contract either side can be held to.
    """
    chrome, here = timestamped
    assert here and {row[13] for row in here if row[0] == "pointermove"} == {"true"}
    assert [row[13] for row in here] == [row[13] for row in chrome]
