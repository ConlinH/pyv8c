import pytest

pyv8 = pytest.importorskip("pyv8c.pyv8")
from pyv8c.async_api import Browser


async def test_evaluate_data_and_handle_boundaries():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("({items: [1, 2]})") == {"items": [1, 2]}
        value = page.evaluate_handle("({items: [1, 2]})")
        assert isinstance(value, pyv8.JSObject)
        assert page.executor.call(value.to_python) == {"items": [1, 2]}
        (await page.close())
        with pytest.raises(RuntimeError):
            value.to_python()
    finally:
        (await browser.close())
