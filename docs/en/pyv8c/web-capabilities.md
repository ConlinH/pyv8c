# Web Capabilities and Coverage

[简体中文](../../pyv8c/web-capabilities.md) | **English**

[Documentation Index](../README.md)

## Binary Data and Messaging

Request, Fetch, XMLHttpRequest, and Blob read BufferSource on the owning Realm's owner. ArrayBuffer, TypedArray, and DataView are copied by offset and length, including empty views; modifying the original buffer after commit does not change the committed content. Native checks do not read page-overridable constructor or transfer methods.

Window, Worker, and MessagePort share structured clone, supporting circular references, Map, Date, shared buffer views, and the implemented Blob, File, and DOMException. Received values are created by the receiving Realm. The transfer list first validates types, duplicate identities, and detachment state, then serializes and prepares received values, and finally commits buffer detachment and port movement. On failure, framework ownership changes are not committed; side effects of getters themselves are not rolled back. A MessagePort inside a message must be listed in transfer. SharedArrayBuffer transfer is not supported.

## XMLHttpRequest

Window supports synchronous XHR through the existing synchronous resource entry point, reusing source checks, redirects, and document ownership validation. A synchronous request requires the default timeout and responseType; its completion state and load/loadend are visible before send returns; network and CORS failures throw NetworkError. Worker synchronous XHR is not yet implemented.

The null, undefined, and no-argument inputs to send are uniformly treated as an empty request body. A successful open cancels the old load and timers and clears request headers and the response; late responses, errors, and timeouts are isolated by request version. Repeating send or modifying request headers while sending is rejected.

Reopening within a callback, synchronous reentry, and abort have a local Chrome 156 comparison, preserving the observed outer termination events. In another case where a HEADERS_RECEIVED callback reopens an asynchronous request, Chrome 156 concatenates the old and new bodies; the framework keeps the new body separate by request ownership, and this difference is not counted as a Chrome conformance pass. None of the above represents full XHR upload, binary response, or document response compatibility.

## Worker

DedicatedWorker and SharedWorker provide atob, btoa, and structuredClone; Base64 shares string conversion and error rules with Window.

A classic Worker's importScripts first converts all URLs, then resolves them relative to the entry script and executes them synchronously in order; nested imports are still resolved relative to the entry. If an earlier item fails to load or execute, later items are not executed. A call in a module Worker throws TypeError. Dependency scripts are loaded through a separate request session; see [Resource Loading](resource-loading.md) for details. Full module loading, CSP, MIME, and Fetch preflight compatibility still need to be verified per scenario.

## DOM, Events and Documents

importNode creates nodes owned by the target document, supporting deep and shallow copies, DocumentFragment, and the SVG namespace; Document and ShadowRoot cannot be imported. A script's already-started execution state is preserved across cloning, and moving or reinserting an executed script does not re-execute it.

A checkbox click listener sees the post-preactivation checked state, and cancelling the default action restores checked and indeterminate. Host input, element click, and label forwarding share the activation flow. click uses PointerEvent, srcElement follows the event target, and layer coordinates use the current layout model.

After document parsing ends, the document first enters interactive, then runs the defer queue, and dispatches DOMContentLoaded on completion. After a callback closes the page or replaces the document, the old flow stops advancing.

HTML start tags reuse the fault-tolerant attribute boundary scan; a stray quote in an attribute name or unquoted value no longer swallows subsequent scripts, and script/style continue to be handled as raw text. This does not represent the full HTML5 tree construction algorithm.

Specified event handlers of body/frameset map to the Window of the owning active document; IDL, inline attributes, and attribute modification/removal share the handler slot. Ordinary element events and addEventListener preserve element ownership. An onerror inline handler mapped to Window receives five error arguments; an ordinary error event receives an event object and applies the corresponding cancellation rule based on the handler's return value.

## Styling and Layout

