"""Shaping, font metrics and line breaking, against the faces this machine has.

The assertions hold for any face; what a real browser reports is pinned by
``tools/collect_text_layout.py`` and by the faces named in ``CHROME_METRICS`` below.
"""
from __future__ import annotations

import struct

import pytest

from pyv8c.foundation import system_fonts
from pyv8c.foundation import text as T


@pytest.fixture(scope="module")
def face():
    """A face that exists here, or nothing to test with."""
    for record in system_fonts.records():
        path = system_fonts.file_for(record)
        if path:
            return path, record.index
    pytest.skip("no font file is installed on this machine")


def shaped(value, face, size=16.0):
    path, index = face
    return T.shape(value, path, index=index, size=size)


def test_glyph_without_extents_has_no_ink():
    class Font:
        def get_glyph_extents(self, glyph):
            return None

    assert T._extent(Font(), 1, 1.0) == (0.0, 0.0, 0.0, 0.0)


def test_a_single_face_run_names_the_face_it_used(face):
    """Metrics are read from ``run.faces``. The fast path has to name its face too."""
    path, index = face
    run = shaped("H", face)
    assert run.faces == ((str(path), int(index)),)


def test_shaping_matches_a_run_of_glyphs(face):
    run = shaped("hello", face)
    assert run.text == "hello"
    assert run.glyphs and len(run.glyphs) == len(run.advances) == len(run.clusters)
    assert run.width == pytest.approx(sum(run.advances))
    assert run.clusters == tuple(range(5))


def test_a_kerned_pair_is_never_wider_than_its_parts(face):
    pair = shaped("AV", face).width
    parts = shaped("A", face).width + shaped("V", face).width
    assert pair <= parts + 1e-6


def test_a_run_is_scaled_by_the_size_it_was_asked_for(face):
    small, large = shaped("mmmm", face, 16.0).width, shaped("mmmm", face, 32.0).width
    assert large == pytest.approx(small * 2, rel=0.02)


def test_scaling_is_linear_within_one_size(face):
    one = shaped("A", face).width
    ten = shaped("A" * 10, face).width
    assert ten == pytest.approx(one * 10, rel=1e-6)


def test_a_combining_mark_stays_with_its_letter(face):
    run = shaped("a\u0301", face)
    assert run.clusters and all(cluster == 0 for cluster in run.clusters)
    assert len(run.glyphs) <= 2


def test_text_presentation_fallback_keeps_explicit_family_first():
    pytest.importorskip("pyv8c.pyv8", reason="layout imports the native browser bindings")
    from pyv8c.foundation.environment import BrowserEnvironment
    from pyv8c.web.dom.font_measurement import _shaped

    if system_fonts.match_family('Cambria Math') is None:
        pytest.skip('Cambria Math is not installed')
    run, _ = _shaped('\u2b06\ufe0e', {
        'font-family': 'Cambria Math, Arial', 'font-size': '100px',
        'font-weight': '400', 'font-style': 'normal',
    }, BrowserEnvironment.from_dict(None))
    assert run.faces
    assert run.faces[0][0].lower().endswith('cambria.ttc')


def test_a_missing_face_measures_nothing_and_says_so(face):
    missing = r"C:\no-such-directory\none.ttf"
    assert T.shape("abc", missing).width == 0.0
    assert T.metrics(missing).height == 0.0
    assert T.estimate("abc") > 0


def test_metrics_come_from_the_face_tables(face):
    path, index = face
    found = T.metrics(path, index=index, size=16.0)
    assert found.height > 0
    assert found.ascent > 0 and found.descent >= 0
    raw = T._table(T._face(path, index), "OS/2")
    if len(raw) >= T._OS2_WIN_DESCENT + 2 and not struct.unpack_from(
            ">H", raw, T._OS2_SELECTION)[0] & T._USE_TYPO_METRICS:
        win = struct.unpack_from(">H", raw, T._OS2_WIN_ASCENT)[0]
        upem = T._face(path, index).upem
        assert found.ascent == pytest.approx(T._rounded(win * 16.0 / upem))
        assert found.ascent == int(found.ascent) and found.descent == int(found.descent)


#: What the local Chrome 155 reports at 16px: font box ascent and descent, and the height
#: ``line-height: normal`` gives.  Measured truth, so this is what the rule is pinned to.
CHROME_METRICS = {
    "Arial": (14, 3, 18),
    "Consolas": (15, 4, 19),
    "Verdana": (16, 3, 19),
    "Segoe UI": (17, 4, 21),
    "Times New Roman": (14, 3, 18),
    "Tahoma": (16, 3, 19),
}


@pytest.mark.parametrize("family", sorted(CHROME_METRICS))
def test_a_measured_face_has_the_extents_chrome_gives_it(family):
    record = system_fonts.match_family(family)
    if record is None:
        pytest.skip(f"{family} is not installed")
    path = system_fonts.file_for(record)
    found = T.metrics(path, index=record.index, size=16.0)
    ascent, descent, height = CHROME_METRICS[family]
    assert (found.ascent, found.descent) == (ascent, descent)
    assert found.height == height


def test_breaks_follow_the_unicode_rules(face):
    assert [point.offset for point in T.breaks("word word")] == [5, 9]
    assert [point.offset for point in T.breaks("a\u00a0b")] == [3]
    forced = [point for point in T.breaks("a\nb") if point.forced]
    assert [point.offset for point in forced] == [2]


def test_a_line_never_starts_with_a_closing_cjk_mark():
    offsets = [point.offset for point in T.breaks("\u4e2d\u6587\u3002\u5b57")]
    assert 2 not in offsets
    assert offsets[-1] == 4


def test_graphemes_keep_a_cluster_whole():
    assert T.graphemes("a\u0301b") == (2, 3)
    assert T.graphemes("\U0001F468\u200D\U0001F469") == (3,)


def test_wrapping_fills_lines_and_keeps_every_character(face):
    value = "hello world again"
    run = shaped(value, face)
    points = T.breaks(value)
    lines = T.wrap(run, 60.0, points)
    assert len(lines) > 1
    assert all(line.advance <= 60.0 + 1e-6 or not any(
        line.start < point.offset < line.resume for point in points) for line in lines)
    covered = "".join(value[line.start:line.end] for line in lines)
    assert covered.replace(" ", "") == value.replace(" ", "")
    assert [line.start for line in lines] == sorted(line.start for line in lines)


def test_a_piece_wider_than_the_line_goes_on_its_own(face):
    value = "word word"
    run = shaped(value, face)
    lines = T.wrap(run, 1.0, T.breaks(value))
    assert len(lines) == 2
    assert lines[0].advance > 1.0


def test_the_trailing_space_is_not_counted(face):
    value = "aa bb"
    run = shaped(value, face)
    lines = T.wrap(run, 100.0, T.breaks(value))
    assert len(lines) == 1
    assert value[lines[0].start:lines[0].end] == "aa bb"


def test_a_shaped_run_is_reused(face):
    assert shaped("cache me", face) is shaped("cache me", face)
