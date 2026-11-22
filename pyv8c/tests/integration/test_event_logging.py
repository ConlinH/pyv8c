"""Every dispatched event reports itself at debug level."""
from loguru import logger

from pyv8c.async_api import Browser

PAGE = ("<body><script>addEventListener('load', () => {});"
        "addEventListener('DOMContentLoaded', () => {});</script></body>")


async def test_each_dispatch_reports_itself():
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), level="DEBUG",
                       format="{message}")
    try:
        browser = (await Browser(stub_logging="off").__aenter__())
        try:
            page = browser.new_profile().new_page()
            (await page.set_content(PAGE, url="https://repro.test/index.html", wait_until="commit"))
            (await page.run_until_idle(timeout=2))
            page.evaluate("dispatchEvent(new Event('probe'))")
        finally:
            (await browser.close())
    finally:
        logger.remove(token)

    assert any("dispatch DOMContentLoaded on HTMLDocument (trusted=True" in message
               for message in messages)
    assert any("dispatch load on Window (trusted=True, listeners=1)" in message
               for message in messages)
    assert any("dispatch readystatechange on HTMLDocument" in message for message in messages)
    # The page's own dispatch is reported too, and marked as not trusted.
    assert any("dispatch probe on Window (trusted=False, listeners=0)" in message
               for message in messages)
