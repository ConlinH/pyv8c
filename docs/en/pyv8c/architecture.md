# Architecture

[简体中文](../../pyv8c/architecture.md) | **English**

[Documentation Index](../README.md)

## Module Layers

| Directory | Responsibility |
| --- | --- |
| `pyv8c/pyv8c/foundation/` | Configuration, executors, task sources, clocks, rendering opportunities, errors, and base value rules |
| `pyv8c/pyv8c/network/` | Transport registration, network backends, cookies, request/response data, and redirect rules |
| `pyv8c/pyv8c/engine/` | IDL decoding, binding compilation, and tracing |
| `pyv8c/pyv8c/features/` | Generic Feature runtime mechanism, built-in declarations, and core Web API assembly |
| `pyv8c/pyv8c/platform/` | Browser, Profile, Page, Realm, navigation, host services, and lifecycle composition |
| `pyv8c/pyv8c/platform/resources/` | Requests, caching, resource ownership, document resources, and iframe loading |
| `pyv8c/pyv8c/web/` | Web API implementations such as DOM, BOM, Fetch, and Worker |
| `pyv8c/pyv8c/idl/` | Interface declaration entry points and the bundled IDL baseline |
| `pyv8c/pyv8c/tools/` and `tools/` | Offline extraction, capture, inspection, and command entry points |
| The `pyv8/` project in this repository | Isolate, Context, binding templates, handles, microtasks, watchdog, and Inspector |

The foundation layer does not depend on Page; Web APIs consume host capabilities through service interfaces and do not depend directly on platform controllers. The native layer provides execution and binding primitives and does not implement page resource or DOM logic. Layering constraints are maintained by architecture tests and policy checks.

## Web API Ownership

The DOM directory holds the node tree and DOM interfaces. Text encoding, permission policy, and AI interfaces live in the corresponding modules of `pyv8c/pyv8c/web/`; media capabilities live in `web/media/`, and Canvas drawing interfaces and WebGL live in `web/graphics/`; device motion and orientation events live in `pyv8c/pyv8c/web/bom/motion.py`. These modules are composed by the Feature registry and are not classified under DOM just because a page calls them.

`web/graphics/canvas/` is divided by 2D context, bitmaps, colors, gradients, encoding, text, and offscreen Canvas;
`web/graphics/webgl/` holds the context, objects, extensions, constants, and rendering backend respectively. A WebGL context calls the simulated or ANGLE backend through
the operations defined in `backends/protocol.py`; the ANGLE EGL context is released by the corresponding page
on its owning thread. HTMLCanvasElement still belongs to `web/html/` and directly uses the implementation modules above.

| Directory or module | Main responsibility |
| --- | --- |
| `web/dom/` | DOM nodes, documents, events, and layout |
| `web/html/` | HTML tags, tag registration, and HTML parsing |
| `web/css/` | CSSOM, stylesheets, selectors, and cascade computation |
| `web/svg/`, `web/mathml/` | SVG and MathML elements |
| `web/graphics/` | Canvas drawing interfaces, image data, and WebGL |
| `web/media/` | Media format detection and MediaSource |
| `web/workers/worker.py` | Worker, SharedWorker, and Worker global scope interfaces |
| `web/workers/runtime.py` | Worker execution, message passing, and lifecycle |
| `web/bom/`, `web/fetch/`, `web/fileapi/` | Browser objects, network interfaces, and file interfaces |

These module paths are internal implementation paths; the installation of Web APIs is still jointly determined by Feature registration and IDL bindings.

## HTML and DOM Elements

The generic `Element` lives in `pyv8c/pyv8c/web/dom/element.py`. HTML tag interfaces, the tag registry, and the HTML document and fragment parsers live in `pyv8c/pyv8c/web/html/`; document element creation and HTML parsing share the registry. The `DOMParser` API stays in the DOM layer and calls the corresponding parser based on document type. SVG and MathML elements live in `pyv8c/pyv8c/web/svg/` and `pyv8c/pyv8c/web/mathml/` respectively and inherit from the generic `Element`. The HTML Canvas tag lives in the HTML package, while drawing contexts, ImageData, ImageBitmap, and OffscreenCanvas live in the graphics package.

