"""Loader callbacks: the owner thread by default, the browser's pool on request."""

import asyncio
import threading
import time

from pyv8c.async_api import Browser


def _body(text="ok", kind="text/plain"):
    return {"body": text, "headers": {"content-type": kind}}


async def test_a_blocked_loader_no_longer_freezes_the_realm():
    def slow(url, request):
        time.sleep(0.4)
        return {**_body(), "url": url}

    browser = (await Browser(resource_loader=slow, threaded_loaders=True).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.evaluate("fetch('/slow').then(r => r.text()).then(v => window.__slow = v)")
        started = time.perf_counter()
        assert page.evaluate("1 + 1") == 2
        assert time.perf_counter() - started < 0.2
        (await page.wait_for("typeof window.__slow !== 'undefined'", timeout=10))
        assert page.evaluate("__slow") == "ok"
    finally:
        (await browser.close())


async def test_the_default_loader_runs_on_the_owner_thread():
    seen = {}

    def loader(url, request):
        seen["owner"] = page.realm.executor.is_owner_thread()
        # Supported there: a loader may look at the page it is loading for.
        seen["dom"] = page.evaluate("1 + 1")
        return {**_body(), "url": url}

    browser = (await Browser(resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.evaluate("fetch('/x').then(r => r.text()).then(v => window.__x = v)")
        (await page.run_until_idle(timeout=2))
        assert page.evaluate("__x") == "ok"
        assert seen == {"owner": True, "dom": 2}
    finally:
        (await browser.close())


async def test_the_threaded_loader_runs_on_the_browser_pool():
    seen = {}

    def loader(url, request):
        seen["owner"] = page.realm.executor.is_owner_thread()
        seen["thread"] = threading.current_thread().name
        return {**_body(), "url": url}

    browser = (await Browser(resource_loader=loader, threaded_loaders=True).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.evaluate("fetch('/x').then(r => r.text()).then(v => window.__x = v)")
        (await page.run_until_idle(timeout=2))
        assert seen["owner"] is False
        assert seen["thread"].startswith("pyv8c-loader")
        assert page.evaluate("__x") == "ok"
    finally:
        (await browser.close())


async def test_the_loader_pool_runs_a_bounded_number_of_callbacks():
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}

    def loader(url, request):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.05)
        with lock:
            live["now"] -= 1
        return {**_body(), "url": url}

    browser = (await Browser(resource_loader=loader, threaded_loaders=True).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.evaluate("for (let i = 0; i < 8; i++) fetch('/x?i=' + i).then(r => r.text())")
        (await page.run_until_idle(timeout=5))
        assert live["peak"] == 4
    finally:
        (await browser.close())


async def test_runtime_waits_for_loader_without_blocking_application_loop():
    entered = threading.Event()
    release = threading.Event()

    def loader(url, request):
        entered.set()
        release.wait(5)
        return {**_body("late"), "url": url}

    browser = (await Browser(resource_loader=loader, threaded_loaders=True).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("fetch('/x').then(r => r.text())")
    assert (await asyncio.to_thread(entered.wait, 2)), "the loader never ran"
    started = time.perf_counter()
    closing = asyncio.create_task(((browser.close())))
    try:
        await asyncio.sleep(0.02)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"close blocked the owner for {elapsed:.2f}s"
        assert page.closed and not closing.done()
        release.set()
        await closing
        assert browser.closed
    finally:
        release.set()
        await browser.close()


async def test_a_failed_submit_tells_the_tail_why_it_failed():
    """The reason a load could not even be queued belongs to the tail that waits for it.

    The task that carries it is queued from inside an ``except`` block, and Python
    deletes the block's variable when the block ends; a closure that read it later used
    to find it gone and die with "cannot access free variable 'exc'" -- the load never
    finished, and the log filled with a warning that named everything but the truth.
    """
    class Closed:
        _threads = set()
        def submit(self, *_):
            raise RuntimeError("the loader pool is closed")

        def shutdown(self, **_):
            pass

    def loader(url, request):
        return _body()

    messages, token = [], __import__("loguru").logger.add(
        lambda message: messages.append(str(message).strip()), format="{message}")
    browser = (await Browser(resource_loader=loader, threaded_loaders=True).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.profile.browser._loader_pool = Closed()
        page.evaluate(
            "fetch('/x').then(r => r.text()).then(v => window.__x = v,"
            " e => window.__e = '' + e)")
        (await page.run_until_idle(timeout=2))
        assert page.evaluate("typeof window.__e") == "string"
        assert "Failed to fetch" in page.evaluate("window.__e")
        assert not any("could not finish" in line for line in messages), messages
    finally:
        __import__("loguru").logger.remove(token)
        (await browser.close())


async def test_a_load_in_flight_when_a_navigation_commits_does_not_hold_the_next_one():
    """A reload that commits while the previous document still has a load in flight must
    not inherit it.

    Chrome drops the old document's pending loads when the new document commits.  A
    cancelled load whose entry stayed behind kept counting as outstanding -- which is
    how one image still loading when a reload committed held the new document's ``load``
    for the whole navigation budget, thirty seconds a page that reloads itself (the
    anti-bot kind) paid every time.
    """
    gate = threading.Event()

    def loader(url, request):
        if url.endswith("/slow.png"):
            gate.wait(5)
            return {**_body("x", "image/png"), "url": url}
        return _body()

    browser = (await Browser(resource_loader=loader, threaded_loaders=True).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><img src="/slow.png"></body>', url="http://example.test/", wait_until="commit"))
        started = time.perf_counter()
        (await page.goto("http://example.test/next.html", wait_until="load", timeout=5))
        took = time.perf_counter() - started
        assert took < 5, f"the new document waited {took:.1f}s on the old one's image"
    finally:
        gate.set()
        (await browser.close())


async def test_a_parser_blocking_script_loads_with_the_threaded_loader():
    def loader(url, request):
        return {**_body("window.__loaded = true", "text/javascript"), "url": url}

    browser = (await Browser(resource_loader=loader, threaded_loaders=True).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body><script src='/a.js'></script></body>", url="http://example.test/", wait_until="commit"))
        (await page.run_until_idle(timeout=3))
        assert page.evaluate("__loaded") is True
    finally:
        (await browser.close())
