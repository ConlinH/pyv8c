"""The construction kernel keeps Python customization and parser boundaries."""

import gc
import json
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyv8c.web.dom.character_data import Text
from pyv8c.web.dom.element import Element, _AttributeMap
from pyv8c.web.dom.events import EventTarget
from pyv8c.web.dom.nodes import Node
from pyv8c.web.html import parser as html_parser

from pyv8c import pyv8
from pyv8c._native import dom
from tools.dom_source import dom_source_sha256


async def test_loaded_dom_kernel_matches_its_source():
    source = Path(__file__).resolve().parents[3] / "pyv8c/native/dom"
    assert dom.SOURCE_SHA256 == dom_source_sha256(source)


def test_element_name_normalization_keeps_ascii_rules_and_custom_translation():
    ascii_element = Element('MiXeD-12')
    unicode_element = Element('MiXeD-Éβ')
    qualified_element = Element('p:MiXeD-Éβ', namespace='http://www.w3.org/1999/xhtml',
                                prefix='p', local_name='MiXeD-Éβ')
    assert (ascii_element.localName, ascii_element.nodeName) == ('mixed-12', 'MIXED-12')
    assert (unicode_element.localName, unicode_element.nodeName) == ('mixed-Éβ', 'MIXED-Éβ')
    assert (qualified_element.localName, qualified_element.nodeName) == ('MiXeD-Éβ', 'P:MIXED-Éβ')

    seen = []

    class ObservableName(str):
        def __str__(self):
            return self

        def translate(self, table):
            seen.append(table[ord('M')])
            return super().translate(table)

    custom = Element(ObservableName('MiXeD'))
    assert seen == [ord('m')]
    assert (custom.localName, custom.nodeName) == ('mixed', 'MIXED')


def test_native_attribute_batch_handles_unicode_empty_values_and_unsupported_inputs():
    rows = [{"x": "商店😀-en", "empty": ""}, {"x": "😀-en", "empty": ""},
            {"x": "商店😀-en"}, {"x": "商店😀-fr", "empty": ""}]
    rules = (("x", "^=", "商店"), ("x", "$=", "-en"), ("x", "*=", "😀"),
             ("x", "|=", "商店😀"), ("empty", None, ""), ("empty", "=", ""))
    nodes = tuple(Element("article", attributes=row) for row in rows)

    def match(candidates, predicates):
        return dom.match_attribute_batch(candidates, "article", predicates, Element, _AttributeMap,
                                         (Element.attributes, object.__getattribute__, EventTarget.__getattr__))

    assert match(nodes, rules)[0] is nodes[0]
    assert len(match(nodes, rules)) == 1
    assert match(nodes, (("x", "*=", ""),)) == []
    nul = Element("article", attributes={"x": "a\0b"})
    assert match((nul,), (("x", "$=", "\0b"),)) == [nul]
    surrogate = Element("article", attributes={"x": "\ud800"})
    assert match((surrogate,), (("x", "=", "x"),)) is None
    assert match(nodes, (("x", "~=", "😀-en"),)) is None
    nodes[0].attributes = {"x": "replacement"}
    assert match(nodes, rules) == []
    assert match(nodes, (("x", "=", "replacement"),)) == [nodes[0]]
    long_value = Element("article", attributes={"x": "商" * 100_000 + "-end"})
    assert match((long_value,), (("x", "^=", "商"), ("x", "$=", "-end"))) == [long_value]

    class CustomElement(Element):
        @property
        def attributes(self):
            return _AttributeMap(self, {"x": "from getter"})

    custom = CustomElement("article", attributes={"x": "from storage"})
    assert match((custom,), (("x", "=", "from getter"),)) == [custom]


