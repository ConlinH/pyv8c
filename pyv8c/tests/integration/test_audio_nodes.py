"""The audio nodes render the samples a real Chrome renders for the same graph."""

from pyv8c.async_api import Browser

RATE = 8000


async def _open():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="https://repro.test/index.html", wait_until="commit"))
    (await page.run_until_idle(timeout=2))
    return browser, page


async def _render(page, body):
    page.evaluate("window.__rendered = null;")
    page.evaluate("(async () => {" + body + "})()")
    (await page.run_until_idle(timeout=10))
    return page.evaluate("window.__rendered")


def _close(value, wanted, tolerance=1e-5):
    return abs(value - wanted) < tolerance


async def test_a_constant_source_holds_its_offset():
    browser, page = (await _open())
    try:
        held, late = (await _render(page, f"""
            const held = new OfflineAudioContext(1, 6, {RATE});
            const steady = held.createConstantSource();
            steady.offset.value = 0.5;
            steady.connect(held.destination);
            steady.start();
            const first = Array.from((await held.startRendering()).getChannelData(0));
            const later = new OfflineAudioContext(1, 6, {RATE});
            const delayed = later.createConstantSource();
            delayed.offset.value = 0.25;
            delayed.connect(later.destination);
            delayed.start(2 / {RATE});
            const second = Array.from((await later.startRendering()).getChannelData(0));
            window.__rendered = [first, second];
        """))
    finally:
        (await browser.close())

    assert held == [0.5] * 6
    assert late == [0.0, 0.0, 0.25, 0.25, 0.25, 0.25]


async def test_a_delay_line_reads_its_input_later():
    browser, page = (await _open())
    try:
        whole, fractional = (await _render(page, f"""
            const read = async (delay) => {{
                const context = new OfflineAudioContext(1, 12, {RATE});
                const source = context.createConstantSource();
                const line = context.createDelay(0.01);
                line.delayTime.value = delay;
                source.connect(line).connect(context.destination);
                source.start();
                return Array.from((await context.startRendering()).getChannelData(0));
            }};
            window.__rendered = [await read(4 / {RATE}), await read(2.5 / {RATE})];
        """))
    finally:
        (await browser.close())

    assert whole == [0.0] * 4 + [1.0] * 8
    assert fractional[:8] == [0.0, 0.0, 0.5, 1.0, 1.0, 1.0, 1.0, 1.0]


async def test_a_wave_shaper_maps_through_its_curve():
    browser, page = (await _open())
    try:
        samples = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 4, {RATE});
            const source = context.createConstantSource();
            const shaper = context.createWaveShaper();
            shaper.curve = new Float32Array([-1, 0, 1]);
            source.offset.value = 0.5;
            source.connect(shaper).connect(context.destination);
            source.start();
            window.__rendered = Array.from(
                (await context.startRendering()).getChannelData(0));
        """))
    finally:
        (await browser.close())

    assert samples == [0.5] * 4


async def test_a_biquad_filter_is_the_response_chrome_reports():
    browser, page = (await _open())
    try:
        step, response = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 400, {RATE});
            const source = context.createConstantSource();
            const filter = context.createBiquadFilter();
            filter.frequency.value = 1000;
            source.connect(filter).connect(context.destination);
            source.start();
            const data = (await context.startRendering()).getChannelData(0);
            const magnitude = new Float32Array(2);
            const phase = new Float32Array(2);
            context.createBiquadFilter().getFrequencyResponse(
                new Float32Array([100, 1000]), magnitude, phase);
            window.__rendered = [[data[1], data[10], data[100], data[399]],
                                 [Array.from(magnitude), Array.from(phase)]];
        """))
    finally:
        (await browser.close())

    assert all(_close(value, wanted) for value, wanted in
               zip(step, [0.453822, 0.977506, 1.0, 1.0])), step
    magnitude, phase = response
    assert _close(magnitude[0], 1.048731) and _close(magnitude[1], 0.119002), magnitude
    assert _close(phase[0], -0.268728) and _close(phase[1], -2.818377), phase


