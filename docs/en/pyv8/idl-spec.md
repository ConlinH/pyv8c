# Native IDL Binding Declarations

[简体中文](../../pyv8/idl-spec.md) | **English**

This document describes `IdlSpec` and the member descriptors in the `_pyv8` native extension of the `pyv8` project. They declare how Python types are exposed as JavaScript interfaces; they are not a Web IDL text parser and do not automatically implement browser APIs.

## 1. Basic Declarations

```python
from pyv8c import pyv8

class Example:
    __v8_idl_spec__ = pyv8.IdlSpec(
        constructor_behavior=pyv8.FlagConstructor.kNew,
        has_constructor=1,
        to_string_tag="Example",
        attributes=(
            pyv8.AttributeSpec(name="value", getter="get_value"),
        ),
        methods=(
            pyv8.MethodSpec(name="add", callback="add", length=1),
        ),
        constants_value=(
            pyv8.ConstantSpec(name="READY", value=1),
        ),
    )

    def get_value(self):
        return 42

    def add(self, value):
        return 42 + value
```

There are four kinds of native descriptors: `IdlSpec`, `AttributeSpec`, `MethodSpec`, and `ConstantSpec`. All are immutable objects that accept keyword arguments only, and their fields are read through attributes.

```python
original = Example.__v8_idl_spec__
updated = original.replace(length=1)
assert original.length == 0
assert updated.length == 1
assert updated.attributes is original.attributes
```

`replace(**changes)` creates a new object and re-validates fields and combination constraints; unmodified members may be shared. Immutability means descriptor fields cannot be reassigned; it does not mean that data captured by callbacks or the objects that own callbacks are frozen.

`spec[key]`, `dict(spec)`, field assignment, deletion, and positional arguments are not supported. Member collections must be a **tuple** of the corresponding descriptors; you cannot pass a list or legacy 4-tuples. For the complete Python signatures, see [Type Declarations](../../../pyv8c/pyv8c/pyv8/_pyv8.pyi).

## 2. Interface Fields

### Identity, Construction, and Inheritance

| Field | Default value and type | Meaning |
| --- | --- | --- |
| `name` | `None`; string or `None` | Explicit interface name configuration |
| `to_string_tag` | `None`; string or `None` | `Symbol.toStringTag` configuration |
| `parent_class` | `None`; Python type or `None` | Explicit parent type |
| `length` | `0`; integer 0..2147483647 | Parameter-count metadata for the interface function; does not perform argument validation |
| `has_constructor` | `0`; integer 0 or 1 | Constructor-related installation configuration, expressed separately from whether calls are allowed |
| `constructor_behavior` | `0`; integer 0..3 | Allowed call forms, see the table below |
| `constructor_adapter` | `None`; callback reference | Construction adapter entry point, receives `(cls, args, kwargs)` and returns a host instance |
| `definition_kind` | `DefinitionKind.kInterface` | Shape of the interface object |
| `unconstructable` | `False`; bool | Reject construction inside the construction callback |
| `skip_inherit` | `False`; bool | Controls inheritance handling when this type is used as a parent type |
| `hidden` | `False`; bool | Controls whether members of this class are hidden during reflective member installation |

`constructor_behavior` uses `FlagConstructor`:

| Constant | Value | Call form |
| --- | --- | --- |
| `kDontAll` | 0 | Neither form is allowed |
| `kNew` | 1 | `new Example()` |
| `kCall` | 2 | `Example()` |
| `kAll` | 3 | Both forms are allowed |

The construction adapter is not the `call` on an instance. It replaces the step where the native layer calls the Python class directly; `args` is a tuple of converted JS positional arguments, `kwargs` is the construction keyword dictionary, and when a host global object exists it includes the `window` argument supplied by the native construction path.

`definition_kind` uses `DefinitionKind`; the three values are mutually exclusive:

| Constant | Value | JS shape |
| --- | --- | --- |
| `kInterface` | 0 | Interface function with a prototype |
| `kIdlNamespace` | 1 | Plain namespace object |
| `kWrapperTypeNoPrototype` | 2 | Interface function without creating a prototype |

### Member Collections and Object Behavior

| Field | Default value | Meaning |
| --- | --- | --- |
| `attributes` | `()` | Tuple of `AttributeSpec` |
| `methods` | `()` | Tuple of `MethodSpec` |
| `constants_value` | `()` | Tuple of `ConstantSpec` |
| `prototype_late_members` | `()` | Tuple of member names moved, in order, to the end of the prototype, used to reflect the installation order of late template members, `constructor`, and context members; does not change member descriptors |
| `call` | `None` | Callback that configures an instance as a callable object |
| `immutable` | `0` | Prototype immutability flag; not frozen object properties |
| `undetectable` | `False` | V8 undetectable behavior; `call` must also be provided |
| `code_like` | `False` | Marks the instance template as V8 code-like, affecting string handling during code evaluation |
| `named_properties_object` | `False` | Uses the named-properties object installation path |
| `named_properties_non_masking` | `False` | The named-properties handler uses V8 `kNonMasking` to avoid masking existing properties or prototype members |
| `global_this` | `False` | A configuration field that is currently stored and validated; there is no separate native execution branch that reads it, so you should not infer global-object binding behavior from it |

The bit values of `immutable` come from `FlagImmutable`: `instance=1`, `prototype=2`, which can be combined with bitwise OR. It restricts prototype modification of the corresponding object and is not equivalent to `Object.freeze()`.

## 3. Member Descriptors

### AttributeSpec: Accessor Properties

`name` and `getter` are required; `setter` can be omitted.

| Field | Default value | Type or range |
| --- | --- | --- |
| `name` | required | String without NUL |
| `getter` | required | Callback reference |
| `setter` | `None` | Callback reference or `None` |
| `location` | `FlagLocation.kPrototype` | 0..2 |
| `property_flags` | `V8Attribute.kNone` | 0..7 bitmask |
| `receiver_check` | `FlagReceiverCheck.kCheck` | 0 or 1 |
| `cross_origin_get_check` | `FlagCrossOriginCheck.kCheck` | 0 or 1 |
| `cross_origin_set_check` | `FlagCrossOriginCheck.kCheck` | 0 or 1 |
| `side_effect` | `SideEffectType.kHasSideEffect` | 0..2 |
| `replaceable` | `False` | bool; installation configuration for replaceable properties |

### MethodSpec: Methods

`name` and `callback` are required. `length` is the `.length` of the JS function, not an upper bound on the number of allowed arguments.

| Field | Default value | Type or range |
| --- | --- | --- |
| `name` | required | String without NUL |
| `callback` | required | Callback reference |
| `length` | `0` | Integer 0..255 |
| `location` | `FlagLocation.kPrototype` | 0..2 |
| `property_flags` | `V8Attribute.kNone` | 0..7 bitmask |
| `receiver_check` | `FlagReceiverCheck.kCheck` | 0 or 1 |
| `cross_origin_check` | `FlagCrossOriginCheck.kCheck` | 0 or 1 |
| `side_effect` | `SideEffectType.kHasSideEffect` | 0..2 |

Both kinds of members share the following constants:

- `FlagLocation`: `kInstance=0`, `kPrototype=1`, `kInterface=2`.
- `V8Attribute`: `ReadOnly=1`, `DontEnum=2`, `DontDelete=4`; defaults to 0 and can be combined with bitwise OR.
- `FlagReceiverCheck`, `FlagCrossOriginCheck`: `kCheck=0`, `kDoNotCheck=1`.
- `SideEffectType`: `kHasSideEffect=0`, `kHasNoSideEffect=1`, `kHasSideEffectToReceiver=2`.

