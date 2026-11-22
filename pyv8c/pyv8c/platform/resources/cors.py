"""CORS preflight decisions shared by Window and Worker resource loaders."""
import re
from collections import OrderedDict
from threading import Lock
from time import monotonic
from urllib.parse import urldefrag
from ...foundation.host_errors import LoadFailure
from ...foundation.urls import origin_tuple, serialize_origin
from ...foundation.cors import header_size, safelisted_header as _safe
from ...network.request_builder import finalize_request

_SIMPLE_METHODS = frozenset(('GET', 'HEAD', 'POST'))
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class PreflightCache:
    """Bounded, Profile-local cache of exact successful authorization checks."""

    def __init__(self):
        self._entries = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def key(partition, origin, url, credentials, method, names):
        source = serialize_origin(origin)
        if partition and partition != 'null' and source != 'null':
            return partition, source, urldefrag(url).url, credentials, method, tuple(names)

    def contains(self, key):
        if key is None:
            return False
        with self._lock:
            expires = self._entries.get(key, 0)
            if expires <= monotonic():
                self._entries.pop(key, None)
                return False
            self._entries.move_to_end(key)
            return True

    def remember(self, key, headers):
        if key is None:
            return
        raw = headers.get('access-control-max-age', '5')
        try:
            age = min(600, int(raw))
        except (TypeError, ValueError):
            age = 5
        if age <= 0:
            return
        with self._lock:
            self._entries[key] = monotonic() + age
            self._entries.move_to_end(key)
            if len(self._entries) > 512:
                self._entries.popitem(last=False)

    def clear(self):
        with self._lock:
            self._entries.clear()


def check_cors(headers, origin, credentials):
    allowed = headers.get('access-control-allow-origin', '')
    if allowed not in ('*', serialize_origin(origin)):
        raise LoadFailure('CORS check failed')
    if credentials == 'include' and (allowed == '*' or
            headers.get('access-control-allow-credentials', '') != 'true'):
        raise LoadFailure('credentialed CORS request was not allowed')


def cross_origin_preflight(method, headers, content_type=None):
    author = {str(k).lower(): str(v) for k, v in headers.items()}
    if content_type and 'content-type' not in author:
        author['content-type'] = str(content_type)
    unsafe = {name for name, value in author.items() if not _safe(name, value)}
    if sum(header_size(value) for name, value in author.items() if _safe(name, value)) > 1024:
        unsafe.update(name for name, value in author.items() if _safe(name, value))
    return sorted(unsafe) if method.upper() not in _SIMPLE_METHODS or unsafe else None


def preflight(origin, target, mode, method, headers, content_type=None):
    """Return sorted unsafe author header names, or None if no preflight is needed."""
    if mode != 'cors' or tuple(origin) == origin_tuple(target):
        return None
    return cross_origin_preflight(method, headers, content_type)


def require_inspectable_redirects(follows, mode, method, headers, content_type=None):
    """An opaque redirect chain could otherwise skip a later hop's preflight."""
    if follows and mode == 'cors' and cross_origin_preflight(method, headers, content_type) is not None:
        raise LoadFailure('transport cannot inspect redirects before CORS preflight')


def preflight_request(origin, method, names, timeout=None):
    headers = {'Origin': serialize_origin(origin), 'Access-Control-Request-Method': method}
    if names:
        headers['Access-Control-Request-Headers'] = ','.join(names)
    return {'method': 'OPTIONS', 'headers': headers, 'credentials': 'omit',
            'mode': 'cors', 'redirect': 'error', 'timeout': timeout, 'body': None}


def network_preflight_request(target, origin, probe):
    return finalize_request(url=target, method='OPTIONS', headers=probe['headers'],
                            credentials='omit', mode='cors', redirect='manual',
                            origin=serialize_origin(origin), timeout=probe['timeout'])


def validate_preflight(status, headers, origin, credentials, method, names):
    if not 200 <= status < 300:
        raise LoadFailure('CORS preflight failed: non-success status')
    headers = {str(k).lower(): str(v) for k, v in headers.items()}
    check_cors(headers, origin, credentials)
    def tokens(name):
        if name not in headers:
            return set()
        values = [item.strip(' \t') for item in headers[name].split(',') if item.strip(' \t')]
        if not values or any(not _TOKEN.fullmatch(value) for value in values):
            raise LoadFailure(f'CORS preflight failed: invalid {name}')
        return set(values)

    methods = tokens('access-control-allow-methods')
    allowed = {name.lower() for name in tokens('access-control-allow-headers')}
    wildcard = credentials != 'include'
    if method not in _SIMPLE_METHODS and method not in methods and not (wildcard and '*' in methods):
        raise LoadFailure('CORS preflight failed: method not allowed')
    if any(name not in allowed and not (wildcard and '*' in allowed and name != 'authorization') for name in names):
        raise LoadFailure('CORS preflight failed: header not allowed')
