"""Window identity and same-origin policy regression tests (no live network)."""

import pytest

from pyv8c.async_api import Browser
from pyv8c.web.bom.window import Window


@pytest.fixture
async def windows():
    def loader(url, request):
        body = '<iframe src="http://a.test/nested"></iframe>' if url.endswith('/outer') else '<body>child</body>'
        return {"body": body, "url": url}
    browser = (await Browser(resource_loader=loader, cache=False).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content('<iframe src="http://b.test/child"></iframe>', url='http://a.test/main', wait_until="commit"))
    (await page.wait_for_load_state('load'))
    child = next(iter(page._owned.frames))
    try:
        yield page, child
    finally:
        (await browser.close())


def caught(page, expression):
    return page.evaluate('''(() => {
        const w = document.querySelector('iframe').contentWindow;
        try { ''' + expression + '''; return ['allowed']; }
        catch (e) { return [e.name, e.code, e instanceof DOMException]; }
    })()''')


@pytest.mark.parametrize('expression', [
    'w.document', 'w.navigator', 'w.history', 'w.screen', 'w.localStorage',
    'w.sessionStorage', 'w.custom', 'w.fetch', 'w.setTimeout',
    'w.location.href', 'w.location.origin', 'w.location.host',
    'w.custom = 1', 'w.document = {}', 'w.top = {}',
    #: V8 decides these itself, reporting through the isolate rather than the handlers
    #: (answered by ``Window.__v8_cross_origin_denied__``).
    'delete w.custom', 'Object.defineProperty(w, "custom", {value: 1})',
    'Object.freeze(w)', 'Object.seal(w)', 'Object.setPrototypeOf(w, null)',
    'Object.getOwnPropertyDescriptor(w, "document")',
])
async def test_cross_origin_rejects_sensitive_access(windows, expression):
    page, _ = windows
    assert caught(page, expression) == ['SecurityError', 18, True]


async def test_cross_origin_safe_members_and_identity(windows):
    page, child = windows
    assert page.evaluate('''(() => { const w = window[0]; return [
        w === document.querySelector('iframe').contentWindow,
        w.window === w, w.self === w, w.frames === w,
        w.top === window, w.parent === window, w.opener === null,
        w.length, w.closed, typeof w.location,
        typeof w.close, typeof w.focus, typeof w.blur, typeof w.postMessage,
        window.top === window, window.parent === window,
        window.self === window, window.window === window,
        document.querySelector('iframe').contentDocument === null
    ]; })()''') == [True] * 7 + [0, False, 'object'] + ['function'] * 4 + [True] * 5
    assert child.evaluate('[top !== window, top === parent, self === window, window.window === window]') == [True] * 4
    assert child.evaluate('try { parent.document; } catch(e) { [e.name, e.code]; }') == ['SecurityError', 18]


async def test_same_origin_reads_real_child_and_custom_properties(windows):
    page, child = windows
    (await child.set_content('<body id="child">hello</body>', url='http://a.test:80/child?x=1#part', wait_until="commit"))
    child.evaluate('window.answer = 42')
    assert page.evaluate('''(() => { const w = window[0]; return [
        w.document.body.id, w.document.URL, w.location.href, w.answer,
        w.document.defaultView === w,
        document.querySelector('iframe').contentDocument === w.document
    ]; })()''') == ['child', 'http://a.test:80/child?x=1#part', 'http://a.test:80/child?x=1#part', 42, True, True]
    page.evaluate('window[0].answer = 73')
    assert child.evaluate('window.answer') == 73
    assert page.evaluate('typeof window[0].doesNotExist') == 'undefined'
    assert child.evaluate('parent.document.URL') == 'http://a.test/main'


async def test_nested_frame_top_and_numeric_index_identity(windows):
    page, child = windows
    (await child.set_content('<iframe src="http://a.test/nested"></iframe>', url='http://a.test/outer', wait_until="commit"))
    (await child.wait_for_load_state('load', timeout=1))
    nested = next(iter(child._owned.frames))
    assert page.evaluate('''[window.length, window[0].length,
        window[0][0].top === window,
        window[0].document.querySelector('iframe').contentWindow === window[0][0],
        window[0][0].parent === window[0]]''') == [1, 1, True, True, True]
    assert nested.evaluate('[top !== window, top !== parent, parent.parent === top]') == [True] * 3
    assert nested.top_page is page


async def test_proxy_rechecks_origin_after_navigation(windows):
    page, child = windows
    (await child.set_content('<body>same</body>', url='http://a.test/same', wait_until="commit"))
    page.evaluate('window.savedFrame = window[0]')
    assert page.evaluate('savedFrame.document.URL') == 'http://a.test/same'
    (await child.set_content('<body>other</body>', url='http://a.test:8080/other', wait_until="commit"))
    assert page.evaluate('savedFrame === window[0]') is True
    assert page.evaluate('try { savedFrame.document; } catch(e) { [e.name,e.code]; }') == ['SecurityError', 18]


@pytest.mark.parametrize('assignment', ['w.location = "http://b.test/next"', 'w.location.href = "http://b.test/next"'])
async def test_cross_origin_location_write_queues_navigation(windows, assignment):
    page, child = windows
    assert caught(page, assignment) == ['allowed']
    (await child.wait_for("location.href === 'http://b.test/next' && document.readyState === 'complete'", timeout=1))
    assert child.url == 'http://b.test/next'
    assert caught(page, 'w.location.href') == ['SecurityError', 18, True]


