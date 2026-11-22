"""WebRTC promise methods reject incompatible receivers as Chrome does."""
import pytest

from pyv8c.async_api import Browser


@pytest.mark.parametrize('method', ['createOffer', 'createAnswer', 'setLocalDescription'])
async def test_invalid_peer_receiver_rejects_promise(method):
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        page.evaluate('''(method => {
            window.results = [];
            const fn = RTCPeerConnection.prototype[method];
            for (const receiver of [window, {}, null, document.body]) {
                try {
                    const p = fn.call(receiver);
                    results.push(['returned', p instanceof Promise]);
                    p.then(() => results.push(['resolved']),
                           e => results.push(['rejected', e.name, e.message]));
                } catch (e) { results.push(['thrown', e.name, e.message]); }
            }
        })(%s)''' % __import__('json').dumps(method))
        (await page.wait_for('results.length === 8', timeout=2))
        results = page.evaluate('results')
        assert results[:4] == [['returned', True]] * 4
        assert results[4:] == [['rejected', 'TypeError',
            f"Failed to execute '{method}' on 'RTCPeerConnection': Illegal invocation"]] * 4
    finally:
        (await browser.close())
