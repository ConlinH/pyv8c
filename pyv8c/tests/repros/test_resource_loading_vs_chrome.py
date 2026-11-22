"""Request mode and source policy, with loader and HTTP against local Chrome."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import urlsplit

import pytest

pytest.importorskip("pyv8c.pyv8")
from harness import chrome_reference, ReferenceUnavailable


@pytest.fixture(scope="module")
async def reference():
    try:
        async with chrome_reference() as session:
            yield session
    except ReferenceUnavailable as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def origins():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path != "/blocked":
                self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "text/html" if self.path == "/page" else "text/plain")
            self.end_headers()
            body = "<body>local</body>" if self.path == "/page" else self.headers.get("Sec-Fetch-Mode", "missing")
            self.wfile.write(body.encode())
        def log_message(self, *args):
            pass
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    try:
        yield [f"http://127.0.0.1:{s.server_port}" for s in servers]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()


@pytest.mark.parametrize("transport", ["loader", "http"])
@pytest.mark.parametrize("source,mode", [("same", "same-origin"), ("same", "no-cors"),
                                         ("cross", "same-origin"), ("cross", "cors"),
                                         ("blocked", "cors"), ("cross", "no-cors"), ("data", "cors")])
async def test_source_and_mode_match_chrome(reference, origins, transport, source, mode):
    from pyv8c.async_api import Browser
    same, other = origins
    target = {"same": same + "/probe", "cross": other + "/probe",
              "blocked": other + "/blocked", "data": "data:text/plain,local"}[source]
    script = """window.__answer=null; fetch(%s, {mode:%s}).then(r => r.text().then(text => {
        window.__answer={status:r.status,text:text};
    })).catch(e => {window.__answer={error:e.name};}); 'started'""" % (json.dumps(target), json.dumps(mode))
    chrome = (await reference.browser.new_page())
    try:
        (await chrome.goto(same + "/page"))
        (await chrome.evaluate(script))
        (await chrome.wait_for_function("window.__answer !== null"))
        expected = (await chrome.evaluate("window.__answer"))
    finally:
        (await chrome.close())
    def loader(url, request):
        path = urlsplit(url).path
        return {"body": "<body>local</body>" if path == "/page" else request["headers"].get("Sec-Fetch-Mode", "missing"),
                "headers": {} if path == "/blocked" else {"access-control-allow-origin": "*"}}
    browser = (await Browser(real_network=transport == "http", resource_loader=loader if transport == "loader" else None,
                      stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto(same + "/page")
        page.evaluate(script)
        (await page.wait_for("window.__answer !== null"))
        result = page.evaluate("JSON.stringify(window.__answer)")
        assert json.loads(result) == expected
    finally:
        (await browser.close())


@pytest.mark.parametrize("mode", ["cors", "no-cors", "same-origin"])
@pytest.mark.parametrize("cross_origin", [False, True])
async def test_blob_fetch_checks_creator_origin(reference, origins, mode, cross_origin):
    from pyv8c.async_api import Browser
    creator_origin, other = origins
    reader_origin = other if cross_origin else creator_origin
    script = """window.__answer=null; fetch(%s, {mode:%s}).then(r => r.text().then(text => {
        window.__answer={text:text};
    })).catch(e => {window.__answer={error:e.name};}); 'started'"""
    chrome_context = (await reference.browser.new_context())
    creator = (await chrome_context.new_page())
    reader = (await chrome_context.new_page())
    try:
        (await creator.goto(creator_origin + "/page"))
        (await reader.goto(reader_origin + "/page"))
        url = (await creator.evaluate("URL.createObjectURL(new Blob(['secret']))"))
        (await reader.evaluate(script % (json.dumps(url), json.dumps(mode))))
        (await reader.wait_for_function("window.__answer !== null"))
        expected = (await reader.evaluate("window.__answer"))
    finally:
        (await chrome_context.close())
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        context = browser.new_profile()
        creator, reader = context.new_page(), context.new_page()
        (await creator.set_content("<body></body>", url=creator_origin + "/page", wait_until="commit"))
        (await reader.set_content("<body></body>", url=reader_origin + "/page", wait_until="commit"))
        url = creator.evaluate("URL.createObjectURL(new Blob(['secret']))")
        reader.evaluate(script % (json.dumps(url), json.dumps(mode)))
        (await reader.wait_for("window.__answer !== null"))
        assert json.loads(reader.evaluate("JSON.stringify(window.__answer)")) == expected
    finally:
        (await browser.close())
