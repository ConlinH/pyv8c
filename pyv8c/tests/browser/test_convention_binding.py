"""A class writes bodies and nothing else: the baseline and the convention answer the rest."""

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")



async def test_a_member_the_class_writes_is_bound_without_a_declaration(shared_page):
    """``Element.setAttributeNS`` has a body and no declaration, so it used to do nothing."""
    (await shared_page.set_content("<div id='host'></div>", wait_until="commit"))

    assert shared_page.evaluate(
        "(() => {"
        " const el = document.getElementById('host');"
        " el.setAttributeNS(null, 'data-x', '1');"
        " return el.getAttribute('data-x');"
        "})()") == "1"


async def test_an_interface_that_states_nothing_still_answers_its_bodies(browser, monkeypatch):
    """The end state, on a real page: nothing stated, and the convention binds."""
    cls = browser.bindings.class_for("Element")
    monkeypatch.setattr(cls, "__idl_overrides__", {})
    page = browser.new_profile().new_page()
    try:
        (await page.set_content("<div id='host'></div>", wait_until="commit"))

        assert page.evaluate(
            "(() => {"
            " const el = document.getElementById('host');"
            " el.setAttribute('data-y', '2');"
            " el.id = 'renamed';"
            " return [el.getAttribute('data-y'), el.id, el.tagName].join('/');"
            "})()") == "2/renamed/DIV"
    finally:
        (await page.close())
