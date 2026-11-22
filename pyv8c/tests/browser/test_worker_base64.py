"""A worker decodes binary strings, rather than returning an unimplemented undefined."""

import asyncio
import json
import time
import pytest
from pyv8c.async_api import Browser

SOURCE = r'''const errors=[];
for (const fn of [()=>atob('%'),()=>btoa('\u2028'),()=>atob()]) {
    try { fn(); } catch(e) { errors.push(e.name); }
}
postMessage([atob(' YQ =='),btoa('\x00\xff'),atob('/w==').charCodeAt(0),errors]);'''


@pytest.mark.parametrize('kind', ['dedicated', 'shared'])
async def test_worker_base64_roundtrip_and_invalid_arguments(kind):
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://probe.test/', wait_until="commit"))
        if kind == 'dedicated':
            source, constructor, receiver = SOURCE, 'Worker', 'w'
        else:
            source = ('onconnect=event=>{const port=event.ports[0];'
                      "const copy=structuredClone({value:42});"
                      "if(copy.value!==42||!(copy instanceof Object))throw new Error('clone realm');"
                      + SOURCE.replace('postMessage(', 'port.postMessage(') + '}')
            constructor, receiver = 'SharedWorker', 'w.port'
        page.evaluate('''const w=new %s(URL.createObjectURL(new Blob([%s],{type:'text/javascript'})));
            %s.onmessage=e=>window.result=e.data;''' % (constructor, json.dumps(source), receiver))
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            result=page.evaluate('window.result')
            if isinstance(result,list):break
            (await asyncio.sleep(.01))
        assert result == ['a','AP8=',255,['InvalidCharacterError','InvalidCharacterError','TypeError']]
    finally:
        (await browser.close())
