"""A worker fetches for itself: ``fetch`` exists there, and its response is readable.

A worker realm has no ``fetch`` of its own at all in this build before, which a challenge that
fingerprints from a worker notices at once; and a response built by the page's pipeline carries
the *page's* realm services, so reading its body inside the worker used to fail with "JavaScript
value belongs to another isolate".  Both halves are what this test holds down.
"""
import json
import threading
import time

from pyv8c import Browser

SOURCE = ("fetch('https://worker.test/answer.json').then(function (response) {"
          " self.postMessage('status:' + response.status);"
          " return response.text();"
          "}).then(function (text) { self.postMessage('body:' + text); })"
          ".catch(function (error) { self.postMessage('failed:' + error.name); });")

PAGE_HTML = """<!doctype html><body><script>
var out = {}; window.__out = out;
out.seen = [];
var worker = new Worker(URL.createObjectURL(new Blob([__SOURCE__],
  {type: 'text/javascript'})));
worker.onmessage = function (event) { out.seen.push(String(event.data)); };
worker.onerror = function (event) { out.seen.push('onerror:' + String(event.message || event)); };
</script></body>""".replace("__SOURCE__", json.dumps(SOURCE).replace("</", "<\\/"))

PROBE_URL = "https://worker.test/"


def _bounded(action, seconds=10.0):
    done = threading.Event()

    def run():
        try:
            action()
        except BaseException:  # noqa: BLE001
            pass
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    done.wait(seconds)


def _pump(page, seconds=0.6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            page.run_until_idle(timeout=0)
        except BaseException:  # noqa: BLE001
            pass
        time.sleep(0.01)


def _load(url, request):
    """The probe page, and the JSON a worker inside it asks for."""
    if str(url).endswith("answer.json"):
        return {"body": "from-the-answer", "headers": {"content-type": "application/json"}}
    return {"body": PAGE_HTML, "headers": {"content-type": "text/html"}}


def test_a_worker_fetches_and_reads_its_response():
    browser = Browser(stub_logging="off", threaded_loaders=True, real_network=False,
                      resource_loader=_load)
    try:
        page = browser.new_profile().new_page()
        page.goto(PROBE_URL)
        _pump(page)
        seen = json.loads(page.evaluate("JSON.stringify(window.__out.seen)"))
        assert seen == ["status:200", "body:from-the-answer"], seen
    finally:
        _bounded(browser.close)


def test_worker_fetch_filters_cross_origin_response_headers():
    def loader(url, request):
        if str(url).startswith('https://api.test/'):
            return {'body': 'ok', 'headers': {
                'Access-Control-Allow-Origin': 'https://worker.test',
                'Access-Control-Expose-Headers': 'X-Public',
                'X-Public': 'visible', 'X-Private': 'hidden',
                'Content-Type': 'text/plain'}}
        return {'body': '<body></body>', 'headers': {'Content-Type': 'text/html'}}

    browser = Browser(stub_logging='off', threaded_loaders=True, real_network=False,
                      resource_loader=loader)
    try:
        page = browser.new_profile().new_page()
        page.set_content('<body></body>', url=PROBE_URL)
        page.evaluate('''window.__worker_result = null;
            const source = `fetch('https://api.test/data').then(r => {
                const c = r.clone();
                postMessage([r.type, r.headers.get('x-public'),
                             r.headers.get('x-private'), c.headers.get('x-private')]);
            }).catch(e => postMessage(['error', e.name]));`;
            const worker = new Worker(URL.createObjectURL(new Blob([source],
                {type: 'text/javascript'})));
            worker.onmessage = e => { window.__worker_result = e.data; };''')
        _pump(page, 1.0)
        assert json.loads(page.evaluate('JSON.stringify(__worker_result)')) == [
            'cors', 'visible', None, None]
    finally:
        _bounded(browser.close)


def test_worker_no_cors_cross_origin_redirect_back_to_source_is_opaque():
    def loader(url, request):
        if str(url) == 'https://worker.test/start':
            return {'status': 302, 'body': '', 'headers': {'Location': 'https://api.test/hop'}}
        if str(url) == 'https://api.test/hop':
            return {'status': 302, 'body': '', 'headers': {'Location': 'https://worker.test/final'}}
        return {'body': 'secret', 'headers': {'Content-Type': 'text/plain'}}

    browser = Browser(stub_logging='off', threaded_loaders=True, real_network=False,
                      resource_loader=loader)
    try:
        page = browser.new_profile().new_page()
        page.set_content('<body></body>', url=PROBE_URL)
        page.evaluate('''window.__worker_result = null;
            const source = `fetch('https://worker.test/start', {mode:'no-cors'})
                .then(async r => postMessage([r.type, r.status, r.url, r.redirected,
                                              r.body === null, await r.text()]))
                .catch(e => postMessage(['error', e.name]));`;
            const worker = new Worker(URL.createObjectURL(new Blob([source],
                {type: 'text/javascript'})));
            worker.onmessage = e => { window.__worker_result = e.data; };''')
        _pump(page, 1.0)
        assert json.loads(page.evaluate('JSON.stringify(__worker_result)')) == [
            'opaque', 0, '', False, True, '']
    finally:
        _bounded(browser.close)


def test_worker_fetch_retains_redirect_taint_from_cache(tmp_path):
    calls = []

    def loader(url, request):
        url = str(url)
        calls.append(url)
        if url == 'https://worker.test/start':
            return {'status': 302, 'body': '', 'headers': {'Location': 'https://api.test/hop'}}
        if url == 'https://api.test/hop':
            return {'status': 302, 'body': '', 'headers': {
                'Location': 'https://worker.test/final',
                'Access-Control-Allow-Origin': 'https://worker.test'}}
        if url == 'https://worker.test/final':
            return {'body': 'ok', 'headers': {'Content-Type': 'text/plain',
                                               'Cache-Control': 'public, max-age=300',
                                               'X-Private': 'hidden'}}
        return {'body': '<body></body>', 'headers': {'Content-Type': 'text/html'}}

    browser = Browser(stub_logging='off', threaded_loaders=True, real_network=False,
                      resource_loader=loader, cache_dir=tmp_path / 'cache')
    try:
        page = browser.new_profile().new_page()
        page.set_content('<body></body>', url=PROBE_URL)
        page.evaluate('''window.__worker_result = null;
            const source = `async function run() {
                const result = [];
                for (let i = 0; i < 2; ++i) {
                    const r = await fetch('https://worker.test/start');
                    result.push([r.type, r.headers.get('x-private')]);
                }
                postMessage(result);
            } run().catch(e => postMessage(['error', e.name]));`;
            const worker = new Worker(URL.createObjectURL(new Blob([source],
                {type: 'text/javascript'})));
            worker.onmessage = e => { window.__worker_result = e.data; };''')
        _pump(page, 1.5)
        assert json.loads(page.evaluate('JSON.stringify(__worker_result)')) == [
            ['cors', None], ['cors', None]]
        assert calls == ['https://worker.test/start', 'https://api.test/hop',
                         'https://worker.test/final']
    finally:
        _bounded(browser.close)
