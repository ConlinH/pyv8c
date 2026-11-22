import pytest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from harness import chrome_reference, ReferenceUnavailable
from pyv8c.async_api import Browser

SCRIPT_HTML = '''<script>globalThis.log=['first'];
  Promise.resolve().then(()=>log.push(document.currentScript && document.currentScript.tagName));
  </script><script>log.push('second')</script>'''


@pytest.fixture
def script_page():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = SCRIPT_HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.fixture(scope='module')
async def reference():
    try:
        async with chrome_reference() as browser:
            yield browser
    except ReferenceUnavailable as error:
        pytest.skip(str(error))


async def test_initial_blank_document_structure(reference):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    try:
        page = browser.new_profile().new_page()
        expression = '[document.readyState, Array.from(document.all, node => node.tagName)]'
        assert page.evaluate(expression) == (await chrome.evaluate(expression))
    finally:
        (await chrome.close())
        (await browser.close())


async def test_classic_script_cleanup_and_current_script(reference, script_page):
    browser = (await Browser(real_network=True, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    try:
        page = browser.new_profile().new_page()
        (await page.goto(script_page))
        (await chrome.goto(script_page))
        (await page.wait_for('log.length === 3', timeout=3))
        (await chrome.wait_for_function('log.length === 3'))
        assert page.evaluate('log') == (await chrome.evaluate('log'))
    finally:
        (await chrome.close())
        (await browser.close())


async def test_same_origin_frame_microtasks_share_enqueue_order(reference):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = '''globalThis.log=['sync'];
      frames[0].eval("Promise.resolve().then(()=>parent.log.push('child'))");
      Promise.resolve().then(()=>log.push('parent'));'scheduled' '''
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', wait_until="commit"))
        (await page.wait_for_load_state(timeout=3))
        (await chrome.set_content('<body><iframe></iframe></body>'))
        page.evaluate(script)
        (await chrome.evaluate(script))
        (await page.wait_for('log.length === 3', timeout=3))
        (await chrome.wait_for_function('log.length === 3'))
        assert page.evaluate('log') == (await chrome.evaluate('log'))
    finally:
        (await chrome.close())
        (await browser.close())


TIMER_PROBE = """globalThis.__timerDone=false;globalThis.__timerTimes=[];
const sample = () => __timerTimes.push(performance.now());
const done = () => { __timerDone=true; };
const next = callback => __NEXT__;
let ident;
function tick() {
    sample();
    if (__timerTimes.length === 10) {
        __FINISH__;
        done();
    } else { __REPEAT__; }
}
__START__;
'scheduled'
"""


@pytest.mark.parametrize('mode', ['timeout', 'microtask', 'frame', 'interval', 'worker'])
async def test_nested_timer_clamp_matches_chrome(reference, mode):
    import json
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    next_step = {
        'timeout': 'setTimeout(callback,0)',
        'microtask': 'queueMicrotask(()=>setTimeout(callback,0))',
        'frame': 'frames[0].setTimeout(callback,0)',
        'interval': 'setTimeout(callback,0)',
        'worker': 'setTimeout(callback,0)',
    }[mode]
    script = (TIMER_PROBE.replace('__NEXT__', next_step)
              .replace('__FINISH__', 'clearInterval(ident)' if mode == 'interval' else '')
              .replace('__REPEAT__', '' if mode == 'interval' else 'next(tick)')
              .replace('__START__', 'ident=setInterval(tick,0)' if mode == 'interval' else 'next(tick)'))
    if mode == 'worker':
        source = script.replace('const done = () => { __timerDone=true; };',
                                'const done = () => { postMessage(__timerTimes); };')
        script = ("globalThis.__timerDone=false;globalThis.__timerTimes=[];"
                  "globalThis.worker=new Worker(URL.createObjectURL(new Blob([" + json.dumps(source)
                  + "])));worker.onmessage=e=>{__timerTimes=e.data;__timerDone=true};'scheduled'")
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', wait_until="commit"))
        (await page.wait_for_load_state(timeout=3))
        (await chrome.set_content('<body><iframe></iframe></body>'))
        (await chrome.evaluate(script))
        (await chrome.wait_for_function('__timerDone === true'))
        page.evaluate(script)
        (await page.wait_for('__timerDone === true', timeout=3))
        for readings in [(await chrome.evaluate('__timerTimes')), page.evaluate('__timerTimes')]:
            assert len(readings) == 10
            deltas = [right-left for left, right in zip(readings, readings[1:])]
            # Scheduling delays may increase elapsed time; assert the clamp's
            # lower bound, without freezing a machine-dependent full timing trace.
            if mode == 'interval':
                # Repeating timers follow a deadline grid: late wakeups can make
                # the next observed gap shorter than the interval.
                assert readings[-1] - readings[5] >= 12, deltas
            else:
                assert min(deltas[5:]) >= 3, deltas
    finally:
        (await chrome.close())
        (await browser.close())


async def test_interval_rearms_before_a_long_handler_and_cancels_once(reference):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = """globalThis.log=[];let count=0;
      const ident=setInterval(()=>{
        log.push('interval');
        if (++count === 1) {
          setTimeout(()=>log.push('timeout'),0);
          const until=performance.now()+20;while(performance.now()<until){}
        } else { clearTimeout(ident); }
      },0);'scheduled' """
    try:
        page = browser.new_profile().new_page()
        (await chrome.evaluate(script))
        (await chrome.wait_for_function('log.length === 3'))
        page.evaluate(script)
        (await page.wait_for('log.length === 3', timeout=3))
        assert page.evaluate('log') == (await chrome.evaluate('log')) == ['interval', 'interval', 'timeout']
    finally:
        (await chrome.close())
        (await browser.close())
