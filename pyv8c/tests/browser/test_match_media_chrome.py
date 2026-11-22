"""Media queries exercised by the Costco frame, compared with local Chrome 155."""

from pyv8c.async_api import Browser


async def test_orientation_equal_dimensions_are_portrait_including_hidden_iframe():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        assert page.evaluate("""(() => {
          return ['width:100px;height:100px;border:0', 'display:none'].map(css => {
            const f = document.createElement('iframe'); f.style.cssText = css;
            document.body.appendChild(f); const w = f.contentWindow;
            return [w.innerWidth, w.innerHeight, w.matchMedia('(orientation: portrait)').matches,
                    w.matchMedia('(orientation: landscape)').matches];
          });
        })()""") == [[100, 100, True, False], [0, 0, True, False]]
    finally:
        (await browser.close())


async def test_resolution_screen_and_desktop_media_features():
    browser = (await Browser(real_network=False, stub_logging="off", environment={
        "viewport.width": 1280, "viewport.height": 720,
        "screen.width": 2560, "screen.height": 1080,
        "viewport.device_scale_factor": 1,
    }).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const queries = [
                '(min-resolution:0.625dppx)', '(min-resolution:5dppx)',
                '(min-resolution:5.960464477539062e-7dppx)',
                '(min-resolution:62.5dpi)', '(min-resolution:125dpi)',
                '(min-resolution:25dpcm)', '(min-resolution:50dpcm)',
                '(resolution = 0dpi)', '(width = 0px)', '(device-width = 0px)',
                '(aspect-ratio = 0.25)', '(device-aspect-ratio = 0.25)',
                '(color = 0)', '(color-index = 0)',
                '(dynamic-range: standard)', '(video-dynamic-range: standard)',
                '(prefers-reduced-transparency: no-preference)',
                '(device-posture: continuous)', '(orientation: landscape)'
            ];
            return queries.map(query => matchMedia(query).matches);
        })()""") == [True, False, True, True, False, True, False,
                    False, False, False, False, False, False, True,
                    True, False, True, True, True]
    finally:
        (await browser.close())


async def test_media_query_list_initializes_event_target_and_uses_dpr():
    browser = (await Browser(real_network=False, stub_logging="off", environment={
        "viewport.device_scale_factor": 2,
    }).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const query = '(min-resolution:2dppx)';
            const list = matchMedia(query);
            const listener = () => {};
            list.addEventListener('change', listener);
            list.removeEventListener('change', listener);
            list.addListener(listener);
            list.removeListener(listener);
            list.onchange = listener;
            return [list.matches, list.media, list.onchange === listener,
                    matchMedia('(min-resolution:2.1dppx)').matches,
                    matchMedia('(resolution:192dpi)').matches];
        })()""") == [True, "(min-resolution:2dppx)", True, False, True]
    finally:
        (await browser.close())


async def test_dpcm_rounds_to_hundredths_of_dppx():
    browser = (await Browser(real_network=False, stub_logging="off", environment={
        "viewport.device_scale_factor": 1,
    }).__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("""[
            matchMedia('(min-resolution:37.89dpcm)').matches,
            matchMedia('(min-resolution:37.99dpcm)').matches,
            matchMedia('(max-resolution:37.89dpcm)').matches,
            matchMedia('(min-resolution:96.1dpi)').matches
        ]""") == [True, False, True, False]
    finally:
        (await browser.close())


async def test_aspect_ratio_compares_cross_products_for_zero_size_frames():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', wait_until="commit"))
        assert page.evaluate("""(() => {
          const queries = ['(aspect-ratio:0.25)', '(aspect-ratio:4)',
            '(min-aspect-ratio:2.125)', '(max-aspect-ratio:2.125)',
            '(aspect-ratio:1/0)', '(aspect-ratio:0/0)', '(aspect-ratio:0/1)'];
          return [[0,0], [100,0], [0,100], [640,360]].map(([width,height]) => {
            const frame = document.createElement('iframe');
            frame.style.cssText = `width:${width}px;height:${height}px;border:0`;
            document.body.appendChild(frame);
            const result = queries.map(q => frame.contentWindow.matchMedia(q).matches);
            frame.remove();
            return result;
          });
        })()""") == [
            [True, True, True, True, True, True, True],
            [False, False, True, False, True, True, False],
            [False, False, False, True, False, False, True],
            [False, False, False, True, False, False, False],
        ]
    finally:
        (await browser.close())


async def test_resolution_rounds_fractional_device_scale_factor_to_binary32():
    browser = (await Browser(real_network=False, environment={
        'viewport.device_scale_factor': 1.0000000149011612,
    }).__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("""[
          matchMedia('(resolution:96dpi)').matches,
          matchMedia('(resolution:1dppx)').matches,
          matchMedia('(min-resolution:96dpi)').matches,
          matchMedia('(max-resolution:96dpi)').matches,
          matchMedia('(min-resolution:96.01dpi)').matches,
          matchMedia('(min-resolution:1e100dpi)').matches,
          matchMedia('(max-resolution:1e100dpi)').matches
        ]""") == [True, True, True, True, False, False, True]
    finally:
        (await browser.close())
