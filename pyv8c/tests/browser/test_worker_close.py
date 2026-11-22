"""A worker's teardown is the host's problem, never the script's.

``close()`` and ``terminate()`` answer ``undefined`` in a browser however the teardown goes.
The host's guard, though, raises ``RealmBusyError`` when a realm will not answer it -- and both
calls are made from a script, so without a boundary at the call a page ends up catching the
host's own scheduler.  This holds that boundary down at both ends: the worker that closes
itself, and the page that terminates it, with the reason logged either way.

The teardown itself is *posted* to the worker's own thread rather than waited for there, so a
worker that is slow to answer costs the caller nothing; the last two tests hold that down from
inside the page's realm, and hold down that the posted teardown still runs.
"""

import asyncio
import json
import threading
import time

import pytest
from loguru import logger

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import RealmBusyError
from pyv8c.web.workers import runtime as worker_runtime

WEDGED = "realm executor did not complete '_cleanup_owner' within 5s (already running)"

CLOSES_ITSELF = ("onmessage = function () {"
                 "  try { self.close(); self.postMessage('closed'); }"
                 "  catch (error) { self.postMessage('threw:' + error.name); }"
                 "};")

TERMINATES_ITSELF = ("try { worker.terminate(); out.push('terminated'); }"
                     " catch (error) { out.push('threw:' + error.name); }")

#: A worker that stays alive until the page terminates it.
IDLE = "self.onmessage = function () {};"

PAGE_HTML = """<!doctype html><body><script>
var out = []; window.__out = out;
var worker = new Worker(URL.createObjectURL(new Blob([__SOURCE__], {type: 'text/javascript'})));
worker.onmessage = function (event) { out.push(String(event.data)); };
worker.onerror = function (event) { out.push('onerror:' + String(event.message || event)); };
worker.postMessage(1);
__AFTER__
</script></body>"""




async def _pump(page, seconds=0.6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            (await page.run_until_idle(timeout=0))
        except BaseException:  # noqa: BLE001
            pass
        (await asyncio.sleep(0.01))


def _html(source, after=""):
    return PAGE_HTML.replace("__SOURCE__", json.dumps(source).replace("</", "<\\/"))\
                    .replace("__AFTER__", after)


async def _served(html):
    """Serve *html* with a real worker runtime; the caller owns the browser."""
    messages = []
    token = logger.add(lambda message: messages.append(str(message).strip()), format="{message}")

    def loader(url, request):
        return {"body": html, "headers": {"content-type": "text/html"}}

    browser = (await Browser(stub_logging="off", real_network=False, resource_loader=loader).__aenter__())
    page = browser.new_profile().new_page()
    (await page.goto("http://worker.test/"))
    (await _pump(page))
    return browser, page, messages, token


async def _run(html, monkeypatch):
    """Serve *html* with a worker runtime whose teardown cannot be completed."""
    calls = []

    def wedged(self):
        calls.append(self.script_url)
        raise RealmBusyError(WEDGED)

    original_stop = worker_runtime.WorkerRuntime.stop
    monkeypatch.setattr(worker_runtime.WorkerRuntime, "stop", wedged)
    browser, page, messages, token = (await _served(html))
    try:
        return json.loads(page.evaluate("JSON.stringify(window.__out)")), calls, messages
    finally:
        logger.remove(token)
        monkeypatch.setattr(worker_runtime.WorkerRuntime, "stop", original_stop)
        await browser.close()


async def test_a_worker_that_closes_itself_is_not_thrown_the_hosts_failure(monkeypatch):
    seen, calls, messages = (await _run(_html(CLOSES_ITSELF), monkeypatch))
    assert calls, "the worker never asked the host to stop it"
    assert seen == ["closed"], seen
    assert any("WorkerGlobalScope.close" in line and "_cleanup_owner" in line for line in messages),\
        messages


async def test_a_page_that_terminates_a_worker_keeps_its_own_script_running(monkeypatch):
    seen, calls, messages = (await _run(_html(CLOSES_ITSELF, TERMINATES_ITSELF), monkeypatch))
    assert calls, "the page never asked the host to stop the worker"
    assert "terminated" in seen, seen
    assert not [entry for entry in seen if entry.startswith(("threw:", "onerror:"))], seen
    assert any("Worker.terminate" in line for line in messages), messages


async def test_a_terminate_does_not_freeze_the_page_while_the_worker_answers(monkeypatch):
    """A teardown the page asked for is the worker's to run, not the page's to wait on.

    ``stop()`` used to call into the worker's executor and wait there, so a worker that
    took a second to answer held the page's realm for that second -- its timers, port
    deliveries and pending callbacks included.  The duration below is measured in the
    page's own realm, so it must be the call's own cost and not the worker's delay.
    """
    answer = threading.Event()
    entered = threading.Event()
    original = worker_runtime.WorkerRuntime._cleanup_owner

    def slow_cleanup(self):
        entered.set()
        answer.wait(5)
        return original(self)

    monkeypatch.setattr(worker_runtime.WorkerRuntime, "_cleanup_owner", slow_cleanup)
    browser, page, _messages, token = (await _served(_html(IDLE)))
    try:
        took = page.evaluate("""(() => {
            const t0 = performance.now();
            try { worker.terminate(); } catch (error) { return "threw:" + error.name; }
            return Math.round(performance.now() - t0);
        })()""")
        assert isinstance(took, int), took
        assert took < 250, f"terminate() blocked the page for {took}ms"
        assert (await asyncio.to_thread(entered.wait, 5)), "the teardown never started"
    finally:
        answer.set()
        logger.remove(token)
        await browser.close()


async def test_the_teardown_a_terminate_asked_for_still_runs(monkeypatch):
    """Posting the teardown is not dropping it: it runs, and closes its own executor."""
    ran = threading.Event()
    runtimes = []
    original_cleanup = worker_runtime.WorkerRuntime._cleanup_owner
    original_stop = worker_runtime.WorkerRuntime.stop

    def recorded_cleanup(self):
        try:
            return original_cleanup(self)
        finally:
            ran.set()

    def recording_stop(self):
        runtimes.append(self)
        return original_stop(self)

    monkeypatch.setattr(worker_runtime.WorkerRuntime, "_cleanup_owner", recorded_cleanup)
    monkeypatch.setattr(worker_runtime.WorkerRuntime, "stop", recording_stop)
    browser, page, _messages, token = (await _served(_html(IDLE)))
    try:
        page.evaluate("worker.terminate()")
        assert (await asyncio.to_thread(ran.wait, 5)), "the posted teardown never ran"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if runtimes and runtimes[0].executor.closed:
                break
            (await asyncio.sleep(0.01))
        assert runtimes and runtimes[0].executor.closed, "the worker's executor is still running"
    finally:
        logger.remove(token)
        await browser.close()
