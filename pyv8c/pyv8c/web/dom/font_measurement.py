"""Font selection for document inputs and text measurement for Canvas and SVG."""

import os
from functools import lru_cache

from ...foundation import text as text_engine
from ...foundation.environment import environment_of
from ...foundation.system_fonts import file_for, normalize, table_of
from ..css.cascade import _number, _px


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


def _shaped(text, style, environment=None, *, layout_units=False):
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
    run = text_engine.shape_selection(text, faces, size=_font_size(style),
                                      weight=_weight_axis(style.get('font-weight', '400')),
                                      layout_units=layout_units, text_presentation_faces=text_faces)
    return (run if run.width else None), chain[0]


def _run_metrics(run, style):
    """Return the extents of the tallest face used in a shaped run."""
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
    """Return the shaped or estimated advance with CSS letter and word spacing."""
    text = "" if text is None else str(text)
    size = _font_size(style)
    run, _record = _shaped(text, style, environment)
    width = run.width if run is not None else text_engine.estimate(text, size)
    return width + _spacing(text, style, size)


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
