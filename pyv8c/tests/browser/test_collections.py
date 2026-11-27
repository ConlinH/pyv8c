"""Runtime-facing behavior for the standard DOM collection interfaces."""

import pytest

pytest.importorskip("pyv8c.pyv8")
from pyv8c import pyv8

from pyv8c.async_api import Browser
from pyv8c.web.dom.collections import HTMLAllCollection, HTMLCollection
from pyv8c.web.html.registry import HTML_CLASSES
from pyv8c.web.dom.element import Element

DOCUMENT = (
    "<html><head><title>t</title></head><body>"
    "<div id='app'>1</div>"
    "<p name='note'>2</p>"
    "<span id='dup'>3</span><span id='dup'>4</span>"
    "</body></html>"
)

# Tree order of DOCUMENT: html, head, title, body, div, p, span, span.
TAGS = ["HTML", "HEAD", "TITLE", "BODY", "DIV", "P", "SPAN", "SPAN"]


@pytest.fixture
async def browser():
    instance = (await Browser(stub_logging="off").__aenter__())
    try:
        yield instance
    finally:
        (await instance.close())


@pytest.fixture
async def page(browser):
    instance = browser.new_profile().new_page()
    (await instance.set_content(
        "<body><div id='root' class='  a\tb  a  '>"
        "<span id='first' name='named'></span>"
        "<span id='second'></span>"
        "</div></body>"
    , wait_until="commit"))
    return instance


@pytest.fixture
async def document_page(browser):
    """A page that has finished parsing DOCUMENT."""
    instance = browser.new_profile().new_page()
    (await instance.set_content(DOCUMENT, wait_until="commit"))
    (await instance.run_until_idle())
    return instance


async def test_node_list_has_live_readonly_indexed_properties(page):
    result = page.evaluate("""
(() => {
  const list = document.getElementById('root').childNodes;
  const descriptor = Object.getOwnPropertyDescriptor(list, '0');
  const keys = Object.keys(list);
  const assigned = (list[0] = 123);
  const deleted = delete list[0];
  const converted = [list.item('') === list[0], list.item('0x1') === list[1],
                     list.item(4294967296) === list[0]];
  list[99] = 'expando';
  const expando = list[99];
  const expandoDeleted = delete list[99];
  document.getElementById('root').appendChild(document.createElement('i'));
  return [
    list[0] === list.item(0), list.item(-1), list.item(NaN) === list[0],
    keys, descriptor, assigned, deleted, list[0].id,
    expando, expandoDeleted, list[99], list.length, Array.from(list).length,
    converted
  ];
})()
""")
    assert result[0:3] == [True, pyv8.Null, True]
    assert result[3] == ["0", "1"]
    assert result[4] == {
        "value": result[4]["value"],
        "writable": False,
        "enumerable": True,
        "configurable": True,
    }
    assert result[5:11] == [123, False, "first", "expando", True, pyv8.Undefined]
    assert result[11:] == [3, 3, [True, True, True]]


async def test_node_list_strict_mutations_use_chromium_error_context(page):
    result = page.evaluate("""
(() => {
  'use strict';
  const list = document.getElementById('root').childNodes;
  const errors = [];
  try { list[0] = 1; } catch (e) { errors.push([e.name, e.message]); }
  try { delete list[0]; } catch (e) { errors.push([e.name, e.message]); }
  return errors;
})()
""")
    assert result == [
        [
            "TypeError",
            "Failed to set an indexed property [0] on 'NodeList': "
            "Indexed property setter is not supported.",
        ],
        [
            "TypeError",
            "Failed to delete an indexed property [0] from 'NodeList': "
            "Indexed property deleter is not supported.",
        ],
    ]


async def test_legacy_readonly_property_reflect_set_reports_failure(page):
    assert page.evaluate("""(() => {
      const root = document.getElementById('root');
      return [Reflect.set(root.childNodes, '0', 1),
              Reflect.set(root.children, 'first', 1)];
    })()""") == [False, False]