async def test_native_selector_keeps_chunk_order_shadow_scope_and_prototypes(page, monkeypatch):
    calls = []
    projections = []
    original = dom.match_attribute_batch
    projected = dom.match_attribute_projection

    def count(nodes, *args):
        calls.append(nodes)
        return original(nodes, *args)

    monkeypatch.setattr(dom, "match_attribute_batch", count)

    def track(projection, nodes, predicates):
        projections.append((projection, nodes))
        return projected(projection, nodes, predicates)

    monkeypatch.setattr(dom, "match_attribute_projection", track)
    matches = {0, 4095, 4096, 4199}
    markup = '<div id="host"></div>' + ''.join(
        f'<article id="r{i}" data-code="价{i}" data-status="{"on" if i in matches else "off"}"></article>'
        for i in range(4200)
    )
    await page.set_content(markup, wait_until="commit")
    assert page.evaluate("""(() => {
        const selector = 'article[data-code^="价"][data-status="on"]';
        const node = document.getElementById('r4096'), proto = Object.getPrototypeOf(node);
        const before = Object.getOwnPropertyDescriptors(proto);
        const token = Symbol('batch');
        Object.defineProperty(proto, token, {value: node, configurable: true});
        const original = document.querySelectorAll(selector);
        const ids = list => [...list].map(n => n.id).join(',');
        const correct = ids(original) === 'r0,r4095,r4096,r4199' && original[2] === node;
        const host = document.getElementById('host'), shadow = host.attachShadow({mode:'open'});
        shadow.innerHTML = '<article data-code="价shadow" data-status="on"></article>'.repeat(260);
        const shadowBefore = shadow.querySelectorAll(selector);
        const shadowHot = shadow.querySelectorAll(selector);
        shadow.firstChild.removeAttribute('data-status');
        const isolated = shadowBefore instanceof NodeList && shadowBefore.length === 260 &&
            shadowHot[0] === shadow.firstChild && shadow.querySelectorAll(selector).length === 259 &&
            document.querySelectorAll(selector).length === 4;
        shadow.innerHTML = '<article data-code="价new" data-status="on"></article>'.repeat(260);
        const replacement = shadow.querySelectorAll(selector);
        const replaced = replacement instanceof NodeList && replacement.length === 260
            && shadow.querySelectorAll(selector).item(0) === replacement.item(0)
            && shadowBefore.length === 260 && shadowBefore.item(0) !== replacement.item(0)
            && !shadowBefore.item(0).isConnected;
        document.getElementById('r4095').setAttribute('data-status', 'off');
        const afterEdit = ids(document.querySelectorAll(selector));
        document.body.prepend(node);
        const later = document.querySelectorAll(selector);
        const after = Object.getOwnPropertyDescriptors(proto);
        const intact = original instanceof NodeList && later instanceof NodeList
            && later.item(0) === node && node[token] === node
            && Object.getPrototypeOf(node) === proto && node instanceof HTMLElement
            && Object.keys(before).length === Object.keys(after).length
            && Object.keys(before).every(name =>
                Reflect.ownKeys(before[name]).every(key => before[name][key] === after[name][key]));
        delete proto[token];
        return [correct, isolated, replaced, afterEdit, ids(later), ids(original), intact];
    })()""") == [True, True, True, "r0,r4096,r4199", "r4096,r0,r4199", "r0,r4095,r4096,r4199", True]
    assert [len(nodes) for nodes in calls] == [4200, 260, 260, 4200]
    assert [len(nodes) for _, nodes in projections] == [260, 260, 4200, 260, 4200]
    assert projections[0][0] is projections[1][0]
    assert projections[2][0] is projections[4][0]
    assert projections[0][0] is not projections[3][0] and projections[3][1] is calls[2]
    assert projections[0][0] is not projections[2][0]
    assert projections[2][1] is calls[0] and projections[2][1] is not calls[-1]


@pytest.mark.parametrize('suffix', [
    'words&amp;rest', '<article data-label="a&amp;b">', '<!-- note -->',
    '<?process?>', '<article a="ok"broken>', '<article a=ends/>', '</article junk>', '<article',
])
async def test_fragment_build_resumes_from_whole_unsupported_token(page, monkeypatch, suffix):
    await page.set_content('<main></main>', wait_until="load")
    published = []
    feed = html_parser.DOMParser.feed

    def track(parser, remainder):
        published.append((remainder, len(parser.stack[0]._children)))
        return feed(parser, remainder)

    monkeypatch.setattr(html_parser.DOMParser, 'feed', track)
    prefix = '<article>中文😀</article>' * 240
    script = f"""(() => {{
        const main = document.querySelector('main');
        main.innerHTML = {json.dumps(prefix + suffix)};
        return main.innerHTML;
    }})()"""
    actual = page.evaluate(script)
    assert published == [(suffix, 240)]
    monkeypatch.setattr(html_parser, 'fragment_classes', lambda *args: None)
    assert page.evaluate(script) == actual


