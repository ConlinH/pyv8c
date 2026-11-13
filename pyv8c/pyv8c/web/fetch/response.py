"""Fetch response model."""
import json
from copy import copy

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import byte_string, dictionary, integer, is_nullish, usv_string
from ...foundation.arguments import IdlArgumentError
from ..page_errors import PageRangeError, PageTypeError
from ..page_errors import js_error_value
from ..fileapi.blob import Blob, _array_buffer_bytes
from ..bom.compression_streams import ReadableStream
from .headers import Headers, readable_response_header_names, response_is_cors


def filtered_network_response(response, *, origin, initial_url, credentials='same-origin',
                              redirect_crossed=False):
    """Give a network response its script-visible headers and immutable guard."""
    response = copy(response)
    cors = response_is_cors(origin, initial_url, response.url, redirect_crossed)
    names = readable_response_header_names(
        response.headers._data, cors=cors, credentials=credentials == 'include')
    response._visible_headers = Headers()
    response._visible_headers._data = {name: value for name, value in response.headers._data.items()
                                      if name in names}
    response._visible_headers._immutable = True
    if response.type not in ('opaque', 'opaqueredirect', 'error'):
        response.type = 'cors' if cors and not initial_url.startswith('data:') else 'basic'
    return response


@interface('Response', None)
class Response:

    def __init__(self, body=None, options=None, *_extra, services=None, _realm=None, **_):
        if body is pyv8.Undefined:
            body = None
        if isinstance(body, ReadableStream):
            if body.get_locked() or body._pipe.disturbed:
                raise PageTypeError("Failed to construct 'Response': Response body object should not be disturbed or locked")
        elif isinstance(body, Blob):
            body = body._data
        elif isinstance(body, pyv8.JSTypeArray):
            body = bytes(body)
        elif isinstance(body, pyv8.JSObject) and (buffer := _array_buffer_bytes(body)) is not None:
            body = buffer
        elif not is_nullish(body) and not isinstance(body, bytes):
            body = usv_string(body)
        if options is pyv8.Undefined:
            options = None
        if not is_nullish(options):
            options = dictionary(options)
        if services is pyv8.Undefined:
            services = None
        options = options if isinstance(options, dict) else {}
        self._services = services or _realm
        self.body = body
        self._body_stream = body if isinstance(body, ReadableStream) else None
        self.headers = Headers(options.get("headers"))
        self._visible_headers = None
        self.status = integer(options.get("status", 200), bits=16)
        self.status_text = byte_string(options.get("statusText", options.get("reason", "")))
        if _realm is not None and not 200 <= self.status <= 599:
            raise PageRangeError("Failed to construct 'Response': The status provided (" + str(self.status) + ") is outside the range [200, 599].")
        if _realm is not None and self.status in (204, 205, 304) and not is_nullish(body):
            raise PageTypeError("Failed to construct 'Response': Response with null body status cannot have body")
        if any(character in self.status_text for character in "\r\n"):
            raise PageTypeError("Failed to construct 'Response': Invalid statusText")
        self.url = str(options.get("url", ""))
        self.redirected = bool(options.get("redirected", False))
        # A response a script builds is "default"; "basic" is what a same-origin
        # fetch returns, and "cors"/"opaque" belong to the network layer.
        self.type = str(options.get("type", "default"))
        self.body_used = False

    def get_status(self): return self.status
    def get_statusText(self): return self.status_text
    def get_ok(self): return 200 <= self.status < 300
    def get_url(self): return self.url
    def get_redirected(self): return self.redirected
    def get_type(self): return self.type
    def get_headers(self):
        return self.headers if self._visible_headers is None else self._visible_headers
    def get_bodyUsed(self):
        return self.body_used or (self._body_stream is not None and self._body_stream._pipe.disturbed)
    def get_body(self):
        if is_nullish(self.body):
            return pyv8.Null
        if self._body_stream is None:
            stream = ReadableStream(services=self._services)
            data = self.body if isinstance(self.body, bytes) else self.body_text().encode('utf-8')
            stream._pipe.enqueue(pyv8.JSTypeArray(list(data), pyv8.ArrayType.Uint8Array))
            stream._pipe.close()
            self._body_stream = stream
        return self._body_stream

    def fn_clone(self, *_extra):
        """A response with the same body, headers, status and type, usable on
        its own; neither side counts as consumed until something reads it.
        """
        if self.get_bodyUsed():
            raise PageTypeError("Body has already been consumed.")
        result = Response(self.body, {"status": self.status, "statusText": self.status_text,
                                      "headers": self.get_headers(), "url": self.url,
                                      "redirected": self.redirected, "type": self.type},
                          services=self._services)
        result.headers._immutable = self.get_headers()._immutable
        return result

    def body_text(self):
        if is_nullish(self.body):
            return ""
        return self.body.decode("utf-8", "replace") if isinstance(self.body, bytes) else usv_string(self.body)

    def _body_value(self):
        if self.get_bodyUsed() or (self._body_stream is not None and self._body_stream.get_locked()):
            raise PageTypeError("Body has already been consumed.")
        if not is_nullish(self.body):
            self.body_used = True
        return self.body_text()

    def _result(self, value):
        return self._services.realm_services.promise_resolve(value) if self._services else value

    def fn_text(self, *_extra):
        if isinstance(self.body, ReadableStream):
            return self._consume_stream(lambda data: data.decode('utf-8-sig', 'replace'))
        if not self._services:
            return self._body_value()
        promise, resolve, reject = self._services.realm_services.make_promise()
        try:
            resolve(self._body_value())
        except (PageTypeError, pyv8.JSException, UnicodeError, IdlArgumentError) as exc:
            reject(js_error_value(exc))
        return promise
    def fn_json(self, *_extra):
        """The body parsed inside the realm: one parse, and no value crosses to Python."""
        if isinstance(self.body, ReadableStream):
            return self._consume_stream(lambda data: self._services.realm_services.parse_json(
                data.decode('utf-8-sig', 'replace')))
        if not self._services:
            return json.loads(self._body_value())
        promise, resolve, reject = self._services.realm_services.make_promise()
        try:
            resolve(self._services.realm_services.parse_json(self._body_value()))
        except (PageTypeError, pyv8.JSException, UnicodeError, IdlArgumentError) as exc:
            reject(js_error_value(exc))
        return promise
    def fn_arrayBuffer(self, *_extra):
        if isinstance(self.body, ReadableStream):
            return self._consume_stream(lambda data: data)
        if not self._services:
            return self._body_value().encode()
        promise, resolve, reject = self._services.realm_services.make_promise()
        try:
            text = self._body_value()
            resolve(self.body if isinstance(self.body, bytes) else text.encode())
        except (PageTypeError, pyv8.JSException, UnicodeError, IdlArgumentError) as exc:
            reject(js_error_value(exc))
        return promise

    def _consume_stream(self, convert):
        promise, resolve, reject = self._services.realm_services.make_promise()
        if self.get_bodyUsed() or self.body.get_locked():
            reject(js_error_value(PageTypeError('Body has already been consumed.')))
            return promise
        self.body_used = True
        reader = self.body.fn_getReader()
        chunks = []

        def failed(reason):
            reject(js_error_value(reason))

        def received(item):
            try:
                if item._get('done'):
                    resolve(convert(b''.join(chunks)))
                    return
                chunk = item._get('value')
                if not isinstance(chunk, pyv8.JSObject) or not chunk._is_uint8_array():
                    raise PageTypeError('Received a non-Uint8Array chunk')
                chunks.append(bytes(chunk))
                read_next()
            except (PageTypeError, pyv8.JSException, TypeError, ValueError, UnicodeError) as error:
                failed(error)

        def read_next():
            next_promise = reader.fn_read()
            next_promise._get('then')._invoke(next_promise, received, failed)

        read_next()
        return promise

    def _start_wasm_stream(self, session, context):
        """Feed the V8 decoder as each stream read resolves on this realm."""
        reader = self.body.fn_getReader()

        def fail(reason):
            try:
                context._wasm_stream_abort(session, PageTypeError(
                    'WebAssembly compilation aborted: Network error: Failed to read from a ReadableStream.'))
            except RuntimeError as error:
                if 'session is closed' not in str(error).lower() and 'context is closed' not in str(error).lower():
                    raise

        def received(item):
            try:
                if item._get('done'):
                    context._wasm_stream_finish(session)
                    return
                chunk = item._get('value')
                if isinstance(chunk, pyv8.JSObject) and chunk._is_uint8_array():
                    data = bytes(chunk)
                else:
                    raise PageTypeError('WebAssembly compilation aborted: Network error: Failed to read from a ReadableStream.')
                context._wasm_stream_feed(session, data)
                read_next()
            except (PageTypeError, pyv8.JSException, TypeError, ValueError) as error:
                fail(error)
            except RuntimeError as error:
                if 'session is closed' not in str(error).lower() and 'context is closed' not in str(error).lower():
                    raise

        def read_next():
            promise = reader.fn_read()
            promise._get('then')._invoke(promise, received, fail)

        read_next()

def wasm_response_payload(response):
    """Snapshot an already buffered response for V8's streaming decoder."""
    if not isinstance(response, Response):
        return ('type', 'An argument must be provided, which must be a Response or Promise<Response> object', None, '')
    if not response.get_ok():
        return ('type', 'HTTP status code is not ok', None, '')
    mime = response.headers._data.get('content-type', '')
    if mime.lower() != 'application/wasm':
        return ('type', "Incorrect response MIME type. Expected 'application/wasm'.", None, '')
    if response.get_bodyUsed() or (response._body_stream is not None and response._body_stream.get_locked()):
        return ('type', 'Cannot compile WebAssembly.Module from an already read Response', None, '')
    if is_nullish(response.body):
        return ('compile', 'Empty WebAssembly module', None, '')
    response.body_used = True
    if isinstance(response.body, ReadableStream):
        return ('stream', None, None, response.url)
    body = response.body if isinstance(response.body, bytes) else response.body_text().encode('utf-8')
    return (None, None, body, response.url)


__all__ = ["Response"]
