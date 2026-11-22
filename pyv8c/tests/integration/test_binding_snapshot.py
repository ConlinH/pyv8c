"""Binding-snapshot semantics under multi-realm concurrency.

Materialization is answer based: a realm stages an overlay and hands it to the native
V8Context as the lazy factory's answer.  The Python class for a given interface name must
be one shared object across every realm (so the native registry stays consistent), while
each realm's config view and materialized set stay isolated from its neighbours.
"""

import asyncio
import threading

import pytest

pytest.importorskip("pyv8c.pyv8")
from pyv8c.async_api import Browser

LAZY_NAME = "XPathEvaluator"
SECOND_LAZY = "GPUBufferUsage"


@pytest.fixture
async def browser():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        yield browser
    finally:
        (await browser.close())


def _fresh_page(browser):
    return browser.new_profile().new_page()


async def test_concurrent_first_access_yields_one_class_object(browser):
    pages = [_fresh_page(browser) for _ in range(6)]
    try:
        async def trigger(page):
            await asyncio.sleep(0)
            assert page.evaluate("typeof " + LAZY_NAME) == "function"
        await asyncio.gather(*(trigger(page) for page in pages))

        expected = browser.bindings.class_for(LAZY_NAME)
        assert expected is not None
        for page in pages:
            assert LAZY_NAME in page.binding_session.materialized_interfaces
            assert page.binding_session.class_for(LAZY_NAME) is expected
    finally:
        for page in pages:
            (await page.close())


async def test_realm_configs_stay_isolated_across_commits(browser):
    first = _fresh_page(browser)
    second = _fresh_page(browser)
    try:
        first.evaluate("typeof " + LAZY_NAME)
        first_cls = browser.bindings.class_for(LAZY_NAME)
        first_configs = dict(first.binding_session.configs)
        assert first_cls in first_configs
        # The untouched realm has neither built nor staged anything for it.
        assert first_cls not in dict(second.binding_session.configs)

        # A commit in the second realm must not mutate the first realm's view.
        second.evaluate("typeof " + SECOND_LAZY)
        second_cls = browser.bindings.class_for(SECOND_LAZY)
        assert second_cls in dict(second.binding_session.configs)
        assert dict(first.binding_session.configs) == first_configs
        assert second_cls not in dict(first.binding_session.configs)
    finally:
        await first.close()
        await second.close()


async def test_commits_keep_existing_templates_stable(browser):
    page = _fresh_page(browser)
    try:
        page.evaluate("""
            window.__ctor = XPathEvaluator;
            window.__proto = XPathEvaluator.prototype;
        """)
        # A second, unrelated answer lands in the realm afterwards.
        page.evaluate("typeof GPUBufferUsage")
        assert "GPUBufferUsage" in page.binding_session.materialized_interfaces
        # The cached template was not invalidated by the later answer.
        assert page.evaluate("window.__ctor === XPathEvaluator")
        assert page.evaluate("window.__proto === XPathEvaluator.prototype")
        assert page.evaluate("Object.prototype.toString.call(new XPathEvaluator())") == "[object XPathEvaluator]"
    finally:
        (await page.close())


async def test_configs_view_is_read_only(browser):
    page = _fresh_page(browser)
    try:
        page.evaluate("typeof " + LAZY_NAME)
        view = page.binding_session.configs
        key = browser.bindings.class_for(LAZY_NAME)
        with pytest.raises(TypeError):
            view[key] = {}
    finally:
        (await page.close())


async def test_repeated_concurrent_materialization_within_one_realm(browser):
    page = _fresh_page(browser)
    try:
        async def touch():
            await asyncio.sleep(0)
            assert page.evaluate("typeof " + LAZY_NAME) == "function"
        await asyncio.gather(*(touch() for _ in range(4)))

        cls = page.binding_session.class_for(LAZY_NAME)
        assert page.binding_session.materialize(LAZY_NAME) is cls
        assert len([n for n in page.binding_session.materialized_interfaces if n == LAZY_NAME]) == 1
    finally:
        (await page.close())
