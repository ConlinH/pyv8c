import pytest
from pyv8c import pyv8

from pyv8c.async_api import Browser

PAGE_HTML = "<body><div id='root'></div></body>"


@pytest.fixture
async def page():
    browser = (await Browser(stub_logging="off").__aenter__())
    instance = browser.new_profile().new_page()
    (await instance.set_content(PAGE_HTML, wait_until="commit"))
    try:
        yield instance
    finally:
        (await browser.close())


async def test_node_type_constants_and_constructor_identity(page):
    result = page.evaluate(
        "(() => { const div = document.createElement('div'); "
        "const text = document.createTextNode('x'); "
        "return [Node.ELEMENT_NODE, Node.TEXT_NODE, Node.COMMENT_NODE, "
        "Node.DOCUMENT_NODE, Node.DOCUMENT_FRAGMENT_NODE, "
        "div instanceof Node, div instanceof Element, "
        "text instanceof Node, text instanceof Element, "
        "document instanceof Node]; })()"
    )
    assert result == [1, 3, 8, 9, 11, True, True, True, False, True]


async def test_node_type_and_name_identity(page):
    result = page.evaluate(
        "(() => { const div = document.createElement('div'); "
        "const text = document.createTextNode('x'); "
        "const comment = document.createComment('c'); "
        "const frag = document.createDocumentFragment(); "
        "return [div.nodeType, div.nodeName, div.nodeValue, "
        "text.nodeType, text.nodeName, "
        "comment.nodeType, comment.nodeName, comment.data, "
        "frag.nodeType, frag.nodeName, "
        "document.nodeType, document.nodeName, document.body.nodeName]; })()"
    )
    assert result == [1, "DIV", pyv8.Null, 3, "#text", 8, "#comment", "c",
                      11, "#document-fragment", 9, "#document", "BODY"]


async def test_node_tree_navigation_and_mutation(page):
    result = page.evaluate(
        "(() => { const root = document.getElementById('root'); "
        "const a = document.createElement('a'); "
        "const b = document.createElement('b'); "
        "const span = document.createElement('span'); "
        "root.appendChild(a); root.appendChild(b); "
        "const replaced = root.replaceChild(span, a); "
        "const removed = root.removeChild(b); "
        "const t = document.createTextNode('t'); root.appendChild(t); "
        "return [root.childNodes.length, root.firstChild === span, "
        "root.lastChild === t, root.firstChild.previousSibling === null, "
        "span.parentNode === root, span.parentElement === root, "
        "span.nextSibling === t, t.previousSibling === span, "
        "t.nextSibling === null, t.parentNode === root, "
        "replaced === a, removed === b, a.parentNode === null]; })()"
    )
    assert result == [2, True, True, True, True, True, True, True, True,
                      True, True, True, True]


async def test_moving_existing_children_repositions_them(page):
    # DOM permits relocating a node this parent already owns; only inserting
    # an inclusive ancestor of the parent must raise HierarchyRequestError.
    result = page.evaluate(
        "(() => { const root = document.getElementById('root'); "
        "const a = document.createElement('a'); "
        "const b = document.createElement('b'); "
        "const c = document.createElement('c'); "
        "root.append(a, b, c); "
        "const moved = root.insertBefore(c, a); "
        "root.appendChild(a); "
        "const swapped = root.replaceChild(c, b); "
        "const names = []; "
        "for (let i = 0; i < root.children.length; i++) "
        "names.push(root.children[i].localName); "
        "return [names, moved === c, swapped === b, root.children.length]; })()"
    )
    assert result == [["c", "a"], True, True, 2]


