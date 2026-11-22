# pyv8 User Guide

[简体中文](../../pyv8/user-guide.md) | **English**

This document corresponds to the built-in V8 engine in pyv8c 0.2.0. The ABI3 target is GIL-enabled CPython 3.12 through 3.15 and V8 15.4.68; 3.15 has been partially validated on Windows.

After the bridge kernel refactor, JS objects return handles by default; when you need a list/dict, explicitly call
`result.to_python()`. For interfaces, ownership, and validation details, see the [bridge kernel documentation](bridge-kernel.md).

pyv8's primary goal is not to replace a browser, but to provide a set of sufficiently low-level Python/V8 bridging APIs
for constructing prototype objects, constructors, property descriptors, and property interceptors that approximate Chromium behavior.

## Contents

- [Installation and Build](#installation-and-build)
- [Context and Script Execution](#context-and-script-execution)
- [Type Conversion](#type-conversion)
- [JavaScript Objects and Functions](#javascript-objects-and-functions)
- [Chromium-like Classes and Prototypes](#chromium-like-classes-and-prototypes)
- [Property interceptor](#property-interceptor)
- [Cross-origin and Trace](#cross-origin-and-trace)
- [TypedArray and Promise](#typedarray-and-promise)
- [Exceptions and Timeouts](#errors-and-timeouts)
- [Multiple Isolates and Lifecycle](#multiple-isolates-and-lifecycle)
- [DevTools Debugging](#devtools-debugging)
- [API Quick Reference](#api-quick-reference)
- [Testing](#tests)
- [FAQ](#faq)

## Installation and Build

### Python and Platform Requirements

- CPython 3.12 through 3.15 (3.15 has so far only been partially validated on Windows)
- Standard CPython builds with the GIL enabled
- A compiler with C++20 support
- V8 static libraries matching the target platform and CPU architecture
- An `icudtl.dat` matching that V8 build

Windows is currently the primary development and fully tested platform. For Windows source builds, we recommend Visual Studio 2022
and the Windows 10/11 SDK. Linux/macOS build branches are already included in the build scripts, but you should still verify on the target platform
the compiler, V8 artifacts, and wheel.

### Installing a Prebuilt wheel

```bash
python -m pip install path/to/pyv8c-0.2.0-cp312-abi3-win_amd64.whl
```

`cp312-abi3` means the same extension can be loaded by GIL-enabled CPython 3.12 through 3.15. ABI3 does not make the binary portable across
operating systems or CPU architectures; for example, a Windows `.pyd` cannot be loaded on Linux.

### Preparing Build Tools

The following source build commands are all run from the repository root.

```bash
python -m pip install --upgrade pip setuptools wheel build tox
```

On Windows you also need:

- The "Desktop development with C++" workload in Visual Studio
- Windows 10/11 SDK
- Git
- Sufficient disk space and memory for compiling V8

### Selecting and Building V8

View the latest official tag for each release branch:

```bash
python setup.py list_v8_versions --limit=15
```

Select a version interactively:

```bash
python setup.py build_v8 --select
```

Specify a version explicitly:

```bash
python setup.py build_v8 --v8-version=15.4.68 --v8-jobs=8
```

The selection is written to a local `.pyv8-version`. Subsequent `build_ext` and wheel builds reuse that version;
deleting `.pyv8-version` restores use of `V8_VERSION` from the repository.

The same V8 tag, CPU, and GN arguments reuse the `.pyv8-build` cache. To force a full rebuild, use:

```bash
python setup.py build_v8 --v8-version=15.4.68 --force
```

### Network and Proxy

Specify a proxy explicitly:

```bash
python setup.py build_v8 --select --proxy=http://127.0.0.1:7890
```

Ignore the process environment and Git global proxy, and force a direct connection:

```bash
python setup.py build_v8 --select --no-proxy
python setup.py list_v8_versions --no-proxy
```

You can also use environment variables:

```powershell
$env:PYV8_PROXY = "http://127.0.0.1:7890"
python setup.py build_v8 --select
```

```powershell
$env:PYV8_NO_PROXY = "1"
python setup.py build_v8 --select
```

### Building the Python Extension

Compile in place from the repository root, producing `pyv8c/pyv8c/pyv8/_pyv8.pyd` (Windows) or `_pyv8.abi3.so` (Linux):

```bash
python setup.py build_ext --inplace
```

Force a rebuild after V8 or C++ source changes:

```bash
python setup.py build_ext --inplace --force
```

Build a distributable wheel:

```bash
python -m build --wheel --outdir dist .
```

Building with Python 3.12 produces a `cp312-abi3` wheel for use with GIL-enabled Python 3.12 through 3.15.

### Using Existing V8 Build Artifacts

If you already have a self-compiled V8, there is no need to download it again:

```powershell
$env:PYV8_V8_VERSION = "15.4.68"
$env:PYV8_V8_HOME = "D:\v8-workspace\v8"
$env:PYV8_V8_OUT = "D:\v8-workspace\v8\out\x64.release"
$env:PYV8_ICU_DATA = "D:\v8-workspace\v8\out\x64.release\icudtl.dat"
python -m build --wheel --outdir dist .
```

Linux/macOS uses the same variable semantics:

```bash
PYV8_V8_VERSION=15.4.68 \
PYV8_V8_HOME=/opt/v8 \
PYV8_V8_OUT=/opt/v8/out/x64.release \
PYV8_ICU_DATA=/opt/v8/out/x64.release/icudtl.dat \
python -m build --wheel --outdir dist .
```

`PYV8_V8_OUT` must contain:

- Windows: `obj/v8_monolith.lib`
- Linux/macOS: `obj/libv8_monolith.a`
- `icudtl.dat`

### Build Environment Variables

| Variable | Purpose |
| --- | --- |
| `PYV8_V8_VERSION` | Official numeric V8 tag |
| `PYV8_BUILD_V8` | Set to `1` to download and build V8 |
| `PYV8_FORCE_V8_BUILD` | Set to `1` to ignore the V8 build cache |
| `PYV8_V8_JOBS` | Number of parallel Ninja jobs |
| `PYV8_BUILD_ROOT` | Root directory for depot_tools and version cache |
| `PYV8_DEPOT_TOOLS` | Use an existing depot_tools directory |
| `PYV8_PROXY` | Proxy used by Git, depot_tools, and CIPD |
| `PYV8_NO_PROXY` | Set to `1` to force a direct connection |
| `PYV8_VS_HOME` | Windows Visual Studio installation directory |
| `PYV8_VS_VERSION` | Visual Studio year, defaults to `2022` |
| `PYV8_WINDOWS_SDK` | Windows SDK root directory |
| `PYV8_V8_HOME` | Existing V8 source directory |
| `PYV8_V8_OUT` | Existing V8 output directory |
| `PYV8_ICU_DATA` | ICU data file matching the V8 static library |
| `PYV8_CPPGC_MICROTASK_QUEUE` | When recognizable V8 Ninja metadata is missing, set to `1` or `0` according to the actual build; the build fails if it conflicts with recognizable metadata |
| `PYV8_GN_ARGS` | Append custom GN arguments |
| `PYV8_TARGET_CPU` | Target CPU, currently supports `x64` and `arm64` |

For automatic download builds, a checkout or sync failure terminates immediately; dirty work trees are not ignored and the build does not fall back to unverified sources. Existing usable artifacts can be specified explicitly via `PYV8_V8_HOME` and `PYV8_V8_OUT`; ABI switches must be based on the build evidence of that artifact, not guessed from the version.

#### Windows 2026 Build Example

The project's V8 builder downloads the source tag specified by `V8_VERSION` and uses depot_tools
to sync dependencies, generate GN configuration, and run Ninja. On Windows you can explicitly pin Visual Studio
and the Windows SDK to avoid auto-detecting the wrong version when multiple toolchains exist on the machine:

```powershell
$env:PYV8_V8_VERSION = "15.4.68"
$env:PYV8_VS_HOME = "D:\soft\Microsoft\vs2026"
$env:PYV8_VS_VERSION = "2026"
$env:PYV8_WINDOWS_SDK = "D:\Windows Kits\10"
$env:PYV8_BUILD_ROOT = "D:\src\pyv8env\pyv8c\pyv8\.pyv8-build"
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"

python setup.py build_v8
python setup.py build_ext --inplace
```

`PYV8_VS_HOME` must point to the Visual Studio installation root (containing `VC` and `Common7`); `PYV8_WINDOWS_SDK` must point to a Windows SDK root
that contains `Include` and `Lib`.

If the network is restricted, set `PYV8_PROXY` or `PYV8_NO_PROXY=1`. After the build completes, the V8
artifacts are located at `.pyv8-build\versions\15.4.68\v8\out\pyv8-x64.release`, where
`obj\v8_monolith.lib` and `icudtl.dat` in the same directory must come from the same build.

### Initialization Timing

`from pyv8c import pyv8` does not start V8: the extension only registers types, notes the `icudtl.dat` in its own directory, and performs two
cheap checks (free-threaded CPython and non-main interpreters are rejected). The V8 platform and default Isolate start only on **first
use** — on the first `pyv8.Context(...)`, `pyv8.Isolate()`, `pyv8.Script(...)`,
`pyv8.new(...)`, `pyv8.current_context()`, `pyv8.v8_gc()`, `pyv8.low_memory_notification()`,
or the default Isolate fallback for typed arrays/Promises. Every check after that is just a boolean check.

`pyv8.v8_version()` and `pyv8.hidden`/`pyv8.unconstructable` do not start V8.

Startup behavior is determined by `pyv8.configure(...)`, which can only be called before first use:

```python
from pyv8c import pyv8

pyv8.configure(params=["--expose-gc"], icudtl_path="/path/to/icudtl.dat")
print(pyv8.is_initialized())  # False
context = pyv8.Context(object)  # V8 starts here
print(pyv8.is_initialized())  # True
```

Parameter type errors are reported at `configure()` time. Calling it after V8 has started raises `RuntimeError`
(startup parameters and the ICU data file cannot be changed after startup).

### ICU Data Constraints

At build time, the SHA-256 of `icudtl.dat` is computed and compiled into `_pyv8`. On the first use of V8,
the file hash is recomputed before V8 starts. If the file is not the one used at build time, this call throws a
`RuntimeError`, preventing mismatched ICU data from causing a native crash or OOM during `Intl` initialization;
if the file cannot be read, the error gives an actionable hint, and you can point to it with `pyv8.configure(icudtl_path=...)`.

An identical hash only means the file contents have not changed. You should still ensure that `v8_monolith` and `icudtl.dat` come from the same
V8/ICU build; do not replace either file on its own.

### Heap Limits and External Memory

V8's heap limit is inferred from physical memory by default, and can also be specified before startup with `--max-old-space-size` (see
`pyv8.configure`); once specified, it is the heap budget for this run. `_pyv8` watches usage on the isolate's thread:
crossing 60% of the budget triggers one rate-limited collection (returning only the garbage portion), and crossing 80% terminates
the running script at the next safe point. On successful termination, the script receives `JavaScriptTerminated`, after which you can release references and continue;
`Context.heap_terminations` records how many times this has happened.

Checks occur at API interrupt safe points and at the end of GC. After termination, large objects may still be referenced; limited
diagnostic and cleanup allocations are allowed, and continued noticeable growth requests termination again. Recovery state is recorded based on usage at actual exit; after collection drops
below the threshold it resets, belongs to the current Isolate, and is not shared with other instances. It does not allow sustained allocation that bypasses the recovery limit.

Termination must happen before V8's own heap exhaustion: V8 aborts the process directly when it exhausts internally during collection, so calling
`TerminateExecution` from inside has no chance to take effect; therefore `near_heap_limit_callback` serves only as a last-resort
fallback: it requests the same termination and provides the existing recovery headroom in one shot, with an absolute cap no greater than twice V8's initial old-space
limit. V8 may recheck usage immediately after the callback returns, so small incremental growth is not enough to guarantee exit.
Recovery headroom is not a normal budget available to scripts, and not all allocations are guaranteed recoverable: a large native allocation may
exhaust the headroom before the next safe point, causing V8 to terminate the process directly. Tasks requiring fault isolation must run in a separate subprocess.

Python memory held by JavaScript (compiled script source, host objects referenced by JavaScript) is invisible to V8, so the bridge layer
has `_pyv8` report it to V8, letting V8 decide when to collect based on that:

```python
isolate = pyv8.Isolate()
context = isolate.context(object)
print(isolate.external_memory)  # bytes reported to V8
```

This value grows as host objects are wrapped and falls as they are released, and is 0 after the isolate closes. Script source is measured by character count,
and host objects are estimated at a fixed cost (the stable ABI cannot query the size of arbitrary objects), so it expresses the scale of pressure, not
memory accounting.

## Context and Script Execution

### Creating a Context

```python
from pyv8c import pyv8


class Window:
    __v8_idl_spec__ = pyv8.IdlSpec(
        global_this=True,
    )


context = pyv8.Context(
    Window,
    trace=False,
    ctx_type="Top",
    timeout=1.0,
)
```

Constructor parameters:

| Parameter | Description |
| --- | --- |
| `global_this` | Python class or instance used as the underlying implementation of the JavaScript global object |
| `trace` | Whether to enable native binding observation immediately |
| `traceable` | Whether to configure the ability to enable observation later when initially off |
| `trace_callback` | Context-level event callback; uses the default log when omitted |
| `trace_scripts` | Tuple of non-empty script URL Glob patterns; records only binding events whose current JS stack source matches; records all sources when omitted |
| `ctx_type` | Context label passed to trace, e.g. `Top` or `Iframe` |
| `timeout` | Default script timeout in seconds; `0` means unlimited |
| `isolate` | Specify a custom `pyv8.Isolate`; uses the default Isolate when omitted |

When you pass a Python class, Context instantiates it with no arguments, so the class constructor cannot require mandatory parameters.
To prepare state in advance, pass an instance:

```python
class Window:
    def __init__(self, origin):
        self.origin = origin


window = Window("https://example.com")
context = pyv8.Context(window)
```

### Executing JavaScript

```python
result = context.exec_js("20 + 22")
assert result == 42
```

Specifying a script filename helps with exception stacks and DevTools location:

```python
result = context.exec_js(
    "(new Error('trace')).stack",
    filename="https://example.com/assets/app.js",
)
```

A single execution can override the Context default timeout:

```python
context.exec_js("runTask()", timeout=0.5)
```

For a string program, compilation and execution share this one budget and are not re-timed after compilation finishes. `timeout=0` overrides
the Context default timeout, but does not cancel outer operation budgets such as `set_deadline`. Some V8 compilation phases cannot
respond to interrupts immediately, so the budget is also checked when compilation returns and `JavaScriptTerminated` is reported; precise preemption at an exact moment cannot be promised.

### Precompiling Script

```python
script = pyv8.Script(context, "40 + 2", "answer.js")
result = context.exec_js(script)
assert result == 42
```

`Script` belongs to the Isolate that created it and cannot be handed to a Context of another Isolate for execution.
Constructing `Script` directly uses the Context default timeout and inherits the outer operation budget; after a compilation timeout you can still
initiate subsequent operations in the same Context. When a precompiled object is executed again, it uses that `exec_js` call's budget.

### Accessing globalThis

`context.glob` returns the `JSObject` wrapper for the global object:

```python
global_object = context.glob
global_object.answer = 42
assert context.exec_js("globalThis.answer") == 42
```

Python attributes not defined on Context are automatically forwarded to the JavaScript global object:

```python
context.name = "pyv8"
assert context.exec_js("name") == "pyv8"

context.exec_js("globalThis.version = 2")
assert context.version == 2
```

Reading a nonexistent Context attribute raises `AttributeError`, while JavaScript still yields
`undefined`.

### Exposing Python Values

Positional arguments use the object's `__name__`:

```python
def add(left, right):
    return left + right


context.expose(add)
assert context.exec_js("add(20, 22)") == 42
```

Keyword arguments explicitly specify the JavaScript name:

```python
class State:
    pass


state = State()
state.value = 42
context.expose(browserState=state, payload={"ready": True})
```

Multiple values can be exposed at once:

```python
context.expose(add, State, state=state, config={"debug": False})
```

To expose members of a module you must name them one by one; keyword arguments are the explicit JavaScript names:

```python
import math

context.expose(sqrt=math.sqrt)
assert context.exec_js("sqrt(81)") == 9
```

### Current Execution Context

Inside a Python callback invoked from JavaScript, you can query the current Context:

```python
def inspect_context():
    assert pyv8.current_context() is context
    assert pyv8.incumbent_context() is context
    return 42


context.expose(inspect_context)
context.exec_js("inspect_context()")
```

When using a custom Isolate, you can also query by passing the Isolate explicitly.

## Type Conversion

### Python to JavaScript

| Python | JavaScript |
| --- | --- |
| `None` | `undefined` |
| `pyv8.Null` | `null` |
| `pyv8.Undefined` | `undefined` |
| `bool` | `boolean` |
| `int`, `float` | `number` |
| `str` | `string` |
| `bytes` | `ArrayBuffer` |
| `list`, `tuple` | `Array` |
| `dict` | plain `Object` |
| Python function/method | JavaScript function |
| Python class | JavaScript constructor/interface object |
| Python instance | JavaScript object with an internal Python owner |
| `JSObject`, `JSValue` | raw V8 value, must belong to the current Isolate |

Python `None` does not mean JavaScript `null`:

```python
context.expose(nothing=None, explicit_null=pyv8.Null)
assert context.exec_js("nothing === undefined") is True
assert context.exec_js("explicit_null === null") is True
```

Circular references and shared references in Python containers are preserved:

```python
root = []
child = {"root": root}
root.extend((child, child))
context.expose(root=root)

assert context.exec_js("root[0].root === root") is True
assert context.exec_js("root[0] === root[1]") is True
```

JavaScript `number` cannot losslessly represent arbitrarily large Python integers. When you need exact large integers, use `BigInt` on the JavaScript
side, or transfer them as strings.

### JavaScript to Python

| JavaScript | Python |
| --- | --- |
| `undefined` | `pyv8.Undefined` |
| `null` | `pyv8.Null` |
| `boolean` | `bool` |
| 32-bit integer | `int` |
| other `number` | `float` |
| `string` | `str` |
| `Array` | `JSObject`; explicit `to_python()` yields `list` |
| plain object literal | `JSObject`; explicit `to_python()` yields `dict` |
| custom object, Date, RegExp | `JSObject` |
| function | `JSFunction` |
| Promise | `JSPromise` |
| TypedArray | `JSTypeArray` |
| BigInt | `JSValue` |

Default conversion does not enumerate properties, read getters, or execute Proxy traps. Explicit materialization allows reading properties,
and a failed read propagates the exception; values that cannot be materialized remain handles. Materialization preserves circular and shared references of arrays and plain objects:

```python
value = context.exec_js(
    "const root = []; const child = {root}; root.push(child, child); root"
).to_python()

assert value[0]["root"] is value
assert value[0] is value[1]
```

When materializing explicitly, holes in sparse arrays are converted to `pyv8.Undefined`.

## JavaScript Objects and Functions

### JSObject

```python
person = context.exec_js(
    """
    new (class Person {
        constructor(name) {
            this.name = name;
        }
        greet() {
            return `Hello, ${this.name}`;
        }
    })("Alice")
    """
)

assert isinstance(person, pyv8.JSObject)
assert person.name == "Alice"
assert person.greet() == "Hello, Alice"

person.name = "Bob"
person["age"] = 18
del person["age"]
```

Common operations:

- `obj.name`, `obj[name]`: read a property
- `obj.name = value`, `obj[name] = value`: set a property
- `del obj.name`, `del obj[name]`: delete a property
- `dir(obj)`: enumerate properties
- `obj1 == obj2`: compare V8 object identity

JSObject retains the Context that created it. Before releasing a Context, release wrappers you no longer use first.

### JSFunction and Constructor Calls

```python
multiply = context.exec_js("(left, right) => left * right")
assert isinstance(multiply, pyv8.JSFunction)
assert multiply(6, 7) == 42
```

Call a JavaScript constructor:

```python
Uint8Array = context.exec_js("Uint8Array")
typed_array = pyv8.new(Uint8Array, [1, 2, 3])
```

The constructor and arguments of `pyv8.new()` must be convertible into the same Isolate.

### The JS wrapper for Python Objects

When JavaScript returns a Python owner object back to Python, it is restored to the original Python object by default. To access properties
added to that object on the JavaScript side, you can obtain the wrapper:

```python
class Payload:
    pass


context.expose(Payload)
payload = context.exec_js("const value = new Payload(); value.extra = 42; value")

wrapper = context.js_obj(payload)
assert wrapper.extra == 42
```

The same Python object keeps the same JavaScript identity within the same Context; in different Contexts or Isolates
it gets mutually isolated wrappers.

## Chromium-like Classes and Prototypes

### Constructor Basics

```python
from pyv8c import pyv8


class DOMPoint:
    __v8_idl_spec__ = pyv8.IdlSpec(
        constructor_behavior=pyv8.FlagConstructor.kNew,
        has_constructor=1,
        length=2,
    )

    def __init__(self, x=0, y=0, win=None):
        self.x = x
        self.y = y


context.expose(DOMPoint)
point = context.exec_js("new DOMPoint(10, 20)")
```

The Python constructor additionally receives a `win=<the Python global_this of the current Context>` keyword argument.
Classes exposed as JavaScript constructors should accept `win=None` or `**kwargs`.

### Class-Level Metadata

| Attribute | Purpose |
| --- | --- |
| `IdlSpec.length` | `length` of the JavaScript constructor |
| `IdlSpec.constructor_behavior` | Whether `new Interface()` and direct calls are allowed |
| `IdlSpec.has_constructor` | Whether to install `constructor` on the prototype |
| `IdlSpec.global_this` | Marks a global Window-like class (decides which Python object is the JavaScript global object) |
| `IdlSpec.skip_inherit` | Skip this intermediate base class when a subclass builds the prototype chain |
| `IdlSpec.undetectable` | Use V8 undetectable object behavior |
| `IdlSpec.code_like` | `eval`/`Function` stringifies instances of this class and compiles them (Blink's `TrustedScript` uses this); other objects are still returned as-is |
| `IdlSpec.unconstructable` | Throws a "not a constructor" error on construction |
| `IdlSpec.iteration_kind` | `IterationKind.none/indexed/pair/set`, selects the data semantics |
| `IdlSpec.array_methods` | Array method mask for indexed: `ArrayProto.iterator`=1, `.iterable`=31 |
| `IdlSpec.iteration_methods` | pair/set view mask, defaults to 31; iterator=1, entries=2, keys=4, values=8, forEach=16 |
| `IdlSpec.iteration_source` | Host factory; receives the receiver, returns a no-argument cursor that yields `(key, value)` or None each time |
| `IdlSpec.immutable` | Mark the prototype of an instance or a prototype as immutable |
| `IdlSpec.call` | Allow an instance to be called as a function in JavaScript |

`FlagConstructor`:

| Value | Behavior |
| --- | --- |
| `kDontAll` | Disallow both construction and direct calls |
| `kNew` | Allow only `new Interface()` |
| `kCall` | Allow function calls; the current implementation also accepts `new` |
| `kAll` | Allow both construction and function calls |

### Attribute and Operation Configuration

Explicit IDL members use immutable named descriptions; positional tuples are no longer accepted.

```python
from pyv8c import pyv8

class Calculator:
    __v8_idl_spec__ = pyv8.IdlSpec(
        attributes=(pyv8.AttributeSpec(name="value", getter="get_value"),),
        methods=(pyv8.MethodSpec(name="add", callback="fn_add", length=2),),
    )

    def get_value(self):
        return 42

    def fn_add(self, left, right):
        return left + right
```

`getter`, `setter`, and `callback` accept a Python callback name or a callable. Names are resolved once at binding time;
the explicit IDL path does not reflectively install undeclared members.
getters/setters use `cross_origin_get_check` / `cross_origin_set_check` to configure cross-origin rules.

Description objects are read-only; use `spec.replace(**changes)` to create a new configuration and share unchanged members.
For the full field list, registration restrictions, and breaking migration notes, see [idl-spec.md](idl-spec.md).

### Installation Location and descriptor flags

`FlagLocation`:

| Value | Installation Location |
| --- | --- |
| `kInstance` | own property of each instance |
| `kPrototype` | `Interface.prototype` |
| `kInterface` | The constructor/interface object itself |

`V8Attribute` can be combined bitwise:

| Value | JavaScript descriptor meaning |
| --- | --- |
| `kNone` | default attribute |
| `ReadOnly` | `writable: false` |
| `DontEnum` | `enumerable: false` |
| `DontDelete` | `configurable: false` |

For example:

```python
flags = (
    pyv8.V8Attribute.ReadOnly
    | pyv8.V8Attribute.DontEnum
    | pyv8.V8Attribute.DontDelete
)
```

### Constants

```python
from pyv8c import pyv8
class ReadyState:
    __v8_idl_spec__ = pyv8.IdlSpec(
        constants_value=(
            pyv8.ConstantSpec(name="UNSENT", value=0),
            pyv8.ConstantSpec(name="OPENED", value=1),
            pyv8.ConstantSpec(name="DONE", value=4),
        ),
    )
```

Constants are installed on the interface object and prototype with read-only, non-deletable descriptors.

### Prototype Inheritance

Python class inheritance maps to JavaScript constructor and prototype inheritance:

```python
class EventTarget:
    pass


class Node(EventTarget):
    pass


class Element(Node):
    pass


context.expose(Element)
assert context.exec_js("Element.__proto__.name") == "Node"
```

When generating large numbers of browser interfaces, templates are cached per Context and Isolate. Modifying already-installed Python class metadata
does not automatically rebuild old templates; create a new Context or explicitly use a new class object.

## Property interceptor

### Named property handler

Defining any combination of the following methods installs a named interceptor:

```python
from pyv8c import pyv8
class NamedCollection:
    def __init__(self):
        self.values = {"first": 1}

    __v8_idl_spec__ = pyv8.IdlSpec(name_get="__v8_name_get__", name_set="__v8_name_set__", name_query="__v8_name_query__", name_del="__v8_name_del__", name_enum="__v8_name_enum__")
    def __v8_name_get__(self, name):
        return self.values.get(name)

    def __v8_name_set__(self, name, value):
        self.values[name] = value
        return True

    def __v8_name_query__(self, name):
        if name not in self.values:
            return None
        return pyv8.V8Attribute.kNone

    def __v8_name_del__(self, name):
        return self.values.pop(name, None) is not None

    def __v8_name_enum__(self):
        return list(self.values)
```

Return conventions:

| Handler | Return Value |
| --- | --- |
| `IdlSpec.name_get` | property value; `None` means not intercepted |
| `IdlSpec.name_set` | non-`None` means handled |
| `IdlSpec.name_query` | `V8Attribute` integer; `None` means absent |
| `IdlSpec.name_del` | `bool` deletion result |
| `IdlSpec.name_enum` | sequence of names; `None` means no enumeration result provided |

### Indexed property handler

```python
from pyv8c import pyv8
class IndexedCollection:
    def __init__(self):
        self.values = [10, 20]

    __v8_idl_spec__ = pyv8.IdlSpec(index_get="__v8_index_get__", index_set="__v8_index_set__", index_query="__v8_index_query__", index_del="__v8_index_del__", index_enum="__v8_index_enum__")
    def __v8_index_get__(self, index):
        if 0 <= index < len(self.values):
            return self.values[index]
        return None

    def __v8_index_set__(self, index, value):
        while len(self.values) <= index:
            self.values.append(None)
        self.values[index] = value
        return True

    def __v8_index_query__(self, index):
        if 0 <= index < len(self.values):
            return pyv8.V8Attribute.kNone
        return None

    def __v8_index_del__(self, index):
        if 0 <= index < len(self.values):
            self.values[index] = None
            return True
        return False

    def __v8_index_enum__(self):
        return len(self.values)
```

`IdlSpec.index_enum` returns the number of consecutive indices to enumerate; V8 generates `0` through `length - 1`.

Python exceptions raised by a handler propagate to JavaScript; if not caught by JavaScript, they are restored to the Python
caller. A single handler failure should not break subsequent Context execution. Only exceptions inheriting the binding error family (`pyv8.JsError`
and related) enter JavaScript; the rest belong to the host itself (see "Python Exceptions Entering JavaScript").

Setters/deleters have one additional declaration: a class that also inherits `pyv8.JsInterceptorFailure` indicates this is a legacy
named/indexed property failure, and the native bridge handles it according to `PropertyCallbackInfo::ShouldThrowOnError()`:

- In non-strict mode the exception is cleared and the operation is treated as an intercepted failure;
- In strict mode the exception is propagated to JavaScript;
- This marker is only for named/indexed setters and deleters, and should not be added to ordinary business exceptions.

```python
from pyv8c import pyv8


class LegacyPropertyError(pyv8.JsTypeError, pyv8.JsInterceptorFailure):
    """A legacy write or delete that V8 may leave unraised."""


class LegacyCollection:
    __v8_idl_spec__ = pyv8.IdlSpec(index_set="__v8_index_set__")

    def __v8_index_set__(self, index, value):
        raise LegacyPropertyError(
            f"Failed to set an indexed property [{index}] on "
            "'LegacyCollection': Indexed property setter is not supported."
        )
```

At this point `collection[0] = value` fails silently in non-strict mode and produces a JavaScript
`TypeError` in strict mode.

## Cross-origin and Trace

### Cross-origin access check

Window-like objects can define an access check:

```python
from pyv8c import pyv8
class Window:
    __v8_idl_spec__ = pyv8.IdlSpec(cross_origin_check="__v8_cross_origin_check__")
    def __v8_cross_origin_check__(self, accessor):
        # self is the object being accessed; accessor is the Python global object initiating the access.
        return self.origin == accessor.origin
```

Returning `True` allows access; returning `False` denies it.

To provide restricted attributes when denying ordinary access, you can implement separate cross-origin handlers:

- `IdlSpec.cross_origin_name_get`
- `IdlSpec.cross_origin_name_set`
- `IdlSpec.cross_origin_name_query`
- `IdlSpec.cross_origin_name_del`
- `IdlSpec.cross_origin_name_enum`
- `IdlSpec.cross_origin_index_get`
- `IdlSpec.cross_origin_index_set`
- `IdlSpec.cross_origin_index_query`
- `IdlSpec.cross_origin_index_del`
- `IdlSpec.cross_origin_index_enum`

The parameters and return conventions of these methods are the same as ordinary named/indexed handlers.

`FlagCrossOriginCheck` in attribute and operation configuration decides whether the corresponding function template accepts
cross-origin receivers:

- `kCheck`: perform the receiver/cross-origin check
- `kDoNotCheck`: allow any receiver

### Environment Supplement Trace

Trace is a RAII observation scope at the native binding boundary: business callbacks still run only once, and on exit it reads existing arguments, return values, and exceptions.

`trace` controls the current observation state; `traceable` is retained as an explicit option for configuring the observation callback at creation time and no longer means installing an interceptor:

`trace_scripts=("https://example.test/sdk/*.js*",)` can filter by the full URL of the currently executing script before native event arguments are converted. Patterns support `*` and `?`, and query parameters also participate in matching; async callbacks are attributed to the JS function source at execution time. Host operations without a named script stack are not recorded after filtering is enabled. The callback argument format is unchanged.

- `trace=True`: observe immediately, and can be paused/resumed later.
- `trace=False, traceable=True`: initially off, and can be enabled later.
- Both `False`: observation capability is not enabled; enabling it later throws RuntimeError.
- `context.trace = False/True` must be assigned a bool and does not rebuild objects; switching is not allowed after the Context is closed.

```python
def trace(event, receiver, name, arguments, value, error, is_construct, ctx_type):
    message = pyv8.format_trace(event, receiver, name, arguments, value, error, is_construct, ctx_type)
    if message is not None:
        print(message)

context = pyv8.Context(Window(), traceable=True, trace_callback=trace, ctx_type="Top")
context.trace = True
context.exec_js("/* observe native binding operations */")
context.trace = False
```

Callback contract:

- `event`: `get`, `set`, `method`, `construct`, `has`, `descriptor`.
- `receiver`: Python object; the class for construct events; `None` when it cannot be resolved.
- `name`: JS property/method/constructor name, an integer for indexed access.
- `arguments`: tuple for method and construct events, `None` for property and probe events.
- `value`: read result, written value, or call result; bool for `has`/`descriptor`. `pyv8.Undefined` on get/method/construct failure; on set failure the written value is still kept, and the property is not read again after the write.
- `error`: `pyv8.Undefined` on success, the Python representation of the original exception value on failure.
- `is_construct`: whether called via `new`.
- `ctx_type`: the context label configured at creation time.

`has` and `descriptor` are **probe** events, not reads:

- `has`: existence queries such as `in` and `hasOwnProperty`; `value` is whether the engine can resolve a property for this name (looked up along the prototype chain, consistent with `in`).
- `descriptor`: descriptor queries such as `Object.getOwnPropertyDescriptor`; `value` is whether **the object itself** provides the property (consistent with `getOwnPropertyDescriptor`).
- `typeof x` is a value read, not a probe, and produces only one `get`.
- Probes do not execute accessors, do not trigger Proxy traps, and never produce a `get`: getter logs for the same access still have only one entry.

**Observation scope**: IDL attributes and methods implemented in Python/JS, native constructors, property accesses handled or rejected by named/indexed interceptors, and the probes above.
When a business handler returns unhandled, the original V8 lookup continues, and this layer also reports that access based on the engine's result: reads and writes of real data properties that can be obtained without side effects carry the real value,
so native globals (`window.eval`, `JSON`, `Object`) and script-added expandos (`element.extra = 1`) all produce events.
Declared members are not reported here at the interceptor; they are reported by their own accessors and calls, so the same access does not produce two log entries.
Accessor properties and nonexistent names are not reported on reads: getting the value would require running JS again or could only be faked, so one fewer event is preferable.
For example, `document.createElement(...)`, `element.id`, `element.extra = 1`, `window.eval`, `'extra' in element`,
`Object.getOwnPropertyDescriptor(window, 'eval')` all produce events.

Python callables exposed to JS (functions hung on `Context.glob`, bound methods such as `chrome.loadTimes`) are reported as `method` when called:
`receiver` is the Python object the callable belongs to, `None` for plain functions, and the `arguments`/`value` conventions are the same as IDL methods.

**Value conversion**: scalars are converted by value; native Python objects keep their identity; other JS objects (including arrays, Proxies, and exception objects) are passed by JSObject-family reference.
Objects and arrays are not traversed, and getters, Proxy traps, or `toString` are not called. These references are not deep-copy snapshots; later object changes remain visible.
By default `format_trace` shows only the type for ordinary objects (e.g. `<Navigator>`) and the source for functions (e.g. `function eval() { [native code] }`);
the source is obtained through the bound raw toString, and does not call the observed object's own `toString`.

When no callback is passed, `pyv8c.pyv8.trace.log_trace` is used, outputting logs through the standard logging `pyv8c.pyv8.trace` logger.
The native layer caches the callback and event names; when disabled it skips the observation scope and value conversion, and does not change object templates.
The callback runs synchronously on the current realm thread, and a separate reentrancy counter suppresses trace events produced again inside the callback.
The callback's return value is ignored; the callback's own Python/JS exceptions are isolated and do not replace the business result or the original exception; dispatch stops on termination.
To diagnose callback errors, catch and log them inside the callback itself.

A custom callback is still user code: explicitly reading JSObject properties, printing it, modifying the receiver, or executing JS can all have side effects.
To stay passive, only save the event or use the safe `format_trace`, and do not actively access/modify JS objects inside the callback.

## TypedArray and Promise

### TypedArray

JavaScript TypedArrays are converted to `pyv8.JSTypeArray`:

```python
array = context.exec_js("new Uint8Array([10, 20, 30])")
assert isinstance(array, pyv8.JSTypeArray)
assert array.length == 3
assert array[1] == 20

array[1] = 99
```

Create a TypedArray from Python:

```python
array = pyv8.JSTypeArray(
    [1, 2, 3],
    pyv8.ArrayType.Uint8Array,
)
context.expose(array=array)
assert context.exec_js("array instanceof Uint8Array") is True
```

Available `ArrayType`s:

- `Uint32Array`
- `Uint16Array`
- `Uint8Array`
- `Int32Array`
- `Int16Array`
- `Int8Array`
- `Float64Array`
- `Float32Array`
- `Uint8ClampedArray`

Python `bytes` is converted to `ArrayBuffer`:

```python
context.expose(payload=b"\x01\x02\xff")
assert context.exec_js("payload.byteLength") == 3
```

### Promise

```python
promise = context.exec_js("Promise.resolve('success')")
assert isinstance(promise, pyv8.JSPromise)

promise.then(lambda value: print(value))
```

`JSPromise` is currently a JavaScript Promise wrapper, not a Python `asyncio.Future`, and cannot be
used directly with `await promise`. For asyncio integration, you should write resolve/reject
results into a Future belonging to the event loop from within a Python callback.

## Errors and Timeouts

### JavaScript Errors

Syntax errors, runtime errors, and getter/proxy errors are converted to `pyv8.JSException`:

```python
try:
    context.exec_js("throw new TypeError('bad input')")
except pyv8.JSException as error:
    print(error)
```

### Python Errors Entering JavaScript

The error hierarchy belongs to the binding: `pyv8.JsError` and the six types beside it (`JsTypeError`, `JsRangeError`,
`JsReferenceError`, `JsSyntaxError`, `JsURIError`, `JsEvalError`) each use the corresponding Python builtin as
their base class. **Inheriting from one of them is the entire declaration** — there is no table to install and no name to align:

```python
from pyv8c import pyv8


class PageTypeError(pyv8.JsTypeError):
    pass


def fail():
    raise PageTypeError("invalid payload")


context.expose(fail=fail)
result = context.exec_js(
    """
    try {
        fail();
    } catch (error) {
        [error.name, error.message, typeof error.stack];
    }
    """
)

assert result.to_python() == ["TypeError", "invalid payload", "string"]
```

- The page sees an error of **the class it inherits from**, built by the accessing realm's own constructor, so
  `DOMException` uses the accessing realm's own one;
- Exceptions that do not inherit from this family (including host-side failures such as scheduling and execution) are visible only to the host: the bridge terminates the current script,
  and re-raises the original exception to the host at the boundary;
- A host class that happens to be named `TypeError` will not be mis-mapped because of this — if it does not inherit, it will not enter JavaScript;
- Classes with a nonstandard shape declare it on their own class: `__v8_error_shape__`, a triple — the constructor name in the realm,
  the argument attribute name that follows the message (for example `("_name",)` for `DOMException`), and the
  `(Python attribute, JavaScript attribute)` pairs copied over after construction. Without a declaration, the default applies: displayed as the inherited family member, with no extra arguments and
  no additional fields;
- The error object carries only browser-semantic content (name, message, declared fields): the host's own call chain and file paths
  are not written onto the object, so when the page `catch`es it or reports it out, the host side cannot be read.

### Execution Timeout

```python
context.timeout = 0.1

try:
    context.exec_js("while (true) {}")
except pyv8.JavaScriptTerminated:
    pass

# The timeout termination state is cleaned up, so the same Context can continue to be used.
context.timeout = 0
assert context.exec_js("6 * 7") == 42
```

You can also limit a single call only:

```python
context.exec_js("runTask()", timeout=0.25)
```

Timeouts are implemented via V8 TerminateExecution, not OS-level memory or process limits. The heap budget uses cooperative termination
at safepoints (see [Heap Limits and External Memory](#heap-limits-and-external-memory)) and cannot guarantee avoiding all process-level OOM; allocation failures, assertion failures,
and other fatal errors may still terminate the process directly.

## Multiple Isolates and Lifecycle

### Creating Independent Isolates

```python
import gc
from pyv8c import pyv8


class Window:
    pass


isolate_a = pyv8.Isolate()
isolate_b = pyv8.Isolate()

context_a = isolate_a.context(Window)
context_b = pyv8.Context(Window, isolate=isolate_b)

context_a.exec_js("globalThis.name = 'A'")
context_b.exec_js("globalThis.name = 'B'")

assert context_a.exec_js("name") == "A"
assert context_b.exec_js("name") == "B"

del context_a, context_b
gc.collect()
isolate_a.close()
isolate_b.close()
```

Each Isolate has its own independent:

- JavaScript heap
- Context and global object
- FunctionTemplate cache
- V8 string cache
- Python owner set

### Handles Cannot Cross Isolates

The following objects cannot be passed from Isolate A into Isolate B:

- `JSObject`
- `JSFunction`
- `JSPromise`
- `JSTypeArray`
- `JSValue`
- `Script`

Violating this constraint raises `RuntimeError: JavaScript value belongs to another isolate`.
To pass data across Isolates, use JSON, plain Python containers, or implement your own structured clone.

### Threads

Different threads can each enter a different Isolate, protected internally by the V8 Locker. Python callbacks are still subject to the GIL,
so multiple Isolates are not equivalent to full parallelism of Python code.

Do not let the same Context be operated on by multiple threads in an unordered way. When scheduling across threads is needed, the application layer should explicitly manage
Context ownership and the message queue.

### Closing Rules

`context.close()` is idempotent. After closing, the `JSObject`,
`JSFunction`, `JSValue`, Promise, TypedArray, and `Script` created by this Context all become invalid; accessing them again from Python
raises `RuntimeError`. Host functions, constructors, methods, and
getters held by another Context also cannot keep calling Python callbacks of the already-closed Context; doing so raises a JavaScript error.
Callbacks and error hooks of other active Contexts are unaffected.

When a Context is closed from inside a Python callback, new access is immediately forbidden, and the underlying reference cleanup is deferred until the current
native execution exits. Context and its associated wrapper types participate in Python cyclic GC, but applications should still explicitly close
pages and Contexts to deterministically release threads, network, and native resources.

`context.set_error_hooks()` only sets the hooks of the current Context; Promise rejections are delivered according to
the Context in which the Promise was created. Closing a Context automatically removes its own hooks.

`context.gc()`, `isolate.gc()`, and `pyv8.v8_gc()` use production-ready low-memory collection notifications,
do not require `--expose-gc` to be enabled, and do not guarantee that the process working set drops immediately.

Before closing a custom Isolate, you must release all of its persistent handles:

```python
context.close()
gc.collect()
isolate.close()
```

If there are still active Contexts, Scripts, or JS wrappers, `close()` raises a `RuntimeError` containing
`persistent handle` and keeps the Isolate usable.
Context.close revokes its associated persistent handles, so there is no need to delete already-invalid Python wrapper objects first.
An Isolate cannot be destroyed while a native call is executing; a `RuntimeError` is raised.

`pyv8.shutdown_v8()` is used to shut down the default Isolate and the process-level V8 Browser:

```python
del context
gc.collect()
pyv8.shutdown_v8()
```

Notes:

- `shutdown_v8()` is refused when an active custom Isolate exists.
- Shutdown is refused when active handles of the default Isolate exist.
- `shutdown_v8()` does not start V8 just to shut it down: if V8 has never been used, it returns `False` directly.
- After V8 Dispose, it cannot be reinitialized in the same process; the first subsequent use raises `RuntimeError`.
- A second call to `shutdown_v8()` returns `False`.
- Subinterpreters and free-threaded CPython are currently explicitly rejected.

Ordinary applications usually do not need to call `shutdown_v8()` proactively; use it only in standalone processes
or tests that need deterministic release of V8 resources.

## DevTools Debugging

### Starting the Debugging Service

```python
from pyv8c import pyv8


class Window:
    __v8_idl_spec__ = pyv8.IdlSpec(
        global_this=True,
    )


def run_target(context):
    context.exec_js(
        """
        function decode(value) {
            const result = value.split('').reverse().join('');
            return result;
        }
        decode('payload');
        """,
        filename="reverse-target.js",
    )


context = pyv8.Context(Window())
try:
    with pyv8.start_devtools(context, options=pyv8.DevToolsOptions(port=9005)) as handle:
        print(handle.url)
        handle.run(run_target)
finally:
    context.close()
```

The caller prints `handle.url`; the address contains a stable Target ID, for example:

```text
devtools://devtools/bundled/inspector.html?experiments=true&ws=127.0.0.1:9005/devtools/page/<target-id>
```

Paste the address into the Chrome address bar. After DevTools sends
`Runtime.runIfWaitingForDebugger`, `handle.run(run_target)` executes the callback on the calling thread.
By default it pauses once at the first statement; Console evaluation does not consume this pause, and reconnecting does not repeat a pause that has already been consumed.

### Usage Notes

- The DevTools CSP in Chrome 130+ only allows the page to connect to `ws://127.0.0.1:*`; do not change the default address to
  `localhost`.
- The same Context can have multiple independent native Inspector sessions; the service keeps a single-client connection model.
- `start_devtools()` does not block business logic. A standalone Context uses `handle.run()` or `wait_for_ready()` to handle the handshake on the calling thread.
- Disconnecting releases the startup wait and pause, keeping the Context and service for reconnection; it no longer takes over process signals.
- `handle.close()` runs on the Context's calling thread, closing the service but not the Context; it supports repeated calls and context management.
- `DevToolsOptions` configures host, port, wait_for_debugger, pause_on_start, and startup_timeout in one place.
- `filename` determines the script name shown in the Sources panel and stack traces.
- DevTools may send `Network.*` and `Target.*` methods not supported by embedded V8; a `-32601` response
  does not mean the debugging connection failed.

### Custom Protocol Bridging

When you need full control over CDP, you can subclass `pyv8.Debugger` and implement:

- `handle(message)`: receive JSON messages returned by the Inspector
- `on_ready()`: receive `Runtime.runIfWaitingForDebugger`, without implicitly scheduling a pause
- `run_loop()`: run a nested message loop while V8 is paused
- `quit_loop()`: exit the nested loop when execution resumes

Ordinary users should prefer `pyv8.start_devtools()` and `DevToolsHandle`.
For the detailed lifecycle, discovery interfaces, and migration notes, see [DEVTOOLS](devtools.md).

## API Quick Reference

### Module-Level API

| API | Description |
| --- | --- |
| `pyv8.Context(...)` / `pyv8.V8Context(...)` | Create a JavaScript Context (`V8Context` is an alias of the same type, distinguished from the host's profile) |
| `pyv8.Isolate()` | Create an independent V8 Isolate |
| `pyv8.Script(context, source, name=None)` | Compile a reusable script |
| `pyv8.new(constructor, *args)` | Call a JavaScript constructor |
| `pyv8.current_context(isolate=None)` | Get the currently executing Context |
| `pyv8.incumbent_context(isolate=None)` | Get the incumbent Context |
| `pyv8.v8_gc()` | Request a full GC of the default Isolate |
| `pyv8.v8_version()` | Return the actually linked V8 version; does not start V8 |
| `pyv8.configure(params=None, icudtl_path=None)` | Specify V8 parameters and ICU data for the next startup; effective only before first use |
| `pyv8.is_initialized()` | Whether V8 has been started |
| `pyv8.shutdown_v8()` | Permanently shut down the process-level V8 Browser; does not start V8 |
| `pyv8.start_devtools(...)` | Start the Chrome DevTools bridge |
| `pyv8.Null` | JavaScript `null` singleton |
| `pyv8.Undefined` | JavaScript `undefined` singleton |

### Context

| API | Description |
| --- | --- |
| `exec_js(program, filename=None, timeout=None)` | Execute a string or Script |
| `expose(*objects, **named)` | Expose Python values onto globalThis |
| `install(names)` | Install each name as a non-enumerable lazy constructor |
| `define_global(name, value, attributes=0)` | Install a global value with the specified attribute bits |
| `hide_global(name)` | Remove an already-exposed global name |
| `js_obj(py_object)` | Look up the JS wrapper for a Python object |
| `gc()` | Request a full GC of the owning Isolate |
| `glob` | The JSObject wrapper of globalThis |
| `timeout` | The Context's default execution timeout in seconds |

### Isolate

| API | Description |
| --- | --- |
| `context(global_this, ...)` | Create a Context in this Isolate |
| `gc()` | Request a full GC of this Isolate |
| `close()` | Close the Isolate when there are no active handles |
| `closed` | Whether the Isolate has been closed |
| `external_memory` | Number of bytes of isolate-external memory already reported to V8 |

## Tests

### Full Python Version Matrix

```bash
python314 -m pip install --upgrade tox
python314 -m tox
```

tox builds the ABI3 wheel once with Python 3.12, then installs it separately into Python 3.12,
3.13, 3.14, and 3.15 environments. This avoids mistakenly having all interpreters load the same temporary file from the source tree.
The ABI3 wheel and native tests for Python 3.15 have been verified on Windows; other platforms still need separate verification.

### Specifying Versions

```bash
python314 -m tox -e py312
python314 -m tox -e py314
python314 -m tox -e py315
```

### Quick In-Place Tests

```bash
python setup.py build_ext --inplace --force
python -m pytest -q -n auto
```

(`pytest-xdist` is in the dev extras. This suite is small, and there is no real difference between parallel and serial runs — 403 passed measured at 5.42 s vs 5.55 s —
just include `-n auto` to follow the repository's uniform convention.)

Specific tests:

```bash
python -m pytest tests/test_context.py -q
python -m pytest tests/test_chromium_contracts.py -q
python -m pytest tests/test_devtools.py -q
```

## FAQ

### `DLL load failed: %1 is not a valid Win32 application`

This usually means `_pyv8.pyd` does not match the architecture or ABI of the current Python:

- 32-bit Python loaded a 64-bit `.pyd`
- Windows ARM64/x64 artifacts are mixed
- The file is not actually a Windows PE extension
- Artifacts from another platform were copied during the build

Check:

```powershell
python -c "import struct, sys; print(sys.version); print(struct.calcsize('P') * 8)"
```

Rebuild `_pyv8` using the target platform's Python and compiler.

### `ICU data does not match this _pyv8 build`

The current `pyv8c/pyv8c/pyv8/icudtl.dat` differs from the file used when `_pyv8` was compiled. Do not copy just one of the files;
rebuild the extension, or restore `_pyv8` and `icudtl.dat` together from the same wheel.

### `Fatal process out of memory` or native crash

This is a process-level fatal error from V8, not a Python exception; loguru, `try/except`, and `sys.excepthook`
cannot record Python logs after the crash.

Recommendations:

- The heap budget cannot guarantee avoiding a process crash (see [Heap Limits and External Memory](#heap-limits-and-external-memory)); real
  allocation failures and fatal errors are still the situation described here
- Ensure the ICU data and V8 static libraries match
- Set a `timeout` for untrusted scripts
- Put high-risk tasks in a separate subprocess
- Use V8 memory parameters to limit process resources
- Collect exit codes, stderr, and crash dumps outside the process

### DevTools Shows `WebSocket disconnected`

Confirm the URL uses:

```text
ws=127.0.0.1:9005/devtools/page/<target-id>
```

Use `handle.url` directly and do not discard the Target path. Check the port, firewall, and whether the standalone Context is
handling commands in `handle.run()` / `handle.wait_for_ready()`. After a disconnect, you can reopen the same address.

### Old DevTools Parameters or `DevtoolsDebugger` Cannot Be Imported

The old implementation has been removed. Put `host`, `port`, and `startup_timeout` into `DevToolsOptions`;
change `callback=fn` to `handle.run(fn)`. See the [migration table](devtools.md#legacy-api-migration).

### `cannot close V8 isolate while ... persistent handle(s) are active`

There are still Contexts, Scripts, or JavaScript wrappers referencing this Isolate. Delete these objects and run
`gc.collect()`, then retry; do not force-destroy an Isolate that is still in use.

### `JavaScript value belongs to another isolate`

A V8 handle such as JSObject, JSFunction, or Script is being used across Isolates. Convert the data to plain Python
containers, JSON, or structured clone before passing it.

### Cannot Overwrite `_pyv8.pyd` When Compiling on Windows

Some Python process has already loaded the file, and Windows locks a DLL that is in use. Exit the corresponding Python, test,
or DevTools process, then run `build_ext --inplace`.
