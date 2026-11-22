"""Escapes and selector-list specificity, checked against local Chrome 156."""
import pytest


CASES = [
    (r'.a\,b{width:37px}', '<div class="a,b"></div>', 37),
    ('[data-value="a,b"]{width:37px}', '<div data-value="a,b"></div>', 37),
    (':is(.selected,#absent){width:37px}', '<div class="selected"></div>', 37),
    ('.absent,.selected{width:37px}', '<div class="selected"></div>', 37),
    ('.selected,#absent{width:37px}.selected{width:41px}', '<div class="selected"></div>', 41),
    ('div{width:23px}.selected,:unsupported{width:37px}', '<div class="selected"></div>', 23),
    ('div{width:23px}.selected,{width:37px}', '<div class="selected"></div>', 23),
    (r'#a\.b{width:37px}#a\2e b{width:41px}', '<div id="a.b"></div>', 41),
    (r'.a\.b{width:37px}.a\2e b{width:41px}', '<div class="a.b"></div>', 41),
    (r'#\61{width:37px}#a{width:41px}', '<div id="a"></div>', 41),
    ('[data-value="#fake"]{width:37px}.selected{width:41px}',
     '<div class="selected" data-value="#fake"></div>', 41),
    ('#item{width:37px}:where(#item){width:41px}', '<div id="item"></div>', 37),
    (':is(div,#absent){width:37px}.selected{width:41px}', '<div class="selected"></div>', 37),
    ('div:not(.absent,#never){width:37px}.selected{width:41px}', '<div class="selected"></div>', 37),
    (':is(:where(#item),.selected){width:37px}.selected{width:41px}',
     '<div id="item" class="selected"></div>', 41),
    (r'#a\.b{width:37px!important}#a\2e b{width:41px}', '<div id="a.b"></div>', 37),
    (r'#a\.b{width:37px}', '<div id="a.b" style="width:41px"></div>', 41),
]


@pytest.mark.parametrize('css,markup,width', CASES)
async def test_selector_list_cascade(page, css, markup, width):
    (await page.set_content('<style>'+css+'</style>'+markup, wait_until="commit"))
    assert page.evaluate("getComputedStyle(document.querySelector('div')).width") == f'{width}px'


async def test_escaped_stylesheet_matching_stays_live_inside_shadow_root(page):
    assert page.evaluate(r'''(() => {
        const host=document.createElement('div');document.body.append(host);
        const root=host.attachShadow({mode:'open'});
        root.innerHTML='<style>.a\\,b{width:37px}.other{width:41px}</style><div class="a,b"></div>';
        const element=root.querySelector('div');
        const before=getComputedStyle(element).width;
        element.className='other';
        return [before,getComputedStyle(element).width];
    })()''') == ['37px','41px']
