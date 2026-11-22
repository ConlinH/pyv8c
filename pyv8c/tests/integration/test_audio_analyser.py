"""An analyser reports the spectrum a real Chrome reports for the same graph."""
import math

from pyv8c.async_api import Browser

RATE = 8000

#: What Chrome answers for a full-scale 1000 Hz tone in a 32-sample window: the peak is
#: the window's coherent gain over two, and its neighbours are the window's sidelobes.
PEAK_DB = 20 * math.log10(0.42 / 2)


async def _open():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="https://repro.test/index.html", wait_until="commit"))
    (await page.run_until_idle(timeout=2))
    return browser, page


async def _render(page, body):
    page.evaluate("window.__rendered = null;")
    page.evaluate("(async () => {" + body + "})()")
    (await page.run_until_idle(timeout=30))
    return page.evaluate("window.__rendered")


def _spectrum(frequency=1000, fft_size=32, smoothing=0.0, length=4096, connect=True,
              gain=None, method="getFloatFrequencyData", array_size=None):
    """A graph that plays one tone into an analyser and reads one of its windows."""
    wiring = ("source.connect(gain).connect(analyser)" if gain is not None
              else "source.connect(analyser)")
    destination = "analyser.connect(context.destination);" if connect else ""
    size = array_size or ("analyser.fftSize" if method == "getFloatTimeDomainData"
                          else "analyser.frequencyBinCount")
    return f"""
        const context = new OfflineAudioContext(1, {length}, {RATE});
        const analyser = context.createAnalyser();
        analyser.fftSize = {fft_size};
        analyser.smoothingTimeConstant = {smoothing};
        const source = context.createOscillator();
        source.frequency.value = {frequency};
        const gain = context.createGain();
        {'gain.gain.value = ' + str(gain) + ';' if gain is not None else ''}
        {wiring};
        {destination}
        source.start();
        await context.startRendering();
        const data = new Float32Array({size});
        analyser.{method}(data);
        window.__rendered = [analyser.frequencyBinCount, Array.from(data)];
    """


async def test_a_tone_lands_in_the_bin_its_frequency_names():
    browser, page = (await _open())
    try:
        bins, spectrum = (await _render(page, _spectrum()))
    finally:
        (await browser.close())

    assert bins == 16
    peak = max(range(bins), key=lambda index: spectrum[index])
    assert peak == 4, spectrum
    assert abs(spectrum[4] - PEAK_DB) < 1e-4, spectrum[4]
    for index, wanted in zip((2, 3, 5, 6), (-33.979401, -18.0618, -18.0618, -33.979401)):
        assert abs(spectrum[index] - wanted) < 1e-4, spectrum[index]
    assert spectrum[0] < -100 and spectrum[8] < -100
    assert all(value < -100 for value in spectrum[9:])


async def test_smoothing_takes_one_step_for_the_whole_render():
    browser, page = (await _open())
    try:
        result = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 4096, {RATE});
            const analyser = context.createAnalyser();
            analyser.fftSize = 32;
            const source = context.createOscillator();
            source.frequency.value = 1000;
            source.connect(analyser).connect(context.destination);
            source.start();
            await context.startRendering();
            const read = () => {{
                const data = new Float32Array(16);
                analyser.getFloatFrequencyData(data);
                return data[4];
            }};
            window.__rendered = [read(), read(), read()];
        """))
    finally:
        (await browser.close())

    first, second, third = result
    assert abs(first - (PEAK_DB + 20 * math.log10(0.2))) < 1e-4, result
    assert first == second == third, result


async def test_the_spectrum_matches_the_one_chrome_reports():
    """A 700 Hz tone leaks across every bin; where there is signal the values are
    Chrome's, and below -100 dB each engine reports its own floating-point floor."""
    browser, page = (await _open())
    try:
        bins, spectrum = (await _render(page, _spectrum(frequency=700)))
    finally:
        (await browser.close())

    wanted = [-139.862, -29.545248, -16.398142, -13.73333, -20.141911, -39.468781,
              -79.622406, -75.81382, -79.476242, -83.281189, -86.834496, -90.197327,
              -93.541985, -97.082275, -101.327126, -107.780594]
    assert bins == len(wanted)
    for index, (measured, target) in enumerate(zip(spectrum, wanted)):
        if min(measured, target) < -100:
            assert measured < -100, (index, measured)
            continue
        assert abs(measured - target) < 0.01, (index, measured, target)


async def test_the_gain_scales_the_spectrum():
    browser, page = (await _open())
    try:
        _, spectrum = (await _render(page, _spectrum(gain=0.5)))
    finally:
        (await browser.close())

    assert abs(spectrum[4] - (PEAK_DB - 20 * math.log10(2))) < 1e-4, spectrum[4]


async def test_a_tone_off_the_destination_is_still_measured():
    browser, page = (await _open())
    try:
        _, spectrum = (await _render(page, _spectrum(connect=False)))
    finally:
        (await browser.close())

    assert abs(spectrum[4] - PEAK_DB) < 1e-4, spectrum[4]


