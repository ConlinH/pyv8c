"""Concurrent and lifecycle stress for Page, worker and DevTools realms.

These tests interleave owner-loop coroutines and then tear pages down
mid-flight.  The assertions are deliberately structural: no thread may wedge,
``close()`` must stay idempotent under races, responses that land after a
close must be dropped silently, and the process must return to its baseline
thread count.
"""
import asyncio
import threading
import time
from urllib.parse import urlsplit

import pytest

pyv8 = pytest.importorskip("pyv8c.pyv8")
pytestmark = [pytest.mark.race, pytest.mark.slow]

from pyv8c.async_api import Browser

ORIGIN = "http://127.0.0.1:9"


def _loader(url, request):
    path = urlsplit(str(url)).path
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
    if path == "/closing.js":
        return {
            "status": 200,
            "url": str(url),
            "body": "self.onmessage = function () { self.close(); };",
            "headers": {"content-type": "text/javascript"},
        }
    if path.startswith("/slow"):
        time.sleep(0.4)
    return {"status": 200, "url": str(url), "body": "", "headers": {"content-type": "text/html"}}


@pytest.fixture
async def browser():
    browser = (await Browser(resource_loader=_loader, stub_logging="off").__aenter__())
    try:
        yield browser
    finally:
        (await browser.close())


async def test_concurrent_close_is_idempotent(browser):
    page = browser.new_profile().new_page()
    await page.goto(ORIGIN + "/")
    await asyncio.gather(*(page.close() for _ in range(4)))
    assert page.closed
    (await page.close())  # still idempotent afterwards
    (await browser.close())
    (await browser.close())  # browser-level close is idempotent too


async def test_evaluate_races_page_close(browser):
    page = browser.new_page()
    await page.goto(ORIGIN + "/")
    async def evaluate_until_closed():
        while not page.closed:
            page.evaluate("Math.random()")
            await asyncio.sleep(0)
    running = asyncio.create_task(evaluate_until_closed())
    await asyncio.sleep(.01)
    await page.close()
    await asyncio.wait_for(running, 1)
    assert page.closed


async def test_worker_message_delivery_races_page_close(browser):
    first = browser.new_profile().new_page()
    await first.goto(ORIGIN + "/")
    second = browser.new_profile().new_page()
    await second.goto(ORIGIN + "/")
    for page in (first, second):
        page.evaluate("""
            window.__worker = 'pending';
            var w = new Worker('/worker.js');
            w.onmessage = function (e) { window.__worker = e.data; };
            w.onerror = function () { window.__worker = 'error'; };
            w.postMessage(7);
        """)
    # Let the workers spin up, then tear both pages down concurrently while
    # worker messages and intervals are still in flight.
    (await asyncio.sleep(0.1))
    await asyncio.gather(first.close(), second.close())
    assert first.closed and second.closed


async def test_a_worker_that_closed_itself_does_not_wedge_the_page_close(browser):
    """A worker's own ``close()`` tears its realm down on the owner thread; the page's
    ``terminate()`` must find that done rather than dispatch onto the dead context."""
    page = browser.new_profile().new_page()
    await page.goto(ORIGIN + "/")
    page.evaluate("""
        var w = new Worker('/closing.js');
        w.postMessage('go');
    """)
    (await asyncio.sleep(0.2))
    (await page.close())
    assert page.closed


async def test_network_response_landing_after_close_is_dropped(browser):
    page = browser.new_profile().new_page()
    await page.goto(ORIGIN + "/")
    # load_resource_async posts the delivery onto the page task queue; closing
    # the page right after evaluate() returns must discard that delivery.
    page.evaluate("fetch('/slow')")
    (await page.close())
    (await asyncio.sleep(0.5))
    assert page.closed


async def test_timer_pending_at_close_is_dropped(browser):
    page = browser.new_profile().new_page()
    await page.goto(ORIGIN + "/")
    page.evaluate("""
        window.__count = 0;
        setInterval(function () { window.__count += 1; }, 10);
        setTimeout(function () { window.__count += 1; }, 500);
    """)
    (await page.close())
    (await asyncio.sleep(0.2))
    assert page.closed


async def test_page_trees_release_native_handles_and_threads_after_close():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    owner = browser.new_profile()
    warm = owner.new_page()
    (await warm.close())
    baseline = threading.active_count()
    try:
        for _ in range(10):
            page = owner.new_page()
            (await page.set_content('<iframe srcdoc="<body>child</body>"></iframe>', wait_until="commit"))
            (await page.run_until_idle(1))
            page.evaluate("globalThis.worker = new Worker(URL.createObjectURL(new Blob(['self.onmessage=()=>{}'])));")
            old_context = page.v8_context
            handle = page.evaluate_handle('({node:document.body, fn:()=>42})')
            (await page.set_content('<body>replacement</body>', wait_until="commit"))
            isolate = page.isolate
            (await page.close())
            assert old_context.live_handles == 0
            assert isolate.live_v8_handles == 0
            with pytest.raises(RuntimeError):
                handle.to_python()
            assert not owner.pages
        deadline = time.monotonic() + 5
        while threading.active_count() > baseline and time.monotonic() < deadline:
            (await asyncio.sleep(0.02))
        assert threading.active_count() <= baseline
    finally:
        (await browser.close())
