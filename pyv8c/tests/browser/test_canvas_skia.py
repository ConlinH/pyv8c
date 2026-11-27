"""Regression coverage for the Skia raster surface."""

from concurrent.futures import ThreadPoolExecutor
import sys
from threading import Barrier

import pytest

from pyv8c._native import RasterCanvas, RasterImage
from pyv8c.async_api import Browser


def test_raster_canvas_can_move_and_be_released_on_another_thread(monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "unraisablehook", errors.append)
    with ThreadPoolExecutor(max_workers=1) as worker:
        canvas = worker.submit(RasterCanvas, 1, 1).result()
        worker.submit(canvas.write, (0, 0), (1, 1), bytes((255, 0, 0, 255)),
                      (0, 0, 1, 1)).result()
        snapshot = worker.submit(canvas.snapshot).result()
    assert canvas.read(0, 0, 1, 1) == bytes((255, 0, 0, 255))
    canvas.write((0, 0), (1, 1), bytes((0, 0, 255, 255)), (0, 0, 1, 1))
    assert canvas.read(0, 0, 1, 1) == bytes((0, 0, 255, 255))
    del canvas
    assert snapshot.read(0, 0, 1, 1) == bytes((255, 0, 0, 255))
    assert not errors, [str(item.exc_value) for item in errors]


@pytest.mark.parametrize("kind", ("image/png", "image/jpeg", "image/webp"))
def test_encoding_can_share_a_snapshot_between_threads(kind):
    canvas = RasterCanvas(640, 360)
    canvas.draw_rect((0, 0, 640, 360), (1, 0, 0, 1, 0, 0),
                     (255, 0, 0, 255), None, "source-over", None)
    snapshot = canvas.snapshot()
    start = Barrier(4, timeout=10)

    def encode():
        start.wait()
        return snapshot.encode(kind, None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [pool.submit(encode) for _ in range(4)]
        first, *others = [result.result() for result in results]
    assert all(encoded == first for encoded in others)
    image = RasterImage.decode(first)
    assert image.size == (640, 360)
    red, green, blue, alpha = image.read(0, 0, 1, 1)
    assert red >= 250 and green <= 4 and blue <= 4 and alpha == 255


def test_large_resize_preserves_snapshot_and_pixels():
    pixel = bytes((13, 27, 101, 255))
    image = RasterImage.from_rgba(2, 2, pixel * 4)
    resized = image.resize((512, 512))
    assert resized.size == (512, 512)
    assert resized.read(511, 511, 1, 1) == pixel
    assert image.size == (2, 2)
    assert image.read(1, 1, 1, 1) == pixel
    with pytest.raises(ValueError, match="invalid raster dimensions"):
        resized.resize((0, 512))


async def test_transformed_paint_preserves_existing_pixels():
    async with Browser(real_network=False) as browser:
        page = browser.new_profile().new_page()
        await page.set_content('<canvas width="4" height="2"></canvas>')
        result = page.evaluate("""(() => {
            const canvas = document.querySelector('canvas');
            const ctx = canvas.getContext('2d');
            const pixel = (x, y) => Array.from(ctx.getImageData(x, y, 1, 1).data);
            ctx.fillStyle = 'rgba(200,100,50,0.5)';
            ctx.fillRect(0, 0, 1, 1);
            ctx.fillRect(1, 0, 1, 1);
            ctx.clearRect(1, 0, 1, 1);
            return [pixel(0, 0), pixel(1, 0), canvas.toDataURL('image/png')];
        })()""")
        context = page.document.fn_querySelector("canvas")._context2d
        surface = context._surface
        assert surface is not None
        assert result[:2] == [[199, 100, 50, 128], [0, 0, 0, 0]]
        assert result[2].startswith("data:image/png;base64,iVBORw0KGgo")

        result = page.evaluate("""(() => {
            const ctx = document.querySelector('canvas').getContext('2d');
            ctx.translate(2, 0);
            ctx.fillStyle = 'blue';
            ctx.fillRect(0, 0, 1, 1);
            return Array.from(ctx.getImageData(0, 0, 3, 1).data);
        })()""")
        assert context._surface is surface
        assert result == [199, 100, 50, 128, 0, 0, 0, 0, 0, 0, 255, 255]


async def test_gradient_path_compositing_and_canvas_snapshot():
    async with Browser(real_network=False) as browser:
        page = browser.new_profile().new_page()
        await page.set_content('<canvas width="8" height="4"></canvas>')
        result = page.evaluate("""(() => {
            const source = new OffscreenCanvas(4, 4);
            const paint = source.getContext('2d');
            paint.translate(1, 0);
            const gradient = paint.createLinearGradient(0, 0, 2, 0);
            gradient.addColorStop(0, 'rebeccapurple');
            gradient.addColorStop(1, 'rebeccapurple');
            paint.resetTransform();
            paint.fillStyle = gradient;
            paint.beginPath();
            paint.rect(0, 0, 4, 4);
            paint.rect(1, 1, 2, 2);
            paint.fill('evenodd');
            const ctx = document.querySelector('canvas').getContext('2d');
            ctx.translate(4, 0);
            ctx.drawImage(source, 0, 0);
            paint.clearRect(0, 0, 4, 4);
            ctx.globalCompositeOperation = 'destination-out';
            ctx.fillRect(0, 0, 1, 1);
            const pixel = (x, y) => Array.from(ctx.getImageData(x, y, 1, 1).data);
            return [pixel(4, 0), pixel(5, 1), pixel(7, 0), pixel(0, 0)];
        })()""")
        assert result == [[0, 0, 0, 0], [0, 0, 0, 0], [102, 51, 153, 255], [0, 0, 0, 0]]
