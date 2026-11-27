"""``OffscreenCanvas`` hands out a working 2D surface with no element behind it."""
from loguru import logger

from pyv8c.async_api import Browser


async def test_bitmap_uses_live_image_data_and_offscreen_webgl_encodes():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate('''(() => {
          const c = document.createElement('canvas'); c.width = c.height = 1;
          const ctx = c.getContext('2d'); const data = ctx.createImageData(1, 1);
          data.data.set([255, 0, 0, 255]); window.bitmapResult = 'pending';
          createImageBitmap(data).then(bitmap => {
            ctx.drawImage(bitmap, 0, 0);
            window.bitmapResult = Array.from(ctx.getImageData(0, 0, 1, 1).data);
          });
          const offscreen = new OffscreenCanvas(1, 1);
          const gl = offscreen.getContext('webgl'); gl.clearColor(1, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
          window.blobResult = 'pending';
          offscreen.convertToBlob().then(blob => window.blobResult = [blob.type, blob.size > 0]);
          window.emptyResult = 'pending';
          new OffscreenCanvas(0, 1).convertToBlob().catch(e => window.emptyResult = e.name);
        })()''')
        (await page.run_until_idle(timeout=2))
        assert page.evaluate('window.bitmapResult') == [255, 0, 0, 255]
        assert page.evaluate('window.blobResult') == ['image/png', True]
        assert page.evaluate('window.emptyResult') == 'IndexSizeError'
    finally:
        (await browser.close())


async def test_an_offscreen_canvas_draws_and_reports_its_pixels():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        value = page.evaluate("""(() => {
            const canvas = new OffscreenCanvas(8, 6);
            const context = canvas.getContext('2d');
            context.fillStyle = '#ff0000';
            context.fillRect(0, 0, 4, 3);
            return [OffscreenCanvas.length, canvas.width, canvas.height,
                    canvas.constructor.name, canvas.getContext('2d') === context,
                    context instanceof OffscreenCanvasRenderingContext2D,
                    context.canvas === canvas, canvas.getContext('nope') === null,
                    typeof canvas.convertToBlob, typeof canvas.transferToImageBitmap,
                    typeof canvas.oncontextlost,
                    Array.from(context.getImageData(0, 0, 1, 1).data)];
        })()""")
    finally:
        (await browser.close())

    assert value == [2, 8, 6, "OffscreenCanvas", True, True, True, True, "function",
                     "function", "object", [255, 0, 0, 255]]


async def test_the_eight_members_the_log_reported_answer_like_chromium():
    """The members the case run reported as 待实现, with Chromium's values and refusals."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        value = page.evaluate("""(() => {
            const context = new OffscreenCanvas(8, 8).getContext('2d', {alpha: false});
            const attempt = (action) => {
                try { return 'ok:' + String(action()); }
                catch (error) { return error.name + ': ' + error.message; }
            };
            return [
                Object.keys(context.getContextAttributes()),
                context.getContextAttributes().alpha, context.getContextAttributes().colorSpace,
                context.globalCompositeOperation, context.filter,
                attempt(() => context.fill('bogus')),
                attempt(() => { context.fill(); return 'filled'; }),
                attempt(() => { context.closePath(); return 'closed'; }),
                attempt(() => { context.scale(Infinity, 1); return 'scaled'; }),
                attempt(() => context.createRadialGradient(0, 0, -1, 1, 1, 2)),
                attempt(() => String(context.createRadialGradient(0, 0, 0, NaN, 1, 2))),
                attempt(() => context.createRadialGradient(1, 1, 2, 1, 1, 2).constructor.name),
                attempt(() => context.drawImage()),
                attempt(() => { context.drawImage(new OffscreenCanvas(2, 2), 0, 0); return 'drawn'; }),
                attempt(() => context.drawImage({}, 0, 0)),
                attempt(() => { context.scale(2, 2); context.fillRect(0, 0, 1, 1); return 'scaled'; }),
            ];
        })()""")
    finally:
        (await browser.close())

    assert value == [
        ["alpha", "desynchronized", "colorSpace", "colorType", "toneMapping", "willReadFrequently"],
        False, "srgb", "source-over", "none",
        "TypeError: Failed to execute 'fill' on 'OffscreenCanvasRenderingContext2D':"
        " The provided value 'bogus' is not a valid enum value of type CanvasFillRule.",
        "ok:filled", "ok:closed", "ok:scaled",
        "IndexSizeError: Failed to execute 'createRadialGradient' on"
        " 'OffscreenCanvasRenderingContext2D': The r0 provided is less than 0.",
        "TypeError: Failed to execute 'createRadialGradient' on 'OffscreenCanvasRenderingContext2D':"
        " The provided double value is non-finite.", "ok:CanvasGradient",
        "TypeError: Failed to execute 'drawImage' on 'OffscreenCanvasRenderingContext2D':"
        " 3 arguments required, but only 0 present.",
        "ok:drawn",
        "TypeError: Failed to execute 'drawImage' on 'OffscreenCanvasRenderingContext2D':"
        " The provided value is not of type '(CSSImageValue or HTMLImageElement or"
        " SVGImageElement or HTMLVideoElement or HTMLCanvasElement or ImageBitmap or"
        " OffscreenCanvas or VideoFrame)'.",
        "ok:scaled",
    ]


async def test_a_worker_webgl_extension_uses_the_worker_realm():
    """A worker's WebGL extension is materialized through its own realm services."""
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result = 'pending';
          const source = `
            try {
              const canvas = new OffscreenCanvas(8, 8);
              const gl = canvas.getContext('webgl');
              const extension = gl && gl.getExtension('WEBGL_debug_renderer_info');
              postMessage(gl && extension ? 'ok' : 'missing');
            } catch (error) {
              postMessage('error:' + error.name + ':' + error.message);
            }`;
          const worker = new Worker(URL.createObjectURL(
            new Blob([source], {type: 'text/javascript'})));
          worker.onmessage = event => { window.result = event.data; };
          worker.onerror = event => { window.result = 'worker-error:' + event.message; };
        """)
        (await page.wait_for("result !== 'pending'", timeout=5))
        assert page.evaluate("result") == 'ok'
    finally:
        (await browser.close())


async def test_no_offscreen_2d_member_reports_itself_as_unimplemented():
    """Driving every member the log reported leaves the stub reporter silent."""
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        browser = (await Browser(stub_logging="on").__aenter__())
        try:
            page = browser.new_profile().new_page()
            (await page.set_content("<body></body>", url="https://repro.test/index.html", wait_until="commit"))
            page.evaluate("""(() => {
                const context = new OffscreenCanvas(4, 4).getContext('2d');
                context.fill(); context.fill('evenodd'); context.closePath(); context.scale(1, 1);
                context.getContextAttributes();
                context.createRadialGradient(0, 0, 1, 2, 2, 3).addColorStop(0.5, 'rgba(1,2,3,0.5)');
                context.drawImage(new OffscreenCanvas(2, 2), 0, 0, 1, 1);
                context.globalCompositeOperation = 'multiply';
                context.filter = 'blur(2px)';
            })()""")
        finally:
            (await browser.close())
    finally:
        logger.remove(token)
    assert [message for message in messages
            if "OffscreenCanvasRenderingContext2D" in message and "待实现" in message] == []
