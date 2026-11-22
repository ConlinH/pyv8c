import json

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c import BrowserEnvironment
from pyv8c.async_api import Browser
from pyv8c.foundation.environment import PROFILE


async def new_page(environment=None):
    browser = (await Browser(environment=environment, stub_logging="off").__aenter__())
    return browser, browser.new_profile().new_page()


async def test_default_plugins_match_current_chrome_pdf_snapshot():
    browser, page = (await new_page())
    try:
        assert page.evaluate("""
            (() => [
                navigator.plugins.length,
                navigator.mimeTypes.length,
                [...navigator.plugins].map(plugin => [
                    plugin.name, plugin.filename, plugin.description,
                    [...plugin].map(mime => mime.type),
                ]),
                [...navigator.mimeTypes].map(mime => [
                    mime.type, mime.suffixes, mime.description, mime.enabledPlugin.name,
                ]),
            ])()
        """) == [
            5,
            2,
            [
                [
                    "PDF Viewer", "internal-pdf-viewer", "Portable Document Format",
                    ["application/pdf", "text/pdf"],
                ],
                [
                    "Chrome PDF Viewer", "internal-pdf-viewer", "Portable Document Format",
                    ["application/pdf", "text/pdf"],
                ],
                [
                    "Chromium PDF Viewer", "internal-pdf-viewer", "Portable Document Format",
                    ["application/pdf", "text/pdf"],
                ],
                [
                    "Microsoft Edge PDF Viewer", "internal-pdf-viewer",
                    "Portable Document Format", ["application/pdf", "text/pdf"],
                ],
                [
                    "WebKit built-in PDF", "internal-pdf-viewer", "Portable Document Format",
                    ["application/pdf", "text/pdf"],
                ],
            ],
            [
                ["application/pdf", "pdf", "Portable Document Format", "PDF Viewer"],
                ["text/pdf", "pdf", "Portable Document Format", "PDF Viewer"],
            ],
        ]
    finally:
        (await browser.close())


async def test_plugin_and_mime_type_collections_follow_browser_legacy_collection_rules():
    browser, page = (await new_page())
    try:
        assert page.evaluate("""
            (() => {
                const plugins = navigator.plugins;
                const mimes = navigator.mimeTypes;
                return [
                    Object.prototype.toString.call(plugins),
                    Object.prototype.toString.call(plugins[0]),
                    Object.prototype.toString.call(mimes),
                    Object.prototype.toString.call(mimes[0]),
                    plugins.item(0) === plugins[0],
                    plugins.namedItem('PDF Viewer') === plugins['PDF Viewer'],
                    mimes.item(0) === mimes['application/pdf'],
                    plugins[0][0] === mimes[0],
                    mimes[0].enabledPlugin === plugins[0],
                    Object.keys(plugins),
                    Object.getOwnPropertyNames(plugins),
                    Object.getOwnPropertyDescriptor(plugins, 'PDF Viewer').enumerable,
                    Object.getOwnPropertyDescriptor(mimes, 'application/pdf').writable,
                    typeof plugins.refresh,
                    plugins.item(99) === null,
                    plugins.namedItem('missing') === null,
                ];
            })()
        """) == [
            "[object PluginArray]", "[object Plugin]", "[object MimeTypeArray]", "[object MimeType]",
            True, True, True, False, True,
            ["0", "1", "2", "3", "4"],
            [
                "0", "1", "2", "3", "4", "PDF Viewer", "Chrome PDF Viewer",
                "Chromium PDF Viewer", "Microsoft Edge PDF Viewer", "WebKit built-in PDF",
            ],
            False, False, "function", True, True,
        ]
    finally:
        (await browser.close())


