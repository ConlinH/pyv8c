"""What a form submission puts on the wire, held to Chrome 153's baseline.

Every expectation is a transcription of a request a local Chrome made for the
same page: the method, the request line, the body bytes and the headers in
arrival order.  A click comes from the host and so is a user activation, which
is the one thing a page's own ``element.click()`` is not.
"""

import http.server
import threading
from urllib.parse import urlparse

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytest.importorskip("curl_cffi", reason="the curl_cffi engine is not installed")
pytestmark = pytest.mark.native

from pyv8c.async_api import Browser

FORM = ('<!doctype html><html><body><form method=post action="/echo">'
        '<p><input name=a value=1></p><p><button name=b value=bv>Go</button></p>'
        '</form></body></html>')
from _shared_web_pages import GET_FORM, EMPTY_GET, KINDS, PLAIN_ENC, MULTIPART_ENC

PAGES = {"/plain": FORM, "/script": FORM, "/get": GET_FORM, "/empty-get": EMPTY_GET,
         "/kinds": KINDS, "/plain-enc": PLAIN_ENC, "/multi-enc": MULTIPART_ENC}

#: Chrome's order for a form POST; ``host``, ``content-length`` and
#: ``connection`` are the transport's to place (``docs/pyv8c/request-headers.md``).
POSTED_NAVIGATION = [
    "host", "cache-control", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "upgrade-insecure-requests", "content-type", "user-agent", "origin", "accept",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-user", "sec-fetch-dest", "referer",
    "accept-encoding", "accept-language", "content-length", "connection",
]

RECORDS: list[dict] = []
LOCK = threading.Lock()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        with LOCK:
            RECORDS.append({
                "method": self.command,
                "path": self.path,
                "headers": list(self.headers.items()),
                "body": body,
            })
        payload = PAGES.get(urlparse(self.path).path, "")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload.encode())))
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
async def submissions():
    """One submission per trigger, recorded as the server received it."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    browser = (await Browser(real_network=True, stub_logging="off").__aenter__())
    collected = {}
    try:
        page = browser.new_profile().new_page()

        async def submit(name, path, drive):
            with LOCK:
                RECORDS.clear()
            (await page.goto(f"{origin}{path}", wait_until="load"))
            await drive(page)
            (await page.wait_for("location.pathname === '/echo' && document.readyState === 'complete'", timeout=5))
            with LOCK:
                records = list(RECORDS)
            collected[name] = next((record for record in records
                                    if urlparse(record["path"]).path == "/echo"), None)
            collected[name + ".landed"] = page.url

        (await submit("click", "/plain", lambda page: page.click("form button")))
        async def script_submit(page):
            page.evaluate("document.querySelector('form').submit()")
        async def script_click(page):
            page.evaluate("document.querySelector('form button').click()")
        (await submit("script", "/script", script_submit))
        (await submit("untrusted-click", "/script",
               script_click))
        (await submit("get", "/get", lambda page: page.click("form button")))
        (await submit("empty-get", "/empty-get", lambda page: page.click("form button")))
        (await submit("kinds", "/kinds", lambda page: page.click("form button")))
        (await submit("text-plain", "/plain-enc", lambda page: page.click("form button")))
        (await submit("multipart", "/multi-enc", lambda page: page.click("form button")))
    finally:
        (await browser.close())
        server.shutdown()
        server.server_close()
    yield collected


def headers_of(record):
    """The recorded headers lower-cased, in arrival order."""
    return {name.lower(): value for name, value in record["headers"]}


async def test_a_clicked_submit_button_posts_the_entry_list(submissions):
    record = submissions["click"]
    assert (record["method"], record["path"]) == ("POST", "/echo")
    assert record["body"] == b"a=1&b=bv"
    sent = headers_of(record)
    assert list(sent) == POSTED_NAVIGATION
    assert sent["content-type"] == "application/x-www-form-urlencoded"
    assert sent["cache-control"] == "max-age=0"
    assert sent["sec-fetch-user"] == "?1"
    assert sent["sec-fetch-mode"] == "navigate"
    assert sent["sec-fetch-dest"] == "document"
    assert sent["upgrade-insecure-requests"] == "1"
    assert sent["origin"].startswith("http://127.0.0.1:")
    assert sent["referer"].endswith("/plain")


async def test_a_script_submission_sends_no_submitter_and_no_user_activation(submissions):
    record = submissions["script"]
    assert (record["method"], record["path"]) == ("POST", "/echo")
    assert record["body"] == b"a=1"
    sent = headers_of(record)
    assert "sec-fetch-user" not in sent
    assert sent["origin"].startswith("http://127.0.0.1:")
    assert sent["cache-control"] == "max-age=0"
    assert sent["content-type"] == "application/x-www-form-urlencoded"


async def test_an_untrusted_click_submits_its_button_without_user_activation(submissions):
    """A page's own ``button.click()`` sends the button's entry, but is no gesture."""
    record = submissions["untrusted-click"]
    assert record["body"] == b"a=1&b=bv"
    assert "sec-fetch-user" not in headers_of(record)


async def test_a_get_submission_replaces_the_actions_query(submissions):
    record = submissions["get"]
    assert record["method"] == "GET"
    assert record["path"] == "/echo?sp=hello+world&b=x+y"
    assert record["body"] == b""
    assert submissions["get.landed"].endswith("/echo?sp=hello+world&b=x+y")
    sent = headers_of(record)
    assert "origin" not in sent
    assert "content-type" not in sent
    assert "cache-control" not in sent


async def test_a_get_submission_with_no_entries_still_has_a_query(submissions):
    record = submissions["empty-get"]
    assert (record["method"], record["path"]) == ("GET", "/echo?")
    assert submissions["empty-get.landed"].endswith("/echo?")


async def test_the_entry_list_leaves_out_what_the_browser_leaves_out(submissions):
    """Chrome sent ``t=v&on=on&s=o1&ta=text&b=bv`` for this page."""
    assert submissions["kinds"]["body"] == b"t=v&on=on&s=o1&ta=text&b=bv"


async def test_text_plain_is_line_per_entry(submissions):
    record = submissions["text-plain"]
    assert record["body"] == b"a=1\r\nb=2\r\n"
    assert headers_of(record)["content-type"] == "text/plain"


async def test_multipart_form_data_delimits_each_entry(submissions):
    record = submissions["multipart"]
    content_type = headers_of(record)["content-type"]
    prefix = "multipart/form-data; boundary="
    assert content_type.startswith(prefix)
    boundary = content_type[len(prefix):]
    assert record["body"] == (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="a"\r\n'
        f"\r\n1\r\n"
        f"--{boundary}--\r\n"
    ).encode()
