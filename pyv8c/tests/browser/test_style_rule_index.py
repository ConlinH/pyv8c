"""Candidate filtering preserves the complete cascade and observes live DOM state."""

import gc
import weakref

import pytest


@pytest.mark.parametrize(
    "css,markup,expected",
    [
        (".unused,#target{width:37px}.hit{width:41px}", '<div id="target" class="hit"></div>', 37),
        ("#missing,.hit{width:37px}.hit{width:41px}", '<div class="hit"></div>', 41),
        (".hit,#target{width:37px}.hit{width:41px}", '<div id="target" class="hit"></div>', 37),
        ("*{width:13px}[data-x]{width:29px}div{width:23px}", "<div data-x></div>", 29),
        (":not(.off){width:37px}.off{width:41px}", '<div class="hit"></div>', 37),
        (":is(.hit,#else){width:37px}.hit{width:41px}", '<div class="hit"></div>', 37),
        (
            "section.hit>div{width:37px}.hit{width:41px}",
            '<section class="hit"><div></div></section>',
            37,
        ),
        (
            ".outer .hit{width:37px}.hit{width:41px}",
            '<section class="outer"><div class="hit"></div></section>',
            37,
        ),
        ("#target.hit{width:37px}.hit{width:41px}", '<div id="target" class="hit"></div>', 37),
        (
            "#target{width:37px!important}.hit{width:41px!important}",
            '<div id="target" class="hit"></div>',
            37,
        ),
        (r".a\,b{width:37px}#a\.b{width:41px}", '<div id="a.b" class="a,b"></div>', 41),
        ("div{width:23px}div:is(.off,#missing){width:37px}", '<div class="hit"></div>', 23),
        ('.hit{width:41px}:where([data-x="x"]){width:37px}', '<div class="hit" data-x="x"></div>', 41),
        (':where([data-x="x"]){width:37px}*{width:41px}', '<div data-x="x"></div>', 41),
        (':where([data-x="x"],[data-y="y"]){width:37px}', '<div data-y="y"></div>', 37),
        (':where([data-x="x"],.hit){width:37px}', '<div class="hit"></div>', 37),
        (':where([data-x] > div,[data-y]){width:37px}', '<section data-x><div></div></section>', 37),
        ('div:where(section > :where([data-x^="x"])){width:37px}', '<section><div data-x="xyz"></div></section>', 37),
        (':where(:not([data-x]),[data-y]){width:37px}', '<div></div>', 37),
        (':where([data-x=""]),#hit{width:37px}.hit{width:41px}', '<div id="hit" class="hit"></div>', 37),
    ],
)
async def test_candidate_groups_preserve_specificity_and_source_order(page, css, markup, expected):
    (await page.set_content("<style>" + css + "</style>" + markup, wait_until="commit"))
    assert page.evaluate("document.querySelector('div').getBoundingClientRect().width") == expected


async def test_candidates_follow_id_class_and_ancestor_mutation(page):
    (await page.set_content("""<style>.one{width:10px}#changed{width:20px}.parent>.one{width:30px}</style>
        <section id="parent"></section><div id="target" class="one"></div>""", wait_until="commit"))
    assert page.evaluate("""(() => {
        const n=document.getElementById('target'),p=document.getElementById('parent');
        const read=()=>n.getBoundingClientRect().width, values=[read()];
        n.id='changed';values.push(read());n.className='';values.push(read());
        n.id='target';n.className='one';p.className='parent';p.appendChild(n);values.push(read());
        p.className='';values.push(read());n.className='other';n.style.width='40px';values.push(read());
        return values;
    })()""") == [10, 20, 20, 30, 10, 40]


async def test_stylesheet_edits_rebuild_candidates_and_preserve_rule_order(page):
    (await page.set_content('<style id="sheet">.hit{width:10px}</style><div class="hit"></div>', wait_until="commit"))
    assert page.evaluate("""(() => {
        const style=document.getElementById('sheet'),n=document.querySelector('div');
        const read=()=>n.getBoundingClientRect().width, values=[read()];
        style.sheet.insertRule('.hit{width:20px}',1);values.push(read());
        style.sheet.deleteRule(1);values.push(read());
        style.textContent='.hit{width:30px}';values.push(read());
        style.remove();n.style.width='40px';values.push(read());return values;
    })()""") == [10, 20, 10, 30, 40]


