"""Moving or cloning an executed script must not execute its source again."""
from pyv8c.async_api import Browser
import pytest


@pytest.fixture
async def browser():
    instance = (await Browser(real_network=False).__aenter__())
    try:
        yield instance
    finally:
        (await instance.close())


async def test_executed_script_move_clone_and_text_update_are_inert(browser):
    page = browser.new_profile().new_page()
    (await page.set_content('<body><script id="x">window.calls=(window.calls||0)+1</script></body>', wait_until="commit"))
    result = page.evaluate('''(()=>{
        const script=document.getElementById('x');
        document.body.appendChild(script);
        document.body.appendChild(script.cloneNode(true));
        script.textContent='window.calls+=10';
        return calls;
    })()''')
    assert result == 1


async def test_clone_of_unexecuted_script_still_executes_once(browser):
    page = browser.new_profile().new_page()
    (await page.set_content('<body></body>', wait_until="commit"))
    result = page.evaluate('''(()=>{
        window.calls=0;
        const script=document.createElement('script');
        script.textContent='window.calls++';
        const clone=script.cloneNode(true);
        document.body.appendChild(clone);
        document.body.appendChild(clone);
        document.body.appendChild(script);
        return calls;
    })()''')
    assert result == 2
