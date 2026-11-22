"""Independent page groups own isolates; frames share their group owner."""
from pyv8c.async_api import Browser


async def _pages(browser, count):
    context = browser.new_profile()
    pages = [context.new_page() for _ in range(count)]
    for page in pages:
        (await page.set_content("<body>x</body>", wait_until="commit"))
    return pages


def _child_page(page):
    return page.executor.call(lambda: page.document.fn_querySelectorAll("iframe")[0]
                              ._child_services._page_realm.page)


async def test_independent_pages_have_distinct_isolates_by_default():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        pages = (await _pages(browser, 3))
        assert len({page.v8_context.isolate_id for page in pages}) == 3
    finally:
        (await browser.close())


async def test_a_frame_shares_the_isolate_of_its_page_tree():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (await _pages(browser, 1))[0]
        page.evaluate("document.body.innerHTML = '<iframe srcdoc=\"child\"></iframe>'")
        (await page.wait_for("frames[0].document.body?.textContent === 'child'", timeout=3))
        assert _child_page(page).v8_context.isolate_id == page.v8_context.isolate_id
        # What the sharing is for: the child's own objects, reached live from the parent.
        assert page.evaluate("""(() => {
            const child = window.frames[0];
            child.reached = 'live';
            return String(child.document.body.textContent) + ' / ' + String(child.reached);
        })()""") == "child / live"
    finally:
        (await browser.close())


async def test_a_script_inserted_frame_initializes_while_its_isolate_is_held():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body>x</body>", wait_until="commit"))
        page.evaluate("document.body.innerHTML = '<iframe></iframe>'")
        (await page.wait_for_load_state(timeout=3))
        assert page.evaluate("window.frames.length") == 1
    finally:
        (await browser.close())


async def test_group_isolate_is_closed_with_its_last_page():
    """The isolate outlives navigation but not the page: a leak would keep its heap alive."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (await _pages(browser, 1))[0]
        isolate = page.isolate
        page.evaluate("document.body.innerHTML = '<iframe></iframe>'")
        (await page.wait_for_load_state(timeout=3))
        (await page.close())
        assert isolate.closed
    finally:
        (await browser.close())
