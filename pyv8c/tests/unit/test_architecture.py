"""Guard the package layering introduced by the domain-package refactor.

One rule about errors lives with these: an error a realm can be shown is built where a
page could be the cause of one, which is ``web/``.  ``tests/unit/test_error_provenance.py``
holds it, with the registered exceptions and the reason each cannot reach a realm.
"""

import asyncio

import ast
import importlib
import re
from pathlib import Path

import pytest

import pyv8c

ROOT = Path(__file__).resolve().parents[2] / "pyv8c"
DOMAIN_PACKAGES = {"web", "web/dom", "web/css", "web/html", "web/svg", "web/mathml", "web/media", "web/graphics",
                   "web/bom", "web/fetch", "web/fileapi", "web/workers"}


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            yield node.level, node.module or ""


#: What the root package promises: the object model, the errors an application
#: catches, and the network extension points.  Everything else -- bundled
#: engines, web interface implementations, internal helpers -- belongs to the
#: module that owns it.
PUBLIC_SURFACE = {
    "__version__",
    "DevToolsHandle", "DevToolsOptions",
    "BrowserEnvironment",
    "HttpTransport", "WebSocketTransport", "NetworkBackend",
    "register_transport", "register_ws_transport", "transport_names",
    "NetworkRequest", "NetworkResponse", "NetworkUnavailableError",
    "PageError", "PageTypeError", "PageRangeError", "PageReferenceError", "PageSyntaxError",
    "PageURIError", "PageEvalError", "DOMException",
    "HostFailure",
    "RequestTimeout",
}


async def test_the_root_exports_only_the_public_surface():
    assert set(pyv8c.__all__) == PUBLIC_SURFACE
    # The root must remain importable for the standalone Chromium extractor;
    # resolving browser classes themselves requires the optional native pyv8.
    assert callable(pyv8c.__getattr__)
    for internal in ("CronetTransport", "CurlCffiTransport", "WebSocketsTransport"):
        with pytest.raises(AttributeError):
            getattr(pyv8c, internal)


async def test_core_has_no_relative_dependency_on_higher_layers():
    for path in (ROOT / "foundation").glob("*.py"):
        for level, module in _imports(path):
            assert not (level > 1 or module.startswith(("engine", "web", "platform"))), path


async def test_builtin_feature_composition_has_one_home():
    features = ROOT / "features"
    for name in ("__init__", "catalog", "core", "runtime"):
        assert (features / f"{name}.py").is_file()
        assert not (ROOT / "web" / "features" / f"{name}.py").exists()
    assert not (ROOT / "engine" / "features.py").exists()
    runtime = features / "runtime.py"
    assert not list(_imports(runtime)), "the runtime must not depend on project layers"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not name.name.startswith("pyv8c") for name in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("pyv8c")
    for path in features.glob("*.py"):
        for level, module in _imports(path):
            assert not (level == 2 and module.split('.')[0] == 'platform'), path
    catalog_source = (features / "catalog.py").read_text(encoding="utf-8")
    assert '"pyv8c.features.core"' in catalog_source
    assert "pyv8c.web.features" not in catalog_source


@pytest.mark.parametrize("module", ["pyv8c.features.runtime", "pyv8c.features.catalog"])
async def test_feature_catalog_import_does_not_load_web_implementations(module):
    import subprocess
    import sys

    script = (f"import sys; import {module}; "
              "assert not any(n == 'pyv8c.web' or n.startswith('pyv8c.web.') "
              "for n in sys.modules)")
    result = (await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script], capture_output=True, text=True, timeout=15))
    assert result.returncode == 0, result.stderr


async def test_domain_packages_do_not_depend_on_runtime_or_offline_tools():
    for package in DOMAIN_PACKAGES:
        for path in (ROOT / package).glob("*.py"):
            for _level, module in _imports(path):
                assert not module.startswith("platform"), path
                assert not module.startswith("tools"), path


#: Implementation modules the Feature runtime loads on first use, mapped to the
#: package they belong to and the relative level sibling features sit at.
OPTIONAL_FEATURES = {
    "web/bom/navigator.py": ("bom", 2),
    "web/bom/indexeddb.py": ("bom", 2),
    "web/bom/filesystem.py": ("bom", 2),
    "web/websocket.py": ("web", 1),
    "web/workers/worker.py": ("workers", 2),
    "web/workers/runtime.py": ("workers", 2),
}

