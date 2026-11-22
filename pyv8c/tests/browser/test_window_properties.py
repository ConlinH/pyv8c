import pytest

from pyv8c.async_api import Browser


@pytest.fixture
async def page():
    browser = (await Browser(cache=False, resource_loader=lambda url, request: '<div id="childOnly"></div>').__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content('''<div id="unique"></div><div id="duplicate"></div>
        <img name="duplicate"><form name="theForm"></form>
        <embed name="theEmbed"><object name="theObject"></object>
        <input name="notGlobal"><div name="alsoNotGlobal"></div>
        <div id="toString"></div><div id="document"></div>
        <iframe name="namedFrame" src="http://other.test/frame"></iframe>
        <div id="namedFrame"></div>''', url='http://example.test/', wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    try:
        yield page
    finally:
        (await browser.close())


async def test_ids_and_supported_name_attributes(page):
    assert page.evaluate('''[unique === document.getElementById('unique'),
        theForm.tagName, theEmbed.tagName, theObject.tagName,
        typeof notGlobal, typeof alsoNotGlobal, typeof missingGlobal,
        typeof window.toString, window.document === document]''') == [
        True, 'FORM', 'EMBED', 'OBJECT', 'undefined', 'undefined', 'undefined', 'function', True]


async def test_duplicates_return_live_html_collection(page):
    assert page.evaluate('''(() => { const matches = duplicate;
        const before = [matches instanceof HTMLCollection, matches.length, matches[0].tagName, matches[1].tagName];
        document.querySelector('img').setAttribute('name', 'changed');
        return before.concat([matches.length, duplicate === document.getElementById('duplicate')]);
    })()''') == [True, 2, 'DIV', 'IMG', 1, True]


async def test_named_frames_precede_elements_and_preserve_security(page):
    assert page.evaluate('[namedFrame === window[0], namedFrame === document.querySelector("iframe").contentWindow]') == [True, True]
    assert page.evaluate('try { namedFrame.document; } catch(e) { [e.name,e.code]; }') == ['SecurityError', 18]


async def test_shadow_delete_and_dom_mutation(page):
    assert page.evaluate('''(() => {
        const original = unique;
        window.unique = 42;
        const shadowed = window.unique;
        delete window.unique;
        const restored = window.unique === original;
        original.id = 'renamed';
        return [shadowed, restored, typeof unique, renamed === original];
    })()''') == [42, True, 'undefined', True]


async def test_named_properties_descriptors_and_no_dynamic_own_keys(page):
    assert page.evaluate('''(() => {
        const props = Object.getPrototypeOf(Window.prototype);
        const d = Object.getOwnPropertyDescriptor(props, 'unique');
        return [d.value === unique, d.writable, d.enumerable, d.configurable,
            'unique' in props, Object.keys(props).includes('unique'),
            Reflect.ownKeys(props).includes('unique'), Object.hasOwn(window, 'unique')];
    })()''') == [True, True, True, True, True, False, False, False]


async def test_direct_prototype_definition_and_deletion_are_rejected(page):
    assert page.evaluate('''(() => {
        const props = Object.getPrototypeOf(Window.prototype);
        return [Reflect.defineProperty(props, 'arbitrary', {value: 1}),
                Reflect.deleteProperty(props, 'unique'),
                Reflect.deleteProperty(props, 'missing'),
                props.unique === unique];
    })()''') == [False, False, False, True]


async def test_window_properties_do_not_intercept_symbols(page):
    assert page.evaluate('''(() => {
        const s = Symbol('unique'); window[s] = 7;
        return [window[s], window[Symbol('unique')] === undefined];
    })()''') == [7, True]


async def test_numeric_names_and_prototype_invariants(page):
    assert page.evaluate('''(() => {
        const node = document.createElement('div'); node.id = '123';
        document.body.appendChild(node);
        const props = Object.getPrototypeOf(Window.prototype);
        return [props[123] === node, Reflect.defineProperty(props, '123', {value: 1}),
                Reflect.deleteProperty(props, '123'),
                Reflect.setPrototypeOf(props, {}), Reflect.preventExtensions(props),
                Reflect.isExtensible(props), Object.hasOwn(props, 'constructor'),
                Reflect.set(props, 'unique', 42)];
    })()''') == [True, False, False, False, False, True, False, False]


async def test_borrowed_prototype_uses_owning_document(page):
    child = next(iter(page._owned.frames))
    (await child.set_content('<div id="childOnly"></div>', url='http://example.test/child', wait_until="commit"))
    props = child.evaluate_handle('Object.getPrototypeOf(Window.prototype)')
    page.executor.call(page.v8_context.expose, childProperties=props)
    page.executor.call(page.v8_context.expose,
                        childElement=child.evaluate_handle("document.getElementById('childOnly')"))
    #: Each realm has its own wrapper, so identity is asked of the owning realm.
    assert page.evaluate('''[childProperties.childOnly === childElement,
        childProperties.childOnly.id,
        childProperties.unique === undefined, 'childOnly' in childProperties,
        'unique' in childProperties]''') == [True, 'childOnly', True, True, False]
    (await child.set_content('<div id="childOnly"></div>', url='http://other.test/child', wait_until="commit"))
    page.executor.call(page.v8_context.expose,
                        childProperties=child.evaluate_handle('Object.getPrototypeOf(Window.prototype)'))
    assert page.evaluate('try { childProperties.childOnly; } catch (e) { [e.name, e.code]; }') == ['SecurityError', 18]


async def test_named_property_visibility_respects_real_properties(page):
    assert page.evaluate('''(() => {
        const props = Object.getPrototypeOf(Window.prototype);
        Object.defineProperty(window, 'unique', {value: 42, configurable: true});
        const hiddenByOwn = Object.getOwnPropertyDescriptor(props, 'unique') === undefined;
        delete window.unique;
        Window.prototype.unique = 73;
        const hiddenByPrototype = Object.getOwnPropertyDescriptor(props, 'unique') === undefined;
        delete Window.prototype.unique;
        return [hiddenByOwn, hiddenByPrototype, props.unique === document.getElementById('unique')];
    })()''') == [True, True, True]


