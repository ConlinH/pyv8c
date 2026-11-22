import json

import pytest


@pytest.mark.parametrize('identifier,selector', [
    ('field.name', r'#field\.name'),
    ('a,b', r'#a\,b'),
    ('a b', r'#a\ b'),
    ('123', r'#\31 23'),
    ('a>b', r'#a\>b'),
    ('a:b', r'#a\:b'),
    ('a"b', r'#a\"b'),
    ('a[b]', r'#a\[b\]'),
    ('a+b', r'#a\+b'),
    ('a.b', r'#a\2e b'),
    ('a.b', r'#a\00002eb'),
    ('a\ufffd', r'#a\0'),
    ('a\ufffd', '#a' + chr(92)),
    ('a ', '#a' + chr(92) + ' '),
])
async def test_escaped_id_queries_match_literal_identifier(page, identifier, selector):
    args = json.dumps([identifier, selector])
    assert page.evaluate('''(([id,selector]) => {
        const element=document.createElement('div');element.id=id;
        const child=document.createElement('span');element.appendChild(child);
        document.body.appendChild(element);
        return [document.querySelector(selector)===element,
            document.querySelectorAll(selector+' > span').length,
            element.matches(selector),child.closest(selector)===element];
    })(''' + args + ')') == [True, 0 if selector.endswith(chr(92)) else 1, True, True]


async def test_escaped_classes_groups_and_nested_arguments_keep_delimiters_literal(page):
    assert page.evaluate(r'''(() => {
        document.body.innerHTML='<div id="one" class="a,b x+y"><span></span></div>'+
            '<div id="two" class="a,b"></div>';
        return [Array.from(document.querySelectorAll(String.raw`.a\,b`)).map(x=>x.id),
            Array.from(document.querySelectorAll(String.raw`:is(.x\+y, #two)`)).map(x=>x.id),
            document.querySelectorAll(String.raw`.a\,b:not(.x\+y)`).length,
            document.querySelectorAll(String.raw`.x\+y > span, #two`).length];
    })()''') == [['one','two'], ['one','two'], 1, 2]


async def test_escaped_attribute_string_does_not_close_quote_or_bracket(page):
    assert page.evaluate(r'''(() => {
        const element=document.createElement('div');element.setAttribute('data-value','a"]b');
        document.body.appendChild(element);
        return document.querySelector(String.raw`[data-value="a\"\]b"]`)===element;
    })()''') is True


async def test_identifier_escape_cannot_continue_across_newline(page):
    assert page.evaluate('''(() => {try{document.querySelector('#a'+String.fromCharCode(92,10)+'b');
        return false}catch(error){return error.name==='SyntaxError'}})()''') is True