async def test_fragment_build_leaves_surrogates_to_htmlparser(page):
    await page.set_content('<main></main>', wait_until="load")

    def parse():
        main = page.document.fn_querySelector('main')
        source = '<article>中文</article>' * 240 + '<span>\ud800</span>'
        classes = html_parser.fragment_classes(main, page.services)
        assert classes
        parser = html_parser._BatchFragmentParser(page.services, main, classes)
        assert parser._native_prefix(source) is source
        assert not main._children
        parser.feed(source)
        parser.close()
        parser.flush()
        return main._children[-1].get_textContent()

    assert page.executor.call(parse) == '\ud800'


async def test_fragment_batch_keeps_static_snapshots_and_shadow_scope(page, monkeypatch):
    calls = []
    build = dom.build_fragment

    def track(*args):
        result = build(*args)
        calls.append(result)
        return result

    monkeypatch.setattr(dom, "build_fragment", track)
    await page.set_content('<main></main><div id="host"></div>', wait_until="load")

    def no_python_tokens(*args):
        pytest.fail('the ordinary fragment took the Python token path')

    monkeypatch.setattr(html_parser.DOMParser, "feed", no_python_tokens)
    monkeypatch.setattr(dom, "build_batch", no_python_tokens)
    markup = '<ARTICLE id=row data-state=on DATA-LABEL="中文 > 值" hidden data-empty="">段落😀<span/>结尾</ARTICLE>' * 350
    assert page.evaluate(f"""(() => {{
        const markup = {json.dumps(markup)}, selector = 'article[data-state][id="row"]';
        const main = document.querySelector('main');
        const shadow = document.getElementById('host').attachShadow({{mode: 'open'}});
        main.innerHTML = markup;
        shadow.innerHTML = markup;
        const before = document.querySelectorAll(selector), old = before.item(0);
        const shadowBefore = shadow.querySelectorAll(selector);
        const proto = Object.getPrototypeOf(old), marker = Symbol();
        Object.defineProperty(proto, marker, {{value: 'kept', configurable: true}});
        try {{
            main.innerHTML = markup;
            const after = document.querySelectorAll(selector), node = after.item(0);
            return before instanceof NodeList && after instanceof NodeList && before.length === 350
                && after.length === 350 && shadowBefore.length === 350 && before.item(0) === old
                && old.parentNode === null && old !== node && node === document.getElementById('row')
                && shadowBefore.item(0) === shadow.querySelector(selector) && shadowBefore.item(0) !== node
                && node.ownerDocument === document && node.getRootNode() === document
                && shadowBefore.item(0).getRootNode() === shadow
                && Object.getPrototypeOf(node) === proto && node[marker] === 'kept'
                && node.childNodes.length === 3 && node.childNodes.item(1).localName === 'span'
                && node.textContent === '段落😀结尾' && node.getAttribute('data-label') === '中文 > 值'
                && node.getAttribute('hidden') === '' && node.getAttribute('data-empty') === '';
        }} finally {{
            delete proto[marker];
        }}
    }})()""") is True
    assert len(calls) >= 6
    assert calls.count((-1, False, False)) == 3
    assert all(not fallback and not boundary for _, fallback, boundary in calls)


@pytest.mark.parametrize("entry", ["document", "fragment"])
async def test_parser_preserves_an_overridden_registry_factory(page, monkeypatch, entry):
    from pyv8c.web.html import registry

    calls = []
    original = registry.class_for_tag

    def element_class(tag):
        if tag == "article":
            calls.append(tag)
        return original(tag)

    markup = "<article>content</article>" * 200
    if entry == "document":
        monkeypatch.setattr(registry, "class_for_tag", element_class)
        await page.set_content("<body>" + markup, wait_until="commit")
    else:
        await page.set_content("<main></main>", wait_until="load")
        monkeypatch.setattr(registry, "class_for_tag", element_class)
        page.evaluate(f"document.querySelector('main').innerHTML = {json.dumps(markup)}")
    assert page.evaluate("document.querySelectorAll('article').length") == 200
    assert calls == ["article"] * 200


