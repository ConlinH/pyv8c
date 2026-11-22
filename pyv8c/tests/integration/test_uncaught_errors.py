"""What the page observes when code throws where nobody is catching.

An exception V8 raises inside a job it runs on its own -- a microtask, a promise
reaction -- never returns to an embedding frame, and an unhandled rejection is
not an exception at all.  Both used to stop at the host's stderr.  These tests
pin the observable behaviour: an ``error`` event on the realm's window, the
specification's rejection events, and a listener that throws without taking the
rest of the dispatch down with it.
"""
import time
from urllib.parse import urlsplit

import pytest

pytest.importorskip("pyv8c.pyv8")
from pyv8c.async_api import Browser

ORIGIN = "http://127.0.0.1:9"

#: A worker whose failures happen inside V8: a microtask job that throws and a
#: rejection that nobody handles.  Both must become observable to the page.
FAILING_WORKER = """
self.onmessage = function () {
    self.onunhandledrejection = function (event) { postMessage('scope:' + event.type); };
    queueMicrotask(function () { throw new Error('worker microtask'); });
    Promise.reject(new Error('worker rejection'));
};
"""


def _loader(url, request):
    path = urlsplit(str(url)).path
    if path == "/failing-worker.js":
        return {
            "status": 200,
            "url": str(url),
            "body": FAILING_WORKER,
            "headers": {"content-type": "text/javascript"},
        }
    return {"status": 200, "url": str(url), "body": "", "headers": {"content-type": "text/html"}}


def _watch_errors(page):
    """Collect the realm's error and rejection notifications in ``__seen``."""
    page.evaluate("""
        window.__seen = [];
        window.onerror = function (message) { __seen.push('onerror: ' + message); return true; };
        window.addEventListener('unhandledrejection', function (event) {
            __seen.push('unhandled: ' + event.reason);
        });
        window.addEventListener('rejectionhandled', function () { __seen.push('handled'); });
    """)


async def test_microtask_exception_reaches_the_window(page):
    """A microtask job that throws reports an error event."""
    (await page.set_content("<html><body>x</body></html>", url=ORIGIN + "/", wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    _watch_errors(page)

    page.evaluate("queueMicrotask(function () { throw new Error('from microtask'); });")
    (await page.run_until_idle(timeout=0.5))

    assert page.evaluate("window.__seen") == ["onerror: Uncaught Error: from microtask"]


async def test_unhandled_rejection_follows_the_specification(page):
    """The rejection events fire where the specification says they do.

    V8 notices a rejection as soon as the promise is rejected and revokes the
    notice if a handler appears before the checkpoint ends, so a rejection that
    is handled within its own turn must stay invisible, and one that is handled
    later gets ``rejectionhandled`` after ``unhandledrejection``.
    """
    (await page.set_content("<html><body>x</body></html>", url=ORIGIN + "/", wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    _watch_errors(page)

    page.evaluate("Promise.reject(new Error('caught at once')).catch(function () {});")
    (await page.run_until_idle(timeout=0.3))
    assert page.evaluate("window.__seen") == []

    page.evaluate("(async function () { throw new Error('from async'); })();")
    (await page.run_until_idle(timeout=0.3))
    assert page.evaluate("window.__seen") == ["unhandled: Error: from async"]

    # A handler attached after the turn gets the paired event instead.
    page.evaluate("window.__seen = []; window.__late = Promise.reject(new Error('late'));")
    (await page.run_until_idle(timeout=0.3))
    assert page.evaluate("window.__seen") == ["unhandled: Error: late"]

    page.evaluate("window.__late.catch(function () {});")
    (await page.run_until_idle(timeout=0.3))
    assert page.evaluate("window.__seen") == ["unhandled: Error: late", "handled"]


async def test_listener_exception_is_reported_and_dispatch_continues(page):
    """A listener that throws is reported, and the remaining ones still run."""
    (await page.set_content("<html><body><button id=b>x</button></body></html>", url=ORIGIN + "/", wait_until="commit"))
    (await page.run_until_idle(timeout=1))

    seen = page.evaluate("""
        (function () {
            const order = [];
            window.addEventListener('error', function (event) { order.push('error: ' + event.message); });
            const button = document.getElementById('b');
            button.addEventListener('click', function () { order.push('first'); throw new Error('boom'); });
            button.addEventListener('click', function () { order.push('second'); });
            button.dispatchEvent(new Event('click'));
            order.push('returned');
            return order;
        })()
    """)

    assert seen[0] == "first"
    # The message carries the throw site and stack, so only its head is fixed.
    assert seen[1].startswith("error: Error: boom")
    # A browser keeps dispatching: the escaping exception used to skip the rest.
    assert seen[2:] == ["second", "returned"]


async def test_a_script_that_raises_is_reported_once(page):
    """An exception the embedder catches is not reported a second time.

    The isolate only hears about what no ``TryCatch`` caught, so a script that
    throws synchronously reaches the page through the script pipeline alone.
    """
    (await page.set_content("""
        <html><body><script>
            window.__seen = [];
            window.addEventListener('error', function (event) { __seen.push(event.message); });
            throw new Error('from inline script');
        </script></body></html>
    """, url=ORIGIN + "/", wait_until="commit"))
    (await page.run_until_idle(timeout=1))

    assert len(page.evaluate("window.__seen")) == 1


async def test_worker_failures_reach_the_parent_and_the_scope():
    """A worker's own V8 failures are no longer silent.

    The worker's global reports its unhandled rejection where the promise lives,
    and the exception V8 could not deliver reaches the parent through the Worker
    object.
    """
    browser = (await Browser(resource_loader=_loader, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<html><body>x</body></html>", url=ORIGIN + "/", wait_until="commit"))
        (await page.run_until_idle(timeout=1))
        page.evaluate("""
            window.__log = [];
            const worker = new Worker('/failing-worker.js');
            worker.onmessage = function (event) { __log.push(event.data); };
            worker.onerror = function (event) { __log.push('error:' + event.message); };
            worker.postMessage('go');
        """)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and page.evaluate("window.__log.length") < 2:
            (await page.run_until_idle(timeout=0.1))
        log = page.evaluate("window.__log")
    finally:
        (await browser.close())

    assert "error:Uncaught Error: worker microtask" in log
    assert "scope:unhandledrejection" in log
