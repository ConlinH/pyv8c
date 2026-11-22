import copy
import json
from pathlib import Path

import pytest
from pyv8c import pyv8

from pyv8c.async_api import Browser
from pyv8c.engine.idl import DEFAULT_IDL_PATH, IDL_VERSION, Idl, PACKAGE_ROOT, idl_path


def _document():
    """The bundled document as plain data, whole: these tests mutate it and write it back."""
    idl = Idl.load(profile=None)
    return {"idl": dict(idl.metadata), "interfaces": copy.deepcopy(dict(idl.interfaces))}


def _baseline_document():
    """The interfaces of the bundled document: what most of these tests mutate."""
    return _document()["interfaces"]


def _custom_baseline(path):
    document = _document()
    interfaces = document["interfaces"]
    interfaces["DOMQuad"]["parent"] = "EventTarget"
    interfaces["DOMQuad"]["tag"] = "VersionedDOMQuad"
    interfaces["DOMQuad"]["constructor"]["length"] = 2
    members = interfaces["DOMQuad"]["members"]["prototype"]
    bounds = next(member for member in members if member["name"] == "getBounds")
    members.remove(bounds)
    replacement = copy.deepcopy(bounds)
    replacement["name"] = "alternateGetBounds"
    members.append(replacement)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return document


async def test_default_baseline_is_bundled_and_describes_snapshot():
    chosen = Path(idl_path()).resolve()
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        assert browser.idl_path == f"pyv8c:{chosen.relative_to(PACKAGE_ROOT).as_posix()}"
        loaded = Idl.load(chosen, profile=None)
        assert browser.idl_info["idl"]["chromium_version"] == loaded.metadata["chromium_version"]
        info = browser.idl_info
        assert info["interface_count"] > 1000
        assert len(info["sha256"]) == 64
        assert info["idl"]["version"] == IDL_VERSION
    finally:
        (await browser.close())


async def test_custom_baseline_path_is_absolute_and_snapshot_is_frozen(tmp_path, monkeypatch):
    baseline = tmp_path / "custom.json"
    document = _custom_baseline(baseline)
    monkeypatch.chdir(tmp_path)
    browser = (await Browser(idl_path="custom.json", stub_logging="off").__aenter__())
    original_hash = browser.idl_info["sha256"]
    try:
        assert browser.idl_path == str(baseline.resolve())
        document["idl"]["note"] = "changed-after-browser-creation"
        baseline.write_text(json.dumps(document), encoding="utf-8")
        assert browser.idl_info["sha256"] == original_hash
        assert "alternateGetBounds" in {
            member["name"]
            for member in browser.bindings.idl.interfaces["DOMQuad"]["members"]["prototype"]
        }
        newer = (await Browser(idl_path=baseline, stub_logging="off").__aenter__())
        try:
            assert newer.idl_info["sha256"] != original_hash
        finally:
            (await newer.close())
    finally:
        (await browser.close())


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("[]", "root must be an object"),
        ("{}", "must state an 'idl' object and an 'interfaces' object"),
        ('{"idl": {"version": %d}, "interfaces": {}}' % IDL_VERSION,
         "must contain interfaces"),
    ],
)
async def test_invalid_baseline_is_rejected_when_browser_is_created(tmp_path, content, error):
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        (await Browser(idl_path=path).__aenter__())


async def test_missing_baseline_is_rejected_when_browser_is_created(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        (await Browser(idl_path=tmp_path / "missing.json").__aenter__())


async def test_a_document_of_another_version_is_refused(tmp_path):
    document = _document()
    document["idl"]["version"] = 2
    path = tmp_path / "version-2.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported IDL version"):
        (await Browser(idl_path=path).__aenter__())


