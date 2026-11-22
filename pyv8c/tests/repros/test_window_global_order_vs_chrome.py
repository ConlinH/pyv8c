"""The Window own-property order, against the Chrome installed on this machine.

The two releases carry different interface sets: this repository's baseline is
Chromium 154, and the local reference may be another 15x.  The cases therefore
compare the names both browsers install, in the segment each one puts them in --
the legacy factories and template constructors before ``window``, the Window
members after it -- rather than the whole list.
"""
import pytest

from harness import ReferenceUnavailable, chrome_reference

CHROME_PAGE = "https://example.test/"


def _names(page):
    return page.evaluate("Object.getOwnPropertyNames(window)")


async def _here():
    from pyv8c.async_api import Browser

    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        return _names(page)
    finally:
        (await browser.close())


async def _there():
    async with chrome_reference() as reference:
        page = (await reference.browser.new_page())
        try:
            (await page.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200, content_type="text/html", body="<body></body>"
                ),
            ))
            (await page.goto(CHROME_PAGE))
            return await page.evaluate("Object.getOwnPropertyNames(window)")
        finally:
            (await page.close())


@pytest.fixture(scope="module")
async def orders():
    here = (await _here())
    try:
        there = (await _there())
    except ReferenceUnavailable as error:
        pytest.skip(str(error))
    return here, there


async def test_the_legacy_factories_lead_the_constructor_block(orders):
    here, there = orders
    start = here.index("Option")
    assert here[start:start + 9] == [
        "Option", "Image", "Audio",
        "webkitURL", "webkitRTCPeerConnection", "webkitMediaStream",
        "WebKitMutationObserver", "WebKitCSSMatrix", "FeaturePolicy",
    ]
    assert there.index("Option") < there.index("webkitURL") < there.index("window")


async def test_the_template_constructors_keep_chrome_order(orders):
    here, there = orders
    here_head = here[here.index("Option"):here.index("window")]
    there_head = there[there.index("Option"):there.index("window")]
    shared = set(here_head) & set(there_head)
    assert [name for name in here_head if name in shared] == [
        name for name in there_head if name in shared
    ]


async def test_the_window_members_keep_chrome_order(orders):
    here, there = orders
    here_tail = here[here.index("window"):here.index("caches")]
    there_tail = there[there.index("window"):there.index("caches")]
    shared = set(here_tail) & set(there_tail)
    assert [name for name in here_tail if name in shared] == [
        name for name in there_tail if name in shared
    ]


async def test_the_context_dependent_members_follow_the_operations_in_both(orders):
    here, there = orders
    for names in (here, there):
        assert names.index("caches") > names.index("fetch")
        assert names.index("cookieStore") > names.index("alert")


async def test_the_host_global_precedes_the_context_dependent_members_in_both(orders):
    here, there = orders
    for names in (here, there):
        assert names.index("chrome") < names.index("crashReport")
        assert names.index("crashReport") < names.index("caches")


async def test_full_order_matches_same_release_chrome():
    """Compare the complete key order in the same secure document context."""
    from pyv8c.async_api import Browser
    from pyv8c.engine.idl import Idl

    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<!doctype html><body></body>", url=CHROME_PAGE, wait_until="commit"))
        (await page.wait_for_load_state("load"))
        here = _names(page)
    finally:
        (await browser.close())
    try:
        async with chrome_reference() as reference:
            if reference.version != Idl.load().metadata.get("chromium_version"):
                pytest.skip("the installed Chrome and the IDL baseline have different releases")
            page = (await reference.browser.new_page())
            try:
                (await page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="<!doctype html><body></body>")))
                (await page.goto(CHROME_PAGE))
                there = await page.evaluate("Object.getOwnPropertyNames(window)")
            finally:
                (await page.close())
    except ReferenceUnavailable as error:
        pytest.skip(str(error))
    assert here == there
