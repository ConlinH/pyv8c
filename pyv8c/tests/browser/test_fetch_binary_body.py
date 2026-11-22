"""Binary fetch bodies cross to loader threads as byte snapshots, including view offsets."""

import asyncio
import time
import pytest

from pyv8c.async_api import Browser


@pytest.mark.parametrize('api', ['fetch', 'xhr'])
async def test_empty_views_and_native_brand_ignore_constructor_hooks(api):
    seen = []
    browser = (await Browser(real_network=False, threaded_loaders=True,
                      resource_loader=lambda url, options: seen.append(options['body']) or 'ok').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://binary.test/', wait_until="commit"))
        page.evaluate('''globalThis.finished=0;globalThis.hooks=0;
            globalThis.views=[new DataView(new ArrayBuffer(4),4,0),new Uint8Array([65,66])];
            for(const value of views)Object.defineProperty(value,'constructor',{get(){hooks++;throw 1}});
        ''')
        if api == 'fetch':
            page.evaluate("for(const body of views)fetch('/value',{method:'POST',body}).then(()=>finished++)")
        else:
            page.evaluate('''for(const body of views){const x=new XMLHttpRequest();x.open('POST','/value');
                x.onload=()=>finished++;x.send(body)}''')
        (await page.wait_for('finished===2', timeout=3))
        assert page.evaluate('hooks') == 0
        assert sorted(seen) == [b'', b'AB']
    finally:
        (await browser.close())


@pytest.mark.parametrize('api', ['fetch', 'xhr'])
async def test_binary_requests_copy_buffer_sources_before_a_loader_thread_reads_them(api):
    seen = []

    def load(url, request):
        seen.append(request['body'])
        return {'status': 200, 'body': 'ok', 'headers': {'content-type': 'text/plain'}}

    browser = (await Browser(real_network=False, threaded_loaders=True, resource_loader=load).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://binary.test/', wait_until="commit"))
        if api == 'fetch':
            page.evaluate("window.send=(url,body)=>fetch(url,{method:'POST',body}).then(r=>statuses.push(r.status))")
        else:
            page.evaluate('''window.send=(url,body)=>{const xhr=new XMLHttpRequest();
                xhr.open('POST',url); xhr.onload=()=>statuses.push(xhr.status); xhr.send(body);};''')
        page.evaluate('''
            const buffer = new Uint8Array([99, 0, 128, 255, 88]);
            const view = new DataView(buffer.buffer, 1, 3);
            window.statuses = [];
            send('/view', view);
            send('/buffer', buffer.buffer);
            send('/typed', buffer.subarray(2, 4));
            buffer.fill(7);
        ''')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len(page.evaluate('window.statuses')) == 3:
                break
            (await asyncio.sleep(.01))
        assert page.evaluate('window.statuses') == [200, 200, 200]
        assert sorted(seen) == sorted([b'\x00\x80\xff', b'c\x00\x80\xffX', b'\x80\xff'])
    finally:
        (await browser.close())
