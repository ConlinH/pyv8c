"""Font measurement and layout facade for DOM geometry.

Style calculation is provided by :mod:`web.css.cascade`.

Boxes are the *border* boxes in viewport coordinates, which is what
``getBoundingClientRect`` reports and what the hit test walks.  Margins place a box
inside its containing block, ``position: relative`` slides it afterwards, and
``position: absolute`` takes it out of flow and measures it against the padding box
of the nearest positioned ancestor.
"""
import re
import math
import os
from functools import lru_cache
from dataclasses import dataclass, field

from ...foundation import text as text_engine
from ...foundation.environment import environment_of
from ...foundation.font_sizes import font_size_term as _font_size_term
from ...foundation.system_fonts import file_for, normalize, table_of
from .character_data import Text
from ..css.cascade import _cascade, _number, _px, _root_font_size, computed_style, document_rules, rules_for
from .element import Element


#: What CSS's generic families mean here: the platform's own default for each, in the
#: order a name would be tried, because a page asking for ``sans-serif`` is asking for
#: whatever this browser would have used.
_GENERIC_FAMILIES = {
    "serif": ("Times New Roman", "Times", "Liberation Serif", "DejaVu Serif"),
    "sans-serif": ("Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"),
    "monospace": ("Consolas", "Courier New", "Liberation Mono", "DejaVu Sans Mono"),
    "cursive": ("Comic Sans MS", "Apple Chancery", "DejaVu Sans"),
    "fantasy": ("Impact", "Papyrus", "DejaVu Sans"),
    "system-ui": ("Segoe UI", "Helvetica Neue", "DejaVu Sans"),
    "ui-serif": ("Times New Roman", "Liberation Serif", "DejaVu Serif"),
    "ui-sans-serif": ("Arial", "Liberation Sans", "DejaVu Sans"),
    "ui-monospace": ("Consolas", "Liberation Mono", "DejaVu Sans Mono"),
    "ui-rounded": ("Segoe UI", "DejaVu Sans"),
    "emoji": ("Segoe UI Emoji", "Apple Color Emoji"),
    "math": ("Cambria Math", "DejaVu Sans"),
    "fangsong": ("FangSong", "DejaVu Serif"),
}

#: What a name no face and no table knows falls back to: CSS's standard font, which is the
#: serif family and not the sans-serif one -- Chrome measures such a family with Times.
_DEFAULT_FAMILIES = _GENERIC_FAMILIES["serif"]

#: The families a character the page's own ones do not have is drawn by, in the order this
#: platform would look for them.  A CJK character in an Arial run is the case that matters:
#: without these the character would be measured as a missing-glyph box.
_FALLBACK_FAMILIES = (
    "Times New Roman", "Cambria Math",
    "Microsoft YaHei", "Malgun Gothic", "Meiryo", "SimSun", "MS Gothic",
    "Noto Sans CJK SC", "PingFang SC", "Hiragino Sans", "Apple SD Gothic Neo",
    "Segoe UI Emoji", "DejaVu Sans", "Segoe UI Symbol",
)

#: What ``line-height: normal`` falls back to when no face can be read, and the weight CSS
#: starts calling bold at.
_LINE_HEIGHT_FALLBACK = 1.2
_BOLD_WEIGHT = 600


def _environment_for(element):
    """The environment a box's text is measured in, which a profile may have set."""
    return environment_of(getattr(element, "_services", None))


def _families(style):
    """The family list a style asks for, with any quoting taken off."""
    return [name.strip().strip('"\'')
            for name in str(style.get("font-family", "sans-serif")).split(",")]


def _wants_bold(weight):
    """Whether a ``font-weight`` asks for a bold face."""
    text = str(weight).strip().lower()
    if text in ("bold", "bolder"):
        return True
    if text in ("normal", "lighter", ""):
        return False
    return _number(text, 400.0) >= _BOLD_WEIGHT


def _weight_axis(weight):
    text = str(weight).strip().lower()
    if text in ('bold', 'bolder'):
        return 700.0
    if text == 'lighter':
        return 300.0
    if text in ('', 'normal'):
        return 400.0
    return min(1000.0, max(1.0, _number(text, 400.0)))


def _family_index(table):
    families = {}
    for record in table:
        families.setdefault(normalize(record.family), []).append(record)
    return families


# Check the table object as well as its id: Python may reuse an evicted id.
_FONT_INDEX_CACHE = {}
_FONT_INDEX_LIMIT = 16


def _indexed_font_table(environment):
    """Keep the family index for an immutable font table across layout passes."""
    table = table_of(environment)
    key = id(table)
    cached = _FONT_INDEX_CACHE.get(key)
    if cached is not None and cached[0] is table:
        return cached
    indexed = (table, _family_index(table), {})
    if len(_FONT_INDEX_CACHE) >= _FONT_INDEX_LIMIT:
        _FONT_INDEX_CACHE.pop(next(iter(_FONT_INDEX_CACHE)))
    _FONT_INDEX_CACHE[key] = indexed
    return indexed


def _face_in(table, family, weight, style, families=None):
    """A family's face at one weight and slant, chosen by subfamily; a match beats none."""
    key = normalize(family)
    faces = (families.get(key, ()) if families is not None else
             [record for record in table if normalize(record.family) == key])
    if not faces:
        return None
    slant_wanted = str(style).strip().lower() in ("italic", "oblique")
    bold_wanted = _wants_bold(weight)

    def score(record):
        name = normalize(record.style)
        slant = "italic" in name or "oblique" in name
        heavy = any(word in name for word in ("bold", "black", "heavy"))
        return ((slant == slant_wanted) + (heavy == bold_wanted), len(name))

    return max(faces, key=score)


def _family_candidates(family, environment):
    candidates = _GENERIC_FAMILIES.get(normalize(family), (family,))
    if normalize(family) == 'sans-serif':
        preferred = getattr(getattr(environment, 'fonts', None), 'default_family', None)
        if preferred and normalize(preferred) != 'sans-serif':
            return (preferred, *candidates)
    return candidates


def _face_for(families, environment=None, weight="400", style="normal"):
    """The face a family list resolves to: the first family known here, then the default.

    It is the table ``FontFace`` answers ``local()`` from, so "the page said this font is
    here" and "measuring text works" stay in step.
    """
    table, index, _chains = _indexed_font_table(environment)
    for family in families:
        for name in _family_candidates(family, environment):
            record = _face_in(table, name, weight, style, index)
            if record is not None:
                return record
    preferred = getattr(getattr(environment, 'fonts', None), 'default_family', None)
    for name in ((preferred,) if preferred else ()) + _DEFAULT_FAMILIES:
        record = _face_in(table, name, weight, style, index)
        if record is not None:
            return record
    return None


def _font_size(style):
    """The size a style asks for, absolute by the time the cascade is through with it."""
    return _px(style.get("font-size", "16px"), 16) or 16


def _face_path(record):
    """The file a record's face lives in here, or ``None`` when this machine lacks it."""
    return None if record is None else _cached_face_path(record)


@lru_cache(maxsize=512)
def _cached_face_path(record):
    return file_for(record)


