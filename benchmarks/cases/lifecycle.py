"""Page lifetime / leak acceptance, runnable as a worker or standalone.

Tracks page, window and native Context retention across repeated create/close
cycles with tracemalloc and weakrefs; retained objects are an error.
"""

import argparse
import ctypes
import gc
import hashlib
import json
import sys
import tracemalloc
import weakref
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "pyv8c")]

CASE_NAMES = ("pages",)


def collect():
    from pyv8c import pyv8

    for _ in range(3):
        pyv8._pyv8.low_memory_notification()
        gc.collect()


def private_bytes():
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_working", "working", "peak_paged", "paged",
                    "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.private
    if sys.platform.startswith("linux"):
        return sum(int(line.split()[1]) * 1024
                   for line in Path("/proc/self/smaps_rollup").read_text().splitlines()
                   if line.startswith(("Private_Clean:", "Private_Dirty:")))
    return None


async def run_case(name="pages", count=1000, *, report=None):
    """Unified worker entry. Returns a summary; a full sample log goes into *report*."""
    from pyv8c import pyv8
    from pyv8c.async_api import Browser

    if name not in CASE_NAMES or count < 1:
        raise ValueError("unknown lifecycle workload or nonpositive count")
    binary = Path(pyv8._pyv8.__file__)
    report = report if report is not None else {
        "python": sys.version, "binary": str(binary),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "samples": []}
    browser = await Browser(stub_logging="off").__aenter__()
    owner = browser.new_profile()
    references = {name: [] for name in ("page", "window")}
    context_ids = set()
    try:
        for _ in range(20):
            page = owner.new_page()
            await page.close()
        del page
        collect()
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        report["private_bytes_baseline"] = private_bytes()
        for index in range(1, count + 1):
            page = owner.new_page()
            context_ids.add(id(page.v8_context))
            for label, value in (("page", page), ("window", page.window)):
                references[label].append(weakref.ref(value))
            del value
            await page.close()
            del page
            if index % 100 == 0 or index == count:
                collect()
                retained = {label: sum(ref() is not None for ref in refs)
                            for label, refs in references.items()}
                # Native Context is GC-tracked but does not support weakrefs.
                retained["context"] = sum(type(obj) is pyv8.Context and id(obj) in context_ids
                                          for obj in gc.get_objects())
                context_ids.clear()
                for refs in references.values():
                    refs.clear()
                sample = {"created": index, "retained": retained,
                          "python_bytes_delta": tracemalloc.get_traced_memory()[0] - baseline,
                          "private_bytes": private_bytes()}
                report["samples"].append(sample)
                assert not any(retained.values()), sample
    finally:
        await browser.close()
        tracemalloc.stop()
    peak = max((s["python_bytes_delta"] for s in report["samples"]), default=0)
    return {
        "median": float(peak) / 1024, "min": float(peak) / 1024, "unit": "KiB",
        "detail": f"page create/close lifecycle, {count} cycles, 0 retained",
        "workload": f"lifecycle-pages-v1:{count}",
        "cycles": count, "python_bytes_delta_peak": peak,
        "samples": report["samples"], "report": report,
    }


async def _main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=CASE_NAMES, default="pages")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("count must be positive")
    result = await run_case(args.case, args.count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result["report"], indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("report", "samples")}))


if __name__ == "__main__":
    from pyv8c.async_api import run
    run(_main())
