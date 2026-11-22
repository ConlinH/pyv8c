"""Font-size resolution against real DOM trees."""
import pytest
from pyv8c.async_api import Browser
from pyv8c.foundation import system_fonts
from pyv8c.foundation.environment import PROFILE, BrowserEnvironment


@pytest.fixture
async def page():
    # The rem case reads the Windows profile's default face. A host profile must not move it.
    browser = (await Browser(
        cache=False, stub_logging="off",
        environment=BrowserEnvironment.from_file(PROFILE),
    ).__aenter__())
    page = browser.new_profile().new_page()
    try:
        yield page
    finally:
        (await browser.close())


async def test_the_relative_sizes_of_a_real_tree_match_the_computed_value(page):
    """The value a page reads and the line box it makes come from the same resolution."""
    (await page.set_content("""
      <body style="font-size:16px;margin:0;font-family:Arial">
        <div id="em" style="margin:0;font-size:2em">x</div>
        <div id="pct" style="margin:0;font-size:150%">x</div>
        <div id="smaller" style="margin:0;font-size:smaller">x</div>
        <div id="outer" style="margin:0;font-size:24px">
          <span id="inner" style="font-size:150%">x</span>
        </div>
      </body>
    """, url="http://font-size.test/", wait_until="commit"))
    answer = page.evaluate("""(() => {
      const read = (id) => [getComputedStyle(document.getElementById(id)).fontSize,
                            document.getElementById(id).offsetHeight];
      return { em: read('em'), pct: read('pct'), smaller: read('smaller'),
               inner: read('inner') };
    })()""")
    # Chrome 155 on the same machine, reading the same page: the line box is the face's own
    # metrics rounded to whole pixels and added up, so 32px Arial is 29 + 7 + 1 = 37, 24px is
    # 22 + 5 + 1 = 28, 13.3333px is 12 + 3 + 0 = 15 and 36px is 33 + 8 + 1 = 42.
    assert answer["em"] == ["32px", 37], answer
    assert answer["pct"] == ["24px", 28], answer
    assert answer["smaller"] == ["13.3333px", 15], answer
    assert answer["inner"] == ["36px", 42], answer


async def test_a_rem_size_reads_the_root_not_the_parent(page):
    (await page.set_content('<body style="margin:0"><div id="rem" style="font-size:1.5rem">x</div></body>',
                     url="http://font-size.test/", wait_until="commit"))
    answer = page.evaluate("""(() => {
      document.documentElement.style.fontSize = '20px';
      const el = document.getElementById('rem');
      return [getComputedStyle(el).fontSize, el.offsetHeight];
    })()""")
    # The profile's Noto Sans SC default produces the same 44px line box as Chrome.
    # Without that file the measure falls back to a local face, so the line box is not 44.
    if not any(record.family == "Noto Sans SC" and record.path
               for record in system_fonts.records()):
        assert answer[0] == "30px", answer
        pytest.skip("Noto Sans SC is not installed")
    assert answer == ["30px", 44], answer
