"""ReadableStream source callbacks and asynchronous reads."""
from pyv8c.async_api import Browser
from pyv8c import pyv8


async def test_source_start_can_enqueue_now_and_later():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null;
          let stream = new ReadableStream({start(c) {
            c.enqueue(1); setTimeout(() => {c.enqueue(2); c.close()}, 0);
          }});
          let reader = stream.getReader();
          (async () => { result = [await reader.read(), await reader.read(), await reader.read(), stream.locked] })();""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == [
            {'value': 1, 'done': False}, {'value': 2, 'done': False},
            {'value': pyv8.Undefined, 'done': True}, True]
    finally:
        (await browser.close())


async def test_async_start_and_pull_resolve_pending_reads():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null; let calls = [];
          let stream = new ReadableStream({
            start() { calls.push('start'); return new Promise(r => setTimeout(() => {calls.push('started'); r()}, 0)) },
            pull(c) { calls.push('pull'); c.enqueue(7); c.close() }
          });
          let reader = stream.getReader();
          (async () => { let a=await reader.read(), b=await reader.read(); result=[calls,a.value,a.done,b.done] })();""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == [['start', 'started', 'pull'], 7, False, True]
    finally:
        (await browser.close())


async def test_pull_exception_rejects_read():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null;
          let reader = new ReadableStream({pull() {throw new Error('broken')}}).getReader();
          reader.read().catch(error => {result = [error.name, error.message]});""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == ['Error', 'broken']
    finally:
        (await browser.close())


async def test_pull_prefills_one_chunk_without_a_reader():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.calls = 0;
          window.stream = new ReadableStream({pull(c) {calls++; c.enqueue(9)}});""")
        (await page.wait_for('calls > 0', timeout=3))
        assert page.evaluate('calls') == 1
        page.evaluate("window.result = null; stream.getReader().read().then(v => result = v.value)")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == 9
    finally:
        (await browser.close())


async def test_response_preserves_stream_and_reports_disturbance():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null;
          let stream = new ReadableStream({start(c) {c.enqueue(new Uint8Array([4,5])); c.close()}});
          let response = new Response(stream), same = response.body === stream;
          let reader = response.body.getReader();
          reader.read().then(chunk => {result = [same,response.bodyUsed,stream.locked,Array.from(chunk.value)]});""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == [True, True, True, [4, 5]]
    finally:
        (await browser.close())


async def test_response_rejects_locked_or_disturbed_stream():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        result = page.evaluate("""(() => {
          let stream = new ReadableStream(), reader = stream.getReader(), errors = [];
          try {new Response(stream)} catch (e) {errors.push(e.name)}
          reader.read().catch(() => {}); reader.releaseLock();
          try {new Response(stream)} catch (e) {errors.push(e.name)}
          return errors;
        })()""")
        assert result == ['TypeError', 'TypeError']
    finally:
        (await browser.close())


async def test_releasing_reader_rejects_its_pending_read():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null; let stream = new ReadableStream();
          let reader = stream.getReader(); reader.read().catch(e => result = [e.name, stream.locked]);
          reader.releaseLock();""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == ['TypeError', False]
    finally:
        (await browser.close())


async def test_source_members_are_read_once_before_start():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("""(() => {
          let log = [];
          let source = new Proxy({start(){log.push('run')}},
            {get(target, key){log.push(String(key)); return target[key]}});
          new ReadableStream(source); return log;
        })()""") == ['type', 'pull', 'cancel', 'start', 'run']
    finally:
        (await browser.close())


async def test_buffered_response_body_is_a_stable_readable_stream():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null;
          let response = new Response(new Uint8Array([0,255,4]));
          let body = response.body, same = body === response.body;
          body.getReader().read().then(chunk => {
            result = [same, response.bodyUsed, Array.from(chunk.value)];
          });""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == [True, True, [0, 255, 4]]
    finally:
        (await browser.close())


async def test_response_text_consumes_async_stream():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null;
          let stream = new ReadableStream({start(c){c.enqueue(new Uint8Array([104]));
            setTimeout(()=>{c.enqueue(new Uint8Array([105]));c.close()},0)}});
          let response = new Response(stream);
          response.text().then(value => result=[value,response.bodyUsed,stream.locked]);""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == ['hi', True, True]
    finally:
        (await browser.close())


async def test_response_text_rejects_non_byte_stream_chunk():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null;
          let stream = new ReadableStream({start(c){c.enqueue('invalid');c.close()}});
          new Response(stream).text().then(()=>result='unexpected', e=>result=e.name);""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == 'TypeError'
    finally:
        (await browser.close())
