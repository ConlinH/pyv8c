"""Event-loop completeness: a realm drives itself without manual pumping.

The Runtime drains posted/timer/microtask sources on its owner thread, so
network responses, worker messages and timers make progress while the test
process only issues ordinary ``evaluate`` calls.  ``run_until_idle`` is a test
driver that yields until the realm is idle; it never has to be called for a
page to run.

"""

import asyncio
import statistics
import time
from urllib.parse import urlsplit

import pytest

pytest.importorskip("pyv8c.pyv8")
from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import RealmBusyError
from pyv8c.platform.page import Page

ORIGIN = "http://127.0.0.1:9"


async def test_wait_for_reports_its_deadline_when_the_owner_is_busy():
    from types import SimpleNamespace

    def busy(*args, **kwargs):
        time.sleep(0.02)
        raise RealmBusyError("owner did not answer")

    page = SimpleNamespace(_check_wait=lambda: None, evaluate=busy)
    with pytest.raises(TimeoutError, match="condition did not become true"):
        (await Page.wait_for(page, "false", timeout=0.01))


async def test_wait_for_preserves_an_early_owner_failure():
    from types import SimpleNamespace

    def busy(*args, **kwargs):
        raise RealmBusyError("owner unavailable")

    page = SimpleNamespace(_check_wait=lambda: None, evaluate=busy)
    with pytest.raises(RealmBusyError, match="owner unavailable"):
        (await Page.wait_for(page, "false", timeout=1))

#: A worker that exercises the scheduling surface its global scope shares with
#: a window.  Each source posts separately so the order is observable.
SCHEDULING_WORKER = """
self.onmessage = function () {
    queueMicrotask(function () { postMessage('microtask'); });
    Promise.resolve().then(function () { postMessage('promise'); });
    setTimeout(function () { postMessage('timeout'); }, 5);
    requestAnimationFrame(function () { postMessage('frame'); });
    postMessage('sync');
};
"""


def _loader(url, request):
    path = urlsplit(str(url)).path
    if path.startswith("/hello"):
        return {
            "status": 200,
            "url": str(url),
            "body": "hello from loader",
            "headers": {"content-type": "text/plain"},
        }
    if path == "/worker.js":
        return {
            "status": 200,
            "url": str(url),
            "body": (
                "self.onmessage = function (e) { postMessage(e.data * 2); };"
                "setInterval(function () {}, 50);"
            ),
            "headers": {"content-type": "text/javascript"},
        }
    if path == "/scheduling.js":
        return {
            "status": 200,
            "url": str(url),
            "body": SCHEDULING_WORKER,
            "headers": {"content-type": "text/javascript"},
        }
    return {"status": 200, "url": str(url), "body": "", "headers": {"content-type": "text/html"}}


@pytest.fixture
async def page():
    browser = (await Browser(resource_loader=_loader, stub_logging="off").__aenter__())
    context = browser.new_profile()
    page = context.new_page()
    await page.goto(ORIGIN + "/")
    try:
        yield page
    finally:
        (await page.close())
        (await browser.close())


