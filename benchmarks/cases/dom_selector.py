"""Offline benchmark for batch CSS attribute matching."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Lock
from time import perf_counter_ns, process_time_ns

from benchmarks.cases.execution import summarize

CASE_NAMES = ("1", "2", "4")

ROWS = 4000


def markup():
    return "<main>" + "".join(
        f'<article id="row{i}" data-sku="store-{i}" data-path="/catalog/{i:05}/detail"'
        f' data-region="{"en-US" if i % 4 == 0 else "fr-FR"}"'
        f' data-status="{"public" if i % 3 == 0 else "private"}">row {i}</article>'
        for i in range(ROWS)
    ) + "</main>"


QUERY = """(() => {
    const nodes = document.querySelectorAll(
        'article[data-sku^="store-"][data-path$="/detail"][data-path*="/catalog/"][data-region|="en"][data-status="public"]');
    return [nodes.length, nodes[0].id, nodes[nodes.length - 1].id];
})()"""
EXPECTED = [(ROWS - 1) // 12 + 1, "row0", f"row{(ROWS - 1) // 12 * 12}"]


def run_case(name, iterations, workload="repeat"):
    from pyv8c.async_api import Browser
    from benchmarks.cases.lifecycle import private_bytes

    threads = int(name)
    warmup = 2 if workload == "replace" else 8
    origin = {}
    launch = Lock()
    loaded = Barrier(threads, timeout=60, action=lambda: origin.update(private_loaded=private_bytes()))

    def begin():
        origin["private_warm"] = private_bytes()
        origin.update(start=perf_counter_ns(), cpu_start=process_time_ns())

    def finish():
        origin.update(end=perf_counter_ns(), cpu_end=process_time_ns())
        origin["private_end"] = private_bytes()

    start = Barrier(threads, timeout=60, action=begin)
    end = Barrier(threads, timeout=60, action=finish)
    content = markup()
    prefix = {
        "repeat": "",
        "mutate": "target.setAttribute('data-status', ++iteration % 2 ? 'private' : 'public');",
        "replace": "container.innerHTML = replacement;",
    }[workload]
    source = prefix + QUERY
    alternate = [EXPECTED[0] - 1, "row12", EXPECTED[2]]

    async def worker():
        try:
            async with Browser(real_network=False, stub_logging="off") as browser:
                with launch:
                    page = browser.new_page()
                await page.set_content(content, url="https://selectors.test/", wait_until="load")
                if workload == "mutate":
                    page.evaluate("globalThis.target = document.getElementById('row0'); globalThis.iteration = 0;")
                elif workload == "replace":
                    page.evaluate("globalThis.container = document.querySelector('main'); globalThis.replacement = "
                                  + json.dumps(content[len("<main>"):-len("</main>")]))
                loaded.wait()
                for i in range(warmup):
                    assert page.evaluate(source) == (alternate if workload == "mutate" and i % 2 == 0 else EXPECTED)
                start.wait()
                times = []
                for i in range(iterations):
                    tick = perf_counter_ns()
                    result = page.evaluate(source)
                    times.append(perf_counter_ns() - tick)
                    assert result == (alternate if workload == "mutate" and i % 2 == 0 else EXPECTED)
                end.wait()
                return times
        except BaseException:
            loaded.abort()
            start.abort()
            end.abort()
            raise

    with ThreadPoolExecutor(max_workers=threads) as pool:
        jobs = [pool.submit(lambda: asyncio.run(worker())) for _ in range(threads)]
        samples = sorted(sample for job in jobs for sample in job.result())
    result = summarize(samples, origin["end"] - origin["start"], origin["cpu_end"] - origin["cpu_start"])
    result.update(unit="us/op", threads=threads, rows=ROWS, warmup=warmup, checksum=EXPECTED,
                  p95_us=samples[(95 * len(samples) + 99) // 100 - 1] / 1000,
                  batches_per_second=1_000_000 / result["median"],
                  detail=f"{threads} Browser threads, {ROWS} candidates, {workload}",
                  workload=f"dom-selector-batch-v2:{iterations}:{ROWS}:{threads}:{workload}",
                  private_loaded=origin["private_loaded"], private_warm=origin["private_warm"],
                  private_end=origin["private_end"])
    return result
