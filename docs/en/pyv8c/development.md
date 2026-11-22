# Development Guide

[简体中文](../../pyv8c/development.md) | **English**

[Documentation Index](../README.md)

## Environment and Working Conventions

The `dev` extra, source checks, and tox require `pytest>=8.4` and `pytest-asyncio>=1.4,<2`. The shared test fixtures register the runtime event-loop factory through the plugin.

Install the project with CPython 3.12 at the repository root, and confirm that the code loaded is from this repository:

```powershell
$env:PYTHONUTF8 = "1"
python312 -m pip install -e ".[dev,reference]"
python312 -c "import pyv8c; from pyv8c import pyv8; print(pyv8.__file__, pyv8c.__file__)"
```

Before building, you must configure the V8 build directory; see the native project documentation. When using Python 3.15, first build the ABI3 wheel with Python 3.12 as described below.

Before making changes, record the repository's working-tree state and preserve existing modifications. Verify the actually loaded native binary, interpreter, IDL, environment profile, and Chrome; after modifying C++, you must rebuild the extension in use and must not run Python tests only against the old binary.

For core rules, see [Architecture](architecture.md): fixes go into the layer that actually owns the behavior and state, and Web APIs do not depend directly on Page controllers; caches state the owning instance, invalidation, and release conditions explicitly, and do not establish process-global mutable page state.

## Building from Source

### Windows Local Build

When building a distribution package locally, first prepare V8 according to the [pyv8 documentation](../../../pyv8/README.md), then use CPython 3.12 at the repository root:

```powershell
py -3.12 -m pip install build wheel
py -3.12 -m build --wheel --outdir dist .
```

The generated `pyv8c-*-cp312-abi3-win_amd64.whl` is a single-package distribution.

### Linux Local Build

Ubuntu 22.04 x86-64 can prepare V8 and build the wheel natively on Linux, using a GIL-enabled
CPython 3.12, a C++20 compiler, and Python build tools:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel build
python setup.py build_v8
python -m build --wheel --no-isolation
python -m pip install dist/pyv8c-*-linux_x86_64.whl
```

If a proxy is needed, pass `--proxy=http://主机:端口` to `build_v8`. The first build downloads
the V8 and Chromium toolchains, which takes considerable time and disk space; control parallelism with `PYV8_V8_JOBS`.
The above Linux wheel has been verified for basic operation on Ubuntu 22.04 x86-64 and does not imply compatibility with all Linux distributions.

### Windows, Linux, and macOS CI

The repository's GitHub Actions workflow `Build wheels` can be triggered manually to select and build wheels for the three platforms in parallel.
All three platforms use GitHub-hosted runners; Linux uses Ubuntu 22.04 x64 and does not require a local VM.
In **Actions → Build wheels → Run workflow**, select the required platforms. The corresponding artifacts are
`windows-wheels`, `linux-wheels`, and `macos-arm64-wheels`.

Each platform's V8 cache key combines the SDK/GN args/V8 source hash with the runner image version.
The image version is a suffix, so an image bump still falls back by prefix to the previous cache built with
the same SDK, GN args, and sources, avoiding a V8 rebuild. V8 is downloaded and rebuilt only when those build
inputs change, which takes considerable time and disk space. Fallback also tries the old exact key layout to
reuse caches saved before the key change (one rebuild per platform during migration). Restored caches are
checked against the GN stamp. Verify available resources and cache effectiveness in GitHub Actions.
All platform jobs run an installed-wheel smoke test, audio and DOM component regressions, and a native crash smoke; Linux and macOS also check the native parameter bridge, and the Windows wheel test matrix also runs the architecture layering gate. None of this means the full browser test suite passes.

## Implementing an Interface

Interface declarations use `pyv8c.idl.interface`. Methods commonly use `fn_name`, and properties commonly use `get_name` and `set_name`; named/indexed and cross-origin handlers use the corresponding `__v8_*__` conventions. Member surface and property flags are based on the IDL; Python handles argument conversion, state, and actual semantics, and does not hand-write shapes already fixed by the baseline.

