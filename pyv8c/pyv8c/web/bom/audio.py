"""``AudioContext``: the node graph a page builds, and the samples it produces.

An ``OfflineAudioContext`` renders that graph in Python; a live one only reports the
shape of it, since there is no device to play it on.
"""

import array
from collections import deque
from functools import lru_cache
import math
import platform
import sys

from pyv8c import pyv8
from pyv8c.pyv8 import AttributeSpec

from ...features.runtime import FeatureImplementation
from ...idl import interface
from ...foundation.arguments import (
    MISSING,
    boolean,
    dictionary,
    dom_string,
    integer,
    is_nullish,
    missing_error,
    sequence,
)
from ...foundation.clock import clock_of
from ...foundation.tasks import TaskSource
from ...foundation.arguments import IdlArgumentError
from ..page_errors import PageTypeError, dom_exception
from ..page_errors import js_error_value
from ..dom.events import Event, EventTarget


#: What a desktop context reports for its sample rate.
SAMPLE_RATE = 48000


#: The quantum a context renders in, which Chrome reports as a member.
RENDER_QUANTUM = 128


#: ``AudioParam`` bounds: the largest finite float, what Chrome reports for a gain.
_PARAM_MAX = 3.4028234663852886e38


def _attribute(name, getter=None, setter=None, side_effect=1):
    return AttributeSpec(name=name, getter=getter or "get_" + name, setter=setter,
                         side_effect=side_effect)


