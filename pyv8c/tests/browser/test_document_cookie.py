"""Runtime-facing ``document.cookie`` behavior."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen

import pytest

pytest.importorskip("pyv8c.pyv8")
from pyv8c.async_api import Browser
from pyv8c.web.fetch.response import Response


class _CookieTestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body>cookie test</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CookieTestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
async def browser(local_server):
    def load(url, _request):
        with urlopen(url, timeout=5) as response:
            return Response(
                response.read(),
                {
                    "url": response.geturl(),
                    "status": response.status,
                    "statusText": response.reason,
                    "headers": dict(response.headers.items()),
                },
            )

    instance = (await Browser(resource_loader=load, stub_logging="off").__aenter__())
    try:
        yield instance
    finally:
        (await instance.close())


async def test_document_cookie_reads_writes_and_updates_shared_context(browser, local_server):
    context = browser.new_profile()
    page = context.new_page()
    await page.goto(f"{local_server}/app/index.html")
    assert page.evaluate("document.cookie") == ""
    assert page.evaluate("document.cookie = 'a=1'") == "a=1"
    assert page.evaluate("document.cookie") == "a=1"
    assert page.evaluate("document.cookie = 'b=hello%20world; Path=/app'") == "b=hello%20world; Path=/app"
    assert page.evaluate("document.cookie") == "a=1; b=hello%20world"
    other = context.new_page()
    await other.goto(f"{local_server}/app/other")
    assert other.evaluate("document.cookie") == "a=1; b=hello%20world"


async def test_document_cookie_is_an_enumerable_prototype_accessor_and_ignores_opaque_urls(browser):
    page = browser.new_profile().new_page()
    descriptor = page.evaluate("Object.getOwnPropertyDescriptor(Document.prototype, 'cookie')")
    assert descriptor["get"] is not None
    assert descriptor["set"] is not None
    assert descriptor["enumerable"] is True
    assert descriptor["configurable"] is True
    assert page.evaluate("document.cookie = 'opaque=1'") == "opaque=1"
    assert page.evaluate("document.cookie") == ""


async def test_document_cookie_honors_path_httponly_and_deletion(browser, local_server):
    context = browser.new_profile()
    page = context.new_page()
    await page.goto(f"{local_server}/app/index.html")
    page.evaluate("document.cookie = 'root=1; Path=/'")
    page.evaluate("document.cookie = 'nested=2; Path=/app' ")
    context.cookies.add_from_response(f"{local_server}/app/index.html", "secret=3; HttpOnly")
    page.evaluate("document.cookie = 'secure=4; Secure'")
    assert page.evaluate("document.cookie") == "nested=2; root=1"
    assert context.cookies.header_for(f"{local_server}/app/index.html") == "nested=2; secret=3; root=1"
    page.evaluate("document.cookie = 'nested=; Max-Age=0; Path=/app'")
    assert page.evaluate("document.cookie") == "root=1"


async def test_document_cookie_ignores_invalid_script_assignments_and_logs_changes(browser, local_server, monkeypatch):
    messages = []
    monkeypatch.setattr(
        "pyv8c.network.cookies.logger.debug",
        lambda message, *values: messages.append((message, *values)),
    )
    page = browser.new_profile().new_page()
    await page.goto(f"{local_server}/")
    page.evaluate("document.cookie = 'bad name=value'")
    assert page.evaluate("document.cookie") == ""
    page.evaluate("document.cookie = 'ok=1'")
    assert page.evaluate("document.cookie = 'ok=1'") == "ok=1"
    assert [entry for entry in messages if "cookie" in entry[0]] == [("cookie changed: {}", "ok=1")]


async def test_document_cookie_cannot_overwrite_a_httponly_cookie(browser, local_server):
    context = browser.new_profile()
    page = context.new_page()
    await page.goto(f"{local_server}/")
    context.cookies.add_from_response(f"{local_server}/", "session=server; HttpOnly")
    page.evaluate("document.cookie = 'session=script'")
    assert page.evaluate("document.cookie") == ""
    assert context.cookies.header_for(f"{local_server}/") == "session=server"
