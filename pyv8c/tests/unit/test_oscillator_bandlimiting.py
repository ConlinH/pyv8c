"""Oscillator samples measured from Chrome for adjacent frequency bands."""
import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c.web.bom.audio import _float32, _waveform, _WAVE_POINTS


@pytest.mark.parametrize('rate,frequency,kind,expected', [
    (44100, 440, 'triangle', [0.0397556014, 0.6790415645, 0.5096238256, 0.1515062749, -0.0818878561]),
    (48000, 600, 'triangle', [0.0498048030, 0.8514521718, 0.3505114615, 0.6494607925, 0.9874345660]),
    (44100, 440, 'square', [0.9559808373, 0.8462647200, 0.8322569132, 0.8998495340, -0.8305870295]),
    (44100, 440, 'sawtooth', [0.0129401926, 0.2931923568, 0.2100578398, 0.8351068497, -0.0406240486]),
])
def test_blended_wave_bands_match_chrome_samples(rate, frequency, kind, expected):
    increment = _float32(frequency * _float32(_WAVE_POINTS / rate, 'rate'), 'increment')
    phase = 0.0
    samples = []
    for i in range(901):
        value = _waveform(kind, phase, frequency, rate)
        if i in (1, 17, 113, 347, 900):
            samples.append(value)
        phase = (phase + increment / _WAVE_POINTS) % 1.0
    assert samples == pytest.approx(expected, abs=5e-5)
