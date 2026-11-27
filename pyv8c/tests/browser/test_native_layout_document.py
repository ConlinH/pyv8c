import pytest

from benchmarks.cases.layout import NESTED_READ, nested_markup, prepare
from pyv8c._native._native import LayoutDocument
from pyv8c.web.dom.layout import layout_snapshot


def test_native_batch_validation_keeps_live_tree_and_snapshot():
    engine = LayoutDocument()
    root = (1, 0, 0, 'html', None, {'width': '80px'}, None, [])
    child = (2, 1, 0, 'div', None, {'width': '20px', 'height': '10px'}, None, [])
    first = engine.solve(0, (100, 100), [root, child], [], True)
    assert first.client_rect(2) == (0, 0, 20, 10)

    with pytest.raises(ValueError, match='layout version mismatch'):
        engine.solve(0, (100, 100), [], [], False)
    with pytest.raises(ValueError, match='missing layout parent'):
        engine.solve(first.version, (100, 100), [], [1], False)
    with pytest.raises(ValueError, match='layout requires one root'):
        engine.solve(first.version, (100, 100), [(1, 2, *root[2:])], [], False)
    second = engine.solve(first.version, (100, 100), [], [2], False)
    assert second.client_rect(2) is None
    assert first.client_rect(2) == (0, 0, 20, 10)


def test_snapshot_metadata_tracks_style_changes_and_preserves_old_geometry():
    engine = LayoutDocument()
    root = (1, 0, 0, 'html', None, {}, None, [])
    body = (2, 1, 0, 'body', None, {}, None, [])
    container = (3, 2, 0, 'section', None, {
        'position': 'relative', 'width': '40px', 'height': '20px',
    }, None, [])
    positioned = {'position': 'absolute', 'left': '0px', 'top': '0px',
                  'width': '10px', 'height': '10px'}
    lower = (4, 3, 0, 'div', None, dict(positioned, **{'z-index': '1'}), None, [])
    upper = (5, 3, 1, 'div', None, dict(positioned, **{'z-index': '2'}), None, [])

    def update(snapshot, row, **style):
        row = (*row[:5], {**row[5], **style}, *row[6:])
        result = engine.solve(snapshot.version, (100, 100), [row], [], False)
        return result, row

    first = engine.solve(0, (100, 100), [root, body, container, lower, upper], [], True)
    assert first.hit_ids(1, 1)[:2] == [5, 4]
    assert first.offset_parent(5) == 3

    raised, lower = update(first, lower, **{'z-index': '3'})
    hidden, upper = update(raised, upper, **{'pointer-events': 'none'})
    removed, _ = update(hidden, lower, display='none')
    assert raised.hit_ids(1, 1)[:2] == [4, 5]
    assert hidden.hit_ids(1, 1)[0] == 4
    assert 5 not in hidden.hit_ids(1, 1)
    assert 4 not in removed.hit_ids(1, 1)
    assert first.hit_ids(1, 1)[:2] == [5, 4]

    scaled, container = update(removed, container, transform='matrix(2, 0, 0, 2, 0, 0)')
    resized, container = update(scaled, container, width='60px')
    assert scaled.client_rect(5) == (-20, -10, 20, 20)
    assert resized.client_rect(5) == (-30, -10, 20, 20)
    reset, _ = update(resized, container, position='static', transform='none')
    assert reset.offset_parent(5) == 2
    assert reset.client_rect(5) == (0, 0, 10, 10)
    assert first.offset_parent(5) == 3