def _face_chain(families, environment, weight, style, *, text_presentation=False):
    """The faces a family list names, then this platform's fallbacks, in tried order."""
    table, index, chains = _indexed_font_table(environment)
    preferred = getattr(getattr(environment, 'fonts', None), 'default_family', None)
    key = (tuple(families), str(weight), str(style), text_presentation,
           normalize(preferred) if preferred else None)
    known = chains.get(key)
    if known is not None:
        return known
    chain = []
    for family in families:
        for name in _family_candidates(family, environment):
            record = _face_in(table, name, weight, style, index)
            if record is not None and record not in chain:
                chain.append(record)
    # A missing named family uses the standard serif face; a real selected
    # family missing one glyph asks the platform fallback chain first.
    preferred = getattr(getattr(environment, 'fonts', None), 'default_family', None)
    default_names = ((preferred,) if preferred else ()) + _DEFAULT_FAMILIES
    fallback_names = (_FALLBACK_FAMILIES[:2] + ((preferred,) if preferred else ())
                      + _FALLBACK_FAMILIES[2:] + default_names if chain
                      else default_names + _FALLBACK_FAMILIES)
    if text_presentation and os.name == "nt":
        # Windows' text-presentation fallback favours monochrome faces. Keep
        # explicitly requested families above this platform fallback list.
        priority = ((preferred,) if preferred else ()) + ("Segoe UI Symbol",)
        fallback_names = priority + tuple(name for name in fallback_names if name not in priority)
    for name in fallback_names:
        record = _face_in(table, name, weight, style, index)
        if record is not None and record not in chain:
            chain.append(record)
    found = tuple(chain)
    if len(chains) >= 128:
        chains.pop(next(iter(chains)))
    chains[key] = found
    return found


def _shaped(text, style, environment=None, *, layout_units=False, size=None):
    """``(run, record)`` for one string; ``None`` when no face here has a file for it."""
    chain = _face_chain(_families(style), environment, style.get("font-weight", "400"),
                        style.get("font-style", "normal"))
    faces = []
    for record in chain:
        path = _face_path(record)
        if path is not None:
            faces.append((path, getattr(record, "index", 0)))
    if not faces:
        return None, (chain[0] if chain else None)
    text_faces = None
    if '\ufe0e' in text:
        text_chain = _face_chain(_families(style), environment, style.get("font-weight", "400"),
                                 style.get("font-style", "normal"), text_presentation=True)
        text_faces = [(_face_path(record), getattr(record, "index", 0)) for record in text_chain
                      if _face_path(record) is not None]
    run = text_engine.shape_selection(text, faces, size=_font_size(style) if size is None else size,
                                      weight=_weight_axis(style.get('font-weight', '400')),
                                      layout_units=layout_units, text_presentation_faces=text_faces)
    return (run if run.width else None), chain[0]


def _run_metrics(run, style):
    """The extents a shaped run asks a line box for: those of the tallest face it used."""
    best = None
    for path, index in getattr(run, "faces", ()):
        found = text_engine.metrics(path, index=index, size=_font_size(style))
        if found.height and (best is None or found.height > best.height):
            best = found
    return best


def _spacing(text, style, size):
    """What letter and word spacing add: every character, and every space.

    Chrome reads 25.797 + 3 x 2 = 31.797 for ``abc`` at 2px, and the flow charges the same
    way line by line, so a line's width is what a box reports.
    """
    extra = 0.0
    letter = str(style.get("letter-spacing", "normal")).strip().lower()
    if letter not in ("", "normal"):
        extra += len(text) * _px(letter, size)
    word = str(style.get("word-spacing", "normal")).strip().lower()
    if word not in ("", "normal"):
        extra += text.count(" ") * _px(word, size)
    return extra


def text_width(text, style, environment=None):
    """The advance width of *text* in *style*: shaped, then spaced the way CSS spaces it.

    A box and a canvas both measure through here, so ``measureText`` and a box agree by
    construction; text no face can be found for is estimated, which is the only fallback.
    """
    text = "" if text is None else str(text)
    size = _font_size(style)
    run, _record = _shaped(text, style, environment)
    width = run.width if run is not None else text_engine.estimate(text, size)
    return width + _spacing(text, style, size)


@dataclass(frozen=True)
class _LineStyle:
    """One line box: how tall it is, and where its baseline sits inside it."""

    height: float
    ascent: float
    descent: float


def _font_metrics(style, environment=None):
    """The face's own extents for a style, or ``None`` when no face can be read.

    A catalog record whose file is not on this machine is not a face. The Windows
    profile names Segoe UI and Noto Sans SC; measurement uses the first face in
    the same chain that actually opens.
    """
    chain = _face_chain(_families(style), environment, style.get("font-weight", "400"),
                        style.get("font-style", "normal"))
    size = _font_size(style)
    for record in chain:
        path = _face_path(record)
        if path is None:
            continue
        found = text_engine.metrics(path, index=getattr(record, "index", 0), size=size)
        if found.height:
            return found
    return None


def _line_style(style, environment=None, root=16.0, found=None):
    """The line box a style asks for, and the baseline inside it.

    ``normal`` is the face's extents, a unitless number is a multiple of the font size, a
    percentage is one of it too, and a length is itself; the leading splits evenly above and
    below, which is where the baseline comes from.  *found* is the face the run really used.
    """
    size = _font_size(style)
    own = _font_metrics(style, environment)
    if found is None or (own is not None and own.height > found.height):
        # The font strut remains on the line when glyphs come from a shorter
        # fallback face (for example emoji inside a taller document font).
        found = own
    ascent = found.ascent if found else size * 0.8
    descent = found.descent if found else size * 0.2
    gap = found.line_gap if found else 0.0
    content = ascent + descent + gap
    declared = str(style.get("line-height", "normal")).strip().lower()
    value = None
    if declared in ("", "normal"):
        value = content
    elif re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)", declared):
        value = float(declared) * size
    else:
        value = _font_size_term(declared, size, root)
    if value is None or value <= 0:
        # A line height CSS could not parse is a declaration it dropped, so what is left is
        # the face's own spacing -- and only a face that cannot be read falls back further.
        height = content if content > 0 else size * _LINE_HEIGHT_FALLBACK
    else:
        height = value
    baseline = ascent + (height - content) / 2.0
    return _LineStyle(height, baseline, height - baseline)


@dataclass
class Box:
    """One element's boxes, in viewport coordinates.

    ``left``/``top``/``width``/``height`` are the border box, and the padding and
    border widths say where the padding box and content box sit inside it.
    """
    width: float = 0
    height: float = 0
    left: float = 0
    top: float = 0
    padding: dict = field(default_factory=dict)
    border: dict = field(default_factory=dict)
    margin: dict = field(default_factory=dict)
    #: How far the content reaches past the border box's left and top edges.
    overflow_right: float = 0.0
    overflow_bottom: float = 0.0

    @property
    def padding_box_left(self): return self.left + self.border.get("left", 0)
    @property
    def padding_box_top(self): return self.top + self.border.get("top", 0)
    @property
    def padding_box_width(self):
        return self.width - self.border.get("left", 0) - self.border.get("right", 0)
    @property
    def padding_box_height(self):
        return self.height - self.border.get("top", 0) - self.border.get("bottom", 0)


#: The four sides a shorthand covers, clockwise from the top.
_SIDES = ("top", "right", "bottom", "left")


#: What an inline-level box takes part in without leaving the line it is on.
_INLINE = ("inline", "inline-block", "inline-flex", "inline-grid", "inline-table", "ruby")


#: The default object size a replaced element falls back to when its content has no
#: intrinsic size of its own, which is what an empty iframe, canvas or video measures.
_DEFAULT_OBJECT_SIZE = {
    "iframe": (300.0, 150.0), "canvas": (300.0, 150.0), "video": (300.0, 150.0),
    "object": (300.0, 150.0), "svg": (300.0, 150.0),
}


