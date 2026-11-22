# Bridge kernel

[简体中文](../../pyv8/bridge-kernel.md) | **English**

> This article records historical bridge experiments and the measurement commands of that time. The current unified entry point is [benchmarks/README.md](../../../benchmarks/README.md); standalone scripts such as `benchmarks/bridge.py` have been folded into it.

The bridge requires the CPython 3.12 Stable ABI. A wheel built on 3.12 is
intended for regular GIL-enabled CPython 3.12 through 3.15 on the same OS
and architecture. Free-threaded Python and subinterpreters are not supported.
The selected V8 version is 15.4.68.

## Values

`Context.exec_js`, JS function calls, property access and host callback
arguments return primitives, the original Python host object, or a JS handle.
They do not enumerate JS objects or invoke accessors to convert them.
The `preserve_arguments` switch and `_to_python` method have been removed.

```python
result = context.exec_js('({items: [1, 2]})')
assert isinstance(result, pyv8.JSObject)
assert result.to_python() == {'items': [1, 2]}
```

`to_python()` recursively materializes arrays and plain objects, preserving
cycles and shared references. It can run getters and propagate their errors.
Other objects, including functions and typed arrays, remain handles.
JS identity is preserved when handles return to V8; Python wrapper identity
is not promised. Function property access preserves its receiver.

Binary data crosses whole, in one call in either direction. `_bytes()` on a
`JSObject` returns the bytes a buffer holds or a view looks at; Python `bytes`
handed back into a realm arrive as a fresh `ArrayBuffer`. Reading a buffer element
by element across the boundary is what these replace.

In pyv8c, `Page.evaluate()` materializes the result on the page executor;
`Page.evaluate_handle()` returns the unmaterialized value. Internal Web API
code uses handles until its Web IDL conversion explicitly needs Python data.

## Ownership and entry

`ExecutionScope` pins the Context and subject, acquires isolate access and
validates the Context again after waiting. Same-thread nested entries reuse
the existing locker. Waiting releases the GIL, and so does the window in which
V8 runs: a crossing into JavaScript gives it up and every callback boundary
takes it back before host code runs. Cleanup holds it. The current scheduling
topology is unchanged.

Contexts move from Active to Closing to Closed. Closing rejects new entry.
In-flight callbacks retain their metadata and finish before deferred teardown.
Closing invalidates all registered handles, including function receivers,
scripts and exception state. Explicit close and GC use the same resource
cleanup; GC does not dispatch page lifecycle events.

ContextResources indexes host owners, callback anchors, wrappers and realm
dependencies. Python references are visible through tp_traverse. The Isolate
coordinates V8 entry and pending cleanup, not strong Python callback roots.
V8 External values point to native ownership records rather than Python
objects. Weak callbacks enqueue preallocated records; decrefs occur outside
the V8 weak phase. Cross-isolate handles are rejected. Same-isolate transfers
conservatively retain the source until either Context closes.

PythonBoundary translates native C++ failures at Python API slots;
CallbackBoundary translates failures at installed host callback boundaries.
Allocation failure is not a promise that a V8 heap OOM can be recovered.
This is an in-process embedding, not a hostile-code security sandbox.

## Performance and validation

`exec_js` reuses the Context entry owned by `ExecutionScope`; attribute callbacks establish their scopes and active-context check once. A migratable Isolate's Locker lives inside its scope; waiting for the lock still releases the GIL. Short one-byte JS strings decode directly as Latin-1, letting CPython select its ASCII representation.

One `to_python()` call shares one identity table, with the first 16 objects stored inline and hashed overflow for larger graphs. V8 Local handles track moving objects during GC; handle equality preserves cycles and aliases. The table lasts only for this materialization, without a persistent result cache. The private dense key array returned by enumeration is read as data; source getters, Proxy checks and value reads retain reentrant GIL-release paths.

If V8 has caught a getter/Proxy exception, its Python wrapper is constructed after clearing the converter's fallback Python error. This prevents CPython from replacing the original failure with `SystemError`; the original thrown JS value keeps its identity.

`benchmarks/bridge.py` runs five child processes, each with five warmup batches
and twenty measured batches. JSON includes raw samples, binary SHA-256 and
source revision. Run both checkouts with the same V8/compiler configuration.
The hot-path geometric mean includes callbacks, methods, accessors, host
object roundtrip and exceptions. Trace, DOM and page lifecycle are reported
separately. Run benchmarks without concurrent builds or tests.

```powershell
$env:PYTHONPATH = "$PWD\pyv8;$PWD\pyv8c"    # run from the repository root
python benchmarks/bridge.py --output "$env:TEMP/bridge-current.json"
python benchmarks/lifecycle.py --count 1000 --output "$env:TEMP/lifecycle-current.json"
python benchmarks/plot_results.py --input "$env:TEMP/lifecycle-current.json" --output-prefix "$env:TEMP/lifecycle-current"
python -m pytest -q
```

The target is at least 15% lower hot-path geometric mean elapsed time, with
no stable regression above 5% for an individual core or lifecycle workload.
Cross-platform and sanitizer results must be reported separately from local
Windows tests; a configured validation command is not a successful run.
`pyv8/tools/validate_linux.sh` enables ASan/UBSan for the bridge. Full V8 coverage
also requires a V8 library built with matching sanitizer instrumentation.


## Native review corrections (2026-10-14)

