"""Fresh-process startup and interface compilation, including deferred work."""

from __future__ import annotations

import sys
import time

CASE_NAMES = ("offline", "default", "compile")


def measure_fresh(name):
    """Time the entry import before creating the application loop."""
    started = time.perf_counter()
    if name == "compile":
        return _compile()
    from pyv8c.async_api import Browser, run
    imported = time.perf_counter()
    return run(run_case(name, _started=started, _imported=imported))


async def run_case(name, *, _started=None, _imported=None):
    if name not in CASE_NAMES:
        raise ValueError(name)
    if name == "compile":
        return _compile()
    started = time.perf_counter() if _started is None else _started
    from pyv8c.async_api import Browser, run

    imported = time.perf_counter() if _imported is None else _imported
    imported_modules = len(sys.modules)
    from pyv8c.foundation.logging import logger

    logger.remove()
    logging_ready = time.perf_counter()
    browser = (await Browser(real_network=name == "default", stub_logging="off").__aenter__())
    constructed = time.perf_counter()
    try:
        profile = browser.new_profile()
        profiled = time.perf_counter()
        page = profile.new_page()
        opened = time.perf_counter()
        (await page.set_content("<body>ready</body>", url="https://startup.test/"))
        ready = time.perf_counter()
        if page.evaluate("document.body.textContent") != "ready":
            raise RuntimeError("startup did not construct a usable document")
        if page.evaluate("document.defaultView === window") is not True:
            raise RuntimeError("startup did not bind the document to its realm")
        result = {
            "median": (ready - started) * 1000,
            "min": (ready - started) * 1000,
            "unit": "ms",
            "detail": f"fresh Browser import through ready local page; {name} network",
            "workload": f"fresh-startup-{name}-v1",
            "phases_ms": {
                "import": (imported - started) * 1000,
                "logging": (logging_ready - imported) * 1000,
                "browser": (constructed - logging_ready) * 1000,
                "profile": (profiled - constructed) * 1000,
                "first_page": (opened - profiled) * 1000,
                "content": (ready - opened) * 1000,
            },
            "modules_after_import": imported_modules,
            "modules_ready": len(sys.modules),
        }
    finally:
        closing = time.perf_counter()
        (await browser.close())
    result["close_ms"] = (time.perf_counter() - closing) * 1000
    return result


def _compile():
    # A fresh worker replaces manual clearing of an incomplete set of caches.
    from pyv8c.engine.bindings import Bindings
    from pyv8c.engine.idl import Idl
    from pyv8c.foundation.logging import logger
    from pyv8c.platform.bootstrap import bootstrap_runtime

    logger.remove()
    bindings = Bindings(Idl.load(), runtime=bootstrap_runtime(), stub_logging="off")
    names = bindings.idl.names()
    started = time.perf_counter()
    configs = [bindings.class_config_for(name) for name in names]
    elapsed = (time.perf_counter() - started) * 1000
    if len(configs) != len(names) or any(config is None for config in configs):
        raise RuntimeError("an interface was not compiled")
    return {
        "median": elapsed,
        "min": elapsed,
        "unit": "ms",
        "detail": f"{len(names)} interfaces compiled in a fresh process",
        "workload": "fresh-all-interfaces-v1",
        "interfaces": len(names),
        "compiled_misses": bindings._binder.compiled_misses,
    }
