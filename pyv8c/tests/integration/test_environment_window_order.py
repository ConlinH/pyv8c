"""The environment observation orders only the phases that can actually move."""
from dataclasses import replace

import pytest

from pyv8c.async_api import Browser
from pyv8c.engine.idl import Idl, Profile, build_plan


def _table(order, *, browser="Chrome/154.0.8030.0", against=None):
    base = Idl.load(profile=None)
    shipped = Profile.load()
    profile = replace(shipped, browser=browser, own_order=tuple(order),
                      against=base.digest if against is None else against)
    return Idl(base.path, base.digest, base.metadata, base.interfaces, profile)


def _swap(order, a, b):
    result = list(order)
    i, j = result.index(a), result.index(b)
    result[i], result[j] = result[j], result[i]
    return result


async def test_observation_controls_constructor_and_member_order(monkeypatch):
    base = Idl.load()
    reference = list(base.observed_window_order)
    reference = _swap(reference, "XPathResult", "XPathExpression")
    reference = _swap(reference, "locationbar", "menubar")
    table = _table(reference)
    monkeypatch.setattr("pyv8c.platform.browser.Idl.load", lambda *args, **kwargs: table)
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        names = page.evaluate("Object.getOwnPropertyNames(window)")
        assert names.index("XPathExpression") < names.index("XPathResult")
        assert names.index("menubar") < names.index("locationbar")
        assert page.evaluate("Object.getOwnPropertyDescriptor(window, 'locationbar').enumerable")
    finally:
        (await browser.close())


async def test_environment_can_interleave_original_installation_phases(monkeypatch):
    base = Idl.load()
    order = _swap(base.observed_window_order, "XPathResult", "alert")
    table = _table(order)
    monkeypatch.setattr("pyv8c.platform.browser.Idl.load", lambda *args, **kwargs: table)
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        names = page.evaluate("Object.getOwnPropertyNames(window)")
        assert names.index("alert") < names.index("XPathExpression")
        assert names.index("XPathResult") > names.index("window")
    finally:
        (await browser.close())


async def test_environment_can_order_builtin_among_host_members(monkeypatch):
    base = Idl.load()
    order = _swap(base.observed_window_order, "Temporal", "alert")
    table = _table(order)
    monkeypatch.setattr("pyv8c.platform.browser.Idl.load", lambda *args, **kwargs: table)
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        names = page.evaluate("Object.getOwnPropertyNames(window)")
        selected = {"Temporal", "alert", "WebAssembly", "chrome"}
        assert [name for name in names if name in selected] == [name for name in order if name in selected]
        assert page.evaluate("typeof Temporal === 'object' && typeof WebAssembly.compile === 'function'")
    finally:
        (await browser.close())


async def test_warn_reports_even_when_stub_logging_is_off(monkeypatch):
    from loguru import logger
    base = Idl.load()
    table = _table(base.observed_window_order, browser="Chrome/156.0.0.0")
    monkeypatch.setattr("pyv8c.platform.browser.Idl.load", lambda *args, **kwargs: table)
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), format="{message}")
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        browser.new_profile().new_page()
        browser.new_profile().new_page()
        warnings = [item for item in messages if "Window order (environment)" in item]
        assert len(warnings) == 1
        assert "environment=156.0.0.0" in warnings[0]
    finally:
        (await browser.close())
        logger.remove(token)


async def test_one_moved_builtin_is_not_reported_as_hundreds_of_differences(monkeypatch):
    from loguru import logger
    base = Idl.load()
    table = _table(base.observed_window_order)
    monkeypatch.setattr("pyv8c.platform.browser.Idl.load", lambda *args, **kwargs: table)
    actual = list(table.observed_window_order)
    actual.remove("Temporal")
    actual.insert(actual.index("Option"), "Temporal")
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), format="{message}")
    browser = (await Browser(real_network=False).__aenter__())
    try:
        browser.bindings.check_window_order(actual)
        assert any("order differences (1)" in item and "Temporal" in item for item in messages)
    finally:
        (await browser.close())
        logger.remove(token)


async def test_missing_environment_order_uses_stable_fallback():
    base = Idl.load(profile=None)
    table = Idl(base.path, base.digest, base.metadata, base.interfaces)
    plan = build_plan(table, "Window")
    assert table.info["window_order_source"] == "fallback"
    assert plan.installs == tuple(sorted(plan.installs))


async def test_stale_surface_does_not_discard_observation():
    base = Idl.load()
    order = _swap(base.observed_window_order, "XPathResult", "XPathExpression")
    table = _table(order, against="different-digest")
    assert table.profile is None
    assert table.observed_window_order == tuple(order)
    assert "XPathExpression" in build_plan(table, "Window").installs


async def test_order_is_part_of_the_binding_cache_identity():
    base = Idl.load()
    original = _table(base.observed_window_order)
    changed = _table(_swap(base.observed_window_order, "XPathResult", "XPathExpression"))
    assert original.surface_key != changed.surface_key
    assert build_plan(original, "Worker") == build_plan(changed, "Worker")


async def test_iframe_uses_same_order(monkeypatch):
    base = Idl.load()
    table = _table(_swap(base.observed_window_order, "XPathResult", "XPathExpression"))
    monkeypatch.setattr("pyv8c.platform.browser.Idl.load", lambda *args, **kwargs: table)
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("""(() => {
            const f = document.createElement('iframe'); document.body.appendChild(f);
            const names = Object.getOwnPropertyNames(f.contentWindow);
            return names.indexOf('XPathExpression') < names.indexOf('XPathResult');
        })()""") is True
    finally:
        (await browser.close())


@pytest.mark.parametrize("value", [["a", "a"], [""], [1], "abc", None])
async def test_invalid_observation_is_rejected(value):
    with pytest.raises(ValueError, match="own_order"):
        Profile("test", own_order=value)