#: Web packages every feature may build on at import time.  ``page_errors`` is the layer
#: that owns the error vocabulary, ``clone`` the value every channel and store crosses, and
#: ``fileapi`` the blob a navigator, a socket or a store hands back, so every feature may
#: name them.
SHARED_WEB_PACKAGES = ("clone", "css", "dom", "html", "svg", "mathml", "fetch", "fileapi",
                       "media", "graphics", "page_errors")


async def test_non_dom_web_apis_have_their_own_modules():
    web = ROOT / "web"
    for name in ("ai", "encoding", "permissions_policy"):
        assert (web / f"{name}.py").is_file(), name
        assert not (web / "dom" / f"{name}.py").exists(), name
    for package, module, old in (("media", "source", "media_source"),):
        assert (web / package / f"{module}.py").is_file()
        assert not (web / f"{old}.py").exists()
        assert not (web / "dom" / f"{old}.py").exists()
    assert (web / "graphics" / "webgl" / "context.py").is_file()
    assert not (web / "graphics" / "webgl.py").exists()
    assert (web / "bom" / "motion.py").is_file()
    assert not (web / "dom" / "motion.py").exists()


async def test_canvas_interfaces_live_in_graphics_without_html_dependency():
    from importlib.util import resolve_name
    web = ROOT / "web"
    tag = (web / "html" / "canvas.py").read_text(encoding="utf-8")
    canvas = web / "graphics" / "canvas"
    graphics = "\n".join(path.read_text(encoding="utf-8") for path in canvas.glob("*.py"))
    assert "class HTMLCanvasElement(" in tag
    for name in ("CanvasRenderingContext2D", "OffscreenCanvas", "ImageBitmap", "ImageData"):
        assert f"class {name}(" in graphics or f"class {name}:" in graphics
        assert f"class {name}(" not in tag and f"class {name}:" not in tag
    for path in (web / "graphics").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not (alias.name == "pyv8c.web.html" or
                                alias.name.startswith("pyv8c.web.html."))
                           for alias in node.names), path
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    package = 'pyv8c.' + '.'.join(path.relative_to(ROOT).parts[:-1])
                    module = resolve_name('.' * node.level + module, package)
                assert not (module == "pyv8c.web.html" or module.startswith("pyv8c.web.html.") or
                            (module == "pyv8c.web" and any(alias.name == "html" for alias in node.names))), path


