import pytest

pyv8 = pytest.importorskip("pyv8c.pyv8")
from pyv8c.async_api import Browser


async def test_client_information_aliases_navigator():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("[window.clientInformation === window.navigator, clientInformation === navigator, clientInformation.userAgent === navigator.userAgent]") == [True, True, True]
    finally:
        (await browser.close())


async def test_client_hints_reach_a_worker():
    """A worker global gets the same hints, through the feature that owns them."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
        page.evaluate("""(() => {
            window.__worker = 'pending';
            const source = new Blob([
                "postMessage(JSON.stringify([typeof navigator.userAgentData," +
                "navigator.userAgentData.brands.map(brand => brand.brand).join(',')," +
                "typeof NavigatorUAData, Object.isFrozen(navigator.userAgentData.brands)]));"
            ], {type: 'text/javascript'});
            const worker = new Worker(URL.createObjectURL(source));
            worker.onmessage = event => window.__worker = event.data;
            worker.onerror = event => window.__worker = 'error:' + event.message;
        })()""")
        (await page.wait_for("window.__worker !== 'pending'", timeout=10))
        assert page.evaluate("window.__worker") ==\
            '["object","Not:A-Brand,Chromium,Google Chrome","function",true]'
    finally:
        (await browser.close())


