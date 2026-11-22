"""The global clone API must return independent graphs and transfer buffers atomically."""

import asyncio
import json
import time
import pytest
from pyv8c.async_api import Browser


@pytest.fixture
async def page():
    browser=(await Browser(real_network=False).__aenter__())
    try:
        page=browser.new_profile().new_page()
        (await page.set_content('<body></body>',url='https://probe.test/', wait_until="commit"))
        yield page
    finally:(await browser.close())


async def test_clone_cycles_maps_dates_and_shared_buffer_views(page):
    assert page.evaluate('''(()=>{const source={number:1,date:new Date(123),map:new Map([['x',2]])};source.self=source;
      const cloned=structuredClone(source);const buffer=new ArrayBuffer(8),a=new Uint8Array(buffer);a[0]=7;
      const b=structuredClone({a,buffer},{transfer:[buffer]});let errors=[];
      for(const value of [()=>{},Symbol('x'),document.body]){try{structuredClone(value)}catch(e){errors.push(e.name)}}
      return [cloned!==source,cloned.self===cloned,cloned.date.getTime(),cloned.map.get('x'),
        b.a[0],b.a.buffer===b.buffer,buffer.byteLength,errors];})()''') == [True,True,123,2,7,True,0,['DataCloneError']*3]


async def test_failed_clone_does_not_detach_and_transfer_duplicates_are_rejected(page):
    assert page.evaluate('''(()=>{const buffer=new ArrayBuffer(8);const failures=[];
      for(const action of [()=>structuredClone({f:()=>{}},{transfer:[buffer]}),
        ()=>structuredClone(buffer,{transfer:[buffer,buffer]}),()=>structuredClone()]){
        try{action()}catch(e){failures.push(e.name)}
      }return [buffer.byteLength,failures];})()''') == [8,['DataCloneError','DataCloneError','TypeError']]


async def test_worker_clone_uses_its_own_realm(page):
    source="const value={x:1};value.self=value;const copy=structuredClone(value);postMessage([copy!==value,copy.self===copy,copy.x]);"
    page.evaluate('''const worker=new Worker(URL.createObjectURL(new Blob([%s],{type:'text/javascript'})));
      worker.onmessage=e=>window.result=e.data;''' % json.dumps(source))
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        result=page.evaluate('window.result')
        if isinstance(result,list):break
        (await asyncio.sleep(.01))
    assert result==[True,True,1]
