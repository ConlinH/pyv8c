import json
import pytest
pytest.importorskip('pyv8c.pyv8')
from harness import chrome_reference, ReferenceUnavailable
from pyv8c.async_api import Browser


@pytest.fixture(scope='module')
async def reference():
    try:
        async with chrome_reference() as reference:
            yield reference
    except ReferenceUnavailable as exc:
        pytest.skip(str(exc))


CORE = """globalThis.__log=[];globalThis.__done=false;
const reason={tag:'reason'};
const describe=e=>[e.type,e.promise===p,e.reason===reason,e.cancelable,e.bubbles,
                  e.composed,e.isTrusted,e.target===globalThis];
onunhandledrejection=e=>{
  __log.push(describe(e));e.preventDefault();setTimeout(()=>e.promise.catch(()=>{}),0);
  queueMicrotask(()=>__log.push(['microtask']));
};
onrejectionhandled=e=>{__log.push(describe(e));__PUBLISH__};
const p=Promise.reject(reason);'scheduled'
"""


@pytest.mark.parametrize('realm', ['window','frame','worker'])
async def test_rejection_event_tasks_shape_reason_and_order(reference, realm):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = CORE.replace('__PUBLISH__','__done=true')
    if realm == 'frame':
        source = CORE.replace('globalThis.__log=[]','globalThis.__log=parent.__log').replace('__PUBLISH__','parent.__done=true')
        script = 'globalThis.__log=[];globalThis.__done=false;frames[0].eval(' + json.dumps(source) + ");'scheduled'"
    if realm == 'worker':
        source = CORE.replace('__PUBLISH__','postMessage(__log)')
        script = ("globalThis.__log=[];globalThis.__done=false;globalThis.worker=new Worker("
                  "URL.createObjectURL(new Blob([" + json.dumps(source) + "])));"
                  "worker.onmessage=e=>{__log=e.data;__done=true};'scheduled'")
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe></iframe>', wait_until="commit"))
        (await page.wait_for_load_state())
        (await chrome.set_content('<iframe></iframe>'))
        (await chrome.evaluate(script))
        (await chrome.wait_for_function('__done === true'))
        page.evaluate(script)
        (await page.wait_for('__done === true',timeout=3))
        assert page.evaluate('__log') == (await chrome.evaluate('__log'))
    finally:
        (await chrome.close())
        (await browser.close())


async def test_handler_in_same_checkpoint_suppresses_notification(reference):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = """globalThis.log=[];globalThis.done=false;
      onunhandledrejection=e=>log.push(e.type);onrejectionhandled=e=>log.push(e.type);
      const p=Promise.reject('caught');queueMicrotask(()=>p.catch(()=>{}));
      setTimeout(()=>done=true,30);'scheduled' """
    try:
        page = browser.new_profile().new_page()
        (await chrome.evaluate(script))
        (await chrome.wait_for_function('done'))
        page.evaluate(script)
        (await page.wait_for('done'))
        assert page.evaluate('log') == (await chrome.evaluate('log')) == []
    finally:
        (await chrome.close())
        (await browser.close())


@pytest.mark.parametrize('catch', ['e.promise.catch(()=>{})', 'queueMicrotask(()=>e.promise.catch(()=>{}))'])
async def test_catch_during_notification_does_not_get_a_handled_event(reference, catch):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = """globalThis.log=[];globalThis.done=false;
      onunhandledrejection=e=>{log.push(e.type);e.preventDefault();__CATCH__;setTimeout(()=>done=true,20)};
      onrejectionhandled=e=>log.push(e.type);Promise.reject('caught');'queued' """.replace('__CATCH__',catch)
    try:
        page = browser.new_profile().new_page()
        (await chrome.evaluate(script))
        (await chrome.wait_for_function('done'))
        page.evaluate(script)
        (await page.wait_for('done'))
        assert page.evaluate('log') == (await chrome.evaluate('log')) == ['unhandledrejection']
    finally:
        (await chrome.close())
        (await browser.close())


async def test_exception_in_rejection_listener_reports_error_and_keeps_dispatching(reference):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = """globalThis.log=[];globalThis.done=false;
      onerror=()=>{log.push('error');return true};
      addEventListener('unhandledrejection',e=>{e.preventDefault();log.push('first');throw new Error('listener')});
      addEventListener('unhandledrejection',()=>{log.push('second');done=true});
      Promise.reject('rejected');'queued' """
    try:
        page = browser.new_profile().new_page()
        (await chrome.evaluate(script))
        (await chrome.wait_for_function('done'))
        page.evaluate(script)
        (await page.wait_for('done'))
        assert page.evaluate('log') == (await chrome.evaluate('log')) == ['first','error','second']
    finally:
        (await chrome.close())
        (await browser.close())


async def test_foreign_realm_promise_notifies_its_creation_global(reference):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = """globalThis.log=[];globalThis.done=false;
      const reason={tag:'shared'};
      onunhandledrejection=e=>{e.preventDefault();log.push('parent')};
      frames[0].onunhandledrejection=function(e){
        e.preventDefault();log.push(['child',e.reason===reason,e.promise===p,
          e.target===frames[0],e.currentTarget===frames[0],this===frames[0],
          e.composedPath()[0]===frames[0]]);
      };
      const p=new frames[0].Promise((resolve,reject)=>globalThis.reject=reject);
      reject(reason);setTimeout(()=>done=true,20);'queued' """
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe></iframe>', wait_until="commit"));(await page.wait_for_load_state())
        (await chrome.set_content('<iframe></iframe>'))
        (await chrome.evaluate(script));(await chrome.wait_for_function('done'))
        page.evaluate(script);(await page.wait_for('done'))
        assert page.evaluate('log') == (await chrome.evaluate('log')) == [['child',True,True,True,True,True,True]]
    finally:
        (await chrome.close());(await browser.close())