async def test_shadow_rule_indexes_remain_scoped_after_style_and_class_changes(page):
    (await page.set_content(
        '<style>.one{width:900px}.two{width:800px}</style><section id="host"></section>'
    , wait_until="commit"))
    assert page.evaluate("""(() => {
        const root=document.getElementById('host').attachShadow({mode:'open'});
        root.innerHTML='<style>.one{width:10px}.two{width:20px}</style><div class="one"></div>';
        const n=root.querySelector('div'),s=root.querySelector('style'),read=()=>n.getBoundingClientRect().width;
        const values=[read()];n.className='two';values.push(read());
        s.textContent='.two{width:30px}';values.push(read());return values;
    })()""") == [10, 20, 30]


@pytest.mark.parametrize("attribute_rules", [False, True])
async def test_sparse_rules_do_not_match_every_rule_to_every_element(page, monkeypatch, attribute_rules):
    from pyv8c.web.css import cascade

    selectors = [f':where([data-r="{i}"])' if attribute_rules else f".item{i}" for i in range(300)]
    css = "".join(f"{selector}{{width:{100+i}px;height:10px}}" for i, selector in enumerate(selectors))
    rows = "".join(f'<div class="item{i}" data-r="{i}"></div>' for i in range(128))
    (await page.set_content("<style>" + css + "</style>" + rows, wait_until="commit"))
    original = cascade._selector_matches
    calls = 0

    def counted(element, selector):
        nonlocal calls
        calls += 1
        return original(element, selector)

    monkeypatch.setattr(cascade, "_selector_matches", counted)
    assert (
        page.evaluate(
            "[...document.querySelectorAll('div')].reduce((s,n)=>s+n.getBoundingClientRect().width,0)"
        )
        == 20928
    )
    assert calls < 300


async def test_where_attribute_index_tracks_mutation_in_its_own_shadow_scope(page):
    await page.set_content(
        '<style>:where([data-r="on"]){width:900px}</style>'
        '<div id="outside" data-r="on"></div><section id="host"></section>', wait_until="commit",
    )
    assert page.evaluate("""(() => {
        const root = document.getElementById('host').attachShadow({mode:'open'});
        root.innerHTML = '<style>*{width:5px}:where([data-r]){width:10px}'
            + ':where([data-r="on"]){width:20px}</style><div></div>';
        const node = root.querySelector('div'), outside = document.getElementById('outside');
        const read = () => node.getBoundingClientRect().width;
        const widths = [read()];
        node.setAttribute('data-r', 'off'); widths.push(read());
        node.setAttribute('data-r', 'on'); widths.push(read());
        node.removeAttribute('data-r'); widths.push(read());
        return [widths, outside.getBoundingClientRect().width];
    })()""") == [[5, 10, 20, 5], 900]


async def test_candidate_iterator_does_not_retain_the_element(page):
    from pyv8c.web.css.cascade import document_rules

    (await page.set_content("<style>.hit{width:10px}</style>", wait_until="commit"))

    def probe():
        node = page.document.fn_createElement("div")
        node.fn_setAttribute("class", "hit")
        reference = weakref.ref(node)
        candidates = document_rules(page.document).candidates(node)
        del node
        gc.collect()
        assert (reference()) is None
        assert len(list(candidates)) == 1

    page.executor.call(probe)


async def test_rule_declaration_edits_invalidate_layout_without_rewriting_dom_text(page):
    text = ".hit{width:10px}.hit{width:15px}"
    (await page.set_content('<style id="sheet">' + text + '</style><div class="hit"></div>', wait_until="commit"))
    assert page.evaluate("""(() => {
        const s=document.getElementById('sheet'),r=s.sheet.cssRules[1],n=document.querySelector('div');
        const read=()=>n.getBoundingClientRect().width, values=[read()];
        r.style.width='25px';values.push(read());r.style.removeProperty('width');values.push(read());
        r.style.cssText='width:35px;height:20px';values.push(read());return [values,s.textContent];
    })()""") == [[15, 25, 10, 35], text]