async def test_silence_and_an_unfed_analyser_read_minus_infinity():
    browser, page = (await _open())
    try:
        unfed, silent = (await _render(page, f"""
            const idle = new Float32Array(4);
            new OfflineAudioContext(1, 128, {RATE}).createAnalyser()
                .getFloatFrequencyData(idle);
            const context = new OfflineAudioContext(1, 4096, {RATE});
            const analyser = context.createAnalyser();
            analyser.fftSize = 32;
            analyser.smoothingTimeConstant = 0;
            analyser.connect(context.destination);
            await context.startRendering();
            const data = new Float32Array(16);
            analyser.getFloatFrequencyData(data);
            window.__rendered = [Array.from(idle), Array.from(data)];
        """))
    finally:
        (await browser.close())

    assert unfed == [float("-inf")] * 4
    assert silent == [float("-inf")] * 16


async def test_a_short_array_is_filled_and_a_long_one_keeps_its_tail():
    browser, page = (await _open())
    try:
        short, tail = (await _render(page, _spectrum() + f"""
            const short = new Float32Array(4);
            analyser.getFloatFrequencyData(short);
            const long = new Float32Array(analyser.frequencyBinCount + 3);
            long.fill(123);
            analyser.getFloatFrequencyData(long);
            window.__rendered = [Array.from(short), Array.from(long).slice(-3)];
        """, ))
    finally:
        (await browser.close())

    assert abs(short[2] - -33.979401) < 1e-4, short
    assert tail == [123, 123, 123]


async def test_the_window_is_the_most_recent_samples():
    browser, page = (await _open())
    try:
        unfed, narrow, wide = (await _render(page, f"""
            const unfed = new Float32Array(4);
            new OfflineAudioContext(1, 128, {RATE}).createAnalyser()
                .getFloatTimeDomainData(unfed);
            const read = async (fftSize) => {{
                const context = new OfflineAudioContext(1, 4096, {RATE});
                const analyser = context.createAnalyser();
                analyser.fftSize = fftSize;
                const source = context.createOscillator();
                source.frequency.value = 1000;
                source.connect(analyser).connect(context.destination);
                source.start();
                await context.startRendering();
                const data = new Float32Array(analyser.fftSize);
                analyser.getFloatTimeDomainData(data);
                return Array.from(data);
            }};
            window.__rendered = [Array.from(unfed), await read(32), await read(2048)];
        """))
    finally:
        (await browser.close())

    assert unfed == [0.0] * 4
    # 1000 Hz at 8000 is eight samples a cycle, and this window starts on one.
    cycle = [0.0, 0.707107, 1.0, 0.707107, 0.0, -0.707107, -1.0, -0.707107]
    assert all(abs(value - wanted) < 1e-5 for value, wanted in zip(narrow, cycle * 4)), narrow
    assert [round(value, 6) for value in wide[:3]] == [0.0, 0.707107, 1.0]
    assert [round(value, 6) for value in wide[-2:]] == [-1.0, -0.707107]


async def test_the_waveform_matches_chrome_at_a_fractional_phase():
    """700 Hz does not land on a sample boundary, so Chrome's own oscillator shows its
    accumulation error here: the two agree to about a ten-thousandth."""
    browser, page = (await _open())
    try:
        _, window = (await _render(page, _spectrum(frequency=700,
                                            method="getFloatTimeDomainData")))
    finally:
        (await browser.close())

    wanted = [-0.587908, -0.923937, -0.987664, -0.760306, -0.308872, 0.233593, 0.707214,
              0.972405, 0.951008, 0.649331, 0.156284, -0.382825, -0.809106, -0.996928,
              -0.890936, -0.522368, 0.000153, 0.522628, 0.891075, 0.996904, 0.808927,
              0.382542, -0.156585, -0.649564, -0.951103, -0.972334, -0.706999, -0.233296,
              0.309162, 0.760505, 0.987712, 0.923821]
    assert len(window) == len(wanted)
    for index, (measured, target) in enumerate(zip(window, wanted)):
        assert abs(measured - target) < 1e-3, (index, measured, target)


async def test_a_shorter_array_takes_the_start_and_a_longer_keeps_its_tail():
    browser, page = (await _open())
    try:
        short, tail = (await _render(page, _spectrum(method="getFloatTimeDomainData") + """
            const short = new Float32Array(4);
            analyser.getFloatTimeDomainData(short);
            const long = new Float32Array(40);
            long.fill(9);
            analyser.getFloatTimeDomainData(long);
            window.__rendered = [Array.from(short), Array.from(long).slice(32)];
        """))
    finally:
        (await browser.close())

    assert [round(value, 6) for value in short] == [0.0, 0.707107, 1.0, 0.707107], short
    assert tail == [9] * 8


async def test_the_argument_has_to_be_a_float32_array():
    browser, page = (await _open())
    try:
        names = (await _render(page, f"""
            const context = new OfflineAudioContext(1, 128, {RATE});
            const analyser = context.createAnalyser();
            const refused = (call, value) => {{
                try {{ analyser[call](value); return 'allowed'; }}
                catch (error) {{ return error.name; }}
            }};
            const calls = ['getFloatFrequencyData', 'getFloatTimeDomainData'];
            const names = [];
            for (const call of calls) {{
                names.push(refused(call, [1, 2, 3]), refused(call, undefined),
                           refused(call, new Float64Array(4)),
                           refused(call, new Uint32Array(4)),
                           refused(call, new Float32Array(0)));
            }}
            window.__rendered = names;
        """))
    finally:
        (await browser.close())

    assert names == ["TypeError"] * 4 + ["allowed"] + ["TypeError"] * 4 + ["allowed"], names