@pytest.mark.parametrize('call', [
    'w.location.assign("http://b.test/next")',
    'w.location.reload()',
])
async def test_cross_origin_location_operations_are_refused(windows, call):
    """A cross-origin operation is refused when invoked, not when named.

    The baseline checks ``assign`` and ``reload`` and leaves only ``replace`` free.
    """
    page, child = windows
    assert page.evaluate('''[typeof window[0].location.assign,
        typeof window[0].location.reload,
        typeof window[0].location.replace]''') == ['function'] * 3
    assert caught(page, call) == ['SecurityError', 18, True]
    (await page.run_until_idle(timeout=1))
    assert child.url == 'http://b.test/child'
    assert caught(page, 'w.location.replace("http://a.test/next")') == ['allowed']


async def test_post_message_delivery_origin_source_and_target_filter(windows):
    page, child = windows
    child.evaluate('window.messages = []; addEventListener("message", e => messages.push([e.data.value,e.origin,e.source===parent]))')
    page.evaluate('window[0].postMessage({value: 1}, "http://b.test"); window[0].postMessage({value: 2}, "http://wrong.test")')
    (await child.run_until_idle(timeout=1))
    assert child.evaluate('messages') == [[1, 'http://a.test', True]]


@pytest.mark.parametrize('url,allowed', [
    ('about:blank', True), ('blob:http://a.test/token', True),
    ('http://A.TEST:80/other', True), ('http://a.test:8080/other', False),
    ('https://a.test/other', False), ('data:text/html,hello', False),
])
async def test_origin_variants(windows, url, allowed):
    page, child = windows
    (await child.set_content('<body>hello</body>', url=url, wait_until="commit"))
    expected = ['allowed'] if allowed else ['SecurityError', 18, True]
    assert caught(page, 'w.document') == expected


async def test_native_window_access_handler_is_automatically_installed(windows):
    page, child = windows
    page.executor.call(lambda: page.v8_context.expose(nativeChild=child.v8_context.js_obj(child.window)))
    assert page.evaluate('try { nativeChild.document; } catch(e) { [e.name,e.code,e instanceof DOMException]; }') == ['SecurityError', 18, True]
    assert page.evaluate('[nativeChild.closed,nativeChild.top===window,nativeChild.self===window[0]]') == [False, True, True]


async def test_context_entries_are_released_on_close():
    before = set(Window._context_origins)
    browser = (await Browser().__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert id(page.v8_context) in Window._context_origins
    finally:
        (await browser.close())
    assert set(Window._context_origins) == before


async def test_iframe_src_navigation_retains_proxy(windows):
    page, child = windows
    page.evaluate('window.savedFrame = window[0]; document.querySelector("iframe").src = "http://a.test/new"')
    (await child.wait_for("document.URL === 'http://a.test/new' && document.readyState === 'complete'"))
    assert page.evaluate('[savedFrame === window[0], savedFrame.document.URL]') == [True, 'http://a.test/new']
    assert len(page._owned.frames) == 1
    assert child.url == 'http://a.test/new'


async def test_initial_about_blank_iframe_does_not_need_network():
    browser = (await Browser(cache=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe></iframe>', url='http://a.test/main', wait_until="commit"))
        (await page.run_until_idle(timeout=1))
        assert page.evaluate('[window.length, window[0].document.URL, window[0].parent === window]') == [1, 'about:blank', True]
    finally:
        (await browser.close())


async def test_saved_location_is_restricted_after_origin_changes(windows):
    """A saved location belongs to the document it was made with, and keeps answering.

    A frame that navigates leaves that document behind; the reference a page saved goes
    on describing it -- the realm it came from is kept with the page rather than closed
    by the navigation, so the read does not fail as a dead handle.  What a cross-origin
    caller is handed is a view of the frame's *current* location, which asks the origin
    on every read.

    This is Blink's own shape: a replaced realm is the "detached global" state, where
    the context and its objects stay alive for the references scripts hold
    (``bindings/core/v8/window_proxy.h:206-231``), and a Location is checked against the
    window it belongs to rather than the frame's current one, its readers never asking
    whether the frame still shows it (``binding_security.cc:92-97``, ``location.cc:94``,
    ``location.cc:326``).
    """
    page, child = windows
    #: Cross-origin to begin with: the caller is handed a view of the frame's location.
    assert caught(page, 'window.savedView = window[0].location') == ['allowed']
    assert caught(page, 'savedView.href') == ['SecurityError', 18, True]
    #: Reachable: this is the window's own Location, and the page may keep it.
    (await child.set_content('<body/>', url='http://a.test/child', wait_until="commit"))
    assert page.evaluate('''(() => { window.savedLocation = window[0].location;
        return [savedLocation.pathname, savedLocation === window[0].location]; })()''')\
        == ['/child', True]
    #: The frame goes on: the location still answers for the document it was made with,
    #: while a fresh read of the frame's location is a cross-origin one and is refused.
    (await child.set_content('<body/>', url='http://b.test/secret', wait_until="commit"))
    assert page.evaluate('savedLocation.href') == 'http://a.test/child'
    assert caught(page, 'window[0].location.pathname') == ['SecurityError', 18, True]


async def test_blob_origin_comes_from_creator_page(windows):
    page, child = windows
    creator = page.profile.new_page()
    (await creator.set_content('<body/>', url='data:text/html,creator', wait_until="commit"))
    blob_url = creator.evaluate('URL.createObjectURL(new Blob(["hello"]))')
    (await child.set_content('<body/>', url=blob_url, wait_until="commit"))
    assert child.origin == creator.origin
    assert child.origin != page.origin
    assert caught(page, 'w.document') == ['SecurityError', 18, True]


async def test_onmessage_receives_cross_origin_message(windows):
    page, child = windows
    child.evaluate('window.onmessage = e => window.received = e.data')
    page.evaluate('window[0].postMessage("received", "*")')
    (await child.run_until_idle(timeout=1))
    assert child.evaluate('received') == 'received'
