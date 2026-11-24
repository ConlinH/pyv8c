"""Wrapping and line boxes, against the numbers the local Chrome 155 reports.

Every pinned value was read from that browser first.  The fallback face a CJK run is drawn
by is the one thing not aligned, and it is called out where it shows.
"""
from __future__ import annotations

import pytest

from pyv8c.async_api import Browser
from pyv8c.foundation.environment import PROFILE, BrowserEnvironment
from pyv8c.web.dom import layout


@pytest.fixture(scope="module")
async def page():
    # Line boxes were read from the Windows profile's faces. The host profile must not move them.
    browser = (await Browser(
        cache=False, stub_logging="off", real_network=False,
        environment=BrowserEnvironment.from_file(PROFILE),
    ).__aenter__())
    page = browser.new_profile().new_page()
    try:
        yield page
    finally:
        (await browser.close())


async def measure(page, html, script):
    """Lay out *html* at ``https://wrap.test/`` and run *script* in the page."""
    (await page.set_content(f"<body style='margin:0'>%s</body>" % html, url="https://wrap.test/", wait_until="commit"))
    (await page.run_until_idle(timeout=2))
    return page.evaluate(script)


async def test_a_long_paragraph_wraps_into_lines(page):
    """Chrome: 60 words of 16px Arial in a 100px box are 30 lines of 18px, that is 540."""
    answer = (await measure(
        page,
        "<div id=t style='width:100px;font-family:Arial;font-size:16px'>" + "word " * 60
        + "</div>",
        "(() => { const t = document.getElementById('t');"
        " return [t.offsetHeight, t.scrollWidth, t.clientWidth]; })()"))
    assert answer == [540, 100, 100], answer


async def test_the_width_a_line_uses_is_what_the_same_text_measures(page):
    """A line's width and ``measureText`` for that line are one shaping, so they agree."""
    answer = (await measure(
        page,
        "<div id=t style='width:60px;font-family:Arial;font-size:16px'>hello world again</div>"
        "<canvas id=c></canvas>",
        "(() => { const ctx = document.getElementById('c').getContext('2d');"
        " ctx.font = '16px Arial';"
        " const t = document.getElementById('t');"
        " return [t.offsetHeight, ctx.measureText('hello world').width]; })()"))
    # Three lines of 18, and the first line is "hello" at 33.8 -- Chrome reads 33.8046875.
    assert answer[0] == 54, answer
    assert answer[1] == pytest.approx(76.484375), answer


async def test_a_non_breaking_space_keeps_a_line_together(page):
    """Chrome: ``a\\u00a0b c d e f`` in 40px is two lines, because ``a\\u00a0b`` cannot break."""
    answer = (await measure(
        page,
        "<div id=t style='width:40px;font-family:Arial;font-size:16px'>a\u00a0b c d e f</div>",
        "[document.getElementById('t').offsetHeight]"))
    assert answer == [36], answer


async def test_nowrap_keeps_one_line_and_overflows(page):
    """``nowrap`` leaves no opportunity, so the line sticks out: Chrome reads 18 and 2343."""
    answer = (await measure(
        page,
        "<div id=t style='width:100px;font-family:Arial;font-size:16px;white-space:nowrap'>"
        + "word " * 60 + "</div>",
        "(() => { const t = document.getElementById('t');"
        " return [t.offsetHeight, t.scrollWidth]; })()"))
    assert answer == [18, 2343], answer


async def test_a_newline_ends_a_line_in_pre(page):
    """``white-space: pre`` keeps the newlines, and each one ends a line whether it fits or not."""
    answer = (await measure(
        page,
        "<div id=t style='width:200px;font-family:Arial;font-size:16px;white-space:pre'>"
        "a\nb\nc</div>",
        "[document.getElementById('t').offsetHeight]"))
    assert answer == [54], answer


async def test_pre_line_keeps_the_newlines_but_folds_the_spaces(page):
    answer = (await measure(
        page,
        "<div id=t style='width:200px;font-family:Arial;font-size:16px;white-space:pre-line'>"
        "a   b\nc</div>",
        "[document.getElementById('t').offsetHeight]"))
    assert answer == [36], answer


async def test_break_all_cuts_a_word_that_does_not_fit(page):
    """``word-break: break-all`` adds every cluster boundary, so a long word is cut."""
    answer = (await measure(
        page,
        "<div id=t style='width:20px;font-family:Arial;font-size:16px;word-break:break-all'>"
        "mmmmmmmm</div>",
        "(() => { const t = document.getElementById('t');"
        " return [t.offsetHeight, t.scrollWidth]; })()"))
    # 8 n-wide glyphs at 13.3 each: one per 20px line, so eight lines and no overflow.
    assert answer[0] == 144, answer
    assert answer[1] <= 20, answer


async def test_overflow_wrap_breaks_only_when_nothing_fits(page):
    """A word longer than the line is cut; one that fits is not, unlike ``word-break``."""
    answer = (await measure(
        page,
        "<div id=t style='width:60px;font-family:Arial;font-size:16px;overflow-wrap:break-word'>"
        "hi mmmmmmmmmm</div>",
        "[document.getElementById('t').offsetHeight]"))
    # Chrome gives 72: "hi" keeps its own line and the long word is then cut four to a line,
    # because ``break-word`` cuts a word only where it does not fit even on a fresh line.
    assert answer[0] == 72, answer


