"""Shaping, font metrics and line breaking: the machine's half of text layout.

The CSS half -- which families a style asks for, how ``white-space`` treats a space,
where ``word-break`` adds an opportunity, how tall a line is -- lives in the layout layer.
"""
from __future__ import annotations

import math
import os
import struct
import threading
from dataclasses import dataclass

import uharfbuzz as hb
from uniseg.graphemecluster import grapheme_clusters
from uniseg.linebreak import line_break_units

from .logging import logger

__all__ = ["BreakPoint", "FontMetrics", "LineSpan", "ShapedRun", "breaks", "estimate",
           "graphemes", "metrics", "next_line", "shape", "shape_selection", "wrap"]

#: Entries kept per cache; the oldest is dropped when a page reads more strings than this.
_CACHE_LIMIT = 512

_FACES: dict[tuple, object] = {}
_FONTS: dict[tuple, object] = {}
_RUNS: dict[tuple, "ShapedRun"] = {}
_METRICS: dict[tuple, "FontMetrics"] = {}
_BREAKS: dict[str, tuple["BreakPoint", ...]] = {}
_GRAPHEMES: dict[str, tuple[int, ...]] = {}
_COVERAGE: dict[tuple, bool] = {}
_LOCK = threading.Lock()

#: ``hhea`` and ``OS/2`` field offsets, and the ``fsSelection`` bit asking for typo metrics.
_HHEA_ASCENDER, _HHEA_DESCENDER, _HHEA_LINE_GAP = 4, 6, 8
_OS2_SELECTION, _OS2_TYPO_ASCENDER, _OS2_TYPO_DESCENDER, _OS2_TYPO_LINE_GAP = 62, 68, 70, 72
_OS2_WIN_ASCENT, _OS2_WIN_DESCENT = 74, 76
_USE_TYPO_METRICS = 1 << 7


@dataclass(frozen=True)
class FontMetrics:
    """Extents from the baseline, in pixels: ``line_gap`` is the space between lines."""

    ascent: float
    descent: float
    line_gap: float = 0.0

    @property
    def height(self) -> float:
        return self.ascent + self.descent + self.line_gap


@dataclass(frozen=True)
class ShapedRun:
    """One string as the face drew it: ``clusters`` names the character behind each glyph."""

    text: str
    width: float
    glyphs: tuple[int, ...]
    clusters: tuple[int, ...]
    advances: tuple[float, ...]
    offsets: tuple[tuple[float, float], ...]
    extents: tuple[tuple[float, float, float, float], ...] = ()
    prefix: tuple[float, ...] = (0.0,)
    faces: tuple[tuple[str, int], ...] = ()

    def advance_between(self, start: int, end: int) -> float:
        """The width of the characters ``[start, end)``, in one subtraction."""
        if not self.prefix:
            return 0.0
        start = max(0, min(int(start), len(self.prefix) - 1))
        end = max(start, min(int(end), len(self.prefix) - 1))
        return self.prefix[end] - self.prefix[start]


@dataclass(frozen=True)
class LineSpan:
    """One wrapped line: ``end`` excludes the spaces before the break, ``resume`` is past them."""

    start: int
    end: int
    advance: float
    resume: int = 0


@dataclass(frozen=True)
class BreakPoint:
    """An offset a line may end at; ``forced`` marks the ones a newline leaves."""

    offset: int
    forced: bool = False


def _remember(cache: dict, key, value):
    """Keep a value, dropping the oldest entry once the cache is full."""
    if len(cache) >= _CACHE_LIMIT:
        cache.pop(next(iter(cache)), None)
    cache[key] = value
    return value


def _face(path, index=0):
    """The ``hb.Face`` of one file and face index, read once; a failure is remembered."""
    key = (str(path), int(index))
    face = _FACES.get(key)
    if face is None and key not in _FACES:
        with _LOCK:
            face = _FACES.get(key)
            if face is None:
                try:
                    face = hb.Face(hb.Blob.from_file_path(str(path)), int(index))
                except Exception as exc:  # noqa: BLE001 - an unreadable face measures nothing
                    logger.debug("text: {} face {} could not be read: {}", path, index, exc)
                    _remember(_FACES, key, None)
                    return None
                _remember(_FACES, key, face)
    return face


