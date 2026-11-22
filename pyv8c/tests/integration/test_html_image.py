"""``HTMLImageElement.complete``: what a page waits on, offline.

A page asks ``complete`` to decide whether it can draw an image right now, so the
answer has to follow the load it is asking about: true while there is nothing to
load, false while a fetch is in flight, and true once that fetch settles -- success
or failure alike, which is what Chrome reports for an image whose bytes never
arrived.  The loader is held open by the test so the middle state is observable.
"""

import asyncio
import threading

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c.async_api import Browser

ORIGIN = "http://image.test"
PAGE = "<!doctype html><title>image</title><body><img id=markup></body>"


async def _page(loader=None):
    browser = (await Browser(stub_logging="off", threaded_loaders=True,
                      resource_loader=loader or (lambda url, request: _response(url))).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content(PAGE, url=ORIGIN + "/", wait_until="commit"))
    return browser, page


def _response(url, status=200, body="ok"):
    return {"status": status, "url": str(url), "body": body,
            "headers": {"Content-Type": "image/png"}}


async def test_an_image_with_nothing_to_load_is_complete():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const loose = document.createElement('img');
            const empty = document.createElement('img');
            empty.src = '';
            return [loose.complete, empty.complete,
                    document.getElementById('markup').complete];
        })()""") == [True, True, True]
    finally:
        (await browser.close())


async def test_complete_is_false_while_a_load_is_in_flight_and_true_once_it_settles():
    started = threading.Event()
    released = threading.Event()

    def loader(url, request):
        started.set()
        released.wait(5)
        return _response(url)

    browser, page = (await _page(loader))
    try:
        page.evaluate("""(() => {
            window.__seen = [];
            window.__image = document.createElement('img');
            window.__image.addEventListener('load',
                () => window.__seen.push(window.__image.complete));
            window.__image.src = '/pictures/a.png';
            window.__inFlight = window.__image.complete;
            return 'asked';
        })()""")
        (await page.run_until_idle(timeout=2))
        assert page.evaluate("window.__inFlight") is False
        assert (await asyncio.to_thread(started.wait, 5)), "the loader never ran"
        released.set()
        (await page.wait_for("window.__seen.length > 0", timeout=10))
        assert page.evaluate("window.__seen") == [True]
        assert page.evaluate("window.__image.complete") is True
    finally:
        released.set()
        (await browser.close())


def _failing(url, request):
    raise RuntimeError("offline")


async def test_a_load_that_fails_still_settles_the_image():
    browser, page = (await _page(_failing))
    try:
        page.evaluate("""(() => {
            window.__errors = [];
            const image = document.createElement('img');
            image.addEventListener('error', () => window.__errors.push('error'));
            image.src = '/pictures/missing.png';
            window.__image = image;
            return 'asked';
        })()""")
        (await page.run_until_idle(timeout=5))
        assert page.evaluate("window.__errors") == ["error"]
        assert page.evaluate("window.__image.complete") is True
    finally:
        (await browser.close())
