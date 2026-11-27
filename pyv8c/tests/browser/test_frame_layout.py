"""Regression tests for iframe geometry and mouse hit testing."""
import pytest

from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import HostStateError


async def test_transformed_control_click_uses_child_viewport_coordinates(page):
    await page.set_content(
        '<body style="margin:0"><iframe style="width:30px;height:30px;border:0"></iframe>',
        wait_until='load',
    )
    page.evaluate('''(() => {
      const frame = document.querySelector('iframe');
      frame.contentDocument.body.innerHTML = '<input type="checkbox" '
        + 'style="position:absolute;left:25.5px;top:26px;width:13px;height:13px;'
        + 'margin:0;transform:translate(-10px,-10px)">';
    })()''')
    await page.click('input', deep=True)
    assert page.evaluate("document.querySelector('iframe').contentDocument.querySelector('input').checked") is True


async def test_deep_click_can_target_control_when_embedding_viewport_is_zero_sized(page):
    await page.set_content(
        '<body style="margin:0"><iframe style="width:0;height:0;border:0"></iframe>',
        wait_until='load',
    )
    page.evaluate('''(() => {
        const frame = document.querySelector('iframe');
        frame.contentDocument.body.innerHTML = '<input id="control" type="checkbox" '
          + 'style="position:absolute;left:26px;top:26px;width:13px;height:13px">';
        frame.contentWindow.trustedClick = false;
        frame.contentDocument.querySelector('input').addEventListener('click', event => {
            frame.contentWindow.trustedClick = event.isTrusted;
        });
    })()''')

    await page.click('input', deep=True, delay=.01)
    assert page.evaluate('''(() => {
        const frame = document.querySelector('iframe');
        const input = frame.contentDocument.querySelector('input');
        return [input.checked, frame.contentWindow.trustedClick];
    })()''') == [True, True]


async def test_hidden_shadow_host_suppresses_embedded_document_layout(page):
    await page.set_content('<!doctype html><div id=host></div>', wait_until='load')
    page.evaluate('''(() => {
        const root = document.querySelector('#host').attachShadow({mode:'closed'});
        root.innerHTML = '<iframe style="width:100px;height:60px;border:0"></iframe>';
        const frame = root.firstElementChild;
        frame.contentDocument.body.innerHTML = '<input id=control type=checkbox '
            + 'style="position:absolute;left:5px;top:5px;width:20px;height:20px">';
        globalThis.control = frame.contentDocument.querySelector('input');
    })()''')
    assert page.evaluate("control.getBoundingClientRect().width") == 20
    page.evaluate("document.querySelector('#host').style.display = 'none'")
    assert page.query_selector('#control', deep=True) is not None
    assert page.evaluate("control.getBoundingClientRect().width") == 0
    with pytest.raises(HostStateError, match='has no box to click'):
        await page.click('#control', deep=True, delay=.01)
    page.evaluate("document.querySelector('#host').style.display = 'block'")
    await page.click('#control', deep=True, delay=.01)
    assert page.evaluate('control.checked') is True


@pytest.mark.parametrize('during_press', [
    'control.remove()',
    'frame.style.cssText = "width:100px;height:60px;border:0"; control.style.left = "50px"',
], ids=['detached', 'viewport-grown'])
async def test_delayed_zero_viewport_click_rechecks_target(page, during_press):
    await page.set_content('<!doctype html><iframe style="width:0;height:0;border:0"></iframe>',
                           wait_until='load')
    page.evaluate('''(() => {
        const frame = document.querySelector('iframe');
        frame.contentDocument.body.innerHTML = '<input id=control type=checkbox '
            + 'style="position:absolute;left:0;top:0;width:20px;height:20px;margin:0">';
        const control = frame.contentDocument.querySelector('input');
        globalThis.control = control;
        globalThis.events = [];
        control.addEventListener('mousedown', () => { DURING_PRESS });
        for (const name of ['mouseup', 'click', 'change']) {
            control.addEventListener(name, event => events.push(event.type));
        }
    })()'''.replace('DURING_PRESS', during_press))
    await page.click('#control', deep=True, delay=.01)
    assert page.evaluate('[control.checked, events]') == [False, []]


