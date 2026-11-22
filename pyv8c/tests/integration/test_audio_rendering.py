"""An offline context renders its graph: the samples an oscillator really produces."""
import math
import sys
import pytest

from pyv8c.async_api import Browser
from pyv8c.web.bom.audio import _OSCILLATOR_X86

RATE = 44100
LENGTH = 512


async def _open():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="https://repro.test/index.html", wait_until="commit"))
    (await page.run_until_idle(timeout=2))
    return browser, page


async def _render(page, body):
    """Run a graph and read what it left in ``window.__rendered``."""
    page.evaluate("window.__rendered = null;")
    page.evaluate("(async () => {" + body + "})()")
    (await page.run_until_idle(timeout=2))
    return page.evaluate("window.__rendered")


def _graph(node="oscillator.type = 'sine';", gain="gain.gain.value = 0.5;",
           schedule="oscillator.start();"):
    return f"""
        const context = new OfflineAudioContext(1, {LENGTH}, {RATE});
        const oscillator = context.createOscillator();
        oscillator.frequency.value = 440;
        {node}
        const gain = context.createGain();
        {gain}
        oscillator.connect(gain).connect(context.destination);
        {schedule}
        const rendered = await context.startRendering();
        const data = rendered.getChannelData(0);
        window.__rendered = [Array.from(data), rendered.length, rendered.sampleRate,
                             rendered.numberOfChannels, data instanceof Float32Array,
                             context.state, context.currentTime];
    """


async def test_a_sine_renders_the_samples_the_frequency_names():
    browser, page = (await _open())
    try:
        samples, length, rate, channels, typed, state, seconds = (await _render(page, _graph()))
    finally:
        (await browser.close())

    assert [length, rate, channels, typed, state] == [LENGTH, RATE, 1, True, "closed"]
    assert seconds == LENGTH / RATE
    for index, sample in enumerate(samples):
        wanted = 0.5 * math.sin(2.0 * math.pi * 440 * index / RATE)
        assert abs(sample - wanted) < 1e-5, (index, sample, wanted)


async def test_every_waveform_has_the_shape_it_is_named_for():
    #: The RMS Chrome measures for the same waveforms, within the slack its own
    #: per-octave band limiting leaves (its square reads 0.844, its sawtooth 0.487).
    wanted = {"sine": (0.707, 0.01), "square": (0.844, 0.02), "triangle": (0.580, 0.01),
              "sawtooth": (0.490, 0.02)}
    browser, page = (await _open())
    try:
        for kind, (rms, tolerance) in wanted.items():
            samples = (await _render(page, _graph(node=f"oscillator.type = '{kind}';",
                                           gain="gain.gain.value = 1;")))[0]
            measured = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
            assert abs(measured - rms) < tolerance, (kind, measured)
            # The table is sampled at 4096 points, so its own peak can pass the
            # continuous one by a hair where the waveform has a sharp corner.
            assert max(abs(sample) for sample in samples) <= 1.001, kind
    finally:
        (await browser.close())


async def test_a_waveform_keeps_the_amplitude_of_the_harmonics_it_has_left():
    """Chrome cuts a table to the harmonics a frequency can carry but scales every
    table by the same factor, so a 10 kHz triangle keeps 8/pi^2 rather than 1."""
    wanted = {("sine", 10000): (1.0, 0.70712), ("square", 10000): (1.07994, 0.76365),
              ("triangle", 10000): (0.81073, 0.57328), ("square", 5000): (1.01818, 0.805),
              ("triangle", 5000): (0.9008, 0.57692)}
    browser, page = (await _open())
    try:
        for (kind, frequency), (peak, rms) in wanted.items():
            samples = (await _render(page, f"""
                const context = new OfflineAudioContext(1, 4096, {RATE});
                const oscillator = context.createOscillator();
                oscillator.type = '{kind}';
                oscillator.frequency.value = {frequency};
                oscillator.connect(context.destination);
                oscillator.start(0);
                const data = (await context.startRendering()).getChannelData(0);
                window.__rendered = Array.from(data.slice(1024));
            """))
            measured = max(abs(sample) for sample in samples)
            energy = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
            assert abs(measured - peak) < 1e-3, (kind, frequency, measured, peak)
            assert abs(energy - rms) < 1e-3, (kind, frequency, energy, rms)
    finally:
        (await browser.close())


