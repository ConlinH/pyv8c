# Resource Loading

[简体中文](../../pyv8c/resource-loading.md) | **English**

[Documentation Index](../README.md)

## Request Pipeline

Page navigation, element resources, Fetch, XMLHttpRequest, and Worker source loading cooperate through the resource capability entry point. Host-direct requests such as `page.net.get()` do not belong to the same pipeline.

The normal flow is: normalize the request and freeze the initiating context → interceptor → cache → local protocol → custom loader or HTTP → per-hop response policy → cache write → result delivery.

Local sources include about:blank, data, blob, and extension resources allowed by the environment declaration, taking precedence over the loader. An interceptor can provide a substitute response directly; a substitute response is not a full network transaction, and you should not assume it runs all cache and response policies of downstream sources.

Synchronous and asynchronous requests share source and redirect policies but are driven separately; the synchronous entry point does not wait on its own asynchronous queue. Asynchronous final results return to the owning owner queue for delivery, and results from cancelled or former owners no longer modify the page.

## Custom Loaders

The loader signature is `loader(url, request)` and may return a Web Response, a response dict, or convertible content. Returning an empty value does not automatically fall back to the real network.
Unsupported URL schemes are not handed to the loader. `chrome-extension:` only handles resources declared and matched in the environment profile; the current project contains no extension files, so placeholder content is returned and a warning is logged; it must not be treated as a real extension resource.

```python
from pyv8c.async_api import Browser, run

async def main():

    def load(url, request):
        if url == 'https://example.test/':
            return {'body': "<script src='/app.js'></script>", 'headers': {'content-type': 'text/html; charset=utf-8'}}
        if url == 'https://example.test/app.js':
            return {'body': 'globalThis.ready = true', 'headers': {'content-type': 'text/javascript'}}
        raise RuntimeError(f'未提供本地资源：{url}')
    async with Browser(real_network=False, resource_loader=load) as browser:
        page = browser.new_profile().new_page()
        await page.goto('https://example.test/')
        await page.wait_for('ready === true', timeout=1)
run(main())
```

Common keys of the response dict are `body`, `status`, `statusText`, `url`, and `headers`. Put binary data into the dict's `body` without first converting it to a text representation. Different resources should return the corresponding Content-Type; requests for unknown fixtures should fail explicitly rather than being uniformly disguised as a successful empty page.

request contains request information such as method, headers, body, mode, credentials, destination, and resource_type. This dict is callback input, not a protocol where modifying arbitrary fields rewrites the request.

## Interceptors

The interceptor signature is `interceptor(url, request)` and is called for every resource request before a source is selected.

| Return value | Behavior |
| --- | --- |
| None or True | Continue the original request |
| False | Return an empty response with status 200, not a network failure |
| Response object, dict, or content | Deliver as a substitute response |

```python
def intercept(url, request):
    if request.get("resource_type") in {"image", "style"}:
        return False
    return None
```

Intercepting images, styles, or scripts changes page execution and layout and is not suitable as a default condition for full page capability validation.

## Threading and Lifecycle

With `threaded_loaders=False`, ordinary host callbacks may run on the page owner, and blocking prevents that thread from advancing. `threaded_loaders=True` uses the Browser's bounded thread pool; host callbacks for Worker source loading, Fetch, and importScripts are forced onto the thread pool. Pool callbacks only handle input and ordinary data, do not access the page, Realm, or live JS handles, and do not wait back on the owner through `page.evaluate()`.

LoadHandle manages the request's atomic final state and late transport binding. ResourceOwnership registers requests with ResourceScope and releases their registrations at settlement. Cancellation stops further redirects and page delivery. Running Python work can finish cleanup.

The document resource coordinator maintains loading barriers using typed entries, tokens, navigation generations, and subdocument identities. After an iframe's transport completes, it still waits for valid subdocuments to finish; stale callbacks after removal, reinsertion, navigation, or srcdoc replacement must not release a new barrier or send an old load event.

A Worker's ResourceRequestSession freezes the resource configuration and effective origin. importScripts blocks the Worker's current call, waits for an ordinary transport result, does not wait on the parent page owner, and does not nest a drive of the event loop. The wait reuses the current operation's remaining deadline; termination or shutdown wakes the wait, and late results no longer execute scripts.

Window, navigation, and Worker loads share `ResourceRequestSession`. It uses the initiating context to advance cache, preflight, and redirect stages and returns plain Python response data. The Window and Worker adapters construct a Web Response on their respective owner threads. When configured, a custom loader or interceptor still runs in the pool or on the Window owner according to the callback settings.

`local.py` validates schemes and resolves local resources. `response.py` converts responses. `fetcher.py` and `worker.py` own their waiting and delivery behavior and share the Web Response constructor. The load handle detaches a completed transport before the request advances.

## Sources, Redirects and Security Policy

HTTP and loader responses use a shared per-hop policy. 301/302 change POST to GET and remove the body; 303 changes methods other than HEAD to GET; 307/308 preserve the method and body; at most 20 hops are followed. The manual/error modes use the corresponding redirect response or failure semantics respectively.

Same-origin and CORS decisions use the real Realm origin at request initiation rather than re-guessing from the URL. Navigation uses the navigate mode; ordinary fetch, classic element resources, and module scripts retain their respective modes. A cross-origin no-cors fetch can produce an opaque response; full preflight and the Fetch standard are not yet covered.

Shared Blob registration within a Profile does not grant cross-origin read permission. Revoking a Blob URL before creating a Worker causes the asynchronous load to fail; revoking the URL after constructing the Worker should not cancel the already-captured source. Blob bytes are handed off to the Worker startup thread through ordinary data.

Extension resources currently answer with placeholder content locally according to the public rules in the environment's `extensions`, and do not execute real extension packages or carry complete file bytes.

## Response Cache

The response cache is enabled with `cache=True` and an explicit `cache_dir`. Each Profile checks its own memory cache first, retaining up to 256 entries or an estimated 16 MiB. Disk access runs in the loader pool. Instances pointing at the same directory can share disk records.

The request handle orders cancellation and the cache publication claim. If cancellation wins, the cache job removes the temporary file. If publication wins, the loader thread completes the atomic replace. Shutdown waits asynchronously for the job to finish cleanup.

GET/HEAD may be written when cache conditions are met. The key includes the method, URL, mode, credentials, initiating origin, and normalized request headers; requests carrying Authorization, Cookie, or content matching the CookieJar bypass the cache. Request no-store/reload/no-cache, and responses with no-store/no-cache/private, Set-Cookie, Vary: *, non-success status, and the like, are not treated as ordinary cacheable responses.

Files use ordinary payload serialization and atomic writes and do not persist DOM-, Page-, or Realm-bound Response objects; cache read/write failures should not change the original request's success result. Full freshness, ETag/304, or revalidation is not currently provided, and redirect caching is not equivalent to Chrome's per-hop caching. Use `cache=False` for validation that requires strict cache semantics.