def _image_intrinsic_size(element):
    """An image's decoded size, or ``None`` for every other replaced element."""
    if str(getattr(element, "localName", "")).lower() == "img":
        return element._natural_size()
    return None


def _expand(value):
    """The four lengths a CSS shorthand holds."""
    parts = str(value).split()
    if not parts:
        return ["0"] * 4
    if len(parts) == 1:
        return parts * 4
    if len(parts) == 2:
        return [parts[0], parts[1], parts[0], parts[1]]
    if len(parts) == 3:
        return [parts[0], parts[1], parts[2], parts[1]]
    return parts[:4]


def _sides(style, name, reference, default="0"):
    """Per-side lengths, the shorthand standing in for whatever is not set."""
    values = _expand(style.get(name, default))
    if name == 'border-width':
        result = {}
        for index, side in enumerate(_SIDES):
            border_style = style.get(f'border-{side}-style', style.get('border-style'))
            result[side] = (0.0 if border_style in ('none', 'hidden') else
                            _px(style.get(f'border-{side}-width', values[index]), reference))
        return result
    return {side: _px(style.get(f"{name}-{side}", values[index]), reference)
            for index, side in enumerate(_SIDES)}


def _auto(value):
    return value is None or str(value).strip().lower() in ("", "auto")


def _margin_is_auto(style, side):
    """Whether one horizontal margin asked for ``auto``: its own, else the shorthand."""
    declared = style.get(f"margin-{side}")
    if declared is None:
        declared = _expand(style.get("margin", "0"))[_SIDES.index(side)]
    return _auto(declared)


def _style_of(element, rules, cache=None):
    if getattr(cache, 'scoped_rules', False):
        rules = rules_for(element, rules)
    return _cascade(element, rules, cache)


@dataclass(frozen=True)
class _Containing:
    """The block a box is measured against, in viewport coordinates."""
    left: float = 0.0
    top: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass(frozen=True)
class _Walk:
    """One layout pass: the rules to cascade, where boxes go, the viewport, and what is filled
    in as it runs -- ``baselines`` per element, ``overflows`` for ``scrollWidth``.
    """
    rules: tuple
    collected: dict | None
    viewport: tuple
    baselines: dict = field(default_factory=dict)
    overflows: dict = field(default_factory=dict)
    styles: dict = field(default_factory=dict)


class _StyleCache(dict):
    """Per-layout computed styles and inline run widths, discarded together."""

    def __init__(self, scoped_rules=True):
        super().__init__()
        self.scoped_rules = scoped_rules
        self.inline_text_widths = {}
        self.inline_siblings = {}


def _viewport_width(element):
    services = getattr(element, '_services', None)
    document_services = getattr(services, 'document_services', None)
    window = getattr(document_services, 'window', None)
    if window is not None:
        return float(window.innerWidth), float(window.innerHeight)
    realm = getattr(getattr(element, "_services", None), "realm_services", None)
    viewport = getattr(getattr(realm, "environment", None), "viewport", None)
    width = getattr(viewport, "width", None) or 1280
    height = getattr(viewport, "height", None) or 720
    return float(width), float(height)


def _offset(style, forward, backward, size):
    """A relative offset: the first side set wins, and ``auto`` means none."""
    if not _auto(style.get(forward)):
        return _px(style[forward], size)
    if not _auto(style.get(backward)):
        return -_px(style[backward], size)
    return 0.0


