"""Chrome 155 parity for the byte-stream path used by CompressionStream."""

from pyv8c.async_api import Browser


async def _run(script):
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<!doctype html><script>' + script + '</script>',
                         url='https://streams.test/', wait_until="commit"))
        (await page.wait_for('window.result && window.result.done === true', timeout=3))
        return page.evaluate('window.result')
    finally:
        (await browser.close())


async def test_gzip_stream_bytes_and_native_interface_identity():
    result = (await _run('''
      window.result = {};
      (async () => {
        try {
          const stream = new CompressionStream('gzip');
          const writer = stream.writable.getWriter();
          const reader = stream.readable.getReader();
          result.identity = [stream.readable instanceof ReadableStream,
                             stream.writable instanceof WritableStream,
                             writer.constructor.name, reader.constructor.name,
                             stream.readable.locked, stream.writable.locked];
          const reading = (async () => {
            const bytes = [];
            for (;;) {
              const item = await reader.read();
              if (item.done) break;
              bytes.push(...item.value);
            }
            return bytes;
          })();
          await writer.write(new Uint8Array([97, 98, 99]));
          await writer.close();
          result.bytes = await reading;
        } catch (error) { result.error = [error.name, error.message]; }
        result.done = true;
      })();
    '''))
    assert result == {
        'identity': [True, True, 'WritableStreamDefaultWriter',
                     'ReadableStreamDefaultReader', True, True],
        'bytes': [31, 139, 8, 0, 0, 0, 0, 0, 0, 10, 75, 76, 74, 6, 0,
                  194, 65, 36, 53, 3, 0, 0, 0],
        'done': True,
    }


async def test_three_formats_round_trip_incrementally():
    result = (await _run('''
      window.result = {};
      (async () => {
        async function collect(reader) {
          const bytes = [];
          for (;;) {
            const item = await reader.read();
            if (item.done) return bytes;
            bytes.push(...item.value);
          }
        }
        async function run(format) {
          const compression = new CompressionStream(format);
          const reading = collect(compression.readable.getReader());
          const writer = compression.writable.getWriter();
          await writer.write(new Uint8Array([97]));
          await writer.write(new Uint8Array([98, 99]));
          await writer.close();
          const encoded = await reading;
          const decompression = new DecompressionStream(format);
          const decoding = collect(decompression.readable.getReader());
          const decoder = decompression.writable.getWriter();
          await decoder.write(new Uint8Array(encoded));
          await decoder.close();
          return [encoded.length, await decoding];
        }
        try {
          result.gzip = await run('gzip');
          result.deflate = await run('deflate');
          result.raw = await run('deflate-raw');
        } catch (error) { result.error = [error.name, error.message]; }
        result.done = true;
      })();
    '''))
    assert result == {'gzip': [23, [97, 98, 99]],
                      'deflate': [11, [97, 98, 99]],
                      'raw': [5, [97, 98, 99]], 'done': True}


async def test_unsupported_format_and_missing_argument_match_chrome():
    result = (await _run('''
      window.result = {done: true, errors: []};
      for (const value of [undefined, 'brotli', null]) {
        try { value === undefined ? new CompressionStream() : new CompressionStream(value); }
        catch (error) { result.errors.push([error.name, error.message]); }
      }
    '''))
    assert result['errors'] == [
        ['TypeError', "Failed to construct 'CompressionStream': 1 argument required, but only 0 present."],
        ['TypeError', "Failed to construct 'CompressionStream': Unsupported compression format: 'brotli'"],
        ['TypeError', "Failed to construct 'CompressionStream': Unsupported compression format: 'null'"],
    ]


async def test_corrupt_and_truncated_input_reject_reader_and_writer():
    result = (await _run('''
      window.result = {};
      (async () => {
        async function attempt(bytes) {
          const stream = new DecompressionStream('gzip');
          const writer = stream.writable.getWriter();
          const reader = stream.readable.getReader();
          const settled = promise => promise.then(
              () => 'ok', error => [error.name, error.message]);
          return Promise.all([settled(reader.read()),
                              settled(writer.write(new Uint8Array(bytes))),
                              settled(writer.close())]);
        }
        result.corrupt = await attempt([1, 2, 3]);
        result.truncated = await attempt([31, 139, 8, 0, 0, 0, 0, 0, 0, 10]);
        result.done = true;
      })();
    '''))
    invalid = ['TypeError', 'The compressed data was not valid: incorrect header check.']
    truncated = ['TypeError', 'Compressed input was truncated.']
    assert result == {'corrupt': [invalid, invalid, invalid],
                      'truncated': [truncated, 'ok', truncated], 'done': True}
