"""ReadableStream.pipeTo: rejected promises, backpressure, shutdown and locks."""
import pytest


async def run(page, body):
    await page.set_content('<!doctype html><body><iframe></iframe>', url='https://streams.test/')
    page.evaluate('window.result = null; (async () => {' + body +
                  '})().catch(e => { result = {unexpected: [e.name, e.message]}; });')
    await page.wait_for('result !== null', timeout=3)
    return page.evaluate('result')


@pytest.mark.parametrize('receiver', ['window', '{}', 'null'])
async def test_pipe_to_invalid_receiver_rejects_instead_of_throwing(page, receiver):
    result = await run(page, '''
      let p;
      try { p = ReadableStream.prototype.pipeTo.call(%s); }
      catch(e) { result = {thrown: e.name}; return; }
      const nativePromise = p instanceof Promise;
      try { await p; } catch(e) { result = [nativePromise, e instanceof TypeError, e.message]; }
    ''' % receiver)
    assert result == [True, True, "Failed to execute 'pipeTo' on 'ReadableStream': Illegal invocation"]


async def test_pipe_to_rejection_uses_method_realm(page):
    result = await run(page, '''
      const child = document.querySelector('iframe').contentWindow;
      const p = child.ReadableStream.prototype.pipeTo.call({});
      try { await p; } catch(e) {
        result = [p instanceof Promise, p instanceof child.Promise,
                  e instanceof TypeError, e instanceof child.TypeError];
      }
    ''')
    assert result == [False, True, False, True]


async def test_pipe_to_moves_chunks_with_backpressure_and_releases_locks(page):
    result = await run(page, '''
      const events = [];
      const source = new ReadableStream({start(c) { c.enqueue(1); c.enqueue(2); c.close(); }});
      const sink = new WritableStream({
        start(c) { events.push(c instanceof WritableStreamDefaultController); },
        write(n) { events.push(n); return new Promise(r => setTimeout(() => { events.push(-n); r(); }, 5)); },
        close() { events.push('closed'); }
      });
      const p = source.pipeTo(sink);
      const locks = [source.locked, sink.locked];
      const value = await p;
      result = [events, locks, source.locked, sink.locked, value === undefined];
    ''')
    assert result == [[True, 1, -1, 2, -2, 'closed'], [True, True], False, False, True]


@pytest.mark.parametrize('prevent', [False, True])
async def test_source_error_aborts_destination_unless_prevented(page, prevent):
    result = await run(page, '''
      const reason = {why:'source'}; let aborted = 0;
      const source = new ReadableStream({pull(c) { c.error(reason); }});
      const sink = new WritableStream({abort(e) { aborted += e === reason ? 1 : 100; }});
      try { await source.pipeTo(sink, {preventAbort:%s}); }
      catch(e) { result = [e === reason, aborted, source.locked, sink.locked]; }
    ''' % str(prevent).lower())
    assert result == [True, 0 if prevent else 1, False, False]


@pytest.mark.parametrize('prevent', [False, True])
async def test_sink_error_cancels_source_unless_prevented(page, prevent):
    result = await run(page, '''
      const reason = {why:'sink'}; let cancelled = 0;
      const source = new ReadableStream({start(c) { c.enqueue(1); }, cancel(e) { cancelled += e === reason ? 1 : 100; }});
      const sink = new WritableStream({write() { throw reason; }});
      try { await source.pipeTo(sink, {preventCancel:%s}); }
      catch(e) { result = [e === reason, cancelled, source.locked, sink.locked]; }
    ''' % str(prevent).lower())
    assert result == [True, 0 if prevent else 1, False, False]


async def test_prevent_close_preserves_writable_destination(page):
    result = await run(page, '''
      const events = [];
      const source = new ReadableStream({start(c) { c.close(); }});
      const sink = new WritableStream({write(v) { events.push(v); }, close() { events.push('close'); }});
      await source.pipeTo(sink, {preventClose:true});
      const w = sink.getWriter(); await w.write(9); await w.close(); w.releaseLock();
      result = [events, source.locked, sink.locked];
    ''')
    assert result == [[9, 'close'], False, False]


async def test_pipe_to_compression_stream_transfers_real_bytes(page):
    result = await run(page, '''
      const source = new ReadableStream({start(c) { c.enqueue(new Uint8Array([97,98,99])); c.close(); }});
      const gzip = new CompressionStream('gzip'); const reader = gzip.readable.getReader();
      const collecting = (async () => { const bytes=[]; for (;;) { const item=await reader.read();
        if(item.done)return bytes; bytes.push(...item.value); } })();
      await source.pipeTo(gzip.writable);
      const bytes = await collecting;
      result = [bytes.length, bytes.slice(0,3), source.locked, gzip.writable.locked];
    ''')
    assert result == [23, [31, 139, 8], False, False]


async def test_abort_signal_shuts_down_both_ends_and_keeps_reason_identity(page):
    result = await run(page, '''
      const controller = new AbortController(), reason = {why:'stop'}, events = [];
      const source = new ReadableStream({cancel(e) { events.push(['cancel', e === reason]); }});
      const sink = new WritableStream({abort(e) { events.push(['abort', e === reason]); }});
      const p = source.pipeTo(sink, {signal:controller.signal}); controller.abort(reason);
      try { await p; } catch(e) { result=[e===reason, events, source.locked, sink.locked]; }
    ''')
    assert result == [True, [['abort', True], ['cancel', True]], False, False]


async def test_conversion_errors_reject_and_do_not_acquire_locks(page):
    result = await run(page, '''
      const source = new ReadableStream(); const sink = new WritableStream(); const results=[];
      for (const invoke of [() => source.pipeTo(), () => source.pipeTo({}),
                            () => source.pipeTo(sink, {get preventClose() { throw 'conversion'; }})]) {
        let p; try { p=invoke(); } catch(e) { results.push('sync'); continue; }
        try { await p; } catch(e) { results.push(e==='conversion' ? e : e.name); }
      }
      result=[results, source.locked, sink.locked];
    ''')
    assert result == [['TypeError', 'TypeError', 'conversion'], False, False]
