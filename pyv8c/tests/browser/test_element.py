import pytest
from pyv8c import pyv8

from pyv8c.async_api import Browser

PAGE_HTML = ("<body><div id='root'>"
             "<div id='box' class='alpha beta'>"
             "<span class='alpha' data-n='1'>one</span>"
             "<span class='gamma'>two</span>"
             "</div></div></body>")


@pytest.fixture
async def page():
    browser = (await Browser(stub_logging="off").__aenter__())
    instance = browser.new_profile().new_page()
    (await instance.set_content(PAGE_HTML, wait_until="commit"))
    try:
        yield instance
    finally:
        (await browser.close())


async def test_element_identity_and_constructor_mapping(page):
    result = page.evaluate(
        "(() => { const div = document.createElement('div'); "
        "const span = document.createElement('span'); "
        "const widget = document.createElement('my-widget'); "
        "return [div.localName, div.tagName, div.nodeName, div.nodeType, "
        "div.namespaceURI, div.prefix, "
        "div instanceof HTMLDivElement, div instanceof HTMLElement, "
        "div instanceof HTMLUnknownElement, span instanceof HTMLSpanElement, "
        "widget instanceof HTMLUnknownElement, widget instanceof HTMLElement, "
        "widget.localName, widget.tagName]; })()"
    )
    assert result == ["div", "DIV", "DIV", 1, "http://www.w3.org/1999/xhtml",
                      pyv8.Null, True, True, False, True, False, True,
                      "my-widget", "MY-WIDGET"]


async def test_element_attribute_round_trip(page):
    result = page.evaluate(
        "(() => { const div = document.createElement('div'); "
        "const out = []; "
        "out.push([div.hasAttributes(), div.getAttribute('missing') === null]); "
        "div.setAttribute('data-NAME', 'v1'); "
        "out.push([div.getAttribute('data-name'), div.getAttribute('DATA-NAME'), "
        "div.hasAttribute('DATA-NAME'), div.getAttributeNames()]); "
        "out.push([div.toggleAttribute('hidden'), div.hasAttribute('hidden'), "
        "div.toggleAttribute('hidden', false), div.hasAttribute('hidden')]); "
        "div.id = 'box-id'; div.className = 'c1 c2'; "
        "out.push([div.id, div.className, div.getAttribute('id'), "
        "div.getAttribute('class')]); "
        "const attrNode = div.getAttributeNode('id'); "
        "out.push([attrNode.name, attrNode.value]); "
        "div.removeAttribute('class'); "
        "out.push([div.hasAttribute('class')]); "
        "div.setAttributeNode(document.createAttribute('extra')); "
        "out.push([div.hasAttribute('extra'), div.getAttribute('extra')]); "
        "const errors = []; "
        "const attempt = (fn) => { "
        "try { fn(); return 'no-error'; } catch (e) { return e.name; } }; "
        "errors.push(attempt(() => div.setAttribute('1bad', 'x'))); "
        "errors.push(attempt(() => div.removeAttributeNode("
        "document.createAttribute('gone')))); "
        "out.push(errors); "
        "return out; })()"
    )
    assert result == [
        [False, True],
        ["v1", "v1", True, ["data-name"]],
        [True, True, False, False],
        ["box-id", "c1 c2", "box-id", "c1 c2"],
        ["id", "box-id"],
        [False],
        [True, ""],
        ["InvalidCharacterError", "NotFoundError"],
    ]


async def test_class_list_is_live_reflection_of_class_attribute(page):
    # add/toggle/value: corpus ``class_list_add_and_contains``, ``class_list_toggle_replace_and_value``.
    result = page.evaluate(
        "(() => { const div = document.createElement('div'); "
        "div.className = 'a  b a'; const list = div.classList; "
        "const indexed = [list.length, list[0], list[1], list.item(2) === null]; "
        "list.remove('a'); "
        "return [indexed, div.className, list.contains('a'), list.length]; })()"
    )
    assert result == [[2, "a", "b", True], "b", False, 1]


