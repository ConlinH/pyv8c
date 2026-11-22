"""Lazy interface materialisation on the global object.

The realm declares every interface the baseline exposes for it.  A cold Runtime
builds the eager core and the stubs its members reference; the rest is built when read.
"""
import pytest

pytest.importorskip("pyv8c.pyv8")
from pyv8c.async_api import Browser


async def test_cold_start_leaves_deferred_interfaces_for_first_access():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        session = page.binding_session
        materialized = set(session.materialized_interfaces)
        plan = browser.bindings.plan("Window")
        staged = set(plan.eager)
        deferred = set(plan.lazy)
        exposed = set(session.interface_names("Window"))

        assert staged
        # Staged seeds the realm; ancestors and alias targets are built with it.
        assert staged <= materialized
        assert deferred - materialized
        assert browser.startup_stats["deferred_globals"] == len(exposed)
    finally:
        (await browser.close())


async def test_the_plan_names_every_interface_startup_builds():
    """Startup builds the skeleton, its ancestors and the alias targets, nothing else."""
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        plan = browser.bindings.plan("Window")
        materialized = set(page.binding_session.materialized_interfaces)
        assert set(plan.ready) == materialized
        assert set(plan.eager) < set(plan.ready)
    finally:
        (await browser.close())


async def test_window_exposes_every_declared_interface_name():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        expected = set(page.binding_session.interface_names("Window"))
        exposed = set(page.evaluate("Object.getOwnPropertyNames(window)"))
        assert expected <= exposed
    finally:
        (await browser.close())


async def test_first_access_materialises_a_constructor():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        # XPathEvaluator has no hand-written implementation, so it can only
        # come from the baseline via lazy materialisation.
        assert page.evaluate("typeof XPathEvaluator") == "function"
        assert page.evaluate("XPathEvaluator.name") == "XPathEvaluator"
        assert page.evaluate(
            "Object.prototype.toString.call(XPathEvaluator.prototype)"
        ) == "[object XPathEvaluator]"
        # Ancestors are materialised alongside the requested interface.
        assert "EventTarget" in page.binding_session.materialized_interfaces
        assert "XPathEvaluator" in page.binding_session.materialized_interfaces
    finally:
        (await browser.close())


async def test_undetectable_callable_interface_materialises_safely():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        assert page.evaluate("typeof HTMLAllCollection") == "function"
        assert page.evaluate("HTMLAllCollection.prototype[Symbol.toStringTag]") == "HTMLAllCollection"
    finally:
        (await browser.close())


async def test_repeated_access_does_not_rematerialise():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        page.evaluate("typeof XPathEvaluator")
        after_first = page.binding_session.materialized_interfaces

        page.evaluate("typeof XPathEvaluator")
        assert page.binding_session.materialized_interfaces == after_first
        # The cached template keeps returning the same constructor.
        assert page.evaluate("XPathEvaluator === XPathEvaluator")
    finally:
        (await browser.close())


async def test_lazy_materialisation_resolves_native_constants():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        # Values come from the generated Chromium constants in the baseline.
        assert page.evaluate("GPUBufferUsage.COPY_SRC") == 4
        assert page.evaluate("GPUBufferUsage.COPY_DST") == 8
    finally:
        (await browser.close())


async def test_lazy_names_not_in_the_realm_resolve_to_undefined():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        names = set(page.binding_session.interface_names("Window"))
        assert names
        # A Worker-only interface must not appear as a Window constructor.
        worker_only = next(
            (name for name in page.binding_session.interface_names("Worker")
             if name not in names),
            None,
        )
        if worker_only is not None:
            assert page.evaluate("typeof " + worker_only) == "undefined"
    finally:
        (await browser.close())


async def test_materialisation_is_isolated_between_pages():
    browser = (await Browser(stub_logging="off").__aenter__())
    first = browser.new_profile().new_page()
    second = browser.new_profile().new_page()
    try:
        assert first.evaluate("typeof XPathEvaluator") == "function"
        assert "XPathEvaluator" in first.binding_session.materialized_interfaces
        assert "XPathEvaluator" not in second.binding_session.materialized_interfaces

        # The second page builds its own constructor for the same interface.
        assert second.evaluate("typeof XPathEvaluator") == "function"
        assert second.evaluate("XPathEvaluator") is not first.evaluate("XPathEvaluator")
    finally:
        (await browser.close())


async def test_eager_interfaces_still_work_after_lazy_declaration():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        (await page.set_content("<html><body><div id='app'>ready</div></body></html>", wait_until="commit"))
        assert page.evaluate("document.querySelector('#app').textContent") == "ready"
        (await page.run_until_idle())
        assert page.evaluate("document.readyState") == "complete"
        assert page.evaluate("new URL('https://example.test/a').pathname") == "/a"
        assert page.evaluate("new URLSearchParams('a=1').get('a')") == "1"
    finally:
        (await browser.close())


async def test_unknown_global_name_is_not_materialised():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        assert page.evaluate("typeof DefinitelyNotAnInterface") == "undefined"
    finally:
        (await browser.close())


async def test_interfaces_the_realm_never_exposes_are_declared_but_not_installed():
    """A realm knows more than it exposes: the name stays off the global object, and
    the realm can still answer for it when such an object arrives."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        bindings = browser.bindings
        # Implemented, in the baseline, but not exposed to Window: exactly the case the
        # realm has to answer for without the name ever being installed.
        assert "WebGLDebugRendererInfo" not in bindings.plan("Window").installs
        assert bindings.runtime.interface("WebGLDebugRendererInfo") is not None
        # The realm answers for it: an object of that interface still gets its overlay.
        assert bindings.describes("WebGLDebugRendererInfo") is True
        assert page.evaluate("[typeof WebGLDebugRendererInfo,"
                             " 'WebGLDebugRendererInfo' in window]") == ["undefined", False]
    finally:
        (await browser.close())


async def test_instances_marshalled_before_global_access_keep_mixin_members():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        # document.createTextNode() marshals the first Text/CharacterData
        # instance to JS without the realm ever reading the global `Text`
        # constructor first. The FunctionTemplate must be built from the
        # committed overlay, otherwise the ChildNode mixin members
        # (remove/before/...) silently disappear from the cached prototype.
        assert page.evaluate(
            "(() => { const t = document.createTextNode('x'); "
            "return [typeof t.remove, typeof Text.prototype.remove, "
            "typeof CharacterData.prototype.remove]; })()"
        ) == ["function", "function", "function"]
    finally:
        (await browser.close())


async def test_lazy_text_node_can_remove_itself_after_append():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        result = page.evaluate(
            "(() => { const span = document.createElement('span'); "
            "const t = document.createTextNode('x'); span.append(t); "
            "const before = span.childNodes.length; t.remove(); "
            "return [before, span.childNodes.length, span.textContent]; })()"
        )
        assert result == [1, 0, ""]
    finally:
        (await browser.close())
