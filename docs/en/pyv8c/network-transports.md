# Network Transport

[简体中文](../../pyv8c/network-transports.md) | **English**

[Documentation Index](../README.md)

## Transport Roles

HTTP and WebSocket are two independently replaceable roles. By default HTTP is `curl_cffi` and WebSocket is `websockets`; `cronet` is optional and uses the cycronet dependency. Both built-in HTTP engines share one cancellable asynchronous delivery skeleton, and `websockets` is the only built-in WebSocket implementation.

```powershell
python -m pip install -e .
# Install separately when cycronet is needed:
python -m pip install -e ".[cronet]"
```

Browser enables real networking by default; `real_network=False` does not install transports and is used for local resource testing. A missing dependency for the selected engine fails explicitly rather than silently switching to another engine.

```python
from pyv8c.async_api import Browser, run

async def main():
    async with Browser(http_engine='curl_cffi', ws_engine='websockets') as browser:
        page = browser.new_profile().new_page()
run(main())
```

`http_engine` and `ws_engine` can be a registered name, an object conforming to the protocol, a factory created once per Profile, or None. None means the corresponding role is missing; it does not mean all loaders and local resources are disabled.

## Arguments and Proxies

`http_options` and `ws_options` are passed to the corresponding engine. Built-in engines build validation from the keys accepted by themselves and the underlying client; misspelled options or options passed to the wrong role fail when the Profile is created. The supported set depends on the currently installed version, and you should not copy another engine's arguments over directly.

Common curl_cffi arguments include `impersonate`, `verify`, and `timeout`; the framework also handles `default_headers`, and the unified `proxy`. `impersonate` affects only the selected transport and does not synchronously change the IDL, navigator, device, or page environment.

The Browser's `proxy` is the unified HTTP/HTTPS proxy entry point and can be overridden by a Profile; unset or an empty string means a direct connection and does not automatically inherit the system proxy. Proxy credentials in the URL are supported; the unified proxy model does not support SOCKS. An engine must not declare proxy support yet silently ignore the configuration.

## Host Requests and Page Requests

Host HTTP, page resources and Worker HTTP share Profile admission for each hop. A root Page, its iframes and Workers share an ordinary request budget, with at most two host calls per group. Navigation has priority and blocking resource requests use at most one slot. Each has a bounded queue. curl_cffi allocates slots in one pool. Cronet uses a separate pool for navigation and blocking resource requests and shares the framework CookieJar across both pools. Queueing, preflights and redirect hops share the request deadline. A queued request can be cancelled, and closing waits for transport completion.

Built-in engines submit directly to the I/O loop. The loader pool handles disk cache I/O for asynchronous page resources and for Workers. The Page joins that work during close. Synchronous resource calls wait for transport completion without relying on the owner loop they block.

curl_cffi reserves one slot for blocking resources when at least ten connections are available. With two to nine connections, blocking resources take turns with ordinary requests within the ordinary budget, preserving navigation capacity. With one connection, navigation, blocking resources and ordinary requests take turns.

| Entry point | Behavior |
| --- | --- |
| `await page.net.get(url, ...)`、`await page.net.request(method, url, ...)` | Host network request; does not execute page Fetch and does not fully apply page resource policy |
| `await page.net.fetch(...)`、`await page.net.xhr(...)` | Calls the page's current Fetch/XMLHttpRequest and waits for a host response snapshot |

Common keywords for host requests include headers, params, data, json, cookies, timeout, referer, and options. Host and page requests share Profile Cookie state, but differ in security policy, interceptors, caching, and page override behavior.

NetworkRequest is constructed with keywords and contains URL, method, headers, body, timeout, credentials, mode, redirect, allow_redirects, origin, site_for_cookies, top_level_navigation, and options. It is a frozen transport input: headers and the Cookie header are finalised at build time, and a downstream adjustment (such as the remaining timeout budget while queued) must derive a new request with `dataclasses.replace`. `options` carries transport extension keys only; a backend ignores keys it has no equivalent for. NetworkResponse is ordinary response data providing read entry points such as status, headers, body, text, content, and json(); it is not a Web Response.

Each Profile owns one CookieJar. The first hop uses the request's credentials and site context. Both built-in engines share redirect policy: store the response's Set-Cookie headers, then select cookies for the next URL. Same-origin hops preserve an explicit Cookie header. Cross-origin hops strip the explicit Cookie, cookie overrides and Authorization before selecting cookies for the target. Access to document.cookie is also restricted by page rules. Use an engine factory or a registered name for multiple Profiles.