def _place(element, style, walk, containing, positioned):
    """Lay *element* out inside *containing* and answer its border box.

    *positioned* is the padding box of the nearest positioned ancestor, which is what
    an absolutely positioned box measures against; ``None`` means the viewport, the
    initial containing block of a page.
    """
    def record(box):
        if walk.collected is not None:
            walk.collected[element] = box
        return box

    if style.get("display") == "none" or not getattr(element, "isConnected", False):
        return record(Box())

    padding = _sides(style, "padding", containing.width)
    border = _sides(style, "border-width", containing.width)
    margin = _sides(style, "margin", containing.width)
    horizontal = padding["left"] + padding["right"] + border["left"] + border["right"]
    vertical = padding["top"] + padding["bottom"] + border["top"] + border["bottom"]

    declared_width = style.get("width", "auto")
    declared_height = style.get("height", "auto")
    position = style.get("position", "static")
    available = max(0.0, containing.width - margin["left"] - margin["right"] - horizontal)
    image_size = _image_intrinsic_size(element)
    if image_size is not None:
        # HTML dimensions are presentational hints, below the CSS cascade.
        if _auto(declared_width) and "width" in element.attributes:
            match = re.match(r"\s*(\d+)", element.attributes["width"])
            if match:
                declared_width = match[1] + "px"
        if _auto(declared_height) and "height" in element.attributes:
            match = re.match(r"\s*(\d+)", element.attributes["height"])
            if match:
                declared_height = match[1] + "px"
    default_size = _DEFAULT_OBJECT_SIZE.get(str(getattr(element, "localName", "")).lower())
    if str(getattr(element, "localName", "")).lower() == "canvas":
        default_size = (element.get_width(), element.get_height())
    elif (str(getattr(element, "localName", "")).lower() == "input"
          and element.get_type() in ("button", "submit", "reset")):
        value = element.get_value()
        environment = _environment_for(element)
        if (element.get_type() in ("submit", "reset") and
                "value" not in element.attributes and not getattr(element, "_dirty_value", False)):
            label_key = f"form_controls.{element.get_type()}_label"
            value = (getattr(environment, label_key) if environment is not None
                     else {"submit": "Submit", "reset": "Reset"}[element.get_type()])
        font_size = style.get('font-size', '16px')
        size = _number(font_size, 16) if str(font_size).strip().lower().endswith('px') else _font_size(style)
        run, _ = _shaped(value, style, environment, size=size)
        metrics = _run_metrics(run, style) if run is not None else None
        advance = (run.width if run is not None else text_engine.estimate(value, size))
        advance += _spacing(value, style, size)
        default_size = (math.ceil(advance * 64) / 64,
                        _line_style(style, environment, found=metrics).height)
    elif str(getattr(element, "localName", "")).lower() == "input":
        input_type = element.get_type()
        if input_type == "color":
            default_size = (44.0, 23.0)
        elif input_type in ("checkbox", "radio"):
            default_size = ((0.0, 0.0) if style.get("appearance") == "none"
                            else (13.0, 13.0))
        elif input_type == "range":
            default_size = (129.0, 16.0)
        elif input_type == "file":
            environment = _environment_for(element)
            button_label = (getattr(environment, "form_controls.file_button_label")
                            if environment is not None else "Choose File")
            empty_label = (getattr(environment, "form_controls.file_empty_label")
                           if environment is not None else "No file chosen")
            run, _ = _shaped(button_label, style, environment)
            metrics = _run_metrics(run, style) if run is not None else None
            button_height = _line_style(style, environment, found=metrics).height + 6.0
            content_width = max(34 * text_width("0", style, environment),
                                text_width(button_label, style, environment) + 20
                                + text_width(empty_label, style, environment))
            default_size = (math.ceil(content_width), button_height)
        elif input_type == "time":
            environment = _environment_for(element)
            time_style = dict(style)
            if str(style.get('font-family', '')).strip().lower() == 'monospace' and environment is not None:
                time_style['font-family'] = getattr(environment, 'form_controls.time_font_family')
            seconds = milliseconds = False
            for candidate in (element.get_value(), element._attr('min')):
                match = re.fullmatch(r'\d{2}:\d{2}(?::(\d{2})(?:\.(\d{1,3}))?)?', str(candidate))
                if match:
                    fraction = match.group(2) or ''
                    milliseconds |= bool(fraction and int(fraction))
                    seconds |= bool((match.group(1) and int(match.group(1))) or milliseconds)
            step = element._attr('step')
            if step and step.lower() != 'any':
                try:
                    number = float(step)
                    if math.isfinite(number) and number > 0:
                        seconds |= number % 60 != 0
                        milliseconds |= not number.is_integer()
                except ValueError:
                    pass
            seconds |= milliseconds
            size = _font_size(time_style)
            chain = _face_chain(_families(time_style), environment,
                                time_style.get('font-weight', '400'),
                                time_style.get('font-style', 'normal'))
            font_record = chain[0] if chain else None
            path = _face_path(font_record) if font_record is not None else None
            bitmap = bool(path and text_engine.bitmap_strike_applies(
                path, getattr(font_record, 'index', 0), size, '0123456789:.'))
            digit = text_width('0', time_style, environment)
            colon = text_width(':', time_style, environment)
            if bitmap:
                digit = math.floor(digit + 0.5)
                colon = math.floor(colon + 0.5)
            fields = 2 + int(seconds)
            editor_width = (2 * digit + 2) * fields + colon * (fields - 1)
            if milliseconds:
                editor_width += 3 * digit + 2 + colon
            editor_width = math.ceil(editor_width)
            font_size = _font_size(style)
            picker = math.ceil(font_size * 1.05) + 6
            default_size = (editor_width + 8 + picker,
                            max(_line_style(time_style, environment).height + 2, picker))
        elif input_type in ("text", "search", "password", "tel", "email", "url"):
            environment = _environment_for(element)
            chain = _face_chain(_families(style), environment, style.get("font-weight", "400"),
                                style.get("font-style", "normal"))
            font_record = chain[0] if chain else None
            path = _face_path(font_record) if font_record is not None else None
            width = (text_engine.text_control_width(path, getattr(font_record, "index", 0),
                                                    _font_size(style), element.get_size())
                     if path else None)
            if width is not None:
                default_size = (width, _line_style(style, environment).height)
    if image_size is not None:
        default_size = image_size
    if _auto(declared_width):
        if default_size is not None:
            # A replaced element with no intrinsic size of its own takes the default
            # object size rather than shrinking to a content it does not have.
            content_width = default_size[0]
            if image_size and all(image_size) and not _auto(declared_height):
                reference = (positioned.height if positioned is not None and
                             style.get("position") == "absolute" else containing.height)
                content_width = _px(declared_height, reference) * image_size[0] / image_size[1]
        else:
            joined_width = _inline_text_width(element, style, walk.rules, walk.styles)
            # An inline-level box shrinks around its content and an empty one stays
            # empty; a block-level box fills what is left of its containing block.
            if joined_width is not None:
                content_width = math.ceil(joined_width * 64) / 64
            elif position in ("absolute", "fixed"):
                content_width = _shrink_to_fit(element, style, walk.rules, available,
                                               cache=walk.styles)
            elif (style.get("display", "block") in _INLINE
                  and style.get("white-space") in ("nowrap", "pre")):
                # Inline boxes retain their max-content width when nowrap text
                # extends past the containing block.
                content_width = math.ceil(_content_widths(
                    element, style, walk.rules, cache=walk.styles)[1] * 64) / 64
            elif style.get("display", "block") in _INLINE:
                content_width = _shrink_to_fit(element, style, walk.rules, available,
                                               cache=walk.styles)
            else:
                content_width = available
    else:
        width = (_number(declared_width) * _root_font_size(element, walk.styles)
                 if str(declared_width).strip().lower().endswith('rem')
                 else _px(declared_width, containing.width))
        content_width = max(0.0, width - horizontal
                            if style.get("box-sizing", "content-box") == "border-box"
                            else width)
    content_width = _constrain_width(content_width, style, containing.width, horizontal)
    border_width = content_width + horizontal
    if not _auto(declared_width) and style.get("display", "block") not in _INLINE:
        # ``auto`` shares the free space; a side with a length keeps it.
        auto_sides = [side for side in ("left", "right") if _margin_is_auto(style, side)]
        if auto_sides:
            free = max(0.0, containing.width - border_width - sum(
                margin[side] for side in ("left", "right") if side not in auto_sides))
            for side in auto_sides:
                margin[side] = free / len(auto_sides)
            left = containing.left + margin["left"]

    left = containing.left + margin["left"]
    top = containing.top + margin["top"]
    if not _auto(declared_height):
        reference = (positioned.height if positioned is not None and
                     style.get("position") == "absolute" else containing.height)
        height = _px(declared_height, reference)
        content_height = max(0.0, height - vertical
                             if style.get("box-sizing", "content-box") == "border-box"
                             else height)
    elif image_size and all(image_size):
        content_height = content_width * image_size[1] / image_size[0]
    elif default_size is not None:
        content_height = default_size[1]
    else:
        content_height = None
    auto_height = content_height is None
    if position in ("absolute", "fixed"):
        # A fixed box measures against the viewport, an absolute one against its ancestor.
        block = (_Containing(0.0, 0.0, *walk.viewport) if position == "fixed"
                 else positioned or _Containing(0.0, 0.0, *walk.viewport))
        if not _auto(style.get("left")):
            left = block.left + _px(style["left"], block.width) + margin["left"]
        elif not _auto(style.get("right")):
            left = (block.left + block.width - _px(style["right"], block.width)
                    - margin["right"] - border_width)
        if not _auto(style.get("top")):
            top = block.top + _px(style["top"], block.height) + margin["top"]
        elif not _auto(style.get("bottom")) and content_height is not None:
            top = (block.top + block.height - _px(style["bottom"], block.height)
                   - margin["bottom"] - content_height - vertical)

    content_left = left + border["left"] + padding["left"]
    content_top = top + border["top"] + padding["top"]
    inner = _Containing(content_left, content_top, content_width,
                        content_height if content_height is not None else 0.0)
    # A positioned box becomes the containing block for its own absolute descendants,
    # measured on the padding box, which is why the offsets exclude the border.
    child_positioned = positioned
    if position != "static":
        child_positioned = _Containing(
            left + border["left"], top + border["top"],
            max(0.0, border_width - horizontal),
            content_height + vertical if content_height is not None else 0.0)
    used_height = _children(element, style, walk, inner, child_positioned)
    recorded = walk.baselines.get(element)
    if recorded is not None:
        # The flow measures from the content's top; a box on a line from its border box.
        walk.baselines[element] = recorded + (content_top - top)
    if content_height is None:
        content_height = used_height
    border_height = content_height + vertical

    if position != 'static' and auto_height and walk.collected is not None:
        # The padding box's height becomes known only after in-flow children.
        # Resolve its absolutely positioned descendants again against that height.
        final_block = _Containing(left + border['left'], top + border['top'],
                                  max(0.0, border_width - horizontal),
                                  content_height + padding['top'] + padding['bottom'])
        pending = list(_child_elements(element))
        while pending:
            child = pending.pop()
            if not isinstance(child, Element):
                continue
            child_style = _style_of(child, walk.rules, walk.styles)
            child_position = child_style.get('position', 'static')
            if child_position == 'absolute':
                old = walk.collected.get(child)
                if old is not None:
                    _place(child, child_style, walk,
                           _Containing(old.left, old.top, final_block.width, final_block.height),
                           final_block)
            elif child_position == 'static':
                pending.extend(_child_elements(child))

    if position == "relative":
        offset_x = _offset(style, "left", "right", containing.width)
        offset_y = _offset(style, "top", "bottom", containing.height)
        if offset_x or offset_y:
            left += offset_x
            top += offset_y
            _shift(element, walk, offset_x, offset_y)

    overflow = walk.overflows.get(element, (0.0, 0.0))
    return record(Box(width=border_width, height=border_height, left=left, top=top,
                      padding=padding, border=border, margin=margin,
                      overflow_right=(border["left"] + padding["left"]
                                      + max(content_width, overflow[0])),
                      overflow_bottom=(border["top"] + padding["top"]
                                       + max(content_height, overflow[1]))))


