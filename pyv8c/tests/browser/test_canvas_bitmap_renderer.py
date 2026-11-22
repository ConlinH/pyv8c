"""``getContext('bitmaprenderer')`` hands out Chrome's ImageBitmapRenderingContext."""

from pyv8c.async_api import Browser


async def test_a_canvas_answers_a_bitmap_renderer_context():
    """One context object per canvas, tied to it, and the canvas keeps that type."""
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const canvas = document.createElement('canvas');
            const first = canvas.getContext('bitmaprenderer');
            const second = canvas.getContext('bitmaprenderer');
            return [!!first,
                    first === second,
                    first.canvas === canvas,
                    typeof first.transferFromImageBitmap,
                    first.constructor.name,
                    canvas.getContext('2d') === null];
        })()""") == [True, True, True, "function", "ImageBitmapRenderingContext", True]
    finally:
        (await browser.close())


async def test_a_null_bitmap_clears_the_canvas():
    """The null argument is the documented way to leave the canvas blank."""
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const context = document.createElement('canvas').getContext('bitmaprenderer');
            context.transferFromImageBitmap(null);
            return context.canvas.getContext('2d') === null;
        })()""") is True
    finally:
        (await browser.close())


async def test_a_canvas_answers_a_webgpu_context():
    """Chrome hands out a GPUCanvasContext that refuses a texture until configured."""
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const canvas = document.createElement('canvas');
            const context = canvas.getContext('webgpu');
            let error = null;
            try { context.getCurrentTexture(); } catch (thrown) { error = thrown.name; }
            return [!!context,
                    context.canvas === canvas,
                    context.constructor.name,
                    error,
                    canvas.getContext('2d') === null];
        })()""") == [True, True, "GPUCanvasContext", "InvalidStateError", True]
    finally:
        (await browser.close())