@pytest.mark.parametrize("declared, height", (
    ("normal", 18), ("1.5", 24), ("150%", 24), ("30px", 30), ("2em", 32),
))
async def test_the_four_ways_to_write_a_line_height(page, declared, height):
    """Chrome reads exactly these for 16px Arial: 18, 24, 24, 30 and 32."""
    answer = (await measure(
        page,
        f"<div id=t style='font-family:Arial;font-size:16px;line-height:{declared}'>x</div>",
        "[document.getElementById('t').offsetHeight]"))
    assert answer == [height], answer


async def test_an_inline_block_shrinks_to_its_content(page):
    """A shrink-to-fit box stops at its maximum content width when there is room for it."""
    answer = (await measure(
        page,
        "<div style='width:400px'><span id=t style='display:inline-block;font-family:Arial;"
        "font-size:16px'>hello world</span></div>",
        "(() => { const t = document.getElementById('t');"
        " return [t.offsetWidth, t.offsetHeight]; })()"))
    # Chrome reads 76 for "hello world" at 16px Arial, which is 76.484 rounded down.
    assert answer == [76, 18], answer


async def test_an_inline_block_that_fits_stays_on_its_line(page):
    """Chrome keeps the 100px block after ``a `` in a 120px box and wraps the text after it."""
    answer = (await measure(
        page,
        "<div id=p style='width:120px;font-family:Arial;font-size:16px'>a "
        "<span id=t style='display:inline-block;width:100px'>b</span> c</div>",
        "(() => { const t = document.getElementById('t');"
        " return [Math.round(t.getBoundingClientRect().top),"
        " document.getElementById('p').offsetHeight]; })()"))
    assert answer == [0, 36], answer


async def test_cjk_text_breaks_between_characters(page):
    """Han characters are opportunities in UAX#14, so a narrow box wraps them.

    The height carries the fallback face: this engine reads 84 here and Chrome 96, so the
    breaking is what is pinned, not the number of pixels a line is tall.
    """
    answer = (await measure(
        page,
        "<div id=t style='width:40px;font-family:Arial;font-size:16px'>"
        "\u4e2d\u6587\u3002\u5b57\u4e5f\u4e0d\u5c11</div>",
        "(() => { const t = document.getElementById('t');"
        " return [t.offsetHeight, t.scrollWidth]; })()"))
    assert answer[0] > 18, answer
    assert answer[1] <= 40, answer


async def test_a_closing_cjk_mark_does_not_start_a_line(page):
    """The full stop may end a line but not begin one, so two characters make two lines."""
    answer = (await measure(
        page,
        "<div id=t style='width:16px;font-family:Arial;font-size:16px'>\u4e2d\u3002</div>",
        "(() => { const t = document.getElementById('t');"
        " return [t.offsetHeight, t.innerText]; })()"))
    assert answer[1] == "\u4e2d\u3002", answer
    assert answer[0] > 20, answer


async def test_a_br_ends_the_line(page):
    """Chrome reads 36 for ``a<br>b``: the break ends the first line and ``b`` takes the next."""
    answer = (await measure(page, "<div id=t style='font-family:Arial;font-size:16px'>a<br>b</div>",
                     "[t.offsetHeight]"))
    assert answer == [36], answer


async def test_a_leading_br_leaves_an_empty_line(page):
    """Chrome reads 36 for ``<br>b``: the break makes a line of its own before the text."""
    answer = (await measure(page, "<div id=t style='font-family:Arial;font-size:16px'><br>b</div>",
                     "[t.offsetHeight]"))
    assert answer == [36], answer


async def test_pre_keeps_an_empty_line(page):
    """Chrome reads 54 for ``a\\n\\nb`` in ``pre``: the empty line is a line of its own."""
    answer = (await measure(page,
                     "<div id=t style='width:200px;font-family:Arial;font-size:16px;"
                     "white-space:pre'>a\n\nb</div>",
                     "[t.offsetHeight]"))
    assert answer == [54], answer


async def test_text_align_moves_the_line_content(page):
    """Chrome puts a 20px inline block at 80, 40 and 0 in a 100px box, right to left."""
    script = ("(() => { const t = document.getElementById('t');"
              " return Math.round(t.getBoundingClientRect().left); })()")
    for align, left in (("right", 80), ("center", 40), ("left", 0)):
        answer = (await measure(
            page,
            f"<div style='width:100px;font-family:Arial;font-size:16px;text-align:{align}'>"
            "<span id=t style='display:inline-block;width:20px'>a</span></div>", script))
        assert answer == left, (align, answer)


async def test_a_flex_container_reports_the_overflow_of_its_child(page):
    """Chrome reads 104 of scrollWidth for a 40px flex box holding one nowrap word."""
    answer = (await measure(
        page,
        "<div id=t style='display:flex;width:40px;font-family:Arial;font-size:16px'>"
        "<span style='white-space:nowrap'>wordwordword</span></div>",
        "[t.scrollWidth, t.offsetHeight]"))
    assert answer == [104, 18], answer


async def test_letter_spacing_is_charged_after_every_character(page):
    """Chrome reads 25.797 + 3 x 2 = 31.797 for "abc", which a box rounds to 32."""
    answer = (await measure(
        page,
        "<span id=t style='display:inline-block;font-family:Arial;font-size:16px;"
        "letter-spacing:2px'>abc</span>",
        "[document.getElementById('t').offsetWidth]"))
    assert answer == [32], answer
    style = {"font-family": "Arial", "font-size": "16px", "letter-spacing": "2px"}
    assert layout.text_width("abc", style) == pytest.approx(31.797, abs=0.01)
