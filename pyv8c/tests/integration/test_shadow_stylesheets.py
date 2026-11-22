"""Shadow stylesheets have a live list isolated from the document's list."""
import pytest


@pytest.mark.parametrize('mode', ['open', 'closed'])
async def test_shadow_stylesheet_list_tracks_connected_owners(page, mode):
    (await page.set_content('<!doctype html><style>body{margin:0}</style><body></body>', wait_until="commit"))
    assert page.evaluate('''(() => {
        const host=document.body.appendChild(document.createElement('div'));
        const root=host.attachShadow({mode:MODE});
        const list=root.styleSheets,style=document.createElement('style');
        const answer=[list instanceof StyleSheetList,list===root.styleSheets,list.length];
        style.textContent='div{color:red}';root.appendChild(style);
        answer.push(list.length,document.styleSheets.length,list[0]===style.sheet,
            list[0] instanceof CSSStyleSheet,list[0].ownerNode===style,
            list[0].cssRules.length,list.item(1)===null);
        host.remove();answer.push(list.length,style.sheet===null);
        document.body.appendChild(host);answer.push(list.length);
        style.remove();answer.push(list.length,style.sheet===null);
        return answer;
    })()'''.replace('MODE', repr(mode))) == [
        True, True, 0, 1, 1, True, True, True, 1, True, 0, True, 1, 0, True,
    ]


@pytest.mark.parametrize('scope', ['document', 'open', 'closed'])
async def test_old_stylesheets_keep_their_rules_after_owner_changes(page, scope):
    (await page.set_content('<!doctype html><body></body>', wait_until="commit"))
    assert page.evaluate('''(() => {
        const host=document.body.appendChild(document.createElement('div'));
        const root=SCOPE==='document'?host:host.attachShadow({mode:SCOPE});
        const style=document.createElement('style');style.textContent='b{color:red}';root.appendChild(style);
        const first=style.sheet;
        style.firstChild.data='i{color:blue}';const second=style.sheet;
        const result=[first!==second,first.ownerNode===null,first.cssRules[0].selectorText,
            second.cssRules[0].selectorText,second.ownerNode===style];
        host.remove();result.push(second.ownerNode===null,style.sheet===null,
            second.cssRules[0].selectorText);
        document.body.appendChild(host);
        result.push(style.sheet!==second,style.sheet.ownerNode===style,
            style.sheet.cssRules[0].selectorText);
        return result;
    })()'''.replace('SCOPE', repr(scope))) == [
        True, True, 'b', 'i', True, True, True, 'i', True, True, 'i',
    ]
