# Task Scheduling

Browser uses the application owner thread and cooperative waits; see the [execution contract](cooperative-runtime.md).

[简体中文](../../pyv8c/task-scheduling.md) | **English**

[Documentation Index](../README.md)

## Execution Ownership

A standalone `new_page()` creates an independent WindowExecutionGroup with its own Isolate, Agents and frame clock; its owner thread is the application thread that started the Browser and is shared with other pages, and only Workers use dedicated threads. An iframe uses the same execution group; Realms with the same effective origin share an Agent, while other Agents queue independently but still run on that owner thread. Opaque origins are distinguished by identity token, and the origins of initial about:blank, srcdoc, and Blob inherit according to their creation relationship.

Each Agent holds task source queues and an explicit V8 microtask queue; a Dedicated Worker uses its own thread, Isolate, Agent, and event loop. Synchronous iframe access requires no cross-thread waiting; passing live JS objects directly between unrelated execution groups is not allowed.

## Task Sources and Entry Points

A task record contains its source, owning Realm/document, generation, order, and cancellation information. Task sources include parser, navigation, networking, timer, posted-message, user-interaction, DOM manipulation, media-element, and embedder.

The OfflineAudioContext completion notification uses the media-element source so that completed rendering is not backlogged by unrelated DOM tasks. That task is still subject to the owning document's generation and shutdown state; the complete event precedes the rendering Promise's fulfillment callback.

Ordinary host commands enter the embedder source and are not drained with unbounded priority. Scheduling fairly selects runnable sources and preserves the FIFO required within a source; parser blocking affects only the corresponding document. Shutdown, termination, and Inspector wake-ups use a restricted control entry point, which cannot be used to execute arbitrary page JS.

There is no single Chrome total order across sources that should be pinned down; tests constrain necessary causal ordering and non-starvation. Full loading prioritization, background budgets, and dynamic input priorities are not currently implemented.

The owner captures a wake sequence before scanning work. Producers publish work or a deadline before
incrementing the sequence and notifying the condition. Waiting atomically releases that condition only
if the sequence still matches. Idle and timed waits share this protocol, without polling or an asyncio loop.

Page commands themselves serve as managed-task callbacks. The Agent notifies the owner after enqueueing;
the executor does not add another wake for that publication. The separate control queue still signals.
After an idle work scan, the owner reads deadlines and checks the captured sequence under the condition,
without scanning every task, foreground and rendering source again. Publications during the scan remain
visible through the sequence; expired timers resume the pump through their deadlines.
Command registration and runner assignment share the submission lock. Cancellation, completion and
shutdown retire the runner. Ticket validation reuses an already-held Agent lock when identical to the
cancellation lock; migrated tickets still acquire their original lock. Source fairness, operation budgets
and microtask checkpoints are unchanged.

Synchronous commands have a single completion record. Queued expiry prevents execution; running expiry
preserves native termination and late-result logging. Results are published after budget cleanup.
Scripts own their checkpoint before result conversion. Copying a result object may run JS getters and
therefore needs another checkpoint; primitive results do not.

Late-result formatting or logging failures do not close the executor; execution control exceptions
still propagate. Script entry completes its checkpoint even if a closed-state check or DevTools
before-script hook fails, so the Agent does not remain busy. A failed before-script hook does not
invoke the after-script hook and retains its original error. Execution termination still prevents
further page JS from running.

With `threaded_loaders`, synchronous resource waits observe the request deadline, active window
operation budget, and stop requests. A running loader cannot be forcibly interrupted, but its late
completion does not hold the owner after the wait exits. Host HTTP Futures fail with `HostStateError`
when the page closes or its document is replaced; explicit cancellation and late answers cannot
complete the same Future twice. Following successful Realm replacement, new promises use the new
context while existing resolvers retain their original context.

Task cancellation marks state and detaches references under lock, then reclaims callbacks and contexts
after both scheduler and document routing locks have been released. Document invalidation, queue
shutdown, and invalid-task pruning follow the same rule, allowing finalizers to reenter the executor.

## Microtasks and Rejection Notifications

