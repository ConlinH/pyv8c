"""WebAssembly completes through the execution group's foreground driver."""

import asyncio
import time

from pyv8c.async_api import Browser


async def test_compile_finishes_without_manual_pump():
    browser = (await Browser().__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.wasmResult = 'pending';
          WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0]))
            .then(() => { window.wasmResult = 'ok' },
                  e => { window.wasmResult = String(e) });""")
        # Do not submit commands while waiting: doing so can accidentally drive
        # an implementation that forgot to wake the sleeping owner.
        (await asyncio.sleep(.5))
        assert page.evaluate('window.wasmResult') == 'ok'
    finally:
        (await browser.close())


async def test_worker_instantiates_without_manual_pump():
    browser = (await Browser().__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.wasmResult = 'pending';
          const source = `WebAssembly.instantiate(new Uint8Array([0,97,115,109,1,0,0,0]))
            .then(() => postMessage('ok'), e => postMessage(String(e)))`;
          window.worker = new Worker(URL.createObjectURL(new Blob([source], {type:'text/javascript'})));
          worker.onmessage = e => { window.wasmResult = e.data };""")
        (await asyncio.sleep(1))
        assert page.evaluate('window.wasmResult') == 'ok'
    finally:
        (await browser.close())


async def test_worker_streaming_compiles_buffered_response():
    browser = (await Browser().__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.wasmResult = 'pending';
          const source = `WebAssembly.compileStreaming(new Response(
            new Uint8Array([0,97,115,109,1,0,0,0]),
            {headers:{'Content-Type':'application/wasm'}}))
            .then(() => postMessage('ok'), e => postMessage(String(e)))`;
          window.worker = new Worker(URL.createObjectURL(new Blob([source], {type:'text/javascript'})));
          worker.onmessage = e => { window.wasmResult = e.data };""")
        (await asyncio.sleep(1))
        assert page.evaluate('window.wasmResult') == 'ok'
    finally:
        (await browser.close())


async def test_streaming_uses_response_bytes_and_checks_mime():
    browser = (await Browser().__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.wasmResult = 'pending';
          const bytes = new Uint8Array([0,97,115,109,1,0,0,0]);
          const options = {headers:{'Content-Type':'application/wasm'}};
          const response = new Response(bytes, options);
          const bad = new Response(bytes, {headers:{'Content-Type':'text/plain'}});
          Promise.all([
            WebAssembly.instantiateStreaming(Promise.resolve(response)).then(
              result => result.instance instanceof WebAssembly.Instance),
            WebAssembly.compileStreaming(bad).then(() => 'wrong', e => e.name)
          ]).then(result => { window.wasmResult = [result, response.bodyUsed, bad.bodyUsed] });""")
        (await asyncio.sleep(.5))
        assert page.evaluate('window.wasmResult') == [[True, 'TypeError'], True, False]
    finally:
        (await browser.close())


async def test_other_isolate_continues_after_first_page_closes():
    browser = (await Browser().__aenter__())
    try:
        profile = browser.new_profile()
        first, second = profile.new_page(), profile.new_page()
        first.evaluate("WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0]))")
        await first.close()
        second.evaluate("""window.wasmResult = 'pending';
          WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0]))
            .then(() => window.wasmResult = 'ok');""")
        (await asyncio.sleep(.5))
        assert second.evaluate('window.wasmResult') == 'ok'
    finally:
        (await browser.close())


async def test_iframe_compiles_in_its_agent_without_manual_pump():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', wait_until="commit"))
        page.evaluate("""frames[0].eval(`window.wasmResult = 'pending';
          WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0]))
            .then(() => window.wasmResult = 'ok')`);""")
        (await asyncio.sleep(.5))
        assert page.evaluate('frames[0].wasmResult') == 'ok'
    finally:
        (await browser.close())


async def test_cross_origin_iframe_completes_streaming_in_its_own_agent():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', url='https://one.test/', wait_until="commit"))
        child = page.document.fn_querySelector('iframe')._child_services._page_realm.page
        (await child.goto('data:text/html,<p>child</p>'))
        assert child.execution_group is page.execution_group
        assert child.realm.agent is not page.realm.agent
        child.evaluate("""window.result='pending'; let s=new ReadableStream({start(c){
          c.enqueue(new Uint8Array([0,97,115,109]));
          setTimeout(()=>{c.enqueue(new Uint8Array([1,0,0,0]));c.close()},0)}});
          WebAssembly.instantiateStreaming(new Response(s,{headers:{'content-type':'application/wasm'}}))
            .then(()=>result='ok',e=>result=e.name);""")
        (await child.wait_for("result !== 'pending'", timeout=5))
        assert child.evaluate('result') == 'ok'
    finally:
        (await browser.close())


