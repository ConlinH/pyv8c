"""XMLHttpRequest's deadline and its header block, offline: a page's own loader answers.

Both were read off Chrome 154 over a local server -- ``getAllResponseHeaders``
lower-cases the names, sorts them, ends every line with CRLF and leaves Set-Cookie out,
and a timeout reports ``loadend`` alone -- and a loader stands in for that server so the
rules stay covered on a machine with none.
"""
import json
import threading

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c import Browser

ORIGIN = "http://xhr.test"
PAGE = "<!doctype html><title>xhr</title><body></body>"
HEADERS = {"Content-Type": "text/plain", "X-Mixed-Case": "One", "Set-Cookie": "a=1",
           "Cache-Control": "no-store", "Content-Length": "2"}
BLOCK = ("cache-control: no-store\r\ncontent-length: 2\r\ncontent-type: text/plain\r\n"
         "x-mixed-case: One\r\n")


def _loader(url, request, released=None):
    if released is not None and "slow" in str(url):
        released.wait(5)
    return {"status": 200, "url": str(url), "body": "ok", "headers": HEADERS}


def _page(loader):
    browser = Browser(stub_logging="off", threaded_loaders=True, resource_loader=loader)
    page = browser.new_profile().new_page()
    page.set_content(PAGE, url=ORIGIN + "/")
    return browser, page


def test_fetch_cache_round_trip_preserves_header_filter_and_immutability():
    def loader(url, request):
        return {'body': 'ok', 'headers': {
            'Access-Control-Allow-Origin': 'https://xhr.test', 'Content-Type': 'text/plain',
            'X-Public': 'shown', 'Access-Control-Expose-Headers': 'X-Public',
            'X-Private': 'hidden', 'Set-Cookie': 'session=secret'}}

    browser, page = _page(loader)
    try:
        page.set_content(PAGE, url='https://xhr.test/')
        page.evaluate('''window.__cache_result = null; (async () => {
            const cache = await caches.open('header-review');
            const response = await fetch('https://api.test/secret');
            await cache.put('/saved', response);
            const saved = await cache.match('/saved');
            await cache.add('https://api.test/added');
            const added = await cache.match('https://api.test/added');
            function inspect(r) {
                const failures = [];
                for (const method of ['set', 'append', 'delete']) {
                    try { r.headers[method]('x-public', 'changed'); failures.push('ok'); }
                    catch (e) { failures.push(e.name); }
                }
                return [r.headers.get('x-public'), r.headers.get('x-private'),
                        r.headers.get('set-cookie'), failures];
            }
            const custom = new Response('x', {headers: {'X-Own': 'before'}});
            const customClone = custom.clone();
            customClone.headers.set('x-own', 'after');
            const copied = new Headers(response.headers);
            copied.set('x-public', 'copy');
            await cache.put('/custom', customClone);
            const savedCustom = await cache.match('/custom');
            savedCustom.headers.set('x-own', 'again');
            __cache_result = [inspect(response), inspect(response.clone()),
                              inspect(saved), inspect(saved.clone()), inspect(added),
                              custom.headers.get('x-own'), savedCustom.headers.get('x-own'),
                              copied.get('x-public')];
        })().catch(e => __cache_result = ['error', e.name, e.message]);''')
        page.wait_for('__cache_result !== null', timeout=5)
        result = json.loads(page.evaluate('JSON.stringify(__cache_result)'))
        visible = ['shown', None, None, ['TypeError', 'TypeError', 'TypeError']]
        assert result == [visible, visible, visible, visible, visible, 'before', 'again', 'copy']
    finally:
        browser.close()


@pytest.mark.parametrize('threaded', [False, True])
def test_no_cors_cross_origin_redirect_back_to_source_is_opaque(threaded):
    def loader(url, request):
        if url == ORIGIN + '/start':
            return {'status': 302, 'headers': {'Location': 'http://api.test/hop'}, 'body': ''}
        if url == 'http://api.test/hop':
            return {'status': 302, 'headers': {'Location': ORIGIN + '/final'}, 'body': ''}
        return {'body': 'secret', 'headers': {'Content-Type': 'text/plain', 'X-Private': 'hidden'}}

    browser = Browser(stub_logging='off', threaded_loaders=threaded, resource_loader=loader)
    try:
        page = browser.new_profile().new_page()
        page.set_content(PAGE, url=ORIGIN + '/')
        page.evaluate('''window.__opaque_result = null;
            fetch('/start', {mode:'no-cors'}).then(async r => {
                __opaque_result = [r.type, r.status, r.url, r.redirected,
                                   r.body === null, await r.text(), [...r.headers]];
            }).catch(e => __opaque_result = ['error', e.name]);''')
        page.wait_for('__opaque_result !== null', timeout=5)
        assert json.loads(page.evaluate('JSON.stringify(__opaque_result)')) == [
            'opaque', 0, '', False, True, '', []]
    finally:
        browser.close()