async def test_inserting_inclusive_ancestor_raises(page):
    result = page.evaluate(
        "(() => { const root = document.getElementById('root'); "
        "const child = document.createElement('i'); root.appendChild(child); "
        "const attempt = (fn) => { "
        "try { fn(); return 'no-error'; } catch (e) { return e.name; } }; "
        "return [attempt(() => root.appendChild(root)), "
        "attempt(() => child.appendChild(root)), "
        "attempt(() => child.appendChild(child)), "
        "attempt(() => root.appendChild(null)), "
        "attempt(() => root.appendChild(document.createElement('p')))]; })()"
    )
    assert result == ["HierarchyRequestError", "HierarchyRequestError",
                      "HierarchyRequestError", "TypeError", "no-error"]


async def test_text_content_and_normalize(page):
    result = page.evaluate(
        "(() => { const root = document.getElementById('root'); "
        "root.append(document.createTextNode('a'), "
        "document.createTextNode(''), document.createTextNode('b')); "
        "const before = [root.childNodes.length, root.textContent]; "
        "root.normalize(); "
        "const after = [root.childNodes.length, root.textContent, "
        "root.firstChild.nodeType]; "
        "const holder = document.createElement('p'); "
        "holder.textContent = 'hello'; "
        "const setViaTextContent = [holder.childNodes.length, "
        "holder.textContent, holder.firstChild.nodeType]; "
        "holder.textContent = ''; "
        "return [before, after, setViaTextContent, "
        "holder.childNodes.length, holder.textContent]; })()"
    )
    assert result == [[3, "ab"], [1, "ab", 3], [1, "hello", 3], 0, ""]


async def test_clone_node_keeps_tree_shape(page):
    result = page.evaluate(
        "(() => { const root = document.getElementById('root'); "
        "const span = document.createElement('span'); "
        "span.setAttribute('id', 'x'); span.textContent = 'hi'; "
        "root.appendChild(span); "
        "const shallow = root.cloneNode(false); "
        "const deep = root.cloneNode(true); "
        "const child = deep.firstChild; "
        "return [shallow.childNodes.length, deep.childNodes.length, "
        "child.nodeType, child.localName, "
        "child.getAttribute('id'), child.textContent, "
        "deep === root]; })()"
    )
    assert result == [0, 1, 1, "span", "x", "hi", False]


async def test_to_string_tag_follows_the_generated_interface(page):
    assert page.evaluate(
        "Object.prototype.toString.call(document.createElement('div'))"
    ) == "[object HTMLDivElement]"


async def test_node_identity_equality_and_document_position(page):
    result = page.evaluate(
        "(() => { const rootEl = document.createElement('div'); "
        "const leaf = document.createElement('span'); "
        "const other = document.createElement('i'); "
        "rootEl.appendChild(leaf); rootEl.appendChild(other); "
        "const offTree = document.createElement('p'); "
        "document.body.appendChild(rootEl); "
        "return [rootEl.isConnected, leaf.isConnected, offTree.isConnected, "
        "leaf.getRootNode() === document, "
        "rootEl.compareDocumentPosition(leaf) & "
        "Node.DOCUMENT_POSITION_CONTAINED_BY ? 1 : 0, "
        "leaf.compareDocumentPosition(rootEl) & "
        "Node.DOCUMENT_POSITION_CONTAINS ? 1 : 0, "
        "leaf.compareDocumentPosition(other) & "
        "Node.DOCUMENT_POSITION_FOLLOWING ? 1 : 0, "
        "other.compareDocumentPosition(leaf) & "
        "Node.DOCUMENT_POSITION_PRECEDING ? 1 : 0, "
        "leaf.compareDocumentPosition(offTree) & "
        "Node.DOCUMENT_POSITION_DISCONNECTED ? 1 : 0, "
        "rootEl.contains(leaf), rootEl.contains(offTree), "
        "rootEl.hasChildNodes(), leaf.isSameNode(leaf), "
        "leaf.isSameNode(other), "
        "leaf.isEqualNode(leaf.cloneNode(true)), "
        "leaf.isEqualNode(other)]; })()"
    )
    assert result == [True, True, False, True, 1, 1, 1, 1, 1,
                      True, False, True, True, False, True, False]
