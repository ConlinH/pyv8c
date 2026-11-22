# Browser / Profile / Page Interface Contract

This contract is the design basis for the public Python API. The project has
not yet released, and callers migrate directly; there are two explicit entry
points, `pyv8c.sync_api` and `pyv8c.async_api`, sharing a same-thread Runtime.
The root package no longer exports Browser, Profile, Page, or run; there are no
legacy entry aliases and no automatic mode switching.

## Objects and Ownership

`async with Browser(...) as browser` starts a browser on the current asyncio
loop and thread. `browser.new_profile()` synchronously creates a Profile with
isolated cookies, storage, and network session; `profile.new_page()`
synchronously creates an about:blank Page. `browser.new_page()` uses this
Browser's default Profile. Page exposes `profile`, and Profile exposes
`browser`.

Browser holds an internal Runtime by composition. Runtime is responsible only
for executors, thread ownership, scheduling, and background resource
reclamation; it holds no browser configuration, creates no Profiles, and is not
exported. Browser owns browser configuration, bindings, and the set of
Profiles. Workers keep their own threads.

All public Browser/Profile/Page operations run on the thread that started the
Browser; asynchronous operations must additionally run on the same event loop.
Direct cross-thread calls and suspending a wait inside a JS stack are
forbidden. Host resource-loading callbacks are a synchronous protocol that
exchanges only Python request/response data; Page APIs must not be called while
executing in a thread pool. Use the transport request/response protocol
explicitly when accessing the network yourself.

## Entry Points and Operation Boundaries

Synchronous applications use `from pyv8c.sync_api import Browser` and
`with Browser(...)`; asynchronous applications use
`from pyv8c.async_api import Browser` and `async with Browser(...)`. Both follow
Browser → Profile → Page, with a single implementation of page sessions,
resources, and the execution kernel. On the synchronous entry point,
navigation, waits, input, HTTP, and close return results directly; the table
below describes the asynchronous entry point. On both entry points, evaluate,
evaluate_handle, queries, and state reads complete synchronously. Synchronous
Profiles/Pages are created by factories on their owning Browser/Profile, never
constructed directly.

| Operation | Call form | Return boundary |
| --- | --- | --- |
| Create Profile / blank Page | synchronous | Object ready, no URL loaded |
| evaluate / evaluate_handle / state reads | synchronous | Current JS and its microtask checkpoint are done; JS Promises are not awaited |
| query_selector (including deep) / dispatch_event / set_visibility | synchronous | Current query, event, or state change and its checkpoint are done |
| goto / set_content | await | Reaches the commit, domcontentloaded, or load specified by wait_until |
| wait_for / wait_for_selector / wait_for_load_state / run_until_idle | await | Condition met or deadline reached |
| sleep | await (synchronous entry returns directly) | Fixed interval elapsed; the page event loop kept running, no JS executed |
| Mouse and Page input operations | await | Events and checkpoint are done; duration advances through cooperative waiting |
| page.net.request/get/post/... | await | Host transport returns a complete response |
| page.net.fetch/xhr | await | Page JS API returns a response, including wrapper logic installed by the page |
| Browser / Profile / Page.close | await | Event sources stopped, native objects released, and owned background work awaited |

`set_content` waits for load by default; `wait_until="commit"` allows
inspecting the page before parsing/resources finish. Internal parsers and JS
callbacks perform atomic operations through internal commit/retire methods and
do not call public coroutines in reverse. Internal retirement and public close
share one async finalisation task: a parent Page waits for child Pages to close
before releasing its own background resources. The network exposes no public
concurrent Future or `*_async` variants.

`deep=True` on `query_selector()` and `wait_for_selector()` is a
host-privileged query: it searches the current light DOM, then each shadow tree
(including closed), then descendant frames recursively in DOM order, returning
the first match in tree order. `deep=True` on `click()` / `dblclick()` /
`hover()` uses the same resolution and dispatches input through the mouse of the
Page that owns the node; the returned handle remains subject to that realm's
thread ownership. Combinators do not cross a shadow boundary, and `:host` /
`::slotted` are not implemented. See the [usage guide](usage.md).

## Timeouts, Cancellation, and Shutdown