def test_cached_opaque_response_remains_opaque():
    def loader(url, request):
        return {'body': 'secret', 'headers': {'Content-Type': 'text/plain', 'X-Private': 'hidden'}}

    browser, page = _page(loader)
    try:
        page.set_content(PAGE, url='https://xhr.test/')
        page.evaluate('''window.__opaque_cache = null; (async () => {
            const cache = await caches.open('opaque-review');
            const response = await fetch('https://api.test/opaque', {mode:'no-cors'});
            await cache.put('/saved', response);
            const saved = await cache.match('/saved');
            let mutation;
            try { saved.headers.set('x-private', 'changed'); mutation = 'ok'; }
            catch (e) { mutation = e.name; }
            __opaque_cache = [saved.type, saved.status, saved.url, saved.redirected,
                              saved.body === null, await saved.text(), mutation];
        })().catch(e => __opaque_cache = ['error', e.name]);''')
        page.wait_for('__opaque_cache !== null', timeout=5)
        assert json.loads(page.evaluate('JSON.stringify(__opaque_cache)')) == [
            'opaque', 0, '', False, True, '', 'TypeError']
    finally:
        browser.close()


def test_cookie_setting_headers_are_hidden_from_both_xhr_header_apis():
    def loader(url, request):
        return {'status': 200, 'url': str(url), 'body': 'ok', 'headers': {
            'Content-Type': 'text/plain', 'X-Test': 'ok',
            'Set-Cookie': 'a=1; Path=/', 'Set-Cookie2': 'b=2; Path=/'}}

    browser, page = _page(loader)
    try:
        page.evaluate('''window.__headers = null;
            const xhr = new XMLHttpRequest(); xhr.open('GET', '/headers');
            xhr.onload = () => { window.__headers = [xhr.getResponseHeader('Set-Cookie') === null,
                xhr.getResponseHeader('SET-COOKIE2') === null, xhr.getResponseHeader('x-test'),
                xhr.getAllResponseHeaders()]; };
            xhr.send();''')
        page.wait_for('__headers !== null', timeout=3)
        assert page.evaluate('__headers') == [True, True, 'ok',
                                               'content-type: text/plain\r\nx-test: ok\r\n']
        page.evaluate('''window.__fetch_headers = null; fetch('/headers').then(r => {
            const c = r.clone();
            __fetch_headers = [r.type, r.headers.get('Set-Cookie') === null,
                r.headers.get('SET-COOKIE2') === null, r.headers.get('x-test'),
                c.headers.get('set-cookie') === null];
        });''')
        page.wait_for('__fetch_headers !== null', timeout=3)
        assert page.evaluate('__fetch_headers') == ['basic', True, True, 'ok', True]
    finally:
        browser.close()


