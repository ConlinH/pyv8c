"""Runtime-facing behavior for ``HTMLMetaElement``."""

import pytest

pytest.importorskip("pyv8c.pyv8")
from pyv8c import pyv8

from pyv8c.async_api import Browser


@pytest.fixture
async def page():
    browser = (await Browser(stub_logging="off").__aenter__())
    instance = browser.new_profile().new_page()
    (await instance.set_content(
        "<html><head><meta name='viewport' http-equiv='refresh' "
        "content='width=device-width' media='screen' scheme='text'>"
        "</head><body></body></html>"
    , wait_until="commit"))
    try:
        yield instance
    finally:
        (await browser.close())


async def test_meta_is_a_real_html_meta_element_with_reflected_attributes(page):
    result = page.evaluate("""
(() => {
  const meta = document.querySelector('meta');
  return [
    meta instanceof HTMLMetaElement,
    Object.prototype.toString.call(meta),
    meta.localName, meta.tagName,
    meta.name, meta.httpEquiv, meta.content, meta.media, meta.scheme,
    meta.getAttribute('http-equiv'), meta.getAttribute('charset'),
  ];
})()
""")
    assert result == [
        True, "[object HTMLMetaElement]", "meta", "META",
        "viewport", "refresh", "width=device-width", "screen", "text",
        "refresh", pyv8.Null,
    ]


async def test_meta_properties_are_domstring_reflections_and_clone_independently(page):
    result = page.evaluate("""
(() => {
  const meta = document.querySelector('meta');
  meta.name = 42;
  meta.httpEquiv = null;
  meta.content = undefined;
  meta.media = false;
  meta.scheme = 0;
  const clone = meta.cloneNode(false);
  const cloneContent = clone.content;
  meta.content = 'changed';
  return [meta.name, meta.httpEquiv, meta.content, meta.media, meta.scheme,
          cloneContent,
          Object.getOwnPropertyNames(meta).includes('content')];
})()
""")
    assert result == ["42", "null", "changed", "false", "0", "undefined", False]
