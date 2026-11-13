"""``window.matchMedia`` and the list it hands back.

Viewport, screen and resolution features read the same environment as Window and
Screen.  A viewport that never resizes never fires a change.
"""

import math
import re
import struct


from ...idl import interface
from ...foundation.arguments import MISSING, missing_error
from ..dom.events import EventTarget
from ..css.numeric import integer_math


#: ``(min-width: 40em)`` and the Media Queries 4 equality form ``(width = 0px)``.
_FEATURE_RE = re.compile(r"^([a-z-]+)\s*(:|=)\s*(.*)$", re.I | re.S)
_QUERY_GAP = r'(?:\s|/\*.*?\*/)'
_QUERY_TOKENS = {
    sep: re.compile(r'(?P<separator>' +
                    (',' if sep == ',' else _QUERY_GAP + '+' + sep.strip() + _QUERY_GAP + '+') +
                    r')|/\*.*?(?:\*/|\Z)|[()]', re.S)
    for sep in (',', ' and ', ' or ')
}
_NUMBER_RE = r"(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?"
_RESOLUTION_RE = re.compile(rf"^\s*({_NUMBER_RE})\s*(dppx|dpi|dpcm)\s*$", re.I)
_RATIO_RE = re.compile(rf"^\s*({_NUMBER_RE})(?:\s*/\s*({_NUMBER_RE}))?\s*$", re.I)


# Accepted values, with this desktop profile's value first.
_FEATURE_VALUES = {
    'prefers-color-scheme': ('light', 'dark'),
    'prefers-reduced-motion': ('no-preference', 'reduce'),
    'prefers-contrast': ('no-preference', 'less', 'more', 'custom'),
    'hover': ('hover', 'none'), 'any-hover': ('hover', 'none'),
    'pointer': ('fine', 'none', 'coarse'), 'any-pointer': ('fine', 'none', 'coarse'),
    'display-mode': ('browser', 'fullscreen', 'standalone', 'minimal-ui',
                     'window-controls-overlay', 'picture-in-picture'),
    'scripting': ('enabled', 'none', 'initial-only'),
    'update': ('fast', 'none', 'slow'), 'overflow-block': ('scroll', 'none', 'paged'),
    'overflow-inline': ('scroll', 'none'), 'color-gamut': ('srgb', 'p3', 'rec2020'),
    'forced-colors': ('none', 'active'), 'dynamic-range': ('standard', 'high'),
    'prefers-reduced-transparency': ('no-preference', 'reduce'),
    'device-posture': ('continuous', 'folded'),
}


def _pixels(text):
    """A media-query length in pixels, or None when it is not a length."""
    match = re.match(r"^\s*(-?(?:\d+\.?\d*|\.\d+))\s*(px|em|rem)?\s*$", str(text))
    if not match:
        return None
    value = float(match.group(1))
    return value * 16.0 if (match.group(2) or "px").lower() in ("em", "rem") else value


def _resolution(value):
    match = _RESOLUTION_RE.fullmatch(value)
    if match is None:
        return None
    amount = float(match.group(1))
    return amount * {"dppx": 1, "dpi": 1 / 96, "dpcm": 2.54 / 96}[match.group(2).lower()]


def _resolution_float(value):
    return struct.unpack('f', struct.pack('f', min(value, 3.4028234663852886e38)))[0]


def _ratio(value):
    match = _RATIO_RE.fullmatch(value)
    if match is None:
        return None
    denominator = float(match.group(2)) if match.group(2) else 1.0
    return float(match.group(1)) / denominator if denominator else math.inf


def _compare(name, actual, wanted):
    if wanted is None:
        return None
    if name.startswith("min-"):
        return actual >= wanted
    if name.startswith("max-"):
        return actual <= wanted
    return abs(actual - wanted) < 1e-9


