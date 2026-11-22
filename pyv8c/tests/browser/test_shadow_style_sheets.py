"""A shadow tree owns its stylesheet list independently of the document."""

from pyv8c.async_api import Browser


async def test_shadow_root_style_sheets_are_live_and_scoped():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const host = document.createElement('div');
            document.body.append(host);
            const root = host.attachShadow({mode: 'open'});
            const sheets = root.styleSheets;
            const before = sheets.length;
            root.innerHTML = '<style id="a">p{color:red}</style><p>x</p>';
            const style = root.querySelector('style');
            const sheet = sheets[0];
            const result = [
                before, sheets === root.styleSheets, sheets.length,
                sheet === sheets.item(0), sheet.ownerNode === style,
                sheet.cssRules.length, document.styleSheets.length,
                Object.prototype.toString.call(sheets),
                Object.getPrototypeOf(sheets).constructor.name
            ];
            style.remove();
            result.push(sheets.length);
            return result;
        })()""") == [0, True, 1, True, True, 1, 0,
                    "[object StyleSheetList]", "StyleSheetList", 0]
    finally:
        (await browser.close())
