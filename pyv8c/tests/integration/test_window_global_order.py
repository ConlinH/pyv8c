"""The order a real Window realm installs its own properties in.

These cases verify the observed order, legacy aliases and descriptor behavior
that a realm exposes through ``Object.getOwnPropertyNames(window)``.
"""
import pytest

pytestmark = pytest.mark.native


def _names(page):
    return page.evaluate("Object.getOwnPropertyNames(window)")


async def test_legacy_factories_and_members_follow_observation(page, browser):
    names = _names(page)
    observed = browser.bindings.idl.observed_window_order
    selected = [name for name in observed if name in {"Option", "Image", "window", "alert"}]
    assert [name for name in names if name in set(selected)] == selected


async def test_the_context_dependent_members_follow_the_operations(page):
    names = _names(page)
    assert names.index("onclick") < names.index("alert")
    assert names.index("caches") > names.index("fetch")
    assert names.index("cookieStore") > names.index("alert")
    assert names.index("documentPictureInPicture") > names.index("structuredClone")


async def test_the_host_globals_precede_the_context_dependent_members(page):
    names = _names(page)
    assert names.index("chrome") > names.index("WebAssembly")
    assert names.index("chrome") < names.index("crashReport")
    assert names.index("crashReport") < names.index("caches")
    assert names.index("caches") < names.index("AbsoluteOrientationSensor")


async def test_the_legacy_aliases_share_the_target_constructor(page):
    assert page.evaluate("window.webkitURL === window.URL")
    assert page.evaluate("window.FeaturePolicy === window.PermissionsPolicy")
    assert page.evaluate("window.WebKitCSSMatrix === window.DOMMatrix")
    assert page.evaluate("window.WebKitMutationObserver === window.MutationObserver")


async def test_the_realm_answers_the_environment_order(page, browser):
    actual = _names(page)
    expected = browser.bindings.idl.observed_window_order
    # V8 installs its own built-ins before this binding can define host names.
    common = set(actual[actual.index("Option"):]) & set(expected)
    assert [name for name in actual if name in common] == [name for name in expected if name in common]


async def test_iframe_window_order_and_descriptor_match_parent(page):
    result = page.evaluate("""(() => {
        const frame = document.createElement('iframe');
        document.body.appendChild(frame);
        const own = w => Object.getOwnPropertyNames(w).filter(n => ['Option','Image','window','fetch','caches'].includes(n));
        const desc = w => { const d = Object.getOwnPropertyDescriptor(w, 'caches');
            return d && [d.enumerable, d.configurable, typeof d.get, typeof d.set]; };
        return [own(window), own(frame.contentWindow), desc(window), desc(frame.contentWindow),
                frame.contentWindow.Option !== window.Option];
    })()""")
    assert result[0] == result[1]
    assert result[2] == result[3]
    assert result[4] is True


async def test_navigation_preserves_window_install_order(browser):
    page = browser.new_profile().new_page()
    names = ["Option", "Image", "window", "fetch", "caches"]
    before = [name for name in _names(page) if name in names]
    (await page.goto("data:text/html,<body>next</body>"))
    after = [name for name in _names(page) if name in names]
    assert after == before