async def test_node_list_exposes_web_idl_iterable_helpers(page):
    result = page.evaluate("""
(() => {
  const list = document.getElementById('root').childNodes;
  const visited = [];
  list.forEach((node, index, owner) => visited.push([node.id, index, owner === list]));
  return [
    Array.from(list.keys()),
    Array.from(list.values()).map(node => node.id),
    Array.from(list.entries()).map(pair => [pair[0], pair[1].id]),
    visited
  ];
})()
""")
    assert result == [
        [0, 1],
        ["first", "second"],
        [[0, "first"], [1, "second"]],
        [["first", 0, True], ["second", 1, True]],
    ]


async def test_a_query_repeated_after_a_mutation_sees_it(page):
    """A cached tree walk must not hide a change made between two reads."""
    result = page.evaluate("""
(() => {
  const byTag = document.getElementsByTagName('span');
  const byClass = document.getElementsByClassName('a');
  const bySelector = document.querySelectorAll('span');
  const before = [byTag.length, byClass.length, bySelector.length];
  const added = document.createElement('span');
  added.className = 'a';
  document.getElementById('root').appendChild(added);
  return [before, byTag.length, byClass.length, bySelector.length,
          byTag[byTag.length - 1] === added, byClass[byClass.length - 1] === added];
})()
""")
    assert result == [[2, 1, 2], 3, 2, 2, True, True]


async def test_query_selector_all_returns_a_static_node_list_for_each_root(page):
    assert page.evaluate("""(() => {
        const fragment = document.createDocumentFragment();
        const root = document.createElement('div');
        const shadow = root.attachShadow({mode: 'open'});
        return [document, document.body, fragment, shadow].map((scope, index) => {
            const parent = index === 0 ? document.body : scope;
            const node = document.createElement('i');
            node.className = 'collection-case-' + index;
            const selector = '.' + node.className;
            const empty = scope.querySelectorAll(selector);
            parent.appendChild(node);
            const result = scope.querySelectorAll(selector);
            const later = scope.querySelectorAll(selector);
            node.remove();
            return [
                empty instanceof NodeList, empty.length,
                result instanceof NodeList, Object.getPrototypeOf(result) === NodeList.prototype,
                result.constructor.name, Object.prototype.toString.call(result),
                Array.isArray(result), result.length, typeof result.item === 'function' &&
                    result.item(0) === node && result.item(1) === null,
                result[0] === node && [...result][0] === node,
                scope.querySelectorAll(selector).length, result === later,
            ];
        });
    })()""") == [[True, 0, True, True, "NodeList", "[object NodeList]",
                  False, 1, True, True, 0, False]] * 4


async def test_empty_class_queries_are_empty_live_collections(page):
    assert page.evaluate(r"""(() => {
        const views = [document, document.body].flatMap(root =>
            ['', ' \t\n'].map(name => root.getElementsByClassName(name)));
        const added = document.createElement('div');
        added.className = 'nonempty';
        document.body.appendChild(added);
        return views.map(view => [view instanceof HTMLCollection, view.length, view.item(0) === null]);
    })()""") == [[True, 0, True]] * 4


async def test_document_forms_keeps_identity_and_tracks_tree_changes(page):
    assert page.evaluate("""(() => {
        const forms = document.forms;
        const initial = forms.length;
        const form = document.createElement('form');
        form.id = 'new-form';
        document.body.appendChild(form);
        const live = forms.length === initial + 1 && forms.namedItem('new-form') === form;
        form.remove();
        return [forms instanceof HTMLCollection, forms === document.forms, live, forms.length];
    })()""") == [True, True, True, 0]


async def test_html_collection_named_properties_match_web_idl(page):
    result = page.evaluate("""
(() => {
  const collection = document.getElementById('root').children;
  const descriptor = Object.getOwnPropertyDescriptor(collection, 'named');
  return [
    collection.first === collection[0],
    collection.named === collection[0],
    collection.namedItem('') === null,
    typeof collection.item,
    Object.keys(collection),
    Object.getOwnPropertyNames(collection),
    descriptor
  ];
})()
""")
    assert result[:4] == [True, True, True, "function"]
    assert result[4] == ["0", "1"]
    assert result[5] == ["0", "1", "first", "named", "second"]
    assert result[6] == {
        "value": result[6]["value"],
        "writable": False,
        "enumerable": False,
        "configurable": True,
    }


