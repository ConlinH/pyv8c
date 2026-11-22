# Request headers

[简体中文](../../pyv8c/request-headers.md) | **English**

[Documentation index](../README.md)

## Generation entry points

Request header rules are centralized in `pyv8c/pyv8c/platform/resources/request_headers.py` and used by resource requests, host page.net, and WebSocket handshakes through their respective adapters. User-Agent, language, and Client Hints come from BrowserEnvironment and should stay consistent with what the page's navigator reports.

Request mode and source are inputs to header generation, not determined by the target URL alone. The initiating origin uses the Realm's effective identity, preserving inheritance and opaque origin semantics. Actual on-the-wire headers are still affected by the transport engine and HTTP protocol, so you cannot claim that names, values, and ordering exactly match all Chrome versions.

## Scenario rules

| Request | destination | Typical mode |
| --- | --- | --- |
| Main document navigation | document | navigate |
| iframe navigation | iframe | navigate |
| Classic script | script | no-cors |
| Module import | script | cors |
| Styles, images, audio, video | style, image, audio, video | no-cors; the specific request configuration can be adjusted |
| Fetch, XMLHttpRequest | empty | cors or an explicit request mode |

Accept is chosen by resource type; Accept-Language is generated from the language list. Sec-Fetch-Mode and Sec-Fetch-Dest record the request context, and Sec-Fetch-Site is generated from the initiator-target relationship.

Navigations may include Upgrade-Insecure-Requests. Navigations initiated by the browser or a user action may carry Sec-Fetch-User; a script setting location or calling an element method cannot be treated directly as real user activation. Referer is generated per the current referrer policy, with strict-origin-when-cross-origin as the usual default; Origin depends on the method, mode, and cross-origin relationship.

Host, Content-Length, and HTTP connection information belong partly to the transport layer. Caller-supplied custom headers are replaced or inserted per the adapter rules; environment headers should not be concatenated again in business logic.

## Ordering and protocol

The framework preserves header dictionary order, and navigations, ordinary resources, media, and WebSocket use their respective orderings. The underlying engine may move Host, Connection, and Content-Length, and HTTP/2 is not equivalent to the textual header ordering of HTTP/1.

Do not treat a wire capture from one transport or Chrome version as the fixed answer for all environments. When comparing, record the client version, TLS/HTTP protocol, page origin, method, body, default headers, and user custom headers, and use a local echo server to observe the requests that actually arrive.

## Implementation boundary

- site determination uses the offline Public Suffix List snapshot bundled with `tldextract`, including private suffixes; that snapshot may differ from a specific Chrome version. same-origin is still judged by exact origin.
- Full CORS OPTIONS preflights are not yet covered; correct request headers alone do not mean the Fetch security model is complete.
- Some Connection decisions approximate via the URL scheme; the actually negotiated protocol may differ.
- Range, Accept-Encoding, and header order for media requests vary by version and implementation.
- WebSocket handshake ordering depends on the chosen implementation; compression declarations and decoding capability depend on the transport build.
- Environment UA, IDL, and HTTP engine identifiers are configured separately; version mismatches must be explained individually and cannot be hidden by overriding a single header.

## Local verification

```powershell
python -m pytest pyv8c/tests/unit/test_request_headers.py pyv8c/tests/integration/test_request_headers_on_the_wire.py -q
python -m pytest pyv8c/tests/repros/test_request_headers_vs_chrome.py -q
```

The request-header and form comparisons can use `PYV8C_CHROME_BASELINE` to select Chrome 154.0.8030.0 and pin its header behavior. If unset, they use `PYV8C_CHROME` or the system Chrome. When a comparison fails, check the actual framework, transport, and Chrome release behavior while preserving the assertions for header names, values, and order.
