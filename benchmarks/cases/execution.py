"""Execution workloads run in fresh, sequential child processes by benchmark.py."""

from __future__ import annotations

import asyncio

import gc
import math
import statistics
import time

CASE_NAMES = (
    "dispatch",
    "routed",
    "evaluate",
    "result",
    "native",
    "script",
    "idle",
    "ua",
    "dom",
    "concurrent",
    "timer_load",
)


def summarize(samples_ns, elapsed_ns, cpu_ns):
    ordered = sorted(value / 1000 for value in samples_ns)
    count = len(ordered)
    if not count:
        raise ValueError("an execution sample must contain operations")
    window = max(1, count // 5)
    return {
        "median": elapsed_ns / count / 1000,
        "min": elapsed_ns / count / 1000,
        "unit": "us/op",
        "operations": count,
        "latency_min_us": ordered[0],
        "latency_median_us": statistics.median(ordered),
        "p90_us": ordered[max(0, math.ceil(count * 0.90) - 1)],
        "p99_us": ordered[max(0, math.ceil(count * 0.99) - 1)],
        "max_us": ordered[-1],
        "over_1ms": sum(value > 1000 for value in ordered),
        "over_100ms": sum(value > 100000 for value in ordered),
        "first_window_us": statistics.median(samples_ns[:window]) / 1000,
        "last_window_us": statistics.median(samples_ns[-window:]) / 1000,
        "cpu_ms": cpu_ns / 1e6,
        "latencies_ns": samples_ns,
    }


def measure_operations(operation, expected, iterations):
    samples = []
    objects = len(gc.get_objects())
    collections = [item["collections"] for item in gc.get_stats()]
    cpu = time.process_time_ns()
    started = time.perf_counter_ns()
    for _ in range(iterations):
        tick = time.perf_counter_ns()
        actual = (operation())
        samples.append(time.perf_counter_ns() - tick)
        if actual != expected:
            raise AssertionError(f"execution workload returned {actual!r}, expected {expected!r}")
    result = summarize(samples, time.perf_counter_ns() - started, time.process_time_ns() - cpu)
    result["tracked_objects_delta"] = len(gc.get_objects()) - objects
    result["gc_collections"] = [
        item["collections"] - before for item, before in zip(gc.get_stats(), collections)
    ]
    return result


async def measure_async_operations(operation, expected, iterations):
    samples = []
    cpu, started = time.process_time_ns(), time.perf_counter_ns()
    for _ in range(iterations):
        tick = time.perf_counter_ns()
        actual = await operation()
        samples.append(time.perf_counter_ns() - tick)
        if actual != expected:
            raise AssertionError(f"execution workload returned {actual!r}, expected {expected!r}")
    return summarize(samples, time.perf_counter_ns() - started, time.process_time_ns() - cpu)


async def measure_concurrent(operation, expected, iterations, workers=None):
    """Owner-loop coroutines, each yielding between individual calls."""
    workers = min(workers or 8, iterations)
    async def caller(count):
        samples = []
        for _ in range(count):
            tick = time.perf_counter_ns()
            actual = operation()
            await asyncio.sleep(0)
            samples.append((tick, time.perf_counter_ns() - tick))
            if actual != expected:
                raise AssertionError(f"concurrent workload returned {actual!r}, expected {expected!r}")
        return samples
    base, extra = divmod(iterations, workers)
    cpu, started = time.process_time_ns(), time.perf_counter_ns()
    batches = await asyncio.gather(*(caller(base + (index < extra)) for index in range(workers)))
    elapsed, cpu_elapsed = time.perf_counter_ns() - started, time.process_time_ns() - cpu
    samples = [duration for _, duration in sorted(sample for batch in batches for sample in batch)]
    result = summarize(samples, elapsed, cpu_elapsed)
    result['workers'] = workers
    return result


async def run_case(name, iterations=12000, gc_mode="default"):
    from pyv8c.foundation.executor import RealmExecutor
    from pyv8c.foundation.logging import logger

    from pyv8c import pyv8
    from pyv8c.async_api import Browser

    logger.remove()
    if name not in CASE_NAMES or iterations < 1:
        raise ValueError("unknown workload or nonpositive operation count")
    enabled = gc.isenabled()
    browser = driver = None
    try:
        if gc_mode == "disabled":
            gc.disable()
        if name == "dispatch":
            driver = RealmExecutor()
            operation = lambda: driver.call(lambda: 2, timeout=30)
            for _ in range(200):
                assert (operation()) == 2
            result = (measure_operations(operation, 2, iterations))
        else:
            browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
            page = browser.new_profile().new_page()
            (await page.set_content(
                "<!doctype html><html><body></body></html>", url="https://benchmark.test/"
            ))
            endpoint = page.realm.executor
            if name == "idle":
                # Observe after the owner has parked, including its native notification hub.
                endpoint.call(lambda: None)
                (await asyncio.sleep(0.05))
                cpu = time.process_time_ns()
                started = time.perf_counter_ns()
                (await asyncio.sleep(0.2))
                result = {
                    "median": (time.process_time_ns() - cpu) / 1e6,
                    "min": 0.0,
                    "unit": "cpu-ms",
                    "wall_ms": (time.perf_counter_ns() - started) / 1e6,
                }
            elif name in ("native", "script"):

                def owner_measure():
                    compiled = pyv8.Script(page.v8_context, "1+1")
                    operation = (
                        (lambda: page.v8_context.exec_js(compiled))
                        if name == "native"
                        else (lambda: page.scripts.evaluate("1+1"))
                    )
                    for _ in range(200):
                        assert (operation()) == 2
                    return (measure_operations(operation, 2, iterations))

                result = endpoint.call(owner_measure, timeout=60)
            elif name == "timer_load":
                # A live 1ms timer keeps the event loop busy while evaluate runs.
                page.evaluate(
                    "globalThis.timerCount=0;globalThis.timerId="
                    "setInterval(()=>timerCount++,1)"
                )
                operation = lambda: page.evaluate("1+1")
                for _ in range(200):
                    assert operation() == 2
                result = await measure_concurrent(operation, 2, iterations, workers=1)
                callbacks = page.evaluate("clearInterval(timerId);timerCount")
                assert callbacks > 0
                result["timer_callbacks"] = callbacks
            else:
                expected = {"value": 2, "items": [1, 2, 3]} if name == "result" else 2
                source = {"ua": "navigator.userAgent", "dom": "document.body.tagName"}.get(
                    name, "1+1"
                )
                if name in ("ua", "dom"):
                    expected = page.evaluate(source)
                    if not isinstance(expected, str) or not expected:
                        raise AssertionError(f"{name} did not return its expected string value")
                operation = (
                    (lambda: endpoint.call(lambda: 2, timeout=30))
                    if name == "routed"
                    else (
                        (lambda: page.evaluate("({value:2,items:[1,2,3]})"))
                        if name == "result"
                        else (lambda: page.evaluate(source))
                    )
                )
                for _ in range(200):
                    assert (operation()) == expected
                result = (
                    await measure_concurrent(operation, expected, iterations)
                    if name == "concurrent"
                    else (measure_operations(operation, expected, iterations))
                )
        result.update(
            detail=f"execution {name}; {iterations} operations; GC={gc_mode}",
            workload=f"execution-{name}-v2:{iterations}:{gc_mode}",
            gc_mode=gc_mode,
            warmup=200 if name != "idle" else 0,
        )
        if name == "concurrent":
            result["workload"] += f":workers={result['workers']}"
        return result
    finally:
        if browser is not None:
            (await browser.close())
        if driver is not None:
            driver.close()
        if enabled:
            gc.enable()
