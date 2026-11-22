"""Body/frameset handler aliases, compared with local Chrome 156."""
import pytest

from pyv8c.async_api import Browser


async def test_body_inline_load_starts_business_script():
    requests = []
    def loader(url, request):
        requests.append(url)
        return {'body': 'globalThis.businessReady=true;',
                'headers': {'content-type': 'text/javascript'}}
    browser = (await Browser(real_network=False, resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<script>globalThis.events=[];</script>
            <body onload="events.push([this===window,event.target===document,
                event.currentTarget===window]);const s=document.createElement('script');
                s.src='/business.js';document.head.appendChild(s)"></body>''',
            url='https://lifecycle.test/', wait_until="commit"))
        (await page.run_until_idle(timeout=3))
        assert page.evaluate('[events,!!globalThis.businessReady,document.body.onload===window.onload]') == [
            [[True, True, True]], True, True]
        assert requests == ['https://lifecycle.test/business.js']
    finally:
        (await browser.close())


@pytest.mark.parametrize('tag', ['body', 'frameset'])
async def test_detached_body_handler_aliases_window_but_listener_stays_on_element(page, tag):
    assert page.evaluate('''(() => {
        const element=document.createElement(''' + repr(tag) + ''');const calls=[];
        const fn=function(){calls.push(this===window?'window':'element')};
        element.onresize=fn;
        const same=window.onresize===fn;
        element.addEventListener('resize',()=>calls.push('listener'));
        element.dispatchEvent(new Event('resize'));
        window.dispatchEvent(new Event('resize'));
        window.onresize=null;
        return [same,calls,element.onresize===null];
    })()''') == [True, ['listener', 'window'], True]


async def test_body_handler_attribute_write_remove_and_idl_share_window_slot(page):
    assert page.evaluate('''(() => {
        const body=document.body;globalThis.calls=[];
        body.setAttribute('onresize','calls.push(this===window)');
        window.dispatchEvent(new Event('resize'));
        const same=window.onresize===body.onresize;
        body.removeAttribute('onresize');
        const cleared=window.onresize===null;
        window.dispatchEvent(new Event('resize'));
        body.onresize=()=>calls.push('replaced');
        // Setting the same content attribute must restore its inline handler.
        body.setAttribute('onresize','calls.push(this===window)');
        body.onresize=()=>calls.push('replaced');
        body.setAttribute('onresize','calls.push(this===window)');
        window.dispatchEvent(new Event('resize'));
        return [same,cleared,calls];
    })()''') == [True, True, [True, True]]


async def test_inert_document_body_cannot_replace_active_window_handler(page):
    assert page.evaluate('''(() => {
        const fn=()=>{};window.onload=fn;
        const doc=document.implementation.createHTMLDocument('inert');
        doc.body.onload=()=>{};
        doc.body.setAttribute('onload','globalThis.unexpected=true');
        return [doc.body.onload===null,window.onload===fn];
    })()''') == [True, True]


async def test_normal_element_load_and_body_click_keep_element_target(page):
    assert page.evaluate('''(() => {
        const body=document.body,div=document.createElement('div');const calls=[];
        body.onclick=function(){calls.push(this===body)};
        div.onload=function(){calls.push(this===div)};
        body.click();div.dispatchEvent(new Event('load'));
        return [calls,window.onclick===null,window.onload===null];
    })()''') == [[True, True], True, True]


async def test_navigation_discards_body_window_handler(page):
    (await page.set_content('<body onresize="globalThis.stale=true">',url='https://lifecycle.test/first', wait_until="commit"))
    (await page.set_content('<body><script>globalThis.fresh=true</script>',url='https://lifecycle.test/second', wait_until="commit"))
    page.evaluate("window.dispatchEvent(new Event('resize'))")
    assert page.evaluate('[fresh,globalThis.stale===undefined,window.onresize===null]') == [True, True, True]


@pytest.mark.parametrize('tag', ['body','frameset'])
async def test_inline_window_error_handler_receives_all_five_arguments(page, tag):
    assert page.evaluate('''(() => {
        const element=document.createElement('''+repr(tag)+''');globalThis.calls=[];
        globalThis.failure=new Error('boom');
        element.setAttribute('onerror',"calls.push([this===window,event,source,lineno,colno,error===failure,arguments.length]);return true");
        const event=new ErrorEvent('error',{message:'boom',filename:'source.js',lineno:3,colno:4,
            error:failure,cancelable:true});
        const accepted=window.dispatchEvent(event);
        element.removeAttribute('onerror');
        return [calls,accepted,event.defaultPrevented,window.onerror===null];
    })()''') == [[[True,'boom','source.js',3,4,True,5]],False,True,True]


@pytest.mark.parametrize('result,prevented', [('true',True),('false',False),('1',False)])
async def test_idl_window_error_handler_return_true_cancels_error(page, result, prevented):
    assert page.evaluate('''(() => {
        window.onerror=function(){return '''+result+'''};
        const event=new ErrorEvent('error',{cancelable:true});
        const accepted=window.dispatchEvent(event);window.onerror=null;
        return [accepted,event.defaultPrevented];
    })()''') == [not prevented,prevented]


async def test_plain_window_error_and_element_error_receive_event_object(page):
    assert page.evaluate('''(() => {
        const calls=[];const plain=new Event('error',{cancelable:true});
        window.onerror=function(event){calls.push([event===plain,arguments.length]);return false};
        const accepted=window.dispatchEvent(plain);window.onerror=null;
        const element=document.createElement('img');
        const error=new ErrorEvent('error',{message:'element',cancelable:true});
        element.onerror=function(event){calls.push([event===error,arguments.length])};
        element.dispatchEvent(error);
        return [calls,accepted,plain.defaultPrevented];
    })()''') == [[[True,1],[True,1]],False,True]
