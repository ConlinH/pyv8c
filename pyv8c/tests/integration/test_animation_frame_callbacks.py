"""Rendering callbacks retain cancellation handles until their own invocation."""
import pytest

from pyv8c.async_api import Browser


@pytest.mark.parametrize('microtask', [False, True])
@pytest.mark.parametrize('worker', [False, True])
async def test_frame_callback_can_cancel_later_callback(microtask, worker):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        cancel = "log.push('cancel');cancelAnimationFrame(second)"
        if microtask:
            cancel = 'queueMicrotask(()=>{' + cancel + '})'
        source = """
            const log=[];
            requestAnimationFrame(()=>{log.push('first');__CANCEL__});
            const second=requestAnimationFrame(()=>log.push('cancelled'));
            requestAnimationFrame(()=>{log.push('last');__DONE__});
        """.replace('__CANCEL__', cancel)
        if worker:
            import json
            source = source.replace('__DONE__', 'postMessage(log)')
            page.evaluate("globalThis.result=null;globalThis.worker=new Worker("
                          "URL.createObjectURL(new Blob([" + json.dumps(source) + "])));"
                          "worker.onmessage=e=>result=e.data")
        else:
            page.evaluate('globalThis.result=null;' + source.replace('__DONE__', 'result=log'))
        (await page.wait_for('result!==null', timeout=3))
        assert page.evaluate('result') == ['first', 'cancel', 'last']
    finally:
        (await browser.close())


async def test_frame_appended_in_cleanup_uses_next_frame():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""
            globalThis.log=[];globalThis.done=false;let first;
            requestAnimationFrame(t=>{
                first=t;log.push('first');
                queueMicrotask(()=>requestAnimationFrame(u=>{
                    log.push(u>first?'next':'same');done=true;
                }));
            });
            requestAnimationFrame(t=>log.push(t===first?'peer':'wrong'));
        """)
        (await page.wait_for('done', timeout=3))
        assert page.evaluate('log') == ['first', 'peer', 'next']
    finally:
        (await browser.close())


async def test_throwing_frame_callback_still_runs_cleanup():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""
            globalThis.log=[];globalThis.done=false;
            addEventListener('error',e=>{e.preventDefault();log.push('error')});
            requestAnimationFrame(()=>{
                queueMicrotask(()=>log.push('micro'));
                throw new Error('frame failure');
            });
            requestAnimationFrame(()=>{log.push('last');done=true});
        """)
        (await page.wait_for('done', timeout=3))
        assert page.evaluate('log') == ['error', 'micro', 'last']
    finally:
        (await browser.close())
