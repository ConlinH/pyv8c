# Browser and its same-thread execution kernel

The public object model is **Browser → Profile → Page**. Browser owns configuration
and Profiles; Profile isolates session state; Page exposes document operations.
An internal Runtime owns executors and background cleanup through composition.
It is not an application entry point or a Browser base class.


Synchronous applications use a separate entry point. Loading, input, network,
waiting and cleanup return directly:

```python
from pyv8c.sync_api import Browser

with Browser(real_network=False) as browser:
    with browser.new_profile() as profile:
        with profile.new_page() as page:
            page.set_content("<title>Local page</title>")
            page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,2)")
            page.wait_for("ready", timeout=1)
            assert page.evaluate("document.title") == "Local page"
```

A synchronous Browser owns one persistent event loop on the calling thread.
Waits drive that loop; evaluate stays direct. time.sleep() does not drive page
work. Use async_api inside an existing asyncio loop; nested synchronous waits
are rejected before entering a loop, including waits inside active JS callbacks.
Root Browser/Profile/Page/run aliases have been removed. Both APIs share one
kernel, and objects cannot be mixed. Failed cleanup retains the loop for retry.
Each synchronous Browser drives its own loop; use async_api for concurrent Browsers.

The following examples use async_api. With sync_api, use the same method names
without await, and with instead of async with. Evaluation returns synchronously
in both entry points.

```python
import asyncio
from pyv8c.async_api import Browser

async def main():
    async with Browser(real_network=False) as browser:
        async with browser.new_profile() as profile:
            async with profile.new_page() as page:
                await page.set_content("<title>Local page</title>")
                assert page.evaluate("document.title") == "Local page"

asyncio.run(main())
```

Evaluation, handles and immediate queries are synchronous. Evaluation retains the
JS and microtask checkpoint and does not await a Promise. Loading, input, network,
waits and closure are awaited. set_content defaults to load; choose
wait_until="commit" to inspect a document whose resources are still loading.
run_until_idle only drains runnable work, not future timers or application state.

Pages and frames execute on the application thread; Workers retain dedicated
threads. Cross-thread Page calls fail. Internal producers deliver data through
queues. Blocking Python callbacks or computation prevent owner-loop progress.
A host wait cannot suspend inside an active JS stack. No nested loop is created.

Standard asyncio.run is supported without modifying the existing clock or
scheduled handles. The optional pyv8c.async_api.run helper creates a high-resolution loop
before scheduling work. Default loop wake-up granularity depends on Python and
the operating system. Internal deadlines use a monotonic clock. Inspector control
must run independently of a paused application loop; pauses suspend JS budgets.

await page.close() includes its host HTTP, loader callbacks, frames and Workers.
Profile closure then disposes shared network sessions. Browser closure finally
joins the internal Runtime and shared loader pool. Closure is idempotent;
cancelling an observer does not cancel cleanup. Failures retain ownership and
allow retry; both built-in HTTP engines attempt a real interrupt by cancelling
the request coroutine (a stalled connect, a header wait and a body-less call are
covered too), but an exhausted in-flight network wait budget
raises `HostStateError`, keeps the count and leaves the Page owned for another
close(). page.closed means
the JS context is retired; await close is the background completion boundary. Internal JS/Inspector callbacks retire synchronously
and schedule the same completion task as public close. Parents await child Page
completion. Queued gestures retain their admitted document generation. Host HTTP
budgets include encoding and queue time; expired requests do not reach transport.

Run examples/browser/cooperative.py for an offline navigation/fetch/timer/Worker
example. Regressions include test_browser_api_contract.py and the navigation,
timeout and DevTools suites. Measure fresh-process cooperative execution with the
unified entry point:
`python benchmarks/benchmark.py --cases execution_concurrent,execution_timer_load,entrypoint_async_evaluate --runs 5 --output build/cooperative-measurement.json`
(`--list` shows all cases). Compare matching workload/native hashes. Linux and macOS
validation is deferred at the user's request for this phase.
