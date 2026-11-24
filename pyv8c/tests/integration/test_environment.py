import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c import BrowserEnvironment
from pyv8c.async_api import Browser
from pyv8c.foundation.environment import PROFILE, profile_path


async def test_environment_defaults_and_mapping_overlay_are_immutable():
    default = BrowserEnvironment()
    assert default.user_agent.startswith("Mozilla/5.0 ")
    assert not default.user_agent.startswith("'")
    assert default.language == default.languages[0]
    assert default.languages
    assert default.viewport.width == 1904

    custom = default.overlay({"languages": ["zh-CN", "en"], "viewport.width": 390})
    assert custom.language == "zh-CN"
    assert custom.languages == ("zh-CN", "en")
    assert custom.viewport.width == 390
    assert custom.viewport.height == default.viewport.height
    assert custom.to_dict() == {"languages": ("zh-CN", "en"), "viewport.width": 390}
    with pytest.raises(AttributeError):
        custom.viewport = None
    with pytest.raises(ValueError):
        BrowserEnvironment({"geolocation.permission": "sometimes"})


async def test_an_unconfigured_browser_loads_the_windows_profile_whole():
    windows = BrowserEnvironment.from_file(PROFILE)
    browser = (await Browser(stub_logging="off", real_network=False).__aenter__())
    try:
        assert browser.environment.user_agent == windows.user_agent
        assert browser.environment.viewport.width == 1904
        assert browser.environment.platform == windows.platform
        assert browser.idl_path == "pyv8c:idl/idl.zip"
        assert browser.idl_info["idl"]["chromium_version"] == "154.0.8030.0"
        page = browser.new_profile().new_page()
        assert page.evaluate("innerWidth") == 1904
    finally:
        (await browser.close())


async def test_a_packaged_darwin_profile_path_selects_its_idl():
    from loguru import logger

    darwin = profile_path("darwin")
    host = BrowserEnvironment.from_file(darwin)
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), format="{message}")
    browser = (await Browser(
        environment=darwin, stub_logging="off", real_network=False,
    ).__aenter__())
    try:
        assert browser.environment.user_agent == host.user_agent
        assert browser.environment.platform == host.platform
        assert browser.idl_path == "pyv8c:idl/idl-158.0.8127.36.zip"
        assert browser.idl_info["idl"]["chromium_version"] == "158.0.8127.36"
        page = browser.new_profile().new_page()
        assert page.evaluate("innerWidth") == host.viewport.width
        assert not any("Window order" in item for item in messages)
    finally:
        (await browser.close())
        logger.remove(token)


async def test_a_partial_environment_keeps_the_windows_profile():
    windows = BrowserEnvironment()
    browser = (await Browser(
        environment={"viewport.width": 800}, stub_logging="off", real_network=False,
    ).__aenter__())
    try:
        assert browser.environment.viewport.width == 800
        assert browser.environment.user_agent == windows.user_agent
        assert browser.environment.platform == windows.platform
        assert browser.idl_path == "pyv8c:idl/idl.zip"
        assert browser.idl_info["idl"]["chromium_version"] == "154.0.8030.0"
        page = browser.new_profile().new_page()
        assert page.evaluate("[navigator.userAgent, innerWidth]") == [windows.user_agent, 800]
    finally:
        (await browser.close())