Side-effect flags are declarations provided to V8; they do not verify that a Python callback is actually free of side effects. Likewise, disabling receiver or cross-origin checks is not a permission-granting mechanism; the caller is responsible for the correctness of the corresponding interface.

### ConstantSpec: Integer Constants

```python
pyv8.ConstantSpec(name="READY", value=1)
```

`name` is required; `value` defaults to 0 and ranges over **-2147483648..4294967295**. It represents only integer constants and cannot hold arbitrary Python objects. To expose dynamic objects, use an accessor or set a value on the Context.

## 4. Callback References and Resolution

Callback fields accept either a callable object or a string naming a class member; optional callbacks accept `None`. For example, `getter="get_value"` resolves that member from the host class at binding time.

Unbound callbacks for instance and prototype members receive the Python host receiver; Python methods that are already bound do not get the receiver added again. Callbacks at the interface location do not automatically add an instance receiver. Special entry points (such as the construction adapter, property handlers, and iteration data sources) follow their own protocols, and you cannot apply the ordinary method signature to them.

Resolved callbacks are held by the template. After a template is installed, reassigning a same-named member on the Python class does not replace an already installed explicit binding; to use a new configuration, you need to establish a Realm/template that uses the new declarations. Resolution does not modify the callback function object itself.

String references are only type-checked when the descriptor is constructed; whether the member exists and whether the resolved result is callable are checked during the binding resolution phase. The internal resolve flow is not a Python instance method exposed by the descriptors.

## 5. Synchronous Iteration Configuration

### What Each of the Four Fields Controls

| Field | Default value | Responsibility |
| --- | --- | --- |
| `iteration_kind` | `IterationKind.none` (0) | Selects the iteration semantics of `none`, `indexed`, `pair`, or `set` |
| `array_methods` | 0 | Used only by the indexed path, selects borrowed V8 Array built-in methods |
| `iteration_methods` | 31 | Used only by the pair/set path, selects native host iteration methods |
| `iteration_source` | `None` | Python data source factory for pair/set |

Both method masks use the same bit values but act on different installation paths:

| Bit value | Member |
| --- | --- |
| 1 | `Symbol.iterator` |
| 2 | `entries` |
| 4 | `keys` |
| 8 | `values` |
| 16 | `forEach` |

For example, 31 publishes all five members, while 1 publishes only `Symbol.iterator`. `ArrayProto.iterator=1` and `ArrayProto.iterable=31` are convenience constants for the indexed path.

**Currently the published range is not determined by intersecting the two masks.** indexed reads only `array_methods`; pair/set reads only `iteration_methods`. When `iteration_kind=none`, the default `iteration_methods=31` still does not enable iteration.

### Valid Combinations

| kind | Value | `array_methods` | `iteration_source` | Effective method mask |
| --- | --- | --- | --- | --- |
| `none` | 0 | must be 0 | must be empty | No iteration methods installed |
| `indexed` | 1 | 0..31 | must be empty | `array_methods` |
| `pair` | 2 | must be 0 | must be provided | `iteration_methods` |
| `set` | 3 | must be 0 | must be provided | `iteration_methods` |

Combination errors raise `TypeError` at construction or in `replace()`. Even if the pair/set method mask is 0, a data source is still required.

### indexed: Reading length and Numeric Indices

```python
spec = pyv8.IdlSpec(
    iteration_kind=pyv8.IterationKind.indexed,
    array_methods=pyv8.ArrayProto.iterable,
)
```

The host must separately provide a readable `length` and numeric indices. The native layer directly installs V8 Array iteration built-ins; `Symbol.iterator` uses `values`. The configuration does not turn the host into a real Array, nor does it automatically add numeric properties to Python containers.

### pair/set: Creating an Independent Cursor Each Time

The `iteration_source` protocol has two steps:

```text
factory(receiver) → callable cursor
cursor() → (key, value) or None
```

A directly runnable declaration example:

```python
class Pairs:
    __v8_idl_spec__ = pyv8.IdlSpec(
        iteration_kind=pyv8.IterationKind.pair,
        iteration_methods=31,
        iteration_source="__v8_iteration_source__",
    )

    def __init__(self):
        self.items = [("a", 1), ("b", 2)]

    def __v8_iteration_source__(self):
        snapshot = iter(tuple(self.items))

        def next_pair():
            return next(snapshot, None)

        return next_pair
```

This explicitly uses a snapshot. If the interface needs live reads, store the current position in the independent cursor and read the host's current data on each call. You cannot share a single instance-level cursor, otherwise two iterators will advance each other.

The native layer requires the factory to return a callable object; a cursor returning `None` signals the end of the current read, and a non-empty result must be convertible to a sequence of length 2. A set data source should return `(member, member)`, and both positions of the object member should reference the same object.

The default iteration method for pair is `entries`; for set it is `values`. When both the default named method and `Symbol.iterator` are published, they point to the same JS function object.

`forEach` is driven by the native layer calling the data source; callback arguments are `(value, key, receiver)` in order, and `thisArg` is supported; an exception thrown by the callback immediately terminates the current traversal. You do not need to declare `MethodSpec` again for these native iteration views.

### Completion State, Realm, and Release

The native layer does not uniformly lock down the cursor's completion state. Whether reads are live, whether a snapshot is used, and whether iteration can continue after returning `None` due to appended data are decided by the interface data model and the cursor implementation. For example, the current browser layer uses live cursors for URLSearchParams/FormData and snapshots for FontFaceSet/StylePropertyMap; these choices are fixed by reference to Chrome.

The data cursor is held through the existing weak host object mechanism and released when the iterator is collected or the Context is closed; it does not enter the ordinary function template cache. The native entry point checks the receiver and Context state; `next` of an iterator for the same interface supports cross-Realm borrowing, while iterators of different interfaces and ordinary objects cannot impersonate the receiver.

Python exceptions propagate through the existing bridging exception mechanism; you should not assume that ordinary Python exceptions can all be caught by JS `try/catch`. This interface does not implement async iterators.

## 6. Property Interception and Cross-Origin Entry Points

The following fields are all callback references, not enable/disable boolean bits. `None` means unconfigured; a string or callable means the corresponding entry point is configured.

| Group | Fields |
| --- | --- |
| Named properties | `name_get`, `name_set`, `name_query`, `name_enum`, `name_del` |
| Numeric index properties | `index_get`, `index_set`, `index_query`, `index_enum`, `index_del` |
| Cross-origin access check | `cross_origin_check` |
| Cross-origin named properties | `cross_origin_name_get`, `cross_origin_name_set`, `cross_origin_name_query`, `cross_origin_name_enum`, `cross_origin_name_del` |
| Cross-origin numeric indices | `cross_origin_index_get`, `cross_origin_index_set`, `cross_origin_index_query`, `cross_origin_index_enum`, `cross_origin_index_del` |

`get/set/query/enum/del` correspond respectively to the read, write, property query, enumeration, and delete entry points; the exact parameter and return conventions are determined by the corresponding native handler and are not the ordinary `MethodSpec` protocol.

Fields, handler identifiers, and Python protocol names are declared uniformly in [handler_table.def](../../../pyv8/src/interfaces/handler_table.def). An explicit IdlSpec should fill in the callback fields directly; pyv8c's binding compiler can discover implementations by agreed-upon names and then fill in the descriptors.

`named_properties_non_masking` and `named_properties_object` are different configurations: the former adjusts the masking rules of the named handler, while the latter selects the named-properties object installation path.

## 7. Realm Registration and Lazy Compilation

Each Realm maintains its own interface declarations and templates. An interface name can correspond to only one Python type; another type claiming the same name is rejected.

A Realm's `lazy_factory` can return:

