"""CSSOM inline style behavior exposed by HTMLElement.style."""

import pytest

from pyv8c.async_api import Browser


@pytest.fixture
async def page():
    browser = (await Browser(cache=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content(
        '<div id="target" style="color: red; margin-top: 2px; '
        'color: blue !important; broken"></div>',
        url="http://style.test/",
     wait_until="commit"))
    try:
        yield page
    finally:
        (await browser.close())


async def test_style_identity_cssom_and_initial_parse(page):
    assert page.evaluate("""(() => {
        const e = target;
        return [e.style === e.style, Object.prototype.toString.call(e.style),
            e.style.cssText, e.style.length, e.style.color,
            e.style.getPropertyPriority('color'), e.style.item(0),
            e.style.item(99), e.style.parentRule === null];
    })()""") == [
        True, "[object CSSStyleDeclaration]",
        "color: blue !important; margin-top: 2px;", 2, "blue", "important",
        "color", "", True,
    ]


async def test_inline_declarations_remain_live_across_attribute_and_cssom_writes(page):
    assert page.evaluate("""(() => {
        const element = target, style = element.style;
        const original = style.color;
        element.setAttribute('style', 'color: red; width: 4px;');
        const attribute = [style.color, style.width];
        style.width = '8px';
        style.setProperty('height', '12px');
        const written = [style.color, style.width, style.height];
        element.removeAttribute('style');
        const removed = [style.color, style.width, style.length];
        style.cssText = 'padding: 1px 2px; color: green !important;';
        style.removeProperty('padding-left');
        return [original, attribute, written, removed, style.paddingLeft,
                style.paddingRight, style.getPropertyPriority('color')];
    })()""") == ['blue', ['red', '4px'], ['red', '8px', '12px'],
                  ['', '', 0], '', '2px', 'important']


async def test_color_keywords_serialize_and_invalid_assignments_are_ignored(page):
    assert page.evaluate('''(() => {
      const e = document.createElement('div'); document.body.appendChild(e);
      e.style.color = 'AccentColor';
      const accent = [e.style.color, getComputedStyle(e).color];
      e.style.color = '-moz-ButtonDefault'; e.style.color = 'FakeColor';
      const invalid = [e.style.color, getComputedStyle(e).color];
      e.style.color = 'transparent';
      const transparent = [e.style.color, getComputedStyle(e).color];
      e.style.cssText = 'color: blue; color: invalid-name;';
      return [accent, invalid, transparent, e.style.color, getComputedStyle(e).color];
    })()''') == [
        ['accentcolor', 'rgb(0, 117, 255)'], ['accentcolor', 'rgb(0, 117, 255)'],
        ['transparent', 'rgba(0, 0, 0, 0)'], 'blue', 'rgb(0, 0, 255)',
    ]


async def test_computed_style_inherits_and_starts_from_the_initial_value(page):
    """A declared colour reaches a child; the inline object still reads its own only."""
    assert page.evaluate("""(() => {
        const style = document.createElement('style');
        style.textContent = '.outer { color: rgb(4, 5, 6); white-space: pre; }';
        document.body.appendChild(style);
        const outer = document.createElement('div');
        outer.className = 'outer';
        const inner = document.createElement('span');
        outer.appendChild(inner);
        document.body.appendChild(outer);
        return [getComputedStyle(inner).color, getComputedStyle(inner).whiteSpace,
                getComputedStyle(outer).color, getComputedStyle(target).color,
                inner.style.color, outer.style.color];
    })()""") == ["rgb(4, 5, 6)", "pre", "rgb(4, 5, 6)", "rgb(0, 0, 255)", "", ""]


async def test_style_named_properties_and_custom_properties(page):
    assert page.evaluate("""(() => {
        const s = target.style;
        s.backgroundColor = 'white';
        s['margin-left'] = '3px';
        s['--theme-color'] = '  red   blue ';
        return [s.backgroundColor, s.getPropertyValue('margin-left'),
            s['--theme-color'], s.cssText, target.getAttribute('style')];
    })()""") == [
        "white", "3px", "red blue",
        "color: blue !important; margin-top: 2px; background-color: white; "
        "margin-left: 3px; --theme-color: red blue;",
        "color: blue !important; margin-top: 2px; background-color: white; "
        "margin-left: 3px; --theme-color: red blue;",
    ]


async def test_style_methods_priority_removal_and_css_text(page):
    assert page.evaluate("""(() => {
        const s = target.style;
        s.setProperty('padding-top', '4px', 'IMPORTANT');
        const old = s.removeProperty('margin-top');
        const missing = s.removeProperty('does-not-exist');
        s.cssText = 'border: 0; invalid; opacity: .5 ! important; border: 1px';
        return [old, missing, s.cssText, s.getPropertyPriority('opacity'),
            s.getPropertyValue('border'), s.length];
    })()""") == [
        "2px", "", "border: 1px; opacity: .5 !important;", "important",
        "1px", 2,
    ]
    # Chromium leaves the attribute behind and empties it; only an element that never
    # carried a style attribute has none.
    assert page.evaluate(
        "target.style.cssText = ''; [target.style.length, target.hasAttribute('style'),"
        " target.getAttribute('style')]"
    ) == [0, True, ""]


async def test_style_named_properties_only_exist_when_supported(page):
    """``in`` and reads follow the properties Chrome supports."""
    assert page.evaluate("""(() => {
        const s = target.style;
        return [
            'MozAppearance' in s, '-moz-appearance' in s, 'msTransform' in s, 'OTransition' in s,
            'notAProperty' in s, 'zzz' in s,
            'color' in s, 'backgroundColor' in s, 'background-color' in s,
            'webkitTransform' in s, 'WebkitAppearance' in s,
            typeof s.MozAppearance, typeof s.display, s.display, s.color,
            Object.getOwnPropertyDescriptor(s, 'MozAppearance') === undefined,
        ];
    })()""") == [
        False, False, False, False,
        False, False,
        True, True, True,
        True, True,
        "undefined", "string", "", "blue",
        True,
    ]


async def test_style_declared_unsupported_name_becomes_visible(page):
    """Assigning a name no property provides still makes it exist."""
    assert page.evaluate("""(() => {
        const s = target.style;
        const before = ['notAProperty' in s, s.notAProperty === undefined, '--theme-color' in s];
        s.notAProperty = '1';
        s['--theme-color'] = 'red';
        return [before, 'notAProperty' in s, s.notAProperty, s['--theme-color']];
    })()""") == [[False, True, True], True, "1", "red"]


async def test_style_attribute_changes_refresh_existing_object(page):
    assert page.evaluate("""(() => {
        const s = target.style;
        target.setAttribute('style', 'padding: 1px; color: green');
        const afterSet = [s.cssText, s.padding, s.color];
        target.removeAttribute('style');
        return [afterSet, s.cssText, s.length, s.color];
    })()""") == [["padding: 1px; color: green;", "1px", "green"], "", 0, ""]


async def test_css_text_padding_shorthand_matches_chrome_cssom(page):
    assert page.evaluate("""(() => {
        const box = document.createElement('div');
        box.style.cssText = `
            container-type: inline-size;
            padding: 10px;
            text-align: center;
            font-weight: 600;
            width: invalid;
        `;
        document.body.appendChild(box);
        const style = box.style;
        return [style.cssText, style.length,
            Array.from({length: style.length}, (_, i) => style.item(i)),
            style.getPropertyValue('padding-top'),
            getComputedStyle(box).paddingTop,
            style.getPropertyValue('width')];
    })()""") == [
        "container-type: inline-size; padding: 10px; text-align: center; font-weight: 600;",
        7,
        ["container-type", "padding-top", "padding-right", "padding-bottom",
         "padding-left", "text-align", "font-weight"],
        "10px", "10px", "",
    ]


async def test_chrome_rejects_unrecognized_single_font_keyword(page):
    assert page.evaluate("""(() => {
      const box = document.createElement('div');
      box.style.cssText = 'font: -apple-system-body !important; color: red';
      return [box.style.cssText, box.style.font, box.style.color];
    })()""") == ['color: red;', '', 'red']


async def test_a_rule_answers_with_its_style_and_lets_the_page_restyle_it(page):
    """A rule's declaration block is the page's to edit, and the rule's own text follows
    what it holds."""
    (await page.set_content('<style id="sheet">p { color: red } .a { margin: 0 }</style>',
                     url="http://style.test/", wait_until="commit"))
    assert page.evaluate("""(() => {
        const sheet = document.getElementById('sheet').sheet;
        const rules = sheet.cssRules;
        const style = rules[0].style;
        const before = [rules.length, rules[0].selectorText, rules[0].cssText,
                        style.parentRule === rules[0], style.color,
                        rules[0].parentStyleSheet === sheet];
        style.setProperty('color', 'blue');
        return [before, rules[0].cssText, style.getPropertyValue('color')];
    })()""") == [[2, "p", "p { color: red; }", True, "red", True],
                 "p { color: blue; }", "blue"]


async def test_rewriting_style_text_replaces_the_sheet_and_preserves_old_rules(page):
    """Chrome detaches the original sheet when the owner's text is replaced."""
    (await page.set_content('<style id="sheet">p { color: red }</style>', url="http://style.test/", wait_until="commit"))
    assert page.evaluate("""(() => {
        const element = document.getElementById('sheet');
        const sheet = element.sheet;
        const before = [sheet.cssRules.length, sheet.cssRules[0].selectorText];
        element.textContent = 'div { color: green } span { color: blue }';
        return [before, sheet.cssRules.length, sheet.cssRules[0].selectorText,
                sheet.ownerNode === null, sheet.title === null,
                element.sheet !== sheet, element.sheet.cssRules.length,
                element.sheet.cssRules[1].selectorText];
    })()""") == [[1, "p"], 1, "p", True, True, True, 2, "span"]


async def test_a_grouping_rule_holds_rules_a_page_can_add_to(page):
    """A @media rule is a sheet inside a sheet: its own cssRules, and its own edits."""
    (await page.set_content('<style id="sheet">@media screen { p { color: red } }</style>',
                     url="http://style.test/", wait_until="commit"))
    assert page.evaluate("""(() => {
        const sheet = document.getElementById('sheet').sheet;
        const media = sheet.cssRules[0];
        const out = [media.type, media.media.mediaText, media.cssRules.length,
                     media.cssRules[0].selectorText,
                     media.cssRules[0].parentRule === media,
                     media.cssRules[0].parentStyleSheet === sheet];
        out.push(media.insertRule('div { color: green }', 0));
        out.push([media.cssRules.length, media.cssRules[0].selectorText]);
        media.deleteRule(0);
        out.push([media.cssRules.length, media.cssRules[0].selectorText]);
        return out;
    })()""") == [4, "screen", 1, "p", True, True, 0, [2, "div"], [1, "p"]]


async def test_box_shorthand_conflicts_and_longhand_removal(page):
    assert page.evaluate('''(() => {
        const e = document.createElement('div'); document.body.appendChild(e);
        const s = e.style;
        s.cssText = 'margin-left: 1px; margin: 2px';
        const before = [s.marginLeft, s.length, Array.from(s), getComputedStyle(e).marginLeft];
        const removed = s.removeProperty('margin-left');
        return [before, removed, s.marginLeft, s.margin, s.cssText];
    })()''') == [
        ['2px', 4, ['margin-top', 'margin-right', 'margin-bottom', 'margin-left'], '2px'],
        '2px', '', '', 'margin-top: 2px; margin-right: 2px; margin-bottom: 2px;',
    ]


async def test_inline_computed_dimensions_and_inherited_monospace_size(page):
    assert page.evaluate('''(() => {
        const outer = document.createElement('div');
        outer.style.fontSize = '30px';
        outer.innerHTML = '<span style="font-family:monospace">hello</span>';
        document.body.appendChild(outer);
        const s = getComputedStyle(outer.firstChild);
        return [s.fontSize, s.width, s.height];
    })()''') == ['30px', 'auto', 'auto']


async def test_box_shorthands_follow_stylesheet_cascade_and_important(page):
    assert page.evaluate('''(() => {
        const sheet = document.createElement('style');
        sheet.textContent = '.box { margin-left: 1px } .box { margin: 2px }';
        document.head.appendChild(sheet);
        const e = document.createElement('div'); e.className = 'box';
        document.body.appendChild(e);
        const cascade = getComputedStyle(e).marginLeft;
        e.style.cssText = 'margin-left: 1px !important; margin: 2px; margin-right: 3px';
        return [cascade, e.style.marginLeft, e.style.marginRight,
                e.style.getPropertyPriority('margin-left'), getComputedStyle(e).marginLeft];
    })()''') == ['2px', '1px', '3px', 'important', '1px']