def _shift(element, walk, x, y):
    """Move everything the subtree recorded, for a relative offset applied afterwards."""
    if walk.collected is None or (not x and not y):
        return
    pending = [element]
    while pending:
        node = pending.pop()
        box = walk.collected.get(node)
        if box is not None:
            box.left += x
            box.top += y
        pending.extend(child for child in _child_elements(node)
                       if isinstance(child, Element))


def _children(element, style, walk, containing, positioned):
    """Lay out *element*'s children and answer the content height they take."""
    display = style.get("display", "block")
    if display in ("flex", "inline-flex"):
        return _flex_children(element, style, walk, containing, positioned)
    if display in ("grid", "inline-grid"):
        return _grid_children(element, style, walk, containing, positioned)
    return _flow_children(element, style, walk, containing, positioned)


def _child_elements(element):
    root = getattr(element, '_shadow_root', None)
    parent = root if root is not None else element
    return [child for child in getattr(parent, "_children", ())
            if isinstance(child, (Element, Text))]


def _text_points(content, white_space, word_break, overflow_wrap):
    """``(points, loose)`` for the line ends of *content*.

    ``points`` are UAX#14's opportunities, which ``nowrap`` and ``pre`` cut back to the
    newlines alone.  ``loose`` are grapheme boundaries, offered only on an empty line, which
    is where ``break-word`` cuts a word that does not fit even on one.
    """
    points = text_engine.breaks(content)
    if white_space in ("nowrap", "pre"):
        return tuple(point for point in points if point.forced), ()
    if word_break == "break-all" or overflow_wrap == "anywhere":
        loose = tuple(text_engine.BreakPoint(offset) for offset in text_engine.graphemes(content))
        return points + loose, ()
    if overflow_wrap == "break-word":
        return points, tuple(text_engine.BreakPoint(offset)
                             for offset in text_engine.graphemes(content))
    return points, ()


def _collapse(content, white_space):
    """A text node's content after ``white-space``: pre keeps all, pre-line keeps newlines."""
    if white_space in ("pre", "pre-wrap"):
        return content
    if white_space == "pre-line":
        return re.sub(r"[^\S\n]+", " ", content)
    return re.sub(r"\s+", " ", content)


def _box_content_widths(element, style, rules, depth, cache=None):
    """The content widths one child asks for: what it declares, its object size, or its content."""
    declared = style.get("width", "auto")
    if not _auto(declared):
        return _px(declared, 0.0), _px(declared, 0.0)
    joined = _inline_text_width(element, style, rules, cache)
    if joined is not None:
        return joined, joined
    intrinsic = _image_intrinsic_size(element)
    if intrinsic is None:
        intrinsic = _DEFAULT_OBJECT_SIZE.get(str(getattr(element, "localName", "")).lower())
    if intrinsic is not None:
        return intrinsic[0], intrinsic[0]
    return _content_widths(element, style, rules, depth + 1, cache)


def _inline_text_width(element, style, rules, cache):
    """Measure text across compatible adjacent inline elements as one shaped run.

    Browsers preserve kerning across element boundaries when those boundaries do
    not change the font.  Measuring each child on its own loses pairs such as
    ``<span>υ</span><span>τ</span>``.
    """
    if style.get("display") != "inline" or not _auto(style.get("width")):
        return None
    widths = getattr(cache, "inline_text_widths", None)
    if widths is not None and element in widths:
        return widths[element]
    parent = getattr(element, "parentElement", None)
    if parent is None:
        return None
    groups = getattr(cache, 'inline_siblings', None)
    if groups is not None:
        if parent not in groups:
            children = _child_elements(parent)
            groups[parent] = children, {child: index for index, child in enumerate(children)}
        siblings, indices = groups[parent]
        index = indices.get(element)
        if index is None:
            return None
    else:
        siblings = _child_elements(parent)
        try:
            index = siblings.index(element)
        except ValueError:
            return None
    font_keys = ("font-family", "font-size", "font-weight", "font-style",
                 "font-stretch", "font-kerning", "font-feature-settings",
                 "font-variation-settings", "letter-spacing", "word-spacing",
                 "white-space", "text-transform", "direction", "unicode-bidi")
    signature = tuple(style.get(key) for key in font_keys)

    def segment(node):
        if not isinstance(node, Element) or len(getattr(node, "_children", ())) != 1:
            return None
        child = node._children[0]
        if not isinstance(child, Text):
            return None
        values = _style_of(node, rules, cache)
        if (values.get("display") != "inline" or not _auto(values.get("width"))
                or tuple(values.get(key) for key in font_keys) != signature):
            return None
        for name in ("margin", "padding", "border-width"):
            if any(_sides(values, name, 0.0).values()):
                return None
        return child.get_textContent() or ""

    own = segment(element)
    if not own:
        if widths is not None:
            widths[element] = None
        return None
    start = index
    while start and segment(siblings[start - 1]):
        start -= 1
    end = index + 1
    while end < len(siblings) and segment(siblings[end]):
        end += 1
    if end - start < 2:
        if widths is not None:
            widths[element] = None
        return None
    parts = [segment(node) for node in siblings[start:end]]
    joined = "".join(parts)
    environment = _environment_for(element)
    run, _ = _shaped(joined, style, environment)
    if run is None:
        return None
    fixed_run, _ = _shaped(joined, style, environment, layout_units=True)
    offset = 0
    for node, part in zip(siblings[start:end], parts):
        advance = run.advance_between(offset, offset + len(part))
        fixed_advance = (fixed_run.advance_between(offset, offset + len(part))
                         if fixed_run is not None else advance)
        # HarfBuzz's 26.6 layout advance can differ slightly from its
        # unrounded font-unit advance.  Preserve the snapped value for a
        # kerned glyph when the difference is below a quarter layout unit.
        isolated, _ = _shaped(part, style, environment)
        kerned = isolated is not None and isolated.width - advance > 1 / 64
        if kerned and abs(fixed_advance - advance) < 0.25 / 64:
            advance = fixed_advance
        result = math.ceil((advance + _spacing(part, style, _font_size(style))) * 64) / 64
        if widths is not None:
            widths[node] = result
        if node is element:
            own_width = result
        offset += len(part)
    return own_width