Cookie Domain is checked with a Public Suffix List snapshot built into the dependency; `SameSite=None` requires `Secure`, and some attributes of `__Secure-` / `__Host-` are validated. Page requests filter Strict, Lax, and default SameSite Cookies by initiating site, ancestor sites, and the top-level navigation method; host requests do not automatically apply the page site context. Partitioned Cookies and the browser's full Cookie policy are not yet implemented, so the CookieJar must not be treated as a Chrome security boundary.

A cross-origin CORS request first sends a credential-less OPTIONS preflight when required. Window and Worker requests can reuse successful authorization within the same Profile when the top-level origin, initiator origin, target URL, credential mode, method, and request header names match exactly. `Access-Control-Max-Age` controls expiry, capped at 600 seconds. Engines that cannot control redirects per hop reject requests requiring a preflight. Streaming request bodies, forced preflight triggered by XHR upload listeners, and private network access policy remain unimplemented.

## Real-Wire Regression

`tests/integration/wire_parity_harness.py` and `test_engine_wire_parity.py` replay the same scene against both built-in engines using real local servers: navigation headers and caller-supplied headers, the CookieJar as the only truth and multi-value `Set-Cookie` write-back, gzip and a 204 empty body, POST bodies and content-type, a redirect chain that delivers only its final hop, cross-origin Cookie stripping, and traffic carried through the unified proxy. Recordings preserve each hop's headers in arrival order, request body and multi-value response headers. Ports come from binding to 0, `daemon_threads` is paired with `shutdown()`/`join()` for reclamation, and records belong to the fixture instance rather than a module-level global, so `--dist loadscope` is safe.

Chrome's absolute header baselines and libcurl header ordering stay with `test_request_headers_on_the_wire.py` and the repros. The new regression does not duplicate a second drifting constant set, nor treat differences between the two underlying stacks as defects: `Host`, `Connection` and `Content-Length` (placed by the transport) and the empty `Cookie` Cronet injects to suppress its client jar are removed before comparison. Only the header set and values the framework owns must agree.

Connection-establishment evidence comes from a local raw TCP recorder that reads one TLS ClientHello and deliberately never answers. The request must therefore end in a failed handshake; the test asserts what was recorded, with no certificate, no `verify=False` and no external network. Parsing covers only the TLS record header, handshake header, cipher suites, supported_versions, ALPN and SNI, and normalises the result into an unordered, GREASE-free capability set (TLS 1.3, ALPN `h2`/`http/1.1`, TLS 1.3 ciphers and key extensions). That shared capability set is the regression baseline; no ja3/ja4 string, suite order, curve list or other absolute fingerprint is pinned.

The cross-origin fixture uses two loopback addresses, `127.0.0.1` and `127.0.0.2`, so the cookie domains genuinely differ. A sandbox exposing only `127.0.0.1` falls back to a different port and drops the Cookie-stripping assertion, because cookie scope ignores ports. The framework handles redirects and recomputes cookies on each hop for both built-in HTTP engines. When cycronet is absent only that engine's parameterised cases skip, not the whole module; the module still requires the native extension and `pytest.mark.native`, and the offline gate runs unit tests only, so it is unaffected.

## Custom Transports

An HTTP engine implements `submit_request(request, handle, remaining)` and a synchronous `close()`. Submission can be called from different threads and returns a `concurrent.futures.Future` promptly. Call `remaining()` on the I/O loop before sending to obtain the remaining seconds or `None`. Settle the Future with `pyv8c.RequestTimeout` if this request deadline expires. Preserve earlier transport failures, including an engine's own `TimeoutError`. Return `None` from `close()` after releasing all resources and raise if teardown fails.

Register a thread-safe interruption hook with `handle.attach_abort(callback)` before starting I/O. An early cancellation invokes that hook as soon as it is registered. Deadline expiry and external cancellation share one interrupt, and the engine publishes the result after cleanup. Engines can implement an asynchronous `_single_call()` on `pyv8c.network.async_delivery.AsyncTransport` to reuse loop ownership, cancellation, deadlines, and shutdown. Its shutdown budget covers default-executor work such as DNS resolution.

Page close stops admitting HTTP work, interrupts its host and resource requests, and waits for background work and terminal transport notifications, renewing the close budget when it observes progress. If the budget expires, `HostStateError` leaves the work owned and a later `close()` can retry. Document replacement and resource cancellation propagate through `LoadHandle.cancel()`. Host request timeouts and cancellation use the same in-flight registration.