def _font(path, index, size, weight=None, layout_units=False):
    """An ``hb.Font`` at one size whose positions come back in pixels.

    Canvas uses font units for its floating-point measurements. Inline layout
    may request 26.6 coordinates to match the browser's LayoutUnit grid.
    """
    face = _face(path, index)
    if face is None:
        return None
    key = (str(path), int(index), round(float(size), 4), weight, layout_units)
    font = _FONTS.get(key)
    if font is None:
        font = hb.Font(face)
        hb.ot_font_set_funcs(font)
        font.scale = ((round(float(size) * 64),) * 2 if layout_units
                      else (face.upem, face.upem))
        if weight is not None:
            # A variable font's default axis can be Thin even when CSS requests
            # normal text. Static faces simply ignore an unknown axis.
            font.set_variations({'wght': float(weight)})
        _remember(_FONTS, key, font)
    return font


def shape(text, path, index=0, size=16.0, *, features=None, weight=None,
          layout_units=False) -> ShapedRun:
    """``text`` as positioned glyphs in the face at *path*, or an empty run without one."""
    text = "" if text is None else str(text)
    key = (text, str(path), int(index), round(float(size), 4), weight, layout_units,
           None if not features else tuple(sorted(features.items())))
    run = _RUNS.get(key)
    if run is not None:
        return run
    font = _font(path, index, size, weight, layout_units)
    face = _face(path, index)
    if font is None or face is None:
        return ShapedRun(text, 0.0, (), (), (), ())
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(font, buffer, features)
    scale = 1 / 64 if layout_units else float(size) / face.upem
    glyphs, clusters, advances, offsets, extents = [], [], [], [], []
    for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
        glyphs.append(int(info.codepoint))
        clusters.append(int(info.cluster))
        advance = position.x_advance * scale
        if os.name == "nt" and str(path).lower().endswith("cambria.ttc") and index == 1:
            # DirectWrite exposes Cambria Math's hinted advances at whole pixels.
            advance = float(round(advance))
        advances.append(advance)
        offsets.append((position.x_offset * scale, position.y_offset * scale))
        extents.append(_extent(font, int(info.codepoint), scale))
    # Layout reads extents from run.faces. This fast path is still one face;
    # leaving faces empty makes a profile family with no local file report none.
    run = ShapedRun(text, sum(advances), tuple(glyphs), tuple(clusters), tuple(advances),
                    tuple(offsets), tuple(extents),
                    _prefix_widths(clusters, advances, len(text)),
                    ((str(path), int(index)),))
    return _remember(_RUNS, key, run)


def _has_glyph(face_key, char) -> bool:
    """Whether a face draws a character, which is how a fallback run is chosen."""
    key = (face_key[0], face_key[1], char)
    known = _COVERAGE.get(key)
    if known is None:
        font = _font(face_key[0], face_key[1], 1.0)
        covered = False
        if font is not None:
            try:
                covered = bool(font.get_nominal_glyph(ord(char)))
            except Exception:  # noqa: BLE001 - a character no face can look up is not covered
                covered = False
        known = _remember(_COVERAGE, key, covered)
    return known