async def test_html_collection_mutation_and_named_property_visibility(page):
    result = page.evaluate("""
(() => {
  const collection = document.getElementById('root').children;
  const sloppySet = (collection.first = 1);
  const sloppyDelete = delete collection.first;
  collection.extra = 2;
  const extra = collection.extra;
  const extraDeleted = delete collection.extra;
  const element = document.createElement('span');
  element.id = 'item';
  document.getElementById('root').appendChild(element);
  const errors = [];
  try { Function('c', "'use strict'; c.first = 1")(collection); }
  catch (e) { errors.push([e.name, e.message]); }
  try { Function('c', "'use strict'; delete c.first")(collection); }
  catch (e) { errors.push([e.name, e.message]); }
  return [sloppySet, sloppyDelete, collection.first.id, extra,
          extraDeleted, collection.extra, typeof collection.item, errors];
})()
""")
    assert result[:7] == [1, False, "first", 2, True, pyv8.Undefined, "function"]
    assert result[7] == [
        [
            "TypeError",
            "Failed to set a named property 'first' on 'HTMLCollection': "
            "Named property setter is not supported.",
        ],
        [
            "TypeError",
            "Failed to delete a named property 'first' from 'HTMLCollection': "
            "Named property deleter is not supported.",
        ],
    ]


async def test_dom_token_list_preserves_value_and_exposes_tokens(page):
    result = page.evaluate("""
(() => {
  const tokens = document.getElementById('root').classList;
  const descriptor = Object.getOwnPropertyDescriptor(tokens, '0');
  return [tokens.value, String(tokens), tokens.length, tokens[0], tokens[1],
          tokens.item(2), Object.keys(tokens), descriptor,
          DOMTokenList.prototype.add.length, DOMTokenList.prototype.remove.length];
})()
""")
    assert result[:6] == ["  a\tb  a  ", "  a\tb  a  ", 2, "a", "b", pyv8.Null]
    assert result[6] == ["0", "1"]
    assert result[7] == {
        "value": "a",
        "writable": False,
        "enumerable": True,
        "configurable": True,
    }
    assert result[8:] == [0, 0]


async def test_dom_token_list_mutations_follow_ordered_set_algorithms(page):
    result = page.evaluate("""
(() => {
  const element = document.getElementById('root');
  const tokens = element.classList;
  const noChange = tokens.toggle('a', true);
  const preserved = tokens.value;
  tokens.add();
  const normalized = tokens.value;
  tokens.value = ' x  x\ty ';
  const rawValue = tokens.value;
  const rawLength = tokens.length;
  const replaced = tokens.replace('x', 'y');
  const afterReplace = tokens.value;
  const absent = tokens.toggle('z', false);
  const afterAbsent = tokens.value;
  return [noChange, preserved, normalized, rawValue, rawLength,
          replaced, afterReplace, absent, afterAbsent];
})()
""")
    assert result == [
        True, "  a\tb  a  ", "a b", " x  x\ty ", 2,
        True, "y", False, "y",
    ]


async def test_dom_token_list_exposes_web_idl_iterable_helpers(page):
    result = page.evaluate("""
(() => {
  const tokens = document.getElementById('root').classList;
  const visited = [];
  tokens.forEach((token, index, owner) => visited.push([token, index, owner === tokens]));
  return [Array.from(tokens.keys()), Array.from(tokens.values()),
          Array.from(tokens.entries()), visited];
})()
""")
    assert result == [
        [0, 1], ["a", "b"], [[0, "a"], [1, "b"]],
        [["a", 0, True], ["b", 1, True]],
    ]


async def test_dom_token_list_validation_is_atomic_and_uses_dom_exceptions(page):
    result = page.evaluate("""
(() => {
  const tokens = document.getElementById('root').classList;
  const errors = [];
  for (const token of ['', 'a b']) {
    try { tokens.add(token); }
    catch (e) { errors.push([e.name, e.code, e instanceof DOMException, e.message]); }
  }
  try { tokens.add('new', ''); } catch (e) {}
  const atomic = !tokens.contains('new');
  tokens.add('\u00a0');
  let supports;
  try { tokens.supports('a'); }
  catch (e) { supports = [e.name, e.message]; }
  return [errors, atomic, tokens.contains('\u00a0'), supports];
})()
""")
    assert result == [
        [
            ["SyntaxError", 12, True, "Failed to execute 'add' on 'DOMTokenList': The token provided must not be empty."],
            [
                "InvalidCharacterError", 5, True,
                "Failed to execute 'add' on 'DOMTokenList': The token provided ('a b') contains HTML space characters, which are not valid in tokens.",
            ],
        ],
        True,
        True,
        ["TypeError", "DOMTokenList has no supported tokens."],
    ]


