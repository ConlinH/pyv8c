"""Shadow content participates in layout and trusted input, with scoped CSS."""
import pytest


async def install(page, mode):
    (await page.set_content('''<!doctype html><style>body{margin:0}input{width:80px}</style>
        <div id="host" style="display:block;position:relative;width:200px;height:80px;margin:10px">
        <button id="light" style="display:block;width:20px;height:20px">light</button></div>''',
        url='https://fixture.test/', wait_until="commit"))
    page.evaluate('''globalThis.root=document.getElementById('host').attachShadow({mode:MODE});
        root.innerHTML=`<style>#box{display:block;position:absolute;left:12px;top:14px;width:24px;height:24px;margin:0;padding:0;border:0}</style><input id="box" type="checkbox">`;
        globalThis.log=[];
        root.querySelector('input').addEventListener('click',e=>log.push([e.target.id,e.isTrusted,e.composedPath()[0].id]));
        document.addEventListener('click',e=>log.push([e.target.id,e.isTrusted,e.composedPath()[0].id]));
    '''.replace('MODE', repr(mode)))


@pytest.mark.parametrize('mode', ['open', 'closed'])
async def test_shadow_control_layout_hit_testing_and_mouse_activation(page, mode):
    (await install(page, mode))
    assert page.evaluate('''(() => {
        const r=root.querySelector('#box').getBoundingClientRect();
        const light=document.getElementById('light').getBoundingClientRect();
        return [r.x,r.y,r.width,r.height,light.width,light.height,
            document.elementFromPoint(23,25).id];
    })()''') == [22, 24, 24, 24, 0, 0, 'host']
    (await page.mouse.click(23, 25))
    assert page.evaluate("root.querySelector('#box').checked") is True
    assert page.evaluate('log') == [
        ['box', True, 'box'], ['host', True, 'box' if mode == 'open' else 'host'],
    ]


async def test_shadow_stylesheet_changes_invalidate_layout(page):
    (await install(page, 'closed'))
    assert page.evaluate("root.querySelector('#box').getBoundingClientRect().width") == 24
    page.evaluate("root.querySelector('style').textContent += '#box{width:32px}'")
    assert page.evaluate("root.querySelector('#box').getBoundingClientRect().width") == 32


async def test_noncomposed_shadow_event_stays_inside_its_tree(page):
    (await install(page, 'closed'))
    page.evaluate("root.querySelector('#box').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
    assert page.evaluate('log') == [['box', False, 'box']]
