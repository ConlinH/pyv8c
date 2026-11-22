"""An XHR response belongs to the request that started it, even after reentry."""

import asyncio
import threading

import pytest

from pyv8c.async_api import Browser


@pytest.mark.race
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_reopen_discards_old_response_and_deadline(asynchronous):
    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    calls=[]

    def loader(url, request):
        calls.append((url, dict(request['headers'])))
        if url.endswith('/old'):
            started.set()
            assert release.wait(3)
            returned.set()
        return {'body':'OLD' if url.endswith('/old') else 'NEW'}

    browser=(await Browser(real_network=False,resource_loader=loader,threaded_loaders=True,stub_logging='off').__aenter__())
    try:
        page=browser.new_profile().new_page();(await page.set_content('<body>',url='https://xhr.test/', wait_until="commit"))
        page.evaluate('''globalThis.events=[];globalThis.x=new XMLHttpRequest();
            x.onload=()=>events.push(x.responseText);x.onloadend=()=>events.push('end');
            x.open('GET','/old');x.timeout=100;x.setRequestHeader('X-Old','private');x.send();''')
        assert (await asyncio.to_thread(started.wait, 2))
        page.evaluate("x.timeout=0;x.open('GET','/new',"+str(asynchronous).lower()+");x.send()")
        (await page.wait_for("x.responseText==='NEW'",timeout=2))
        release.set();assert (await asyncio.to_thread(returned.wait, 2))
        page.evaluate('globalThis.settled=false;setTimeout(()=>settled=true,150)')
        (await page.wait_for('settled',timeout=2))
        assert page.evaluate('[x.status,x.responseText,events]') == [200,'NEW',['NEW','end']]
        headers=next(headers for url,headers in calls if url.endswith('/new'))
        assert not any(key.lower()=='x-old' for key in headers)
        assert page.resource_ownership.active_count == 0
    finally:
        release.set();(await browser.close())


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('action', ['open','abort'])
async def test_done_handler_reentry_matches_chrome_terminal_events(asynchronous, action):
    browser=(await Browser(real_network=False,resource_loader=lambda u,r:{'body':'OLD' if u.endswith('/old') else 'NEW'},
                    stub_logging='off').__aenter__())
    try:
        page=browser.new_profile().new_page();(await page.set_content('<body>',url='https://xhr.test/', wait_until="commit"))
        page.evaluate('''globalThis.events=[];globalThis.done=false;globalThis.sendError=null;
            const x=new XMLHttpRequest();
            x.onload=()=>events.push(x.responseText);x.onloadend=()=>events.push('end');
            x.onreadystatechange=()=>{if(x.readyState===4){x.onreadystatechange=null;
                x.'''+("open('GET','/new',false);x.send()" if action=='open' else 'abort()')+''';done=true}};
            x.open('GET','/old','''+str(asynchronous).lower()+''');
            try{x.send()}catch(error){sendError=error.name}''')
        (await page.wait_for('done',timeout=2))
        # Chrome keeps both terminal event pairs when DONE reopens and sends.
        assert page.evaluate('events') == (['NEW','end','NEW','end'] if action=='open' else [])
        assert page.evaluate('sendError || ""') == ('NetworkError' if action=='abort' and not asynchronous else '')
    finally:
        (await browser.close())


@pytest.mark.parametrize('stage', [2,3])
@pytest.mark.parametrize('new_asynchronous', [False,True])
async def test_async_readystatechange_reentry_matches_chrome(stage, new_asynchronous):
    browser=(await Browser(real_network=False,resource_loader=lambda u,r:{'body':'OLD' if u.endswith('/old') else 'NEW'},
                    stub_logging='off').__aenter__())
    try:
        page=browser.new_profile().new_page();(await page.set_content('<body>',url='https://xhr.test/', wait_until="commit"))
        page.evaluate('''globalThis.events=[];globalThis.done=false;const x=new XMLHttpRequest();
            x.onload=()=>events.push(x.responseText);x.onloadend=()=>{events.push('end');done=true};
            x.onreadystatechange=()=>{if(x.readyState==='''+str(stage)+'''){
                x.onreadystatechange=null;x.open('GET','/new','''+str(new_asynchronous).lower()+''');x.send()}};
            x.open('GET','/old');x.send();''')
        (await page.wait_for('done',timeout=2))
        assert page.evaluate('events') == ['NEW','end'] * (1 if new_asynchronous else 2)
    finally:
        (await browser.close())


