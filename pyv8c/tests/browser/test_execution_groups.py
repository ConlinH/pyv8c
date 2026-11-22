import threading

from pyv8c.async_api import Browser


async def test_frames_share_group_and_agent_while_pages_share_owner_only():
    with_browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        profile = with_browser.new_profile()
        page, other = profile.new_page(), profile.new_page()
        (await page.set_content('<body><iframe></iframe></body>', wait_until="commit"))
        child = page.document.fn_querySelector('iframe')._child_services._page_realm.page
        assert page.execution_group is child.execution_group
        assert page.realm.agent is child.realm.agent
        assert page.realm.agent is not other.realm.agent
        assert page.executor.call(threading.get_ident) == child.executor.call(threading.get_ident)
        assert page.execution_group is not other.execution_group
        assert page.isolate is not other.isolate
        assert page.executor.call(threading.get_ident) == other.executor.call(threading.get_ident) == threading.get_ident()
        await child.close()
        assert not page.executor.closed
        assert page.evaluate('1+1') == 2
    finally:
        (await with_browser.close())


async def test_same_origin_frames_share_microtasks_in_enqueue_order():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', wait_until="commit"))
        assert page.evaluate('''globalThis.log=[];
            frames[0].eval("Promise.resolve().then(()=>parent.log.push('child'))");
            Promise.resolve().then(()=>log.push('parent')); 'scheduled'
        ''') == 'scheduled'
        assert page.evaluate('log') == ['child', 'parent']
    finally:
        (await browser.close())


async def test_origin_navigation_changes_agent_without_replacing_group():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        group = page.execution_group
        opaque = page.realm.agent
        (await page.set_content('<body></body>', 'https://one.test/', wait_until="commit"))
        first = page.realm.agent
        assert first is not opaque
        (await page.set_content('<body></body>', 'https://two.test/', wait_until="commit"))
        assert page.realm.agent is not first
        (await page.set_content('<body></body>', 'https://one.test/again', wait_until="commit"))
        assert page.realm.agent is first
        assert page.execution_group is group
    finally:
        (await browser.close())


async def test_foreign_close_is_rejected_and_owner_close_releases_the_group():
    import asyncio
    import pytest
    from pyv8c.foundation.host_errors import HostStateError
    async with Browser(real_network=False, stub_logging="off") as runtime:
        page = runtime.new_page()
        group = page.execution_group
        assert not hasattr(group.executor, "_thread")
        with pytest.raises(HostStateError, match="owner thread"):
            await asyncio.to_thread(lambda: asyncio.run(page.close()))
        assert page.evaluate("42") == 42
        (await page.close())
        assert page.closed and group.closed and group.executor.closed
        assert group.executor._soon is None and group.executor._timer is None


async def test_evaluate_function_result_can_be_printed_outside_the_realm():
    from pyv8c.async_api import Browser
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        result = page.evaluate("window.AbsoluteOrientationSensor")
        assert isinstance(result, str)
        assert "AbsoluteOrientationSensor" in result
        assert str(result) == result
        nested = page.evaluate("({fn: function example(){}, values: [() => 1]})")
        assert isinstance(nested["fn"], str) and "example" in nested["fn"]
        assert isinstance(nested["values"][0], str)
        handle = page.evaluate_handle("window.AbsoluteOrientationSensor")
        from pyv8c import pyv8
        assert isinstance(handle, pyv8.JSFunction)
        assert "AbsoluteOrientationSensor" in page.executor.call(repr, handle)
    finally:
        (await browser.close())


async def test_evaluate_noncopyable_objects_are_safe_to_print():
    from pyv8c import pyv8
    from pyv8c.async_api import Browser

    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        if page.evaluate("typeof Temporal !== 'undefined'"):
            assert page.evaluate("window.Temporal") == "[object Temporal]"
        for expression in ("new Map([[1, 2]])", "new Set([1])", "Promise.resolve(1)",
                           "Object.create({[Symbol.toStringTag]: 'Temporal'})"):
            result = page.evaluate(expression)
            assert isinstance(result, str)
            assert str(result) == result
        nested = page.evaluate("({map: new Map(), items: [new Set()]})")
        assert isinstance(nested["map"], str)
        assert isinstance(nested["items"][0], str)
        handle = page.evaluate_handle("new Map()")
        assert isinstance(handle, pyv8.JSObject)
        assert page.executor.call(repr, handle) == "[object Map]"
    finally:
        (await browser.close())
