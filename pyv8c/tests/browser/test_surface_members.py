"""DOM/BOM members a challenge reads, and the differences a two-sided surface diff found.

Measured against Chrome 154 on 2026-10-07 -- the same
page, the same probe, both engines, comparing every member of ~86 DOM/BOM objects by kind
(accessor, value, function arity).  Two of the three classes are guarded here:

* a member this engine carried that Chrome has nowhere (``Event.prototype.persisted``,
  ``Performance.navigation_id``) -- a fingerprint in the direction that is easy to miss, because
  the page reads a value where Chrome reads ``undefined``;
* a member Chrome has and this engine does not, **when the engine can declare it**.  ``Response``
  is the example that cannot: its static ``json`` shares a name with the instance method, and a
  member table is keyed by name, so the guard watches the instance one instead.

The third class -- members the baseline itself has nowhere to put, such as the eight ``2d``
context attributes -- is a data gap with no honest assertion available until the baseline is
regenerated.

A fourth is not about members but about the object holding them: a callback interface has no
``prototype``, which Blink states in ``SetupIDLCallbackInterfaceTemplate`` and this engine had
to learn (see the ``NodeFilter`` case below).
"""

import asyncio
import json
import threading
import time

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")

from pyv8c.async_api import Browser

PAGE = "<!doctype html><title>members</title><body></body>"

SCRIPT = """(() => {
  var out = {}; window.__out = out;
  out.eventHasPersisted = ('persisted' in Event.prototype);
  out.newEventPersisted = typeof new Event('x').persisted;
  var transition = new PageTransitionEvent('pageshow', {persisted: true});
  var descriptor = Object.getOwnPropertyDescriptor(PageTransitionEvent.prototype, 'persisted');
  out.transition = [transition.persisted, !!(descriptor && descriptor.get)];
  out.performanceNames = Object.getOwnPropertyNames(performance)
    .concat(Object.keys(performance));
  out.performanceWorks = typeof performance.now() === 'number';
  new Response('{"b":2}').json().then(function (value) { out.instanceBody = value; });
  return 'asked';
})()"""


