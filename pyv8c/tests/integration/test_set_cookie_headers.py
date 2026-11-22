"""Several ``Set-Cookie`` headers in one response: does every one reach the jar?

The steamdb.info runs of 2026-10-06 granted ``cf_clearance`` in a submit response -- the exchange
table read its name out of ``set-cookie`` -- and by the next exchange the jar held only
``cf_chl_rc_ni``.  A jar given that header keeps both cookies (``tests/unit/test_cookie_storage``
holds that), so the question is what the transport *hands* it: repeated headers are only visible
through ``multi_items``, and a plain view keeps just the last one.

A local server sending two headers answers it without touching the site.
"""
import http.server
import threading

import pytest

pytest.importorskip("pyv8c.pyv8", reason="the engine is imported by the package under test")

from pyv8c.async_api import Browser

BODY = b"<!doctype html><body>cookies"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the name the base class calls
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("Set-Cookie", "first=1; Path=/")
        self.send_header("Set-Cookie", "second=2; Path=/")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *arguments):  # keep the test's output about the test
        pass


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()
        httpd.server_close()


async def test_every_set_cookie_header_of_one_response_reaches_the_jar(server):
    browser = (await Browser(stub_logging="off", real_network=True, threaded_loaders=True).__aenter__())
    try:
        context = browser.new_profile()
        page = context.new_page()
        (await page.goto(server))
        stored = sorted(name for (_domain, _path, name)
                        in dict(getattr(context.cookies, "_cookies", {}) or {}))
        assert stored == ["first", "second"], (
            f"the jar kept {stored}: repeated Set-Cookie headers reached it as one value"
        )
    finally:
        (await browser.close())