async def test_fragment_batch_replays_inline_handlers_and_foreign_markup(page, monkeypatch):
    calls = []
    build = dom.build_fragment

    def track(*args):
        result = build(*args)
        calls.append(result)
        return result

    monkeypatch.setattr(dom, "build_fragment", track)
    await page.set_content('<main></main>', wait_until="load")
    prefix = '<p data-x="价">start😀<section>nested<em/>text</section>' * 90
    tail = ('<article ONCLICK="globalThis.fragmentClick = true">click</article>'
            '<svg><title>title</title></svg><!--keep--><template><span>&amp;amp;😀</span></template>')
    script = f"""(() => {{
        const main = document.querySelector('main');
        globalThis.fragmentClick = false;
        main.innerHTML = {json.dumps(prefix + tail)};
        main.querySelector('[onclick]').click();
        return [main.innerHTML, fragmentClick, main.querySelector('svg title').namespaceURI,
                main.querySelector('template').content.firstChild.localName];
    }})()"""
    actual = page.evaluate(script)
    assert calls[-1] == (len(prefix.encode()), True, True)
    monkeypatch.setattr(html_parser, "fragment_classes", lambda *args: None)
    assert page.evaluate(script) == actual
    assert actual[1:] == [True, "http://www.w3.org/2000/svg", "span"]


async def test_fragment_lexical_fallback_keeps_open_element_and_entity_boundaries(page, monkeypatch):
    await page.set_content('<main></main>', wait_until="load")
    remainders = []
    feed = html_parser.DOMParser.feed

    def track(parser, text):
        remainders.append(text)
        return feed(parser, text)

    monkeypatch.setattr(html_parser.DOMParser, 'feed', track)
    prefix = '<article>序😀</article>' * 240 + '<article id="boundary">'
    suffix = '左&amp;amp;😀</article><article id="later">后</article>'
    assert page.evaluate(f"""(() => {{
        const main = document.querySelector('main');
        main.innerHTML = {json.dumps(prefix + suffix)};
        const node = document.getElementById('boundary');
        return [main.children.length, node.parentNode === main,
                [...node.childNodes].map(child => child.nodeValue), node.nextSibling.id];
    }})()""") == [242, True, ['左', '&', 'amp;😀'], 'later']
    assert remainders == [suffix]


async def test_native_fragment_invalidates_caches_repopulated_during_construction(page, monkeypatch):
    from pyv8c.web.dom import element

    await page.set_content('<main></main>', wait_until="load")
    upper = element._ascii_upper
    started = 0
    observed = []
    selector = 'article[data-state][id]'

    def observe(name):
        nonlocal started
        if name == 'article':
            started += 1
            if started in (2, 3, 300, 301, 342, 343):
                document = page.document
                nodes = document.fn_querySelectorAll(selector)
                again = document.fn_querySelectorAll(selector)
                descendants = sum(isinstance(node, Element) and node.localName == 'article'
                                  for node in document._descendant_snapshot())
                previous = document.fn_getElementById(f'r{started - 2}')
                unseen = document.fn_getElementById(f'r{started - 1}')
                observed.append((started, nodes.get_length(), again.get_length(), descendants,
                                 previous is nodes.fn_item(started - 2), unseen is pyv8.Null,
                                 document.layout_version))
        return upper(name)

    monkeypatch.setattr(element, '_ascii_upper', observe)
    original = dom.build_fragment
    batches = []

    def track(*args):
        batches.append(args[1])
        return original(*args)

    monkeypatch.setattr(dom, 'build_fragment', track)
    markup = ''.join(f'<article id="r{i}" data-state="on">text</article>' for i in range(420))
    assert page.evaluate(f"""(() => {{
        document.querySelector('main').innerHTML = {json.dumps(markup)};
        return document.querySelectorAll('{selector}').length;
    }})()""") == 420
    assert len(batches) == 2
    assert [row[:-1] for row in observed] == [
        (i, i - 1, i - 1, i - 1, True, True) for i in (2, 3, 300, 301, 342, 343)
    ]
    assert [row[-1] - observed[0][-1] for row in observed] == [0, 2, 596, 598, 680, 682]


