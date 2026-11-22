'''Regression tests for direct Window bindings and interceptor removal.'''

import pytest

from pyv8c.async_api import Browser
from pyv8c.web.bom.window import Window


@pytest.fixture
async def page():
    browser = (await Browser(cache=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content('<div id="namedElement"></div>', url="http://a.test/", wait_until="commit"))
    try:
        yield page
    finally:
        (await browser.close())


async def test_window_declares_no_named_handler_of_its_own():
    browser = (await Browser(cache=False, stub_logging="off").__aenter__())
    try:
        bindings = browser.bindings
        config = bindings.class_config_for("Window")
        # Stub filling is covered by test_interface_stubs; here the class declares none.
        for suffix in ("get", "set", "query", "del", "enum"):
            name = "__v8_name_" + suffix + "__"
            assert not any(name in base.__dict__ for base in Window.__mro__)
        assert config.index_get
        assert config.cross_origin_check
        assert config.cross_origin_name_get
        assert not hasattr(bindings, 'binding_plan')
        # What the page gets: the convention binds ``fn_postMessage``/``get_onmessage``.
        assert "postMessage" in {item.name for item in config.methods}
        assert "onmessage" in {item.name for item in config.attributes}
    finally:
        (await browser.close())


async def test_normal_access_never_calls_python_named_handler(page, monkeypatch):
    calls = []
    original = Window.__getattribute__

    def traced(self, name):
        if name in ("__v8_name_get__", "__v8_name_set__"):
            calls.append(name)
        return original(self, name)

    monkeypatch.setattr(Window, "__getattribute__", traced)
    # Window inherits the named-properties-object flag from WindowProperties, so
    # the engine installs no named interceptor here and the handlers the
    # baseline declares stay inert stubs.
    assert page.evaluate('''(() => {
        for (let i = 0; i < 100; i++) {
            window.answer = i;
            window.document; window.postMessage; window.onmessage;
            window.missingProperty;
        }
        return [answer, namedElement.id];
    })()''') == [99, "namedElement"]
    assert calls == []


async def test_message_member_descriptors_and_identity(page):
    assert page.evaluate('''(() => {
        const m = Object.getOwnPropertyDescriptor(window, 'postMessage');
        const e = Object.getOwnPropertyDescriptor(window, 'onmessage');
        const handler = () => {};
        onmessage = handler;
        return [postMessage.name, postMessage.length, postMessage === postMessage,
            m.value === postMessage, m.writable, m.enumerable, m.configurable,
            typeof e.get, typeof e.set, e.enumerable, e.configurable,
            onmessage === handler];
    })()''') == ['postMessage', 1, True, True, True, True, True,
                  'function', 'function', True, True, True]


@pytest.mark.parametrize("arguments", [
    '"message"', '"message", "*"', '"message", "*", []',
    '"message", {}', '"message", {targetOrigin: "*", transfer: []}',
    '"message", null', '"message", undefined',
])
async def test_post_message_overloads(page, arguments):
    page.evaluate('window.received = []; onmessage = e => received.push(e.data)')
    page.evaluate('postMessage(' + arguments + ')')
    (await page.run_until_idle(timeout=1))
    assert page.evaluate('received') == ['message']


@pytest.mark.parametrize("arguments,error", [
    ('', 'TypeError'),
    ('1, "*", null', 'TypeError'),
    ('1, {targetOrigin: "*"}, []', 'SyntaxError'),
    ('1, "not-an-origin"', 'SyntaxError'),
])
async def test_post_message_rejects_invalid_arguments(page, arguments, error):
    assert page.evaluate('''(() => {
        try { postMessage(''' + arguments + '''); return 'allowed'; }
        catch (e) { return e.name; }
    })()''') == error


@pytest.mark.parametrize("value", ['null', 'undefined', '42', 'false', '"text"'])
async def test_onmessage_non_object_becomes_null(page, value):
    assert page.evaluate('onmessage = ' + value + '; onmessage === null')


async def test_post_message_target_filter(page):
    page.evaluate('window.received = []; onmessage = e => received.push(e.data); '
                  'postMessage("drop", {targetOrigin:"http://other.test"})')
    (await page.run_until_idle(timeout=1))
    assert page.evaluate('received') == []


async def test_non_callable_onmessage_is_not_invoked(page):
    page.evaluate('onmessage = {}')
    assert page.evaluate('''dispatchEvent(new Event('message'))''')