@pytest.mark.parametrize('expose,credentials,expected', [
    ('X-Public', False, ['visible', None, None]),
    ('X-Public', True, ['visible', None, None]),
    ('*', False, ['visible', 'hidden', 'other']),
    ('*', True, [None, None, None]),
    ('X-Public, bad value', False, [None, None, None]),
    ('X-Public,, X-Other', False, ['visible', None, 'other']),
    ('', False, [None, None, None]),
])
def test_cross_origin_xhr_exposes_only_cors_allowed_headers(expose, credentials, expected):
    def loader(url, request):
        return {'status': 200, 'url': str(url), 'body': 'ok', 'headers': {
            'Access-Control-Allow-Origin': ORIGIN,
            'Access-Control-Allow-Credentials': 'true',
            'Access-Control-Expose-Headers': expose,
            'Content-Type': 'text/plain', 'X-Public': 'visible',
            'X-Private': 'hidden', 'X-Other': 'other', 'Set-Cookie': 'a=1'}}

    browser, page = _page(loader)
    try:
        page.evaluate('''window.__headers = null;
            const xhr = new XMLHttpRequest(); xhr.open('GET', 'http://api.test/data');
            xhr.withCredentials = %s;
            xhr.onload = () => { window.__headers = [xhr.status,
                xhr.getResponseHeader('x-public'), xhr.getResponseHeader('x-private'),
                xhr.getResponseHeader('x-other'), xhr.getResponseHeader('set-cookie'),
                xhr.getResponseHeader('content-type'), xhr.getAllResponseHeaders()]; };
            xhr.onerror = () => { window.__headers = ['error']; };
            xhr.send();''' % ('true' if credentials else 'false'))
        page.wait_for('__headers !== null', timeout=3)
        actual = json.loads(page.evaluate('JSON.stringify(__headers)'))
        assert actual[:6] == [200, *expected, None, 'text/plain']
        header_names = [line.split(':', 1)[0] for line in actual[6].splitlines()]
        assert ('x-public' in header_names) == (expected[0] is not None)
        assert ('x-private' in header_names) == (expected[1] is not None)
        assert ('x-other' in header_names) == (expected[2] is not None)
        assert 'set-cookie' not in header_names

        page.evaluate('''window.__fetch_headers = null;
            fetch('http://api.test/data', {credentials: '%s'}).then(r => {
                const clone = r.clone();
                __fetch_headers = [r.status, r.type, r.headers.get('x-public'),
                    r.headers.get('x-private'), r.headers.get('x-other'),
                    r.headers.get('set-cookie'), r.headers.get('content-type'),
                    clone.headers.get('x-private')];
            }).catch(() => { __fetch_headers = ['error']; });''' %
                      ('include' if credentials else 'omit'))
        page.wait_for('__fetch_headers !== null', timeout=3)
        fetch_actual = json.loads(page.evaluate('JSON.stringify(__fetch_headers)'))
        assert fetch_actual == [200, 'cors', *expected, None, 'text/plain', expected[1]]
    finally:
        browser.close()


def test_xhr_redirect_to_cross_origin_uses_final_response_exposure():
    def loader(url, request):
        if str(url).endswith('/redirect'):
            return {'status': 302, 'url': str(url), 'headers': {
                'Location': 'http://api.test/final'}, 'body': ''}
        return {'status': 200, 'url': str(url), 'body': 'ok', 'headers': {
            'Access-Control-Allow-Origin': ORIGIN, 'Content-Type': 'text/plain',
            'X-Private': 'hidden'}}

    browser, page = _page(loader)
    try:
        page.evaluate('''window.__headers = null; const xhr = new XMLHttpRequest();
            xhr.open('GET', '/redirect');
            xhr.onload = () => { window.__headers = [xhr.status,
                xhr.getResponseHeader('x-private') === null, xhr.getResponseHeader('content-type')]; };
            xhr.onerror = () => { window.__headers = ['error']; };
            xhr.send();''')
        page.wait_for('__headers !== null', timeout=3)
        assert page.evaluate('__headers') == [200, True, 'text/plain']
    finally:
        browser.close()


@pytest.mark.parametrize('legacy_cache', [False, True])
def test_cached_xhr_remembers_cross_origin_redirect_taint(tmp_path, legacy_cache):
    calls = []

    def loader(url, request):
        url = str(url)
        calls.append(url)
        if url == ORIGIN + '/start':
            return {'status': 302, 'url': url, 'headers': {
                'Location': 'http://api.test/hop'}, 'body': ''}
        if url == 'http://api.test/hop':
            return {'status': 302, 'url': url, 'headers': {
                'Access-Control-Allow-Origin': ORIGIN,
                'Location': ORIGIN + '/final'}, 'body': ''}
        return {'status': 200, 'url': url, 'body': 'ok', 'headers': {
            'Content-Type': 'text/plain', 'X-Private': 'hidden',
            'Cache-Control': 'public, max-age=300'}}

    browser = Browser(stub_logging='off', threaded_loaders=True,
                      resource_loader=loader, cache_dir=tmp_path / 'cache')
    try:
        page = browser.new_profile().new_page()
        page.set_content(PAGE, url=ORIGIN + '/')
        page.evaluate('''window.__results = []; function read() {
            const x = new XMLHttpRequest(); x.open('GET', '/start');
            x.onload = () => { __results.push([x.status,
                x.getResponseHeader('x-private') === null]); };
            x.onerror = () => { __results.push(['error']); }; x.send();
        } read();''')
        page.wait_for('__results.length === 1', timeout=5)
        if legacy_cache:
            records = list((tmp_path / 'cache').rglob('*.json'))
            assert len(records) == 1
            record = json.loads(records[0].read_text(encoding='utf-8'))
            assert record.pop('redirectCrossed') is True
            records[0].write_text(json.dumps(record), encoding='utf-8')
        page.evaluate('read()')
        page.wait_for('__results.length === 2', timeout=5)
        assert page.evaluate('__results') == [[200, True], [200, True]]
        hops = [ORIGIN + '/start', 'http://api.test/hop', ORIGIN + '/final']
        assert calls == hops * (2 if legacy_cache else 1)
        page.evaluate('''window.__fetch_headers = null; fetch('/start').then(r => {
            __fetch_headers = [r.type, r.headers.get('x-private') === null,
                               r.clone().headers.get('x-private') === null];
        }).catch(() => { __fetch_headers = ['error']; });''')
        page.wait_for('__fetch_headers !== null', timeout=5)
        assert page.evaluate('__fetch_headers') == ['cors', True, True]
    finally:
        browser.close()


