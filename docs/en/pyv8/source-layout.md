# Source layout

[简体中文](../../pyv8/source-layout.md) | **English**

`src/` groups the binding by ownership and dependency direction. Run
`python pyv8/tools/check_layout.py --list` from the repository root for the current file inventory; compiled
sources, headers and included `.def` macro tables are all checked. File counts
are generated rather than maintained in this document.

This is the *where*. For the *why* -- how the isolate, the realm, the pair table
and the handle types hold each other up, and what each layer is for -- see
[architecture.md](architecture.md).

## Groups

| group | layer | owns | files |
| --- | --- | --- | --- |
| `src/` | 0 | portability shims and the private module entry point | `pyv8.h` `pyv8.cpp` `polyfill.h` `abi3.h/.cpp` `v8_compat.h` `boundary.h` `callback_boundary.h` `bridge_base.h` |
| `src/isolate/` | 1 | the V8 platform, the isolate, the realm and their lifetimes | `platform_state.*` `v8_string_cache.*` `isolate.*` `isolate_state.*` `isolate_scope.*` `entry.h` `execution.*` `runtime.*` `context.*` `context_names.cpp` `context_resources.cpp` `watchdog.*` `error_hooks.*` `microtask_queue.*` |
| `src/values/` | 2 | values crossing the boundary, error mapping, handle registration | `convert.h` `convert_to_python.cpp` `convert_to_javascript.cpp` `error_family.*` `serialization.h` `js_exception.*` `jsvalue.*` `jsnull.*` `jsundefined.*` `handle_resource.h` |
| `src/handles/` | 3 | the Python types that stand for JavaScript values | `jsobject.*` `jsfunction.*` `jspromise.*` `jstypedarray.*` `script.*` `serialization.cpp` |
| `src/tools/` | 3 | tracing -- the hook the interface layer wraps its entry points in -- and the devtools bridge | `trace.h` `debugger.*` |
| `src/interfaces/` | 4 | declarations into templates into callbacks | `idl_spec.*` `pyclass.*` `pyclass_handlers.*` `pyclass_interceptors.cpp` `pyclass_cross_origin_handlers.*` `v8tools.*` `pyclass_callbacks.cpp` `pyfunction.*` |

The checker reports the current counts and verifies build include roots.

`tools/` sits at the same layer as `handles/`, not above `interfaces/`. The
tracing hook is included *by* the interface layer (`pyclass_handlers.cpp`,
`v8tools.cpp`, `pyclass_callbacks.cpp`, `pyclass_interceptors.cpp`,
`pyfunction.cpp`) and by the realm's owner and its name installer, so it is
infrastructure those files use rather than something that uses them. The devtools
bridge is reached only from the composition points below.

## Allowed includes

A file may include headers from its own layer and from lower ones. Reaching
upward is what stops a layer from being one. A small set of files exists to tie layers
together. The table describes their roles; `COMPOSITION_EDGES` enumerates the
exact permitted source/target pairs, rather than exempting entire files:

| file | may include | why |
| --- | --- | --- |
| `pyv8.h` | exact declared edges | private module composition; not a dependency of lower units |
| `pyv8.cpp` | exact module registration edges | the module entry point registers every type |
| `context.cpp` | declared edges through layer 4 | the realm owner builds the interface templates, creates the realm's handles, converts explicit-checkpoint exceptions and wires the devtools bridge |
| `context_names.cpp` | 1-4 | installing names into a realm and keeping the registry behind them is what `install`, `expose` and the lazy factory protocol do, so this is the part of a context's life that knows the interface layer; it also provides registered constructors to error conversion and installs the failed-access callback at realm entry |
| `context_resources.cpp` | 1-3 | the pair table materialises a Python handle for a JavaScript object |
| `convert_to_python.cpp` | 1-3 | one direction of the converter: it builds handles for objects coming in, which the handles layer defines |
| `convert_to_javascript.cpp` | 1-4 | the other direction, which additionally has to know the class and function machinery |
| `isolate_state.cpp` | 1-4 | disposal is where what was built on an isolate is taken apart, including the shared interface templates, and an inspector belongs to the isolate it was created against, so it has to go before `Isolate::Dispose` |
| `runtime.cpp` | 1-2 | shutdown refuses to dispose V8 while serialized WebAssembly modules are still alive |
| `wasm_streaming.cpp` | 1-2 | aborting a streaming compile converts its Python reason into a JavaScript value or error |
| `error_hooks.cpp` | 1-2 | reporting an isolate's errors means handing JavaScript values to Python, which is conversion |
| `error_family.cpp` | 2-3 | constructing an error silences tracing, which the tracing hook owns |

Everything else, headers included, follows the plain rule. Two facts the checker
reports rather than hides:

* `bridge_base.h` contains only base contracts. `pyv8.h` is private to the module
  composition unit; lower files cannot include it. Isolate-only macros live in
  `isolate/entry.h`, and crossing macros in `values/crossing.h`.