Declaration parsing recognizes separators inside strings, URLs, and functions. The computation entry point supports custom property inheritance, var fallback, numeric calc/min/max/clamp, and some color and transform computations. Cascade rules, font tables, HarfBuzz measurement, and style invalidation stay unified. only-child and only-of-type are distinguished by element siblings.

Existing selectors support escaped punctuation, hexadecimal identifiers, and trailing escaped spaces. Stylesheets reuse selector AST grouping to avoid wrongly splitting commas in escapes, attribute strings, or pseudo-class arguments; an invalid ordinary selector list is ignored as a whole. Specificity is computed from the AST, is/not use the highest value among arguments, and where is zero. Syntax, specificity, and inline declarations can be cached while element matching still reads the current DOM; live updates are maintained after attribute, class, and CSSOM changes. These capabilities do not represent full Selectors Level 4 or the complete CSS stylesheet grammar.

getClientRects returns a rectangle snapshot, and hidden or disconnected elements return an empty list. Layout currently provides a single border box and does not provide full inline fragmentation. Padding and min/max-width constraints for nested inline-block are supported. Windows system colors are verified against light-theme constants and do not represent dark, high-contrast, or dynamic theme compatibility.

hasStorageAccess and hasUnpartitionedCookieAccess return a Promise from the owning Realm. An inactive document rejects; effective origin, secure context, and ancestor sites determine the default state. There is currently no full storage permission grant flow.

## Window Own-Property Order

Host properties of `Object.getOwnPropertyNames(window)` are uniformly installed according to the environment profile's `bindings.own_order`; unobserved names are appended in a stable order. The configurable `Temporal` and `WebAssembly` are also installed in observed order, preserving the original objects and descriptors; the positions of other V8 built-in global properties are constrained by the context creation mechanism, and orders that cannot be satisfied are reported.

The V8 built-in global segment (`Object`, `Function`, `SuppressedError`, etc.) is determined by the V8 version used to build this repository's `pyv8/` and is outside the scope of Blink property-order data. The current default version is V8 15.4.68; when comparing against Chrome, verify the actual V8 versions, build options, and runtime-enabled interface status on both sides.

## Canvas and SVG

The 2D context of Canvas/OffscreenCanvas supports linear and radial gradients and color stops, rect paths, size resets, and context attributes. The native Skia CPU backend handles drawing, bitmap operations, and PNG, JPEG, and WebP encoding. Font measurement uses the existing text engine. Assigning a size resets pixels and drawing state while preserving the context object and creation options. Framework drawing is not equivalent to Chrome's full rasterization, color management, or GPU rendering.

Compare exported Canvas PNGs by decoding them to pixels first. Encoders can produce different compressed bytes from the same pixels.

SVGPoint and SVGRect provide mutable single-precision numbers, while SVGMatrix retains double precision; these attributes reject non-finite values. getCTM combines ancestor transform, viewBox, and the implemented preserveAspectRatio alignment. The validation scope of getBBox and isPointInFill is rectangles and rectangle descendants and does not represent full path/text geometry or filter effects.

## Offline Audio Completion Notification

OfflineAudioContext.startRendering provides a Promise result and dispatches the complete event through the queue,
supporting oncomplete and addEventListener. OfflineAudioCompletionEvent.renderedBuffer and
the Promise result keep the same AudioBuffer identity; the event constructor requires a valid renderedBuffer.
The order and flags of the completion event have been compared with Chrome 156, covering the case where the handler is replaced after synchronous return.
The completion notification uses the separate media-element task source and preserves document generation and shutdown cancellation constraints. The rendering computation itself still runs synchronously.
Underlying rendering still uses the framework's existing Python audio graph and does not represent sample-by-sample parity with Chrome's full audio implementation.

DynamicsCompressorNode uses a stateful mono compression kernel with lookahead delay, soft knee, makeup gain, detector, and attack/release envelopes; parameters are sampled on 32-frame boundaries. Default, low-amplitude, silence, hard-knee, and high-frequency triangle wave cases have Chrome 156 numeric comparisons and tolerance regressions; this does not represent full multichannel Web Audio or bit-for-bit parity.
