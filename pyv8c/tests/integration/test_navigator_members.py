"""The Navigator sub-objects: when they exist, and what they answer."""

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c.async_api import Browser

MEMBERS = ("mediaDevices", "permissions", "mediaSession", "serviceWorker", "credentials",
           "bluetooth", "storage")


async def _settled(page, expression, *, name="__settled"):
    """What *expression*'s promise settles as: a value, or an error's name."""
    page.evaluate(
        "(() => { Promise.resolve(" + expression + ").then("
        f"(value) => {{ window.{name} = ['value', value]; }},"
        f"(error) => {{ window.{name} = ['error', error.name, error.message]; }}); }})()")
    (await page.run_until_idle(timeout=2))
    return page.evaluate(f"window.{name}")


async def test_each_member_answers_by_secure_context():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        assert page.evaluate("[" + ",".join(
            f"typeof navigator.{name}" for name in MEMBERS) + ", navigator.javaEnabled()]") == [
            "object", "object", "object", "object", "object", "object", "object", False]
        (await page.set_content("<body></body>", url="http://insecure.test/", wait_until="commit"))
        assert page.evaluate("[" + ",".join(
            f"typeof navigator.{name}" for name in MEMBERS) + ", navigator.javaEnabled()]") == [
            "undefined", "object", "object", "undefined", "undefined", "undefined", "undefined",
            False]
    finally:
        (await browser.close())


async def test_each_member_is_the_same_object_twice():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        assert page.evaluate("[" + ",".join(
            f"navigator.{name} === navigator.{name}" for name in MEMBERS) + "]") == [True] * 7
    finally:
        (await browser.close())


async def test_a_worker_gets_only_the_members_chrome_gives_it():
    source = ("self.postMessage([typeof navigator.permissions, typeof navigator.storage,"
              " typeof navigator.serviceWorker, typeof navigator.mediaDevices,"
              " typeof navigator.mediaSession, typeof navigator.credentials,"
              " typeof navigator.bluetooth, typeof navigator.javaEnabled]);")
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        page.evaluate("""(() => {
            const source = new Blob([%s], {type: 'text/javascript'});
            const worker = new Worker(URL.createObjectURL(source));
            worker.onmessage = (event) => { window.__workerMembers = event.data; };
        })()""" % __import__("json").dumps(source))
        (await page.wait_for("typeof window.__workerMembers !== 'undefined'", timeout=10))
        assert page.evaluate("__workerMembers") == [
            "object", "object", "undefined", "undefined", "undefined", "undefined", "undefined",
            # Chrome's WorkerNavigator has no javaEnabled at all; a member declared
            # on the shared prototype cannot be taken away from it.
            "function"]
    finally:
        (await browser.close())


