"""``HTMLUnknownElement`` and the tag-to-interface resolution rule.

``class_for_tag`` is the single resolver both element-creation paths use:
``Document.createElement`` and the parser's element factory.  A local name it does
not model becomes ``HTMLUnknownElement``, except when it has a hyphen in it: that is
a valid custom element name, which Chromium answers with a plain ``HTMLElement``.
"""
import pytest

from pyv8c.async_api import Browser
from pyv8c.web.html.registry import HTML_CLASSES, TAG_CLASSES, class_for_tag
from pyv8c.web.html.elements import HTMLUnknownElement

PAGE_HTML = ("<foo id='unknown'></foo><my-widget id='custom'></my-widget>"
             "<div id='known'></div>")

#: Tags whose local name is not the lower-cased middle of its interface name.
IRREGULAR_TAGS = ("img", "td", "tr", "textarea", "iframe")

#: Obsolete tags Chromium still maps to ``HTMLUnknownElement``, plus the names
#: this DOM does not model at all and that carry no hyphen.
UNMODELLED_TAGS = ("foo", "bgsound", "blink", "isindex", "keygen", "multicol",
                   "nextid", "spacer", "media")

#: Hyphenated names are custom element names, so an undefined one is an ``HTMLElement``.
CUSTOM_TAGS = ("my-widget", "x-y", "DIV-")


@pytest.fixture
async def document_page():
    browser = (await Browser(stub_logging="off").__aenter__())
    instance = browser.new_profile().new_page()
    (await instance.set_content(PAGE_HTML, url="https://example.test/", wait_until="commit"))
    try:
        yield instance
    finally:
        (await browser.close())


async def test_class_for_tag_resolves_modelled_tags_and_falls_back():
    for tag in ("div", "span", "form", "table", "select", "option", "button",
                "a", "html", "head", "body", "script", "style", "link", "meta",
                "template", "canvas", "input") + IRREGULAR_TAGS:
        assert class_for_tag(tag) is not HTMLUnknownElement, tag
        assert class_for_tag(tag.upper()) is class_for_tag(tag), tag
    for tag in UNMODELLED_TAGS + ("",):
        assert class_for_tag(tag) is HTMLUnknownElement, tag
    for tag in CUSTOM_TAGS:
        assert class_for_tag(tag) is HTML_CLASSES["HTMLElement"], tag
    assert HTML_CLASSES["HTMLUnknownElement"] is HTMLUnknownElement
    # Every tag maps to a class the registry knows about, and the fallback
    # interface is not a tag of its own.
    for tag, cls in TAG_CLASSES.items():
        assert cls in HTML_CLASSES.values(), tag
    assert "unknown" not in TAG_CLASSES


async def test_unknown_names_are_unknown_and_custom_ones_are_plain_elements(document_page):
    assert document_page.evaluate("""
        (() => {
          const parsed = id => document.getElementById(id).constructor.name;
          const created = tag => document.createElement(tag).constructor.name;
          return [parsed('unknown'), parsed('custom'), parsed('known'),
                  created('foo'), created('my-widget'), created('div'),
                  document.createElement('my-widget') instanceof HTMLElement];
        })()
    """) == ["HTMLUnknownElement", "HTMLElement", "HTMLDivElement",
             "HTMLUnknownElement", "HTMLElement", "HTMLDivElement", True]


async def test_parser_and_create_element_agree_on_every_local_name(document_page):
    assert document_page.evaluate("""
        (() => {
          const names = ['img', 'td', 'tr', 'textarea', 'iframe', 'canvas', 'input',
                         'script', 'template', 'select', 'option', 'a', 'foo',
                         'bgsound', 'keygen', 'spacer', 'my-widget'];
          const host = document.createElement('div');
          return names.filter(tag => {
            const created = document.createElement(tag);
            const parsed = (host.innerHTML = '<' + tag + '></' + tag + '>',
                            host.firstChild);
            return created.constructor !== parsed.constructor;
          });
        })()
    """) == []


async def test_create_element_accepts_chrome_html_names(document_page):
    assert document_page.evaluate("""
        ['_', 'äx', 'a=b', ':foo', 'foo:bar', '😀'].map(name => {
          const element = document.createElement(name);
          return [element.localName, element.tagName];
        })
    """) == [['_', '_'], ['äx', 'äX'], ['a=b', 'A=B'],
              [':foo', ':FOO'], ['foo:bar', 'FOO:BAR'], ['😀', '😀']]


async def test_create_element_rejects_invalid_html_names(document_page):
    assert document_page.evaluate("""
        ['1x', 'a b', 'a/b', 'a>b', ''].map(name => {
          try { document.createElement(name); return 'accepted'; }
          catch (error) { return [error.name, error.message]; }
        })
    """) == [
        ['InvalidCharacterError', "Failed to execute 'createElement' on 'Document': The tag name provided ('1x') is not a valid name."],
        ['InvalidCharacterError', "Failed to execute 'createElement' on 'Document': The tag name provided ('a b') is not a valid name."],
        ['InvalidCharacterError', "Failed to execute 'createElement' on 'Document': The tag name provided ('a/b') is not a valid name."],
        ['InvalidCharacterError', "Failed to execute 'createElement' on 'Document': The tag name provided ('a>b') is not a valid name."],
        ['InvalidCharacterError', "Failed to execute 'createElement' on 'Document': The tag name provided ('') is not a valid name."],
    ]


async def test_document_fragment_children_is_live_collection(document_page):
    assert document_page.evaluate("""
        (() => {
          const fragment = document.createDocumentFragment();
          const children = fragment.children;
          fragment.append(document.createTextNode('text'), document.createElement('_'));
          return [Object.prototype.toString.call(children),
                  typeof children[Symbol.iterator], children === fragment.children,
                  children.length,
                  [...children].map(element => element.localName)];
        })()
    """) == ['[object HTMLCollection]', 'function', True, 1, ['_']]


async def test_document_and_element_children_keep_identity(document_page):
    assert document_page.evaluate("""
        [document.children === document.children,
         document.body.children === document.body.children]
    """) == [True, True]


async def test_modelled_tags_resolve_to_their_interface(document_page):
    assert document_page.evaluate("""
        ['img', 'td', 'tr', 'textarea', 'iframe', 'canvas', 'input', 'script', 'template',
         'select', 'option', 'a'].map(tag => document.createElement(tag).constructor.name)
    """) == ["HTMLImageElement", "HTMLTableCellElement", "HTMLTableRowElement",
             "HTMLTextAreaElement", "HTMLIFrameElement", "HTMLCanvasElement",
             "HTMLInputElement", "HTMLScriptElement", "HTMLTemplateElement",
             "HTMLSelectElement", "HTMLOptionElement", "HTMLAnchorElement"]


async def test_html_unknown_element_is_exposed_but_not_constructible(document_page):
    assert document_page.evaluate("""
        (() => {
          const unknown = document.createElement('foo');
          let thrown = 'ok';
          try { new HTMLUnknownElement(); } catch (e) { thrown = e.name; }
          return [typeof HTMLUnknownElement,
                  unknown instanceof HTMLUnknownElement,
                  unknown instanceof HTMLElement,
                  document.createElement('div') instanceof HTMLUnknownElement,
                  thrown,
                  Object.prototype.toString.call(unknown)];
        })()
    """) == ["function", True, True, False, "TypeError", "[object HTMLUnknownElement]"]