def _content_widths(element, style, rules, depth=0, cache=None):
    """``(min, max)`` content widths: the widest unbreakable run, and one line's worth.

    Inline-level pieces have no opportunity between them, so their minima add up; a
    block-level child stands on its own line and asks for its own content instead.
    """
    if depth > 32:
        return 0.0, 0.0
    if style.get('display') in ('grid', 'inline-grid'):
        placement = _implicit_grid_items(element, rules, cache)
        if placement is not None:
            minima, maxima = _implicit_grid_widths(placement, rules, depth, cache)
            return sum(minima), sum(maxima)
    white_space = style.get("white-space", "normal")
    word_break = str(style.get("word-break", "normal")).strip().lower()
    overflow_wrap = str(style.get("overflow-wrap", "normal")).strip().lower()
    environment = _environment_for(element)
    inline_min = inline_max = 0.0
    widest_min = widest_max = 0.0
    children = _child_elements(element)
    for index, child in enumerate(children):
        if isinstance(child, Text):
            content = _collapse(child.get_textContent() or "", white_space)
            if not content.strip():
                # Inter-element whitespace occupies an advance on the line.
                # Dropping it from max-content makes nested inline boxes too
                # narrow, even though flow lays the space out later.
                if inline_max and any(
                    (isinstance(later, Text) and (later.get_textContent() or '').strip())
                    or (isinstance(later, Element)
                        and _style_of(later, rules, cache).get('display') in _INLINE)
                    for later in children[index + 1:]
                ):
                    advance = math.ceil(text_width(content, style, environment) * 64) / 64
                    inline_max += advance
                    if white_space in ('nowrap', 'pre'):
                        inline_min += advance
                continue
            run, _record = _shaped(content, style, environment)
            if run is None:
                # No face to shape with: the estimate stands in, unbroken.
                width = text_engine.estimate(content, _font_size(style))
                inline_min += width
                inline_max += width
                continue
            inline_max += run.width + _spacing(content, style, _font_size(style))
            points, _loose = _text_points(content, white_space, word_break, overflow_wrap)
            pieces = text_engine.wrap(run, 0.0, points)
            inline_min += max((piece.advance for piece in pieces), default=run.width)
            continue
        child_style = _style_of(child, rules, cache)
        if (child_style.get("display") == "none"
                or child_style.get('position') in ('absolute', 'fixed')):
            continue
        edges = {side: sum(_sides(child_style, name, 0.0)[side]
                           for name in ("margin", "padding", "border-width"))
                 for side in _SIDES}
        extra = edges["left"] + edges["right"]
        minimum, maximum = _box_content_widths(child, child_style, rules, depth, cache)
        horizontal = sum(_sides(child_style, name, 0.0)[side]
                         for name in ("padding", "border-width") for side in ("left", "right"))
        if not _auto(child_style.get("width")) and child_style.get("box-sizing") == "border-box":
            minimum, maximum = max(0, minimum-horizontal), max(0, maximum-horizontal)
        minimum, maximum = (_constrain_width(value, child_style, 0.0, horizontal)
                            for value in (minimum, maximum))
        if child_style.get("display", "block") in _INLINE:
            inline_min += minimum + extra
            inline_max += maximum + extra
        else:
            widest_min = max(widest_min, minimum + extra)
            widest_max = max(widest_max, maximum + extra)
    return max(inline_min, widest_min), max(inline_max, widest_max)


def _shrink_to_fit(element, style, rules, available, depth=0, cache=None):
    """CSS's ``min(max(min_content, available), max_content)``."""
    minimum, maximum = _content_widths(element, style, rules, depth, cache)
    used = min(max(minimum, available), maximum)
    # Text shaping keeps fractional advances, while Blink's used box sizes sit
    # on the 1/64px LayoutUnit grid and round an intrinsic width upward.
    return math.ceil(used * 64) / 64


class _Line:
    """A line box while it is being filled: what is on it, and how far the text has run."""

    __slots__ = ("width", "ascent", "descent", "items", "pending")

    def __init__(self):
        self.width = 0.0
        self.ascent = 0.0
        self.descent = 0.0
        self.items: list = []
        #: Collapsible spaces before the next opportunity: they count once something follows.
        self.pending = 0.0

    @property
    def started(self):
        return bool(self.items) or self.width > 0 or self.ascent > 0

    @property
    def height(self):
        return self.ascent + self.descent

    def add(self, width, ascent, descent):
        """Put a piece on the line: its width, and how far it reaches above and below."""
        self.width += self.pending + width
        self.pending = 0.0
        self.ascent = max(self.ascent, ascent)
        self.descent = max(self.descent, descent)

    def hang(self, width):
        """Leave spaces at the end of the line: they count only if something follows."""
        self.pending += width


