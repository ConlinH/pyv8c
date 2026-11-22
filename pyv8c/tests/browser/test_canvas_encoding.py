"""Canvas serialization follows the browser's format and quality rules."""

import base64
import io

import pytest

from pyv8c.async_api import Browser


@pytest.fixture
async def page():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://canvas.test/', wait_until="commit"))
        yield page
    finally:
        (await browser.close())


def decode(data_url):
    header, body = data_url.split(',', 1)
    return header, base64.b64decode(body)


async def test_blank_canvas_formats_and_context_are_independent(page):
    pytest.importorskip('PIL')
    from PIL import features
    if not features.check('jpg') or not features.check('webp'):
        pytest.skip('Pillow JPEG and WebP codecs are required')
    result = page.evaluate('''(() => {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 1;
      const png = canvas.toDataURL();
      const jpeg = canvas.toDataURL('IMAGE/JPEG');
      const webp = canvas.toDataURL('image/webp');
      const unsupported = canvas.toDataURL('image/jpg');
      const webglAfterExport = !!canvas.getContext('webgl');
      const stillWebGL = canvas.toDataURL() === png && canvas.getContext('2d') === null;
      const zero = document.createElement('canvas'); zero.width = 0;
      const oversized = document.createElement('canvas'); oversized.width = 65536; oversized.height = 1;
      return {png, jpeg, webp, unsupported, webglAfterExport, stillWebGL,
        zero: zero.toDataURL(), oversized: oversized.toDataURL()};
    })()''')
    # Chromium's low-compression PNG stream for a transparent 1x1 surface.
    assert result['png'] == ('data:image/png;base64,'
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4AWJiYGBgAAAAAP//'
        'XRcpzQAAAAZJREFUAwAADwADJDd96QAAAABJRU5ErkJggg==')
    assert result['unsupported'] == result['png']
    assert decode(result['jpeg'])[0] == 'data:image/jpeg;base64'
    assert decode(result['webp'])[0] == 'data:image/webp;base64'
    assert result['webglAfterExport'] is True
    assert result['stillWebGL'] is True
    assert result['zero'] == 'data:,'
    assert result['oversized'] == 'data:,'


async def test_canvas_formats_encode_pixels_and_only_numeric_quality(page):
    image_module = pytest.importorskip('PIL.Image')
    from PIL import features
    if not features.check('jpg') or not features.check('webp'):
        pytest.skip('Pillow JPEG and WebP codecs are required')
    result = page.evaluate('''(() => {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 8;
      const context = canvas.getContext('2d');
      context.fillStyle = 'rgba(255, 0, 0, 0.5)'; context.fillRect(0, 0, 8, 8);
      const defaultJpeg = canvas.toDataURL('image/jpeg');
      const stringQuality = canvas.toDataURL('image/jpeg', '0.2');
      const objectQuality = canvas.toDataURL('image/jpeg', {valueOf() { throw Error('must not coerce'); }});
      return {pixel:Array.from(context.getImageData(0,0,1,1).data), png:canvas.toDataURL('image/png'), jpeg:defaultJpeg,
        webp:canvas.toDataURL('image/webp', 1), lowQuality:canvas.toDataURL('image/jpeg', 0),
        stringQuality, objectQuality};
    })()''')
    assert result['stringQuality'] == result['jpeg'] == result['objectQuality']
    assert result['lowQuality'] != result['jpeg']
    png = image_module.open(io.BytesIO(decode(result['png'])[1]))
    assert png.size == (8, 8)
    assert list(png.getpixel((0, 0))) == result['pixel']
    jpeg = image_module.open(io.BytesIO(decode(result['jpeg'])[1]))
    red, green, blue = jpeg.convert('RGB').getpixel((0, 0))
    assert 125 <= red <= 130 and green <= 2 and blue <= 2
    webp = image_module.open(io.BytesIO(decode(result['webp'])[1])).convert('RGBA')
    assert list(webp.getpixel((0, 0))) == result['pixel']
