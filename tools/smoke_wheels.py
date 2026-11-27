"""Exercise the installed wheels outside the source tree."""

from base64 import b64decode
from importlib import metadata
import importlib
from pathlib import Path
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
        await page.set_content('''<div style="display:grid;grid-template-columns:40px 60px">
            <span data-cell="one" style="height:12px"></span><span data-cell="two"></span>
        </div>''')
        assert page.evaluate("""(() => {
            const cells = document.querySelectorAll('[data-cell]');
            const first = cells.item(0), second = cells[1];
            const old = first.getBoundingClientRect();
            first.parentElement.style.gridTemplateColumns = '50px 60px';
            const fresh = first.getBoundingClientRect();
            return [cells instanceof NodeList, cells[0] === first,
                    old instanceof DOMRect, old.width, fresh.width,
                    second.getBoundingClientRect().left - fresh.left];
        })()""") == [True, True, True, 40, 50, 50]
        pixel, *urls = page.evaluate("""(() => {
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = 8;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = 'red';
            ctx.fillRect(0, 0, 8, 8);
            return [Array.from(ctx.getImageData(0, 0, 1, 1).data),
                    ...['image/png', 'image/jpeg', 'image/webp'].map(type => canvas.toDataURL(type))];
        })()""")
        assert pixel == [255, 0, 0, 255]
        for url, kind in zip(urls, ('image/png', 'image/jpeg', 'image/webp'), strict=True):
            header, payload = url.split(',', 1)
            assert header == f'data:{kind};base64'
            image = binary.RasterImage.decode(b64decode(payload, validate=True))
            assert image.size == (8, 8)
            red, green, blue, alpha = image.read(0, 0, 1, 1)
            assert red >= 250 and green <= 4 and blue <= 4 and alpha == 255
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
            assert page.evaluate("""(() => {
                const ctx = new OffscreenCanvas(1, 1).getContext('2d');
                ctx.fillStyle = 'blue';
                ctx.fillRect(0, 0, 1, 1);
                return Array.from(ctx.getImageData(0, 0, 1, 1).data);
            })()""") == [0, 0, 255, 255]

print(f"Wheel smoke test passed: {pyv8.__file__}")
