"""The packaged Rust generator reproduces the reference wave bands."""

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c._native import audio as native_audio
from pyv8c.web.bom import audio
from pyv8c.web.bom.audio import OscillatorNode, _OSCILLATOR_X86, _constant_oscillator_batch


@pytest.mark.parametrize('name,minimum,maximum', [
    ('threshold', -100, 0), ('knee', 0, 40), ('ratio', 1, 20),
    ('attack', 0, 1), ('release', 0, 1),
])
def test_compressor_parameter_value_clamps_like_chrome(name, minimum, maximum):
    parameter = getattr(audio.DynamicsCompressorNode(), 'get_' + name)()
    parameter.set_value(-1000)
    assert parameter.get_value() == minimum
    parameter.set_value(1000)
    assert parameter.get_value() == maximum
    assert parameter.get_automationRate() == 'k-rate'


def test_overflowing_audio_preserves_infinite_samples_like_chrome():
    import math
    context = audio.OfflineAudioContext(1, 1000, 44100)
    oscillator = context.fn_createOscillator()
    oscillator.get_frequency().set_value(1000)
    oscillator.fn_start()
    compressor = context.fn_createDynamicsCompressor()
    compressor.get_threshold().set_value(-50)
    for _ in range(2):
        gain = context.fn_createGain()
        gain.get_gain().set_value(3e38)
        oscillator.fn_connect(gain).fn_connect(compressor)
    compressor.fn_connect(context.get_destination())
    samples = context._render()
    assert sum(math.isinf(value) for value in samples) == 454
    assert not any(math.isnan(value) for value in samples)


def test_generated_tables_match_reference_bands():
    expected = (
        (0.0015339851, 0.1875621080, 1.0),
        (1.0, 0.8380299211, 0.9359567761),
        (0.0004143780, 0.0518184900, 0.4681723714),
        (0.0009767559, 0.1201339364, 0.9332402349),
    )
    for kind, reference in enumerate(expected):
        actual = [native_audio.oscillator_waveform(kind, index / 4096, band, band, 0)
                  for band, index in ((0, 1), (10, 123), (25, 1024))]
        assert actual == pytest.approx(reference, abs=2e-6)


@pytest.mark.skipif(not _OSCILLATOR_X86, reason='batch follows the x86 oscillator path')
@pytest.mark.parametrize('kind', ['sine', 'square', 'sawtooth', 'triangle'])
@pytest.mark.parametrize('rate,frequency,length', [(44100, 440, 129), (48000, 5000, 257),
                                                    (44100, 10000, 5000)])
def test_oscillator_batch_matches_every_sample_and_final_state(kind, rate, frequency, length):
    class Context:
        def get_sampleRate(self): return rate
    reference = OscillatorNode(context=Context())
    reference.set_type(kind)
    reference.get_frequency().set_value(frequency)
    reference.fn_start()
    batch = _constant_oscillator_batch(reference, length, rate)
    assert batch is not None
    values, phase, lanes, lane, increment = batch
    expected = [reference._compute(i / rate) for i in range(length)]
    assert list(values) == expected
    assert phase == reference._phase
    assert lanes == reference._vector_lanes
    if lanes is not None:
        assert (lane, increment) == (reference._vector_lane, reference._vector_increment)


def test_scheduled_frequency_does_not_use_constant_batch():
    node = OscillatorNode()
    node.fn_start()
    node.get_frequency().fn_linearRampToValueAtTime(1000, 1)
    assert _constant_oscillator_batch(node, 5000, 44100) is None


@pytest.mark.parametrize('increment', [-12.25, -0.5, 0.0, 0.5, 12.25])
def test_scalar_phase_wraps_for_positive_and_negative_frequency(increment):
    phase = 0.0
    expected = 0.0
    for frame in range(260):
        read, phase, lanes, lane, previous = native_audio.oscillator_phase_step(
            phase, None, 0, None, increment, frame, False)
        assert read == expected
        expected = (expected + increment / 4096) % 1.0
        assert phase == expected
        assert lanes is None and lane == 0 and previous is None


@pytest.mark.skipif(not _OSCILLATOR_X86, reason='batch follows the x86 oscillator path')
def test_offline_graph_batch_matches_general_renderer(monkeypatch):
    def render():
        context = audio.OfflineAudioContext(1, 5000, 44100)
        oscillator = context.fn_createOscillator()
        oscillator.set_type('triangle')
        oscillator.get_frequency().set_value(10000)
        oscillator.fn_start()
        gain = context.fn_createGain()
        gain.get_gain().fn_linearRampToValueAtTime(0.5, 0.1)
        compressor = context.fn_createDynamicsCompressor()
        compressor.get_threshold().set_value(-50)
        oscillator.fn_connect(gain).fn_connect(compressor).fn_connect(context.get_destination())
        samples = context._render()
        return samples, compressor.get_reduction(), oscillator._phase, oscillator._vector_lanes

    accelerated = render()
    monkeypatch.setattr(audio, '_constant_oscillator_batch', lambda *_: None)
    assert accelerated == render()


@pytest.mark.skipif(not _OSCILLATOR_X86, reason='batch follows the x86 oscillator path')
@pytest.mark.parametrize('length', [1, 32, 33, 5000])
def test_direct_compressor_batch_matches_single_samples_with_automation(monkeypatch, length):
    def render():
        context = audio.OfflineAudioContext(1, length, 44100)
        oscillator = context.fn_createOscillator()
        oscillator.set_type('triangle')
        oscillator.get_frequency().set_value(10000)
        oscillator.fn_start()
        compressor = context.fn_createDynamicsCompressor()
        compressor.get_threshold().fn_setValueAtTime(-50, 0)
        compressor.get_threshold().fn_linearRampToValueAtTime(-30, .08)
        compressor.get_ratio().fn_setValueAtTime(6, .04)
        oscillator.fn_connect(compressor).fn_connect(context.get_destination())
        samples = context._render()
        return samples, compressor.get_reduction(), compressor._compressor.frame, oscillator._phase

    accelerated = render()
    monkeypatch.setattr(audio, '_constant_oscillator_batch', lambda *_: None)
    assert accelerated == render()