- A Python class: use that class's declarations.
- `(class, {name: spec})`: provide complete declarations for the corresponding interface, including ancestor declarations that need to be registered in advance; the complete declarations replace the class declarations, with no field-by-field merging.
- `None`: the name is not provided; existing class declarations remain available for subsequent bindings.

Results for unknown names are remembered to avoid repeated queries; an exception thrown by the factory does not count as a successful answer, and retries are possible later. An interface whose template is already established will not be replaced by late configuration.

`Context.install(names)` installs non-enumerable lazy interface access entry points on the global object; the corresponding interface is built only when the script actually reads it. Reading the entry point through another Realm still builds it in the Realm that created the global holder, preserving the Realm identity of the constructor and prototype.

pyv8c aggregates baseline metadata, implementation declarations, and mixins at the upper layer, and generates immutable `IdlSpec`s through the binder. JS parent members remain on the prototype chain; compilation results that are Realm-independent can be shared, while callbacks that need a Realm are bound separately. `BindingSession` prepares the ancestor chain before native registration and publishes state on success. `strict_validation` performs upper-layer compilation review; it does not replace native runtime checks.

## 8. Validation and Common Errors

- Unknown fields, wrong member types, non-tuple member collections, out-of-range enum or flag values: fail at construction or replacement.
- All bool fields require a real Python bool; do not substitute the integers 0/1. Fields explicitly declared as integers, such as `has_constructor`, are excepted.
- String fields and callback names cannot contain NUL; the existence of a callback name is checked at binding time.
- `undetectable=True` without `call`: fails at construction.
- pair/set missing a data source, indexed carrying a data source, or other invalid iteration combinations: fail at construction.
- The factory not returning a callable, or a cursor returning a result of the wrong length: fail during actual iteration.

Objects without `__v8_idl_spec__` still go through ordinary Python reflection and the generic bridging protocol. Tracing and exception protocols are independent runtime capabilities and do not belong to member descriptors. Ordinary reflection cannot install an arbitrary Python object as a template constant when no Realm is present; that path raises `RuntimeError`, and you should use a Context value or an explicit accessor instead.

## 9. Legacy API Migration

| Legacy usage | Current usage |
| --- | --- |
| Legacy tuple member declarations | `AttributeSpec`, `MethodSpec`, `ConstantSpec` |
| `spec[key]` or modifying a dict | `spec.field` and `spec.replace(...)` |
| Handler boolean switches | The name, callable, or `None` of the corresponding callback field |
| Standalone ConstructorConfig | IdlSpec construction fields; installation hooks handled separately |
| `array=...` | `iteration_kind=IterationKind.indexed, array_methods=...` |
| `iterate=...` / `__v8_iterate__` | `iteration_kind=pair/set, iteration_source=...`, with the callback changed to an independent cursor factory |
| Handwritten pair/set view methods | `iteration_methods` controls the published range; the native layer implements the views and `forEach` |

No compatibility aliases are provided for legacy fields or legacy callback naming. When changing `iteration_kind` from indexed to pair, you must clear `array_methods` and provide a data source in the same `replace()` to avoid producing an invalid intermediate configuration.

## 10. Implementation Entry Points

- [Field structure](../../../pyv8/src/interfaces/idl_spec.h): native storage layout of descriptors.
- [Field registration and validation](../../../pyv8/src/interfaces/idl_spec.cpp): default values, ranges, replace, callback resolution, and GC traversal.
- [Enumerations and flags](../../../pyv8c/pyv8c/pyv8/flag.py): public constants.
- [Synchronous iteration](../../../pyv8/src/interfaces/iterable.cpp): indexed, pair, and set installation and execution.
- [Template installation](../../../pyv8/src/interfaces/pyclass.cpp): member, inheritance, and object template configuration.
- [Python type declarations](../../../pyv8c/pyv8c/pyv8/_pyv8.pyi): public signatures and read-only fields.
