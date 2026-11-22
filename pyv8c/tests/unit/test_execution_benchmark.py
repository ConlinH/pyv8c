"""Concurrent measurements count actual calls and surface worker failures."""

import threading

import pytest

from benchmarks.cases.execution import measure_concurrent


@pytest.mark.parametrize("iterations", [1, 7, 19])
async def test_concurrent_benchmark_measures_each_operation(iterations):
    lock = threading.Lock()
    calls = []

    def operation():
        with lock:
            calls.append(threading.get_ident())
        return 42

    result = (await measure_concurrent(operation, 42, iterations))
    assert len(calls) == result["operations"] == len(result["latencies_ns"]) == iterations
    assert result["workers"] == min(8, iterations)
    assert result["max_us"] >= result["latency_median_us"] >= result["latency_min_us"]


async def test_concurrent_benchmark_does_not_hide_an_incorrect_result():
    with pytest.raises(AssertionError, match="concurrent workload returned"):
        (await measure_concurrent(lambda: 41, 42, 9))
