"""FontFace and FontFaceSet as a page sees them, in Chromium's own terms."""

import json

import pytest

from pyv8c.async_api import Browser
from pyv8c.foundation import system_fonts


@pytest.fixture
async def page():
    browser = (await Browser(cache=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="https://fonts.test/", wait_until="commit"))
    try:
        yield page
    finally:
        (await browser.close())


async def _probe(page, script):
    """Run an async probe and read back what it left on the window."""
    page.evaluate("window.__probe = null; (" + script + ").then(value => {"
                  " window.__probe = JSON.stringify(value); });")
    (await page.run_until_idle(timeout=5))
    return json.loads(page.evaluate("window.__probe"))


def _names():
    """One face this machine has, by PostScript name and by full name, if there is one."""
    records = system_fonts.records()
    for record in records:
        if record.postscript_name and record.full_name and record.postscript_name != record.full_name:
            return record
    pytest.skip("this machine has no face with two distinct names")


@pytest.mark.parametrize("receiver", ["window", "{}", "null"])
async def test_load_with_invalid_receiver_rejects_a_promise(page, receiver):
    page.evaluate("""globalThis.__invalidLoad = null;
        const result = FontFace.prototype.load.call(RECEIVER);
        globalThis.__isPromise = result instanceof Promise;
        result.then(() => __invalidLoad = 'resolved',
                    error => __invalidLoad = error.name);
    """.replace("RECEIVER", receiver))
    (await page.wait_for("__invalidLoad !== null", timeout=2))
    assert page.evaluate("__isPromise") is True
    assert page.evaluate("__invalidLoad") == "TypeError"


async def test_font_face_family_serializes_as_a_single_css_family_name(page):
    assert page.evaluate('''(() => [
      new FontFace('Cambria Math', 'local(Arial)').family,
      new FontFace('Arial', 'local(Arial)').family,
      new FontFace('serif', 'local(Arial)').family,
      new FontFace('a"b', 'local(Arial)').family,
      new FontFace('123', 'local(Arial)').family
    ])()''') == ['"Cambria Math"', 'Arial', '"serif"', '"a\\"b"', '"123"']


async def test_local_finds_a_face_by_postscript_name_and_by_full_name(page):
    record = _names()
    answer = (await _probe(page, """
      (async () => {
        const probe = async (name) => {
          try {
            const face = await new FontFace(name, 'local("' + name + '")').load();
            return { ok: true, status: face.status };
          } catch (exc) {
            return { ok: false, name: exc.name, code: exc.code, message: exc.message };
          }
        };
        return { ps: await probe(%s), full: await probe(%s) };
      })()
    """ % (json.dumps(record.postscript_name), json.dumps(record.full_name))))
    assert answer["ps"] == {"ok": True, "status": "loaded"}, answer
    assert answer["full"] == {"ok": True, "status": "loaded"}, answer


async def test_a_name_no_face_has_rejects_the_way_chromium_does(page):
    answer = (await _probe(page, """
      (async () => {
        try {
          await new FontFace('x', 'local("Definitely Not A Font 123")').load();
          return { ok: true };
        } catch (exc) {
          return { ok: false, name: exc.name, code: exc.code, message: exc.message };
        }
      })()
    """))
    assert answer == {"ok": False, "name": "NetworkError", "code": 19,
                      "message": "A network error occurred."}, answer


async def test_an_unparseable_source_is_an_error_before_it_is_loaded(page):
    answer = (await _probe(page, """
      (async () => {
        const face = new FontFace('x', 'local(Arial:bold)');
        const status = face.status;
        try {
          await face.load();
          return { status, loaded: true };
        } catch (exc) {
          return { status, loaded: false, name: exc.name, code: exc.code, message: exc.message };
        }
      })()
    """))
    assert answer["status"] == "error", answer
    assert answer["name"] == "SyntaxError" and answer["code"] == 12, answer
    assert answer["message"] == (
        "The source provided ('local(Arial:bold)') could not be parsed as a value list."), answer