async def test_deleting_the_last_group_rule_does_not_restore_its_original_source(page):
    (await page.set_content(
        '<style id="sheet">.hit{width:10px}@media screen{.hit{width:20px}}</style><div class="hit"></div>'
    , wait_until="commit"))
    assert page.evaluate("""(() => {
        const g=document.getElementById('sheet').sheet.cssRules[1],n=document.querySelector('div');
        const read=()=>n.getBoundingClientRect().width, values=[read()];
        g.deleteRule(0);values.push(g.cssRules.length,read());
        g.insertRule('.hit{width:30px}',0);values.push(g.cssRules.length,read());return values;
    })()""") == [20, 0, 10, 1, 30]


async def test_detached_cssom_edits_cannot_change_the_replacement_sheet(page):
    (await page.set_content('<style id="sheet">.hit{width:10px}</style><div class="hit"></div>', wait_until="commit"))
    assert page.evaluate("""(() => {
        const s=document.getElementById('sheet'),old=s.sheet,n=document.querySelector('div');
        old.insertRule('.hit{width:20px}',1);const before=n.getBoundingClientRect().width;
        s.textContent='.hit{width:30px}';old.cssRules[0].style.width='90px';
        old.insertRule('.hit{width:100px}',2);
        return [before,n.getBoundingClientRect().width,old.ownerNode===null,old.cssRules.length];
    })()""") == [20, 30, True, 3]


async def test_shadow_cssom_edits_remain_scoped(page):
    (await page.set_content(
        '<style>.hit{width:900px}</style><div class="hit"></div><section id="host"></section>'
    , wait_until="commit"))
    assert page.evaluate("""(() => {
        const root=document.getElementById('host').attachShadow({mode:'open'});
        root.innerHTML='<style>.hit{width:10px}</style><div class="hit"></div>';
        root.querySelector('style').sheet.insertRule('.hit{width:20px}',1);
        return [document.querySelector('div').getBoundingClientRect().width,
            root.querySelector('div').getBoundingClientRect().width];
    })()""") == [900, 20]


async def test_font_face_descriptors_survive_cssom_edits_and_remain_live(page):
    from pyv8c.web.dom.fonts import _font_face_rules

    (await page.set_content("""<style id="sheet">@font-face{font-family:"Bench Face";src:local("Arial")}
        .hit{width:10px}</style><div class="hit"></div>""", wait_until="commit"))
    before = _font_face_rules(page.document)
    assert len(before) == 1
    assert page.evaluate("""(() => {
        const sheet=document.getElementById('sheet').sheet;
        sheet.insertRule('.hit{width:20px}',2);
        return [sheet.cssRules[0] instanceof CSSFontFaceRule,sheet.cssRules[0].type,
            document.querySelector('div').getBoundingClientRect().width];
    })()""") == [True, 5, 20]
    assert _font_face_rules(page.document) == before
    page.evaluate("document.getElementById('sheet').sheet.cssRules[0].style.fontFamily='Other'")
    after = _font_face_rules(page.document)
    assert after[0][0] == "Other"
    assert after[0][1:] == before[0][1:]


