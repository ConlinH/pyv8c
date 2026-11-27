"""Behaviour this environment claims, in a form a second browser can answer.

A case is a page, one script, and what that script must observe.  ``expected`` is
pyv8c's answer and is asserted on every run; the same case also runs in the locally
installed Chrome, one record each, so a claim about page behaviour is checked against
a second implementation instead of only against what this repository believed last
time.

``known_divergence`` marks a case where the two are known to differ and carries the
reason.  A case that diverges without one fails, and a case whose divergence has gone
away fails too -- so the list cannot rot in either direction.

Keep the corpus small and purposeful: one corner per case, both engines able to
answer it, and an observable that survives being written down (a value, not a
timing).
"""
from __future__ import annotations

import dataclasses
from typing import Any

URL = "http://repro.test/dir/page"


def page(head="", body=""):
    """The document a case starts from."""
    return f"<html><head>{head}</head><body>{body}</body></html>"


#: A form holding one of everything the collection rules tell apart: an image button
#: (submittable, not listed), controls outside it that name it with ``form=``, and an
#: element whose tag is unmodelled here but is still a listed element in both engines.
FORM_PAGE = (page(body="""
    <form id=f>
      <input id=one name=a>
      <input type=image id=image name=img>
      <fieldset id=fs><input id=nested name=n></fieldset>
      <object id=obj name=o></object>
      <output id=out name=u></output>
      <button id=btn name=btn value=bv>x</button>
      <select id=sel name=s></select>
      <textarea id=ta name=t></textarea>
    </form>
    <input id=outside form=f>
    <input id=other>
"""))

#: A document with an empty body.  The cases that put nodes in the tree need one;
#: a document built from no markup at all is a different question, and it is asked
#: separately.
EMPTY_PAGE = (page(body=""))


@dataclasses.dataclass(frozen=True)
class Repro:
    name: str
    script: str
    expected: Any = None
    html: str = ""
    note: str = ""
    url: str = URL
    expect_error: str = ""
    known_divergence: str = ""
    #: Seconds the reference browser waits after load before reading, for behaviour
    #: that lands on a task of its own; this environment runs to idle either way.
    settle: float = 0.0
    match_client_hints: bool = False
    comparison_script: str = ""
    baseline_reference: bool = False
    software_rendering: bool = False


WEBGL_EXTENSIONS = """((type, exactCount) => {
  const gl = document.createElement('canvas').getContext(type);
  const names = gl.getSupportedExtensions();
  const missing = names.filter((name) => gl.getExtension(name) === null);
  const inventory = exactCount ? names.length :
      names.length > 0 && new Set(names).size === names.length && missing.length === 0;
  if (type === 'webgl') return [inventory, missing];
  return [inventory, names.includes('EXT_color_buffer_float'),
          names.includes('OES_vertex_array_object'),
          gl.getExtension('OES_vertex_array_object')];
})"""