async def test_closing_iframe_cancels_its_stream_but_parent_still_compiles():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', url='https://one.test/', wait_until="commit"))
        child = page.document.fn_querySelector('iframe')._child_services._page_realm.page
        (await child.goto('data:text/html,<p>child</p>'))
        child.evaluate("""let s = new ReadableStream({pull(){return new Promise(()=>{})}});
          WebAssembly.compileStreaming(new Response(s,{headers:{'content-type':'application/wasm'}}));""")
        (await child.close())
        page.evaluate("""window.result='pending';
          WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0])).then(()=>result='ok');""")
        (await page.wait_for("result === 'ok'", timeout=5))
        assert page.evaluate('result') == 'ok'
    finally:
        (await browser.close())


async def test_streaming_rejects_invalid_responses_and_used_body():
    browser = (await Browser().__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.wasmResult = 'pending';
          const options = {headers:{'Content-Type':'application/wasm'}};
          const used = new Response(new Uint8Array([0,97,115,109,1,0,0,0]), options);
          used.arrayBuffer();
          const inputs = [{}, new WebAssembly.Memory({initial:1}),
                          new Response(null, options), used,
                          new Response('', {status:404, headers:options.headers})];
          Promise.all(inputs.map(value => WebAssembly.compileStreaming(value)
            .then(() => 'unexpected', e => e.name)))
            .then(names => window.wasmResult = names);""")
        (await asyncio.sleep(.5))
        assert page.evaluate('window.wasmResult') == [
            'TypeError', 'TypeError', 'CompileError', 'TypeError', 'TypeError']
    finally:
        (await browser.close())


async def test_wasm_completion_runs_after_current_microtasks():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.events = ['sync'];
          queueMicrotask(() => events.push('microtask'));
          setTimeout(() => events.push('timer'), 0);
          WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0]))
            .then(() => events.push('wasm'));""")
        (await asyncio.sleep(.5))
        events = page.evaluate('events')
        assert events[:2] == ['sync', 'microtask']
        assert set(events) == {'sync', 'microtask', 'timer', 'wasm'}
    finally:
        (await browser.close())


async def test_streaming_accepts_asynchronously_produced_chunks():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.wasmResult = 'pending';
          const stream = new ReadableStream({start(controller) {
            controller.enqueue(new Uint8Array([0,97,115,109]));
            setTimeout(() => { controller.enqueue(new Uint8Array([1,0,0,0]));
                               controller.close() }, 0);
          }});
          window.response = new Response(stream, {headers:{'content-type':'application/wasm'}});
          WebAssembly.compileStreaming(response).then(() => wasmResult = 'ok',
            error => wasmResult = error.name);""")
        (await asyncio.sleep(.5))
        assert page.evaluate('window.wasmResult') == 'ok'
        assert page.evaluate('response.bodyUsed') is True
        assert page.evaluate('response.body.locked') is True
    finally:
        (await browser.close())


async def test_streaming_reports_stream_error_as_network_type_error():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null; let failure = new Error('source failed');
          let stream = new ReadableStream({start(c) {setTimeout(() => c.error(failure), 0)}});
          WebAssembly.compileStreaming(new Response(stream, {headers:{'content-type':'application/wasm'}}))
            .then(() => result = 'unexpected', e => result = [e.name, e.message, stream.locked]);""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == [
            'TypeError', 'WebAssembly compilation aborted: Network error: Failed to read from a ReadableStream.', True]
    finally:
        (await browser.close())


async def test_streaming_response_errors_match_chrome_messages():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result=null; (async()=>{
          let bytes=new Uint8Array([0,97,115,109,1,0,0,0]);
          let good={'content-type':'application/wasm'}, failures=[];
          let used=new Response(bytes,{headers:good}); await used.arrayBuffer();
          for (let value of [null, new Response(bytes,{status:404,headers:good}),
                             new Response(bytes,{headers:{'content-type':'text/plain'}}),
                             new Response(null,{headers:good}), used]) {
            try {await WebAssembly.compileStreaming(value)} catch(e) {failures.push([e.name,e.message])}
          }
          try {await WebAssembly.instantiateStreaming(null)} catch(e) {failures.push([e.name,e.message])}
          let reason=new Error('input rejected');
          try {await WebAssembly.compileStreaming(Promise.reject(reason))}
            catch(e) {failures.push([e===reason,e.message])}
          result=failures;
        })()""")
        (await page.wait_for('result !== null', timeout=5))
        prefix = "Failed to execute 'compile' on 'WebAssembly': "
        missing = 'An argument must be provided, which must be a Response or Promise<Response> object'
        assert page.evaluate('result') == [
            ['TypeError', prefix + missing],
            ['TypeError', prefix + 'HTTP status code is not ok'],
            ['TypeError', prefix + "Incorrect response MIME type. Expected 'application/wasm'."],
            ['CompileError', prefix + 'Empty WebAssembly module'],
            ['TypeError', prefix + 'Cannot compile WebAssembly.Module from an already read Response'],
            ['TypeError', prefix + missing],
            [True, 'input rejected'],
        ]
    finally:
        (await browser.close())