def test_arraybuffer_response_keeps_raw_bytes_and_is_available_only_when_done():
    payload = b'!\x00\x80\xff\xc3\xa9'
    def loader(url, request):
        return {'status': 200, 'body': payload, 'headers': {'content-type': 'application/octet-stream'}}
    browser, page = _page(loader)
    try:
        page.evaluate('''
          globalThis.result=null;globalThis.states=[];
          const xhr=new XMLHttpRequest();xhr.open('GET','/binary');xhr.responseType='arraybuffer';
          globalThis.before=xhr.response===null;
          xhr.onreadystatechange=()=>states.push([xhr.readyState,xhr.response===null]);
          xhr.onload=()=>{
            const body=xhr.response;let textError='';
            try{void xhr.responseText}catch(e){textError=e.name}
            result=[body instanceof ArrayBuffer,body===xhr.response,Array.from(new Uint8Array(body)),textError];
          };xhr.send();
        ''')
        page.wait_for('result!==null', timeout=3)
        assert page.evaluate('before') is True
        assert page.evaluate('states') == [[2, True], [3, True], [4, False]]
        assert page.evaluate('result') == [True, True, list(payload), 'InvalidStateError']
    finally:
        browser.close()


def test_reopening_xhr_drops_cached_arraybuffer_without_changing_old_buffer():
    def loader(url, request):
        return {'status': 200, 'body': b'AB' if url.endswith('/first') else b'\x00\xff'}
    browser, page = _page(loader)
    try:
        page.evaluate('''
          globalThis.result=null;const xhr=new XMLHttpRequest();let first;
          xhr.open('GET','/first');xhr.responseType='arraybuffer';
          xhr.onload=()=>{
            if(!first){
              first=xhr.response;new Uint8Array(first)[0]=90;
              globalThis.same=xhr.response===first&&new Uint8Array(xhr.response)[0]===90;
              xhr.open('GET','/second');globalThis.reset=xhr.response===null;xhr.send();
            }else result=[same,reset,xhr.response!==first,
                          Array.from(new Uint8Array(first)),Array.from(new Uint8Array(xhr.response))];
          };xhr.send();
        ''')
        page.wait_for('result!==null', timeout=3)
        assert page.evaluate('result') == [True, True, True, [90, 66], [0, 255]]
    finally:
        browser.close()


def test_request_guard_filters_forbidden_headers_and_xhr_combines_values():
    requests = []
    def loader(url, request):
        requests.append((url, request))
        return _loader(url, request)

    browser, page = _page(loader)
    try:
        assert page.evaluate("""(() => {
            const standalone = new Headers({Cookie: 'standalone'});
            const request = new Request('/data', {headers: {Cookie: 'bad', 'X-Test': 'one'}});
            request.headers.set('Sec-Fetch-Site', 'bad');
            request.headers.set('Permissions-Policy', 'allowed');
            request.headers.append('X-Test', 'two');
            return [standalone.get('cookie'), request.headers.get('cookie') === null,
                    request.headers.get('sec-fetch-site') === null, request.headers.get('x-test'),
                    request.headers.get('permissions-policy')];
        })()""") == ['standalone', True, True, 'one, two', 'allowed']
        page.evaluate("""(() => {
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/data');
            xhr.setRequestHeader('Cookie', 'bad');
            xhr.setRequestHeader('X-Test', 'one');
            xhr.setRequestHeader('X-Test', 'two');
            xhr.send();
        })()""")
        page.run_until_idle(timeout=5)
        data = next(request for url, request in requests if url.endswith('/data'))
        headers = {name.lower(): value for name, value in data['headers'].items()}
        assert headers.get('cookie') != 'bad'
        assert headers['x-test'] == 'one, two'
    finally:
        browser.close()