async def test_single_point_update_and_clean_reads_share_native_snapshot(page, monkeypatch):
    from pyv8c.web.dom import layout

    source, expected = await prepare(page, 'layout')
    assert page.evaluate("getComputedStyle(document.querySelector('[data-r]')).color")
    assert page.executor.call(getattr, page.document, '_native_layout_document', None) is None
    assert page.evaluate(source) == expected

    def geometry():
        doc = page.document
        node = doc.fn_querySelector('[data-r]')
        return doc._native_layout_document, layout_snapshot(doc)[0], node

    state, previous, node = page.executor.call(geometry)
    recorded = []
    original_record = layout.DocumentLayout._record

    def record(self, candidate, *args):
        recorded.append(candidate)
        return original_record(self, candidate, *args)

    monkeypatch.setattr(layout.DocumentLayout, '_record', record)

    def unexpected_rules(_document):
        pytest.fail("clean geometry read requested the stylesheet rules")

    with monkeypatch.context() as patch:
        patch.setattr(layout, 'document_rules', unexpected_rules)
        assert page.evaluate("document.querySelector('[data-r]').getBoundingClientRect().width") == 101
        assert page.executor.call(geometry)[1] is previous
        assert recorded == []
    assert page.evaluate("""(() => {
        const el = document.querySelector('[data-r]');
        const old = el.getBoundingClientRect();
        const independent = old !== el.getBoundingClientRect();
        el.className = 'item0';
        const fresh = el.getBoundingClientRect();
        old.width = 63;
        return [Object.getPrototypeOf(old) === DOMRect.prototype, independent,
                old.width, fresh.width, el.offsetWidth];
    })()""") == [True, True, 63, 100, 100]
    assert recorded == [node]
    assert page.executor.call(geometry)[0] is state
    assert page.executor.call(previous.client_rect, node)[2] == 101


async def test_native_geometry_uses_positioning_and_box_edges_for_cssom_and_hit_testing(page):
    await page.set_content("""<!doctype html><style>body{margin:0}</style>
        <section style="position:relative;margin:4px;width:100px;height:60px;border:2px solid;padding:4px">
          <div id=wrap><div id=target style="position:absolute;top:6px;left:10px;width:30px;height:15px;padding:3px"></div></div>
        </section>""")
    assert page.evaluate("""(() => {
        const node = document.getElementById('target');
        const r = node.getBoundingClientRect();
        return [r.x, r.y, r.width, r.height, node.offsetParent.localName, node.offsetLeft, node.offsetTop,
                node.clientWidth, node.scrollHeight, getComputedStyle(node).width,
                document.elementFromPoint(r.x + 1, r.y + 1) === node];
    })()""") == [16, 12, 36, 21, 'section', 10, 6, 36, 21, '30px', True]


async def test_native_layout_reorders_and_replaces_live_nodes_without_reusing_ids(page):
    await page.set_content('<main><div style="height:10px"></div><aside style="height:20px"></aside></main>')

    def capture():
        boxes, _ = layout_snapshot(page.document)
        return boxes, page.document.fn_querySelector('aside')

    before, aside = page.executor.call(capture)
    key = before.keys_by_node[aside]
    assert page.evaluate("""(() => {
        const main = document.querySelector('main');
        main.prepend(main.lastElementChild);
        return Array.from(main.children, n => n.offsetTop - main.offsetTop);
    })()""") == [0, 20]
    moved, _ = page.executor.call(capture)
    assert moved.keys_by_node[aside] == key
    page.evaluate("document.querySelector('main').innerHTML = '<aside style=height:5px></aside>'")
    after, replacement = page.executor.call(capture)
    assert replacement is not aside
    assert after.keys_by_node[replacement] != key
    assert after.snapshot.client_rect(key) is None
    assert before.snapshot.client_rect(key)[3] == 20


async def test_native_layout_refreshes_cached_document_root_after_replacement(page):
    await page.set_content('<html style="width:90px"><body></body></html>')
    assert page.evaluate("""(() => {
        const previous = document.documentElement;
        const before = previous.getBoundingClientRect();
        const replacement = document.createElement('html');
        replacement.style.width = '120px';
        document.replaceChild(replacement, previous);
        return [before.width, replacement.getBoundingClientRect().width,
                previous.getBoundingClientRect().width, before.width];
    })()""") == [90, 120, 0, 90]


async def test_native_nested_grid_flex_and_text(page):
    await page.set_content(nested_markup(unequal=True))
    assert page.evaluate(f'({NESTED_READ})()')[:7] == [
        [0, 0, 208, 30], [0, 0, 90, 30], [0, 0, 20, 10], [20, 0, 20, 10],
        [118, 0, 90, 30], [118, 0, 20, 10], [118, 10, 20, 10],
    ]
    await page.set_content('<div style="width:90px;line-height:20px;font-size:16px">word word word word word</div>')
    assert page.evaluate("document.querySelector('div').getBoundingClientRect().height") == 60