def _matches_feature(name, value, width, height, screen_width, screen_height,
                     dpr, color_depth):
    name, value = name.lower(), value.strip().lower()
    base = name.removeprefix("min-").removeprefix("max-")
    dimensions = {"width": width, "height": height,
                  "device-width": screen_width, "device-height": screen_height}
    if base in dimensions:
        return _compare(name, dimensions[base], _pixels(value))
    if base == "resolution":
        wanted = _resolution(value)
        # Blink compares resolution in binary32 even when devicePixelRatio
        # exposes a double (e.g. 1.0000000149011612 on Windows).
        actual = _resolution_float(dpr)
        if wanted is not None:
            wanted = _resolution_float(wanted)
        if wanted is not None and value.endswith("dpcm"):
            # Blink compares dpcm values at two decimal places in dppx.
            return _compare(name, math.floor(0.5 + 100 * actual) / 100,
                            math.floor(0.5 + 100 * wanted) / 100)
        return _compare(name, actual, wanted)
    if base in ("aspect-ratio", "device-aspect-ratio"):
        numerator, denominator = ((width, height) if base == "aspect-ratio"
                                  else (screen_width, screen_height))
        wanted = _ratio(value)
        if wanted is None:
            return None
        if math.isinf(wanted):
            return True if name.startswith('max-') else denominator == 0
        # Cross products also define comparisons for a zero-height viewport;
        # in particular both sides are zero for a hidden 0x0 frame.
        return _compare(name, numerator, denominator * wanted)
    if base in ('color', 'color-index', 'monochrome'):
        return _compare(name, color_depth // 3 if base == 'color' else 0, integer_math(value))
    if name == "orientation":
        return (value == ("landscape" if width > height else "portrait")
                if value in ('landscape', 'portrait') else None)
    accepted = _FEATURE_VALUES.get(name, ())
    return accepted[0] == value if value in accepted else None


def _split_query(text, separator, *, recover=False):
    """Split only at balanced, top-level delimiters; reject malformed input."""
    parts, depth, start = [], 0, 0
    for token in _QUERY_TOKENS[separator].finditer(text):
        spelling = token.group()
        if token['separator'] is not None:
            if depth == 0:
                parts.append(text[start:token.start()].strip())
                start = token.end()
        elif spelling.startswith('/*'):
            if not spelling.endswith('*/'):
                return None
        elif spelling == '(':
            depth += 1
            if depth > 64:
                return None
        else:
            depth -= 1
            if depth < 0:
                if not recover:
                    return None
                depth = 0
    return None if depth else [*parts, text[start:].strip()]


def _condition(text, features, depth=0):
    if depth > 64:
        return None
    conjunction = _split_query(text, ' and ')
    disjunction = _split_query(text, ' or ')
    if conjunction is None or disjunction is None:
        return None
    if len(conjunction) > 1 and len(disjunction) > 1:
        return None
    operands = conjunction if len(conjunction) > 1 else disjunction
    if len(operands) > 1:
        values = [_condition(part, features, depth + 1) for part in operands]
        if len(conjunction) > 1:
            return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    if text.startswith('not '):
        value = _condition(text[4:].strip(), features, depth + 1)
        return None if value is None else not value
    if not text.startswith('(') or not text.endswith(')'):
        return None
    # Ensure the outer pair covers the whole operand, including nested math.
    if _split_query(text[1:-1], ',') is None:
        return None
    inner = text[1:-1].strip()
    match = _FEATURE_RE.fullmatch(inner)
    if match:
        name, _operator, value = match.groups()
        return _matches_feature(name, value, *features)
    if re.fullmatch('[a-z-]+', inner):
        booleans = dict(zip(('width', 'height', 'device-width', 'device-height',
                             'resolution', 'color'), features))
        if inner in booleans:
            return booleans[inner] > 0
        if inner in ('monochrome', 'color-index'):
            return False
        if inner in _FEATURE_VALUES:
            return _FEATURE_VALUES[inner][0] not in ('none', 'no-preference')
        return None
    return _condition(inner, features, depth + 1)


def evaluate(query, width, height, *, screen_width=None, screen_height=None,
             dpr=1.0, color_depth=24):
    """Whether *query* holds for a viewport of this size; an unknown feature
    answers false, as a browser answers an unsupported feature.
    """
    screen_width = width if screen_width is None else screen_width
    screen_height = height if screen_height is None else screen_height
    text = str(query).strip().lower()
    if not text:
        return True
    alternatives = _split_query(text, ',', recover=True)
    if alternatives is None:
        return False
    features = (width, height, screen_width, screen_height, dpr, color_depth)
    for alternative in alternatives:
        if alternative.startswith(('(', 'not (')):
            matches = _condition(alternative, features)
        else:
            media = re.fullmatch(r'(?:(not|only)\s+)?([a-z-]+)(?:\s+and\s+(.+))?',
                                 alternative, re.S)
            if media is None:
                continue
            modifier, kind, condition = media.groups()
            value = _condition(condition, features) if condition else True
            matches = None if value is None else kind in ('all', 'screen') and value
            if modifier == 'not' and matches is not None:
                matches = not matches
        if matches is True:
            return True
    return False


@interface('MediaQueryList', 'EventTarget')
class MediaQueryList(EventTarget):
    """``matchMedia``'s answer: the query and whether it holds right now."""

    def __init__(self, query="", width=0, height=0, *, screen_width=None,
                 screen_height=None, dpr=1.0, color_depth=24, **_):
        super().__init__()
        self._query = str(query)
        self._matches = evaluate(self._query, width, height,
                                 screen_width=screen_width, screen_height=screen_height,
                                 dpr=dpr, color_depth=color_depth)

    def get_media(self): return self._query
    def get_matches(self): return bool(self._matches)
    def get_onchange(self): return self._get_handler("onchange")
    def set_onchange(self, value): self._set_handler("onchange", value)

    def fn_addListener(self, callback=MISSING, *_extra):
        if callback is MISSING:
            raise missing_error('MediaQueryList', 'addListener', 1, 0, constructor=False)
        return self.fn_addEventListener("change", callback)

    def fn_removeListener(self, callback=MISSING, *_extra):
        if callback is MISSING:
            raise missing_error('MediaQueryList', 'removeListener', 1, 0, constructor=False)
        return self.fn_removeEventListener("change", callback)

__all__ = ["MediaQueryList", "evaluate"]
