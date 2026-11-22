"""Filter binding traces by the currently executing script's source URL."""

import pytest

from pyv8c.async_api import Browser


def _created(events):
    return [event[3][0] for event in events
            if event[0] == "method" and event[2] == "createElement"]


async def test_external_script_filter_includes_async_callbacks_and_redirect_url():
    events = []

    def loader(url, _request):
        if url.endswith("/a.js?version=2"):
            return {"status": 302, "headers": {"location": "/actual-a.js?version=2"}}
        if url.endswith("/actual-a.js?version=2"):
            return {"body": """
                document.createElement('sync-a');
                Promise.resolve().then(() => document.createElement('promise-a'));
                setTimeout(() => { document.createElement('timer-a'); window.timerDone = true; }, 0);
                addEventListener('custom', () => document.createElement('event-a'));
            """}
        if url.endswith("/b.js"):
            return {"url": url, "body": """
                document.createElement('sync-b');
                dispatchEvent(new Event('custom'));
            """}
        raise AssertionError(url)

    browser = (await Browser(real_network=False, resource_loader=loader, trace=True,
                      trace_scripts="https://example.test/actual-a.js?*",
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<script src='/a.js?version=2'></script><script src='/b.js'></script>",
                         url="https://example.test/", wait_until="commit"))
        (await page.wait_for("window.timerDone === true", timeout=3))
        assert any(filename == "https://example.test/actual-a.js?version=2"
                   for _source, filename in page.scripts._compiled)
        created = _created(events)
        assert {"sync-a", "promise-a", "timer-a", "event-a"} <= set(created)
        assert "sync-b" not in created
        assert all(len(event) == 8 for event in events)
    finally:
        (await browser.close())


async def test_profile_page_overrides_and_trace_switch():
    events = []
    browser = (await Browser(real_network=False, trace=True, traceable=True,
                      trace_scripts="https://example.test/a.js",
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        profile = browser.new_profile(trace_scripts="https://example.test/b.js")
        page = profile.new_page(trace_scripts="https://example.test/c.js")
        for name in ("a", "b", "c"):
            page.evaluate(f"document.createElement('{name}')",
                          filename=f"https://example.test/{name}.js")
        assert _created(events) == ["c"]
        page.trace = False
        page.evaluate("document.createElement('off')", filename="https://example.test/c.js")
        page.trace = True
        page.evaluate("document.createElement('on')", filename="https://example.test/c.js")
        assert _created(events) == ["c", "on"]

        unfiltered = profile.new_page(trace_scripts=None)
        events.clear()
        unfiltered.evaluate("document.createElement('all')")
        assert _created(events) == ["all"]
    finally:
        (await browser.close())


async def test_frame_and_worker_inherit_filter_across_navigation():
    events = []

    def loader(url, _request):
        if url.endswith("/child"):
            return {"url": url, "body": "<script src='/target.js'></script>"}
        if url.endswith("/target.js"):
            return {"url": url, "body": "document.createElement('frame-target')"}
        if url.endswith("/worker.js"):
            return {"url": url, "body": "postMessage(new Event('worker-target').type)"}
        if url.endswith("/next"):
            return {"url": url, "body": "<script src='/target.js'></script>"}
        raise AssertionError(url)

    browser = (await Browser(real_network=False, resource_loader=loader, trace=True,
                      trace_scripts="https://example.test/*target.js",
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<iframe src='/child'></iframe>", url="https://example.test/", wait_until="commit"))
        (await page.run_until_idle(timeout=3))
        assert "frame-target" in _created(events)
        (await page.goto("https://example.test/next"))
        assert _created(events).count("frame-target") >= 2

        worker_page = browser.new_profile().new_page(trace_scripts="https://example.test/worker.js")
        (await worker_page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        worker_page.evaluate("""const worker = new Worker('/worker.js');
            worker.onmessage = event => window.workerValue = event.data;""")
        (await worker_page.wait_for("window.workerValue === 'worker-target'", timeout=5))
        assert any(event[7] == "Worker" and event[0] == "construct" and
                   event[2] == "Event" for event in events)
    finally:
        (await browser.close())


@pytest.mark.parametrize("patterns", [[], [""], [42], 42])
async def test_invalid_patterns_rejected(patterns):
    with pytest.raises((TypeError, ValueError), match="trace_scripts"):
        (await Browser(trace_scripts=patterns).__aenter__())


async def test_invalid_profile_and_page_patterns_rejected():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        with pytest.raises(ValueError, match="trace_scripts"):
            browser.new_profile(trace_scripts=[])
        profile = browser.new_profile()
        with pytest.raises(ValueError, match="trace_scripts"):
            profile.new_page(trace_scripts=[""])
        assert profile.pages == []
    finally:
        (await browser.close())
