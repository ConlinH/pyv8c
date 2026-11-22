"""Separate JS arithmetic, transcendental functions, tiering and host dispatch."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

CASE_NAMES = (
    "trig_fresh",
    "trig_retained",
    "hoisted_trig_fresh",
    "hoisted_trig_retained",
    "integer_fresh",
    "integer_retained",
    "float_fresh",
    "float_retained",
)
LOOPS = 200_000
WARMUP = 10
ENGINE_NAMES = ("pyv8c", "raw", "raw_current", "raw_incumbent", "raw_owned")


def workload(name, loops=LOOPS):
    if name not in CASE_NAMES or loops < 1:
        raise ValueError("unknown JS workload or nonpositive loop count")
    kind, lifetime = name.rsplit("_", 1)
    if kind in ("trig", "hoisted_trig"):
        body = "let s=0;"
        if kind == "hoisted_trig":
            body += "const sin=Math.sin,cos=Math.cos;"
            expression = "sin(i)*cos(i)"
        else:
            expression = "Math.sin(i)*Math.cos(i)"
        body += f"for(let i=0;i<n;i++)s+={expression};return s"
        expected = sum(math.sin(i) * math.cos(i) for i in range(loops))
    elif kind == "integer":
        body = "let s=1;for(let i=0;i<n;i++)s=(s*1664525+1013904223)|0;return s"
        expected = 1
        for _ in range(loops):
            expected = (expected * 1664525 + 1013904223) & 0xFFFFFFFF
        if expected >= 0x80000000:
            expected -= 0x100000000
    else:
        body = "let s=0;for(let i=0;i<n;i++)s=(s+(i&255)*.0001)*1.00000001;return s"
        expected = 0.0
        for i in range(loops):
            expected = (expected + (i & 255) * 0.0001) * 1.00000001
    if lifetime == "retained":
        setup = f"function compute(n){{{body}}}"
        source = f"compute({loops})"
    else:
        setup = ""
        source = f"(()=>{{const n={loops};{body}}})()"
    return setup, source, expected


@asynccontextmanager
async def _engine(name, trace=False):
    from pyv8c import pyv8

    if trace:
        pyv8.configure(params=["--trace-opt", "--trace-deopt"])
    binary = Path(pyv8._pyv8.__file__)
    identity = {
        "engine": name,
        "v8": pyv8.v8_version(),
        "binaries": {binary.name: hashlib.sha256(binary.read_bytes()).hexdigest()},
    }
    if name in ("raw", "raw_current", "raw_incumbent", "raw_owned"):
        isolate = pyv8.Isolate(thread_bound=True) if name == "raw_owned" else None
        context = pyv8.Context(object(), **({"isolate": isolate} if isolate is not None else {}))
        scripts = {}
        query_name = {
            "raw_current": "current_context",
            "raw_incumbent": "incumbent_context",
        }.get(name)
        query = getattr(pyv8, query_name) if query_name else None
        identity["context_query"] = query_name

        def evaluate(source):
            if query is not None and query() is not None:
                raise AssertionError("an idle context query reported an active realm")
            if source not in scripts:
                scripts[source] = pyv8.Script(context, source)
            return context.exec_js(scripts[source])

        try:
            yield evaluate, identity
        finally:
            scripts.clear()
            context.close()
            if isolate is not None:
                isolate.close()
        return
    from pyv8c.foundation.logging import logger

    from pyv8c.async_api import Browser, run

    logger.remove()
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        yield page.evaluate, identity
    finally:
        (await browser.close())


async def run_case(name, engine="pyv8c", iterations=20, *, loops=LOOPS, trace=False, warmup=WARMUP):
    if engine not in ENGINE_NAMES or iterations < 1 or warmup < 0:
        raise ValueError("unknown JS engine, nonpositive samples or negative warmup")
    setup, source, expected = workload(name, loops)
    async with _engine(engine, trace) as (evaluate, identity):
        if setup:
            evaluate(setup)

        def check(value):
            if not isinstance(value, (int, float)) or not math.isclose(
                value, expected, rel_tol=1e-12, abs_tol=1e-10
            ):
                raise AssertionError(f"JS workload returned {value!r}; expected {expected!r}")

        for _ in range(warmup):
            check(evaluate(source))
        samples = []
        for _ in range(iterations):
            started = time.perf_counter_ns()
            value = evaluate(source)
            samples.append(time.perf_counter_ns() - started)
            check(value)
    query_suffix = f":query={identity['context_query']}" if identity.get("context_query") else ""
    return {
        "median": statistics.median(samples) / 1e6,
        "min": min(samples) / 1e6,
        "unit": "ms",
        "detail": f"{loops} JS loop iterations; {name}; {engine}",
        "workload": f"js-compute-{name}-v1:{loops}:{iterations}:{warmup}{query_suffix}",
        "samples_ns": samples,
        "loop_count": loops,
        "warmup": warmup,
        "checksum": value,
        "runtime": identity,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASE_NAMES, required=True)
    parser.add_argument("--engine", choices=ENGINE_NAMES, default="pyv8c")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=WARMUP)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    result = (await run_case(args.case, args.engine, args.iterations, trace=args.trace, warmup=args.warmup))
    result["python"] = sys.version
    print("JS_COMPUTE_RESULT " + json.dumps(result))


if __name__ == "__main__":
    from pyv8c.async_api import Browser, run
    run(main())
