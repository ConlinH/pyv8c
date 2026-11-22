"""Exercise the installed wheels outside the source tree."""

from pathlib import Path
from importlib import metadata
import importlib
import sys

from pyv8c import pyv8
from pyv8c.async_api import Browser, run
from pyv8c.engine import binder
from pyv8c._native import audio, dom


class Global:
    __v8_idl_spec__ = pyv8.IdlSpec(global_this=True)


assert (Path(pyv8.__file__).parent / "icudtl.dat").is_file()
assert importlib.import_module('pyv8c.pyv8') is pyv8 is binder.pyv8
native_dir = Path(pyv8.__file__).parents[1]
binary = importlib.import_module("pyv8c._native._native")
assert Path(binary.__file__).parent == native_dir / "_native"
assert {path for path in native_dir.rglob("*") if path.suffix in {".pyd", ".so"}} == {
    Path(binary.__file__), Path(pyv8._pyv8.__file__),
}
assert audio is binary.audio and dom is binary.dom
assert abs(audio.oscillator_waveform(3, 123 / 4096, 10, 10, 0) - 0.1201339364) < 2e-6
compressor = audio.CompressorDSP(44100)
assert compressor.process(0.5, (-50, 40, 12, 0, 0.25)) == 0.0
assert compressor.frame == 1
assert 'pyv8' not in sys.modules
assert not any(item.lower().startswith("pyv8<") or item.lower().startswith("pyv8>") or item.lower() == "pyv8" for item in metadata.requires("pyv8c") or ())
from pyv8c.pyv8.devtools import DevToolsOptions
assert DevToolsOptions
context = pyv8.Context(Global(), trace=True)
try:
    assert context.exec_js("new Intl.NumberFormat('en-US').format(1234)") == "1,234"
    try:
        context.exec_js("throw new TypeError('wheel smoke')")
    except pyv8.JSException as error:
        assert 'wheel smoke' in str(error)
    else:
        raise AssertionError('JavaScript TypeError was not propagated')
finally:
    context.close()

async def check_runtime():
    async with Browser(real_network=False) as runtime:
        page = runtime.new_page()
        assert page.evaluate("[1, 2, 3].reduce((a, b) => a + b, 0)") == 6
        page.evaluate("globalThis.timerDone=false;setTimeout(()=>timerDone=true,1)")
        await page.wait_for("timerDone", timeout=2)


run(check_runtime())

from pyv8c.sync_api import Browser as SyncBrowser

with SyncBrowser(real_network=False) as browser:
    with browser.new_profile() as profile:
        with profile.new_page() as page:
            page.set_content("<title>sync wheel</title><body><p>native DOM</p></body>")
            page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,1)")
            page.wait_for("ready", timeout=2)
            assert page.evaluate("document.title") == "sync wheel"
            assert page.evaluate("document.body.firstChild.textContent") == "native DOM"

print(f"Wheel smoke test passed: {pyv8.__file__}")
