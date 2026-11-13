"""XMLHttpRequest implementation."""
import json
import re
from functools import partial
from urllib.parse import urljoin, urlsplit

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import (
    MISSING, boolean, byte_string, dom_string, integer, is_nullish, missing_error, usv_string,
)
from ...foundation.error_policy import control_exceptions
from ..page_errors import (InvalidStateError, InvalidAccessError, NetworkError,
                          NotSupportedError, SecurityError, PageSyntaxError, PageTypeError)
from ..dom.events import Event, EventTarget
from ..fileapi.blob import Blob
from .events import ProgressEvent
from .headers import Headers, readable_response_header_names, response_is_cors


@interface('XMLHttpRequestEventTarget', 'EventTarget')
class XMLHttpRequestEventTarget(EventTarget):
    pass


@interface('XMLHttpRequest', 'XMLHttpRequestEventTarget')
class XMLHttpRequest(XMLHttpRequestEventTarget):
    UNSENT, OPENED, HEADERS_RECEIVED, LOADING, DONE = 0, 1, 2, 3, 4

    #: The IDL's response type enum; blob and document responses are not implemented.
    _RESPONSE_TYPES = ("", "arraybuffer", "blob", "document", "json", "text")

    def __init__(self, *_extra, services=None, _realm=None, **kw):
        if services is pyv8.Undefined:
            services = None
        super().__init__(services=services or _realm, **kw)
        self._method = "GET"
        self._url = ""
        self._async = True
        self._request_headers = Headers()
        self._request_headers._set_request_guard()
        self._response = None
        self._response_buffer = None
        self.readyState = self.UNSENT
        self.status = 0
        self.status_text = ""
        self.response_url = ""
        self.responseText = ""
        self.response_type = ""
        self.timeout = 0
        self.withCredentials = False
        self._request_include_credentials = False
        #: The load in flight and the deadline armed for it, so a timeout can drop it.
        self._handle = None
        self._timer = None
        self._generation = 0
        self._send_flag = False

    def get_readyState(self): return self.readyState
    def get_status(self): return self.status
    def get_statusText(self): return self.status_text
    def get_responseURL(self): return self.response_url
    def get_responseText(self):
        if self.response_type not in ('', 'text'):
            raise InvalidStateError('responseText is only available for text responses')
        return self.responseText

    def get_response(self):
        """The body in the shape ``responseType`` asks for, as Chrome leaves it."""
        if self._response is None:
            return "" if self.response_type in ("", "text") else pyv8.Null
        if self.response_type == 'arraybuffer':
            if self.readyState != self.DONE:
                return pyv8.Null
            if self._response_buffer is None:
                body = self._response.body
                data = body if isinstance(body, bytes) else self._response.body_text().encode('utf-8')
                self._response_buffer = pyv8.JSTypeArray(list(data), pyv8.ArrayType.Uint8Array)._get('buffer')
            return self._response_buffer
        if self.response_type == "json":
            if not self._services:
                return json.loads(self.responseText)
            try:
                return self._services.realm_services.parse_json(self.responseText)
            except (ValueError, pyv8.JSException):
                return pyv8.Null
        if self.response_type in ("", "text"):
            return self.responseText
        return pyv8.Null

    def get_responseType(self): return self.response_type

    def set_responseType(self, value):
        if self.readyState in (self.LOADING, self.DONE):
            raise InvalidStateError("XMLHttpRequest is in the LOADING or DONE state")
        name = dom_string(value)
        if name not in self._RESPONSE_TYPES:
            raise PageTypeError(
                f"'{name}' is not a valid value for enumeration XMLHttpRequestResponseType")
        if not self._async:
            raise InvalidAccessError("responseType cannot be set for synchronous Window requests")
        self.response_type = name

    def get_responseXML(self):
        """No response here is a document, so there is no XML value to answer."""
        return pyv8.Null

    def get_withCredentials(self): return bool(self.withCredentials)
    def set_withCredentials(self, value): self.withCredentials = boolean(value)
    def get_timeout(self): return self.timeout
    def set_timeout(self, value):
        """``unsigned long``, and ignored once the request is running."""
        if not self._async:
            raise InvalidAccessError("timeout cannot be set for synchronous Window requests")
        if self.readyState not in (self.UNSENT, self.OPENED):
            return
        self.timeout = integer(value, bits=32, signed=False)

    def _state(self, value):
        self.readyState = value
        self._dispatch_event(Event("readystatechange"))

    def _clear_response(self):
        self._response = None
        self._response_buffer = None
        self.status, self.status_text, self.response_url, self.responseText = 0, '', '', ''

    def _invalidate_request(self):
        # Invalidate callbacks before cancellation, which may complete inline.
        self._generation += 1
        self._send_flag = False
        handle, self._handle = self._handle, None
        self._stop_timer()
        if handle is not None:
            handle.cancel()

    def _current_request(self, generation):
        return (generation == self._generation and self._services is not None
                and not self._services.realm_services.closed)

    def fn_open(self, method=MISSING, url=MISSING, async_value=True, *args):
        if method is MISSING:
            raise missing_error('XMLHttpRequest', 'open', 2, 0, constructor=False)
        if url is MISSING:
            raise missing_error('XMLHttpRequest', 'open', 2, 1, constructor=False)
        method = byte_string(method)
        url = usv_string(url)
        if async_value is pyv8.Undefined:
            async_value = True
        async_value = boolean(async_value)
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", method):
            raise PageSyntaxError("Invalid HTTP method")
        if method.upper() in ("CONNECT", "TRACE", "TRACK"):
            raise SecurityError("Forbidden HTTP method")
        base = self._services.realm_services.url if self._services else "about:blank"
        resolved = urljoin(base, url)
        try:
            parts = urlsplit(resolved)
            if not parts.scheme:
                raise ValueError("missing scheme")
            # Reading the port is what rejects a URL whose port cannot be one.
            _ = parts.port
        except ValueError as exc:
            raise PageSyntaxError("Invalid URL") from exc
        if not async_value:
            resources = getattr(self._services, 'resource_services', None)
            if not callable(getattr(resources, 'load_resource', None)):
                raise NotSupportedError("synchronous XMLHttpRequest is unavailable in this realm")
            if self.timeout or self.response_type:
                raise InvalidAccessError("synchronous Window requests require default timeout and responseType")
        # A new request starts from nothing: whatever the last one left is gone.
        self._invalidate_request()
        self._request_headers = Headers()
        self._request_headers._set_request_guard()
        self._clear_response()
        self._method, self._url, self._async = method.upper(), resolved, async_value
        if self.readyState != self.OPENED:
            self._state(self.OPENED)

    def fn_setRequestHeader(self, key=MISSING, value=MISSING, *_extra):
        if key is MISSING:
            raise missing_error('XMLHttpRequest', 'setRequestHeader', 2, 0, constructor=False)
        if value is MISSING:
            raise missing_error('XMLHttpRequest', 'setRequestHeader', 2, 1, constructor=False)
        key = byte_string(key)
        value = byte_string(value)
        if self.readyState != self.OPENED or self._send_flag:
            raise InvalidStateError("XMLHttpRequest is not in the OPENED state")
        self._request_headers.fn_append(key, value)

    def fn_getResponseHeader(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('XMLHttpRequest', 'getResponseHeader', 1, 0, constructor=False)
        name = dom_string(name)
        if not self._response:
            return pyv8.Null
        value = self._response.headers.fn_get(name)
        allowed = self._readable_response_header_names()
        return value if name.lower() in allowed else pyv8.Null

    def _readable_response_header_names(self):
        """Header names exposed to this XHR, without altering the network response."""
        headers = self._response.headers._data
        realm = getattr(self._services, 'realm_services', None)
        origin = getattr(realm, 'origin', None)
        cors = response_is_cors(origin, self._url, self.response_url,
                                getattr(self._response, '_redirect_crossed', False))
        return readable_response_header_names(
            headers, cors=cors, credentials=self._request_include_credentials)

    def _default_content_type(self, body):
        """What ``send`` puts in ``Content-Type`` when the author set none.

        The standard's rule, and the one ``sendBeacon`` already follows: a Blob brings its own
        type, nothing has no type, and everything else travels as text.  Chrome 154 sends
        ``text/plain;charset=UTF-8`` on the Cloudflare challenge's own submit POST -- the answer
        to *that* request is the one carrying ``cf-chl-out``, and a submit arriving without the
        header is answered without it, which is where the widget stopped one step short of
        ``cf_clearance`` (measured against Chrome 154, 2026-10-07).
        """
        if is_nullish(body):
            return None
        #: ``fn_get`` answers ``Null`` for a header it does not hold, not ``None``: testing
        #: against ``None`` made this always true, and every body came back untyped.
        if not is_nullish(self._request_headers.fn_get("content-type")):
            return None
        if isinstance(body, (bytes, bytearray, memoryview)):
            return None
        if isinstance(body, Blob):
            return body.get_type() or None
        return "text/plain;charset=UTF-8"

    def fn_send(self, body=None, *_extra):
        if is_nullish(body):
            body = None
        if self.readyState != self.OPENED or self._send_flag:
            raise InvalidStateError("XMLHttpRequest is not in the OPENED state")
        from ..fileapi.blob import _array_buffer_bytes
        binary = _array_buffer_bytes(body)
        if binary is not None:
            body = binary
        request = {
            "method": self._method, "headers": self._request_headers, "body": body,
            "credentials": "include" if self.withCredentials else "same-origin",
            "timeout": self.timeout / 1000 if self.timeout else None,
            "resource_type": "xhr",
        }
        self._request_include_credentials = self.withCredentials
        content_type = self._default_content_type(body)
        if content_type is not None:
            request["content_type"] = content_type
        generation = self._generation
        self._send_flag = True
        if not self._async:
            return self._send_sync(request)
        if self._services:
            handle = self._services.resource_services.load_resource_async(
                self._url, request, partial(self._finish, generation))
            # Inline completion may already have finished or opened a new request.
            if not self._current_request(generation) or not self._send_flag:
                handle.cancel()
                return
            self._handle = handle
            if self.timeout:
                self._timer = self._services.task_services.schedule(
                    partial(self._on_timeout, generation), self.timeout / 1000.0, repeat=False)

    def _send_sync(self, request):
        # The existing synchronous resource path enforces origin, redirects and
        # document ownership without pumping unrelated tasks or JS callbacks.
        try:
            response = self._services.resource_services.load_resource(self._url, request)
        except control_exceptions():
            raise
        except Exception as error:
            self._send_flag = False
            self._clear_response()
            self.readyState = self.DONE
            raise NetworkError("Synchronous XMLHttpRequest failed") from error
        self._response = response
        self.status, self.status_text = response.status, response.status_text
        self.response_url = str(response.url or self._url)
        self.responseText = response._body_value()
        self._send_flag = False
        # Sync XHR skips observable HEADERS_RECEIVED/LOADING and progress events.
        self._state(self.DONE)
        if self.readyState == self.UNSENT:
            # abort() inside the synchronous DONE handler resets the response.
            raise NetworkError("Synchronous XMLHttpRequest was aborted")
        body = response.body
        loaded = len(body) if isinstance(body, (bytes, bytearray)) else len(str(body).encode('utf-8'))
        for name in ('load', 'loadend'):
            self._dispatch_event(ProgressEvent(name, {
                'isTrusted': True, 'lengthComputable': True, 'loaded': loaded, 'total': loaded}))

    def _finish(self, generation, response, error):
        if not self._current_request(generation) or not self._send_flag:
            return
        self._handle, self._timer = None, self._stop_timer()
        if error:
            self._send_flag = False
            self._clear_response()
            self._state(self.DONE)
            if self.readyState == self.UNSENT:
                return
            # A ProgressEvent with nothing in it, as Chrome sends it: the reason a load
            # failed stays host-side.
            self._dispatch_event(ProgressEvent("error", {"isTrusted": True}))
            self._dispatch_event(ProgressEvent("loadend", {"isTrusted": True}))
            return
        self._response = response
        self.status = response.status
        self.status_text = response.status_text
        # What the request ended at: a redirect the engine followed moves it.
        self.response_url = str(response.url or self._url)
        self.responseText = response._body_value()
        self._state(self.HEADERS_RECEIVED)
        if not self._current_request(generation):
            self._finish_reentered_response(response)
            return
        self._state(self.LOADING)
        if not self._current_request(generation):
            self._finish_reentered_response(response)
            return
        self._send_flag = False
        self._state(self.DONE)
        if self.readyState == self.UNSENT:
            return
        # Chrome retains terminal events when a DONE handler opens another request.
        self._dispatch_load_events(response)

    def _finish_reentered_response(self, response):
        # A nested synchronous request can finish inside HEADERS_RECEIVED/LOADING.
        # Chrome then emits the outer terminal events, without changing the new
        # response or its state. A new request still in progress stays untouched.
        if self._current_request(self._generation) and self.readyState == self.DONE:
            self._dispatch_load_events(response)

    def _dispatch_load_events(self, response):
        body = response.body
        loaded = len(body) if isinstance(body, (bytes, bytearray)) else len(str(body))
        self._dispatch_event(ProgressEvent("load", {
            "isTrusted": True, "lengthComputable": True, "loaded": loaded, "total": loaded}))
        self._dispatch_event(ProgressEvent("loadend", {
            "isTrusted": True, "lengthComputable": True, "loaded": loaded, "total": loaded}))

    def fn_getAllResponseHeaders(self, *_extra):
        """The response's headers as one block, lower-case and sorted as Chrome writes
        it. Cookie-setting headers are not exposed to a page.
        """
        if not self._response:
            return ""
        lines = []
        allowed = self._readable_response_header_names()
        for name, value in self._response.headers._indexed_items():
            if name not in allowed:
                continue
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(item) for item in value)
            lines.append("%s: %s\r\n" % (name, value))
        return "".join(lines)

    def fn_abort(self, *_extra):
        active = self._send_flag and self.readyState in (self.OPENED, self.HEADERS_RECEIVED, self.LOADING)
        self._invalidate_request()
        generation = self._generation
        self._clear_response()
        if active:
            self._state(self.DONE)
            self._dispatch_event(ProgressEvent("abort", {"isTrusted": True}))
            self._dispatch_event(ProgressEvent("loadend", {"isTrusted": True}))
        if generation == self._generation:
            self.readyState = self.UNSENT

    def _stop_timer(self):
        """Disarm the timeout, answering with the ident so the caller can store it."""
        timer, self._timer = self._timer, None
        if timer is not None:
            self._services.task_services.cancel(timer)
        return None

    def _on_timeout(self, generation):
        """A timeout drops the request and reports ``loadend`` alone -- no ``error``
        and no ``abort``, which is what Chrome sends.  The realm's task queue runs
        this, so a host loader that blocks the owner thread delays it.
        """
        if not self._current_request(generation) or not self._send_flag:
            return
        self._invalidate_request()
        self._clear_response()
        self._state(self.DONE)
        self._dispatch_event(ProgressEvent("loadend", {"isTrusted": True}))

__all__ = ["XMLHttpRequestEventTarget", "XMLHttpRequest"]