async def test_environment_plugins_support_mapping_and_page_overrides():
    base = {
        "plugins": [{
            "name": "Runtime Plugin",
            "filename": "browser-plugin",
            "description": "Runtime description",
            "mime_types": [{"type": "application/browser", "suffixes": "browser", "description": "Runtime MIME"}],
        }],
    }
    override = {
        "plugins": [{
            "name": "Page Plugin",
            "filename": "page-plugin",
            "description": "Page description",
            "mime_types": [
                {"type": "application/page", "suffixes": "pg", "description": "Page MIME"},
                {"type": "text/page", "suffixes": "page", "description": "Text Page MIME"},
            ],
        }],
    }
    browser = (await Browser(environment=base, stub_logging="off").__aenter__())
    try:
        context = browser.new_profile()
        default_page = context.new_page()
        override_page = context.new_page(environment=override)
        assert default_page.evaluate(
            "[navigator.plugins[0].name, navigator.mimeTypes[0].type]"
        ) == ["Runtime Plugin", "application/browser"]
        assert override_page.evaluate("""
            [
                navigator.plugins.length,
                navigator.plugins['Page Plugin'].filename,
                navigator.plugins[0].namedItem('text/page').suffixes,
                navigator.mimeTypes['application/page'].enabledPlugin.name,
            ]
        """) == [
            1, "page-plugin", "page", "Page Plugin",
        ]
    finally:
        (await browser.close())


async def test_plugin_configuration_is_immutable_and_validated():
    environment = BrowserEnvironment({
        "plugins": [{"name": "Test", "mime_types": [{"type": "application/test"}]}],
    })
    assert environment.plugins[0].mime_types[0].type == "application/test"
    with pytest.raises(ValueError):
        BrowserEnvironment({"plugins": [{"name": "duplicate"}, {"name": "duplicate"}]})
    with pytest.raises(ValueError):
        BrowserEnvironment({"plugins": [{
            "name": "invalid",
            "mime_types": [
                {"type": "application/test"},
                {"type": "application/test"},
            ],
        }]})
    with pytest.raises(TypeError):
        BrowserEnvironment({"plugins": "not a plugin sequence"})


async def test_navigator_runtime_surfaces_match_chrome_shapes_and_identity():
    browser, page = (await new_page({
        "user_agent": "Mozilla/5.0 Test Runtime",
        "webdriver": True,
        "connection.effective_type": "3g",
        "connection.rtt": 250,
        "connection.downlink": 1.5,
        "connection.save_data": True,
        "battery.charging": False,
        "battery.charging_time": 120,
        "battery.discharging_time": 3600,
        "battery.level": 0.75,
        "storage.persistent.usage": 10,
        "storage.persistent.quota": 40,
    }))
    try:
        assert page.evaluate("""
            (() => [
                navigator.appVersion,
                navigator.webdriver,
                navigator.connection === navigator.connection,
                Object.prototype.toString.call(navigator.connection),
                navigator.connection.effectiveType,
                navigator.connection.rtt,
                navigator.connection.downlink,
                navigator.connection.saveData,
                navigator.webkitPersistentStorage === navigator.webkitPersistentStorage,
                Object.prototype.toString.call(navigator.webkitPersistentStorage),
                navigator.getBattery.length,
            ])()
        """) == [
            "5.0 Test Runtime", True, True, "[object NetworkInformation]",
            "3g", 250, 1.5, True, True,
            "[object DeprecatedStorageQuota]", 0,
        ]

        page.evaluate("""
            navigator.getBattery().then(battery => globalThis.batterySnapshot = [
                Object.prototype.toString.call(battery), battery.charging,
                battery.chargingTime, battery.dischargingTime, battery.level,
            ]);
            navigator.webkitPersistentStorage.queryUsageAndQuota(
                (usage, quota) => globalThis.quotaSnapshot = [usage, quota]
            );
            navigator.webkitPersistentStorage.requestQuota(
                100, quota => globalThis.grantedQuota = quota
            );
        """)
        (await page.run_until_idle())
        assert page.evaluate("batterySnapshot") == [
            "[object BatteryManager]", False, 120, 3600, 0.75,
        ]
        assert page.evaluate("quotaSnapshot") == [10, 40]
        assert page.evaluate("grantedQuota") == 40
    finally:
        (await browser.close())


