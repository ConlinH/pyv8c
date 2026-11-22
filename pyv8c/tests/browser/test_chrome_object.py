"""``window.chrome`` mirrors the object Chromium installs on every Window."""

import asyncio

import time

from pyv8c import pyv8
from pyv8c.pyv8.trace import format_trace

from pyv8c.async_api import Browser


async def _page(browser, url="https://example.test/"):
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url=url, wait_until="commit"))
    return page


def _messages(events):
    return [message for message in (format_trace(*event) for event in events) if message is not None]


async def test_chrome_object_is_a_plain_global_object():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""(() => {
            const chrome = window.chrome;
            return [typeof chrome, 'chrome' in window, chrome === window.chrome,
                Object.getPrototypeOf(chrome) === Object.prototype,
                chrome.constructor === Object,
                Object.prototype.toString.call(chrome),
                Object.keys(chrome), Object.keys(window).includes('chrome'),
                typeof chrome.loadTimes, typeof chrome.csi, typeof chrome.app,
                chrome.runtime === undefined,
                window.__pyv8cChromeHost__ === undefined];
        })()""") == [
            "object", True, True, True, True, "[object Object]",
            ["loadTimes", "csi", "app"], True,
            "function", "function", "object", True, True,
        ]
    finally:
        (await browser.close())


async def test_chrome_global_property_keeps_chromium_attributes():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""(() => {
            const descriptor = Object.getOwnPropertyDescriptor(window, 'chrome');
            return [descriptor.writable, descriptor.enumerable, descriptor.configurable,
                'get' in descriptor, 'set' in descriptor];
        })()""") == [True, True, True, False, False]
        # Assignment replaces the value and deletion removes the property.
        assert page.evaluate("""(() => {
            window.chrome = 1;
            return [typeof window.chrome, delete window.chrome, typeof window.chrome];
        })()""") == ["number", True, "undefined"]
    finally:
        (await browser.close())


async def test_chrome_members_are_native_functions():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""[
            String(chrome.csi), String(chrome.loadTimes), String(chrome.app.getDetails),
            chrome.csi.name, chrome.loadTimes.name,
            /native code/.test(String(chrome.csi)),
        ]""") == [
            "function () { [native code] }",
            "function () { [native code] }",
            "function getDetails() { [native code] }",
            "", "", True,
        ]
    finally:
        (await browser.close())


async def test_chrome_timing_functions_report_chromium_shapes():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""(() => {
            const csi = chrome.csi();
            return [Object.keys(csi), csi.tran, typeof csi.startE,
                csi.pageT >= 0, csi.pageT < csi.startE,
                chrome.csi() === csi, chrome.csi().startE === csi.startE,
                chrome.csi().pageT >= csi.pageT && chrome.csi().pageT - csi.pageT < 5000,
                Object.keys(chrome.loadTimes()),
                [chrome.loadTimes().navigationType, chrome.loadTimes().wasFetchedViaSpdy,
                 chrome.loadTimes().npnNegotiatedProtocol, chrome.loadTimes().connectionInfo,
                 chrome.loadTimes().wasAlternateProtocolAvailable,
                 chrome.loadTimes().firstPaintAfterLoadTime]];
        })()""") == [
            # pageT is a duration, as Chromium's ``now - start`` is.
            ["startE", "onloadT", "pageT", "tran"], 15, "number", True, True, False, True, True,
            ["requestTime", "startLoadTime", "commitLoadTime", "finishDocumentLoadTime",
             "finishLoadTime", "firstPaintTime", "firstPaintAfterLoadTime", "navigationType",
             "wasFetchedViaSpdy", "wasNpnNegotiated", "npnNegotiatedProtocol",
             "wasAlternateProtocolAvailable", "connectionInfo"],
            ["Other", True, "h2", "h2", False, 0],
        ]
        first = page.evaluate("chrome.csi().startE")
        # A navigation restarts the clock: with a stale anchor the elapsed time
        # would still contain the sleep below, which the windowed value forbids.
        (await asyncio.sleep(0.2))
        (await page.set_content("<body>second</body>", url="https://example.test/next", wait_until="commit"))
        assert page.evaluate(f"chrome.csi().startE >= {first!r}") is True
        assert page.evaluate("chrome.csi().pageT") < 60000
    finally:
        (await browser.close())


