# Architecture

[简体中文](../../pyv8/architecture.md) | **English**

What the binding is made of, and what holds the pieces together.  `source-layout.md`
says where a file goes; this says why the parts are what they are.

## The layers

```
Python                 pyv8/            the package: flags, tracing helpers, devtools
                          |
extension              pyv8.cpp         the module: init, shutdown, the free functions
                          |
interfaces             interfaces/      declarations -> templates -> callbacks
                          |
handles                handles/         the Python types that stand for JavaScript values
values                 values/          values and errors crossing the boundary
isolate                isolate/         the isolate, the realm, and their lifetimes
shims                  *.h at the root  ABI3 and V8 API adapters
```

A file may include its own layer and the ones below it.  The exceptions are
listed as exact relative source/target edges in `pyv8/tools/check_layout.py` under `COMPOSITION_EDGES`, each with a reason:
an aggregate header, the module entry point, and the five files whose job is to
tie layers together (a realm building interfaces, a converter reaching into the
handle types it produces).  `python pyv8/tools/check_layout.py --enforce` holds the
tree to that, and to two build files: `setup.py` has to offer every group as an
include root, because sources include their neighbours by bare name, and
`pyproject.toml` has to name package data that exists.

## Lifetimes, in the order they are nested

**The isolate** owns the V8 heap, the interned strings (`V8StringCache`, one entry
per `V8StringId`), the pair table's owners, and the callback data.  Everything
that belongs to it is torn down in `IsolateState::shutdown`, in one order that
matters: reporters first (they reach into Python and must not run while the
isolate is being taken apart), then the inspectors built on it, then the Python
references, then `Isolate::Dispose`.

**The realm** (`context_c`) owns a V8 context, the classes installed into it, and
the Python objects it must keep alive.  Those references are listed once, in
`context_gc_fields`, which `context_traverse` and `context_finish_close` both
walk -- the two used to be written out by hand, one edit apart from disagreeing.

**The pair table** is what makes `Py -> JS` identity work: a Python object and the
JavaScript object that stands for it in one realm, keyed by the Python object's
address.  It lives in `ContextLifetime`, which outlives the context so that a
handle closed later reads an empty table rather than freed memory, and it holds
no strong reference on either side -- the entry *is* the `PythonObjectOwner` that
holds the one strong reference to the Python object, and it is erased in the same
step that releases it.  Neither side can be kept alive by the table, and neither
can be collected out from under it.

**Handles** (`js_object`, `js_function`, `jspromise`, `jstypedarray`, `js_value`)
are Python objects standing for V8 values.  Each holds a persistent handle, counts
it in `live_v8_handles_`, and participates in the context's handle registry: a
context can be closed, which invalidates every handle, or collected, which happens
only when no handle is left.  A handle that outlives its realm answers with an
error -- "no active V8 isolate" or "closed" -- and never reads V8.

## Threads

An isolate can host multiple realms; iframe realms can share their parent heap.
V8 execution is mutually exclusive per isolate. Native entry can originate on
different threads, while pyv8c separately owns task/DOM thread affinity.  `ExecutionScope` is how a Python entry point says "I am entering
this realm": it takes the isolate lock and builds the handle and context scopes the
work needs.  `isolate/entry.h` provides Isolate-only scopes for initialization, teardown and
already-entered handle construction. It does not substitute for Context entry or
post-wait validation.  Waiting for the isolate lock releases the GIL, and so does the window
in which V8 actually runs: `CallbackBoundary` takes it back at every V8 -> Python
entry, which is the discipline the pair table and the owner lists rely on
(`context_resources.cpp` says so where it matters).

## The boundary

`values/` is the direction-of-travel split: `convert_to_javascript.cpp` and
`convert_to_python.cpp`.  Between them sit the two rules that keep the boundary
honest:

* A value crossing back and forth keeps its identity where the host can tell the
  difference: `ps_from_js`/`js_from_py` remember the pairs they made.
* A failure has to arrive as the failure it was.  A JavaScript exception becomes a
  Python exception carrying the original value; a Python exception raised inside a
  callback is re-thrown into JavaScript; and a failure with no exception of its own
  is given a message, because CPython reports "returned NULL without setting an
  exception" as a `SystemError` about the binding rather than about the value.

## Errors

