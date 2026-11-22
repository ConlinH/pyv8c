"""Measure what building a Browser and its first page costs.


It prints the phases the startup stats and the binder report -- baseline, compile, the
first page, and how much of a second ``Bindings`` ``_COMPILED`` answered -- so a change to
``binder.py`` is judged by the numbers the plan it came from used.
"""
from __future__ import annotations


import argparse
import json
import time


from ..engine.idl import Idl
from ..engine.bindings import Bindings
from ..platform.bootstrap import bootstrap_runtime


def _compile_all(bindings):
    started = time.perf_counter()
    for name in bindings.idl.names():
        bindings.class_config_for(name)
    return (time.perf_counter() - started) * 1000


async def measure(pages=1):
    """Phase timings in milliseconds, and the member specs a page builds."""
    result = {}
    started = time.perf_counter()
    snapshot = Idl.load()
    result["baseline_ms"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    bindings = Bindings(snapshot, runtime=bootstrap_runtime(), stub_logging="off")
    result["bindings_ms"] = (time.perf_counter() - started) * 1000
    result["interfaces"] = len(bindings.idl.names())

    result["compile_all_ms"] = _compile_all(bindings)
    result["compiled_misses"] = bindings._binder.compiled_misses

    # A second bindings: the same baseline, profile and classes, so ``_COMPILED`` carries it.
    started = time.perf_counter()
    again = Bindings(snapshot, runtime=bootstrap_runtime(), stub_logging="off")
    result["bindings_again_ms"] = (time.perf_counter() - started) * 1000
    result["compile_again_ms"] = _compile_all(again)
    result["compiled_misses_again"] = again._binder.compiled_misses

    from ..async_api import Browser

    started = time.perf_counter()
    browser = await Browser(stub_logging="off").__aenter__()
    try:
        result["browser_ms"] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        for _ in range(pages):
            page = browser.new_profile().new_page()
            context_done = time.perf_counter()
            (await page.set_content("<body>x", url="https://measure.test/"))
        finished = time.perf_counter()
        # Split so the cost is attributable: ``new_page`` materializes the realm, ``set_content``
        # runs the parser and the load pipeline.
        result["new_page_ms"] = (context_done - started) * 1000
        result["set_content_ms"] = (finished - context_done) * 1000
        result["first_page_ms"] = (finished - started) * 1000
        result["startup_stats"] = getattr(browser, "_startup_stats", {})
    finally:
        await browser.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--json", metavar="PATH", help="write the measurements here")
    parser.add_argument("--quiet", action="store_true",
                        help="drop the logger's default sink, which the event dispatcher fills")
    args = parser.parse_args(argv)

    if args.quiet:
        from ..foundation.logging import logger

        logger.remove()
    if args.pages < 1:
        parser.error("--pages must be positive")
    from ..async_api import run
    result = run(measure(args.pages))
    for key, value in result.items():
        if isinstance(value, float):
            print(f"  {key:<24} {value:.1f} ms")
        else:
            print(f"  {key:<24} {value}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=1, sort_keys=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
