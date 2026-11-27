"""Parse CSS colors used for canvas painting."""

import colorsys
import math
import re

from ...css.colors import NAMED_COLORS


def parses_as_color(value):
    return parse_color(value) is not None


def canvas_color(value):
    red, green, blue, alpha = parse_color(value) or (0, 0, 0, 1.0)
    return red, green, blue, math.floor(alpha * 255 + 0.5)


def _fraction(text):
    value = float(text.removesuffix('%'))
    if not math.isfinite(value):
        raise ValueError("non-finite color component")
    return value / 100 if text.endswith('%') else value


def _clamp(value):
    return max(0.0, min(1.0, value))


def _hue(text):
    angle = re.fullmatch(r'([+-]?(?:\d*\.\d+|\d+\.?)(?:e[+-]?\d+)?)(deg|grad|rad|turn)?', text)
    if angle is None:
        raise ValueError("invalid hue")
    return float(angle[1]) * {None: 1/360, 'deg': 1/360, 'grad': 1/400,
                              'rad': 1/math.tau, 'turn': 1}[angle[2]] % 1


def parse_color(value):
    """Return straight RGB and an alpha fraction, or None for an invalid color."""
    text = str(value).strip().lower()
    if text in NAMED_COLORS:
        argb = NAMED_COLORS[text]
        return (argb >> 16 & 255, argb >> 8 & 255, argb & 255, (argb >> 24) / 255)
    if re.fullmatch(r'#[0-9a-f]+', text):
        digits = text[1:]
        if len(digits) in (3, 4):
            digits = ''.join(channel * 2 for channel in digits)
        if len(digits) in (6, 8):
            red, green, blue, alpha = bytes.fromhex(digits.ljust(8, 'f'))
            return red, green, blue, alpha / 255
    match = re.fullmatch(r'(rgba?|hsla?|hwb)\(([^()]*)\)', text)
    if match is None:
        return None
    name, arguments = match.groups()
    if ',' in arguments:
        components = [part.strip() for part in arguments.split(',')]
    else:
        channels, slash, opacity = arguments.partition('/')
        components = channels.split()
        if slash:
            components.append(opacity.strip())
    if len(components) not in (3, 4):
        return None
    try:
        alpha = _clamp(_fraction(components[3])) if len(components) == 4 else 1.0
        if name.startswith('rgb'):
            rgb = [_clamp(_fraction(part) / (1 if part.endswith('%') else 255))
                   for part in components[:3]]
        elif name.startswith('hsl'):
            hue, saturation, lightness = components[:3]
            rgb = colorsys.hls_to_rgb(_hue(hue), _clamp(_fraction(lightness)),
                                     _clamp(_fraction(saturation)))
        else:
            hue, white, black = components[:3]
            white, black = _clamp(_fraction(white)), _clamp(_fraction(black))
            if white + black >= 1:
                rgb = [white / (white + black)] * 3
            else:
                rgb = [channel * (1 - white - black) + white
                       for channel in colorsys.hsv_to_rgb(_hue(hue), 1, 1)]
        return (*(math.floor(channel * 255 + 0.5) for channel in rgb), alpha)
    except (ValueError, OverflowError):
        return None


def number_text(value):
    """How Chromium's ``String::Number`` spells a double in its messages."""
    if math.isnan(value): return "NaN"
    if math.isinf(value): return "Infinity" if value > 0 else "-Infinity"
    return f"{value:g}"


def serialize_color(rgba):
    """Chrome's canvas serialization: opaque as hex, translucent as ``rgba()``."""
    red, green, blue, alpha = rgba
    if alpha >= 1: return f"#{red:02x}{green:02x}{blue:02x}"
    return f"rgba({red}, {green}, {blue}, {number_text(round(alpha, 3))})"
