from pyv8c.async_api import Browser


async def test_performance_can_register_and_dispatch_events():
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        assert page.evaluate("""
            (() => {
              let calls = 0;
              performance.addEventListener('resourcetimingbufferfull', () => calls++);
              performance.dispatchEvent(new Event('resourcetimingbufferfull'));
              return calls;
            })()
        """) == 1
    finally:
        (await browser.close())


async def test_performance_resource_methods_accept_same_origin_frame_receiver():
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        assert page.evaluate("""
            (() => {
              const frame = document.createElement('iframe');
              document.body.appendChild(frame);
              const target = frame.contentWindow.performance;
              performance.setResourceTimingBufferSize.call(target, 1000);
              performance.clearResourceTimings.call(target);
              return true;
            })()
        """) is True
    finally:
        (await browser.close())
