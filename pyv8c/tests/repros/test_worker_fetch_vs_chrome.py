"""Worker fetch wire values, opaque origin and abort checkpoints against Chrome."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import quote

import pytest

from pyv8c.async_api import Browser
from harness import chrome_reference


@pytest.fixture(scope='module')
async def reference():
    async with chrome_reference() as result:
        yield result


@pytest.mark.parametrize('case', ['same-origin','data-allow','data-deny','preaborted','abort-order'])
async def test_worker_fetch_matches_chrome(reference, case):
    hits = []
    if case == 'preaborted':
        source = ("onmessage=()=>{const c=new AbortController(),reason={tag:42},order=[];"
                  "c.abort(reason);fetch('/value',{signal:c.signal})"
                  ".catch(e=>order.push(e===reason?'rejected':'wrong'));"
                  "queueMicrotask(()=>order.push('micro'));setTimeout(()=>postMessage(order),0)}")
    elif case == 'abort-order':
        source = ("onmessage=()=>{const c=new AbortController(),reason={tag:42},order=[];"
                  "c.signal.addEventListener('abort',()=>order.push('event'));"
                  "fetch('/value',{signal:c.signal}).catch(e=>order.push(e===reason?'rejected':'wrong'));"
                  "c.abort(reason);order.push('after');queueMicrotask(()=>order.push('micro'));"
                  "setTimeout(()=>postMessage(order),0)}")
    else:
        source = ''  # Filled once the local origin is known.
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply()
        def do_POST(self):
            self.reply()
        def reply(self):
            if self.path == '/worker.js':
                body, kind = source.encode(), 'text/javascript'
            elif self.path == '/value':
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                hits.append(body)
                body = json.dumps(dict(body=body.decode(), header=self.headers.get('X-Test',''),
                                       origin=self.headers.get('Origin',''))).encode()
                kind = 'application/json'
            else:
                body, kind = b'<body></body>', 'text/html'
            self.send_response(200)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Access-Control-Allow-Origin',url[:-1] if case == 'data-deny' else '*')
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    if case not in ('preaborted','abort-order'):
        target = '/value' if case == 'same-origin' else url+'value'
        init = "{method:'POST',headers:{'X-Test':'yes'},body:new Uint8Array([65,66])}" if case == 'same-origin' else '{}'
        source = ("onmessage=()=>fetch("+json.dumps(target)+","+init+")"
                  ".then(async r=>postMessage([r instanceof Response,r.headers instanceof Headers,"
                  "r.constructor===Response,JSON.parse(await r.text())]))"
                  ".catch(e=>postMessage(['error',e.name,e.message]))")
    results = []
    try:
        for kind in ('chrome','pyv8c'):
            hits.clear()
            browser = (await Browser(stub_logging='off').__aenter__()) if kind == 'pyv8c' else None
            page = browser.new_profile().new_page() if browser else (await reference.browser.new_page())
            runtimes = ()
            try:
                (await page.goto(url))
                worker_url = 'data:text/javascript,'+quote(source,safe='') if case.startswith('data-') else url+'worker.js'
                ((await page.evaluate("globalThis.messages=[];globalThis.worker=new Worker("+json.dumps(worker_url)+");"
                              "worker.onmessage=e=>messages.push(e.data);worker.postMessage('start')")) if not browser else page.evaluate("globalThis.messages=[];globalThis.worker=new Worker("+json.dumps(worker_url)+");"
                              "worker.onmessage=e=>messages.push(e.data);worker.postMessage('start')"))
                if browser:
                    runtimes = page.executor.call(lambda: tuple(w._runtime for w in page._owned.workers))
                    (await page.wait_for('messages.length===1', timeout=3))
                else:
                    (await page.wait_for_function('messages.length===1', timeout=3000))
                results.append(((await page.evaluate('messages')) if not browser else page.evaluate('messages')))
                if case == 'abort-order':
                    assert len(hits) <= 1  # An in-flight abort can race with transmission.
                else:
                    assert len(hits) == (0 if case == 'preaborted' else 1)
            finally:
                (await browser.close()) if browser else (await page.close())
                for runtime in runtimes:
                    (await asyncio.to_thread(runtime.executor._thread.join, 2))
                    assert not runtime.executor._thread.is_alive()
        assert results[0] == results[1]
        if case == 'preaborted':
            assert results[0] == [['rejected','micro']]
        elif case == 'abort-order':
            assert results[0] == [['event','after','rejected','micro']]
        elif case == 'data-deny':
            assert results[0] == [['error','TypeError','Failed to fetch']]
        else:
            expected = dict(body='AB',header='yes',origin=url[:-1]) if case == 'same-origin' else dict(body='',header='',origin='null')
            assert results[0] == [[True,True,True,expected]]
    finally:
        server.shutdown()
        server.server_close()
        (await asyncio.to_thread(thread.join, 2))
        assert not thread.is_alive()