async def test_inline_frame_uses_containing_block_percentage_height(page):
    await page.set_content('''<!doctype html><style>body{margin:0}</style>
        <div id=wrapper style="width:150px;height:20px">
          <iframe style="width:100%;height:100%;border:0"></iframe>
        </div>''', wait_until='load')
    assert page.evaluate('''(() => {
        const frame = document.querySelector('iframe');
        const size = () => [frame.offsetWidth, frame.offsetHeight, frame.contentWindow.innerHeight];
        const before = size();
        document.querySelector('#wrapper').style.height = '40px';
        return [before, size()];
    })()''') == [[150, 20, 20], [150, 40, 40]]


async def test_transformed_frame_maps_click_to_screen_coordinates(page):
    await page.set_content('''<!doctype html><style>body{margin:0}</style>
        <iframe style="display:block;width:80px;height:50px;border:0;
                      transform-origin:0 0;transform:translateX(100px) scale(2)"></iframe>''',
                           wait_until='load')
    page.evaluate('''(() => {
        const frame = document.querySelector('iframe');
        frame.contentDocument.body.innerHTML = '<button id=control '
            + 'style="position:absolute;left:0;top:0;width:20px;height:20px">Click</button>';
        frame.contentDocument.querySelector('button').onclick = event => {
            globalThis.coords = [event.clientX, event.clientY, event.screenX - screenX,
                                 event.screenY - screenY];
        };
    })()''')
    await page.click('#control', deep=True)
    assert page.evaluate('coords') == [10, 10, 120, 20]


@pytest.mark.parametrize("markup", [
    '<iframe style="position:fixed;left:0;top:0;border:0;width:100%;height:100%"></iframe>',
    '<div style="position:fixed;inset:0">'
    '<iframe style="display:block;border:0;width:100%;height:100%"></iframe></div>',
    '<section><iframe style="position:absolute;left:0;top:0;border:0;width:100%;height:100%">'
    '</iframe></section>',
], ids=["fixed-frame", "fixed-container", "absolute-frame"])
async def test_frame_positioned_against_viewport_accepts_mouse_input(page, markup):
    await page.set_content(
        '<!doctype html><html style="width:80px;height:0"><body style="margin:0">' + markup,
        wait_until="load",
    )
    page.evaluate("""(() => {
        const frame = document.querySelector('iframe');
        frame.contentDocument.body.innerHTML = '<input type="checkbox" '
            + 'style="position:absolute;left:25.5px;top:26px;width:13px;height:13px;margin:0">';
        frame.contentDocument.querySelector('input').addEventListener('click', event => {
            frame.contentWindow.trustedClick = event.isTrusted;
        });
    })()""")

    await page.click('input', deep=True)
    assert page.evaluate("""(() => {
        const frame = document.querySelector('iframe');
        const child = frame.contentWindow;
        return [document.documentElement.offsetWidth, document.documentElement.offsetHeight,
                child.innerWidth === innerWidth, child.innerHeight === innerHeight,
                child.document.querySelector('input').checked, child.trustedClick];
    })()""") == [80, 0, True, True, True, True]


async def test_embedding_display_none_suppresses_and_restores_child_boxes():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><section><iframe></iframe></section></body>', wait_until="commit"))
        assert page.evaluate("""(() => {
          const holder = document.querySelector('section');
          const frame = holder.firstElementChild;
          const child = frame.contentDocument;
          child.body.innerHTML = '<div style="width:100px;height:20px;padding:4px"></div>';
          const element = child.querySelector('div');
          return ['', 'display:none', 'visibility:hidden', 'opacity:0',
                  'content-visibility:hidden', ''].map(css => {
            holder.style.cssText = css;
            const rect = element.getBoundingClientRect();
            const style = frame.contentWindow.getComputedStyle(element);
            return [element.offsetWidth, element.offsetHeight,
              element.clientWidth, element.clientHeight,
              element.scrollWidth, element.scrollHeight,
              rect.width, rect.height, element.getClientRects().length,
              element.offsetParent === null, style.width, style.height,
              element.checkVisibility()];
          });
        })()""") == [
            [108, 28, 108, 28, 108, 28, 108, 28, 1, False, '100px', '20px', True],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, True, '100px', '20px', False],
            *[[108, 28, 108, 28, 108, 28, 108, 28, 1, False, '100px', '20px', True]] * 4,
        ]
    finally:
        (await browser.close())