async def test_inner_html_outer_html_and_fragment_context_parsing(page):
    result = page.evaluate(
        "(() => { const box = document.getElementById('box'); "
        "box.innerHTML = '<p>a</p><p>b</p>text<i></i>'; "
        "const inner = [box.children.length, box.childNodes.length, "
        "box.children[0].localName, box.children[1].localName, "
        "box.children[2].localName, "
        "box.querySelector('p').textContent, box.textContent, "
        "box.innerHTML]; "
        "const fresh = document.createElement('div'); "
        "fresh.innerHTML = '<p>x</p>'; "  # <p> must not auto-close the root
        "const detached = [fresh.children.length, fresh.children[0].textContent, "
        "fresh.innerHTML]; "
        "const nestedP = document.createElement('p'); "
        "nestedP.innerHTML = '<p>nested</p>'; "
        "const nested = [nestedP.children.length, "
        "nestedP.firstElementChild.textContent, nestedP.innerHTML]; "
        "const card = document.createElement('div'); "
        "card.id = 'x'; card.className = 'k'; card.innerHTML = '<em>t</em>'; "
        "const outer = [card.outerHTML, card.querySelector('em').textContent]; "
        "return [inner, detached, nested, outer]; })()"
    )
    assert result == [
        [3, 4, "p", "p", "i", "a", "abtext",
         "<p>a</p><p>b</p>text<i></i>"],
        [1, "x", "<p>x</p>"],
        [1, "nested", "<p>nested</p>"],
        ['<div id="x" class="k"><em>t</em></div>', "t"],
    ]


async def test_children_traversal_and_child_node_mixins(page):
    result = page.evaluate(
        "(() => { const root = document.createElement('div'); "
        "const a = document.createElement('a'); "
        "const b = document.createElement('b'); "
        "root.append(a, b); "
        "root.prepend(document.createElement('i')); "
        "const first = [root.children.length, "
        "root.firstElementChild.localName, root.lastElementChild.localName]; "
        "b.remove(); "
        "const afterRemove = [root.children.length, b.parentNode === null, "
        "b.parentElement === null]; "
        "const sib = document.createElement('s'); a.before(sib); "
        "const afterBefore = [root.childElementCount, "
        "root.children[0].localName, root.children[1].localName]; "
        "const n = document.createElement('n'); a.after(n); "
        "a.replaceWith(document.createElement('r')); "
        "const names = []; "
        "for (let i = 0; i < root.children.length; i++) "
        "names.push(root.children[i].localName); "
        "const details = [sib.parentElement === root, "
        "sib.previousElementSibling.localName, sib.nextElementSibling.localName, "
        "root.firstElementChild === root.children[0], "
        "root.lastElementChild === root.children[root.children.length - 1]]; "
        "return [first, afterRemove, afterBefore, names, details]; })()"
    )
    assert result == [
        [3, "i", "b"],
        [2, True, True],
        [3, "i", "s"],
        ["i", "s", "r", "n"],
        [True, "i", "r", True, True],
    ]


async def test_element_selection_and_insert_adjacent(page):
    result = page.evaluate(
        "(() => { const box = document.getElementById('box'); "
        "box.innerHTML = '<section id=\"s\">"
        "<div class=\"item one\"></div>"
        "<div class=\"item two\"><span></span></div>"
        "</section>'; "
        "const out = []; "
        "out.push(box.querySelector('.item').className); "
        "out.push(box.querySelector('#s').id); "
        "out.push(box.querySelectorAll('.item').length); "
        "out.push(box.getElementsByTagName('div').length); "
        "out.push(box.getElementsByClassName('item').length); "
        "const target = box.querySelector('.two span'); "
        "out.push([target.matches('span'), target.matches('div'), "
        "target.closest('.two').className, target.closest('#s').id, "
        "target.closest('.missing') === null]); "
        "const section = box.querySelector('#s'); "
        "section.insertAdjacentHTML('afterbegin', '<em></em>'); "
        "section.insertAdjacentHTML('beforeend', '<strong></strong>'); "
        "out.push([section.firstElementChild.localName, "
        "section.lastElementChild.localName]); "
        "section.insertAdjacentHTML('afterend', '<footer></footer>'); "
        "out.push(box.lastElementChild.localName); "
        "return out; })()"
    )
    assert result == [
        "item one",
        "s",
        2,
        2,
        2,
        [True, False, "item two", "s", True],
        ["em", "strong"],
        "footer",
    ]


async def test_html_element_reflected_title_and_hidden(page):
    result = page.evaluate(
        "(() => { const div = document.createElement('div'); "
        "div.title = 'tt'; div.hidden = true; "
        "const on = [div.title, div.getAttribute('title'), "
        "div.hidden, div.hasAttribute('hidden')]; "
        "div.hidden = false; "
        "return [on, div.hidden, div.hasAttribute('hidden')]; })()"
    )
    assert result == [["tt", "tt", True, True], False, False]


