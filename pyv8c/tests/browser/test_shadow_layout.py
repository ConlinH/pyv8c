"""Rendered shadow-tree geometry and trusted input."""
from pyv8c.async_api import Browser


async def test_shadow_styles_layout_invalidation_and_document_hit_retargeting():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<style>input { width: 80px }</style><body></body>',
                         url='https://example.test/', wait_until="commit"))
        assert page.evaluate('''(() => {
            const host = document.createElement('div'); document.body.append(host);
            const root = host.attachShadow({mode: 'open'});
            root.innerHTML = '<style>input { width: 24px; height: 20px }</style>' +
                             '<input type="checkbox">';
            const input = root.querySelector('input');
            const rect = input.getBoundingClientRect();
            const result = [rect.width, rect.height,
                document.elementFromPoint(rect.x + 1, rect.y + 1) === host];
            root.querySelector('style').textContent = 'input {width: 30px; height: 22px}';
            result.push(input.getBoundingClientRect().width, input.getBoundingClientRect().height);
            host.remove();
            result.push(input.getBoundingClientRect().width);
            return result;
        })()''') == [24, 20, True, 30, 22, 0]
    finally:
        (await browser.close())


async def test_mouse_click_reaches_checkbox_in_closed_shadow_tree():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        page.evaluate('''(() => {
            const root = document.body.attachShadow({mode:'closed'});
            root.innerHTML = '<style>input {width:24px;height:20px}</style>' +
                             '<input type="checkbox">';
            window.clicks = [];
            root.querySelector('input').addEventListener('click', e => {
                clicks.push([e.isTrusted, e.target.checked]);
            });
        })()''')
        checkbox = page.realm.executor.call(
            lambda: page.document.get_body()._shadow_root.fn_querySelector('input'))
        (await page.mouse.click_element(checkbox))
        assert page.evaluate('clicks') == [[True, True]]
    finally:
        (await browser.close())


async def test_absolute_percent_height_uses_auto_height_positioned_grid():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<body><div id="outer" style="position:relative;display:grid;width:120px">
            <span style="display:block;width:40px;height:24px"></span>
            <input id="target" type="checkbox" style="position:absolute;left:0;top:0;
                width:100%;height:100%;margin:0"></div></body>''', url='https://example.test/', wait_until="commit"))
        assert page.evaluate('''(() => {
            const a = document.querySelector('#target').getBoundingClientRect();
            const b = document.querySelector('#outer').getBoundingClientRect();
            return [a.width, a.height, b.width, b.height];
        })()''') == [120, 24, 120, 24]
    finally:
        (await browser.close())


async def test_iframe_viewport_tracks_its_content_box_and_resize():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<body><iframe style="width:300px;height:65px;
                          padding:10px;border:2px solid"></iframe></body>''',
                         url='https://example.test/', wait_until="commit"))
        assert page.evaluate('''(() => {
            const frame = document.querySelector('iframe');
            const child = frame.contentWindow;
            const first = [child.innerWidth, child.innerHeight, frame.clientWidth,
                           frame.clientHeight, child.document.documentElement.clientWidth,
                           child.visualViewport.width, child.outerWidth === window.outerWidth];
            frame.style.width = '120px'; frame.style.height = '40px';
            return [first, [child.innerWidth, child.innerHeight,
                            child.document.documentElement.clientWidth]];
        })()''') == [[300, 65, 320, 85, 300, 300, True], [120, 40, 120]]
    finally:
        (await browser.close())


async def test_flex_item_with_implicit_grid_tracks_uses_content_width():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<body><div style="display:flex;width:300px"><div style="display:grid">
            <div style="display:flex"><label style="display:grid;position:relative">
            <input type="checkbox" style="position:absolute;width:100%;height:100%;margin:0">
            <span style="grid-area:1/1;width:24px;height:24px;box-sizing:border-box;border:2px solid"></span>
            <span style="grid-column:2;margin-left:8px;width:98px;height:18px"></span>
            </label></div></div></div></body>''', url='https://example.test/', wait_until="commit"))
        assert page.evaluate('''(() => {
            const a = document.querySelector('input').getBoundingClientRect();
            const b = document.querySelector('label').getBoundingClientRect();
            return [a.width, a.height, b.width, b.height];
        })()''') == [130, 24, 130, 24]
    finally:
        (await browser.close())


async def test_flex_cross_axis_center_moves_descendant_boxes():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<body style="margin:0"><div style="display:flex;width:300px;
                          height:65px;align-items:center"><div style="width:130px;height:24px">
                          <input type="checkbox" style="margin:0"></div></div></body>''',
                         url='https://example.test/', wait_until="commit"))
        assert page.evaluate('''(() => {
            const parent = document.querySelector('div div').getBoundingClientRect();
            const input = document.querySelector('input').getBoundingClientRect();
            return [parent.y, parent.height, input.y];
        })()''') == [20.5, 24, 20.5]
    finally:
        (await browser.close())


async def test_iframe_border_shorthand_and_frame_pointer_screen_coordinates():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<body style="margin:0"><iframe style="position:absolute;left:100px;
                          top:80px;width:300px;height:65px;border:0"></iframe></body>''',
                         url='https://example.test/', wait_until="commit"))
        page.evaluate('''(() => {
            const frame = document.querySelector('iframe');
            const doc = frame.contentDocument;
            doc.body.style.margin = '0';
            const root = doc.body.attachShadow({mode:'closed'});
            root.innerHTML = '<div style="display:flex;height:65px;align-items:center">' +
                '<input type="checkbox" style="width:130px;height:24px;margin:0"></div>';
            frame.contentWindow.coords = [];
            for (const type of ['pointerdown','mousedown','click'])
                root.querySelector('input').addEventListener(type, e =>
                    frame.contentWindow.coords.push([type,e.clientX,e.clientY,e.screenX,e.screenY]));
        })()''')
        def target():
            frame = page.document.fn_querySelector('iframe')
            child = frame._child_services._page_realm.page
            return child, child.document.get_body()._shadow_root.fn_querySelector('input')
        child, node = page.realm.executor.call(target)
        (await child.mouse.click_element(node))
        assert child.evaluate('coords') == [
            ['pointerdown', 65, 32.5, 165, 112.5],
            ['mousedown', 65, 32, 165, 112],
            ['click', 65, 32, 165, 112],
        ]
        assert page.evaluate('''(() => {
            const frame = document.querySelector('iframe');
            return [frame.offsetWidth, frame.offsetHeight];
        })()''') == [300, 65]
    finally:
        (await browser.close())
