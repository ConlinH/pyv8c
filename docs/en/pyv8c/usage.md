# Usage Guide

Browser uses the application owner thread and cooperative waits; see the [execution contract](cooperative-runtime.md).

[简体中文](../../pyv8c/usage.md) | **English**

[Documentation Index](../README.md)

## Creation and Shutdown

`Browser` manages shared configuration, `Profile` manages session state, and `Page` manages pages. Cookies and in-memory state are independent across Profiles; a single Profile can create multiple pages.


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
        profile = browser.new_profile()
        page = profile.new_page()
        await page.set_content("<p id='text'>内容</p>", url='https://example.test/')
        assert page.evaluate("document.querySelector('#text').textContent") == '内容'
asyncio.run(main())
```

`new_page()` defaults to `about:blank`. Use `goto()` to load the main document; do not conflate creating a Page with completing network navigation.

`await page.close()`, `await profile.close()`, and `await browser.close()` release owner resources level by level; a normal shutdown can be called repeatedly. If cleanup of an independent resource fails, an exception is reported while other resources are still released on a best-effort basis; an exception must not be treated as a successful shutdown. Closing a child page does not stop an execution group still used by other pages.

Public close APIs belong to the owner thread; independent stop signals use the internal control channel. A background task terminated by a matching shutdown ticket does not report a host failure; a host waiting on a synchronous operation still receives `pyv8.JavaScriptTerminated`. This exception inherits from `BaseException` and is not an ordinary JS exception. Catch it explicitly when classification is needed; real timeouts and unknown terminations must not be uniformly ignored.

## Save and reuse a signed-in session

Pass a directory to `new_profile()` to restore cookies and HTTP(S) `localStorage`. Only one profile can open a directory at a time. Use a separate directory for each account.

```python
from pathlib import Path
from pyv8c.sync_api import Browser

with Browser() as browser:
    with browser.new_profile(
        user_data_dir=Path("userdata/account-a"),
        persist_session_cookies=True,
    ) as profile:
        page = profile.new_page()
        page.goto("https://example.com/")
        # Follow the site's sign-in flow here.
        profile.flush()
```

When you reopen the directory, the first page request can use restored cookies that are still valid and eligible to be sent. By default, the profile saves cookies with an expiry. The example also opts in to restoring session cookies. With the async API, call `new_profile()` directly and use `await profile.flush()` to save immediately. Changes are batched during use, and normal shutdown commits any remaining changes. In sync applications, background saves run while the Browser loop is being driven. Call `flush()` before an idle period if you need the state on disk.

The SQLite file can contain credentials in cookies and site storage. Keep it in a private directory owned by the current user, outside version control and shared folders. Persistence covers cookies and HTTP(S) `localStorage`. `sessionStorage` lasts for the current page session.

## Page APIs

| Call | Contract |
| --- | --- |
| `set_content(html, url="about:blank")` | Injects HTML and awaits wait_until (load by default); commit exposes an in-progress document |
| `goto(url, timeout=30, wait_until="load")` | Navigates and waits for `commit`, `domcontentloaded`, or `load`; `None` is treated as the commit phase |
| `evaluate(script, filename=None, timeout=30)` | Executes source code and materializes returnable data; does not automatically await Promises |
| `evaluate_handle(script, filename=None)` | Returns a primitive value or a live JS handle; does not perform data snapshot conversion |
| `wait_for(expression, timeout=30)` | Waits for a JS condition to become true; the condition should have no side effects |
| `wait_for_load_state(state="load", timeout=30)` | Waits for the load state of the current document generation; fails if the document is replaced or closed |
| `run_until_idle(timeout=1.0)` | Waits for the target Agent's runnable work and checkpoints to drain; returns True on success and False on timeout |
| `sleep(seconds)` | Waits a fixed interval while the page's event loop keeps running; runs no JS and waits on no condition |
| `dispatch_event(event_type, ...)` | Dispatches a host-constructed event to the specified window, document, or element |
| `set_visibility(hidden)` | Updates visibility and drives related lifecycle behavior |

`goto(..., wait_until="domcontentloaded")` may return while `load` listeners are still running. Network request completion, DOM readiness, and application readiness are different conditions and should be awaited separately. When the loading flow is terminated, waiting for `load` fails; an already-started dispatch is not treated as a successful load.

```python
await page.set_content('<script>globalThis.ready = false; setTimeout(() => ready = true, 0)</script>')
await page.wait_for_load_state('load', timeout=2)
await page.wait_for('ready === true', timeout=2)
```

`run_until_idle(timeout=0)` only queries state. Far-future timers, in-flight network activity, long-lived connections, and live Workers do not directly block idleness; do not use idle waiting as a substitute for waiting on business conditions. The owner thread must not synchronously wait on unfinished navigation or network operations.

`page.sleep(seconds)` differs from the host's `time.sleep()`: it lets the page's event loop keep running, so timers, network callbacks, and CDP-driven work can make progress during the wait. It executes no JS, so it makes no condition check and cannot raise `JavaScriptTerminated`. Use `sleep` to give the page time to advance; use `wait_for` / `wait_for_selector` to wait for a business condition, and do not treat `sleep` as a condition wait. In the async API it is equivalent to `await asyncio.sleep(seconds)` and exists to keep the sync/async interfaces aligned.

## Return Values and Handles

`evaluate()` converts objects and arrays into materializable Python data. When object identity such as DOM nodes or functions is required, use `evaluate_handle()`; operations must return to the owner thread of the owning execution group:

```python
handle = page.evaluate_handle("({answer: 42})")
snapshot = handle.to_python()
assert snapshot == {"answer": 42}
```

`page.executor` is an internal execution facility, used only when native handle operations are needed. Do not read handles directly from other threads, and do not pass live objects between unrelated pages. After navigation replaces the Context, an old handle is no longer an object of the new document; the handle becomes invalid once the original Context is finally closed.

## Environment Configuration

`BrowserEnvironment` uses flat key names and is immutable after construction. Values not explicitly set come from the bundled Windows environment profile and derived rules. When `environment` is omitted, `Browser` reads that Windows profile whole. A profile path is also read whole, for example `Browser(environment=profile_path("darwin"))`. A mapping or `BrowserEnvironment` still overlays the Windows profile. `new_page(environment=...)` can override the page environment.

```python
from pyv8c import BrowserEnvironment

