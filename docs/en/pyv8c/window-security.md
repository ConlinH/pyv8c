# Windows and same-origin access

[简体中文](../../pyv8c/window-security.md) | **English**

[Documentation index](../README.md)

## Windows and iframes

A Page represents one document plus host state; an iframe creates a child Page and a separate Realm. Synchronous parent-child access happens on the owner thread of the same WindowExecutionGroup, and cross-window access is routed through WindowProxy rather than pinning the child window as an ordinary object of the parent Realm.

The initial about:blank is visible synchronously when the iframe sets up its context; later commits of src and srcdoc and resource completion follow their own lifecycles. The proxy identity, current document, constructor, and prototype identity of contentWindow are managed separately; navigation should not simply copy the parent window object to replace the child Realm.

Removing an iframe invalidates its document's resource registrations and asynchronously releases the child context; removal-and-reinsertion and srcdoc replacement must prevent old callbacks from completing the new document. The end of transport does not mean the child document's load barrier has been met.

## Origin identity

The effective origin is maintained by document creation and navigation rules. about:blank, srcdoc, and Blob documents involve inheritance from the creator relationship; sandbox can produce an independent opaque origin. Two origins whose serialized result is `null` are not necessarily the same, and access cannot be granted by string equality.

Same-origin checks do not re-guess from the page URL. A new document after navigation uses a new identity, and an old Realm that is still referenced is handled separately from the active document. Both the resource initiator and the cross-window accessor must use the real Realm origin.

## Cross-origin access

A cross-origin WindowProxy exposes only the allowed window operations and properties; other accesses use the corresponding rejection behavior. Property reads, writes, enumeration, descriptors, and prototype access cannot each bypass the access check. Members such as window, self, frames, parent, top, location, and postMessage are handled by their own rules rather than exposing the whole object.

`postMessage` is delivered asynchronously through a task source, carries sender identity and target-origin constraints, and copies data according to the implemented structured clone rules. Window names, numeric indices, and WindowProperties named access must all obey DOM identity, liveness, and access boundaries.

## Shutdown and retained references

Closing a child page releases only its resources and does not stop an owner or Isolate still used by sibling pages. Navigation and removal must not allow late callbacks to operate on the new document; retaining an old Realm reference also cannot bypass the same-origin rules. After the original Context is finally closed, the associated live JS handles become invalid.

Isolate termination unwinds the entire active cross-Realm call stack and does not promise to abort only one window. Resource-generation cancellation and microtask queue ownership are different problems; old-document callbacks cannot be resolved by clearing the shared Agent queue.

## Current limits

The current model does not cover OOPIF, full COOP/COEP, Origin-Agent-Cluster, `document.domain`, or browser-level security sandboxes. Cross-origin behavior should be compared with local Chrome using the same page, origin, response headers, and operation sequence, and full compatibility should not be inferred from the prototype surface or a single contentWindow read.