# document.all: interface shape


async def test_document_all_typeof_is_undefined(document_page):
    # The interface is marked undetectable, the historical document.all quirk.
    assert document_page.evaluate("typeof document.all") == "undefined"


async def test_document_all_object_tag_is_html_all_collection(document_page):
    assert document_page.evaluate("Object.prototype.toString.call(document.all)") == "[object HTMLAllCollection]"


async def test_document_all_instance_of_html_all_collection(document_page):
    assert document_page.evaluate("document.all instanceof HTMLAllCollection") is True
    assert document_page.evaluate("Object.getPrototypeOf(document.all) === HTMLAllCollection.prototype") is True
    assert document_page.evaluate("HTMLAllCollection.prototype[Symbol.toStringTag]") == "HTMLAllCollection"


async def test_document_all_undetectable_object_is_falsy_and_loosely_null(document_page):
    assert document_page.evaluate("Boolean(document.all)") is False
    assert document_page.evaluate("!document.all") is True
    assert document_page.evaluate("document.all == null") is True
    assert document_page.evaluate("document.all == undefined") is True
    assert document_page.evaluate("document.all === null") is False
    assert document_page.evaluate("document.all === undefined") is False
    assert document_page.evaluate("document.all ? 'truthy' : 'falsy'") == "falsy"


async def test_document_all_python_type_is_the_implementation_class(document_page):
    assert isinstance(document_page.evaluate("document.all"), HTMLAllCollection)
    assert isinstance(document_page.evaluate("document.all[0]"), Element)
    assert isinstance(document_page.evaluate("document.all.app"), HTML_CLASSES["HTMLDivElement"])
    assert isinstance(document_page.evaluate("document.all.dup"), HTMLCollection)


async def test_a_non_constructible_interface_object_is_its_prototypes_constructor(document_page):
    """Exposure comes from the baseline: the prototype must answer with the interface object.

    A class stating ``has_constructor=0`` used to lose that link, so the page read the chain
    through to ``Object.prototype.constructor`` and saw ``Object`` (arity 1) where Chrome has
    the interface object itself, with length 0 for every interface it does not construct.
    """
    answer = document_page.evaluate("""(() => {
        const names = ['CSSStyleSheet', 'CSSStyleRule', 'CSSRule', 'CSSGroupingRule', 'StyleSheet',
                       'BeforeUnloadEvent', 'RTCRtpReceiver', 'CanvasRenderingContext2D', 'Window'];
        return names.map(name => {
            const ctor = window[name];
            const descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, 'constructor');
            return [name, typeof ctor, !!descriptor && descriptor.value === ctor, ctor.length];
        });
    })()""")

    assert answer == [[name, "function", True, 0] for name in (
        "CSSStyleSheet", "CSSStyleRule", "CSSRule", "CSSGroupingRule", "StyleSheet",
        "BeforeUnloadEvent", "RTCRtpReceiver", "CanvasRenderingContext2D", "Window")], answer


async def test_document_all_interface_constructor_is_exposed_but_not_constructible(document_page):
    assert document_page.evaluate("typeof HTMLAllCollection") == "function"
    assert document_page.evaluate("HTMLAllCollection.name") == "HTMLAllCollection"
    assert document_page.evaluate("document.all.constructor === HTMLAllCollection") is True
    for script in ("new HTMLAllCollection()", "HTMLAllCollection()"):
        with pytest.raises(pyv8.JSException) as caught:
            document_page.evaluate(script)
        assert "Illegal constructor" in str(caught.value)