async def test_css_has_one_home_and_no_layout_dependency():
    css = ROOT / "web" / "css"
    for name in ("parser", "properties", "values", "cascade", "selectors", "cssom",
                 "stylesheet", "supports", "rules", "typed", "colors"):
        assert (css / f"{name}.py").is_file(), name
    for name in ("css", "css_computed", "css_rules", "css_typed", "named_colors", "selectors"):
        assert not (ROOT / "web" / "dom" / f"{name}.py").exists(), name
    for path in css.glob("*.py"):
        for level, module in _imports(path):
            assert not (level == 2 and (module == "dom.layout" or module.startswith("dom.layout."))), path
    layout = ast.parse((ROOT / "web" / "dom" / "layout.py").read_text(encoding="utf-8"))
    definitions = {node.name for node in layout.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert not definitions.intersection({"_cascade", "_specificity", "document_rules", "computed_style"})


async def test_html_and_foreign_elements_have_separate_homes():
    web = ROOT / "web"
    for name in ("elements", "registry", "parser", "attributes", "forms", "inputs",
                 "iframe", "media", "canvas", "tag_interfaces"):
        assert (web / "html" / f"{name}.py").is_file(), name
    for name in ("html_elements", "html_classes", "html_form", "html_input", "html_iframe",
                 "html_canvas", "html_media", "html_props", "html_tag_interfaces", "html_svg",
                 "html_math", "parser"):
        assert not (web / "dom" / f"{name}.py").exists(), name
    assert "class Element(" in (web / "dom" / "element.py").read_text(encoding="utf-8")
    assert "class HTMLElement(" in (web / "html" / "elements.py").read_text(encoding="utf-8")
    for package in ("svg", "mathml"):
        assert (web / package / "elements.py").is_file()
        for path in (web / package).glob("*.py"):
            for level, module in _imports(path):
                assert not (level == 2 and (module == "html" or module.startswith("html."))), path


@pytest.mark.parametrize("module", ["pyv8c.web.html.forms", "pyv8c.web.html.registry",
                                    "pyv8c.web.html.parser", "pyv8c.web.css.selectors",
                                    "pyv8c.web.svg.elements", "pyv8c.web.mathml.elements",
                                    "pyv8c.web.graphics.canvas.context2d", "pyv8c.web.graphics.canvas.offscreen", "pyv8c.web.graphics.webgl.context",
                                    "pyv8c.web.media.source", "pyv8c.web.html.canvas"])
async def test_web_element_modules_import_independently(module):
    import subprocess
    import sys

    pytest.importorskip("pyv8c.pyv8", reason="the imported web modules require the native extension")
    result = (await asyncio.to_thread(subprocess.run, [sys.executable, "-c", f"import {module}"],
                            capture_output=True, text=True, timeout=15))
    assert result.returncode == 0, result.stderr

#: ``bom`` helper modules that hold no interface behind them.
SHARED_BOM_MODULES = ("bom.scheduling", "bom.utils")


async def test_optional_features_build_only_on_shared_layers():
    """Loading one optional feature must not drag in another one.

    A module-body import of a sibling feature would defeat the on-demand
    contract in docs/pyv8c/architecture.md; its runtime half is the ``sys.modules``
    probe in tests/unit/test_lazy_interface_modules.py.  Only import-time
    imports are inspected: a call body may reach a sibling feature lazily.

    ``dom`` and ``fetch`` are shared by every feature, but ``bom`` holds
    sibling features, so only the helper modules that carry no interface behind
    them may be named from it.
    """
    for relative, (own_package, sibling_level) in OPTIONAL_FEATURES.items():
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.level != sibling_level:
                continue
            module = node.module or ""
            shared = module in SHARED_BOM_MODULES or module.split(".")[0] in SHARED_WEB_PACKAGES
            assert shared or module.split(".")[0] == own_package, (relative, module)


async def test_bindings_do_not_depend_on_runtime():
    """bindings must reach realm services through core, never through Page."""
    for path in (ROOT / "engine").glob("*.py"):
        for _level, module in _imports(path):
            assert not module.startswith("platform"), path
        source = path.read_text(encoding="utf-8")
        assert "_make_promise" not in source, path
        assert "_js_error_value" not in source, path


async def test_no_implicit_current_page_state_remains():
    forbidden = ("current_page", "set_current_page", "_current_page")
    for path in ROOT.rglob("*.py"):
        assert not any(token in path.read_text(encoding="utf-8") for token in forbidden), path


async def test_offline_scripts_do_not_import_browser_runtime():
    for path in (ROOT / "tools").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "platform.browser" not in source
        assert "from pyv8c import Browser" not in source


async def test_every_offline_script_has_a_repository_wrapper():
    """Every tool under tools/ forwards to its packaged implementation."""
    pytest.importorskip("pyv8c.pyv8", reason="imports every tool, and two of them reach the extension")
    for path in (ROOT / "tools").glob("*.py"):
        if path.name.startswith("__"):
            continue
        implementation = importlib.import_module(f"pyv8c.tools.{path.stem}")
        shell = importlib.import_module(f"tools.{path.stem}")
        assert shell.main is implementation.main, path


async def test_the_idl_and_the_binder_each_have_one_home():
    bindings_source = (ROOT / "engine" / "bindings.py").read_text(encoding="utf-8")
    extractor_source = (ROOT / "tools" / "extract_idl.py").read_text(encoding="utf-8")
    idl_source = (ROOT / "engine" / "idl.py").read_text(encoding="utf-8")
    binder_source = (ROOT / "engine" / "binder.py").read_text(encoding="utf-8")
    # One collection: the document, the profile that narrows it, and the lookups over both.
    for declaration in ("class Idl", "class IdlInterface", "class IdlMember", "class Profile",
                        "class RealmPlan", "def write_idl"):
        assert declaration in idl_source, declaration
    # Turning it into native specs, and the shells and realm callbacks that go with it, are one
    # module, and ``Binder`` is the only concept in it.
    for declaration in ("class Binder", "def _parameters_for", "class _Callbacks", "class _Stubs",
                        "apply_template_flags"):
        assert declaration in binder_source, declaration
    # The modules those two replaced are gone, not re-exported.
    for gone in ("interface_table", "plan", "surface", "schema", "stubs", "wrappers", "config",
                 "sessions", "baseline", "assembly", "universe"):
        assert not (ROOT / "engine" / f"{gone}.py").exists(), gone
    assert "Idl" in bindings_source
    assert "class Binder" not in bindings_source
    # The offline extractor writes documents, so the writer, the version it writes and the two
    # value vocabularies it states have to stay reachable there -- one statement, so what the
    # document and the engine agree on changes in one place.
    assert "from ..engine.idl import DefinitionKind, IDL_VERSION, write_idl" in (
        extractor_source)


async def test_bindings_stay_a_composition_root():
    """Stub, callback and binder bodies must not creep back into bindings.py."""
    bindings_source = (ROOT / "engine" / "bindings.py").read_text(encoding="utf-8")
    for token in (
        "_stub_method", "_member_configs", "_class_config_for", "_declared_handlers",
        "class _Stubs", "class _Callbacks", "class ClassConfigBuilder",
        "class BindingSession", "def _stub_", "mark_direct",
    ):
        assert token not in bindings_source, token


async def test_binder_collaborators_use_public_surface_only():
    """The binder and a realm's session must not reach into bindings privates."""
    for path in (ROOT / "engine" / "binder.py", ROOT / "platform" / "sessions.py"):
        source = path.read_text(encoding="utf-8")
        assert "self.bindings._" not in source, path
        assert "bindings._binder" not in source, path


async def test_layering_has_no_upward_imports():
    forbidden = {
        "foundation": ("engine", "network", "web", "platform"),
        "engine": ("web", "platform"),
        "network": ("engine", "web", "platform"),
        "web": ("platform",),
    }
    for layer, names in forbidden.items():
        for path in (ROOT / layer).rglob("*.py"):
            parts = path.relative_to(ROOT).with_suffix("").parts
            package = ".".join(("pyv8c", *parts[:-1]))
            imports = _absolute_imports(path.read_text(encoding="utf-8"), package)
            for name in names:
                target = f"pyv8c.{name}"
                assert not any(module == target or module.startswith(target + ".")
                               for module in imports), path


def _absolute_imports(source, package):
    """Resolve static imports at all nesting levels, including imported names."""
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                    if node.level else node.module or "")
            modules.add(base)
            modules.update(f"{base}.{alias.name}" for alias in node.names)
    return modules