## CSS and Layout

The CSS property table, declaration parsing, value computation, selectors, cascade, and CSSOM live in `pyv8c/pyv8c/web/css/`. DOM elements hold style declarations and the document manages stylesheet versions; the layout module reads computed styles and maintains geometry snapshots. `getComputedStyle()` obtains the properties that need used values from the layout snapshot supplied by the caller; the CSS subpackage itself does not import the layout module. Node, stylesheet, and viewport changes still invalidate snapshots through the existing versioning mechanism.

## Object and State Ownership

The public model is Browser → Profile → Page. Browser owns an internal Runtime
through composition. The kernel manages execution and background ownership,
without browser configuration or Profile factories. Evaluation stays synchronous;
loading, input, networking and closure are awaited.

Browser holds configuration, FeatureRuntime, binding facilities, and the host callback thread pool. Profile always owns a CookieJar and holds the network backend, Blob URL registry, storage, and resource cache configuration. Mutable session state should be isolated across Profiles, though persistent content may still be shared when the same disk cache directory is explicitly specified.

Page composes navigation, lifecycle, scripts, documents, and resource components, exposing narrow capabilities to Web APIs through PageServices. PageRealm manages V8Context, binding sessions, and rejection tracking. A Realm's referencable lifetime is not the same concept as a document's active state; navigation cannot revoke an old document by clearing shared Agent microtasks.

A standalone page owns an independent WindowExecutionGroup. An execution group owns the owner thread, Isolate, multiple Agents, and the page-tree frame clock. An iframe shares the execution group with its parent page but uses an independent Realm; Agents are assigned by effective origin identity, and an opaque origin is merged by identity rather than the string `null`.

A Worker owns an independent thread, Isolate, and event loop. Cross-execution-group or Worker delivery uses plain data and structured clone results, not shared live JS handles. Synchronous cross-iframe calls switch Context on the same thread of the owning execution group.

V8 foreground tasks are executed automatically by the owner thread of the execution group or Worker; V8 background threads only modify native notification state, and a process-shared waiting thread wakes the executor. After a task ends, the microtask queues of surviving Agents for that Isolate are checked. The executor runs tasks through the internal interface `Context._run_foreground_task()`; page code does not need to call it. `run_until_idle()` waits for foreground tasks that are ready, but not for background compilation or future network I/O that has not yet delivered a completion task; use `wait_for()` when waiting for a concrete result.

`compileStreaming` and `instantiateStreaming` check the Response's status, MIME, and consumed state, and hand the data to V8's streaming compiler. A user-constructed default ReadableStream delivers chunks in the order reads arrive; when a Realm navigates or closes, its unfinished native compilation sessions are aborted. Network Fetch still buffers the response body first; ReadableStream BYOB and full backpressure semantics are not yet implemented.

In 20 offline small-module probes on local Chromium 154.0.8030.0, synchronous code and microtasks always ran first, and timers always completed before WASM; in pyv8c probes under the same conditions, the relative order of these two async callbacks varied. Timers and background compilation completion have no fixed relative order; this sample cannot prove an ordering error, nor can it replace further timing and load validation.

## Interface Assembly

Interface shapes are described by IDL documents, and `bindings` in the environment profile determines the subset that the current environment is allowed to expose. Python implementations provide data models and business behavior; the binding compiler combines metadata and callbacks to generate a native IdlSpec, and the Realm installs interfaces according to the installation plan.

`pyv8c/pyv8c/features/runtime.py` provides generic mechanisms independent of Web, `pyv8c/pyv8c/features/catalog.py` only declares built-in Features, and `pyv8c/pyv8c/features/core.py` assembles core Web APIs; FeatureSpec describes interface groups, dependencies, and implementation modules; FeatureCatalog builds a static index, and FeatureRuntime manages dependencies, module loading, and interface caching within the Browser scope. Implementation modules explicitly export `FEATURE`, and optional capabilities are loaded on demand. Interfaces must not be registered implicitly by scanning all variables of a module.

