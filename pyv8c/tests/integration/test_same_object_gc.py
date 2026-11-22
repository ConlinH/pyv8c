"""Window-owned SameObject values retain JS properties through collection."""
import gc
import weakref

import pytest

from pyv8c.async_api import Browser


@pytest.mark.parametrize('name', ['crypto', 'performance', 'customElements'])
async def test_window_singleton_expando_survives_gc_without_an_extra_js_reference(name):
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<!doctype html><body></body>', url='https://singleton.test/', wait_until="commit"))
        page.evaluate(f'{name}.probe=()=>42;true')
        page.realm.executor.call(page.realm.v8_context.gc)
        assert page.evaluate(f'typeof {name}.probe') == 'function'
        assert page.evaluate(f'{name}.probe()') == 42
        # A document replacement gets its own singleton, even though the
        # browsing context retains its WindowProxy.
        (await page.set_content('<!doctype html><body>new</body>', url='https://singleton.test/next', wait_until="commit"))
        assert page.evaluate(f'typeof {name}.probe') == 'undefined'
        page.realm.executor.call(page.realm.v8_context.gc)
    finally:
        (await browser.close())


async def test_closed_pages_release_retained_singleton_wrappers():
    browser = (await Browser(stub_logging='off').__aenter__())
    profile = browser.new_profile()
    refs = []
    try:
        for _ in range(5):
            page = profile.new_page()
            page.evaluate('crypto.probe=()=>42;performance.probe=crypto;true')
            refs.append(weakref.ref(page))
            (await page.close())
            assert page.realm._object_roots is None
            assert not page.realm._object_root_ids
            del page
        gc.collect()
        assert not profile.pages
        assert not any(reference() is not None for reference in refs)
    finally:
        (await browser.close())
