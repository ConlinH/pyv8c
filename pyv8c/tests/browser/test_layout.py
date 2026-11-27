"""Layout and computed-style behavior needed by browser scripts."""

import os

import pytest

from pyv8c.async_api import Browser
from pyv8c.foundation import system_fonts
from pyv8c.foundation import text as text_engine
from pyv8c.web.dom import font_measurement, layout


@pytest.fixture
async def page():
    browser = (await Browser(cache=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://layout.test/", wait_until="commit"))
    try:
        yield page
    finally:
        (await browser.close())


async def test_a_family_is_measured_with_the_face_that_family_names():
    """One table answers both "is this font here" and "how wide is this text", so a face
    ``local()`` finds is the face the page then measures with."""
    record = next((item for item in system_fonts.records() if item.path), None)
    if record is None:
        pytest.skip("this machine has no fonts to measure with")
    face = font_measurement._face_for([record.family])
    assert face is not None
    assert system_fonts.normalize(face.family) == system_fonts.normalize(record.family)


async def test_a_collection_face_is_measured_at_its_own_index():
    """A face inside a ``.ttc`` is a face of its own: measuring it needs the index."""
    record = next((item for item in system_fonts.records() if item.index), None)
    if record is None:
        pytest.skip("this machine has no font collection")
    path = system_fonts.file_for(record)
    assert path is not None
    assert text_engine.shape("A", path, index=record.index, size=16.0).width > 0


async def test_adjacent_inline_elements_keep_cross_element_kerning(page):
    if not any(item.family == "Noto Sans SC" and system_fonts.file_for(item)
               for item in system_fonts.records()):
        pytest.skip("Noto Sans SC is not installed")
    assert page.evaluate("""(() => {
        const host = document.createElement('div');
        host.style.cssText = 'white-space:nowrap;font-family:"Noto Sans SC"';
        host.innerHTML = '<span>υ</span><span>τ</span>';
        document.body.appendChild(host);
        return [...host.children].map(child => child.getBoundingClientRect().width);
    })()""") == [8.765625, 8.234375]


async def test_windows_cambria_math_uses_hinted_advance():
    if os.name != "nt":
        pytest.skip("Windows font metrics")
    record = next((item for item in system_fonts.records()
                   if item.family == "Cambria Math" and system_fonts.file_for(item)), None)
    if record is None:
        pytest.skip("Cambria Math is not installed")
    assert text_engine.shape("𝔈", system_fonts.file_for(record),
                             index=record.index, size=16).width == 11


async def test_an_uninstalled_family_falls_back_instead_of_failing():
    """CSS walks the family list and then uses the platform's default, so a name this
    machine does not have still measures -- which is what an uninstalled font looks like."""
    if not system_fonts.records():
        pytest.skip("this machine has no fonts to measure with")
    assert font_measurement._face_for(["Definitely Not A Font 123"]) is not None
    assert font_measurement.text_width("abc", {"font-family": "Definitely Not A Font 123"}) > 0


async def test_center_is_a_block_containing_a_fixed_width_child(page):
    (await page.set_content('''<!doctype html><body style="margin:0;width:640px">
      <center id="container"><div id="child" style="width:300px;height:34px"></div></center>
    </body>''', wait_until="commit"))
    assert page.evaluate('''(() => {
      const host=document.getElementById('container'),child=document.getElementById('child');
      return [getComputedStyle(host).display,host.offsetWidth,child.offsetWidth];
    })()''') == ['block', 640, 300]


async def test_nested_layout_reuses_cascade_within_one_pass_and_recomputes_after_change(page, monkeypatch):
    from pyv8c.web.css import cascade
    original = cascade._selector_matches
    calls = 0

    def counted(element, selector):
        nonlocal calls
        calls += 1
        return original(element, selector)

    monkeypatch.setattr(cascade, "_selector_matches", counted)
    (await page.set_content("<style>div { color: red } .box { padding: 1px }</style>"
                     + "<div class='box'>" * 35 + "x" + "</div>" * 35,
                     url="http://layout.test/", wait_until="commit"))
    calls = 0
    assert page.evaluate("document.querySelector('.box').offsetWidth > 0")
    assert calls < 300  # linear passes rather than every ancestor per element
    assert page.evaluate("""(() => {
        const node = document.querySelector('.box');
        node.style.width = '123px';
        return node.offsetWidth >= 123;
    })()""")


async def test_geometry_reuses_boxes_within_one_property_read(page):
    await page.set_content("<div id='box' style='width:40px;height:20px'></div>",
                           wait_until="commit")
    assert page.evaluate("document.getElementById('box').scrollWidth") >= 40
    snapshot = page.document._layout_snapshot
    assert snapshot is not None
    page.evaluate("document.getElementById('box').offsetLeft")
    assert page.document._layout_snapshot is snapshot


async def test_hit_testing_reuses_layout_styles_and_sees_mutations(page):
    (await page.set_content("<style>#box { width: 40px; height: 20px }</style>"
                     "<div id='box'></div>", url="http://layout.test/", wait_until="commit"))
    assert page.evaluate("document.elementFromPoint(10, 10).id") == "box"
    snapshot = page.document._layout_snapshot
    assert snapshot is not None
    assert page.evaluate("document.elementFromPoint(10, 10).id") == "box"
    assert page.document._layout_snapshot is snapshot
    assert page.evaluate("""(() => {
        document.getElementById('box').style.display = 'none';
        return document.elementFromPoint(10, 10).id;
    })()""") != "box"


async def test_get_computed_style_invalid_element_throws_in_javascript(page):
    assert page.evaluate("""(() => [null, undefined, {}, document.createTextNode('x')].map(value => {
        try { getComputedStyle(value); return null; }
        catch (error) { return [error instanceof TypeError, error.name]; }
    }))()""") == [[True, "TypeError"]] * 4


async def test_layout_snapshot_reuses_pass_and_invalidates_on_dom_changes(page):
    (await page.set_content("<div id='box' style='width:40px;height:20px'>x</div>",
                     url="http://layout.test/", wait_until="commit"))
    assert page.evaluate("""(() => {
        let b = document.getElementById('box');
        return [b.offsetWidth, b.clientWidth, b.scrollWidth,
                b.getBoundingClientRect().width, document.elementFromPoint(10, 10).id];
    })()""")[:4] == [40, 40, 40, 40]
    state = page.document._native_layout_document
    first_version = state.version
    assert page.evaluate("document.getElementById('box').offsetWidth") == 40
    assert state.version == first_version
    assert page.evaluate("""(() => {
        let b = document.getElementById('box');
        b.style.width = '60px';
        return b.offsetWidth;
    })()""") == 60
    assert state.version > first_version
    first_version = state.version
    assert page.evaluate("""(() => {
        let b = document.getElementById('box');
        b.firstChild.data = 'a longer text';
        return b.offsetWidth;
    })()""") == 60
    assert state.version > first_version
    first_version = state.version
    assert page.evaluate("""(() => {
        let b = document.getElementById('box');
        b.remove();
        return b.offsetWidth;
    })()""") == 0
    assert state.version > first_version


async def test_layout_snapshot_observes_class_and_viewport(page, monkeypatch):
    (await page.set_content("<style>.wide { width: 90px }</style><div id='box'></div>",
                     url="http://layout.test/", wait_until="commit"))
    node = page.document.fn_getElementById('box')
    before = layout.layout(node).width
    node.attributes['class'] = 'wide'
    assert layout.layout(node).width == 90
    assert before != 90
    original = layout._viewport_width
    monkeypatch.setattr(layout, '_viewport_width',
                        lambda element: (400.0, original(element)[1]))
    assert layout.layout(page.document.get_body()).width == 384


async def test_inline_size_change_preserves_unaffected_geometry_and_prototypes(page):
    await page.set_content("""
        <main><section id="changed" style="width:80px;height:20px">
          <div id="percent" style="width:50%"></div></section>
        <section id="stable"><div id="nested" style="width:40px;height:10px"></div></section></main>
        """, wait_until="commit")
    assert page.evaluate("document.getElementById('percent').offsetWidth") == 40
    old = page.document._layout_snapshot
    nested = page.document.fn_getElementById("nested")
    assert page.evaluate("""(() => {
        const box = document.getElementById('changed');
        const prototype = Object.getPrototypeOf(box);
        const style = box.style, stylePrototype = Object.getPrototypeOf(style);
        const before = Object.getOwnPropertyDescriptors(prototype);
        const key = Symbol('layout test');
        Object.defineProperty(prototype, key, {value: style, configurable: true});
        box.style.width = '120px';
        const rect = box.getBoundingClientRect();
        const after = Object.getOwnPropertyDescriptors(prototype);
        const intact = Object.keys(before).length === Object.keys(after).length
            && Object.keys(before).every(name =>
                Reflect.ownKeys(before[name]).every(prop => before[name][prop] === after[name][prop]));
        const identity = box === document.querySelector('#changed')
            && Object.getPrototypeOf(box) === prototype && box.style === style
            && Object.getPrototypeOf(style) === stylePrototype && box[key] === style
            && Object.getPrototypeOf(rect) === DOMRect.prototype;
        delete prototype[key];
        return [rect.width, document.getElementById('percent').offsetWidth, intact, identity];
    })()""") == [120, 60, True, True]
    assert page.document._layout_snapshot.boxes[nested].top == old.boxes[nested].top

    assert page.evaluate("""(() => {
        const node = document.getElementById('nested'), before = node.getBoundingClientRect().top;
        document.getElementById('changed').style.height = '35px';
        return node.getBoundingClientRect().top - before;
    })()""") == 15
    assert page.document._layout_snapshot.boxes[nested].top == old.boxes[nested].top + 15


async def test_style_attribute_selector_can_invalidate_a_sibling_during_size_edit(page):
    await page.set_content("""
        <style>#peer {width:30px} #changed:is([style]) + #peer {width:90px}</style>
        <div id="changed"></div><div id="peer"></div>
        """, wait_until="commit")
    assert page.evaluate("document.getElementById('peer').offsetWidth") == 30
    assert page.evaluate("""(() => {
        document.getElementById('changed').style.width = '40px';
        return document.getElementById('peer').offsetWidth;
    })()""") == 90


async def test_inline_size_change_reflows_shadow_content(page):
    await page.set_content('<div id="host" style="width:100px"></div>', wait_until="commit")
    assert page.evaluate("""(() => {
        const host = document.getElementById('host'), root = host.attachShadow({mode:'open'});
        root.innerHTML = '<style>#inner{width:50%;height:10px}</style><div id="inner"></div>';
        const child = root.getElementById('inner'), proto = Object.getPrototypeOf(child);
        const before = child.getBoundingClientRect().width;
        host.style.width = '160px';
        return [before, child.getBoundingClientRect().width,
                child === root.getElementById('inner') && Object.getPrototypeOf(child) === proto];
    })()""") == [50, 80, True]


async def test_incremental_layout_matches_cold_pass_with_dependent_placements(page):
    await page.set_content("""
        <div id="parent" style="width:200px;position:relative">
          <div id="flex" style="display:flex"><div id="changed" style="width:20px;height:25px"></div>
            <div id="flexpeer" style="width:30px"></div></div>
          <div id="grid" style="display:grid;grid-template-columns:50% 50%">
            <div id="cell" style="height:15px"></div><div></div></div>
          <div id="positioned" style="position:absolute;right:0;bottom:0;width:10px;height:10px"></div>
        </div>
        <div id="sibling" style="height:20px"></div>
        """, wait_until="commit")
    read = """['parent','flexpeer','cell','positioned','sibling'].map(id => {
      const {x,y,width,height}=document.getElementById(id).getBoundingClientRect();
      return [x,y,width,height];
    })"""
    before = page.evaluate(read)
    page.evaluate("""document.getElementById('parent').style.width = '300px';
                     document.getElementById('changed').style.height = '40px';""")
    incremental = page.evaluate(read)
    assert incremental != before

    def reset_layout():
        page.document.__dict__.pop("_native_layout_document")
        page.document._layout_snapshot = None

    page.realm.executor.call(reset_layout)
    assert page.evaluate(read) == incremental


async def test_layout_snapshot_observes_stylesheet_fonts_and_navigation(page):
    (await page.set_content("<style>#box { width: 40px }</style><div id='box'></div>",
                     url="http://layout.test/", wait_until="commit"))
    node = page.document.fn_getElementById('box')
    assert layout.layout(node).width == 40
    old_snapshot = page.document._layout_snapshot
    assert page.evaluate("""(() => {
        document.querySelector('style').textContent = '#box { width: 70px }';
        return document.getElementById('box').offsetWidth;
    })()""") == 70
    assert page.document._layout_snapshot is not old_snapshot

    fonts = page.document.get_fonts()
    face = page.evaluate("new FontFace('LayoutTest', 'local(Arial)')")
    before = page.document.layout_version
    fonts.fn_add(face)
    assert page.document.layout_version > before
    assert page.document._layout_snapshot is None

    previous = page.document
    (await page.set_content("<div id='box' style='width:25px'></div>",
                     url="http://layout.test/next", wait_until="commit"))
    assert page.document is not previous
    assert page.evaluate("document.getElementById('box').offsetWidth") == 25


async def test_layout_snapshot_starts_fresh_at_event_boundary(page):
    (await page.set_content("<button id='box' style='width:40px;height:20px'>ok</button>",
                     url="http://layout.test/", wait_until="commit"))
    assert page.evaluate("document.getElementById('box').offsetWidth") == 40
    previous = page.document._layout_snapshot
    assert previous is not None
    page.dispatch_event('click', selector='#box')
    assert page.document._layout_snapshot is None
    assert page.evaluate("document.getElementById('box').offsetWidth") == 40
    assert page.document._layout_snapshot is not previous


async def test_link_sheet_attribute_change_reparses_rules(page):
    (await page.set_content("<link id='sheet' rel='stylesheet'><div id='box'></div>",
                     url="http://layout.test/", wait_until="commit"))
    sheet = page.document.fn_getElementById('sheet')
    box = page.document.fn_getElementById('box')
    sheet._stylesheet_text = '#box { width: 70px }'
    page.document.style_version += 1
    assert layout.layout(box).width == 70
    sheet.attributes['rel'] = 'alternate'
    assert layout.layout(box).width != 70


async def test_hidden_element_keeps_layout_and_font_size_changes_height(page):
    # Chrome uses the profile's default font for the unknown family; its line is 165px.
    assert page.evaluate("""(() => {
        const div = document.createElement('div');
        div.style.visibility = 'hidden';
        document.body.appendChild(div);
        div.innerHTML = '<span style="font-family:mmllii;font-size:114px">mmmmmmmmmmmlliii</span>';
        div.style.fontFamily = 'DFPhelvetica';
        return [div.offsetWidth === document.body.clientWidth, div.offsetHeight,
            getComputedStyle(div).fontFamily,
            getComputedStyle(div.firstElementChild).fontSize,
            div.getBoundingClientRect().height > 0];
    })()""") == [True, 165, "DFPhelvetica", "114px", True]


async def test_the_cascade_orders_by_importance_and_specificity(page):
    """Important beats plain, and specificity beats the order the rules were written in."""
    assert page.evaluate("""(() => {
        document.body.innerHTML =
            '<style>#a { height: 9px } div { height: 1px }'
            + ' .b { height: 3px !important } .b { height: 7px }</style>'
            + '<div id="a"></div><div class="b" style="height:5px"></div>';
        return [document.getElementById('a').offsetHeight,
                document.querySelector('.b').offsetHeight];
    })()""") == [9, 3]


async def test_a_fixed_box_measures_from_the_viewport(page):
    """A fixed box measures against the viewport, and has no offset parent."""
    assert page.evaluate("""(() => {
        document.body.innerHTML = '<div style="position:relative;padding:40px">'
            + '<div id="t" style="position:fixed;left:0;top:0;width:10px;height:10px"></div>'
            + '</div>';
        const box = document.getElementById('t');
        const rect = box.getBoundingClientRect();
        return [rect.left, rect.top, String(box.offsetParent)];
    })()""") == [0, 0, "null"]


async def test_auto_margins_centre_the_box(page):
    """``margin: auto`` shares the free space, so a declared width sits in the middle."""
    assert page.evaluate("""(() => {
        document.body.innerHTML = '<div id="t" style="width:100px;height:10px;margin:0 auto"></div>';
        const box = document.getElementById('t');
        const left = box.getBoundingClientRect().left - document.body.getBoundingClientRect().left;
        return [left, (document.body.clientWidth - 100) / 2, left === (document.body.clientWidth - 100) / 2];
    })()""")[2] is True


async def test_style_rules_and_basic_flex_grid_geometry(page):
    assert page.evaluate("""(() => {
        document.body.innerHTML = '<style>.item { height: 10px } span { color: red }</style>'
            + '<div id="flex" style="display:flex"><div class="item"></div><div class="item"></div></div>'
            + '<span>text</span>';
        const flex = document.querySelector('#flex');
        const span = document.querySelector('span');
        return [flex.offsetHeight, getComputedStyle(span).color,
            span.offsetHeight, flex.clientWidth === flex.offsetWidth];
    })()""") == [10, "rgb(255, 0, 0)", 24, True]


async def test_nowrap_inline_width_includes_inter_element_space(page):
    assert page.evaluate("""(() => {
      document.body.innerHTML = '<span style="white-space:nowrap">'
        + '<b>first</b> <b>second</b></span>';
      const span = document.querySelector('span');
      const children = span.querySelectorAll('b');
      return span.getBoundingClientRect().width >
        children[0].getBoundingClientRect().width +
        children[1].getBoundingClientRect().width;
    })()""") is True


async def test_css_rotation_changes_client_rect_but_not_offset_size(page):
    assert page.evaluate("""(() => {
      const box = document.createElement('div');
      box.style.cssText = 'width:16px;height:15px;transform:rotate(13deg)';
      document.body.appendChild(box);
      const rect = box.getBoundingClientRect();
      return [box.offsetWidth, box.offsetHeight, rect.width, rect.height];
    })()""") == [16, 15, 18.964187622070312, 18.214767456054688]


async def test_absolute_css_units_have_chrome_used_pixel_sizes(page):
    assert page.evaluate("""(() => {
      const parent = document.createElement('div');
      parent.style.cssText = 'position:fixed;left:-99999px;top:0;width:0;height:0;'
          + 'visibility:hidden;pointer-events:none;contain:strict';
      document.body.appendChild(parent);
      return ['1.03in', '2.6162cm', '74.16pt', '1.0053in'].map(value => {
        const box = document.createElement('div');
        box.style.cssText = `position:absolute;width:${value};height:${value}`;
        parent.appendChild(box);
        return [box.style.width, getComputedStyle(box).width,
                box.getBoundingClientRect().width, box.offsetWidth];
      });
    })()""") == [
        ['1.03in', '98.875px', 98.875, 99],
        ['2.6162cm', '98.875px', 98.875, 99],
        ['74.16pt', '98.875px', 98.875, 99],
        ['1.0053in', '96.5px', 96.5, 97],
    ]


async def test_css_math_functions_produce_used_widths(page):
    assert page.evaluate("""(() => {
      return ['calc(1px * pi)', 'calc(1px * e)',
              'calc(1px * sqrt(16))',
              'calc(1px * (1 + sin(0)))', 'calc(1px * tan(0.5))'].map(value => {
        const box = document.createElement('div');
        box.style.width = value;
        document.body.appendChild(box);
        return [box.style.width, getComputedStyle(box).width,
                box.getBoundingClientRect().width];
      });
    })()""") == [
        ['calc(3.14159px)', '3.14062px', 3.140625],
        ['calc(2.71828px)', '2.70312px', 2.703125],
        ['calc(4px)', '4px', 4],
        ['calc(1px)', '1px', 1],
        ['calc(0.546302px)', '0.53125px', 0.53125],
    ]


async def test_css_math_keeps_used_precision_through_cssom_writes(page):
    assert page.evaluate("""(() => {
      const box = document.createElement('div');
      box.style.width = 'calc(1px * (12345.678901234))';
      document.body.appendChild(box);
      const read = () => [box.style.width, getComputedStyle(box).width,
                          box.getBoundingClientRect().width];
      const result = [read()];
      box.style.color = 'red';
      result.push(read());
      box.setAttribute('style', 'width:calc(2px)');
      result.push(read());
      return result;
    })()""") == [
        ['calc(12345.7px)', '12345.7px', 12345.671875],
        ['calc(12345.7px)', '12345.7px', 12345.671875],
        ['calc(2px)', '2px', 2],
    ]


async def test_nested_css_math_keeps_blink_trigonometry_and_layout_units(page):
    assert page.evaluate("""(() => {
      const expressions = [
        'calc( 1px * ( ( 2.71828 * 0.987654321 - 0.123456789 / ( 987654.321 * 12345.678 ) ) + 0.5555555 / sin( sin( 10000.1 * tan( 50000 ) / tan( 20000 ) + 1.0 / pi * 5.0 - 0.1111 ) / 100.0 + tan( 30000 + 40000 * 50000 + 0.0001 ) / 9999.9 * pi ) - 0.999999 * -100000.5 ) )',
        'calc( 1px * ( ( 3.14159 * 0.543210987 - 0.987654321 / ( 123456.789 * 98765.432 ) ) + 0.8888888 / cos( cos( 12345.6 * tan( 65432.1 ) / tan( 10987.6 ) + 2.0 / pi * 7.0 - 0.2222 ) / 200.0 + sqrt( 50000 + 60000 * 70000 + 0.0002 ) / 8888.8 * pi ) - 0.777777 * -200000.7 ) )',
      ];
      return expressions.map(expression => {
        const box = document.createElement('div');
        box.style.cssText = 'container-type:inline-size;padding:10px;'
            + 'text-align:center;font-weight:bold;box-sizing:content-box';
        box.style.width = expression;
        box.textContent = '$#QKmn__gabenforgiveme';
        document.body.appendChild(box);
        return [box.style.width, getComputedStyle(box).width,
                box.getBoundingClientRect().width];
      });
    })()""") == [
        ['calc(100075px)', '100075px', 100094.984375],
        ['calc(155556px)', '155556px', 155576.203125],
    ]


async def test_default_and_variable_font_weights_match_chrome_geometry(page):
    assert page.evaluate("""(() => {
      const result = [];
      for (const weight of ['400', '700']) {
        const box = document.createElement('div');
        box.style.cssText = 'position:absolute;font-family:"Noto Sans SC";font-weight:'
            + weight;
        box.textContent = 'a';
        document.body.appendChild(box);
        result.push([getComputedStyle(box).fontWeight,
                     getComputedStyle(box).width,
                     box.getBoundingClientRect().width]);
      }
      return result;
    })()""") == [
        ['400', '9.01562px', 9.015625],
        ['700', '9.46875px', 9.46875],
    ]


async def test_root_client_box_uses_viewport_even_with_empty_body(page):
    assert page.evaluate("""[document.documentElement.clientWidth === innerWidth,
      document.documentElement.clientHeight === innerHeight,
      document.body.clientHeight]""") == [True, True, 0]


async def test_negative_absolute_positions_keep_computed_precision_and_quantize_geometry(page):
    assert page.evaluate("""(() => {
      const box = document.createElement('div');
      box.style.cssText = 'position:fixed;left:-1.0053in;top:-1.03in;'
          + 'width:1in;height:1in';
      document.body.appendChild(box);
      const style = getComputedStyle(box);
      const rect = box.getBoundingClientRect();
      return [style.left, style.top, rect.left, rect.top];
    })()""") == ['-96.5088px', '-98.88px', -96.5, -98.875]


async def test_rect_applies_transform_origin_and_ancestor_transform(page):
    assert page.evaluate('''(() => {
        const outer = document.createElement('div');
        outer.style.cssText = 'position:absolute;left:0;top:0;transform:translateX(100px)';
        const inner = document.createElement('div');
        inner.style.cssText = 'width:100px;height:50px;transform:rotate(90deg);transform-origin:0 0';
        outer.appendChild(inner); document.body.appendChild(outer);
        const r = inner.getBoundingClientRect();
        return [r.left, r.top, r.width, r.height];
    })()''') == [50, 0, 50, 100]


async def test_button_intrinsic_size_tracks_value_and_variation_presentation(page):
    from pyv8c.foundation import system_fonts
    available = {record.family.lower() for record in system_fonts.records()}
    if not {'arial', 'segoe ui emoji', 'noto sans sc'} <= available:
        pytest.skip('the Chrome comparison uses Arial, Segoe UI Emoji and Noto Sans SC')
    (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
    assert page.evaluate('''(() => {
        const input = document.createElement('input');
        input.type = 'button'; input.style.all = 'revert';
        input.style.fontSize = '100px'; input.style.margin = '-9999px';
        document.body.appendChild(input);
        return ['‼︎', '‼️', '〰︎', '〰️', 'abc', ''].map(value => {
            input.value = value;
            const rect = input.getBoundingClientRect();
            return [input.clientWidth, input.clientHeight, rect.width, rect.height];
        });
    })()''') == [
        [62, 117, 66, 121], [93, 135, 97.15625, 139],
        [112, 147, 116, 151], [146, 135, 149.796875, 139],
        [173, 117, 177.234375, 121], [12, 117, 16, 121],
    ]


async def test_button_text_presentation_uses_monochrome_fallback(page):
    import os
    from pyv8c.foundation import system_fonts
    available = {record.family.lower() for record in system_fonts.records()}
    if os.name != 'nt' or not {'arial', 'segoe ui emoji', 'segoe ui symbol', 'noto sans sc'} <= available:
        pytest.skip('the Chrome comparison uses Windows Arial, Noto Sans SC and Segoe UI')
    (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
    assert page.evaluate(r'''(() => {
        const input = document.createElement('input');
        input.type = 'button';
        input.style.cssText = 'all: revert; font-size: 100px; margin: -9999px';
        document.body.appendChild(input);
        return ['\u2b06\ufe0e', '\u2b06\ufe0f', '\u26aa\ufe0e', '\u26aa\ufe0f'].map(value => {
            input.value = value;
            return [input.clientWidth, input.clientHeight];
        });
    })()''') == [[112, 147], [149, 135], [96, 135], [149, 135]]


async def test_button_explicit_border_box_size(page):
    assert page.evaluate('''(() => {
        const input = document.createElement('input');
        input.type = 'button';
        input.style.cssText = 'width:100px;height:80px';
        document.body.appendChild(input);
        const rect = input.getBoundingClientRect();
        return [rect.width, rect.height, input.clientWidth, input.clientHeight];
    })()''') == [100, 80, 96, 76]


async def test_native_input_intrinsic_sizes_and_explicit_dimensions(page):
    assert page.evaluate('''(() => {
        return ['color', 'checkbox', 'radio', 'range'].map(type => {
            const input = document.createElement('input');
            input.type = type;
            input.style.cssText = 'all: revert; margin: -9999px';
            document.body.appendChild(input);
            const rect = input.getBoundingClientRect();
            const initial = [rect.width, rect.height, input.clientWidth, input.clientHeight];
            input.style.width = '80px'; input.style.height = '40px';
            const sized = input.getBoundingClientRect();
            input.remove();
            return [...initial, sized.width, sized.height];
        });
    })()''') == [[50, 27, 48, 25, 80, 40], [13, 13, 13, 13, 80, 40],
                 [13, 13, 13, 13, 80, 40], [129, 16, 129, 16, 80, 40]]


async def test_checkbox_without_native_appearance_has_no_intrinsic_size(page):
    assert page.evaluate('''(() => {
        const input = document.createElement('input');
        input.type = 'checkbox'; input.style.appearance = 'none';
        document.body.appendChild(input);
        const rect = input.getBoundingClientRect();
        return [rect.width, rect.height];
    })()''') == [0, 0]


async def test_text_input_intrinsic_width_uses_size_and_font(page):
    from pyv8c.foundation import system_fonts
    if 'arial' not in {record.family.lower() for record in system_fonts.records()}:
        pytest.skip('the Chrome comparison uses Arial')
    assert page.evaluate('''(() => {
        const result = [];
        for (const columns of [1, 5, 10, 20, 30]) {
            for (const fontSize of [13.3333, 20]) {
                const input = document.createElement('input');
                input.type = 'text'; input.size = columns;
                input.style.fontSize = fontSize + 'px';
                document.body.appendChild(input);
                const rect = input.getBoundingClientRect();
                result.push([rect.width, rect.height]); input.remove();
            }
        }
        return result;
    })()''') == [[44, 21], [61, 29], [72, 21], [101, 29], [107, 21],
                 [151, 29], [177, 21], [251, 29], [247, 21], [351, 29]]


async def test_text_like_input_types_have_intrinsic_dimensions(page):
    from pyv8c.foundation import system_fonts
    if 'arial' not in {record.family.lower() for record in system_fonts.records()}:
        pytest.skip('the Chrome comparison uses Arial')
    assert page.evaluate('''(() => ['text', 'search', 'password', 'tel', 'email', 'url'].map(type => {
        const input = document.createElement('input'); input.type = type;
        document.body.appendChild(input); const rect = input.getBoundingClientRect();
        const result = [rect.width, rect.height, input.clientWidth, input.clientHeight];
        input.remove(); return result;
    }))()''') == [[177, 21, 169, 17]] * 6


async def test_submit_and_reset_with_explicit_labels_use_button_metrics(page):
    from pyv8c.foundation import system_fonts
    if 'arial' not in {record.family.lower() for record in system_fonts.records()}:
        pytest.skip('the Chrome comparison uses Arial')
    assert page.evaluate('''(() => ['submit', 'reset'].flatMap(type => ['', 'OK'].map(label => {
        const input = document.createElement('input'); input.type = type;
        input.value = label; document.body.appendChild(input);
        const rect = input.getBoundingClientRect();
        const result = [rect.width, rect.height, input.clientWidth, input.clientHeight];
        input.remove(); return result;
    })))()''') == [[16, 21, 12, 17], [35.265625, 21, 31, 17]] * 2


async def test_submit_and_reset_default_labels_use_browser_ui_profile(page):
    from pyv8c.foundation import system_fonts
    families = {record.family.lower() for record in system_fonts.records()}
    if not {'arial', 'noto sans sc'} <= families:
        pytest.skip('the Chrome comparison uses Arial and Noto Sans SC')
    results = page.evaluate('''(() => ['submit', 'reset'].map(type => {
        const input = document.createElement('input'); input.type = type;
        document.body.appendChild(input); const rect = input.getBoundingClientRect();
        const result = [input.value, rect.width, rect.height, input.clientWidth, input.clientHeight];
        input.remove(); return result;
    }))()''')
    for value, width, height, client_width, client_height in results:
        assert value == ''
        assert width == 42.671875
        assert [height, client_width, client_height] == [25, 39, 21]


async def test_submit_ui_label_is_separate_from_navigator_language():
    browser = (await Browser(real_network=False, stub_logging='off', environment={
        'form_controls.submit_label': 'OK', 'languages': ['zh-CN'],
    }).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><input type="submit"></body>', wait_until="commit"))
        assert page.evaluate('''(() => {
            const input = document.querySelector('input');
            return [navigator.language, input.value, input.getBoundingClientRect().width];
        })()''') == ['zh-CN', '', 35.265625]
    finally:
        (await browser.close())


async def test_file_input_intrinsic_size_uses_font_and_ui_labels(page):
    from pyv8c.foundation import system_fonts
    families = {record.family.lower() for record in system_fonts.records()}
    if not {'arial', 'noto sans sc'} <= families:
        pytest.skip('the Chrome comparison uses Arial and Noto Sans SC')
    assert page.evaluate('''(() => {
        const input = document.createElement('input'); input.type = 'file';
        document.body.appendChild(input);
        const measure = () => {
            const rect = input.getBoundingClientRect();
            return [rect.width, rect.height, input.clientWidth, input.clientHeight];
        };
        const normal = measure();
        input.style.fontSize = '20px'; const larger = measure();
        input.style.width = '80px'; input.style.height = '40px';
        return [normal, larger, measure()];
    })()''') == [[253, 25, 253, 25], [379, 35, 379, 35], [80, 40, 80, 40]]


async def test_time_input_fields_and_picker_dimensions(page):
    from pyv8c.foundation import system_fonts
    if 'nsimsun' not in {record.family.lower() for record in system_fonts.records()}:
        pytest.skip('the Chrome comparison uses NSimSun')
    assert page.evaluate('''(() => {
        const input = document.createElement('input'); input.type = 'time';
        document.body.appendChild(input);
        const measure = () => {
            const r = input.getBoundingClientRect();
            return [r.width, r.height, input.clientWidth, input.clientHeight];
        };
        const normal = measure(); input.step = '1'; const seconds = measure();
        input.step = '0.001'; const milliseconds = measure();
        input.step = '60'; input.style.fontSize = '20px'; const larger = measure();
        input.style.width = '80px'; input.style.height = '40px';
        return [normal, seconds, milliseconds, larger, measure()];
    })()''') == [[72, 24, 68, 20], [95, 24, 91, 20], [125, 24, 121, 20],
                 [94, 31, 90, 27], [80, 40, 76, 36]]


async def test_time_input_seconds_follow_value_and_minimum(page):
    from pyv8c.foundation import system_fonts
    if 'nsimsun' not in {record.family.lower() for record in system_fonts.records()}:
        pytest.skip('the Chrome comparison uses NSimSun')
    assert page.evaluate('''(() => {
        const input = document.createElement('input'); input.type = 'time';
        document.body.appendChild(input); const widths = [];
        for (const value of ['12:34:00', '12:34:01', '12:34:00.001']) {
            input.value = value; widths.push(input.getBoundingClientRect().width);
        }
        input.value = ''; input.min = '00:00:03'; widths.push(input.getBoundingClientRect().width);
        input.min = '00:00:00.123'; widths.push(input.getBoundingClientRect().width);
        return widths;
    })()''') == [72, 95, 125, 95, 125]


async def test_computed_word_spacing_resolves_normal_and_inheritance(page):
    (await page.set_content('<div id="a"><span id="b">hello world</span></div>', wait_until="commit"))
    values = page.evaluate('''(() => {
        const a = document.querySelector('#a'), b = document.querySelector('#b');
        return ['', 'normal', 'initial', '1em'].map(value => {
            a.style.wordSpacing = value;
            return [getComputedStyle(a).wordSpacing, getComputedStyle(b).wordSpacing];
        });
    })()''')
    assert [list(item) for item in values] == [
        ['0px', '0px'], ['0px', '0px'], ['0px', '0px'], ['16px', '16px'],
    ]


async def test_empty_atomic_inline_boxes_still_create_a_line_box(page):
    (await page.set_content('''<!doctype html><body style="line-height:20px">
        <div id="empty"><span></span></div>
        <div id="block"><span style="display:inline-block;width:0;height:0"></span></div>
        <div id="frame"><iframe style="width:0;height:0;border:0"></iframe></div>
        <div id="image"><img style="width:0;height:0"></div>
        </body>''', wait_until="commit"))
    assert page.evaluate("['empty','block','frame','image'].map(id => document.getElementById(id).clientHeight)") == [0, 20, 20, 20]


async def test_text_input_client_width_excludes_editable_padding(page):
    (await page.set_content('''<input id="search" type="search" style="width:200px;padding:10px 20px;border:3px solid">
                        <input id="button" type="button" style="width:200px;padding:10px 20px;border:3px solid">''', wait_until="commit"))
    assert page.evaluate("['search','button'].map(id => {let x=document.getElementById(id);return [x.getBoundingClientRect().width,x.clientWidth]})") == [[200, 154], [200, 194]]


async def test_time_input_width_uses_font_metrics_and_bitmap_strikes(page):
    from pyv8c.foundation import system_fonts
    installed = {record.family.lower() for record in system_fonts.records()}
    if not {'nsimsun', 'consolas', 'courier new'} <= installed:
        pytest.skip('the Chrome reference uses Windows fonts')
    assert page.evaluate('''(() => ['NSimSun','Consolas','Courier New'].map(font => {
        const input = document.createElement('input'); input.type = 'time';
        input.style.fontFamily = font; document.body.appendChild(input);
        const width = () => input.getBoundingClientRect().width;
        const first = width(); input.step='1'; const seconds=width();
        input.step='0.001'; const millis=width(); input.step='60'; input.style.fontSize='20px';
        const large=width(); input.remove(); return [font,first,seconds,millis,large];
    }))()''') == [['NSimSun',72,95,125,94], ['Consolas',74,98,129,99],
                  ['Courier New',77,103,137,105]]


async def test_font_fallback_cache_respects_each_environment():
    from pyv8c.foundation.environment import BrowserEnvironment
    from pyv8c.foundation import system_fonts
    from pyv8c.web.dom.font_measurement import _face_chain
    families = {record.family.lower() for record in system_fonts.records()}
    if not {'arial', 'noto sans sc', 'microsoft yahei'} <= families:
        pytest.skip('requires the Windows fonts used by this comparison')
    first = BrowserEnvironment({'fonts.default_family': 'Noto Sans SC'})
    second = BrowserEnvironment({'fonts.default_family': 'Microsoft YaHei'})
    chain_a = _face_chain(['Arial'], first, '400', 'normal')
    chain_b = _face_chain(['Arial'], second, '400', 'normal')
    names_a = [font.family.lower() for font in chain_a]
    names_b = [font.family.lower() for font in chain_b]
    assert names_a.index('noto sans sc') < names_a.index('microsoft yahei')
    assert 'noto sans sc' not in names_b or names_b.index('microsoft yahei') < names_b.index('noto sans sc')
    assert names_a != names_b


async def test_bounding_rect_reads_one_snapshot_and_returns_independent_objects(page, monkeypatch):
    from pyv8c.web.dom import layout

    await page.set_content('<div style="width:40px;height:12px"></div>', wait_until='load')
    calls = 0
    snapshot = layout.layout_snapshot

    def counted(document, **kwargs):
        nonlocal calls
        calls += 1
        return snapshot(document, **kwargs)

    monkeypatch.setattr(layout, 'layout_snapshot', counted)
    assert page.evaluate("""(() => {
        const box = document.querySelector('div');
        const first = box.getBoundingClientRect();
        first.width = 50;
        const second = box.getBoundingClientRect();
        box.style.width = '60px';
        const third = box.getBoundingClientRect();
        return [first instanceof DOMRect, Object.getPrototypeOf(first) === DOMRect.prototype,
                first !== second, first.width, second.width, third.width];
    })()""") == [True, True, True, 50, 40, 60]
    assert calls == 3


async def test_detached_rect_does_not_start_layout_and_old_rect_keeps_its_value(page):
    await page.set_content('<div id="box" style="width:40px;height:12px"></div>',
                           wait_until='load')
    page.evaluate("""
        globalThis.box = document.getElementById('box');
        globalThis.oldRect = box.getBoundingClientRect();
        box.remove();
    """)
    assert page.document._layout_snapshot is None
    assert page.evaluate('[box.getBoundingClientRect().width, oldRect.width]') == [0, 40]
    assert page.document._layout_snapshot is None
    page.evaluate('document.body.getBoundingClientRect()')
    assert page.document._layout_snapshot is not None
    assert page.evaluate('[box.getBoundingClientRect().width, oldRect.width]') == [0, 40]


async def test_rect_snapshot_rechecks_ancestor_transform_and_keeps_constructor_coercion(page):
    await page.set_content('<section><div style="width:40px;height:12px"></div></section>',
                           wait_until='load')
    assert page.evaluate("""(() => {
        const parent = document.querySelector('section'), child = parent.firstElementChild;
        const width = () => child.getBoundingClientRect().width;
        const before = width();
        parent.style.transform = 'rotate(90deg)';
        const rotated = width();
        parent.style.removeProperty('transform');
        const rect = new DOMRect('12', true, undefined, null);
        return [before, rotated, width(), rect.x, rect.y, rect.width, rect.height];
    })()""") == [40, 12, 40, 12, 1, 0, 0]