async def test_environment_drives_navigator_viewport_and_screen():
    environment = BrowserEnvironment({
        "user_agent": "Example UA",
        "platform": "Linux x86_64",
        "languages": ["zh-CN", "en"],
        "hardware_concurrency": 12,
        "device_memory": 4,
        "max_touch_points": 5,
        "viewport.width": 390,
        "viewport.height": 844,
        "viewport.device_scale_factor": 3,
        "screen.width": 1080,
        "screen.height": 2400,
        "screen.avail_width": 1080,
        "screen.avail_height": 2300,
    })
    browser = (await Browser(environment=environment).__aenter__())
    try:
        page = browser.new_profile().new_page()
        # The insets are whatever the profile collected: outer is inner plus those.
        assert page.evaluate("[navigator.userAgent, navigator.platform, navigator.language, navigator.languages, navigator.hardwareConcurrency, navigator.deviceMemory, navigator.maxTouchPoints, innerWidth, innerHeight, outerWidth, outerHeight, devicePixelRatio, screen.width, screen.availHeight]") == [
            "Example UA", "Linux x86_64", "zh-CN", ["zh-CN", "en"], 12, 4, 5,
            390, 844,
            390 + environment.viewport.outer_inset_width,
            844 + environment.viewport.outer_inset_height,
            3, 1080, 2300,
        ]
    finally:
        (await browser.close())


