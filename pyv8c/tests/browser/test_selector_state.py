"""Selector state pseudo-classes, and the selector subset the parser accepts."""
import pytest

from pyv8c.async_api import Browser

DEFAULT_HTML = "<div id='a' class='one two'><span class='one'>x</span><span>y</span></div>"


async def page_with(html=DEFAULT_HTML):
    browser = (await Browser().__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content(html, url="http://example.test/", wait_until="commit"))
    return browser, page


async def test_structural_pseudo_classes_and_live_collections():
    browser, page = (await page_with())
    assert page.evaluate("[document.querySelector('#a').children.length, document.querySelector('#a').classList.contains('one'), document.querySelector('#a').attributes.length]") == [2, True, 2]
    assert page.evaluate("document.querySelectorAll('div > span:first-of-type').length") == 1
    assert page.evaluate("document.querySelectorAll('span:not(:first-child)').length") == 1
    (await page.close()); (await browser.close())


async def test_form_state_pseudo_classes():
    browser, page = (await page_with("""
      <form>
        <input id='checked' type='checkbox' checked>
        <input id='unchecked' type='radio'>
        <input id='disabled-input' disabled>
        <button id='enabled-button'>go</button>
        <button id='disabled-button' disabled>stop</button>
        <select><option id='selected' selected>yes</option><option id='other'>no</option></select>
        <select><optgroup disabled><option id='group-disabled'>no</option></optgroup></select>
      </form>
    """))
    assert page.evaluate("[document.querySelectorAll(':checked').length, document.querySelectorAll('input:checked').length, document.querySelectorAll('option:checked').length]") == [2, 1, 1]
    assert page.evaluate("[document.querySelectorAll('input:enabled').length, document.querySelectorAll('button:enabled').length, document.querySelectorAll(':disabled').length]") == [2, 1, 4]
    assert page.evaluate("[document.querySelectorAll('option:enabled').length, document.querySelector('#checked').matches(':checked'), document.querySelector('#disabled-input').matches(':enabled'), document.querySelector('#group-disabled').matches(':disabled')]") == [2, True, False, True]
    with pytest.raises(BaseException):
        page.evaluate("document.querySelector('input:checked(foo)')")
    (await page.close()); (await browser.close())