async def test_attribute_edit_recascades_descendants_and_reuses_unrelated_styles(page, monkeypatch):
    from pyv8c.web.css import cascade
    from pyv8c.web.dom import layout

    await page.set_content("""
        <style>
            .group { color: red; --tone: red }
            .group.active { color: blue; --tone: blue }
            .leaf { width: 40px; background-color: var(--tone) }
            .group.active .leaf { width: 80px }
            #control { width: 20px; color: green }
        </style>
        <div class="group"><div class="leaf"></div></div>
        <div id="control"></div>
    """, wait_until="commit")
    assert page.evaluate("document.getElementById('control').offsetWidth") == 20
    misses = []
    cascade_element = cascade._cascade

    def track(element, rules, cache=None):
        if cache is None or element not in cache:
            misses.append(element)
        return cascade_element(element, rules, cache)

    monkeypatch.setattr(cascade, "_cascade", track)
    monkeypatch.setattr(layout, "_cascade", track)
    assert page.evaluate("""(() => {
        const group = document.querySelector('.group'), leaf = group.firstElementChild;
        const control = document.getElementById('control');
        group.classList.add('active');
        const style = getComputedStyle(leaf);
        return [getComputedStyle(control).color, style.color, style.backgroundColor,
                style.getPropertyValue('--tone'), leaf.offsetWidth, control.offsetWidth];
    })()""") == ["rgb(0, 128, 0)", "rgb(0, 0, 255)", "rgb(0, 0, 255)", "blue", 80, 20]
    group = page.executor.call(lambda: page.document.fn_querySelector('.group'))
    leaf = page.executor.call(lambda: page.document.fn_querySelector('.leaf'))
    assert misses == [leaf, group]


@pytest.mark.parametrize("combinator", ["+", "~"])
async def test_attribute_edit_invalidates_siblings_through_functional_selectors(page, combinator):
    joiner = '<i></i>' if combinator == '~' else ''
    await page.set_content(f"""
        <style>
            section {{ --tone: red }}
            :where([data-on] {combinator} section) {{ color: blue }}
            section:not(:where([data-on] {combinator} section)) {{ color: red }}
            section[data-target] {{ --tone: green }}
            section:is([data-on] {combinator} section) {{ --tone: blue }}
            .leaf {{ background-color: var(--tone) }}
        </style>
        <section id="previous"><div class="leaf"></div></section>
        <div id="toggle"></div>{joiner}<section data-target><div class="leaf"></div></section>
    """, wait_until="commit")
    assert page.evaluate("""(() => {
        const toggle = document.getElementById('toggle');
        const watched = getComputedStyle(document.querySelector('[data-target] .leaf'));
        const previous = getComputedStyle(document.querySelector('#previous .leaf'));
        const read = () => [watched.color, watched.backgroundColor, previous.color];
        const before = read();
        toggle.setAttribute('data-on', '');
        const after = read();
        toggle.removeAttribute('data-on');
        return [before, after, read()];
    })()""") == [
        ["rgb(255, 0, 0)", "rgb(0, 128, 0)", "rgb(255, 0, 0)"],
        ["rgb(0, 0, 255)", "rgb(0, 0, 255)", "rgb(255, 0, 0)"],
        ["rgb(255, 0, 0)", "rgb(0, 128, 0)", "rgb(255, 0, 0)"],
    ]


async def test_attribute_edit_invalidates_shadow_inheritance_and_scoped_siblings(page):
    await page.set_content("""
        <style>
            #host { color: red; --tone: red }
            #host[data-theme] { color: blue; --tone: blue }
            [data-on] + .target { width: 900px }
            .target { width: 600px }
        </style>
        <div id="host"></div><div id="outside" class="target"></div>
    """, wait_until="commit")
    assert page.evaluate("""(() => {
        const host = document.getElementById('host');
        const root = host.attachShadow({ mode: 'open' });
        root.innerHTML = '<style>:where(.target){width:10px;background-color:var(--tone)}'
            + ':where([data-on] + .target){width:20px}</style><i></i><div class="target"></div>';
        const target = root.querySelector('.target'), style = getComputedStyle(target);
        const outside = document.getElementById('outside');
        const read = () => [style.color, style.backgroundColor, target.offsetWidth, outside.offsetWidth];
        const before = read();
        host.setAttribute('data-theme', '');
        const inherited = read();
        root.querySelector('i').setAttribute('data-on', '');
        return [before, inherited, read()];
    })()""") == [
        ["rgb(255, 0, 0)", "rgb(255, 0, 0)", 10, 600],
        ["rgb(0, 0, 255)", "rgb(0, 0, 255)", 10, 600],
        ["rgb(0, 0, 255)", "rgb(0, 0, 255)", 20, 600],
    ]