@pytest.mark.parametrize('stage', [2,3])
async def test_async_readystatechange_abort_cancels_old_load_events(stage):
    browser=(await Browser(real_network=False,resource_loader=lambda u,r:{'body':'OLD'},stub_logging='off').__aenter__())
    try:
        page=browser.new_profile().new_page();(await page.set_content('<body>',url='https://xhr.test/', wait_until="commit"))
        page.evaluate('''globalThis.events=[];globalThis.done=false;globalThis.x=new XMLHttpRequest();
            x.onload=()=>events.push('load');x.onabort=()=>events.push('abort');
            x.onloadend=()=>{events.push('end');done=true};
            x.onreadystatechange=()=>{if(x.readyState==='''+str(stage)+'''){
                x.onreadystatechange=null;x.abort()}};x.open('GET','/old');x.send();''')
        (await page.wait_for('done',timeout=2))
        assert page.evaluate('[events,x.readyState,x.status,x.responseText]') == [['abort','end'],0,0,'']
    finally:
        (await browser.close())


async def test_reopen_rejected_arguments_leave_current_request_running():
    browser=(await Browser(real_network=False,resource_loader=lambda u,r:{'body':'original'},stub_logging='off').__aenter__())
    try:
        page=browser.new_profile().new_page();(await page.set_content('<body>',url='https://xhr.test/', wait_until="commit"))
        page.evaluate('''globalThis.answer=null;const x=new XMLHttpRequest();x.open('GET','/old');
            x.onload=()=>answer=x.responseText;x.send();
            try{x.open('BAD METHOD','/new')}catch(error){}''')
        (await page.wait_for('answer!==null',timeout=2))
        assert page.evaluate('answer') == 'original'
    finally:
        (await browser.close())


@pytest.mark.parametrize('late_result', ['response','error','timeout'])
async def test_already_queued_old_callback_cannot_touch_new_request(page, monkeypatch, late_result):
    from types import SimpleNamespace
    from pyv8c.web.fetch.response import Response

    callbacks, handles, deadlines = [], [], []
    resources=page.window._services.resource_services
    timers=page.window._services.task_services
    schedule=type(timers).schedule

    def start(self, url, request, callback):
        handle=SimpleNamespace(cancelled=False)
        handle.cancel=lambda:setattr(handle,'cancelled',True)
        callbacks.append(callback);handles.append(handle)
        return handle

    def deadline(self, callback, delay, repeat=False):
        deadlines.append(callback)
        return schedule(self,callback,delay,repeat)

    monkeypatch.setattr(type(resources),'load_resource_async',start)
    monkeypatch.setattr(type(timers),'schedule',deadline)
    (await page.set_content('<body>',url='https://xhr.test/', wait_until="commit"))
    page.evaluate('''globalThis.events=[];globalThis.x=new XMLHttpRequest();
        x.onload=()=>events.push(x.responseText);x.open('GET','/old');x.timeout=10000;x.send();
        x.timeout=0;x.open('GET','/new');x.send();''')
    if late_result=='timeout':
        callback=deadlines[0]
    else:
        callback=lambda:callbacks[0](Response('OLD'),None) if late_result=='response'\
            else callbacks[0](None,OSError('old failure'))
    page.tasks.post_task(callback)
    (await page.run_until_idle())
    assert handles[0].cancelled and not handles[1].cancelled
    assert page.evaluate('[x.readyState,x.status,x.responseText,events]') == [1,0,'',[]]
    page.tasks.post_task(lambda:callbacks[1](Response('NEW'),None))
    (await page.run_until_idle())
    assert page.evaluate('[x.status,x.responseText,events]') == [200,'NEW',['NEW']]