async def test_the_constructor_asks_for_two_arguments(page):
    answer = (await _probe(page, """(async () => {
      try { new FontFace(); return { threw: false }; }
      catch (exc) { return { name: exc.name, message: exc.message }; }
    })()"""))
    assert answer == {"name": "TypeError",
                      "message": "Failed to construct 'FontFace': 2 arguments required, "
                                 "but only 0 present."}, answer


async def test_descriptors_from_js_dictionary_and_width_alias_match_chrome(page):
    answer = page.evaluate('''(() => {
      const names = ['width', 'stretch', 'variant', 'descentOverride',
                     'lineGapOverride', 'sizeAdjust'];
      const plain = new FontFace('Probe', 'local(Arial)');
      const configured = new FontFace('Probe', 'local(Arial)', {
        width: 'condensed', stretch: 'expanded', variant: 'small-caps',
        descentOverride: '20%', lineGapOverride: '30%', sizeAdjust: '80%'
      });
      const before = [plain, configured].map(face => names.map(name => face[name]));
      for (const name of names) plain[name] = configured[name];
      return {before, after: names.map(name => plain[name])};
    })()''')
    defaults = ['normal', 'normal', 'normal', 'normal', 'normal', '100%']
    configured = ['condensed', 'condensed', 'small-caps', '20%', '30%', '80%']
    assert answer == {'before': [defaults, configured], 'after': configured}


async def test_check_answers_true_for_a_family_no_face_and_no_platform_knows(page):
    """Chromium's rule, which its name does not suggest: this is not a false."""
    answer = page.evaluate("""(() => ({
      system: document.fonts.check('16px Arial'),
      unknown: document.fonts.check('16px Definitely Not A Font 123'),
      generic: document.fonts.check('16px monospace'),
    }))()""")
    assert answer == {"system": True, "unknown": True, "generic": True}, answer


async def test_check_and_load_refuse_a_shorthand_they_cannot_parse(page):
    answer = (await _probe(page, """
      (async () => {
        const out = {};
        try { document.fonts.check('Arial'); out.check = 'no throw'; }
        catch (exc) { out.check = { name: exc.name, code: exc.code, message: exc.message }; }
        try { await document.fonts.load('Arial'); out.load = 'resolved'; }
        catch (exc) { out.load = { name: exc.name, code: exc.code, message: exc.message }; }
        return out;
      })()
    """))
    expected = {"name": "SyntaxError", "code": 12, "message": "Could not resolve 'Arial' as a font."}
    assert answer == {"check": expected, "load": expected}, answer


async def test_load_resolves_with_the_faces_it_matched(page):
    """A platform family has no face in the set, which is an empty list and not a failure."""
    answer = (await _probe(page, """
      (async () => {
        const none = await document.fonts.load('16px Arial');
        return { count: none.length, size: document.fonts.size };
      })()
    """))
    assert answer == {"count": 0, "size": 0}, answer


async def test_a_document_font_face_is_a_face_of_the_set(page):
    """``@font-face`` faces are CSS-connected: counted, listed first, and loadable."""
    (await page.set_content("""
      <body>
      <style id="sheet">
        @font-face { font-family: "Web Only"; src: url(web.woff2) format("woff2"); font-weight: 700; }
      </style>
      </body>
    """, url="https://fonts.test/", wait_until="commit"))
    answer = (await _probe(page, """
      (async () => {
        const out = { size: document.fonts.size, before: document.fonts.check('16px "Web Only"') };
        const loaded = await document.fonts.load('16px "Web Only"');
        out.count = loaded.length;
        out.family = loaded[0].family;
        out.weight = loaded[0].weight;
        out.status = loaded[0].status;
        out.after = document.fonts.check('16px "Web Only"');
        out.values = [...document.fonts.values()].map(face => face.family);
        document.getElementById('sheet').textContent =
          '@font-face { font-family: "Later"; src: local("" +  "Arial"); }';
        out.afterEdit = document.fonts.size;
        return out;
      })()
    """))
    assert answer["size"] == 1 and answer["values"] == ["Web Only"], answer
    assert answer["before"] is False and answer["after"] is True, answer
    assert [answer["count"], answer["family"], answer["weight"], answer["status"]] ==\
        [1, "Web Only", "700", "loaded"], answer
    assert answer["afterEdit"] == 1, answer
