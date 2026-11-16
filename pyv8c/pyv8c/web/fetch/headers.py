"""Fetch header collection; conversion precedes validation and mutation."""

import re

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import (
    MISSING,
    byte_string,
    dictionary,
    header_name,
    header_value,
    is_nullish,
    missing_error,
    sequence,
)
from ...foundation.cors import NO_CORS_HEADER_NAMES, safelisted_header
from ...foundation.urls import origin_tuple
from ..page_errors import PageTypeError

_FORBIDDEN_REQUEST_HEADERS = frozenset({
    "accept-charset", "accept-encoding", "access-control-request-headers",
    "access-control-request-method", "connection", "content-length", "cookie",
    "cookie2", "date", "dnt", "expect", "host", "keep-alive", "origin",
    "referer", "set-cookie", "te", "trailer",
    "transfer-encoding", "upgrade", "via",
})

_FORBIDDEN_RESPONSE_HEADERS = frozenset(('set-cookie', 'set-cookie2'))
_CORS_SAFE_RESPONSE_HEADERS = frozenset((
    'cache-control', 'content-language', 'content-length', 'content-type',
    'expires', 'last-modified', 'pragma',
))
_HEADER_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def response_is_cors(origin, initial_url, final_url, redirect_crossed=False):
    """Whether the request or an inspected redirect crossed the caller's origin."""
    if origin is None:
        return False
    origin = tuple(origin)
    return (origin_tuple(initial_url, inherited=origin) != origin
            or origin_tuple(final_url or initial_url, inherited=origin) != origin
            or redirect_crossed)


def readable_response_header_names(headers, *, cors=False, credentials=False):
    """Names visible to web APIs, leaving the underlying network headers intact."""
    names = set(headers) - _FORBIDDEN_RESPONSE_HEADERS
    if not cors:
        return names
    raw = headers.get('access-control-expose-headers', '')
    if isinstance(raw, (tuple, list)):
        raw = ','.join(str(item) for item in raw)
    tokens = [token.strip(' \t').lower() for token in str(raw).split(',')
              if token.strip(' \t')]
    if any(not _HEADER_TOKEN.fullmatch(token) for token in tokens):
        tokens = []
    if '*' in tokens and not credentials:
        return names
    return names & (_CORS_SAFE_RESPONSE_HEADERS | set(tokens))


def forbidden_request_header(name, value=""):
    name = str(name).lower()
    if name in _FORBIDDEN_REQUEST_HEADERS or name.startswith(("proxy-", "sec-")):
        return True
    if name in ("x-http-method", "x-http-method-override", "x-method-override"):
        return any(part.strip().upper() in ("CONNECT", "TRACE", "TRACK")
                   for part in str(value).split(","))
    return False


@interface('Headers', None)
class Headers:

    def _indexed_items(self):
        return [(name, self._data[name]) for name in self._sorted_names()]

    def __v8_iteration_source__(self):
        index = 0
        def next_pair():
            nonlocal index
            items = self._indexed_items()
            if index >= len(items): return None
            pair = items[index]
            index += 1
            return pair
        return next_pair

    def _sorted_names(self):
        """Header names in the order Chromium iterates them: sorted by name."""
        return sorted(self._data)




    def __init__(self, init=pyv8.Undefined, *_extra, **_):
        self._data = {}
        self._request_guard = False
        self._no_cors_guard = False
        self._immutable = False
        if init is pyv8.Undefined:
            return
        if isinstance(init, Headers):
            self._data.update(init._data)
            return
        # None is an internal absence marker; explicit JavaScript null is not.
        if init is None:
            return
        if is_nullish(init) or isinstance(init, (str, bool, int, float)):
            raise PageTypeError("Failed to construct 'Headers': The provided value is not of type '(sequence<sequence<ByteString>> or record<ByteString, ByteString>)'.")
        iterable = isinstance(init, (list, tuple))
        iterator = MISSING
        if isinstance(init, pyv8.JSObject):
            iterator = init._iterator_method()
            iterable = not is_nullish(iterator)

        def convert_pair(entry):
            pair = sequence(entry, byte_string)
            if len(pair) != 2:
                raise PageTypeError("Failed to construct 'Headers': Sequence initializer must only contain pair elements.")
            return tuple(pair)

        if iterable:
            converted = sequence(init, convert_pair, iterator_method=iterator)
        else:
            converted = [(byte_string(key), byte_string(value)) for key, value in dictionary(init).items()]
        for key, value in converted:
            self.fn_append(key, value)

    def _set_request_guard(self):
        self._data = {name: value for name, value in self._data.items()
                      if not forbidden_request_header(name, value)}
        self._request_guard = True

    def _set_no_cors_guard(self):
        self._data = {name: value for name, value in self._data.items()
                      if name in NO_CORS_HEADER_NAMES and safelisted_header(name, value)}
        self._no_cors_guard = True

    def fn_get(self, key=MISSING, *_extra):
        if key is MISSING:
            raise missing_error('Headers', 'get', 1, 0)
        key = byte_string(key, prefix="Failed to execute 'get' on 'Headers': ")
        return self._data.get(header_name(key), pyv8.Null)

    def fn_set(self, key=MISSING, value=MISSING, *_extra):
        if key is MISSING:
            raise missing_error('Headers', 'set', 2, 0)
        if value is MISSING:
            raise missing_error('Headers', 'set', 2, 1)
        key = byte_string(key, prefix="Failed to execute 'set' on 'Headers': ")
        value = byte_string(value, prefix="Failed to execute 'set' on 'Headers': ")
        key, value = header_name(key), header_value(value)
        if self._immutable:
            raise PageTypeError('Headers are immutable')
        if ((self._request_guard and forbidden_request_header(key, value)) or
                (self._no_cors_guard and (key not in NO_CORS_HEADER_NAMES or not safelisted_header(key, value)))):
            return
        self._data[key] = value

    def fn_append(self, key=MISSING, value=MISSING, *_extra):
        if key is MISSING:
            raise missing_error('Headers', 'append', 2, 0)
        if value is MISSING:
            raise missing_error('Headers', 'append', 2, 1)
        key = byte_string(key, prefix="Failed to execute 'append' on 'Headers': ")
        value = byte_string(value, prefix="Failed to execute 'append' on 'Headers': ")
        key, value = header_name(key), header_value(value)
        if self._immutable:
            raise PageTypeError('Headers are immutable')
        combined = self._data[key] + ", " + value if key in self._data else value
        if ((self._request_guard and forbidden_request_header(key, combined)) or
                (self._no_cors_guard and (key not in NO_CORS_HEADER_NAMES or not safelisted_header(key, combined)))):
            return
        self._data[key] = combined

    def fn_delete(self, key=MISSING, *_extra):
        if key is MISSING:
            raise missing_error('Headers', 'delete', 1, 0)
        key = byte_string(key, prefix="Failed to execute 'delete' on 'Headers': ")
        key = header_name(key)
        if self._immutable:
            raise PageTypeError('Headers are immutable')
        if ((not self._request_guard or not forbidden_request_header(key)) and
                (not self._no_cors_guard or key in NO_CORS_HEADER_NAMES)):
            self._data.pop(key, None)

    def fn_has(self, key=MISSING, *_extra):
        if key is MISSING:
            raise missing_error('Headers', 'has', 1, 0)
        key = byte_string(key, prefix="Failed to execute 'has' on 'Headers': ")
        return header_name(key) in self._data

__all__ = ["Headers"]