def shape_selection(text, faces, size=16.0, *, features=None, weight=None,
                    layout_units=False, text_presentation_faces=None) -> ShapedRun:
    """*text* drawn by the first of *faces* that has each character, runs shaped together.

    Consecutive characters resolving to one face are shaped as one piece, so kerning and
    ligatures still apply; the result is a single run whose advances are per character and
    can be wrapped as usual.  A CJK character in an Arial run is drawn by a CJK face rather
    than as a missing-glyph box.
    """
    text = "" if text is None else str(text)
    candidates = [(str(path), int(index)) for path, index in faces if path]
    text_candidates = ([(str(path), int(index)) for path, index in text_presentation_faces if path]
                       if text_presentation_faces is not None else candidates)
    if not text or not candidates:
        return ShapedRun(text, 0.0, (), (), (), ())
    variation = '\ufe0e' in text or '\ufe0f' in text
    if len(candidates) == 1 or (not variation and _has_every_glyph(candidates[0], text)):
        return shape(text, candidates[0][0], candidates[0][1], size,
                     features=features, weight=weight, layout_units=layout_units)
    key = (text, tuple(candidates), tuple(text_candidates), round(float(size), 4), weight, layout_units,
           None if not features else tuple(sorted(features.items())))
    known = _RUNS.get(key)
    if known is not None:
        return known
    groups: list[list] = []
    units = ((position, char) for position, char in enumerate(text))
    if variation:
        def clusters():
            position = 0
            for cluster in grapheme_clusters(text):
                yield position, cluster
                position += len(cluster)
        units = clusters()
    for position, char in units:
        chosen = candidates[0]
        base = next((value for value in char if value not in ('\ufe0e', '\ufe0f', '\u200d')), char[0])
        if '\ufe0f' in char:
            for candidate in candidates:
                face = _face(*candidate)
                if (face is not None and _has_glyph(candidate, base)
                        and any(_table(face, tag) for tag in ('COLR', 'CBDT', 'sbix', 'SVG '))):
                    chosen = candidate
                    break
            else:
                chosen = None
        else:
            chosen = None
        if chosen is None:
            choices = text_candidates if '\ufe0e' in char else candidates
            chosen = next((candidate for candidate in choices if _has_glyph(candidate, base)), candidates[0])
        if groups and groups[-1][1] == chosen:
            continue
        groups.append([position, chosen])
    glyphs: list[int] = []
    clusters: list[int] = []
    advances: list[float] = []
    offsets: list[tuple[float, float]] = []
    extents: list[tuple[float, float, float, float]] = []
    used: list[tuple[str, int]] = []
    for index, (start, candidate) in enumerate(groups):
        end = groups[index + 1][0] if index + 1 < len(groups) else len(text)
        piece = shape(text[start:end], candidate[0], candidate[1], size,
                      features=features, weight=weight, layout_units=layout_units)
        if candidate not in used:
            used.append(candidate)
        glyphs.extend(piece.glyphs)
        clusters.extend(start + cluster for cluster in piece.clusters)
        advances.extend(piece.advances)
        offsets.extend(piece.offsets)
        extents.extend(piece.extents)
    run = ShapedRun(text, sum(advances), tuple(glyphs), tuple(clusters), tuple(advances),
                    tuple(offsets), tuple(extents),
                    _prefix_widths(clusters, advances, len(text)), tuple(used))
    return _remember(_RUNS, key, run)


def _has_every_glyph(face_key, text) -> bool:
    """Whether one face draws a whole string, which keeps the common case short."""
    return all(_has_glyph(face_key, char) for char in text)


def _extent(font, glyph, scale):
    """One glyph's ink box in pixels, as ``(x, y, width, height)`` from the origin."""
    try:
        box = font.get_glyph_extents(glyph)
    except Exception:  # noqa: BLE001 - a glyph without outlines has no ink to report
        return (0.0, 0.0, 0.0, 0.0)
    if box is None:
        return (0.0, 0.0, 0.0, 0.0)
    return (box.x_bearing * scale, box.y_bearing * scale,
            box.width * scale, box.height * scale)


def _table(face, name):
    """A face's raw table as bytes, or empty bytes when it does not have one."""
    try:
        table = face.reference_table(name)
    except Exception:  # noqa: BLE001 - a missing table is not an error here
        return b""
    data = getattr(table, "data", None)
    if data is None:
        return b""
    return data if isinstance(data, bytes) else bytes(data)


