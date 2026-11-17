"""Promise operations reject foreign receivers in the method's realm."""

import pytest


OPERATIONS = [
    ('GPU', 'navigator.gpu', 'requestAdapter'),
    ('MediaDevices', 'navigator.mediaDevices', 'enumerateDevices'),
    ('SubtleCrypto', 'crypto.subtle', 'digest'),
]


@pytest.fixture
def secure_page(page):
    page.set_content('<body><input><iframe></iframe></body>', url='https://example.test/')
    page.run_until_idle(timeout=1)
    return page


@pytest.mark.parametrize('interface,object_path,method', OPERATIONS)
def test_invalid_receiver_returns_rejected_promise(secure_page, interface, object_path, method):
    secure_page.evaluate('''(() => {
        const fn = %s.%s;
        window.out = [];
        for (const receiver of [document.querySelector('input'), {}, null, window]) {
            try {
                const result = fn.call(receiver);
                out.push(['returned', result instanceof Promise]);
                result.catch(error => out.push(['rejected', error.name, error.message]));
            } catch (error) { out.push(['threw', error.name, error.message]); }
        }
    })()''' % (object_path, method))
    secure_page.wait_for('out.length === 8', timeout=2)
    result = secure_page.evaluate('out')
    assert result[:4] == [['returned', True]] * 4
    assert result[4:] == [['rejected', 'TypeError',
                          f"Failed to execute '{method}' on '{interface}': Illegal invocation"]] * 4


@pytest.mark.parametrize('interface,object_path,method', OPERATIONS)
def test_invalid_receiver_uses_method_realm(secure_page, interface, object_path, method):
    secure_page.evaluate('''(() => {
        const child = document.querySelector('iframe').contentWindow;
        const promise = child.%s.%s.call({});
        window.out = [promise instanceof Promise, promise instanceof child.Promise];
        promise.catch(error => out.push(error instanceof TypeError,
                                       error instanceof child.TypeError, error.message));
    })()''' % (object_path, method))
    secure_page.wait_for('out.length === 5', timeout=2)
    assert secure_page.evaluate('out') == [
        False, True, False, True,
        f"Failed to execute '{method}' on '{interface}': Illegal invocation",
    ]


@pytest.mark.parametrize('interface,object_path,method', OPERATIONS)
def test_borrowed_method_preserves_valid_receiver_realm(secure_page, interface, object_path, method):
    arguments = "['SHA-256', new Uint8Array([1, 2, 3])]" if interface == 'SubtleCrypto' else '[]'
    secure_page.evaluate('''(() => {
        const child = document.querySelector('iframe').contentWindow;
        const promise = child.%s.%s.apply(%s, %s);
        window.out = [promise instanceof Promise, promise instanceof child.Promise];
        promise.then(() => out.push('resolved'), error => out.push(error.name));
    })()''' % (object_path, method, object_path, arguments))
    secure_page.wait_for('out.length === 3', timeout=2)
    assert secure_page.evaluate('out') == [True, False, 'resolved']


def test_digest_valid_receiver_still_hashes_bytes(secure_page):
    secure_page.evaluate('''window.out = null;
        crypto.subtle.digest('SHA-256', new TextEncoder().encode('abc')).then(buffer => {
            out = Array.from(new Uint8Array(buffer), n => n.toString(16).padStart(2, '0')).join('');
        });''')
    secure_page.wait_for('out !== null', timeout=2)
    assert secure_page.evaluate('out') == (
        'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    )