`JSObject._buffer_view()` exports only fixed-size backing stores. Resizable
ArrayBuffers, growable SharedArrayBuffers and their views raise TypeError;
`bytes(value)` remains available for copying their current range. A backing-store
reference alone cannot pin committed memory against ArrayBuffer.resize(). Fixed
buffer exports remain read-only, honor buffer flags, and reject PyBUF_WRITABLE.

Entering a different isolate while already holding an isolate never waits for a
contended entry lock: it raises RuntimeError. Uncontended nesting, returning to
an already-held isolate and ordinary top-level waiting remain supported. Hosts
should dispatch contended cross-isolate work after returning from the callback.
Legacy entry macros also stop before constructing V8 scopes when entry fails.

Watchdog deadlines use a growing stack: every nested scope has its own deadline
entry and removes exactly that entry on unwind. Nesting past eight levels no
longer drops an outer deadline. Timeout and exception unwind tests run in child
processes and verify that subsequent execution still succeeds.

`current_context()` and `incumbent_context()` without an explicit isolate query
this thread's active isolate first. Outside execution they retain the default
isolate fallback. An explicit Isolate argument still queries that isolate.
This prevents Worker callbacks from acquiring a parent's unrelated isolate merely
to discover their own context during serialization or transferable processing.

Context queries observe entries already owned by the calling thread. Outside
execution they return `None`, without opening a top-level Locker merely to inspect
an idle context or waiting for another thread to leave JavaScript. Same-thread
cross-isolate nesting still permits querying an outer Context. A callback querying
a contended foreign isolate retains the existing nested-execution error. Explicit
argument type/closed-state validation remains. Initialization is unchanged: first
use may still start V8.

V8 15.4 initializes thread state at top-level Locker entry and resets interrupt
flags. Previously an idle query could therefore clear notifications needed by
background optimization, stalling TurboFan work between short scripts. Both query
APIs now share a read-only implementation that checks existing entries first.
Timeouts remain; no synchronous JIT or private V8 compiler API is forced.

`Isolate(thread_bound=True)` binds VM access and close to its creating thread.
Page execution groups and Workers always create and close these isolates on their
executor owners. Entry checks affinity and retains host mutual exclusion, without
constructing a V8 Locker that repeatedly initializes thread state and loses compiler
interrupts between calls. Background compilation remains concurrent. Migratable
`Isolate()` instances use the Locker for VM entry; both modes support same-thread nesting.

Explicitly close a bound isolate on its owner before that thread exits. VM access and explicit close from another thread raise an error.
If another thread drops the final Python reference to the Isolate and no thread is entering or executing it, the destructor uses a V8 Locker to complete teardown.
Foreign GC cleanup of individual objects such as Contexts stays queued until the next owner entry or close. Thread-safe operation termination and foreground
notification waits remain available to control threads. Regressions cover recovery,
foreign GC, context observation and mixed-mode nesting.


## Build and ownership contracts (2026-10-14)

Normal builds track project .h/.def and source content, not just .cpp timestamps.
`build/pyv8-extension.json` associates inputs and compiler identity with the built
binary SHA256. Header-only edits invalidate the artifact without requiring force.

Context/handle work that waits uses ExecutionScope. import_handle revalidates both
the target and source handle after acquiring the isolate. Closed handles never
recreate dependency records. Entry counters and pending queues are private;
Inspector and close registration own their references and rollback locally.


## Maintainability contracts (2026-10-14)

The build resolves and initializes the actual compiler before comparing inputs.
Tool paths, executable digests, SDK include/library paths and compiler options
participate in the fingerprint; an automatically selected toolchain change must
rebuild the extension. Build preparation code is also an input. Layout checking
reads literal Extension include roots through AST and resolves quoted includes
against the including file and those roots, rather than matching basenames.

Handler storage and IdlSpec constructor metadata expand handler_table.def.
Browser contract tests require each handler to have a declared stub keyword,
a None default, immutable replacement, and consistent callback validation.
BufferRange owns its BackingStore and describes the current byte offset/length;
copying and fixed-store exporting share that calculation. Resizable storage
continues to refuse zero-copy exports. Ranges are not cached across JS execution.

A DeadlineScope owns an opaque ticket; Context manual deadlines have a separate
owner. clear_deadline removes only the newest manual ticket of that Context,
and is a no-op when none exists. Logical close releases all manual tickets of
that Context, even if binding metadata cannot be freed until the outer entry
returns. Cleanup can withdraw that deadline's undelivered idle abort, but cannot
withdraw explicit terminate() or heap termination. Idle expiry is delivered on
the next crossing once unless its owner clears/closes it first.

Termination transitions distinguish requesting, V8 issuance, pending delivery,
active unwind, completion and resumption. A later entry cannot cancel an abort
before TerminateExecution has actually returned; nested unwind cannot claim the
outer crossing has finished. DeadlineScope start/stop are void and idempotent;
allocation failures propagate through the existing native exception boundary.
These ownership changes do not claim recovery from every V8 fatal OOM scenario.

Context(timeout=None) selects zero; exec_js(timeout=None) inherits the Context
setting. Non-finite or unrepresentable deadlines raise ValueError. Invalid
argument parsing returns TypeError before touching V8 values. The actual keyword
is exec_js(program=...), now correctly named in the stub; callers should replace
an unsupported script= keyword with program= or use the positional argument.