async def test_absolute_static_position_tracks_original_flow_and_independent_insets(page):
    await page.set_content('''<!doctype html><style>body{margin:0}</style>
        <div id=spacer style="height:120px"></div><section style="margin-left:40px">
        <button id=target style="position:absolute;width:30px;height:20px"></button></section>''')
    assert page.evaluate('''(() => {
        const button = document.querySelector('#target');
        const position = () => {const r = button.getBoundingClientRect(); return [r.x, r.y];};
        const before = position();
        document.querySelector('#spacer').style.height = '90px';
        const moved = position();
        button.style.left = '5px';
        return [before, moved, position()];
    })()''') == [[40, 120], [40, 90], [5, 90]]


async def test_positioned_child_does_not_contribute_to_inline_block_line_height(page):
    await page.set_content('''<!doctype html><style>body{margin:0}</style>
        <div id=parent style="display:inline-block;position:relative;width:150px;line-height:24px">
          <div id=child style="position:absolute;left:70px;top:20px;width:30px;height:40px"></div>
          <span>A</span></div>''')
    assert page.evaluate('''(() => {
        const parent = document.querySelector('#parent');
        const r = document.querySelector('#child').getBoundingClientRect();
        return [parent.offsetHeight, r.x, r.y, r.width, r.height];
    })()''') == [24, 70, 20, 30, 40]


async def test_mixed_percentage_calc_resolves_against_current_content_width(page):
    await page.set_content('''<!doctype html><div id=parent style="width:200px;padding:10px">
        <div id=child style="width:calc(50% - 10px);height:20px"></div></div>''')
    assert page.evaluate('''(() => {
        const parent = document.querySelector('#parent'), child = parent.firstElementChild;
        const before = child.offsetWidth;
        parent.style.width = '300px';
        const resized = child.offsetWidth;
        child.style.maxWidth = 'calc(40% - 20px)';
        return [before, resized, child.offsetWidth];
    })()''') == [90, 140, 100]


async def test_anonymous_line_box_refreshes_strut_on_style_edits(page):
    await page.set_content('''<!doctype html><div id=target style="font-size:10px;line-height:20px">
        <span style="display:inline-block;width:15px;height:0"></span></div>''')
    assert page.evaluate('''(() => {
        const target = document.querySelector('#target');
        const before = target.getBoundingClientRect();
        target.style.lineHeight = '50px';
        return [before.height, target.offsetHeight];
    })()''') == [20, 50]


async def test_hit_testing_respects_dynamic_overflow_clips(page):
    await page.set_content('''<!doctype html><style>body{margin:0}</style>
        <div id=clip style="position:relative;width:30px;height:30px">
          <button id=target style="position:absolute;left:50px;top:0;width:30px;height:20px"></button>
        </div>''')
    assert page.evaluate('''(() => {
        const clip = document.querySelector('#clip'), target = document.querySelector('#target');
        const hits = () => document.elementsFromPoint(65,10).includes(target);
        const visible = hits();
        clip.style.overflow = 'hidden';
        const clipped = hits();
        const width = target.getBoundingClientRect().width;
        clip.style.overflow = 'visible';
        return [visible, clipped, width, hits()];
    })()''') == [True, False, 30, True]


async def test_shadow_geometry_and_hit_testing_follow_host_transform(page):
    await page.set_content('''<!doctype html><style>body{margin:0}</style>
        <div id=host style="width:100px;height:40px;transform:translateX(100px)"></div>''')
    assert page.evaluate('''(() => {
        const host = document.querySelector('#host');
        const shadow = host.attachShadow({mode:'open'});
        shadow.innerHTML = '<button style="display:block;width:20px;height:20px"></button>';
        const rect = shadow.firstElementChild.getBoundingClientRect();
        return [rect.x, rect.y, rect.width, document.elementFromPoint(10,10) === host,
                document.elementFromPoint(110,10) === host];
    })()''') == [100, 0, 20, False, True]


async def test_deep_document_solves_on_initial_read_and_after_mutation(page):
    await page.set_content('<!doctype html>' + '<div>' * 510
                           + '<div id=target style="width:25px;height:10px"></div>' + '</div>' * 510)
    assert page.evaluate('''(() => {
        const target = document.querySelector('#target');
        const before = target.getBoundingClientRect();
        target.style.width = '35px';
        return [before.width, target.offsetWidth];
    })()''') == [25, 35]