def test_get_all_response_headers_writes_the_block_chrome_writes():
    browser, page = _page(_loader)
    try:
        page.evaluate("""(() => {
            window.__block = null;
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/data');
            xhr.onload = () => {
                window.__block = [xhr.getAllResponseHeaders(),
                                  xhr.getResponseHeader('content-TYPE'),
                                  xhr.getAllResponseHeaders() === xhr.getAllResponseHeaders()];
            };
            xhr.send();
            return 'asked';
        })()""")
        page.run_until_idle(timeout=5)
        assert page.evaluate("window.__block") == [BLOCK, "text/plain", True]
    finally:
        browser.close()


def test_get_all_response_headers_is_empty_until_a_response_arrives():
    browser, page = _page(_loader)
    try:
        assert page.evaluate("""(() => {
            const fresh = new XMLHttpRequest();
            const opened = new XMLHttpRequest();
            opened.open('GET', '/data');
            return [fresh.getAllResponseHeaders(), fresh.readyState, fresh.timeout,
                    opened.getAllResponseHeaders()];
        })()""") == ["", 0, 0, ""]
    finally:
        browser.close()


def test_a_timeout_drops_the_request_and_reports_loadend_alone():
    released = threading.Event()
    browser, page = _page(lambda url, request: _loader(url, request, released))
    try:
        page.evaluate("""(() => {
            window.__log = null;
            const xhr = new XMLHttpRequest();
            const log = [];
            xhr.onload = () => log.push('load');
            xhr.onerror = () => log.push('error');
            xhr.onabort = () => log.push('abort');
            xhr.onloadend = () => log.push('loadend');
            xhr.timeout = 50;
            xhr.open('GET', '/slow');
            xhr.send();
            setTimeout(() => {
                window.__log = [log, xhr.readyState, xhr.status, xhr.timeout,
                                xhr.getAllResponseHeaders()];
            }, 400);
            return 'asked';
        })()""")
        page.wait_for("window.__log !== null", timeout=10)
        assert page.evaluate("window.__log") == [["loadend"], 4, 0, 50, ""]
    finally:
        released.set()
        browser.close()


def test_abort_drops_a_request_that_is_still_running():
    released = threading.Event()
    browser, page = _page(lambda url, request: _loader(url, request, released))
    try:
        page.evaluate("""(() => {
            window.__log = null;
            const xhr = new XMLHttpRequest();
            const log = [];
            xhr.onload = () => log.push('load');
            xhr.onloadend = () => log.push('loadend');
            xhr.open('GET', '/slow');
            xhr.timeout = 40;
            xhr.send();
            xhr.abort();
            setTimeout(() => { window.__log = [log, xhr.readyState]; }, 300);
            return 'asked';
        })()""")
        page.wait_for("window.__log !== null", timeout=10)
        # abort() disarms the deadline and cancels the load, so the response that
        # arrives afterwards is dropped with the request rather than reported.
        assert page.evaluate("window.__log") == [["loadend"], 0]
    finally:
        released.set()
        browser.close()


def test_send_gives_a_string_body_the_content_type_chrome_gives_it():
    """A string body is ``text/plain;charset=UTF-8`` unless the author said otherwise.

    Chrome 154 sends exactly that on the Cloudflare challenge's own submit POST, and the answer
    that lets its widget take the last step -- ``cf-chl-out`` -- comes back only when it does;
    the engine sent no ``Content-Type`` at all, so the widget stopped one step short of
    ``cf_clearance`` (measured 2026-10-07).  A Blob brings its own type, and no body carries
    none -- the same rule ``sendBeacon`` already followed.
    """
    seen = []

    def loader(url, request):
        seen.append((str(url), dict(request or {})))
        return {"status": 200, "url": str(url), "body": "ok", "headers": {}}

    browser, page = _page(loader)
    try:
        page.evaluate("""(() => {
            const plain = new XMLHttpRequest();
            plain.open('POST', '/plain');
            plain.send('a=1');
            const typed = new XMLHttpRequest();
            typed.open('POST', '/typed');
            typed.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
            typed.send('a=1');
            const blob = new XMLHttpRequest();
            blob.open('POST', '/blob');
            blob.send(new Blob(['x'], {type: 'text/csv'}));
            const empty = new XMLHttpRequest();
            empty.open('GET', '/empty');
            empty.send();
            return 'asked';
        })()""")
        page.run_until_idle(timeout=5)
    finally:
        browser.close()

    def sent_type(name):
        for url, request in seen:
            if url.endswith(name):
                return request.get("content_type")
        raise AssertionError(f"no request to {name}; saw {[url for url, _ in seen]}")

    assert sent_type("/plain") == "text/plain;charset=UTF-8"
    assert sent_type("/typed") is None, "the author's own Content-Type must stand"
    assert sent_type("/blob") == "text/csv"
    assert sent_type("/empty") is None, "no body, no type"

