"""The gates a change has to pass, runnable before it is committed.

Six gates, each one something this repository has until now relied on a person
remembering:

* ``layers``     -- the layering rules.  Delegated to
                    ``tests/unit/test_architecture.py`` rather than restated here, so
                    there is one implementation and no drift.
* ``native-layout`` -- enforce the C++ source layout and include layering.
* ``companions`` -- an implementation file changed with no test changed alongside it.
* ``startup``    -- the documented startup measurement, against a recorded budget.
* ``offline``    -- ``tests/unit`` runs with no native extension on the path;
                    a static pre-check also requires every module that reaches
                    the extension to be guarded by ``pytest.importorskip``.
* ``duplicates`` -- a test outside the corpus that repeats a corpus case, which is
                    a warning: the corpus case is the one run against Chrome.

Usage::

    python tools/check_policy.py                  # every gate, working tree vs HEAD
    python tools/check_policy.py --base main      # ... against a branch
    python tools/check_policy.py --strict         # warnings fail the run
    python tools/check_policy.py --record-startup # accept today's startup as the budget

Importing pyv8 has to work for the startup gate; when it does not, that gate says so
and the others still run.  A site-specific-behaviour rule is deliberately absent: it
cannot be written without false positives in a framework whose whole job is emulating
a browser, so that one stays a review rule.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import difflib
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE_FILE = Path(__file__).resolve().parent / "startup_baseline.json"

#: A tenth more and 5 ms: the measurement repeats within about 5 ms of itself on a
#: quiet machine, while putting one import back on the startup path costs 35 ms -- a
#: fifth of a 150 ms number, which is why the looser tolerance of a latency gate does
#: not apply here.
STARTUP_TOLERANCE = 0.1
STARTUP_FLOOR_MS = 5.0

#: Browser -> profile -> page -> set_content, the path a host pays before it can run
#: anything, plus the module count that made the import graph visible.
STARTUP_CODE = """
import sys
import time

import pyv8c

started = time.perf_counter()
import pyv8c.async_api

async def measure():
    async with pyv8c.async_api.Browser() as runtime:
        profile = runtime.new_profile()
        page = profile.new_page()
        await page.set_content("<body></body>", url="http://example.test/")
        elapsed = (time.perf_counter() - started) * 1000.0
        print(round(elapsed, 1), len(sys.modules))

pyv8c.async_api.run(measure())
"""

GATES = ("layers", "native-layout", "companions", "startup", "offline", "duplicates")


def gate_native_layout():
    """Enforce the native source layout rather than merely printing findings."""
    result = subprocess.run([sys.executable, str(REPO / "pyv8" / "tools" / "check_layout.py"),
                             "--enforce"], cwd=REPO, check=False)
    return result.returncode == 0


def changed_paths(base):
    """Repository-relative paths this change touches, committed or not."""
    listed = subprocess.run(
        ["git", "diff", "--name-only", "-z", base], cwd=REPO, capture_output=True, text=True
    )
    if listed.returncode != 0:
        raise SystemExit(f"git diff --name-only {base} failed:\n{listed.stderr.strip()}")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return sorted(set(filter(None, listed.stdout.split("\0")))
                  | set(filter(None, untracked.stdout.split("\0"))))


def classify(path):
    """Which bucket a changed path falls into."""
    if path.startswith(("tests/", "pyv8c/tests/", "pyv8/tests/")):
        return "test"
    if path.startswith("pyv8/src/"):
        return "implementation" if path.endswith((".cpp", ".h", ".def")) else "data"
    if path.startswith("pyv8c/pyv8c/pyv8/"):
        return "implementation" if path.endswith((".py", ".pyi")) else "data"
    if path.startswith("pyv8c/"):
        return "implementation" if path.endswith(".py") else "data"
    if path.startswith("docs/") or path.endswith(".md"):
        return "docs"
    return "tooling"


def companion_findings(paths):
    """Implementation files that changed while no test changed with them."""
    implementation = [path for path in paths if classify(path) == "implementation"]
    if any(classify(path) == "test" for path in paths):
        return []
    return implementation


def startup_budget(baseline):
    """The number the startup gate compares against, from the recorded baseline."""
    return baseline["median_ms"] * (1.0 + STARTUP_TOLERANCE) + STARTUP_FLOOR_MS


def measure_startup(runs=3, timeout=300):
    """``(min_ms, median_ms, modules)``, or ``(None, None, reason)`` when unavailable."""
    environment = os.environ.copy()
    search = [str(REPO / "pyv8c"), str(REPO / "pyv8")]
    environment["PYTHONPATH"] = os.pathsep.join(
        [*search, environment.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    samples = []
    modules = 0
    for _ in range(runs):
        result = subprocess.run(
            [sys.executable, "-c", STARTUP_CODE],
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, None, result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "failed"
        elapsed, count = result.stdout.split()
        samples.append(float(elapsed))
        modules = int(count)
    return min(samples), statistics.median(samples), modules


def gate_layers():
    """Run the layering rules where they already live."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "pyv8c/tests/unit/test_architecture.py", "-q"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("ok   layers: the architecture rules hold")
        return True
    print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "layers failed")
    return False


