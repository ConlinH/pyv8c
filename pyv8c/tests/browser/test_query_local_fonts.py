"""queryLocalFonts: the gates in front of it, and the entries behind them."""

import json

import pytest

from pyv8c.async_api import Browser
from pyv8c.foundation import system_fonts

PROBE = """
(async () => {
  try {
    const fonts = await window.queryLocalFonts(%s);
    return { step: 'ok', count: fonts.length,
             first: fonts[0] ? [fonts[0].postscriptName, fonts[0].fullName, fonts[0].family,
                                fonts[0].style] : null,
             isFontData: fonts[0] ? fonts[0] instanceof window.FontData : null };
  } catch (exc) {
    return { step: 'rejected', name: exc.name, code: exc.code, message: exc.message };
  }
})()
"""


async def _run(environment=None, options="", hide=False):
    browser = (await Browser(cache=False, stub_logging="off", real_network=False, environment=environment).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://fonts.test/", wait_until="commit"))
        if hide:
            page.set_visibility(True)
        page.evaluate("window.__probe = null; (" + PROBE % options + ").then(value => {"
                      " window.__probe = JSON.stringify(value); });")
        (await page.run_until_idle(timeout=5))
        if hide:
            page.set_visibility(False)
        return json.loads(page.evaluate("window.__probe"))
    finally:
        (await browser.close())


async def test_a_page_without_a_grant_is_refused_the_way_chrome_refuses_it():
    """A pending permission needs a gesture, and a probe without a click has none."""
    assert (await _run()) == {"step": "rejected", "name": "SecurityError", "code": 18,
                      "message": "User activation is required."}


async def test_a_hidden_page_is_refused_before_the_permission_is_even_asked():
    assert (await _run(environment={"permissions.granted": ["local-fonts"]}, hide=True)) == {
        "step": "rejected", "name": "SecurityError", "code": 18,
        "message": "Page needs to be visible."}


async def test_a_granted_permission_hands_out_the_machine_s_faces():
    records = system_fonts.records()
    expected = sum(1 for record in records if record.postscript_name and record.style)
    answer = (await _run(environment={"permissions.granted": ["local-fonts"]}))
    assert answer["step"] == "ok", answer
    assert answer["count"] == expected, answer
    assert answer["isFontData"] is True, answer
    if expected:
        assert all(isinstance(value, str) and value for value in answer["first"]), answer


async def test_the_postscript_names_option_filters_the_table():
    records = system_fonts.records()
    wanted = [record.postscript_name for record in records
              if record.postscript_name and record.style][:2]
    if len(wanted) < 2:
        pytest.skip("this machine has fewer than two named faces")
    granted = {"permissions.granted": ["local-fonts"]}
    answer = (await _run(environment=granted, options=json.dumps({"postscriptNames": wanted})))
    assert answer["step"] == "ok" and answer["count"] == 2, answer
    empty = (await _run(environment=granted,
                 options=json.dumps({"postscriptNames": ["Definitely Not A Font 123"]})))
    assert empty == {"step": "ok", "count": 0, "first": None, "isFontData": None}, empty


async def test_the_surface_is_the_one_chrome_publishes():
    browser = (await Browser(cache=False, stub_logging="off", real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://fonts.test/", wait_until="commit"))
        answer = page.evaluate("""(() => ({
          fn: typeof window.queryLocalFonts,
          ctor: typeof window.FontData,
          members: Object.getOwnPropertyNames(window.FontData.prototype).sort(),
        }))()""")
    finally:
        (await browser.close())
    assert answer["fn"] == "function" and answer["ctor"] == "function", answer
    assert answer["members"] == ["blob", "constructor", "family", "fullName",
                                 "postscriptName", "style"], answer
