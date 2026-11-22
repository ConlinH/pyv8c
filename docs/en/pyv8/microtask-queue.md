# Explicit Microtask Queue

[简体中文](../../pyv8/microtask-queue.md) | **English**

`MicrotaskQueue` lets the host explicitly express the microtask ownership of an Agent. Multiple Contexts within the same Isolate can share a queue or use mutually isolated queues. If you omit `microtask_queue` when creating a Context, the existing V8 automatic queue mode is still used.

```python
from pyv8c import pyv8

isolate = pyv8.Isolate()
queue = pyv8.MicrotaskQueue(isolate)
left = isolate.context(object(), microtask_queue=queue)
right = isolate.context(object(), microtask_queue=queue)
try:
    left.exec_js('globalThis.done = false; Promise.resolve().then(() => done = true)')
    assert left.exec_js('done') is False
    queue.checkpoint(timeout=1.0)
    assert left.exec_js('done') is True
finally:
    left.close()
    right.close()
    queue.close()
    isolate.close()
```

## Threading and Ownership

- The queue is fixed to the Isolate and Python thread that created it; Context construction, script execution, explicit close, and checkpoints must run on that thread.
- The queue strongly references the Isolate and holds a counted V8 root. Associated Contexts strongly reference the queue. If the queue still has associated Contexts, `close()` raises `RuntimeError`.
- Closing a Context first breaks the V8 queue association, then releases the Python reference. Other Contexts in the shared queue are unaffected.
- Repeatedly closing the queue has no side effects; after closing you cannot run checkpoints or create new associated Contexts.
- Using the queue across Isolates raises `ValueError`. The threading constraints of automatic mode keep their original behavior; you should not take this to mean it already has the new execution-group ownership model.

## Checkpoints

`checkpoint(timeout=0)` drains the queue, including microtasks appended during the checkpoint. V8 suppresses reentrancy of same-queue checkpoints. `timeout` must be a finite non-negative number of seconds; zero means no additional time limit is set. A positive value reuses the existing watchdog, and a timeout raises `JavaScriptTerminated`. Termination affects the entire active Isolate call stack.

In explicit mode, ordinary property access and script execution do not automatically drain the queue. The host must call checkpoints at the correct script, callback, and task-cleanup boundaries; this object itself does not implement the HTML event loop, nor does it select tasks, dispatch rejection notifications, or infer document generations.

A queue timeout cannot interrupt a pure-Python blocking callback. The host still needs to move blocking I/O off the owner thread. A single termination must not be treated as a rollback of side effects that have already occurred.

## Building

When V8 manages the microtask queue with cppgc, the binding holds the queue with a cross-thread persistent root and does not use `delete` to free the queue object. Non-cppgc ABIs use V8's corresponding `unique_ptr`. The two ABIs are selected by existing build probe results, not guessed from the Python version.

When V8 build metadata cannot be probed, you must set `PYV8_CPPGC_MICROTASK_QUEUE=1` or `0` based on evidence provided by the build; a setting that conflicts with recognizable build metadata raises an error. A wrong configuration can cause a native ABI mismatch.

Exception conversion reuses the Context boundary; queue type registration is wired in through the Isolate module interface. The source layout check has not added any cross-layer dependency exemptions.

## Switching Queues

`context.set_microtask_queue(queue)` is only for the host to reassign an explicit queue at a safe boundary. Both the old and new queues must belong to the same Isolate and the current owner thread, and the Context must still be valid. Switching is refused whenever any Context has been entered, a microtask checkpoint is executing, or a microtask scope exists; it cannot be switched from inside a Python callback called by JS. You cannot pass `None` to restore automatic mode.

Switching does not migrate microtasks already queued in the old queue, nor does it handle document navigation. pyv8c uses it only when the first host `set_content` retains the Context while changing the effective origin; real navigation creates a new Context.

## Operation Budget and Control

