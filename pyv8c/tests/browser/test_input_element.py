"""``HTMLInputElement``: value semantics, selection, stepping and validation."""

import pytest
from pyv8c import pyv8

from pyv8c.async_api import Browser


@pytest.fixture
async def page():
    browser = (await Browser(cache=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content(
        "<html><body><label for='text'>Name</label>"
        "<input id='text' type='text' value='seed' maxlength='10' required>"
        "<input id='check' type='checkbox' checked>"
        "<input id='number' type='number' min='0' max='10' step='2'>"
        "<input id='email' type='email'>"
        "<input id='file' type='file'></body></html>",
        url="https://input.test/",
     wait_until="commit"))
    try:
        yield page
    finally:
        (await browser.close())


async def test_value_tracks_the_dirty_flag_and_keeps_the_attribute(page):
    assert page.evaluate("""(() => {
        const input = document.getElementById('text');
        const before = [input.value, input.defaultValue, input.getAttribute('value')];
        input.value = 'typed';
        return [before, input.value, input.getAttribute('value'), input.defaultValue,
            (input.defaultValue = 'seed2', input.value)];
    })()""") == [
        ["seed", "seed", "seed"], "typed", "seed", "seed", "typed",
    ]


async def test_type_is_sanitized_to_a_known_value(page):
    assert page.evaluate("""(() => {
        const input = document.getElementById('text');
        input.type = 'TEXT';
        const kept = input.getAttribute('type');
        const unknown = (input.type = 'banana', input.type);
        return [kept, unknown, input.type];
    })()""") == ["TEXT", "text", "text"]


async def test_checkbox_click_toggles_and_fires_activation_events(page):
    assert page.evaluate("""(() => {
        const check = document.getElementById('check');
        const seen = [];
        check.addEventListener('input', () => seen.push('input'));
        check.addEventListener('change', () => seen.push('change'));
        const before = check.checked;
        check.click();
        return [before, check.checked, check.defaultChecked, seen];
    })()""") == [True, False, True, ["input", "change"]]


async def test_color_input_sanitizes_default_attribute_and_assigned_value(page):
    assert page.evaluate("""(() => {
        const input = document.createElement('input');
        input.type = 'color';
        const values = [input.value];
        input.setAttribute('value', '#ABCDEF'); values.push(input.value);
        input.value = '#Ff0012'; values.push(input.value);
        input.value = 'invalid'; values.push(input.value);
        return values;
    })()""") == ['#000000', '#abcdef', '#ff0012', '#000000']


async def test_number_input_steps_and_sanitizes(page):
    assert page.evaluate("""(() => {
        const number = document.getElementById('number');
        const empty = number.valueAsNumber;
        number.value = '8';
        const asNumber = number.valueAsNumber;
        number.stepUp();
        const stepped = number.value;
        number.stepDown(3);
        const steppedDown = number.value;
        number.value = 'garbage';
        return [Number.isNaN(empty), asNumber, stepped, steppedDown, number.value,
            (number.value = '20', number.validity.rangeOverflow), number.checkValidity()];
    })()""") == [True, 8, "10", "4", "", True, False]


async def test_text_input_supports_selection_apis_and_others_throw(page):
    assert page.evaluate("""(() => {
        const text = document.getElementById('text');
        text.value = 'hello';
        text.setSelectionRange(1, 3);
        const selected = [text.selectionStart, text.selectionEnd,
            text.value.slice(text.selectionStart, text.selectionEnd)];
        text.select();
        const whole = [text.selectionStart, text.selectionEnd];
        const checkbox = document.getElementById('check');
        let thrown = null;
        try { checkbox.selectionStart; } catch (error) { thrown = error.name; }
        return [selected, whole, thrown];
    })()""") == [[1, 3, "el"], [0, 5], "InvalidStateError"]


async def test_constraint_validation_reports_each_flag(page):
    assert page.evaluate("""(() => {
        const text = document.getElementById('text');
        const email = document.getElementById('email');
        text.value = '';
        const missing = [text.validity.valueMissing, text.checkValidity()];
        text.required = false;
        text.value = 'abcdef';
        text.maxLength = 3;
        const tooLong = [text.validity.tooLong, text.checkValidity()];
        text.maxLength = 10;
        email.value = 'nope';
        const mismatch = [email.validity.typeMismatch, email.checkValidity()];
        email.value = 'user@example.com';
        const valid = email.checkValidity();
        return [missing, tooLong, mismatch, valid,
            document.getElementById('file').files.length];
    })()""") == [[True, False], [True, False], [True, False], True, 0]


async def test_file_input_exposes_an_empty_file_list_and_others_throw(page):
    assert page.evaluate("""(() => {
        const file = document.getElementById('file');
        const text = document.getElementById('text');
        let thrown = null;
        try { text.files; } catch (error) { thrown = error.name; }
        return [file.files.length, file.files.item(0), thrown];
    })()""") == [0, pyv8.Null, "InvalidStateError"]


async def test_labels_and_form_owner(page):
    (await page.set_content(
        "<html><body><form id='f'><label for='in'>L</label><input id='in' form='f'></form>"
        "<label><input id='wrapped'></label></body></html>",
        url="https://input.test/",
     wait_until="commit"))
    assert page.evaluate("""(() => {
        const inside = document.getElementById('in');
        const wrapped = document.getElementById('wrapped');
        return [inside.form.id, inside.labels.length, inside.labels[0].textContent,
            wrapped.labels.length, wrapped.form];
    })()""") == ["f", 1, "L", 1, pyv8.Null]


async def test_webkit_entries_is_a_fresh_frozen_array(page):
    assert page.evaluate("""(() => {
        const input = document.getElementById('file');
        const first = input.webkitEntries;
        return [first === input.webkitEntries, Array.isArray(first),
                Object.isFrozen(first), first.length,
                Object.prototype.toString.call(first)];
    })()""") == [False, True, True, 0, "[object Array]"]
