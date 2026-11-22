"""Measurement backends for the unified benchmark driver.

Each module exposes ``CASE_NAMES`` and ``run_case(name, ...)`` and is invoked by
``benchmarks/benchmark.py`` in a fresh child process. Modules do not start the
V8 engine at import time.
"""
