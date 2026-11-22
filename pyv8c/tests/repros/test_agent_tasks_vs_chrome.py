import pytest
pytest.importorskip('pyv8c.pyv8')
from harness import chrome_reference, ReferenceUnavailable
from pyv8c.async_api import Browser


@pytest.fixture(scope='module')
async def reference():
    try:
        async with chrome_reference() as reference:
            yield reference
    except ReferenceUnavailable as error:
        pytest.skip(str(error))


async def test_same_agent_messages_across_frames_preserve_source_order(reference):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    chrome = (await reference.browser.new_page())
    script = """globalThis.log=[];
      addEventListener('message',event=>log.push(event.data));
      frames[0].addEventListener('message',event=>log.push(event.data));
      postMessage('parent-first','*');
      frames[0].postMessage('child','*');
      postMessage('parent-last','*');'queued' """
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<iframe></iframe>', wait_until="commit"));(await page.wait_for_load_state())
        (await chrome.set_content('<iframe></iframe>'))
        (await chrome.evaluate(script));(await chrome.wait_for_function('log.length===3'))
        page.evaluate(script);(await page.wait_for('log.length===3'))
        assert page.evaluate('log') == (await chrome.evaluate('log')) == ['parent-first','child','parent-last']
    finally:
        (await chrome.close());(await browser.close())
