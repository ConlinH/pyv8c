"""Regression coverage for HTML form state and canvas context selection."""
import pytest

from pyv8c.async_api import Browser


@pytest.fixture
async def page():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/page", wait_until="commit"))
        yield page
    finally:
        (await browser.close())


async def test_textarea_value_is_independent_and_reset(page):
    assert page.evaluate("""(() => {
      const f = document.createElement('form');
      f.innerHTML = '<textarea name=t>initial</textarea>'; document.body.appendChild(f);
      const t = f.querySelector('textarea'); const before = t.value;
      t.value = 'changed'; t.defaultValue = 'updated';
      const edited = [t.value, t.defaultValue]; f.reset();
      return [before, edited, t.value];
    })()""") == ['initial', ['changed', 'updated'], 'updated']


async def test_select_exclusivity_and_reset(page):
    assert page.evaluate("""(() => {
      const f = document.createElement('form');
      f.innerHTML = '<select><option selected value=a>A</option><option value=b>B</option></select>';
      document.body.appendChild(f); const s = f.querySelector('select');
      s.options[1].selected = true;
      const changed = [s.value, s.selectedOptions.length]; f.reset();
      return [changed, s.value, s.selectedOptions.length];
    })()""") == [['b', 1], 'a', 1]


async def test_radio_groups_and_form_owner(page):
    assert page.evaluate("""(() => {
      const f = document.createElement('form'); f.id = 'owner';
      f.innerHTML = '<input type=radio name=x><input type=radio name=x>' +
        '<input form=missing><button form=missing></button>' +
        '<textarea form=missing></textarea><select form=missing></select>';
      document.body.appendChild(f); const inputs = f.querySelectorAll('input');
      inputs[0].checked = true; inputs[1].checked = true;
      return [inputs[0].checked, inputs[1].checked, inputs[2].form === null,
        f.querySelector('button').form === null, f.querySelector('textarea').form === null,
        f.querySelector('select').form === null, f.elements.length];
    })()""") == [False, True, True, True, True, True, 2]


async def test_radio_checked_attributes_are_exclusive(page):
    assert page.evaluate("""(() => {
      const div = document.createElement('div');
      div.innerHTML = '<input type=radio name=x checked><input type=radio name=x checked>';
      document.body.appendChild(div);
      return Array.from(div.children).map(input => input.checked);
    })()""") == [False, True]


async def test_failed_canvas_options_do_not_lock_context(page):
    assert page.evaluate("""(() => {
      const results = [];
      for (const c of [document.createElement('canvas'), new OffscreenCanvas(1, 1)]) {
        try { c.getContext('2d', {colorSpace: 'invalid'}); } catch (e) {}
        results.push(c.getContext('webgl') !== null);
      }
      return results;
    })()""") == [True, True]


async def test_submitter_overrides_request():
    from pyv8c.web.html.forms import HTMLFormElement, submission
    from pyv8c.web.html.inputs import HTMLInputElement
    form = HTMLFormElement('form', attributes={'action': 'https://example.test/base', 'method': 'get'})
    submitter = HTMLInputElement('input', attributes={'type': 'submit', 'formaction': 'https://example.test/override',
                                  'formmethod': 'post', 'formenctype': 'text/plain'})
    form.append_child(submitter)
    request = submission(form, submitter)
    assert request.url == 'https://example.test/override'
    assert request.method == 'POST'
    assert request.content_type == 'text/plain'


async def test_submitter_can_skip_form_validation(page):
    assert page.evaluate("""(() => {
      const form = document.createElement('form');
      form.innerHTML = '<input required><button type=submit formnovalidate>go</button>';
      document.body.appendChild(form);
      let submits = 0;
      form.addEventListener('submit', event => { submits++; event.preventDefault(); });
      form.requestSubmit(); const without = submits;
      form.requestSubmit(form.querySelector('button'));
      return [without, submits];
    })()""") == [0, 1]