async def test_native_fragment_refreshes_layout_between_constructor_callbacks(page, monkeypatch):
    from pyv8c.web.dom import element

    await page.set_content('''<style>
        main, article { display: block; margin: 0; padding: 0; border: 0 }
        article { height: 7px }
        </style><main></main>''', wait_until="load")
    upper = element._ascii_upper
    started = 0
    observed = []
    checkpoints = (2, 3, 342, 343)

    def observe(name):
        nonlocal started
        if name == 'article':
            started += 1
            if started in checkpoints:
                document = page.document
                main = document.fn_querySelector('main')
                invalidated = document._layout_snapshot is None
                height = main.get_offsetHeight()
                cached = document._layout_snapshot
                observed.append((started, height, invalidated, cached.version[0] == document.layout_version))
        return upper(name)

    monkeypatch.setattr(element, '_ascii_upper', observe)
    native = dom.build_fragment
    batches = []

    def track(*args):
        batches.append(args[1])
        return native(*args)

    monkeypatch.setattr(dom, 'build_fragment', track)
    markup = '<article>text</article>' * 420
    assert page.evaluate(f"""(() => {{
        const main = document.querySelector('main');
        main.innerHTML = {json.dumps(markup)};
        return main.offsetHeight;
    }})()""") == 420 * 7
    assert len(batches) == 2
    assert observed == [(index, (index - 1) * 7, True, True) for index in checkpoints]


def test_layout_invalidation_reads_version_once_and_propagates_write_failure():
    class LayoutState:
        reads = 0
        revision = 0
        fail_on_write = False
        _layout_snapshot = object()

        @property
        def layout_version(self):
            self.reads += 1
            return self.revision

        @layout_version.setter
        def layout_version(self, value):
            if self.fail_on_write:
                raise AttributeError('version write failed')
            self.revision = value

    document = LayoutState()
    service = SimpleNamespace(document=document)
    node = Node(services=SimpleNamespace(document_services=service))
    node._invalidate_layout()
    assert (document.reads, document.revision, document._layout_snapshot) == (1, 1, None)
    document.fail_on_write = True
    with pytest.raises(AttributeError, match='version write failed'):
        node._invalidate_layout()

    service.document = SimpleNamespace(_layout_snapshot=object())
    snapshot = service.document._layout_snapshot
    node._invalidate_layout()
    assert service.document._layout_snapshot is snapshot


async def test_fragment_replacement_preserves_node_identity_and_observer_records(page):
    await page.set_content('<main><article id="row">old</article></main>', wait_until="load")
    assert page.evaluate("""(() => {
        const main = document.querySelector('main');
        const before = main.querySelectorAll('article'), old = before.item(0);
        const proto = Object.getPrototypeOf(old), marker = Symbol();
        Object.defineProperty(proto, marker, {value: 'kept', configurable: true});
        const observer = new MutationObserver(() => {});
        observer.observe(main, {childList: true});
        try {
            main.innerHTML = '<article id="row" data-pad="' + 'x'.repeat(4096)
                + '">new</article><article>next</article>';
            const after = main.querySelectorAll('article');
            const records = observer.takeRecords();
            const removed = records.flatMap(record => [...record.removedNodes]);
            const added = records.flatMap(record => [...record.addedNodes]);
            return before instanceof NodeList && before.length === 1 && before.item(0) === old
                && old.parentNode === null && old.firstChild.parentNode === old && old.textContent === 'old'
                && after instanceof NodeList && after.length === 2 && after.item(0) !== old
                && after.item(0) === document.getElementById('row') && after.item(0).textContent === 'new'
                && Object.getPrototypeOf(after.item(0)) === proto && after.item(0)[marker] === 'kept'
                && removed.length === 1 && removed[0] === old
                && added.length === 2 && added[0] === after.item(0) && added[1] === after.item(1);
        } finally {
            observer.disconnect();
            delete proto[marker];
        }
    })()""") is True


