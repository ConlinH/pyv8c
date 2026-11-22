"""A page cannot make the network stack load a scheme the browser owns.

Chrome fails ``chrome-extension://`` locally and never puts it on the wire; the values
below are the ones Chrome 154.0.8037.98 shows a page.
"""
import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")

from pyv8c.async_api import Browser

EXTENSION_URL = "chrome-extension://kjacjjdnoddnpbbcjilcajfhhbdhkpg/images/about/en/1.png"


@pytest.mark.parametrize('target', ['file:///etc/passwd', 'custom://host/resource', EXTENSION_URL])
async def test_blocked_scheme_cannot_reach_custom_loader(target):
    from pyv8c.foundation.host_errors import LoadFailure

    calls = []
    async with Browser(real_network=False, resource_loader=lambda *args: calls.append(args)) as browser:
        page = browser.new_page()
        await page.set_content("<p>local</p>", "https://example.test/")
        with pytest.raises(LoadFailure, match="not a scheme a page loads"):
            page.executor.call(page.resource_fetcher.load_resource, target, {})
    assert calls == []


async def test_blocked_scheme_cannot_reach_interceptor():
    calls = []
    browser = (await Browser(stub_logging='off', resource_interceptor=lambda *args: calls.append(args)).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        page.evaluate("fetch('file:///secret').catch(() => { window.done = true; })")
        (await page.run_until_idle(1))
        assert page.evaluate('window.done') is True
        assert calls == []
    finally:
        (await browser.close())


async def test_a_browser_own_scheme_never_reaches_the_engine():
    """The failure is the browser's, and the network engine is never asked."""
    asked = []

    class Transport:
        """The engine ``http_engine`` builds: reaching it at all fails the test."""

        def submit_request(self, request, handle, remaining):
            asked.append(request.url)
            raise AssertionError("the network engine was asked for a browser scheme")

        def close(self):
            pass

    browser = (await Browser(stub_logging="off", http_engine=Transport).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
        page.evaluate("""
          fetch(%s).then(
            () => { window.__fetched = ['resolved']; },
            (error) => { window.__fetched = [error.name, error.message,
                                            error instanceof TypeError]; });
          const xhr = new XMLHttpRequest();
          xhr.onerror = (event) => { window.__xhr = [xhr.status, xhr.statusText,
              xhr.responseText, xhr.response, xhr.responseType, String(xhr.responseXML),
              xhr.readyState, event.type, event.message === undefined]; };
          xhr.open('GET', %s);
          xhr.send();
        """ % (repr(EXTENSION_URL), repr(EXTENSION_URL)))
        (await page.run_until_idle(1))

        assert page.evaluate("window.__fetched") == ["TypeError", "Failed to fetch", True]
        assert page.evaluate("window.__xhr") == [0, "", "", "", "", "null", 4, "error", True]

        # The same URL as an image fails the way an element reports a load failure.
        page.evaluate("window.__image = new Image(); window.__image.src = %s;"
                      % repr(EXTENSION_URL))
        (await page.run_until_idle(1))
        assert page.evaluate("[window.__image.complete, window.__image.naturalWidth]") == [True, 0]
    finally:
        (await browser.close())
    assert asked == []
