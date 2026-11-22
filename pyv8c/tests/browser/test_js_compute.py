"""Compute workloads validate results independently from the engine being timed."""

import math

import pytest

from benchmarks.cases.js_compute import CASE_NAMES, WARMUP, run_case, workload


@pytest.mark.parametrize("name", CASE_NAMES)
async def test_compute_workloads_match_python_reference(page, name):
    setup, source, expected = workload(name, loops=500)
    if setup:
        page.evaluate(setup)
    value = page.evaluate(source)
    assert isinstance(value, (int, float))
    assert math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-10)


@pytest.mark.parametrize("query", ["current_context", "incumbent_context"])
@pytest.mark.parametrize("case", ["integer_retained", "float_retained"])
@pytest.mark.parametrize("warmup", [0, WARMUP])
async def test_raw_query_benchmarks_check_every_execution(monkeypatch, query, case, warmup):
    from pyv8c import pyv8

    original = getattr(pyv8, query)
    observed = []

    def inspect():
        value = original()
        observed.append(value)
        return value

    monkeypatch.setattr(pyv8, query, inspect)
    engine = "raw_current" if query == "current_context" else "raw_incumbent"
    result = await run_case(case, engine, iterations=3, loops=500, warmup=warmup)
    assert observed == [None] * (1 + warmup + 3)
    assert result["warmup"] == warmup
    assert result["runtime"]["context_query"] == query
    assert result["workload"].endswith(f":query={query}")
