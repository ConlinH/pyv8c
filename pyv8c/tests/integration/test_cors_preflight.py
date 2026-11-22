"""CORS preflight must settle before an unsafe request can have side effects."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
pytest.importorskip('pyv8c.pyv8')
from pyv8c.async_api import Browser


@pytest.fixture
def target():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            seen.append((self.command, dict(self.headers)))
            self.send_response(204)
            self.send_header('Set-Cookie', 'preflight=must-not-be-stored; Path=/')
            self.send_header('Access-Control-Allow-Origin', 'http://source.test')
            if self.path != '/deny':
                self.send_header('Access-Control-Allow-Methods', 'PUT')
                self.send_header('Access-Control-Allow-Headers', 'x-token, content-type')
            self.end_headers()
        def do_PUT(self):
            seen.append((self.command, dict(self.headers)))
            self.send_response(307 if self.path == '/redirect' else 200)
            self.send_header('Access-Control-Allow-Origin', 'http://source.test')
            if self.path == '/redirect':
                self.send_header('Location', '/deny')
            self.end_headers()
            self.wfile.write(b'ok')
        def do_GET(self):
            seen.append((self.command, dict(self.headers)))
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', 'http://source.test')
            self.end_headers()
            self.wfile.write(b'ok')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.parametrize('path,expected', [('/ok', 'ok'), ('/deny', 'TypeError')])
async def test_fetch_preflight_blocks_actual_request_until_allowed(target, path, expected):
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        page.evaluate("globalThis.result='pending'; fetch(" + repr(url + path) +
                      ", {method:'PUT', headers:{'X-Token':'secret'}})"
                      ".then(r=>r.text()).then(x=>result=x).catch(e=>result=e.name)")
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate('result') == expected
        assert [item[0] for item in seen] == (['OPTIONS', 'PUT'] if path == '/ok' else ['OPTIONS'])
        headers = {k.lower(): v for k, v in seen[0][1].items()}
        assert headers['origin'] == 'http://source.test'
        assert headers['access-control-request-method'] == 'PUT'
        assert headers['access-control-request-headers'] == 'x-token'
        assert 'cookie' not in headers and 'authorization' not in headers
        assert browser.profiles[0].cookies.header_for(url) == ''
    finally:
        (await browser.close())


async def test_simple_cross_origin_get_needs_no_preflight(target):
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        page.evaluate("globalThis.result='pending'; fetch(" + repr(url + '/ok') +
                      ").then(r=>r.text()).then(x=>result=x).catch(e=>result=e.name)")
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate('result') == 'ok'
        assert [item[0] for item in seen] == ['GET']
    finally:
        (await browser.close())


async def test_no_cors_does_not_send_unsafe_author_headers(target):
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        page.evaluate("globalThis.result='pending';fetch(" + repr(url + '/ok') +
                      ",{mode:'no-cors',headers:{'X-Token':'secret','Range':'bytes=0-1'}})"
                      ".then(()=>result='ok').catch(e=>result=e.name)")
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate('result') == 'ok'
        assert [item[0] for item in seen] == ['GET']
        assert 'x-token' not in {name.lower() for name in seen[0][1]}
        assert 'range' not in {name.lower() for name in seen[0][1]}
    finally:
        (await browser.close())


@pytest.mark.parametrize('mode', ['cors', 'no-cors'])
async def test_unsupported_stream_request_never_reaches_the_transport(target, mode):
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        page.evaluate("globalThis.result='pending';fetch(" + repr(url + '/ok') +
                      ",{method:'POST',mode:" + repr(mode) +
                      ",body:new ReadableStream(),duplex:'half'})"
                      ".then(()=>result='ok').catch(e=>result=e.name)")
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate('result') == 'TypeError'
        assert seen == []
    finally:
        (await browser.close())


async def test_xhr_denied_preflight_has_no_put_side_effect(target):
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        page.evaluate("globalThis.result='pending'; let x=new XMLHttpRequest(); x.open('PUT', " +
                      repr(url + '/deny') + "); x.setRequestHeader('X-Token','secret');"
                      "x.onload=()=>result='load';x.onerror=()=>result='error';x.send()")
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate('result') == 'error'
        assert [item[0] for item in seen] == ['OPTIONS']
    finally:
        (await browser.close())


async def test_redirect_checks_preflight_again_before_next_unsafe_hop(target):
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        page.evaluate("globalThis.result='pending'; fetch(" + repr(url + '/redirect') +
                      ", {method:'PUT', headers:{'X-Token':'secret'}})"
                      ".then(()=>result='load').catch(e=>result=e.name)")
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate('result') == 'TypeError'
        assert [item[0] for item in seen] == ['OPTIONS', 'PUT', 'OPTIONS']
    finally:
        (await browser.close())


async def test_credentialed_xhr_requires_preflight_allow_credentials(target):
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        page.evaluate("globalThis.result='pending'; let x=new XMLHttpRequest(); x.open('PUT', " +
                      repr(url + '/ok') + "); x.withCredentials=true;x.setRequestHeader('X-Token','secret');"
                      "x.onload=()=>result='load';x.onerror=()=>result='error';x.send()")
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate('result') == 'error'
        assert [item[0] for item in seen] == ['OPTIONS']
    finally:
        (await browser.close())


async def test_worker_denied_preflight_has_no_put_side_effect(target):
    import json
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        source = ("fetch(" + json.dumps(url + '/deny') +
                  ",{method:'PUT',headers:{'X-Token':'secret'}})"
                  ".then(()=>postMessage('load')).catch(e=>postMessage(e.name))")
        page.evaluate("globalThis.result='pending';globalThis.w=new Worker(URL.createObjectURL(new Blob([" +
                      json.dumps(source) + "])));w.onmessage=e=>result=e.data")
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate('result') == 'TypeError'
        assert [item[0] for item in seen] == ['OPTIONS']
    finally:
        (await browser.close())


async def test_sync_resource_load_checks_preflight_before_sending(target):
    from pyv8c.foundation.host_errors import LoadFailure
    url, seen = target
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        with pytest.raises(LoadFailure):
            page.executor.call(page.services.resource_services.load_resource, url + '/deny',
                               {'method': 'PUT', 'headers': {'X-Token': 'secret'}})
        assert [item[0] for item in seen] == ['OPTIONS']
    finally:
        (await browser.close())


async def test_sync_preflight_does_not_send_after_owner_changes(monkeypatch):
    from pyv8c.foundation.host_errors import LoadFailure

    calls = []
    def loader(url, request):
        calls.append(request['method'])
        return {'status': 204, 'headers': {
            'access-control-allow-origin': 'http://source.test',
            'access-control-allow-methods': 'PUT',
        }}

    async with Browser(real_network=False, resource_loader=loader, threaded_loaders=False) as browser:
        page = browser.new_page()
        await page.set_content('<body></body>', url='http://source.test/', wait_until='commit')
        fetcher = page.resource_fetcher
        active = fetcher.active
        monkeypatch.setattr(fetcher, 'active', lambda load: not calls and active(load))
        with pytest.raises(LoadFailure, match='owner changed'):
            page.executor.call(fetcher.load_resource, 'http://target.test/', {'method': 'PUT'})
    assert calls == ['OPTIONS']


async def test_aborting_during_preflight_never_sends_actual_request():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def loader(url, request):
        calls.append(request['method'])
        entered.set()
        release.wait(3)
        finished.set()
        return {'status': 204, 'headers': {
            'access-control-allow-origin': 'http://source.test',
            'access-control-allow-methods': 'PUT',
        }}
    browser = (await Browser(real_network=False, resource_loader=loader, threaded_loaders=True, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://source.test/', wait_until="commit"))
        page.evaluate("globalThis.result='pending';globalThis.c=new AbortController();"
                      "fetch('http://target.test/',{method:'PUT',signal:c.signal})"
                      ".then(()=>result='load').catch(e=>result=e.name)")
        assert (await asyncio.to_thread(entered.wait, 3))
        page.evaluate('c.abort()')
        release.set()
        assert (await asyncio.to_thread(finished.wait, 3))
        (await page.wait_for("result !== 'pending'", timeout=3))
        assert page.evaluate('result') == 'AbortError'
        (await page.run_until_idle(timeout=1))
        assert calls == ['OPTIONS']
    finally:
        release.set()
        (await browser.close())