async def test_document_all_document_owns_all_as_a_prototype_accessor(document_page):
    descriptor = "Object.getOwnPropertyDescriptor(Document.prototype, 'all')"
    assert document_page.evaluate(f"typeof {descriptor}.get") == "function"
    assert document_page.evaluate(f"{descriptor}.set") is pyv8.Undefined
    assert document_page.evaluate(f"{descriptor}.enumerable") is True
    assert document_page.evaluate(f"{descriptor}.configurable") is True


# document.all: indexed, named, and legacy operations


async def test_document_all_length_counts_every_element(document_page):
    assert document_page.evaluate("document.all.length") == len(TAGS)
    assert document_page.evaluate("document.all.length") == document_page.evaluate("document.getElementsByTagName('*').length")


async def test_document_all_indexed_access_follows_tree_order(document_page):
    assert document_page.evaluate("[].slice.call(document.all).map(function(e){return e.tagName})") == TAGS
    assert document_page.evaluate("document.all[0].tagName") == "HTML"
    assert document_page.evaluate("document.all[4].id") == "app"


async def test_document_all_index_out_of_range_is_undefined(document_page):
    assert document_page.evaluate("document.all[document.all.length]") is pyv8.Undefined
    assert document_page.evaluate("document.all[99]") is pyv8.Undefined
    assert document_page.evaluate("document.all.item(99)") is pyv8.Null
    assert document_page.evaluate("99 in document.all") is False


async def test_document_all_item_matches_indexed_access(document_page):
    assert document_page.evaluate("document.all.item(0) === document.all[0]") is True
    assert document_page.evaluate("document.all.item(4).id") == "app"
    assert document_page.evaluate("document.all.item()") is pyv8.Null
    assert document_page.evaluate("document.all.item(-1)") is pyv8.Null
    assert document_page.evaluate("document.all.item('0') === document.all[0]") is True
    assert document_page.evaluate("document.all.item('04')") is pyv8.Null
    assert document_page.evaluate("document.all.item(true)") is pyv8.Null
    assert document_page.evaluate("document.all.item(null)") is pyv8.Null
    assert document_page.evaluate("document.all.item('app') === document.all.namedItem('app')") is True
    assert document_page.evaluate("document.all.item('missing')") is pyv8.Null


async def test_document_all_named_item_by_id_and_by_name(document_page):
    assert document_page.evaluate("document.all.namedItem('app').tagName") == "DIV"
    assert document_page.evaluate("document.all.namedItem('note')") is pyv8.Null
    assert document_page.evaluate("document.all.namedItem('missing')") is pyv8.Null


async def test_document_all_named_access_by_id_and_by_name(document_page):
    assert document_page.evaluate("document.all.app.tagName") == "DIV"
    assert document_page.evaluate("document.all.note") is pyv8.Undefined
    assert document_page.evaluate("document.all['app'] === document.all.app") is True
    assert document_page.evaluate("'app' in document.all") is True


async def test_document_all_unknown_name_is_undefined_but_lookup_is_null(document_page):
    assert document_page.evaluate("document.all.missing") is pyv8.Undefined
    assert document_page.evaluate("document.all.namedItem('missing')") is pyv8.Null
    assert document_page.evaluate("'missing' in document.all") is False


async def test_document_all_duplicate_names_return_a_live_html_collection(document_page):
    assert document_page.evaluate("typeof document.all.dup") == "object"
    assert document_page.evaluate("document.all.dup instanceof HTMLCollection") is True
    assert document_page.evaluate("document.all.dup.length") == 2
    assert document_page.evaluate("document.all.namedItem('dup').length") == 2
    assert document_page.evaluate("document.all.dup[0] === document.all[6]") is True
    assert document_page.evaluate("document.all.dup[1] === document.all[7]") is True


async def test_document_all_legacy_call_resolves_names_and_indices(document_page):
    assert document_page.evaluate("document.all('app').tagName") == "DIV"
    assert document_page.evaluate("document.all('note')") is pyv8.Null
    assert document_page.evaluate("document.all(0) === document.all[0]") is True
    assert document_page.evaluate("document.all('0') === document.all[0]") is True
    assert document_page.evaluate("document.all(4).id") == "app"
    assert document_page.evaluate("document.all('missing')") is pyv8.Null
    assert document_page.evaluate("document.all()") is pyv8.Null


