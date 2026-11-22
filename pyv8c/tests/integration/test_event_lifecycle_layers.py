"""The three lifecycle layers: document/window, navigation/visibility, resources."""

import asyncio
import time

from pyv8c.async_api import Browser
from pyv8c.web.fetch.response import Response


async def _wait_for_page_load(page, timeout=5.0):
    """Wait until this realm reports ``load``.

    The iframe's ``load``/``DOMFrameContentLoaded``/``loadend`` are dispatched
    by the child realm, and the child's completion is posted back to this
    realm's queue, so a load-wait covers every frame event as well.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not page.lifecycle.document_load_fired:
        (await page.run_until_idle())
        (await asyncio.sleep(0.005))
    assert page.lifecycle.document_load_fired, "the page never reached load"


async def test_deferred_scripts_run_before_dom_content_loaded_and_after_ready_state():
    def loader(url, request):
        if url.endswith("/deferred.js"):
            return Response("window.__order.push('deferred')", {"url": url, "content-type": "text/javascript"})
        return Response("", {"url": url})

    browser = (await Browser(resource_loader=loader, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""
            window.__order = [];
            window.addEventListener('DOMContentLoaded', () => __order.push('domcontentloaded'));
            window.addEventListener('load', () => __order.push('load'));
        """)
        (await page.set_content("""
            <html><head>
              <script defer src='/deferred.js'></script>
              <script>document.addEventListener('readystatechange',
                  () => window.__order.push('rs:' + document.readyState));</script>
            </head><body></body></html>
        """, url="https://example.test/", wait_until="commit"))
        (await page.run_until_idle())
        assert page.evaluate("__order") == [
            "rs:interactive", "deferred", "domcontentloaded", "rs:complete", "load",
        ]
    finally:
        (await browser.close())


async def test_page_show_and_hide_are_page_transition_events():
    record = """
        const mark = (kind, event) => sessionStorage.setItem(kind,
            [event instanceof PageTransitionEvent, event.persisted].join());
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(
            "<body></body><script>" + record +
            "addEventListener('pagehide', e => mark('hide', e));</script>",
            url="https://example.test/first",
         wait_until="commit"))
        (await page.run_until_idle())
        (await page.set_content(
            "<body>next</body><script>" + record +
            "addEventListener('pageshow', e => mark('show', e));</script>",
            url="https://example.test/second",
         wait_until="commit"))
        (await page.run_until_idle())
        assert page.evaluate(
            "[sessionStorage.getItem('hide'), sessionStorage.getItem('show')]"
        ) == ["true,false", "true,false"]
    finally:
        (await browser.close())


async def test_hashchange_and_popstate_carry_their_fields():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/page#one", wait_until="commit"))
        (await page.run_until_idle())
        page.evaluate("""
            window.__nav = [];
            window.addEventListener('hashchange', e => __nav.push(
                ['hash', e.oldURL, e.newURL, e.isTrusted]));
            window.addEventListener('popstate', e => __nav.push(
                ['pop', e.state === null ? 'null' : e.state]));
        """)
        page.evaluate("location.hash = '#two'")
        assert page.evaluate("[location.href, history.length]") == ["https://example.test/page#two", 2]
        assert page.evaluate("__nav") == [
            ["hash", "https://example.test/page#one", "https://example.test/page#two", True],
        ]

        page.evaluate("history.back()")
        (await page.run_until_idle())
        assert page.evaluate("[location.href, __nav.length]") == ["https://example.test/page#one", 3]
        assert page.evaluate("__nav[1]") == ["pop", "null"]
        assert page.evaluate("__nav[2]") == [
            "hash", "https://example.test/page#two", "https://example.test/page#one", True,
        ]

        page.evaluate("history.pushState({n: 5}, '', '/page#one')")
        page.evaluate("history.back()")
        (await page.run_until_idle())
        page.evaluate("history.forward()")
        (await page.run_until_idle())
        # Forward returns to the pushed entry, whose state shows up on popstate.
        assert page.evaluate("__nav[__nav.length - 1]") == ["pop", {"n": 5}]
        # A same-document entry change keeps the document alive.
        assert page.evaluate("document.readyState") == "complete"
    finally:
        (await browser.close())


async def test_location_assign_and_replace_keep_the_document_alive():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/page#one", wait_until="commit"))
        (await page.run_until_idle())
        # A listener on this document body only survives if the fragment
        # navigations below stay in the same document.
        page.evaluate("document.body.addEventListener('probe', () => window.__kept = true)")
        page.evaluate("location.assign('#two')")
        assert page.evaluate("[location.hash, history.length]") == ["#two", 2]
        page.evaluate("location.replace('#three')")
        assert page.evaluate("[location.hash, history.length]") == ["#three", 2]
        page.evaluate("document.body.dispatchEvent(new Event('probe'))")
        assert page.evaluate("window.__kept") is True
    finally:
        (await browser.close())


async def test_resource_element_and_frame_events():
    def loader(url, request):
        if url.endswith("/image.png"):
            return Response(b"0123456789", {"url": url, "headers": {"content-type": "image/png"}})
        if url.endswith("/frame.html"):
            return Response("<body>frame</body>", {"url": url, "headers": {"content-type": "text/html"}})
        return Response("", {"url": url})

    browser = (await Browser(resource_loader=loader, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("""
            <script>
              window.__resource = [];
              for (const type of ['loadstart', 'progress', 'load', 'loadend', 'error', 'DOMFrameContentLoaded']) {
                document.addEventListener(type, e => __resource.push(
                    [type, e.target.localName, e.lengthComputable === undefined ? null : e.loaded]), true);
              }
            </script>
            <img id='im' src='/image.png'>
            <iframe id='fr' src='/frame.html'></iframe>
        """, url="https://example.test/", wait_until="commit"))
        (await _wait_for_page_load(page))
        events = page.evaluate("__resource")
        image = [item[0] for item in events if item[1] == "img"]
        frame = [item[0] for item in events if item[1] == "iframe"]
        assert image == ["loadstart", "progress", "load", "loadend"]
        assert [item[2] for item in events if item[0] == "progress"] == [10]
        assert frame == ["loadstart", "load", "DOMFrameContentLoaded", "loadend"]
    finally:
        (await browser.close())
