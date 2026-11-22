from pathlib import Path

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c.async_api import Browser
from pyv8c.web.dom import DOMTokenList


async def _page(html="<body></body>"):
    browser = (await Browser().__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content(html, url="https://example.test/", wait_until="commit"))
    return browser, page


async def test_required_arguments_and_interface_arguments_are_type_errors():
    browser, page = (await _page("<body><div></div></body>"))
    value = page.evaluate("""(() => {
      const out = [];
      for (const fn of [
        () => document.createElement(),
        () => document.body.querySelector(),
        () => document.body.appendChild(1),
        () => document.body.dispatchEvent({}),
      ]) {
        try { fn(); out.push(false); } catch (e) { out.push([e.name, e instanceof TypeError]); }
      }
      return out;
    })()""")
    assert value == [["TypeError", True]] * 4
    (await browser.close())


async def test_variadic_optional_and_dictionary_arguments():
    browser, page = (await _page())
    value = page.evaluate("""(() => {
      const list = document.body.classList;
      list.add();
      const afterEmptyAdd = list.value;
      list.add('one', 'two');
      document.body.addEventListener('x', () => {}, {once: true, capture: false});
      return [afterEmptyAdd, list.value];
    })()""")
    assert value == ["", "one two"]
    (await browser.close())


async def test_null_undefined_and_webidl_coercion():
    browser, page = (await _page())
    value = page.evaluate("""(() => {
      const event = new Event('x', undefined);
      const params = new URLSearchParams(null);
      const blob = new Blob(undefined);
      return [event.type, event.bubbles, params.toString(), blob.size,
              document.body.classList.toggle('x', undefined)];
    })()""")
    assert value == ["x", False, "null=", 0, True]
    (await browser.close())


async def test_constructor_length_and_url_argument_validation():
    browser, page = (await _page())
    value = page.evaluate("""(() => {
      let invalid;
      try { new URL(); } catch (e) { invalid = [e.name, e instanceof TypeError]; }
      const u = new URL('https://example.test/a');
      return [invalid, URL.length, u.searchParams === u.searchParams];
    })()""")
    assert value[0][0] in ("TypeError", "URIError")
    assert value[0][1] is True
    assert value[1:] == [1, True]
    (await browser.close())


async def test_attribute_setter_enum_and_numeric_validation():
    browser, page = (await _page())
    value = page.evaluate("""(() => {
      const out = [];
      const ws = Object.create(WebSocket.prototype);
      try { ws.binaryType = 'invalid'; } catch (e) { out.push([e.name, e instanceof TypeError]); }
      document.body.classList.value = 7;
      out.push(document.body.classList.value);
      return out;
    })()""")
    assert value == [["TypeError", True], "7"]
    (await browser.close())


async def test_fetch_argument_failures_are_rejected_promises():
    browser, page = (await _page())
    page.evaluate("fetch().catch(e => window.__fetch_error = [e.name, e instanceof TypeError])")
    (await page.run_until_idle())
    assert page.evaluate("__fetch_error") == ["TypeError", True]
    (await browser.close())


async def test_browser_does_not_mutate_real_implementation_classes():
    original_init = DOMTokenList.__dict__["__init__"]
    original_contains = DOMTokenList.__dict__["fn_contains"]
    original_keys = frozenset(DOMTokenList.__dict__)
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        browser.new_profile().new_page()
        assert DOMTokenList.__dict__["__init__"] is original_init
        assert DOMTokenList.__dict__["fn_contains"] is original_contains
        assert frozenset(DOMTokenList.__dict__) == original_keys
        assert not hasattr(original_contains, "__webidl_wrapped__")
    finally:
        (await browser.close())


async def test_runtime_has_no_signature_driven_argument_validator():
    runtime = Path(__file__).resolve().parents[2] / "pyv8c"
    assert not (runtime / "engine" / "webidl.py").exists()
    for path in runtime.rglob("*.py"):
        if "tools" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for forbidden in ("validate_arguments", "BindingPlan", "compile_parameter", "from .webidl import"):
            assert forbidden not in source, (path, forbidden)


async def test_legacy_stub_marker_is_absent_from_runtime_sources():
    workspace = Path(__file__).resolve().parents[3]
    sources = list((workspace / "pyv8c" / "pyv8c").glob("*.py"))
    sources.extend((workspace / "pyv8" / "src").glob("*.cpp"))
    legacy_marker = "__idl_" + "stub__"
    assert all(legacy_marker not in path.read_text(encoding="utf-8") for path in sources)