CASES = (
    Repro(
        name="title_collapses_whitespace",
        note="The IDL attribute collapses; the element keeps what was written.",
        html=(page(head="<title>  Live   Page </title>")),
        script="document.title",
        expected="Live Page",
    ),
    Repro(
        name="title_element_keeps_raw_text",
        html=(page(head="<title>  Live   Page </title>")),
        script="document.querySelector('title').textContent",
        expected="  Live   Page ",
    ),
    Repro(
        name="get_elements_by_name_is_live",
        note="The list follows the tree; querySelectorAll does not.",
        html=(page(body="<form name=login><input name=user><input name=user></form>"
                       "<p name=user></p>")),
        script="""
            (() => {
              const list = document.getElementsByName('user');
              const before = list.length;
              document.body.appendChild(document.createElement('i'))
                .setAttribute('name', 'user');
              return [before, list.length];
            })()
        """,
        expected=[3, 4],
    ),
    Repro(
        name="get_elements_by_name_keeps_document_order",
        html=(page(body="<form name=login><input name=user><input name=user></form>"
                       "<p name=user></p>")),
        script="Array.from(document.getElementsByName('user'), node => node.tagName)",
        expected=["INPUT", "INPUT", "P"],
    ),
    Repro(
        name="static_and_live_collections_differ",
        html=(page(body="<div id=one></div>")),
        script="""
            (() => {
              const static_list = document.querySelectorAll('div');
              const live_list = document.getElementsByTagName('div');
              const before = [static_list.length, live_list.length];
              document.body.appendChild(document.createElement('div'));
              return [before, [static_list.length, live_list.length]];
            })()
        """,
        expected=[[1, 1], [1, 2]],
    ),
    Repro(
        name="anchor_href_resolves_against_the_document",
        html=(page(body="<a id=link href=\"/dir/target?q=1#frag\">link</a>")),
        script="""
            (() => {
              const anchor = document.getElementById('link');
              return [anchor.href, anchor.protocol, anchor.pathname, anchor.search,
                      anchor.hash];
            })()
        """,
        expected=["http://repro.test/dir/target?q=1#frag", "http:", "/dir/target",
                  "?q=1", "#frag"],
    ),
    Repro(
        name="select_value_clears_selection_when_nothing_matches",
        note="Setting .value clears every option first, then selects the first match --"
        " so a value that matches nothing, 'b' against an option worth 'B', leaves the"
        " select with no selection at all instead of keeping the previous one.",
        html=(page(body="<select><option value=a>A</option><option>B</option></select>")),
        script="""
            (() => {
              const options = document.querySelectorAll('option');
              document.querySelector('select').value = 'b';
              return [options[0].selected, options[1].selected, options[1].value];
            })()
        """,
        expected=[False, False, "B"],
    ),
    Repro(
        name="a_select_exposes_its_options_collection",
        note="The collection is the same object every read, and it is live.",
        html=(page(body="<select><option value=a>A</option><option value=b>B</option></select>")),
        script="""
            (() => {
              const select = document.querySelector('select');
              const options = select.options;
              const before = options.length;
              const added = document.createElement('option');
              added.value = 'c';
              select.appendChild(added);
              return [before, options.length, options[2] === added, options.item(1).value,
                      select.length, options === select.options];
            })()
        """,
        expected=[2, 3, True, "b", 3, True],
    ),
    Repro(
        name="a_select_reports_its_own_value_and_index",
        html=(page(body="<select><option value=a>A</option></select>")),
        script="""
            (() => {
              const select = document.querySelector('select');
              select.value = 'a';
              return [typeof select.selectedIndex, select.value];
            })()
        """,
        expected=["number", "a"],
    ),
    Repro(
        name="selected_index_moves_the_selection",
        note="A position outside the list clears the selection rather than clamping it.",
        html=(page(body="<select><option value=a>A</option><option value=b>B</option>"
                       "<option value=c>C</option></select>")),
        script="""
            (() => {
              const select = document.querySelector('select');
              select.selectedIndex = 2;
              const chosen = [select.selectedIndex, select.value,
                              select.options.selectedIndex, select.selectedOptions.length];
              select.selectedIndex = -1;
              return [chosen, [select.selectedIndex, select.value,
                               select.selectedOptions.length]];
            })()
        """,
        expected=[[2, "c", 2, 1], [-1, "", 0]],
    ),
    Repro(
        name="form_controls_are_in_document_order",
        html=(page(body="<form><input name=a><input name=b><button name=c></button></form>")),
        script="""
            (() => {
              const controls = document.forms[0].querySelectorAll('input, button');
              return [controls.length, controls[1].name, controls[2].tagName,
                      Array.from(controls, element => element.tagName).join(',')];
            })()
        """,
        expected=[3, "b", "BUTTON", "INPUT,INPUT,BUTTON"],
    ),
    Repro(
        name="a_button_reflects_name_value_and_type",
        note="type reports one of its three states: a value it does not know, and a"
        " missing attribute, are both submit.",
        script="""
            (() => {
              const button = document.createElement('button');
              const empty = [typeof button.name, button.name, button.value, button.type];
              button.name = 'n';
              button.value = 'v';
              const written = [button.name, button.getAttribute('name'),
                               button.getAttribute('value')];
              button.setAttribute('type', 'reset');
              const reset = button.type;
              button.type = 'bogus';
              return [empty, written, reset, [button.getAttribute('type'), button.type]];
            })()
        """,
        expected=[["string", "", "", "submit"], ["n", "n", "v"], "reset", ["bogus", "submit"]],
    ),
    Repro(
        name="a_form_control_reports_its_name",
        html=FORM_PAGE,
        script="[document.getElementById('sel').name, document.getElementById('ta').name,"
               " document.getElementById('btn').name]",
        expected=["s", "t", "btn"],
    ),
    Repro(
        name="form_elements_follows_the_document_order",
        note="Ordered by the document, not by the form: a control that names the form"
        " with form= sits where the document puts it, and an image button is not listed.",
        html=FORM_PAGE,
        script="""
            (() => {
              const form = document.forms[0];
              return [Array.from(form.elements, element => element.id), form.elements.length,
                      form.length, form.elements === form.elements];
            })()
        """,
        expected=[["one", "fs", "nested", "obj", "out", "btn", "sel", "ta", "outside"],
                  9, 9, True],
    ),
    Repro(
        name="form_elements_is_indexed_named_and_live",
        html=FORM_PAGE,
        script="""
            (() => {
              const form = document.forms[0];
              const elements = form.elements;
              const before = elements.length;
              const added = document.createElement('input');
              added.id = 'late';
              form.appendChild(added);
              return [elements.namedItem('outside') === document.getElementById('outside'),
                      elements.one === document.getElementById('one'),
                      elements[1] === document.getElementById('fs'),
                      before, elements.length,
                      Array.from(elements, element => element.id).pop()];
            })()
        """,
        expected=[True, True, True, 9, 10, "outside"],
    ),
    Repro(
        name="a_control_belongs_to_the_form_its_attribute_names",
        note="form= wins over the ancestor: naming a form that is not there leaves the"
        " control owned by nobody rather than by the form it sits in.",
        html=(page(body="<form id=first><input id=inside form=nosuch><input id=plain>"
                       "<input id=moved form=second></form><form id=second></form>")),
        script="[Array.from(document.forms[0].elements, element => element.id),"
               " Array.from(document.forms[1].elements, element => element.id)]",
        expected=[["plain"], ["moved"]],
    ),
    Repro(
        name="computed_style_follows_the_cascade",
        html=(page(
            head="<style>div { font-size: 20px } #sized { width: 40px; display: block }</style>",
            body="<div><span id=sized>x</span></div>",
        )),
        script="""
            (() => {
              const style = getComputedStyle(document.getElementById('sized'));
              return [style.fontSize, style.width];
            })()
        """,
        expected=["20px", "40px"],
    ),
    Repro(
        name="innerhtml_round_trip",
        html=(page(body="<div id=host><b>x</b></div>")),
        script="""
            (() => {
              const host = document.getElementById('host');
              host.innerHTML = '<i>y</i>';
              return [host.innerHTML, host.outerHTML, host.childNodes.length];
            })()
        """,
        expected=["<i>y</i>", '<div id="host"><i>y</i></div>', 1],
    ),
    Repro(
        name="class_list_add_and_contains",
        script="""
            (() => {
              const element = document.createElement('div');
              element.classList.add('a', 'b');
              return [element.className, element.classList.length,
                      element.classList.contains('b'), element.classList.contains('c')];
            })()
        """,
        expected=["a b", 2, True, False],
    ),
    Repro(
        name="an_element_exposes_its_dataset",
        script="typeof document.createElement('div').dataset",
        expected="object",
    ),
    Repro(
        name="dataset_writes_reads_and_deletes_its_attributes",
        note="A map name and its data-* attribute are the same value from two sides.",
        script="""
            (() => {
              const element = document.createElement('div');
              element.dataset.fooBar = 'x';
              const written = element.getAttribute('data-foo-bar');
              delete element.dataset.fooBar;
              return [written, element.hasAttribute('data-foo-bar'),
                      typeof element.dataset.fooBar];
            })()
        """,
        expected=["x", False, "undefined"],
    ),
    Repro(
        name="dataset_reads_attributes_written_as_attributes",
        script="""
            (() => {
              const element = document.createElement('div');
              element.setAttribute('data-x-y', 'v');
              element.setAttribute('data-plain', '2');
              return [element.dataset.xY, element.dataset.plain,
                      Object.keys(element.dataset).sort(),
                      'xY' in element.dataset, 'absent' in element.dataset];
            })()
        """,
        expected=["v", "2", ["plain", "xY"], True, False],
    ),
    Repro(
        name="document_cookie_writes_and_reads_back",
        note="Needs a real origin, which the reference side gets from its routed URL.",
        script="(() => { document.cookie = 'a=1'; return document.cookie; })()",
        expected="a=1",
    ),
    Repro(
        name="instanceof_reaches_the_interfaces",
        html=(page(body="<p id=para>x</p>")),
        script="[document instanceof Document, window instanceof Window,"
               " document.body instanceof HTMLElement, document.body instanceof Node]",
        expected=[True, True, True, True],
    ),
    Repro(
        name="a_thrown_error_keeps_its_name",
        script="(() => { throw new TypeError('boom'); })()",
        expect_error="TypeError",
    ),
    Repro(
        name="json_crosses_the_boundary_unchanged",
        note="Sanity for the harness itself: the record has to survive the transport.",
        script="JSON.stringify({a: [1, 2], b: 'x'})",
        expected='{"a":[1,2],"b":"x"}',
    ),
    # -- events ----------------------------------------------------------------
    Repro(
        name="event_listener_order_is_capture_then_target_then_bubble",
        note="A capture listener on the ancestor runs before the target's own, and a "
             "bubble one on that ancestor after it, in registration order.",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const log = [];
              const outer = document.createElement('div');
              const inner = document.createElement('span');
              outer.appendChild(inner);
              document.body.appendChild(outer);
              outer.addEventListener('click', () => log.push('outer-capture'), true);
              outer.addEventListener('click', () => log.push('outer-bubble'));
              inner.addEventListener('click', () => log.push('inner'));
              outer.addEventListener('click', () => log.push('outer-bubble-2'));
              inner.dispatchEvent(new Event('click', {bubbles: true}));
              return log;
            })()
        """,
        expected=["outer-capture", "inner", "outer-bubble", "outer-bubble-2"],
    ),
    Repro(
        name="event_once_and_removed_listeners",
        note="A once listener is gone after its first call, and removeEventListener "
             "after that has nothing left to remove.",
        script="""
            (() => {
              const target = document.createElement('div');
              const log = [];
              const first = () => log.push('first');
              target.addEventListener('x', first, {once: true});
              target.addEventListener('x', () => log.push('second'));
              target.dispatchEvent(new Event('x'));
              target.dispatchEvent(new Event('x'));
              target.removeEventListener('x', first);
              target.dispatchEvent(new Event('x'));
              return log;
            })()
        """,
        expected=["first", "second", "second", "second"],
    ),
    Repro(
        name="event_prevent_default_decides_the_return_value",
        note="dispatchEvent reports whether the event was not cancelled, so only a "
             "cancelable event a listener cancelled answers false.",
        script="""
            (() => {
              const target = document.createElement('div');
              const results = [];
              target.addEventListener('y', event => event.preventDefault());
              results.push(target.dispatchEvent(new Event('y', {cancelable: true})));
              results.push(target.dispatchEvent(new Event('y')));
              results.push(target.dispatchEvent(new Event('z')));
              return results;
            })()
        """,
        expected=[False, True, True],
    ),
    Repro(
        name="event_stop_propagation_keeps_the_event_off_the_ancestor",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const log = [];
              const outer = document.createElement('div');
              const inner = document.createElement('span');
              outer.appendChild(inner);
              document.body.appendChild(outer);
              outer.addEventListener('click', () => log.push('outer'));
              inner.addEventListener('click', event => {
                log.push('inner');
                event.stopPropagation();
              });
              inner.dispatchEvent(new Event('click', {bubbles: true}));
              return log;
            })()
        """,
        expected=["inner"],
    ),
    Repro(
        name="event_reports_its_dispatch_state",
        script="""
            (() => {
              const target = document.createElement('div');
              let seen = null;
              target.addEventListener('k', event => {
                seen = [event.type, event.target === target, event.currentTarget === target,
                        event.eventPhase, event.bubbles, event.cancelable,
                        event.defaultPrevented];
              });
              target.dispatchEvent(new Event('k', {bubbles: true, cancelable: true}));
              return seen;
            })()
        """,
        expected=["k", True, True, 2, True, True, False],
    ),
    # -- tree and node semantics -----------------------------------------------
    Repro(
        name="insert_before_places_and_appends",
        note="A second argument of null appends, and the node lands where it was "
             "asked for.",
        script="""
            (() => {
              const names = ['a', 'b', 'c', 'd', 'e'];
              const parent = document.createElement('ul');
              const nodes = {};
              for (const name of ['a', 'b', 'c']) {
                nodes[name] = document.createElement('li');
                parent.appendChild(nodes[name]);
              }
              nodes.d = document.createElement('li');
              parent.insertBefore(nodes.d, nodes.b);
              nodes.e = document.createElement('li');
              parent.insertBefore(nodes.e, null);
              return [Array.from(parent.children,
                                 node => names.find(name => nodes[name] === node)),
                      parent.childNodes.length];
            })()
        """,
        expected=[["a", "d", "b", "c", "e"], 5],
    ),
    Repro(
        name="insert_before_moves_an_existing_node",
        script="""
            (() => {
              const parent = document.createElement('div');
              const nodes = {};
              for (const id of ['a', 'b', 'c']) {
                nodes[id] = document.createElement('p');
                nodes[id].id = id;
                parent.appendChild(nodes[id]);
              }
              parent.insertBefore(nodes.c, nodes.a);
              return [Array.from(parent.children, node => node.id), parent.children.length];
            })()
        """,
        expected=[["c", "a", "b"], 3],
    ),
    Repro(
        name="clone_node_is_shallow_or_deep_but_never_the_same_object",
        script="""
            (() => {
              const node = document.createElement('div');
              node.id = 'x';
              node.innerHTML = '<span id="child">t</span>';
              const shallow = node.cloneNode(false);
              const deep = node.cloneNode(true);
              return [shallow.childNodes.length, deep.childNodes.length, deep.firstChild.id,
                      deep === node, deep.isEqualNode(node),
                      deep.firstChild === node.firstChild, deep.isSameNode(node)];
            })()
        """,
        expected=[0, 1, "child", False, True, False, False],
    ),
    Repro(
        name="text_content_skips_comments",
        note="A comment is character data with a textContent of its own, but it is "
             "not text and so contributes nothing to an ancestor's.",
        script="""
            (() => {
              const div = document.createElement('div');
              div.innerHTML = 'a<b>c</b>d<!--e-->';
              const text = document.createTextNode('t');
              const comment = document.createComment('c');
              return [div.textContent, div.childNodes.length, text.textContent,
                      comment.textContent, text.nodeValue, comment.data,
                      div.firstChild.data];
            })()
        """,
        expected=["acd", 4, "t", "c", "t", "c", "a"],
    ),
    Repro(
        name="contains_and_document_position",
        note="compareDocumentPosition reports 20 for a descendant: following and "
             "contained by.",
        script="""
            (() => {
              const parent = document.createElement('div');
              const child = document.createElement('span');
              parent.appendChild(child);
              const other = document.createElement('p');
              return [parent.contains(child), child.contains(parent), parent.contains(parent),
                      other.contains(child), parent.compareDocumentPosition(child)];
            })()
        """,
        expected=[True, False, True, False, 20],
    ),
    Repro(
        name="normalize_merges_adjacent_text_and_drops_the_empty_one",
        script="""
            (() => {
              const div = document.createElement('div');
              div.appendChild(document.createTextNode('a'));
              div.appendChild(document.createTextNode('b'));
              div.appendChild(document.createElement('i'));
              div.appendChild(document.createTextNode(''));
              const before = div.childNodes.length;
              div.normalize();
              return [before, div.childNodes.length, div.textContent,
                      Array.from(div.childNodes, node => node.nodeName)];
            })()
        """,
        expected=[4, 2, "ab", ["#text", "I"]],
    ),
    # -- reflection -------------------------------------------------------------
    Repro(
        name="input_reflects_its_constraints",
        note="maxlength does not truncate a value a script writes.",
        script="""
            (() => {
              const input = document.createElement('input');
              input.type = 'text';
              input.maxLength = 5;
              input.value = 'abcdefgh';
              const required = document.createElement('input');
              required.required = true;
              const disabled = document.createElement('input');
              disabled.disabled = true;
              return [input.maxLength, input.getAttribute('maxlength'), input.value,
                      required.required, required.hasAttribute('required'),
                      disabled.disabled, disabled.hasAttribute('disabled'), input.type];
            })()
        """,
        expected=[5, "5", "abcdefgh", True, True, True, True, "text"],
    ),
    Repro(
        name="an_option_derives_text_label_and_value_from_its_text",
        note="With no value or label attribute, all three are the option's own text, "
             "with whitespace collapsed.",
        script="""
            (() => {
              const option = document.createElement('option');
              option.textContent = '  Spaced   text ';
              const derived = [option.text, option.label, option.value];
              option.label = 'L';
              const labelled = [option.label, option.getAttribute('label'), option.text];
              option.value = 'v';
              return [derived, labelled, option.value];
            })()
        """,
        expected=[["Spaced text", "Spaced text", "Spaced text"],
                  ["L", "L", "Spaced text"], "v"],
    ),
    Repro(
        name="image_dimensions_are_dimension_values",
        note="width and height read the number an attribute starts with, so a "
             "dimension written with a unit still reads back, and a negative one is "
             "stored as 0 rather than wrapped around.",
        script="""
            (() => {
              const read = value => {
                const image = document.createElement('img');
                image.setAttribute('width', value);
                return [value, image.width];
              };
              const image = document.createElement('img');
              const empty = [image.width, image.height];
              image.height = '30';
              image.width = -5;
              return [empty, read('12px'), read('12%'), read('12.9'), read('abc'),
                      read('-5'), read(' 12 '),
                      [image.width, image.getAttribute('width'), image.height,
                       typeof image.width]];
            })()
        """,
        expected=[[0, 0], ["12px", 12], ["12%", 12], ["12.9", 12], ["abc", 0],
                  ["-5", 0], [" 12 ", 12], [0, "0", 30, "number"]],
    ),
    Repro(
        name="selector_collections_and_closest",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const root = document.createElement('div');
              root.innerHTML = '<p class="a b" id="one"><span class="b" id="two">t</span></p>'
                             + '<p class="a" id="three"></p>';
              document.body.appendChild(root);
              const two = document.getElementById('two');
              return [root.getElementsByClassName('a b').length,
                      root.querySelectorAll('p.a').length,
                      root.querySelectorAll('p > span').length,
                      two.closest('p').id, two.matches('.b'), two.matches('p span'),
                      Array.from(root.children, node => node.id)];
            })()
        """,
        expected=[1, 2, 1, "one", True, True, ["one", "three"]],
    ),
    # -- URLs ------------------------------------------------------------------
    Repro(
        name="url_search_params_mutation_reaches_the_url",
        script="""
            (() => {
              const url = new URL('https://example.test:8443/a/b?x=1&y=2#frag');
              const params = url.searchParams;
              params.append('z', '3');
              params.delete('x');
              return [url.href, url.port, url.pathname, url.search, url.hash,
                      params.toString(), params.get('y'), params.has('x'), url.origin,
                      params.size];
            })()
        """,
        expected=["https://example.test:8443/a/b?y=2&z=3#frag", "8443", "/a/b", "?y=2&z=3",
                  "#frag", "y=2&z=3", "2", False, "https://example.test:8443", 2],
    ),
    Repro(
        name="url_search_params_iterators",
        note="Duplicated names keep their order in every view.",
        script="""
            (() => {
              const params = new URLSearchParams('a=1&b=2&a=3');
              return [Array.from(params.keys()), Array.from(params.values()),
                      Array.from(params.entries()), params.size];
            })()
        """,
        expected=[["a", "b", "a"], ["1", "2", "3"],
                  [["a", "1"], ["b", "2"], ["a", "3"]], 3],
    ),
    Repro(
        name="url_search_params_is_itself_iterable",
        note="The object iterates its entries, a view is an iterator over the list "
             "rather than a snapshot array, and Symbol.iterator is entries itself.",
        script="""
            (() => {
              const params = new URLSearchParams('a=1&b=2');
              const keys = params.keys();
              return [Array.from(params), typeof keys.next, keys[Symbol.iterator]() === keys,
                      URLSearchParams.prototype[Symbol.iterator] === URLSearchParams.prototype.entries,
                      URLSearchParams.prototype[Symbol.iterator].name,
                      Object.prototype.toString.call(params.entries())];
            })()
        """,
        expected=[[["a", "1"], ["b", "2"]], "function", True, True, "entries",
                  "[object URLSearchParams Iterator]"],
    ),
    Repro(
        name="url_search_params_views_are_native",
        note="The views come from the binding, like Chromium's, rather than from a "
             "wrapper a host wrote in JavaScript.",
        script="""
            [String(URLSearchParams.prototype.entries), URLSearchParams.prototype.entries.name,
             URLSearchParams.prototype.entries.length, URLSearchParams.prototype.keys.name,
             URLSearchParams.prototype[Symbol.iterator] === URLSearchParams.prototype.entries,
             Object.getOwnPropertyNames(Object.getPrototypeOf(new URLSearchParams('a=1').entries())).join()]
        """,
        expected=["function entries() { [native code] }", "entries", 0, "keys", True, "next"],
    ),
    Repro(
        name="url_search_params_views_are_live",
        note="A view reads the list when next() runs, so an entry added after the "
             "view was made is still visited.",
        script="""
            (() => {
              const params = new URLSearchParams('a=1');
              const values = params.values();
              params.append('b', '2');
              return [values.next().value, values.next().value, values.next().done];
            })()
        """,
        expected=["1", "2", True],
    ),
    Repro(
        name="url_search_params_delete_takes_an_optional_value",
        note="A second argument narrows the match to that value; undefined means it "
             "was left out, while null is the string 'null'.",
        script="""
            (() => {
              const params = new URLSearchParams('a=1&a=2&b=3');
              params.delete('a', 1);
              params.delete('b', undefined);
              return [params.toString(), params.has('a', '1'), params.has('a', '2'),
                      params.has('a', 2)];
            })()
        """,
        expected=["a=2", False, True, True],
    ),
    Repro(
        name="url_search_params_delete_removes_every_match",
        note="The pairs it keeps are the ones that are not that name and value.",
        script="""
            (() => {
              const params = new URLSearchParams('a=1&a=1&a=2&b=null');
              params.delete('a', '1');
              params.delete('b', null);
              return params.toString();
            })()
        """,
        expected="a=2",
    ),
    Repro(
        name="url_search_params_serializes_the_way_the_url_does",
        note="A space is '+', '*' and '-' are literal, and '~' is encoded.",
        script="""
            [new URLSearchParams('a=a b').toString(), new URLSearchParams('a=~*!').toString(),
             new URLSearchParams('a=%').toString(), new URLSearchParams('a=1&a=2').toString()]
        """,
        expected=["a=a+b", "a=%7E*%21", "a=%25", "a=1&a=2"],
    ),
    Repro(
        name="url_search_params_writes_the_url_spelling_back",
        note="What a mutation writes into the URL is what toString() would spell, and "
             "removing the last entry takes the '?' with it.",
        script="""
            (() => {
              const url = new URL('https://example.test/p');
              url.searchParams.set('a', 'x~y z');
              const written = url.href;
              url.searchParams.delete('a');
              return [written, url.href, url.search];
            })()
        """,
        expected=["https://example.test/p?a=x%7Ey+z", "https://example.test/p", ""],
    ),
    Repro(
        name="url_search_params_copies_another_params",
        note="The copy is its own list: later changes to the source leave it alone.",
        script="""
            (() => {
              const source = new URLSearchParams('a=1');
              const copy = new URLSearchParams(source);
              source.append('b', '2');
              return [copy.toString(), copy === source, new URLSearchParams(source).size];
            })()
        """,
        expected=["a=1", False, 2],
    ),
    Repro(
        name="url_search_params_sorts_by_code_unit",
        note="Stable by name, comparing UTF-16 code units: an astral name sorts before "
             "U+FFFF, which a code point order would reverse.",
        script="""
            (() => {
              const params = new URLSearchParams('b=1&A=2&a=3&\\u00e9=4');
              params.sort();
              const astral = new URLSearchParams();
              astral.append('\\u{1f600}', 'x');
              astral.append('\\uffff', 'y');
              astral.sort();
              return [params.toString(), Array.from(astral.keys()).map(
                  (name) => name.codePointAt(0).toString(16)).join(',')];
            })()
        """,
        expected=["A=2&a=3&b=1&%C3%A9=4", "1f600,ffff"],
    ),
    Repro(
        name="url_search_params_forEach_arguments",
        note="The callback gets the value first, then the name, then the params, and "
             "runs with the given this.",
        script="""
            (() => {
              const params = new URLSearchParams('a=1&b=2');
              const seen = [];
              const result = params.forEach(function (value, name, source) {
                seen.push([value, name, source === params, this === undefined ? 'none' : this.tag,
                           arguments.length]);
              }, {tag: 'tagged'});
              return [seen, String(result), URLSearchParams.prototype.forEach.length];
            })()
        """,
        expected=[[["1", "a", True, "tagged", 3], ["2", "b", True, "tagged", 3]], "undefined", 1],
    ),
    Repro(
        name="url_search_params_forEach_walks_the_list_as_it_changes",
        note="Removing an entry during the walk drops it from the rest of the walk.",
        script="""
            (() => {
              const params = new URLSearchParams('a=1&b=2');
              const seen = [];
              params.forEach((value, name) => {
                seen.push(name);
                if (name === 'a') params.delete('b');
              });
              return [seen, params.toString()];
            })()
        """,
        expected=[["a"], "a=1"],
    ),
    Repro(
        name="url_relative_resolution",
        script="new URL('../x?q=1', 'https://example.test/dir/page').href",
        expected="https://example.test/x?q=1",
    ),
    Repro(
        name="url_spaces_are_percent_encoded_wherever_they_sit",
        note="Including the authority and the userinfo; a space in the port is what "
             "no URL can have.",
        script="""
            (() => {
              const encoded = new URL('../x?q=1 2', 'https://example.test/dir/page');
              const made = ['https://a.te st/', 'https://u s@a.test/', 'https://a.test:80 80/'];
              return [encoded.href, new URL('https://a.test/p q#f 3').href,
                      new URL('  https://a.test/ok\\t').href,
                      made.map(text => {
                        try { return new URL(text).href; } catch (error) { return 'REJECTED'; }
                      })];
            })()
        """,
        expected=["https://example.test/x?q=1%202", "https://a.test/p%20q#f%203",
                  "https://a.test/ok",
                  ["https://a.te%20st/", "https://u%20s@a.test/", "REJECTED"]],
    ),
    Repro(
        name="a_constructor_is_not_a_function",
        note="URL is a constructor, so calling it without new throws; Chrome words the "
             "refusal the same way for every interface built from IDL.",
        script="""
            (() => {
              try { URL('https://www.koreanair.com/'); return ['NO-THROW']; }
              catch (error) { return [error.name, error.message, error instanceof TypeError]; }
            })()
        """,
        expected=["TypeError",
                  "Failed to construct 'URL': Please use the 'new' operator,"
                  " this DOM object constructor cannot be called as a function.",
                  True],
    ),
    # -- what is still missing here --------------------------------------------
    Repro(
        name="table_rows_and_cells",
        note="A table is walked the way a page walks it: its rows in order, then the "
             "cells of a row.",
        script="""
            (() => {
              const table = document.createElement('table');
              table.innerHTML = '<tbody><tr><td>1</td><td>2</td></tr>'
                              + '<tr><td>3</td></tr></tbody>';
              const rows = table.rows;
              return [rows.length, rows[0].cells.length, rows[1].cells[0].cellIndex,
                      rows[1].rowIndex, rows[0].cells[0].parentNode.tagName];
            })()
        """,
        expected=[2, 2, 0, 1, "TR"],
    ),
    Repro(
        name="rows_come_from_the_sections_in_document_order",
        note="A row's place is its place among all the table's rows, whichever "
             "section holds it.",
        script="""
            (() => {
              const table = document.createElement('table');
              document.body.appendChild(table);
              table.innerHTML = '<thead><tr><td>h</td></tr></thead>'
                              + '<tbody><tr><td>b</td></tr></tbody>'
                              + '<tfoot><tr><td>f</td></tr></tfoot>';
              return [table.rows.length, table.rows[0].cells[0].textContent,
                      table.rows[2].cells[0].textContent, table.rows[1].rowIndex];
            })()
        """,
        expected=[3, "h", "f", 1],
    ),
    Repro(
        name="a_nested_table_keeps_its_own_rows",
        note="A table nested inside a cell is a table of its own: its rows belong to "
             "it, not to the table around it.",
        script="""
            (() => {
              const table = document.createElement('table');
              document.body.appendChild(table);
              table.innerHTML = '<tr><td>1</td></tr>'
                              + '<tr><td><table><tr><td>n</td></tr></table></td></tr>';
              return [table.rows.length, table.rows[1].cells.length,
                      table.querySelectorAll('tr').length];
            })()
        """,
        expected=[2, 1, 3],
    ),
    Repro(
        name="a_th_counts_as_a_cell",
        note="A heading cell is a cell: it is in the row's collection and knows its "
             "place in it.",
        script="""
            (() => {
              const row = document.createElement('tr');
              const head = document.createElement('th');
              row.appendChild(head);
              document.body.appendChild(row);
              return [row.cells.length, head.cellIndex,
                      head instanceof HTMLTableCellElement, head.constructor.name,
                      row.constructor.name];
            })()
        """,
        expected=[1, 0, True, "HTMLTableCellElement", "HTMLTableRowElement"],
    ),
    Repro(
        name="row_and_cell_indexes_outside_a_table",
        note="A cell knows its place as soon as a row holds it, while a row that no "
             "table owns has no place to report.",
        script="""
            (() => {
              const row = document.createElement('tr');
              const cell = document.createElement('td');
              row.appendChild(cell);
              return [row.rowIndex, cell.cellIndex, document.createElement('td').cellIndex,
                      document.createElement('tr').rowIndex];
            })()
        """,
        expected=[-1, 0, -1, -1],
    ),
    Repro(
        name="rows_and_cells_are_live_and_held_onto",
        note="Reaching for the collection twice gives the same live object, so it "
             "shows a row or cell added afterwards.",
        script="""
            (() => {
              const table = document.createElement('table');
              document.body.appendChild(table);
              table.innerHTML = '<tr><td>1</td></tr>';
              const rows = table.rows;
              const cells = rows[0].cells;
              const added = document.createElement('tr');
              table.appendChild(added);
              rows[0].appendChild(document.createElement('td'));
              return [rows.length, cells.length, table.rows === rows,
                      rows[0].cells === cells, added.rowIndex, table.rows[1] === added];
            })()
        """,
        expected=[2, 2, True, True, 1, True],
    ),
    Repro(
        name="a_label_element_and_its_html_for",
        note="<label> has its own interface, its htmlFor reflects the attribute, and "
             "its control resolves the element it labels.",
        html=(page(body="<label for=f>L</label><input id=f>")),
        script="(() => { const label = document.querySelector('label');"
               " return [label.constructor.name, typeof label.htmlFor, label.htmlFor,"
               "         label.control === document.getElementById('f'),"
               "         label.control.constructor.name]; })()",
        expected=["HTMLLabelElement", "string", "f", True, "HTMLInputElement"],
    ),
    Repro(
        name="a_form_control_box_is_its_attribute_size",
        note="Excluded: in a standards-mode document the box model measures a control "
             "from its attributes alone, so the user-agent sheet's border, padding and "
             "margins are not part of the box.",
        html="<!DOCTYPE html>" + (page(
            body='<input id=i type=text style="margin:0;width:120px;height:20px">')),
        script="""(() => { const r = document.getElementById('i').getBoundingClientRect();
                   return [r.width, r.height]; })()""",
        expected=[120, 20],
        known_divergence="a standards-mode Chrome adds the UA sheet's 2px inset border and"
        " 1px 2px padding, measuring the same input 128x26; its quirks-mode sheet switches"
        " the control to border-box and agrees",
    ),
    Repro(
        name="an_empty_document_still_has_html_head_and_body",
        note="No markup at all still parses into the skeleton every document has.",
        script="[document.documentElement.tagName, document.body !== null,"
               " document.head !== null]",
        expected=["HTML", True, True],
    ),
    # -- fetch types -----------------------------------------------------------
    Repro(
        name="headers_construction_and_lookup",
        note="Names are case-insensitive and appending combines with ', '.",
        script="""
            (() => {
              const headers = new Headers({'content-type': 'text/html'});
              headers.append('x-a', '1');
              headers.append('x-a', '2');
              headers.set('X-B', 'b');
              headers.delete('content-type');
              return [headers.get('X-A'), headers.get('x-a'), headers.has('Content-Type'),
                      headers.get('x-b'), headers.has('nope')];
            })()
        """,
        expected=["1, 2", "1, 2", False, "b", False],
    ),
    Repro(
        name="headers_copy_and_override",
        script="""
            (() => {
              const first = new Headers([['a', '1'], ['b', '2']]);
              const second = new Headers(first);
              second.set('A', '9');
              return [first.get('a'), second.get('a'), second.get('b'),
                      new Headers().get('a') === null];
            })()
        """,
        expected=["1", "9", "2", True],
    ),
    Repro(
        name="headers_iterate_sorted_by_name",
        note="The list is sorted by name however it was built.",
        script="""
            (() => {
              const headers = new Headers([['b', '2'], ['a', '1']]);
              return [Array.from(headers.keys()), Array.from(headers.values()),
                      Array.from(headers.entries())];
            })()
        """,
        expected=[["a", "b"], ["1", "2"], [["a", "1"], ["b", "2"]]],
    ),
    Repro(
        name="request_fields",
        script="""
            (() => {
              const request = new Request('http://example.test/a?x=1');
              return [request.method, request.url, request.headers.get('content-type'),
                      request.bodyUsed, request.destination, request.mode];
            })()
        """,
        expected=["GET", "http://example.test/a?x=1", None, False, "", "cors"],
    ),
    Repro(
        name="request_resolves_a_relative_url_and_keeps_its_init",
        script="""
            (() => {
              const request = new Request('/x', {method: 'post', headers: {'x-a': '1'}});
              return [request.url, request.method, request.headers.get('X-A')];
            })()
        """,
        expected=["http://repro.test/x", "POST", "1"],
    ),
    Repro(
        name="response_fields",
        note="A response a script builds is type 'default', not 'basic'.",
        script="""
            (() => {
              const response = new Response('hello', {status: 201, statusText: 'Created',
                                                      headers: {'x-a': '1'}});
              return [response.status, response.ok, response.statusText,
                      response.headers.get('x-a'), response.bodyUsed, response.type,
                      response.redirected, response.url];
            })()
        """,
        expected=[201, True, "Created", "1", False, "default", False, ""],
    ),
    Repro(
        name="response_clone_is_independent",
        script="""
            (() => {
              const response = new Response('hello', {status: 201, headers: {'x-a': '1'}});
              const copy = response.clone();
              copy.headers.set('x-a', '2');
              return [copy instanceof Response, copy !== response, copy.bodyUsed,
                      response.bodyUsed, [response.status, copy.status],
                      [response.headers.get('x-a'), copy.headers.get('x-a')]];
            })()
        """,
        expected=[True, True, False, False, [201, 201], ["1", "2"]],
    ),
    # -- storage ----------------------------------------------------------------
    # Chromium's key order is its own, so a case that reads the keys sorts them.
    Repro(
        name="storage_round_trip_length_and_order",
        html=EMPTY_PAGE,
        script="""
            (() => {
              localStorage.clear();
              localStorage.setItem('a', '1');
              localStorage.setItem('b', '2');
              localStorage.setItem('c', '3');
              const keys = [];
              for (let index = 0; index < localStorage.length; index += 1) {
                keys.push(localStorage.key(index));
              }
              const removed = localStorage.removeItem('b');
              return [localStorage.getItem('a'), localStorage.getItem('b'), keys.sort(),
                      localStorage.length, removed, localStorage.key(9)];
            })()
        """,
        expected=["1", None, ["a", "b", "c"], 2, None, None],
    ),
    Repro(
        name="storage_coerces_values_and_keys",
        html=EMPTY_PAGE,
        script="""
            (() => {
              localStorage.clear();
              localStorage.setItem(7, 42);
              return [localStorage.getItem('7'), localStorage.getItem(7),
                      localStorage.length, localStorage.getItem('missing')];
            })()
        """,
        expected=["42", "42", 1, None],
    ),
    Repro(
        name="storage_clears_and_separates_the_two_kinds",
        html=EMPTY_PAGE,
        script="""
            (() => {
              localStorage.clear();
              sessionStorage.clear();
              localStorage.setItem('a', '1');
              sessionStorage.setItem('a', '2');
              const both = [localStorage.getItem('a'), sessionStorage.getItem('a')];
              localStorage.clear();
              return [both, [localStorage.length, sessionStorage.length],
                      localStorage === localStorage];
            })()
        """,
        expected=[["1", "2"], [0, 1], True],
    ),
    Repro(
        name="storage_as_named_properties",
        note="A stored name is also a property, and the prototype's members stay "
             "themselves.",
        html=EMPTY_PAGE,
        script="""
            (() => {
              localStorage.clear();
              localStorage.token = 'v';
              const read = [localStorage.token, localStorage.getItem('token'),
                            'token' in localStorage];
              delete localStorage.token;
              return [read, [localStorage.token, localStorage.getItem('token')],
                      localStorage.getItem === undefined ? 'shadowed' : 'kept'];
            })()
        """,
        expected=[["v", "v", True], [None, None], "kept"],
    ),
    # -- selectors --------------------------------------------------------------
    Repro(
        name="attribute_and_child_selectors",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const root = document.createElement('div');
              root.innerHTML = '<p class="x" data-kind="a">1</p><p data-kind="b">2</p>'
                             + '<span data-kind="a">3</span>';
              document.body.appendChild(root);
              return [root.querySelectorAll('p[data-kind]').length,
                      root.querySelectorAll('[data-kind="a"]').length,
                      root.querySelectorAll('div > p').length,
                      root.querySelectorAll('p + p').length,
                      root.querySelector('p[data-kind="b"]').textContent];
            })()
        """,
        expected=[2, 2, 2, 1, "2"],
    ),
    Repro(
        name="nth_child_and_first_last_selectors",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const list = document.createElement('ul');
              list.innerHTML = '<li>1</li><li>2</li><li>3</li><li>4</li>';
              document.body.appendChild(list);
              return [list.querySelectorAll('li:first-child').length,
                      list.querySelectorAll('li:last-child').length,
                      list.querySelectorAll('li:nth-child(2)').length,
                      list.querySelectorAll('li:nth-child(odd)').length,
                      list.querySelector('li:nth-child(3)').textContent];
            })()
        """,
        expected=[1, 1, 1, 2, "3"],
    ),
    Repro(
        name="not_and_group_selectors",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const root = document.createElement('div');
              root.innerHTML = '<p class="a">1</p><p class="b">2</p><span class="a">3</span>';
              document.body.appendChild(root);
              return [root.querySelectorAll('p:not(.a)').length,
                      root.querySelectorAll('p, span').length,
                      root.querySelectorAll('.a').length,
                      root.querySelectorAll('p:not(.b):not(.c)').length];
            })()
        """,
        expected=[1, 3, 2, 1],
    ),
    Repro(
        name="selector_lists_are_document_order_and_unique",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const root = document.createElement('div');
              root.innerHTML = '<i class="hit">1</i><b class="hit">2</b><u class="hit">3</u>';
              document.body.appendChild(root);
              const names = Array.from(root.querySelectorAll('.hit, .hit, b'),
                                       node => node.tagName);
              return [names, root.querySelectorAll('.hit').length];
            })()
        """,
        expected=[["I", "B", "U"], 3],
    ),
    # -- classList and CSSOM ----------------------------------------------------
    Repro(
        name="class_list_toggle_replace_and_value",
        note="toggle with force reports the state it left, and value writes the "
             "attribute verbatim.",
        script="""
            (() => {
              const element = document.createElement('div');
              element.className = 'a b';
              const toggled = [element.classList.toggle('c'),
                               element.classList.toggle('a'),
                               element.classList.toggle('c', true),
                               element.classList.contains('c')];
              const replaced = [element.classList.replace('b', 'd'), element.className];
              element.classList.value = 'x  y';
              return [toggled, replaced, element.className, element.classList.length];
            })()
        """,
        expected=[[True, False, True, True], [True, "d c"], "x  y", 2],
    ),
    Repro(
        name="style_set_property_and_css_text",
        note="cssText replaces the whole declaration block, and removing the last "
             "declaration leaves the attribute behind but empty.",
        script="""
            (() => {
              const element = document.createElement('div');
              element.style.setProperty('color', 'red');
              element.style.setProperty('margin-top', '4px', 'important');
              const read = [element.style.getPropertyValue('color'),
                            element.style.getPropertyPriority('margin-top'),
                            element.getAttribute('style')];
              element.style.cssText = 'padding-top: 2px; padding-left: 3px';
              return [read, [element.style.length, element.style.item(0),
                             element.style.getPropertyValue('color'),
                             element.style.cssText]];
            })()
        """,
        expected=[["red", "important", "color: red; margin-top: 4px !important;"],
                  [2, "padding-top", "", "padding-top: 2px; padding-left: 3px;"]],
    ),
    Repro(
        name="style_remove_property_returns_the_old_value",
        script="""
            (() => {
              const element = document.createElement('div');
              element.style.color = 'blue';
              const removed = element.style.removeProperty('color');
              const missing = element.style.removeProperty('color');
              return [removed, missing, element.getAttribute('style')];
            })()
        """,
        expected=["blue", "", ""],
    ),
    Repro(
        name="emptying_the_style_block_leaves_the_attribute_behind",
        note="Removing the last declaration, assigning cssText or assigning an empty "
             "value all leave style=\"\"; an element that never carried one has no "
             "attribute at all.",
        script="""
            (() => {
              const emptied = kind => {
                const element = document.createElement('div');
                element.style.color = 'blue';
                if (kind === 'remove') element.style.removeProperty('color');
                else if (kind === 'text') element.style.cssText = '';
                else element.style.color = '';
                return [element.getAttribute('style'), element.hasAttribute('style')];
              };
              const untouched = document.createElement('div');
              untouched.style.color = '';
              return [emptied('remove'), emptied('text'), emptied('value'),
                      [untouched.getAttribute('style'), untouched.hasAttribute('style')]];
            })()
        """,
        expected=[["", True], ["", True], ["", True], [None, False]],
    ),
    Repro(
        name="computed_style_reports_declared_values",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const root = document.createElement('div');
              root.innerHTML = '<div id="outer" style="color: rgb(1, 2, 3); font-size: 12px">'
                             + '<span id="inner">t</span></div>';
              document.body.appendChild(root);
              return [getComputedStyle(document.getElementById('inner')).fontSize,
                      getComputedStyle(document.getElementById('outer')).color,
                      root.querySelector('#outer').style.color];
            })()
        """,
        expected=["12px", "rgb(1, 2, 3)", "rgb(1, 2, 3)"],
    ),
    Repro(
        name="response_body_is_a_stream",
        note="A Response made from a string exposes its body as a ReadableStream, so "
             "the body is an object and is not null.",
        script="""
            (() => {
              const response = new Response('hello');
              return [typeof response.body, response.body === null];
            })()
        """,
        expected=["object", False],
    ),
    # -- what is still missing here ---------------------------------------------
    Repro(
        name="storage_spread_copies_the_entries",
        note="Excluded: Object.keys lists the stored names and reading one works, but "
             "copying them into an object does not.",
        html=EMPTY_PAGE,
        script="""
            (() => {
              localStorage.clear();
              localStorage.setItem('a', '1');
              return JSON.stringify({...localStorage});
            })()
        """,
        expected="{}",
        known_divergence="spreading a Storage copies nothing: the own-property"
        " definer hook that path needs is not wired; Chrome yields '{\"a\":\"1\"}'",
    ),
    Repro(
        name="computed_style_inherits_an_inline_colour",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const outer = document.createElement('div');
              outer.setAttribute('style', 'color: rgb(1, 2, 3)');
              const inner = document.createElement('span');
              outer.appendChild(inner);
              document.body.appendChild(outer);
              return getComputedStyle(inner).color;
            })()
        """,
        expected="rgb(1, 2, 3)",
    ),
    Repro(
        name="computed_style_inherits_from_a_stylesheet_rule",
        note="Both children read the rule's colour, and the one that declares its own "
             "spacing keeps it.",
        html=(page(body="""
            <style>.outer { color: rgb(4, 5, 6); font-weight: 700; }
                   .inner { letter-spacing: 3px; }</style>
            <div class="outer"><span class="inner">t</span></div>
            <div class="outer"><span>t</span></div>
        """)),
        script="""
            (() => {
              const inner = document.querySelector('span.inner');
              const plain = document.querySelectorAll('span')[1];
              return [getComputedStyle(inner).color, getComputedStyle(plain).color,
                      getComputedStyle(plain).fontWeight,
                      getComputedStyle(inner).fontWeight,
                      getComputedStyle(inner).letterSpacing];
            })()
        """,
        expected=["rgb(4, 5, 6)", "rgb(4, 5, 6)", "700", "700", "3px"],
    ),
    Repro(
        name="a_fragment_gets_a_body_of_its_own",
        note="Markup that carries no html/body is wrapped, so the fragment's element "
             "is a child of the body rather than a node that is the body.",
        html='<div id="x" style="color: red">hi</div>',
        script="""
            (() => {
              const element = document.getElementById('x');
              return [document.body === element, document.body.tagName,
                      document.body.getAttribute('id'), document.body.getAttribute('style'),
                      document.body.childNodes.length,
                      document.documentElement.tagName,
                      document.body.parentNode === document.documentElement];
            })()
        """,
        expected=[False, "BODY", None, None, 1, "HTML", True],
    ),
    Repro(
        name="the_navigation_api_reports_the_current_entry",
        note="A page that has not moved has one entry -- the current one -- nothing"
             " behind or ahead of it, and no transition.",
        html=(page(body="")),
        script="""
            (() => {
              const current = navigation.currentEntry;
              const entries = navigation.entries();
              return [navigation.canGoBack, navigation.canGoForward,
                      navigation.entries.length, current === navigation.currentEntry,
                      current.index, current.url, current.sameDocument,
                      current.getState(), navigation.transition,
                      typeof navigation.activation, typeof current.key,
                      typeof navigation.entries, Array.isArray(entries), entries.length,
                      entries[0] === current];
            })()
        """,
        expected=[False, False, 0, True, 0, "http://repro.test/dir/page", True,
                  None, None, "object", "string",
                  "function", True, 1, True],
    ),
    Repro(
        name="trusted_types_wraps_what_a_policy_returns",
        note="A policy calls the page's own callback and wraps the string it returns.",
        html=(page(body="")),
        script="""
            (() => {
              const policy = trustedTypes.createPolicy('repro', {
                createHTML: value => '<b>' + value,
                createScriptURL: value => '/safe/' + value,
              });
              const html = policy.createHTML('x');
              const url = policy.createScriptURL('x.js');
              return [trustedTypes.emptyHTML.toString(), trustedTypes.emptyScript.toString(),
                      trustedTypes.emptyHTML === trustedTypes.emptyHTML,
                      trustedTypes.defaultPolicy === null, policy.name,
                      html.toString(), String(html), typeof html,
                      trustedTypes.isHTML(html), trustedTypes.isHTML('x'),
                      trustedTypes.isScriptURL(url), trustedTypes.isScriptURL(html),
                      url.toString()];
            })()
        """,
        expected=["", "", True, True, "repro", "<b>x", "<b>x", "object", True, False,
                  True, False, "/safe/x.js"],
    ),
    Repro(
        name="trusted_types_answers_nothing_about_attribute_types",
        note="Excluded: the spec's attribute and property type tables are not modelled,"
             " so the queries answer nothing.",
        html=(page(body="")),
        script="""
            (() => {
              return [trustedTypes.getAttributeType('div', 'onclick'),
                      trustedTypes.getPropertyType('div', 'innerHTML'),
                      trustedTypes.getTypeMapping()];
            })()
        """,
        expected=["", None, None],
        known_divergence="attribute and property types come from a spec table this DOM"
        " does not model, so the queries answer nothing where Chrome consults the table",
    ),
    Repro(
        name="get_image_data_reads_the_drawn_pixels",
        note="A drawn pixel comes back as its own RGBA bytes, the array is a real"
             " Uint8ClampedArray, and outside the canvas pixels are transparent black.",
        html=(page(body='<canvas id="c" width="4" height="2"></canvas>')),
        script="""
            (() => {
              const canvas = document.getElementById('c');
              const context = canvas.getContext('2d');
              context.fillStyle = '#ff0000';
              context.fillRect(0, 0, 2, 1);
              const image = context.getImageData(0, 0, 4, 2);
              const outside = context.getImageData(-1, -1, 1, 1);
              return [image.width, image.height, image.data.length,
                      image.data instanceof Uint8ClampedArray, image.colorSpace,
                      image.data === image.data, Array.from(image.data.slice(0, 4)),
                      [outside.width, outside.height, outside.data.length, outside.data[3]]];
            })()
        """,
        expected=[4, 2, 32, True, "srgb", True, [255, 0, 0, 255], [1, 1, 4, 0]],
    ),
    Repro(
        name="window_accessories_report_their_idle_state",
        note="Each accessory object is present and says nothing is happening.",
        html=(page(body="")),
        script="""
            (() => {
              return [external.constructor.name, typeof external.AddSearchProvider,
                      styleMedia.type, styleMedia.matchMedium('screen'),
                      styleMedia.matchMedium('print'),
                      crashReport.constructor.name, typeof crashReport.set,
                      launchQueue.constructor.name, typeof launchQueue.setConsumer,
                      fence === null, typeof viewport.segments];
            })()
        """,
        expected=["External", "function", "screen", True, False,
                  "CrashReportContext", "function", "LaunchQueue", "function",
                  True, "object"],
    ),
    Repro(
        name="speech_synthesis_and_the_scheduler_start_idle",
        note="Nothing is spoken, and scheduling hands back a promise.",
        html=(page(body="")),
        script="""
            (() => {
              return [speechSynthesis.pending, speechSynthesis.speaking,
                      speechSynthesis.paused, speechSynthesis.getVoices().length,
                      typeof speechSynthesis.speak,
                      scheduler.constructor.name, typeof scheduler.postTask,
                      typeof scheduler.yield];
            })()
        """,
        expected=[False, False, False, 0, "function",
                  "Scheduler", "function", "function"],
    ),
    Repro(
        name="a_document_with_no_referrer_reads_empty",
        note="A navigation nothing linked to has an empty referrer, and reading it as a"
             " string is what a script does.",
        html=(page(body="")),
        script="[typeof document.referrer, document.referrer, document.referrer.length]",
        expected=["string", "", 0],
    ),
    Repro(
        name="the_page_reports_its_sheets_orientation_and_timings",
        note="The sheet list counts what the page wrote, the screen's orientation follows"
             " its dimensions, and the deprecated timing objects are still there.",
        html=(page(head="<style>.a{color:red}</style>", body="<div class=a>x</div>")),
        script="""(() => {
            return [document.styleSheets.length, typeof document.styleSheets,
                    screen.orientation.type, screen.orientation.angle,
                    screen.orientation.constructor.name,
                    typeof performance.timing.navigationStart,
                    performance.timing.navigationStart > 0,
                    performance.navigation.type, performance.navigation.redirectCount,
                    performance.navigation.constructor.name];
        })()""",
        expected=[1, "object", "landscape-primary", 0, "ScreenOrientation", "number", True,
                  0, 0, "PerformanceNavigation"],
    ),
    Repro(
        name="a_media_element_reports_its_idle_state",
        note="Nothing decodes here, so an audio element reports the state a fresh one"
             " has, and canPlayType answers the types Chrome answers.",
        html=(page(body="")),
        script="""
            (() => {
              const audio = document.createElement('audio');
              const video = document.createElement('video');
              const read = (fn) => { try { return fn(); } catch (error) { return error.name; } };
              return [audio.tagName, audio.constructor.name, video.constructor.name,
                      audio.canPlayType('audio/mpeg'), audio.canPlayType('audio/mp4'),
                      audio.canPlayType('video/webm'), audio.canPlayType('nonsense/thing'),
                      audio.canPlayType.length,
                      audio.textTracks.length, read(() => video.videoTracks.length),
                      audio.paused, audio.ended, audio.currentTime, String(audio.duration),
                      audio.readyState, audio.networkState, audio.volume, audio.muted,
                      audio.loop, audio.autoplay, audio.controls, audio.preload,
                      audio.src, audio.currentSrc, audio.crossOrigin,
                      audio.defaultPlaybackRate, audio.playbackRate, audio.seeking,
                      audio.error, audio.buffered.length, audio.played.length,
                      audio.seekable.length, typeof audio.play().then];
            })()
        """,
        expected=["AUDIO", "HTMLAudioElement", "HTMLVideoElement", "probably", "maybe",
                  "maybe", "", 1, 0, "TypeError", True, False, 0, "NaN", 0, 0, 1, False,
                  False, False, False, "metadata", "", "", None, 1, 1, False, None, 0, 0,
                  0, "function"],
    ),
    Repro(
        name="can_play_type_reads_the_container_and_its_codecs",
        note="Chrome answers for the container and the codecs it carries: a codec it "
             "does not play is an empty string, and a family with no profile a maybe.",
        html=(page(body="")),
        script="""(() => {
            const audio = document.createElement('audio');
            const video = document.createElement('video');
            const ask = (type) => (type.trim().toLowerCase().startsWith('video')
                ? video : audio).canPlayType(type);
            const types = [
                'audio/mpeg', 'audio/mpeg; codecs="mp3"', 'audio/mpeg; codecs=mp3',
                'audio/mpeg; codecs="MP3"', 'audio/mp4',
                'audio/mp4; codecs="mp4a.40.2"', 'audio/mp4; codecs="mp4a.40.2"; foo=bar',
                'audio/ogg; codecs="vorbis"', 'audio/ogg; codecs="opus"',
                'audio/webm; codecs="vorbis"', 'audio/aac', 'audio/flac', 'audio/opus',
                'audio/wav; codecs="1"', 'audio/wav; codecs="2"',
                'video/mp4; codecs="avc1.42E01E"', 'video/mp4; codecs="avc1"',
                'video/mp4; codecs="hvc1.1.6.L93.B0"',
                'video/mp4; codecs="avc1.42E01E, mp4a.40.2"',
                'video/mp4; codecs="theora"', "video/mp4; codecs='avc1.42E01E'",
                'video/webm; codecs="vp8, vorbis"', 'video/webm; codecs="vp9"',
                'video/quicktime', 'application/ogg; codecs="vorbis"',
                '  AUDIO/MPEG  ', 'nonsense/thing', 'audio/mpeg3',
            ];
            return types.reduce((all, type) => (all[type] = ask(type), all), {});
        })()""",
        expected={
            "audio/mpeg": "probably",
            'audio/mpeg; codecs="mp3"': "probably",
            "audio/mpeg; codecs=mp3": "probably",
            'audio/mpeg; codecs="MP3"': "",
            "audio/mp4": "maybe",
            'audio/mp4; codecs="mp4a.40.2"': "probably",
            'audio/mp4; codecs="mp4a.40.2"; foo=bar': "probably",
            'audio/ogg; codecs="vorbis"': "probably",
            'audio/ogg; codecs="opus"': "probably",
            'audio/webm; codecs="vorbis"': "probably",
            "audio/aac": "probably",
            "audio/flac": "probably",
            "audio/opus": "",
            'audio/wav; codecs="1"': "probably",
            'audio/wav; codecs="2"': "",
            'video/mp4; codecs="avc1.42E01E"': "probably",
            'video/mp4; codecs="avc1"': "maybe",
            'video/mp4; codecs="hvc1.1.6.L93.B0"': "probably",
            'video/mp4; codecs="avc1.42E01E, mp4a.40.2"': "probably",
            'video/mp4; codecs="theora"': "",
            "video/mp4; codecs='avc1.42E01E'": "",
            'video/webm; codecs="vp8, vorbis"': "probably",
            'video/webm; codecs="vp9"': "probably",
            "video/quicktime": "",
            'application/ogg; codecs="vorbis"': "probably",
            "  AUDIO/MPEG  ": "probably",
            "nonsense/thing": "",
            "audio/mpeg3": "",
        },
    ),
    Repro(
        name="a_link_reports_the_as_it_was_given",
        note="as reflects the attribute limited to known values: an unknown one is"
             " stored and reads back empty.",
        html=(page(body="")),
        script="""(() => {
            const link = document.createElement('link');
            const set = (value) => { link.as = value;
                return [String(link.getAttribute('as')), link.as]; };
            return [link.as, set('script'), set('bogus'), set('SCRIPT'), set(''), set(null)];
        })()""",
        expected=["", ["script", "script"], ["bogus", ""], ["SCRIPT", "script"], ["", ""],
                  ["null", ""]],
    ),
    Repro(
        name="document_open_empties_the_document_and_returns_it",
        note="open() drops what the document held and hands the document back, so the"
             " writes that follow build a new one.",
        html=(page(body="<p id=old>old</p>")),
        script="""(() => {
            const before = [document.readyState, document.querySelectorAll('*').length];
            const returned = document.open() === document;
            const after = [document.readyState, document.querySelectorAll('*').length,
                           String(document.getElementById('old')), String(document.body)];
            document.write('<p id="new">new</p>');
            return [before, returned, after, document.getElementById('new') !== null];
        })()""",
        expected=[["complete", 4], True, ["loading", 0, "null", "null"], True],
    ),
    Repro(
        name="the_document_reports_its_mode_and_type",
        note="A page with no doctype is in quirks mode, and what it is, where its"
             " direction comes from, and what the browser promises about cookies.",
        html="<html><head></head><body></body></html>",
        script="""[document.compatMode, document.contentType, document.dir,
                    navigator.cookieEnabled, navigator.pdfViewerEnabled,
                    navigator.webdriver === undefined || typeof navigator.webdriver,
                    document.querySelectorAll('*').length]""",
        expected=["BackCompat", "text/html", "", True, True, "boolean", 3],
    ),
    Repro(
        name="a_page_with_a_doctype_is_in_standards_mode",
        html="<!DOCTYPE html><html><head></head><body></body></html>",
        script="[document.compatMode, document.dir]",
        expected=["CSS1Compat", ""],
    ),
    Repro(
        name="the_navigator_answers_by_secure_context",
        note="An insecure origin keeps permissions and mediaSession and withholds the rest.",
        html=EMPTY_PAGE,
        script="""['mediaDevices', 'permissions', 'mediaSession', 'serviceWorker', 'credentials',
                   'bluetooth', 'storage'].map((name) => typeof navigator[name])
                   .concat([navigator.javaEnabled()])""",
        expected=["undefined", "object", "object", "undefined", "undefined", "undefined",
                  "undefined", False],
    ),
    Repro(
        name="the_mime_type_collection_answers_on_its_own",
        note="Reading the mime types first has to work: the two legacy collections "
             "reference each other, and neither may wait on the other being asked for.",
        html=EMPTY_PAGE,
        script="""(() => {
  const collection = navigator.mimeTypes;
  return ['mimeTypes' in navigator, Object.prototype.toString.call(collection),
          collection === navigator.mimeTypes, typeof collection.item,
          typeof collection.namedItem, typeof collection[Symbol.iterator],
          Array.prototype.every.call(collection, (entry) =>
              Object.prototype.toString.call(entry) === '[object MimeType]' &&
              (entry.enabledPlugin === null ||
               Object.prototype.toString.call(entry.enabledPlugin) === '[object Plugin]'))];
})()""",
        expected=[True, "[object MimeTypeArray]", True, "function", "function", "function", True],
    ),
    Repro(
        name="a_global_interface_object_is_not_enumerable",
        note="An interface object is an own, writable, configurable property of the "
             "window that is not enumerable.",
        html=EMPTY_PAGE,
        script="""Object.keys(window).filter((name) => /^[A-Z]/.test(name)).length""",
        expected=0,
    ),
    Repro(
        name="the_window_hides_what_the_reference_browser_never_installs",
        note="Retired and platform-gated Window members are absent, not present as "
             "undefined: the baseline lists every state, a browser installs its subset.",
        html=EMPTY_PAGE,
        script="""['orientation', 'defaultStatus', 'defaultstatus', 'printing', 'sharedStorage',
                   'subApps', 'crossOriginEmbedderPolicy', 'testOriginTrialGlobalAttribute',
                   'ontouchstart', 'maximize'].map((name) => name in window)""",
        expected=[False] * 10,
    ),
    Repro(
        name="the_legacy_factory_functions_build_their_own_elements",
        note="Image, Option and Audio are functions of their own rather than interfaces: "
             "hidden on the window, and a call builds a detached element of the interface "
             "they belong to.",
        html=EMPTY_PAGE,
        script="""(() => {
  const main = {Image: 'HTMLImageElement', Option: 'HTMLOptionElement', Audio: 'HTMLAudioElement'};
  return Object.keys(main).map((name) => {
    const factory = window[name];
    const made = name === 'Option' ? new factory('t', 'v')
               : name === 'Audio' ? new factory() : new factory(3, 4);
    return [typeof factory, factory.name, factory.length, name in window,
            Object.keys(window).includes(name),
            made instanceof window[main[name]],
            Object.prototype.toString.call(made)];
  });
})()""",
        expected=[["function", "Image", 0, True, False, True, "[object HTMLImageElement]"],
                  ["function", "Option", 0, True, False, True, "[object HTMLOptionElement]"],
                  ["function", "Audio", 0, True, False, True, "[object HTMLAudioElement]"]],
    ),
    Repro(
        name="a_document_location_is_the_windows_location",
        note="One Location per window, from an own accessor, not the prototype.",
        html=(page(body="")),
        script="""(() => {
            const own = Object.getOwnPropertyDescriptor(document, 'location');
            return [document.location === window.location, document.location.href,
                    document.location.pathname, typeof document.location,
                    'location' in document,
                    Object.getOwnPropertyDescriptor(Document.prototype, 'location') === undefined,
                    own.enumerable, own.configurable,
                    typeof own.get, typeof own.set];
        })()""",
        expected=[True, URL, "/dir/page", "object", True, True,
                  True, False, "function", "function"],
    ),
    Repro(
        name="a_document_with_no_browsing_context_has_neither_location_nor_view",
        note="A document a script built has no window, so both read null.",
        html=(page(body="")),
        script="""(() => {
            const made = document.implementation.createHTMLDocument('t');
            return [String(made.location), String(made.defaultView)];
        })()""",
        expected=["null", "null"],
    ),
    Repro(
        name="assigning_location_on_that_document_throws",
        note="The assignment has no window to forward to.",
        html=(page(body="")),
        script="(() => { document.implementation.createHTMLDocument('t').location = '/x'; })()",
        expect_error="TypeError",
    ),
    Repro(
        name="a_sandbox_blocks_scripts_it_does_not_grant",
        note="Without allow-scripts the frame's own script never runs, while the "
             "element and its text stay in the document.",
        html=(page(head="<script>window.__ran = 'no';</script>",
                  body='<iframe id="f" sandbox="allow-same-origin" srcdoc='
                       '"&lt;script&gt;parent.__ran = &quot;yes&quot;;&lt;/script&gt;">'
                       '</iframe>')),
        script="""(() => {
            const frame = document.getElementById('f');
            return [window.__ran,
                    frame.contentDocument.querySelectorAll('script').length];
        })()""",
        expected=["no", 1],
    ),
    Repro(
        name="a_sandbox_that_grants_scripts_runs_them",
        note="The same frame with allow-scripts runs the script it carries.",
        html=(page(head="<script>window.__ran = 'no';</script>",
                  body='<iframe id="f" sandbox="allow-scripts allow-same-origin" srcdoc='
                       '"&lt;script&gt;parent.__ran = &quot;yes&quot;;&lt;/script&gt;">'
                       '</iframe>')),
        script="""(() => {
            const frame = document.getElementById('f');
            return [window.__ran,
                    frame.contentDocument.querySelectorAll('script').length];
        })()""",
        expected=["yes", 1],
    ),
    Repro(
        name="the_client_hints_report_the_browser_shape",
        match_client_hints=True,
        note="Brands and their order follow the captured browser profile; the frozen array "
             "and the object's identity are checked independently of version numbers.",
        url="https://repro.test/dir/page",
        html=(page(body="")),
        script="""(() => {
            const data = navigator.userAgentData;
            const brands = data.brands;
            return [typeof data, data.constructor.name,
                    brands.map(brand => brand.brand).join(','),
                    brands.every(brand => /^\\d+$/.test(brand.version)),
                    typeof data.mobile, typeof data.platform,
                    Object.isFrozen(brands), brands === data.brands,
                    data === navigator.userAgentData,
                    Object.keys(data.toJSON()).join(','),
                    data.toJSON().brands === data.toJSON().brands,
                    String(data), data instanceof NavigatorUAData];
        })()""",
        expected=["object", "NavigatorUAData", "Not:A-Brand,Chromium,Google Chrome",
                  True, "boolean", "string", True, False, False,
                  "brands,mobile,platform", False, "[object NavigatorUAData]", True],
    ),
    Repro(
        name="the_client_hints_agree_with_the_user_agent",
        note="Both come from the same build: the hint's Chromium version is the "
             "one the user agent string names.",
        url="https://repro.test/dir/page",
        html=(page(body="")),
        script="""(() => {
            const version = navigator.userAgent.match(/Chrome\\/(\\d+)/)[1];
            const brands = navigator.userAgentData.brands;
            const chromium = brands.find(brand => brand.brand === 'Chromium');
            const chrome = brands.find(brand => brand.brand === 'Google Chrome');
            return [chromium.version === version, chrome.version === version, brands.length];
        })()""",
        expected=[True, True, 3],
    ),
    Repro(
        name="the_client_hints_are_hidden_outside_a_secure_context",
        note="Chrome exposes no userAgentData on an insecure origin.",
        url="http://repro.test/dir/page",
        html=(page(body="")),
        script="[typeof navigator.userAgentData, isSecureContext]",
        expected=["undefined", False],
    ),
    Repro(
        name="the_motion_events_arrive_with_empty_readings",
        note="Chrome hands a page that listens for motion one reading per type when the "
             "document loads: the fields are null, the interval is its own, and only the "
             "absolute orientation type says so.",
        url="https://repro.test/dir/page",
        html=(page(head="""<script>
            window.__motion = [];
            const note = (e) => window.__motion.push([
                e.type,
                [e instanceof Event, e instanceof DeviceMotionEvent,
                 e instanceof DeviceOrientationEvent],
                e.interval === undefined ? null : e.interval,
                e.acceleration ? [e.acceleration.x, e.acceleration.y, e.acceleration.z] : null,
                e.rotationRate ? [e.rotationRate.alpha, e.rotationRate.beta,
                                  e.rotationRate.gamma] : null,
                'alpha' in e ? e.alpha : null,
                'absolute' in e ? e.absolute : null]);
            for (const type of ['devicemotion', 'deviceorientation', 'deviceorientationabsolute'])
                addEventListener(type, note);
        </script>""")),
        script="""(() => {
            const seen = {};
            for (const entry of window.__motion) seen[entry[0]] = entry.slice(1);
            return [Object.keys(seen).sort(), seen.devicemotion, seen.deviceorientation,
                    seen.deviceorientationabsolute];
        })()""",
        settle=0.3,
        expected=[
            ["devicemotion", "deviceorientation", "deviceorientationabsolute"],
            [[True, True, False], 16, [None, None, None], [None, None, None], None, None],
            [[True, False, True], None, None, None, None, False],
            [[True, False, True], None, None, None, None, True],
        ],
    ),
    Repro(
        name="dom_exception_length_counts_only_required_arguments",
        note="DOMException takes two optional arguments, so its constructor length is 0 "
             "while the legacy code still answers for the name.",
        html=(page(body="")),
        script="[DOMException.length, new DOMException('bad', 'InvalidStateError').code,"
               " new DOMException('bad', 'InvalidStateError').name]",
        expected=[0, 11, "InvalidStateError"],
    ),
    Repro(
        name="the_motion_interfaces_are_gone_outside_a_secure_context",
        note="Chrome exposes no device-motion interface on an insecure origin: the names "
             "are absent, not merely silent.",
        url="http://repro.test/dir/page",
        html=(page(body="")),
        script="[typeof DeviceMotionEvent, typeof DeviceMotionEventAcceleration,"
               " typeof DeviceMotionEventRotationRate, typeof DeviceOrientationEvent,"
               " isSecureContext]",
        expected=["undefined", "undefined", "undefined", "undefined", False],
    ),
    Repro(
        name="the_time_origin_belongs_to_the_document",
        note="Chrome counts performance.now() from the document's own navigation, so the "
             "epoch origin plus the offset is still today and the offset starts small.",
        url="https://repro.test/dir/page",
        html=(page(body="")),
        script="[Math.abs(performance.timeOrigin + performance.now() - Date.now()) < 50,"
               " Math.round(performance.now()) < 1000,"
               " Math.abs(performance.timing.navigationStart - performance.timeOrigin) < 50]",
        expected=[True, True, True],
    ),
    Repro(
        name="pageshow_bubbles_and_the_page_is_revealed",
        note="Chrome reports pageshow as bubbling and follows it with one pagereveal, "
             "both fired at the window once the document has loaded.",
        url="https://repro.test/dir/page",
        html=(page(head="""<script>
            window.__seen = [];
            const note = (e) => window.__seen.push([e.type, e.bubbles,
                e.constructor && e.constructor.name]);
            for (const name of ['pageshow', 'pagereveal']) addEventListener(name, note);
        </script>""")),
        script="window.__seen",
        settle=0.3,
        expected=[["pageshow", True, "PageTransitionEvent"],
                  ["pagereveal", False, "PageRevealEvent"]],
    ),
    Repro(
        name="a_media_element_ends_where_undecodable_bytes_end",
        note="Nothing here decodes, and Chrome ends bytes it cannot use the same way: "
             "loadstart, progress, suspend, error, and never a load.",
        url="https://repro.test/dir/page",
        html=(page(head="<script>window.__media = [];</script>",
                  body='<video id="v" src="/movie.mp4"></video><script>'
                       'const video = document.querySelector("video");'
                       'for (const name of ["loadstart", "progress", "suspend", "error",'
                       ' "load", "loadend"])'
                       ' video.addEventListener(name, (e) => __media.push(e.type));</script>')),
        script="window.__media",
        settle=0.5,
        expected=["loadstart", "progress", "suspend", "error"],
    ),
    Repro(
        name="an_inserted_frame_has_its_window_right_away",
        note="A frame with no document yet hands out no window; the insertion gives "
             "it one in the same turn, and every read returns that same object.",
        html=(page(body="")),
        script="""(() => {
            const frame = document.createElement('iframe');
            const detached = frame.contentWindow;
            document.head.append(frame);
            return [detached, frame.contentWindow === null,
                    frame.contentWindow === frame.contentWindow,
                    frame.contentDocument !== null, window.length,
                    frame.contentDocument.readyState];
        })()""",
        expected=[None, False, True, True, 1, "complete"],
    ),
      *(Repro(
          name=f"an_inline_inserted_{label}_frame_starts_with_about_blank",
          note="Insertion exposes the child realm immediately; the requested document "
               "has not committed while the parent's inline script is running.",
          html=(page(body="""<script>
              const frame = document.createElement('iframe');
              __SETUP__;
              const detached = frame.contentWindow === null;
              document.body.appendChild(frame);
              const child = frame.contentWindow, doc = frame.contentDocument;
              window.__initialFrame = [detached, child !== null, doc.URL, doc.readyState,
                  doc.body !== null, doc.compatMode, child === frame.contentWindow,
                  child === window[0], child === child.window, doc === child.document,
                  doc.defaultView === child, child.parent === window,
                  Object.getPrototypeOf(child) === child.Window.prototype,
                  child.Object !== Object, child.document.getElementById('inside') === null];
          </script>""".replace("__SETUP__", setup))),
          script="window.__initialFrame",
          expected=[True, True, "about:blank", "complete", True, "BackCompat",
                    True, True, True, True, True, True, True, True, True],
      ) for label, setup in (
          ("empty", ""),
          ("blank", "frame.src = 'about:blank'"),
          ("srcdoc", "frame.srcdoc = '<body><p id=inside>inner</p>'"),
          ("data", "frame.src = 'data:text/html,<body>inner</body>'"),
      )),
      Repro(
          name="initial_blank_frame_writes_survive_its_load_event",
          note="An initial about:blank load does not replace the document the inserting "
               "script wrote; the child supplies its own native Window and intrinsics.",
          html=(page(body="""<script>
              const frame = document.createElement('iframe');
              frame.src = 'about:blank';
              window.__blankLoads = 0;
              frame.onload = () => ++__blankLoads;
              document.body.appendChild(frame);
              window.__blankWindow = frame.contentWindow;
              window.__blankDocument = frame.contentDocument;
              __blankDocument.body.innerHTML = '<p id=inside>kept</p>';
          </script>""")),
          script="""[__blankLoads, __blankWindow === document.querySelector('iframe').contentWindow,
              __blankDocument === __blankWindow.document,
              __blankWindow.document.body.textContent, __blankDocument.readyState]""",
          settle=0.1,
          expected=[1, True, True, "kept", "complete"],
      ),
      *(Repro(
          name=f"initial_{label}_frame_load_fires_during_insertion",
          note="Chrome's initial empty document is complete and its load listener "
               "runs before the parent's appendChild returns.",
          html=EMPTY_PAGE,
          script="""(() => {
              const events = [], frame = document.createElement('iframe');
              __SETUP__;
              frame.onload = () => events.push('load:' + frame.contentDocument.readyState);
              events.push('before');
              document.body.appendChild(frame);
              events.push('after');
              return events;
          })()""".replace("__SETUP__", setup),
          expected=["before", "load:complete", "after"],
      ) for label, setup in (("empty", ""), ("blank", "frame.src = 'about:blank'"))),
      Repro(
          name="srcdoc_commits_after_the_inserting_script",
          note="A srcdoc script runs after the insertion returns, with a stable "
               "WindowProxy and a different document from the initial about:blank.",
          html=(page(body="""<script>
              const frame = document.createElement('iframe');
              frame.srcdoc = '<body><script>parent.__srcdocRanAfter = parent.__insertionReturned === true<\\/script>';
              document.body.appendChild(frame);
              window.__srcdocWindow = frame.contentWindow;
              window.__initialDocument = frame.contentDocument;
              window.__insertionReturned = true;
          </script>""")),
          script="""[__srcdocRanAfter, __srcdocWindow === document.querySelector('iframe').contentWindow,
              __initialDocument !== __srcdocWindow.document, __srcdocWindow.document.URL]""",
          settle=0.1,
          expected=[True, True, True, "about:srcdoc"],
      ),
      Repro(
          name="reinserting_a_frame_creates_a_new_browsing_context",
          note="Removing a frame clears contentWindow/contentDocument; reinserting "
               "it creates a fresh window rather than reviving the detached one.",
          html=EMPTY_PAGE,
          script="""(() => {
              const frame = document.createElement('iframe');
              document.body.appendChild(frame);
              const child = frame.contentWindow;
              child.document.body.innerHTML = '<p>old</p>';
              frame.remove();
              const detached = [frame.contentWindow === null, frame.contentDocument === null];
              document.body.appendChild(frame);
              return [...detached, frame.contentWindow !== child,
                      frame.contentDocument.body.textContent, frame.contentDocument.readyState];
          })()""",
          expected=[True, True, True, "", "complete"],
      ),
    Repro(
        name="a_child_window_keeps_its_member_names",
        note="The same-origin child is a proxy here, so what it hands back has to be the "
             "child's own member: a Python callback reaches the page named fn_<member>.",
        html=(page(body='<iframe name="f"></iframe>')),
        script="""(() => {
            const frame = document.querySelector('iframe');
            const child = frame.contentWindow;
            return [String(child.addEventListener), child.addEventListener.name,
                    child.addEventListener === frame.contentWindow.addEventListener,
                    String(child.setTimeout)];
        })()""",
        expected=["function addEventListener() { [native code] }", "addEventListener",
                  True, "function setTimeout() { [native code] }"],
    ),
    Repro(
        name="a_webgl_context_answers_like_chrome",
        note="No GL backend: identity, limits, object creation and the state a page can "
             "query are the observable part; drawing leaves the clear colour in the buffer.",
        script="""(() => {
  const canvas = document.createElement('canvas');
  const gl = canvas.getContext('webgl');
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  const attributes = gl.getContextAttributes();
  return [Object.prototype.toString.call(gl), gl instanceof WebGLRenderingContext,
          gl.canvas === canvas, canvas.getContext('webgl') === gl,
          [gl.drawingBufferWidth, gl.drawingBufferHeight],
          typeof gl.getParameter(gl.VERSION), gl.getParameter(gl.MAX_TEXTURE_SIZE),
          [gl.getParameter(gl.TRIANGLES), gl.getError()],
          [gl.TRIANGLES, WebGLRenderingContext.TRIANGLES,
           WebGLRenderingContext.prototype.TRIANGLES],
          [gl.createShader(gl.VERTEX_SHADER).constructor.name,
           gl.createProgram().constructor.name, gl.createTexture().constructor.name,
           buffer instanceof WebGLObject],
          [gl.isBuffer(buffer), gl.isBuffer(null), gl.isContextLost(), gl.getError()],
          Array.from(gl.getParameter(gl.VIEWPORT)),
          gl.getParameter(gl.VIEWPORT) instanceof Int32Array,
          gl.getSupportedExtensions().length > 0,
          [attributes.alpha, attributes.stencil],
          [gl.getParameter(0), gl.getError()]];
})()""",
        expected=["[object WebGLRenderingContext]", True, True, True, [300, 150], "string",
                  16384, [None, 1280], [4, 4, 4],
                  ["WebGLShader", "WebGLProgram", "WebGLTexture", True],
                  [True, False, False, 0], [0, 0, 300, 150], True, True, [True, False],
                  [None, 1280]],
    ),
    Repro(
        name="a_cleared_webgl_buffer_reads_back_the_clear_colour",
        software_rendering=True,
        note="The one drawing effect this backend keeps: clear fills the buffer.",
        script="""(() => {
  const gl = document.createElement('canvas').getContext('webgl');
  gl.clearColor(0.25, 0.5, 0.75, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  const pixels = new Uint8Array(4);
  gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
  return [Array.from(pixels).join(','),
          Array.from(gl.getParameter(gl.COLOR_CLEAR_VALUE)).join(',')];
})()""",
        expected=["64,128,191,255", "0.25,0.5,0.75,1"],
    ),
    Repro(
        name="a_dom_parser_builds_a_document_of_every_supported_type",
        note="An HTML parse builds the skeleton a navigation does, without scripts; an XML"
             " parse keeps namespaces and whitespace, and reports a parsererror document"
             " rather than throwing.  A parsed document belongs to no window.",
        script="""(() => {
  const answers = [];
  const step = (label, fn) => {
    try { answers.push(label + '=' + fn()); }
    catch (error) { answers.push(label + '!' + error.name + ': ' + error.message); }
  };
  const shape = doc => [Object.prototype.toString.call(doc), doc.contentType,
                        doc.compatMode, doc.URL === location.href,
                        doc.defaultView === null,
                        doc.documentElement ? doc.documentElement.localName : 'null',
                        String(doc.doctype && doc.doctype.name),
                        doc.location === null].join('|');
  step('page', () => shape(document));
  step('createHTMLDocument', () => shape(document.implementation.createHTMLDocument('t')));
  step('createDocument', () => shape(document.implementation.createDocument(null, 'root')));
  step('createDocument xhtml', () => shape(document.implementation.createDocument(
    'http://www.w3.org/1999/xhtml', 'html')));
  step('proto', () => [Object.getPrototypeOf(document) === HTMLDocument.prototype,
    document instanceof HTMLDocument, XMLDocument.prototype instanceof Document].join(','));
  step('parser', () => { const parser = new DOMParser();
    return [Object.prototype.toString.call(parser), DOMParser.length,
            Object.getPrototypeOf(parser) === DOMParser.prototype].join(','); });
  step('html', () => shape(new DOMParser().parseFromString(
    '<!DOCTYPE html><html><body><p id=x>hi</p></body></html>', 'text/html')));
  step('html basics', () => { const doc = new DOMParser().parseFromString(
      '<!DOCTYPE html><html><head><title>t</title></head><body>'
      + '<div id=d class=c>text</div></body></html>', 'text/html');
    const div = doc.getElementById('d');
    return [doc.title, div.className, div.textContent, div.ownerDocument === doc,
            div.tagName, doc.createElement('span').ownerDocument === doc].join('|'); });
  step('html fragment', () => shape(new DOMParser().parseFromString('<b>x</b>', 'text/html')));
  step('html empty', () => shape(new DOMParser().parseFromString('', 'text/html')));
  step('html script not run', () => { const before = window.__ran;
    const doc = new DOMParser().parseFromString(
      '<!DOCTYPE html><body><script>window.__ran = 1<\\/script><img src="/nope.png"></body>',
      'text/html');
    return [window.__ran === before, doc.body.childNodes.length].join(','); });
  step('html relative urls', () => { const doc = new DOMParser().parseFromString(
      '<!DOCTYPE html><body><a id=a href="sub/page.html">x</a><img id=i src="/i.png"></body>',
      'text/html');
    return [doc.getElementById('a').href, doc.getElementById('i').src].join('|'); });
  step('html is separate', () => { const doc = new DOMParser().parseFromString(
      '<!DOCTYPE html><body><div id=d></div>', 'text/html');
    return [doc === document, doc.getElementById('d') !== null,
            document.getElementById('d') === null, doc.ownerDocument === null,
            doc.parentNode === null].join(','); });
  step('xml', () => { const doc = new DOMParser().parseFromString(
      '<root a="1"><child/></root>', 'text/xml');
    return [Object.prototype.toString.call(doc), doc.contentType, doc.compatMode,
            doc.documentElement.localName, doc.documentElement.getAttribute('a'),
            doc.documentElement.childNodes.length, String(doc.documentElement.namespaceURI),
            doc.location === null].join('|'); });
  step('xml declaration', () => { const doc = new DOMParser().parseFromString(
      '<?xml version="1.0"?><root/>', 'application/xml');
    return [doc.documentElement.localName, doc.documentElement.childNodes.length].join(','); });
  step('xml malformed', () => { const doc = new DOMParser().parseFromString(
      '<root><unclosed></root>', 'text/xml');
    return [doc.documentElement.localName,
            doc.getElementsByTagName('parsererror').length,
            doc.documentElement.textContent.length > 0].join('|'); });
  step('xml whitespace', () => { const doc = new DOMParser().parseFromString(
      '<root> <a/> </root>', 'text/xml');
    return [doc.documentElement.childNodes.length,
            doc.documentElement.firstChild.nodeType].join(','); });
  step('xml entities', () => { const doc = new DOMParser().parseFromString(
      '<root a="&lt;x&gt;">a &amp; b &#65;</root>', 'text/xml');
    return [doc.documentElement.getAttribute('a'),
            doc.documentElement.textContent].join('|'); });
  step('xml comment', () => { const doc = new DOMParser().parseFromString(
      '<root><!--c--><a/></root>', 'text/xml');
    return [doc.documentElement.childNodes.length,
            doc.documentElement.firstChild.nodeType].join(','); });
  step('svg', () => { const doc = new DOMParser().parseFromString(
      '<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>', 'image/svg+xml');
    const circle = doc.documentElement.firstElementChild;
    return [doc.contentType, String(doc.documentElement.namespaceURI),
            String(circle.namespaceURI),
            Object.prototype.toString.call(circle)].join('|'); });
  step('xhtml', () => { const doc = new DOMParser().parseFromString(
      '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p></body></html>',
      'application/xhtml+xml');
    const p = doc.getElementsByTagName('p')[0];
    return [doc.contentType, String(doc.documentElement.namespaceURI),
            Object.prototype.toString.call(p)].join('|'); });
  step('bad mime', () => { try { new DOMParser().parseFromString('<a/>', 'bogus/type');
    return 'no throw'; } catch (error) { return error.name + ': ' + error.message; } });
  step('mime variants', () => { const make = mime => {
      try { return new DOMParser().parseFromString('<a/>', mime).contentType; }
      catch (error) { return error.name; } };
    return ['text/html', 'text/xml', 'application/xml', 'application/xhtml+xml',
            'image/svg+xml', 'text/plain', 'TEXT/HTML', 'text/html;charset=utf-8',
            ' application/xml ', ''].map(make).join('|'); });
  step('missing arg', () => { try { new DOMParser().parseFromString('<a/>'); return 'no throw'; }
    catch (error) { return error.name + ': ' + error.message; } });
  step('computed style', () => { const doc = new DOMParser().parseFromString(
      '<!DOCTYPE html><body><div id=d></div>', 'text/html');
    return ['[' + getComputedStyle(doc.getElementById('d')).display + ']',
            '[' + getComputedStyle(document.body).display + ']'].join('|'); });
  return answers;
})()""",
        expected=[
            "page=[object HTMLDocument]|text/html|BackCompat|true|false|html|null|false",
            "createHTMLDocument=[object HTMLDocument]|text/html|CSS1Compat|false|true|html|html|true",
            "createDocument=[object XMLDocument]|application/xml|CSS1Compat|false|true|root|null|true",
            "createDocument xhtml=[object XMLDocument]|application/xhtml+xml|CSS1Compat|false|true|html|null|true",
            "proto=true,true,true",
            "parser=[object DOMParser],0,true",
            "html=[object HTMLDocument]|text/html|CSS1Compat|true|true|html|html|true",
            "html basics=t|c|text|true|DIV|true",
            "html fragment=[object HTMLDocument]|text/html|BackCompat|true|true|html|null|true",
            "html empty=[object HTMLDocument]|text/html|BackCompat|true|true|html|null|true",
            "html script not run=true,2",
            "html relative urls=http://repro.test/dir/sub/page.html|http://repro.test/i.png",
            "html is separate=false,true,true,true,true",
            "xml=[object XMLDocument]|text/xml|CSS1Compat|root|1|1|null|true",
            "xml declaration=root,0",
            "xml malformed=root|1|true",
            "xml whitespace=3,3",
            "xml entities=<x>|a & b A",
            "xml comment=2,8",
            "svg=image/svg+xml|http://www.w3.org/2000/svg|http://www.w3.org/2000/svg|[object SVGCircleElement]",
            "xhtml=application/xhtml+xml|http://www.w3.org/1999/xhtml|[object HTMLParagraphElement]",
            "bad mime=TypeError: Failed to execute 'parseFromString' on 'DOMParser':"
            " The provided value 'bogus/type' is not a valid enum value of type SupportedType.",
            "mime variants=text/html|text/xml|application/xml|application/xhtml+xml|image/svg+xml"
            "|TypeError|TypeError|TypeError|TypeError|TypeError",
            "missing arg=TypeError: Failed to execute 'parseFromString' on 'DOMParser':"
            " 2 arguments required, but only 1 present.",
            "computed style=[]|[block]",
        ],
    ),
    Repro(
        name="the_interface_a_tag_gets_is_the_reference_browsers",
        note="Chrome answers a name it has no interface for without a hyphen with"
             " HTMLUnknownElement, and a hyphenated one -- a custom element name -- with"
             " a plain HTMLElement.",
        script="""(() => ['p', 'h1', 'ul', 'li', 'table', 'tr', 'td', 'caption', 'tbody',
  'form', 'input', 'a', 'img', 'br', 'hr', 'q', 'del', 'time', 'source', 'details',
  'dialog', 'output', 'fieldset', 'legend', 'slot', 'div', 'html', 'b', 'em', 'code',
  'nav', 'section', 'main', 'bogus', 'unknown-thing', 'my-widget']
  .map(tag => tag + '=' + document.createElement(tag).constructor.name).join(' '))()""",
        expected="p=HTMLParagraphElement h1=HTMLHeadingElement ul=HTMLUListElement"
                 " li=HTMLLIElement table=HTMLTableElement tr=HTMLTableRowElement"
                 " td=HTMLTableCellElement caption=HTMLTableCaptionElement"
                 " tbody=HTMLTableSectionElement form=HTMLFormElement input=HTMLInputElement"
                 " a=HTMLAnchorElement img=HTMLImageElement br=HTMLBRElement hr=HTMLHRElement"
                 " q=HTMLQuoteElement del=HTMLModElement time=HTMLTimeElement"
                 " source=HTMLSourceElement details=HTMLDetailsElement"
                 " dialog=HTMLDialogElement output=HTMLOutputElement"
                 " fieldset=HTMLFieldSetElement legend=HTMLLegendElement slot=HTMLSlotElement"
                 " div=HTMLDivElement html=HTMLHtmlElement b=HTMLElement em=HTMLElement"
                 " code=HTMLElement nav=HTMLElement section=HTMLElement main=HTMLElement"
                 " bogus=HTMLUnknownElement unknown-thing=HTMLElement my-widget=HTMLElement",
    ),
    Repro(
        name="a_permissions_policy_answers_the_features_a_document_controls",
        baseline_reference=True,
        note="The list is the reference browser's, so it is pinned to the Chrome it was read from.",
        script="""(() => {
  const policy = document.featurePolicy;
  return [Object.prototype.toString.call(policy), policy.features().length,
          policy.allowedFeatures().length, policy.allowsFeature('geolocation'),
          policy.allowsFeature('bogus'), policy.getAllowlistForFeature('geolocation').length,
          document.featurePolicy === policy, typeof PermissionsPolicy,
          document.createElement('iframe').featurePolicy.allowsFeature('camera')];
})()""",
        expected=["[object PermissionsPolicy]", 76, 76, True, False, 1, True, "function", True],
    ),
    Repro(
        name="an_attribute_style_map_reads_and_writes_the_style_attribute",
        note="The map and the declaration are two views of one attribute.",
        script="""(() => {
  const element = document.createElement('div');
  const map = element.attributeStyleMap;
  const lines = [Object.prototype.toString.call(map), map.size];
  element.style.opacity = '0.5';
  lines.push(map.size, Object.prototype.toString.call(map.get('opacity')),
             String(map.get('opacity')),
             map.get('opacity').value + '/' + map.get('opacity').unit,
             [...map.keys()].join(','));
  map.set('margin-top', '4px');
  lines.push(element.getAttribute('style'), map.size, map.has('margin-top'));
  map.delete('margin-top');
  lines.push(element.getAttribute('style'), map.size, String(map.get('margin-top')),
             map.getAll('margin-top').length);
  map.clear();
  lines.push(JSON.stringify(element.getAttribute('style')), map.size);
  return lines;
})()""",
        expected=["[object StylePropertyMap]", 0, 1, "[object CSSUnitValue]", "0.5", "0.5/number",
                  "opacity", "opacity: 0.5; margin-top: 4px;", 2, True,
                  "opacity: 0.5;", 1, "undefined", 0, '""', 0],
    ),
    Repro(
        name="an_absolutely_positioned_box_sits_where_its_offsets_put_it",
        note="The absolute box takes the padding box of the nearest positioned ancestor"
             " as its containing block, and offsetLeft measures from the same place.",
        html=(page(head="<style>#outer{position:absolute;left:10px;top:10px;width:200px;height:100px}"
                       "#box{position:absolute;left:50px;top:60px;width:40px;height:20px}</style>",
                  body="<div id=outer><div id=box></div></div>")),
        script="""(() => {
  const box = document.getElementById('box');
  const outer = document.getElementById('outer').getBoundingClientRect();
  const rect = box.getBoundingClientRect();
  return [rect.x, rect.y, outer.x, outer.y, box.offsetLeft, box.offsetTop,
          box.offsetParent === document.getElementById('outer')];
})()""",
        expected=[60, 70, 10, 10, 50, 60, True],
    ),
    Repro(
        name="a_relative_offset_and_a_left_margin_move_the_box",
        note="A relative offset slides the box and its subtree without moving the flow;"
             " a left margin moves the box inside its containing block.",
        html=(page(head="<style>body{margin:0}"
                       "#rel{position:relative;left:5px;top:7px;width:40px;height:20px}"
                       "#flow{margin-left:8px;width:40px;height:20px}</style>",
                  body="<div id=rel></div><div id=flow></div>")),
        script="""(() => {
  const relNode = document.getElementById('rel');
  const flowNode = document.getElementById('flow');
  const rel = relNode.getBoundingClientRect();
  const flow = flowNode.getBoundingClientRect();
  return [rel.x, rel.y, flow.x, flow.y, relNode.offsetLeft, flowNode.offsetLeft,
          typeof relNode.offsetLeft, typeof flowNode.offsetLeft];
})()""",
        expected=[5, 7, 8, 20, 5, 8, "number", "number"],
    ),
    Repro(
        name="a_dom_matrix_parses_and_serializes_every_form_it_takes",
        note="A sequence of six or sixteen numbers, a matrix string, another matrix; and"
             " what it writes back.",
        script="""(() => {
  const ids = m => m.toString();
  const identity = new DOMMatrix();
  return [Object.prototype.toString.call(identity), ids(identity),
          identity.is2D, identity.isIdentity,
          ids(new DOMMatrix([1, 2, 3, 4, 5, 6])),
          ids(new DOMMatrix('matrix(2, 0, 0, 2, 10, 20)')),
          ids(new DOMMatrix('matrix3d(1,0,0,0, 0,1,0,0, 0,0,1,0, 5,6,7,1)')),
          new DOMMatrix('matrix3d(1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1)').is2D,
          ids(new DOMMatrix('none')),
          Array.from(new DOMMatrix([1, 2, 3, 4, 5, 6]).toFloat32Array()).join(','),
          JSON.stringify(new DOMMatrix([1, 2, 3, 4, 5, 6]).toJSON())];
})()""",
        expected=[
            "[object DOMMatrix]", "matrix(1, 0, 0, 1, 0, 0)", True, True,
            "matrix(1, 2, 3, 4, 5, 6)", "matrix(2, 0, 0, 2, 10, 20)",
            "matrix3d(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 6, 7, 1)", False,
            "matrix(1, 0, 0, 1, 0, 0)", "1,2,0,0,3,4,0,0,0,0,1,0,5,6,0,1",
            '{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6,"m11":1,"m12":2,"m13":0,"m14":0,'
            '"m21":3,"m22":4,"m23":0,"m24":0,"m31":0,"m32":0,"m33":1,"m34":0,'
            '"m41":5,"m42":6,"m43":0,"m44":1,"is2D":true,"isIdentity":false}',
        ],
    ),
    Repro(
        name="a_dom_matrix_transforms_the_way_the_reference_does",
        note="The quarter turn is exact, and a translation keeps what a float holds.",
        script="""(() => {
  const ids = m => m.toString();
  return [ids(new DOMMatrix([1, 2, 3, 4, 5, 6]).translate(10, 20)),
          ids(new DOMMatrix([1, 0, 0, 1, 5, 6]).scale(2, 3)),
          ids(new DOMMatrix().rotate(45)), ids(new DOMMatrix().rotate(90)),
          ids(new DOMMatrix().rotateAxisAngle(0, 0, 1, 90)),
          ids(new DOMMatrix().rotateFromVector(1, 1)),
          ids(new DOMMatrix().skewX(45)),
          ids(new DOMMatrix([1, 0, 0, 1, 1, 1]).multiply(new DOMMatrix([2, 0, 0, 2, 0, 0]))),
          ids(new DOMMatrix([2, 0, 0, 4, 6, 8]).inverse()),
          ids(new DOMMatrix([2, 0, 0, 3, 4, 5]).flipX()),
          ids(new DOMMatrix().translate(0.1, 0.2))];
})()""",
        expected=[
            "matrix(1, 2, 3, 4, 75, 106)", "matrix(2, 0, 0, 3, 5, 6)",
            "matrix(0.7071067811865476, 0.7071067811865476,"
            " -0.7071067811865476, 0.7071067811865476, 0, 0)",
            "matrix(0, 1, -1, 0, 0, 0)", "matrix(0, 1, -1, 0, 0, 0)",
            "matrix(0.7071067811865476, 0.7071067811865476,"
            " -0.7071067811865476, 0.7071067811865476, 0, 0)",
            "matrix(1, 0, 0.9999999999999999, 1, 0, 0)",
            "matrix(2, 0, 0, 2, 1, 1)", "matrix(0.5, 0, 0, 0.25, -3, -2)",
            "matrix(-2, 0, 0, 3, 4, 5)",
            "matrix(1, 0, 0, 1, 0.10000000149011612, 0.20000000298023224)",
        ],
    ),
    Repro(
        name="a_dom_matrix_writes_its_members_and_stays_3d_once_it_is",
        script="""(() => {
  const ids = m => m.toString();
  const written = new DOMMatrix();
  written.a = 5;
  written.m13 = 9;
  const sticky = new DOMMatrix();
  sticky.multiplySelf(new DOMMatrix('matrix3d(1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1)'));
  const reread = new DOMMatrix();
  reread.m11 = 2;
  reread.m13 = 0;
  const point = new DOMMatrix([2, 0, 0, 3, 10, 20]).transformPoint({x: 1, y: 2});
  return [written.a, written.m11, written.is2D, ids(written),
          sticky.is2D, sticky.isIdentity, ids(sticky),
          reread.is2D, reread.isIdentity,
          [point.x, point.y, point.z, point.w].join(','),
          Object.prototype.toString.call(point),
          new DOMMatrix() instanceof DOMMatrixReadOnly,
          new DOMPoint() instanceof DOMPointReadOnly,
          [new DOMPoint().x, new DOMPoint().y, new DOMPoint().z, new DOMPoint().w].join(','),
          JSON.stringify(new DOMPoint(1, 2, 3, 4).toJSON()),
          String(new DOMPoint(1, 2).matrixTransform(new DOMMatrix([2, 0, 0, 3, 10, 20])))];
})()""",
        expected=[5, 5, False, "matrix3d(5, 0, 9, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)",
                  False, True, "matrix3d(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)",
                  True, False, "12,26,0,1", "[object DOMPoint]", True, True, "0,0,0,1",
                  '{"x":1,"y":2,"z":3,"w":4}', "[object DOMPoint]"],
    ),
    Repro(
        name="a_dom_matrix_refuses_what_the_reference_refuses",
        script="""(() => {
  const answers = [];
  for (const call of [
      () => new DOMMatrix('bogus'),
      () => new DOMMatrix([1, 2, 3]),
      () => new DOMMatrix({a: 1}),
      () => new DOMMatrix([1, 0, 0, 1, 0, 0]).setMatrixValue('bogus'),
      () => new DOMMatrix().translate(NaN, 0).toString(),
  ]) {
    try { call(); answers.push('no throw'); }
    catch (error) { answers.push(error.name + ': ' + error.message); }
  }
  return answers;
})()""",
        expected=[
            "SyntaxError: Failed to construct 'DOMMatrix': Failed to parse 'bogus'.",
            "TypeError: Failed to construct 'DOMMatrix': The sequence must contain"
            " 6 elements for a 2D matrix or 16 elements for a 3D matrix.",
            "SyntaxError: Failed to construct 'DOMMatrix': Failed to parse '[object Object]'.",
            "SyntaxError: Failed to execute 'setMatrixValue' on 'DOMMatrix':"
            " Failed to parse 'bogus'.",
            "InvalidStateError: Failed to execute 'toString' on 'DOMMatrixReadOnly':"
            " DOMMatrix cannot be serialized with NaN or Infinity values.",
        ],
    ),
    Repro(
        name="is_type_supported_answers_the_reference_browsers_codecs",
        note="Both answers are this machine's Chrome: what its build carries, codec by codec.",
        script="""(() => {
  const types = ['audio/mp4; codecs="mp4a.40.2"', 'audio/mpeg;', 'audio/webm; codecs="vorbis"',
                 'audio/ogg; codecs="vorbis"', 'audio/wav; codecs="1"', 'audio/ogg; codecs="speex"',
                 'audio/ogg; codecs="flac"', 'audio/3gpp; codecs="samr"',
                 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"', 'video/mp4; codecs="avc1.42E01E"',
                 'video/mp4; codecs="avc1.58A01E"', 'video/mp4; codecs="avc1.4D401E"',
                 'video/mp4; codecs="avc1.64001E"', 'video/mp4; codecs="mp4v.20.8"',
                 'video/mp4; codecs="mp4v.20.240"', 'video/webm; codecs="vp8"',
                 'video/ogg; codecs="theora"', 'video/ogg; codecs="dirac"',
                 'video/3gpp; codecs="mp4v.20.8"', 'video/x-matroska; codecs="theora"'];
  return [types.map(type => MediaSource.isTypeSupported(type) ? 1 : 0).join(''),
          MediaSource.isTypeSupported('video/mp4'), MediaSource.isTypeSupported('audio/mpeg'),
          MediaSource.isTypeSupported('nonsense'), MediaSource.isTypeSupported('')];
})()""",
        expected=["11100000111110010000", False, True, False, False],
    ),
    Repro(
        name="rtp_receiver_capabilities_are_the_reference_browsers",
        note="The codec tables are the reference build's, so they move when it moves.",
        script="""(() => {
  const audio = RTCRtpReceiver.getCapabilities('audio');
  const video = RTCRtpReceiver.getCapabilities('video');
  return [audio.codecs.length, video.codecs.length,
          audio.headerExtensions.length, video.headerExtensions.length,
          audio.codecs[0].mimeType, video.codecs[0].mimeType, video.codecs[18].mimeType,
          String(RTCRtpReceiver.getCapabilities('bogus')),
          JSON.stringify(audio).length, JSON.stringify(video).length];
})()""",
        expected=[8, 23, 4, 11, "audio/opus", "video/VP8", "video/H265", "null", 891, 3226],
    ),
    Repro(
        name="a_webgl_context_event_carries_the_status_message_it_was_given",
        script="""(() => {
  const event = new WebGLContextEvent('webglcontextlost', {statusMessage: 'gone', bubbles: true});
  return [Object.prototype.toString.call(event), event.type, event.statusMessage,
          event.bubbles, event instanceof Event, new WebGLContextEvent('x').statusMessage];
})()""",
        expected=["[object WebGLContextEvent]", "webglcontextlost", "gone", True, True, ""],
    ),
    Repro(
        name="a_transferred_message_port_arrives_in_the_events_ports",
        note="A transfer moves the port: the sender's reference is neutered, and the port"
             " arrives in the message event's ports, still entangled with its twin.",
        html=(page(body="""
<script>
  window.__seen = [];
  window.__neutered = false;
  const channel = new MessageChannel();
  window.__channel = channel;
  channel.port2.onmessage = () => { window.__neutered = true; };
  onmessage = event => {
    window.__seen.push([event.data, event.ports.length, event.ports[0] instanceof MessagePort]);
    const moved = event.ports[0];
    moved.onmessage = reply => window.__seen.push(['reply', reply.data]);
    moved.start();
    channel.port1.postMessage('from-port1');
  };
  postMessage('init', '*', [channel.port2]);
</script>""")),
        settle=0.5,
        script="[window.__seen, window.__neutered]",
        expected=[[["init", 1, True], ["reply", "from-port1"]], False],
    ),
    Repro(
        name="a_transfer_list_refuses_what_it_cannot_move",
        html=(page(body="""
<script>
  window.__error = '';
  try { postMessage('x', '*', [{}]); } catch (error) { window.__error = error.name + ': ' + error.message; }
</script>""")),
        script="window.__error",
        expected="DataCloneError: Failed to execute 'postMessage' on 'Window':"
                 " Value at index 0 does not have a transferable type.",
    ),
    Repro(
        name="a_port_that_was_already_moved_cannot_be_moved_again",
        html=(page(body="""
<script>
  window.__error = '';
  const channel = new MessageChannel();
  postMessage('first', '*', [channel.port2]);
  try { postMessage('second', '*', [channel.port2]); }
  catch (error) { window.__error = error.name + ': ' + error.message; }
</script>""")),
        script="window.__error",
        expected="DataCloneError: Failed to execute 'postMessage' on 'Window':"
                 " Port at index 0 is already neutered.",
    ),
    Repro(
        name="a_canvas_keeps_the_first_context_type",
        note="Whichever context a canvas answers for first fixes its type for later calls.",
        script="""(() => {
  const drawn = document.createElement('canvas');
  const three = document.createElement('canvas');
  const fresh = document.createElement('canvas');
  return [drawn.getContext('2d') !== null && drawn.getContext('webgl') === null,
          three.getContext('webgl') !== null && three.getContext('2d') === null,
          fresh.getContext('nope'), fresh.getContext('webgl') !== null,
          fresh.getContext('experimental-webgl') !== null];
})()""",
        expected=[True, True, None, True, True],
    ),
    Repro(
        name="a_canvas_answers_for_webgl2",
        note="The second context type a canvas hands out, with the version it reports.",
        script="""(() => {
  const context = document.createElement('canvas').getContext('webgl2');
  return [context !== null, Object.prototype.toString.call(context),
          context.getParameter(context.VERSION),
          context.getParameter(context.SHADING_LANGUAGE_VERSION)];
})()""",
        expected=[True, "[object WebGL2RenderingContext]",
                  "WebGL 2.0 (OpenGL ES 3.0 Chromium)",
                  "WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)"],
    ),
    Repro(
        name="every_extension_a_context_lists_is_obtainable",
        note="Every name the context lists is one `getExtension` hands back.",
        script=WEBGL_EXTENSIONS + "('webgl', true)",
        comparison_script=WEBGL_EXTENSIONS + "('webgl', false)",
        expected=[35, []],
    ),
    Repro(
        name="a_webgl2_context_lists_its_own_extensions",
        note="What WebGL 2 adds, and what it stops listing because the feature is core.",
        script=WEBGL_EXTENSIONS + "('webgl2', true)",
        comparison_script=WEBGL_EXTENSIONS + "('webgl2', false)",
        expected=[32, True, False, None],
    ),
    Repro(
        name="the_debug_renderer_names_the_implementation",
        note="The two pnames behind the extension carry the machine's GL identity.",
        script="""(() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const info = gl.getExtension('WEBGL_debug_renderer_info');
  const vendor = gl.getParameter(info.UNMASKED_VENDOR_WEBGL);
  const renderer = gl.getParameter(info.UNMASKED_RENDERER_WEBGL);
  return [typeof vendor, typeof renderer, vendor.length > 0, renderer.length > 0];
})()""",
        expected=["string", "string", True, True],
    ),
    Repro(
        name="an_extension_object_is_handed_out_once_per_context",
        note="Asking twice for the same extension answers with the object it made first.",
        script="""(() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const first = gl.getExtension('OES_vertex_array_object');
  const array = first.createVertexArrayOES();
  return [first === gl.getExtension('OES_vertex_array_object'),
          Object.prototype.toString.call(first),
          Object.prototype.toString.call(array),
          first.isVertexArrayOES(null), gl.isBuffer(null)];
})()""",
        expected=[True, "[object OESVertexArrayObject]", "[object WebGLVertexArrayObjectOES]",
                  False, False],
    ),
    Repro(
        name="binding_is_what_brings_a_vertex_array_into_being",
        note="A created array is not one yet: binding makes it, deleting unmakes it.",
        script="""(() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const extension = gl.getExtension('OES_vertex_array_object');
  const array = extension.createVertexArrayOES();
  const made = extension.isVertexArrayOES(array);
  extension.bindVertexArrayOES(array);
  const bound = extension.isVertexArrayOES(array);
  extension.deleteVertexArrayOES(array);
  return [made, bound, extension.isVertexArrayOES(array)];
})()""",
        expected=[False, True, False],
    ),
    Repro(
        name="an_extension_object_carries_its_constants",
        note="Each extension answers with a typed object of its own.",
        script="""(() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const names = gl.getSupportedExtensions();
  const tags = names.map((name) => Object.prototype.toString.call(gl.getExtension(name)));
  const instanced = gl.getExtension('ANGLE_instanced_arrays');
  return [tags.every((tag) => tag.startsWith('[object ')), tags.includes('[object Array]'),
          instanced.VERTEX_ATTRIB_ARRAY_DIVISOR_ANGLE,
          gl.getExtension('nope')];
})()""",
        expected=[True, False, 35070, None],
    ),
    Repro(
        name="losing_the_context_is_reported",
        note="`loseContext` loses the context, and the reference never hands it back.",
        script="""(() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const lose = gl.getExtension('WEBGL_lose_context');
  const before = gl.isContextLost();
  lose.loseContext();
  const lost = gl.isContextLost();
  lose.restoreContext();
  return [before, lost, gl.isContextLost()];
})()""",
        expected=[False, True, True],
    ),
    Repro(
        name="a_canvas_reports_its_drawing_buffer_format",
        note="The one attribute a WebGL canvas answers about its surface.",
        script="document.createElement('canvas').getContext('webgl').drawingBufferFormat",
        expected=32856,
    ),
    Repro(
        name="an_unknown_permission_name_rejects_the_promise",
        note="A name outside the enum rejects the promise instead of throwing.",
        html=(page(body="""<script>
            window.__permission = '';
            try {
                navigator.permissions.query({name: 'speaker'}).then(
                    () => { window.__permission = 'resolved'; },
                    (error) => { window.__permission = error.name + ': ' + error.message; });
            } catch (error) {
                window.__permission = 'threw: ' + error.name;
            }
        </script>""")),
        script="window.__permission",
        settle=0.3,
        expected="TypeError: Failed to execute 'query' on 'Permissions': Failed to read the "
                 "'name' property from 'PermissionDescriptor': The provided value 'speaker' "
                 "is not a valid enum value of type PermissionName.",
    ),
    Repro(
        name="a_permission_status_names_the_spec_permission",
        note="The status reports the permission the spec names, not always the one queried.",
        url="https://repro.test/dir/page",
        html=(page(body="""<script>
            window.__status = '';
            for (const name of ['camera', 'microphone', 'geolocation']) {
                navigator.permissions.query({name: name}).then(
                    (status) => { window.__status += status.name + ':' + status.state + ' '; },
                    (error) => { window.__status += 'rejected '; });
            }
        </script>""")),
        script="window.__status",
        settle=0.3,
        expected="video_capture:prompt audio_capture:prompt geolocation:prompt ",
    ),
    Repro(
        name="performance_now_counts_from_the_time_origin",
        note="now() is milliseconds since timeOrigin, which is this process' start.",
        script="""(() => {
  const first = performance.now();
  const second = performance.now();
  return [typeof first, first > 0, second >= first,
          Math.abs(performance.timeOrigin + first - Date.now()) < 1000];
})()""",
        expected=["number", True, True, True],
    ),
    Repro(
        name="marks_and_measures_land_in_one_buffer",
        note="A mark keeps its detail, the buffer hands the same object back twice, and "
             "both calls answer with the entry Chrome answers with.",
        script="""(() => {
  performance.clearMarks(); performance.clearMeasures();
  const made = performance.mark('a', {detail: {x: 1}});
  const measured = performance.measure('m', 'a');
  const mark = performance.getEntriesByType('mark')[0];
  return [mark.name, mark.entryType, mark.duration, mark.detail.x,
          Object.keys(mark.toJSON()).sort().join(),
          measured.name, measured.duration >= 0,
          performance.getEntriesByName('a').length,
          mark === performance.getEntriesByType('mark')[0],
          made.entryType];
})()""",
        expected=["a", "mark", 0, 1, "duration,entryType,name,navigationId,startTime",
                  "m", True, 1, True, "mark"],
    ),
    Repro(
        name="the_buffer_reports_by_start_time",
        note="getEntries() is ordered by when things happened, not by when they were "
             "recorded: a measure that started at 100 comes last.",
        script="""(() => {
  performance.clearMarks(); performance.clearMeasures();
  performance.measure('first', {start: 100, duration: 10});
  performance.mark('second', {startTime: 1});
  performance.mark('third', {startTime: 5});
  return performance.getEntries()
                      .filter(entry => entry.entryType === 'mark'
                                       || entry.entryType === 'measure')
                      .map(entry => entry.name);
})()""",
        expected=["second", "third", "first"],
    ),
    Repro(
        name="clearing_takes_one_name_at_a_time",
        note="clearMarks(name) drops the marks with that name, and only those.",
        script="""(() => {
  performance.clearMarks(); performance.clearMeasures();
  performance.mark('keep'); performance.mark('drop');
  performance.mark('keep', {startTime: 3});
  performance.clearMarks('keep');
  return performance.getEntriesByType('mark').map(entry => entry.name);
})()""",
        expected=["drop"],
    ),
    Repro(
        name="a_measure_needs_the_mark_it_names",
        note="Naming a mark that does not exist is the specification's SyntaxError.",
        script="""(() => {
  try { performance.measure('m', 'never-marked'); return 'no error'; }
  catch (error) { return [error.name, error instanceof DOMException]; }
})()""",
        expected=["SyntaxError", True],
    ),
    Repro(
        name="the_navigation_entry_carries_this_documents_milestones",
        baseline_reference=True,
        note="The document is its own resource: 48 fields, its milestones, and a duration "
             "that runs from its start to its load event.",
        script="""(() => {
  const entry = performance.getEntriesByType('navigation')[0];
  return [entry.name === location.href, entry.entryType, entry.startTime >= 0,
          entry.initiatorType, entry.type, entry.redirectCount,
          entry.domContentLoadedEventStart >= entry.startTime,
          entry.loadEventEnd >= entry.loadEventStart,
          entry.duration === entry.loadEventEnd - entry.startTime,
          Object.keys(entry.toJSON()).length];
})()""",
        expected=[True, "navigation", True, "navigation", "navigate", 0, True, True, True, 48],
    ),
    Repro(
        name="a_fetched_script_leaves_a_resource_entry",
        baseline_reference=True,
        note="The page times its own script fetch and its observer hears about it; the "
             "entry reports the 34 fields Chrome writes.",
        html=(page(head="""<script>
            window.__seen = [];
            new PerformanceObserver(list => {
              for (const entry of list.getEntries()) window.__seen.push(entry.entryType);
            }).observe({type: 'resource'});
            const url = URL.createObjectURL(new Blob(['window.__ran = 1;'],
                                                     {type: 'text/javascript'}));
            const made = document.createElement('script');
            made.src = url;
            document.head.append(made);
        </script>""")),
        script="""(() => {
  const entries = performance.getEntriesByType('resource');
  return [window.__ran, entries.length > 0,
          entries.every(entry => entry.fetchStart === entry.startTime
                                 && entry.responseEnd >= entry.responseStart
                                 && entry.duration >= 0
                                 && Object.keys(entry.toJSON()).length === 34),
          window.__seen.length > 0];
})()""",
        expected=[1, True, True, True],
    ),
    Repro(
        name="an_observer_is_handed_the_marks_it_asked_for",
        note="takeRecords() hands over what the observer has collected, and the observed "
             "type is the only one it collects.",
        script="""(() => {
  window.__marks = [];
  const observer = new PerformanceObserver(list => {
    for (const entry of list.getEntries()) window.__marks.push(entry.name);
  });
  observer.observe({type: 'mark'});
  performance.mark('one');
  performance.measure('not-a-mark', {start: 0, duration: 1});
  performance.mark('two');
  const pending = observer.takeRecords().length;
  observer.disconnect();
  return pending;
})()""",
        expected=2,
    ),
    Repro(
        name="a_buffered_observer_is_handed_what_was_measured_before",
        note="buffered: true reaches back to the entries already in the buffer.",
        html=(page(head="""<script>
            window.__buffered = [];
            performance.mark('earlier');
            new PerformanceObserver(list => {
              window.__buffered.push(list.getEntriesByType('mark').length);
            }).observe({type: 'mark', buffered: true});
        </script>""")),
        script="String(window.__buffered)",
        expected="1",
    ),
    Repro(
        name="a_thrown_script_reports_where_it_failed",
        note="The error event carries the source, the line and the column, and the value "
             "the handler catches is the page's own Error.",
        html=(page(head="""<script>
            window.__seen = [];
            addEventListener('error', event => window.__seen.push([
              event.filename === location.href, event.lineno > 0, event.colno > 0,
              event.error instanceof Error, event.error.message]));
        </script>""", body="<script>throw new Error('boom')</script>")),
        script="window.__seen",
        expected=[[True, True, True, True, "boom"]],
    ),
    Repro(
        name="a_rel_list_knows_the_link_types_it_supports",
        note="supports() answers for the link types Blink implements: a <link> set, a "
             "hyperlink three, anything else an ordinary false.",
        html=(page(body="")),
        script="""(() => {
  const link = document.createElement('link');
  const anchor = document.createElement('a');
  const form = document.createElement('form');
  const asked = (list, tokens) => tokens.map(token => list.supports(token));
  return [asked(link.relList, ['preload', 'prefetch', 'modulepreload', 'stylesheet',
                               'canonical', 'nonsense']),
          asked(anchor.relList, ['noreferrer', 'noopener', 'opener', 'preload']),
          asked(form.relList, ['noreferrer', 'preload'])];
})()""",
        expected=[[True, True, True, True, True, False],
                  [True, True, True, False],
                  [True, False]],
    ),
    Repro(
        name="an_audio_context_answers_like_chrome",
        note="Audio feature detection at a shared sample rate: a running context with its clock, "
             "an oscillator at 440 Hz, an analyser's window, and promises that settle.",
        script="""(() => {
  const context = new AudioContext({sampleRate: 48000});
  const oscillator = context.createOscillator();
  const analyser = context.createAnalyser();
  const gain = context.createGain();
  const offline = new OfflineAudioContext(1, 44100, 44100);
  return [context.state, context.sampleRate, typeof context.currentTime,
          context.destination.numberOfInputs, oscillator.type, oscillator.frequency.value,
          typeof oscillator.start, typeof oscillator.stop, analyser.fftSize,
          analyser.frequencyBinCount, gain.gain.value, typeof context.close().then,
          typeof context.resume().then, typeof context.decodeAudioData(new ArrayBuffer(0)).then,
          offline.length, offline.sampleRate, typeof offline.startRendering().then,
          context.createBuffer(2, 8, 44100).length,
          context.createBuffer(2, 8, 44100).getChannelData(0).length];
})()""",
        expected=["running", 48000, "number", 1, "sine", 440, "function", "function", 2048,
                  1024, 1, "function", "function", "function", 44100, 44100, "function", 8, 8],
    ),
    Repro(
        name="every_audio_graph_node_answers_like_chrome",
        note="What the log reported as 待实现: the nodes a page builds report Chrome's "
             "parameters, ranges and channel metadata, measured from it and pinned here.",
        html=(page(body="")),
        script="""(() => {
  const context = new OfflineAudioContext(1, 128, 8000);
  const param = (value) => [value.value, value.defaultValue, value.minValue, value.maxValue,
                            value.automationRate];
  const shape = (node) => [node.numberOfInputs, node.numberOfOutputs, node.channelCount,
                           node.channelCountMode, node.channelInterpretation,
                           Object.prototype.toString.call(node)];
  const biquad = context.createBiquadFilter();
  const delay = context.createDelay(0.5);
  const panner = context.createPanner();
  const listener = context.listener;
  return [shape(biquad), biquad.type, param(biquad.frequency), param(biquad.detune),
          param(biquad.Q), param(biquad.gain), shape(context.createChannelMerger(2)),
          shape(context.createChannelSplitter(3)), shape(context.createConstantSource()),
          param(context.createConstantSource().offset), shape(context.createConvolver()),
          context.createConvolver().normalize, shape(delay), param(delay.delayTime),
          shape(context.createIIRFilter([1, 0.5], [1, -0.2])), shape(panner),
          panner.panningModel, panner.distanceModel, panner.refDistance, panner.maxDistance,
          panner.coneInnerAngle, panner.coneOuterAngle, panner.coneOuterGain,
          param(panner.positionX), param(panner.orientationX),
          shape(context.createStereoPanner()), param(context.createStereoPanner().pan),
          shape(context.createWaveShaper()), context.createWaveShaper().curve,
          context.createWaveShaper().oversample,
          shape(context.createScriptProcessor(4096, 2, 2)),
          context.createScriptProcessor().bufferSize,
          Object.prototype.toString.call(context.createPeriodicWave([0, 1], [0, 0])),
          Object.prototype.toString.call(new PeriodicWave(context, {real: [0, 1], imag: [0, 1]})),
          [listener instanceof AudioListener, param(listener.positionX),
           param(listener.forwardZ), param(listener.upY)],
          typeof biquad.getFrequencyResponse, typeof delay.start,
          typeof context.createDelay, typeof PeriodicWave, typeof AudioListener];
})()""",
        expected=[
            [1, 1, 2, "max", "speakers", "[object BiquadFilterNode]"], "lowpass",
            [350, 350, 0, 4000, "a-rate"], [0, 0, -153600, 153600, "a-rate"],
            [1, 1, -3.4028234663852886e+38, 3.4028234663852886e+38, "a-rate"],
            [0, 0, -3.4028234663852886e+38, 1541.273681640625, "a-rate"],
            [2, 1, 1, "explicit", "speakers", "[object ChannelMergerNode]"],
            [1, 3, 3, "explicit", "discrete", "[object ChannelSplitterNode]"],
            [0, 1, 2, "max", "speakers", "[object ConstantSourceNode]"],
            [1, 1, -3.4028234663852886e+38, 3.4028234663852886e+38, "a-rate"],
            [1, 1, 2, "clamped-max", "speakers", "[object ConvolverNode]"], True,
            [1, 1, 2, "max", "speakers", "[object DelayNode]"], [0, 0, 0, 0.5, "a-rate"],
            [1, 1, 2, "max", "speakers", "[object IIRFilterNode]"],
            [1, 1, 2, "clamped-max", "speakers", "[object PannerNode]"],
            "equalpower", "inverse", 1, 10000, 360, 360, 0,
            [0, 0, -3.4028234663852886e+38, 3.4028234663852886e+38, "a-rate"],
            [1, 1, -3.4028234663852886e+38, 3.4028234663852886e+38, "a-rate"],
            [1, 1, 2, "clamped-max", "speakers", "[object StereoPannerNode]"],
            [0, 0, -1, 1, "a-rate"],
            [1, 1, 2, "max", "speakers", "[object WaveShaperNode]"], None, "none",
            [1, 1, 2, "explicit", "speakers", "[object ScriptProcessorNode]"], 256,
            "[object PeriodicWave]", "[object PeriodicWave]",
            [True, [0, 0, -3.4028234663852886e+38, 3.4028234663852886e+38, "a-rate"],
             [-1, -1, -3.4028234663852886e+38, 3.4028234663852886e+38, "a-rate"],
             [1, 1, -3.4028234663852886e+38, 3.4028234663852886e+38, "a-rate"]],
            "function", "undefined", "function", "function", "function",
        ],
    ),
    Repro(
        name="an_analyser_reports_its_data_like_chrome",
        note="Both data methods on an unfed analyser: silence is -Infinity in the "
             "frequency domain and zeros in the time domain, an array longer than the "
             "window keeps whatever it held, and another type is refused.",
        html=(page(body="")),
        script="""(() => {
  const context = new OfflineAudioContext(1, 128, 8000);
  const analyser = context.createAnalyser();
  const spectrum = new Float32Array(analyser.frequencyBinCount);
  analyser.getFloatFrequencyData(spectrum);
  const window = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(window);
  const short = new Float32Array(3);
  analyser.getFloatTimeDomainData(short);
  const long = new Float32Array(analyser.fftSize + 2);
  long.fill(7);
  analyser.getFloatTimeDomainData(long);
  const refused = (call, value) => {
    try { analyser[call](value); return 'allowed'; }
    catch (error) { return error.name; }
  };
  return [Array.from(spectrum.slice(0, 2)), Array.from(window.slice(0, 2)), analyser.fftSize,
          analyser.frequencyBinCount, analyser.minDecibels, analyser.maxDecibels,
          Array.from(short), Array.from(long.slice(-2)),
          refused('getFloatFrequencyData', [1, 2, 3]),
          refused('getFloatFrequencyData', new Float64Array(4)),
          refused('getFloatTimeDomainData', undefined),
          refused('getFloatTimeDomainData', [1]),
          typeof analyser.getFloatFrequencyData, typeof analyser.getFloatTimeDomainData];
})()""",
        expected=[[float("-inf"), float("-inf")], [0, 0], 2048, 1024, -100, -30, [0, 0, 0],
                  [7, 7], "TypeError", "TypeError", "TypeError", "TypeError",
                  "function", "function"],
    ),
    Repro(
        name="the_debug_renderer_extension_carries_its_constants",
        note="Both constants belong on the extension object's prototype, and the interface "
             "is not a global in either browser.",
        script="""(() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const info = gl.getExtension('WEBGL_debug_renderer_info');
  return [info.UNMASKED_VENDOR_WEBGL, info.UNMASKED_RENDERER_WEBGL,
          Object.prototype.toString.call(info),
          gl.getExtension('WEBGL_debug_renderer_info') === info];
})()""",
        expected=[37445, 37446, "[object WebGLDebugRendererInfo]", True],
    ),
    Repro(
        name="every_canvas_gets_the_debug_renderer_extension",
        note="A second canvas asks for the extension again; the interface is committed to "
             "the realm once, and asking again must not fail.",
        script="""(() => {
  const read = () => {
    const canvas = document.createElement('canvas');
    const info = canvas.getContext('webgl').getExtension('WEBGL_debug_renderer_info');
    return [info && info.UNMASKED_VENDOR_WEBGL, info && info.UNMASKED_RENDERER_WEBGL];
  };
  return [read(), read(), read()];
})()""",
        expected=[[37445, 37446], [37445, 37446], [37445, 37446]],
    ),
    Repro(
        name="an_extension_from_a_frame_keeps_its_constants",
        note="A frame's extension object crosses in as a class with no config here: its "
             "overlay is committed before the template is built, so the constants survive.",
        html=(page(body='<iframe name="f"></iframe>')),
        script="""(() => {
  const child = document.querySelector('iframe').contentWindow;
  const childGl = child.document.createElement('canvas').getContext('webgl');
  const handedOver = childGl.getExtension('WEBGL_debug_renderer_info');
  const gl = document.createElement('canvas').getContext('webgl');
  return [handedOver.UNMASKED_VENDOR_WEBGL, handedOver.UNMASKED_RENDERER_WEBGL,
          typeof gl.getExtension('WEBGL_debug_renderer_info'),
          typeof WebGLDebugRendererInfo, 'WebGLDebugRendererInfo' in window];
})()""",
        expected=[37445, 37446, "object", "undefined", False],
    ),
    Repro(
        name="a_file_input_has_no_capture_member",
        note="The reference browser has no capture, alpha, colorSpace or switch member on "
             "HTMLInputElement; the content attribute still round-trips.",
        script="""(() => {
  const input = document.createElement('input');
  input.setAttribute('type', 'file');
  input.setAttribute('capture', 'user');
  return [typeof input.capture, input.getAttribute('capture'),
          ['alpha', 'colorSpace', 'switch'].map((name) => typeof input[name]),
          ['capture', 'alpha'].map((name) => name in HTMLInputElement.prototype)];
})()""",
        expected=["undefined", "user", ["undefined", "undefined", "undefined"], [False, False]],
    ),
    Repro(
        name="a_document_reports_its_domain",
        note="The getter is the host, and the setter takes that host or a suffix of it.",
        url="http://repro.test/dir/page",
        script="""(() => {
  const before = document.domain;
  document.domain = 'repro.test';
  const after = document.domain;
  const refused = [];
  for (const value of ['', 'test', 'other.test']) {
    try { document.domain = value; refused.push('accepted'); }
    catch (error) { refused.push(error.name); }
  }
  return [before, after, refused];
})()""",
        expected=["repro.test", "repro.test",
                  ["SecurityError", "SecurityError", "SecurityError"]],
    ),
    Repro(
        name="a_named_attribute_read_answers_the_attribute",
        note="NamedNodeMap answers a named read with the Attr, and an absent name with "
             "nothing at all.",
        html=(page(body="<p id=app data-x=1>x</p>")),
        script="""(() => {
  const map = document.querySelector('p').attributes;
  return [String(map['data-x']), map.nope === undefined, 'data-x' in map, 'nope' in map,
          map.length];
})()""",
        expected=["[object Attr]", True, True, False, 2],
    ),
    Repro(
        name="a_performance_observer_validates_its_registration",
        note="Observe takes entryTypes or type and never both, and the supported list is "
             "the browser's own.",
        script="""(() => {
  const seen = [];
  const attempt = (fn) => { try { fn(); } catch (error) { seen.push(error.name); } };
  attempt(() => new PerformanceObserver());
  attempt(() => new PerformanceObserver(1));
  const observer = new PerformanceObserver(() => {});
  attempt(() => observer.observe());
  attempt(() => observer.observe({entryTypes: ['mark'], type: 'mark'}));
  attempt(() => observer.observe({}));
  observer.observe({entryTypes: ['mark']});
  attempt(() => observer.observe({type: 'mark', buffered: true}));
  const typed = new PerformanceObserver(() => {});
  attempt(() => typed.observe({type: 'mark', buffered: true}));
  const types = PerformanceObserver.supportedEntryTypes;
  return [seen, observer.takeRecords(), observer.disconnect(), Array.isArray(types),
          types.includes('mark'), types.includes('resource'), types.length,
          Object.prototype.toString.call(observer)];
})()""",
        expected=[["TypeError"] * 5 + ["InvalidModificationError"], [], None, True, True, True,
                  15, "[object PerformanceObserver]"],
    ),
    Repro(
        name="an_iframe_reports_the_attributes_it_was_given",
        note="Every reflecting attribute reads back the way a browser reads it, and "
             "the frame counts towards the window's frames.",
        html=(page(body='<iframe id="f" name="given" allow="camera" allowfullscreen '
                       'width="100" height="50" referrerpolicy="no-referrer" '
                       'loading="lazy"></iframe>')),
        script="""(() => {
            const frame = document.getElementById('f');
            return [frame.name, frame.allow, frame.allowFullscreen, frame.width,
                    frame.height, frame.referrerPolicy, frame.loading,
                    frame.getSVGDocument(), frame.sandbox.value,
                    frame.sandbox.supports('allow-scripts'), window.length,
                    window.frames[0] === frame.contentWindow];
        })()""",
        expected=["given", "camera", True, "100", "50", "no-referrer", "lazy", None,
                  "", True, 1, True],
    ),
    Repro(
        name="a_window_indexes_its_child_frames",
        note="The indexed slots are the child browsing contexts: they belong to the "
             "window itself and they enumerate in order.",
        html=(page(body="<iframe name=f></iframe>")),
        script="""(() => {
            const slots = [];
            for (const key in window) if (/^[0-9]+$/.test(key)) slots.push(key);
            return [window.length, typeof window[0], Object.hasOwn(window, '0'),
                    0 in window, Object.keys(window).includes('0'), slots];
        })()""",
        expected=[1, "object", True, True, True, ["0"]],
    ),
    Repro(
        name="a_frame_slot_is_neither_writable_nor_deletable",
        note="Assigning over a slot is refused: silent outside strict mode, a TypeError "
             "inside it.  Deleting one leaves the frame in place, and a slot holding "
             "nothing is not a property at all.",
        html=(page(body="<iframe name=f></iframe>")),
        script="""(() => {
            let thrown = null;
            try { (function () { 'use strict'; window[0] = 5; })(); }
            catch (error) { thrown = error.name; }
            window[7] = 'seven';
            return [thrown, typeof window[0], typeof window[7], 7 in window,
                    Reflect.deleteProperty(window, 0), Reflect.deleteProperty(window, 9)];
        })()""",
        expected=["TypeError", "object", "undefined", False, False, True],
    ),
    Repro(
        name="a_reflected_indexed_define_is_refused",
        note="A define aimed at a slot the window does not have changes nothing; the "
             "store is refused either way.",
        html=(page(body="<iframe name=f></iframe>")),
        script="Reflect.defineProperty(window, 7, {value: 1})",
        expect_error="TypeError",
        known_divergence="the native indexed definer answers a refused define with false, "
                         "so Reflect.defineProperty throws here where Chrome returns true",
    ),
    Repro(
        name="an_iframe_reads_back_the_defaults_a_browser_reports",
        note="Absent attributes read as the empty string, ``loading`` as ``auto``, and "
             "a value outside the allowed set falls back to it.",
        html=(page(body="")),
        script="""(() => {
            const frame = document.createElement('iframe');
            const before = [frame.width, frame.height, frame.loading,
                            frame.referrerPolicy, frame.sandbox.value,
                            frame.allowFullscreen, frame.name, frame.getSVGDocument()];
            frame.loading = 'bogus';
            frame.referrerPolicy = 'bogus';
            frame.width = 100;
            return before.concat([frame.width, frame.loading, frame.referrerPolicy,
                                  frame.getAttribute('width')]);
        })()""",
        expected=["", "", "auto", "", "", False, "", None, "100", "auto", "", "100"],
    ),
    Repro(
        name="a_srcdoc_frame_holds_its_own_markup",
        note="A frame whose markup travels with the element parses it as a document of "
             "its own, same-origin with the page that wrote it.",
        html=(page(body='<iframe id="f" srcdoc="&lt;p&gt;hi&lt;/p&gt;"></iframe>')),
        script="""(() => {
            const frame = document.getElementById('f');
            return [frame.contentDocument.body.textContent, frame.contentDocument.URL,
                    frame.contentDocument.readyState, frame.contentWindow.name];
        })()""",
        expected=["hi", "about:srcdoc", "complete", ""],
    ),
    Repro(
        name="a_sandboxed_frame_is_cross_origin",
        note="Without ``allow-same-origin`` the frame gets an opaque origin, so its "
             "document is hidden and its window refuses the read.",
        html=(page(body='<iframe id="f" sandbox="allow-scripts" '
                       'srcdoc="&lt;p&gt;hi&lt;/p&gt;"></iframe>')),
        script="""(() => {
            const frame = document.getElementById('f');
            let name;
            try { name = frame.contentWindow.name; }
            catch (error) { name = error.name; }
            return [frame.contentDocument, name, window.length];
        })()""",
        expected=[None, "SecurityError", 1],
    ),
    Repro(
        name="a_xpath_expression_walks_the_document",
        note="One compiled expression, evaluated against the document and walked a "
             "node at a time.",
        html=(page(body="<p>one</p>")),
        script="""
            (() => {
              const expression = document.createExpression('//html', null);
              const result = expression.evaluate(document, XPathResult.ANY_TYPE, null);
              const first = result.iterateNext();
              return [result.resultType, first.tagName, result.iterateNext(),
                      result instanceof XPathResult];
            })()
        """,
        expected=[4, "HTML", None, True],
    ),
    Repro(
        name="xpath_predicates_and_snapshots",
        note="An attribute predicate and a positional one select what a page expects, "
             "and a snapshot reports its length and hands nodes back by index.",
        html=(page(body='<p class="x">1</p><p class="x">2</p><p>3</p>')),
        script="""
            (() => {
              const snapshot = document.evaluate("//p[@class='x']", document, null,
                  XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
              const second = document.evaluate("//p[2]", document, null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE, null);
              const none = document.evaluate("//p[@class='z']", document, null,
                  XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
              return [snapshot.snapshotLength, snapshot.snapshotItem(0).textContent,
                      snapshot.snapshotItem(1).textContent, second.singleNodeValue.textContent,
                      none.snapshotLength, snapshot.snapshotItem(9)];
            })()
        """,
        expected=[2, "1", "2", "2", 0, None],
    ),
    Repro(
        name="an_xpath_function_is_outside_the_subset",
        note="Excluded: functions and the other axes are refused rather than answered "
             "with the wrong nodes.",
        html=(page(body="<p>one</p>")),
        script="""
            (() => {
              return document.evaluate("count(//p)", document, null,
                  XPathResult.NUMBER_TYPE, null).numberValue;
            })()
        """,
        expect_error="NotSupportedError",
        known_divergence="this DOM evaluates a path subset only, so count(//p) raises "
        "NotSupportedError where Chrome answers 1",
    ),
    Repro(
        name="a_lock_manager_needs_a_secure_context",
        note="No lock manager is offered outside a secure context, which is where "
             "Chrome offers one too.",
        html=(page(body="")),
        script="navigator.locks === undefined",
        expected=True,
    ),
    Repro(
        name="a_custom_element_registry_remembers_its_definitions",
        note="Defining a name makes it readable back, and an unregistered class has"
             " no name at all.",
        html=(page(body="")),
        script="""
            (() => {
              const Ctor = function () {};
              const unknown = [customElements.get('x-unknown'), customElements.getName(Ctor)];
              customElements.define('x-corpus', Ctor);
              return [unknown, customElements.get('x-corpus') === Ctor,
                      customElements.getName(Ctor),
                      typeof customElements.whenDefined('x-corpus').then];
            })()
        """,
        expected=[[None, None], True, "x-corpus", "function"],
    ),
    Repro(
        name="defining_a_name_without_a_dash_is_rejected",
        html=(page(body="")),
        script="customElements.define('nodash', function () {})",
        expect_error="SyntaxError",
    ),
    Repro(
        name="the_visual_viewport_matches_the_window_here",
        html=(page(body="")),
        script="""
            (() => {
              const view = visualViewport;
              return [view.width === innerWidth, view.height === innerHeight, view.scale,
                      view.offsetLeft, view.offsetTop, view.pageLeft, view.pageTop,
                      view === visualViewport];
            })()
        """,
        expected=[True, True, 1, 0, 0, 0, 0, True],
    ),
    Repro(
        name="create_event_makes_an_uninitialised_event",
        html=(page(body="")),
        script="""
            (() => {
              const made = ['Event', 'HTMLEvents', 'MouseEvent'].map(
                  name => document.createEvent(name).constructor.name);
              return [made, document.createEvent('Event').type,
                      document.createEvent('MouseEvent').type];
            })()
        """,
        expected=[["Event", "Event", "MouseEvent"], "", ""],
    ),
    Repro(
        name="create_event_rejects_a_type_it_does_not_know",
        html=(page(body="")),
        script="document.createEvent('Nope')",
        expect_error="NotSupportedError",
    ),
    Repro(
        name="a_form_action_falls_back_to_the_document_url",
        note="With no action attribute the submission URL is the document's own, and a"
             " declared one resolves with its fragment dropped.",
        html=(page(body='<form id=f action="/go#frag"></form>')),
        script="""
            (() => {
              const form = document.getElementById('f');
              const declared = [form.action, form.getAttribute('action')];
              form.removeAttribute('action');
              return [declared, form.action, form.getAttribute('action')];
            })()
        """,
        expected=[["http://repro.test/go#frag", "/go#frag"],
                  "http://repro.test/dir/page", None],
    ),
    Repro(
        name="a_scripts_inner_text_is_its_source",
        html=(page(body="<script>window.__seen = 1;</script>")),
        script="document.querySelector('script').innerText",
        expected="window.__seen = 1;",
    ),
    Repro(
        name="screen_avail_left_and_top_and_do_not_track",
        html=(page(body="")),
        script="[screen.availLeft, screen.availTop, navigator.doNotTrack]",
        expected=[0, 0, None],
    ),
    Repro(
        name="an_event_target_prototype_has_no_handler_attributes",
        note="Handler attributes belong to the interfaces that own them, so they do"
             " not show up on EventTarget, which a browser gives three methods.",
        html=(page(body="")),
        script="Object.getOwnPropertyNames(EventTarget.prototype)"
               ".filter(name => name.startsWith('on'))",
        expected=[],
    ),
    Repro(
        name="a_window_handler_slot_starts_empty",
        note="A handler attribute is null until a page sets one, and holds what it set.",
        html=(page(body="")),
        script="""
            (() => {
              const before = window.ononline;
              const handler = () => {};
              window.ononline = handler;
              const held = window.ononline === handler;
              window.ononline = null;
              return [before === null, held, window.ononline === null,
                      typeof window.onresize];
            })()
        """,
        expected=[True, True, True, "object"],
    ),
    Repro(
        name="a_style_added_by_script_applies",
        note="Rules a page builds at runtime are read from the tree, so the cached "
             "sheet has to be dropped when a style element arrives.",
        html=(page(body="<div id=t>t</div>")),
        script="""
            (() => {
              const style = document.createElement('style');
              style.textContent = '#t { color: rgb(4, 5, 6); }';
              document.body.appendChild(style);
              return getComputedStyle(document.getElementById('t')).color;
            })()
        """,
        expected="rgb(4, 5, 6)",
    ),
    Repro(
        name="an_undeclared_colour_is_the_initial_one",
        note="Nothing declared anywhere: the computed colour is the browser's initial "
             "black, not an empty value.",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const element = document.createElement('div');
              document.body.appendChild(element);
              return [getComputedStyle(element).color, element.style.color];
            })()
        """,
        expected=["rgb(0, 0, 0)", ""],
    ),
    Repro(
        name="every_tag_starts_from_its_own_display",
        note="The initial display is picked per tag: a span is inline where a div is"
             " block, head content is out of the flow, a table part says which part it"
             " is, and a tag this DOM does not model is inline like a custom element.",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const tags = ['span', 'div', 'img', 'input', 'select', 'button', 'head',
                            'style', 'template', 'table', 'tr', 'td', 'x-thing'];
              const out = {};
              for (const tag of tags) {
                const element = document.createElement(tag);
                document.body.appendChild(element);
                out[tag] = getComputedStyle(element).display;
              }
              return [out.span, out.div, out.img, out.input, out.select, out.button,
                      out.head, out.style, out.template, out.table, out.tr, out.td,
                      out['x-thing']];
            })()
        """,
        expected=["inline", "block", "inline", "inline-block", "inline-block",
                  "inline-block", "none", "none", "none", "table", "table-row",
                  "table-cell", "inline"],
    ),
    Repro(
        name="an_undeclared_property_reports_its_initial_value",
        note="An undeclared margin longhand exposes its computed initial value.",
        html=EMPTY_PAGE,
        script="""
            (() => {
              const element = document.createElement('div');
              document.body.appendChild(element);
              return getComputedStyle(element).marginTop;
            })()
        """,
        expected="0px",
    ),
    Repro(
        name="atob_decodes_what_it_can",
        note="Forgiving decode: whitespace goes, padding is optional, and the low bits "
             "of an unfinished quartet are dropped.",
        html=EMPTY_PAGE,
        script="""
            [atob('YQ=='), atob('YQ'), atob('YR'), atob(' YQ =='),
             Array.from(atob('/w=='), (character) => character.charCodeAt(0))]
        """,
        expected=["a", "a", "a", "a", [255]],
    ),
    Repro(
        name="atob_refuses_a_length_it_cannot_decode",
        html=EMPTY_PAGE,
        script="atob('abcde')",
        expect_error="InvalidCharacterError",
    ),
    Repro(
        name="atob_refuses_a_character_outside_the_alphabet",
        html=EMPTY_PAGE,
        script="atob('%')",
        expect_error="InvalidCharacterError",
    ),
    Repro(
        name="atob_refuses_a_code_unit_above_latin1",
        note="Chrome words this one as a range problem instead of an encoding one; both "
             "are an InvalidCharacterError.",
        html=EMPTY_PAGE,
        script="atob('\\u2028')",
        expect_error="InvalidCharacterError",
    ),
    Repro(
        name="atob_is_a_native_window_function",
        html=EMPTY_PAGE,
        script="""
            [atob.length, atob.name, String(atob), window.hasOwnProperty('atob'),
             'atob' in Window.prototype]
        """,
        expected=[1, "atob", "function atob() { [native code] }", True, False],
    ),
    Repro(
        name="btoa_encodes_latin1_with_padding",
        html=EMPTY_PAGE,
        script="[btoa(''), btoa('a'), btoa('ab'), btoa('abc'), btoa('\\u00ff'), btoa('\\u0000')]",
        expected=["", "YQ==", "YWI=", "YWJj", "/w==", "AA=="],
    ),
    Repro(
        name="btoa_refuses_a_code_unit_above_latin1",
        html=EMPTY_PAGE,
        script="btoa('\\u2028')",
        expect_error="InvalidCharacterError",
    ),
    Repro(
        name="dom_exception_stringifies_like_an_error",
        note="It extends Error, so toString and toLocaleString are Error's own, over "
             "its name and message.",
        script="""
            (() => {
              const failure = new DOMException('Service workers are not implemented here.',
                                               'NotSupportedError');
              return [
                failure.toLocaleString(), failure.toString(), String(failure), `${failure}`,
                Object.getPrototypeOf(DOMException.prototype) === Error.prototype,
                failure instanceof Error,
                DOMException.prototype.toLocaleString === Error.prototype.toLocaleString,
                new DOMException().toString(),
                new DOMException('m', '').toString(),
                new DOMException('m').toLocaleString()
              ];
            })()
        """,
        expected=["NotSupportedError: Service workers are not implemented here.",
                  "NotSupportedError: Service workers are not implemented here.",
                  "NotSupportedError: Service workers are not implemented here.",
                  "NotSupportedError: Service workers are not implemented here.",
                  True, True, True, "Error", "m", "Error: m"],
    ),
    Repro(
        name="a_latin1_string_survives_a_round_trip",
        note="Encode then decode returns the same code units, and a hand-written"
             " quartet decodes to the byte it names.",
        html=EMPTY_PAGE,
        script="""
            [Array.from(atob(btoa('k\\u00e6\\u00ff')), (character) => character.charCodeAt(0)),
             btoa(atob('6Q=='))]
        """,
        expected=[[107, 230, 255], "6Q=="],
    ),
    Repro(
        name="btoa_is_a_native_window_function",
        html=EMPTY_PAGE,
        script="""
            [btoa.length, btoa.name, String(btoa), window.hasOwnProperty('btoa'),
             'btoa' in Window.prototype]
        """,
        expected=[1, "btoa", "function btoa() { [native code] }", True, False],
    ),
    Repro(
        name="performance_memory_reports_the_heap",
        note="Chrome hands out a fresh MemoryInfo for every read, and the three numbers "
             "are bytes ordered used <= total < limit.",
        html=EMPTY_PAGE,
        script="""(() => {
            const memory = performance.memory;
            return [typeof memory.jsHeapSizeLimit, typeof memory.totalJSHeapSize,
                    typeof memory.usedJSHeapSize, memory.usedJSHeapSize > 0,
                    memory.usedJSHeapSize <= memory.totalJSHeapSize,
                    memory.totalJSHeapSize < memory.jsHeapSizeLimit,
                    performance.memory === performance.memory, Object.keys(memory).length];
        })()""",
        expected=["number", "number", "number", True, True, True, False, 0],
    ),
    Repro(
        name="the_font_face_set_and_cookie_store_are_modelled",
        note="A secure origin hands out both: the face set reports itself loaded with no "
             "faces, the cookie store an object with four promise-returning methods.",
        html=EMPTY_PAGE,
        url="http://127.0.0.1:8080/doc",
        script="""(() => {
            const faces = document.fonts;
            const store = window.cookieStore;
            return [faces.status, faces.size, faces.ready === faces.ready,
                    faces.check('16px sans-serif'), Object.prototype.toString.call(faces),
                    typeof faces[Symbol.iterator], typeof store, typeof store.get,
                    typeof store.getAll, typeof store.set, typeof store.delete,
                    store.get.length, store.set.length, store.onchange === null,
                    Object.prototype.toString.call(store)];
        })()""",
        expected=["loaded", 0, True, True, "[object FontFaceSet]", "function", "object",
                  "function", "function", "function", "function", 0, 1, True,
                  "[object CookieStore]"],
    ),
    Repro(
        name="text_encoding_round_trips_utf8",
        note="The encoder writes UTF-8 and replaces a lone surrogate; the decoder reads it "
             "back, drops a leading BOM, replaces an invalid byte and refuses a bad label.",
        html=EMPTY_PAGE,
        script="""(() => {
            const encoder = new TextEncoder();
            const decoder = new TextDecoder();
            const bytes = encoder.encode('\\u20ac\\ud800');
            let badLabel;
            try { badLabel = new TextDecoder('nonsense').encoding; }
            catch (error) { badLabel = error.name; }
            return [Array.from(bytes), encoder instanceof TextEncoder, encoder.encoding,
                    encoder.encode.length, TextEncoder.prototype.encodeInto.length,
                    decoder.decode(bytes), decoder.encoding, decoder.fatal, decoder.ignoreBOM,
                    decoder.decode(new Uint8Array([0xef, 0xbb, 0xbf, 65])),
                    decoder.decode(new Uint8Array([0xff])), badLabel];
        })()""",
        expected=[[226, 130, 172, 239, 191, 189], True, "utf-8", 0, 2, "\u20ac\ufffd",
                  "utf-8", False, False, "A", "\ufffd", "RangeError"],
    ),
    Repro(
        name="a_message_channel_hands_out_two_ports",
        note="The ports are distinct MessagePorts with the messaging surface.  Delivery is "
             "a task, so nothing has arrived by the time the script returns.",
        html=EMPTY_PAGE,
        script="""(() => {
            const channel = new MessageChannel();
            let arrived = false;
            channel.port2.onmessage = () => { arrived = true; };
            channel.port1.postMessage('hello');
            return [channel.port1 instanceof MessagePort, channel.port2 instanceof MessagePort,
                    channel.port1 === channel.port2, typeof channel.port1.postMessage,
                    typeof channel.port1.start, typeof channel.port1.close,
                    channel.port1.onmessage === null, channel.port1.postMessage.length,
                    Object.prototype.toString.call(channel), arrived];
        })()""",
        expected=[True, True, False, "function", "function", "function", True, 1,
                  "[object MessageChannel]", False],
    ),
    Repro(
        name="create_element_ns_gives_the_interface_of_its_namespace",
        note="An HTML-namespace name goes through the tag registry, an SVG name gets the "
             "interface Chrome has for it, and MathML has the one interface.",
        script="""(() => {
            const SVG = 'http://www.w3.org/2000/svg';
            const MATHML = 'http://www.w3.org/1998/Math/MathML';
            const XHTML = 'http://www.w3.org/1999/xhtml';
            const svg = document.createElementNS(SVG, 'svg');
            const math = document.createElementNS(MATHML, 'math');
            const html = document.createElementNS(XHTML, 'div');
            return [svg.constructor.name, svg.namespaceURI, svg.localName, svg.tagName,
                    svg.prefix, svg instanceof SVGElement, svg instanceof Element,
                    svg instanceof HTMLElement, svg.ownerDocument === document,
                    math.constructor.name, math.namespaceURI, math instanceof MathMLElement,
                    html.constructor.name, html.localName, html.tagName,
                    html instanceof HTMLDivElement, html.namespaceURI];
        })()""",
        expected=["SVGSVGElement", "http://www.w3.org/2000/svg", "svg", "svg", None,
                  True, True, False, True, "MathMLElement",
                  "http://www.w3.org/1998/Math/MathML", True, "HTMLDivElement", "div",
                  "DIV", True, "http://www.w3.org/1999/xhtml"],
    ),
    Repro(
        name="create_element_ns_keeps_the_name_it_was_given",
        note="A name is case-sensitive and is not lower-cased: ``clippath`` is an unknown "
             "SVG name while ``clipPath`` is not, and ``DIV`` is an unknown HTML one.  "
             "Blink takes the first two colon-separated segments and drops the rest.",
        script="""(() => {
            const SVG = 'http://www.w3.org/2000/svg';
            const XHTML = 'http://www.w3.org/1999/xhtml';
            const clip = document.createElementNS(SVG, 'clipPath');
            const tspan = document.createElementNS(SVG, 'tspan');
            const blur = document.createElementNS(SVG, 'feGaussianBlur');
            const lower = document.createElementNS(SVG, 'clippath');
            const upper = document.createElementNS(XHTML, 'DIV');
            const bare = document.createElementNS(null, 'thing');
            const surplus = document.createElementNS(SVG, 'a:b:c');
            return [clip.localName, clip.constructor.name, tspan.constructor.name,
                    blur.constructor.name, lower.constructor.name, upper.constructor.name,
                    upper.localName, upper.tagName, String(bare.namespaceURI), bare.tagName,
                    bare.constructor.name, surplus.localName, surplus.prefix,
                    surplus.tagName];
        })()""",
        expected=["clipPath", "SVGClipPathElement", "SVGTSpanElement",
                  "SVGFEGaussianBlurElement", "SVGElement", "HTMLUnknownElement", "DIV",
                  "DIV", "null", "thing", "Element", "b", "a", "a:b"],
    ),
    Repro(
        name="an_element_from_another_namespace_works_as_an_element",
        note="The element model is real whichever interface the realm bound: it holds "
             "attributes, takes children, and is found by name in the document.",
        html=(page(body="<div id=host></div>")),
        script="""(() => {
            const SVG = 'http://www.w3.org/2000/svg';
            const element = document.createElementNS(SVG, 'svg');
            const kid = document.createElementNS(SVG, 'circle');
            kid.setAttribute('r', '4');
            element.appendChild(kid);
            document.getElementById('host').appendChild(element);
            return [element.childNodes.length, kid.namespaceURI, kid.getAttribute('r'),
                    document.getElementsByTagName('svg').length, element.isConnected,
                    element.ownerDocument === document, element.nodeName,
                    typeof element.appendChild, element.getAttribute('viewBox'),
                    element.firstChild === kid];
        })()""",
        expected=[1, "http://www.w3.org/2000/svg", "4", 1, True, True, "svg", "function",
                  None, True],
    ),
    Repro(
        name="an_svg_element_leaves_its_interface_members_to_the_baseline",
        note="What an SVG element can do is its interface's business, and those "
             "interfaces are the baseline's placeholders: the element is real, its "
             "members are not implemented here -- but ``style`` is, which is where this "
             "case used to answer with the placeholder value.",
        script="""(() => {
            const SVG = 'http://www.w3.org/2000/svg';
            const element = document.createElementNS(SVG, 'svg');
            return [typeof element.getBBox, String(element.style)];
        })()""",
        expected=["function", "[object CSSStyleDeclaration]"],
    ),
    Repro(
        name="create_element_ns_refuses_a_prefix_without_a_namespace",
        note="A qualified name may only carry a prefix when there is a namespace for it.",
        script="""document.createElementNS(null, 'a:b')""",
        expect_error="NamespaceError",
    ),
    Repro(
        name="create_element_ns_refuses_a_name_it_cannot_use",
        note="The name is checked per character, and the check is Chromium's rather than "
             "XML's -- ``a=b`` is a name there, ``a b`` is not, and a prefix is checked "
             "as loosely as it is split.",
        script="""(() => {
            const XHTML = 'http://www.w3.org/1999/xhtml';
            const refused = ['a b', '1a', '', 'a:', 'a b:c', ':b'];
            const accepted = ['a=b', 'a<:b', '_a', '\\u00e4x', 'a-b'];
            return [refused.map(name => {
                try { return document.createElementNS(XHTML, name).localName; }
                catch (error) { return error.name; }
            }), accepted.map(name => document.createElementNS(XHTML, name).localName)];
        })()""",
        expected=[["InvalidCharacterError", "InvalidCharacterError",
                   "InvalidCharacterError", "InvalidCharacterError",
                   "InvalidCharacterError", "InvalidCharacterError"],
                  ["a=b", "b", "_a", "\u00e4x", "a-b"]],
    ),
    Repro(
        name="an_html_name_is_looked_up_the_way_its_interface_is_measured",
        note="Both factories agree on the rule: an HTML-namespace name is looked up as "
             "written, and whatever has no interface is an HTMLUnknownElement -- unless "
             "the name has a hyphen, which makes it a custom element name.",
        script="""(() => {
            const XHTML = 'http://www.w3.org/1999/xhtml';
            return [document.createElement('b').constructor.name,
                    document.createElement('section').constructor.name,
                    document.createElementNS(XHTML, 'a:b').constructor.name,
                    document.createElementNS(XHTML, 'p').constructor.name,
                    document.createElementNS(XHTML, 'P').constructor.name,
                    document.createElementNS(XHTML, 'my-widget').constructor.name,
                    document.createElement('div').constructor.name];
        })()""",
        expected=["HTMLElement", "HTMLElement", "HTMLElement", "HTMLParagraphElement",
                  "HTMLUnknownElement", "HTMLElement", "HTMLDivElement"],
    ),
    Repro(
        name="a_rect_reads_its_geometry_through_an_animated_length",
        note="A geometry attribute is an SVGAnimatedLength over the attribute of that "
             "name, and the SVGLength behind it is the same object on every read.",
        script="""(() => {
            const SVG = 'http://www.w3.org/2000/svg';
            const rect = document.createElementNS(SVG, 'rect');
            rect.setAttribute('width', '3868');
            rect.setAttribute('x', '12');
            const width = rect.width;
            const base = width.baseVal;
            return [width.constructor.name, width instanceof SVGAnimatedLength,
                    rect.width === width, base.constructor.name, base instanceof SVGLength,
                    base.value, base.valueAsString, base.valueInSpecifiedUnits,
                    base.unitType, width.animVal === base, width.animVal.value,
                    rect.x.baseVal.value, typeof rect.height, rect.ry.baseVal.value,
                    Object.prototype.toString.call(base), rect instanceof SVGRectElement,
                    rect instanceof SVGGeometryElement, rect instanceof SVGElement];
        })()""",
        expected=["SVGAnimatedLength", True, True, "SVGLength", True, 3868, "3868", 3868,
                  1, False, 3868, 12, "object", 0, "[object SVGLength]", True, True, True],
    ),
    Repro(
        name="a_rect_length_reports_the_unit_it_was_written_in",
        note="The number as written, the unit it was written in, and -- for a relative "
             "unit, which nothing here can resolve -- the failure Chrome reports.",
        script="""(() => {
            const SVG = 'http://www.w3.org/2000/svg';
            const rect = document.createElementNS(SVG, 'rect');
            const read = (text) => {
                if (text === null) { rect.removeAttribute('width'); }
                else { rect.setAttribute('width', text); }
                const base = rect.width.baseVal;
                let value = null, error = null;
                try { value = base.value; } catch (e) { error = e.name; }
                return [String(text), value, base.valueAsString,
                        base.valueInSpecifiedUnits, base.unitType, error];
            };
            return [['3868', '50%', '10px', '2em', 'abc', '', null].map(read)];
        })()""",
        expected=[[["3868", 3868, "3868", 3868, 1, None],
                   ["50%", None, "50%", 50, 2, "NotSupportedError"],
                   ["10px", 10, "10px", 10, 5, None],
                   ["2em", None, "2em", 2, 3, "NotSupportedError"],
                   ["abc", 0, "0", 0, 1, None],
                   ["", 0, "0", 0, 1, None],
                   ["null", 0, "0", 0, 1, None]]],
    ),
    Repro(
        name="writing_a_rect_length_rewrites_the_attribute",
        note="The three writable faces of an SVGLength all write through to the "
             "attribute, each keeping the unit the attribute had.",
        script="""(() => {
            const SVG = 'http://www.w3.org/2000/svg';
            const rect = document.createElementNS(SVG, 'rect');
            rect.setAttribute('width', '10');
            rect.width.baseVal.value = 20;
            const first = [rect.getAttribute('width'), rect.width.baseVal.valueAsString];
            rect.width.baseVal.valueAsString = '30px';
            const second = [rect.getAttribute('width'), rect.width.baseVal.unitType,
                            rect.width.baseVal.value];
            rect.width.baseVal.valueInSpecifiedUnits = 7;
            return [first, second, [rect.getAttribute('width'),
                                    rect.width.baseVal.value]];
        })()""",
        expected=[["20", "20"], ["30px", 5, 30], ["7px", 7]],
    ),
    Repro(
        name="an_xhr_timeout_is_an_unsigned_long",
        note="``timeout`` is an unsigned long: a negative value wraps, a fraction is "
             "truncated, and anything that does not convert to a number becomes zero.",
        script="""(() => {
            const write = (value) => {
                const xhr = new XMLHttpRequest();
                xhr.timeout = value;
                return [String(value), xhr.timeout];
            };
            return [0, 500, -1, 1.5, '500', Infinity, NaN, true, null, undefined,
                    4294967296].map(write);
        })()""",
        expected=[["0", 0], ["500", 500], ["-1", 4294967295], ["1.5", 1], ["500", 500],
                  ["Infinity", 0], ["NaN", 0], ["true", 1], ["null", 0],
                  ["undefined", 0], ["4294967296", 0]],
    ),
    Repro(
        name="a_location_reads_as_its_href",
        note="``toString`` is the href, with the arity and the brand Chrome gives it.",
        script="""(() => [typeof location.toString, location.toString.length, String(location),
                            location.toString(), location.toString() === location.href,
                            Object.prototype.toString.call(location)])()""",
        expected=["function", 0, "http://repro.test/dir/page", "http://repro.test/dir/page",
                  True, "[object Location]"],
    ),
    Repro(
        name="ancestor_origins_is_empty_on_a_top_level_document",
        note="A DOMStringList of the origins above this document, and the same list "
             "answers every read.  A page that is not framed has none.",
        script="""(() => {
            const list = location.ancestorOrigins;
            return [list.length, Array.from(list), list.item(0),
                    list.contains(location.origin), list === location.ancestorOrigins,
                    list.constructor.name, list instanceof DOMStringList,
                    Object.prototype.toString.call(list)];
        })()""",
        expected=[0, [], None, False, True, "DOMStringList", True, "[object DOMStringList]"],
    ),
    Repro(
        name="navigator_serial_needs_a_secure_context",
        note="Web Serial is a secure-context interface, so an ordinary http page reads "
             "nothing at all where a secure one reads the entry point.",
        script="[typeof navigator.serial, navigator.serial === undefined]",
        expected=["undefined", True],
    ),
    Repro(
        name="send_beacon_answers_whether_it_was_queued",
        note="A beacon is on its way once it is queued, so the answer is a boolean; only a "
             "scheme that cannot carry one is refused.",
        script="""(() => {
            const beacon = (url, data, hasData) => {
                try { return hasData ? navigator.sendBeacon(url, data)
                                     : navigator.sendBeacon(url); }
                catch (error) { return [error.name, error.message]; }
            };
            return [navigator.sendBeacon.length, beacon('/b1'), beacon('/b2', 'text', true),
                    beacon('/b3', {}, true), beacon('/b4', '', true),
                    beacon('javascript:0', 'x', true), beacon('data:text/plain,x', 'y', true)];
        })()""",
        expected=[1, True, True, True, True,
                  ["TypeError", "Failed to execute 'sendBeacon' on 'Navigator': Beacons are"
                                " only supported over HTTP(S)."],
                  ["TypeError", "Failed to execute 'sendBeacon' on 'Navigator': Beacons are"
                                " only supported over HTTP(S)."]],
    ),

    # ---- AbortController / AbortSignal -------------------------------------
    Repro(
        name="abort_signal_is_the_shape_chrome_describes",
        note="A signal is an EventTarget that no page may construct, and the statics are "
             "the three the interface object carries.",
        script="""(() => {
            const controller = new AbortController();
            const signal = controller.signal;
            return [AbortController.length, AbortController.prototype.abort.length,
                    AbortSignal.prototype.throwIfAborted.length,
                    [AbortSignal.abort.length, AbortSignal.any.length,
                     AbortSignal.timeout.length],
                    controller.signal === signal, signal.aborted, String(signal.reason),
                    signal instanceof AbortSignal, signal instanceof EventTarget,
                    Object.getPrototypeOf(AbortSignal.prototype) === EventTarget.prototype,
                    Object.getPrototypeOf(AbortController.prototype) === Object.prototype,
                    Object.prototype.toString.call(signal),
                    Object.prototype.toString.call(controller)];
        })()""",
        expected=[0, 0, 0, [0, 1, 1], True, False, "undefined", True, True, True, True,
                  "[object AbortSignal]", "[object AbortController]"],
    ),
    Repro(
        name="aborting_fires_one_plain_event_and_keeps_the_first_reason",
        note="The abort event neither bubbles nor cancels, and a second abort() keeps "
             "the first reason and fires nothing.",
        script="""(() => {
            const controller = new AbortController();
            const signal = controller.signal;
            const seen = [];
            signal.addEventListener('abort', (event) => seen.push([event.type, event.bubbles,
                event.cancelable, event.composed, event.target === signal]));
            controller.abort();
            controller.abort('second');
            return [seen, signal.aborted, signal.reason.name, signal.reason.message,
                    signal.reason.code, signal.reason instanceof DOMException,
                    String(signal.reason)];
        })()""",
        expected=[[["abort", False, False, False, True]], True, "AbortError",
                  "signal is aborted without reason", 20, True,
                  "AbortError: signal is aborted without reason"],
    ),
    Repro(
        name="a_reason_the_page_chose_travels_as_itself",
        note="Only a missing reason becomes Blink's DOMException: anything the page "
             "passed, a primitive included, is what reason reads back.",
        script="""(() => {
            const withReason = (value) => {
                const controller = new AbortController();
                controller.abort(value);
                return [controller.signal.aborted, controller.signal.reason === value,
                        typeof controller.signal.reason];
            };
            const omitted = new AbortController();
            omitted.abort();
            const explicit = new AbortController();
            explicit.abort(undefined);
            return [withReason('why'), withReason(5), withReason(null),
                    [omitted.signal.reason.name, omitted.signal.reason.message],
                    [explicit.signal.reason.name, explicit.signal.reason.message]];
        })()""",
        expected=[[True, True, "string"], [True, True, "number"], [True, True, "object"],
                  ["AbortError", "signal is aborted without reason"],
                  ["AbortError", "signal is aborted without reason"]],
    ),
    Repro(
        name="throw_if_aborted_throws_the_reason_and_is_silent_before",
        note="A signal nobody aborted says nothing at all; an aborted one throws its "
             "reason, which is how a page opts out of a silent cancellation.",
        script="""(() => {
            const controller = new AbortController();
            const signal = controller.signal;
            const quiet = signal.throwIfAborted();
            controller.abort();
            let caught = null;
            try { signal.throwIfAborted(); } catch (error) { caught = [error.name, error.message]; }
            return [String(quiet), caught, typeof signal.onabort];
        })()""",
        expected=["undefined", ["AbortError", "signal is aborted without reason"], "object"],
    ),
    Repro(
        name="abort_any_follows_its_members_and_keeps_their_reason",
        note="One signal for several: it is aborted as soon as any member is, carries "
             "that member's reason, and an empty list never aborts.",
        script="""(() => {
            const empty = AbortSignal.any([]);
            const first = new AbortController();
            const combined = AbortSignal.any([first.signal, new AbortController().signal]);
            let fired = 0;
            combined.onabort = () => fired++;
            first.abort('z');
            const already = new AbortController();
            already.abort('x');
            const late = AbortSignal.any([new AbortController().signal, already.signal]);
            const wait = AbortSignal.any([AbortSignal.abort(), new AbortController().signal]);
            return [empty.aborted, String(empty.reason), empty instanceof AbortSignal,
                    combined.aborted, combined.reason === first.signal.reason,
                    String(combined.reason), fired, combined.signal === undefined,
                    late.aborted, String(late.reason),
                    wait.aborted, String(wait.reason)];
        })()""",
        expected=[False, "undefined", True, True, True, "z", 1, True, True, "x", True,
                  "AbortError: signal is aborted without reason"],
    ),
    Repro(
        name="the_abort_statics_check_their_arguments_in_chromes_words",
        note="A missing argument, a value that is not iterable, an element that is not a "
             "signal, a deadline that is not a number, and one a double cannot hold "
             "exactly: five refusals, five wordings.",
        script="""(() => {
            const out = [];
            const attempt = (body) => {
                try { out.push(['no-throw', body()]); }
                catch (error) { out.push([error.name, error.message]); }
            };
            attempt(() => AbortSignal.any());
            attempt(() => AbortSignal.any(1));
            attempt(() => AbortSignal.any([1]));
            attempt(() => AbortSignal.timeout(NaN));
            attempt(() => AbortSignal.timeout(2 ** 53));
            attempt(() => AbortSignal.timeout(-1));
            return out;
        })()""",
        expected=[["TypeError", "Failed to execute 'any' on 'AbortSignal': 1 argument"
                               " required, but only 0 present."],
                  ["TypeError", "Failed to execute 'any' on 'AbortSignal': The provided"
                                " value cannot be converted to a sequence."],
                  ["TypeError", "Failed to execute 'any' on 'AbortSignal': Failed to"
                                " convert value to 'AbortSignal'."],
                  ["TypeError", "Failed to execute 'timeout' on 'AbortSignal': Value is"
                                " not of type 'unsigned long long'."],
                  ["TypeError", "Failed to execute 'timeout' on 'AbortSignal': Value is"
                                " outside the 'unsigned long long' value range."],
                  ["TypeError", "Failed to execute 'timeout' on 'AbortSignal': Value is"
                                " outside the 'unsigned long long' value range."]],
    ),
    Repro(
        name="a_listener_takes_itself_back_out_when_its_signal_fires",
        note="Registering with a signal is a lease: the listener runs until the signal "
             "fires and never again, and one that had already fired registers nothing.",
        html=(page(body="<div id=host></div>")),
        script="""(() => {
            const host = document.getElementById('host');
            const out = [];
            const live = new AbortController();
            let n = 0;
            host.addEventListener('click', () => n++, {signal: live.signal});
            host.dispatchEvent(new Event('click'));
            live.abort();
            host.dispatchEvent(new Event('click'));
            out.push(n);
            const dead = new AbortController();
            dead.abort();
            let k = 0;
            host.addEventListener('click', () => k++, {signal: dead.signal});
            host.dispatchEvent(new Event('click'));
            out.push(k);
            try { host.addEventListener('click', () => {}, {signal: 1}); }
            catch (error) { out.push([error.name, error.message]); }
            return out;
        })()""",
        expected=[1, 0,
                  ["TypeError", "Failed to execute 'addEventListener' on 'EventTarget':"
                                " Failed to read the 'signal' property from"
                                " 'AddEventListenerOptions': Failed to convert value to"
                                " 'AbortSignal'."]],
    ),
    Repro(
        name="a_request_carries_a_signal_of_its_own",
        note="Request.signal is never null and never the signal it was handed: naming one "
             "makes the request's own signal follow it, reason and all, and a request built "
             "from another request follows that one's signal the same way.",
        script="""(() => {
            const controller = new AbortController();
            const named = new Request('/a', {signal: controller.signal});
            const plain = new Request('/a');
            const before = [named.signal.aborted, named.signal === controller.signal,
                            plain.signal === null, plain.signal instanceof AbortSignal];
            controller.abort('why');
            const derived = new Request(named);
            let error = null;
            try { new Request('/a', {signal: 1}); }
            catch (failure) { error = [failure.name, failure.message]; }
            return before.concat([named.signal.aborted,
                                  named.signal.reason === controller.signal.reason,
                                  String(named.signal.reason),
                                  derived.signal === named.signal, derived.signal.aborted,
                                  error]);
        })()""",
        expected=[False, False, False, True, True, True, "why", False, True,
                  ["TypeError", "Failed to construct 'Request': Failed to read the 'signal'"
                                " property from 'RequestInit': Failed to convert value to"
                                " 'AbortSignal'."]],
    ),
    Repro(
        name="css_supports_answers_for_a_property_and_a_value",
        note="One argument asks whether the name is a property, two ask whether the pair "
             "is a declaration; a name this tree does not resolve is refused by name, and "
             "a missing argument is refused by count.",
        script="""(() => {
            const ask = (a, b) => {
                try { return b === undefined ? CSS.supports(a) : CSS.supports(a, b); }
                catch (error) { return [error.name, error.message]; }
            };
            return [ask('color'), ask('color: red'), ask('color: notacolor'),
                    ask('nonsense', 'x'), ask('display', 'flex'), ask(1, 2),
                    ask('color', 2), ask(), CSS.supports.length];
        })()""",
        expected=[False, True, True, False, True, False, True, False, 1],
        known_divergence="the value half is not parsed here: this tree checks that a"
                         " value is there, so 'color: notacolor' and a numeric value for"
                         " a colour answer true where Chrome reads the grammar and answers"
                         " false. The property half, the declaration form and the coercion"
                         " of a number or a missing argument all agree.",
    ),
    Repro(
        name="a_style_element_says_only_a_type_it_understands",
        note="The type attribute keeps whatever the page wrote, but the property reads "
             "back empty for anything that is not text/css, and an absent attribute is "
             "the same empty string.",
        html=(page(head='<style id="s">p { color: red }</style>')),
        script="""(() => {
            const style = document.getElementById('s');
            const fresh = document.createElement('style');
            const out = [style.type, fresh.type, fresh.getAttribute('type')];
            style.type = 'text/less';
            out.push([style.type, style.getAttribute('type')]);
            style.type = 'text/css';
            out.push([style.type, style.getAttribute('type')]);
            style.type = 'TEXT/CSS';
            out.push([style.type, style.getAttribute('type')]);
            style.type = '';
            out.push([style.type, style.getAttribute('type')]);
            return out;
        })()""",
        expected=["", "", None, ["text/less", "text/less"], ["text/css", "text/css"],
                  ["TEXT/CSS", "TEXT/CSS"], ["", ""]],
    ),
    Repro(
        name="the_capture_property_keeps_what_it_was_given",
        note="capture is not a reflection in Chrome: the property hands back exactly what "
             "was assigned, and the content attribute is never touched. A boolean "
             "reflection is the obvious reading of an attribute called capture, and it is "
             "wrong in both directions.",
        html=(page(body='<input id=inp type=text>')),
        script="""(() => {
            const input = document.getElementById('inp');
            const out = [input.capture, input.getAttribute('capture')];
            for (const value of [true, false, 1, 0, 'yes', null]) {
                input.capture = value;
                out.push([String(value), input.capture, input.getAttribute('capture')]);
            }
            return out;
        })()""",
        expected=[None, None, ["true", True, None], ["false", False, None],
                  ["1", 1, None], ["0", 0, None], ["yes", "yes", None],
                  ["null", None, None]],
    ),
    Repro(
        name="the_clipboard_handlers_belong_to_element",
        note="onbeforecopy, onbeforecut and onbeforepaste are Element's, not "
             "HTMLElement's, and each is a prototype slot a page reads back unchanged.",
        script="""(() => {
            const out = [];
            for (const name of ['onbeforecopy', 'onbeforecut', 'onbeforepaste']) {
                const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, name);
                const el = document.createElement('div');
                const fn = () => name;
                el[name] = fn;
                out.push([name, !!descriptor, typeof descriptor.get, el[name] === fn,
                          el.hasOwnProperty(name)]);
            }
            const el = document.createElement('div');
            let seen = 0;
            el.onbeforecopy = () => seen++;
            el.dispatchEvent(new Event('beforecopy', {bubbles: true, cancelable: true}));
            out.push(seen);
            return out;
        })()""",
        expected=[["onbeforecopy", True, "function", True, False],
                  ["onbeforecut", True, "function", True, False],
                  ["onbeforepaste", True, "function", True, False], 1],
    ),
    Repro(
        name="a_style_element_answers_with_the_sheet_it_owns",
        note="The sheet belongs to the document's list, so a style element in a document "
             "answers with the very object styleSheets holds for it, and one that is in no "
             "document answers null.",
        html=(page(head='<style id=one>p { color: red }</style>')),
        script="""(() => {
            const style = document.getElementById('one');
            const loose = document.createElement('style');
            return [style.sheet === document.styleSheets[0],
                    style.sheet === document.styleSheets.item(0),
                    style.sheet === style.sheet,
                    style.sheet.ownerNode === style,
                    Object.prototype.toString.call(style.sheet),
                    loose.sheet === null, document.styleSheets.length];
        })()""",
        expected=[True, True, True, True, "[object CSSStyleSheet]", True, 1],
    ),
    Repro(
        name="a_sheet_keeps_its_owner_and_says_it_has_no_rules_yet",
        note="What a sheet answers with is the object the list holds for its owner, and the "
             "owner is the element it came from. The rules come from the owner's text, so "
             "the sheet of a style element holding one rule reports one.",
        html=(page(head='<style id=one>p { color: red }</style>')),
        script="""(() => {
            const sheet = document.getElementById('one').sheet;
            return [sheet.ownerNode === document.getElementById('one'),
                    sheet === document.styleSheets[0],
                    Object.prototype.toString.call(sheet.cssRules),
                    sheet.cssRules.length, sheet.cssRules[0].selectorText,
                    sheet.disabled];
        })()""",
        expected=[True, True, "[object CSSRuleList]", 1, "p", False],
    ),
    Repro(
        name="the_contenteditable_attribute_says_which_keyword_it_holds",
        note="The attribute is a plain string, the property is the keyword: an empty "
             "attribute and any spelling of a keyword read back lower-cased, and anything "
             "the enum does not hold -- an unknown keyword or no attribute at all -- reads "
             "as inherit.",
        html=(page(body='<div id=host><span id=kid></span></div>')),
        script="""(() => {
            const host = document.getElementById('host');
            const out = [[host.contentEditable, String(host.getAttribute('contenteditable')),
                          host.isContentEditable]];
            for (const value of ['', 'true', 'TRUE', 'True', 'false', 'FALSE', 'inherit',
                                 'INHERIT', 'plaintext-only', 'PLAINTEXT-ONLY', 'bogus',
                                 ' true ', 'yes']) {
                host.setAttribute('contenteditable', value);
                out.push([value, host.contentEditable, host.isContentEditable]);
            }
            return out;
        })()""",
        expected=[["inherit", "null", False],
                  ["", "true", True], ["true", "true", True], ["TRUE", "true", True],
                  ["True", "true", True], ["false", "false", False],
                  ["FALSE", "false", False], ["inherit", "inherit", False],
                  ["INHERIT", "inherit", False],
                  ["plaintext-only", "plaintext-only", True],
                  ["PLAINTEXT-ONLY", "plaintext-only", True],
                  ["bogus", "inherit", False], [" true ", "inherit", False],
                  ["yes", "inherit", False]],
    ),
    Repro(
        name="the_contenteditable_setter_holds_four_keywords_and_refuses_the_rest",
        note="The IDL enum is enforced, so anything but the four keywords is a "
             "SyntaxError; what is accepted is stored in lower case, and inherit takes the "
             "attribute away rather than writing it.",
        script="""(() => {
            const values = ['true', 'TRUE', 'false', 'inherit', 'plaintext-only', '',
                            null, 5, undefined, {}, 'bogus', ' true '];
            return values.map((v) => {
                const el = document.createElement('div');
                el.setAttribute('contenteditable', 'sentinel');
                let out = null;
                try {
                    el.contentEditable = v;
                    out = [el.getAttribute('contenteditable'), el.contentEditable,
                           el.hasAttribute('contenteditable')];
                } catch (e) { out = [e.name, e.message, e.code]; }
                return [String(v), out];
            });
        })()""",
        expected=[["true", ["true", "true", True]],
                  ["TRUE", ["true", "true", True]],
                  ["false", ["false", "false", True]],
                  ["inherit", [None, "inherit", False]],
                  ["plaintext-only", ["plaintext-only", "plaintext-only", True]],
                  ["", ["SyntaxError", "Failed to set the 'contentEditable' property on"
                       " 'HTMLElement': The value provided ('') is not one of 'true',"
                       " 'false', 'plaintext-only', or 'inherit'.", 12]],
                  ["null", ["SyntaxError", "Failed to set the 'contentEditable' property"
                       " on 'HTMLElement': The value provided ('null') is not one of"
                       " 'true', 'false', 'plaintext-only', or 'inherit'.", 12]],
                  ["5", ["SyntaxError", "Failed to set the 'contentEditable' property on"
                        " 'HTMLElement': The value provided ('5') is not one of 'true',"
                        " 'false', 'plaintext-only', or 'inherit'.", 12]],
                  ["undefined", ["SyntaxError", "Failed to set the 'contentEditable'"
                       " property on 'HTMLElement': The value provided ('undefined') is"
                       " not one of 'true', 'false', 'plaintext-only', or 'inherit'.", 12]],
                  ["[object Object]", ["SyntaxError", "Failed to set the 'contentEditable'"
                       " property on 'HTMLElement': The value provided ('[object Object]')"
                       " is not one of 'true', 'false', 'plaintext-only', or 'inherit'.",
                       12]],
                  ["bogus", ["SyntaxError", "Failed to set the 'contentEditable' property"
                       " on 'HTMLElement': The value provided ('bogus') is not one of"
                       " 'true', 'false', 'plaintext-only', or 'inherit'.", 12]],
                  [" true ", ["SyntaxError", "Failed to set the 'contentEditable' property"
                       " on 'HTMLElement': The value provided (' true ') is not one of"
                       " 'true', 'false', 'plaintext-only', or 'inherit'.", 12]]],
    ),
    Repro(
        name="is_content_editable_walks_up_to_the_nearest_ancestor",
        note="Inheritance runs up the tree, an explicit false stops it, and inherit hands "
             "the question back to the ancestor above. A detached tree counts too, which is "
             "why a loose element still answers.",
        html=(page(body='<div id=host><span id=kid></span></div>')),
        script="""(() => {
            const host = document.getElementById('host');
            const kid = document.getElementById('kid');
            host.contentEditable = 'true';
            const inherited = kid.isContentEditable;
            kid.contentEditable = 'false';
            const blocked = [kid.isContentEditable, kid.contentEditable];
            kid.contentEditable = 'inherit';
            const back = [kid.isContentEditable, kid.contentEditable];
            const loose = document.createElement('div');
            loose.contentEditable = 'true';
            const below = document.createElement('span');
            loose.appendChild(below);
            return [inherited, blocked, back, loose.isContentEditable, below.isContentEditable];
        })()""",
        expected=[True, [False, "false"], [True, "inherit"], True, True],
    ),
    Repro(
        name="design_mode_is_on_or_off_and_ignores_anything_else",
        note="Chrome keeps the mode it had rather than store a value it does not know, and "
             "never throws. It is a property of a document, so it reaches the elements in "
             "it: an element the page holds loose needs its own contenteditable. The "
             "contenteditable property itself is untouched by it.",
        html=(page(body='<div id=host><span id=kid></span></div>')),
        script="""(() => {
            const doc = document;
            const start = doc.designMode;
            const seen = [];
            for (const value of ['on', 'ON', 'On', 'bogus', 'off', 'OFF', '']) {
                doc.designMode = value;
                const loose = doc.createElement('div');
                seen.push([value, doc.designMode, loose.isContentEditable,
                           loose.contentEditable]);
            }
            doc.designMode = start;
            const attached = doc.createElement('div');
            doc.body.appendChild(attached);
            const below = doc.createElement('span');
            attached.appendChild(below);
            const before = [attached.isContentEditable, below.isContentEditable];
            doc.designMode = 'on';
            const on = [attached.isContentEditable, below.isContentEditable,
                        attached.contentEditable, doc.body.isContentEditable];
            doc.designMode = 'off';
            const off = [attached.isContentEditable];
            for (const value of [null, 5, true, {}]) {
                try { doc.designMode = value; seen.push([String(value), doc.designMode]); }
                catch (e) { seen.push([String(value), e.name]); }
            }
            doc.designMode = start;
            return [start, seen, before, on, off];
        })()""",
        expected=["off",
                  [["on", "on", False, "inherit"], ["ON", "on", False, "inherit"],
                   ["On", "on", False, "inherit"], ["bogus", "on", False, "inherit"],
                   ["off", "off", False, "inherit"], ["OFF", "off", False, "inherit"],
                   ["", "off", False, "inherit"],
                   ["null", "off"], ["5", "off"], ["true", "off"],
                   ["[object Object]", "off"]],
                  [False, False], [True, True, "inherit", True], [False]],
    ),
    Repro(
        name="the_editable_pair_belongs_to_html_elements_only",
        note="Both are HTMLElement's, so an SVG element has neither, and a page cannot see "
             "them as own properties of its element.",
        script="""(() => {
            const spec = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'contentEditable');
            const second = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'isContentEditable');
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            const el = document.createElement('div');
            return [!!spec.get, !!spec.set, !!second.get, !!second.set,
                    el.hasOwnProperty('contentEditable'),
                    Object.getOwnPropertyDescriptor(Element.prototype, 'contentEditable')
                        ? 'on-Element' : 'absent',
                    svg.contentEditable === undefined,
                    svg.isContentEditable === undefined];
        })()""",
        expected=[True, True, True, False, False, "absent", True, True],
    ),
    Repro(
        name="a_sheet_answers_with_the_rules_it_holds",
        note="cssRules is a live CSSRuleList: a style rule answers its type, its "
             "selector, its serialised text and its style, and a @media rule answers a "
             "medium and the rules nested in it. The text is Blink's own -- a style rule "
             "on one line, a grouping rule over several with its children indented two "
             "spaces, and a shorthand expanded the way the declaration block expands it.",
        html=(page(head="<style id=s>p { color: red } .a, .b { color: blue }"
                       "@media screen { div { top: 1px } }</style>")),
        script="""(() => {
            const sheet = document.getElementById('s').sheet;
            const rules = sheet.cssRules;
            return [rules.length, Object.prototype.toString.call(rules),
                    typeof rules.item, rules[0] === rules.item(0),
                    [rules.item(99), rules[99]],
                    [rules[0].type, rules[0].selectorText, rules[0].cssText,
                     rules[0].parentRule, rules[0].parentStyleSheet === sheet,
                     Object.prototype.toString.call(rules[0])],
                    [rules[1].selectorText, rules[1].cssText, rules[1].style.length,
                     rules[1].style[0], rules[1].style.getPropertyValue('color'),
                     rules[1].style.parentRule === rules[1]],
                    [rules[2].type, rules[2].cssText, rules[2].selectorText,
                     rules[2].media.mediaText, rules[2].cssRules.length,
                     Object.prototype.toString.call(rules[2]),
                     rules[2].cssRules[0].selectorText]];
        })()""",
        expected=[3, "[object CSSRuleList]", "function", True, [None, None],
                  [1, "p", "p { color: red; }", None, True, "[object CSSStyleRule]"],
                  [".a, .b", ".a, .b { color: blue; }", 1, "color", "blue", True],
                  [4, "@media screen {\n  div { top: 1px; }\n}", None, "screen", 1,
                   "[object CSSMediaRule]", "div"]],
    ),
    Repro(
        name="a_rule_style_keeps_a_shorthand_and_a_bare_zero_as_written",
        note="A rule's style is a declaration block read from the sheet's text.",
        html=(page(head="<style id=s>p { margin: 0 } div { top: 0 }</style>")),
        script="""(() => {
            const sheet = document.getElementById('s').sheet;
            const style = sheet.cssRules[0].style;
            return [[style.length, style[0], style.getPropertyValue('margin')],
                    sheet.cssRules[0].cssText, sheet.cssRules[1].cssText];
        })()""",
        expected=[[1, "margin", "0"], "p { margin: 0; }", "div { top: 0; }"],
        known_divergence="a declaration block is kept as written here: the shorthand is not"
                         " expanded into its longhands and a bare zero keeps no unit, so a"
                         " rule's style reports one declaration where Chrome reports four"
                         " and the text reads 0 where Chrome reads 0px. The same gap applies"
                         " to an element's inline style, which is why it is written down"
                         " rather than fixed in the rule layer alone.",
    ),
    Repro(
        name="a_sheet_is_edited_in_place_by_insert_and_delete",
        note="insertRule puts the rule at the index it was given and answers that index, "
             "and deleteRule takes one away; a string that is not a rule is a "
             "SyntaxError.",
        html=(page(head="<style id=s>p { color: red }</style>")),
        script="""(() => {
            const sheet = document.getElementById('s').sheet;
            const out = [sheet.cssRules.length];
            const at = sheet.insertRule('h1 { color: blue }', 0);
            out.push([at, sheet.cssRules.length, sheet.cssRules[0].selectorText,
                      sheet.cssRules[1].selectorText]);
            sheet.deleteRule(0);
            out.push([sheet.cssRules.length, sheet.cssRules[0].selectorText]);
            try { sheet.insertRule('not a rule'); out.push('no-throw'); }
            catch (error) { out.push([error.name, error.code, error.message]); }
            return out;
        })()""",
        expected=[1, [0, 2, "h1", "p"], [1, "p"],
                  ["SyntaxError", 12, "Failed to execute 'insertRule' on"
                   " 'CSSStyleSheet': Failed to parse the rule 'not a rule'."]],
    ),
    Repro(
        name="the_legacy_frame_and_image_members_answer_what_chrome_answers",
        note="The frame's legacy members are string reflections whose attribute names are "
             "one word -- frameborder, marginheight, marginwidth -- while longDesc is a "
             "URL and the four modern ones are boolean flags that a false takes away. "
             "complete is true for an image with nothing to load.",
        script="""(() => {
            const out = [];
            const frame = document.createElement('iframe');
            out.push([frame.align, frame.scrolling, frame.frameBorder, frame.longDesc,
                      frame.marginHeight, frame.marginWidth, frame.csp,
                      frame.privateToken]);
            frame.align = 'left';
            frame.scrolling = 'no';
            frame.frameBorder = '0';
            frame.longDesc = 'http://example.test/page.html';
            frame.marginHeight = '4';
            frame.marginWidth = '5';
            frame.csp = 'default-src none';
            frame.privateToken = 'token';
            out.push([frame.getAttribute('align'), frame.getAttribute('scrolling'),
                      frame.getAttribute('frameborder'), frame.getAttribute('longdesc'),
                      frame.getAttribute('marginheight'), frame.getAttribute('marginwidth'),
                      frame.getAttribute('csp'), frame.getAttribute('privatetoken')]);
            out.push([frame.align, frame.longDesc, frame.marginWidth]);
            out.push([frame.credentialless, frame.allowPaymentRequest,
                      frame.browsingTopics, frame.adAuctionHeaders]);
            frame.credentialless = true;
            frame.allowPaymentRequest = 'x';
            frame.browsingTopics = true;
            frame.adAuctionHeaders = 0;
            out.push([frame.credentialless, frame.allowPaymentRequest,
                      frame.browsingTopics, frame.adAuctionHeaders,
                      frame.hasAttribute('adauctionheaders'),
                      frame.getAttributeNames().slice().sort()]);
            const image = document.createElement('img');
            out.push([image.complete]);
            const empty = document.createElement('img');
            empty.src = '';
            out.push([empty.complete]);
            out.push([Notification.maxActions]);
            const face = new FontFace('x', 'url(y)');
            out.push([face.ascentOverride]);
            face.ascentOverride = '90%';
            out.push([face.ascentOverride]);
            out.push([document.webkitFullscreenElement === null,
                      document.webkitIsFullScreen]);
            return out;
        })()""",
        expected=[["", "", "", "", "", "", "", None],
                  ["left", "no", "0", "http://example.test/page.html", "4", "5",
                   "default-src none", None],
                  ["left", "http://example.test/page.html", "5"],
                  [False, False, None, None],
                  [True, True, None, None, False,
                   ["align", "allowpaymentrequest", "credentialless", "csp", "frameborder",
                    "longdesc", "marginheight", "marginwidth", "scrolling"]],
                  [True], [True], [2], ["normal"], ["90%"], [True, False]],
        known_divergence="privateToken, browsingTopics and adAuctionHeaders exist only in"
                         " a secure context, so on this insecure page Chrome leaves them"
                         " off the element and a page that assigns to one lands the value"
                         " on an ordinary property of its own, which reads back as what"
                         " was written. Here the members are declared and the accessors"
                         " answer undefined and drop the assignment, so the read is the"
                         " same and the write is not.",
    ),
    Repro(
        name="the_small_element_members_answer_what_chrome_answers",
        note="The string members reflect a content attribute, part is a token list, and "
             "the geometry members answer as they do for an element nothing has laid out "
             "-- which is what this tree models: it lays out widths and heights, not the "
             "border and scroll offsets, so a rendered element's clientTop and scrollTop "
             "stay 0 here where Chrome reports the border and what was scrolled.",
        script="""(() => {
            const el = document.createElement('div');
            const out = [];
            out.push([el.slot, el.getAttribute('slot')]);
            el.slot = 'alpha';
            out.push([el.slot, el.getAttribute('slot')]);
            out.push([Object.prototype.toString.call(el.part), el.part.length,
                      String(el.part.value)]);
            el.part = 'one two';
            out.push([el.getAttribute('part'), el.part.length, el.part[0], el.part[1],
                      String(el.part)]);
            out.push([el.assignedSlot === null, el.elementTiming,
                      el.getAttribute('elementtiming')]);
            el.elementTiming = 'mark';
            out.push([el.elementTiming, el.getAttribute('elementtiming')]);
            out.push([el.scrollTop, el.scrollLeft, el.clientTop, el.clientLeft,
                      el.clientWidth, el.clientHeight, el.scrollWidth, el.scrollHeight,
                      el.currentCSSZoom]);
            el.scrollTop = 50;
            el.scrollLeft = 50;
            out.push([el.scrollTop, el.scrollLeft]);
            out.push([el.activeViewTransition === null,
                      el.customElementRegistry === window.customElements,
                      Object.prototype.toString.call(el.customElementRegistry)]);
            out.push(['onfullscreenchange', 'onfullscreenerror',
                      'onwebkitfullscreenchange', 'onwebkitfullscreenerror'].map(
                (name) => typeof Object.getOwnPropertyDescriptor(Element.prototype,
                                                                  name).get));
            return out;
        })()""",
        expected=[["", None], ["alpha", "alpha"],
                  ["[object DOMTokenList]", 0, ""],
                  ["one two", 2, "one", "two", "one two"],
                  [True, "", None], ["mark", "mark"],
                  [0, 0, 0, 0, 0, 0, 0, 0, 1], [0, 0],
                  [True, True, "[object CustomElementRegistry]"],
                  ["function", "function", "function", "function"]],
    ),
    Repro(
        name="the_aria_attributes_reflect_content_attributes",
        note="Each ARIA property is the content attribute of the same name, and the "
             "names are not the dashed form of the property: ariaAutoComplete is "
             "aria-autocomplete and ariaColCount is aria-colcount. A value is written as "
             "it is given -- nothing is checked against a keyword list -- and nullish "
             "takes the attribute away.",
        html=(page(body='<div id=host></div>')),
        script="""(() => {
            const host = document.getElementById('host');
            const names = ['ariaAtomic', 'ariaAutoComplete', 'ariaColCount', 'ariaKeyShortcuts',
                           'ariaLabel', 'ariaMultiLine', 'ariaPosInSet', 'ariaReadOnly',
                           'ariaValueNow', 'ariaBrailleLabel'];
            const attributes = ['aria-atomic', 'aria-autocomplete', 'aria-colcount',
                                'aria-keyshortcuts', 'aria-label', 'aria-multiline',
                                'aria-posinset', 'aria-readonly', 'aria-valuenow',
                                'aria-braillelabel'];
            const out = [];
            for (let index = 0; index < names.length; index += 1) {
                const name = names[index], attribute = attributes[index];
                out.push([host[name] === null, host.getAttribute(attribute)]);
                host.setAttribute(attribute, 'written');
                out.push([host[name], host.getAttribute(attribute)]);
                host[name] = 'through the property';
                out.push([host[name], host.getAttribute(attribute)]);
                host[name] = null;
                out.push([host[name], host.getAttribute(attribute),
                          host.hasAttribute(attribute)]);
            }
            return out;
        })()""",
        expected=[[True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False],
                  [True, None], ["written", "written"], ["through the property",
                     "through the property"], [None, None, False]],
    ),
    Repro(
        name="the_aria_idref_members_answer_with_the_elements_they_name",
        note="Nine members answer with elements rather than with the string. Absent is "
             "null, an id that names nothing contributes nothing, an empty attribute gives "
             "an empty list, and a tree outside a document resolves nothing at all.",
        html=(page(body='<div id=host><span id=one></span><span id=two></span></div>')),
        script="""(() => {
            const host = document.getElementById('host');
            const ids = (list) => list.map((e) => (e === null ? 'null' : '#' + e.id));
            const out = [];
            out.push([host.ariaLabelledByElements === null,
                      host.ariaActiveDescendantElement === null]);
            host.setAttribute('aria-labelledby', 'one two absent');
            host.setAttribute('aria-activedescendant', 'two');
            out.push([ids(host.ariaLabelledByElements),
                      host.ariaActiveDescendantElement.id]);
            host.setAttribute('aria-labelledby', '');
            out.push([ids(host.ariaLabelledByElements), host.ariaLabelledByElements.length]);
            const loose = document.createElement('div');
            loose.setAttribute('aria-controls', 'one');
            out.push([ids(loose.ariaControlsElements), loose.ariaControlsElements.length]);
            host.removeAttribute('aria-labelledby');
            host.removeAttribute('aria-activedescendant');
            out.push([host.ariaLabelledByElements === null,
                      host.ariaActiveDescendantElement === null]);
            return out;
        })()""",
        expected=[[True, True], [["#one", "#two"], "two"], [[], 0], [[], 0], [True, True]],
    ),
    Repro(
        name="a_plain_reflection_keeps_the_attribute_name_chrome_writes",
        note="Two of them are here because the attribute is not the dashed form of the "
             "property: accessKey is accesskey. A value is kept as written, and a "
             "nullish one is stringified rather than removing the attribute, so lang "
             "ends up reading as the string null with the attribute still there. "
             "focusGroup is here too, and is a sequence rather than a string.",
        script="""(() => {
            const el = document.createElement('div');
            const out = [];
            for (const [name, attribute] of [['lang', 'lang'], ['accessKey', 'accesskey']]) {
                out.push([name, el[name], el.hasAttribute(attribute)]);
                el[name] = 'written';
                out.push([name, el[name], el.getAttribute(attribute)]);
                el[name] = null;
                out.push([name, el[name], el.hasAttribute(attribute)]);
            }
            out.push([Array.from(el.focusGroup), el.hasAttribute('focusgroup')]);
            el.setAttribute('focusgroup', 'one two');
            out.push([Array.from(el.focusGroup), el.getAttribute('focusgroup')]);
            el.focusGroup = null;
            out.push([Array.from(el.focusGroup), el.hasAttribute('focusgroup')]);
            return out;
        })()""",
        expected=[["lang", "", False], ["lang", "written", "written"],
                  ["lang", "null", True],
                  ["accessKey", "", False], ["accessKey", "written", "written"],
                  ["accessKey", "null", True],
                  [[], False], [["one", "two"], "one two"], [["null"], True]],
    ),
    Repro(
        name="a_keyword_attribute_is_written_through_and_read_back_validated",
        note="The setter keeps whatever it is given, the attribute included, and the "
             "getter answers from the words it knows: a value outside the set reads as "
             "the fallback rather than as what was written. An absent attribute reads as "
             "the default, which for popover is nothing at all.",
        script="""(() => {
            const el = document.createElement('div');
            const out = [];
            for (const name of ['dir', 'autocapitalize', 'enterKeyHint', 'inputMode',
                                'virtualKeyboardPolicy', 'popover']) {
                out.push([name, el[name] === null ? 'null' : el[name]]);
                el[name] = 'not a word';
                out.push([name, el[name], el.getAttribute(name.toLowerCase())]);
                el[name] = null;
                out.push([name, el[name] === null ? 'null' : el[name],
                          el.hasAttribute(name.toLowerCase())]);
            }
            const valid = document.createElement('div');
            valid.dir = 'RTL';
            valid.inputMode = 'tel';
            valid.popover = 'hint';
            valid.autocapitalize = 'WORDS';
            out.push([valid.dir, valid.inputMode, valid.popover, valid.autocapitalize,
                      valid.getAttribute('dir'), valid.getAttribute('autocapitalize')]);
            return out;
        })()""",
        expected=[["dir", ""], ["dir", "", "not a word"], ["dir", "", True],
                  ["autocapitalize", ""], ["autocapitalize", "sentences", "not a word"],
                  ["autocapitalize", "sentences", True],
                  ["enterKeyHint", ""], ["enterKeyHint", "", "not a word"],
                  ["enterKeyHint", "", True],
                  ["inputMode", ""], ["inputMode", "", "not a word"], ["inputMode", "", True],
                  ["virtualKeyboardPolicy", ""], ["virtualKeyboardPolicy", "", "not a word"],
                  ["virtualKeyboardPolicy", "", True],
                  ["popover", "null"], ["popover", "manual", "not a word"],
                  ["popover", "null", False],
                  ["rtl", "tel", "hint", "words", "RTL", "WORDS"]],
    ),
    Repro(
        name="a_boolean_attribute_spells_itself_and_answers_from_a_default",
        note="These five are booleans that write a word rather than a bare attribute: "
             "translate writes yes or no, autocorrect on or off, and a nullish value is "
             "the false end rather than a removal. An absent attribute reads as the "
             "default, which is true for three of them and false for draggable.",
        script="""(() => {
            const el = document.createElement('div');
            const out = [];
            for (const name of ['translate', 'autocorrect', 'spellcheck', 'draggable',
                                'writingSuggestions']) {
                out.push([name, el[name], el.hasAttribute(name.toLowerCase())]);
                el[name] = true;
                out.push([name, el[name], el.getAttribute(name.toLowerCase())]);
                el[name] = false;
                out.push([name, el[name], el.getAttribute(name.toLowerCase())]);
                el[name] = null;
                out.push([name, el[name], el.getAttribute(name.toLowerCase())]);
            }
            const zero = document.createElement('div');
            zero.draggable = 0;
            zero.spellcheck = 1;
            out.push([zero.draggable, zero.getAttribute('draggable'), zero.spellcheck,
                      zero.getAttribute('spellcheck')]);
            return out;
        })()""",
        expected=[["translate", True, False], ["translate", True, "yes"],
                  ["translate", False, "no"], ["translate", False, "no"],
                  ["autocorrect", True, False], ["autocorrect", True, "on"],
                  ["autocorrect", False, "off"], ["autocorrect", False, "off"],
                  ["spellcheck", True, False], ["spellcheck", True, "true"],
                  ["spellcheck", False, "false"], ["spellcheck", False, "false"],
                  ["draggable", False, False], ["draggable", True, "true"],
                  ["draggable", False, "false"], ["draggable", False, "false"],
                  ["writingSuggestions", "true", False],
                  ["writingSuggestions", "true", "true"],
                  ["writingSuggestions", "false", "false"],
                  ["writingSuggestions", "true", "null"],
                  [False, "false", True, "true"]],
    ),
    Repro(
        name="the_boolean_flags_read_as_whether_the_attribute_is_there",
        note="autofocus, inert and focusGroupStart are booleans in the plain sense: any "
             "value at all makes them true, and false takes the attribute away rather "
             "than writing false.",
        script="""(() => {
            const el = document.createElement('div');
            const out = [];
            for (const [name, attribute] of [['autofocus', 'autofocus'],
                                             ['inert', 'inert'],
                                             ['focusGroupStart', 'focusgroupstart']]) {
                out.push([name, el[name], el.hasAttribute(attribute)]);
                el[name] = 'anything';
                out.push([name, el[name], el.getAttribute(attribute)]);
                el[name] = false;
                out.push([name, el[name], el.hasAttribute(attribute)]);
                el[name] = true;
                el[name] = null;
                out.push([name, el[name], el.hasAttribute(attribute)]);
            }
            return out;
        })()""",
        expected=[["autofocus", False, False], ["autofocus", True, ""],
                  ["autofocus", False, False], ["autofocus", False, False],
                  ["inert", False, False], ["inert", True, ""],
                  ["inert", False, False], ["inert", False, False],
                  ["focusGroupStart", False, False], ["focusGroupStart", True, ""],
                  ["focusGroupStart", False, False], ["focusGroupStart", False, False]],
    ),
    Repro(
        name="outer_text_reads_like_inner_text_and_writes_like_a_replacement",
        note="Reading outerText gives the same text innerText does, and writing it takes "
             "the element out and leaves the text behind, which is what assigning to "
             "outerText does in a browser. editContext is null here and takes what it is "
             "given without keeping it.",
        script="""(() => {
            const el = document.createElement('div');
            el.textContent = 'body text';
            document.body.appendChild(el);
            const read = el.outerText;
            el.outerText = 'replaced';
            const out = [[read, document.body.contains(el), document.body.textContent],
                         [el.editContext === null]];
            try { el.editContext = 'ignored'; out.push('no-throw'); }
            catch (error) { out.push([error.name, error.message]); }
            out.push([el.editContext === null, el.hasAttribute('editcontext')]);
            return out;
        })()""",
        expected=[["body text", False, "replaced"], [True],
                  ["TypeError", "Failed to set the 'editContext' property on"
                   " 'HTMLElement': Failed to convert value to 'EditContext'."],
                  [True, False]],
    ),
    Repro(
        name="inner_text_is_the_rendered_text_not_the_markup",
        note="innerText is not textContent: whitespace collapses, a block boundary and a "
             "br become newlines, and the ends are trimmed. The setter takes the string as "
             "written -- markup in it is not parsed, a newline becomes a br -- and null "
             "clears the element.",
        html=(page(body="<div id=host>one<span>two</span><div>three</div></div>")),
        script="""(() => {
            const host = document.getElementById('host');
            const keep = document.createElement('pre');
            keep.appendChild(document.createTextNode('  keep  '));
            const out = [host.innerText, keep.innerText];
            const loose = document.createElement('div');
            loose.innerHTML = '<div>a</div><div>b</div>';
            out.push(loose.innerText);
            const br = document.createElement('div');
            br.innerHTML = 'x<br>y';
            out.push([br.innerText, host.textContent]);
            const el = document.createElement('div');
            el.innerText = '<b>markup</b>';
            out.push([el.innerHTML, el.childNodes.length]);
            el.innerText = 'a' + String.fromCharCode(10) + 'b';
            out.push([el.innerHTML, JSON.stringify(el.innerText)]);
            el.innerText = null;
            out.push([el.innerHTML, el.innerHTML === '']);
            return out;
        })()""",
        expected=["onetwo\nthree", "  keep  ", "ab", ["xy", "onetwothree"],
                  ["&lt;b&gt;markup&lt;/b&gt;", 1],
                  ["a<br>b", '"ab"'], ["", True]],
    ),
    Repro(
        name="the_style_sheet_list_answers_by_position_and_by_name",
        note="document.styleSheets is live and its entries are stable objects: the same "
             "index twice is the same sheet, a name reaches the sheet whose owner carries "
             "that id, and an entry says where it came from.",
        html=(page(head='<style id="sheet-one">p { color: red }</style>'
                       '<style>div { margin: 0 }</style>')),
        script="""(() => {
            const sheets = document.styleSheets;
            const first = sheets[0];
            return [sheets.length, sheets.item(0) === first, sheets[0] === sheets[0],
                    sheets['sheet-one'] === first, sheets['nothing'] === undefined,
                    sheets[99], sheets.item(99),
                    Object.prototype.toString.call(sheets),
                    first instanceof CSSStyleSheet, first.type, first.title,
                    first.disabled, first.ownerNode === null, first.ownerNode.localName,
                    first.ownerNode.id, first.parentStyleSheet, first.href,
                    sheets.item.length, sheets.item === sheets.item];
        })()""",
        expected=[2, True, True, True, True, None, None, "[object StyleSheetList]", True,
                  "text/css", None, False, False, "style", "sheet-one", None, None, 1,
                  True],
    ),
    Repro(
        name="a_shadow_tree_is_the_hosts_own",
        note="attachShadow hands back a fragment the host keeps to itself: none of it "
             "shows through the host's children, text or markup, and only an open tree "
             "is reported again.",
        html=(page(body="<div id=host></div>")),
        script="""(() => {
            const host = document.getElementById('host');
            const root = host.attachShadow({mode: 'open'});
            root.innerHTML = '<span>in</span>';
            const shut = document.createElement('div');
            const closed = shut.attachShadow({mode: 'closed'});
            return [root === host.shadowRoot, root.mode, root.host === host,
                    Object.prototype.toString.call(root), root instanceof DocumentFragment,
                    Object.getPrototypeOf(ShadowRoot.prototype)
                      === DocumentFragment.prototype,
                    root.nodeType, root.ownerDocument === document, root.innerHTML,
                    host.innerHTML, host.childNodes.length, host.textContent,
                    shut.shadowRoot === null, closed.mode, shut.shadowRoot === null];
        })()""",
        expected=[True, "open", True, "[object ShadowRoot]", True, True, 11, True,
                  "<span>in</span>", "", 0, "", True, "closed", True],
    ),
    Repro(
        name="attach_shadow_refuses_what_blink_refuses",
        note="Five refusals in Blink's own words: a second tree on one host, an argument "
             "that is not a dictionary, a dictionary with no mode, a mode that is not the "
             "enum, and a tag that is not a shadow host.",
        html=(page(body="<div id=host></div>")),
        script="""(() => {
            const out = [];
            const attempt = (host, options) => {
                try { host.attachShadow(options); out.push('no-throw'); }
                catch (error) { out.push([error.name, error.message]); }
            };
            const again = document.getElementById('host');
            again.attachShadow({mode: 'open'});
            attempt(again, {mode: 'open'});
            for (const options of [undefined, null, 1, 'open', {}]) {
                attempt(document.createElement('div'), options);
            }
            for (const mode of ['bogus', 'Open', 1]) {
                attempt(document.createElement('div'), {mode});
            }
            for (const tag of ['img', 'li', 'table', 'body', 'head', 'button']) {
                attempt(document.createElement(tag), {mode: 'open'});
            }
            return out;
        })()""",
        expected=[["NotSupportedError", "Failed to execute 'attachShadow' on 'Element':"
                     " Shadow root cannot be created on a host which already hosts a"
                     " shadow tree."],
                  ["TypeError", "Failed to execute 'attachShadow' on 'Element': The"
                     " provided value is not of type 'ShadowRootInit'."],
                  ["TypeError", "Failed to execute 'attachShadow' on 'Element': The"
                     " provided value is not of type 'ShadowRootInit'."],
                  ["TypeError", "Failed to execute 'attachShadow' on 'Element': The"
                     " provided value is not of type 'ShadowRootInit'."],
                  ["TypeError", "Failed to execute 'attachShadow' on 'Element': The"
                     " provided value is not of type 'ShadowRootInit'."],
                  ["TypeError", "Failed to execute 'attachShadow' on 'Element': Failed to"
                     " read the 'mode' property from 'ShadowRootInit': Required member is"
                     " undefined."],
                  ["TypeError", "Failed to execute 'attachShadow' on 'Element': Failed to"
                     " read the 'mode' property from 'ShadowRootInit': The provided value"
                     " 'bogus' is not a valid enum value of type ShadowRootMode."],
                  ["TypeError", "Failed to execute 'attachShadow' on 'Element': Failed to"
                     " read the 'mode' property from 'ShadowRootInit': The provided value"
                     " 'Open' is not a valid enum value of type ShadowRootMode."],
                  ["TypeError", "Failed to execute 'attachShadow' on 'Element': Failed to"
                     " read the 'mode' property from 'ShadowRootInit': The provided value"
                     " '1' is not a valid enum value of type ShadowRootMode."],
                  ["NotSupportedError", "Failed to execute 'attachShadow' on 'Element':"
                     " This element does not support attachShadow"],
                  ["NotSupportedError", "Failed to execute 'attachShadow' on 'Element':"
                     " This element does not support attachShadow"],
                  ["NotSupportedError", "Failed to execute 'attachShadow' on 'Element':"
                     " This element does not support attachShadow"],
                  "no-throw",
                  ["NotSupportedError", "Failed to execute 'attachShadow' on 'Element':"
                     " This element does not support attachShadow"],
                  ["NotSupportedError", "Failed to execute 'attachShadow' on 'Element':"
                     " This element does not support attachShadow"]],
    ),
    Repro(
        name="a_non_constructible_interface_object_is_its_prototypes_constructor",
        note="AbortSignal cannot be constructed, and its prototype still answers with the "
             "interface object itself: exposure belongs to the baseline, the arity does not.",
        script="""(() => {
            const describe = (entry, parent) => [entry.name,
                Object.getOwnPropertyDescriptor(entry.prototype, 'constructor') ? 'own'
                                                                              : 'absent',
                entry.prototype.constructor === entry,
                Object.getPrototypeOf(entry.prototype) === parent.prototype];
            return [describe(AbortSignal, EventTarget), describe(BeforeUnloadEvent, Event),
                    BeforeUnloadEvent.length, typeof BeforeUnloadEvent];
        })()""",
        expected=[["AbortSignal", "own", True, True], ["BeforeUnloadEvent", "own", True, True],
                  0, "function"],
    ),
    Repro(
        name="a_worker_from_a_revoked_blob_url_still_starts",
        note="A page that bundles its worker inline revokes the URL in the same turn it "
             "hands it over; the script is taken while the worker is made, so the revoke "
             "is too late to take it away.",
        html=(page(body="""<script>
            window.__out = [];
            const url = URL.createObjectURL(new Blob(
                ["self.postMessage('started');"], {type: 'text/javascript'}));
            const worker = new Worker(url);
            URL.revokeObjectURL(url);
            worker.onmessage = (event) => window.__out.push(String(event.data));
            worker.onerror = () => window.__out.push('onerror');
        </script>""")),
        script="window.__out",
        expected=["started"],
        settle=0.5,
    ),
    Repro(
        name="element_selector_queries_exclude_the_root",
        html=(page(body='<div id="root" class="match"><span class="match"></span></div>')),
        script="""(() => {const root=document.getElementById('root'); return [
            root.querySelector('#root')===null, root.querySelectorAll('.match').length,
            root.querySelectorAll('#root,.match').length, root.matches('#root')];})()""",
        expected=[True, 1, 1, True],
    ),
    Repro(
        name="selector_combinators_backtrack_over_candidate_ancestors_and_siblings",
        html=(page(body='<main><div class="a"><div class="a"><i id="target"></i></div></div></main>'
                       '<section><b class="a"></b><b class="b"></b><b class="b"></b><i id="sibling"></i></section>')),
        script="""(() => {return [document.querySelector('main > .a i')?.id,
            document.querySelector('.a + .b ~ i')?.id];})()""",
        expected=['target', 'sibling'],
    ),
    Repro(
        name="zero_coefficient_nth_selectors_match_only_the_offset",
        html=(page(body='<div><i id="a"></i><i id="b"></i><i id="c"></i></div>')),
        script="Array.from(document.querySelectorAll('i:nth-child(0n+2)')).map(x=>x.id)",
        expected=['b'],
    ),
    Repro(
        name="empty_attribute_substring_selectors_match_nothing",
        html=(page(body='<i data-x="hello"></i><i data-x=""></i>')),
        script="""['^=','$=','*='].map(op=>document.querySelectorAll('[data-x'+op+'""'+']').length)""",
        expected=[0,0,0],
    ),

    Repro(
        name="redispatched_events_update_their_target_and_clear_dispatch_state",
        html=(page(body='<div id="parent"><i id="a"></i><i id="b"></i></div>')),
        script="""(() => {const a=document.getElementById('a'),b=document.getElementById('b');
          const e=new Event('x',{bubbles:true}); const seen=[];
          document.getElementById('parent').addEventListener('x',v=>v.stopPropagation(),true);
          a.dispatchEvent(e); seen.push(e.eventPhase,e.currentTarget===null,e.cancelBubble,e.composedPath().length);
          b.dispatchEvent(e); seen.push(e.target===b,e.eventPhase,e.currentTarget===null); return seen;})()""",
        expected=[0,True,False,0,True,0,True],
    ),
    Repro(
        name="a_once_listener_is_removed_before_reentrant_dispatch",
        script="""(() => {const t=new EventTarget(); let calls=0;
          t.addEventListener('x',()=>{if(++calls===1)t.dispatchEvent(new Event('x'));},{once:true});
          t.dispatchEvent(new Event('x')); return calls;})()""",
        expected=1,
    ),
    Repro(
        name="a_listener_removed_during_dispatch_is_not_invoked",
        script="""(() => {const t=new EventTarget();const seen=[];const second=()=>seen.push('second');
          t.addEventListener('x',()=>{seen.push('first');t.removeEventListener('x',second);});
          t.addEventListener('x',second); t.dispatchEvent(new Event('x'));return seen;})()""",
        expected=['first'],
    ),
    Repro(
        name="an_event_cannot_be_recursively_dispatched_or_reinitialized",
        script="""(() => {const t=new EventTarget();const e=new Event('x');const seen=[];
          t.addEventListener('x',()=>{e.initEvent('y');seen.push(e.type);
            try{t.dispatchEvent(e);}catch(error){seen.push(error.name);}} ,{once:true});
          t.dispatchEvent(e); return seen;})()""",
        expected=['x','InvalidStateError'],
    ),

    Repro(
        name="event_listener_identity_survives_multiple_bridge_crossings",
        script="""(() => {const t=new EventTarget();let calls=0;const f=()=>calls++;
          t.addEventListener('x',f);t.addEventListener('x',f);t.dispatchEvent(new Event('x'));
          const first=calls;t.removeEventListener('x',f);t.dispatchEvent(new Event('x'));return [first,calls];})()""",
        expected=[1,1],
    ),

    Repro(
        name="indexeddb_bulk_results_and_indexes_read_stored_objects",
        html=(page(body="""<script>
          const open=indexedDB.open('objects',1);
          open.onupgradeneeded=()=>{const s=open.result.createObjectStore('s',{keyPath:'id'});s.createIndex('name','name');};
          open.onsuccess=()=>{const db=open.result,tx=db.transaction('s','readwrite'),store=tx.objectStore('s');
            store.put({id:1,name:'first',data:new Uint8Array([7,8]).buffer});store.put({id:2,name:'second'});
            tx.oncomplete=()=>{const s=db.transaction('s').objectStore('s'),all=s.getAll();
              all.onsuccess=()=>{const rows=all.result;const notes=[rows.map(x=>x.name),rows===all.result,
                rows[0].data instanceof ArrayBuffer,Array.from(new Uint8Array(rows[0].data))];
                const index=db.transaction('s').objectStore('s').index('name').get('second');
                index.onsuccess=()=>{notes.push(index.result.id);globalThis.idbProbe=notes;db.close();};};};};
        </script>""")),
        script="globalThis.idbProbe",
        expected=[['first','second'],True,True,[7,8],2],
        settle=0.05,
    ),
    Repro(
        name="indexeddb_generated_keys_are_injected_into_the_clone_once",
        html=(page(body="""<script>
          const open=indexedDB.open('generated',1);
          open.onupgradeneeded=()=>open.result.createObjectStore('s',{keyPath:'id',autoIncrement:true});
          open.onsuccess=()=>{const db=open.result,tx=db.transaction('s','readwrite');let reads=0;const value={};
            Object.defineProperty(value,'name',{enumerable:true,get(){reads++;return 'first';}});
            const put=tx.objectStore('s').put(value);let key;put.onsuccess=()=>key=put.result;
            tx.oncomplete=()=>{const get=db.transaction('s').objectStore('s').get(key);
              get.onsuccess=()=>{globalThis.idbProbe=[key,value.id===undefined,get.result.id,get.result.name,reads];db.close();};};};
        </script>""")),
        script="globalThis.idbProbe",
        expected=[1,True,1,'first',1],
        settle=0.05,
    ),
    Repro(
        name="indexeddb_handled_unique_index_errors_preserve_existing_records",
        html=(page(body="""<script>
          const open=indexedDB.open('unique',1);
          open.onupgradeneeded=()=>{const s=open.result.createObjectStore('s',{keyPath:'id'});s.createIndex('name','name',{unique:true});};
          open.onsuccess=()=>{const db=open.result,seed=db.transaction('s','readwrite');
            seed.objectStore('s').put({id:1,name:'a'});seed.objectStore('s').put({id:2,name:'b'});
            seed.oncomplete=()=>{const tx=db.transaction('s','readwrite');const put=tx.objectStore('s').put({id:2,name:'a'});let error;
              put.onerror=e=>{error=put.error.name;e.preventDefault();};
              tx.oncomplete=()=>{const all=db.transaction('s').objectStore('s').getAll();
                all.onsuccess=()=>{globalThis.idbProbe=[error,all.result.map(x=>x.name)];db.close();};};};};
        </script>""")),
        script="globalThis.idbProbe",
        expected=['ConstraintError',['a','b']],
        settle=0.05,
    ),

    Repro(
        name="indexeddb_multi_entry_indexes_deduplicate_keys_without_changing_values",
        html=(page(body="""<script>
          const open=indexedDB.open('multi',1);
          open.onupgradeneeded=()=>{const s=open.result.createObjectStore('s',{keyPath:'id'});s.createIndex('tags','tags',{unique:true,multiEntry:true});};
          open.onsuccess=()=>{const db=open.result,tx=db.transaction('s','readwrite');
            tx.objectStore('s').put({id:1,tags:['x','x']});
            tx.oncomplete=()=>{const store=db.transaction('s').objectStore('s'),count=store.index('tags').count('x');
              count.onsuccess=()=>{const get=db.transaction('s').objectStore('s').get(1);
                get.onsuccess=()=>{globalThis.idbProbe=[count.result,get.result.tags];db.close();};};};};
        </script>""")),
        script="globalThis.idbProbe",
        expected=[1,['x','x']],
        settle=0.05,
    ),

)


# Local browser capability regressions.
CASES += (
    Repro(name='storage_access_queries_are_promises_and_reject_inactive_documents',
          script='window.__out',
          expected=[True, True, True, 'InvalidStateError'],
          html="<html><head></head><body><script>\n            window.__out = [];\n            const query = document.hasStorageAccess();\n            __out.push(query instanceof Promise);\n            Promise.all([query, document.hasUnpartitionedCookieAccess(),\n                document.implementation.createHTMLDocument('').hasStorageAccess()\n                    .catch(error => error.name)]).then(values => __out.push(...values));\n        </script></body></html>",
          url='https://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.2),
    Repro(name='css_declarations_preserve_semicolons_inside_urls_and_strings',
          script='(() => {\n            const target = document.getElementById(\'target\');\n            const sheet = getComputedStyle(target).backgroundImage;\n            target.style.cssText = \'background-image: url("data:image/png;base64,BAUG"); \'\n                + \'content: "a;b"; opacity: 0.5\';\n            return [sheet, target.style.backgroundImage, target.style.content,\n                    getComputedStyle(target).backgroundImage, target.style.opacity];\n        })()',
          expected=['url("data:image/png;base64,AQID")', 'url("data:image/png;base64,BAUG")', '"a;b"', 'url("data:image/png;base64,BAUG")', '0.5'],
          html='<html><head><style>#target {background-image: url("data:image/png;base64,AQID");\n            color: red;}</style></head><body><div id="target"></div></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='worker_import_scripts_execute_synchronously_and_stop_on_exception',
          script='window.__out',
          expected=['start', 'undefined', 'first', 42, 'RangeError', 'coerce', 'TypeError', 'end'],
          html='<html><head></head><body><script>\n            const source = `\n                self.order = [\'start\'];\n                const script = text => \'data:text/javascript,\' + encodeURIComponent(text);\n                order.push(typeof importScripts());\n                importScripts(script(\'self.value = 41; order.push("first");\'),\n                              script(\'order.push(self.value + 1);\'));\n                try {\n                    importScripts(script(\'throw new RangeError("stop");\'),\n                                  script(\'order.push("unreachable");\'));\n                } catch (error) { order.push(error.name); }\n                try {\n                    importScripts({toString() { order.push(\'coerce\');\n                        return script(\'order.push("unreachable");\'); }}, Symbol());\n                } catch (error) { order.push(error.name); }\n                order.push(\'end\'); postMessage(order);\n            `;\n            window.__out = [];\n            const worker = new Worker(URL.createObjectURL(new Blob([source],\n                {type: \'text/javascript\'})));\n            worker.onmessage = event => window.__out = event.data;\n            worker.onerror = event => window.__out = [\'error\', event.message];\n        </script></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.5),
    Repro(name='client_rects_are_snapshots_and_omit_unrendered_elements',
          script="(() => {\n            const parent=document.createElement('div'), child=document.createElement('div');\n            child.style.cssText='width:24px;height:12px;padding:0;border:0;margin:0';\n            parent.appendChild(child);\n            const detached=child.getClientRects();\n            const out=[[detached.length,detached.constructor.name]];\n            document.body.appendChild(parent);\n            const rects=child.getClientRects();\n            out.push([rects instanceof DOMRectList,rects.length,rects[0].width,rects.item(0).height,\n                      rects[0]===rects.item(0),rects.item(10)===null,rects[10]===undefined,Array.isArray(rects)]);\n            child.style.height='20px';\n            out.push([Array.from(rects).length,rects[0].height,child.getClientRects()[0].height]);\n            child.style.visibility='hidden';out.push(child.getClientRects().length);\n            parent.style.display='none';out.push(child.getClientRects().length);\n            parent.remove();out.push(child.getClientRects().length);\n            return out;\n        })()",
          expected=[[0, 'DOMRectList'], [True, 1, 24, 12, True, True, True, False], [1, 12, 20], 1, 0, 0],
          html='<html><head></head><body></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='svg_ctm_composes_groups_and_tracks_attribute_changes',
          script="(() => {const make=(name,attrs)=>{const e=document.createElementNS('http://www.w3.org/2000/svg',name);\n                for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e};\n            const svg=make('svg',{width:200,height:100}),parent=make('g',{transform:'translate(10,20)'}),g=make('g',{transform:'scale(2,3)'});\n            document.body.appendChild(svg);svg.appendChild(parent);parent.appendChild(g);\n            const read=()=>{const m=g.getCTM();return [m.a,m.b,m.c,m.d,m.e,m.f]};\n            const before=read();g.setAttribute('transform','translate(5,7)');\n            return [before,read(),g.getCTM() instanceof SVGMatrix,g.getCTM()!==g.getCTM()];})()",
          expected=[[2, 0, 0, 3, 10, 20], [1, 0, 0, 1, 15, 27], True, True],
          html='<html><head></head><body></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='svg_ctm_includes_view_box_and_centered_rotation',
          script="(() => {const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');\n            svg.setAttribute('width',200);svg.setAttribute('height',100);svg.setAttribute('viewBox','0 0 100 50');\n            const g=document.createElementNS('http://www.w3.org/2000/svg','g');\n            g.setAttribute('transform','translate(3,4) scale(2) rotate(90,5,6)');\n            document.body.appendChild(svg);svg.appendChild(g);const m=g.getCTM();\n            return [m.a,m.b,m.c,m.d,m.e,m.f].map(v=>Math.round(v*1000)/1000);})()",
          expected=[0, 4, -4, 0, 50, 12],
          html='<html><head></head><body></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='nested_inline_blocks_include_padding_and_max_width',
          script="['outer','middle','group','only','variable','cap','leaf'].map(id=>document.getElementById(id).offsetWidth)",
          expected=[832, 622, 194, 194, 174, 58, 105],
          html='<html><head><style>\n              #measure { --gap:58px }\n              .cap {display:inline-block;max-width:58px}\n              #measure>div>div>div>div:only-of-type {display:inline-block;padding:10px}\n              .group {display:inline-block;max-width:706px}\n              #measure>div>div:first-of-type {display:inline-block;padding:214px}\n              #measure>div {display:inline-block;padding:105px}\n            </style></head><body><div id=measure><div id=outer><div id=middle><div id=group class=group>\n              <div id=only><div id=variable style="display:inline-block;padding:var(--gap)">\n                <div id=cap class=cap><div id=leaf style="width:105px"></div></div>\n              </div></div></div></div></div></div></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='inherited_custom_properties_resolve_css_length_math',
          script="['a','b','c'].map(id=>document.getElementById(id).offsetWidth)",
          expected=[399, 100, 30],
          html='<html><head></head><body><div style="--base:399px;--size:max(var(--base),20px)">\n          <div id=a style="display:inline-block;width:var(--size)"></div>\n          <div id=b style="width:min(100px,var(--size))"></div>\n          <div id=c style="width:clamp(20px,calc(10px + 25px),30px)"></div>\n        </div></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='only_child_and_only_of_type_distinguish_element_siblings',
          script="['a','b','c'].map(id=>{const e=document.getElementById(id);return [e.matches(':only-child'),e.matches(':only-of-type')]})",
          expected=[[False, True], [False, True], [True, True]],
          html='<html><head></head><body><div><p id=a></p><span id=b></span></div><div><p id=c></p></div></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='response_json_preserves_root_and_nested_null',
          script='jsonResult',
          expected=[True, True, True, True],
          html='<html><head></head><body><script>\n            globalThis.jsonResult = \'pending\';\n            Promise.all([\n                new Response(\'null\').json(),\n                new Response(\'{"a":null,"b":[null,{"c":null}]}\').json()\n            ]).then(([root, value]) => {\n                jsonResult = [root === null, value.a === null,\n                              value.b[0] === null, value.b[1].c === null];\n            });\n        </script></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.05),
    Repro(name='script_inserted_iframe_exposes_initial_window_synchronously',
          script='frameResult',
          expected=[True, True, True],
          html="<html><head></head><body><script>\n            const frame = document.createElement('iframe');\n            globalThis.frameResult = [frame.contentWindow === null];\n            document.body.appendChild(frame);\n            frameResult.push(frame.contentWindow !== null,\n                             frame.contentDocument.readyState === 'complete');\n        </script></body></html>",
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='same_origin_frame_window_methods_keep_receiver_and_identity',
          script="(() => {\n            const w = document.getElementById('frame').contentWindow;\n            const timer = w.setTimeout;\n            let wrongReceiver = false;\n            try { timer.call({}, () => {}, 0); }\n            catch (error) { wrongReceiver = error instanceof TypeError || error.name === 'TypeError'; }\n            return [typeof w.setTimeout(() => {}, 0),\n                    typeof timer.call(w, () => {}, 0), timer === w.setTimeout,\n                    timer.name, timer.length, wrongReceiver,\n                    w === window[0], w.self === w];\n        })()",
          expected=['number', 'number', True, 'setTimeout', 1, True, True, True],
          html='<html><head></head><body><iframe id="frame"></iframe></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='iframe_window_has_its_own_realm_and_document_identity',
          script="(() => {\n            const frame = document.getElementById('frame');\n            const w = frame.contentWindow;\n            return [Object.getPrototypeOf(w) === w.Window.prototype,\n                    w.Object !== Object, w.Array !== Array,\n                    frame.contentDocument === w.document,\n                    w.document.defaultView === w,\n                    w.eval('window') === w,\n                    w.Function('return this')() === w,\n                    Object.getOwnPropertyDescriptor(w, 'setTimeout').value === w.setTimeout];\n        })()",
          expected=[True, True, True, True, True, True, True, True],
          html='<html><head></head><body><iframe id="frame"></iframe></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='deferred_scripts_observe_interactive_before_dom_content_loaded',
          script='order',
          expected=[['state', 'interactive'], ['defer', 'interactive'], ['dom', 'interactive'], ['state', 'complete'], ['load', 'complete']],
          html='<html><head><script>\n            window.order=[];\n            document.addEventListener(\'readystatechange\',()=>order.push([\'state\',document.readyState]));\n            document.addEventListener(\'DOMContentLoaded\',()=>order.push([\'dom\',document.readyState]));\n            window.addEventListener(\'load\',()=>order.push([\'load\',document.readyState]));\n            </script><script defer src="data:text/javascript,order.push(%5B%22defer%22%2Cdocument.readyState%5D)"></script></head><body></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='html_and_svg_style_assignment_forwards_to_css_text',
          script="(() => {\n            const html = document.createElement('div');\n            const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');\n            document.body.append(html,svg);\n            return [html,svg].map(element => {\n                const style = element.style;\n                element.style = 'height:15px;display:block';\n                const out = [element.style === style,style.height,style.display,\n                             getComputedStyle(element).height];\n                element.style = null;\n                out.push(style.cssText);\n                return out;\n            });\n        })()",
          expected=[[True, '15px', 'block', '15px', ''], [True, '15px', 'block', '15px', '']],
          html='',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='computed_custom_properties_resolve_numeric_colors_and_3d_transforms',
          script="(() => {\n            const parent = document.createElement('div'), child = document.createElement('div');\n            parent.style = '--a:3;--b:5;';\n            child.style = 'height:var(--height,15px);color:rgb(calc(var(--a) * 10 + (var(--b) - 1)),calc(2 * var(--b)),calc(var(--missing,7) + 1));transform:perspective(10px) rotate3d(1,0,0,90deg) scale3d(2,3,4) rotateX(.25turn) translateZ(5px)';\n            parent.appendChild(child); document.body.appendChild(parent);\n            const css = getComputedStyle(child);\n            return [css.color,css.height,css.transform,css.getPropertyValue('--a')];\n        })()",
          expected=['rgb(34, 10, 8)', '15px', 'matrix3d(2, 0, 0, 0, 0, -4, 0, 0, 0, 0, -3, 0.3, 0, 0, -15, 2.5)', '3'],
          html='',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='computed_styles_resolve_windows_light_theme_system_colors',
          script="(() => {\n            const element = document.createElement('div');\n            document.body.appendChild(element);\n            return ['CanvasText','Canvas','VisitedText','HighlightText'].map(name => {\n                element.style.color = name;\n                element.style.backgroundColor = name;\n                const css = getComputedStyle(element);\n                return [css.color,css.backgroundColor];\n            });\n        })()",
          expected=[['rgb(0, 0, 0)', 'rgb(0, 0, 0)'], ['rgb(255, 255, 255)', 'rgb(255, 255, 255)'], ['rgb(0, 102, 204)', 'rgb(0, 102, 204)'], ['rgb(255, 255, 255)', 'rgb(255, 255, 255)']],
          html='',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='canvas_dimensions_follow_attributes_layout_and_reset_surface',
          script="(() => {\n            const c = document.querySelector('canvas'), ctx = c.getContext('2d');\n            const read = () => [c.width,c.height,c.getBoundingClientRect().width,\n                               c.getBoundingClientRect().height];\n            const out = [read()];\n            ctx.fillStyle = 'red'; ctx.fillRect(0,0,4,4);\n            c.setAttribute('width','28');\n            out.push(ctx.fillStyle,Array.from(ctx.getImageData(1,1,1,1).data),ctx===c.getContext('2d'));\n            c.width=27; c.height=19; out.push(read());\n            c.setAttribute('width','42'); out.push(read());\n            c.removeAttribute('width'); out.push(read());\n            c.setAttribute('height','invalid'); out.push(read());\n            c.width = NaN; out.push(c.width,c.getAttribute('width'));\n            return out;\n        })()",
          expected=[[28, 15, 28, 15], '#000000', [0, 0, 0, 0], True, [27, 19, 27, 19], [42, 19, 42, 19], [300, 19, 300, 19], [300, 150, 300, 150], 0, '0'],
          html='<html><head></head><body><canvas width="28" height="15"></canvas></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='window_postmessage_names_unclonable_browser_objects',
          script="(() => {\n            const values = [navigator.plugins, navigator.plugins[0],\n                            navigator.mimeTypes, navigator.mimeTypes[0]];\n            return values.map(value => {\n                try { window.postMessage(value, '*'); return 'accepted'; }\n                catch (error) { return [error.name, error.message, error instanceof DOMException]; }\n            });\n        })()",
          expected=[['DataCloneError', "Failed to execute 'postMessage' on 'Window': PluginArray object could not be cloned.", True], ['DataCloneError', "Failed to execute 'postMessage' on 'Window': Plugin object could not be cloned.", True], ['DataCloneError', "Failed to execute 'postMessage' on 'Window': MimeTypeArray object could not be cloned.", True], ['DataCloneError', "Failed to execute 'postMessage' on 'Window': MimeType object could not be cloned.", True]],
          html='',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='canvas_gradients_draw_and_validate_arguments',
          script="(() => {\n            const canvas = document.createElement('canvas');\n            canvas.width = canvas.height = 8;\n            const ctx = canvas.getContext('2d');\n            const radial = ctx.createRadialGradient(4,4,0,4,4,9);\n            radial.addColorStop(0,'red'); radial.addColorStop(1,'red');\n            ctx.fillStyle = radial;\n            const out = [radial.constructor.name, ctx.fillStyle === radial];\n            ctx.fillRect(0,0,8,8);\n            out.push(Array.from(ctx.getImageData(1,1,1,1).data));\n            const linear = ctx.createLinearGradient(0,0,8,0);\n            linear.addColorStop(0,'blue'); linear.addColorStop(1,'blue');\n            ctx.fillStyle = linear;\n            ctx.beginPath(); ctx.rect(0,0,8,8); ctx.fill();\n            out.push(Array.from(ctx.getImageData(1,1,1,1).data));\n            for (const action of [() => radial.addColorStop(-1,'red'),\n                 () => radial.addColorStop(1,'bogus'),\n                 () => ctx.createRadialGradient(0,0,-1,0,0,1),\n                 () => ctx.createLinearGradient(NaN,0,0,1)]) {\n                try { action(); out.push('accepted'); }\n                catch(e) { out.push([e.name,e.message,e instanceof DOMException]); }\n            }\n            return out;\n        })()",
          expected=['CanvasGradient', True, [255, 0, 0, 255], [0, 0, 255, 255], ['IndexSizeError', "Failed to execute 'addColorStop' on 'CanvasGradient': The provided value (-1) is outside the range (0.0, 1.0).", True], ['SyntaxError', "Failed to execute 'addColorStop' on 'CanvasGradient': The value provided ('bogus') could not be parsed as a color.", True], ['IndexSizeError', "Failed to execute 'createRadialGradient' on 'CanvasRenderingContext2D': The r0 provided is less than 0.", True], ['TypeError', "Failed to execute 'createLinearGradient' on 'CanvasRenderingContext2D': The provided double value is non-finite.", False]],
          html='',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='svg_point_factory_returns_distinct_mutable_float_points',
          script="(() => {\n            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');\n            const p = svg.createSVGPoint();\n            const out = [p.constructor.name, Object.prototype.toString.call(p),\n                         p.x, p.y, typeof p.z, typeof p.w, p instanceof DOMPoint,\n                         p === svg.createSVGPoint()];\n            p.x = 1.234567890123; p.y = '2.5';\n            out.push(p.x, p.y);\n            try { p.x = Infinity; } catch (e) { out.push(e.name, e.message); }\n            out.push(p.x);\n            return out;\n        })()",
          expected=['SVGPoint', '[object SVGPoint]', 0, 0, 'undefined', 'undefined', False, False, 1.2345678806304932, 2.5, 'TypeError', "Failed to set the 'x' property on 'SVGPoint': The provided float value is non-finite.", 1.2345678806304932],
          html='',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='svg_rect_bbox_and_fill_follow_attachment_and_display',
          script="(() => {\n            const ns = 'http://www.w3.org/2000/svg';\n            const svg = document.createElementNS(ns, 'svg');\n            const rect = document.createElementNS(ns, 'rect');\n            for (const [key, value] of Object.entries({x:3, y:4, width:10, height:20}))\n                rect.setAttribute(key, value);\n            const read = () => {\n                const r = rect.getBBox();\n                return [r.constructor.name, r.x, r.y, r.width, r.height,\n                        rect.isPointInFill({x:5, y:5})];\n            };\n            const out = [read()];\n            svg.appendChild(rect); out.push(read());\n            document.body.appendChild(svg); out.push(read());\n            svg.style.display = 'none'; out.push(read());\n            return out;\n        })()",
          expected=[['SVGRect', 0, 0, 0, 0, False], ['SVGRect', 0, 0, 0, 0, False], ['SVGRect', 3, 4, 10, 20, True], ['SVGRect', 0, 0, 0, 0, False]],
          html='',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
    Repro(name='hit_testing_refreshes_style_between_layout_passes',
          script="(() => {\n            const target = document.getElementById('hit');\n            const out = [document.elementFromPoint(25,25) === target];\n            target.style.left = '100px';\n            out.push(document.elementFromPoint(25,25) !== target,\n                     document.elementFromPoint(105,25) === target);\n            target.style.visibility = 'hidden';\n            out.push(document.elementFromPoint(105,25) !== target);\n            target.style.visibility = 'visible';\n            out.push(document.elementFromPoint(105,25) === target);\n            return out;\n        })()",
          expected=[True, True, True, True, True],
          html='<html><head></head><body><div id="hit" style="position:absolute;left:20px;top:20px;width:50px;height:50px"></div></body></html>',
          url='http://repro.test/dir/page',
          expect_error='',
          known_divergence='',
          settle=0.0),
)


CASES += (Repro(
    name='canvas_context_dictionary_and_nonfinite_radial_gradient',
    script="""(()=>[document.createElement('canvas'),new OffscreenCanvas(1,1)].map(c=>{
      const x=c.getContext('2d'),a=x.getContextAttributes();let error;
      try{x.createRadialGradient(NaN,0,0,0,0,1)}catch(e){error=e.name}
      return [a.alpha,a.colorSpace,a.colorType,a.desynchronized,a.toneMapping.mode,a.willReadFrequently,error];
    }))()""",
    expected=[[True,'srgb','unorm8',False,'standard',False,'TypeError']]*2,
),)

CASES += (Repro(
    name='canvas_rect_conversion_and_svg_matrix_double_precision',
    script="""(()=>{
      const c=document.createElement('canvas'), x=c.getContext('2d');const errors=[];
      for(const args of [[],[1],[1,2],[1,2,3]]){
        try{x.rect(...args)}catch(e){errors.push(e.name)}
      }
      x.rect(NaN,0,10,10);x.fill();
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
      const g=document.createElementNS(svg.namespaceURI,'g');svg.append(g);document.body.append(svg);
      g.setAttribute('transform','rotate(45)');const m=g.getCTM();
      const rounded=m.a===Math.fround(m.a);m.a=1/3;const exact=m.a===1/3;
      let error;try{m.a=Infinity}catch(e){error=e.name}
      return [errors,Array.from(x.getImageData(1,1,1,1).data),rounded,exact,error];
    })()""",
    expected=[['TypeError']*4,[0,0,0,0],False,True,'TypeError'],
),)
