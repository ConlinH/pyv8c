"""Promise-returning MediaCapabilities operations validate their receiver."""

import pytest


@pytest.fixture
async def media_page(page):
    (await page.set_content('<body><input id="target"><iframe></iframe></body>',
                     url='https://example.test/', wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    return page


@pytest.mark.parametrize('method', ['decodingInfo', 'encodingInfo'])
async def test_invalid_receiver_rejects_promise_instead_of_throwing(media_page, method):
    media_page.evaluate('''(method => {
        const fn = navigator.mediaCapabilities[method];
        window.out = [];
        for (const receiver of [document.querySelector('input'), {}, null, window]) {
            try {
                const result = fn.call(receiver, {});
                out.push(['returned', result instanceof Promise]);
                result.catch(error => out.push(['rejected', error.name, error.message]));
            } catch (error) {
                out.push(['threw', error.name, error.message]);
            }
        }
    })(%s)''' % repr(method))
    (await media_page.wait_for('out.length === 8', timeout=2))
    result = media_page.evaluate('out')
    assert result[:4] == [['returned', True]] * 4
    assert result[4:] == [['rejected', 'TypeError',
                           f"Failed to execute '{method}' on 'MediaCapabilities': Illegal invocation"]] * 4


async def test_invalid_event_listener_receiver_reports_promise_rejection(media_page):
    media_page.evaluate('''(() => {
        window.out = [];
        window.addEventListener('unhandledrejection', event => {
            out.push([event.reason.name, event.reason.message]); event.preventDefault();
        });
        const target = document.getElementById('target');
        target.addEventListener('mousedown', navigator.mediaCapabilities.decodingInfo);
        target.dispatchEvent(new MouseEvent('mousedown'));
    })()''')
    (await media_page.wait_for('out.length === 1', timeout=2))
    assert media_page.evaluate('out') == [[
        'TypeError', "Failed to execute 'decodingInfo' on 'MediaCapabilities': Illegal invocation"]]


async def test_invalid_receiver_uses_method_realm_for_promise_and_error(media_page):
    media_page.evaluate('''(() => {
        const child = document.querySelector('iframe').contentWindow;
        const fn = child.navigator.mediaCapabilities.decodingInfo;
        const promise = fn.call({}, {});
        window.out = {parent: promise instanceof Promise, child: promise instanceof child.Promise};
        promise.catch(error => {
            out.error = [error instanceof TypeError, error instanceof child.TypeError,
                         error.name, error.message];
        });
    })()''')
    (await media_page.wait_for('out.error', timeout=2))
    assert media_page.evaluate('out') == {
        'parent': False, 'child': True,
        'error': [False, True, 'TypeError',
                  "Failed to execute 'decodingInfo' on 'MediaCapabilities': Illegal invocation"],
    }


async def test_cross_realm_valid_receiver_still_uses_receiver_services(media_page):
    media_page.evaluate('''(() => {
        const child = document.querySelector('iframe').contentWindow;
        const fn = child.navigator.mediaCapabilities.decodingInfo;
        const promise = fn.call(navigator.mediaCapabilities, {
            type: 'file', audio: {contentType: 'audio/mp4', channels: '2',
                                  bitrate: 128000, samplerate: 44100}
        });
        window.out = {parent: promise instanceof Promise, child: promise instanceof child.Promise};
        promise.then(value => { out.value = typeof value.supported; },
                     error => { out.error = [error.name, error.message]; });
    })()''')
    (await media_page.wait_for('out.value || out.error', timeout=2))
    assert media_page.evaluate('out') == {'parent': True, 'child': False, 'value': 'boolean'}