* A stale include is a violation nobody meant to write. The first pass found
  `isolate_state.cpp -> pyclass.h`, `exception.cpp -> script.h` and
  `context.cpp -> jsfunction.h` / `pyfunction.h`; the pass that flattened
  `tools/` found one more, `context.cpp -> trace.h`: the symbols those headers
  declare are not used by the files that include them. Dropping them belongs to
  the move that touches those directories, and the compiler is the judge.

## Where a new file goes

1. Which layer does it describe: the isolate and its lifetime, a value, a handle,
   an interface, or the tracing and dispatch that sits under the interfaces?
2. Which files will include it? Nothing below the layer it lands in may.
3. If the answer is "it has to reach upward", it is a composition point: write it
   into this document, add it to `pyv8/tools/check_layout.py`, and keep the list short.

## Historical tree migration

Staged so that every step is a plain move plus a build-configuration change, each
verified on its own.

| stage | moves | also |
| --- | --- | --- |
| S1 | `isolate/` (21 files) and `tools/` (7 at the time, `faker/` included) | the two `setup.py` places, `faker/Faker.cpp`'s three `../` includes, `doc/devtools.md`, the MSVC project paths |
| S2 | `values/` (11) and `handles/` (10) | - |
| S3 | `interfaces/` (13) | rename `binding_callbacks.cpp` to `pyclass_callbacks.cpp`; rename `pyclass_handlers_internal.h` to `pyclass_handlers.h` and move the interceptor entry declarations out of `pyclass.h`; move the internal-field layout from `pyclass.h` to `values/handle_resource.h`, where the handles that use it can reach it without reaching up; drop the stale includes above; make the checker enforcing |
| S5 | nothing moves | splitting the three files that were past a thousand lines: `convert.cpp` into one file per direction, `pyclass_handlers.cpp` into the machinery and the entry points V8 calls, `context.cpp` into the realm's lifecycle and the names it installs.  Each split lands on its own, with the same verification as a move, because a split rewrites the tree's shape without meaning to change its behaviour |

Two facts made the moves cheap. `setup.py` collects its sources by recursing over
`src` -- a new group must not need a new glob there -- and `include_dirs` names
each group, so a file that changes group keeps the bare includes it already had.

The last subdirectory went further than flattened: `tools/faker/` was taken apart
and the feature it implemented was removed. What the binding no longer has, in the
order it was reachable, is:

* the `faker` object itself -- the JavaScript-visible helpers `print`, `run_js`,
  `run_js_file`, `structuredClone`, `get_arg`, `set_arg`, `new_document_all`,
  `native_wrapper` and the `v8_version` string -- which `pyclass.cpp` used to
  install on the global of every class declaring `IdlSpec.global_this`;
* the dispatch that made a member implemented in JavaScript possible at all:
  `CallbackType` and the `callback_type` field on `AttributeSpec`/`MethodSpec`,
  the three `FakerJsBindingCallback` entry points that resolved a callback name
  against `globalThis.faker`, and the lazy-constant path (`IdlSpec.
  constants_callback` and `ConstantCallbackSpec`) that resolved a name the same
  way;
* `js_function_repr`'s `faker.raw_tostring` hook, which nothing could define any
  more once the object was gone -- a function's repr is its own conversion again;
* the string-cache ids only those code paths read: `Faker`, `RawToString`, `Name`,
  `Length` and `Callback`.

`IdlSpec.global_this` stays: it says which Python class backs the JavaScript
global object. The tracing hook stays as well and is not part of any of this -- it
was only *in* the `faker` namespace, so with the object gone it answers to
`trace::`, which is what its call sites say now. What remains in `tools/` is
`trace.h` and the devtools bridge.

The following commands and counts record the original refactor, not the current
repository layout or current test totals. After each move: delete `build/temp*` (MSVC names object files after the source
path, so a move would otherwise link stale objects), rebuild, then run

```
python -m pytest tests -q                          # 338 passed
python -m pytest tests/unit tests/integration -q   # 372 passed, 1 environment failure
python -m pytest tests/browser -q                  # 210 passed
python benchmarks/bench_object_pairs.py
python benchmarks/bench_strings.py
```

The one unit failure is `test_idl_extractor.py`'s renderer snapshot test,
which needs a `_pyv8env/renderer` checkout that is not part of this tree.

Nothing in this plan changes behaviour, so the criterion is that those numbers do
not move beyond their own run-to-run noise: a move that changes a test result is
a bug in the move.


## Current enforcement

The composition list in the table describes roles, not file-wide exemptions.
`COMPOSITION_EDGES` pins each current allowed dependency by relative source and
target path. New includes, same-name files in other directories and lower-layer
includes of the module composition header fail enforcement. Header consumers
must declare their runtime/crossing/entry dependencies explicitly. Use
`python pyv8/tools/check_layout.py --list` for the current inventory; migration counts
and test results above are historical records.


The checker resolves quoted paths exactly against the including file and the
literal Extension include roots. Equivalent quote/list/tuple spellings are
accepted; nonliteral roots fail with a diagnostic. A basename existing elsewhere
in the tree does not make an incorrect include directory valid. buffer_range.h
belongs to handles and owns no Context or cache; its callers supply guarded entry.