Interface surface, iteration semantics, and host data sources are independent responsibilities. Unimplemented interfaces can be exposed as placeholder members by configuration; a missing pair/set iteration source must be recorded as an implementation gap and must not be pretended to be an empty collection. See [IDL Format](idl-format.md) for the concrete format.

## Global Property Installation Order

Window's `Object.getOwnPropertyNames(window)` uses the environment profile's `bindings.own_order` as the sole ordering data. Before creation, the Realm merges interface constructors, Window members, aliases, and the host `chrome` into one sequence; after context creation, native bindings install member descriptors and lazy-installed constructors in this sequence. Names not covered by the environment are appended in the stable IDL order. When the environment lacks an order, a warning is issued and a stable fallback is used.

Global properties provided by V8 itself do not belong to IDL bindings; for the configurable data properties `Temporal` and `WebAssembly`, the native layer obtains and preserves the original objects and descriptors before page scripts run, then installs them in environment order, so these objects may be created earlier than the first script access. The positions of other V8 built-in properties are still determined by the context creation process. When the environment version and IDL version are inconsistent, properties are missing, or the actual order differs, `baseline_mismatch` reports or rejects it. Workers use their own interface installation path and do not apply the Window observation order.

## Resource Components

Page directly composes `ResourceFetchController`, `DocumentResourceController`, and `FrameResourceController`, which handle the request pipeline, document resource coordination, and iframe document lifecycle respectively.

Workers use ResourceRequestSession from the same resource directory, freezing resource configuration and effective origin, and sharing origin, security, and redirect rules; transport threads deliver only plain data and do not call the parent Page. The entry source carries the final response URL through WorkerSource for use by both Worker location and relative imports.

Request state is held by the Fetcher's request objects; ResourceOwnership uniformly registers active requests and plugs into the existing ResourceScope. Transport completion and document resource completion are handled separately: an iframe's network response does not mean the child document's `load` has completed.

The document resource module is divided among discovery, tracker, completion, and the coordinator. The discovery phase handles node snapshots, the tracker manages tokens, generations, entries, and indexes, and completion handles the completion rules for fixed resource kinds. Other lifecycle code cooperates through public queries and completion entry points and does not read internal record tables.

## Scheduling and Shutdown

Ordinary tasks and host commands enter typed task sources; the control entry handles only shutdown, termination, and Inspector wake-ups. One runnable task is selected at a time; microtasks are checked at legal script/callback cleanup boundaries, and task completion provides a fallback checkpoint. See [Task Scheduling](task-scheduling.md) for details.

Async resources return to the owner before response processing runs; cancellation state and owner identity are verified before delivery. Navigation advances asynchronously, and host synchronous APIs await a Future; the owner does not nest a pumped event loop to wait for its own result.

Shutdown first releases Inspector waits or pauses, then the owner releases its owned resources. Closing a child Page does not destroy an execution group that still has members; only after the last member exits are the microtask queues, Isolate, and executor closed. Active native operations are terminated by identity ticket, and an expired ticket must not affect the next operation.

JS termination must penetrate listeners, scripts, resource delivery, and task wrappers, and be classified at ticket-holding boundaries. Background cancellation during normal shutdown does not report a host failure, while synchronous host operations and real execution failures still preserve exceptions. Unload and cleanup failures are not swallowed because of the shutdown state.

## Current Limits

The framework does not have full Chromium rendering or a multi-process architecture. Virtual rendering opportunities, text measurement, and partial geometry computation cannot replace a compositor; the origin and Agent models do not cover full COOP/COEP, Origin-Agent-Cluster, OOPIF, or `document.domain`. Any capability claim should be backed by corresponding tests and local comparison.

An inconsistency between the environment capture version and the IDL version, and observable order differences that existing installation phases cannot satisfy, are aggregated as warnings or direct failures by `baseline_mismatch`; cross-version references do not imply full compatibility.