`context.begin_operation(timeout)` establishes a watchdog ticket on the owner thread and returns an operation identity; among finite non-negative seconds, zero means an unlimited budget. `context.end_operation(ticket)` releases the ticket. The host must pair the release with `finally`.

```python
ticket = context.begin_operation(1.0)
try:
    result = context.exec_js('1 + 1')
finally:
    context.end_operation(ticket)
```

Another thread can call `context.terminate_operation(ticket)` to request termination of the specified active operation; it does not enter V8 on the calling thread. Tickets that are expired, already released, or from a closed Context return False and do not affect the next operation. A termination request received before V8 has been entered is bound to that ticket, and releasing the ticket also clears the request. When a Context's close is deferred while on an active stack, the operation ticket stays valid until it is actually released; the old `set_deadline` budget is still revoked at logical close.

Termination targets the entire active Isolate stack; it cannot isolate a single Context on the stack, nor roll back side effects. An Inspector pause freezes execution budgets that already exist; the host's own wait deadlines keep counting. New budgets established during a pause are timed independently. `_suspend_deadlines` and `_resume_deadlines` are Inspector internal coordination entry points, not page APIs.

## Host Accounting for Rejection Notifications

`Context._promise_rejection_state(promise, action)` is an internal interface of the browser runtime, used by the V8 rejection hook and notification tasks. It only manipulates native private metadata; it does not execute JavaScript, run property getters, or establish extra execution or microtask boundaries.

| action | Result |
| --- | --- |
| 0 | Obtain a unique, stable Python integer identity within that Context, avoiding V8 identity hash collisions |
| 1 | Mark rejection as notified; accepts only a rejected Promise |
| 2 | Take out and revoke the notified state, returning `(bool, original reason)`; returns `(False, None)` when not marked |

The host performs action 1 only if no handler has been installed after the notification event and its listener cleanup have completed. Whether a handler is installed, when it is queued, and DOM event dispatch are still coordinated by the browser runtime; this interface does not implement an event loop.

Metadata is attached to the Promise and contains an integer identity and a boolean flag; there is no strong reference from a global registry to the Promise. The private key uses a counted V8HandleSlot and is released with the native Context's resources; the metadata does not appear in Reflect.ownKeys. Action 2 reads the original reason from Promise.Result without enumerating or copying user objects.

Access across Contexts, across Isolates, with a non-Promise, an invalid action, or a closed or wrong owner thread raises an error. When the Isolate is terminating, it returns None, indicating this accounting pass was interrupted; the host must stop that pass and must not treat it as a normal identity. Do not emulate this interface with helper JavaScript inside the rejection hook: reentrant execution increases the risks around termination and checkpoints.

## Python Reclamation During Busy Periods

Python's cyclic GC and reference-count destruction can happen on any thread, including the Inspector transport thread. Cleanup of Context, JSObject (including functions and Promises), JSValue, Script, and PromiseResolver uses `IsolateCleanupScope`: when the owning Isolate can be entered immediately, the object is released normally; when an active call already occupies the Isolate, the object pending cleanup is registered and a strong reference is added while holding the GIL, then it returns immediately.

Before the outer `IsolateLockScope` exits, while still holding the Isolate and the GIL, the registry is drained, the original cleanup entry point is called, and the strong reference is returned. Registering the same object repeatedly does not hold it repeatedly; if an object being destructed is registered, its Python storage must not be released early. Nested closes of the Context itself continue to use the existing deferred-close mechanism.

This resolves the wait cycle of "the debug transport thread triggers GC during object allocation → reclamation waits for the Isolate → the paused owner waits for the transport thread to submit resume". It does not allow other threads to enter page execution, nor does it release the V8 lock held by the pause stack. Explicit `Context.close()` still enters the Isolate under the original synchronous contract; the owner thread restriction of explicit queues stays unchanged. If a native callback blocks forever, final reclamation is also deferred; you cannot claim it forcefully ends arbitrary Python calls.