async def test_document_all_named_item_requires_an_argument(document_page):
    with pytest.raises(pyv8.JSException) as caught:
        document_page.evaluate("document.all.namedItem()")
    assert "namedItem" in str(caught.value)
    assert "argument" in str(caught.value)


async def test_document_all_identity_is_stable_across_reads(document_page):
    assert document_page.evaluate("document.all === document.all") is True
    document_page.evaluate("window.__cached = document.all")
    assert document_page.evaluate("window.__cached === document.all") is True


async def test_document_all_named_properties_are_not_enumerable(document_page):
    keys = document_page.evaluate("Object.keys(document.all)")
    assert keys == [str(i) for i in range(len(TAGS))]
    assert document_page.evaluate("Object.getOwnPropertyNames(document.all)") == keys + ["app", "dup"]
    assert document_page.evaluate("Object.getOwnPropertyDescriptor(document.all, 'app').enumerable") is False
    assert document_page.evaluate("Object.getOwnPropertyDescriptor(document.all, 'app').writable") is False
    assert document_page.evaluate("Object.getOwnPropertyDescriptor(document.all, 'app').configurable") is True


async def test_document_all_legacy_named_properties_do_not_mask_the_prototype(document_page):
    document_page.evaluate(
        "document.body.innerHTML += '<div id=item></div><div id=length></div>"
        "<div id=constructor></div>'"
    )
    assert document_page.evaluate("typeof document.all.item") == "function"
    assert document_page.evaluate("document.all.length") == len(TAGS) + 3
    assert document_page.evaluate("document.all.constructor === HTMLAllCollection") is True


async def test_document_all_empty_names_and_forbidden_name_attributes_are_ignored(document_page):
    assert document_page.evaluate("document.all.namedItem('')") is pyv8.Null
    assert document_page.evaluate("document.all['']") is pyv8.Undefined
    assert document_page.evaluate("'' in document.all") is False
    assert document_page.evaluate("document.all.note") is pyv8.Undefined
    document_page.evaluate("document.body.appendChild(document.createElement('input')).setAttribute('name', 'field')")
    assert document_page.evaluate("document.all.field.tagName") == "INPUT"


async def test_document_all_collection_is_iterable(document_page):
    assert document_page.evaluate("typeof document.all[Symbol.iterator]") == "function"
    assert document_page.evaluate("Array.from(document.all).length") == len(TAGS)


async def test_document_all_legacy_mutations_follow_strictness_and_do_not_define_properties(document_page):
    assert document_page.evaluate("(document.all.app = 1, document.all.app.id)") == "app"
    assert document_page.evaluate("(delete document.all.app, document.all.app.id)") == "app"
    assert document_page.evaluate("(document.all.created = 1, document.all.created)") == 1
    with pytest.raises(pyv8.JSException, match=(
        "Failed to set a named property 'app' on 'HTMLAllCollection': "
        "Named property setter is not supported"
    )):
        document_page.evaluate("'use strict'; document.all.app = 1")
    with pytest.raises(pyv8.JSException, match=(
        "Failed to delete a named property 'app' from 'HTMLAllCollection': "
        "Named property deleter is not supported"
    )):
        document_page.evaluate("'use strict'; delete document.all.app")
    with pytest.raises(pyv8.JSException, match=(
        r"Failed to delete an indexed property \[0\] from 'HTMLAllCollection': "
        "Indexed property deleter is not supported"
    )):
        document_page.evaluate("'use strict'; delete document.all[0]")


async def test_document_all_strict_indexed_setter_uses_chromium_error_context(document_page):
    result = document_page.evaluate(
        """(function () {
  "use strict";
  try {
    document.all[0] = 123;
  } catch (e) {
    return [e.name, e.message, e.stack];
  }
})();"""
    )
    message = (
        "Failed to set an indexed property [0] on 'HTMLAllCollection': "
        "Indexed property setter is not supported."
    )
    assert result[:2] == ["TypeError", message]
    assert result[2].startswith(f"TypeError: {message}\n")
    assert "at <anonymous>:4:" in result[2]


async def test_document_all_for_in_only_yields_indices_and_prototype_members(document_page):
    seen = document_page.evaluate("(function(){var out=[];for(var k in document.all){out.push(k)}return out})()")
    assert [key for key in seen if key.isdigit()] == [str(i) for i in range(len(TAGS))]
    assert ["length", "item", "namedItem"] == [key for key in seen if not key.isdigit()]


