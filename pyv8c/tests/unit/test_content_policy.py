"""Hash authorization is scoped to exact inline source and every active policy."""
import base64
import hashlib

import pytest

from pyv8c.foundation.content_policy import ContentPolicy


def hash_source(source, algorithm='sha256'):
    digest = hashlib.new(algorithm, source.encode('utf-8')).digest()
    return f"'{algorithm}-{base64.b64encode(digest).decode('ascii')}'"


@pytest.mark.parametrize('algorithm', ['sha256', 'sha384', 'sha512'])
def test_hash_matches_exact_utf8_script(algorithm):
    source = '\nwindow.message = "雪😃";\n'
    policy = ContentPolicy()
    policy.add(f"script-src 'unsafe-inline' {hash_source(source, algorithm)}")
    assert policy.allows_inline(source_text=source)
    assert not policy.allows_inline(source_text=source + ' ')
    assert not policy.allows_inline(source_text=source.strip())
    assert not policy.allows_inline()


def test_hash_and_nonce_must_satisfy_every_policy():
    source = 'globalThis.allowed = true;'
    policy = ContentPolicy()
    policy.add(f'script-src {hash_source(source)}')
    policy.add("script-src 'nonce-approved'")
    assert policy.allows_inline('approved', source_text=source)
    assert not policy.allows_inline('wrong', source_text=source)
    assert not policy.allows_inline('approved', source_text=source + '0')


def test_script_src_elem_overrides_other_script_hashes():
    source = 'globalThis.allowed = true;'
    policy = ContentPolicy()
    policy.add(f"default-src {hash_source(source)}; script-src-elem 'none'")
    assert not policy.allows_inline(source_text=source)


def test_hash_base64url_padding_and_algorithm_case():
    source = 'globalThis.allowed = true;'
    token = hash_source(source).replace('sha256-', 'SHA256-').replace('+', '-').replace('/', '_').replace('=', '')
    policy = ContentPolicy()
    policy.add(f'script-src {token}')
    assert policy.allows_inline(source_text=source)


def test_invalid_hash_does_not_authorize_source():
    policy = ContentPolicy()
    policy.add("script-src 'sha256-!!!'")
    assert not policy.allows_inline(source_text='arbitrary()')


def test_hash_encoding_replaces_lone_surrogates():
    policy = ContentPolicy()
    policy.add(f'script-src {hash_source(chr(0xfffd))}')
    assert policy.allows_inline(source_text='\ud800')