def bitmap_strike_applies(path, index, size, chars):
    """Whether a Windows bitmap strike includes the nominal glyph range at this size."""
    if os.name != 'nt':
        return False
    face = _face(path, index)
    font = _font(path, index, size) if face is not None else None
    if font is None:
        return False
    table = _table(face, 'EBLC')
    if len(table) < 8:
        return False
    count = struct.unpack_from('>I', table, 4)[0]
    glyphs = [font.get_nominal_glyph(ord(char)) for char in chars]
    if not glyphs or any(glyph is None for glyph in glyphs):
        return False
    ppem = math.floor(float(size) + 0.5)
    for i in range(count):
        offset = 8 + i * 48
        if offset + 48 > len(table):
            break
        first, last = struct.unpack_from('>HH', table, offset + 40)
        if table[offset + 45] == ppem and all(first <= glyph <= last for glyph in glyphs):
            return True
    return False


def text_control_width(path, index=0, size=16.0, columns=20):
    """Estimate Blink's text-field intrinsic width from a face's glyph metrics."""
    face = _face(path, index)
    if face is None or not face.upem:
        return None
    head = _table(face, "head")
    if len(head) < 44:
        return None
    x_min, x_max = struct.unpack_from(">hh", head, 36)[0], struct.unpack_from(">hh", head, 40)[0]
    average = shape('x', path, index, size).width
    if average <= 0:
        return None
    average = max(average, _rounded(average))
    maximum = _rounded(max(0, x_max - x_min) * float(size) / face.upem)
    return math.ceil(average * max(1, int(columns)) + max(0.0, maximum - average))


def metrics(path, index=0, size=16.0) -> FontMetrics:
    """A face's extents at one size, as the platform's font stack hands them out.

    The ascent and descent are the face's typographic pair when ``OS/2`` asks for it
    (``fsSelection`` bit 7) and the Windows pair otherwise; the gap is what ``hhea``'s own
    spacing has left over that pair.  Each is then rounded to whole pixels: at 16px Arial is
    14 + 3 + 1 = 18 and Consolas 15 + 4 + 0 = 19, which is what a real Chrome reports.
    """
    key = (str(path), int(index), round(float(size), 4))
    known = _METRICS.get(key)
    if known is not None:
        return known
    face = _face(path, index)
    if face is None:
        return FontMetrics(0.0, 0.0)
    scale = float(size) / float(face.upem or 1000)
    os2 = _table(face, "OS/2")
    hhea = _table(face, "hhea")
    ascent = descent = 0
    typographic = (len(os2) >= _OS2_SELECTION + 2
                   and struct.unpack_from(">H", os2, _OS2_SELECTION)[0] & _USE_TYPO_METRICS)
    if typographic and len(os2) >= _OS2_TYPO_LINE_GAP + 2:
        ascent = struct.unpack_from(">h", os2, _OS2_TYPO_ASCENDER)[0]
        descent = -struct.unpack_from(">h", os2, _OS2_TYPO_DESCENDER)[0]
    elif len(os2) >= _OS2_WIN_DESCENT + 2:
        ascent = struct.unpack_from(">H", os2, _OS2_WIN_ASCENT)[0]
        descent = struct.unpack_from(">H", os2, _OS2_WIN_DESCENT)[0]
    hhea_ascent = hhea_descent = hhea_gap = 0
    if len(hhea) >= _HHEA_LINE_GAP + 2:
        hhea_ascent = struct.unpack_from(">h", hhea, _HHEA_ASCENDER)[0]
        hhea_descent = -struct.unpack_from(">h", hhea, _HHEA_DESCENDER)[0]
        hhea_gap = struct.unpack_from(">h", hhea, _HHEA_LINE_GAP)[0]
    if not ascent and hhea_ascent:
        ascent, descent = hhea_ascent, hhea_descent
    gap = max(0, (hhea_ascent + hhea_descent + hhea_gap) - (ascent + descent))
    found = FontMetrics(_rounded(max(0, ascent) * scale), _rounded(max(0, descent) * scale),
                        _rounded(max(0, gap) * scale))
    if not found.height:
        return FontMetrics(0.0, 0.0)
    return _remember(_METRICS, key, found)