async def test_lazy_child_views_and_static_snapshots_survive_replacement_and_gc(page):
    await page.set_content('<main><article id="old">light</article></main><div id="host"></div>',
                           wait_until="load")
    page.evaluate("""(() => {
        const main = document.querySelector('main');
        const shadow = document.getElementById('host').attachShadow({mode: 'open'});
        shadow.innerHTML = '<article id="hidden-old">shadow</article>';
        const marker = Symbol();
        Object.defineProperty(NodeList.prototype, marker, {value: 'kept', configurable: true});
        globalThis.savedViews = {main, shadow, marker, lightView: main.childNodes, shadowView: shadow.childNodes,
            lightSnapshot: main.querySelectorAll('article'), shadowSnapshot: shadow.querySelectorAll('article')};
        main.innerHTML = '<article id="new">replacement</article><span></span>';
        shadow.innerHTML = '<article id="hidden-new">replacement</article>';
    })()""")
    page.executor.call(gc.collect)
    assert page.evaluate("""(() => {
        const {main, shadow, marker, lightView, shadowView, lightSnapshot, shadowSnapshot} = savedViews;
        try {
            const old = lightSnapshot.item(0), oldShadow = shadowSnapshot.item(0), oldView = old.childNodes;
            const oldText = oldView.item(0);
            old.appendChild(document.createElement('em'));
            return main.childNodes === lightView && lightView.length === 2 && lightView.item(0).id === 'new'
                && shadow.childNodes === shadowView && shadowView.length === 1 && shadowView.item(0).id === 'hidden-new'
                && old.parentNode === null && oldShadow.parentNode === null && old.id === 'old'
                && oldShadow.id === 'hidden-old' && oldShadow.textContent === 'shadow'
                && old.childNodes === oldView && oldView.length === 2 && oldView.item(0) === oldText
                && oldText.parentNode === old && oldText.nodeValue === 'light'
                && oldText.childNodes === oldText.childNodes && oldText.childNodes.length === 0
                && oldText.childNodes[marker] === 'kept'
                && lightSnapshot.length === 1 && lightSnapshot.item(0) === old && lightView.item(0) !== old
                && Object.getPrototypeOf(oldView) === NodeList.prototype && oldView[marker] === 'kept'
                && lightSnapshot[marker] === 'kept' && shadowView[marker] === 'kept'
                && document.querySelector('#hidden-new') === null && shadow.querySelector('#new') === null;
        } finally {
            delete NodeList.prototype[marker];
            delete globalThis.savedViews;
        }
    })()""") is True


@pytest.mark.parametrize("base", [EventTarget, Node, Text])
async def test_native_initialization_preserves_overriding_setters(base):
    assignments = []

    class Target(base):
        def __setattr__(self, name, value):
            assignments.append(name)
            super().__setattr__(name, value)

    target = Target()
    expected = ["_services", "_listeners", "_listener_order", "_handlers"]
    if issubclass(base, Node):
        expected += [
            "nodeType",
            "nodeName",
            "nodeValue",
            "parentNode",
            "_children",
            "_child_nodes_view",
        ]
    assert assignments == expected
    assert target._listeners == {} and target._handlers == {}


async def test_native_initialization_invokes_data_descriptors():
    assignments = []

    class Target(Node):
        @property
        def nodeName(self):
            return self.__dict__["name"]

        @nodeName.setter
        def nodeName(self, value):
            assignments.append(value)
            self.__dict__["name"] = value

    node = Target(node_name="example")
    assert node.nodeName == "example" and assignments == ["example"]


async def test_descriptor_can_change_the_setter_for_remaining_initialization():
    assignments = []

    def changed(self, name, value):
        assignments.append(name)
        EventTarget.__setattr__(self, name, value)

    class Target(Node):
        @property
        def nodeName(self):
            return self.__dict__["name"]

        @nodeName.setter
        def nodeName(self, value):
            self.__dict__["name"] = value
            type(self).__setattr__ = changed

    node = Target(node_name="changed")
    assert node.nodeName == "changed"
    assert assignments == ["nodeValue", "parentNode", "_children", "_child_nodes_view"]


async def test_failed_native_initialization_preserves_error_and_releases_partial_node():
    seen = []
    error = ValueError("descriptor failed")

    class Target(Node):
        @property
        def nodeName(self):
            return ""

        @nodeName.setter
        def nodeName(self, value):
            seen.append(weakref.ref(self))
            raise error

    try:
        Target()
    except ValueError as caught:
        assert caught is error
    else:
        pytest.fail("initialization succeeded")
    error.__traceback__ = None
    gc.collect()
    assert len(seen) == 1 and seen[0]() is None