Seconds are the uniform time unit. A timeout budget covers operation startup,
JS execution, and waiting; time queued before a synchronous entry enters the
persistent loop also consumes that budget, with elapsed time deducted before
the operation starts, and microtasks inherit the current execution budget. The
host HTTP budget additionally includes request encoding and Profile HTTP admission
queuing; remaining budget is checked before submission and before transmission
starts, so an exhausted request is not sent. Synchronous JS execution uses a
native termination mechanism rather than modifying V8.
Cancellation only stops the current observation/operation and never closes the
Browser automatically. Cancelling navigation prevents a late, uncommitted
response from overwriting the page; an already committed document is not rolled
back. Cancelling content injection or input also does not roll back DOM changes
or events that already happened; later steps stop, and a cancelled click does
not synthesise a click. Input actions bind the document generation before
waiting for the gesture lock and verify it after acquiring the lock. A
synchronous entry binds the generation when called and verifies again after
entering the event loop, so a navigation that became ready in between cannot
land the action on the new document. Input dispatch also checks the generation after
an event handler runs. If the handler replaces the document synchronously, for
example with `document.open()`, the gesture stops with `HostStateError`.

Shutdown is idempotent. New work is rejected once shutdown begins; cancelling
one shutdown waiter does not cancel the actual cleanup. A cleanup failure
retains the retry entry and ownership and does not report still-live resources
as fully closed. Page close still dispatches teardown events, but their handlers
cannot start synchronous resource loads. Close phase timings carry the
`close_timing` log field; phases taking at least 250 ms are logged as warnings.
Page close waits for its own load callbacks, host HTTP, and
Workers, but not for sibling Pages. Profile close waits for all its Pages, then
releases the shared network session; Browser waits last for the internal
Runtime and shared loading pool. Both built-in HTTP engines (`curl_cffi` and
`cronet`) attempt a real interrupt by cancelling the whole request coroutine on
their dedicated event loop, so a stalled connect, a wait for response headers
and a body-less call can all be stopped; custom HTTP engines must enforce request deadlines, and synchronous loader callbacks must bound their own execution time. The transport receives interruption through `RequestHandle.cancel()`. The scheduler releases the in-flight count after transport cleanup settles the Future. Shutdown waits for them to exit and does not claim
to cancel Python threads; an exhausted close budget raises `HostStateError` and
allows a retry.

The Page shutdown budget is 60 seconds without progress. Each observed background
completion, including a network request or a tracked host callback, renews that budget. When
that budget expires, close raises `HostStateError`: the in-flight count is not
released, the Page stays owned by its Profile, and `close()` can be retried with
a fresh budget. No terminal state is faked and no second result is delivered.
When the shared network session closes its engines, any engine failure is
reported while the other engines are still closed; failed references are kept so
the next `close()` retries only those, and references are cleared only after all
engines succeeded.

`page.closed` means the JS context has retired and scripts can no longer run;
all background resources are finished only when `await page.close()` completes.
A Browser's closed state means the execution kernel has been reclaimed.

## Event Loop

A synchronous Browser creates a persistent event loop on `with` entry, bound to
the calling thread and released after the Browser is fully closed. Waits,
navigation, input, and network operations advance on that same loop; synchronous
evaluate starts no loop and adds no extra event-loop round trip. Exiting a
Page/Profile context cleans only that scope, and the Browser loop stays usable.
Synchronous shutdown stops the owned page event sources before advancing the
loop, then awaits background cleanup. If shutdown fails or an observer
interrupts, the loop is retained so close can be retried. If startup is
interrupted, the opened Runtime is cleaned up and, on cleanup failure, the close
retry entry is likewise retained.

Every synchronous wait uses a snapshot of the ContextVar taken at call time.
When an operation is interrupted, that wait task is cancelled and finalised;
I/O already started and non-interruptible, and protected resource-cleanup tasks,
remain the responsibility of their owners.

Synchronous entry points must not be used inside an already running asyncio
loop; use async_api instead. Starting a synchronous wait from a Python callback
on any active JS stack is forbidden, and the check happens before driving the
loop. Synchronous and asynchronous objects cannot be passed to each other.
Each synchronous Browser drives only its own loop: nesting two Browsers does
not advance the outer one; use the asynchronous entry point to run multiple
Browsers concurrently. Host `time.sleep`, blocking I/O, or sustained computation
do not advance page timers, network callbacks, or Worker messages. Use explicit
business-condition waits such as page.wait_for and wait_for_load_state.

Standard `asyncio.run(main())` is supported; private precision thresholds are
not read to reject loops, and a running loop's clock is not modified. The
Python/system default loop determines wake-up granularity, while internal
deadlines are still checked against a monotonic clock. From the command line,
`pyv8c.async_api.run(main())` creates a high-precision loop; this is a startup
helper and introduces no other object model.

## Acceptance

On Windows, compare against frozen pre-refactor source, running the benchmark
with the same workload and the same native binary. Regression coverage includes
navigation, timers, microtasks, fetch/XHR, Workers, DevTools, timeouts,
cancellation, and shutdown, and checks ordinary asyncio loops as well as an
installed wheel. Pre-existing failures are listed separately rather than
masking regressions by changing expectations. Linux and macOS are not verified
at this stage, by user request.
