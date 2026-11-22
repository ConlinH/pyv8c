# DevTools Architecture and Migration

[简体中文](../../pyv8/devtools.md) | **English**

## Public API

```python
from pyv8c import pyv8

class Window:
    pass

context = pyv8.Context(Window())
try:
    options = pyv8.DevToolsOptions(port=9005, startup_timeout=60)
    with pyv8.start_devtools(context, options=options) as handle:
        print(handle.url)
        result = handle.run(lambda ctx: ctx.exec_js(
            "globalThis.debugValue = 42", filename="example.js"
        ))
finally:
    context.close()
```

`start_devtools()` only starts the background listener; it does not run callbacks, print, or take over signals. A standalone Context has no executor,
so `handle.run(callback)` handles the handshake on the calling thread that created the Handle, then calls `callback(context)` and returns its result.
Pauses within the callback are also handled by this thread; the service is not closed automatically when the callback ends. Call run again when you want to debug again.
For a standalone Context, run, wait_for_ready, and close should be used on this calling thread; socket waits can be used on other threads.

| Option | Default | Behavior |
| --- | --- | --- |
| host | 127.0.0.1 | Local listener, no authentication, should not be exposed to untrusted networks |
| port | 0 | Port assigned automatically, obtained via Handle.port |
| wait_for_debugger | False | Whether the Browser waits for readiness at the execution entry; a standalone Context's run always handles the handshake first |
| pause_on_start | True | After debugging is ready, pause once at the first executable statement of the next host script |
| startup_timeout | None | Limits only explicit startup waits; None means no time limit |

`wait_for_connection(timeout)` waits for a socket, `wait_for_ready(timeout)` waits for Runtime.runIfWaitingForDebugger;
returns True on success, False on timeout or service shutdown. A connection does not mean debugging is ready. A run startup timeout raises TimeoutError,
and does not call callback. A timeout at the corresponding Browser entry does not run the awaited script, nor does it close the page.

First-statement pause is set at the execution boundary after successful compilation; it does not insert a debugger statement or recompile compiled Scripts.
Console Runtime.evaluate does not consume the first-statement pause. By default it is consumed only once per Target lifetime; reconnection does not repeat it.
The low-level Debugger.schedule_pause_on_next_statement(reason) saves a custom reason and supports cancellation and retry after compilation failure.

## Layers and Threads

| Layer | Files | Responsibility |
| --- | --- | --- |
| V8 Inspector | src/tools/debugger.cpp | One InspectorHub per Isolate, a separate group per Context, a separate native session per session |
| Native entry scheduling | src/isolate_scope.cpp, Context._try_inspector | Non-blocking entry into the Isolate, registering a release notification when busy |
| Scheduling | pyv8/devtools/scheduler.py | One mailbox per Isolate; startup waits and nested pauses share commands, no temporary queue is moved |
| Session and execution state | session.py | ready, startup waits, first-statement pause, native session close |
| Protocol routing | router.py, requests.py | Target/flattened session, connection generation, request correlation, discovery |
| Transport | transport.py | HTTP/WebSocket, single client, connection generation; does not enter or wait on V8 |
| Host adaptation | CallingThread / pyv8c.platform.devtools | Explicit Context, executor, URL, parent target and close callback |

Ordinary Inspector commands execute on the target host thread; before the Isolate is acquired, commands remain in the mailbox.
If the Isolate is busy, a release notification wakes delivery, preventing other threads from taking the command and then blocking on the V8 lock.
Pause notifications first mark the Isolate's pause thread, then are sent to the client. The pause thread enters the Context corresponding to the command,
and handles all of that Isolate's Inspector commands uniformly; it does not run the host task queue. Nested pauses use stack-style wait conditions.
Queued destruction tasks run only after resume; the last Inspector must not be destroyed on the native pause stack.

## Discovery, Sessions and Reconnection

- HTTP `/json`, `/json/list` return target descriptions, WebSocket addresses, and frontend addresses; `/json/version` returns protocol and V8 information.
- WebSocket supports `/devtools/browser/<service-id>`, `/devtools/page/<target-id>`; `/` points to the root target.
- A second client is rejected with WebSocket 1013. There is no multi-client coordination or authentication protocol.
- `Target.setAutoAttach` only supports flatten=true. The auto-attach scope is the direct child targets of the current parent session;
  a child session can enable the next level of auto-attach itself. Closing the parent session detaches its auto-attached child sessions.
- Attaching to an existing child target does not fall back to the startup wait. New child targets are registered before their startup script runs;
  waitForDebuggerOnStart decides whether to wait, and attachedToTarget.waitingForDebugger reports the actual state.
- Each explicitly attached session has its own native V8InspectorSession. Internal numbers increase globally and correlate the session,
  the original request number, and the connection generation the session belongs to; late responses, disconnect queues, and old sessions do not enter a new connection.
- On disconnect, old sessions are first invalidated, request mappings cleared, startup waits woken, and pauses resumed, before native sessions are released;
  the host and listener service are not closed. After reconnection, the frontend re-enables the Debugger and sets breakpoints; old commands are not replayed.

Shutdown order: reject requests → wake waits / resume execution → release session/Inspector on a legitimate thread → stop the network service.
Handle.close is idempotent; failures raise observable RuntimeError/TimeoutError instead of forcing release on the wrong thread.
Closing the Handle does not close the host Context. You must still observe the Isolate lifecycle constraints of Context/Script/JSObject.
The low-level native API does not allow a direct disconnect or Context close inside an active Inspector protocol/pause callback;
you should resume first, then close after returning to the host execution thread. The shared service arranges this order automatically.

## Legacy API Migration

| Old usage | New usage |
| --- | --- |
| start_devtools(ctx, callback=fn, port=9005) | handle = start_devtools(ctx, options=DevToolsOptions(port=9005)); handle.run(fn) |
| context.start_devtools(page, host=..., port=...) | context.start_devtools(page, options=DevToolsOptions(host=..., port=...)) |
| start_paused=True | DevToolsOptions(wait_for_debugger=True) |
| handle.wait / implicit process signal takeover | The caller maintains the program lifecycle, using close / context management |
| DevtoolsDebugger / DevToolsServer / DevToolsTarget | No longer public; use DevToolsOptions and DevToolsHandle |
| Debugger.run_js / implicit first-statement pause | The on_ready callback only reports readiness; low-level explicit schedule_pause_on_next_statement |

No second compatibility implementation is provided. Browser, Debugger, and the Target subset above are supported; full Page, Network, and DOM inspection is not implemented.
