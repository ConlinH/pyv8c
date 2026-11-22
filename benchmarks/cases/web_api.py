"""Checked page workloads; profiling runs on the owner after timing finishes."""

from __future__ import annotations

import cProfile
import gc

from benchmarks.cases.execution import measure_operations

CASE_NAMES = (
    "window_getter",
    "navigator_getter",
    "attribute_read",
    "attribute_write",
    "attribute_same",
    "attribute_coercion",
    "observed_write",
    "id_lookup",
    "selector",
    "selector_classes",
    "selector_groups",
    "selector_class_groups",
    "selector_first",
    "mixed",
    "matches",
    "event",
)
NODES = 128


def workload(name, user_agent):
    count = 50 if name.startswith("selector") or name == "mixed" else 2000
    setup = ""
    if name == "window_getter":
        body, expected = "sum+=navigator.userAgent.length", len(user_agent) * count
    elif name == "navigator_getter":
        setup = "const nav=navigator;"
        body, expected = "sum+=nav.userAgent.length", len(user_agent) * count
    elif name == "attribute_read":
        body, expected = "sum+=target.getAttribute('data-x').length", 4 * count
    elif name in ("attribute_write", "attribute_same", "observed_write"):
        argument = "'seed'" if name == "attribute_same" else "String(i)"
        body, expected = f"target.setAttribute('data-x',{argument})", 0
    elif name == "attribute_coercion":
        setup = "let conversions=0;const key={toString(){conversions++;return 'data-x'}};"
        body, expected = "sum+=target.getAttribute(key).length", 4 * count
    elif name == "id_lookup":
        body = f"sum+=document.getElementById('r'+(i%{NODES})).id.length"
        expected = sum(len(f"r{i % NODES}") for i in range(count))
    elif name == "selector":
        body, expected = (
            "sum+=document.querySelectorAll('div.row > span.hit').length",
            NODES * count,
        )
    elif name == "selector_groups":
        body = "sum+=document.querySelectorAll('div.row > span.hit, div#r0').length"
        expected = (NODES + 1) * count
    elif name == "selector_classes":
        body, expected = "sum+=document.querySelectorAll('.row > .hit').length", NODES * count
    elif name == "selector_class_groups":
        body = "sum+=document.querySelectorAll('.row > .hit, #r0').length"
        expected = (NODES + 1) * count
    elif name == "selector_first":
        body, expected = (
            "sum+=document.querySelector('div.row > span.hit').tagName.length",
            4 * count,
        )
    elif name == "matches":
        setup = "const leaf=target.firstChild;"
        body, expected = "sum+=leaf.matches('div.row > span.hit') ? 1 : 0", count
    elif name == "event":
        setup = "const listener=()=>sum++;target.addEventListener('tick',listener);const event=new Event('tick');"
        body, expected = "target.dispatchEvent(event)", count
    elif name == "mixed":
        body = (
            f"const item=document.getElementById('r'+(i%{NODES}));"
            "item.setAttribute('data-count',String(i));"
            "sum+=document.querySelectorAll('.row > .hit').length;"
            "sum+=item.getAttribute('data-count').length"
        )
        expected = NODES * count + sum(len(str(i)) for i in range(count))
    else:
        raise ValueError("unknown Web API workload")

    if name == "observed_write":
        setup += "globalThis.batch={records:0};"
        returned = "return batch"
        expected = {"records": count}
    elif name == "attribute_coercion":
        returned = "return {sum,conversions}"
        expected = {"sum": expected, "conversions": count}
    elif name in ("attribute_write", "attribute_same"):
        returned = "return target.getAttribute('data-x')"
        expected = "seed" if name == "attribute_same" else str(count - 1)
    else:
        returned = "return sum"
    cleanup = "target.removeEventListener('tick',listener);" if name == "event" else ""
    source = (
        "(()=>{let sum=0;const target=document.getElementById('r0');"
        + setup
        + f"for(let i=0;i<{count};i++){{{body};}}"
        + cleanup
        + returned
        + ";})()"
    )
    return source, expected, count


async def run_case(name, iterations=20, profile_dir=None, gc_mode="default"):
    from pyv8c.foundation.logging import logger

    from pyv8c.async_api import Browser

    if name not in CASE_NAMES or iterations < 1 or gc_mode not in ("default", "disabled"):
        raise ValueError("unknown workload or nonpositive operation count")
    logger.remove()
    browser = None
    enabled = gc.isenabled()
    try:
        if gc_mode == "disabled":
            gc.disable()
        browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
        page = browser.new_profile().new_page()
        (await page.set_content(
            "<body>"
            + "".join(
                f'<div class="row" id="r{i}" data-x="seed"><span class="hit">x</span></div>'
                for i in range(NODES)
            )
            + "</body>",
            url="https://benchmark.test/",
        ))
        if name == "observed_write":
            page.evaluate(
                """globalThis.observer=new MutationObserver(records=>batch.records+=records.length);
                observer.observe(document.getElementById('r0'),{attributes:true});"""
            )
        source, expected, count = workload(name, page.evaluate("navigator.userAgent"))
        operation = lambda: page.evaluate(source)
        for _ in range(3):
            assert (operation()) == expected
        result = (measure_operations(operation, expected, iterations))
        result["median"] /= count
        result["min"] /= count
        result.update(
            unit="us/iteration",
            detail=f"Web API {name}; {iterations} batches of {count}",
            workload=f"web-{name}-v1:{iterations}:{count}:{NODES}:{gc_mode}",
            api_iterations=count,
            warmup=3,
            gc_mode=gc_mode,
        )
        if profile_dir is not None:
            from pathlib import Path

            destination = Path(profile_dir) / f"{name}.pstats"
            destination.parent.mkdir(parents=True, exist_ok=True)

            def profile():
                profiler = cProfile.Profile()
                profiler.enable()
                try:
                    return page._evaluate_data(source, None)
                finally:
                    profiler.disable()
                    profiler.dump_stats(destination)

            assert page.executor.call(profile) == expected
            result["profile_path"] = str(destination)
        return result
    finally:
        if browser is not None:
            (await browser.close())
        if enabled:
            gc.enable()