async def test_layer_import_analysis_resolves_nested_and_relative_imports():
    source = """\nfrom pyv8c import platform\ndef delayed():\n    from ... import platform\n    import pyv8c.web.dom\n"""
    assert {"pyv8c.platform", "pyv8c.web.dom"} <= _absolute_imports(source, "pyv8c.web.dom")


@pytest.mark.native
async def test_runtime_interface_catalog_is_frozen_after_bootstrap():
    pytest.importorskip("pyv8c.pyv8", reason="imports platform modules bound to the native extension")
    from pyv8c.platform.bootstrap import bootstrap_runtime

    runtime = bootstrap_runtime()
    assert "core" in runtime.loaded_features
    assert bootstrap_runtime() is not runtime


@pytest.mark.parametrize("attribute", ["_realm", "_services"])
@pytest.mark.parametrize("bundled", [False, True])
async def test_realm_of_returns_the_realm_capability(attribute, bundled):
    from types import SimpleNamespace

    from pyv8c.foundation.realm import realm_of

    capability = object()
    services = SimpleNamespace(realm_services=capability) if bundled else capability
    subject = SimpleNamespace(**{attribute: services})
    assert realm_of(subject) is capability


async def test_realm_of_prefers_injection_and_falls_back_to_services():
    from types import SimpleNamespace

    from pyv8c.foundation.realm import realm_of

    injected = object()
    fallback = object()
    subject = SimpleNamespace(_realm=injected, _services=SimpleNamespace(realm_services=fallback))
    assert realm_of(subject) is injected
    subject._realm = None
    assert realm_of(subject) is fallback
    assert realm_of(object()) is None


