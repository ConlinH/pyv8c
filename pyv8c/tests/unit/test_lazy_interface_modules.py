"""Optional interface modules load only at their first observable use."""

import asyncio

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


async def test_optional_interface_modules_stay_unimported_until_first_use():
    """Use a child process because module imports are process-global caches."""
    pytest.importorskip("pyv8c.pyv8", reason="Runtime requires the native extension")
    code = textwrap.dedent('\nfrom pyv8c.async_api import Browser, run\nasync def _program():\n\n    import json\n    import os\n    import sys\n    from pyv8c.async_api import Browser, run\n\n    modules = {\n        "idb": "pyv8c.web.bom.indexeddb",\n        "navigator": "pyv8c.web.bom.navigator",\n        "websocket": "pyv8c.web.websocket",\n        "worker": "pyv8c.web.workers.worker",\n        "filesystem": "pyv8c.web.bom.filesystem",\n    }\n    browser = (await Browser(stub_logging="off", idl_path=os.environ.get("PYV8C_TEST_IDL_PATH")).__aenter__())\n    try:\n        page = browser.new_profile().new_page()\n        output = {"startup": {key: module in sys.modules for key, module in modules.items()}}\n        output["indexeddb_identity"] = page.evaluate("indexedDB === indexedDB")\n        output["after_idb"] = {key: module in sys.modules for key, module in modules.items()}\n        output["idb_type"] = page.evaluate("typeof IDBKeyRange")\n        output["navigator_identity"] = page.evaluate("navigator === navigator")\n        output["after_navigator"] = {key: module in sys.modules for key, module in modules.items()}\n        output["websocket_type"] = page.evaluate("typeof WebSocket")\n        output["after_websocket"] = {key: module in sys.modules for key, module in modules.items()}\n        output["worker_type"] = page.evaluate("typeof Worker")\n        output["after_worker"] = {key: module in sys.modules for key, module in modules.items()}\n        output["file_type"] = page.evaluate("typeof File")\n        output["after_file"] = {key: module in sys.modules for key, module in modules.items()}\n        output["idb_group"] = [\n            name for name in page.binding_session.materialized_interfaces\n            if name.startswith("IDB")\n        ]\n        print(json.dumps(output))\n    finally:\n        (await browser.close())\nrun(_program())\n')
    environment = dict(os.environ)
    environment["PYTHONPATH"] = (await asyncio.to_thread(os.pathsep.join, (str(ROOT.parent / "pyv8"), str(ROOT))))
    result = (await asyncio.to_thread(subprocess.run,
        [sys.executable, "-c", code], cwd=ROOT, env=environment,
        text=True, capture_output=True, timeout=30,
    ))
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    def loaded(idb=False, navigator=False, websocket=False, worker=False, filesystem=False):
        return {"idb": idb, "navigator": navigator, "websocket": websocket,
                "worker": worker, "filesystem": filesystem}

    assert output["startup"] == loaded()
    assert output["indexeddb_identity"] is True
    assert output["after_idb"] == loaded(idb=True)
    assert output["idb_type"] == "function"
    assert output["navigator_identity"] is True
    assert output["after_navigator"] == loaded(idb=True, navigator=True)
    assert output["websocket_type"] == "function"
    assert output["after_websocket"] == loaded(idb=True, navigator=True, websocket=True)
    assert output["worker_type"] == "function"
    assert output["after_worker"] == loaded(idb=True, navigator=True, websocket=True, worker=True)
    assert output["file_type"] == "function"
    assert output["after_file"] == loaded(idb=True, navigator=True, websocket=True, worker=True, filesystem=True)
    assert {"IDBFactory", "IDBKeyRange", "IDBDatabase", "IDBTransaction"}.issubset(output["idb_group"])


async def test_feature_runtime_loads_an_explicit_feature_once():
    pytest.importorskip("pyv8c.pyv8", reason="the interface decorator reaches the native extension")
    from types import SimpleNamespace

    from pyv8c.features.runtime import FeatureCatalog, FeatureImplementation, FeatureRuntime, FeatureSpec
    from pyv8c.idl import interface

    @interface("LazyUnitInterface", None)
    class LazyUnitInterface:
        pass

    calls = []

    def loader(module_name):
        assert module_name == "tests.lazy_unit"
        calls.append(True)
        return SimpleNamespace(FEATURE=FeatureImplementation({"LazyUnitInterface": LazyUnitInterface}))

    catalog = FeatureCatalog((FeatureSpec("unit", "tests.lazy_unit", ("LazyUnitInterface",)),))
    runtime = FeatureRuntime(catalog, importer=loader)

    assert runtime.interface("LazyUnitInterface") is LazyUnitInterface
    assert runtime.interface("LazyUnitInterface") is LazyUnitInterface
    assert calls == [True]
    assert runtime.implementations["LazyUnitInterface"] is LazyUnitInterface


async def test_feature_runtime_rejects_partial_exports_without_caching_them():
    from types import SimpleNamespace

    from pyv8c.features.runtime import FeatureCatalog, FeatureImplementation, FeatureRuntime, FeatureSpec

    runtime = FeatureRuntime(
        FeatureCatalog((FeatureSpec("broken", "tests.broken_feature", ("Expected",)),)),
        importer=lambda _: SimpleNamespace(FEATURE=FeatureImplementation({})),
    )

    with pytest.raises(RuntimeError, match="interfaces differ"):
        runtime.load("broken")
    assert not runtime.loaded_features
    assert not runtime.implementations


async def test_feature_runtime_reports_cycles_before_importing_a_module():
    from pyv8c.features.runtime import FeatureCatalog, FeatureRuntime, FeatureSpec

    runtime = FeatureRuntime(
        FeatureCatalog((
            FeatureSpec("first", "tests.first_feature", ("First",), requires=("second",)),
            FeatureSpec("second", "tests.second_feature", ("Second",), requires=("first",)),
        )),
        importer=lambda _: pytest.fail("cycle must be detected before module import"),
    )

    with pytest.raises(RuntimeError, match="first -> second -> first"):
        runtime.load("first")


async def test_lazy_value_store_caches_none_and_retries_failed_creation():
    from pyv8c.features.runtime import LazyValueStore

    class Values(LazyValueStore):
        def __init__(self):
            super().__init__()
            self.calls = []

        def _create_value(self, name):
            self.calls.append(name)
            if name == "retry" and self.calls.count(name) == 1:
                raise RuntimeError("creation failed")
            return None if name == "empty" else object()

    values = Values()
    assert values.value("empty") is None
    assert values.value("empty") is None
    assert values.calls == ["empty"]
    assert values.cached_names == {"empty"}

    with pytest.raises(RuntimeError, match="creation failed"):
        values.value("retry")
    assert "retry" not in values.cached_names
    created = values.value("retry")
    assert values.value("retry") is created
    assert values.calls == ["empty", "retry", "retry"]
