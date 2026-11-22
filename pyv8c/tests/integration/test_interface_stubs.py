from pyv8c import pyv8
from loguru import logger

from pyv8c.async_api import Browser
from pyv8c.engine.binder import CROSS_ORIGIN_FAMILIES, HANDLER_FAMILIES
from pyv8c.features.catalog import CATALOG


async def test_baseline_only_interface_is_generated_and_keeps_metadata():
    browser = (await Browser().__aenter__())
    page = browser.new_profile().new_page()
    value = page.evaluate("""
      (() => {
        const c = new AbortController();
        return [
          typeof AbortController,
          typeof AbortController.prototype.abort,
          c instanceof AbortController,
          Object.prototype.toString.call(c),
          AbortController.prototype.constructor === AbortController,
          typeof URL.createObjectURL
        ];
      })()
    """)
    assert value == ["function", "function", True, "[object AbortController]", True, "function"]
    (await browser.close())


async def test_stub_method_logs_and_real_member_wins():
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        browser = (await Browser(stub_logging="on").__aenter__())
        page = browser.new_profile().new_page()
        assert page.evaluate("new AudioDecoder({}).state") is pyv8.Undefined
        assert page.evaluate("AudioDecoder.isConfigSupported({})") is pyv8.Undefined
        assert any("AudioDecoder.state getter 待实现" in message for message in messages)
        assert any("AudioDecoder.isConfigSupported() 待实现" in message for message in messages)
        assert page.evaluate("new URL('https://example.test/').toString()") == "https://example.test/"
        assert not any("URL.toString() 待实现" in message for message in messages)
        (await browser.close())
    finally:
        logger.remove(token)


async def test_internal_objects_are_not_exposed():
    browser = (await Browser().__aenter__())
    page = browser.new_profile().new_page()
    value = page.evaluate("[typeof Page, typeof Runtime, typeof BaselineRegistry, typeof __pyv8c_interface_runtime]")
    assert value == ["undefined", "undefined", "undefined", "undefined"]
    (await browser.close())


async def test_stub_identity_and_direct_callback_markers_are_separate():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        bindings = browser.bindings
        decoder = bindings.class_for("AudioDecoder")
        url = bindings.class_for("URL")
        assert decoder.__dict__["__v8_idl_stub__"] is True
        assert "__v8_idl_stub__" not in url.__dict__

        decoder_config = bindings.class_config_for("AudioDecoder")
        decoder_item = next(item for item in decoder_config.methods
                            if item.name == "isConfigSupported")
        decoder_callback = decoder_item.callback
        assert not hasattr(decoder_callback, "__v8_direct_callback__")
        assert decoder_callback.__v8_idl_stub__ is True

        url_config = bindings.class_config_for("URL")
        to_string = next(item for item in url_config.methods if item.name == "toString")
        url_callback = to_string.callback
        assert not hasattr(url_callback, "__v8_direct_callback__")
        assert not hasattr(url_callback, "__v8_idl_stub__")
    finally:
        (await browser.close())


async def test_a_declared_handler_the_class_forgot_is_filled():
    """A baseline handler a hand-written class omits is filled, not silenced."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        bindings = browser.bindings
        storage = bindings.class_config_for("Storage")
        assert not getattr(storage.name_get, "__v8_idl_stub__", False)
        assert storage.index_get.__v8_idl_stub__ is True
        assert storage.index_set.__v8_idl_stub__ is True
        # Window's names arrive through WindowProperties, so the baseline declares no
        # plain named handler for it and none is filled; the cross-origin one is the
        # class's own.  The baseline alone decides.
        window = bindings.class_config_for("Window")
        assert window.name_get is None
        assert window.name_query is None
        assert not getattr(window.cross_origin_name_get, "__v8_idl_stub__", False)
        assert bindings.class_config_for("WindowProperties").name_get.__v8_idl_stub__ is True
    finally:
        (await browser.close())


async def test_a_filled_handler_reports_itself_only_when_asked():
    """The placeholder runs on ordinary probes, so it logs at the verbose level."""
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        browser = (await Browser(stub_logging="on").__aenter__())
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        assert page.evaluate("localStorage[0]") is pyv8.Undefined
        assert not any("__v8_index_get__ 待实现" in message for message in messages)

        noisy = (await Browser(stub_logging="verbose").__aenter__())
        try:
            page = noisy.new_profile().new_page()
            (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
            assert page.evaluate("localStorage[0]") is pyv8.Undefined
            assert page.evaluate("localStorage[1]") is pyv8.Undefined
            assert any("Storage.__v8_index_get__ 待实现" in message for message in messages)
            assert len([m for m in messages if "Storage.__v8_index_get__" in m]) == 1
        finally:
            (await noisy.close())
    finally:
        logger.remove(token)
        (await browser.close())


async def test_every_declared_handler_is_installed_or_filled():
    """The baseline decides: what it declares the class declares or a stub does."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        bindings = browser.bindings
        runtime = bindings.runtime
        for feature in CATALOG.specs.values():
            runtime.load(feature.name)
        for name in dict(runtime.implementations):
            config = bindings.class_config_for(name)
            handlers = (bindings.spec(name).get("template") or {}).get("handlers") or {}
            for family, mapping in HANDLER_FAMILIES + CROSS_ORIGIN_FAMILIES:
                table = (handlers.get("cross_origin") or {}) if "cross_origin" in mapping[0][1]\
                    else handlers
                for key, callback in mapping:
                    if not (table.get(family) or {}).get(key):
                        continue
                    assert getattr(config, callback[5:-2], None) is not None, (name, callback)
    finally:
        (await browser.close())


async def test_window_interfaces_are_materialized_on_demand(shared_browser):
    page = shared_browser.new_profile().new_page()
    try:
        session = page.binding_session
        at_cold_start = set(session.materialized_interfaces)
        exposed = set(session.interface_names("Window"))
        assert set(shared_browser.bindings.plan("Window").eager) <= at_cold_start
        assert exposed - at_cold_start
        assert "AbortController" not in at_cold_start
        assert page.evaluate("typeof AbortController") == "function"
        assert "AbortController" in session.materialized_interfaces
        assert page.evaluate("AbortController === AbortController") is True
        assert page.evaluate("Object.getOwnPropertyNames(window).includes('AbortController')") is True
    finally:
        (await page.close())