async def test_an_iir_filter_is_the_response_chrome_reports():
    browser, page = (await _open())
    try:
        samples, response = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 6, {RATE});
            const source = context.createConstantSource();
            const filter = context.createIIRFilter([1], [1]);
            source.connect(filter).connect(context.destination);
            source.start();
            const data = (await context.startRendering()).getChannelData(0);
            const gain = context.createIIRFilter([0.5], [1]);
            const magnitude = new Float32Array(1);
            const phase = new Float32Array(1);
            gain.getFrequencyResponse(new Float32Array([1000]), magnitude, phase);
            window.__rendered = [Array.from(data), [Array.from(magnitude), Array.from(phase)]];
        """))
    finally:
        (await browser.close())

    assert samples == [1.0] * 6
    assert response == [[0.5], [0.0]]


async def test_a_convolver_scales_the_impulse_the_way_chrome_does():
    browser, page = (await _open())
    try:
        late, early, gain, scaled = (await _render(page, f"""
            const impulse = (context, values) => {{
                const buffer = context.createBuffer(1, values.length, {RATE});
                buffer.copyToChannel(new Float32Array(values), 0);
                return buffer;
            }};
            const play = async (values, normalize) => {{
                const context = new OfflineAudioContext(1, 4, {RATE});
                const source = context.createConstantSource();
                const convolver = context.createConvolver();
                if (normalize !== 'late') convolver.normalize = normalize;
                convolver.buffer = impulse(context, values);
                if (normalize === 'late') convolver.normalize = false;
                source.connect(convolver).connect(context.destination);
                source.start();
                return Array.from((await context.startRendering()).getChannelData(0));
            }};
            window.__rendered = [await play([1], 'late'), await play([1], false),
                                 await play([0.5, 0.5], false), await play([0.5, 0.5], true)];
        """))
    finally:
        (await browser.close())

    assert all(_close(value, 0.00694) for value in late), late
    assert early == [1.0] * 4
    assert gain == [0.5, 1.0, 1.0, 1.0]
    assert all(_close(value, wanted) for value, wanted in
               zip(scaled, [0.00694, 0.01388, 0.01388, 0.01388])), scaled


async def test_a_panner_places_the_source_where_chrome_places_it():
    browser, page = (await _open())
    try:
        centre, right, diagonal, distance, moved, cone = (await _render(page, f"""
            const play = async (place) => {{
                const context = new OfflineAudioContext(1, 4, {RATE});
                const source = context.createConstantSource();
                const panner = context.createPanner();
                place(context, panner);
                source.connect(panner).connect(context.destination);
                source.start();
                return Array.from((await context.startRendering()).getChannelData(0))[0];
            }};
            window.__rendered = [
                await play(() => {{}}),
                await play((c, p) => {{ p.positionX.value = 1; }}),
                await play((c, p) => {{ p.positionX.value = 1; p.positionZ.value = -1; }}),
                await play((c, p) => {{ p.positionZ.value = -2; }}),
                await play((c, p) => {{ c.listener.setPosition(0, 0, -1); p.positionZ.value = -2; }}),
                await play((c, p) => {{ p.coneInnerAngle = 0; p.coneOuterAngle = 90;
                                        p.coneOuterGain = 0.25; p.positionZ.value = -1; }})];
        """))
    finally:
        (await browser.close())

    for measured, wanted in zip([centre, right, diagonal, distance, moved, cone],
                                [0.707107, 0.5, 0.46194, 0.353553, 0.707107, 0.176777]):
        assert _close(measured, wanted), (measured, wanted)


async def test_a_stereo_panner_spreads_the_source():
    browser, page = (await _open())
    try:
        centre, left, right = (await _render(page, f"""
            const play = async (pan) => {{
                const context = new OfflineAudioContext(1, 4, {RATE});
                const source = context.createConstantSource();
                const panner = context.createStereoPanner();
                if (pan !== null) panner.pan.value = pan;
                source.connect(panner).connect(context.destination);
                source.start();
                return Array.from((await context.startRendering()).getChannelData(0))[0];
            }};
            window.__rendered = [await play(null), await play(-1), await play(1)];
        """))
    finally:
        (await browser.close())

    for measured, wanted in zip([centre, left, right], [0.707107, 0.5, 0.5]):
        assert _close(measured, wanted), (measured, wanted)


async def test_a_splitter_and_a_merger_pass_the_signal_through():
    browser, page = (await _open())
    try:
        samples = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 4, {RATE});
            const source = context.createConstantSource();
            const splitter = context.createChannelSplitter(2);
            const merger = context.createChannelMerger(2);
            source.connect(splitter);
            splitter.connect(merger, 0, 0);
            merger.connect(context.destination);
            source.start();
            window.__rendered = Array.from(
                (await context.startRendering()).getChannelData(0));
        """))
    finally:
        (await browser.close())

    assert samples == [0.5] * 4