@pytest.mark.skipif(not _OSCILLATOR_X86, reason='Chrome reference uses x86 vector interpolation')
@pytest.mark.parametrize('frequency_setting', ['o.frequency.value = 10000;',
                                               'o.frequency.setValueAtTime(10000, 0);'])
async def test_triangle_phase_matches_chrome_across_render_quanta(frequency_setting):
    browser, page = (await _open())
    try:
        samples = (await _render(page, """
            const c = new OfflineAudioContext(1, 5000, 44100), o = c.createOscillator();
            o.type = 'triangle';
        """ + frequency_setting + """
            o.connect(c.destination); o.start();
            const a = (await c.startRendering()).getChannelData(0);
            window.__rendered = [99,106,110,115,119,123,126,128,129].map(i => a[i])
                .concat(Array.from(a.slice(4500, 4512)));
        """))
    finally:
        (await browser.close())
    assert samples == pytest.approx([
        0.25547096133232117, 0.1832147240638733, -0.28270959854125977,
        0.3775476813316345, -0.08072683960199356, -0.512229323387146,
        -0.3517583906650543, 0.12653622031211853, 0.8106836676597595,
        0.44241952896118164, -0.6077620387077332, -0.6193006634712219,
        0.4275202751159668, 0.7437264323234558, -0.21106889843940735,
        -0.8051546812057495, -0.02326374314725399, 0.7983841300010681,
        0.25562307238578796, -0.7239880561828613, -0.4663325846195221,
    ], abs=5e-7)


@pytest.mark.skipif(sys.platform != 'win32' or not _OSCILLATOR_X86,
                    reason='reference vector is from Chrome 157 on Windows x86')
async def test_offline_triangle_compressor_matches_chrome_float32_sum():
    browser, page = (await _open())
    try:
        total = (await _render(page, """
            const c = new OfflineAudioContext(1, 5000, 44100), o = c.createOscillator();
            o.type = 'triangle'; o.frequency.setValueAtTime(10000, 0);
            const d = c.createDynamicsCompressor();
            d.threshold.value = -50; d.knee.value = 40; d.ratio.value = 12;
            d.attack.value = 0; d.release.value = .25;
            o.connect(d).connect(c.destination); o.start();
            const a = (await c.startRendering()).getChannelData(0);
            window.__rendered = Array.from(a.slice(4500, 5000)).reduce((sum, v) => sum + Math.abs(v), 0);
        """))
    finally:
        (await browser.close())
    assert total == 124.04347776696522


@pytest.mark.skipif(sys.platform != 'win32' or not _OSCILLATOR_X86,
                    reason='reference samples are from Chrome 157 on Windows x86')
async def test_compressor_automation_clamps_after_interpolation_at_render_quanta():
    browser, page = (await _open())
    try:
        samples = (await _render(page, """
            const c = new OfflineAudioContext(1, 5000, 44100), o = c.createOscillator();
            o.frequency.value = 1000;
            const d = c.createDynamicsCompressor();
            d.threshold.setValueAtTime(-50, 0);
            d.threshold.linearRampToValueAtTime(-1000, 0.1);
            o.connect(d).connect(c.destination); o.start();
            const a = (await c.startRendering()).getChannelData(0);
            window.__rendered = [a[1000], a[2000], a[4000]];
        """))
    finally:
        (await browser.close())
    assert samples == pytest.approx([-0.03327003866434097, 0.026643099263310432,
                                      -0.03499085083603859], abs=2e-4)


async def test_compressor_parameter_value_and_automation_rate_match_chrome():
    browser, page = (await _open())
    try:
        result = page.evaluate("""(() => {
            const d = new OfflineAudioContext(1, 100, 44100).createDynamicsCompressor();
            d.threshold.value = -1000;
            const low = d.threshold.value;
            d.threshold.value = 1000;
            const high = d.threshold.value;
            let error;
            try { d.threshold.automationRate = 'k-rate'; }
            catch (e) { error = e.name; }
            return [low, high, d.threshold.automationRate, error];
        })()""")
    finally:
        (await browser.close())
    assert result == [-100, 0, 'k-rate', 'InvalidStateError']


