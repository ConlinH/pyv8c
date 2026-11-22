"""An image's width/height report rendered content size, not the HTML attribute."""

import asyncio
import base64
import time

from pyv8c.async_api import Browser

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def test_image_width_and_height_follow_the_rendered_box():
    browser = (await Browser(
        real_network=False,
        resource_loader=lambda url, request: {
            "body": PNG, "headers": {"content-type": "image/png"},
        },
    ).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(
            '<img id="a" src="https://img.test/a.png" style="width:200px">'
            '<img id="b" src="https://img.test/b.png" style="height:30px">',
            url="https://example.test/",
         wait_until="commit"))
        deadline = time.monotonic() + 2
        while not page.evaluate(
                "document.getElementById('a').complete "
                "&& document.getElementById('a').naturalWidth > 0"):
            assert time.monotonic() < deadline
            (await asyncio.sleep(0.01))
        assert page.evaluate("""(() => {
            const a = document.getElementById('a'), b = document.getElementById('b');
            return [a.naturalWidth, a.naturalHeight, a.width, a.height,
                    b.width, b.height];
        })()""") == [1, 1, 200, 200, 30, 30]
    finally:
        (await browser.close())


async def test_absolute_image_height_percentage_uses_its_containing_block():
    browser = (await Browser(
        real_network=False,
        resource_loader=lambda url, request: {
            "body": PNG, "headers": {"content-type": "image/png"},
        },
    ).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(
            '<div style="position:relative;width:200px;height:100px">'
            '<img id="a" src="https://img.test/a.png" '
            'style="position:absolute;height:50%">'
            '</div>',
            url="https://example.test/",
         wait_until="commit"))
        (await page.wait_for("document.getElementById('a').complete", timeout=2))
        assert page.evaluate(
            "[document.getElementById('a').width, document.getElementById('a').height]"
        ) == [50, 50]
    finally:
        (await browser.close())


async def test_image_load_invalidates_existing_layout_snapshot():
    import threading
    ready = threading.Event()
    def loader(url, request):
        ready.wait(3)
        return {"body": PNG, "headers": {"content-type": "image/png"}}
    browser = (await Browser(real_network=False, threaded_loaders=True, resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<img id="a" src="https://img.test/a.png" style="width:200px">', url="https://example.test/", wait_until="commit"))
        page.evaluate("document.getElementById('a').getBoundingClientRect().height")
        ready.set()
        (await page.wait_for("document.getElementById('a').complete && document.getElementById('a').naturalWidth > 0", timeout=3))
        assert page.evaluate("document.getElementById('a').getBoundingClientRect().height") == 200
    finally:
        ready.set()
        (await browser.close())