async def test_native_child_getter_is_live_and_its_cycles_are_collectible():
    node = Node()
    child = Node()
    assert "_child_nodes_view" not in node.__dict__
    view = node.get_childNodes()
    assert node.get_childNodes() is view
    node._children.append(child)
    assert view.get_length() == 1 and view.fn_item(0) is child
    node._children = []
    assert view.get_length() == 0
    reference = weakref.ref(node)
    del node, child
    gc.collect()
    assert reference() is not None
    del view
    gc.collect()
    assert reference() is None


async def test_text_child_storage_materializes_on_first_write():
    text = Text('first')
    child = Text('second')
    empty_storage = text._children
    assert empty_storage is child._children
    view = text.get_childNodes()
    text._replace_children(())
    assert text._children is empty_storage and view.get_length() == 0

    text.append_child(child)
    assert text._children is not empty_storage and child._children is empty_storage
    assert text.get_childNodes() is view and view.fn_item(0) is child and child.parentNode is text
    text.fn_removeChild(child)
    assert text.get_childNodes() is view and view.get_length() == 0 and child.parentNode is None


@pytest.mark.parametrize("name", ["initialize_target", "initialize_node", "create_child_nodes_view", "build_batch"])
async def test_invalid_kernel_call_raises_instead_of_crashing(name):
    with pytest.raises(TypeError):
        getattr(dom, name)()


async def test_document_uses_bounded_native_runs(page, monkeypatch):
    native = dom.build_batch
    sizes = []

    def build(*args):
        start = args[1]
        end = native(*args)
        sizes.append(end - start)
        return end

    monkeypatch.setattr(dom, "build_batch", build)
    (await page.set_content("<body>" + "<div><span>a&amp;b</span></div>" * 600, wait_until="commit"))
    assert page.evaluate("document.querySelectorAll('div > span').length") == 600
    assert page.evaluate("document.body.firstChild.textContent") == "a&b"
    assert sum(sizes) >= 3000 and max(sizes) <= 1024
    assert page.navigation.parser.done and not page.navigation.parser.tokens


async def test_attribute_names_do_not_require_utf8_encoding(page):
    name = "data-\ud800"
    (await page.set_content("<body><div " + name + '="value"></div>', wait_until="commit"))
    assert (
        page.executor.call(lambda: page.document.get_body()._children[0].attributes[name])
        == "value"
    )


@pytest.mark.parametrize(
    "markup",
    [
        "<body><p>a<div>b</div><p>c<li>d<li>e<BR>f</BR><DIV>g</DIV>",
        '<body><div id=a id=b data-x="&amp;&#x41;" title="x > y">雪🙂&lt;</div>',
        "<body><template><div><span>x</span></div>after</template><div id=end>last</div>",
        "<body><svg><foreignObject><div>x</div></foreignObject></svg><div>end</div>",
        "<body><math><mtext><div>x</div></mtext></math><div>end</div>",
        "<body><div>before<!--comment--><b>after</b></div><textarea>&lt;x&gt;</textarea>",
        "<body><div>before</div><style>div{color:red}</style><div>after</div>",
        "<body><div><span>one</div>two</span><i>three</i><div/>four",
    ],
)
async def test_native_tree_matches_semantic_parser_path(page, monkeypatch, markup):
    from pyv8c.web.html import parser as parser_module

    (await page.set_content(markup, wait_until="commit"))
    source = "document.body.innerHTML"
    actual = page.evaluate(source)
    reference = page.profile.new_page()
    try:
        monkeypatch.setattr(parser_module, "build_batch", lambda parser: parser.index)
        (await reference.set_content(markup, wait_until="commit"))
        assert actual == reference.evaluate(source)
    finally:
        (await reference.close())


async def test_script_sees_completed_prefix_and_document_write_preserves_order(page):
    prefix = "<div class=row>x</div>" * 300
    (await page.set_content("<body>" + prefix + """<script>
      globalThis.prefixCount=document.querySelectorAll('.row').length;
      globalThis.hasTail=!!document.getElementById('tail');
      document.write('<span id=written>inserted</span>');
    </script><div id=tail>tail</div>""", wait_until="commit"))
    assert page.evaluate(
        "[prefixCount,hasTail,document.getElementById('written').nextSibling.id]"
    ) == [300, False, "tail"]


