"""Measured numbers for the paths a host pays for, before and after a change.

    python benchmarks/benchmark.py --record     # record benchmarks/perf_baseline.json
    python benchmarks/benchmark.py              # compare the current run against it

Cases run in fresh Browser instances; HTTP cases use loopback only.
Compare each machine against itself.  ``host_call`` minus ``js_loop``
is what crossing into Python costs, and ``evaluate`` is the floor both of them pay.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import platform
import subprocess
import json
import statistics
import sys
import time
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE_FILE = Path(__file__).resolve().parent / "perf_baseline.json"
for _path in (REPO, REPO / "pyv8c", REPO / "pyv8"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

sys.path.insert(0, str(REPO / "tools"))
from check_policy import measure_startup  # noqa: E402  the gate's own startup measurement
from benchmarks.cases.network import (cronet_completion_http, cronet_host_http,
                                      cronet_http_stages, cronet_resource_http,
                                      curl_host_http, curl_http_stages, curl_resource_http)  # noqa: E402

#: A regression has to move the median this much, and the minimum too.
TOLERANCE = 0.10

#: Chosen so a case takes tens of milliseconds and the run stays under a minute.
DOM_NODES = 4000
PARSE_NODES = 4000
QUERY_REPEATS = 200
DISPATCH_REPEATS = 20000
HOST_CALL_REPEATS = 20000
MEMBER_CALL_REPEATS = 20000
JS_LOOP_REPEATS = 20000
EVALUATE_REPEATS = 5000
PAGE_CYCLES = 15
RESOURCE_COUNT = 200
RESIDENT_PAGES = 8


def _document(nodes):
    rows = "".join(f'<div class="row" id="r{i}"><span>row {i}</span></div>' for i in range(nodes))
    return f"<!DOCTYPE html><html><body><div id='root'>{rows}</div></body></html>"


def _loader(url, request):
    from pyv8c import NetworkResponse
    return NetworkResponse(url=url, status=200, body=b"x" * 64,
                           headers={"content-type": "image/gif"})


async def _browser(**kwargs):
    from pyv8c.async_api import Browser, run
    return (await Browser(stub_logging="off", **kwargs).__aenter__())


async def _page(browser, markup="<body></body>"):
    page = browser.new_profile().new_page()
    (await page.set_content(markup))
    return page


def _seconds_per(operation, repeats):
    """Seconds one repeat of *operation* takes."""
    started = time.perf_counter()
    operation()
    return (time.perf_counter() - started) / repeats


def _js_seconds(page, script):
    """Seconds one run of the JavaScript *script* takes, round trip included."""
    return ((_seconds_per(lambda: page.evaluate(f"(() => {{ {script} }})()"), 1)))


def _committed_bytes():
    """This process's committed memory, in bytes.

    The working set is what the machine has actually touched, and a V8 realm that
    reserves pages without reading them does not move it, which is why the first
    version of this case measured zero.  psutil answers when it is installed; the
    native call is the fallback, and it raises rather than report a false zero.
    """
    try:
        import psutil
        return float(psutil.Process().memory_info().private)
    except ImportError:
        pass
    if sys.platform == "win32":
        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        if not ok:
            raise OSError("GetProcessMemoryInfo failed")
        return float(counters.PagefileUsage)
    import resource
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024.0


def _sample(value, unit, detail):
    return {"median": float(value), "min": float(value), "unit": unit, "detail": detail}


async def case_startup():
    """Browser -> profile -> page -> set_content, measured the way the gate measures it."""
    minimum, median, modules = measure_startup(runs=3)
    if median is None:
        raise RuntimeError(f"startup could not be measured: {modules}")
    return {"median": median, "min": minimum, "unit": "ms",
            "detail": f"{modules} modules imported"}


async def case_cold_compile():
    """Compile all interfaces with genuinely fresh process caches."""
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), '--startup-worker', 'compile'],
        capture_output=True, text=True, timeout=180, check=True)
    return json.loads(child.stdout)


async def _seconds_per_async(operation, repeats):
    started = time.perf_counter()
    await operation()
    return (time.perf_counter() - started) / repeats


async def case_page_cycle():
    """Opening and closing a page: realm assembly and teardown."""
    browser = (await _browser())
    profile = browser.new_profile()
    try:
        async def operation():
            for _ in range(PAGE_CYCLES):
                (await profile.new_page().close())
        return _sample(((await _seconds_per_async(operation, PAGE_CYCLES))) * 1000, "ms/op",
                       f"open then close, {PAGE_CYCLES} cycles")
    finally:
        (await browser.close())


async def case_evaluate():
    """An empty host round trip: page.evaluate returning a primitive."""
    browser = (await _browser())
    page = (await _page(browser))
    try:
        def operation():
            for _ in range(EVALUATE_REPEATS):
                page.evaluate("1")
        return _sample(((_seconds_per(operation, EVALUATE_REPEATS))) * 1000, "ms/op",
                       "evaluate('1')")
    finally:
        (await browser.close())


async def case_host_call():
    """JavaScript calling into Python: setAttribute in a loop."""
    browser = (await _browser())
    page = (await _page(browser))
    try:
        elapsed = ((_js_seconds(page, (
            "const el = document.createElement('div');"
            f" for (let i = 0; i < {HOST_CALL_REPEATS}; i++) el.setAttribute('data-x', '1');"
        ))))
        return _sample(elapsed * 1000, "ms", f"{HOST_CALL_REPEATS} setAttribute calls")
    finally:
        (await browser.close())


async def case_js_loop():
    """The same loop with no host call in it: the floor for the case above."""
    browser = (await _browser())
    page = (await _page(browser))
    try:
        elapsed = ((_js_seconds(page, (
            "let total = 0;"
            f" for (let i = 0; i < {JS_LOOP_REPEATS}; i++) total += i;"
        ))))
        return _sample(elapsed * 1000, "ms", f"{JS_LOOP_REPEATS} additions, no host call")
    finally:
        (await browser.close())


async def _member_call_seconds(browser, *, expect_aborted):
    """Seconds the same member call takes, however that member is answered."""
    page = (await _page(browser))
    try:
        actual = page.evaluate('(() => { const c = new AbortController(); c.abort(); return c.signal.aborted; })()')
        if actual is not expect_aborted:
            raise RuntimeError('member benchmark implementation does not match its workload')
        elapsed = ((_js_seconds(page, (
            f"for (let i = 0; i < {MEMBER_CALL_REPEATS}; i++) new AbortController().abort();"
        ))))
        return elapsed * 1000
    finally:
        (await browser.close())


async def case_stub_call():
    """The floor a host hook is held to: the same member call, answered by its stub."""
    from pyv8c.engine.binder import _Stubs
    callback = _Stubs('off').method('AbortController', 'abort')
    if not getattr(callback, '__v8_idl_stub__', False):
        raise RuntimeError('benchmark callback is not a stub')
    result = _sample((await _member_call_seconds((await _browser(interface_overrides={
        'AbortController': {'abort': callback}})), expect_aborted=False)), 'ms',
        f'{MEMBER_CALL_REPEATS} stub member calls')
    result['workload'] = 'stub-abort-v2'
    return result


async def case_hook_call():
    """What answering that member from a host hook costs instead."""
    browser = (await _browser(interface_overrides={"AbortController": {"abort": lambda *_: None}}))
    return _sample((await _member_call_seconds(browser, expect_aborted=False)), "ms",
                   f"{MEMBER_CALL_REPEATS} hooked member calls")


async def case_abort_call():
    return _sample((await _member_call_seconds((await _browser()), expect_aborted=True)), 'ms',
                   f'{MEMBER_CALL_REPEATS} implemented AbortController calls')


async def case_dom_build():
    """Building nodes from script: createElement plus appendChild."""
    browser = (await _browser())
    page = (await _page(browser))
    try:
        elapsed = ((_js_seconds(page, (
            "const root = document.body;"
            f" for (let i = 0; i < {DOM_NODES}; i++) {{"
            " const el = document.createElement('div'); el.className = 'row';"
            " el.appendChild(document.createElement('span')); root.appendChild(el); }"
        ))))
        assert page.evaluate('document.querySelectorAll("div.row > span").length') == DOM_NODES
        return _sample(elapsed * 1000, "ms", f"{DOM_NODES} divs with a span each")
    finally:
        (await browser.close())


async def case_dom_parse():
    """Handing markup to the parser: set_content with the same tree."""
    browser = (await _browser())
    page = (await _page(browser))
    markup = _document(PARSE_NODES)
    try:
        elapsed = ((await _seconds_per_async(lambda: page.set_content(markup, url="http://benchmark.test/"), 1))) * 1000
        assert page.evaluate('document.querySelectorAll("div.row > span").length') == PARSE_NODES
        return _sample(elapsed, "ms", f"{PARSE_NODES} rows")
    finally:
        (await browser.close())


async def case_html_fragment():
    browser = (await _browser())
    page = (await _page(browser))
    markup = _document(4000)
    try:
        page.evaluate('globalThis.fragmentTarget=document.createElement("section");document.body.appendChild(fragmentTarget)')
        def operation():
            return page.evaluate('fragmentTarget.innerHTML=' + json.dumps(markup) + ';fragmentTarget.querySelectorAll("div.row > span").length')
        started = time.perf_counter()
        count = (operation())
        elapsed = time.perf_counter() - started
        assert count == 4000
        result = _sample(elapsed * 1000, 'ms', '4000 rows through innerHTML, consumed query')
        result['workload'] = 'html-fragment-v1:4000'
        return result
    finally:
        (await browser.close())


async def case_html_attributes():
    browser = (await _browser())
    page = (await _page(browser))
    markup = '<body>' + ''.join(
        f'<div id="r{i}" class="row" data-a="a&amp;b" data-b="&quot;x&quot;" title="a > b" lang=en hidden><span>{i}</span></div>'
        for i in range(4000)) + '</body>'
    try:
        started = time.perf_counter()
        (await page.set_content(markup, url='https://benchmark.test/'))
        elapsed = time.perf_counter() - started
        assert page.evaluate('document.querySelectorAll("div.row").length') == 4000
        assert page.evaluate('document.getElementById("r3999").getAttribute("data-a")') == 'a&b'
        result = _sample(elapsed * 1000, 'ms', '4000 attribute-heavy rows, decoded values checked')
        result['workload'] = 'html-attributes-v1:4000'
        return result
    finally:
        (await browser.close())


async def case_dom_python_build():
    browser = (await _browser())
    page = (await _page(browser))
    try:
        def build():
            document = page.document
            root = document.get_body()
            started = time.perf_counter()
            for _ in range(4000):
                element = document.fn_createElement('div')
                element.append_child(document.fn_createElement('span'))
                root.append_child(element)
            return (time.perf_counter() - started) * 1000
        elapsed = page.executor.call(build)
        assert page.evaluate('document.querySelectorAll("div > span").length') == 4000
        result = _sample(elapsed, 'ms', '4000 div/span pairs built directly on owner')
        result['workload'] = 'dom-python-build-v1:4000'
        return result
    finally:
        (await browser.close())


async def case_dom_query():
    """Scanning a built tree: querySelectorAll and getElementsByTagName."""
    browser = (await _browser())
    page = (await _page(browser, _document(DOM_NODES)))
    try:
        elapsed = ((_js_seconds(page, (
            f"for (let i = 0; i < {QUERY_REPEATS}; i++) {{"
            " document.querySelectorAll('div.row').length; document.getElementsByTagName('span').length; }"
        ))))
        result = _sample(elapsed * 1000, "ms", f"{QUERY_REPEATS} scans of a {DOM_NODES}-row tree")
        result['workload'] = 'consumed-dom-query-v2'
        return result
    finally:
        (await browser.close())


async def case_event_dispatch():
    """Dispatching a bubbling event to a listener."""
    browser = (await _browser())
    page = (await _page(browser, "<body><div id='host'><span id='leaf'></span></div></body>"))
    try:
        elapsed = ((_js_seconds(page, (
            "const leaf = document.getElementById('leaf'); let seen = 0;"
            " document.getElementById('host').addEventListener('ping', () => { seen += 1; });"
            f" for (let i = 0; i < {DISPATCH_REPEATS}; i++)"
            " leaf.dispatchEvent(new Event('ping', {bubbles: true}));"
        ))))
        return _sample(elapsed * 1000, "ms", f"{DISPATCH_REPEATS} dispatches through one listener")
    finally:
        (await browser.close())


async def case_resource_batch():
    """Loading the subresources of one document through a host loader."""
    markup = "".join(f"<img src='/image{i}.gif'>" for i in range(RESOURCE_COUNT))
    browser = (await _browser(resource_loader=_loader))
    page = browser.new_profile().new_page()
    try:
        async def operation():
            (await page.set_content(markup, url="http://benchmark.test/"))
            (await page.wait_for_load_state('load', timeout=30))
        started = time.perf_counter()
        await operation()
        result = _sample((time.perf_counter() - started) * 1000, "ms", f"{RESOURCE_COUNT} images")
        result['workload'] = 'document-load-resource-batch-v2'
        return result
    finally:
        (await browser.close())


async def case_resident_pages():
    """Measure one memory batch in a fresh process, independent of prior cases."""
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--memory-worker'],
                           capture_output=True, text=True, check=True)
    return json.loads(child.stdout)


async def _resident_pages_worker():
    from pyv8c.foundation.logging import logger
    logger.remove()
    browser = (await _browser())
    profile = browser.new_profile()
    pages = []
    try:
        # Separate retained pages from one-time V8/binder initialization.
        warm = profile.new_page()
        (await warm.set_content('<body>x</body>'))
        (await warm.close())
        gc.collect()
        before = _committed_bytes()
        pages = [profile.new_page() for _ in range(RESIDENT_PAGES)]
        for page in pages:
            (await page.set_content("<body><div>x</div></body>"))
        after = _committed_bytes()
        contexts = [page.v8_context for page in pages]
        handles = sum(context.live_handles for context in contexts)
        result = _sample((after - before) / RESIDENT_PAGES / (1024 * 1024), 'MB/page',
                         f'{RESIDENT_PAGES} live pages, {handles // RESIDENT_PAGES} python handles each')
        result.update(workload='isolated-resident-pages-v2', private_before=before,
                      private_resident=after, handles_resident=handles)
    finally:
        (await browser.close())
    gc.collect()
    result.update(private_after_close=_committed_bytes(),
                  handles_after_close=sum(context.live_handles for context in contexts),
                  owner_threads_after_close=sum(thread.name.startswith('pyv8c-')
                                                for thread in threading.enumerate()))
    return result


CASES = (
    ("startup", case_startup),
    ("cold_compile", case_cold_compile),
    ("page_cycle", case_page_cycle),
    ("evaluate", case_evaluate),
    ("host_call", case_host_call),
    ("js_loop", case_js_loop),
    ("stub_call", case_stub_call),
    ("hook_call", case_hook_call),
    ("abort_call", case_abort_call),
    ("dom_build", case_dom_build),
    ("dom_parse", case_dom_parse),
    ("html_fragment", case_html_fragment),
    ("html_attributes", case_html_attributes),
    ("dom_python_build", case_dom_python_build),
    ("dom_query", case_dom_query),
    ("event_dispatch", case_event_dispatch),
    ("resource_batch", case_resource_batch),
    ("resident_pages", case_resident_pages),
    ("network_host_curl", curl_host_http),
    ("network_host_cronet", cronet_host_http),
    ("network_resources_curl", curl_resource_http),
    ("network_resources_cronet", cronet_resource_http),
    ("network_stages_curl", curl_http_stages),
    ("network_stages_cronet", cronet_http_stages),
    ("network_cronet_completion", cronet_completion_http),
)


async def measure(case, runs):
    """``(result, reason)``: the median and minimum of *runs* runs, or why there is none."""
    samples = []
    for _ in range(runs):
        try:
            samples.append(await case())
        except subprocess.CalledProcessError as exc:
            return None, f"child exited {exc.returncode}: {(exc.stderr or str(exc))[-2000:]}"
        except Exception as exc:  # one case failing must not stop the others
            return None, f"{type(exc).__name__}: {exc}"
    first = dict(samples[0])
    first["median"] = statistics.median(sample["median"] for sample in samples)
    first["min"] = min(sample["min"] for sample in samples)
    first["runs"] = runs
    first["samples"] = samples
    return first, None


def measurement_identity(native_build_record=None):
    """Fingerprint sources, including new files, separately from the loaded binary."""
    from pyv8c import pyv8
    from pyv8c._native import _native, dom

    dom_source_hash = hashlib.sha256((REPO / 'pyv8c/native/dom/builder.cpp').read_bytes()).hexdigest()
    if dom.SOURCE_SHA256 != dom_source_hash:
        raise RuntimeError('rebuild _native before benchmarking: DOM source and binary differ')
    binary = Path(pyv8._pyv8.__file__)
    digest = hashlib.sha256()
    native = REPO / 'pyv8c/native'
    sources = [native / name for name in ('Cargo.toml', 'Cargo.lock', 'build.rs')]
    for root in (REPO / 'pyv8/src', native / 'src', native / 'dom', REPO / 'pyv8c/pyv8c',
                 REPO / 'benchmarks'):
        sources.extend(path for path in root.rglob('*')
                       if path.is_file() and path.suffix in ('.py', '.cpp', '.h', '.rs'))
    for path in sorted(sources):
        digest.update(path.relative_to(REPO).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    fingerprint = REPO / 'pyv8/build/pyv8-extension.json'
    recorded = (native_build_record if native_build_record is not None else
                json.loads(fingerprint.read_text()) if fingerprint.is_file() else {})
    build = recorded.get('inputs', recorded)
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    if 'binary_sha256' in recorded and binary_hash != recorded['binary_sha256']:
        raise RuntimeError('rebuild or load the matching _pyv8 before benchmarking: binary and build record differ')
    return {
        'python': sys.version.split()[0], 'python_full': sys.version,
        'platform': platform.platform(), 'processor': platform.processor(),
        'binary_path': str(binary), 'v8_version': pyv8.v8_version(),
        'binary_sha256': binary_hash,
        'dom_binary_path': _native.__file__,
        'dom_binary_sha256': hashlib.sha256(Path(_native.__file__).read_bytes()).hexdigest(),
        'dom_source_sha256': dom.SOURCE_SHA256,
        'dom_build_source_matches': dom.SOURCE_SHA256 == dom_source_hash,
        'native_build_binary_matches': (binary_hash == recorded['binary_sha256']
                                         if 'binary_sha256' in recorded else None),
        'source_tree_sha256': digest.hexdigest(),
        'native_build': {key: build.get(key) for key in
                         ('bridge_sources_sha256', 'v8_library_sha256', 'gn_args', 'version')},
        'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        'worktree_sha256': hashlib.sha256(subprocess.check_output(
            ['git', 'diff', '--binary', 'HEAD'], cwd=REPO)).hexdigest(),
    }


def baseline_fields(result):
    """Keep only the aggregate fields compare() needs; drop raw sample arrays.

    Worker results carry large per-iteration arrays (``latencies_ns``) and a
    full ``samples`` list. Persisting them inflates the baseline to tens of
    MB without adding comparison value -- compare() only reads median, min,
    unit, detail and workload.
    """
    keep = ("median", "min", "unit", "detail", "workload", "runs")
    return {key: result[key] for key in keep if key in result}


def record(results, identity=None):
    baseline = {
        "recorded": time.strftime("%Y-%m-%d"),
        "python": sys.version.split()[0],
        "cases": {name: baseline_fields(result) for name, (result, _) in results.items()
                  if result is not None},
        "unavailable": {name: reason for name, (result, reason) in results.items()
                        if result is None},
    }
    baseline.update(identity or {})
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {len(baseline['cases'])} cases in {BASELINE_FILE.name}")
    for name in results:
        result = results[name][0]
        if result is not None:
            print(f"  {name:18s}{result['median']:10.2f} {result['unit']:10s}{result['detail']}")


def compare(results):
    if not BASELINE_FILE.exists():
        print("-- no baseline recorded; run with --record")
        return all(result is not None for result, _ in results.values())
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    baseline_python = baseline.get("python")
    current_python = sys.version.split()[0]
    if baseline_python and baseline_python != current_python:
        print(f"-- incomparable Python versions: baseline {baseline_python}, current {current_python}")
        return False
    print(f"{'case':18s}{'baseline':>10s}{'now':>10s}{'delta':>9s}  unit       detail")
    regressed = []
    incomparable = []
    for name, (result, reason) in results.items():
        if result is None:
            print(f"{name:18s}{'--':>10s}{'--':>10s}{'':9s}  {reason}")
            continue
        recorded = baseline["cases"].get(name)
        now, unit, detail = result["median"], result["unit"], result["detail"]
        if recorded is None:
            print(f"{name:18s}{'new':>10s}{now:10.2f}{'':9s}  {unit:10s}{detail}")
            continue
        if result.get('workload') != recorded.get('workload'):
            print(f'{name:18s}  incomparable workload; baseline was not changed')
            incomparable.append(name)
            continue
        before = recorded["median"]
        change = (now - before) / before if before else 0.0
        slower = change > TOLERANCE and result["min"] > recorded["min"] * (1 + TOLERANCE)
        if slower:
            regressed.append(name)
        flag = "  <-- slower" if slower else ""
        print(f"{name:18s}{before:10.2f}{now:10.2f}{change:+8.1%}{flag}  {unit:10s}{detail}")
    if regressed:
        print(f"\nslower: {', '.join(regressed)} (median and minimum both past {TOLERANCE:.0%})")
    return not regressed and not incomparable and all(result is not None for result, _ in results.values())


async def main(argv=None):
    from benchmarks.cases.js_compute import ENGINE_NAMES as JS_ENGINE_NAMES
    from benchmarks.cases.js_compute import WARMUP as JS_WARMUP

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record", action="store_true", help="write the baseline file")
    parser.add_argument("--output", type=Path, help="save measured samples and environment without changing the baseline")
    parser.add_argument("--runs", type=int, default=3, help="runs per case")
    parser.add_argument("--cases", default="", help="comma-separated subset of case names")
    parser.add_argument("--list", action="store_true", help="list all case names and exit")
    parser.add_argument('--memory-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--execution-worker', help=argparse.SUPPRESS)
    parser.add_argument('--bridge-worker', help=argparse.SUPPRESS)
    parser.add_argument('--web-worker', help=argparse.SUPPRESS)
    parser.add_argument('--startup-worker', help=argparse.SUPPRESS)
    parser.add_argument('--style-worker', help=argparse.SUPPRESS)
    parser.add_argument('--javascript-worker', help=argparse.SUPPRESS)
    parser.add_argument('--entrypoint-worker', help=argparse.SUPPRESS)
    parser.add_argument('--lifecycle-worker', help=argparse.SUPPRESS)
    parser.add_argument('--entrypoint-api', choices=('sync', 'async'), default='sync',
                        help='public entry point for entrypoint_* cases')
    parser.add_argument('--lifecycle-count', type=int, default=1000,
                        help='page create/close cycles for lifecycle_pages')
    parser.add_argument('--javascript-iterations', type=int, default=20, help='samples in each JS compute workload')
    parser.add_argument('--javascript-warmup', type=int, default=JS_WARMUP, help='warmup calls before each JS compute workload')
    parser.add_argument('--javascript-engine', choices=JS_ENGINE_NAMES, default='pyv8c')
    parser.add_argument('--style-iterations', type=int, default=12, help='batches in each style/layout workload')
    parser.add_argument('--web-iterations', type=int, default=20, help='batches in each Web API workload')
    parser.add_argument('--web-profile-dir', type=Path, help='save owner-thread profiles after Web API measurements')
    parser.add_argument('--bridge-iterations', type=int, default=60,
                        help='batches in each native bridge workload')
    parser.add_argument('--iterations', type=int, default=12000, help='operations in each execution workload')
    parser.add_argument('--gc-mode', choices=('default', 'disabled'), default='default')
    parser.add_argument('--baseline', type=Path, help='baseline to compare or record')
    arguments = parser.parse_args(argv)
    if arguments.iterations < 1:
        parser.error('--iterations must be positive')
    if arguments.bridge_iterations < 1:
        parser.error('--bridge-iterations must be positive')
    if arguments.web_iterations < 1:
        parser.error('--web-iterations must be positive')
    if arguments.style_iterations < 1:
        parser.error('--style-iterations must be positive')
    if arguments.javascript_iterations < 1:
        parser.error('--javascript-iterations must be positive')
    if arguments.javascript_warmup < 0:
        parser.error('--javascript-warmup must be non-negative')
    if arguments.lifecycle_count < 1:
        parser.error('--lifecycle-count must be positive')
    if arguments.javascript_worker:
        from benchmarks.cases.js_compute import run_case
        print('JS_COMPUTE_RESULT ' + json.dumps((await run_case(
            arguments.javascript_worker, arguments.javascript_engine, arguments.javascript_iterations,
            warmup=arguments.javascript_warmup))))
        return 0
    if arguments.style_worker:
        from benchmarks.cases.style import run_case
        print(json.dumps((await run_case(arguments.style_worker, arguments.style_iterations))))
        return 0
    if arguments.startup_worker:
        from benchmarks.cases.startup import run_case
        print(json.dumps((await run_case(arguments.startup_worker))))
        return 0
    if arguments.web_worker:
        from benchmarks.cases.web_api import run_case
        print(json.dumps((await run_case(arguments.web_worker, arguments.web_iterations,
                                  arguments.web_profile_dir, arguments.gc_mode))))
        return 0
    if arguments.bridge_worker:
        from benchmarks.cases.native_bridge import run_case
        print(json.dumps((await run_case(arguments.bridge_worker, arguments.bridge_iterations))))
        return 0
    if arguments.execution_worker:
        from benchmarks.cases.execution import run_case
        print(json.dumps((await run_case(arguments.execution_worker, arguments.iterations, arguments.gc_mode))))
        return 0
    if arguments.entrypoint_worker:
        if arguments.entrypoint_api == 'sync':
            raise RuntimeError('sync entrypoint must run outside an event loop (use the CLI worker path)')
        from benchmarks.cases.entrypoints import run_case
        result = await run_case(arguments.entrypoint_worker, arguments.iterations, arguments.entrypoint_api)
        print(json.dumps({k: v for k, v in result.items() if k != 'samples'}))
        return 0
    if arguments.lifecycle_worker:
        from benchmarks.cases.lifecycle import run_case
        result = await run_case(arguments.lifecycle_worker, arguments.lifecycle_count)
        print(json.dumps({k: v for k, v in result.items() if k not in ('report', 'samples')}))
        return 0
    if arguments.memory_worker:
        print(json.dumps((await _resident_pages_worker())))
        return 0
    if arguments.runs < 1:
        parser.error("--runs must be positive")
    global BASELINE_FILE
    if arguments.baseline is not None:
        BASELINE_FILE = arguments.baseline

    from benchmarks.cases.execution import CASE_NAMES
    from benchmarks.cases.native_bridge import CASE_NAMES as BRIDGE_CASE_NAMES
    from benchmarks.cases.web_api import CASE_NAMES as WEB_CASE_NAMES
    from benchmarks.cases.startup import CASE_NAMES as STARTUP_CASE_NAMES
    from benchmarks.cases.style import CASE_NAMES as STYLE_CASE_NAMES
    from benchmarks.cases.js_compute import CASE_NAMES as JS_CASE_NAMES
    from benchmarks.cases.entrypoints import CASE_NAMES as ENTRYPOINT_CASE_NAMES
    from benchmarks.cases.lifecycle import CASE_NAMES as LIFECYCLE_CASE_NAMES

    def worker_case(command, name, options):
        async def run():
            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), f'--{command}-worker', name, *options],
                capture_output=True, text=True, timeout=180, check=True)
            if command == 'javascript':
                payload = next(line.removeprefix('JS_COMPUTE_RESULT ') for line in child.stdout.splitlines()
                               if line.startswith('JS_COMPUTE_RESULT '))
                return json.loads(payload)
            return json.loads(child.stdout)
        return run

    web_options = ['--web-iterations', str(arguments.web_iterations), '--gc-mode', arguments.gc_mode]
    if arguments.web_profile_dir is not None:
        web_options += ['--web-profile-dir', str(arguments.web_profile_dir)]
    families = (
        ('execution', 'execution', CASE_NAMES,
         ['--iterations', str(arguments.iterations), '--gc-mode', arguments.gc_mode]),
        ('bridge', 'bridge', BRIDGE_CASE_NAMES,
         ['--bridge-iterations', str(arguments.bridge_iterations)]),
        ('web', 'web', WEB_CASE_NAMES, web_options),
        ('startup', 'startup', tuple(name for name in STARTUP_CASE_NAMES if name != 'compile'), []),
        ('style', 'style', STYLE_CASE_NAMES, ['--style-iterations', str(arguments.style_iterations)]),
        ('javascript', 'javascript', JS_CASE_NAMES,
         ['--javascript-iterations', str(arguments.javascript_iterations),
          '--javascript-warmup', str(arguments.javascript_warmup),
          '--javascript-engine', arguments.javascript_engine]),
        ('entrypoint_sync', 'entrypoint', ENTRYPOINT_CASE_NAMES,
         ['--iterations', str(arguments.iterations), '--entrypoint-api', 'sync']),
        ('entrypoint_async', 'entrypoint', ENTRYPOINT_CASE_NAMES,
         ['--iterations', str(arguments.iterations), '--entrypoint-api', 'async']),
        ('lifecycle', 'lifecycle', LIFECYCLE_CASE_NAMES,
         ['--lifecycle-count', str(arguments.lifecycle_count)]),
    )
    cases = CASES + tuple((f'{prefix}_{name}', worker_case(command, name, options))
                          for prefix, command, names, options in families for name in names)

    available = dict(cases)
    if arguments.list:
        for name in available:
            print(name)
        return 0
    selected = [name.strip() for name in arguments.cases.split(",") if name.strip()]
    unknown = [name for name in selected if name not in available]
    if unknown:
        parser.error(f"unknown case(s): {', '.join(unknown)}")
    chosen = [(name, case) for name, case in cases if not selected or name in selected]

    results = {}
    for name, case in chosen:
        results[name] = (await measure(case, arguments.runs))
        if results[name][1] is not None:
            print(f"-- {name}: {results[name][1]}")

    identity = (measurement_identity() if (arguments.output or arguments.record)
                and any(result is not None for result, _ in results.values()) else {})
    if arguments.output:
        report = dict(identity, cases={name: {"result": result, "error": reason}
                                      for name, (result, reason) in results.items()})
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if arguments.record:
        record(results, identity)
        return int(any(result is None for result, _ in results.values()))
    return 0 if (compare(results)) else 1


def _entrypoint_sync_worker(argv):
    """Run a synchronous entrypoint case outside any event loop."""
    name = argv[argv.index('--entrypoint-worker') + 1]
    iterations = int(argv[argv.index('--iterations') + 1]) if '--iterations' in argv else 12000
    from benchmarks.cases.entrypoints import CASE_NAMES, sync_measure
    if name not in CASE_NAMES:
        raise SystemExit(f'unknown entrypoint case: {name}')
    import threading
    import time
    result = sync_measure(name, iterations)
    result.update(
        api='sync', caller_thread=threading.get_ident(),
        detail=f'entrypoint sync {name}; {iterations} operations',
        workload=f'entrypoints-{name}-v2:{iterations}:sync',
    )
    result['execution_thread'] = int(result['execution_thread'])
    assert result['execution_thread'] == result['caller_thread']
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    if '--startup-worker' in sys.argv:
        from benchmarks.cases.startup import measure_fresh
        print(json.dumps(measure_fresh(sys.argv[sys.argv.index('--startup-worker') + 1])))
    elif ('--entrypoint-worker' in sys.argv and '--entrypoint-api' in sys.argv
          and sys.argv[sys.argv.index('--entrypoint-api') + 1] == 'sync'):
        sys.exit(_entrypoint_sync_worker(sys.argv))
    else:
        from pyv8c.async_api import run
        sys.exit(run(main()))