The binding owns the standard error family: `pyv8.JsError` and the six types beside it,
each deriving from the builtin its error stands for, so a host catching that builtin keeps
catching what it always did.  A host joins the family by inheriting one of them, and that
inheritance is the whole declaration -- there is no table to install, nothing to declare
before a realm runs, and no name to keep in step.  A class the family does not contain is
the host's own failure, which no realm is ever shown, because the crossing is terminated
and the exception is raised at the boundary that began it.  The family is stated in one
place, `values/error_family.cpp`: a table the module publishes itself, so no second copy
of the names exists to fall out of step.  A realm constructor that is not one of the
standard names is declared on the class itself, the way `DOMException` says it is shown
as `DOMException` with `__v8_error_shape__` -- one triple of (constructor name,
constructor argument attributes, copied (python, JavaScript) field pairs).
What a realm is shown is the error itself: the host's own frames and file paths are not
put on the object, so a script that catches it -- or reports it -- learns nothing about
the host's side of the failure.
Uncaught exceptions and unhandled rejections are reported through the hooks installed
with `Context.set_error_hooks`, which is also where a host that wants the page's
`error` event gets its chance.

## Interfaces

`interfaces/` turns a declaration into templates and callbacks:

```
IdlSpec (Python)   ->  py_class (one per class per context)  ->  FunctionTemplate
                   ->  CallbackData (one per member per context) -> info.Data()
```

`AttributeSpec`/`MethodSpec`/`ConstantSpec` are the host's declarations;
`InstallAttribute` and `InstallOperation` turn them into templates; at call time
`IsolateState::callback_target` turns `info.Data()` back into the spec, and
`py_class_handler` reaches the Python callable by `HandlerId`.  Interceptors are
per class, and the window's own name table is a separate, layered thing:
`pyclass_interceptors.cpp` implements both halves, and the note in
`pyv8/tests/test_window_name_table.py` explains what an index means to it.

## What is checked, and where

| Decision | Held by |
|---|---|
| Layer direction, build files | `pyv8/tools/check_layout.py --enforce` |
| The public surface: stubs, exports, the extension | `pyv8/tests/test_public_surface.py` |
| Identity and lifetime of the pair table | `pyv8/tests/test_memory_ownership.py` |
| Attribute protocol: hooks, errors, subscript | `pyv8/tests/test_jsobject.py` |
| The window name table and indices | `pyv8/tests/test_window_name_table.py` |
| Cost of crossing, installing, strings | `benchmarks/` |

## Structured-clone callback lifetime

`handles/serialization.cpp` owns the serializer and its temporary Python stream
wrappers; `values/serialization.h` declares the conversion protocol. Streams are
valid only on the callback's creating thread and are invalidated on both success
and failure. Serializer values and transfer entries use the ordinary conversion
path, including closed-context and foreign-isolate checks. Host callbacks may
reenter JavaScript through V8's explicit allow scope. A Python callback failure
is checked even when V8 reports a successful write or read, preserving the original
exception rather than returning a value with an exception set.


## Architecture contracts after the 2026-10-14 review

`bridge_base.h` exports Python/V8 ABI shims and exception/GIL boundaries only;
`pyv8.h` is private module composition. Lower units include each contract they
consume. `ExecutionScope` is mandatory for Context/handle operations that can wait;
owner and handle state must be checked after entry. Host-only diagnostic reads do
not need a V8 entry. `import_handle` and trace configuration use this contract.

`IsolateLockScope` alone modifies private entry counters/locks. IsolateState owns
Inspector waiter registration and deferred Context close, including references
and failed-allocation rollback. ContextResources registries are private to the
Context lifecycle functions and IsolateState; dependency unlinking is an operation
of that owner, and HandleRegistration delegates to the registration functions.

Isolate selection declares DefaultOnly, ActiveOrDefault or Explicit. It does not
change the public API's established policy: GC uses default, Context queries and
callback constructors use active/default, explicit owners are validated explicitly.
pyv8c retains scheduling and Web API ownership; this kernel supplies entry safety.

The extension declares every .h/.def as a build dependency. Its manifest records
source-content identity, ABI, compiler inputs, actual compiler and binary digest;
failed builds do not publish a new manifest. Rebuilding unchanged sources must
skip compilation. Architecture negative tests must reject new composition edges.


## The explicit microtask queue

`MicrotaskQueue` belongs to the isolate layer: it records the creating thread and
holds counted V8 roots. A context attaches to a queue with `microtask_queue=`, and
several contexts may share one queue. Standalone calls that omit the argument still
use the original automatic mode, so context identity must not be treated as
microtask-queue identity. In explicit mode, ordinary V8 entry points do not run
checkpoints automatically; the HTML cleanup boundary has to be wired up by the host
scheduling layer.

Queue storage, thread checks and release live in `microtask_queue.*`; type
registration goes through the isolate interface. Checkpoint exception conversion
stays at the composition boundary in `context.cpp`, so the isolate layer does not
depend back on the crossing-level exception conversion above it. See
[the explicit microtask queue](microtask-queue.md).
