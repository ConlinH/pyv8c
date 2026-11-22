"""Serial local iterable comparison using the explicit migrated baseline."""
import pytest
from harness import chrome_reference, ReferenceUnavailable
from pyv8c.async_api import Browser
from pathlib import Path
from runpy import run_path
_cases = run_path(str(Path(__file__).resolve().parents[1] / "browser" / "test_iteration.py"))
CASES, IDL = _cases["CASES"], _cases["IDL"]

@pytest.fixture(scope='module')
async def reference():
    try:
        async with chrome_reference() as session: yield session
    except ReferenceUnavailable as exc: pytest.skip(str(exc))

@pytest.mark.parametrize('name,script', CASES)
async def test_iteration_matches_chrome(reference,name,script):
    chrome=(await reference.browser.new_page())
    browser=(await Browser(idl_path=IDL, real_network=False, stub_logging='off').__aenter__())
    try:
        (await chrome.route('https://iteration.test/**',lambda route: route.fulfill(status=200,content_type='text/html',body='<body></body>')))
        (await chrome.goto('https://iteration.test/'))
        page=browser.new_profile().new_page();(await page.set_content('<body></body>',url='https://iteration.test/', wait_until="commit"))
        assert page.evaluate('('+script+')()') == (await chrome.evaluate(script))
    finally:
        (await browser.close());(await chrome.close())