Worker cancellation also interrupts active HTTP requests, including a CORS preflight or the current redirect hop. Each hop detaches its interrupt handle before the Worker processes the response. The Page still waits for transport cleanup. The `background-futures` close log distinguishes `worker-resource` work from `host-callback` work. A running Python callback must still return on its own.

Both built-in engines share `AsyncTransport` and send one hop per submission. Each navigation, Window, Worker, or host HTTP hop reenters fair admission at the Profile. Resource requests share preflight, redirect, and response policy. Host HTTP uses its own redirect options. The whole request shares one deadline, and the Profile scheduler expires queued hops. The transport settles its Future after its I/O task exits and cleanup is complete. On Windows the curl_cffi thread uses a selector loop.

`network/backend.py` owns engine lifetime and admission. `network/host_http.py` handles host redirects, timeouts, and final delivery. Host and resource requests share `network/request_builder.py` to build transport requests and compute their remaining budget.

Engine close stops admission, waits for in-flight coroutines to finish cleanup, and then closes the session and loop. Timeouts or failures retain ownership for another close attempt.

WebSocket implements `websocket(tasks, url, protocols, callbacks, options=None)`, `close_websocket(socket)`, and `close()`. The role protocols are the public HttpTransport and WebSocketTransport respectively.

```python
from pyv8c import register_transport

# MyHttpTransport must implement the full transport protocol.
def install_transport(MyHttpTransport):
    register_transport(
        "local-http",
        lambda options=None: MyHttpTransport(options=options),
        option_keys=lambda: frozenset({"timeout"}),
    )
```

A registered factory is called with `options=<dict or None>`; an unregistered factory passed directly to Browser is called with no arguments. Existing instances and unregistered factories can no longer be configured through http_options/ws_options or the unified proxy, so configuration should be completed when the instance is created. `register_ws_transport()` registers the WebSocket role; `proxy=True` at registration means the unified proxy is actually supported. The asynchronous cancellation handle belongs to the transport layer and divides responsibilities with the page resource LoadHandle, and owner validation of late results must not be omitted.

The resource layer generally requires the engine to return a single-hop response, advanced by the shared redirects rules; an engine that cannot disable automatic redirects must explicitly declare that behavior and must not silently swallow intermediate hops.

## WebSockets and Limits

The built-in engine uses `websockets.asyncio` and requires `websockets 17.0` or newer for TCP cleanup when a proxy CONNECT is cancelled or times out. A Browser starts one WebSocket I/O loop on demand, and each Profile owns its connections and cookies. A Profile may register up to 256 connections and run up to 32 handshakes at once. Time spent waiting for a handshake slot counts toward `open_timeout`. WebSocket handshakes reject HTTP redirects.

`open`, `message`, `error`, and `close` run in order as networking tasks on the owning document or Worker. A message requires the socket to be `OPEN` at dispatch. Cross-document navigation retires old sockets before unload and stops accepting handshakes from the departing document. The Worker stop signal interrupts built-in transport handshakes directly. JS `close()` sends accepted messages in FIFO order before sending a close frame. Page or Profile shutdown cancels pending handshakes, gives idle connections a brief closing handshake, and waits for transports to exit.

Each connection has a 256-message and 16 MiB limit in each direction. A full receive queue pauses reads until the Runtime handles messages. Send admission counts queued messages and the active write, and writing releases that capacity immediately on the I/O loop. A full send queue fails the connection. Each non-throwing `send()` contributes to `bufferedAmount`; the Runtime acknowledges sent bytes in batches. Library options `max_size` and `max_queue` remain configurable. The default message limit is 1 MiB, individual messages are capped at 16 MiB, and the frame queue stays bounded. The built-in engine's `ws_options["create_connection"]` takes a `websockets.asyncio.client.ClientConnection` constructor. Independently registered engines continue to implement `WebSocketTransport`.

TLS, HTTP version, compression, proxy, and handshake permutations depend on the engine and dependency build; choosing a particular engine does not mean parity with Chrome's full network stack. For specific request header rules see [Request Headers](request-headers.md).

Transport methods exchange ordinary Python request/response data. The async Browser/Profile/Page API keeps V8 on its owner thread; transport close may run on a cleanup thread after the Profile pages have finished. Transport implementations must not enter V8 or depend on the application loop to finish close.

When the shared network session closes its engines, every engine is attempted
and a failure is reported to the host instead of being swallowed. Failed engine
references are retained so a later `close()` retries only those; `http_available`
and the other capability properties still describe the assembled engines until
all of them have closed, so shutdown-time admission checks remain the guard.