async def test_document_all_empty_document_has_an_empty_collection(browser):
    empty = browser.new_profile().new_page()
    empty.evaluate('document.documentElement.remove()')
    assert empty.evaluate("document.all.length") == 0
    assert empty.evaluate("typeof document.all") == "undefined"
    assert empty.evaluate("document.all[0]") is pyv8.Undefined


# document.all: live updates and materialisation


async def test_document_all_settling_the_page_keeps_length_consistent(browser):
    instance = browser.new_profile().new_page()
    (await instance.set_content(DOCUMENT, wait_until="commit"))
    (await instance.run_until_idle())
    assert instance.evaluate("document.readyState") == "complete"
    assert instance.evaluate("document.all.length") == len(TAGS)
    assert instance.evaluate("document.all.app.id") == "app"


async def test_document_all_length_updates_after_a_queued_task_appends(document_page):
    before = document_page.evaluate("document.all.length")
    document_page.evaluate(
        "setTimeout(function(){ var s = document.createElement('section');"
        " s.id = 'late'; document.body.appendChild(s); }, 0)"
    )
    (await document_page.run_until_idle())
    assert document_page.evaluate("document.all.length") == before + 1
    assert document_page.evaluate("document.all.late.tagName") == "SECTION"
    assert document_page.evaluate("document.all[document.all.length - 1].id") == "late"


async def test_document_all_cached_reference_tracks_later_insertions(document_page):
    document_page.evaluate("window.__cached = document.all")
    document_page.evaluate("document.body.appendChild(document.createElement('hr'))")
    assert document_page.evaluate("window.__cached.length") == len(TAGS) + 1
    assert document_page.evaluate("window.__cached.length === document.all.length") is True
    assert document_page.evaluate("window.__cached[window.__cached.length - 1].tagName") == "HR"


async def test_document_all_removal_shortens_the_collection(document_page):
    document_page.evaluate("document.body.removeChild(document.getElementById('app'))")
    assert document_page.evaluate("document.all.length") == len(TAGS) - 1
    assert document_page.evaluate("document.all.app") is pyv8.Undefined
    assert document_page.evaluate("document.all.namedItem('app')") is pyv8.Null


async def test_document_all_new_element_without_id_is_only_reachable_by_index(document_page):
    document_page.evaluate("document.body.appendChild(document.createElement('b'))")
    assert document_page.evaluate("document.all.length") == len(TAGS) + 1
    assert document_page.evaluate("document.all[document.all.length - 1].tagName") == "B"


async def test_document_all_renaming_an_id_changes_the_named_lookup(document_page):
    document_page.evaluate("document.getElementById('app').id = 'renamed'")
    assert document_page.evaluate("document.all.app") is pyv8.Undefined
    assert document_page.evaluate("document.all.renamed.tagName") == "DIV"
    assert document_page.evaluate("'app' in document.all") is False
    assert document_page.evaluate("'renamed' in document.all") is True


async def test_document_all_lazy_interface_is_materialised_on_first_use(browser):
    page = browser.new_profile().new_page()
    session = page.binding_session
    assert "HTMLAllCollection" not in session.materialized_interfaces
    (await page.set_content(DOCUMENT, wait_until="commit"))
    (await page.run_until_idle())
    assert page.evaluate("document.all.length") == len(TAGS)
    assert "HTMLAllCollection" in session.materialized_interfaces


async def test_document_all_materialises_through_the_feature_runtime():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(DOCUMENT, wait_until="commit"))
        (await page.run_until_idle())
        assert page.evaluate("typeof document.all") == "undefined"
        assert page.evaluate("document.all instanceof HTMLAllCollection") is True
        assert page.evaluate("document.all('app').id") == "app"
    finally:
        (await browser.close())


async def test_document_all_accepts_the_interface_after_catalog_validation():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        browser.bindings.validate()
        page = browser.new_profile().new_page()
        (await page.set_content(DOCUMENT, wait_until="commit"))
        (await page.run_until_idle())
        assert page.evaluate("document.all.length") == len(TAGS)
    finally:
        (await browser.close())
