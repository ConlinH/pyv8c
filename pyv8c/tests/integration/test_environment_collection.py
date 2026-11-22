"""Collecting a profile from a browser that is actually installed."""
import json
from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c import BrowserEnvironment
from pyv8c.async_api import Browser
from pyv8c.engine.idl import ArrayProto, Idl  # noqa: E402
from pyv8c.foundation.environment import COLLECTED_KEYS  # noqa: E402
from pyv8c.foundation.system_fonts import records as font_records  # noqa: E402
from pyv8c.tools import collect_environment as collector  # noqa: E402

# Both cases drive the installed Chrome through CDP.
pytestmark = pytest.mark.slow


def chrome_or_skip():
    try:
        return collector.find_chrome()
    except collector.CollectionError as missing:
        pytest.skip(str(missing))


async def test_a_collected_profile_drives_the_environment_it_was_read_from(tmp_path, capsys):
    path = tmp_path / "profile.json"
    assert collector.main(["--chrome", str(chrome_or_skip()), "--out", str(path)]) == 0
    assert "wrote" in capsys.readouterr().err

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["source"]["browser"].startswith(("Chrome", "Chromium", "HeadlessChrome"))
    assert set(document) == {"schema", "source", "bindings", "fonts.records", *COLLECTED_KEYS}
    # The collector serializes the host font table beside Chrome's collected
    # values. Check every field and portable file name, not merely its presence.
    expected_fonts = [{**asdict(face), "path": Path(face.path).name} for face in font_records()]
    assert document["fonts.records"] == expected_fonts
    # The same session that answered the keys named what it installs on ``window``.
    assert "Object" in document["bindings"]["own_order"]
    assert "innerWidth" in document["bindings"]["keys"]
    # 鈥nd which of the members the baseline declares it leaves out.
    surface = document["bindings"]
    assert "exposed" not in surface
    assert len(surface["own_order"]) == len(set(surface["own_order"]))
    assert surface["own_order"].index("Object") < surface["own_order"].index("Function")
    gaps = surface["omits"]["prototype"]
    assert len(gaps) > 20
    assert "computedRole" in gaps["Element"]
    assert "slot" not in gaps["Element"]
    # The interface objects are read too: ``CSS.animationWorklet`` lives there, not on a
    # prototype, and this reference browser does not install it.
    objects = surface["omits"]["interface"]
    assert objects, "no interface object was measured"
    assert "animationWorklet" in objects["CSS"]
    assert "supports" not in objects["CSS"]
    # 鈥nd the array word each interface is measured to have in this release.
    protos = surface["iteration_methods"]
    assert protos, "no array value was measured"
    assert protos["NodeList"] == ArrayProto.iterable
    assert protos["HTMLCollection"] == ArrayProto.iterator
    assert "Cache" not in protos, "Cache.keys is an async method, not an iterator member"
    assert surface["against"] == Idl.load().digest

    profile = BrowserEnvironment.from_file(path)
    collected = profile.resolved()
    assert [asdict(face) for face in profile.fonts.records] == expected_fonts
    browser = (await Browser(environment=profile, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://profile.test/", wait_until="commit"))
        assert page.evaluate("""[
            navigator.userAgent, navigator.appVersion, navigator.platform, navigator.vendor,
            navigator.language, navigator.languages, navigator.hardwareConcurrency,
            navigator.deviceMemory, navigator.maxTouchPoints, navigator.onLine,
            navigator.webdriver, innerWidth, innerHeight, devicePixelRatio,
            screen.width, screen.height, screen.availWidth, screen.availHeight,
            screen.colorDepth, screen.pixelDepth,
            navigator.connection.effectiveType, navigator.connection.rtt,
            navigator.connection.downlink, navigator.connection.saveData,
            navigator.userAgentData.platform,
            navigator.userAgentData.brands.map(brand => brand.brand + '/' + brand.version),
            navigator.plugins.length,
        ]""") == [
            collected["user_agent"], collected["app_version"], collected["platform"],
            collected["vendor"], collected["language"], list(collected["languages"]),
            collected["hardware_concurrency"], collected["device_memory"],
            collected["max_touch_points"], collected["on_line"], collected["webdriver"],
            collected["viewport.width"], collected["viewport.height"],
            collected["viewport.device_scale_factor"],
            collected["screen.width"], collected["screen.height"], collected["screen.avail_width"],
            collected["screen.avail_height"], collected["screen.color_depth"],
            collected["screen.pixel_depth"],
            collected["connection.effective_type"], collected["connection.rtt"],
            collected["connection.downlink"], collected["connection.save_data"],
            collected["client_hints.platform"],
            [f"{brand['brand']}/{brand['version']}" for brand in collected["client_hints.brands"]],
            len(collected["plugins"]),
        ]
    finally:
        (await browser.close())


async def test_the_high_entropy_hints_are_read_not_derived(tmp_path):
    path = tmp_path / "profile.json"
    assert collector.main(["--chrome", str(chrome_or_skip()), "--out", str(path),
                           "--timeout", "60"]) == 0
    profile = BrowserEnvironment.from_file(path)
    # The user agent alone cannot say these; the browser had to be asked.
    assert profile.client_hints.platform_version.split(".")[0].isdigit()
    assert profile.client_hints.bitness in ("32", "64")
    assert profile.client_hints.architecture in ("x86", "arm")
    assert [brand.version for brand in profile.client_hints.full_version_list][0].count(".") == 3
