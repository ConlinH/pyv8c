"""Crypto random fills operate on native integer ArrayBufferViews, in bytes."""

import json
import hashlib

import pytest

from pyv8c import pyv8
from pyv8c.web.bom import crypto
from pyv8c.web.page_errors import PageTypeError


def test_crypto_rejects_non_views_and_non_integer_views(page):
    result = page.evaluate('''(() => {
        const cases = [[], {}, new ArrayBuffer(8), new DataView(new ArrayBuffer(8)),
                       new Float32Array(2), new Float64Array(2)];
        if (typeof Float16Array !== 'undefined') cases.push(new Float16Array(2));
        return cases.map(value => {
            try { crypto.getRandomValues(value); return 'accepted'; }
            catch (e) { return e.name; }
        });
    })()''')
    assert result[:6] == ['TypeError', 'TypeError', 'TypeError', 'TypeMismatchError',
                          'TypeMismatchError', 'TypeMismatchError']
    assert all(name == 'TypeMismatchError' for name in result[6:])


def test_crypto_fills_the_entire_view_without_touching_its_neighbors(page, monkeypatch):
    monkeypatch.setattr(crypto.os, 'urandom', lambda length: bytes(range(length)))
    assert page.evaluate('''(() => {
        const buffer = new ArrayBuffer(16), bytes = new Uint8Array(buffer);
        bytes.fill(90);
        const view = new Uint32Array(buffer, 4, 2);
        Object.defineProperty(view, 'byteLength', {get() {throw Error('must not read');}});
        Object.defineProperty(view, 'constructor', {get() {throw Error('must not read');}});
        const same = crypto.getRandomValues(view) === view;
        return [same, Array.from(bytes)];
    })()''') == [True, [90] * 4 + list(range(8)) + [90] * 4]


def test_crypto_uses_byte_length_for_quota_and_supports_bigints(page, monkeypatch):
    monkeypatch.setattr(crypto.os, 'urandom', lambda length: b'\x80' * length)
    result = page.evaluate('''(() => {
        const big = new BigUint64Array(1), cases = [new Uint8Array(65536),
          new Uint8Array(65537), new Uint16Array(32768), new Uint16Array(32769)];
        const quota = cases.map(value => {
            try { crypto.getRandomValues(value); return 'ok'; }
            catch (e) { return e.name; }
        });
        return [quota, crypto.getRandomValues(big) === big, String(big[0])];
    })()''')
    assert result == [['ok', 'QuotaExceededError', 'ok', 'QuotaExceededError'], True,
                      '9259542123273814144']


def test_crypto_detached_views_follow_chrome(page):
    assert page.evaluate('''(() => {
        const a = new Uint8Array(8), d = new DataView(new ArrayBuffer(8));
        structuredClone(a.buffer, {transfer: [a.buffer]});
        structuredClone(d.buffer, {transfer: [d.buffer]});
        let failure;
        try { crypto.getRandomValues(d); } catch (e) { failure = e.name; }
        return [crypto.getRandomValues(a) === a, a.byteLength, failure];
    })()''') == [True, 0, 'TypeMismatchError']


def test_crypto_rejects_shared_views():
    context = pyv8.Context(object())
    try:
        view = context.exec_js('new Uint16Array(new SharedArrayBuffer(8), 2, 2)')
        with pytest.raises(PageTypeError, match='must not be shared'):
            crypto.Crypto().fn_getRandomValues(view)
        with pytest.raises(PageTypeError, match='must not be shared'):
            crypto._bytes_of(view, 'digest', 2)
    finally:
        context.close()


def test_subtle_digest_reads_view_bytes_and_offsets(page):
    page.set_content('<body></body>', url='https://repro.test/')
    page.evaluate('''window.__digests = null; (async () => {
        const bytes = new Uint8Array([1,2,3,4,5,6,7,8]);
        const u16 = new Uint16Array(bytes.buffer, 2, 2);
        Object.defineProperty(u16, 'byteLength', {get() {throw Error('must not read');}});
        const values = [u16, new Uint32Array(bytes.buffer, 4, 1),
                        new DataView(bytes.buffer, 1, 2), bytes.buffer];
        const detached = new Uint8Array(4);
        structuredClone(detached.buffer, {transfer: [detached.buffer]});
        values.push(detached);
        const result = [];
        for (const value of values) {
            const digest = await crypto.subtle.digest('SHA-256', value);
            result.push(Array.from(new Uint8Array(digest)));
        }
        window.__digests = result;
    })();''')
    page.wait_for('__digests !== null', timeout=3)
    actual = json.loads(page.evaluate('JSON.stringify(__digests)'))
    data = (bytes([3, 4, 5, 6]), bytes([5, 6, 7, 8]), bytes([2, 3]),
            bytes([1, 2, 3, 4, 5, 6, 7, 8]), b'')
    assert actual == [list(hashlib.sha256(value).digest()) for value in data]
