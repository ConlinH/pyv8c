"""Each collected fact reaches the page through the API that reports it.

No pump here: driving the task queue in this file perturbs the frame tests that run after it
(measured), so the facts that answer asynchronously are checked without a browser below.
"""
import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c.async_api import Browser
from pyv8c.foundation.environment import BrowserEnvironment  # noqa: E402
from pyv8c.web.bom.accessories import _SPEECH_VOICES, _voice_entries  # noqa: E402

PAGE = "<!doctype html><body>environment</body>"


async def test_each_collected_fact_reaches_the_page():
    """Overriding one key has to move the answer a page reads, or the key is decorative."""
    browser = (await Browser(environment={
        "viewport.outer_inset_width": 4,
        "viewport.outer_inset_height": 30,
        "media.constraints": ["width", "height"],
        "gpu.wgsl_features": ["subgroup_id"],
    }, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://environment.test/", wait_until="commit"))
        assert page.evaluate("""(() => ({
            inset: [outerWidth - innerWidth, outerHeight - innerHeight],
            constraints: Object.keys(navigator.mediaDevices.getSupportedConstraints()),
            wgsl: navigator.gpu ? [navigator.gpu.wgslLanguageFeatures.size,
                                    Array.from(navigator.gpu.wgslLanguageFeatures)[0]] : null,
        }))()""") == {
            "inset": [4, 30],
            "constraints": ["width", "height"],
            "wgsl": [1, "subgroup_id"],
        }
    finally:
        (await browser.close())


async def test_the_voices_are_the_collected_ones_and_the_table_without_a_profile():
    class _Realm:
        environment = BrowserEnvironment({
            "speech.voices": [{"name": "Test Voice", "lang": "en-GB", "local_service": True}]})

    class _Services:
        realm_services = _Realm()

    assert _voice_entries(_Services()) == (("Test Voice", "en-GB", True, False),)
    assert _voice_entries(None) == _SPEECH_VOICES
    assert _voice_entries(_Services()) != _SPEECH_VOICES, "the machine's voices are not this table"


async def test_window_screen_position_is_configurable():
    browser = (await Browser(environment={'viewport.screen_x': -25, 'viewport.screen_y': 10}, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url='https://environment.test/', wait_until="commit"))
        assert page.evaluate('[screenX, screenY, screenLeft, screenTop]') == [-25, 10, -25, 10]
    finally:
        (await browser.close())


async def test_configured_window_position_offsets_mouse_screen_coordinates():
    browser = (await Browser(environment={'viewport.screen_x': -25, 'viewport.screen_y': 10}, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body style="margin:0; width:200px; height:200px"></body>',
                         url='https://environment.test/', wait_until="commit"))
        page.evaluate("window.coords=[];addEventListener('mousemove', e => coords.push([e.clientX,e.clientY,e.screenX,e.screenY]))")
        (await page.mouse.move(30, 40))
        assert page.evaluate('coords') == [[30, 40, 5, 50]]
    finally:
        (await browser.close())
