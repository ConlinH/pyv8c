# Browser tracing

[简体中文](../../pyv8c/tracing.md) | **English**

[Documentation index](../README.md)

## Enabling tracing

Trace observes the arguments, results, and exceptions of binding operations; request interceptors change resource results, and the two are independent.

```python
from pyv8c.async_api import Browser, run

async def main():
    async with Browser(real_network=False, traceable=True) as browser:
        page = browser.new_profile().new_page()
        page.trace = True
        page.evaluate("document.createElement('div').id")
        page.trace = False
run(main())
```

`trace=True` starts tracing on and allows it; `traceable=True` allows it to be enabled later. When both are off, you cannot temporarily set a page's trace to True. `page.trace` accepts only bool, and switching is serialized through the owner without rebuilding the page object.

Profile and Page can each set their own initial trace; configuration on Browser is not broadcast to change existing pages. The initial tracing of iframes and Workers is determined by their respective creation paths; toggling the parent page cannot be treated as a sync switch for the whole page tree.

## Filtering by script URL

`trace_scripts` accepts a Glob pattern string or a non-empty list of patterns. Patterns are matched case-sensitively against the **complete script URL**; `*` matches any length and `?` matches a single character; query parameters take part in matching. Filtering happens at the native trace entry, and unmatched operations do not convert trace arguments or enter callbacks.

```python
from pyv8c.async_api import Browser, run

async def main():
    async with Browser(trace=True, trace_scripts='https://cdn.example.test/sdk/p.js*') as browser:
        async with Browser(trace=True, trace_scripts=['https://cdn.example.test/sdk/p.js?*', 'https://cdn.example.test/sdk/ips.js?*']) as browser:
            pass
run(main())
```

`new_profile(trace_scripts=...)` and `new_page(trace_scripts=...)` can override the upper-level configuration; when omitted they inherit, and explicitly passing `None` clears the upper-level filter and traces all scripts. An iframe inherits the parent page's patterns, and a Worker inherits the patterns of the page that created it; a new Realm after navigation keeps the page's configuration. `page.trace` still only turns tracing on or off.

Attribution is based on the nearest named script on the JS stack when the operation occurs, not the URL that issued the resource request or the TOP/FRAME/Worker label. Timers, events, and Promise callbacks registered by the target script are also selected when they run. Inline scripts currently use the document URL as the compile filename; host operations with no attributable script name are not recorded once filtering is enabled. When `trace_scripts` is unset, the original trace behavior is kept.

## Event callbacks

Loguru is used by default. A custom trace_callback receives eight arguments:

```python
from pyv8c.pyv8 import format_trace

def observe(event, receiver, name, arguments, value, error, is_construct, ctx_type):
    message = format_trace(event, receiver, name, arguments, value, error, is_construct, ctx_type)
    if message is not None:
        print(message)
```

| Argument | Content |
| --- | --- |
| event | For example get, set, method, construct, has, or descriptor; depends on the binding operation actually entered |
| receiver | The host receiver object; for construct events the class, or None when it cannot be resolved |
| name | The property, method, or constructor name; an index may be an integer |
| arguments | A tuple of method/constructor arguments, or None for property events |
| value | The operation result or setter input; a failed read/call is pyv8.Undefined |
| error | pyv8.Undefined on success, or the Python representation of the exception on failure |
| is_construct | Whether it was called via new |
| ctx_type | The Context type tag, such as Window or Worker |

The callback runs synchronously on the thread the event belongs to. Multiple Realms can share a sink, and shared state must be synchronized by you; do not hold a lock and wait back on another owner. The callback's return value does not change the business result, the observer's own exceptions are isolated from business exceptions, and no observer callbacks are dispatched after execution terminates.

## Observation boundary

Trace mainly covers IDL getters/setters, methods, constructors, and named/indexed handlers that actually handle operations. It does not guarantee recording pure-JS custom properties, constants, ordinary method-property reads, or rejected operations that never enter a binding callback.

Primitive values are passed by value; complex JS objects use live references and are not automatically enumerated into Python data. The default format_trace avoids reading properties or calling a page-overridden toString; an object shown as `<JSObject>...` is not a missing value. On a failed set, the input value is still kept and not read back to confirm the write.

Accessing a JSObject, calling evaluate, or modifying the receiver inside a sink produces real side effects. For passive observation, use only the existing arguments and the formatter; keeping live references for a long time also extends object lifetimes, so release them promptly, and do not access them after the Context is closed.

## Locating problems

First capture the load result without injected probes, then enable observation for the earliest causal divergence. A single undefined, exception, or missing request is only a clue and should be judged together with the Realm, timing, and Chrome behavior comparison. The enabled state of trace, stub_logging, and DevTools should all be recorded as experimental conditions.

Unimplemented-member logging is controlled by `stub_logging`: off records nothing, on records each member once, and verbose includes more placeholder-handling information. A member-gap log does not mean execution actually failed, nor does it mean the placeholder behavior is correct.
