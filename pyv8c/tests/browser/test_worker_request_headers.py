"""Whose request is it: a worker's fetch is attributed to the worker, not to its page.

Measured against Chrome 154 on 2026-10-07, on the Cloudflare challenge's own worker: the page
postMessages a ``fetch("https://challenges.cloudflare.com/…/pat/…")`` string to a worker started
from a blob URL, and that worker's request carries **no** ``Referer`` -- its script URL is a
``blob:`` and a blob is never a referrer -- with ``Sec-Fetch-Site: same-origin``.

The engine sent the owner page's URL instead, which is where the challenge's ``pat`` and ``ci``
requests started coming back ``400`` where Chrome gets ``401`` and ``200``.  The page's own fetch
still sends its referrer: the rule is about the initiator, not about referrers in general, and
both halves are held here so a fix for one cannot quietly break the other.
"""

import asyncio
import json
import threading
import time

from pyv8c.async_api import Browser

#: ``(url, request dict)`` in the order the loader was handed them.
SEEN = []

WORKER_SOURCE = ("fetch('https://worker.test/from-worker.json').then(function (response) {"
                 " self.postMessage('worker:' + response.status); })"
                 ".catch(function (error) { self.postMessage('worker-failed:' + error.name); });")

PAGE_HTML = """<!doctype html><body><script>
var out = {}; window.__out = out;
out.seen = [];
fetch('/from-page.json').then(function (response) {
  out.seen.push('page:' + response.status);
  var worker = new Worker(URL.createObjectURL(new Blob([__SOURCE__],
    {type: 'text/javascript'})));
  worker.onmessage = function (event) { out.seen.push(String(event.data)); };
  worker.onerror = function (event) { out.seen.push('onerror:' + String(event.message || event)); };
});
</script></body>""".replace("__SOURCE__", json.dumps(WORKER_SOURCE).replace("</", "<\\/"))

PROBE_URL = "https://worker.test/"




async def _pump(page, seconds=0.8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            (await page.run_until_idle(timeout=0))
        except BaseException:  # noqa: BLE001
            pass
        (await asyncio.sleep(0.01))


def _load(url, request):
    SEEN.append((str(url), dict(request or {})))
    if str(url).endswith(".json"):
        return {"body": "{}", "headers": {"content-type": "application/json"}}
    return {"body": PAGE_HTML, "headers": {"content-type": "text/html"}}


def _headers_of(url_suffix):
    for url, request in SEEN:
        if url.endswith(url_suffix):
            return {str(name).lower(): value for name, value in (request.get("headers") or {}).items()}
    raise AssertionError(f"no request to {url_suffix}; saw {[url for url, _ in SEEN]}")


async def test_a_workers_fetch_sends_no_referer_and_the_pages_own_fetch_does():
    del SEEN[:]
    browser = (await Browser(stub_logging="off", threaded_loaders=True, real_network=False,
                      resource_loader=_load).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto(PROBE_URL))
        (await _pump(page))
        seen = json.loads(page.evaluate("JSON.stringify(window.__out.seen)"))
        assert "page:200" in seen and "worker:200" in seen, seen
    finally:
        await browser.close()

    worker = _headers_of("from-worker.json")
    page_headers = _headers_of("from-page.json")
    #: The worker: a blob script URL cannot be a referrer, and the request is same-origin
    #: because the blob carries the page's origin.
    assert worker.get("referer", "") == "", worker
    assert worker.get("sec-fetch-site") == "same-origin", worker
    assert SEEN[-1][1].get("initiator_url", "").startswith("blob:") or any(
        str(request.get("initiator_url", "")).startswith("blob:") for _url, request in SEEN), SEEN
    #: The page: unchanged, it still names itself.
    assert page_headers.get("referer", "").startswith("https://worker.test/"), page_headers
