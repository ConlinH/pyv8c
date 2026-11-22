"""Deep selector resolution: host-side queries and input across shadow trees and frames.

``deep=True`` is a host capability, not page JavaScript: it reaches closed
shadow trees and cross-origin frames that page script cannot see.  These tests
fix the observable contract of that capability.
"""
import pytest

from pyv8c.async_api import Browser
from pyv8c.sync_api import Browser as SyncBrowser
from pyv8c import PageSyntaxError


async def _page_with_shadow(mode='open', content='<input id="inside" type="checkbox">'):
    browser = await Browser(real_network=False, stub_logging='off').__aenter__()
    page = browser.new_profile().new_page()
    await page.set_content('<body><div id="host"></div></body>',
                           url='https://example.test/', wait_until='commit')
    page.evaluate(f'''(() => {{
        const root = document.getElementById('host').attachShadow({{mode:'{mode}'}});
        root.innerHTML = {content!r};
    }})()''')
    return browser, page


async def test_query_selector_deep_reaches_open_shadow():
    browser, page = await _page_with_shadow()
    try:
        assert page.query_selector('#inside') is None
        node = page.query_selector('#inside', deep=True)
        assert node is not None and node.attributes.get('id') == 'inside'
    finally:
        await browser.close()


async def test_query_selector_deep_reaches_closed_shadow():
    browser, page = await _page_with_shadow(
        mode='closed', content='<input id="sealed" type="checkbox">')
    try:
        assert page.evaluate("document.querySelector('#sealed') === null") is True
        node = page.query_selector('#sealed', deep=True)
        assert node is not None and node.attributes.get('id') == 'sealed'
    finally:
        await browser.close()


async def test_click_deep_closed_shadow_checkbox():
    browser = await Browser(real_network=False, stub_logging='off').__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.set_content('<body><div id="host"></div></body>',
                               url='https://example.test/', wait_until='commit')
        page.evaluate('''(() => {
            const root = document.getElementById('host').attachShadow({mode:'closed'});
            root.innerHTML = '<style>input{width:24px;height:20px}</style>' +
                             '<input id="box" type="checkbox">';
            window.hits = [];
            root.querySelector('input').addEventListener('click', e =>
                hits.push([e.isTrusted, e.target.checked]));
        })()''')
        # A closed shadow root is unreachable from page script, yet the host
        # finds it and drives the checkbox through the owning realm's mouse.
        assert page.evaluate("document.getElementById('host').shadowRoot === null") is True
        await page.click('#box', deep=True)
        assert page.evaluate('hits') == [[True, True]]
    finally:
        await browser.close()


async def test_dblclick_deep_reaches_shadow_element():
    browser, page = await _page_with_shadow(
        content='<style>button{width:80px;height:30px}</style><button id="twice">x</button>')
    try:
        page.evaluate('''(() => {
            const root = document.getElementById('host').shadowRoot;
            window.hits = 0;
            root.querySelector('button').addEventListener('dblclick', () => hits++);
        })()''')
        assert page.query_selector('#twice') is None
        await page.dblclick('#twice', deep=True)
        assert page.evaluate('hits') == 1
    finally:
        await browser.close()


async def test_hover_deep_fires_mouseover_in_shadow():
    browser, page = await _page_with_shadow(
        content='<style>button{width:80px;height:30px}</style><button id="over">x</button>')
    try:
        page.evaluate('''(() => {
            const root = document.getElementById('host').shadowRoot;
            window.hits = 0;
            root.querySelector('button').addEventListener('mouseover', () => hits++);
        })()''')
        await page.hover('#over', deep=True)
        assert page.evaluate('hits') >= 1
    finally:
        await browser.close()


async def test_click_deep_reaches_frame_element():
    browser = await Browser(real_network=False, stub_logging='off').__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.set_content('''<body style="margin:0"><iframe id="f" style="
            position:absolute;left:20px;top:20px;width:200px;height:60px;border:0"></iframe></body>''',
                               url='https://example.test/', wait_until='commit')
        page.evaluate('''(() => {
            const doc = document.getElementById('f').contentDocument;
            doc.body.style.margin = '0';
            doc.body.innerHTML = '<button id="go" style="width:120px;height:30px">hi</button>';
            const frameWindow = doc.defaultView;
            frameWindow.hits = 0;
            doc.getElementById('go').addEventListener('click', () => { frameWindow.hits++; });
        })()''')
        assert page.query_selector('#go') is None
        node = page.query_selector('#go', deep=True)
        assert node is not None and node.attributes.get('id') == 'go'
        await page.click('#go', deep=True)
        assert page.evaluate("document.getElementById('f').contentWindow.hits") == 1
    finally:
        await browser.close()


