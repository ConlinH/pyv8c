# Debugger

[简体中文](../../pyv8c/devtools.md) | **English**

[Documentation index](../README.md)

## Startup and connection

The protocol, sessions, network service, and Isolate mailbox are provided by `pyv8.devtools`; pyv8c handles target descriptions and lifecycle integration for pages, iframes, and Workers.

```python
from pyv8c.async_api import Browser, run

async def main():
    from pyv8c import DevToolsOptions
    async with Browser(real_network=False) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        handle = profile.start_devtools(page, options=DevToolsOptions(pause_on_start=True, wait_for_debugger=True, port=9005))
        print(handle.url)
        page.evaluate('globalThis.answer = 42', filename='example.js')
        input('连接调试器后，按回车结束：')
run(main())
```

DevToolsOptions defaults to host `127.0.0.1`, port 0, wait_for_debugger False, pause_on_start True, and startup_timeout None. port=0 allocates a port automatically.

Set `wait_for_debugger=True` and an appropriate startup_timeout when business logic must wait until the client is ready. A startup handshake timeout explicitly raises TimeoutError and does not count an unexecuted script as success. `pause_on_start` controls the pause at the first statement of the next host script after the handshake; initialization helpers do not consume that pause opportunity.

## Handles and targets

Handle provides url, host, port, close, `wait_for_connection(timeout)`, and `wait_for_ready(timeout)`. Both waits return bool; a connected WebSocket does not mean the Debugger handshake is complete. pyv8c owns its executor and calls `page.evaluate()` normally, rather than using the handle.run workflow of a separate native Context.

The service provides `/json`, `/json/list`, `/json/version`, and a WebSocket debugging address. Target IDs live as long as the host; after reconnecting, clients must re-enable the relevant domains and restore breakpoints; filename determines the script source name and URL breakpoint matching.

This is currently a single-client service; a second WebSocket is rejected. The listening port has no browser-level authentication and should stay bound to localhost.

flattened Target.setAutoAttach manages direct sub-targets per parent session. iframes/Workers are registered before their startup scripts, and waitForDebuggerOnStart controls whether to wait; attaching to an existing target does not mean restarting it. Detaching a parent session cleans up child sessions, and closing the parent page closes its sub-targets.

## Pause and shutdown

Windows in the same page tree share an Isolate; Workers use separate Isolates. While paused, the corresponding paused thread handles Inspector commands and does not run normal page tasks; unrelated execution groups keep working.

An Inspector pause does not consume the JS execution budget, and host waits still have deadlines. Shutdown first releases the startup gate and pause, then destroys sessions and targets on the legal owner thread, and finally stops the service. A normal shutdown can be called repeatedly, and failures must be reported; closing the Handle itself does not close the page.

## Capability scope

It supports the V8 Inspector and the framework's Target management, but does not provide the full CDP Page, Network, or DOM domains, nor multi-client coordination. A Chrome frontend being able to connect does not mean all Chrome DevTools features are available.

Dedicated entry points are `pyv8c/tests/browser/test_devtools.py` and `pyv8c/tests/integration/test_lifecycle_stress.py`. Pause notifications, detachment, and shutdown involve concurrency and need repeated validation with failure evidence retained; a passing rerun alone cannot prove a race is resolved.

The sync_api debugger handle drives the Browser loop during wait_for_connection and
wait_for_ready. With async_api, call the native handle waits through asyncio.to_thread
to keep the owner loop responsive. Both waits return bool.
