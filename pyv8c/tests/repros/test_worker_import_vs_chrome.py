"""Classic Worker synchronous imports use plain transport data and entry URLs."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from pyv8c.async_api import Browser
from harness import chrome_reference

pytestmark = pytest.mark.slow


@pytest.fixture(scope='module')
async def reference():
    async with chrome_reference() as session:
        yield session


@pytest.mark.parametrize('source_kind', ['http', 'blob', 'data', 'redirect-entry'])
async def test_worker_sync_imports_match_chrome(reference, source_kind):
    seen = []
    source = ''
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            status, headers = 200, {}
            if self.path == '/dir/entry.js':
                body = source
            elif self.path == '/start.js':
                status, headers, body = 302, {'Location': '/dir/entry.js'}, ''
            elif self.path == '/redirect.js':
                status, headers, body = 302, {'Location': '/dir/first.js'}, ''
            elif self.path == '/dir/first.js':
                body = "self.order.push('first');importScripts(self.second);"
            elif self.path == '/dir/second.js':
                body = "self.order.push('second');"
            elif self.path == '/bad.js':
                status, body = 404, "self.order.push('must-not-run')"
            else:
                body = '<body></body>'
            body = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/javascript' if self.path.endswith('.js') else 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    second = 'second.js' if source_kind in ('http', 'redirect-entry') else url+'dir/second.js'
    source = (f'self.second={json.dumps(second)};self.order=[];'
              f'importScripts({json.dumps(url+"redirect.js")});'
              f'try{{importScripts({json.dumps(url+"bad.js")});}}catch(e){{order.push(e.name)}}'
              'order.push("after");postMessage(order)')
    if source_kind in ('http', 'redirect-entry'):
        entry = 'start.js' if source_kind == 'redirect-entry' else 'dir/entry.js'
        worker = f'new Worker({json.dumps(url+entry)})'
    elif source_kind == 'blob':
        worker = f'new Worker(URL.createObjectURL(new Blob([{json.dumps(source)}],{{type:"text/javascript"}})))'
    else:
        worker = f'new Worker("data:text/javascript,"+encodeURIComponent({json.dumps(source)}))'
    setup = f'globalThis.result=null;globalThis.w={worker};w.onmessage=e=>result=e.data;void 0'
    browser = (await Browser(real_network=True, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    try:
        page = browser.new_profile().new_page()
        (await page.goto(url))
        page.evaluate(setup)
        (await page.wait_for('result!==null', timeout=5))
        observed = page.evaluate('result')
        (await chrome.goto(url))
        (await chrome.evaluate(setup))
        (await chrome.wait_for_function('result!==null', timeout=5000))
        expected = (await chrome.evaluate('result'))
        assert observed == expected == ['first', 'second', 'NetworkError', 'after']
        assert '/redirect.js' in seen and '/dir/second.js' in seen
    finally:
        (await chrome.close())
        (await browser.close())
        server.shutdown()
        server.server_close()
        (await asyncio.to_thread(thread.join, 2))
