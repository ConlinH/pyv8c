async def test_cached_stylesheet_parse_keeps_matching_live_and_query_errors_strict(page):
    (await page.set_content('''<style>.selected{width:37px}
        .selected:unsupported-pseudo{width:900px}</style><div id="item"></div>''', wait_until="commit"))
    assert page.evaluate('''(() => {
        const element=document.getElementById('item');
        element.className='selected';const first=element.offsetWidth;
        element.className='';element.style.width='23px';const second=element.offsetWidth;
        element.className='selected';element.style.width='';const third=element.offsetWidth;
        let strict=false;try{document.querySelector('.selected:unsupported-pseudo')}
        catch(error){strict=error instanceof SyntaxError}
        return [first,second,third,strict];
    })()''') == [37,23,37,True]
