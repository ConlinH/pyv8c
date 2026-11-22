"""Actual browser trace events, rather than V8Context constructor arguments."""

import pytest
from pyv8c import pyv8
from loguru import logger
from pyv8c.pyv8.trace import format_trace

from pyv8c.async_api import Browser


async def test_browser_events_lazy_interfaces_and_page_override():
    events = []
    browser = (await Browser(trace=True, trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        events.clear()
        assert page.evaluate("""(() => {
            const element = document.createElement('div');
            element.id = 'traced';
            const lazy = new AbortController();
            lazy.extra = 42;
            lazy.abort();
            return [element.id, lazy.extra, new Event('ready').type];
        })()""") == ['traced', 42, 'ready']
        assert any(event[0] == 'method' and event[2] == 'createElement' for event in events)
        assert any(event[0] == 'set' and event[2] == 'id' and event[4] == 'traced' for event in events)
        assert any(event[0] == 'get' and event[2] == 'id' and event[4] == 'traced' for event in events)
        assert any(event[0] == 'construct' and event[2] == 'Event' for event in events)
        assert any(event[0] == 'method' and event[2] == 'abort' for event in events)
        # ``extra`` is a plain JS expando on a host object: the engine resolves it
        # outside the binding boundary and the trace reports the stored value.
        assert any(event[0] == 'set' and event[2] == 'extra' and event[4] == 42 for event in events)
        for name in page.binding_session.materialized_interfaces:
            cls = page.binding_session.class_for(name)
            assert not any(key.endswith('_hook__') for base in cls.__mro__ for key in base.__dict__)
        quiet = browser.new_profile().new_page(trace=False)
        events.clear()
        quiet.evaluate("document.createElement('div').id")
        assert events == []
    finally:
        (await browser.close())


async def test_default_browser_sink_really_logs():
    messages = []
    sink = logger.add(lambda message: messages.append(message.record['message']))
    browser = (await Browser(trace=True, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        messages.clear()
        page.evaluate("document.createElement('div').id")
        assert any('method: HTMLDocument.createElement' in message for message in messages)
        assert any('getter: HTMLDivElement.id' in message for message in messages)
    finally:
        (await browser.close())
        logger.remove(sink)


def _messages(events):
    return [message for message in (format_trace(*event) for event in events) if message is not None]


async def test_an_interface_level_member_is_reported_with_its_interface_name():
    """A static member is read off the interface itself, so that is what the trace names."""
    events = []
    browser = (await Browser(trace=True, traceable=True,
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        page.evaluate("Notification.permission")
        messages = page.executor.call(_messages, events)
        assert "[TOP] getter: Notification.permission -> 'default'" in messages
        assert not any(message.startswith("[TOP] getter: NoneType.") for message in messages)
    finally:
        (await browser.close())


async def test_an_object_read_stays_on_one_bounded_line():
    """A converted object carried whole graphs, and its embedded newlines broke the
    one-event-per-line shape of the trace; it is a preview now, and a conversion that
    raises still reports the event instead of losing it."""
    events = []
    browser = (await Browser(trace=True, traceable=True, stub_logging="off",
                      trace_callback=lambda *event: events.append(event)).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body><p>x</p></body>", url="https://example.test/", wait_until="commit"))
        page.evaluate("""(() => {
            const cyclic = { name: 'cyclic' };
            cyclic.self = cyclic;
            window.cyclic = cyclic;
            window.big = new Array(400).fill('line\\nline\\nline');
            return [window.cyclic, window.big].length;
        })()""")
        messages = page.executor.call(_messages, events)
    finally:
        (await browser.close())

    assert any("Window.cyclic" in message for message in messages)
    assert any("Window.big" in message for message in messages)
    assert not any("\n" in message for message in messages)
    assert any(message.endswith("...") for message in messages)


async def test_declined_reads_are_reported_with_the_value_they_resolve_to():
    """Native globals resolve outside the binding and still carry their value."""
    events = []
    browser = (await Browser(trace=True, traceable=True, trace_callback=lambda *event: events.append(event),
                      stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        events.clear()
        assert page.evaluate("eval") is not None
        assert page.executor.call(_messages, events) == ["[TOP] getter: Window.eval -> function eval() { [native code] }"]
        events.clear()
        # A name no property provides has no value to show, so it stays silent.
        assert page.evaluate("window.notThere") is pyv8.Undefined
        assert page.executor.call(_messages, events) == []
    finally:
        (await browser.close())


async def test_probes_are_reported_separately_from_reads():
    """Existence and descriptor probes get their own events, reads stay single."""
    events = []
    browser = (await Browser(trace=True, traceable=True, trace_callback=lambda *event: events.append(event),
                      stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        events.clear()
        assert page.evaluate("'eval' in window") is True
        assert page.evaluate("'notThere' in window") is False
        assert page.evaluate("Object.getOwnPropertyDescriptor(window, 'eval') === undefined") is False
        probes = [message for message in page.executor.call(_messages, events) if " has: " in message or " descriptor: " in message]
        assert probes == [
            "[TOP] has: Window.eval -> True",
            "[TOP] has: Window.notThere -> False",
            "[TOP] descriptor: Window.eval -> True",
        ]
        assert [event for event in events if event[0] == "get" and event[2] == "eval"] == []
        events.clear()
        assert page.evaluate("typeof window.eval") == 'function'
        assert [event for event in events if event[0] in ("has", "descriptor")] == []
        assert [message for message in page.executor.call(_messages, events) if "eval" in message] == [
            "[TOP] getter: Window.eval -> function eval() { [native code] }"]
    finally:
        (await browser.close())


async def test_assembling_a_realm_is_not_reported_as_page_behaviour():
    """Installing the realm is the host's own work, so it reports nothing.

    Defining ``webkitURL`` and its ten siblings reads the interface each one points
    at, and that read is the host's: Chromium installs a legacy alias with no
    script-visible lookup of its target, so a realm that has not run a page script
    yet has an empty trace -- probes included.
    """
    events = []
    browser = (await Browser(trace=True, traceable=True,
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        messages = page.executor.call(_messages, events)
        assert [message for message in messages if " has: " in message] == []
        assert messages == []
        # The aliases are still installed, and still point at their interfaces.
        assert page.evaluate("""[typeof webkitURL, webkitURL === URL, typeof WebKitCSSMatrix,
            FeaturePolicy === PermissionsPolicy]""") == [
            "function", True, "function", True]
    finally:
        (await browser.close())


async def test_declared_getters_are_reported_once_with_their_value():
    events = []
    browser = (await Browser(trace=True, traceable=True, trace_callback=lambda *event: events.append(event),
                      stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        events.clear()
        page.evaluate("navigator.userAgent")
        messages = page.executor.call(_messages, events)
        assert [message.split(" -> ")[0] for message in messages] == [
            "[TOP] getter: Window.navigator",
            "[TOP] getter: Navigator.userAgent",
        ]
        assert "undefined" not in messages[-1]
    finally:
        (await browser.close())


async def test_host_error_conversion_does_not_emit_internal_trace_events():
    events = []
    browser = (await Browser(trace=True, traceable=True,
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        events.clear()
        assert page.evaluate("""(() => {
          try { window.postMessage(navigator.plugins, '*'); }
          catch (error) {
            return ['stack' in error, error.name, error.code,
                    error.stack.startsWith('DataCloneError:')];
          }
        })()""") == [True, 'DataCloneError', 25, True]
        assert not any(event[0] == 'construct' and event[2] == 'DOMException'
                       for event in events)
        assert not any(event[0] == 'set' and event[2] == 'code'
                       for event in events)
        assert not any(event[0] == 'descriptor' and event[2] == 'stack'
                       for event in events)
        assert [(event[0], event[2]) for event in events if event[2] == 'stack'] == [
            ('has', 'stack')]
        assert any(event[0] == 'method' and event[2] == 'postMessage'
                   for event in events)
        events.clear()
        assert page.evaluate("new DOMException('page', 'DataCloneError').name") == 'DataCloneError'
        assert any(event[0] == 'construct' and event[2] == 'DOMException'
                   for event in events)
    finally:
        (await browser.close())


async def test_a_frames_realm_is_labelled_apart_from_the_pages_own():
    """A frame's realm is not the page's: its events say FRAME, not TOP."""
    events = []
    browser = (await Browser(trace=True, traceable=True, trace_callback=lambda *event: events.append(event),
                      stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        events.clear()
        (await page.set_content('<body><iframe srcdoc="&lt;script&gt;navigator.userAgent&lt;/script&gt;">'
                         "</iframe></body>", url="https://example.test/", wait_until="commit"))
        # srcdoc commits after insertion, as in Chrome; the assertion is about
        # the realm label, not whether the parser completed the child already.
        (await page.run_until_idle(timeout=1))
        messages = page.executor.call(_messages, events)
        assert any(message.startswith("[FRAME] ") and "userAgent" in message for message in messages)
        assert not any(message.startswith("[TOP] ") and "userAgent" in message for message in messages)
    finally:
        (await browser.close())


async def test_worker_inherits_context_sink_without_faker_switch():
    events = []
    browser = (await Browser(trace=True, trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""(() => {
            const source = new Blob(["postMessage(new Event('worker-trace').type)"], {type:'text/javascript'});
            const worker = new Worker(URL.createObjectURL(source));
            worker.onmessage = event => window.workerResult = event.data;
        })()""")
        (await page.wait_for("typeof workerResult !== 'undefined'", timeout=10))
        assert page.evaluate("workerResult") == 'worker-trace'
        assert any(event[7] == 'Worker' and event[0] == 'construct' and event[2] == 'Event' for event in events)
    finally:
        (await browser.close())


async def test_trace_keeps_cross_origin_access_denied():
    browser = (await Browser(trace=True, trace_callback=lambda *event: None, stub_logging="off",
                      resource_loader=lambda url, request: {'body': '<body>child</body>', 'url': url}, cache=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe src="http://b.test/child"></iframe>', url='http://a.test/main', wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        assert page.evaluate("try { window[0].document; } catch(error) { error.name; }") == 'SecurityError'
    finally:
        (await browser.close())


@pytest.mark.parametrize("initial", [False, True])
async def test_page_trace_can_pause_and_resume(initial):
    events = []
    browser = (await Browser(trace=initial, traceable=True,
                      trace_callback=lambda *event: events.append(event), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        quiet = browser.new_profile().new_page(trace=False)
        page.evaluate("window.element = document.createElement('div')")
        original_context = page.v8_context
        for enabled in (True, False, True, False):
            page.trace = enabled
            events.clear()
            assert page.evaluate("element.id='value';element.id") == 'value'
            assert bool(events) is enabled
            assert page.trace is enabled
            assert page.v8_context.trace is enabled
            assert page.services.trace is enabled
            assert page.v8_context is original_context
            events.clear()
            quiet.evaluate("document.createElement('div').id")
            assert events == []
        with pytest.raises(TypeError, match='bool'):
            page.trace = 1
        (await page.close())
        with pytest.raises(RuntimeError):
            page.trace = True
    finally:
        (await browser.close())


async def test_page_without_instrumentation_rejects_activation():
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        with pytest.raises(RuntimeError, match='traceable=True'):
            page.trace = True
        assert page.trace is False
        assert page.services.trace is False
    finally:
        (await browser.close())


async def test_invalid_browser_sink():
    with pytest.raises(TypeError, match='trace_callback'):
        (await Browser(trace_callback=42).__aenter__())