async def test_chrome_app_surface_and_missing_runtime():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""[
            chrome.app.isInstalled, chrome.app.getIsInstalled(), chrome.app.installState(),
            chrome.app.runningState(), chrome.app.getDetails() === null,
            chrome.app.InstallState.DISABLED, chrome.app.InstallState.INSTALLED,
            chrome.app.InstallState.NOT_INSTALLED, chrome.app.RunningState.CANNOT_RUN,
            chrome.app.RunningState.READY_TO_RUN, chrome.app.RunningState.RUNNING,
            Object.keys(chrome.app)]""") == [
            False, False, "not_installed", "cannot_run", True,
            "disabled", "installed", "not_installed",
            "cannot_run", "ready_to_run", "running",
            ["isInstalled", "getDetails", "getIsInstalled", "installState", "runningState",
             "InstallState", "RunningState"],
        ]
        assert page.evaluate("typeof chrome.runtime") == "undefined"
    finally:
        (await browser.close())


async def test_app_members_refuse_arguments_their_schema_does_not_declare():
    """The extensions layer validates an invocation before the implementation runs, so an
    argument the schema does not declare is a ``TypeError`` the page catches rather than a
    host failure; the message is the one those bindings build out of the schema.  Chrome 155
    answers the same text for the same calls.
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""(() => {
            const refused = (call) => {
                try { call(); return 'accepted'; }
                catch (error) {
                    return [error.name, error instanceof TypeError, error.message];
                }
            };
            return [
                refused(() => chrome.app.getDetails(1)),
                refused(() => chrome.app.getDetails(undefined)),
                refused(() => chrome.app.getDetails(1, 2)),
                refused(() => chrome.app.getIsInstalled(1)),
                refused(() => chrome.app.runningState(null)),
                refused(() => chrome.app.installState(1, 2)),
                [chrome.app.getDetails.length, chrome.app.installState.length],
            ];
        })()""") == [
            ["TypeError", True, "Error in invocation of app.getDetails(): "],
            ["TypeError", True, "Error in invocation of app.getDetails(): "],
            ["TypeError", True, "Error in invocation of app.getDetails(): "],
            ["TypeError", True, "Error in invocation of app.getIsInstalled(): "],
            ["TypeError", True, "Error in invocation of app.runningState(): "],
            ["TypeError", True,
             "Error in invocation of app.installState(optional function callback): "],
            # An optional parameter leaves the count Chrome reports at zero.
            [0, 0],
        ]
    finally:
        (await browser.close())


async def test_the_arity_check_does_not_narrow_what_the_schema_accepts():
    """Only the argument count is validated: ``installState``'s optional callback may be
    anything (Chrome accepts ``null`` and a number there) and the other members take none.
    """
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""(() => {
            const accepted = (call) => {
                try { return ['ok', String(call())]; }
                catch (error) { return ['throw', error.name]; }
            };
            return [
                accepted(() => chrome.app.getDetails()),
                accepted(() => chrome.app.getIsInstalled()),
                accepted(() => chrome.app.runningState()),
                accepted(() => chrome.app.installState()),
                accepted(() => chrome.app.installState(null)),
                accepted(() => chrome.app.installState(1)),
                accepted(() => chrome.app.installState(() => {})),
            ];
        })()""") == [
            ["ok", "null"], ["ok", "false"], ["ok", "cannot_run"],
            ["ok", "not_installed"], ["ok", "not_installed"], ["ok", "not_installed"],
            ["ok", "not_installed"],
        ]
    finally:
        (await browser.close())


async def test_timing_functions_ignore_the_arguments_they_are_handed():
    """``csi`` and ``loadTimes`` never went through the extensions layer, so there is no
    schema for a call to be measured against and Chrome reads past what it is given."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""(() => {
            const load = chrome.loadTimes(1, 2);
            const csi = chrome.csi(1);
            return [Object.keys(load).length, load.navigationType, csi.tran,
                    typeof csi.startE, csi.pageT >= 0];
        })()""") == [13, "Other", 15, "number", True]
    finally:
        (await browser.close())


async def test_chrome_is_ready_for_inline_head_scripts():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(
            "<head><script>window.__chromeType = typeof window.chrome;</script></head>",
            url="https://example.test/",
         wait_until="commit"))
        assert page.evaluate("__chromeType") == "object"
    finally:
        (await browser.close())


