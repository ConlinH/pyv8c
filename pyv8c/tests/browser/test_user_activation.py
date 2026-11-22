from pyv8c.async_api import Browser


async def test_trusted_click_sets_user_activation_but_script_click_does_not():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<button id="x">ok</button>', url='https://example.test/', wait_until="commit"))
        flags = '[navigator.userActivation.isActive,navigator.userActivation.hasBeenActive]'
        assert page.evaluate(flags) == [False, False]
        page.evaluate("document.querySelector('#x').click()")
        assert page.evaluate(flags) == [False, False]
        (await page.click('#x'))
        assert page.evaluate(flags) == [True, True]
    finally:
        (await browser.close())
