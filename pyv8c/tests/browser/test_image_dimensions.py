"""A preload link is fetched, and an image answers for the size its file declares.

Both halves were missing: nothing fetched ``<link rel=preload>``, so a page could tell that a
resource it asked for never arrived, and no image reported a size at all, so a challenge that
measures one had nothing to read.  The size comes from the file's header -- no decoder here.
"""

import asyncio
import json
import threading
import time

from pyv8c.async_api import Browser

LOGO = ("<svg xmlns='http://www.w3.org/2000/svg' width='88' height='33'>"
        "<rect width='88' height='33' fill='#333'/></svg>")

PAGE_HTML = """<!doctype html><head>
<link rel="preload" as="image" href="/logo.svg">
<link rel="preload" as="font" href="/quiet.woff2" crossorigin>
</head><body>
<img id="logo" src="/logo.svg">
<script>
var out = {}; window.__out = out;
var image = document.getElementById('logo');
image.addEventListener('load', function () { out.loaded = true; });
out.seen = [];
</script></body>"""

PROBE_URL = "https://preload.test/"

MEDIA_PAGE = """<!doctype html><head>
<link id="reel" rel="preload" as="video" href="/reel.mp4">
<link id="tune" rel="preload" as="audio" href="/tune.mp3">
</head><body><script>
var out = window.__out = {loaded: []};
for (const id of ['reel', 'tune'])
    document.getElementById(id).addEventListener('load', function () { out.loaded.push(this.id); });
</script></body>"""
FETCHED = []




async def _pump(page, seconds=0.6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            (await page.run_until_idle(timeout=0))
        except BaseException:  # noqa: BLE001
            pass
        (await asyncio.sleep(0.01))


def _load(url, request):
    target = str(url)
    FETCHED.append(target)
    if target.endswith("logo.svg"):
        return {"body": LOGO, "headers": {"content-type": "image/svg+xml"}}
    if target.endswith("quiet.woff2"):
        return {"body": b"\x00\x01\x00\x00", "headers": {"content-type": "font/woff2"}}
    return {"body": PAGE_HTML, "headers": {"content-type": "text/html"}}


async def main(seconds=0.6):
    browser = (await Browser(stub_logging="off", threaded_loaders=True, real_network=False,
                      resource_loader=_load).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto(PROBE_URL))
        (await _pump(page, seconds))
        return page, json.loads(page.evaluate("""JSON.stringify((() => {
            const image = document.getElementById('logo');
            return {width: image.naturalWidth, height: image.naturalHeight,
                    complete: image.complete,
                    font: performance.getEntriesByType('resource')
                      .map(e => String(e.name)).filter(n => n.indexOf('woff2') >= 0)};
        })())"""))
    finally:
        await browser.close()


async def test_an_image_reports_the_size_its_file_declares():
    _page, reading = (await main())
    assert reading["width"] == 88, reading
    assert reading["height"] == 33, reading
    assert reading["complete"] is True, reading


async def test_a_preload_link_is_fetched_even_though_nothing_uses_it():
    del FETCHED[:]
    _page, reading = (await main())
    assert [target for target in FETCHED if target.endswith("quiet.woff2")], FETCHED
    assert reading["font"], reading


async def test_media_preload_links_are_fetched_too():
    """``as=video``/``as=audio`` are Chrome's destinations: fetched, with ``load`` fired."""
    del FETCHED[:]

    def load(url, request):
        target = str(url)
        FETCHED.append(target)
        if target.endswith("reel.mp4"):
            return {"body": b"\x00\x00\x00\x18ftypmp42", "headers": {"content-type": "video/mp4"}}
        if target.endswith("tune.mp3"):
            return {"body": b"ID3\x03", "headers": {"content-type": "audio/mpeg"}}
        return {"body": MEDIA_PAGE, "headers": {"content-type": "text/html"}}

    browser = (await Browser(stub_logging="off", threaded_loaders=True, real_network=False,
                      resource_loader=load).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto("https://preload.test/media"))
        (await _pump(page))
        loaded = json.loads(page.evaluate("JSON.stringify(__out.loaded)"))
    finally:
        await browser.close()
    assert [target for target in FETCHED if target.endswith("reel.mp4")], FETCHED
    assert [target for target in FETCHED if target.endswith("tune.mp3")], FETCHED
    assert sorted(loaded) == ["reel", "tune"], loaded