async def test_installing_chrome_is_invisible_to_trace():
    """Host installation is not page behaviour, so it reports no event."""
    events = []
    browser = (await Browser(trace=True, traceable=True,
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        # Building the realm probes interfaces, none of it about chrome.
        assert [event for event in events if event[2:3] == ("chrome",)] == []
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert [event for event in events if event[2:3] == ("chrome",)] == []
        # Tracing is still attached once the page runs its own script.
        page.evaluate("chrome.csi().tran")
        # Native function calls carry their JS name; csi is anonymous in Chromium.
        assert [event[2] for event in events if event[2] in ("chrome", "")] == ["chrome", ""]
    finally:
        (await browser.close())


async def test_chrome_method_calls_are_reported_with_their_results():
    """The members are Python-backed, so a call is traced like any host method.  An object
    result is reported as a bounded preview of its contents."""
    events = []
    browser = (await Browser(trace=True, traceable=True,
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        events.clear()
        assert page.evaluate("chrome.app.getDetails()") is pyv8.Null
        messages = _messages(events)
        assert messages[0].startswith("[TOP] getter: Window.chrome -> ")
        assert messages[1] == "[TOP] method: ChromeBindings.getDetails() -> null"
        events.clear()
        page.evaluate("chrome.loadTimes().navigationType")
        assert _messages(events)[-1].startswith(
            "[TOP] method: ChromeBindings.() -> ")
    finally:
        (await browser.close())


async def test_worker_globals_have_no_chrome_object():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        page.evaluate("""(() => {
            const source = new Blob(["postMessage(typeof chrome)"], {type: 'text/javascript'});
            const worker = new Worker(URL.createObjectURL(source));
            worker.onmessage = event => window.__workerChrome = event.data;
        })()""")
        (await page.wait_for("typeof window.__workerChrome !== 'undefined'", timeout=10))
        assert page.evaluate("__workerChrome") == "undefined"
    finally:
        (await browser.close())


async def test_timing_fields_are_accessors_with_an_empty_setter():
    """A write is accepted and ignored, as ``SetNativeDataProperty(..., EmptySetter, value)``."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""(() => {
            const fields = chrome.loadTimes();
            const field = Object.getOwnPropertyDescriptor(fields, 'requestTime');
            const before = fields.requestTime;
            let write = 'accepted';
            try { fields.requestTime = 12345; } catch (error) { write = 'threw ' + error.name; }
            const csi = Object.getOwnPropertyDescriptor(chrome.csi(), 'pageT');
            return [typeof field.get, typeof field.set, field.enumerable,
                    field.configurable, write, fields.requestTime === before,
                    typeof csi.get, typeof csi.set];
        })()""") == ["function", "function", True, True, "accepted", True, "function", "function"]
    finally:
        (await browser.close())


async def test_the_timings_are_the_ones_performance_reports():
    """Chromium reads all three from the browser's performance metrics, not by measuring again."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        (await page.run_until_idle(timeout=2))
        assert page.evaluate("""(() => {
            const timing = performance.timing;
            const load = chrome.loadTimes();
            const csi = chrome.csi();
            return [load.requestTime === timing.navigationStart,
                    load.finishDocumentLoadTime === timing.domContentLoadedEventEnd,
                    load.finishLoadTime === timing.loadEventEnd,
                    csi.onloadT === timing.domContentLoadedEventEnd,
                    csi.startE === performance.timeOrigin,
                    timing.domContentLoadedEventEnd > 0, timing.loadEventEnd > 0];
        })()""") == [True, True, True, True, True, True, True]
    finally:
        (await browser.close())


async def test_chrome_app_is_installed_lazily():
    """A lazy accessor, as ``SetLazyDataProperty`` leaves it: the first read replaces it."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("""(() => {
            const before = Object.getOwnPropertyDescriptor(chrome, 'app');
            const app = chrome.app;
            const after = Object.getOwnPropertyDescriptor(chrome, 'app');
            return [typeof before.get, 'value' in before, 'value' in after,
                    after.value === app, typeof app.getDetails];
        })()""") == ["function", False, True, True, "function"]
    finally:
        (await browser.close())


async def test_an_existing_chrome_object_is_reused():
    """The object is created once, so what a realm already carries survives a second install
    (run on the page's owner thread, where such a layer sits)."""
    from pyv8c.web.bom.chrome import install_window_chrome

    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (((await _page(browser))))
        page.evaluate("window.chrome.__marker = 7")
        page.realm.executor.call(
            lambda: install_window_chrome(page.v8_context, page.services))
        assert page.evaluate(
            "[window.chrome.__marker, typeof chrome.csi, chrome.app.isInstalled]"
        ) == [7, "function", False]
    finally:
        (await browser.close())
