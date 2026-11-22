import threading
import time

import pytest
from loguru import logger

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import HostStateError
from pyv8c.web.fetch.response import Response


async def test_goto_downloads_html_and_waits_for_document_load():
    calls = []

    def loader(url, request):
        calls.append((url, request.get("mode")))
        if url.endswith("/app.js"):
            return Response("window.__downloaded = document.querySelector('#app').textContent", {"url": url})
        return Response(
            "<html><body><main id='app'>loaded</main><script src='/app.js'></script></body></html>",
            {"url": url, "headers": {"content-type": "text/html; charset=utf-8"}},
        )

    browser = (await Browser(resource_loader=loader).__aenter__())
    page = browser.new_profile().new_page()
    response = (await page.goto("http://example.test/index.html"))

    assert response.url == "http://example.test/index.html"
    assert page.evaluate("[document.URL, document.readyState, __downloaded]") == [
        "http://example.test/index.html", "complete", "loaded",
    ]
    assert calls == [
        ("http://example.test/index.html", "navigate"),
        #: A classic script states no mode: it is a no-cors load of the page's own.
        ("http://example.test/app.js", None),
    ]
    (await browser.close())


async def test_an_iframe_reload_uses_the_parent_resource_controller(monkeypatch):
    """A child reloads through the frame owner, not through its own controller."""
    from pyv8c.platform.resources.frames import FrameResourceController

    controllers = []
    original = FrameResourceController.load_iframe

    def load_iframe(self, iframe):
        controllers.append(self.page)
        return original(self, iframe)

    monkeypatch.setattr(FrameResourceController, 'load_iframe', load_iframe)
    browser = (await Browser(
        real_network=False,
        resource_loader=lambda url, _request: Response(
            '<html><body>child</body></html>',
            {'url': url, 'headers': {'content-type': 'text/html'}},
        ),
        cache=False,
    ).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe src="/child"></iframe>', url='https://example.test/', wait_until="commit"))
        (await page.run_until_idle(timeout=1))
        child = next(iter(page._owned.frames))
        controllers.clear()
        child.evaluate('location.reload()')
        (await page.run_until_idle(timeout=1))
        assert controllers and all(owner is page for owner in controllers)
        assert child.url == 'https://example.test/child'
    finally:
        (await browser.close())


async def test_new_page_url_and_location_assignment_navigate_automatically():
    pages = {
        "http://example.test/first": "<html><body><h1>first</h1></body></html>",
        "http://example.test/next": "<html><body><h1>next</h1></body></html>",
    }
    browser = (await Browser(resource_loader=lambda url, _request: Response(pages[url], {"url": url})).__aenter__())
    page = browser.new_profile().new_page()
    await page.goto("http://example.test/first")

    assert page.evaluate("[location.href, document.querySelector('h1').textContent]") == [
        "http://example.test/first", "first",
    ]
    page.evaluate("location.href = '/next'")
    (await page.wait_for("location.href === 'http://example.test/next' && document.readyState === 'complete'", timeout=10))
    assert page.evaluate("[location.href, document.querySelector('h1').textContent]") == [
        "http://example.test/next", "next",
    ]
    await page.goto("/first")
    assert page.evaluate("[location.href, document.querySelector('h1').textContent]") == [
        "http://example.test/first", "first",
    ]
    (await browser.close())


async def test_document_location_is_the_windows_and_assigning_it_navigates():
    """The navigation an assignment performs needs a server; the corpus has none."""
    pages = {
        "http://example.test/first": "<html><body><h1>first</h1></body></html>",
        "http://example.test/next": "<html><body><h1>next</h1></body></html>",
    }
    browser = (await Browser(resource_loader=lambda url, _request: Response(pages[url], {"url": url}),
                      stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    await page.goto("http://example.test/first")
    try:
        assert page.evaluate("document.location === location") is True
        page.evaluate("document.location = '/next'")
        (await page.wait_for("location.href === 'http://example.test/next' && document.readyState === 'complete'"))
        assert page.evaluate("[location.href, document.location.href, "
                             "document.querySelector('h1').textContent]") == [
            "http://example.test/next", "http://example.test/next", "next",
        ]
    finally:
        (await browser.close())


async def test_goto_decodes_http_charset_and_requires_a_loader_or_network_backend():
    html = "<html><body>中文页面</body></html>".encode("gb18030")
    browser = (await Browser(resource_loader=lambda url, _request: Response(
        html, {"url": url, "headers": {"content-type": "text/html; charset=gb18030"}},
    )).__aenter__())
    page = browser.new_profile().new_page()
    (await page.goto("http://example.test/chinese"))
    assert page.evaluate("document.body.textContent") == "中文页面"
    (await browser.close())

    browser = (await Browser(real_network=False).__aenter__())
    with pytest.raises(Exception, match="Network access requires"):
        await browser.new_profile().new_page().goto("http://example.test/")
    (await browser.close())


async def test_set_content_without_markup_starts_an_empty_standards_document():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        assert (await page.set_content(wait_until="commit")) is page
        assert page.evaluate(
            "[document.compatMode, document.documentElement.tagName, document.body.textContent]"
        ) == ["CSS1Compat", "HTML", ""]
    finally:
        (await browser.close())


# -- a navigation that does not finish says why ---------------------------------


async def test_a_navigation_that_times_out_names_the_state_and_what_is_still_loading():
    """A blocking script that never arrives is what the wait was about."""
    release = threading.Event()

    def loader(url, request):
        if url.endswith("/hangs.js"):
            release.wait(10)
        return Response(
            "<html><head><script src='/hangs.js'></script></head><body>waiting</body></html>",
            {"url": url, "headers": {"content-type": "text/html"}},
        )

    browser = (await Browser(stub_logging="off", threaded_loaders=True, resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        started = time.perf_counter()
        with pytest.raises(HostStateError, match="waiting for domcontentloaded") as caught:
            (await page.goto("http://example.test/page", wait_until="domcontentloaded", timeout=0.5))
        elapsed = time.perf_counter() - started
        text = str(caught.value)
        assert "timed out after 0.5s" in text
        assert "still loading 1 script" in text
        assert "the document request took" in text
        # The navigation gives up, not the executor: the grace is there so the
        # reason survives instead of a RealmBusyError with nothing in it.
        assert elapsed < 0.5 + 5
    finally:
        release.set()
        (await browser.close())


async def test_a_resource_that_fails_puts_its_reason_in_the_log():
    """The page hears Chrome's wording; the log keeps what actually happened."""
    messages = []
    token = logger.add(lambda message: messages.append(str(message).strip()), format="{message}")

    def loader(url, request):
        if url.endswith("/app.js"):
            raise RuntimeError("no such file")
        return Response(
            "<html><body><script src='/app.js'></script>loaded</body></html>",
            {"url": url, "headers": {"content-type": "text/html"}},
        )

    browser = (await Browser(stub_logging="off", resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto("http://example.test/index.html", wait_until="load", timeout=5))
        (await page.run_until_idle(timeout=1))
    finally:
        logger.remove(token)
        (await browser.close())
    failed = [line for line in messages if "resource failed" in line]
    assert len(failed) == 1
    assert "http://example.test/app.js" in failed[0]
    assert "no such file" in failed[0]

