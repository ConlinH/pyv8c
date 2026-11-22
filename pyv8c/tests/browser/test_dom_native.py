"""The construction kernel keeps Python customization and parser boundaries."""

import gc
import hashlib
import weakref
from pathlib import Path

import pytest
from pyv8c.web.dom.events import EventTarget
from pyv8c.web.dom.nodes import Node

from pyv8c import pyv8
from pyv8c._native import dom


async def test_loaded_dom_kernel_matches_its_source():
    source = Path(__file__).resolve().parents[3] / "pyv8c/native/dom/builder.cpp"
    assert dom.SOURCE_SHA256 == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize("base", [EventTarget, Node])
async def test_native_initialization_preserves_overriding_setters(base):
    assignments = []

    class Target(base):
        def __setattr__(self, name, value):
            assignments.append(name)
            super().__setattr__(name, value)

    target = Target()
    expected = ["_services", "_listeners", "_listener_order", "_handlers"]
    if base is Node:
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
    view = node.get_childNodes()
    node._children.append(child)
    assert view.get_length() == 1 and view.fn_item(0) is child
    node._children = []
    assert view.get_length() == 0
    reference = weakref.ref(node)
    del view, node, child
    gc.collect()
    assert (reference()) is None


@pytest.mark.parametrize("name", ["initialize_target", "initialize_node", "build_batch"])
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