async def test_get_battery_invalid_receiver_rejects_instead_of_throwing():
    browser, page = (await new_page())
    try:
        page.evaluate('''(() => {
            window.batteryReceiverResults = [];
            const method = navigator.getBattery;
            for (const receiver of [window, {}, null, document.body]) {
                try {
                    const promise = method.call(receiver);
                    batteryReceiverResults.push(['returned', promise instanceof Promise]);
                    promise.then(() => batteryReceiverResults.push(['resolved']),
                        e => batteryReceiverResults.push(['rejected', e.name, e.message]));
                } catch (error) {
                    batteryReceiverResults.push(['thrown', error.name, error.message]);
                }
            }
        })()''')
        (await page.wait_for('batteryReceiverResults.length === 8', timeout=2))
        results = page.evaluate('batteryReceiverResults')
        assert results[:4] == [['returned', True]] * 4
        assert results[4:] == [['rejected', 'TypeError',
                               "Failed to execute 'getBattery' on 'Navigator': Illegal invocation"]] * 4
    finally:
        (await browser.close())


async def test_get_battery_invalid_receiver_uses_method_realm():
    browser, page = (await new_page())
    try:
        page.evaluate('''(() => {
            const frame = document.createElement('iframe'); document.body.appendChild(frame);
            const child = frame.contentWindow;
            const promise = child.navigator.getBattery.call({});
            window.batteryRealm = {parent: promise instanceof Promise, child: promise instanceof child.Promise};
            promise.catch(error => { batteryRealm.error = [error instanceof TypeError,
                error instanceof child.TypeError, error.name, error.message]; });
        })()''')
        (await page.wait_for('batteryRealm.error', timeout=2))
        assert page.evaluate('batteryRealm') == {
            'parent': False, 'child': True,
            'error': [False, True, 'TypeError',
                      "Failed to execute 'getBattery' on 'Navigator': Illegal invocation"],
        }
    finally:
        (await browser.close())


async def test_navigator_subobjects_are_created_only_on_first_access():
    browser, page = (await new_page())
    try:
        navigator = page.window.navigator
        assert navigator._features.cached_names == frozenset()

        assert page.evaluate("navigator.userAgent.length > 0")
        assert navigator._features.cached_names == frozenset()

        assert page.evaluate("navigator.connection === navigator.connection")
        assert navigator._features.cached_names == {"connection"}

        assert page.evaluate("navigator.plugins === navigator.plugins")
        assert navigator._features.cached_names == {
            "connection", "plugins", "mimeTypes",
        }

        assert page.evaluate("navigator.webkitPersistentStorage === navigator.webkitPersistentStorage")
        assert "webkitPersistentStorage" in navigator._features.cached_names
        assert "webkitTemporaryStorage" not in navigator._features.cached_names
        assert "battery" not in navigator._features.cached_names

        page.evaluate("navigator.getBattery()")
        assert "battery" in navigator._features.cached_names
        assert "geolocation" not in navigator._features.cached_names

        assert page.evaluate("navigator.geolocation === navigator.geolocation")
        assert "geolocation" in navigator._features.cached_names
    finally:
        (await browser.close())


async def test_navigator_environment_records_validate_defaults():
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    environment = BrowserEnvironment()
    assert environment.app_version == environment.user_agent.removeprefix("Mozilla/")
    assert environment.webdriver is False
    # The shipped profile is the default source, so compare with it (like ``battery.level``
    # below) rather than pinning the reading one collection happened to make.
    assert environment.connection.effective_type == profile["connection.effective_type"]
    assert environment.connection.effective_type in ("slow-2g", "2g", "3g", "4g")
    assert environment.connection.downlink_max == float("inf")
    assert environment.battery.level == profile["battery.level"]
    assert environment.storage.persistent.usage == 0
    assert environment.storage.persistent.quota == 0

    assert environment.overlay({"user_agent": "Custom UA"}).app_version == "Custom UA"
    assert environment.overlay({
        "user_agent": "Custom UA", "app_version": "Explicit Version",
    }).app_version == "Explicit Version"
    with pytest.raises(ValueError):
        BrowserEnvironment({"connection.effective_type": "5g"})
    with pytest.raises(ValueError):
        BrowserEnvironment({"storage.persistent.usage": 2, "storage.persistent.quota": 1})