async def test_a_gain_ramp_scales_the_tone_as_it_climbs():
    browser, page = (await _open())
    try:
        samples = (await _render(page, _graph(
            gain=f"gain.gain.setValueAtTime(0, 0);"
                 f" gain.gain.linearRampToValueAtTime(1, {LENGTH / RATE});")))[0]
    finally:
        (await browser.close())

    for index, sample in enumerate(samples):
        wanted = math.sin(2.0 * math.pi * 440 * index / RATE) * index / LENGTH
        assert abs(sample - wanted) < 1e-5, (index, sample, wanted)


async def test_a_source_is_silent_outside_its_start_and_stop():
    half = LENGTH / 2 / RATE
    browser, page = (await _open())
    try:
        samples = (await _render(page, _graph(schedule=f"oscillator.start({half});"
                                                f" oscillator.stop({LENGTH / RATE});")))[0]
    finally:
        (await browser.close())

    assert samples[:LENGTH // 2] == [0.0] * (LENGTH // 2)
    assert any(sample != 0.0 for sample in samples[LENGTH // 2:])


async def test_a_buffer_source_plays_the_samples_the_buffer_holds():
    """At rate 1 the buffer comes out as written, then silence, and the end is reported."""
    browser, page = (await _open())
    try:
        rendered = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 8, {RATE});
            const buffer = context.createBuffer(1, 4, {RATE});
            buffer.copyToChannel(new Float32Array([0.5, -0.25, 0.125, 1.0]), 0);
            const source = context.createBufferSource();
            source.buffer = buffer;
            let ended = false;
            source.onended = () => {{ ended = true; }};
            source.connect(context.destination);
            source.start();
            const data = (await context.startRendering()).getChannelData(0);
            window.__rendered = [Array.from(data), ended, source.buffer === buffer,
                                 source.playbackRate.value, source.loop];
        """))
    finally:
        (await browser.close())

    samples, ended, same_buffer, rate, loop = rendered
    assert samples == [0.5, -0.25, 0.125, 1.0, 0.0, 0.0, 0.0, 0.0]
    assert ended is True
    assert [same_buffer, rate, loop] == [True, 1, False]


async def test_a_buffer_source_reads_faster_when_its_rate_says_so():
    browser, page = (await _open())
    try:
        rendered = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 6, {RATE});
            const buffer = context.createBuffer(1, 4, {RATE});
            buffer.copyToChannel(new Float32Array([0.5, -0.25, 0.125, 1.0]), 0);
            const source = context.createBufferSource();
            source.buffer = buffer;
            source.playbackRate.value = 2;
            source.connect(context.destination);
            source.start();
            window.__rendered = Array.from(
                (await context.startRendering()).getChannelData(0));
        """))
    finally:
        (await browser.close())

    assert rendered == [0.5, 0.125, 0.0, 0.0, 0.0, 0.0]


async def test_compressor_lookahead_matches_chrome_at_two_sample_rates():
    browser, page = (await _open())
    try:
        for rate, first in [(44100, 265), (48000, 289)]:
            result = (await _render(page, f'''
                const context = new OfflineAudioContext(1, 1024, {rate});
                const oscillator = context.createOscillator();
                oscillator.frequency.value = 1000;
                oscillator.connect(context.createDynamicsCompressor()).connect(context.destination);
                oscillator.start(0);
                const samples = (await context.startRendering()).getChannelData(0);
                window.__rendered = [samples.findIndex(value => value !== 0),
                                     Array.from(samples.slice(0, 16))];
            '''))
            assert result == [first, [0] * 16]
    finally:
        (await browser.close())


