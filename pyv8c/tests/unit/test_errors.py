import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c import PageTypeError
from pyv8c.async_api import Browser


async def test_dom_exception_constructor_and_legacy_code():
    browser = (await Browser().__aenter__()); page = browser.new_profile().new_page()
    value = page.evaluate("(() => { const e = new DOMException('bad', 'InvalidStateError'); return [e.name, e.message, e.code, e instanceof DOMException, Object.prototype.toString.call(e), DOMException.length]; })()")
    assert value == ["InvalidStateError", "bad", 11, True, "[object DOMException]", 0]
    (await browser.close())


async def test_python_v8_error_metadata_and_dom_errors():
    browser = (await Browser().__aenter__()); page = browser.new_profile().new_page(); (await page.set_content("<body><div></div></body>", wait_until="commit"))
    value = page.evaluate("""
      (() => {
        const results = [];
        try { document.body.appendChild(1); } catch (e) { results.push([e.name, e.message, e instanceof TypeError]); }
        try { document.body.removeChild(document.createElement('span')); } catch (e) { results.push([e.name, e.code, e instanceof DOMException]); }
        try { document.querySelector('div['); } catch (e) { results.push([e.name, e instanceof SyntaxError]); }
        return results;
      })()
    """)
    assert value[0][0:3] == [
        "TypeError",
        "Failed to execute 'appendChild' on 'Node': parameter 1 is not of type 'Node'.",
        True,
    ]
    assert value[1] == ["NotFoundError", 8, True]
    assert value[2] == ["SyntaxError", True]
    (await browser.close())


async def test_history_security_error_and_fetch_rejection_object():
    browser = (await Browser(resource_loader=lambda url, request: (_ for _ in ()).throw(RuntimeError("offline"))).__aenter__())
    page = browser.new_profile().new_page(); (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
    assert page.evaluate("(() => { try { history.pushState({}, '', 'https://other.test/'); } catch (e) { return [e.name, e.code, e instanceof DOMException]; } })()") == ["SecurityError", 18, True]
    page.evaluate("fetch('/offline').catch(e => window.__fetchError = [e.name, e instanceof Error, e.message])")
    (await page.run_until_idle())
    assert page.evaluate("__fetchError.slice(0, 3)") == ["TypeError", True, "Failed to fetch"]
    (await browser.close())


async def test_a_dom_exception_reaches_python_as_the_browser_stringifies_it():
    browser = (await Browser().__aenter__()); page = browser.new_profile().new_page()
    value = page.evaluate(
        "e = new DOMException('Service workers are not implemented here.',"
        " 'NotSupportedError'); e")
    assert str(value) == "NotSupportedError: Service workers are not implemented here."
    assert str(page.evaluate("new DOMException('m')")) == "Error: m"
    assert str(page.evaluate("new DOMException('m', '')")) == "m"
    assert str(page.evaluate("new DOMException()")) == "Error"
    (await browser.close())


async def test_the_error_family_is_declared_for_a_realm():
    """Inheriting the family is the whole declaration: what a page sees is what a class
    inherited, which the browser tests hold end to end."""
    from pyv8c import pyv8

    assert isinstance(PageTypeError("boom"), pyv8.JsError)
    assert isinstance(PageTypeError("boom"), pyv8.JsTypeError)
    with pytest.raises(PageTypeError):
        raise PageTypeError("boom")


async def test_the_legacy_code_table_agrees_with_the_document():
    """``DOM_EXCEPTION_CODES`` answers ``code``, by DOM error name; the document installs the
    same numbers as ``*_ERR`` constants.  Two spellings of one table, held together by the
    numbers and by the count."""
    from pyv8c.engine.idl import Idl
    from pyv8c.web.page_errors import DOM_EXCEPTION_CODES

    constants = Idl.load().interface("DOMException").constants
    documented = {item["value"] for item in constants
                  if item.get("resolved") and isinstance(item.get("value"), int)}
    assert len(documented) == len(constants)
    assert {value for value in DOM_EXCEPTION_CODES.values() if value} == documented
    # The names a browser gives no legacy number are this table's own contribution.
    assert sorted(name for name, value in DOM_EXCEPTION_CODES.items() if not value) == [
        "EncodingError", "NotAllowedError", "RangeError", "UnknownError"]