def _flow_children(element, style, walk, containing, positioned):
    """Block boxes stacked, and inline-level content wrapped into line boxes.

    A line ends where the next piece would not fit or where the text forces it, and is as
    tall as the tallest thing on it; its baseline follows from its items, which is what puts
    an inline block and the text beside it on one line of type.
    """
    environment = _environment_for(element)
    root = _root_font_size(element, walk.styles)
    white_space = style.get("white-space", "normal")
    word_break = str(style.get("word-break", "normal")).strip().lower()
    overflow_wrap = str(style.get("overflow-wrap", "normal")).strip().lower()
    text_size = _font_size(style)
    cursor = 0.0
    line = _Line()
    first_baseline = None
    widest = 0.0
    leftmost = 0.0
    deepest = 0.0

    def flush():
        """End the line: put its items where its baseline and ``text-align`` say they go."""
        nonlocal cursor, line, first_baseline, widest
        if not line.started:
            return
        if first_baseline is None:
            first_baseline = cursor + line.ascent
        baseline = containing.top + cursor + line.ascent
        free = max(0.0, containing.width - line.width)
        align = str(style.get("text-align", "start")).strip().lower()
        shift = free / 2.0 if align == "center" else free if align in ("right", "end") else 0.0
        for child, box, item_ascent in line.items:
            _shift(child, walk, shift, baseline - item_ascent - box.top)
        widest = max(widest, line.width + shift)
        cursor += line.height
        line = _Line()

    def break_line(br_style=None):
        """End the current line; a break with nothing before it leaves an empty one behind."""
        nonlocal cursor
        if line.started:
            flush()
        else:
            cursor += _line_style(br_style or style, environment, root).height

    def add_text(content, text_style, points, loose=()):
        """Wrap one text node into the current line, ending lines as they fill."""
        nonlocal line
        if not content:
            return
        run, _record = _shaped(content, text_style, environment)
        line_style = _line_style(text_style, environment, root,
                                 None if run is None else _run_metrics(run, text_style))
        if run is None:
            # No face to shape with: the estimate stands in, unbroken.
            if content.strip() or line.started:
                line.add(text_engine.estimate(content, text_size),
                         line_style.ascent, line_style.descent)
            return
        start = 0
        while start < len(content):
            available = max(0.0, containing.width - line.width)
            span = text_engine.next_line(run, start, available, points)
            if span is not None and span.advance > available and not line.started and loose:
                # Only a word that does not fit on a line of its own may be cut inside.
                span = text_engine.next_line(run, start, available, points + loose)
            if span is None:
                break
            if span.end == span.start:
                if "\n" in content[span.start:span.resume]:
                    # A line the text asked for and left empty, which ``pre`` keeps.
                    break_line(text_style)
                elif line.started:
                    # Spaces only: they widen a line that has begun, and never make one.
                    line.add(run.advance_between(span.start, span.resume), 0.0, 0.0)
                start = span.resume
                continue
            piece = content[span.start:span.end]
            last = span.end >= len(content)
            advance = span.advance + _spacing(piece, text_style, text_size)
            if (white_space not in ('nowrap', 'pre') and line.started
                    and span.start == start and advance > available):
                # No room for this piece here: try it on a line of its own.
                flush()
                continue
            line.add(advance, line_style.ascent, line_style.descent)
            if last:
                break
            rest = content[span.resume:]
            if not rest.strip() and "\n" not in rest:
                # Only spaces are left: they hang here and the line goes on.
                line.hang(run.advance_between(span.end, span.resume))
                break
            flush()
            start = span.resume if span.resume > span.start else span.end

    for child in _child_elements(element):
        if isinstance(child, Text):
            content = _collapse(child.get_textContent() or "", white_space)
            if not content.strip() and not line.started:
                # Whitespace with nothing before it makes no line box.
                continue
            points, loose = _text_points(content, white_space, word_break, overflow_wrap)
            add_text(content, style, points, loose)
            continue
        if str(getattr(child, "localName", "")).lower() == "br":
            # A line break of its own: it ends the current line, and makes one when empty.
            break_line()
            continue
        child_style = _style_of(child, walk.rules, walk.styles)
        if child_style.get("display") == "none":
            continue
        if child_style.get("position", "static") in ("absolute", "fixed"):
            # Out of flow: it keeps the cursor as its static position and leaves it alone.
            flush()
            _place(child, child_style, walk,
                   _Containing(containing.left, containing.top + cursor, containing.width,
                               containing.height),
                   positioned)
            continue
        child_margin = _sides(child_style, "margin", containing.width)
        if child_style.get("display", "block") in _INLINE:
            placed = _place(child, child_style, walk,
                            _Containing(containing.left + line.width, containing.top + cursor,
                                        containing.width, containing.height),
                            positioned)
            outer = placed.width + child_margin["left"] + child_margin["right"]
            if (white_space not in ('nowrap', 'pre') and line.started and line.width
                    and line.width + outer > containing.width):
                # It does not fit after what is on the line, so the line ends first.
                flush()
                placed = _place(child, child_style, walk,
                                _Containing(containing.left, containing.top + cursor,
                                            containing.width, containing.height),
                                positioned)
                outer = placed.width + child_margin["left"] + child_margin["right"]
            leftmost = max(leftmost, (placed.left - containing.left)
                           + max(placed.width, placed.overflow_right) + child_margin["right"])
            height = placed.height + child_margin["top"] + child_margin["bottom"]
            recorded = walk.baselines.get(child)
            item_ascent = height if recorded is None else recorded + child_margin["top"]
            # Atomic inline boxes establish a line even at zero dimensions. The
            # containing element's font strut participates in its baseline.
            tag = str(getattr(child, "localName", "")).lower()
            if height == 0 and (child_style.get("display") != "inline" or
                    tag in ("img", "iframe", "canvas", "video", "audio", "object", "input")):
                strut = _line_style(style, environment, root)
                line.add(0.0, strut.ascent, strut.descent)
            line.add(outer, item_ascent, max(0.0, height - item_ascent))
            line.items.append((child, placed, item_ascent))
            continue
        flush()
        placed = _place(child, child_style, walk,
                        _Containing(containing.left, containing.top + cursor, containing.width,
                                    containing.height),
                        positioned)
        recorded = walk.baselines.get(child)
        if first_baseline is None and recorded is not None:
            # A block's baseline is the one its first line box has, wherever that line is.
            first_baseline = cursor + child_margin["top"] + recorded
        leftmost = max(leftmost, (placed.left - containing.left)
                       + max(placed.width, placed.overflow_right) + child_margin["right"])
        deepest = max(deepest, (placed.top - containing.top)
                      + max(placed.height, placed.overflow_bottom))
        cursor += child_margin["top"] + placed.height + child_margin["bottom"]
    flush()
    if first_baseline is not None:
        walk.baselines[element] = first_baseline
    walk.overflows[element] = (max(widest, leftmost), max(cursor, deepest))
    return cursor


def _flex_children(element, style, walk, containing, positioned):
    row = style.get("flex-direction", "row") not in ("column", "column-reverse")
    children = [child for child in _child_elements(element) if isinstance(child, Element)]
    if row:
        cursor = 0.0
        tallest = 0.0
        widest = 0.0
        placed_items = []
        for child in children:
            child_style = _style_of(child, walk.rules, walk.styles)
            flex = str(child_style.get('flex', '')).strip().lower()
            grows = (flex not in ('', 'none', '0', '0 1 auto')
                     or _number(child_style.get('flex-grow', '0')) > 0)
            if _auto(child_style.get('width')) and not grows:
                available = max(0.0, containing.width - cursor)
                width = _shrink_to_fit(child, child_style, walk.rules, available, cache=walk.styles)
                child_style = dict(child_style, width=f'{width}px')
            placed = _place(child, child_style, walk,
                            _Containing(containing.left + cursor, containing.top,
                                        max(0.0, containing.width - cursor), containing.height),
                            positioned)
            placed_items.append((child, child_style, placed))
            cursor += placed.width
            tallest = max(tallest, placed.height)
            widest = max(widest, (placed.left - containing.left)
                         + max(placed.width, placed.overflow_right))
        line_height = max(tallest, containing.height)
        for child, child_style, placed in placed_items:
            align = str(child_style.get('align-self', 'auto')).strip().lower()
            if align in ('', 'auto'):
                align = str(style.get('align-items', 'stretch')).strip().lower()
            margin = _sides(child_style, 'margin', containing.width)
            remaining = line_height - placed.height - margin['top'] - margin['bottom']
            shift = (remaining / 2 if align == 'center' else
                     remaining if align in ('end', 'flex-end') else 0.0)
            if shift:
                _shift(child, walk, 0.0, shift)
        walk.overflows[element] = (widest, tallest)
        return tallest
    cursor = 0.0
    widest = 0.0
    deepest = 0.0
    for child in children:
        child_style = _style_of(child, walk.rules, walk.styles)
        placed = _place(child, child_style, walk,
                        _Containing(containing.left, containing.top + cursor, containing.width,
                                    max(0.0, containing.height - cursor)),
                        positioned)
        cursor += placed.height
        widest = max(widest, (placed.left - containing.left)
                     + max(placed.width, placed.overflow_right))
        deepest = max(deepest, (placed.top - containing.top)
                     + max(placed.height, placed.overflow_bottom))
    walk.overflows[element] = (widest, max(cursor, deepest))
    return cursor


def _implicit_grid_items(element, rules, cache):
    """Simple explicit numeric placement in an otherwise implicit grid."""
    children = [child for child in _child_elements(element) if isinstance(child, Element)]
    placements, explicit, occupied = [], False, set()
    for child in children:
        style = _style_of(child, rules, cache)
        if style.get('display') == 'none' or style.get('position') in ('absolute', 'fixed'):
            continue
        area = str(style.get('grid-area', '')).split('/')
        column = str(style.get('grid-column-start') or style.get('grid-column') or '').split('/')[0].strip()
        row = str(style.get('grid-row-start') or style.get('grid-row') or '').split('/')[0].strip()
        if len(area) >= 2:
            row, column = area[0].strip(), area[1].strip()
        col = int(column) if column.isdigit() and int(column) > 0 else None
        row_num = int(row) if row.isdigit() and int(row) > 0 else None
        explicit |= col is not None or row_num is not None
        col = col or 1
        row_num = row_num or 1
        while (row_num, col) in occupied:
            row_num += 1
        occupied.add((row_num, col))
        placements.append((child, style, row_num - 1, col - 1))
    return placements if explicit else None


