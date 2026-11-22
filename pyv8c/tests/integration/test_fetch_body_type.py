"""The ``Content-Type`` a request body contributes when the author set none.

Fetch's "extract a body" types the body, and Chrome sends that type: a string goes as
``text/plain;charset=UTF-8``.  A Cloudflare challenge's submit POST depends on it -- the
answer that carries the widget result is refused with ``400`` when the header is absent
(measured against Chrome 150/155, 2026-11-03).
"""

import asyncio
import time

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c.async_api import Browser

ORIGIN = "http://site.test"
PAGE = "<!doctype html><title>page</title><body>page</body>"
SEEN = []


def _interceptor(url, request):
    SEEN.append({name.lower(): value for name, value in (request.get("headers") or {}).items()})
    return False  # an empty 200: the request is what this test asks about


@pytest.fixture
async def page():
    browser = (await Browser(real_network=False, resource_interceptor=_interceptor, stub_logging="off").__aenter__())
    instance = browser.new_profile().new_page()
    (await instance.set_content(PAGE, url=ORIGIN + "/start", wait_until="commit"))
    del SEEN[:]
    try:
        yield instance
    finally:
        (await browser.close())


async def _post(page, url, body):
    page.evaluate(
        "fetch(%r, {method: 'POST', body: %s}).catch(() => {})" % (url, body))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not SEEN:
        (await page.run_until_idle(timeout=1))
        (await asyncio.sleep(0.01))
    assert SEEN, "the request never left the page"
    return SEEN[-1]


async def test_a_string_body_travels_as_text_plain(page):
    assert (await _post(page, "/string", "'abc'")).get("content-type") == "text/plain;charset=UTF-8"


async def test_an_author_set_type_is_kept(page):
    page.evaluate("fetch('/own', {method: 'POST', body: 'abc', "
                  "headers: {'Content-Type': 'application/x-custom'}}).catch(() => {})")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not SEEN:
        (await page.run_until_idle(timeout=1))
        (await asyncio.sleep(0.01))
    assert SEEN[-1].get("content-type") == "application/x-custom"


async def test_url_search_params_travel_as_urlencoded(page):
    header = (await _post(page, "/search", "new URLSearchParams({a: '1'})"))
    assert header.get("content-type") == "application/x-www-form-urlencoded;charset=UTF-8"


async def test_a_blob_brings_its_own_type(page):
    header = (await _post(page, "/blob", "new Blob(['x'], {type: 'text/csv'})"))
    assert header.get("content-type") == "text/csv"


async def test_a_buffer_stays_untyped(page):
    header = (await _post(page, "/buffer", "new Uint8Array([1, 2, 3])"))
    assert "content-type" not in header


async def test_the_type_is_visible_on_the_request_object(page):
    assert page.evaluate(
        "new Request('/visible', {method: 'POST', body: 'x'}).headers.get('content-type')"
    ) == "text/plain;charset=UTF-8"
