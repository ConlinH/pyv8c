"""Synchronous and asyncio entry points measured on the caller's thread.

Exposes ``CASE_NAMES`` and ``run_case`` for the unified driver, and stays runnable
standalone for focused sync-vs-async comparisons.
"""

import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "pyv8c")]

from benchmarks.cases.execution import measure_operations, summarize

CASE_NAMES = ("evaluate", "result", "ua", "dom", "wait")


def direct(page, case, iterations):
    source = {
        "result": "({value:2,items:[1,2,3]})",
        "ua": "navigator.userAgent",
        "dom": "document.body.tagName",
    }.get(case, "1+1")
    expected = page.evaluate(source)
    for _ in range(200):
        assert page.evaluate(source) == expected
    return measure_operations(lambda: page.evaluate(source), expected, iterations)


def sync_measure(case, iterations):
    from pyv8c.sync_api import Browser

    with Browser(real_network=False) as browser:
        page = browser.new_page()
        page.set_content("<body></body>", "https://benchmark.test/")
        page.run_until_idle()
        if case == "wait":
            result = measure_operations(lambda: page.wait_for("true", timeout=1), None, iterations)
        else:
            result = direct(page, case, iterations)
        result["execution_thread"] = browser._impl._runtime.owner
        return result


async def async_measure(case, iterations):
    from pyv8c.async_api import Browser

    async with Browser(real_network=False) as browser:
        page = browser.new_page()
        await page.set_content("<body></body>", "https://benchmark.test/")
        await page.run_until_idle()
        if case == "wait":
            for _ in range(200):
                await page.wait_for("true", timeout=1)
            samples = []
            started, cpu = time.perf_counter_ns(), time.process_time_ns()
            for _ in range(iterations):
                tick = time.perf_counter_ns()
                await page.wait_for("true", timeout=1)
                samples.append(time.perf_counter_ns() - tick)
            result = summarize(samples, time.perf_counter_ns() - started, time.process_time_ns() - cpu)
        else:
            result = direct(page, case, iterations)
        result["execution_thread"] = browser._runtime.owner
        return result


async def run_case(name, iterations=12000, api="sync"):
    """Unified worker entry used by benchmark.py (one measurement per child process)."""
    from pyv8c.foundation.logging import logger

    logger.remove()
    if name not in CASE_NAMES or iterations < 1:
        raise ValueError("unknown entrypoint workload or nonpositive operation count")
    if api not in ("sync", "async"):
        raise ValueError("unknown entrypoint API")
    result = (sync_measure(name, iterations) if api == "sync"
              else await async_measure(name, iterations))
    result["execution_thread"] = int(result["execution_thread"])
    result.update(
        api=api,
        detail=f"entrypoint {api} {name}; {iterations} operations",
        workload=f"entrypoints-{name}-v2:{iterations}:{api}",
        caller_thread=threading.get_ident(),
    )
    assert result["execution_thread"] == result["caller_thread"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", choices=("sync", "async"), required=True)
    parser.add_argument("--case", choices=CASE_NAMES, default="evaluate")
    parser.add_argument("--iterations", type=int, default=12000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    from pyv8c.foundation.logging import logger

    from pyv8c.async_api import run
    from pyv8c import pyv8
    logger.remove()
    result = (sync_measure(args.case, args.iterations) if args.api == "sync"
              else run(async_measure(args.case, args.iterations)))
    result.update(api=args.api, case=args.case, iterations=args.iterations,
                  caller_thread=threading.get_ident(),
                  workload=f"entrypoints-{args.case}-v2:{args.iterations}:{args.api}",
                  native_sha256=hashlib.sha256(Path(pyv8._pyv8.__file__).read_bytes()).hexdigest())
    assert result["execution_thread"] == result["caller_thread"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "latencies_ns"}))


if __name__ == "__main__":
    main()