def _implicit_grid_widths(placements, rules, depth, cache):
    count = max((col for _, _, _, col in placements), default=0) + 1
    minima, maxima = [0.0] * count, [0.0] * count
    for child, style, _row, col in placements:
        minimum, maximum = _box_content_widths(child, style, rules, depth, cache)
        edges = sum(_sides(style, name, 0.0)[side]
                    for name in ('margin', 'padding', 'border-width') for side in ('left', 'right'))
        if not _auto(style.get('width')) and style.get('box-sizing') == 'border-box':
            inside = sum(_sides(style, name, 0.0)[side]
                         for name in ('padding', 'border-width') for side in ('left', 'right'))
            minimum, maximum = max(0.0, minimum - inside), max(0.0, maximum - inside)
        minima[col] = max(minima[col], minimum + edges)
        maxima[col] = max(maxima[col], maximum + edges)
    return minima, maxima


def _grid_children(element, style, walk, containing, positioned):
    if _auto(style.get('grid-template-columns')):
        placement = _implicit_grid_items(element, walk.rules, walk.styles)
        if placement is not None:
            _minima, tracks = _implicit_grid_widths(placement, walk.rules, 0, walk.styles)
            extra = max(0.0, containing.width - sum(tracks)) / len(tracks)
            tracks = [width + extra for width in tracks]
            row_heights = {}
            for child, child_style, row, col in placement:
                top = containing.top + sum(row_heights.get(i, 0.0) for i in range(row))
                placed = _place(child, child_style, walk,
                                _Containing(containing.left + sum(tracks[:col]), top,
                                            tracks[col], containing.height), positioned)
                margins = _sides(child_style, 'margin', containing.width)
                row_heights[row] = max(row_heights.get(row, 0.0),
                                       placed.height + margins['top'] + margins['bottom'])
            for child in _child_elements(element):
                if isinstance(child, Element):
                    child_style = _style_of(child, walk.rules, walk.styles)
                    if child_style.get('position') in ('absolute', 'fixed'):
                        _place(child, child_style, walk, containing, positioned)
            height = sum(row_heights.values())
            walk.overflows[element] = (sum(tracks), height)
            return height
    columns = max(1, len(str(style.get("grid-template-columns", "1fr")).split()))
    cell = containing.width / columns
    children = [child for child in _child_elements(element) if isinstance(child, Element)]
    cursor = 0.0
    row_height = 0.0
    widest = 0.0
    for index, child in enumerate(children):
        column = index % columns
        if column == 0 and index:
            cursor += row_height
            row_height = 0.0
        child_style = _style_of(child, walk.rules, walk.styles)
        placed = _place(child, child_style, walk,
                        _Containing(containing.left + column * cell, containing.top + cursor,
                                    cell, containing.height),
                        positioned)
        row_height = max(row_height, placed.height)
        widest = max(widest, (placed.left - containing.left)
                     + max(placed.width, placed.overflow_right))
    walk.overflows[element] = (widest, cursor + row_height)
    return cursor + row_height


def document_has_layout(document):
    """An embedding display:none subtree suppresses the child document's boxes.

    Check embedding styles before consulting the child cache: a parent style
    change does not increment the child's layout version.
    """
    window = getattr(getattr(document, '_services', None), 'window', None)
    frame = getattr(window, '_frame_element', None)
    while frame is not None:
        node = frame
        while isinstance(node, Element):
            if computed_style(node).get('display') == 'none':
                return False
            node = node.parentElement
        parent = getattr(getattr(frame, '_services', None), 'window', None)
        frame = getattr(parent, '_frame_element', None)
    return True


def layout(element, containing_width=None, top=0.0, rules=None, style=None, collected=None,
           styles=None):
    """*element*'s border box in viewport coordinates, laying the document out.

    The document is walked once per call: a box only knows where it sits after its
    ancestors have placed it, so ``containing_width`` and ``top`` are ignored and
    every recorded box carries its own viewport coordinates.
    """
    document = getattr(element, "ownerDocument", None)
    if document is None or not document_has_layout(document):
        return Box()
    if (rules is None and style is None and collected is None and styles is None
            and containing_width is None and top == 0.0):
        return layout_snapshot(document)[0].get(element, Box())
    boxes = {} if collected is None else collected
    root = document.get_documentElement()
    width, height = _viewport_width(element)
    if isinstance(root, Element):
        walk = _Walk(document_rules(document) if rules is None else rules,
                     boxes, (width, height), styles=styles if styles is not None else _StyleCache(rules is None))
        _place(root, _style_of(root, walk.rules, walk.styles), walk,
               _Containing(0.0, 0.0, width, height), None)
    return boxes.get(element, Box())


def layout_snapshot(document):
    """The current document's boxes and styles, shared by geometry and hit testing."""
    if not document_has_layout(document):
        return {}, {}
    root = document.get_documentElement()
    if not isinstance(root, Element):
        return {}, {}
    viewport = _viewport_width(root)
    version = (document.layout_version, document.style_version, viewport)
    cached = document._layout_snapshot
    animated = any(element.isConnected and any(
        animation._play_state == 'running' and animation._playback_rate != 0
        for animation in getattr(element, '_animations', ()))
        for element in getattr(document, '_animated_elements', ()))
    if not animated and cached is not None and cached[0] == version:
        return cached[1], cached[2]
    boxes, styles = {}, _StyleCache()
    layout(root, collected=boxes, styles=styles)
    if not animated and (document.layout_version, document.style_version, _viewport_width(root)) == version:
        document._layout_snapshot = (version, boxes, styles)
    return boxes, styles


def positioned_box(element):
    """*element*'s box with the offset its ancestors put it at."""
    return layout(element)


def offset_parent(element, styles=None):
    """The box an element's ``offsetLeft`` is measured against, or ``None``."""
    if not getattr(element, "isConnected", False):
        return None
    if not document_has_layout(getattr(element, 'ownerDocument', None)):
        return None
    own = styles.get(element) if styles is not None else None
    if own is None:
        own = computed_style(element)
    if own.get("position", "static") == "fixed":
        # Chrome answers no offset parent for a fixed box, nor for one with no box at all.
        return None
    if own.get("display", "block") == "none":
        return None
    document = getattr(element, "ownerDocument", None)
    if getattr(element, "localName", "") in ("html", "body"):
        return None
    current = getattr(element, "parentElement", None)
    while current is not None:
        style = styles.get(current) if styles is not None else None
        if style is None:
            style = computed_style(current)
        if style.get("position", "static") != "static":
            return current
        if getattr(current, "localName", "") in ("body", "td", "th", "table"):
            return current
        current = getattr(current, "parentElement", None)
    if document is not None:
        return document.get_body()
    return None


__all__ = [
    "Box", "computed_style", "document_rules", "layout",
    "offset_parent", "positioned_box", "text_width",
]


def _constrain_width(width, style, reference, edges):
    """Constrain the content width; a minimum wins over a smaller maximum."""
    border_box = style.get('box-sizing') == 'border-box'
    for name, constrain in (('max-width', min), ('min-width', max)):
        limit = style.get(name)
        if limit is None or str(limit).strip().lower() in ('', 'auto', 'none'):
            continue
        used = _px(limit, reference)
        if border_box:
            used -= edges
        width = constrain(width, max(0.0, used))
    return max(0.0, width)
