"""Timers report themselves at debug level: armed, run, cleared."""
from loguru import logger

from pyv8c.async_api import Browser

PAGE = ("<body><script>"
        "function tick() {globalThis.__ticked=true}"
        "const first = setTimeout(tick, 30);"
        "const repeat = setInterval('void 0', 500);"
        "const third = setTimeout(tick, 10);"
        "clearTimeout(third);"
        "clearInterval(repeat);"
        "</script></body>")


async def test_timers_report_themselves():
    messages = []
    token = logger.add(lambda message: messages.append(str(message).strip()), level="DEBUG",
                       format="{message}")
    try:
        browser = (await Browser(stub_logging="off").__aenter__())
        try:
            page = browser.new_profile().new_page()
            (await page.set_content(PAGE, url="https://repro.test/index.html", wait_until="commit"))
            (await page.wait_for('globalThis.__ticked === true', timeout=2))
        finally:
            (await browser.close())
    finally:
        logger.remove(token)

    timeouts = [m for m in messages if m.startswith("setTimeout ")]
    assert any(m.endswith("scheduled in 30.0 ms (function)") for m in timeouts)
    assert any(m.startswith("setInterval ")
               and m.endswith("scheduled in 500.0 ms (string)") for m in messages)
    assert any(m.startswith("clearTimeout ") for m in messages)
    assert any(m.startswith("clearInterval ") for m in messages)
    # The timer left standing is the one that runs; the cleared pair never does.
    assert [m.split()[1] for m in timeouts if m.endswith(" runs")] ==\
        [m.split()[1] for m in timeouts if "scheduled in 30.0 ms" in m]