async def test_wait_for_selector_deep_waits_for_shadow_node():
    browser, page = await _page_with_shadow()
    try:
        page.evaluate("document.getElementById('host').shadowRoot.innerHTML = ''")
        page.evaluate('''(() => {
            const root = document.getElementById('host').shadowRoot;
            setTimeout(() => { root.innerHTML = '<input id="late" type="checkbox">'; }, 50);
        })()''')
        node = await page.wait_for_selector('#late', deep=True, timeout=3)
        assert node is not None and node.attributes.get('id') == 'late'
    finally:
        await browser.close()


async def test_wait_for_selector_deep_times_out():
    browser, page = await _page_with_shadow()
    try:
        with pytest.raises(TimeoutError):
            await page.wait_for_selector('#never', deep=True, timeout=0.1)
    finally:
        await browser.close()


async def test_deep_query_sees_later_shadow_insertion():
    browser, page = await _page_with_shadow(content='<p>before</p>')
    try:
        assert page.query_selector('#dynamic', deep=True) is None
        page.evaluate("document.getElementById('host').shadowRoot.innerHTML = '<b id=\"dynamic\">after</b>'")
        # Must not be served by a stale shadow/document descendant snapshot.
        node = page.query_selector('#dynamic', deep=True)
        assert node is not None and node.attributes.get('id') == 'dynamic'
    finally:
        await browser.close()


async def test_deep_query_multi_frame_returns_tree_order_first():
    browser = await Browser(real_network=False, stub_logging='off').__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.set_content('''<body style="margin:0">
            <iframe id="a" style="position:absolute;left:0;top:0;width:120px;height:40px;border:0"></iframe>
            <iframe id="b" style="position:absolute;left:0;top:50px;width:120px;height:40px;border:0"></iframe>
        </body>''', url='https://example.test/', wait_until='commit')
        page.evaluate('''(() => {
            for (const id of ['a', 'b']) {
                const doc = document.getElementById(id).contentDocument;
                doc.body.innerHTML = '<span id="same">' + id + '</span>';
            }
        })()''')
        node = page.query_selector('#same', deep=True)
        assert node is not None
        # First in tree order is the first iframe (a); its span text is 'a'.
        assert node.firstChild.nodeValue == 'a'
    finally:
        await browser.close()


async def test_deep_query_combinators_stay_inside_one_tree():
    browser, page = await _page_with_shadow(
        content='<div class="wrap"><span id="nested">ok</span></div>')
    try:
        # Combinators are matched within a single query root; the shadow host
        # is not part of the shadow tree, so a cross-boundary selector fails.
        assert page.query_selector('#host #nested', deep=True) is None
        node = page.query_selector('div.wrap > #nested', deep=True)
        assert node is not None and node.attributes.get('id') == 'nested'
    finally:
        await browser.close()


def test_sync_api_deep_parameters():
    with SyncBrowser(real_network=False, stub_logging='off') as browser:
        page = browser.new_page()
        page.set_content('<body><div id="host"></div></body>',
                         url='https://example.test/', wait_until='commit')
        page.evaluate('''(() => {
            const root = document.getElementById('host').attachShadow({mode:'open'});
            root.innerHTML = '<style>input{width:24px;height:20px}</style>' +
                             '<input id="box" type="checkbox">';
            window.hits = 0;
            root.querySelector('input').addEventListener('click', () => hits++);
        })()''')
        assert page.query_selector('#box') is None
        node = page.query_selector('#box', deep=True)
        assert node is not None and node.attributes.get('id') == 'box'
        page.click('#box', deep=True)
        assert page.evaluate('hits') == 1
        assert page.wait_for_selector('#box', deep=True, timeout=1) is not None


