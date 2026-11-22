"""``page.net.fetch``/``page.net.xhr``: host calls that run the page's own JS APIs.

What these methods are for is the implementation the *page* has: a page that
replaced ``fetch`` or ``XMLHttpRequest`` -- instrumentation, a test harness -- is
the one that answers, and the request carries the page's rules (CORS, the
interceptor, the cache) instead of the transport the ``get``/``post`` helpers run on.
"""

import asyncio
import json

import pytest

from pyv8c.async_api import Browser

#: ``NetworkError`` and ``InvalidStateError`` are DOMException factories here, so
#: the raised type is the exception they build.
from pyv8c.foundation.host_errors import LoadFailure

DOCUMENT = "<body></body>"


def loader(url, request):
    """Answer with what the request asked for, so a test can read it back."""
    return {
        "url": url,
        "headers": {"content-type": "application/json", "x-demo": "1"},
        "body": json.dumps({
            "method": request.get("method"),
            "url": url,
            "body": request.get("body"),
        }),
    }


@pytest.fixture
async def page():
    browser = (await Browser(real_network=False, resource_loader=loader, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto("http://example.test/")
        yield page
    finally:
        (await browser.close())


async def test_fetch_reads_the_response_through_the_page_stack(page):
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    answer = (await page.net.fetch("/echo"))

    assert (answer.ok, answer.status, answer.url) == (True, 200, "http://example.test/echo")
    assert answer.headers["x-demo"] == "1"
    assert answer.json()["url"] == "http://example.test/echo"
    assert answer.text == answer.body


async def test_fetch_posts_a_body_and_headers(page):
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    answer = (await page.net.fetch("/echo", method="POST", body="payload", headers={"X-Test": "1"}))

    seen = answer.json()
    assert seen["method"] == "POST"
    assert seen["body"] == "payload"


async def test_fetch_uses_a_replaced_fetch(page):
    """The page's own ``fetch`` is the implementation that runs."""
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    page.evaluate("""
      globalThis.seen = [];
      globalThis.fetch = (input, init) => {
        seen.push([String(input), init && init.method]);
        return Promise.resolve({
          ok: true, status: 299, statusText: 'Replaced', url: 'http://example.test/ovr',
          redirected: true, headers: {entries: () => [['x-made', 'by-page']]},
          text: () => Promise.resolve('from the page'),
        });
      };
    """)

    answer = (await page.net.fetch("/hooked", method="PUT"))

    assert page.evaluate("seen") == [["/hooked", "PUT"]]
    assert (answer.status, answer.status_text, answer.url) == (299, "Replaced", "http://example.test/ovr")
    assert (answer.headers, answer.body, answer.redirected) == ({"x-made": "by-page"}, "from the page", True)


async def test_fetch_async_settles_on_the_realm_queue(page):
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    future = asyncio.create_task(page.net.fetch("/echo"))

    answer = (await asyncio.wait_for(future, 10))
    assert answer.json()["url"] == "http://example.test/echo"


async def test_a_rejected_fetch_raises_the_pages_error(page):
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    page.evaluate("globalThis.fetch = () => Promise.reject(new TypeError('nope'));")

    with pytest.raises(LoadFailure, match="nope"):
        (await page.net.fetch("/echo"))
    with pytest.raises(LoadFailure, match="TypeError"):
        (await page.net.fetch("/echo"))


async def test_a_fetch_that_never_settles_times_out(page):
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    page.evaluate("globalThis.fetch = () => new Promise(() => {});")

    with pytest.raises(TimeoutError, match="did not settle"):
        (await page.net.fetch("/forever", timeout=0.2))


async def test_xhr_reads_the_response_text(page):
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    answer = (await page.net.xhr("/echo"))

    assert (answer.ok, answer.status) == (True, 200)
    assert answer.json()["url"] == "http://example.test/echo"


async def test_xhr_uses_a_replaced_xmlhttprequest(page):
    """A page that replaced the constructor is the one the host reaches."""
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    page.evaluate("""
      globalThis.made = 0;
      const Original = globalThis.XMLHttpRequest;
      globalThis.XMLHttpRequest = function () { made += 1; return new Original(); };
    """)

    (await page.net.xhr("/echo"))

    assert page.evaluate("made") == 1


async def test_xhr_async_settles_on_the_realm_queue(page):
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    future = asyncio.create_task(page.net.xhr("/echo"))

    assert (await asyncio.wait_for(future, 10)).json()["url"] == "http://example.test/echo"


async def test_fetch_and_xhr_reuse_the_driver_until_the_realm_is_replaced(page):
    realm = page.realm
    await page.net.fetch("/first")
    driver = realm.http_driver
    assert driver is not None
    page.evaluate("""
        const OriginalXHR = globalThis.XMLHttpRequest;
        globalThis.xhrCalls = 0;
        globalThis.XMLHttpRequest = function () { xhrCalls++; return new OriginalXHR(); };
    """)
    assert (await page.net.xhr("/second")).status == 200
    assert page.evaluate("xhrCalls") == 1
    assert realm.http_driver is driver

    await page.set_content(DOCUMENT, url="http://example.test/next", wait_until="commit")
    assert page.realm is not realm and realm.http_driver is None
    assert (await page.net.fetch("/third")).status == 200
    assert page.realm.http_driver is not driver


@pytest.mark.parametrize("method", ["fetch", "xhr"])
async def test_the_http_driver_preserves_string_code_units(page, method):
    body = "中😀\0\ud800end"
    page.evaluate("""
        globalThis.fetch = async (url, init) => ({
            status: 200, text: () => init.body,
            headers: {entries: () => [["x-value", init.headers["X-Test"]]]}
        });
        globalThis.XMLHttpRequest = class {
            open(method, url) { this.status = 201; this.responseURL = url; }
            setRequestHeader(name, value) { globalThis.seenHeader = value; }
            send(value) {
                this.responseText = value;
                this.readyState = 4;
                this.onreadystatechange();
            }
        };
    """)
    answer = await getattr(page.net, method)("/echo", body=body, headers={"X-Test": body})
    assert answer.text == body
    if method == "fetch":
        assert answer.headers == {"x-value": body}
    else:
        assert page.evaluate(r"seenHeader === '中😀\0\ud800end'") is True


async def test_fetch_passes_data_without_exposing_host_objects_or_using_page_json(page):
    page.evaluate("""
        JSON.stringify = () => { throw new Error("page JSON serializer"); };
        globalThis.fetch = async (url, init) => ({
            status: 200,
            text: () => init.headers["X-Options"][0] === null ? "null" : "changed"
        });
    """)
    assert (await page.net.fetch("/echo", headers={"X-Options": [None]})).text == "null"
    with pytest.raises(TypeError, match="not JSON serializable"):
        await page.net.fetch("/echo", headers={"X-Options": object()})


async def test_a_closed_page_refuses_both(page):
    (await page.set_content(DOCUMENT, url="http://example.test/", wait_until="commit"))
    (await page.close())

    with pytest.raises(RuntimeError, match="closed"):
        (await page.net.fetch("/echo"))
    with pytest.raises(RuntimeError, match="closed"):
        (await page.net.xhr("/echo"))
    # The async form hands the closed realm back through its future.
    with pytest.raises(RuntimeError, match="closed"):
        (await page.net.fetch("/echo"))