async def wait_until(page, expression, until=True, timeout=8.0, interval=0.01):
    """Poll ``expression`` until it evaluates equal to ``until``.

    JS ``null``/``undefined`` surface as their own pyv8 types (never ``None``)
    and ``0``/``false``/``''`` are legitimate values, so pending state must be
    expressed as a distinct value and compared with ``==`` rather than truthiness.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = page.evaluate(expression)
        if last == until:
            return last
        await asyncio.sleep(interval)
    return last


async def test_fetch_resolves_without_manual_pump(page):
    page.evaluate("""
        window.__result = 'pending';
        fetch('/hello').then(function (r) { return r.text(); })
            .then(function (t) { window.__result = t; })
            .catch(function () { window.__result = 'rejected'; });
    """)
    assert (await wait_until(page, "window.__result", until="hello from loader"))


async def test_xhr_completes_without_manual_pump(page):
    page.evaluate("""
        window.__xhr = 'pending';
        var x = new XMLHttpRequest();
        x.open('GET', '/hello', true);
        x.onload = function () { window.__xhr = x.responseText; };
        x.onerror = function () { window.__xhr = 'error'; };
        x.send();
    """)
    assert (await wait_until(page, "window.__xhr", until="hello from loader"))


async def test_worker_postmessage_delivers_without_manual_pump(page):
    page.evaluate("""
        window.__worker = 'pending';
        var w = new Worker('/worker.js');
        w.onmessage = function (e) { window.__worker = e.data; };
        w.onerror = function () { window.__worker = 'error'; };
        w.postMessage(21);
    """)
    assert (await wait_until(page, "window.__worker", until=42, timeout=10.0))


async def test_microtask_checkpoint_runs_between_sources(page):
    page.evaluate("""
        window.__log = [];
        setTimeout(function () {
            window.__log.push('t');
            Promise.resolve().then(function () { window.__log.push('m-after-t'); });
        }, 5);
        Promise.resolve().then(function () { window.__log.push('m'); });
        window.__log.push('sync');
    """)
    assert (await wait_until(
        page,
        "window.__log.length === 4 && window.__log[window.__log.length - 1] === 'm-after-t'",
        until=True,
        interval=0.02,
    ))
    assert page.evaluate("window.__log") == ["sync", "m", "t", "m-after-t"]


async def test_posted_response_runs_before_soon_timer(page):
    # The fetch delivery is a posted task, the zero timer is a timer task:
    # the turn order (posted -> microtask checkpoint -> timers) makes the
    # fetch resolution observable before the timer callback.
    page.evaluate("""
        window.__order = [];
        fetch('/hello').then(function () { window.__order.push('fetch'); });
        setTimeout(function () { window.__order.push('timer'); }, 0);
    """)
    assert (await wait_until(page, "window.__order.length === 2", until=True))
    assert page.evaluate("window.__order") == ["fetch", "timer"]


async def test_a_short_timer_is_not_left_a_system_tick_late(page):
    """A timer fires close to the deadline it was armed with.

    Two platform details made this fail by a whole 64 Hz tick: Windows'
    ``time.monotonic`` only moves once per tick, so the deadline comparison was
    quantised, and an ordinary wait rounds up to the same tick.  The bound is
    deliberately loose -- it is a regression guard, not a precision claim.
    """
    lateness = []
    for _ in range(5):
        page.evaluate("""
            (function () {
                window.__fired = false;
                setTimeout(function () { window.__fired = true; }, 5);
            })()
        """)
        started = time.perf_counter()
        while not page.evaluate("window.__fired"):
            if time.perf_counter() - started > 2:
                pytest.fail("a 5 ms timer never ran")
            await asyncio.sleep(0)
        lateness.append((time.perf_counter() - started) * 1000)

    assert statistics.median(lateness) < 12.0, lateness


async def test_queue_microtask_shares_the_promise_queue(page):
    """``queueMicrotask`` and promise reactions are one FIFO queue.

    The specification puts both on the microtask queue, so they interleave in
    enqueue order.  A second queue of the embedder's own would instead run one
    kind entirely after the other.
    """
    page.evaluate("""
        window.__order = [];
        queueMicrotask(function () { window.__order.push('microtask-1'); });
        Promise.resolve().then(function () { window.__order.push('promise-1'); });
        queueMicrotask(function () { window.__order.push('microtask-2'); });
        Promise.resolve().then(function () { window.__order.push('promise-2'); });
        setTimeout(function () { window.__order.push('timer'); }, 0);
        window.__order.push('sync');
    """)
    assert (await wait_until(page, "window.__order.length === 6", until=True)) is True
    assert page.evaluate("window.__order") == [
        "sync", "microtask-1", "promise-1", "microtask-2", "promise-2", "timer",
    ]


async def test_queue_microtask_ignores_a_replaced_promise(page):
    """The page must not be able to break ``queueMicrotask``.

    The specification defines it on the implementation's own microtask queue, so
    replacing ``Promise`` must leave it working -- it is not a page-level API
    implemented in terms of a page-level object.
    """
    page.evaluate("""
        window.__log = [];
        const real = Promise;
        Promise = undefined;
        try { queueMicrotask(function () { window.__log.push('ran'); }); }
        catch (error) { window.__log.push('threw: ' + error); }
        Promise = real;
    """)
    assert (await wait_until(page, "window.__log.length === 1", until=True)) is True
    assert page.evaluate("window.__log") == ["ran"]


async def test_queue_microtask_is_not_routed_through_a_patched_then(page):
    """Wrapping ``Promise.prototype.then`` must not add observable steps.

    Instrumentation libraries patch exactly that method, and doing so must not
    move the microtask, duplicate it, or make it run through the wrapper.
    """
    page.evaluate("""
        window.__order = [];
        const realThen = Promise.prototype.then;
        Promise.prototype.then = function (onFulfilled, onRejected) {
            return realThen.call(this, function (value) {
                window.__order.push('then-wrapper');
                return onFulfilled ? onFulfilled(value) : value;
            }, onRejected);
        };
        queueMicrotask(function () { window.__order.push('microtask'); });
        Promise.resolve().then(function () { window.__order.push('user-promise'); });
        window.__order.push('sync');
    """)
    assert (await wait_until(page, "window.__order.length === 4", until=True)) is True
    # Only the page's own .then reaches its wrapper.
    assert page.evaluate("window.__order") == [
        "sync", "microtask", "then-wrapper", "user-promise",
    ]


async def test_throwing_microtask_does_not_stop_the_checkpoint(page):
    """A faulty microtask is reported, and the jobs behind it still run.

    The callback is queued on V8's own microtask queue, so an exception escaping
    it is reported the way V8 reports an uncaught microtask exception -- through
    the isolate, on stderr -- rather than as a window ``error`` event, and the
    checkpoint carries on with the jobs already queued behind it.
    """
    page.evaluate("""
        window.__log = [];
        queueMicrotask(function () { throw new Error('microtask boom'); });
        queueMicrotask(function () { window.__log.push('after-throw'); });
        Promise.resolve().then(function () { window.__log.push('promise'); });
        window.__log.push('sync');
    """)
    assert (await wait_until(page, "window.__log.length === 3", until=True)) is True
    assert page.evaluate("window.__log") == ["sync", "after-throw", "promise"]


async def test_worker_runs_timers_and_microtasks(page):
    """A worker global schedules on its own task queue, not on stub calls."""
    page.evaluate("""
        window.__workerOrder = [];
        var scheduling = new Worker('/scheduling.js');
        scheduling.onmessage = function (event) { window.__workerOrder.push(event.data); };
        scheduling.onerror = function (event) { window.__workerOrder.push('error: ' + event.message); };
        scheduling.postMessage('go');
    """)
    assert (await wait_until(page, "window.__workerOrder.length === 5", until=True)) is True
    prefix, tail = page.evaluate(
        "[window.__workerOrder.slice(0, 3), window.__workerOrder.slice(3).sort()]"
    )
    # The task body runs first, then its microtasks in enqueue order; the timer
    # and the frame request both run afterwards, in whichever order they are due.
    assert prefix == ["sync", "microtask", "promise"]
    assert tail == ["frame", "timeout"]


async def test_interval_repeats_and_can_be_cleared(page):
    page.evaluate("""
        window.__count = 0;
        window.__id = setInterval(function () {
            window.__count += 1;
            if (window.__count === 3) { clearInterval(window.__id); }
        }, 4);
    """)
    assert (await wait_until(page, "window.__count", until=3, timeout=6.0)) == 3
    (await asyncio.sleep(0.08))
    assert page.evaluate("window.__count") == 3


async def test_timer_fires_after_its_delay(page):
    page.evaluate("""
        window.__t0 = Date.now();
        window.__t1 = null;
        setTimeout(function () { window.__t1 = Date.now(); }, 30);
    """)
    assert (await wait_until(page, "window.__t1 !== null", until=True, timeout=6.0))
    elapsed = page.evaluate("window.__t1 - window.__t0")
    # The lower bound tolerates wall-clock drift between the JS Date.now()
    # used here and the monotonic clock the scheduler fires on; it still
    # catches a timer that ran far too early (delay ignored entirely).
    assert 15 <= elapsed < 500, f"timer fired after {elapsed}ms instead of ~30ms"