#: Script similarity at which a test reads as a copy of a corpus case.
DUPLICATE_RATIO = 0.80


def _squeeze(text):
    """The script with its whitespace collapsed, so formatting does not matter."""
    return re.sub(r"\s+", " ", text).strip()


def _script_literals(path):
    """``(test name, line, [scripts])`` for *path*; short strings are not scripts."""
    #: A test file may carry a UTF-8 BOM; ``utf-8-sig`` reads it and plain UTF-8 alike.
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        scripts = [
            item.value for item in ast.walk(node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
            and len(item.value) > 60 and "(" in item.value and "def " not in item.value
        ]
        if scripts:
            found.append((node.name, node.lineno, scripts))
    return found


def corpus_cases():
    """``(case name, line, script)`` for every case the corpus declares."""
    tree = ast.parse((REPO / "pyv8c" / "tests" / "repros" / "corpus.py").read_text(encoding="utf-8"))
    cases = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "Repro":
            continue
        keywords = {item.arg: item.value for item in node.keywords}
        name, script = keywords.get("name"), keywords.get("script")
        if isinstance(name, ast.Constant) and isinstance(script, ast.Constant):
            cases.append((name.value, node.lineno, script.value))
    return cases


def duplicate_findings():
    """``(ratio, path, line, test, case, case line)`` for near-identical scripts."""
    cases = [(name, line, _squeeze(script)) for name, line, script in corpus_cases()]
    findings = []
    for path in sorted((REPO / "pyv8c" / "tests").rglob("test_*.py")):
        if "repros" in path.parts or "__pycache__" in path.parts:
            continue
        for test, line, scripts in _script_literals(path):
            best = None
            for script in scripts:
                text = _squeeze(script)
                for name, case_line, case_text in cases:
                    matcher = difflib.SequenceMatcher(None, text, case_text)
                    if matcher.real_quick_ratio() < DUPLICATE_RATIO or matcher.quick_ratio() < DUPLICATE_RATIO:
                        continue
                    ratio = matcher.ratio()
                    if best is None or ratio > best[0]:
                        best = (ratio, name, case_line)
            if best is not None and best[0] >= DUPLICATE_RATIO:
                findings.append(
                    (best[0], path.relative_to(REPO).as_posix(), line, test, best[1], best[2])
                )
    return sorted(findings, reverse=True)


def gate_duplicates(strict):
    """A test outside the corpus that repeats a corpus case is the copy to drop."""
    findings = duplicate_findings()
    if not findings:
        print("ok   duplicates: no test repeats a corpus case")
        return True
    print(f"!    duplicates: {len(findings)} test(s) repeat a corpus case:")
    for ratio, path, line, test, case, case_line in findings:
        print(f"       {ratio:.2f} {path}:{line} {test} ~ tests/repros/corpus.py:{case_line} {case}")
    print("       keep the corpus case (it is compared against Chrome too) and drop the"
          " copy, or say why the test needs one")
    return not strict


#: The module the offline gate deliberately makes unavailable.  The static
#: pre-check derives its list of native-bound modules from the package's own
#: import graph, so adding an import that pulls in the engine cannot slip past a
#: hand-maintained list.
NATIVE_ROOT = "pyv8c.pyv8"


def _top_level_imports(tree):
    """``(module, line)`` for imports a module executes while being collected.

    Function bodies and classes are left out: they do not run at collection
    time.  ``try``/``if``/``with`` bodies do run, so they are walked.  Relative
    imports are resolved by the runtime pytest pass that follows this check.
    """
    found = []

    def visit(nodes):
        for node in nodes:
            if isinstance(node, ast.Import):
                found.extend((alias.name, node.lineno) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    found.append((node.module, node.lineno))
                    found.extend(
                        (f"{node.module}.{alias.name}", node.lineno) for alias in node.names
                    )
            elif isinstance(node, ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
                for handler in node.handlers:
                    visit(handler.body)
            elif isinstance(node, (ast.If, ast.With)):
                visit(node.body)
                visit(node.orelse)

    visit(tree.body)
    return found


def _package_import_graph():
    """``{module: [top-level imports]}`` for the shipped package."""
    package_root = REPO / "pyv8c"
    graph = {}
    for path in package_root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        parts = list(path.relative_to(package_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        graph[".".join(parts)] = [name for name, _ in _top_level_imports(tree)]
    return graph


def native_bound_modules():
    """Modules whose import reaches the embedded engine, directly or transitively."""
    graph = _package_import_graph()
    native_prefix = NATIVE_ROOT + "."
    bound = {"pyv8c._native", "pyv8c._native._native", "pyv8c._native.audio", "pyv8c._native.dom"}
    bound.update(m for m in graph if m == NATIVE_ROOT or m.startswith(native_prefix))
    while True:
        added = {
            module for module, imported in graph.items()
            if any(name in bound or name == NATIVE_ROOT or name.startswith(native_prefix)
                   for name in imported)
        } - bound
        if not added:
            return bound
        bound |= added


def _guard_lines(tree):
    """Lines of module-level ``pytest.importorskip("pyv8c.pyv8")`` calls.

    Both a bare call and the ``pyv8 = pytest.importorskip(...)`` form count; a
    call inside a test function is too late to protect a top-level import.
    """
    lines = []
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(statement):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "importorskip" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == NATIVE_ROOT):
                lines.append(node.lineno)
    return lines


def offline_guard_findings():
    """``(path, line, module)`` for unit tests that reach the engine unguarded."""
    bound = native_bound_modules()
    findings = []
    unit = REPO / "pyv8c" / "tests" / "unit"
    for path in sorted(unit.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        triggers = [(line, name) for name, line in _top_level_imports(tree) if name in bound]
        if not triggers:
            continue
        guards = _guard_lines(tree)
        if not guards or min(guards) > min(line for line, _ in triggers):
            line, name = min(triggers)
            findings.append((path.relative_to(REPO).as_posix(), line, name))
    return findings


def gate_offline():
    """``tests/unit`` has to run with no native extension on the import path.

    A static pre-check runs first: a unit test whose top-level imports reach
    the embedded engine must call ``pytest.importorskip("pyv8c.pyv8")`` before
    them.  Without it the module fails during collection and pytest reports a
    traceback instead of the file that broke the offline rule.
    ``PYV8C_OFFLINE`` asks ``tests/conftest.py`` to hide the extension package
    and also keeps this gate's own test from recursing.
    """
    findings = offline_guard_findings()
    if findings:
        print("!    offline: tests/unit imports the native extension without a guard:")
        for path, line, module in findings:
            print(f"       {path}:{line} imports {module}")
        print(f'       add pytest.importorskip("{NATIVE_ROOT}") above those imports,'
              " or move the test to tests/browser / tests/integration")
        return False

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO / "pyv8c")
    environment["PYV8C_OFFLINE"] = "1"
    # Editable installs and the root pytest configuration can otherwise put the
    # embedded engine back on sys.path, masking an unintended dependency.  The
    # conftest finder is the single implementation of "no native extension".
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", "pytest.ini",
         "pyv8c/tests/unit", "-q", "--no-header"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        tail = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        summary = tail[-1] if tail else ""
        print("ok   offline: tests/unit runs without the native extension"
              + (f" [{summary[:110]}]" if summary else ""))
        return True
    print("!    offline: tests/unit failed:")
    for line in [line for line in result.stdout.strip().splitlines() if line.strip()]:
        print(f"       {line}")
    if result.stderr.strip():
        print(result.stderr, file=sys.stderr)
    return False


def gate_companions(base, strict):
    paths = changed_paths(base)
    findings = companion_findings(paths)
    if not findings:
        print(f"ok   companions: {len(paths)} changed path(s), tests move with them")
        return True
    print(f"!    companions: {len(findings)} implementation file(s) changed with no test:")
    for path in findings:
        print(f"       {path}")
    print("       add or update a test, or say why this change cannot be tested")
    return not strict


def gate_startup(runs, record):
    if record:
        minimum, median, modules = measure_startup(runs=runs)
        if median is None:
            print(f"!    startup: cannot measure ({modules})")
            return False
        baseline = {
            "min_ms": round(minimum, 1),
            "median_ms": round(median, 1),
            "modules": modules,
            "runs": runs,
            "recorded": datetime.date.today().isoformat(),
        }
        BASELINE_FILE.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"ok   startup: recorded {baseline['median_ms']} ms (budget {round(startup_budget(baseline), 1)} ms)")
        return True

    if not BASELINE_FILE.exists():
        print("--   startup: no baseline recorded; run with --record-startup")
        return None

    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    minimum, median, modules = measure_startup(runs=runs)
    if median is None:
        print(f"--   startup: not measured ({modules}); pyv8c.pyv8 has to be importable for this gate")
        return None

    budget = startup_budget(baseline)
    verdict = f"startup: {median} ms median (min {minimum}), budget {round(budget, 1)} ms"
    if median <= budget:
        print(f"ok   {verdict}")
        return True
    print(f"FAIL {verdict} -- {round(median - budget, 1)} ms over the budget recorded from"
          f" {baseline['median_ms']} ms; fix the regression, or --record-startup if the"
          f" new number is the one you mean to keep")
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="HEAD", help="git ref to diff against (default: HEAD)")
    parser.add_argument("--gates", default=",".join(GATES), help=f"comma-separated subset of {','.join(GATES)}")
    parser.add_argument("--strict", action="store_true", help="treat warnings and skipped gates as failures")
    parser.add_argument("--runs", type=int, default=3, help="startup measurement repeats")
    parser.add_argument("--record-startup", action="store_true", help="write today's startup as the budget")
    arguments = parser.parse_args(argv)

    selected = [name.strip() for name in arguments.gates.split(",") if name.strip()]
    unknown = [name for name in selected if name not in GATES]
    if unknown:
        parser.error(f"unknown gate(s): {', '.join(unknown)}")

    results = []
    if "layers" in selected:
        results.append(gate_layers())
    if "native-layout" in selected:
        results.append(gate_native_layout())
    if "companions" in selected:
        results.append(gate_companions(arguments.base, arguments.strict))
    if "startup" in selected:
        results.append(gate_startup(arguments.runs, arguments.record_startup))
    if "offline" in selected:
        results.append(gate_offline())
    if "duplicates" in selected:
        results.append(gate_duplicates(arguments.strict))

    failed = sum(result is False for result in results)
    skipped = sum(result is None for result in results)
    passed = sum(result is True for result in results)
    if skipped:
        print(f"{'FAIL' if failed or arguments.strict else 'SKIP'}: {passed} passed, {failed} failed, {skipped} skipped")
    else:
        print(f"{'FAIL' if failed else 'PASS'}: {passed}/{len(results)} gate(s) clean")
    return 1 if failed or (arguments.strict and skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