environment = BrowserEnvironment({
    "languages": ["zh-CN", "zh"],
    "viewport.width": 1280,
    "viewport.height": 720,
    "viewport.device_scale_factor": 1,
})
values = environment.resolved()
assert values["viewport.width"] == 1280
```

Common configuration includes `user_agent`, `platform`, `language`, `languages`, `timezone`, `webdriver`, `hardware_concurrency`, `device_memory`, as well as `viewport.*`, `screen.*`, `client_hints.*`, `media.*`, `permissions.*`, and others. Not every string is valid; unknown keys and invalid values are rejected during validation.

`timezone` is an IANA time zone and `language` is a BCP 47 language tag. Each top-level page and each Worker keeps its own pair, including one created after another. An iframe uses the time zone and language of its page. `Intl` without an explicit locale uses `language`. The weekday and month in `Date()` stay English abbreviations, and the long time-zone name in parentheses follows the language. Aliases are canonicalized; for example, `US/Eastern` becomes `America/New_York`. An empty or unrecognized time zone or language raises `ValueError` when the page or Worker is created.

`to_dict()` returns the explicit configuration, and `resolved()` returns the fully resolved values; `overlay()` creates a new environment with overrides applied, while `from_file()` and `to_file()` work with JSON files. Changing environment values does not automatically provide real hardware, permissions, or media decoding capabilities.

`form_controls.submit_label` and `form_controls.reset_label` configure the browser UI text for submit and reset buttons when `value` is not explicitly specified, and are used to compute button dimensions. This text is determined by the browser UI language and may differ from `navigator.language`; the bundled profile uses the Chinese labels from local Chrome. They do not change the `value` attribute of input elements.

`form_controls.file_button_label` and `form_controls.file_empty_label` are used to estimate the dimensions of the file-selection control's button and the placeholder text shown when no file is selected; they should also be configured according to the browser UI language.

`form_controls.time_font_family` specifies the actual font family used by the time control's default `monospace` font, for internal field dimension calculation; other fonts explicitly set by the page are not affected by this option.
The current time control estimates internal field dimensions using a 24-hour clock; other UI formats, custom fonts, and control pseudo-element styles are not yet guaranteed to match Chrome exactly.

## Browser Options

| Option | Default value and meaning |
| --- | --- |
| `real_network` | True; whether to install the real network engine |
| `http_engine` / `ws_engine` | `curl_cffi` / `websockets`; engine name, instance, factory, or None. `cronet` is the other HTTP name; `websockets` is the only built-in WebSocket name |
| `http_options` / `ws_options` | Per-engine configuration, validated when a Profile is created |
| `proxy` | None; direct connection when unset, overridable by Profile |
| `default_headers` | Default headers for host `page.net` requests |
| `resource_loader` / `resource_interceptor` | None; resource source and interception callbacks |
| `threaded_loaders` | False; whether to place host callbacks in a bounded thread pool |
| `cache` / `cache_dir` | True / None; a persistent resource response cache exists only when a directory is specified |
| `frame_rate` | 60.0; virtual frame clock frequency of the page tree, must be a finite positive number |
| `trace` / `traceable` / `trace_callback` / `trace_scripts` | False / False / None / None; tracing configuration and script URL filtering |
| `expose_unimplemented` | True; whether to expose unimplemented interfaces in the baseline |
| `stub_logging` | `off`; can also be `on` or `verbose`, controls logging of unimplemented members |
| `interface_overrides` | None; experimental replacement callbacks for interface members |
| `webgl_backend` | `simulated`; WebGL backend, `angle` is experimental (see [ANGLE prototype](angle-prototype.md)) |
| `angle_library_dir` | None; EGL/GLES library directory for ANGLE, resolved by `comfy-angle` when unset |
| `idl_path` | None. When omitted, a packaged Mac profile path pairs with `idl/idl-158.0.8127.36.zip` (a missing file falls back). `Browser()` with no environment, a mapping, a `BrowserEnvironment`, and any other profile all read the in-package `idl.zip`. An explicit path wins. A direct `Idl.load()` still reads the in-package `idl.zip` |
| `baseline_mismatch` | `warn`; warns when the implementation or Window order does not match the selected baseline, `error` enables strict validation |

`new_profile(proxy=..., trace=..., traceable=..., trace_callback=..., trace_scripts=...)` can set the corresponding session options; `new_page(trace_scripts=...)` can also override script filtering, see [Browser Tracing](tracing.md). For thread restrictions on network and resource callbacks, see [Resource Loading](resource-loading.md).

The format of `interface_overrides` is `{"接口名": {"成员名": 回调}}`; a property can use `{"get": 回调, "set": 回调}`. Instance methods receive `(receiver, *args)`, static methods receive `(*args)`, property reads receive `(receiver)`, and writes receive `(receiver, value)`. Overrides for unknown members, illegal slots, and writes to read-only properties are rejected; constructors, constants, and named/indexed handlers are not overridden. Official fixes should go into the owning implementation layer.

## Network Calls

`await page.net.get()` and `await page.net.request()` are asyncio host requests using the Profile backend and cookies. They do not execute page fetch or the full resource policy. Cancellation first tries to interrupt the in-flight request coroutine, then stops observing the result; the transport receives interruption through `RequestHandle.cancel()` and owns settlement after cleanup. Page closure still waits on the in-flight count, and an exhausted budget raises `HostStateError` and allows a retry.

`await page.net.fetch()` and `await page.net.xhr()` call the page's current Fetch/XMLHttpRequest, whose implementations may have been replaced by the page; the result is a host response snapshot subject to the page resource pipeline and the corresponding same-origin rules. Do not treat the two groups of calls as interchangeable entry points.

## Events and Input

```python
await page.set_content("<button id='go'>继续</button>")
page.evaluate("document.querySelector('#go').addEventListener('click', () => globalThis.clicked = true)")
await page.click('#go')
await page.wait_for('globalThis.clicked === true', timeout=1)
```

The host supports `click()`, `dblclick()`, `hover()`, and `page.mouse`. Input behavior is based on current layout and hit testing; geometry and real Chrome rendering may still differ. `dispatch_event()` provides explicit event dispatch and is not equivalent to a full input action sequence.

## Reaching into Shadow DOM and iframes

`query_selector()`, `click()`, `dblclick()`, `hover()`, and `wait_for_selector()` all take a `deep` keyword argument. The default `deep=False` searches only the current document's light DOM, matching page `document.querySelector()`. With `deep=True` the host searches in this order and returns the first match:

1. the current document's light DOM;
2. each host's shadow tree in tree order, including `mode='closed'` (whose root page script cannot retrieve again);
3. each descendant frame recursively, in the order its iframe appears in the DOM.

When more than one frame matches, the first match in tree order wins. This is a host capability: `deep=True` reaches closed shadow trees and cross-origin frames that page JavaScript cannot see, and corresponds to no page API.

```python
node = page.query_selector('#widget', deep=True)      # maybe inside a shadow tree or frame
await page.click('#widget', deep=True)                # driven by the owning Page's mouse
node = await page.wait_for_selector('#late', deep=True, timeout=3)
```

`click()` / `dblclick()` / `hover()` dispatch input through the mouse of the Page that owns the node, so no manual switching is needed across frames. The returned handle belongs to its own realm; all later operations on it must still run on that Page's thread/executor.

Combinators match within a single query root and do not cross a shadow boundary: `#host #inner` will not find an `#inner` inside the host's shadow tree, while `div.wrap > #inner` works within the same tree. Shadow-scoped pseudo-classes such as `:host` and `::slotted` are not implemented by the selector engine. `wait_for_selector()` polls with the same query until a match or timeout, following the same timeout rules as `wait_for()`.

Application-side exceptions, host failures, network failures, and execution termination are different outcomes. When troubleshooting, first inspect the failing stage, then choose [Browser Tracing](tracing.md), the [Debugger](devtools.md), or local Chrome comparison; do not mask problems by extending fixed sleeps.
