"""What a beacon puts on the wire, and the Serial entry point in a secure context.

The values come from Chrome 154 over a local server: a beacon is a POST, a body given as
a string is text/plain, a Blob brings its own type, and a URLSearchParams is form
encoded.  A loader records what it was asked for, which is the only place the request
itself is visible offline.
"""
import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c.async_api import Browser

PAGE = "<!doctype html><title>n</title><body></body>"
SENT = []


def _loader(url, request):
    headers = request.get("headers")
    if hasattr(headers, "fn_entries"):
        headers = {str(name).lower(): str(value) for name, value in headers.fn_entries()}
    SENT.append([str(request.get("method")), str(url),
                 (headers or {}).get("content-type"), request.get("body")])
    return {"status": 200, "url": str(url), "body": "ok",
            "headers": {"content-type": "text/plain"}}



@pytest.fixture
async def page():
    del SENT[:]
    browser = (await Browser(stub_logging="off", resource_loader=_loader).__aenter__())
    instance = browser.new_profile().new_page()
    (await instance.set_content(PAGE, url="https://navigator.test/dir/page", wait_until="commit"))
    try:
        yield instance
    finally:
        (await browser.close())


async def test_serial_answers_the_interface_chrome_exposes(page):
    assert page.evaluate("""(() => {
        const serial = navigator.serial;
        return [typeof serial, serial.constructor.name, serial instanceof Serial,
                Object.prototype.toString.call(serial), typeof serial.getPorts,
                typeof serial.requestPort, serial === navigator.serial,
                typeof serial.onconnect];
    })()""") == ["object", "Serial", True, "[object Serial]", "function", "function",
                  True, "object"]


async def test_a_beacon_is_a_post_carrying_what_the_page_gave_it(page):
    assert page.evaluate("""(() => {
        navigator.sendBeacon('/b1');
        navigator.sendBeacon('/b2', 'text');
        navigator.sendBeacon('/b3', new Blob(['blob'], {type: 'application/x-thing'}));
        navigator.sendBeacon('/b4', new URLSearchParams('a=1'));
        return 'asked';
    })()""") == "asked"
    (await page.run_until_idle(timeout=5))
    assert SENT == [
        ["POST", "https://navigator.test/b1", None, None],
        ["POST", "https://navigator.test/b2", "text/plain;charset=UTF-8", "text"],
        ["POST", "https://navigator.test/b3", "application/x-thing", b"blob"],
        ["POST", "https://navigator.test/b4",
         "application/x-www-form-urlencoded;charset=UTF-8", "a=1"],
    ]