@pytest.mark.native
async def test_page_exposes_narrow_realm_services():
    pytest.importorskip("pyv8c.pyv8", reason="constructs a live Runtime")
    from pyv8c.async_api import Browser
    from pyv8c.foundation.realm import (
        ContextServices,
        DocumentServices,
        RealmServices,
        ResourceServices,
        TaskServices,
        WorkerServices,
    )

    browser = (await Browser(stub_logging="off", real_network=False).__aenter__())
    page = browser.new_profile().new_page()
    try:
        assert isinstance(page.services.context_services, ContextServices)
        assert isinstance(page.services.realm_services, RealmServices)
        assert isinstance(page.services.task_services, TaskServices)
        assert isinstance(page.services.resource_services, ResourceServices)
        assert isinstance(page.services.document_services, DocumentServices)
        assert isinstance(page.services.worker_services, WorkerServices)
        for protocol in (ContextServices, RealmServices, TaskServices, ResourceServices,
                         DocumentServices, WorkerServices):
            assert not isinstance(page.services, protocol)
        assert page.services.context_services.network_available is False
    finally:
        (await browser.close())


async def test_foundation_and_services_have_no_owner_backdoors():
    """TaskQueue must not reach the Page (or any host) through an owner field."""
    tasks_source = (ROOT / "foundation" / "tasks.py").read_text(encoding="utf-8")
    assert "self.owner" not in tasks_source
    assert "TaskHost" in tasks_source

    services_source = (ROOT / "platform" / "services.py").read_text(encoding="utf-8")
    assert "_tasks.owner" not in services_source
    assert "_host.owner" not in services_source


async def test_task_queue_exposes_event_loop_and_error_contract():
    from pyv8c.foundation.tasks import TaskQueue, TaskSource

    for member in ("run_turn", "next_deadline", "drain_task_errors"):
        assert hasattr(TaskQueue, member), member
    assert TaskSource.PARSER.value == "parser"


async def test_domain_code_uses_page_resource_ownership_api():
    #: The rule is about reaching the Page's fields, so it matches a member access or a
    #: quoted name: a bare word would also fire on the prose and the IDL vocabulary these
    #: modules legitimately carry.
    forbidden = re.compile(
        r"\b(?:self|page|owner|host)\.(profile|network_backend|binding_session|v8_context)\b"
        r"|\"(?:profile|network_backend|binding_session)\""
        r"|'(?:profile|network_backend|binding_session)'"
    )
    for path in (ROOT / "web").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        found = forbidden.search(source)
        assert found is None, (path, found.group(0))
        for token in (
            "_owned_websockets", "_owned_workers", "_owned_frames",
            "binding_configuration(", "resource_loader",
            "self._page", "self.page",
        ):
            assert token not in source, (path, token)


@pytest.mark.native
async def test_page_services_surface_is_structurally_narrow():
    """PageServices must not grow escape hatches into platform internals."""
    pytest.importorskip("pyv8c.pyv8", reason="constructs a live Runtime")
    from pyv8c.async_api import Browser

    source = (ROOT / "platform" / "services.py").read_text(encoding="utf-8")
    for token in ("self.profile", "self.network_backend", "self.binding_session", "self.page"):
        assert token not in source, token

    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        services = page.services
        for name in ("profile", "network_backend", "binding_session", "page", "realm", "v8_context", "browser"):
            assert not hasattr(services, name), name
    finally:
        (await browser.close())


@pytest.mark.native
async def test_page_keeps_only_user_facing_api():
    """Web-serving forwards must not creep back onto the Page facade."""
    pytest.importorskip("pyv8c.pyv8", reason="imports platform modules bound to the native extension")
    from pyv8c.platform.browser import Page

    for name in (
        "post_task", "schedule", "cancel", "begin_network", "complete_network",
        "begin_worker", "complete_worker", "make_promise",
        "promise_resolve", "install_inline_handler", "load_resource",
        "load_resource_async", "load_iframe", "load_worker_source_async",
        "websocket", "close_websocket", "network_available", "cookie_jar",
        "context_services", "binding_services", "devtools_server",
        "worker_configuration", "create_worker_realm", "new_child_page",
        "materialize_interface", "request_navigation",
        "element_class", "_node_adopted", "_parser_script_start",
    ):
        assert not hasattr(Page, name), name


