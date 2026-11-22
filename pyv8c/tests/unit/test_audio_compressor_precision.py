"""Fixed compressor vectors measured from Chrome with identical Float32 input."""
import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c._native import audio  # noqa: E402


@pytest.mark.parametrize('rate,expected', [
    (44100, [0, 0, -0.3735393285751343, 0.19949735701084137,
             0.22444836795330048, 0.026051880791783333,
             0.1324615180492401, 0.21167856454849243]),
    (48000, [0, 0, 0, 0, -0.38933372497558594, 0.2126673012971878,
             0.31698259711265564, 0.39751070737838745]),
])
def test_compressor_matches_chrome_for_fixed_signal(rate, expected):
    compressor = audio.CompressorDSP(rate)
    indices = {0, 263, 264, 287, 288, 1024, 4500, 4999}
    actual = []
    for i in range(5000):
        output = compressor.process(((i % 31) - 15) / 15, (-50., 40., 12., 0., .25))
        if i in indices:
            actual.append(output)
    assert actual == pytest.approx(expected, abs=2e-6)


def test_blocks_and_single_samples_share_state_across_division_boundaries():
    values = [((i % 31) - 15) / 15 for i in range(850)]
    params = [(-50 + i * .001, 20 + i * .002, 4 + i * .001, .003, .25)
              for i in range(len(values))]
    single, blocks = audio.CompressorDSP(44100), audio.CompressorDSP(44100)
    expected = [single.process(value, p) for value, p in zip(values, params)]
    actual = []
    start = 0
    for length in (1, 17, 46, 127, 511, 148):
        end = start + length
        actual.extend(blocks.process_block(values[start:end],
                                          [params[i] for i in range(start, end) if i % 32 == 0]))
        start = end
    assert actual == expected
    assert blocks.reduction == single.reduction
    assert blocks.frame == single.frame == len(values)


def test_invalid_block_parameters_do_not_change_compressor_state():
    compressor = audio.CompressorDSP(44100)
    with pytest.raises(ValueError):
        compressor.process_block([0.5] * 33, [(-50, 40, 12, 0, .25)])
    assert compressor.frame == 0


@pytest.mark.parametrize('out_of_range,clamped', [
    ((-1000, 40, 12, 0, .25), (-100, 40, 12, 0, .25)),
    ((1000, -10, 0, -1, 4), (0, 0, 1, 0, 1)),
    ((-50, 1000, 1000, 2, -3), (-50, 40, 20, 1, 0)),
])
def test_compressor_clamps_out_of_range_parameters(out_of_range, clamped):
    actual, expected = audio.CompressorDSP(44100), audio.CompressorDSP(44100)
    for i in range(600):
        value = ((i % 31) - 15) / 15
        assert actual.process(value, out_of_range) == expected.process(value, clamped)
    assert actual.reduction == expected.reduction


def test_compressor_clamps_automated_values_after_interpolation():
    actual, expected = audio.CompressorDSP(44100), audio.CompressorDSP(44100)
    for i in range(1000):
        threshold = -50 - 950 * i / 4410
        value = ((i % 31) - 15) / 15
        assert actual.process(value, (threshold, 30, 12, .003, .25)) == expected.process(
            value, (max(-100, threshold), 30, 12, .003, .25))
