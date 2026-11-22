"""The object a page gets for a reachable frame: the frame's own window, in its own realm.

A browser hands out one object per browsing context -- the frame's global proxy -- so
the parent sees exactly the shape the child's own scripts see, calls made on it run in
the child, and the object outlives the child's navigations (Chromium's
``LocalWindowProxy``: the proxy is filled in once and passed to later contexts).
"""
import pytest

from pyv8c.async_api import Browser


@pytest.fixture
async def frames():
    def loader(url, request):
        return {"body": '<body id="child">child</body>', "url": url}

    browser = (await Browser(resource_loader=loader, cache=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content('<iframe src="http://a.test/child"></iframe>', url='http://a.test/main', wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    try:
        yield page
    finally:
        (await browser.close())


async def test_a_reachable_frame_window_is_the_frame_itself(frames):
    assert frames.evaluate('''(() => {
        const frame = document.querySelector('iframe');
        const w = frame.contentWindow;
        const descriptor = Object.getOwnPropertyDescriptor(w, 'document');
        return [
            w === window[0],
            w === frame.contentWindow,
            w.top === window,
            w.parent === window,
            w.document.defaultView === w,
            Object.keys(w).length > 100,
            Object.keys(w).length === Object.keys(frame.contentWindow).length,
            Object.getOwnPropertyNames(w).length > 1000,
            Object.getPrototypeOf(w).constructor.name,
            Object.prototype.toString.call(Object.getPrototypeOf(w)),
            Object.prototype.toString.call(w),
            descriptor.enumerable === true && descriptor.configurable === false,
        ];
    })()''') == [True, True, True, True, True, True, True, True,
                 'Window', '[object Window]', '[object Window]', True]


async def test_members_reached_through_a_frame_window_run_on_the_frame(frames):
    assert frames.evaluate('''(() => {
        const w = document.querySelector('iframe').contentWindow;
        w.setTimeout(() => { w.__ran = true; }, 0);
        w.addEventListener('probe', () => { w.__heard = true; });
        w.dispatchEvent(new Event('probe'));
        return [typeof w.setTimeout, typeof w.addEventListener, w.__heard === true];
    })()''') == ['function', 'function', True]

    child = next(iter(frames._owned.frames))
    (await child.run_until_idle(timeout=1))
    assert child.evaluate('[window.__ran === true, window.__heard]') == [True, True]


async def _until(page, expression, expected, attempts=8):
    """Pump the frame until *expression* answers *expected*; a navigation lands asynchronously."""
    for _ in range(attempts):
        (await page.run_until_idle(timeout=1))
        for frame in list(page._owned.frames):
            (await frame.run_until_idle(timeout=1))
        if page.evaluate(expression) == expected:
            return True
    return False


async def test_a_frame_window_keeps_its_identity_across_navigations(frames):
    assert frames.evaluate('''(() => {
        window.saved = document.querySelector('iframe').contentWindow;
        document.querySelector('iframe').src = 'http://a.test/next';
        return true;
    })()''') is True
    assert (await _until(frames, 'saved.document.URL', 'http://a.test/next'))
    assert frames.evaluate('''(() => [
        saved === document.querySelector('iframe').contentWindow,
        saved === window[0],
        typeof saved.setTimeout,
    ])()''') == [True, True, 'function']

    # A second navigation, asked for through the same reference.
    frames.evaluate("saved.location = 'http://a.test/final'")
    assert (await _until(frames, 'saved.document.URL', 'http://a.test/final'))
    assert frames.evaluate('''(() => [
        saved === document.querySelector('iframe').contentWindow,
        saved === window[0],
    ])()''') == [True, True]
