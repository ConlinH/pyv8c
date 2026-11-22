# IDL Format and Bindings

[简体中文](../../pyv8c/idl-format.md) | **English**

[Documentation Index](../README.md)

## Baseline and Environment Profile

At runtime, `pyv8c/pyv8c/idl/idl.zip` is read by default; the archive contains one JSON document, and a ZIP or JSON can also be specified via the Browser's `idl_path`. Only the current format version **10** is accepted; the in-package baseline is marked Chromium **154.0.8030.0**; other formats must be regenerated.

The environment profile is at `pyv8c/pyv8c/data/chrome-environment.json`, and the current `source` records the capture browser as Chrome **156.0.8078.80**. The IDL source version, environment profile version, native V8 version, and the Chrome used for testing are different pieces of information and cannot be inferred from one another. The environment profile also records actual capture parameters, which should be checked together with the results.

`source.preserved_overrides` lists explicit configurations not captured by this collector but retained from the original profile; these values are not measurement results of this Chrome session.

The IDL describes interface shapes and binding metadata and does not contain complete method implementations. The number of interfaces, extraction success, and the navigator version string cannot represent the browser compatibility rate.

## Document Structure

```json
{
  "idl": {
    "version": 10,
    "source": "tools/extract_idl.py",
    "chromium_version": "154.0.8030.0",
    "report": {}
  },
  "interfaces": {}
}
```

The empty `interfaces` here is only for illustrating the hierarchy and cannot be loaded as a valid baseline. A real set must contain valid interfaces and their inheritance relationships. The Window observation order is stored only in the environment profile's `bindings.own_order`.

| Interface field | Responsibility |
| --- | --- |
| parent, tag, exposed_in | Parent interface, toString tag, and allowed Realm categories |
| constructor | Interface object exposure, new/plain call permission, and length |
| template | Prototype, global object, definition_kind, access checks, and handler metadata |
| members.instance/prototype/interface | Members installed on the instance, prototype, and interface object |
| constants | Constant names, values, and installation locations |
| iteration | Independent synchronous iteration semantics and method exposure |

Members contain the information required for actual binding, such as name, kind, length, property_flags or flags, setter, receiver, cross_origin, side_effect, and main_world. property_flags is the integer representation of property flags; an interface's iteration capability cannot be inferred from an ordinary operation with the same name.

The optional `install_phase` distinguishes initial template installation (omitted or `template`), late template installation (`template_late`), and context object installation (`object`). The extractor records this information from Chromium-generated installation functions, and the native layer uses it to preserve the relative order of prototype properties and `constructor` in each Realm. Older documents without this field are treated as initial template.

template.handlers records the roles of named, indexed, and cross_origin respectively. They are extraction evidence, not Python callables; the binding compiler then resolves them to the actual host handlers.

## Synchronous Iteration

```json
{
  "iteration": {
    "kind": "pair",
    "default": "entries",
    "iterator": true,
    "methods": ["entries", "forEach", "keys", "values"]
  }
}
```

| kind | Data model | Default iteration method |
| --- | --- | --- |
| indexed | length and numeric indexes | values |
| pair | key-value pairs | entries |
| set | the same value as both key and value | values |

The responsibilities of the native IdlSpec are respectively: `iteration_kind` selects the semantics, `array_methods` specifies the array methods borrowed by indexed, `iteration_methods` specifies the host pair/set views, and `iteration_source` provides the host data source. Not every interface fills in all four; invalid combinations are validated and rejected.

The pair/set host convention is `__v8_iteration_source__(self)`; **each iteration creates an independent source** and returns a no-argument next callback; next returns `(key, value)` or None. set returns the same object twice. indexed does not use a pair/set data source.

The data source expresses live reads, snapshots, and end states according to the specific interface and Chrome comparison results, and does not mask differences with a uniform snapshot or a uniform sticky completion rule. Symbol.iterator and its default named method must be the same function object; forEach is executed natively with callback arguments `(value, key, receiver)`.

async iterable is separately recognized as not yet supported and does not generate synchronous iteration configuration. A missing host iteration source is recorded under the unimplemented policy and does not silently return an empty collection.

## Extraction and Loading

```powershell
python tools/extract_idl.py .\idl-output.zip --chromium-root D:\src\chromium --build-dir D:\src\chromium\src\out\Idl154Fresh --report
```

Extraction input is the Chromium-generated bindings and source IDL of the corresponding version. The example above outputs to an independent file; the build directory should be adjusted to the actual environment, and `--bindings-dir` can also be used to specify the bindings directory explicitly. The version is read from the source VERSION, using a justified `--chromium-version` when necessary. Merely changing the version string cannot replace generation evidence for the corresponding version.

`pyv8c/idls/` stores archives named by Chromium version for use by tests or callers targeting a specific version; at runtime the in-package `idl.zip` is read by default, and updating a version archive does not automatically update the in-package baseline. When rebuilding an older version, both the generated bindings and source `.idl` must come from the target version: for example, when the current source working tree is 154, its source `.idl` must not be mixed with generated bindings from 145. You can prepare an independent older source tree from the corresponding local Git tag, then specify the older bindings directory and source root while preserving version evidence. `unresolved_signatures` in the extraction report indicates the number of signatures not yet restored; even when `parse_failures` is empty, it does not mean all signatures have been resolved.

Synchronous iteration combines array intrinsic installation, SyncIterator, Symbol.iterator targets, and the source IDL's iterable/maplike/setlike; partial and fragment merges are applied. Conflicting evidence or an undeterminable type should report the interface and file and fail, rather than silently degrading to array iteration by default.

Format 10 does not store the Window order; formats 8 and 9 are read for compatibility, and the order fields in older archives do not participate at runtime. Other format versions are explicitly rejected. Loading also validates archive safety, entry shapes, parent interfaces, and inheritance cycles. The sidecar JSON and binary cache are local loading caches, not part of the release baseline, and are invalidated by version and digest.

The environment `bindings` describes global exposure, member omission, and method exposure facts; it only restricts the interface surface and cannot override iteration semantics. At runtime, the IDL set generates the installation plan, the binder compiles IdlSpec, and the Realm's binding session completes the installation.

## Environment Capture

```powershell
python tools/collect_environment.py --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe" --out .\environment.json
```

The environment profile format is **4**. The capture result's `bindings.own_order` stores the Window own-property enumeration order before checking interfaces and is also used to derive the exposure set; a separate `bindings.exposed` is no longer stored. Host properties are installed in this order, and unrecorded names use a stable IDL order. When no binding profile is configured, a warning is issued and a stable order is used; the old format and `bindings.exposed` are no longer supported. The environment order can be used across IDL digests, but member omission, iteration, and exposure filtering are still validated by `bindings.against`. Version differences and observable order differences are aggregated by `baseline_mismatch`; when V8 built-in names cannot be distinguished from names newly added by the target browser, they are marked as pending classification and not treated as a definite omission. `browser.idl_info["window_order_source"]` shows environment or fallback.

First output to an independent file and verify `source`, startup arguments, and capability origins before deciding whether to update the bundled profile. Do not overwrite the baseline by default; captured device, driver, and permission states do not mean the framework has implemented the corresponding hardware capabilities.