async def test_deep_staged_order_light_then_shadow_then_frame():
    browser = await Browser(real_network=False, stub_logging='off').__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.set_content('<body><i id="prio">light</i><div id="h"></div>'
                               '<iframe id="fr"></iframe></body>',
                               url='https://example.test/', wait_until='commit')
        page.evaluate('''(() => {
            document.getElementById('h').attachShadow({mode:'open'})
                .innerHTML = '<i id="prio">shadow</i>';
            document.getElementById('fr').contentDocument.body.innerHTML =
                '<i id="prio">frame</i>';
        })()''')
        node = page.query_selector('#prio', deep=True)
        assert node.firstChild.nodeValue == 'light'
        page.evaluate("document.querySelector('#prio').remove()")
        node = page.query_selector('#prio', deep=True)
        assert node.firstChild.nodeValue == 'shadow'
        page.evaluate("document.getElementById('h').remove()")
        node = page.query_selector('#prio', deep=True)
        assert node.firstChild.nodeValue == 'frame'
    finally:
        await browser.close()


async def test_deep_query_nested_shadow_and_shadow_hosted_frame():
    browser = await Browser(real_network=False, stub_logging='off').__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.set_content('<body><div id="host"></div></body>',
                               url='https://example.test/', wait_until='commit')
        page.evaluate('''(() => {
            const outer = document.getElementById('host').attachShadow({mode:'open'});
            outer.innerHTML = '<div id="inner"></div>' +
                '<iframe id="f" style="width:80px;height:40px"></iframe>';
            const inner = outer.querySelector('#inner').attachShadow({mode:'closed'});
            inner.innerHTML = '<input id="deepest">';
            outer.querySelector('#f').contentDocument.body.innerHTML =
                '<button id="inframe">x</button>';
        })()''')
        for selector in ('#deepest', '#inframe'):
            node = page.query_selector(selector, deep=True)
            assert node is not None and node.attributes.get('id') == selector[1:]
    finally:
        await browser.close()


async def test_frame_input_dispatch_and_cross_origin_reach():
    browser = await Browser(real_network=False, stub_logging='off').__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.set_content('<body style="margin:0"><iframe id="f" style="'
                               'position:absolute;left:0;top:0;width:200px;height:'
                               '120px;border:0"></iframe></body>',
                               url='https://example.test/', wait_until='commit')
        page.evaluate('''(() => {
            const doc = document.getElementById('f').contentDocument;
            doc.body.style.margin = '0';
            doc.body.innerHTML = '<style>button{width:60px;height:24px}</style>' +
                '<button id="b">x</button>';
            const w = doc.defaultView;
            w.hits = {dbl: 0, over: 0};
            const btn = doc.getElementById('b');
            btn.addEventListener('dblclick', () => w.hits.dbl++);
            btn.addEventListener('mouseover', () => w.hits.over++);
        })()''')
        assert page.query_selector('#b') is None
        await page.dblclick('#b', deep=True)
        await page.hover('#b', deep=True)
        hits = page.evaluate("document.getElementById('f').contentWindow.hits")
        assert hits == {'dbl': 1, 'over': hits['over']} and hits['over'] >= 1
    finally:
        await browser.close()


async def test_wait_for_selector_deep_finds_later_frame():
    browser = await Browser(real_network=False, stub_logging='off').__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.set_content('<body><iframe id="f"></iframe></body>',
                               url='https://example.test/', wait_until='commit')
        page.evaluate('''(() => {
            const doc = document.getElementById('f').contentDocument;
            setTimeout(() => {
                doc.body.innerHTML = '<b id="late">x</b>';
            }, 50);
        })()''')
        node = await page.wait_for_selector('#late', deep=True, timeout=3)
        assert node is not None and node.attributes.get('id') == 'late'
    finally:
        await browser.close()


def test_deep_invalid_selector_raises_page_syntax_error():
    import asyncio
    async def run():
        b = await Browser(real_network=False, stub_logging='off').__aenter__()
        try:
            page = b.new_profile().new_page()
            await page.set_content('<body></body>', url='https://example.test/',
                                   wait_until='commit')
            for call in (lambda: page.query_selector('>>>', deep=True),
                         lambda: page.query_selector('>>>')):
                with pytest.raises(PageSyntaxError):
                    call()
        finally:
            await b.close()
    asyncio.run(run())
