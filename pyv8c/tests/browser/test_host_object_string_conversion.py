"""Web IDL string conversion retains the JavaScript wrapper's behavior."""
import pytest


@pytest.mark.parametrize('expression', [
    "document.createElement('span')", "document", "window", "navigator",
])
async def test_web_object_dom_string_uses_its_js_string_value(page, expression):
    assert page.evaluate('''(() => {
        const value = ''' + expression + ''';
        const target = document.createElement('div');
        target.setAttribute('data-probe', value);
        return target.getAttribute('data-probe') === String(value);
    })()''') is True


@pytest.mark.parametrize('kind', ['toString', 'toPrimitive'])
async def test_blob_part_conversion_propagates_a_host_wrappers_coercion_error(page, kind):
    member = 'Symbol.toPrimitive' if kind == 'toPrimitive' else "'toString'"
    assert page.evaluate('''(() => {
        const node = document.createElement('div');
        const sentinel = new Error('conversion sentinel');
        node[''' + member + '''] = () => { throw sentinel; };
        try { new Blob([node]); return false; }
        catch (error) { return error === sentinel; }
    })()''') is True


async def test_dom_string_calls_a_host_wrappers_override_once(page):
    assert page.evaluate('''(() => {
        const node = document.createElement('div');
        let calls = 0;
        node[Symbol.toPrimitive] = hint => { ++calls; return hint + '-value'; };
        const target = document.createElement('div');
        target.setAttribute('data-probe', node);
        return [target.getAttribute('data-probe'), calls];
    })()''') == ['string-value', 1]
