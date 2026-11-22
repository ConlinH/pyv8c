"""The page events Chrome sends on its own: reveal, page show, and media loads."""
import json

from pyv8c.async_api import Browser
from pyv8c.web.fetch.response import Response


async def test_pageshow_and_pagereveal_arrive_once_after_load():
    """Chrome reports pageshow as bubbling and sends one pagereveal behind it."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("""<head><script>
            window.__seen = [];
            const note = (e) => window.__seen.push([e.type, e.bubbles,
                e.constructor && e.constructor.name, document.readyState]);
            for (const name of ['pageshow', 'pagereveal']) addEventListener(name, note);
        </script></head><body>body</body>""", url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle())
        assert json.loads(page.evaluate("JSON.stringify(__seen)")) == [
            ["pageshow", True, "PageTransitionEvent", "complete"],
            ["pagereveal", False, "PageRevealEvent", "complete"],
        ]
    finally:
        (await browser.close())


async def test_a_media_element_ends_where_chrome_ends_undecodable_bytes():
    """Nothing decodes here, so a media element runs the sequence Chrome runs for
    bytes it cannot use: loadstart, progress, suspend, error, and no load."""
    def loader(url, request):
        if url.endswith("/movie.mp4"):
            return Response("not a movie", {"url": url, "content-type": "video/mp4"})
        return Response("", {"url": url, "content-type": "text/html"})

    browser = (await Browser(resource_loader=loader, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("""<body><video id="v" src="/movie.mp4"></video><script>
            window.__media = [];
            const video = document.querySelector('video');
            for (const name of ['loadstart', 'progress', 'suspend', 'error', 'load', 'loadend'])
                video.addEventListener(name, (e) => __media.push(e.type));
        </script></body>""", url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle())
        assert page.evaluate("__media") == ["loadstart", "progress", "suspend", "error"]
    finally:
        (await browser.close())
