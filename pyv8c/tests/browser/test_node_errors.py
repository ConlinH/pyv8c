"""What ``replaceChild`` refuses, and the words it refuses with.

The 23:04 trace had ``HTMLBodyElement.replaceChild(undefined, <HTMLDivElement>)`` raise, and a
raise is only a difference if the other side does not raise: asked of both browsers
on 2026-10-06, Chrome answers the same ``TypeError``
with the same words.  The one thing that did differ was the missing call prefix on
``NotFoundError``, which is what this holds.
"""
import pytest

from pyv8c.async_api import Browser

PAGE_HTML = "<body><div id='root'></div></body>"


@pytest.fixture
async def page():
    browser = (await Browser(stub_logging="off").__aenter__())
    instance = browser.new_profile().new_page()
    (await instance.set_content(PAGE_HTML, url="https://node.test/", wait_until="commit"))
    try:
        yield instance
    finally:
        (await browser.close())


async def test_replace_child_refuses_with_chrome_s_wording(page):
    assert page.evaluate("""(() => {
        const messages = [];
        const root = document.getElementById('root');
        const empty = document.createElement('div');
        try { root.replaceChild(undefined, empty); messages.push('no error'); }
        catch (error) { messages.push(error.name + ': ' + error.message); }
        try { root.replaceChild(empty, document.createElement('p')); messages.push('no error'); }
        catch (error) { messages.push(error.name + ': ' + error.message); }
        const valid = document.createElement('span');
        root.appendChild(valid);
        const replaced = root.replaceChild(document.createElement('b'), valid);
        messages.push(replaced.tagName + '/' + valid.isConnected + '/' + root.lastChild.tagName);
        return messages;
    })()""") == [
        "TypeError: Failed to execute 'replaceChild' on 'Node': "
        "parameter 1 is not of type 'Node'.",
        "NotFoundError: Failed to execute 'replaceChild' on 'Node': "
        "The node to be replaced is not a child of this node.",
        "SPAN/false/B",
    ]