@pytest.mark.native
async def test_page_net_exposes_both_http_flavours():
    """The host transport and the page's own JS APIs are one surface, under ``net``."""
    pytest.importorskip("pyv8c.pyv8", reason="imports platform modules bound to the native extension")
    from pyv8c.platform.net import PageNet
    from pyv8c.platform.page import Page

    for name in ("get", "post", "fetch", "xhr"):
        assert hasattr(PageNet, name), name
    import inspect
    assert all(inspect.iscoroutinefunction(getattr(PageNet, name)) for name in ("request", "get", "post", "fetch", "xhr"))
    assert not any(hasattr(PageNet, name) for name in ("request_async", "get_async", "post_async", "close"))
    assert inspect.iscoroutinefunction(PageNet.fetch)
    assert inspect.iscoroutinefunction(PageNet.xhr)
    assert not hasattr(PageNet, "fetch_async")
    assert not hasattr(PageNet, "xhr_async")
    for name in ("fetch", "xhr"):
        assert not hasattr(Page, name), f"{name} belongs to page.net"


async def test_web_capability_split_has_no_legacy_mixed_modules():
    assert not (ROOT / "web" / "fetch" / "backend.py").exists()
    assert not (ROOT / "web" / "fetch" / "api.py").exists()
    assert (ROOT / "web" / "workers" / "worker.py").is_file()
    assert not (ROOT / "web" / "workers" / "api.py").exists()
    assert not (ROOT / "network" / "composite.py").exists()
    # One cancellable delivery path only: no opt-in sibling or synchronous twin.
    assert not (ROOT / "network" / "curl_cffi_async.py").exists()
    assert "class AsyncTransport" in (ROOT / "network" / "async_delivery.py").read_text(encoding="utf-8")
    assert "class CookieJar" in (ROOT / "network" / "cookies.py").read_text(encoding="utf-8")
    assert "class CronetTransport" in (ROOT / "network" / "cronet.py").read_text(encoding="utf-8")
    assert "class NetworkBackend" in (ROOT / "network" / "backend.py").read_text(encoding="utf-8")
    assert "def assemble_backend" in (ROOT / "network" / "assembly.py").read_text(encoding="utf-8")
    assert "class WebSocketsTransport" in (ROOT / "network" / "websockets.py").read_text(encoding="utf-8")
    assert "class WorkerRuntime" in (ROOT / "web" / "workers" / "runtime.py").read_text(encoding="utf-8")
    assert "class HTMLMediaElement" in (ROOT / "web" / "html" / "media.py").read_text(encoding="utf-8")
    assert "class HTMLMediaElement" not in (ROOT / "web" / "html" / "elements.py").read_text(encoding="utf-8")


async def test_the_two_network_roles_stay_separate_contracts():
    """Each role is what an engine must implement to fill it, and no more."""
    from pyv8c.network.registry import HttpTransport, WebSocketTransport, transport_names

    assert not hasattr(HttpTransport, "websocket")
    assert not hasattr(WebSocketTransport, "request_sync")
    # cycronet's async session has no websocket(): naming it for the ws role
    # would recreate a second socket mechanism.
    assert "cronet" not in transport_names("ws")
    assert "websockets" in transport_names("ws")


def test_the_registry_registers_one_name_per_built_in_role():
    """A host may add probes at runtime, so read the static registry itself."""
    tree = ast.parse((ROOT / "network" / "registry.py").read_text(encoding="utf-8"))
    built_in = {"http": set(), "ws": set()}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("register_transport", "register_ws_transport")
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            role = "ws" if node.func.id == "register_ws_transport" else "http"
            built_in[role].add(node.args[0].value)
    assert built_in == {"http": {"curl_cffi", "cronet"}, "ws": {"websockets"}}
