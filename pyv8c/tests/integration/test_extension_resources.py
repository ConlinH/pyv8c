"""A page reading a resource out of an installed extension.

Chrome answers an installed extension's web-accessible resources locally -- nothing
leaves the browser -- and a page whose origin the extension's ``matches`` names reads
one the way it reads a same-origin response.  Every other probe fails: an unknown id, a
path the extension does not expose, and an extension that is not running.  The shapes
here are what Chrome 154 answers, measured against a profile with one enabled extension
and one suspended one.
"""
import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")

from pyv8c.async_api import Browser
from pyv8c.foundation.urls import origin_string

#: Installed and answering; suspended, so it answers a page with nothing; and absent.
INSTALLED = "cneomjecgakdfoeehmmmoiklncdiodmh"
SUSPENDED = "aapbdbdomjkkjkaonfhkkikfgjllcleb"
UNKNOWN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

ICON = "icons/icon_64.png"
EXTENSIONS = {
    INSTALLED: [{"resources": [f"/{ICON}", "/_locales/*/messages.json"],
                 "matches": ["http://*/*", "https://*/*"]}],
}
PAGE = "https://www.radissonhotels.com/zh-cn/"


def url(identifier, path):
    return f"chrome-extension://{identifier}/{path}"


async def open_page(extensions=EXTENSIONS, **overrides):
    browser = (await Browser(stub_logging="off", environment={"extensions": extensions}, **overrides).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url=PAGE, wait_until="commit"))
    return browser, page


async def rows(page, script):
    page.evaluate("window.__rows = [];" + script)
    (await page.run_until_idle(2))
    return page.evaluate("window.__rows")


async def test_an_installed_extension_answers_a_page_the_way_chrome_does():
    browser, page = (await open_page())
    try:
        collected = (await rows(page, """
          const push = (...row) => window.__rows.push(row);
          fetch(%(icon)s).then(
            async (response) => push('file', response.status, response.type,
                                     (await response.arrayBuffer()).byteLength > 0),
            (error) => push('file-error', error.name, error.message));
          fetch(%(locale)s).then(
            (response) => push('glob', response.status, response.type),
            (error) => push('glob-error', error.name));
        """ % {"icon": repr(url(INSTALLED, ICON)),
               "locale": repr(url(INSTALLED, "_locales/am/messages.json"))}))
    finally:
        (await browser.close())
    assert collected == [["file", 200, "basic", True], ["glob", 200, "basic"]]


async def test_intercepted_extension_url_keeps_cors_response_filtering():
    target = url(INSTALLED, ICON)

    def intercept(address, request):
        if address == target:
            return {"body": "fixture", "headers": {
                "access-control-allow-origin": origin_string(PAGE),
                "access-control-expose-headers": "x-exposed",
                "x-exposed": "visible",
                "x-other": "other",
            }}

    browser, page = await open_page(real_network=False, resource_interceptor=intercept)
    try:
        await rows(page, """
          globalThis.results = {};
          const address = %(address)r;
          fetch(address).then(response => {
            results.fetch = [response.type, response.headers.get('x-exposed'),
                             response.headers.get('x-other') === null];
          });
          const xhr = new XMLHttpRequest();
          xhr.open('GET', address);
          xhr.onload = () => {
            results.xhr = [xhr.getResponseHeader('x-exposed'), xhr.getResponseHeader('x-other') === null];
          };
          xhr.send();
        """ % {"address": target})
        assert page.evaluate("results") == {"fetch": ["cors", "visible", True], "xhr": ["visible", True]}
    finally:
        await browser.close()


async def test_a_probe_that_should_fail_fails_for_every_reason_and_none_is_asked_of_the_engine():
    """A missing id, an undeclared path and an extension that is not running read alike."""
    asked = []

    class Transport:
        def submit_request(self, request, handle, remaining):
            asked.append(request.url)
            raise AssertionError("the engine was asked for an extension's resource")

        def close(self):
            pass

    browser, page = (await open_page(http_engine=Transport))
    try:
        collected = (await rows(page, """
          const push = (...row) => window.__rows.push(row);
          fetch(%(path)s).catch((error) => push('undeclared-path', error.name, error.message));
          fetch(%(id)s).catch((error) => push('unknown-id', error.name, error.message));
          fetch(%(suspended)s).catch((error) => push('suspended', error.name, error.message));
        """ % {"path": repr(url(INSTALLED, "content.css")),
               "id": repr(url(UNKNOWN, ICON)),
               "suspended": repr(url(SUSPENDED, "popup_css_compiled.css"))}))
    finally:
        (await browser.close())
    assert collected == [["undeclared-path", "TypeError", "Failed to fetch"],
                         ["unknown-id", "TypeError", "Failed to fetch"],
                         ["suspended", "TypeError", "Failed to fetch"]]
    assert asked == []


async def test_an_image_of_the_resource_loads_and_reports_its_size():
    browser, page = (await open_page())
    try:
        collected = (await rows(page, """
          const push = (...row) => window.__rows.push(row);
          const image = new Image();
          image.onload = () => push('image', image.complete, image.naturalWidth > 0);
          image.onerror = () => push('image', 'error');
          image.src = %(icon)s;
          const blocked = new Image();
          blocked.onload = () => push('undeclared', 'loaded');
          blocked.onerror = () => push('undeclared', 'error');
          blocked.src = %(path)s;
        """ % {"icon": repr(url(INSTALLED, ICON)), "path": repr(url(INSTALLED, "content.css"))}))
    finally:
        (await browser.close())
    assert collected == [["image", True, True], ["undeclared", "error"]]


async def test_a_page_the_declaration_does_not_name_cannot_read_the_resource():
    narrow = {INSTALLED: [{"resources": [ICON], "matches": ["https://elsewhere.test/*"]}]}
    browser, page = (await open_page(narrow))
    try:
        collected = (await rows(page, """
          fetch(%s).catch((error) => window.__rows.push(error.name));
        """ % repr(url(INSTALLED, ICON))))
    finally:
        (await browser.close())
    assert collected == ["TypeError"]
