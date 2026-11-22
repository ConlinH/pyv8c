"""``new Option(...)`` builds the element Chrome's legacy factory builds."""

from pyv8c.async_api import Browser

CASES = """(() => {
  const cases = [
    () => { const o = new Option(); return [o.text, o.value, o.outerHTML]; },
    () => { const o = new Option('only'); return [o.text, o.value, o.outerHTML]; },
    () => { const o = new Option('t', 'v', true, false); return [o.text, o.value, o.defaultSelected, o.selected, o.outerHTML]; },
    () => { const o = new Option('t', 'v', false, true); return [o.text, o.value, o.defaultSelected, o.selected, o.outerHTML]; },
  ];
  return cases.map((run) => run());
})()"""


async def test_a_factory_shares_the_interface_prototype():
    """Chromium's legacy factories reuse the interface prototype object.

    ``Option.prototype`` is ``HTMLOptionElement.prototype`` -- the factory is a distinct
    function, so ``Option !== HTMLOptionElement``, but its prototype (and therefore its
    members and ``instanceof``) is the interface's own.
    """
    browser = (await Browser(cache=False, real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const pairs = [['Option', 'HTMLOptionElement'],
                           ['Image', 'HTMLImageElement'],
                           ['Audio', 'HTMLAudioElement']];
            return pairs.map(([factory, iface]) => [
                window[factory].prototype === window[iface].prototype,
                window[factory] === window[iface],
                new window[factory]() instanceof window[iface],
                Object.getOwnPropertyNames(window[factory].prototype).length
                    === Object.getOwnPropertyNames(window[iface].prototype).length,
            ]);
        })()""") == [[True, False, True, True]] * 3
    finally:
        (await browser.close())


async def test_option_arguments_fill_text_and_value():
    browser = (await Browser(cache=False, real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate(CASES) == [
            ["", "", "<option></option>"],
            ["only", "only", "<option>only</option>"],
            ["t", "v", True, False, '<option value="v" selected="">t</option>'],
            ["t", "v", False, True, '<option value="v">t</option>'],
        ]
    finally:
        (await browser.close())