async def test_the_environment_answers_bluetooth_and_the_geolocation_permission():
    browser = (await Browser(environment={"bluetooth.available": False,
                                   "geolocation.permission": "granted"},
                      stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        assert (await _settled(page, "navigator.bluetooth.getAvailability()")) == ["value", False]
        assert (await _settled(page, "navigator.permissions.query({name: 'geolocation'})"
                              ".then((status) => [status.name, status.state])")) == [
            "value", ["geolocation", "granted"]]
    finally:
        (await browser.close())


@pytest.mark.parametrize('configured,expected', [
    (None, 'prompt'), ('prompt', 'prompt'), ('denied', 'denied'), ('granted', 'granted'),
])
async def test_geolocation_permission_query_reflects_configured_state(configured, expected):
    environment = {} if configured is None else {'geolocation.permission': configured}
    browser = (await Browser(real_network=False, environment=environment, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://secure.test/', wait_until="commit"))
        assert (await _settled(page, "navigator.permissions.query({name:'geolocation'}).then(s => s.state)")) == [
            'value', expected]
    finally:
        (await browser.close())


async def test_empty_permission_profile_does_not_restore_default_grants_or_names():
    browser = (await Browser(real_network=False, environment={'permissions.granted': [],
                      'permissions.spec_names': {}}, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://secure.test/', wait_until="commit"))
        assert (await _settled(page, "navigator.permissions.query({name:'background-sync'})"
                        ".then(s => [s.name, s.state])")) == ['value', ['background-sync', 'prompt']]
    finally:
        (await browser.close())

    browser = (await Browser(real_network=False, environment={'permissions.names': []}, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://secure.test/', wait_until="commit"))
        assert (await _settled(page, "navigator.permissions.query({name:'camera'})"))[1] == 'TypeError'
    finally:
        (await browser.close())


async def test_top_level_storage_permission_validates_requested_origin():
    browser = (await Browser(real_network=False, environment={
        'permissions.names': ['top-level-storage-access'],
        'permissions.granted': [], 'permissions.spec_names': {},
    }, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://secure.test/', wait_until="commit"))
        for origin in (None, "'not a URL'", "'https://example.test:99999'"):
            extra = '' if origin is None else ', requestedOrigin:' + origin
            result = (await _settled(page, "navigator.permissions.query({name:'top-level-storage-access'" + extra + "})"))
            assert result == ['error', 'TypeError',
                              "Failed to execute 'query' on 'Permissions': The requested origin is invalid."]
        for origin, state in [('https://other.test/path', 'prompt'), ('http://other.test', 'denied'),
                              ('http://localhost', 'prompt'), ('data:text/plain,a', 'denied'),
                              ('file:///tmp/a', 'prompt'), ('blob:https://other.test/a', 'prompt')]:
            import json
            result = (await _settled(page, "navigator.permissions.query({name:'top-level-storage-access',requestedOrigin:" +
                              json.dumps(origin) + "}).then(s => s.state)"))
            assert result == ['value', state]
        page.evaluate('window.__reads = []')
        result = (await _settled(page, "navigator.permissions.query({get name(){__reads.push('name');return 'top-level-storage-access'},"
                          "get requestedOrigin(){__reads.push('requestedOrigin');return 'https://other.test'}}).then(s => s.state)"))
        assert result == ['value', 'prompt']
        assert page.evaluate('__reads') == ['name', 'name', 'requestedOrigin']
    finally:
        (await browser.close())


async def test_permissions_query_validation_rejects_a_promise_like_chrome():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        cases = (
            ("", "1 argument required, but only 0 present."),
            ("null", "parameter 1 is not of type 'object'."),
            ("{}", "Failed to read the 'name' property from 'PermissionDescriptor': "
                   "Required member is undefined."),
            ("1", "parameter 1 is not of type 'object'."),
        )
        for index, (argument, message) in enumerate(cases):
            expression = f"navigator.permissions.query({argument})"
            assert page.evaluate(
                "(() => { try { const pending = " + expression + ";"
                "return [pending instanceof Promise, typeof pending.then]; }"
                "catch (error) { return [false, error.name]; } })()"
            ) == [True, "function"]
            result = (await _settled(page, expression, name=f"__permissionValidation{index}"))
            assert result == ["error", "TypeError",
                              "Failed to execute 'query' on 'Permissions': " + message]
        assert (await _settled(page, "navigator.permissions.query({name: Symbol()})",
                        name="__permissionSymbol")) == [
            "error", "TypeError",
            "Failed to execute 'query' on 'Permissions': Failed to read the 'name' "
            "property from 'PermissionDescriptor': Cannot convert a Symbol value to a string"]
        assert (await _settled(page, "navigator.permissions.query({get name() {"
                        " throw new Error('sentinel'); }})",
                        name="__permissionGetter")) == ["error", "Error", "sentinel"]
    finally:
        (await browser.close())


async def test_frame_permissions_differ_from_top_level_like_chrome():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://secure.test/', wait_until="commit"))
        page.evaluate("window.__frame = document.createElement('iframe'); document.body.appendChild(__frame)")
        for name in ('background-fetch', 'storage-access'):
            assert (await _settled(page, f"navigator.permissions.query({{name: '{name}'}}).then(s => s.state)")) == [
                'value', 'granted']
            assert (await _settled(page, f"__frame.contentWindow.navigator.permissions.query({{name: '{name}'}})"
                            ".then(s => s.state)")) == ['value', 'prompt']
    finally:
        (await browser.close())


async def test_push_permission_converts_the_derived_descriptor_like_chrome():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        for value in ("true", "1", "'false'"):
            assert (await _settled(page, "navigator.permissions.query({name: 'push', "
                            f"userVisibleOnly: {value}" + "}).then(s => [s.name, s.state])")) == [
                "value", ["notifications", "prompt"]]
        for value in ("false", "undefined", "null"):
            assert (await _settled(page, "navigator.permissions.query({name: 'push', "
                            f"userVisibleOnly: {value}" + "})"))[1] == "NotSupportedError"
        page.evaluate("window.__permissionReads = []")
        assert (await _settled(page, "navigator.permissions.query({"
                        "get name() { __permissionReads.push('name'); return 'push'; },"
                        "get userVisibleOnly() { __permissionReads.push('userVisibleOnly'); return true; }"
                        "}).then(s => s.name)")) == ["value", "notifications"]
        assert page.evaluate("__permissionReads") == ["name", "name", "userVisibleOnly"]
        assert (await _settled(page, "(() => { let i = 0; return navigator.permissions.query({"
                        "get name() { return i++ ? 'unknown' : 'push'; },"
                        "get userVisibleOnly() { throw new Error('must not read'); } }); })()"))[:2] == [
            "error", "TypeError"]
    finally:
        (await browser.close())


@pytest.mark.parametrize('first', ['clipboard-read', 'clipboard-write'])
async def test_clipboard_permission_descriptor_and_shared_status(first):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://secure.test/', wait_until="commit"))
        assert (await _settled(page, "navigator.permissions.query({name: 'clipboard-write'})"
                        ".then(s => [s.name, s.state])")) == ['value', ['clipboard_write', 'granted']]
        expression = ("navigator.permissions.query({name: '" + first + "', allowWithoutGesture: true})"
                      ".then(s => [s.name, s.state])")
        expected_name = 'clipboard_read' if first == 'clipboard-read' else 'clipboard_write'
        assert (await _settled(page, expression)) == ['value', [expected_name, 'prompt']]
        other = 'clipboard-write' if first == 'clipboard-read' else 'clipboard-read'
        expression = ("navigator.permissions.query({name: '" + other + "', allowWithoutGesture: true})"
                      ".then(s => [s.name, s.state])")
        assert (await _settled(page, expression)) == ['value', [expected_name, 'prompt']]
        page.evaluate('window.__clipboardReads = []')
        assert (await _settled(page, "navigator.permissions.query({"
                        "get name() { __clipboardReads.push('name'); return 'clipboard-write'; },"
                        "get allowWithoutGesture() { __clipboardReads.push('allowWithoutGesture'); return true; }"
                        "}).then(s => s.state)")) == ['value', 'prompt']
        assert page.evaluate('__clipboardReads') == ['name', 'name', 'allowWithoutGesture']
    finally:
        (await browser.close())


async def test_the_calls_that_need_a_chooser_or_a_registration_refuse():
    def loader(url, _request):
        return {"body": "self.onmessage = () => {};", "url": url,
                "headers": {"content-type": "text/javascript"}}

    browser = (await Browser(resource_loader=loader, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        assert (await _settled(page, "navigator.mediaDevices.getDisplayMedia({video: true})")) == [
            "error", "NotAllowedError", "Permission denied"]
        assert (await _settled(page, "navigator.bluetooth.requestDevice({acceptAllDevices: true})")) == [
            "error", "NotFoundError", "User cancelled the requestDevice() chooser."]
        assert (await _settled(page, "navigator.serviceWorker.register('/worker.js')")) == [
            "error", "NotSupportedError", "Service workers are not implemented here."]
        assert (await _settled(page, "navigator.serviceWorker.register('http://other.test/sw.js')")) == [
            "error", "SecurityError",
            "Failed to register a ServiceWorker: The origin of the provided scriptURL "
            "('http://other.test') does not match the current origin ('https://secure.test')."]
    finally:
        (await browser.close())


async def test_a_registration_whose_script_cannot_be_fetched_rejects():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        settled = (await _settled(page, "navigator.serviceWorker.register('/sw.js')"))
        assert settled[0] == "error"
        assert settled[1] == "TypeError"
    finally:
        (await browser.close())
