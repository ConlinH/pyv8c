"""A canvas measures text with the same font table the layout does.

``measureText`` is a page-readable measurement, so it reads the profile's fonts too: a
canvas width and the width of the same text in a box have to agree, or a page can tell
one engine from another by comparing them.
"""

import pytest

from pyv8c.async_api import Browser
from pyv8c.foundation import system_fonts
from pyv8c.foundation import text as text_engine
from pyv8c.foundation.environment import PROFILE, BrowserEnvironment, profile_path
from pyv8c.web.dom import font_measurement

TEXT = "mmmmmmmmmm"

MEASURE = """
(() => {
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = %s;
  return ctx.measureText(%s).width;
})()
"""


async def _measure(environment=None, font='16px "Arial"'):
    import json

    browser = (await Browser(cache=False, stub_logging="off", real_network=False,
                      environment=environment).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://canvas.test/", wait_until="commit"))
        return page.evaluate(MEASURE % (json.dumps(font), json.dumps(TEXT)))
    finally:
        (await browser.close())


def _latin_family():
    record = next((item for item in system_fonts.records()
                   if item.family and item.path and item.style == "Regular"), None)
    if record is None:
        pytest.skip("this machine has no regular face to measure with")
    return record.family


async def test_a_canvas_width_is_the_width_the_same_text_has_in_a_box():
    family = _latin_family()
    windows = BrowserEnvironment.from_file(PROFILE)
    measured = (await _measure(environment=windows, font=f'16px "{family}"'))
    expected = font_measurement.text_width(TEXT, {"font-size": "16px", "font-family": family},
                                 windows)
    assert measured == pytest.approx(expected, abs=0.5), (measured, expected)


async def test_a_canvas_follows_a_profile_that_carries_its_own_fonts():
    """The point of the shared table: a profile changes what a canvas measures, too."""
    fake = {"postscript_name": "FakeOnly-Regular", "full_name": "FakeOnly",
            "family": "FakeOnly", "style": "Regular",
            "path": r"C:\Windows\Fonts\consola.ttf", "index": 0}
    environment = BrowserEnvironment({"fonts.records": [fake]})
    measured = (await _measure(environment={"fonts.records": [fake]}, font='16px "FakeOnly"'))
    expected = font_measurement.text_width(TEXT, {"font-size": "16px", "font-family": "FakeOnly"},
                                 environment)
    assert measured == pytest.approx(expected, abs=0.5), (measured, expected)
    # The same canvas with the machine's own table measures something else for that name:
    # it has no such face, so the default family stands in.
    assert (await _measure(font='16px "FakeOnly"')) != pytest.approx(measured, abs=0.5)


async def test_a_family_no_table_knows_uses_the_environment_default():
    """The configured browser default supplies the fallback for an unknown family."""
    windows = BrowserEnvironment.from_file(PROFILE)
    record = font_measurement._face_for(["sans-serif"], windows)
    if system_fonts.file_for(record) is None:
        pytest.skip("the profile default face is not installed")
    unknown = (await _measure(environment=windows, font='16px "Definitely Not A Font 123"'))
    standard = (await _measure(environment=windows, font='16px sans-serif'))
    assert unknown == pytest.approx(standard, abs=1e-9)


async def test_canvas_default_font_uses_chrome_fractional_advance_precision():
    if not any(record.family == 'Noto Sans SC' and record.path for record in system_fonts.records()):
        pytest.skip('Noto Sans SC is not installed')
    browser = (await Browser(
        real_network=False, stub_logging='off',
        environment=BrowserEnvironment.from_file(PROFILE),
    ).__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate('''(() => {
          const ctx = document.createElement('canvas').getContext('2d');
          return ['', ' ', 'm', 'mm'].map(value => ctx.measureText(value).width);
        })()''') == [0, 2.239990234375, 9.259994506835938, 18.519989013671875]
    finally:
        (await browser.close())


async def test_a_point_size_is_read_as_pixels_at_96_dpi():
    """``12pt`` is 16px, which is the conversion the layout uses as well."""
    assert (await _measure(font='12pt "Arial"')) == pytest.approx(
        (await _measure(font='16px "Arial"')), abs=0.5)


BOXES = """
(() => {
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = %s;
  const m = ctx.measureText(%s);
  return { width: m.width, left: m.actualBoundingBoxLeft, right: m.actualBoundingBoxRight,
           ascent: m.actualBoundingBoxAscent, descent: m.actualBoundingBoxDescent,
           fontAscent: m.fontBoundingBoxAscent, fontDescent: m.fontBoundingBoxDescent,
           emAscent: m.emHeightAscent, emDescent: m.emHeightDescent,
           hanging: m.hangingBaseline, alphabetic: m.alphabeticBaseline,
           ideographic: m.ideographicBaseline };
})()
"""


async def _boxes(font, text=TEXT, environment=None):
    import json

    if environment is None:
        environment = BrowserEnvironment.from_file(PROFILE)
    browser = (await Browser(
        cache=False, stub_logging="off", real_network=False,
        environment=environment,
    ).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://canvas.test/", wait_until="commit"))
        return page.evaluate(BOXES % (json.dumps(font), json.dumps(text)))
    finally:
        (await browser.close())


async def test_the_boxes_come_from_the_face_that_was_measured_with():
    family = _latin_family()
    # The family comes from this machine. Measure it with the host table, which names it.
    host = BrowserEnvironment.from_file(profile_path())
    answer = (await _boxes(f'16px "{family}"', environment=host))
    record = font_measurement._face_for([family], host)
    path = system_fonts.file_for(record)
    assert path is not None
    found = text_engine.metrics(path, index=record.index, size=16.0)
    assert answer["fontAscent"] == found.ascent, answer
    assert answer["fontDescent"] == found.descent, answer
    assert answer["ascent"] > 0, answer
    # The ink is the glyphs' own box counted from the origin, so its right edge lands on
    # either side of the advance: Chrome reads 1..9 for "x" and a right edge past the advance
    # for Times.  Hinting is on this engine's not-aligned list, so the box is held to the
    # relation rather than to the last decimal Chrome reports.
    assert -2 <= answer["left"] <= 2, answer
    assert answer["right"] >= answer["width"] - 2, answer


async def test_a_profile_family_without_a_local_file_still_reports_its_font_box():
    """Segoe UI is in the Windows catalog. Without the file, the box is the local fallback."""
    answer = (await _boxes('16px "Segoe UI"', text="H"))
    assert answer["width"] > 0, answer
    assert answer["fontAscent"] > 0, answer


async def test_an_empty_run_has_the_face_box_and_no_ink():
    answer = (await _boxes('16px "Arial"', text=""))
    assert answer["width"] == 0 and answer["ascent"] == 0 and answer["descent"] == 0, answer
    assert answer["fontAscent"] > 0, answer


async def test_the_baselines_are_the_known_zero():
    """The surface publishes ten members; the em-height pair and the three baselines stay
    zero because that is what a page reads from Chrome 155 as well."""
    answer = (await _boxes('16px "Arial"'))
    assert answer["hanging"] == 0 and answer["alphabetic"] == 0, answer
    assert answer["ideographic"] == 0, answer
    # The em-height pair is not one of the ten members this surface publishes, and Chrome
    # 155 answers undefined for it as well -- so the reading is not a number, here or there.
    assert not isinstance(answer["emAscent"], (int, float)), answer
    assert not isinstance(answer["emDescent"], (int, float)), answer


INK = """
(() => {
  const canvas = document.createElement('canvas');
  canvas.width = 80; canvas.height = 40;
  const ctx = canvas.getContext('2d');
  ctx.font = '14px "Arial"';
  ctx.textBaseline = %s;
  ctx.textAlign = %s;
  ctx.fillStyle = '#000';
  ctx.fillText('Hxg', %d, 20);
  const data = ctx.getImageData(0, 0, 80, 40).data;
  let minX = 999, minY = 999, maxX = -1, maxY = -1;
  for (let y = 0; y < 40; y++) {
    for (let x = 0; x < 80; x++) {
      if (data[(y * 80 + x) * 4 + 3] > 32) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  return [minX, minY, maxX, maxY];
})()
"""


async def _ink(baseline="alphabetic", align="left", x=4):
    import json

    browser = (await Browser(cache=False, stub_logging="off", real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://canvas.test/", wait_until="commit"))
        return page.evaluate(INK % (json.dumps(baseline), json.dumps(align), x))
    finally:
        (await browser.close())


async def test_text_baseline_names_where_the_run_sits():
    """``top`` puts the em box's top at *y*; ``alphabetic`` puts the baseline there."""
    alphabetic = (await _ink("alphabetic"))
    top = (await _ink("top"))
    assert 5 <= alphabetic[1] <= 16, alphabetic
    assert 17 <= top[1] <= 23, top
    assert top[1] - alphabetic[1] >= 6, (alphabetic, top)


async def test_text_align_names_where_the_run_ends():
    left = (await _ink("alphabetic", "left", 4))
    right = (await _ink("alphabetic", "right", 60))
    center = (await _ink("alphabetic", "center", 40))
    assert abs(left[0] - 4) <= 2, left
    assert abs(right[2] - 60) <= 2, right
    assert abs((center[0] + center[2]) / 2 - 40) <= 3, center


async def test_a_canvas_font_reads_back_serialized():
    """``ctx.font`` answers the shorthand Chrome would print, not what was assigned."""
    script = """(() => {
      const ctx = document.createElement('canvas').getContext('2d');
      const out = [];
      for (const font of ['14px "Arial"', 'bold 14px "Times New Roman"', '10px sans-serif']) {
        ctx.font = font;
        out.push(ctx.font);
      }
      return out;
    })()"""
    browser = (await Browser(cache=False, stub_logging="off", real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://canvas.test/", wait_until="commit"))
        assert page.evaluate(script) == [
            "14px Arial", 'bold 14px "Times New Roman"', "10px sans-serif"]
    finally:
        (await browser.close())