async def test_a_compressor_takes_a_full_scale_tone_down():
    """Chrome 156: detector reduction and audible gain differ by makeup gain."""
    browser, page = (await _open())
    try:
        reduction, peak = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 4096, {RATE});
            const oscillator = context.createOscillator();
            const compressor = context.createDynamicsCompressor();
            oscillator.connect(compressor).connect(context.destination);
            oscillator.start();
            const data = (await context.startRendering()).getChannelData(0);
            let peak = 0;
            for (let i = 2048; i < data.length; i++) peak = Math.max(peak, Math.abs(data[i]));
            window.__rendered = [compressor.reduction, peak];
        """))
    finally:
        (await browser.close())

    assert abs(reduction - -12.614407539367676) < 0.001, reduction
    assert abs(peak - 0.5828936100006104) < 0.0001, peak


async def test_compressor_static_curve_makeup_gain_and_detector_match_chrome():
    # Chrome 156.0.8078.80, headless Windows, identical graphs and PCM windows.
    cases = [
        ("sine", 1000, 48000, 4096, 0.001, "", 0.0010887630051001906, 1.1739967049469768, -12.799138069152832),
        ("sine", 1000, 48000, 4096, 1, "compressor.ratio.value=1;", 0.6705998182296753, 739.1878723369584, -13.024518966674805),
        ("sine", 440, 44100, 4096, 0, "", 0, 0, -12.481213569641113),
        ("sine", 1000, 48000, 8192, 1,
         "compressor.threshold.value=-10;compressor.knee.value=0;compressor.ratio.value=20;"
         "compressor.attack.value=.01;compressor.release.value=.05;", 0.6807208061218262, 2646.853668376237, -9.154073715209961),
        ("triangle", 10000, 44100, 44100, 1,
         "compressor.threshold.value=-50;compressor.knee.value=40;compressor.ratio.value=12;"
         "compressor.attack.value=0;compressor.release.value=.25;", 0.3947939872741699, 124.04347776696522, -19.004104614257812),
    ]
    browser, page = (await _open())
    try:
        for kind, frequency, rate, length, amplitude, parameters, peak, total, reduction in cases:
            measured = (await _render(page, f"""
                const context = new OfflineAudioContext(1, {length}, {rate});
                const oscillator = context.createOscillator();
                oscillator.type = '{kind}'; oscillator.frequency.value = {frequency};
                const gain = context.createGain(); gain.gain.value = {amplitude};
                const compressor = context.createDynamicsCompressor(); {parameters}
                oscillator.connect(gain).connect(compressor).connect(context.destination); oscillator.start();
                const data = (await context.startRendering()).getChannelData(0);
                const tail = data.slice({4500 if length == 44100 else 2048}, {5000 if length == 44100 else length});
                window.__rendered = [tail.reduce((p,v)=>Math.max(p,Math.abs(v)),0),
                                    tail.reduce((s,v)=>s+Math.abs(v),0), compressor.reduction,
                                    data.every(Number.isFinite)];
            """))
            assert measured[3] is True
            assert abs(measured[0] - peak) < 0.0001, (kind, measured)
            assert abs(measured[1] - total) < max(0.03, total * 0.0001), (kind, measured)
            assert abs(measured[2] - reduction) < 0.004, (kind, measured)
    finally:
        (await browser.close())


async def test_a_buffer_hands_back_a_float32_array_that_holds_what_was_written():
    browser, page = (await _open())
    try:
        result = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 8, {RATE});
            const buffer = context.createBuffer(1, 8, {RATE});
            buffer.copyToChannel(new Float32Array([0.5, -0.25, 0.125]), 0, 2);
            const data = buffer.getChannelData(0);
            window.__rendered = [data instanceof Float32Array, data === buffer.getChannelData(0),
                                 Array.from(data)];
        """))
    finally:
        (await browser.close())

    assert result[0] is True
    assert result[1] is True
    assert result[2] == [0.0, 0.0, 0.5, -0.25, 0.125, 0.0, 0.0, 0.0]


async def test_a_waveform_scale_is_the_peak_of_its_series():
    """Each scale is a constant of its waveform, carried as one: the search it came
    from has to agree with it, digit for digit."""
    from pyv8c.web.bom.audio import _WAVE_POINTS, _wave_samples, _wave_scale

    for kind in ("sine", "square", "sawtooth", "triangle"):
        peak = max(abs(value) for value in _wave_samples(kind, _WAVE_POINTS // 2)) or 1.0
        assert _wave_scale(kind) == 1.0 / peak, kind


async def test_high_frequency_oscillator_uses_chromium_phase_increment():
    browser, page = (await _open())
    try:
        samples = (await _render(page, '''
            const context = new OfflineAudioContext(1, 4600, 44100);
            const oscillator = context.createOscillator();
            oscillator.type = 'triangle'; oscillator.frequency.value = 10000;
            oscillator.connect(context.destination); oscillator.start();
            const data = (await context.startRendering()).getChannelData(0);
            window.__rendered = Array.from(data.slice(4500,4504));
        '''))
        reference = [0.44241952896118164, -0.6077620387077332,
                     -0.6193006634712219, 0.4275202751159668]
        assert all(abs(actual - expected) < 0.00001 for actual, expected in zip(samples, reference))
    finally:
        (await browser.close())
