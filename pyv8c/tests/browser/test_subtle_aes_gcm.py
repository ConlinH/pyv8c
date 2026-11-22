"""Exercise the page's native Web Crypto bindings with AES-GCM known answers."""

import json

import pytest


async def run_crypto(page, script):
    await page.set_content("<!doctype html>", url="https://repro.test/", wait_until="commit")
    page.evaluate(
        "globalThis.cryptoResult = null; (async () => {" + script + "})().then("
        "value => { cryptoResult = {value}; },"
        "error => { cryptoResult = {error: String(error)}; }); 'scheduled'"
    )
    await page.wait_for("cryptoResult !== null", timeout=3)
    result = json.loads(page.evaluate("JSON.stringify(cryptoResult)"))
    assert "error" not in result, result
    return result["value"]


# McGrew/Viega, The Galois/Counter Mode of Operation (GCM), Appendix B, cases 1 and 2.
@pytest.mark.parametrize(("plaintext", "ciphertext", "tag"), [
    ("", "", "58e2fccefa7e3061367f1d57a4e7455a"),
    ("00" * 16, "0388dace60b6a392f328c2b971b2fe78", "ab6e47d42cec13bdf53a67b21257bddf"),
])
async def test_aes_gcm_standard_vectors(page, plaintext, ciphertext, tag):
    vector = json.dumps([plaintext, ciphertext, tag])
    result = await run_crypto(page, """
        const [plaintext, ciphertext, tag] = VECTOR;
        const fromHex = text => Uint8Array.from(text.match(/../g) || [], x => parseInt(x, 16));
        const toHex = buffer => Array.from(new Uint8Array(buffer),
            x => x.toString(16).padStart(2, '0')).join('');
        const subtle = crypto.subtle;
        const key = await subtle.importKey('raw', new Uint8Array(16), 'AES-GCM',
                                           false, ['encrypt', 'decrypt']);
        const iv = new DataView(new ArrayBuffer(16), 2, 12);
        const input = fromHex('ff' + plaintext + 'ff').subarray(1, 1 + plaintext.length / 2);
        const observations = [];
        for (const tagLength of [undefined, 32, 64, 96, 104, 112, 120, 128]) {
            const options = {name: 'aes-gcm', iv, tagLength};
            const encrypted = await subtle.encrypt(options, key, input);
            const known = fromHex(ciphertext + tag.slice(0, (tagLength ?? 128) / 4));
            const decrypted = await subtle.decrypt(options, key, known.buffer);
            observations.push([encrypted instanceof ArrayBuffer, toHex(encrypted),
                               decrypted instanceof ArrayBuffer, toHex(decrypted)]);
        }
        return observations;
    """.replace("VECTOR", vector))
    assert result == [
        [True, ciphertext + tag[:bits // 4], True, plaintext]
        for bits in (128, 32, 64, 96, 104, 112, 120, 128)
    ]


async def test_aes_gcm_authenticates_additional_data(page):
    # McGrew/Viega Appendix B, case 4.
    result = await run_crypto(page, """
        const bytes = hex => Uint8Array.from(hex.match(/../g), x => parseInt(x, 16));
        const hex = buffer => Array.from(new Uint8Array(buffer),
            x => x.toString(16).padStart(2, '0')).join('');
        const key = await crypto.subtle.importKey('raw', bytes('feffe9928665731c6d6a8f9467308308'),
                                                  'AES-GCM', false, ['encrypt', 'decrypt']);
        const options = {name: 'AES-GCM', iv: bytes('cafebabefacedbaddecaf888'),
                         additionalData: bytes('feedfacedeadbeeffeedfacedeadbeefabaddad2')};
        const plaintext = 'd9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72' +
                          '1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39';
        const encrypted = await crypto.subtle.encrypt(options, key, bytes(plaintext));
        const decrypted = await crypto.subtle.decrypt(options, key, encrypted);
        options.additionalData[0] ^= 1;
        const rejection = await crypto.subtle.decrypt(options, key, encrypted)
            .then(() => 'accepted', error => error.name);
        return [hex(encrypted), hex(decrypted) === plaintext, rejection];
    """)
    assert result == [
        "42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e"
        "21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091"
        "5bc94fbc3221a5db94fae95ae7121a47",
        True, "OperationError",
    ]


async def test_aes_gcm_rejects_invalid_operations(page):
    assert await run_crypto(page, """
        const subtle = crypto.subtle;
        const raw = new Uint8Array(16);
        const key = await subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']);
        const encryptOnly = await subtle.generateKey({name: 'AES-GCM', length: 256},
                                                     false, ['encrypt']);
        const wrongKey = await subtle.importKey('raw', raw, 'AES-CBC', false, ['encrypt']);
        const options = {name: 'AES-GCM', iv: new Uint8Array(12)};
        const tagged = new Uint8Array(await subtle.encrypt(options, key, raw));
        tagged[tagged.length - 1] ^= 1;
        const errors = {};
        for (const [label, call] of Object.entries({
            missingArgument: () => subtle.encrypt(options, key),
            missingIv: () => subtle.encrypt('AES-GCM', key, raw),
            badIv: () => subtle.encrypt({...options, iv: []}, key, raw),
            badAad: () => subtle.decrypt({...options, additionalData: null}, key, tagged),
            badData: () => subtle.encrypt(options, key, {}),
            badKey: () => subtle.encrypt(options, {}, raw),
            badReceiver: () => subtle.encrypt.call({}, options, key, raw),
            badKeySize: () => subtle.importKey('raw', new Uint8Array(15), 'AES-GCM',
                                              false, ['encrypt']),
            unsupported: () => subtle.encrypt({name: 'AES-CBC', iv: raw}, wrongKey, raw),
            wrongAlgorithm: () => subtle.encrypt(options, wrongKey, raw),
            wrongUsage: () => subtle.decrypt(options, encryptOnly, tagged),
            emptyIv: () => subtle.encrypt({...options, iv: new Uint8Array(0)}, key, raw),
            invalidLength: () => subtle.encrypt({...options, tagLength: 72}, key, raw),
            outsideOctet: () => subtle.decrypt({...options, tagLength: 384}, key, tagged),
            shortCiphertext: () => subtle.decrypt(options, key, new Uint8Array(15)),
            badTag: () => subtle.decrypt(options, key, tagged),
        })) {
            const promise = call();
            errors[label] = await promise.then(() => 'accepted', error => error.name);
        }
        return errors;
    """) == {
        "missingArgument": "TypeError", "missingIv": "TypeError", "badIv": "TypeError",
        "badAad": "TypeError", "badData": "TypeError", "badKey": "TypeError",
        "badReceiver": "TypeError",
        "badKeySize": "DataError", "unsupported": "NotSupportedError",
        "wrongAlgorithm": "InvalidAccessError", "wrongUsage": "InvalidAccessError",
        "emptyIv": "OperationError", "invalidLength": "OperationError",
        "outsideOctet": "TypeError", "shortCiphertext": "OperationError", "badTag": "OperationError",
    }