async def test_constructor_callbacks_see_retired_tokens_and_fresh_lookup_caches(page, monkeypatch):
    from pyv8c.web.dom import element

    initialize = element._AttributeMap.__init__
    seen = []

    def init(attributes, owner, values):
        initialize(attributes, owner, values)
        if owner.localName == "div":
            parser = page.navigation.parser
            assert parser.index > 0 and parser.tokens[parser.index - 1] is None
            seen.append(len(page.document.fn_getElementsByTagName("div")._items()))

    monkeypatch.setattr(element._AttributeMap, "__init__", init)
    (await page.set_content("<body>" + "<div>x</div>" * 20, wait_until="commit"))
    assert seen == list(range(20))


async def test_instance_insertion_hooks_created_during_initialization_still_run(page, monkeypatch):
    from pyv8c.web.dom import element

    initialize = element._AttributeMap.__init__
    seen = []

    def init(attributes, owner, values):
        initialize(attributes, owner, values)
        if owner.localName == "span":
            owner._on_tree_inserted = lambda: seen.append(owner.isConnected)

    monkeypatch.setattr(element._AttributeMap, "__init__", init)
    (await page.set_content("<body>" + "<div><span>x</span></div>" * 5, wait_until="commit"))
    assert seen == [True] * 5


@pytest.mark.parametrize("method", ["append_child", "_adopt_child"])
async def test_constructor_can_customize_the_parents_insertion_method(page, monkeypatch, method):
    from pyv8c.web.dom import element

    initialize = element._AttributeMap.__init__
    seen = []

    def init(attributes, owner, values):
        initialize(attributes, owner, values)
        if owner.localName == "span":
            parent = page.navigation.parser.current
            original = getattr(parent, method)

            def insert(node, *args, **kwargs):
                seen.append(node.localName)
                return original(node, *args, **kwargs)

            setattr(parent, method, insert)

    monkeypatch.setattr(element._AttributeMap, "__init__", init)
    (await page.set_content("<body><div><span>text</span></div>", wait_until="commit"))
    assert seen == ["span"]
    assert page.evaluate("document.body.firstChild.textContent") == "text"


async def test_constructor_can_insert_more_tokens_at_the_current_parse_position(page, monkeypatch):
    from pyv8c.web.dom import element

    initialize = element._AttributeMap.__init__

    def init(attributes, owner, values):
        initialize(attributes, owner, values)
        if values.get("id") == "trigger":
            assert page.navigation.parser.write_markup("<i id=written>inserted</i>")

    monkeypatch.setattr(element._AttributeMap, "__init__", init)
    (await page.set_content("<body><div id=trigger>original</div><b id=tail>end</b>", wait_until="commit"))
    assert page.evaluate(
        "[document.getElementById('written').parentNode.id,document.getElementById('trigger').textContent,document.body.lastChild.id]"
    ) == ["trigger", "insertedoriginal", "tail"]


async def test_batch_does_not_enter_a_node_rejected_by_the_parent(page, monkeypatch):
    from pyv8c.web.dom import element

    initialize = element._AttributeMap.__init__

    def init(attributes, owner, values):
        initialize(attributes, owner, values)
        if values.get("id") == "skip":
            parent = page.navigation.parser.current
            append = parent.append_child
            parent.append_child = lambda node: node if node is owner else append(node)

    monkeypatch.setattr(element._AttributeMap, "__init__", init)
    await page.set_content("<body><div><span id=skip><b>hello</b></span></div>")
    assert page.evaluate("document.body.innerHTML") == "<div><b>hello</b></div>"


async def test_native_runs_recheck_operation_control_before_the_next_chunk(page, monkeypatch):
    control = page.services.document_services
    check = control.check_parser_operation
    checked = []

    def stop():
        checked.append(None)
        if len(checked) == 2:
            raise pyv8.JavaScriptTerminated()
        check()

    monkeypatch.setattr(control, "check_parser_operation", stop)
    with pytest.raises(pyv8.JavaScriptTerminated):
        (await page.set_content("<body>" + "<div>x</div>" * 800, wait_until="commit"))
    assert len(checked) == 2
    assert 0 < page.evaluate("document.querySelectorAll('div').length") < 800
    assert page.evaluate("42") == 42