def _double(value, what):
    if isinstance(value, bool) or is_nullish(value):
        raise PageTypeError(f"The provided value for {what} is not of type 'double'.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PageTypeError(f"The provided value for {what} is not of type 'double'.") from exc


def _float32(value, what):
    """Convert a Web IDL float to the finite IEEE-754 binary32 value."""
    value = _double(value, what)
    if not math.isfinite(value):
        raise PageTypeError(f"The provided value for {what} is not a finite float.")
    try:
        result = array.array('f', [value])[0]
    except OverflowError as exc:
        raise PageTypeError(f"The provided value for {what} is outside the float range.") from exc
    if not math.isfinite(result):
        raise PageTypeError(f"The provided value for {what} is outside the float range.")
    return result


@interface('AudioParam', None)
class AudioParam:
    """One automatable number, and the points scheduled on it."""

        # Absent: ``fn_<name>`` binds them, and the bodies below answer.

    def __init__(self, value=1.0, default=None, minimum=-_PARAM_MAX, maximum=_PARAM_MAX, **_):
        self._value = _float32(value, "AudioParam.value")
        self._default = _float32(value if default is None else default, "AudioParam.defaultValue")
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._automation_rate = "a-rate"
        self._fixed_automation_rate = None
        #: ``(time, value, kind, time constant)``, in time order.
        self._events = []

    def get_value(self): return self._value
    def set_value(self, value):
        self._value = min(self._maximum, max(self._minimum, _float32(value, "AudioParam.value")))
    def get_defaultValue(self): return self._default
    def get_minValue(self): return self._minimum
    def get_maxValue(self): return self._maximum
    def get_automationRate(self): return self._automation_rate
    def set_automationRate(self, value):
        value = dom_string(value)
        if value not in ("a-rate", "k-rate"):
            raise PageTypeError(f"The provided value '{value}' is not a valid enum value.")
        if self._fixed_automation_rate is not None:
            raise dom_exception('InvalidStateError',
                                "Failed to set the 'automationRate' property on 'AudioParam': "
                                f'{self._fixed_automation_rate}.automationRate is fixed and cannot '
                                f'be changed to "{value}"')
        self._automation_rate = value

    def value_at(self, seconds):
        """What the parameter holds at *seconds*: the last point, or the ramp reaching it.

        A value assigned to the member stands before the first scheduled point.
        """
        previous = (0.0, self._value, "value", 0.0)
        for event in self._events:
            if event[0] > seconds:
                if event[2] == "linear":
                    return _ramp(previous, event, seconds)
                if event[2] == "exponential":
                    return _exponential(previous, event, seconds)
                return previous[1]
            if event[2] == "target":
                return _target(event, previous[1], seconds)
            previous = event
        return previous[1]

    def _point(self, name, count, value, time):
        if time is MISSING:
            raise missing_error('AudioParam', name, count, 0, constructor=False)
        return _double(time, f"AudioParam.{name}"), _float32(value, f"AudioParam.{name}")

    def _schedule(self, time, value, kind, constant=0.0):
        self._events.append((time, value, kind, constant))
        self._events.sort(key=lambda event: event[0])
        return self

    def fn_setValueAtTime(self, value=MISSING, start=MISSING, *_extra):
        time, value = self._point('setValueAtTime', 2, value, start)
        return self._schedule(time, value, "value")

    def fn_linearRampToValueAtTime(self, value=MISSING, end=MISSING, *_extra):
        time, value = self._point('linearRampToValueAtTime', 2, value, end)
        return self._schedule(time, value, "linear")

    def fn_exponentialRampToValueAtTime(self, value=MISSING, end=MISSING, *_extra):
        time, value = self._point('exponentialRampToValueAtTime', 2, value, end)
        return self._schedule(time, value, "exponential")

    def fn_setTargetAtTime(self, target=MISSING, start=MISSING, constant=MISSING, *_extra):
        time, target = self._point('setTargetAtTime', 3, target, start)
        if constant is MISSING:
            raise missing_error('AudioParam', 'setTargetAtTime', 3, 2, constructor=False)
        return self._schedule(time, target, "target",
                              _double(constant, "AudioParam.setTargetAtTime"))

    def fn_cancelScheduledValues(self, start=MISSING, *_extra):
        if start is MISSING:
            raise missing_error('AudioParam', 'cancelScheduledValues', 1, 0, constructor=False)
        time = _double(start, "AudioParam.cancelScheduledValues")
        self._events = [event for event in self._events if event[0] < time]
        return self

    def fn_cancelAndHoldAtTime(self, start=MISSING, *_extra):
        if start is MISSING:
            raise missing_error('AudioParam', 'cancelAndHoldAtTime', 1, 0, constructor=False)
        time = _double(start, "AudioParam.cancelAndHoldAtTime")
        held = self.value_at(time)
        self._events = [event for event in self._events if event[0] < time]
        return self._schedule(time, held, "value")

    def _automation(self, *_extra):
        """A call accepted without an effect on the samples."""
        return self


#: ``setValueCurveAtTime`` takes a curve this environment does not integrate.
setattr(AudioParam, "fn_setValueCurveAtTime", AudioParam._automation)


def _ramp(start, end, seconds):
    """Linear interpolation between two scheduled points."""
    span = end[0] - start[0]
    if span <= 0:
        return end[1]
    return start[1] + (end[1] - start[1]) * (seconds - start[0]) / span


def _exponential(start, end, seconds):
    """The same, exponentially: both ends have to be non-zero and share a sign."""
    span = end[0] - start[0]
    if span <= 0 or start[1] == 0 or start[1] * end[1] < 0:
        return start[1]
    return start[1] * (end[1] / start[1]) ** ((seconds - start[0]) / span)


def _target(event, start, seconds):
    """An exponential approach to a target, which only a later point interrupts."""
    time, target, _kind, constant = event
    if constant <= 0:
        return target
    return target + (start - target) * math.exp(-(seconds - time) / constant)


#: How many points one period of a waveform is sampled into.
_WAVE_POINTS = 4096

# Blink uses four float lanes for ordinary k-rate oscillators on x86.
_OSCILLATOR_X86 = platform.machine().lower() in ('amd64', 'x86_64', 'x86', 'i386', 'i686')


#: The tables built so far, keyed by the waveform and the harmonics it may carry.
_WAVE_TABLES = {}


@lru_cache(maxsize=1024)
def _chrome_wave_bands(frequency, rate):
    f = lambda x: _float32(x, 'oscillator band')
    ratio = f(frequency / f(rate / _WAVE_POINTS))
    cents = f(f(math.log2(ratio)) * 1200.)
    pitch = min(35., max(0., f(1. + f(cents / 400.))))
    index = int(pitch)
    return index, min(index + 1, 35), f(pitch - index)


def _chrome_waveform(kind, phase, frequency, rate):
    """Read FFT bands using the same interpolation as the batch renderer."""
    from ..._native import audio as native_audio
    high, low, mix = _chrome_wave_bands(frequency, rate)
    return native_audio.oscillator_waveform(
        ('sine', 'square', 'sawtooth', 'triangle').index(kind), phase, high, low, mix)


def _coefficients(kind, limit):
    """The Fourier coefficients the specification gives a waveform, up to *limit*."""
    for harmonic in range(1, limit + 1):
        if kind == "sine":
            yield harmonic, 1.0 if harmonic == 1 else 0.0
        elif kind == "square":
            yield harmonic, 4.0 / (math.pi * harmonic) if harmonic % 2 else 0.0
        elif kind == "sawtooth":
            yield harmonic, 2.0 / (math.pi * harmonic) * (-1) ** (harmonic + 1)
        else:
            yield harmonic, (8.0 / (math.pi ** 2 * harmonic ** 2)
                             * (-1) ** ((harmonic - 1) // 2) if harmonic % 2 else 0.0)


#: The scale each waveform keeps when its harmonics are cut for a frequency: the inverse
#: of the peak its full series reaches, from the 4096-point search this used to run once
#: per kind (about half a second for one, on every process that rendered a tone).
_WAVE_SCALES = {
    "sine": 1.0,
    "square": 0.8481909356632211,
    "sawtooth": 0.8485424501794038,
    "triangle": 1.000197932090434,
}


def _wave_samples(kind, limit):
    """One period of the waveform, from the harmonics up to *limit*."""
    coefficients = [entry for entry in _coefficients(kind, limit) if entry[1]]
    table = []
    for point in range(_WAVE_POINTS):
        angle = 2.0 * math.pi * point / _WAVE_POINTS
        table.append(sum(value * math.sin(harmonic * angle)
                         for harmonic, value in coefficients))
    return table


def _wave_scale(kind):
    """One scale per waveform, as Chrome takes it: the inverse of the full series' peak."""
    scale = _WAVE_SCALES.get(kind)
    if scale is None:
        # A waveform the table does not carry: search for its peak the way the table did.
        peak = max(abs(value) for value in _wave_samples(kind, _WAVE_POINTS // 2)) or 1.0
        scale = 1.0 / peak
    return scale


def _build_wave(kind, limit):
    """One period, band-limited to *limit* harmonics and scaled as Chrome scales it.

    A table cut down to a single harmonic keeps that harmonic's own amplitude, which
    is why a 10 kHz triangle reads 8/pi^2 rather than a normalised 1.
    """
    scale = _wave_scale(kind)
    return [value * scale for value in _wave_samples(kind, limit)]


@lru_cache(maxsize=1024)
def _wave_bands(frequency, rate):
    """Adjacent octave-third tables and the mix for a 4096-point wave."""
    ratio = frequency / (rate / _WAVE_POINTS)
    last = 3 * int(math.log2(_WAVE_POINTS)) - 1
    pitch = max(0.0, min(float(last), 1.0 + 3.0 * math.log2(ratio)))
    index = int(pitch)
    def partials(band):
        return int((_WAVE_POINTS / 2) * 2.0 ** (-band / 3.0))
    return partials(index), partials(min(index + 1, last)), pitch - index


def _wave_table(kind, partials):
    key = (kind, partials)
    table = _WAVE_TABLES.get(key)
    if table is None:
        table = _build_wave(kind, partials)
        _WAVE_TABLES[key] = table
    return table


def _waveform(kind, phase, frequency, rate):
    """One sample of the waveform, band-limited to the harmonics *frequency* can carry."""
    if kind == "custom":
        return 0.0
    limit = int(rate / 2 / frequency) if frequency > 0 else 0
    if limit < 1:
        return 0.0
    if (_OSCILLATOR_X86 and 24000 < rate <= 88200
            and frequency * (_WAVE_POINTS / rate) >= .3 and frequency <= rate / 2):
        return _chrome_waveform(kind, phase, frequency, rate)
    # Chromium creates three band-limited tables per octave and blends the
    # adjacent tables. A hard Nyquist cutoff changes low-frequency waveforms.
    if 24000 < rate <= 88200 and frequency <= rate / 2:
        high, low, mix = _wave_bands(frequency, rate)
    else:
        high, low, mix = limit, limit, 0.0
    table = _wave_table(kind, high)
    position = phase * _WAVE_POINTS
    index = int(position)
    following = (index + 1) % _WAVE_POINTS
    fraction = position - index
    sample = table[index] + (table[following] - table[index]) * fraction
    if mix and high != low:
        lower = _wave_table(kind, low)
        other = lower[index] + (lower[following] - lower[index]) * fraction
        sample += (other - sample) * mix
    return sample


@interface('AudioNode', 'EventTarget')
class AudioNode(EventTarget):
    """A node in the graph."""

    #: How many inputs and outputs a node of this kind has.
    inputs, outputs = 1, 1

    def __init__(self, *_extra, context=None, **_):
        super().__init__(*_extra, **_)
        self._context = context
        self._channel_count = 2
        self._channel_count_mode = "max"
        self._channel_interpretation = "speakers"
        self._inputs = []
        self._outputs = []
        self._out = 0.0

    def get_context(self): return self._context
    def get_numberOfInputs(self): return self.inputs
    def get_numberOfOutputs(self): return self.outputs
    def get_channelCount(self): return self._channel_count
    def set_channelCount(self, value):
        count = integer(value)
        if count < 1 or count > 32:
            raise dom_exception("NotSupportedError",
                                "The channel count provided is outside the supported range.")
        self._channel_count = count
    def get_channelCountMode(self): return self._channel_count_mode
    def set_channelCountMode(self, value):
        value = dom_string(value)
        if value not in ("max", "clamped-max", "explicit"):
            raise PageTypeError(f"The provided value '{value}' is not a valid enum value.")
        self._channel_count_mode = value
    def get_channelInterpretation(self): return self._channel_interpretation
    def set_channelInterpretation(self, value):
        value = dom_string(value)
        if value not in ("speakers", "discrete"):
            raise PageTypeError(f"The provided value '{value}' is not a valid enum value.")
        self._channel_interpretation = value

    def fn_connect(self, destination=MISSING, *_extra):
        if destination is MISSING:
            raise missing_error('AudioNode', 'connect', 1, 0, constructor=False)
        if not isinstance(destination, AudioNode):
            raise PageTypeError("Failed to execute 'connect' on 'AudioNode': parameter 1 is not"
                              " of type 'AudioNode'.")
        self._outputs.append(destination)
        destination._inputs.append(self)
        # Chrome answers with the destination it was handed, so a chain keeps working.
        return destination

    def fn_disconnect(self, *_extra):
        for destination in self._outputs:
            if self in destination._inputs:
                destination._inputs.remove(self)
        del self._outputs[:]
        return None

    def _compute(self, seconds):
        """One sample: a plain node passes its inputs through, summed."""
        return sum(source._out for source in self._inputs)


@interface('AudioScheduledSourceNode', 'AudioNode')
class AudioScheduledSourceNode(AudioNode):
    """A source that runs between ``start`` and ``stop``."""

    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._start = None
        self._stop = math.inf
        self._ended_reported = False

    def get_onended(self): return self._get_handler("onended")
    def set_onended(self, value): self._set_handler("onended", value)

    def fn_start(self, when=0.0, *_extra):
        if self._start is not None:
            raise dom_exception("InvalidStateError", "cannot call start more than once.")
        self._start = _double(when, "AudioScheduledSourceNode.start")
        return None

    def fn_stop(self, when=0.0, *_extra):
        self._stop = _double(when, "AudioScheduledSourceNode.stop")
        return None

    def _sounding(self, seconds):
        """Whether the source is between the ``start`` and ``stop`` it was given."""
        return self._start is not None and self._start <= seconds < self._stop

    def _notify_finished(self):
        """Report the stop a source reaches, once."""
        if self._ended_reported or self._start is None:
            return
        self._ended_reported = True
        self._dispatch_event(Event("ended", {"isTrusted": True}))


@interface('OscillatorNode', 'AudioScheduledSourceNode')
class OscillatorNode(AudioScheduledSourceNode):
    """A tone: the type a page sets and the frequency it automates."""

    #: What ``type`` accepts, in Chrome's spelling.
    TYPES = ("sine", "square", "sawtooth", "triangle", "custom")

    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._type = "sine"
        self._frequency = AudioParam(440.0)
        self._detune = AudioParam(0.0)
        self._phase = 0.0
        self._periodic = None
        self._vector_lanes = None
        self._vector_lane = 0
        self._vector_increment = None

    def get_type(self): return self._type
    def set_type(self, value):
        value = dom_string(value)
        if value not in self.TYPES:
            raise PageTypeError(f"The provided value '{value}' is not a valid enum value.")
        self._type = value
    def get_frequency(self): return self._frequency
    def get_detune(self): return self._detune

    def fn_setPeriodicWave(self, wave=MISSING, *_extra):
        if wave is MISSING:
            raise missing_error('OscillatorNode', 'setPeriodicWave', 1, 0, constructor=False)
        if not isinstance(wave, PeriodicWave):
            raise PageTypeError("Failed to execute 'setPeriodicWave' on 'OscillatorNode':"
                              " parameter 1 is not of type 'PeriodicWave'.")
        self._periodic = wave
        self._type = "custom"
        return None

    def _compute(self, seconds):
        """One sample, at the frequency the parameter holds just then."""
        if not self._sounding(seconds):
            return 0.0
        rate = self._context.get_sampleRate() if self._context is not None else SAMPLE_RATE
        detune = self._detune.value_at(seconds)
        frequency = self._frequency.value_at(seconds) * 2.0 ** (detune / 1200.0)
        rate_scale = _float32(_WAVE_POINTS / rate, "OscillatorNode rate scale")
        increment = _float32(frequency * rate_scale, "OscillatorNode phase increment")
        # Chromium's x86 k-rate path advances four float indices within each
        # render quantum, while preserving a double-precision phase across it.
        # Keep changing automation and nonzero-start oscillators on the general
        # path. A setValueAtTime at zero is constant for the whole render.
        frequency_constant = all(event[0] <= 0 and event[2] == 'value'
                                 for event in self._frequency._events)
        detune_constant = all(event[0] <= 0 and event[2] == 'value'
                              for event in self._detune._events)
        frame = round(seconds * rate)
        vector = (_OSCILLATOR_X86 and self._start == 0 and self._type != 'custom'
                  and frequency_constant and detune_constant
                  and frame >= RENDER_QUANTUM and 24000 < rate <= 88200 and increment >= .3)
        from ..._native import audio as native_audio
        phase, next_phase, lanes, lane, previous_increment = native_audio.oscillator_phase_step(
            self._phase, self._vector_lanes, self._vector_lane,
            self._vector_increment, increment, frame, vector)
        if self._type == "custom" and self._periodic is not None:
            sample = self._periodic.sample(phase, frequency, rate)
        else:
            sample = _waveform("sine" if self._type == "custom" else self._type,
                               phase, frequency, rate)
        # Blink computes the wavetable rate scale and increment as floats,
        # then accumulates the read index in double precision.
        self._phase = next_phase
        self._vector_lanes = list(lanes) if lanes is not None else None
        self._vector_lane = lane
        self._vector_increment = previous_increment
        return sample


def _constant_oscillator_batch(node, length, rate):
    """Render a supported constant oscillator ahead of the sample loop."""
    if (not _OSCILLATOR_X86 or not length or not 24000 < rate <= 88200
            or node._start != 0 or node._stop < length / rate or node._phase != 0
            or node._vector_lanes is not None or node._type == 'custom'):
        return None
    params = (node._frequency, node._detune)
    if any(any(time > 0 or kind != 'value' for time, _value, kind, _constant in p._events)
           for p in params):
        return None
    try:
        frequency = node._frequency.value_at(0) * 2.0 ** (node._detune.value_at(0) / 1200.0)
        if not math.isfinite(frequency) or not 0 < frequency <= rate / 2:
            return None
        increment = _float32(frequency * _float32(_WAVE_POINTS / rate, 'rate'), 'increment')
        if increment < .3 or frequency * (_WAVE_POINTS / rate) < .3:
            return None
        high, low, mix = _chrome_wave_bands(frequency, rate)
    except (OverflowError, ValueError):
        return None
    from ..._native import audio as native_audio
    payload, phase, lanes, lane = native_audio.oscillator_block(
        ('sine', 'square', 'sawtooth', 'triangle').index(node._type),
        length, increment, high, low, mix)
    values = array.array('f')
    values.frombytes(payload)
    if sys.byteorder != 'little':
        values.byteswap()
    return values, phase, lanes, lane, increment


@interface('AnalyserNode', 'AudioNode')
class AnalyserNode(AudioNode):
    """What a page reads the spectrum through."""

    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._fft_size = 2048
        self._min_decibels = -100.0
        self._max_decibels = -30.0
        self._smoothing = 0.8
        self._samples = [0.0] * self._fft_size
        self._position = 0
        self._window = None
        self._estimate = [0.0] * (self._fft_size // 2)
        self._decibels = [-math.inf] * (self._fft_size // 2)

    def get_fftSize(self): return self._fft_size
    def set_fftSize(self, value):
        size = integer(value)
        # The specification's window, and a power of two inside it.
        if size < 32 or size > 32768 or size & (size - 1):
            raise dom_exception(
                "IndexSizeError",
                "Failed to set the 'fftSize' property on 'AnalyserNode': The value provided"
                " is not a power of two.")
        self._fft_size = size
        self._samples = [0.0] * size
        self._position = 0
        self._window = None
        self._estimate = [0.0] * (size // 2)
        self._decibels = [-math.inf] * (size // 2)
    def get_frequencyBinCount(self): return self._fft_size // 2
    def get_minDecibels(self): return self._min_decibels
    def set_minDecibels(self, value): self._min_decibels = _double(value, "AnalyserNode.minDecibels")
    def get_maxDecibels(self): return self._max_decibels
    def set_maxDecibels(self, value): self._max_decibels = _double(value, "AnalyserNode.maxDecibels")
    def get_smoothingTimeConstant(self): return self._smoothing
    def set_smoothingTimeConstant(self, value):
        self._smoothing = min(max(_double(value, "AnalyserNode.smoothingTimeConstant"), 0.0), 1.0)

    def _compute(self, seconds):
        value = super()._compute(seconds)
        self._samples[self._position] = value
        self._position = (self._position + 1) % self._fft_size
        return value

    def _render_finished(self, rate, processed):
        """One analysis for the render that just ended, as Chrome takes it."""
        size = self._fft_size
        if self._window is None:
            self._window = [_blackman(index, size) for index in range(size)]
        windowed = [complex(self._samples[(self._position + index) % size] * self._window[index])
                    for index in range(size)]
        spectrum = _fft(windowed)
        for index in range(size // 2):
            magnitude = abs(spectrum[index]) / size
            self._estimate[index] = (self._smoothing * self._estimate[index]
                                     + (1.0 - self._smoothing) * magnitude)
            self._decibels[index] = (20.0 * math.log10(self._estimate[index])
                                     if self._estimate[index] else -math.inf)

    def fn_getFloatFrequencyData(self, array=MISSING, *_extra):
        if array is MISSING:
            raise missing_error('AnalyserNode', 'getFloatFrequencyData', 1, 0, constructor=False)
        array = _float32_view(array, 'getFloatFrequencyData')
        # A longer array keeps whatever it held past the bin count; a shorter one is filled.
        for index in range(min(_view_length(array, 'getFloatFrequencyData'),
                               len(self._decibels))):
            array[index] = self._decibels[index]
        return None

    def fn_getFloatTimeDomainData(self, array=MISSING, *_extra):
        if array is MISSING:
            raise missing_error('AnalyserNode', 'getFloatTimeDomainData', 1, 0, constructor=False)
        array = _float32_view(array, 'getFloatTimeDomainData')
        # The window holds the fftSize most recent samples, oldest first; a shorter array
        # takes its beginning and a longer one keeps its tail.
        for index in range(min(_view_length(array, 'getFloatTimeDomainData'),
                               self._fft_size)):
            array[index] = self._samples[(self._position + index) % self._fft_size]
        return None


@interface('GainNode', 'AudioNode')
class GainNode(AudioNode):
    """A volume: the one parameter a page ramps."""

    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._gain = AudioParam(1.0, minimum=-_PARAM_MAX)

    def get_gain(self): return self._gain

    def _compute(self, seconds):
        return self._gain.value_at(seconds) * super()._compute(seconds)


@interface('AudioDestinationNode', 'AudioNode')
class AudioDestinationNode(AudioNode):
    """Where a graph ends: the speakers this environment does not have."""

    inputs, outputs = 1, 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._channel_count_mode = "explicit"

    def get_maxChannelCount(self): return 2


@interface('AudioBuffer', None)
class AudioBuffer:
    """A block of samples a graph reads from and writes into."""

    def __init__(self, options=None, *, length=0, sample_rate=SAMPLE_RATE, channels=1,
                 samples=None, **_):
        if not is_nullish(options) and options is not None:
            options = dictionary(options, name="AudioBufferOptions",
                                 prefix="Failed to construct 'AudioBuffer': ")
            length = options.get("length", length)
            sample_rate = options.get("sampleRate", sample_rate)
            channels = options.get("numberOfChannels", channels)
        self._length = max(integer(length), 0)
        self._sample_rate = float(sample_rate) or float(SAMPLE_RATE)
        self._channels = max(integer(channels), 1)
        # One list per channel, or one list shared by every channel.
        if samples is None:
            self._samples = [[0.0] * self._length for _ in range(self._channels)]
        elif samples and isinstance(samples[0], (list, tuple)):
            self._samples = [list(channel) for channel in samples]
        else:
            self._samples = [list(samples) for _ in range(self._channels)]
        self._views = [None] * self._channels

    def get_length(self): return self._length
    def get_sampleRate(self): return self._sample_rate
    def get_numberOfChannels(self): return self._channels
    def get_duration(self):
        return self._length / self._sample_rate if self._sample_rate else 0.0

    def _values(self, channel):
        """The channel's samples, holding whatever a page wrote through the view."""
        view = self._views[channel]
        if view is None:
            return self._samples[channel]
        length = min(int(view.length), self._length)
        return [float(view[position]) for position in range(length)]

    def _channel(self, number, method):
        index = integer(number)
        if index < 0 or index >= self._channels:
            raise dom_exception("IndexSizeError",
                                f"Failed to execute '{method}' on 'AudioBuffer': channel"
                                f" index ({index}) exceeds number of channels"
                                f" ({self._channels})")
        return index

    def fn_getChannelData(self, channel=MISSING, *_extra):
        if channel is MISSING:
            raise missing_error('AudioBuffer', 'getChannelData', 1, 0, constructor=False)
        index = self._channel(channel, 'getChannelData')
        view = self._views[index]
        if view is None:
            # Chrome hands back the same array every time.
            view = pyv8.JSTypeArray(list(self._samples[index]), pyv8.ArrayType.Float32Array)
            self._views[index] = view
        return view

    def fn_copyToChannel(self, source=MISSING, channel=MISSING, start=0, *_extra):
        if channel is MISSING:
            raise missing_error('AudioBuffer', 'copyToChannel', 2, 1, constructor=False)
        index = self._channel(channel, 'copyToChannel')
        offset = max(integer(start), 0)
        if isinstance(source, pyv8.JSTypeArray):
            values = [float(source[position]) for position in range(int(source.length))]
        else:
            values = [float(value) for value in source]
        channel_samples = self._samples[index]
        for position, value in enumerate(values[:self._length - offset]):
            channel_samples[offset + position] = value
        self._views[index] = None
        return None

    def fn_copyFromChannel(self, destination=MISSING, channel=MISSING, start=0, *_extra):
        if channel is MISSING:
            raise missing_error('AudioBuffer', 'copyFromChannel', 2, 1, constructor=False)
        index = self._channel(channel, 'copyFromChannel')
        offset = max(integer(start), 0)
        view = destination if _is_float32_array(destination) else None
        if view is None:
            raise PageTypeError("Failed to execute 'copyFromChannel' on 'AudioBuffer':"
                              " parameter 1 is not of type 'Float32Array'.")
        samples = self._samples[index]
        for position in range(min(self._length - offset, int(view.length))):
            view[position] = samples[offset + position]
        return None


@interface('BaseAudioContext', 'EventTarget')
class BaseAudioContext(EventTarget):
    """A graph's context: its clock, its state and the factories that fill it."""

    def __init__(self, *_extra, state="running", sample_rate=None, services=pyv8.Undefined,
                 _realm=None, **_):
        if services is pyv8.Undefined:
            services = None
        super().__init__(services=services or _realm)
        self._services = services or _realm
        self._clock = clock_of(self._services)
        self._state = state
        self._sample_rate = float(sample_rate or SAMPLE_RATE)
        self._started = self._clock.now_ms()
        self._destination = AudioDestinationNode(context=self)
        self._listener = AudioListener()
        #: Nodes a page reads a result from, which Chrome renders even without a way out.
        self._observers = []
        #: Where an offline context's clock stands; a live one has no rendered position.
        self._rendered = None

    def get_destination(self): return self._destination
    def get_listener(self): return self._listener
    def get_sampleRate(self): return self._sample_rate
    def get_currentTime(self):
        if self._rendered is not None:
            return self._rendered
        return (self._clock.now_ms() - self._started) / 1000.0
    def get_state(self): return self._state
    def get_renderQuantumSize(self): return RENDER_QUANTUM
    def get_onstatechange(self): return self._get_handler("onstatechange")
    def set_onstatechange(self, value): self._set_handler("onstatechange", value)

    def fn_createAnalyser(self, *_extra):
        return self._observe(AnalyserNode(context=self))
    def fn_createBiquadFilter(self, *_extra): return BiquadFilterNode(context=self)
    def fn_createBufferSource(self, *_extra): return AudioBufferSourceNode(context=self)
    def fn_createConstantSource(self, *_extra): return ConstantSourceNode(context=self)
    def fn_createConvolver(self, *_extra): return ConvolverNode(context=self)
    def fn_createDynamicsCompressor(self, *_extra): return DynamicsCompressorNode(context=self)
    def fn_createGain(self, *_extra): return GainNode(context=self)
    def fn_createOscillator(self, *_extra): return OscillatorNode(context=self)
    def fn_createPanner(self, *_extra): return PannerNode(context=self)
    def fn_createStereoPanner(self, *_extra): return StereoPannerNode(context=self)
    def fn_createWaveShaper(self, *_extra): return WaveShaperNode(context=self)

    def fn_createChannelMerger(self, number_of_inputs=6, *_extra):
        return ChannelMergerNode(number_of_inputs, context=self)

    def fn_createChannelSplitter(self, number_of_outputs=6, *_extra):
        return ChannelSplitterNode(number_of_outputs, context=self)

    def fn_createDelay(self, max_delay_time=1.0, *_extra):
        return DelayNode(max_delay_time, context=self)

    def fn_createIIRFilter(self, feedforward=MISSING, feedback=MISSING, *_extra):
        if feedback is MISSING:
            raise missing_error('BaseAudioContext', 'createIIRFilter', 2, 1, constructor=False)
        return IIRFilterNode(feedforward, feedback, context=self)

    def fn_createPeriodicWave(self, real=MISSING, imag=MISSING, *_extra):
        if imag is MISSING:
            raise missing_error('BaseAudioContext', 'createPeriodicWave', 2, 1, constructor=False)
        return PeriodicWave(self, {"real": real, "imag": imag})

    def fn_createScriptProcessor(self, buffer_size=0, input_channels=2, output_channels=2,
                                 *_extra):
        return self._observe(ScriptProcessorNode(buffer_size, input_channels, output_channels,
                                                 context=self))

    def _observe(self, node):
        """Record a node whose result a page reads, so the render reaches it either way."""
        self._observers.append(node)
        return node

    def fn_createBuffer(self, channels=MISSING, length=0, sample_rate=None, *_extra):
        if channels is MISSING:
            raise missing_error('BaseAudioContext', 'createBuffer', 3, 0, constructor=False)
        return AudioBuffer(length=length, sample_rate=sample_rate or SAMPLE_RATE,
                           channels=channels)

    def fn_decodeAudioData(self, data=MISSING, success=None, failure=None, *_extra):
        """The samples a RIFF/WAVE file holds, resampled to this context's rate.

        Every other container answers with the ``EncodingError`` Chrome raises, so a
        caller sees the same rejection rather than a buffer that holds nothing.
        """
        try:
            decoded = _wav_channels(_buffer_bytes(data))
        except (PageTypeError, IdlArgumentError) as error:
            return self._decoded(None, error, success, failure)
        if decoded is None:
            # Nothing throws here: Chrome answers this one as a rejected promise too.
            return self._decoded(None, dom_exception("EncodingError", _DECODE_FAILURE),
                                 success, failure)
        channels, rate = decoded
        samples = [_resampled(channel, rate, self._sample_rate) for channel in channels]
        value = AudioBuffer(sample_rate=self._sample_rate, channels=len(samples),
                            length=len(samples[0]), samples=samples)
        return self._decoded(value, None, success, failure)

    def _decoded(self, value, error, success, failure):
        """The promise Chrome answers with, and the callbacks the older form takes."""
        services = self._services
        if services is None:
            return value if error is None else None
        realm = getattr(services, "realm_services", services)
        handler = failure if error is not None else success
        if callable(handler):
            handler(js_error_value(error) if error is not None else value)
        promise, resolve, reject = realm.make_promise()
        reject(js_error_value(error)) if error is not None else resolve(value)
        return promise

    def _promise(self, value=pyv8.Undefined):
        """An already-resolved promise from this realm, or ``None`` outside one."""
        services = self._services
        if services is None:
            return None
        realm = getattr(services, "realm_services", services)
        promise, resolve, _reject = realm.make_promise()
        resolve(value)
        return promise


@interface('AudioContext', 'BaseAudioContext')
class AudioContext(BaseAudioContext):
    """A live context: its clock runs and its state can be moved."""

    def __init__(self, options=None, *, _realm=None, **kwargs):
        sample_rate = None
        if not is_nullish(options) and options is not None:
            options = dictionary(options, name="AudioContextOptions",
                                 prefix="Failed to construct 'AudioContext': ")
            wanted = options.get("sampleRate", pyv8.Undefined)
            if not is_nullish(wanted):
                sample_rate = _double(wanted, "AudioContextOptions.sampleRate")
        super().__init__(sample_rate=sample_rate, _realm=_realm, **kwargs)

    def get_baseLatency(self): return 0.01
    def get_outputLatency(self): return 0.0
    def get_onerror(self): return self._get_handler("onerror")
    def set_onerror(self, value): self._set_handler("onerror", value)

    def fn_close(self, *_extra):
        self._state = "closed"
        return self._promise()
    def fn_resume(self, *_extra):
        if self._state != "closed":
            self._state = "running"
        return self._promise()
    def fn_suspend(self, *_extra):
        if self._state == "running":
            self._state = "suspended"
        return self._promise()


@interface('OfflineAudioContext', 'BaseAudioContext')
class OfflineAudioContext(BaseAudioContext):
    """A context that renders ahead: it knows how long its output is."""

    def __init__(self, channels_or_options=MISSING, length=0, sample_rate=None, *,
                 _realm=None, **kwargs):
        channels = 1
        if not is_nullish(channels_or_options) and channels_or_options is not MISSING:
            if isinstance(channels_or_options, (int, float)) and not isinstance(channels_or_options, bool):
                channels = integer(channels_or_options)
            else:
                options = dictionary(channels_or_options, name="OfflineAudioContextOptions",
                                     prefix="Failed to construct 'OfflineAudioContext': ")
                channels = options.get("numberOfChannels", channels)
                length = options.get("length", length)
                wanted = options.get("sampleRate", pyv8.Undefined)
                if not is_nullish(wanted):
                    sample_rate = _double(wanted, "OfflineAudioContextOptions.sampleRate")
        super().__init__(sample_rate=sample_rate, state="suspended", _realm=_realm, **kwargs)
        self._channels = max(integer(channels), 1)
        self._length = max(integer(length), 0)
        #: How far rendering has come; an offline clock starts at zero.
        self._rendered = 0.0

    def get_length(self): return self._length
    def get_oncomplete(self): return self._get_handler("oncomplete")
    def set_oncomplete(self, value): self._set_handler("oncomplete", value)

    def _order(self):
        """The graph from the destination backwards, sources first."""
        order, seen = [], set()

        def visit(node):
            if id(node) in seen:
                return
            seen.add(id(node))
            for source in node._inputs:
                visit(source)
            order.append(node)

        for source in self._destination._inputs:
            visit(source)
        for node in self._observers:
            visit(node)
        return order

    def _render(self):
        """The samples the graph produces, one at a time, in the order it is wired in."""
        order = self._order()
        rate = self._sample_rate
        # Batch only nodes that cannot change another node's parameters while
        # rendering. Other graphs keep the general sample-by-sample path.
        batches = {}
        if not self._observers and all(type(node) in (OscillatorNode, GainNode, DynamicsCompressorNode)
                                       for node in order):
            for node in order:
                if type(node) is OscillatorNode:
                    batch = _constant_oscillator_batch(node, self._length, rate)
                    if batch is not None:
                        batches[node] = batch
        # The direct oscillator-compressor chain has no intermediate node whose
        # per-sample state can be observed. Process it in one native block.
        direct = (len(order) == 2 and type(order[0]) is OscillatorNode
                  and type(order[1]) is DynamicsCompressorNode
                  and order[0] in batches and order[1]._inputs == [order[0]]
                  and order[1]._compressor.frame == 0
                  and self._destination._inputs == [order[1]])
        if direct:
            oscillator, compressor = order
            divisions = [compressor._parameters_at(index / rate)
                         for index in range(0, self._length, 32)]
            oscillator_values = batches[oscillator][0]
            samples = compressor._compressor.process_block(oscillator_values, divisions)
            oscillator._out = oscillator_values[-1]
            compressor._out = samples[-1]
            compressor._reduction = compressor._compressor.reduction
        else:
            samples = []
            for index in range(self._length):
                seconds = index / rate
                for node in order:
                    batch = batches.get(node)
                    node._out = batch[0][index] if batch is not None else node._compute(seconds)
                samples.append(sum(source._out for source in self._destination._inputs))
        for node, (_values, phase, lanes, lane, increment) in batches.items():
            node._phase = phase
            node._vector_lanes = lanes
            if lanes is not None:
                node._vector_increment = increment
                node._vector_lane = lane
        processed = math.ceil(self._length / RENDER_QUANTUM) * RENDER_QUANTUM
        for node in order:
            if hasattr(node, "_render_finished"):
                node._render_finished(rate, processed)
        self._rendered = self._length / rate if rate else 0.0
        return samples

    def fn_startRendering(self, *_extra):
        samples = self._render()
        self._state = "closed"
        for node in self._order():
            if getattr(node, "_finished", False):
                node._notify_finished()
        buffer = AudioBuffer(length=self._length, sample_rate=self._sample_rate,
                             channels=self._channels, samples=samples)
        if self._services is None:
            return None
        realm = getattr(self._services, "realm_services", self._services)
        promise, resolve, _reject = realm.make_promise()

        def complete():
            #: Chrome delivers both the legacy event and the promise result, and the
            #: event's buffer is the very one the promise resolves with.  Completion is
            #: a queued task, so a handler installed after this call still replaces the
            #: one that was there when it was made.
            event = OfflineAudioCompletionEvent("complete", {
                "renderedBuffer": buffer, "bubbles": True, "isTrusted": True,
            }, services=self._services)
            self._dispatch_event(event)
            resolve(buffer)

        # Web Audio completion uses the media element task source. Sharing the
        # DOM queue lets unrelated frame/resource work delay a finished render.
        realm.post_task(complete, TaskSource.MEDIA_ELEMENT,
                        label="offline-audio-complete", document_bound=True)
        return promise
    def fn_resume(self, *_extra): return self._promise()
    def fn_suspend(self, *_extra): return self._promise()


@interface('DynamicsCompressorNode', 'AudioNode')
class DynamicsCompressorNode(AudioNode):
    """A compressor: the level it takes off the signal, which ``reduction`` reports."""


    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        # Web Audio's nominal ranges are per-parameter, not the shared float
        # maximum: threshold [-100, 0], knee [0, 40], ratio [1, 20],
        # attack [0, 1], release [0, 1].  Chrome exposes these through
        # AudioParam.minValue / maxValue (phase 9 parity).
        self._threshold = AudioParam(-24.0, minimum=-100.0, maximum=0.0)
        self._knee = AudioParam(30.0, minimum=0.0, maximum=40.0)
        self._ratio = AudioParam(12.0, minimum=1.0, maximum=20.0)
        self._attack = AudioParam(0.003, minimum=0.0, maximum=1.0)
        self._release = AudioParam(0.25, minimum=0.0, maximum=1.0)
        for name in ('threshold', 'knee', 'ratio', 'attack', 'release'):
            parameter = getattr(self, '_' + name)
            parameter._automation_rate = 'k-rate'
            parameter._fixed_automation_rate = 'DynamicsCompressor.' + name
        self._reduction = 0.0
        rate = self._context.get_sampleRate() if self._context is not None else SAMPLE_RATE
        from ..._native import audio as native_audio
        self._compressor = native_audio.CompressorDSP(rate)

    def get_threshold(self): return self._threshold
    def get_knee(self): return self._knee
    def get_ratio(self): return self._ratio
    def get_attack(self): return self._attack
    def get_release(self): return self._release
    def get_reduction(self): return self._reduction

    def _parameters_at(self, seconds):
        rate = self._context.get_sampleRate() if self._context is not None else SAMPLE_RATE
        frame = round(seconds * rate)
        seconds = (frame // RENDER_QUANTUM * RENDER_QUANTUM) / rate
        return tuple(parameter.value_at(seconds) for parameter in
                     (self._threshold, self._knee, self._ratio, self._attack, self._release))

    def _compute(self, seconds):
        value = super()._compute(seconds)
        output = self._compressor.process(value, self._parameters_at(seconds))
        self._reduction = self._compressor.reduction
        return output


@interface('AudioBufferSourceNode', 'AudioScheduledSourceNode')
class AudioBufferSourceNode(AudioScheduledSourceNode):
    """A buffer played back: its samples, read at the rate and position it was given."""

    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._buffer = None
        self._playback_rate = AudioParam(1.0)
        self._detune = AudioParam(0.0)
        self._loop = False
        self._loop_start = 0.0
        self._loop_end = 0.0
        self._position = 0.0
        self._offset = 0.0
        self._duration_samples = None
        self._finished = False

    def get_buffer(self): return pyv8.Null if self._buffer is None else self._buffer
    def set_buffer(self, value):
        if is_nullish(value) or value is pyv8.Null:
            self._buffer = None
            return
        if not isinstance(value, AudioBuffer):
            raise PageTypeError("Failed to set the 'buffer' property on 'AudioBufferSourceNode':"
                              " The provided value is not of type 'AudioBuffer'.")
        self._buffer = value
    def get_playbackRate(self): return self._playback_rate
    def get_detune(self): return self._detune
    def get_loop(self): return self._loop
    def set_loop(self, value): self._loop = boolean(value)
    def get_loopStart(self): return self._loop_start
    def set_loopStart(self, value):
        self._loop_start = _double(value, "AudioBufferSourceNode.loopStart")
    def get_loopEnd(self): return self._loop_end
    def set_loopEnd(self, value):
        self._loop_end = _double(value, "AudioBufferSourceNode.loopEnd")

    def fn_start(self, when=0.0, offset=None, duration=None, *_extra):
        if self._start is not None:
            raise dom_exception("InvalidStateError", "cannot call start more than once.")
        self._start = _double(when, "AudioBufferSourceNode.start")
        if self._buffer is None:
            return None
        if not is_nullish(offset) and offset is not pyv8.Null:
            self._offset = _double(offset, "AudioBufferSourceNode.start") * self._buffer._sample_rate
        if not is_nullish(duration) and duration is not pyv8.Null:
            self._duration_samples = (_double(duration, "AudioBufferSourceNode.start")
                                      * self._buffer._sample_rate)
        self._position = self._offset
        return None

    def _read(self, position):
        """One sample *position* buffer samples in, interpolated between neighbours."""
        buffer = self._buffer
        view = buffer._views[0]
        samples = buffer._samples[0] if view is None else view
        index = int(position)
        value = float(samples[index]) if 0 <= index < buffer._length else 0.0
        following = float(samples[index + 1]) if 0 <= index + 1 < buffer._length else 0.0
        return value + (following - value) * (position - index)

    def _compute(self, seconds):
        if self._finished or self._buffer is None or not self._sounding(seconds):
            return 0.0
        if self._stop != math.inf and seconds >= self._stop:
            self._finished = True
            return 0.0
        buffer, rate = self._buffer, 1.0
        rate = self._context.get_sampleRate() if self._context is not None else SAMPLE_RATE
        if buffer._length == 0 or buffer._sample_rate <= 0 or rate <= 0:
            self._finished = True
            return 0.0
        if (self._duration_samples is not None
                and self._position >= self._offset + self._duration_samples):
            self._finished = True
            return 0.0
        loop_end = self._loop_end * buffer._sample_rate if self._loop_end > 0 else buffer._length
        loop_start = self._loop_start * buffer._sample_rate if self._loop_start > 0 else 0.0
        if self._loop and loop_end > loop_start and self._position >= loop_end:
            self._position = loop_start + (self._position - loop_start) % (loop_end - loop_start)
        if self._position >= buffer._length and not self._loop:
            self._finished = True
            return 0.0
        value = self._read(self._position)
        self._position += (self._playback_rate.value_at(seconds)
                           * 2.0 ** (self._detune.value_at(seconds) / 1200.0)
                           * buffer._sample_rate / rate)
        return value


#: What Chrome answers when the bytes it was handed are not audio it can read.
_DECODE_FAILURE = "Unable to decode audio data"


def _buffer_bytes(data):
    """The bytes of the ArrayBuffer a page handed over; a view is a TypeError there."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    name = getattr(data._get("constructor"), "name", None) \
        if isinstance(data, pyv8.JSObject) else None
    if name != "ArrayBuffer":
        raise PageTypeError("Failed to execute 'decodeAudioData' on 'BaseAudioContext':"
                          " parameter 1 is not of type 'ArrayBuffer'.")
    view = pyv8.JSTypeArray(data, pyv8.ArrayType.Uint8Array)
    return bytes(int(view[index]) for index in range(int(view._get("byteLength"))))


def _wav_channels(blob):
    """``(channels of samples, rate)`` of a RIFF/WAVE file, or ``None`` when not one."""
    if len(blob) < 44 or blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
        return None
    position, encoding, channels, rate, bits, body = 12, None, 0, 0, 0, None
    while position + 8 <= len(blob):
        name = blob[position:position + 4]
        size = int.from_bytes(blob[position + 4:position + 8], "little")
        chunk = blob[position + 8:position + 8 + size]
        if name == b"fmt " and len(chunk) >= 16:
            encoding = int.from_bytes(chunk[0:2], "little")
            channels = int.from_bytes(chunk[2:4], "little")
            rate = int.from_bytes(chunk[4:8], "little")
            bits = int.from_bytes(chunk[14:16], "little")
        elif name == b"data":
            body = chunk
        position += 8 + size + (size & 1)
    if encoding is None or body is None or channels < 1 or rate < 1:
        return None
    width = bits // 8
    if encoding not in (1, 3) or width not in (1, 2, 3, 4) or (encoding == 3 and width != 4):
        return None
    frames = len(body) // (width * channels)
    if frames < 1:
        return None
    return _wav_samples(body, frames, width, channels, encoding), rate


def _wav_samples(body, frames, width, channels, encoding):
    """One list of samples per channel, from the interleaved frame data."""
    count = frames * channels
    if width == 1:
        values = [(byte - 128) / (128.0 if byte < 128 else 127.0) for byte in body[:count]]
    elif width == 3:
        values = [_scaled(int.from_bytes(body[position:position + 3], "little", signed=True), 24)
                  for position in range(0, count * 3, 3)]
    else:
        raw = array.array("f" if encoding == 3 else {2: "h", 4: "i"}[width])
        raw.frombytes(body[:count * width])
        if sys.byteorder != "little":
            raw.byteswap()
        values = list(raw) if encoding == 3 else [_scaled(value, width * 8) for value in raw]
    return [values[channel::channels] for channel in range(channels)]


def _scaled(sample, bits):
    """Chrome's PCM conversion: the negative half divides by 2**n, the rest by 2**n - 1."""
    half = 1 << (bits - 1)
    return sample / (half if sample < 0 else half - 1)


def _resampled(channel, rate, target):
    """One channel at *target*, read between its samples."""
    count = max(int(round(len(channel) * target / rate)), 1)
    if rate == target:
        return list(channel)
    last = len(channel) - 1
    step = rate / target
    out = []
    for index in range(count):
        position = index * step
        left = min(int(position), last)
        right = min(left + 1, last)
        out.append(channel[left] + (channel[right] - channel[left]) * (position - left))
    return out


def _blackman(index, size):
    """The window the specification's spectrum is taken through."""
    return (0.42 - 0.5 * math.cos(2.0 * math.pi * index / size)
            + 0.08 * math.cos(4.0 * math.pi * index / size))


def _fft(values):
    """The discrete Fourier transform of a power-of-two length, in place."""
    count = len(values)
    for index in range(1, count):
        bit = count >> 1
        reversed_index = 0
        position = index
        while bit:
            if position & 1:
                reversed_index += bit
            position >>= 1
            bit >>= 1
        if reversed_index > index:
            values[index], values[reversed_index] = values[reversed_index], values[index]
    size = 2
    while size <= count:
        half = size // 2
        step = complex(math.cos(-2.0 * math.pi / size), math.sin(-2.0 * math.pi / size))
        for start in range(0, count, size):
            factor = 1.0 + 0.0j
            for offset in range(half):
                even = values[start + offset]
                odd = values[start + offset + half] * factor
                values[start + offset] = even + odd
                values[start + offset + half] = even - odd
                factor *= step
        size *= 2
    return values


#: The scale Chrome's convolver takes from an impulse: ``sqrt(length / squares) / rate``,
#: times this, measured at four rates.
_CONVOLVER_CALIBRATION = 55.5186


#: What a ``BiquadFilterNode`` gain may reach, as Chrome reports it.
_BIQUAD_GAIN_MAX = 1541.273681640625


#: The buffer sizes ``ScriptProcessorNode`` accepts, and the one it picks by itself.
_SCRIPT_BUFFER_SIZES = (256, 512, 1024, 2048, 4096, 8192, 16384)
_DEFAULT_SCRIPT_BUFFER_SIZE = 256


def _sequence(value, what):
    """The numbers a JS array or typed array holds."""
    if isinstance(value, pyv8.JSTypeArray):
        return [float(value[position]) for position in range(int(value.length))]
    try:
        return [float(item) for item in sequence(value)]
    except (TypeError, ValueError) as exc:
        raise PageTypeError(f"Failed to execute '{what}': the value is not a sequence.") from exc


def _is_float32_array(value):
    """Whether a page handed over a Float32Array, which the specification asks for."""
    if not isinstance(value, pyv8.JSTypeArray):
        return False
    return getattr(value._get("constructor"), "name", "") == "Float32Array"


def _float32_view(value, what):
    """The Float32Array a page handed over, or the TypeError another type earns."""
    if _is_float32_array(value):
        return value
    raise PageTypeError(f"Failed to execute '{what}': parameter 1 is not of type 'Float32Array'.")


def _view_length(view, what):
    """The length of a Float32Array a page handed over."""
    return int(_float32_view(view, what)._get("length"))


@interface('PeriodicWave', None)
class PeriodicWave:
    """The harmonics an oscillator plays in place of a built-in waveform."""


    def __init__(self, context=None, options=None, *, _realm=None, **_):
        if is_nullish(options):
            raise PageTypeError("Failed to construct 'PeriodicWave': 2 arguments required, but"
                              " only 1 present.")
        options = dictionary(options, name="PeriodicWaveOptions",
                             prefix="Failed to construct 'PeriodicWave': ")
        real = options.get("real", pyv8.Undefined)
        imag = options.get("imag", pyv8.Undefined)
        self._real = [] if is_nullish(real) else _sequence(real, "PeriodicWave")
        self._imag = [] if is_nullish(imag) else _sequence(imag, "PeriodicWave")
        if not self._real and not self._imag:
            # Chrome's default wave is a sine.
            self._real, self._imag = [0.0, 0.0], [0.0, 1.0]
        if len(self._real) != len(self._imag):
            raise dom_exception("IndexSizeError", "The length of the real array provided must"
                                " match the length of the imaginary array provided.")
        self._normalize = not boolean(options.get("disableNormalization", False))
        self._tables = {}
        self._peak = None

    def _coefficients(self, limit):
        harmonics = min(limit + 1, len(self._real))
        return [(harmonic, self._real[harmonic], self._imag[harmonic])
                for harmonic in range(1, harmonics)]

    def _table(self, limit):
        table = self._tables.get(limit)
        if table is None:
            terms = self._coefficients(limit)
            table = []
            for point in range(_WAVE_POINTS):
                angle = 2.0 * math.pi * point / _WAVE_POINTS
                table.append(sum(real * math.cos(harmonic * angle)
                                 + imag * math.sin(harmonic * angle)
                                 for harmonic, real, imag in terms))
            peak = self._peak
            if peak is None:
                peak = max((abs(value) for value in table), default=0.0)
                self._peak = peak
            if peak:
                table = [value / peak for value in table]
            self._tables[limit] = table
        return table

    def sample(self, phase, frequency, rate):
        """One sample of the wave, band-limited to the harmonics *frequency* can carry."""
        if frequency <= 0:
            return 0.0
        limit = min(int(rate / 2 / frequency), len(self._real) - 1)
        if limit < 1:
            return 0.0
        if not self._normalize:
            terms = self._coefficients(limit)
            angle = 2.0 * math.pi * phase
            return sum(real * math.cos(harmonic * angle) + imag * math.sin(harmonic * angle)
                       for harmonic, real, imag in terms)
        table = self._table(limit)
        position = phase * _WAVE_POINTS
        index = int(position)
        following = (index + 1) % _WAVE_POINTS
        fraction = position - index
        return table[index] + (table[following] - table[index]) * fraction


@interface('AudioListener', None)
class AudioListener:
    """Where the ears are: the position and orientation a panner reads."""

    def __init__(self, **_):
        self._positionX = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._positionY = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._positionZ = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._forwardX = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._forwardY = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._forwardZ = AudioParam(-1.0, minimum=-_PARAM_MAX)
        self._upX = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._upY = AudioParam(1.0, minimum=-_PARAM_MAX)
        self._upZ = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._params = {name: getattr(self, "_" + name) for name in (
            "positionX", "positionY", "positionZ", "forwardX", "forwardY", "forwardZ",
            "upX", "upY", "upZ")}

    def get_positionX(self): return self._positionX
    def get_positionY(self): return self._positionY
    def get_positionZ(self): return self._positionZ
    def get_forwardX(self): return self._forwardX
    def get_forwardY(self): return self._forwardY
    def get_forwardZ(self): return self._forwardZ
    def get_upX(self): return self._upX
    def get_upY(self): return self._upY
    def get_upZ(self): return self._upZ

    def fn_setPosition(self, x=MISSING, y=MISSING, z=MISSING, *_extra):
        if z is MISSING:
            raise missing_error('AudioListener', 'setPosition', 3, 0, constructor=False)
        for name, value in zip(("positionX", "positionY", "positionZ"), (x, y, z)):
            self._params[name].set_value(_double(value, "AudioListener.setPosition"))
        return None

    def fn_setOrientation(self, x=MISSING, y=MISSING, z=MISSING, xUp=0, yUp=0, zUp=0, *_extra):
        if z is MISSING:
            raise missing_error('AudioListener', 'setOrientation', 6, 3, constructor=False)
        for name, value in zip(("forwardX", "forwardY", "forwardZ"), (x, y, z)):
            self._params[name].set_value(_double(value, "AudioListener.setOrientation"))
        for name, value in zip(("upX", "upY", "upZ"), (xUp, yUp, zUp)):
            self._params[name].set_value(_double(value, "AudioListener.setOrientation"))
        return None

    def position(self, seconds):
        return tuple(self._params[name].value_at(seconds)
                     for name in ("positionX", "positionY", "positionZ"))


@interface('ConstantSourceNode', 'AudioScheduledSourceNode')
class ConstantSourceNode(AudioScheduledSourceNode):
    """A steady value a page switches on and off."""

    inputs, outputs = 0, 1

    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._offset = AudioParam(1.0, minimum=-_PARAM_MAX)
        self._finished = False

    def get_offset(self): return self._offset

    def _compute(self, seconds):
        if not self._sounding(seconds):
            if self._start is not None and seconds >= self._stop:
                self._finished = True
            return 0.0
        return self._offset.value_at(seconds)


@interface('DelayNode', 'AudioNode')
class DelayNode(AudioNode):
    """A delay line: the input, later by the time the parameter names."""

    def __init__(self, max_delay_time=1.0, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        seconds = 1.0 if is_nullish(max_delay_time) else _double(max_delay_time, "DelayNode")
        if seconds < 0 or seconds > 180:
            raise dom_exception("NotSupportedError",
                                "The max delay time provided is outside the supported range.")
        self._delay_time = AudioParam(0.0, minimum=0.0, maximum=seconds)
        self._history = [0.0] * (int(seconds * self._rate()) + 2)

    def _rate(self):
        return self._context.get_sampleRate() if self._context is not None else SAMPLE_RATE

    def get_delayTime(self): return self._delay_time

    def _compute(self, seconds):
        delay = max(0.0, _float32(self._delay_time.value_at(seconds) * self._rate(),
                                  "DelayNode delay in samples"))
        self._history.insert(0, super()._compute(seconds))
        del self._history[self._history_length:]
        position = delay
        index = int(position)
        value = self._history[index] if index < len(self._history) else 0.0
        following = self._history[index + 1] if index + 1 < len(self._history) else 0.0
        return value + (following - value) * (position - index)

    @property
    def _history_length(self):
        return int(self._delay_time._maximum * self._rate()) + 2


@interface('ConvolverNode', 'AudioNode')
class ConvolverNode(AudioNode):
    """Reverb: the input convolved with an impulse, scaled as Chrome scales it."""


    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._channel_count_mode = "clamped-max"
        self._buffer = None
        self._normalize = True
        self._impulse = []
        self._scale = 0.0
        self._history = []

    def get_buffer(self): return pyv8.Null if self._buffer is None else self._buffer
    def get_normalize(self): return self._normalize

    def set_buffer(self, value):
        if is_nullish(value) or value is pyv8.Null:
            self._buffer = None
        elif not isinstance(value, AudioBuffer):
            raise PageTypeError("Failed to set the 'buffer' property on 'ConvolverNode':"
                              " The provided value is not of type 'AudioBuffer'.")
        elif value.get_numberOfChannels() not in (1, 2, 4):
            raise dom_exception("NotSupportedError",
                                "The buffer must have 1, 2, or 4 channels.")
        else:
            self._buffer = value
        self._impulse = [] if self._buffer is None else [
            self._buffer._values(channel) for channel in range(self._buffer._channels)]
        self._history = []
        self._scale = self._normalization() if self._normalize else 1.0

    def set_normalize(self, value):
        # Chrome takes the scale when the buffer arrives, and never again.
        self._normalize = boolean(value)

    def _normalization(self):
        squares = sum(sample * sample for channel in self._impulse for sample in channel)
        length = max((len(channel) for channel in self._impulse), default=0)
        if not squares or not length:
            return 0.0
        return (_CONVOLVER_CALIBRATION * math.sqrt(length / squares) / self._rate())

    def _rate(self):
        return self._context.get_sampleRate() if self._context is not None else SAMPLE_RATE

    def _compute(self, seconds):
        value = super()._compute(seconds)
        if not self._impulse:
            return 0.0
        self._history.insert(0, value)
        length = max(len(channel) for channel in self._impulse)
        del self._history[length + 1:]
        total = 0.0
        for channel in self._impulse:
            for index, sample in enumerate(channel[:len(self._history)]):
                total += sample * self._history[index]
        return total * self._scale


@interface('IIRFilterNode', 'AudioNode')
class IIRFilterNode(AudioNode):
    """A filter from its feedforward and feedback coefficients."""

    #: The most coefficients each list may hold.
    MAX_COEFFICIENTS = 20

    def __init__(self, feedforward=MISSING, feedback=MISSING, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        if feedback is MISSING:
            raise missing_error('IIRFilterNode', 'IIRFilterNode', 2, 1, constructor=True)
        self._feedforward = _sequence(feedforward, "IIRFilterNode")
        self._feedback = _sequence(feedback, "IIRFilterNode")
        if not self._feedforward or not self._feedback:
            raise dom_exception("NotSupportedError", "The number of coefficients must be at least"
                                " one.")
        if len(self._feedforward) > self.MAX_COEFFICIENTS \
                or len(self._feedback) > self.MAX_COEFFICIENTS:
            raise dom_exception("NotSupportedError", "The number of coefficients cannot exceed"
                                f" {self.MAX_COEFFICIENTS}.")
        if not any(self._feedforward):
            raise dom_exception("InvalidStateError", "At least one feedforward coefficient must be"
                                " non-zero.")
        if not any(self._feedback):
            raise dom_exception("InvalidStateError", "At least one feedback coefficient must be"
                                " non-zero.")
        if self._feedback[0] == 0:
            raise dom_exception("InvalidStateError", "The first feedback coefficient must be"
                                " non-zero.")
        self._state = [0.0] * (max(len(self._feedforward), len(self._feedback)) - 1)

    def _compute(self, seconds):
        x = super()._compute(seconds)
        feedback = self._feedback[0]
        total = self._feedforward[0] * x
        for index, coefficient in enumerate(self._feedforward[1:]):
            if index < len(self._state):
                total += coefficient * self._state[index]
        for index, coefficient in enumerate(self._feedback[1:]):
            if index < len(self._state):
                total -= coefficient * self._state[index]
        total /= feedback
        if self._state:
            for index in range(len(self._state) - 1, 0, -1):
                self._state[index] = self._state[index - 1]
            self._state[0] = total
        return total

    def _response(self, frequency, rate):
        omega = 2.0 * math.pi * frequency / rate
        numerator = sum(coefficient * complex(math.cos(-omega * index), math.sin(-omega * index))
                        for index, coefficient in enumerate(self._feedforward))
        denominator = sum(coefficient * complex(math.cos(-omega * index), math.sin(-omega * index))
                          for index, coefficient in enumerate(self._feedback))
        return numerator / denominator if denominator else 0j

    def fn_getFrequencyResponse(self, frequency=MISSING, magnitude=MISSING, phase=MISSING,
                                *_extra):
        if phase is MISSING:
            raise missing_error('IIRFilterNode', 'getFrequencyResponse', 3, 0, constructor=False)
        values = _sequence(frequency, "getFrequencyResponse")
        if _view_length(magnitude, "getFrequencyResponse") != len(values) \
                or _view_length(phase, "getFrequencyResponse") != len(values):
            raise dom_exception("InvalidAccessError", "The arrays must all have the same length.")
        rate = self._rate()
        for index, value in enumerate(values):
            response = self._response(min(max(value, 0.0), rate / 2.0), rate)
            magnitude[index] = abs(response)
            phase[index] = math.atan2(response.imag, response.real)
        return None

    def _rate(self):
        return self._context.get_sampleRate() if self._context is not None else SAMPLE_RATE


@interface('BiquadFilterNode', 'AudioNode')
class BiquadFilterNode(AudioNode):
    """A filter: the response its type and parameters describe."""

    TYPES = ("lowpass", "highpass", "bandpass", "lowshelf", "highshelf", "peaking",
             "notch", "allpass")


    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        rate = self._rate()
        self._type = "lowpass"
        self._frequency = AudioParam(350.0, minimum=0.0, maximum=rate / 2.0)
        self._detune = AudioParam(0.0, minimum=-153600.0, maximum=153600.0)
        self._Q = AudioParam(1.0, minimum=-_PARAM_MAX)
        self._gain = AudioParam(0.0, minimum=-_PARAM_MAX, maximum=_BIQUAD_GAIN_MAX)
        self._state = [0.0, 0.0]

    def _rate(self):
        return self._context.get_sampleRate() if self._context is not None else SAMPLE_RATE

    def get_type(self): return self._type
    def set_type(self, value):
        value = dom_string(value)
        if value not in self.TYPES:
            raise PageTypeError(f"The provided value '{value}' is not a valid enum value.")
        self._type = value
    def get_frequency(self): return self._frequency
    def get_detune(self): return self._detune
    def get_Q(self): return self._Q
    def get_gain(self): return self._gain

    def _coefficients(self, frequency, quality, gain, rate):
        """``(b0, b1, b2, a0, a1, a2)`` for the type at these values."""
        frequency = min(max(frequency, 0.0), rate / 2.0)
        omega = 2.0 * math.pi * frequency / rate
        cosine, sine = math.cos(omega), math.sin(omega)
        amplitude = 10.0 ** (gain / 40.0)
        kind = self._type
        if kind in ("lowpass", "highpass"):
            # These two spell the quality in decibels, as the specification does.
            alpha = sine / (2.0 * 10.0 ** (quality / 20.0))
            if kind == "lowpass":
                b0, b1, b2 = (1.0 - cosine) / 2.0, 1.0 - cosine, (1.0 - cosine) / 2.0
            else:
                b0, b1, b2 = (1.0 + cosine) / 2.0, -(1.0 + cosine), (1.0 + cosine) / 2.0
            a0, a1, a2 = 1.0 + alpha, -2.0 * cosine, 1.0 - alpha
        elif kind == "bandpass":
            alpha = sine / (2.0 * quality)
            b0, b1, b2 = alpha, 0.0, -alpha
            a0, a1, a2 = 1.0 + alpha, -2.0 * cosine, 1.0 - alpha
        elif kind == "notch":
            alpha = sine / (2.0 * quality)
            b0, b1, b2 = 1.0, -2.0 * cosine, 1.0
            a0, a1, a2 = 1.0 + alpha, -2.0 * cosine, 1.0 - alpha
        elif kind == "allpass":
            alpha = sine / (2.0 * quality)
            b0, b1, b2 = 1.0 - alpha, -2.0 * cosine, 1.0 + alpha
            a0, a1, a2 = 1.0 + alpha, -2.0 * cosine, 1.0 - alpha
        elif kind == "peaking":
            alpha = sine / (2.0 * quality)
            b0, b1, b2 = 1.0 + alpha * amplitude, -2.0 * cosine, 1.0 - alpha * amplitude
            a0, a1, a2 = 1.0 + alpha / amplitude, -2.0 * cosine, 1.0 - alpha / amplitude
        else:
            root = math.sqrt(amplitude)
            alpha = sine / 2.0 * math.sqrt(2.0)
            if kind == "lowshelf":
                b0 = amplitude * ((amplitude + 1.0) - (amplitude - 1.0) * cosine
                                  + 2.0 * root * alpha)
                b1 = 2.0 * amplitude * ((amplitude - 1.0) - (amplitude + 1.0) * cosine)
                b2 = amplitude * ((amplitude + 1.0) - (amplitude - 1.0) * cosine
                                  - 2.0 * root * alpha)
                a0 = (amplitude + 1.0) + (amplitude - 1.0) * cosine + 2.0 * root * alpha
                a1 = -2.0 * ((amplitude - 1.0) + (amplitude + 1.0) * cosine)
                a2 = (amplitude + 1.0) + (amplitude - 1.0) * cosine - 2.0 * root * alpha
            else:
                b0 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cosine
                                  + 2.0 * root * alpha)
                b1 = -2.0 * amplitude * ((amplitude - 1.0) + (amplitude + 1.0) * cosine)
                b2 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cosine
                                  - 2.0 * root * alpha)
                a0 = (amplitude + 1.0) - (amplitude - 1.0) * cosine + 2.0 * root * alpha
                a1 = 2.0 * ((amplitude - 1.0) - (amplitude + 1.0) * cosine)
                a2 = (amplitude + 1.0) - (amplitude - 1.0) * cosine - 2.0 * root * alpha
        return b0, b1, b2, a0, a1, a2

    def _values(self, seconds):
        frequency = self._frequency.value_at(seconds) * 2.0 ** (self._detune.value_at(seconds)
                                                               / 1200.0)
        return frequency, self._Q.value_at(seconds), self._gain.value_at(seconds)

    def _compute(self, seconds):
        frequency, quality, gain = self._values(seconds)
        b0, b1, b2, a0, a1, a2 = self._coefficients(frequency, quality, gain, self._rate())
        b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0
        x = super()._compute(seconds)
        y = b0 * x + self._state[0]
        self._state[0] = b1 * x - a1 * y + self._state[1]
        self._state[1] = b2 * x - a2 * y
        return y

    def fn_getFrequencyResponse(self, frequency=MISSING, magnitude=MISSING, phase=MISSING,
                                *_extra):
        if phase is MISSING:
            raise missing_error('BiquadFilterNode', 'getFrequencyResponse', 3, 0,
                                constructor=False)
        values = _sequence(frequency, "getFrequencyResponse")
        if _view_length(magnitude, "getFrequencyResponse") != len(values) \
                or _view_length(phase, "getFrequencyResponse") != len(values):
            raise dom_exception("InvalidAccessError", "The arrays must all have the same length.")
        rate = self._rate()
        frequency_now, quality, gain = self._values(0.0)
        b0, b1, b2, a0, a1, a2 = self._coefficients(frequency_now, quality, gain, rate)
        for index, value in enumerate(values):
            omega = 2.0 * math.pi * min(max(value, 0.0), rate / 2.0) / rate
            z1 = complex(math.cos(-omega), math.sin(-omega))
            z2 = z1 * z1
            response = ((b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2))
            magnitude[index] = abs(response)
            phase[index] = math.atan2(response.imag, response.real)
        return None


@interface('WaveShaperNode', 'AudioNode')
class WaveShaperNode(AudioNode):
    """A shape applied to every sample, from the curve a page hands over."""


    OVERSAMPLE = ("none", "2x", "4x")

    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._curve = None
        self._view = None
        self._oversample = "none"

    def get_curve(self): return pyv8.Null if self._curve is None else self._view
    def get_oversample(self): return self._oversample
    def set_oversample(self, value):
        value = dom_string(value)
        if value not in self.OVERSAMPLE:
            raise PageTypeError(f"The provided value '{value}' is not a valid enum value.")
        self._oversample = value

    def set_curve(self, value):
        if is_nullish(value) or value is pyv8.Null:
            self._curve = None
            self._view = None
            return
        if not _is_float32_array(value):
            raise PageTypeError("Failed to set the 'curve' property on 'WaveShaperNode':"
                              " The provided value is not of type 'Float32Array'.")
        values = _sequence(value, "WaveShaperNode")
        if len(values) < 2:
            raise dom_exception("InvalidStateError", "The curve must have at least two values.")
        self._curve = values
        self._view = value

    def _compute(self, seconds):
        value = super()._compute(seconds)
        if self._curve is None:
            return value
        count = len(self._curve)
        position = (count - 1) / 2.0 * (min(max(value, -1.0), 1.0) + 1.0)
        index = min(int(position), count - 2)
        fraction = position - index
        return self._curve[index] + (self._curve[index + 1] - self._curve[index]) * fraction


@interface('StereoPannerNode', 'AudioNode')
class StereoPannerNode(AudioNode):
    """A source placed across the two channels; a mono graph hears the sum."""


    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._channel_count_mode = "clamped-max"
        self._pan = AudioParam(0.0, minimum=-1.0, maximum=1.0)

    def get_pan(self): return self._pan

    def _compute(self, seconds):
        value = super()._compute(seconds)
        position = min(max(self._pan.value_at(seconds), -1.0), 1.0)
        half = (position + 1.0) / 2.0 * (math.pi / 2.0)
        return value * (math.cos(half) + math.sin(half)) / 2.0


@interface('PannerNode', 'AudioNode')
class PannerNode(AudioNode):
    """A source in space: where it is, how far, and which way it points."""


    PANNING_MODELS = ("equalpower", "HRTF")
    DISTANCE_MODELS = ("linear", "inverse", "exponential")

    def __init__(self, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._channel_count_mode = "clamped-max"
        self._panning_model = "equalpower"
        self._positionX = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._positionY = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._positionZ = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._orientationX = AudioParam(1.0, minimum=-_PARAM_MAX)
        self._orientationY = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._orientationZ = AudioParam(0.0, minimum=-_PARAM_MAX)
        self._distance_model = "inverse"
        self._ref_distance = 1.0
        self._max_distance = 10000.0
        self._rolloff_factor = 1.0
        self._cone_inner_angle = 360.0
        self._cone_outer_angle = 360.0
        self._cone_outer_gain = 0.0
        self._params = {name: getattr(self, "_" + name) for name in (
            "positionX", "positionY", "positionZ", "orientationX", "orientationY",
            "orientationZ")}

    def get_positionX(self): return self._positionX
    def get_positionY(self): return self._positionY
    def get_positionZ(self): return self._positionZ
    def get_orientationX(self): return self._orientationX
    def get_orientationY(self): return self._orientationY
    def get_orientationZ(self): return self._orientationZ
    def get_panningModel(self): return self._panning_model
    def set_panningModel(self, value):
        value = dom_string(value)
        if value not in self.PANNING_MODELS:
            raise PageTypeError(f"The provided value '{value}' is not a valid enum value.")
        self._panning_model = value
    def get_distanceModel(self): return self._distance_model
    def set_distanceModel(self, value):
        value = dom_string(value)
        if value not in self.DISTANCE_MODELS:
            raise PageTypeError(f"The provided value '{value}' is not a valid enum value.")
        self._distance_model = value
    def get_refDistance(self): return self._ref_distance
    def set_refDistance(self, value):
        self._ref_distance = max(0.0, _double(value, "PannerNode.refDistance"))
    def get_maxDistance(self): return self._max_distance
    def set_maxDistance(self, value):
        self._max_distance = max(0.0, _double(value, "PannerNode.maxDistance"))
    def get_rolloffFactor(self): return self._rolloff_factor
    def set_rolloffFactor(self, value):
        self._rolloff_factor = max(0.0, _double(value, "PannerNode.rolloffFactor"))
    def get_coneInnerAngle(self): return self._cone_inner_angle
    def set_coneInnerAngle(self, value):
        self._cone_inner_angle = _double(value, "PannerNode.coneInnerAngle")
    def get_coneOuterAngle(self): return self._cone_outer_angle
    def set_coneOuterAngle(self, value):
        self._cone_outer_angle = _double(value, "PannerNode.coneOuterAngle")
    def get_coneOuterGain(self): return self._cone_outer_gain
    def set_coneOuterGain(self, value):
        self._cone_outer_gain = _double(value, "PannerNode.coneOuterGain")

    def fn_setPosition(self, x=MISSING, y=MISSING, z=MISSING, *_extra):
        if z is MISSING:
            raise missing_error('PannerNode', 'setPosition', 3, 0, constructor=False)
        for name, value in zip(("positionX", "positionY", "positionZ"), (x, y, z)):
            self._params[name].set_value(_double(value, "PannerNode.setPosition"))
        return None

    def fn_setOrientation(self, x=MISSING, y=MISSING, z=MISSING, xUp=0, yUp=0, zUp=0, *_extra):
        if z is MISSING:
            raise missing_error('PannerNode', 'setOrientation', 6, 3, constructor=False)
        for name, value in zip(("orientationX", "orientationY", "orientationZ"), (x, y, z)):
            self._params[name].set_value(_double(value, "PannerNode.setOrientation"))
        return None

    def _distance_gain(self, distance):
        reference = self._ref_distance
        rolloff = self._rolloff_factor
        if self._distance_model == "linear":
            if self._max_distance <= reference:
                return 1.0
            gain = 1.0 - rolloff * (min(distance, self._max_distance) - reference) \
                / (self._max_distance - reference)
        elif self._distance_model == "exponential":
            gain = (max(distance, reference) / reference) ** (-rolloff) if reference else 1.0
        else:
            if reference <= 0:
                return 1.0
            gain = reference / (reference + rolloff * (max(distance, reference) - reference))
        return min(max(gain, 0.0), 1.0)

    def _compute(self, seconds):
        value = super()._compute(seconds)
        listener = self._context.get_listener() if self._context is not None else None
        position = tuple(self._params[name].value_at(seconds)
                         for name in ("positionX", "positionY", "positionZ"))
        ears = listener.position(seconds) if listener is not None else (0.0, 0.0, 0.0)
        offset = tuple(position[index] - ears[index] for index in range(3))
        distance = math.sqrt(sum(component * component for component in offset))
        azimuth = 0.0
        if offset[0] or offset[2]:
            azimuth = math.atan2(offset[0], -offset[2])
        pan = azimuth / (math.pi / 2.0)
        half = (min(max(pan, -1.0), 1.0) + 1.0) / 2.0 * (math.pi / 2.0)
        gain = self._distance_gain(distance) * self._cone_gain(offset, distance, seconds)
        return value * gain * (math.cos(half) + math.sin(half)) / 2.0

    def _cone_gain(self, offset, distance, seconds):
        inner, outer = self._cone_inner_angle, self._cone_outer_angle
        if (inner >= 360 or outer >= 360 or not distance
                or self._cone_outer_gain == 1.0 or inner == outer):
            return 1.0 if inner >= 360 or outer >= 360 else self._cone_outer_gain
        orientation = tuple(self._params[name].value_at(seconds)
                            for name in ("orientationX", "orientationY", "orientationZ"))
        length = math.sqrt(sum(component * component for component in orientation))
        if not length:
            return 1.0
        cosine = sum(orientation[index] * offset[index] for index in range(3)) / (length * distance)
        angle = math.degrees(math.acos(min(max(cosine, -1.0), 1.0)))
        if angle <= inner / 2.0:
            return 1.0
        if angle >= outer / 2.0:
            return self._cone_outer_gain
        extent = (angle - inner / 2.0) / (outer / 2.0 - inner / 2.0)
        return 1.0 + extent * (self._cone_outer_gain - 1.0)


@interface('ChannelMergerNode', 'AudioNode')
class ChannelMergerNode(AudioNode):
    """Brings several inputs into one, which a mono graph hears summed."""

    outputs = 1
    channel_count = 1
    channel_count_mode = "explicit"


    def __init__(self, number_of_inputs=6, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self.inputs = _channel_count(number_of_inputs, "ChannelMergerNode")
        self._channel_count = 1
        self._channel_count_mode = "explicit"

    def _compute(self, seconds):
        # Its output carries one channel per input, so a mono graph hears the average.
        return super()._compute(seconds) / self.inputs

    def set_channelCount(self, value):
        raise dom_exception("InvalidStateError", "The channel count of a merger is fixed.")
    def set_channelCountMode(self, value):
        raise dom_exception("InvalidStateError", "The channel count mode of a merger is fixed.")


@interface('ChannelSplitterNode', 'AudioNode')
class ChannelSplitterNode(AudioNode):
    """Hands one input to each of its outputs."""

    inputs = 1
    channel_count_mode = "explicit"
    channel_interpretation = "discrete"


    def __init__(self, number_of_outputs=6, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        count = _channel_count(number_of_outputs, "ChannelSplitterNode")
        self.outputs = count
        self._channel_count = count
        self._channel_count_mode = "explicit"
        self._channel_interpretation = "discrete"

    def set_channelCount(self, value):
        raise dom_exception("InvalidStateError", "The channel count of a splitter is fixed.")
    def set_channelCountMode(self, value):
        raise dom_exception("InvalidStateError", "The channel count mode of a splitter is fixed.")


@interface('ScriptProcessorNode', 'AudioNode')
class ScriptProcessorNode(AudioNode):
    """A block callback: its output is silence, and the block runs at render time."""

    channel_count = 2
    channel_count_mode = "explicit"

    def __init__(self, buffer_size=0, input_channels=2, output_channels=2, *_extra, **kwargs):
        super().__init__(*_extra, **kwargs)
        self._buffer_size = _script_buffer_size(buffer_size)
        self._input_channels = _channel_count(input_channels, "ScriptProcessorNode")
        self._output_channels = _channel_count(output_channels, "ScriptProcessorNode")
        self._channel_count_mode = "explicit"
        self._blocks = []

    def get_bufferSize(self): return self._buffer_size
    def get_onaudioprocess(self): return self._get_handler("onaudioprocess")
    def set_onaudioprocess(self, value): self._set_handler("onaudioprocess", value)

    def _compute(self, seconds):
        # Chrome's offline callback arrives after the samples it was given, so the block
        # it would have written never reaches the render.
        self._blocks.append(super()._compute(seconds))
        return 0.0

    def _render_finished(self, rate, processed):
        """The block callbacks a finished render owes, one per filled buffer."""
        size = self._buffer_size
        for index in range(processed // size):
            start = index * size
            if start >= len(self._blocks):
                return
            self._fire(start, size, rate, processed / rate)
        del self._blocks[:]

    def _fire(self, start, size, rate, playback_time):
        values = self._blocks[start:start + size]
        if len(values) < size:
            values = values + [0.0] * (size - len(values))
        event = AudioProcessingEvent("audioprocess", {
            "isTrusted": True,
            "playbackTime": playback_time,
            "inputBuffer": AudioBuffer(length=size, sample_rate=rate, channels=1,
                                       samples=values),
            "outputBuffer": AudioBuffer(length=size, sample_rate=rate,
                                        channels=max(self._output_channels, 1)),
        })
        self._dispatch_event(event)


@interface('AudioProcessingEvent', 'Event')
class AudioProcessingEvent(Event):
    """What a script processor hands its callback each block."""

    def __init__(self, type=MISSING, init=None, **_):
        init = {} if is_nullish(init) else dict(init)
        self._playback_time = float(init.pop("playbackTime", 0.0))
        self._input_buffer = init.pop("inputBuffer", None)
        self._output_buffer = init.pop("outputBuffer", None)
        super().__init__(type, init)

    def get_playbackTime(self): return self._playback_time
    def get_inputBuffer(self): return self._input_buffer
    def get_outputBuffer(self): return self._output_buffer


@interface('OfflineAudioCompletionEvent', 'Event')
class OfflineAudioCompletionEvent(Event):
    """The rendered output an offline context hands to its ``complete`` listeners."""

    def __init__(self, type=MISSING, init=None, *_extra, **kwargs):
        if type is MISSING:
            raise missing_error('OfflineAudioCompletionEvent', '', 2, 0, constructor=True)
        if init is MISSING or is_nullish(init):
            init = {}
        init = dictionary(init,
                          name="OfflineAudioCompletionEventInit",
                          prefix="Failed to construct 'OfflineAudioCompletionEvent': ")
        if "renderedBuffer" not in init:
            raise PageTypeError("Failed to construct 'OfflineAudioCompletionEvent': "
                                "Required member renderedBuffer is undefined.")
        buffer = init["renderedBuffer"]
        if not isinstance(buffer, AudioBuffer):
            raise PageTypeError("Failed to construct 'OfflineAudioCompletionEvent': "
                                "renderedBuffer is not of type 'AudioBuffer'.")
        super().__init__(type, init, **kwargs)
        self._rendered_buffer = buffer

    def get_renderedBuffer(self): return self._rendered_buffer


def _channel_count(value, what):
    """A channel or connection count Chrome accepts, one to thirty-two."""
    count = 6 if is_nullish(value) or value is MISSING else integer(value)
    if count < 1 or count > 32:
        raise dom_exception("IndexSizeError",
                            f"Failed to construct '{what}': The number of channels provided"
                            " ({0}) is outside the range [1, 32].".format(count))
    return count


def _script_buffer_size(value):
    size = 0 if is_nullish(value) or value is MISSING else integer(value)
    if size == 0:
        return _DEFAULT_SCRIPT_BUFFER_SIZE
    if size not in _SCRIPT_BUFFER_SIZES:
        raise dom_exception("IndexSizeError", "The buffer size provided ({0}) must be one of"
                            " 256, 512, 1024, 2048, 4096, 8192, 16384.".format(size))
    return size

FEATURE = FeatureImplementation({
    "AudioBuffer": AudioBuffer,
    "AudioParam": AudioParam,
    "AudioListener": AudioListener,
    "AudioNode": AudioNode,
    "AudioScheduledSourceNode": AudioScheduledSourceNode,
    "OscillatorNode": OscillatorNode,
    "AudioBufferSourceNode": AudioBufferSourceNode,
    "ConstantSourceNode": ConstantSourceNode,
    "AnalyserNode": AnalyserNode,
    "BiquadFilterNode": BiquadFilterNode,
    "ChannelMergerNode": ChannelMergerNode,
    "ChannelSplitterNode": ChannelSplitterNode,
    "ConvolverNode": ConvolverNode,
    "DelayNode": DelayNode,
    "GainNode": GainNode,
    "IIRFilterNode": IIRFilterNode,
    "PannerNode": PannerNode,
    "StereoPannerNode": StereoPannerNode,
    "WaveShaperNode": WaveShaperNode,
    "DynamicsCompressorNode": DynamicsCompressorNode,
    "ScriptProcessorNode": ScriptProcessorNode,
    "AudioProcessingEvent": AudioProcessingEvent,
    "OfflineAudioCompletionEvent": OfflineAudioCompletionEvent,
    "PeriodicWave": PeriodicWave,
    "AudioDestinationNode": AudioDestinationNode,
    "BaseAudioContext": BaseAudioContext,
    "AudioContext": AudioContext,
    "OfflineAudioContext": OfflineAudioContext,
})

__all__ = [
    "AnalyserNode", "AudioBuffer", "AudioBufferSourceNode", "AudioContext",
    "AudioDestinationNode", "AudioListener", "AudioNode", "AudioParam",
    "AudioProcessingEvent", "AudioScheduledSourceNode", "BaseAudioContext",
    "BiquadFilterNode", "ChannelMergerNode", "ChannelSplitterNode", "ConstantSourceNode",
    "ConvolverNode", "DelayNode", "DynamicsCompressorNode", "GainNode", "IIRFilterNode",
    "OfflineAudioCompletionEvent", "OfflineAudioContext", "OscillatorNode", "PannerNode",
    "PeriodicWave",
    "ScriptProcessorNode", "StereoPannerNode", "WaveShaperNode",
]
