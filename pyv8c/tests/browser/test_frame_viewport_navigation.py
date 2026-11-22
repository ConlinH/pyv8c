"""Frame geometry and the timing/referrer of an actual document load."""
import time

import pytest

from pyv8c.async_api import Browser


@pytest.mark.parametrize('size', [(0, 0), (240, 120)])
async def test_frame_uses_its_content_box_for_window_and_layout(page, size):
    width, height = size
    (await page.set_content(f'''<!doctype html><iframe id="frame"
        style="width:{width}px;height:{height}px;border:3px solid;padding:2px"
        srcdoc="<!doctype html><style>html,body{{margin:0}}</style><div></div>"></iframe>''', wait_until="commit"))
    (await page.wait_for("document.querySelector('iframe').contentDocument.querySelector('div') !== null", timeout=3))
    assert page.evaluate('''(() => {
        const frame = document.querySelector('iframe');
        const win = frame.contentWindow;
        return [win.innerWidth, win.innerHeight, win.visualViewport.width,
                win.document.querySelector('div').getBoundingClientRect().width];
    })()''') == [width, height, width, width]
    page.evaluate("document.querySelector('iframe').style.width = '360px'")
    assert page.evaluate("document.querySelector('iframe').contentWindow.innerWidth") == 360


async def test_frame_navigation_reports_referrer_and_reached_network_phases():
    def load(url, request):
        if url == 'https://parent.test/start?query=yes':
            body = '<!doctype html><iframe src="/frame"></iframe>'
        elif url == 'https://parent.test/frame':
            time.sleep(.02)
            body = '<!doctype html><p>child</p>'
        else:
            raise AssertionError(url)
        return {'headers': {'content-type': 'text/html'}, 'body': body}

    browser = (await Browser(real_network=False, resource_loader=load).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto('https://parent.test/start?query=yes'))
        result = page.evaluate('''(() => {
            const win = document.querySelector('iframe').contentWindow;
            const t = win.performance.timing;
            return {referrer:win.document.referrer,
                    times:[t.navigationStart,t.fetchStart,t.requestStart,t.responseStart,
                           t.responseEnd,t.domLoading,t.domInteractive,t.loadEventEnd]};
        })()''')
        assert result['referrer'] == 'https://parent.test/start?query=yes'
        assert result['times'] == sorted(result['times'])
        assert all(value > 0 for value in result['times'])
        assert result['times'][4] - result['times'][1] >= 15
    finally:
        (await browser.close())


async def test_layout_resolves_font_relative_dimensions(page):
    (await page.set_content('''<!doctype html><div id="box"
        style="font-size:20px;width:10em;height:2em;padding:1em"></div>''', wait_until="commit"))
    assert page.evaluate('''(() => {
        const node=document.querySelector('#box'), box=node.getBoundingClientRect();
        return [box.width,box.height,getComputedStyle(node).width];
    })()''') == [240, 80, '200px']