async def test_environment_drives_the_client_hints():
    browser = (await Browser(environment={
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                      " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "client_hints.platform_version": "6.5.0",
    }).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const data = navigator.userAgentData;
            return [data.platform, data.mobile,
                    data.brands.map(brand => brand.brand + '/' + brand.version)];
        })()""") == ["Linux", False,
                    ["Not:A-Brand/8", "Chromium/120", "Google Chrome/120"]]

        # The hints are derived from the user agent, and an explicit member wins.
        page.evaluate("""(() => {
            window.__hints = 'pending';
            navigator.userAgentData
                .getHighEntropyValues(['architecture', 'bitness', 'platformVersion',
                                       'uaFullVersion', 'formFactors'])
                .then(value => window.__hints = [value.architecture, value.bitness,
                                                 value.platformVersion, value.uaFullVersion,
                                                 value.formFactors],
                      error => window.__hints = error.name);
        })()""")
        (await page.run_until_idle(timeout=1))
        # The full version is not derivable from a user agent, so it stays the
        # profile's, while the platform the UA names is worked out again.
        assert page.evaluate("window.__hints") == [
            "x86", "64", "6.5.0",
            next(brand.version for brand in BrowserEnvironment().client_hints.full_version_list
                 if brand.brand == "Google Chrome"),
            ["Desktop"],
        ]
    finally:
        (await browser.close())


async def test_page_environment_override_and_iframe_inheritance():
    browser = (await Browser(
        environment={"user_agent": "Runtime UA"},
        resource_loader=lambda url, request: {"body": "<body>frame</body>", "url": url},
    ).__aenter__())
    try:
        context = browser.new_profile()
        first = context.new_page(environment={"user_agent": "Page UA", "viewport.width": 800})
        second = context.new_page()
        assert first.evaluate("[navigator.userAgent, innerWidth]") == ["Page UA", 800]
        assert second.evaluate("[navigator.userAgent, innerWidth]") == ["Runtime UA", 1904]
        iframe = first.document.fn_createElement("iframe")
        iframe.set_src("https://example.test/frame")
        child = first.frame_resources.load_iframe(iframe)
        assert child.window.navigator.get_userAgent() == "Page UA"
    finally:
        (await browser.close())


async def test_geolocation_is_async_and_clear_watch_cancels_pending_callback():
    browser = (await Browser(environment={
        "geolocation.permission": "granted",
        "geolocation.latitude": 31.2,
        "geolocation.longitude": 121.5,
        "geolocation.accuracy": 5,
    }).__aenter__())
    try:
        page = browser.new_profile().new_page()
        # The position callback is delivered on the page task queue, never
        # inline, so it is still pending when the requesting script ends.
        assert page.evaluate(
            "navigator.geolocation.getCurrentPosition(p => globalThis.geo = [p.coords.latitude, p.coords.longitude, p.coords.accuracy]); typeof geo"
        ) == "undefined"
        (await page.run_until_idle())
        assert page.evaluate("geo") == [31.2, 121.5, 5]

        assert page.evaluate("(() => { globalThis.cancelled = 0; const id = navigator.geolocation.watchPosition(() => cancelled++); navigator.geolocation.clearWatch(id); return id; })()") == 1
        (await page.run_until_idle())
        assert page.evaluate("cancelled") == 0
    finally:
        (await browser.close())


@pytest.mark.parametrize(
    ("geolocation", "expected"),
    [
        ({"geolocation.permission": "denied"}, [1, "User denied Geolocation"]),
        ({"geolocation.permission": "granted"}, [2, "Unable to determine the position"]),
    ],
)
async def test_geolocation_reports_permission_and_position_errors(geolocation, expected):
    browser = (await Browser(environment=geolocation).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("navigator.geolocation.getCurrentPosition(() => globalThis.geoError = ['success'], error => globalThis.geoError = [error.code, error.message])")
        (await page.run_until_idle())
        assert page.evaluate("geoError") == expected
    finally:
        (await browser.close())


async def test_resource_loader_gets_environment_headers_and_explicit_headers_win():
    calls = []

    def loader(url, request):
        calls.append(request)
        return {"body": "ok", "url": url}

    browser = (await Browser(resource_loader=loader, environment={"user_agent": "Env UA", "languages": ["fr-FR", "en"]}).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto("https://example.test/")
        calls.clear()
        page.resource_fetcher.load_resource("/one")
        page.resource_fetcher.load_resource("/two", {"headers": {"user-agent": "Explicit UA", "ACCEPT-LANGUAGE": "de"}})
        assert calls[0]["headers"]["User-Agent"] == "Env UA"
        assert calls[0]["headers"]["Accept-Language"] == "fr-FR,en;q=0.9"
        assert calls[0]["headers"]["Accept"] == "*/*"
        assert calls[0]["headers"]["Sec-Fetch-Site"] == "same-origin"
        assert calls[0]["headers"]["Referer"] == "https://example.test/"
        assert calls[1]["headers"]["user-agent"] == "Explicit UA"
        assert calls[1]["headers"]["ACCEPT-LANGUAGE"] == "de"
        assert calls[1]["headers"]["Accept"] == "*/*"
        assert calls[1]["headers"]["Sec-Fetch-Dest"] == "empty"
        assert "User-Agent" not in calls[1]["headers"]
    finally:
        (await browser.close())


async def test_resource_interceptor_filters_images_and_styles_and_cache_reuses_loader(tmp_path):
    calls = []

    def loader(url, request):
        calls.append((url, request.get("resource_type")))
        return {"body": "cached", "url": url, "headers": {"content-type": "text/javascript"}}

    def interceptor(url, request):
        if request.get("resource_type") in {"image", "style"}:
            return False

    browser = (await Browser(resource_loader=loader, resource_interceptor=interceptor, cache_dir=tmp_path / "cache").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body><img src='/a.png'><link rel='stylesheet' href='/a.css'></body>", url="http://example.test/", wait_until="commit"))
        # Cache reuse starts after the first response is delivered. Concurrent
        # cache misses do not imply transport coalescing.
        page.evaluate("fetch('/app.js').then(r => r.text()).then(v => {window.__one=v; return fetch('/app.js')}).then(r => r.text()).then(v => window.__two=v)")
        (await page.wait_for("window.__one === 'cached' && window.__two === 'cached'", timeout=2))
        assert page.evaluate("[__one, __two]") == ["cached", "cached"]
        assert [kind for _url, kind in calls].count("fetch") == 1
        assert all(kind not in {"image", "style"} for _url, kind in calls)
    finally:
        (await browser.close())


async def test_every_request_a_page_makes_names_its_resource_type():
    """A caller filters on ``resource_type``, so each path names itself: the element
    ones, the API ones (``fetch``, ``xhr``), module imports and worker sources."""
    kinds = {}
    body = """<body>
