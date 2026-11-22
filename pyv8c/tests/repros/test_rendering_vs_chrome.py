"""Local page-tree rendering observations; no online site dependencies."""
import pytest
from harness import chrome_reference
from pyv8c.async_api import Browser


@pytest.fixture(scope='module')
async def reference():
    async with chrome_reference(baseline=True) as reference:
        yield reference


@pytest.mark.parametrize('mode', ['parent-first', 'child-first', 'parent-add-child', 'microtask-add-child'])
async def test_frame_clock_across_parent_and_child(reference, mode):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = """globalThis.samples=[];globalThis.done=false;
      let count=0,tick=0;
      requestAnimationFrame(function advance(){
        tick++;if(!done)requestAnimationFrame(advance);
      });
      function sample(w,name) {
        w.requestAnimationFrame(t=>{
          samples.push([name,tick]);
          if(++count===2)done=true;
        });
      }
      __REQUESTS__;'scheduled'
    """
    requests = {
        'parent-first': "sample(window,'parent');sample(frames[0],'child')",
        'child-first': "sample(frames[0],'child');sample(window,'parent')",
        'parent-add-child': "requestAnimationFrame(t=>{samples.push(['parent',tick]);"
                            "count++;sample(frames[0],'child')})",
        'microtask-add-child': "requestAnimationFrame(t=>{samples.push(['parent',tick]);"
                              "count++;queueMicrotask(()=>sample(frames[0],'child'))})",
    }
    script = script.replace('__REQUESTS__', requests[mode])
    try:
        page = browser.new_profile().new_page()
        for current in (chrome, page):
            if current is chrome:
                await current.set_content('<body><iframe></iframe></body>')
            else:
                (await current.set_content('<body><iframe></iframe></body>', wait_until="commit"))
            if current is page:
                (await current.wait_for_load_state(timeout=3))
            # Finish initial layout/rendering work, then enter a separate task.
            ((await current.evaluate("globalThis.primed=false;frames[0].requestAnimationFrame(()=>{});"
                             "requestAnimationFrame(()=>requestAnimationFrame(()=>"
                             "setTimeout(()=>primed=true,0)))")) if current is chrome else current.evaluate("globalThis.primed=false;frames[0].requestAnimationFrame(()=>{});"
                             "requestAnimationFrame(()=>requestAnimationFrame(()=>"
                             "setTimeout(()=>primed=true,0)))"))
            if current is page:
                (await current.wait_for('primed', timeout=3))
            else:
                (await current.wait_for_function('primed', timeout=3000))
            ((await current.evaluate(script)) if current is chrome else current.evaluate(script))
            if current is page:
                (await current.wait_for('done', timeout=3))
            else:
                (await current.wait_for_function('done', timeout=3000))
        actual, expected = page.evaluate('samples'), (await chrome.evaluate('samples'))
        assert actual == expected
        assert expected == [['parent', 1], ['child', 2 if mode.endswith('add-child') else 1]]
    finally:
        (await browser.close())
        (await chrome.close())


@pytest.mark.parametrize('mode', ['cancel', 'remove', 'reinsert', 'sibling-order'])
async def test_parent_can_change_child_frame_delivery(reference, mode):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = """globalThis.log=[];globalThis.done=false;
        const frame=document.querySelector('iframe');
        const child=frame.contentWindow;
        const handle=child.requestAnimationFrame(()=>log.push('child'));
        __ACTION__;
        requestAnimationFrame(()=>requestAnimationFrame(()=>done=true));'scheduled'
    """
    action = {
        'cancel': 'requestAnimationFrame(()=>{log.push("parent");child.cancelAnimationFrame(handle)})',
        'remove': 'requestAnimationFrame(()=>{log.push("parent");frame.remove()})',
        'reinsert': 'requestAnimationFrame(()=>{log.push("parent");frame.remove();document.body.appendChild(frame)})',
        'sibling-order': 'frames[1].requestAnimationFrame(()=>log.push("second-child"))',
    }[mode]
    script = script.replace('__ACTION__', action)
    try:
        page = browser.new_profile().new_page()
        results = []
        for current in (chrome, page):
            if current is chrome:
                await current.set_content('<body><iframe></iframe><iframe></iframe></body>')
            else:
                (await current.set_content('<body><iframe></iframe><iframe></iframe></body>', wait_until="commit"))
            if current is page:
                (await current.wait_for_load_state(timeout=3))
            ((await current.evaluate(script)) if current is chrome else current.evaluate(script))
            if current is page:
                (await current.wait_for('done', timeout=3))
            else:
                (await current.wait_for_function('done', timeout=3000))
            results.append(((await current.evaluate('log')) if current is chrome else current.evaluate('log')))
        assert results[0] == results[1] == (['child', 'second-child'] if mode == 'sibling-order' else ['parent'])
    finally:
        (await browser.close())
        (await chrome.close())