async def test_closing_page_cancels_pending_streaming_compile():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""let stream = new ReadableStream({pull(){return new Promise(()=>{})}});
          WebAssembly.compileStreaming(new Response(stream, {headers:{'content-type':'application/wasm'}}));""")
        (await page.close())
        assert page.execution_group.executor.closed
    finally:
        (await browser.close())


async def test_worker_compiles_asynchronous_stream_chunks():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.wasmResult = 'pending';
          let script = `let stream = new ReadableStream({start(c){
            c.enqueue(new Uint8Array([0,97,115,109]));
            setTimeout(()=>{c.enqueue(new Uint8Array([1,0,0,0]));c.close()},0)}});
            WebAssembly.compileStreaming(new Response(stream,{headers:{'content-type':'application/wasm'}}))
              .then(()=>postMessage('ok'),e=>postMessage(e.name+': '+e.message));`;
          window.worker = new Worker(URL.createObjectURL(new Blob([script],{type:'text/javascript'})));
          worker.onmessage = e => {wasmResult=e.data};""")
        (await page.wait_for("wasmResult !== 'pending'", timeout=5))
        assert page.evaluate('wasmResult') == 'ok'
    finally:
        (await browser.close())


async def test_streaming_rejects_non_byte_chunks():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null;
          let stream = new ReadableStream({start(c){c.enqueue('invalid');c.close()}});
          WebAssembly.compileStreaming(new Response(stream,{headers:{'content-type':'application/wasm'}}))
            .then(()=>result='unexpected',e=>result=e.name);""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == 'TypeError'
    finally:
        (await browser.close())


async def test_streaming_requires_uint8array_chunks():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = null;
          async function check(value) {
            let stream = new ReadableStream({start(c){c.enqueue(value);c.close()}});
            try {await WebAssembly.compileStreaming(new Response(stream,{headers:{'content-type':'application/wasm'}}));
                 return 'unexpected'} catch(e) {return [e.name,e.message]}
          }
          Promise.all([check(new Uint16Array([1])), check(new Uint8ClampedArray([1])),
                       check(new ArrayBuffer(1))]).then(value => result=value);""")
        (await page.wait_for('result !== null', timeout=3))
        assert page.evaluate('result') == [[
            'TypeError', 'WebAssembly compilation aborted: Network error: Failed to read from a ReadableStream.'
        ]] * 3
    finally:
        (await browser.close())


async def test_navigation_cancels_stream_and_new_document_can_compile():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""let stream = new ReadableStream({pull(){return new Promise(()=>{})}});
          WebAssembly.compileStreaming(new Response(stream, {headers:{'content-type':'application/wasm'}}));""")
        old_context = page.v8_context
        (await page.goto('data:text/html,<p>new document</p>'))
        assert page.v8_context is not old_context
        page.evaluate("""window.result='pending';
          WebAssembly.compile(new Uint8Array([0,97,115,109,1,0,0,0])).then(()=>result='ok')""")
        (await page.wait_for("result === 'ok'", timeout=5))
        assert page.evaluate('result') == 'ok'
    finally:
        (await browser.close())


async def test_navigation_retires_native_stream_session(monkeypatch):
    import pytest
    from pyv8c.web.fetch.response import Response

    sessions = []
    original = Response._start_wasm_stream

    def capture(response, session, context):
        sessions.append((session, context))
        return original(response, session, context)

    monkeypatch.setattr(Response, '_start_wasm_stream', capture)
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""let stream = new ReadableStream({pull(){return new Promise(()=>{})}});
          WebAssembly.compileStreaming(new Response(stream, {headers:{'content-type':'application/wasm'}}));""")
        assert len(sessions) == 1
        session, context = sessions[0]
        (await page.goto('data:text/html,<p>new document</p>'))
        with pytest.raises(RuntimeError, match='session is closed'):
            context._wasm_stream_feed(session, b'abc')
    finally:
        (await browser.close())


async def test_repeated_navigation_during_streaming_does_not_keep_old_sessions():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        for _ in range(12):
            page.evaluate("""(()=>{let stream = new ReadableStream({start(c){
              c.enqueue(new Uint8Array([0,97,115,109]));
              setTimeout(()=>{c.enqueue(new Uint8Array([1,0,0,0]));c.close()},50)}});
              WebAssembly.compileStreaming(new Response(stream,{headers:{'content-type':'application/wasm'}}));})()""")
            old_context = page.v8_context
            (await page.goto(f'data:text/html,<p>next-{_}</p>'))
            assert page.v8_context is not old_context
        assert page.evaluate('1+1') == 2
    finally:
        (await browser.close())


async def test_terminating_worker_during_streaming_does_not_block_page():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""let script = `let stream=new ReadableStream({pull(){return new Promise(()=>{})}});
          WebAssembly.compileStreaming(new Response(stream,{headers:{'content-type':'application/wasm'}}));
          postMessage('ready')`;
          window.worker = new Worker(URL.createObjectURL(new Blob([script],{type:'text/javascript'})));
          window.ready=false; worker.onmessage=()=>ready=true;""")
        (await page.wait_for('ready', timeout=5))
        page.evaluate('worker.terminate()')
        assert page.evaluate('1+1') == 2
    finally:
        (await browser.close())