async def test_a_periodic_wave_replaces_the_waveform():
    browser, page = (await _open())
    try:
        cosine, sines, silent, kind = (await _render(page, f"""
            const play = async (make) => {{
                const context = new OfflineAudioContext(1, 8, {RATE});
                const oscillator = context.createOscillator();
                oscillator.frequency.value = 1000;
                oscillator.setPeriodicWave(make(context));
                oscillator.connect(context.destination);
                oscillator.start();
                const data = (await context.startRendering()).getChannelData(0);
                return [Array.from(data), oscillator.type];
            }};
            const wave = (context, options) => new PeriodicWave(context, options);
            const cosine = await play((c) => c.createPeriodicWave([0, 1], [0, 0]));
            const sines = await play((c) => wave(c, {{real: new Float32Array([0, 0, 0]),
                imag: new Float32Array([0, 1, 0.5]), disableNormalization: true}}));
            const silent = await play((c) => wave(c, {{real: new Float32Array([0, 0]),
                imag: new Float32Array([0, 0])}}));
            window.__rendered = [cosine[0], sines[0], silent[0], cosine[1]];
        """))
    finally:
        (await browser.close())

    assert all(_close(value, wanted) for value, wanted in
               zip(cosine, [1, 0.707107, 0, -0.707107, -1, -0.707107, 0, 0.707107])), cosine
    assert all(_close(value, wanted) for value, wanted in
               zip(sines, [0, 1.207107, 1, 0.207107, 0, -0.207107, -1, -1.207107])), sines
    assert silent == [0.0] * 8
    assert kind == "custom"


async def test_a_script_processor_gets_one_block_and_stays_silent():
    browser, page = (await _open())
    try:
        blocks, samples = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 300, {RATE});
            const source = context.createConstantSource();
            const processor = context.createScriptProcessor(256, 1, 1);
            const seen = [];
            processor.onaudioprocess = (event) => {{
                seen.push([event.inputBuffer.length, event.inputBuffer.getChannelData(0)[0],
                           event.playbackTime]);
                const output = event.outputBuffer.getChannelData(0);
                for (let index = 0; index < output.length; index++) output[index] = 1;
            }};
            source.connect(processor).connect(context.destination);
            source.start();
            const data = (await context.startRendering()).getChannelData(0);
            window.__rendered = [seen, [data[0], data[255], data[299]]];
        """))
    finally:
        (await browser.close())

    assert len(blocks) == 1, blocks
    length, first, playback = blocks[0]
    assert [length, first] == [256, 1.0]
    assert _close(playback, 384 / RATE)
    # Chrome runs the callback after the samples it was given, so its output never lands.
    assert samples == [0.0, 0.0, 0.0]


async def test_a_live_context_reports_chromes_base_latency():
    browser, page = (await _open())
    try:
        assert page.evaluate("""(() => {
            const context = new AudioContext();
            const value = [context.sampleRate, context.baseLatency, context.outputLatency,
                           context.destination.maxChannelCount];
            context.close();
            return value;
        })()""") == [48000, 0.01, 0, 2]
    finally:
        (await browser.close())


async def test_audio_param_values_use_binary32():
    browser, page = (await _open())
    try:
        values = page.evaluate('''(() => {
            const context = new AudioContext();
            const param = context.createDynamicsCompressor().attack;
            const initial = [param.value, param.defaultValue];
            param.value = 0.1;
            return [...initial, param.value];
        })()''')
        assert list(values) == [0.003000000026077032, 0.003000000026077032, 0.10000000149011612]
    finally:
        (await browser.close())