The generic Feature runtime mechanism lives in `pyv8c/pyv8c/features/runtime.py`, built-in declarations in `pyv8c/pyv8c/features/catalog.py`, and core interface assembly in `pyv8c/pyv8c/features/core.py`. Feature implementation modules explicitly export FEATURE and declare dependencies in FeatureSpec. Adding optional features should not make the base package import load all implementations or the native runtime. Pure metadata, extractors, and basic resource records must still support import without a native environment. For the directory ownership of Web APIs, see [Architecture](architecture.md#web-api-ownership).

Before development, first establish a minimal failing scenario, then fix it and add a regression test. Ordinary JS errors, host failures, execution termination, and resource failures are handled separately; when broadly catching `BaseException`, you must confirm whether execution control signals need to be propagated, and must not keep the host alive by swallowing termination and continuing the page flow.

Async callbacks validate cancellation, document generation, and identity. Real network, Python blocking, and worker-thread result delivery must not bypass the owner rules. When adding a public interface, update type declarations and callers at the same time; new lifecycle records must have a unified terminal state and a release path.

## Test Channels

```powershell
# Run native module and framework tests from the repository root, excluding slow
python -m pytest -q

# Run only browser framework tests
python -m pytest -c pytest.ini pyv8c/tests -q

# Regular main suite without Chrome comparison (with the native crash smoke)
python -m pytest -c pytest.ini pyv8c/tests/unit pyv8c/tests/integration pyv8c/tests/browser pyv8c/tests/native -q

# Native crash smoke only (separate child processes)
python -m pytest -c pytest.ini pyv8c/tests/native -q

# Slow lifecycle, heap budget, and policy review
python -m pytest -c pytest.ini pyv8c/tests -o addopts=--strict-markers -m slow -q

# Repeat race-marked cases at least five rounds
python -m pytest -c pytest.ini pyv8c/tests -o addopts=--strict-markers -m race --count=5 -q

# Architecture, change companion, startup, offline import, and duplicate case checks
python tools/check_policy.py
```

The default pytest configuration excludes slow, so `-m slow` also overrides addopts to avoid selector conflicts. Race tests may also be marked slow, and repeated runs likewise require the override. Do not run Chrome comparison in parallel by default.

When modifying native support, run the build, tests, and layout check at the repository root:

```powershell
python312 setup.py build_ext --inplace
python -m pytest -c pytest.ini pyv8/tests -q
python pyv8/tools/check_layout.py
```

The build requires the V8 SDK, compiler, and related settings required by this repository. Do not overwrite the target binary while other processes still have it loaded, and do not clean up processes not created by this run.

## Test Layers and Placement Rules

The five directories under `pyv8c/tests/` are not grouped by feature module but by **what a test needs in order to run** and **where its assertion gets its authority**. `tox.ini`, `pytest.ini` and `tools/check_policy.py` reference them directly, so putting a new test in the wrong layer breaks a gate.

| Directory | Source of truth | Browser needs | Typical content |
| --- | --- | --- | --- |
| `unit` | Plain Python assertions | Must import without a compiled extension | Static architecture checks, IDL/metadata, argument conversion, CookieJar, tooling scripts |
| `browser` | One Web API's return value inside pyv8c | Offline (`default_loader` returns an empty 200), no Playwright | DOM/BOM interfaces, events, storage |
| `integration` | Whether several subsystems still hold together | Local HTTP/WebSocket servers, Workers, timers, thread teardown | Navigation, resources, execution budgets |
| `native` | Whether a child process stays alive | Separate process + `-X faulthandler` | `test_native_smoke.py`; a C++ segfault kills the pytest process, so the exit code can only be asserted from outside |
| `repros` | Whether real Chrome and pyv8c agree | Local Chrome + Playwright (`reference` extra) | The behaviour corpus in `corpus.py`; each case runs on both sides |

Placement rule: no V8 -> `unit`; one page behaviour -> `browser`; cross-component or real I/O -> `integration`; worried about a C++ crash -> `native`; needs an answer checked against Chrome -> `repros`.

Three hard constraints:

- The py312–py315 wheel matrix in `tox.ini` runs only `pyv8/tests` + `tests/unit` + `tests/browser` + `tests/integration`, deliberately excluding `native` and `repros`: the matrix validates one abi3 wheel across four CPython versions, cannot depend on a locally installed Chrome, and need not repeat a child-process smoke test four times.
- The `offline` gate of `tools/check_policy.py` sets `PYV8C_OFFLINE=1`, which makes `tests/conftest.py` hide `pyv8c.pyv8`, and then runs only `tests/unit`. Therefore **any unit test whose top-level imports reach the native extension must call `pytest.importorskip("pyv8c.pyv8")` before all `pyv8c` imports**. The gate runs a static pre-check first: it derives the transitive set of native-bound modules from the package's own import graph (including `pyv8c._native` and its compatibility imports) and reports the offending file and line. `@pytest.mark.native` only means "this case needs the compiled extension" and cannot replace that guard, because collection has already failed by then. A clean CI checkout must also set `PYV8C_OFFLINE=1`; otherwise `pytest.importorskip` re-raises `cannot import name '_pyv8'` instead of skipping.
- `pytest.ini` registers the `native`, `race` and `slow` markers and defaults to `-m "not slow"`. **`@pytest.mark.native` is not the `native/` directory**: the former marks a case as requiring the extension and also appears in `unit`; the latter means the test must be verified in a separate child process.

`repros` checks both pyv8c's fixed expectations and behaviour across engines. The default `pytest -q` collects it too. The general reference is system Chrome **158.0.8127.36**. Permission policy inventory, performance entries, cross-document animation frames, and request headers use Chrome for Testing **154.0.8030.0**, which matches the IDL baseline. The independently captured `chrome-environment.json` still uses **156.0.8078.80**. When diagnosing a version difference, run the same case against both releases and preserve its `expected` value.

`native/` is wired into the `Build wheels` workflow: the Windows wheel test matrix and the Linux and macOS build jobs each run the native crash smoke after installing the wheel, and the Windows matrix also runs the `layers` architecture gate on Python 3.12. When the source tree has no compiled extension the smoke test loads the installed wheel automatically; locally it keeps using the source tree as before. The `layers` gate spawns child processes that really import Web modules and the `startup` gate constructs a Browser, so both need a compiled extension and stay with the wheel jobs and local pre-commit; the PR gate in `.github/workflows/source-checks.yml` runs only `check_license.py`, the three source-only policy gates and the offline `tests/unit` suite.

## Publishing wheels

Run **Build wheels** on `main` with Windows, Linux, and macOS enabled. Linux x64 builds on Ubuntu 22.04, repairs the wheel to `manylinux_2_35_x86_64` with `auditwheel`, and installs and tests the repaired wheel.

Once the builds and tests pass, run **Publish pyv8c wheels** on the same commit. Enter the numeric ID from the build run's URL as `build_run_id` and start with the default `testpypi` target. The workflow checks the run source, commit, project name, version, and platform tags before uploading. To rehearse installation, download the pyv8c wheel from TestPyPI and use regular PyPI to install its dependencies.

Configure a GitHub Trusted Publisher separately on TestPyPI and PyPI. Use a pending publisher for a first release. Set the project to `pyv8c`, owner to `ConlinH`, repository to `pyv8c`, and workflow to `publish.yml`. Set the environment to `testpypi` on TestPyPI and `pypi` on PyPI. Create the matching environments in GitHub and restrict deployment to `main`. Once the TestPyPI installation is verified, you can explicitly select `pypi` for the same build run.

## Chrome Behavior Comparison

```powershell
$env:PYV8C_CHROME = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$env:PYV8C_CHROME_BASELINE = "C:\path\to\chrome-154.0.8030.0\chrome-win64\chrome.exe"
python -m pytest -o addopts=--strict-markers -q -ra
```

When `PYV8C_CHROME_BASELINE` is unset, those cases also use `PYV8C_CHROME`. If neither variable is set, the harness looks for system Chrome. Missing Chrome or Playwright can cause skips. The command above includes `slow` cases. The corpus uses software rendering for clear-color readback. WebGL extension comparisons check uniqueness, availability, and version-specific semantics, while the local fixed expectations also check exact counts. Device enumeration, audio sample rate, and client hints use inputs that both engines can reproduce.

Both sides use the same HTML, origin, response headers, scripts, and operation sequence. Real visits are used to discover problems, while a local server or fixture is used for stable acceptance; a fixture serves only explicit resources and fails on unknown requests. First compare navigation, resources, scripts, events, and readiness conditions to locate the earliest causal divergence, then inspect the interface surface or environment branches.

Distinguish framework defects, missing features, configuration/version/device differences, server responses, and probe interference. WebGL, media, permission, and network differences especially require recording environmental factors. A page's environment checks should be distilled into repeatable browser semantics, not replaced by constant success, site patches, or script substitution instead of framework fixes.

## Extraction and Performance

For IDL and environment capture commands, see [IDL Format](idl-format.md). Before generating artifacts, verify the source version and first write to an independent output for validation; do not overwrite the Chromium/V8/profile baseline without authorization.

The Window order comes from the environment profile's `bindings.own_order`; when updating the order, re-capture the target Chrome and record the version and capture conditions. The IDL records only interfaces and members and no longer stores a separate Window order baseline. The runtime uniformly installs the actually exposed properties, and the order of V8 built-in properties that cannot be satisfied is reported by the difference diagnosis.

For performance, profile before optimizing; measure in serial alternation for at least five rounds on the same machine, same interpreter, same source/binary identity, and same workload, recording the median, minimum, variance, and post-shutdown reclamation. Changes without a stable gain or that introduce a behavior regression are not kept.

```powershell
python benchmarks/benchmark.py --runs 5 --output .\benchmark-result.json
python benchmarks/benchmark.py --cases startup_offline,cold_compile,page_cycle --runs 5 --output .\benchmark-startup.json
python benchmarks/benchmark.py --list
```

These commands run measurements. `benchmark.py --record` and the policy tool's `--record-startup` rewrite the performance baseline and must not be used to eliminate regressions. Environment changes and workload changes require separate explanation; non-equivalent numbers must not be compared directly.

## Current implementation

This section describes how the Python Web API layer is organized and where its limits lie, so changes can be placed in the owning layer. For native bridging and thread ownership see the [bridge kernel](../pyv8/bridge-kernel.md); for task and wakeup protocols see [task scheduling](task-scheduling.md).

### Web APIs and selectors

Ordinary IDL methods bind their callbacks directly, with no extra argument-wrapper layer. `DOMString` returns exact Python strings directly while JS objects still perform actual conversion. Optional attribute hooks and observer state explicitly default to `None`, so unobserved writes do not repeatedly create `AttributeError`.

Queries and stylesheet matching share `matches_group`; selector matching does not create a self-referencing recursive closure per candidate. Full queries narrow candidates by their rightmost tag. Groups preserve tree order and deduplication; first-result queries remain lazy. Existing AST and tree-version indexes are reused, but attributes and relationships are read fresh, without cached DOM results across queries.

### HTML parsing and DOM construction

Regex matching uses offsets into the original input. One standard-library attribute tokenizer emits one decoded record per start tag; consumed records are retired and the completed token buffer is cleared. Blocking scripts retain unconsumed records; `document.write` keeps its insertion point. Node insertion, removal, shadow-root and parser-created state have explicit defaults, while subclass and instance hooks remain dynamic. Adoption, resource discovery, events and invalidation are not skipped.

The C++ DOM code in `pyv8c/native/dom` links with the Rust audio code in `pyv8c/native/src/audio` to produce `pyv8c._native`. The extension uses the CPython 3.12 stable ABI and has no V8 link dependency. Project code uses `from pyv8c._native import audio, dom`. The DOM component initializes EventTarget/Node base fields and the live child view, and dispatches ordinary HTML tokens in batches of at most 1,024 during ordinary body construction of active documents. Constructors remain the original Python classes and every insertion still invalidates tree caches; custom setters, descriptors, setter changes during initialization and live views retain their behavior, and each insertion checks the parent's currently bound insertion and adoption methods. Only fresh, same-document nodes without insertion hooks or shadow trees use direct insertion; other nodes use normal tree operations. Scripts, event attributes, resources, templates, raw text and foreign namespaces remain semantic parser steps. Tokens retire and parser positions advance before constructors, and each batch observes operation control so timeouts, stops and signals remain effective.

Python tokenization remains and the tree builder is not a complete HTML5 parser; DOMParser and fragment construction follow the same semantic steps.

### Startup and IDL assembly

Each document view lazily indexes descendant members instead of traversing descendants for every missing name. Ancestor precedence, descendant document order, main-world preference and location-specific profile omissions are preserved. Registered bases reuse declarations for their own interfaces; unregistered mixins remain target-specific. Weak-keyed, per-view caches isolate environment profiles and release with their views, preventing different profiles of the same document from contaminating each other. Import timing is unchanged.

### Style computation and layout

Document and ShadowRoot rule caches hold `StyleRules`: candidates are selected by required ID, class or tag terms in the rightmost compound, then complete selector lists are matched in source order with their highest matching specificity. Unanchored groups remain candidates; when every rule is unanchored, existing source order is traversed directly. Indexes retain no elements or match results, read live DOM state, and use the owning tree's existing `style_version` invalidation.

The applied stylesheet source is unified: rule insertion/deletion and declaration edits notify their owning document, while DOM text retains its independent semantics. `CSSFontFaceRule` preserves live font declarations, and editing a detached old sheet does not affect its replacement. Existing selector, conditional-rule and layout support limits still apply; this does not implement a complete Chromium renderer.

### Same-thread Browser and dual entry points

The public entry points are `pyv8c.sync_api` and `pyv8c.async_api`, sharing one
same-thread Browser/Profile/Page implementation; the root package no longer exports
Browser, and objects from the two entry points cannot be mixed. Pages and iframes stay
on the application owner, while Workers keep dedicated threads. Navigation, host HTTP
and waits are awaited in the async entry point and return directly in the sync one;
page operations and live handles still reject foreign-thread use. Browser shutdown
stops admission and joins Worker, network and loader threads; cancelling one shutdown
waiter does not cancel cleanup. Both entry points share one execution kernel and do not
automatically switch to or fall back on the old thread model.

Five serial alternating Windows / CPython 3.12.9 / V8 15.4.68 rounds use the same native binary,
200 warmup calls and 12000 measured operations. Scalar evaluation falls from 64.421 to 8.489 μs;
object snapshots from 87.086 to 21.949 μs. Native execution stays near 1.59 μs. Full records are in
`build/runtime-cutover-measurements-final-20261117`. These are local results; Linux and macOS
remain unverified for this phase. Correctness checks retain and separately report baseline failures.

## Delivery Checklist

State the specific problem, trigger conditions, the fix in the owning layer, the regression test, and the actual verification results. Distinguish passed, unverified, skipped, and known failures; preserve the original evidence of race failures and do not claim the root cause has disappeared based on a successful rerun. Do not loosen assertions, widen exclusions, or update baselines to mask failures.

Documentation maintains only currently available interfaces and known limitations. Commands, links, and examples must be verified after changes; topic files uniformly use lowercase hyphenated names, the index uses README.md, and the body text is uniformly in Chinese.