<img src="/a.png"><link rel="stylesheet" href="/app.css">
<iframe src="/frame.html"></iframe>
<script src="/classic.js"></script>
<script type="module">import '/module.js';</script>
<script>
  new Worker('/worker.js');
  fetch('/page-fetch');
  var xhr = new XMLHttpRequest(); xhr.open('GET', '/page-xhr', true); xhr.send();
</script>
</body>"""

    def loader(url, request):
        kinds.setdefault(request.get("resource_type"), []).append(str(url))
        name = str(url).rsplit("/", 1)[-1]
        if name.endswith(".css"):
            return {"body": "body{}", "url": url, "headers": {"content-type": "text/css"}}
        if name.endswith(".js"):
            return {"body": "0;", "url": url,
                    "headers": {"content-type": "text/javascript"}}
        if name.endswith(".html"):
            return {"body": "<body>frame</body>", "url": url,
                    "headers": {"content-type": "text/html"}}
        return {"body": body, "url": url, "headers": {"content-type": "text/html"}}

    browser = (await Browser(resource_loader=loader, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto("http://example.test/"))
        (await page.run_until_idle(timeout=3))
        (await page.net.fetch("/host-fetch"))
        (await page.net.xhr("/host-xhr"))
        (await page.run_until_idle(timeout=3))
    finally:
        (await browser.close())

    assert None not in kinds
    assert {"document", "script", "image", "style", "iframe", "fetch", "xhr"} <= set(kinds)


async def test_resource_cache_persists_across_browser_restart(tmp_path, monkeypatch):
    cache_dir = tmp_path / "resource-cache"
    first_calls = []

    def first_loader(url, request):
        first_calls.append(url)
        return {"body": "persisted-js", "url": url, "headers": {"content-type": "text/javascript"}}

    browser = (await Browser(resource_loader=first_loader, cache_dir=cache_dir).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://cache.test/", wait_until="commit"))
        assert page.resource_fetcher.load_resource("/app.js").body_text() == "persisted-js"
        assert first_calls == ["http://cache.test/app.js"]
        assert len(list((cache_dir / "cache.test").glob("*.json"))) == 1
    finally:
        (await browser.close())

    def unavailable_loader(url, request):
        raise AssertionError("persistent cache was not used")

    messages = []
    monkeypatch.setattr(
        "pyv8c.platform.resources.cache.logger.debug",
        lambda message, *values: messages.append((message, *values)),
    )
    browser = (await Browser(resource_loader=unavailable_loader, cache_dir=cache_dir).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://cache.test/", wait_until="commit"))
        assert page.resource_fetcher.load_resource("/app.js").body_text() == "persisted-js"
        assert ("resource cache hit: {}", "http://cache.test/app.js") in messages
    finally:
        (await browser.close())


async def test_resource_cache_hot_hit_skips_the_loader_pool_across_pages(tmp_path, monkeypatch):
    def load(url, options):
        return {"body": "cached", "headers": {"content-type": "text/plain"}}

    async with Browser(real_network=False, resource_loader=load, cache_dir=tmp_path) as browser:
        profile = browser.new_profile()
        first, second = profile.new_page(), profile.new_page()
        for page in (first, second):
            await page.set_content("<body></body>", url="http://cache.test/", wait_until="commit")
        assert (await first.net.fetch("/cached")).text == "cached"

        def reject_disk(*args):
            pytest.fail("hot cache entry should be served without disk I/O")

        monkeypatch.setattr(profile.resource_cache, "get", reject_disk)
        assert (await second.net.fetch("/cached")).text == "cached"


async def test_worker_inherits_page_environment_snapshot():
    source = "self.onmessage = () => self.postMessage([navigator.userAgent, navigator.language, navigator.maxTouchPoints]);"
    browser = (await Browser(
        resource_loader=lambda url, request: {"body": source, "url": url},
        environment={"user_agent": "Runtime UA", "languages": ["en-US"], "max_touch_points": 0},
    ).__aenter__())
    try:
        page = browser.new_profile().new_page(environment={"user_agent": "Worker UA", "languages": ["ja-JP"], "max_touch_points": 2})
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        page.evaluate("const worker = new Worker('https://example.test/worker.js'); worker.onmessage = event => globalThis.workerEnvironment = event.data; worker.postMessage(null);")
        (await page.wait_for("typeof workerEnvironment !== 'undefined'", timeout=10))
        assert page.evaluate("workerEnvironment") == ["Worker UA", "ja-JP", 2]
    finally:
        (await browser.close())


async def test_worker_navigator_does_not_expose_window_only_surfaces():
    source = "self.postMessage([typeof navigator.plugins, typeof navigator.mimeTypes, typeof navigator.geolocation]);"
    browser = (await Browser(resource_loader=lambda url, request: {"body": source, "url": url}).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        page.evaluate("const worker = new Worker('https://example.test/worker.js'); worker.onmessage = event => globalThis.workerSurfaces = event.data;")
        (await page.wait_for("typeof workerSurfaces !== 'undefined'", timeout=10))
        assert page.evaluate("workerSurfaces") == ["undefined", "undefined", "undefined"]
    finally:
        (await browser.close())


async def test_fetch_and_xhr_receive_environment_headers():
    calls = []

    def loader(url, request):
        calls.append((url, request["headers"]))
        return {"body": "ok", "url": url, "headers": {"access-control-allow-origin": "*"}}

    browser = (await Browser(resource_loader=loader, environment={"user_agent": "Async UA", "languages": ["ko-KR", "en"]}).__aenter__())
    try:
        page = browser.new_profile().new_page()
        await page.goto("https://example.test/")
        calls.clear()
        page.evaluate("fetch('/fetch').then(() => globalThis.fetchDone = true)")
        page.evaluate("const xhr = new XMLHttpRequest(); xhr.open('GET', '/xhr'); xhr.onload = () => globalThis.xhrDone = true; xhr.send()")
        (await page.run_until_idle(1))
        assert page.evaluate("[fetchDone, xhrDone]") == [True, True]
        # fetch and XHR are one destination on the wire: the browser set, identical.
        assert [headers for _, headers in calls][0] == [headers for _, headers in calls][1]
        for _url, headers in calls:
            assert headers["User-Agent"] == "Async UA"
            assert headers["Accept-Language"] == "ko-KR,en;q=0.9"
            assert headers["Sec-Fetch-Mode"] == "cors"
            assert headers["Sec-Fetch-Dest"] == "empty"
            assert headers["Referer"] == "https://example.test/"
    finally:
        (await browser.close())


async def test_fetch_blob_url_uses_context_storage_async():
    browser = (await Browser(environment={"user_agent": "Blob UA"}).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate("(() => { const u = URL.createObjectURL(new Blob(['blob-body'])); fetch(u).then(r => r.text()).then(v => globalThis.blobFetch = v); return u; })()").startswith("blob:")
        (await page.run_until_idle(1))
        assert page.evaluate("blobFetch") == "blob-body"
    finally:
        (await browser.close())


@pytest.mark.parametrize("headers", [{"Cache-Control":"no-store"}, {"Cache-Control":"no-cache"},
                                     {"Cache-Control":"private"}, {"Vary":"*"}, {"Set-Cookie":"a=1"}])
async def test_persistent_resource_cache_honours_response_exclusions(tmp_path, headers):
    calls = []
    def loader(url, request):
        calls.append(url)
        return {"body": str(len(calls)), "headers": headers}
    browser = (await Browser(real_network=False, stub_logging="off", resource_loader=loader, cache_dir=tmp_path).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://cache.test/', wait_until="commit"))
        assert page.resource_fetcher.load_resource('/data').body_text() == '1'
        assert page.resource_fetcher.load_resource('/data').body_text() == '2'
        assert not list(tmp_path.rglob('*.json'))
    finally:
        (await browser.close())


async def test_persistent_cache_partitions_request_headers_and_bypasses_credentials(tmp_path):
    calls = []
    def loader(url, request):
        calls.append(request)
        return {"body": request['headers'].get('Accept-Language', '')}
    browser = (await Browser(real_network=False, stub_logging="off", resource_loader=loader, cache_dir=tmp_path).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='http://cache.test/', wait_until="commit"))
        for language in ['en', 'ja', 'en']:
            assert page.resource_fetcher.load_resource('/data', {'headers':{'Accept-Language':language}}).body_text() == language
        assert len(calls) == 2
        for _ in range(2):
            page.resource_fetcher.load_resource('/private', {'headers':{'Authorization':'Bearer fixture'}})
        assert len(calls) == 4
        page.profile.cookies.add_from_response('http://cache.test/', 'session=fixture; Path=/')
        for _ in range(2):
            page.resource_fetcher.load_resource('/data', {'headers':{'Accept-Language':'en'}})
        assert len(calls) == 6
    finally:
        (await browser.close())


_CLOCK = """
(() => {
    const winter = new Date(Date.UTC(2026, 0, 15, 12));
    const summer = new Date(Date.UTC(2026, 6, 15, 12));
    return [winter.getTimezoneOffset(), winter.toString(), summer.getTimezoneOffset(),
            Intl.DateTimeFormat().resolvedOptions().timeZone,
            Intl.DateTimeFormat().resolvedOptions().locale];
})()
"""


async def test_two_browsers_keep_independent_time_zones_and_languages():
    first = (await Browser(
        environment={"timezone": "Asia/Shanghai", "languages": ["zh-CN", "zh"]},
        stub_logging="off",
        real_network=False,
    ).__aenter__())
    second = (await Browser(
        environment={"timezone": "America/New_York", "languages": ["en-US", "en"]},
        stub_logging="off",
        real_network=False,
    ).__aenter__())
    try:
        page_a = first.new_profile().new_page()
        page_b = second.new_profile().new_page()
        (await page_a.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        (await page_b.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        assert page_a.evaluate(_CLOCK) == [
            -480, "Thu Jan 15 2026 20:00:00 GMT+0800 (中国标准时间)", -480,
            "Asia/Shanghai", "zh-CN",
        ]
        assert page_b.evaluate(_CLOCK) == [
            300, "Thu Jan 15 2026 07:00:00 GMT-0500 (Eastern Standard Time)", 240,
            "America/New_York", "en-US",
        ]
        page_b.evaluate("""(() => {
            window.__worker = 'pending';
            const source = new Blob([
                "const winter = new Date(Date.UTC(2026, 0, 15, 12));" +
                "postMessage(JSON.stringify([winter.getTimezoneOffset(), winter.toString()," +
                " Intl.DateTimeFormat().resolvedOptions().locale]));"
            ], {type: 'text/javascript'});
            const worker = new Worker(URL.createObjectURL(source));
            worker.onmessage = event => window.__worker = event.data;
            worker.onerror = event => window.__worker = 'error:' + event.message;
        })()""")
        (await page_b.wait_for("window.__worker !== 'pending'", timeout=10))
        assert page_b.evaluate("window.__worker") == (
            '[300,"Thu Jan 15 2026 07:00:00 GMT-0500 (Eastern Standard Time)","en-US"]'
        )
        assert page_a.evaluate(_CLOCK)[0] == -480
        assert page_a.evaluate("Intl.DateTimeFormat().resolvedOptions().locale") == "zh-CN"
    finally:
        (await second.close())
        (await first.close())


async def test_invalid_page_timezone_is_rejected():
    browser = (await Browser(stub_logging="off", real_network=False).__aenter__())
    try:
        with pytest.raises(ValueError, match="unknown time zone"):
            browser.new_profile().new_page(environment={"timezone": "Not/AZone"})
    finally:
        (await browser.close())
