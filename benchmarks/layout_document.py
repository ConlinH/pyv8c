"""Measure Rust document layout with concurrent offline pages."""

import argparse
import asyncio
import ctypes
import json
import math
import platform
import sys
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, Thread
from time import perf_counter_ns, process_time_ns

from benchmarks.cases.layout import prepare
from pyv8c.async_api import Browser
from pyv8c.foundation.logging import logger


_TILE = ('<article class="tile"><p>text text text text</p><div class="flex">'
         '<b></b><i></i></div></article>')
_TEXT_PAGE = ('<!doctype html><style>body{margin:0} main{width:240px;display:grid;'
              'grid-template-columns:120px 120px} .tile{width:120px;height:60px} '
              'p{width:105px;height:20px;margin:0;font:16px/20px Arial} '
              '.flex{display:flex;width:120px;height:20px} b,i{display:block;width:20px;height:10px}'
              'b.wide{width:32px}</style><main>' + _TILE * 16 + '</main>')
_TEXT_SCRIPT = """(() => {
    const first = document.querySelector('b');
    const read = () => {
        let result = 0;
        for (const node of document.querySelectorAll('b,p,i')) result += node.getBoundingClientRect().width;
        return result;
    };
    first.className = '';
    const a = read();
    first.className = 'wide';
    return [a, read()];
})()"""


def private_memory():
    try:
        import psutil
    except ImportError:
        if sys.platform != 'win32':
            raise RuntimeError('install psutil to collect process memory on this platform')

        class Counters(ctypes.Structure):
            _fields_ = [
                ('cb', wintypes.DWORD), ('page_faults', wintypes.DWORD),
                ('peak_working_set', ctypes.c_size_t), ('working_set', ctypes.c_size_t),
                ('peak_paged_pool', ctypes.c_size_t), ('paged_pool', ctypes.c_size_t),
                ('peak_nonpaged_pool', ctypes.c_size_t), ('nonpaged_pool', ctypes.c_size_t),
                ('pagefile', ctypes.c_size_t), ('peak_pagefile', ctypes.c_size_t),
                ('private_usage', ctypes.c_size_t),
            ]

        counters = Counters(cb=ctypes.sizeof(Counters))
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not get_memory(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return float(counters.private_usage)
    return float(psutil.Process().memory_info().private)


async def setup(page, case):
    if case == 'text_nested':
        await page.set_content(_TEXT_PAGE, url='https://style.test/')
        return _TEXT_SCRIPT, [2320, 2332]
    return await prepare(page, case)


def measure(case, workers, iterations):
    launch = Lock()
    done = Event()
    clocks = {}
    memory = [private_memory()]

    def sample_memory():
        while not done.wait(0.01):
            memory.append(private_memory())

    def mark(label):
        clocks[label] = perf_counter_ns(), process_time_ns()

    start = Barrier(workers, timeout=60, action=lambda: mark('start'))
    finish = Barrier(workers, timeout=60, action=lambda: mark('finish'))

    async def worker():
        try:
            async with Browser(real_network=False, stub_logging='off') as browser:
                with launch:
                    page = browser.new_profile().new_page()
                source, expected = await setup(page, case)
                for _ in range(3):
                    assert page.evaluate(source) == expected
                start.wait()
                samples = []
                for _ in range(iterations):
                    begin = perf_counter_ns()
                    actual = page.evaluate(source)
                    samples.append(perf_counter_ns() - begin)
                    assert actual == expected, (case, actual, expected)
                finish.wait()
                return samples
        except BaseException:
            start.abort()
            finish.abort()
            raise

    monitor = Thread(target=sample_memory, daemon=True)
    monitor.start()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = [pool.submit(asyncio.run, worker()) for _ in range(workers)]
            samples = sorted(sample for job in jobs for sample in job.result())
    finally:
        done.set()
        monitor.join()
    elapsed = clocks['finish'][0] - clocks['start'][0]
    cpu = clocks['finish'][1] - clocks['start'][1]
    return {
        'total_batches_per_second': 1e9 * len(samples) / elapsed,
        'p95_ms': samples[math.ceil(0.95 * len(samples)) - 1] / 1e6,
        'process_cpu_ms_per_batch': cpu / len(samples) / 1e6,
        'average_cores': cpu / elapsed,
        'memory_initial_mib': memory[0] / 2**20,
        'memory_peak_mib': max(memory) / 2**20,
        'memory_after_mib': private_memory() / 2**20,
        'latencies_ns': samples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('threads', type=int, choices=(1, 2, 4))
    parser.add_argument('--iterations', type=int, default=40)
    parser.add_argument('--case', choices=('layout', 'text_nested', 'replace_block'), action='append')
    options = parser.parse_args()
    logger.remove()
    result = {
        'threads': options.threads,
        'iterations': options.iterations,
        'python': sys.version,
        'platform': platform.platform(),
        'gil_enabled': getattr(sys, '_is_gil_enabled', lambda: True)(),
        'cases': {case: measure(case, options.threads, options.iterations)
                  for case in (options.case or ('layout', 'text_nested', 'replace_block'))},
    }
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
