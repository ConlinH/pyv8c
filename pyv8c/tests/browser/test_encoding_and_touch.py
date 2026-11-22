"""Two labels-and-interfaces questions, answered as Chrome answers them.

Measured side by side on 2026-10-06:

* ``new TextDecoder('iso-8859-1')`` -- we raised, Chrome does not: the standard files that
  label under **windows-1252**, so 0x80 decodes to the euro sign and ``encoding`` reports the
  canonical name.
* ``document.createEvent('TouchEvent')`` -- **both** answer ``NotSupportedError``, because the
  legacy ``createEvent`` list does not carry it.  The touch interfaces themselves do exist in
  Chrome, which is what the constructor test below keeps.

A raise is only a difference when the other side does not raise; that is why both directions
are held here.
"""
import json

import pytest

from pyv8c.async_api import Browser




@pytest.fixture
async def page():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<!doctype html><body>probe", url="https://encoding.test/", wait_until="commit"))
        yield page
    finally:
        await browser.close()


async def test_the_standard_labels_report_their_canonical_name(page):
    assert json.loads(page.evaluate("""JSON.stringify(
        ['utf-8', 'iso-8859-1', 'latin1', 'us-ascii', 'windows-1252', 'utf-16le', 'shift_jis',
         'gbk', 'koi8-r', 'macintosh']
            .map(function (label) {
                try { return new TextDecoder(label).encoding; }
                catch (error) { return error.name; }
            }))""")) == ["utf-8", "windows-1252", "windows-1252", "windows-1252", "windows-1252",
                         "utf-16le", "shift_jis", "gbk", "koi8-r", "macintosh"]


async def test_a_windows_1252_label_decodes_the_bytes_chrome_decodes(page):
    #: 0x80 is the euro sign under windows-1252 and a replacement character under utf-8.
    assert json.loads(page.evaluate("""JSON.stringify(
        ['utf-8', 'iso-8859-1'].map(function (label) {
            return new TextDecoder(label).decode(new Uint8Array([0x80, 0xE9, 0x41]));
        }))""")) == ["\ufffd\ufffdA", "\u20ac\u00e9A"]


async def test_decode_takes_the_bare_array_buffer_chrome_takes(page):
    """``BufferSource`` is an ``ArrayBuffer`` or a view over one, and ``decode`` takes either.

    A live run of the challenge handed ``decode`` the bare buffer and got a ``TypeError`` for it
    twice, which the widget then reported back to the edge (measured 2026-10-10).  Chrome
    answers "ABC" for all three forms here, as a two-sided reading confirmed.
    """
    assert json.loads(page.evaluate("""JSON.stringify(
        [new Uint8Array([65, 66, 67]),
         new Uint8Array([65, 66, 67]).buffer,
         new Uint8Array([65, 66, 67]).buffer.slice(0)]
            .map(function (source) { return new TextDecoder().decode(source); }))""")) ==\
        ["ABC", "ABC", "ABC"]


async def test_decode_still_refuses_what_is_not_a_buffer_source(page):
    """The widening has to stop at the standard's edge, or a wrong argument stops raising."""
    assert page.evaluate("""(() => {
        try { new TextDecoder().decode({}); return 'no error'; }
        catch (error) { return error.name; }
    })()""") == "TypeError"


async def test_an_unknown_label_is_a_range_error(page):
    assert page.evaluate("""(() => {
        try { new TextDecoder('bogus-encoding'); return 'no error'; }
        catch (error) { return error.name; }
    })()""") == "RangeError"


async def test_create_event_still_refuses_touch_event_as_chrome_does(page):
    assert page.evaluate("""(() => {
        try { document.createEvent('TouchEvent'); return 'no error'; }
        catch (error) { return error.name; }
    })()""") == "NotSupportedError"


async def test_the_touch_interfaces_exist_and_carry_empty_lists(page):
    assert json.loads(page.evaluate("""JSON.stringify((() => {
        const event = new TouchEvent('touchstart', {bubbles: true, cancelable: true});
        return [Object.prototype.toString.call(event), event.type, event.bubbles,
                event.cancelable, event.touches.length, event.targetTouches.length,
                event.changedTouches.length,
                Object.prototype.toString.call(event.touches),
                typeof Touch, typeof TouchList];
    })())""")) == ["[object TouchEvent]", "touchstart", True, True, 0, 0, 0,
                   "[object TouchList]", "function", "function"]


async def test_a_touch_carries_what_it_was_given(page):
    assert json.loads(page.evaluate("""JSON.stringify((() => {
        const touch = new Touch({identifier: 7, target: document.body, clientX: 12, clientY: 34});
        return [touch.identifier, touch.clientX, touch.clientY, touch.pageX, touch.pageY,
                Number.isNaN(touch.force), touch.rotationAngle];
    })())""")) == [7, 12, 34, 12, 34, False, 0]