async def _pump(page, seconds=1.5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            (await page.run_until_idle(timeout=0))
        except BaseException:  # noqa: BLE001
            pass
        (await asyncio.sleep(0.01))


async def test_a_collection_publishes_the_iterators_its_interface_has():
    """``HTMLCollection`` is a sibling of ``NodeList``, not a subclass of it.

    Sharing ``NodeList``'s implementation as a *base class* carried its iterator flags along, so
    ``HTMLCollection``, its two subclasses and ``NamedNodeMap`` published
    ``entries``/``keys``/``values``/``forEach`` -- four names Chrome does not have there, on
    interfaces whose baseline parent is ``None`` (measured against Chrome 154 on 2026-10-07;
    the same probe shows both sides do have ``Symbol.iterator``, which is why the fix narrows
    the flags rather than dropping them).

    The other direction matters as much: what really is a ``NodeList`` keeps the four names,
    inherited from it.
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://surface.test/", wait_until="commit"))
        answer = page.evaluate("""(() => {
          const four = ['entries', 'keys', 'values', 'forEach'];
          const named = (name) => four.filter((m) => m in window[name].prototype);
          return {
            iterator: [typeof HTMLCollection.prototype[Symbol.iterator],
                       typeof NamedNodeMap.prototype[Symbol.iterator]],
            named: {HTMLCollection: named('HTMLCollection'),
                    HTMLOptionsCollection: named('HTMLOptionsCollection'),
                    NamedNodeMap: named('NamedNodeMap')},
            both: [named('NodeList'), named('RadioNodeList'), named('DOMTokenList')],
            chain: {
              above: [Object.getPrototypeOf(HTMLCollection.prototype)
                        === NodeList.prototype,
                      Object.getPrototypeOf(NamedNodeMap.prototype) === NodeList.prototype],
              name: Object.getPrototypeOf(HTMLCollection.prototype).constructor.name,
              radio: Object.getPrototypeOf(RadioNodeList.prototype) === NodeList.prototype,
            },
          };
        })()""")
    finally:
        await browser.close()

    assert answer["iterator"] == ["function", "function"], answer["iterator"]
    assert answer["named"] == {"HTMLCollection": [], "HTMLOptionsCollection": [],
                               "NamedNodeMap": []}, answer["named"]
    assert answer["both"] == [["entries", "keys", "values", "forEach"]] * 3, answer["both"]
    #: The interface graph, not just the members: a subclass would put ``NodeList.prototype``
    #: above these two, and Chrome has no such link.  What sits above them now is an
    #: ``Object``-named prototype -- measured, and *not* the page's ``Object.prototype``, which
    #: is a separate difference left for later (see the case document).
    assert answer["chain"]["above"] == [False, False], answer["chain"]
    assert answer["chain"]["name"] == "Object", answer["chain"]
    assert answer["chain"]["radio"] is True, answer["chain"]


async def test_a_maplike_interface_publishes_its_iterator_and_a_value_is_not_one():
    """``Symbol.iterator`` for the interfaces that have no hand-written spec.

    ``AudioParamMap``, ``CustomStateSet`` and ``RTCStatsReport`` answer ``Symbol.iterator`` in
    Chrome; their baseline entry declares ``entries`` as an *operation*, and nothing in the
    engine published the iterator for them until the rule in ``assembly._compile``.  The trap is
    next to it: ``CSSMathMax`` declares ``values`` as an *attribute* -- a length, not an
    iterator -- and its prototype has no ``Symbol.iterator`` in Chrome, so keying the rule on the
    name alone published thirteen iterators too many (measured against Chrome 154 on 2026-10-07
    with ``surface_diff.py --probe surface_wide``, which compares the labelled symbols).

    Async-only interfaces are the known residue: ``FileSystemDirectoryHandle`` is iterable
    asynchronously there and not synchronously, and the baseline does not say which.
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://surface.test/", wait_until="commit"))
        answer = page.evaluate("""(() => {
          const has = (name) => typeof window[name].prototype[Symbol.iterator];
          return {maps: [has('AudioParamMap'), has('CustomStateSet'), has('RTCStatsReport')],
                  values: has('CSSMathMax'),
                  attribute: 'values' in CSSMathMax.prototype};
        })()""")
    finally:
        await browser.close()

    assert answer["maps"] == ["function"] * 3, answer["maps"]
    assert answer["values"] == "undefined", answer["values"]
    #: The attribute that made the loose rule wrong is still published: the rule skips the
    #: iterator for that interface, and takes nothing else away.
    assert answer["attribute"] is True, answer


async def test_implementation_state_is_not_an_idl_constant():
    """A plain ``int`` class attribute is implementation state, not surface.

    ``AudioNode.inputs``/``outputs`` are channel counts and ``ScriptProcessorNode``'s
    ``channel_count`` is a default; harvesting them into the constant list put them on 46
    interface objects, where Chrome has neither (measured against Chrome 154 on 2026-10-07
    with a surface-wide probe comparing every member of all 2046 holders a window exposes).
    The same seed produced ``RULE_TYPE`` on 54 holders and was the root of
    ``Performance.navigation_id`` as well.

    The other half is just as important: a real constant must still be there, and it now comes
    from the interface baseline rather than from a Python attribute.
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://surface.test/", wait_until="commit"))
        answer = page.evaluate("""(() => {
          const state = ['inputs', 'outputs', 'channel_count'];
          const own = (name) => Object.getOwnPropertyNames(window[name] || {});
          const leaked = [];
          for (const name of ['AudioNode', 'ScriptProcessorNode', 'AnalyserNode']) {
            for (const member of state) if (own(name).indexOf(member) >= 0) leaked.push(name + '.' + member);
          }
          return {
            leaked,
            elementNode: [typeof Node.ELEMENT_NODE, Node.ELEMENT_NODE, Node.TEXT_NODE],
            own: Object.getOwnPropertyNames(Node).filter((n) => n === 'ELEMENT_NODE'),
          };
        })()""")
    finally:
        await browser.close()

    assert answer["leaked"] == [], answer["leaked"]
    assert answer["elementNode"] == ["number", 1, 3], answer["elementNode"]
    assert answer["own"] == ["ELEMENT_NODE"], answer["own"]


async def test_the_dom_bom_surface_matches_what_chrome_reports():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://surface.test/", wait_until="commit"))
        page.evaluate(SCRIPT)
        (await _pump(page))
        out = json.loads(page.evaluate("JSON.stringify(window.__out)"))
    finally:
        await browser.close()

    #: ``persisted`` belongs to ``PageTransitionEvent``: Chrome's ``Event.prototype`` has no such
    #: member, so ``new Event('x').persisted`` is ``undefined`` there, and the event that does
    #: carry it still works.
    assert out["eventHasPersisted"] is False, out
    assert out["newEventPersisted"] == "undefined", out
    assert out["transition"] == [True, True], out

    #: ``Performance.navigation_id`` was an enumerable data property here and exists nowhere in
    #: Chrome; what Chrome does expose has to survive the change.
    assert "navigation_id" not in out["performanceNames"], out["performanceNames"]
    assert out["performanceWorks"] is True, out

    #: The instance ``Response.json``: the static of the same name cannot be declared yet, and
    #: this guard is what keeps the next attempt at it from quietly taking the instance's place.
    assert out["instanceBody"] == {"b": 2}, out


async def test_a_callback_interface_is_a_function_without_a_prototype():
    """``NodeFilter`` is a callback interface, and Chrome's shape says so.

    Blink gives the three shapes in ``SetupIDLCallbackInterfaceTemplate`` and its neighbours
    (``third_party/blink/renderer/bindings/core/v8/generated_code_helper.cc``): an ordinary
    interface calls ``ReadOnlyPrototype()``, ``SetClassName()`` and tags its **prototype**; a
    namespace is an ``ObjectTemplate`` tagged on itself; a callback interface calls
    ``RemovePrototype()`` and ``SetClassName()``, and tags nothing.  ``NodeFilter`` is the
    third, so its ``prototype`` is ``undefined`` while ``typeof`` is still ``function`` --
    measured with ``surface_diff.py --probe surface_wide``, where Chrome's holder has the
    sixteen constants plus the function's non-enumerable ``name``/``length`` and ours had a
    twenty-eight member ``prototype`` too.
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://surface.test/", wait_until="commit"))
        answer = page.evaluate("""(() => {
          const flags = (name) => {
            const d = Object.getOwnPropertyDescriptor(NodeFilter, name);
            return d ? [d.writable, d.enumerable, d.configurable] : null;
          };
          let constructed;
          try { new NodeFilter(); constructed = 'no throw'; } catch (e) { constructed = e.name; }
          return {
            kind: typeof NodeFilter,
            prototype: [typeof NodeFilter.prototype, 'prototype' in NodeFilter],
            tag: Object.prototype.toString.call(NodeFilter),
            name: [NodeFilter.name, flags('name')],
            length: [NodeFilter.length, flags('length')],
            constants: [NodeFilter.SHOW_ALL, NodeFilter.FILTER_ACCEPT, flags('SHOW_ALL')],
            constructed: constructed,
          };
        })()""")
    finally:
        (await browser.close())

    assert answer["kind"] == "function", answer
    assert answer["prototype"] == ["undefined", False], answer
    assert answer["tag"] == "[object Function]", answer
    #: A function's own ``name``/``length``, non-enumerable and settable, as V8 makes them.
    assert answer["name"] == ["NodeFilter", [False, False, True]], answer
    assert answer["length"] == [0, [False, False, True]], answer
    #: The constants stay readable and enumerable, and a page still cannot build one.
    assert answer["constants"] == [4294967295, 1, [False, True, False]], answer
    assert answer["constructed"] == "TypeError", answer


async def test_a_namespace_is_an_object_without_a_prototype():
    """``CSS`` is a namespace, so Chrome's shape for it is a plain tagged object.

    ``SetupIDLNamespaceTemplate`` tags the object itself and gives it no ``prototype``, ``name``
    or ``length``, which is what the six ``GPU*`` namespaces answer too.  The kind comes from
    ``WrapperTypeInfo::kIdlNamespace`` in the document, so no class declares it -- and one word
    rather than two flags is what keeps this shape apart from a callback interface, which has no
    prototype either (a namespace that read as both would install neither correctly).
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://surface.test/", wait_until="commit"))
        answer = page.evaluate("""(() => ({
          kind: typeof CSS,
          prototype: 'prototype' in CSS,
          tag: Object.prototype.toString.call(CSS),
          own: [typeof CSS.name, typeof CSS.length],
          member: typeof CSS.supports,
        }))()""")
    finally:
        (await browser.close())

    assert answer["kind"] == "object", answer
    assert answer["prototype"] is False, answer
    assert answer["tag"] == "[object CSS]", answer
    assert answer["own"] == ["undefined", "undefined"], answer
    assert answer["member"] == "function", answer
