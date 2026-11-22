"""A framed document with no address of its own still resolves relative loads.

``about:blank`` and ``about:srcdoc`` carry no scheme, so a browser takes the base URL from the
document that created them.  Resolution used to read the frame's own URL instead, which turned
``<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js">`` into a bare path -- refused
as "not a scheme a page loads", so the script was never requested at all (measured on the live
Cloudflare challenge, 2026-10-10).

A two-sided reading of the same three shapes shows that Chrome resolves the plain ``srcdoc``
and the ``about:blank`` cases; the sandboxed ``srcdoc`` one it *does* drop (an open divergence,
not asserted here as a Chrome fact).
"""

import asyncio
import json
import threading
import time

from pyv8c.async_api import Browser

URL = "http://frames.test/page.html"

#: Every URL the loader was asked for, in order.
SEEN = []


def _page(frame_attributes, script_src):
    """The page, with one frame whose markup asks for *script_src*."""
    markup = f'<script src="{script_src}"></scr' + "ipt>"
    return (
        "<!doctype html><html><body><script>"
        "var frame = document.createElement('iframe');"
        f"{frame_attributes}"
        "frame.setAttribute('srcdoc', __MARKUP__);"
        "document.body.appendChild(frame);"
        "</" + "script></body></html>"
    ).replace("__MARKUP__", json.dumps(markup).replace("</", "<\\/"))


def _load(page_html):
    def loader(url, request):
        SEEN.append(str(url))
        if str(request.get("destination")) == "document" or str(url).endswith(".html"):
            return {"body": page_html, "headers": {"content-type": "text/html"}}
        return {"body": "/* a script */", "headers": {"content-type": "text/javascript"}}
    return loader


async def _pump(page, seconds=1.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            (await page.run_until_idle(timeout=0))
        except BaseException:  # noqa: BLE001 - a busy realm is retried, not asserted on
            pass
        (await asyncio.sleep(0.05))




async def _asked_for(page_html):
    """Serve *page_html* to one page and report the URLs its loader was asked for."""
    del SEEN[:]
    browser = (await Browser(stub_logging="off", threaded_loaders=True, real_network=False,
                      resource_loader=_load(page_html)).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto(URL))
        (await _pump(page))
    finally:
        await browser.close()
    return list(SEEN)


async def test_a_srcdoc_frame_resolves_a_root_relative_script_against_its_creator():
    seen = (await _asked_for(((_page("", "/frame-relative.js")))))
    assert "http://frames.test/frame-relative.js" in seen, seen
    assert "/frame-relative.js" not in seen, seen


async def test_a_sandboxed_srcdoc_frame_resolves_it_the_same_way():
    """The widget frame's shape: ``sandbox`` takes the origin away, never the base URL."""
    seen = (await _asked_for(((_page("frame.setAttribute('sandbox', 'allow-scripts');",
                            "/sandboxed-relative.js")))))
    assert "http://frames.test/sandboxed-relative.js" in seen, seen
    assert "/sandboxed-relative.js" not in seen, seen


async def test_a_document_with_its_own_url_still_resolves_against_itself():
    """The creator walk must not reach past a document that has an address of its own."""
    seen = (await _asked_for(
        "<!doctype html><html><body>"
        '<script src="/page-relative.js"></scr' + "ipt>"
        "</body></html>"
    ))
    assert "http://frames.test/page-relative.js" in seen, seen
