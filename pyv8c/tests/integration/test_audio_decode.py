"""decodeAudioData: the samples Chrome reads out of a WAV file, and what it refuses."""
from pyv8c.async_api import Browser

RATE = 8000

#: A RIFF/WAVE file built in the page, so the bytes are spelled out here and nowhere else.
WAV = """
    const wav = (sampleRate, channels, samples, bits, isFloat) => {
        const width = bits / 8;
        const bytes = samples.length * width;
        const buffer = new ArrayBuffer(44 + bytes);
        const view = new DataView(buffer);
        const text = (offset, value) => {
            for (let index = 0; index < value.length; index++)
                view.setUint8(offset + index, value.charCodeAt(index));
        };
        text(0, 'RIFF'); view.setUint32(4, 36 + bytes, true); text(8, 'WAVE');
        text(12, 'fmt '); view.setUint32(16, 16, true);
        view.setUint16(20, isFloat ? 3 : 1, true);
        view.setUint16(22, channels, true);
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * channels * width, true);
        view.setUint16(32, channels * width, true);
        view.setUint16(34, bits, true);
        text(36, 'data'); view.setUint32(40, bytes, true);
        let offset = 44;
        for (const value of samples) {
            if (isFloat) { view.setFloat32(offset, value, true); offset += 4; }
            else if (bits === 16) { view.setInt16(offset, Math.round(value * 32767), true);
                                    offset += 2; }
            else { view.setUint8(offset, Math.round(value * 127 + 128)); offset += 1; }
        }
        return buffer;
    };
    const ramp = [0, 0.25, 0.5, 0.75, 1, 0.5, -0.5, -1];
"""


async def _open():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="https://repro.test/index.html", wait_until="commit"))
    (await page.run_until_idle(timeout=2))
    return browser, page


def _six(samples):
    """The samples as the tests quote them, which is what Chrome reports rounded."""
    return [round(sample, 6) for sample in samples]


async def _render(page, body):
    page.evaluate("window.__rendered = null;")
    page.evaluate("(async () => {" + WAV + body + "})()")
    (await page.run_until_idle(timeout=30))
    return page.evaluate("window.__rendered")


async def test_a_wav_at_the_context_rate_keeps_its_samples():
    browser, page = (await _open())
    try:
        rendered = (await _render(page, f"""
            const context = new AudioContext({{sampleRate: {RATE}}});
            const buffer = await context.decodeAudioData(wav({RATE}, 1, ramp, 16, false));
            window.__rendered = [buffer.sampleRate, buffer.length, buffer.numberOfChannels,
                                 Array.from(buffer.getChannelData(0))];
            await context.close();
        """))
    finally:
        (await browser.close())

    rate, length, channels, samples = rendered
    assert [rate, length, channels] == [RATE, 8, 1]
    # Chrome's integers divide by 32767 above zero and by 32768 below it.
    assert _six(samples) == [0.0, 0.250008, 0.500015, 0.749992, 1.0, 0.500015,
                             -0.499969, -0.999969]


async def test_each_width_comes_back_with_chrome_s_conversion():
    browser, page = (await _open())
    try:
        rendered = (await _render(page, f"""
            const read = async (bits, isFloat) => {{
                const context = new AudioContext({{sampleRate: {RATE}}});
                const buffer = await context.decodeAudioData(
                    wav({RATE}, 1, ramp, bits, isFloat));
                const data = Array.from(buffer.getChannelData(0));
                await context.close();
                return data;
            }};
            window.__rendered = [await read(8, false), await read(32, true)];
        """))
    finally:
        (await browser.close())

    eight, floats = rendered
    assert _six(eight) == [0.0, 0.251969, 0.503937, 0.748031, 1.0, 0.503937, -0.492188,
                           -0.992188]
    assert _six(floats) == [0.0, 0.25, 0.5, 0.75, 1.0, 0.5, -0.5, -1.0]


async def test_a_stereo_file_keeps_its_channels_apart():
    browser, page = (await _open())
    try:
        rendered = (await _render(page, f"""
            const context = new AudioContext({{sampleRate: {RATE}}});
            const buffer = await context.decodeAudioData(
                wav({RATE}, 2, [0.5, -0.5, 0.25, -0.25, 1, -1, 0, 0], 16, false));
            window.__rendered = [buffer.numberOfChannels, buffer.length,
                                 Array.from(buffer.getChannelData(1))];
            await context.close();
        """))
    finally:
        (await browser.close())

    channels, length, second = rendered
    assert [channels, length] == [2, 4]
    assert _six(second) == [-0.499969, -0.25, -0.999969, 0.0]


async def test_a_file_at_another_rate_arrives_at_the_context_rate():
    """Chrome resamples onto the context's rate; its own resampler is a filtered one,
    so the length and rate are pinned here and the samples only roughly."""
    browser, page = (await _open())
    try:
        rendered = (await _render(page, f"""
            const context = new AudioContext({{sampleRate: 44100}});
            const buffer = await context.decodeAudioData(wav({RATE}, 1, ramp, 16, false));
            window.__rendered = [buffer.sampleRate, buffer.length,
                                 Array.from(buffer.getChannelData(0))];
            await context.close();
        """))
    finally:
        (await browser.close())

    rate, length, samples = rendered
    assert [rate, length] == [44100, 44]
    assert samples[0] == 0.0 and abs(samples[-1] - -1.0) < 0.01
    assert samples[4] < samples[5] < samples[8]  # a ramp, read between its samples


async def test_bytes_that_are_not_audio_are_refused():
    browser, page = (await _open())
    try:
        rendered = (await _render(page, """
            const context = new AudioContext();
            const attempt = async (value) => {
                try { await context.decodeAudioData(value); return 'resolved'; }
                catch (error) { return error.name + ': ' + error.message; }
            };
            const throws = (value) => {
                try { context.decodeAudioData(value); return 'no'; }
                catch (error) { return error.name; }
            };
            window.__rendered = [await attempt(new ArrayBuffer(0)),
                                 await attempt(new ArrayBuffer(64)),
                                 await attempt(new Uint8Array(64)),
                                 await attempt(),
                                 throws(new ArrayBuffer(8)), throws(new Uint8Array(8))];
            await context.close();
        """))
    finally:
        (await browser.close())

    empty, garbage, view, missing, plain_throw, view_throw = rendered
    assert empty == "EncodingError: Unable to decode audio data"
    assert garbage == empty
    # Chrome's argument conversion rejects the same promise with a TypeError, and
    # nothing it refuses leaves the call as a synchronous throw.
    assert view.startswith("TypeError: ") and missing.startswith("TypeError: "), rendered
    assert [plain_throw, view_throw] == ["no", "no"], rendered


async def test_the_older_callback_form_is_called_too():
    browser, page = (await _open())
    try:
        rendered = (await _render(page, f"""
            const context = new AudioContext({{sampleRate: {RATE}}});
            const seen = [];
            context.decodeAudioData(wav({RATE}, 1, ramp, 16, false),
                (buffer) => seen.push('ok:' + buffer.length),
                (error) => seen.push('err:' + error.name));
            context.decodeAudioData(new ArrayBuffer(8),
                (buffer) => seen.push('ok:' + buffer.length),
                (error) => seen.push('err:' + error.name));
            window.__rendered = seen;
            await context.close();
        """))
    finally:
        (await browser.close())

    assert rendered == ["ok:8", "err:EncodingError"]
