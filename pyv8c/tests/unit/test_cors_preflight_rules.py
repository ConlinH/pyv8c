import pytest

from pyv8c.foundation.host_errors import LoadFailure
from pyv8c.platform.resources.cors import (preflight, preflight_request,
                                            require_inspectable_redirects, validate_preflight)

ORIGIN = ('http', 'source.test', 80)
TARGET = 'http://target.test/'


@pytest.mark.parametrize('method,headers,expected', [
    ('GET', {}, None), ('POST', {'content-type': 'text/plain;charset=UTF-8'}, None),
    ('POST', {'content-type': 'application/json'}, ['content-type']),
    ('GET', {'x-b': 'b', 'x-a': 'a'}, ['x-a', 'x-b']),
    ('PUT', {}, []), ('GET', {'range': 'bytes=0-10'}, None),
    ('GET', {'range': 'bytes=10-1'}, ['range']),
    ('GET', {'accept-language': 'x' * 129}, ['accept-language']),
])
def test_preflight_detection(method, headers, expected):
    assert preflight(ORIGIN, TARGET, 'cors', method, headers) == expected


def test_preflight_rejects_unallowed_method_or_header_and_credential_wildcards():
    headers = {'access-control-allow-origin': '*', 'access-control-allow-methods': '*',
               'access-control-allow-headers': '*'}
    validate_preflight(204, headers, ORIGIN, 'omit', 'PUT', ['x-test'])
    with pytest.raises(LoadFailure):
        validate_preflight(204, headers, ORIGIN, 'include', 'PUT', ['x-test'])
    with pytest.raises(LoadFailure):
        validate_preflight(204, headers, ORIGIN, 'omit', 'PUT', ['authorization'])
    with pytest.raises(LoadFailure):
        validate_preflight(302, headers, ORIGIN, 'omit', 'PUT', [])
    with pytest.raises(LoadFailure, match='invalid access-control-allow-methods'):
        validate_preflight(204, {**headers, 'access-control-allow-methods': 'PUT, invalid/name'},
                           ORIGIN, 'omit', 'PUT', [])
    with pytest.raises(LoadFailure, match='invalid access-control-allow-headers'):
        validate_preflight(204, {**headers, 'access-control-allow-headers': 'x-test, invalid/name'},
                           ORIGIN, 'omit', 'PUT', ['x-test'])
    assert preflight_request(ORIGIN, 'PUT', ['authorization'])['credentials'] == 'omit'


def test_unsafe_cors_requests_reject_uninspectable_redirect_chains():
    with pytest.raises(LoadFailure, match='cannot inspect redirects'):
        require_inspectable_redirects(True, 'cors', 'PUT', {})
    require_inspectable_redirects(False, 'cors', 'PUT', {})
    require_inspectable_redirects(True, 'cors', 'GET', {})
    require_inspectable_redirects(True, 'navigate', 'PUT', {})