def _rounded(value) -> float:
    """A metric in whole pixels, half away from zero, the way the font stack rounds it."""
    return float(math.floor(value + 0.5))


def breaks(text) -> tuple[BreakPoint, ...]:
    """Where lines may end by UAX#14, end of text included, newlines marked forced."""
    text = "" if text is None else str(text)
    known = _BREAKS.get(text)
    if known is not None:
        return known
    points: list[BreakPoint] = []
    offset = 0
    for unit in line_break_units(text):
        offset += len(unit)
        points.append(BreakPoint(offset, unit.endswith(("\n", "\r"))))
    if not points or points[-1].offset != len(text):
        points.append(BreakPoint(len(text)))
    return _remember(_BREAKS, text, tuple(points))


def graphemes(text) -> tuple[int, ...]:
    """The offsets a grapheme cluster ends at: inside a word, but never inside a cluster."""
    text = "" if text is None else str(text)
    known = _GRAPHEMES.get(text)
    if known is not None:
        return known
    offset = 0
    ends: list[int] = []
    for cluster in grapheme_clusters(text):
        offset += len(cluster)
        ends.append(offset)
    return _remember(_GRAPHEMES, text, tuple(ends))


def _prefix_widths(clusters, advances, length) -> tuple[float, ...]:
    """``prefix[i]`` is the width of the first *i* characters, so a range is a subtraction."""
    widths: dict[int, float] = {}
    for cluster, advance in zip(clusters, advances):
        widths[cluster] = widths.get(cluster, 0.0) + advance
    total = 0.0
    prefix = [0.0] * (length + 1)
    for index in range(1, length + 1):
        total += widths.get(index - 1, 0.0)
        prefix[index] = total
    return tuple(prefix)


def _trimmed_end(text, start, end):
    """The offset a line really ends at, with the collapsible spaces before it removed."""
    while end > start and text[end - 1] in " \t\r\n":
        end -= 1
    return end


def next_line(run: ShapedRun, start: int, width: float, points) -> LineSpan | None:
    """The next line of *run* from *start*: the last opportunity that fits.

    If none fits, the first one ends the line and the piece sticks out.  A forced break ends
    the line wherever it falls; a trailing space is not counted into the line's width.
    """
    text = run.text
    if not text or start >= len(text):
        return None
    positions = sorted({point.offset for point in points if start < point.offset <= len(text)}
                       | {len(text)})
    forced = {point.offset for point in points if point.forced}
    chosen = None
    overflow = None
    for candidate in positions:
        end = _trimmed_end(text, start, candidate)
        if end <= start:
            # Only spaces lie before this opportunity: the line ends here and they go with it.
            chosen = (candidate, candidate)
            break
        if overflow is None:
            overflow = (candidate, end)
        if run.advance_between(start, end) > width:
            break
        chosen = (candidate, end)
        if candidate in forced:
            break
    if chosen is None:
        chosen = overflow
    if chosen is None:
        return None
    resume, end = chosen
    return LineSpan(start, end, run.advance_between(start, end), resume)


def wrap(run: ShapedRun, width: float, points) -> tuple[LineSpan, ...]:
    """Every line of *run* at one *width*: :func:`next_line` repeated until it is spent."""
    spans: list[LineSpan] = []
    start = 0
    while start < len(run.text):
        span = next_line(run, start, width, points)
        if span is None:
            break
        spans.append(span)
        start = span.resume if span.resume > start else span.end
        if start <= span.start:  # never loops on the spot, whatever a caller passes in
            break
    return tuple(spans)


def estimate(text, size=16.0) -> float:
    """A width for text no face could measure: the layout layer's only fallback."""
    return sum(size * (0.34 if ch in "ilI.,' !" else 0.56 if ord(ch) < 128 else 1.0)
               for ch in (text or ""))
