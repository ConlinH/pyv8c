"""A missing performance measurement must not be reported as a passing comparison."""
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def benchmark(monkeypatch):
    monkeypatch.setattr(sys, "path", sys.path.copy())
    path = Path(__file__).resolve().parents[3] / 'benchmarks/benchmark.py'
    spec = importlib.util.spec_from_file_location('audit_benchmark', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_unavailable_case_fails_comparison(benchmark, tmp_path, capsys):
    baseline = tmp_path / 'baseline.json'
    baseline.write_text('{"cases": {}}')
    benchmark.BASELINE_FILE = baseline
    assert benchmark.compare({'failed': (None, 'measurement failed')}) is False
    assert 'measurement failed' in capsys.readouterr().out


def test_comparison_rejects_a_different_python_version(benchmark, tmp_path, capsys):
    baseline = tmp_path / 'baseline.json'
    baseline.write_text('{"python":"0.0.0","cases":{}}')
    benchmark.BASELINE_FILE = baseline
    assert benchmark.compare({}) is False
    assert 'incomparable Python versions' in capsys.readouterr().out


async def test_measure_preserves_raw_samples(benchmark):
    samples = iter([3, 1, 2])
    async def sample():
        return {'median': next(samples), 'min': 0}
    result, error = (await benchmark.measure(sample, 3))
    assert error is None
    assert result['median'] == 2 and result['runs'] == 3
    assert [sample['median'] for sample in result['samples']] == [3, 1, 2]


async def test_nonpositive_runs_are_rejected(benchmark):
    with pytest.raises(SystemExit) as error:
        (await benchmark.main(['--runs', '0']))
    assert error.value.code == 2


async def test_js_worker_protocol_tolerates_native_library_banner(benchmark, monkeypatch, tmp_path):
    from types import SimpleNamespace

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        # A native SDK may print a startup banner before the JSON line.
        return SimpleNamespace(stdout='engine banner\nJS_COMPUTE_RESULT '
                               '{"median":1,"min":1,"unit":"ms","workload":"compute-v1","detail":"compute"}\n')

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    monkeypatch.setattr(benchmark, "measurement_identity", lambda: {})
    assert (await benchmark.main([
        "--cases", "javascript_float_fresh", "--javascript-engine", "raw", "--runs", "1",
        "--record", "--baseline", str(tmp_path / "before.json"),
    ])) == 0
    assert calls[0][-1] == "raw"


async def test_nonpositive_js_sample_count_is_rejected(benchmark):
    with pytest.raises(SystemExit):
        (await benchmark.main(["--javascript-iterations", "0"]))


async def test_negative_js_warmup_is_rejected(benchmark):
    with pytest.raises(SystemExit):
        (await benchmark.main(["--javascript-warmup", "-1"]))


def test_measurement_rejects_a_stale_dom_binary(benchmark, monkeypatch):
    pytest.importorskip("pyv8c.pyv8")
    from pyv8c._native import dom

    monkeypatch.setattr(dom, "SOURCE_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="rebuild _native"):
        benchmark.measurement_identity()


def test_measurement_rejects_a_different_native_build(benchmark):
    pytest.importorskip("pyv8c.pyv8")
    with pytest.raises(RuntimeError, match="binary and build record differ"):
        benchmark.measurement_identity(native_build_record={"binary_sha256": "0" * 64})


@pytest.mark.parametrize("record", [False, True])
async def test_failed_measurement_never_succeeds_without_a_baseline(benchmark, tmp_path, record):
    benchmark.BASELINE_FILE = tmp_path / 'missing.json'
    async def fail(): raise RuntimeError('measurement failed')
    benchmark.CASES = (('failed', fail),)
    assert (await benchmark.main(['--runs', '1', '--cases', 'failed'] + (['--record'] if record else []))) == 1


def test_execution_summary_keeps_tail_and_phase_information():
    from benchmarks.cases.execution import summarize

    result = summarize([1000] * 9 + [300_000_000], 300_009_000, 10_000_000)
    assert result['operations'] == 10
    assert result['p99_us'] == result['max_us'] == 300_000
    assert result['over_100ms'] == 1
    assert result['last_window_us'] > result['first_window_us']
    assert result['latencies_ns'][-1] == 300_000_000


def test_changed_workload_is_not_compared_or_marked_passed(benchmark, tmp_path, capsys):
    baseline = tmp_path / 'baseline.json'
    baseline.write_text('{"cases":{"query":{"median":10,"min":10}}}')
    benchmark.BASELINE_FILE = baseline
    result = {'median':1,'min':1,'unit':'ms','detail':'new','workload':'v2'}
    assert benchmark.compare({'query':(result,None)}) is False
    assert 'incomparable workload' in capsys.readouterr().out
    assert 'workload' not in baseline.read_text()


async def test_list_reports_all_cases_without_measuring(benchmark, capsys):
    assert (await benchmark.main(['--list'])) == 0
    names = capsys.readouterr().out.split()
    assert 'evaluate' in names
    assert 'execution_timer_load' in names
    assert 'entrypoint_sync_evaluate' in names
    assert 'entrypoint_async_evaluate' in names
    assert 'lifecycle_pages' in names
    assert len(names) == len(set(names))


async def test_iv8_engine_is_no_longer_accepted(benchmark):
    with pytest.raises(SystemExit):
        await benchmark.main(['--javascript-engine', 'iv8', '--runs', '1'])


def test_recorded_baseline_drops_raw_sample_arrays(benchmark, tmp_path):
    """The baseline on disk must keep only aggregate fields compare() reads."""
    import json

    baseline = tmp_path / 'baseline.json'
    benchmark.BASELINE_FILE = baseline
    full = {
        'median': 12.5, 'min': 12.0, 'unit': 'ms/op', 'detail': 'x',
        'workload': 'v1', 'runs': 3,
        'latencies_ns': list(range(1000)),
        'samples': [{'median': 13, 'latencies_ns': list(range(1000))}],
    }
    benchmark.record({'case': (full, None)}, identity={'v8_version': '15.4'})
    saved = json.loads(baseline.read_text(encoding='utf-8'))
    assert saved['cases']['case'] == {
        'median': 12.5, 'min': 12.0, 'unit': 'ms/op', 'detail': 'x',
        'workload': 'v1', 'runs': 3,
    }
    assert 'latencies_ns' not in saved['cases']['case']
    assert 'samples' not in saved['cases']['case']
    assert saved['v8_version'] == '15.4'


def test_recorded_baseline_without_workload_stays_comparable(benchmark, tmp_path, capsys):
    """Inline cases have no workload; both sides being absent must still compare."""
    import json

    baseline = tmp_path / 'baseline.json'
    benchmark.BASELINE_FILE = baseline
    result = {'median': 1.0, 'min': 1.0, 'unit': 'ms/op', 'detail': 'x', 'runs': 3}
    benchmark.record({'case': (result, None)})
    assert benchmark.compare({'case': (dict(result), None)}) is True