Promise, queueMicrotask, and MutationObserver use the owning Agent's explicit microtask queue. Ordinary property reads, object conversions, and nested cross-Realm calls should not unexpectedly start a checkpoint; script and callback cleanup check when JS stack conditions allow, with a fallback at task end, and reentrancy is prevented.

Checkpoints drain to stability and do not use an artificial microtask quota to change ordering. Infinite microtasks are handled by the operation budget and termination mechanism. Promise rejections collect candidates at the checkpoint and then notify `unhandledrejection` and `rejectionhandled` through a task, rather than dispatching directly at the end of an arbitrary property call.

Navigation generation invalidation cancels only the operations of the corresponding document; it must not clear the entire Agent queue and inadvertently affect sibling Realms.

## Network, Navigation and Waiting

External network completion is delivered to the owning networking task; after returning to the owner, response conversion, redirects, and internal continuations may proceed within that task. Asynchronous final results from the cache, local URLs, and inline loaders are likewise delivered through the queue, and the final user callback is not invoked synchronously just because execution is currently on the owner.

Navigation does not block on the owner waiting for the main-document network. `await page.goto()` waits cooperatively while the owner can process runnable work; inside the owner, code must not synchronously wait on unfinished operations, nor nest a drive of the event loop to complete a wait.

| Wait entry point | Condition |
| --- | --- |
| `goto(..., wait_until="commit")` | The current navigation commits |
| `wait_for_load_state("domcontentloaded")` | The DOMContentLoaded state of the target document |
| `wait_for_load_state("load")` | The loading-completion flow ends normally |
| `wait_for(expression)` | A read-only JS condition holds |
| `run_until_idle(timeout)` | The target Agent's runnable tasks are drained and the checkpoint completes |

Distant timers, in-flight network requests, persistent connections, and live Workers do not directly prevent the runnable queue from going idle. An idle call with `timeout=0` only queries the current state; network or application readiness must use the corresponding wait entry point.

## Timers and Rendering Opportunities

The timer heap manages deadlines; once a timer expires it enters the timer source. Cancelled heap-top entries must be skipped and must not block later-expiring work. Timers have nesting levels and corresponding minimum-delay rules; interval is not a separate model with a fixed 1 ms floor.

rAF belongs to a separate rendering opportunity; the page tree shares a frame clock, virtual 60 Hz by default, configurable with the Browser's `frame_rate`. A single frame uses a unified time base; rAFs added or cancelled within callbacks are handled by the current batch rules, and hidden state affects frame opportunities. Workers have separate rendering scheduling facilities and do not share a thread with the page.

The virtual frame clock does not represent real screen refresh, painting, or the full Chromium rendering pipeline.

## Budgets, Cancellation and Shutdown

Host operations use an absolute deadline computed at submission; queueing, execution, and checkpoints share the budget. `evaluate(timeout=30)` has a finite budget by default; the default budget for ordinary calls on the executor is controlled by `PYV8C_CALL_TIMEOUT`, 60 seconds when unset, and a non-positive configuration means that default deadline is not set.

Native operations bind the watchdog with an identity ticket. Queued tasks can be cancelled; work already started cannot promise rollback of side effects. Isolate termination applies to the entire active call stack and does not guarantee stopping only one Context in a cross-Realm stack. Pure Python blocking cannot be interrupted by the V8 watchdog and should be moved off the owner.

When execution termination unwinds outward, event state and resource registration still need to be reset, but page JS, error events, or microtask checkpoints must not run again. Only a shutdown request natively accepted and matching the current ticket can be classified as an expected cancellation; other terminations still report failure.

The start and completion of load are recorded separately. After a load event is interrupted, subsequent pageshow, pagereveal, or success callbacks are not sent, and load waiters receive a failure; document replacement resets the old state. Shutdown, document generation, and identity are checked after every call into page code.

An Inspector pause does not consume the JS execution budget, and host wait deadlines still apply. Shutdown first wakes the pause/start gate, then completes release on the owner. Window closure, Worker termination, and resource expiry are handled separately and are not conflated with clearing a shared queue.