async def test_baseline_parent_cycle_is_rejected_when_browser_is_created(tmp_path):
    entry = lambda parent, tag: {
        "parent": parent, "members": {}, "constructor": {}, "tag": tag,
        "template": {}, "constants": [],
    }
    path = tmp_path / "cycle.json"
    path.write_text(json.dumps({
        "idl": {"version": IDL_VERSION},
        "interfaces": {"First": entry("Second", "First"), "Second": entry("First", "Second")},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="parent cycle"):
        (await Browser(idl_path=path).__aenter__())


async def test_two_browsers_keep_generated_classes_and_templates_isolated(tmp_path):
    custom = tmp_path / "custom.json"
    _custom_baseline(custom)
    default_browser = (await Browser(idl_path=DEFAULT_IDL_PATH, stub_logging="off").__aenter__())
    custom_browser = (await Browser(idl_path=custom, stub_logging="off").__aenter__())
    try:
        assert default_browser.bindings.class_for("DOMQuad") is not custom_browser.bindings.class_for("DOMQuad")
        # A hand-written implementation remains shared; this test isolates the
        # generated class and its per-baseline V8 template instead.
        assert default_browser.bindings.class_for("ReadableStream") is custom_browser.bindings.class_for("ReadableStream")
        default_page = default_browser.new_profile().new_page()
        custom_page = custom_browser.new_profile().new_page()
        assert default_page.evaluate("(() => { const stream = new ReadableStream(); const reader = stream.getReader(); return [stream.locked, reader instanceof ReadableStreamDefaultReader]; })()") == [True, True]
        assert default_page.evaluate("[typeof DOMQuad.prototype.getBounds, typeof DOMQuad.prototype.alternateGetBounds]") == ["function", "undefined"]
        assert custom_page.evaluate("[typeof DOMQuad.prototype.getBounds, typeof DOMQuad.prototype.alternateGetBounds]") == ["undefined", "function"]
        assert default_page.evaluate("Object.getPrototypeOf(DOMQuad.prototype) === Object.prototype") is True
        assert custom_page.evaluate("Object.getPrototypeOf(DOMQuad.prototype) === EventTarget.prototype") is True
        assert default_page.evaluate("[DOMQuad.length, Object.prototype.toString.call(new DOMQuad())]") == [0, "[object DOMQuad]"]
        assert custom_page.evaluate("[DOMQuad.length, Object.prototype.toString.call(new DOMQuad())]") == [2, "[object VersionedDOMQuad]"]

        (await custom_page.set_content("<main><div id='value'></div></main>", wait_until="commit"))
        assert custom_page.evaluate("(() => { const node=document.querySelector('#value'); return [node instanceof Node, node instanceof Element, document instanceof Document, document.querySelector('#value') === node]; })()") == [True, True, True, True]

        (await default_browser.close())
        assert custom_page.evaluate("new DOMQuad().alternateGetBounds()") is pyv8.Undefined
        assert custom_page.evaluate("typeof URL === 'function'") is True
    finally:
        (await default_browser.close())
        (await custom_browser.close())


async def test_baseline_signatures_do_not_control_implementation_arguments(tmp_path):
    document = _document()
    contains = next(
        member for member in document["interfaces"]["DOMTokenList"]["members"]["prototype"]
        if member["name"] == "contains"
    )
    contains.setdefault("signature", {})["required"] = 2
    path = tmp_path / "strict-contains.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    default_browser = (await Browser(stub_logging="off").__aenter__())
    strict_browser = (await Browser(idl_path=path, stub_logging="off").__aenter__())
    try:
        default_page = default_browser.new_profile().new_page()
        strict_page = strict_browser.new_profile().new_page()
        (await default_page.set_content("<body></body>", wait_until="commit"))
        (await strict_page.set_content("<body></body>", wait_until="commit"))
        assert default_page.evaluate("document.body.classList.contains('x')") is False
        assert strict_page.evaluate("document.body.classList.contains('x')") is False
        assert strict_page.evaluate("document.body.classList.contains('x', 'ignored')") is False
    finally:
        (await default_browser.close())
        (await strict_browser.close())


async def test_unresolved_signatures_do_not_prevent_structurally_valid_bindings(tmp_path):
    document = _document()
    contains = next(
        member for member in document["interfaces"]["DOMTokenList"]["members"]["prototype"]
        if member["name"] == "contains"
    )
    contains.setdefault("signature", {})["status"] = "unresolved"
    path = tmp_path / "unresolved-real.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    browser = (await Browser(idl_path=path, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("document.createElement('div').classList.contains('x')") is False
    finally:
        (await browser.close())

    document = _document()
    bounds = next(
        member for member in document["interfaces"]["DOMQuad"]["members"]["prototype"]
        if member["name"] == "getBounds"
    )
    bounds.setdefault("signature", {})["status"] = "unresolved"
    path = tmp_path / "unresolved-stub.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    browser = (await Browser(idl_path=path, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("new DOMQuad().getBounds(1, 2, 3)") is pyv8.Undefined
    finally:
        (await browser.close())


async def test_worker_and_iframe_reuse_their_browser_bindings(tmp_path):
    custom = tmp_path / "custom.json"
    _custom_baseline(custom)
    browser = (await Browser(
        idl_path=custom,
        resource_loader=lambda _url, _request: {
            "body": "", "headers": {"access-control-allow-origin": "*"},
        },
        stub_logging="off",
    ).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<iframe src='about:blank'></iframe>", url="http://example.test/", wait_until="commit"))
        (await page.run_until_idle(timeout=1))
        frame_page = next(iter(page._owned.frames))
        assert frame_page.profile.browser.bindings is browser.bindings
        assert frame_page.evaluate("[typeof DOMQuad.prototype.getBounds, typeof DOMQuad.prototype.alternateGetBounds]") == ["undefined", "function"]

        page.evaluate("""(() => {
          const source = new Blob([
            "postMessage([typeof DOMQuad.prototype.getBounds, typeof DOMQuad.prototype.alternateGetBounds]);"
          ], {type: "text/javascript"});
          const worker = new Worker(URL.createObjectURL(source));
          worker.onmessage = event => window.__baselineFromWorker = event.data;
        })()""")
        (await page.wait_for("typeof window.__baselineFromWorker !== 'undefined'", timeout=10))
        assert page.evaluate("__baselineFromWorker") == ["undefined", "function"]
    finally:
        (await browser.close())
