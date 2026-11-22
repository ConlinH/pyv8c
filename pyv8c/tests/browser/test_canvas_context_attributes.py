"""Canvas and OffscreenCanvas expose the surface attributes of their 2D renderer."""
from pyv8c.async_api import Browser


async def test_default_context_attributes_and_image_pixel_format():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', wait_until="commit"))
        results = page.evaluate('''[document.createElement('canvas'),new OffscreenCanvas(10,10)].map(canvas=>{
          const context=canvas.getContext('2d');const attributes=context.getContextAttributes();
          attributes.toneMapping.mode='changed';
          return [context.getContextAttributes(),context.getImageData(0,0,1,1).pixelFormat];
        })''')
        assert results == [[{'alpha':True,'colorSpace':'srgb','colorType':'unorm8',
                             'desynchronized':False,'toneMapping':{'mode':'standard'},
                             'willReadFrequently':False},'rgba-unorm8']]*2
    finally:
        (await browser.close())