async def test_nested_frame_layout_uses_current_outer_style():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', wait_until="commit"))
        assert page.evaluate("""(() => {
          const outer = document.querySelector('iframe');
          outer.contentDocument.body.innerHTML = '<iframe></iframe>';
          const inner = outer.contentDocument.querySelector('iframe');
          const child = inner.contentDocument;
          child.body.innerHTML = '<div style="width:100px">text</div>';
          const element = child.querySelector('div');
          const before = element.offsetWidth;
          outer.style.display = 'none';
          const hidden = [element.offsetWidth, element.getClientRects().length,
            inner.contentWindow.getComputedStyle(element).height,
            child.documentElement.clientWidth, child.documentElement.clientHeight];
          outer.style.display = '';
          return [before, hidden, element.offsetWidth];
        })()""") == [100, [0, 0, 'auto', 0, 0], 100]
    finally:
        (await browser.close())


async def test_warm_nested_rect_reads_each_embedding_box_once(page, monkeypatch):
    from pyv8c.web.dom import layout

    await page.set_content('<iframe id="outer"></iframe>', wait_until='load')
    page.evaluate('''(() => {
        const doc = document.getElementById('outer').contentDocument;
        doc.body.innerHTML = '<iframe style="box-sizing:border-box;width:120px;height:60px;'
                           + 'border:2px solid;padding:3px"></iframe>';
        globalThis.innerFrame = doc.querySelector('iframe');
        const child = innerFrame.contentDocument;
        child.body.style.margin = '0';
        child.body.innerHTML = '<div style="width:100%;height:10px"></div>';
        globalThis.leaf = child.body.firstElementChild;
    })()''')
    assert page.evaluate('leaf.getBoundingClientRect().width') == 110

    calls = []
    original = layout.layout

    def record(element):
        calls.append(element)
        return original(element)

    monkeypatch.setattr(layout, 'layout', record)
    assert page.evaluate('leaf.getBoundingClientRect().width') == 110
    assert len(calls) == len(set(calls)) == 2
    assert page.evaluate('''innerFrame.style.width = '140px';
        [innerFrame.contentWindow.innerHeight, leaf.getBoundingClientRect().width]''') == [50, 130]


async def test_warm_frame_visibility_reuses_styles_until_parent_sheet_changes(page, monkeypatch):
    from pyv8c.web.css import cascade

    await page.set_content('<style></style><section><iframe></iframe></section>', wait_until='load')
    assert page.evaluate('''(() => {
        const child = document.querySelector('iframe').contentDocument;
        child.body.innerHTML = '<div style="width:32px;height:8px"></div>';
        globalThis.leaf = child.querySelector('div');
        return leaf.getBoundingClientRect().width;
    })()''') == 32

    def unexpected_cascade(*args, **kwargs):
        pytest.fail("a warm rect read recomputed an embedding style")

    with monkeypatch.context() as patch:
        patch.setattr(cascade, '_cascade', unexpected_cascade)
        assert page.evaluate('leaf.getBoundingClientRect().width') == 32

    assert page.evaluate('''(() => {
        const sheet = document.querySelector('style').sheet;
        const index = sheet.insertRule('section { display: none }', 0);
        const hidden = leaf.getBoundingClientRect().width;
        sheet.deleteRule(index);
        return [hidden, leaf.getBoundingClientRect().width];
    })()''') == [0, 32]
