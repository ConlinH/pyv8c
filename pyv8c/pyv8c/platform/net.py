"""Host-side HTTP for one page: ``page.net.get(...)``, ``page.net.post(...)``.

Driving a page from Python usually means talking to the same site it is on.  That
means sending the cookies it collected, the headers a browser would send, and
doing it over whichever transport the page itself uses -- cycronet, curl_cffi or
a host-supplied engine (see ``network/registry.py``).  This facade is that
shortcut.

It lives in the platform layer because it holds the context's transport and its
cookie jar, neither of which ``web/**`` may reach by name.  It never touches V8,
and nothing of it is visible to page code: a page sends its own requests through
``fetch``/XHR, which take the resource path instead.
"""
from __future__ import annotations

import json as json_module
from concurrent.futures import Future, ThreadPoolExecutor
import threading
from urllib.parse import urlencode, urljoin, urlsplit

from ..foundation.host_errors import HostStateError, LoadFailure
from ..foundation.urls import header_set as _header_set
from ..network.models import NetworkRequest, NetworkResponse
from .js_http import JsHttpController, JsHttpResponse
from .resources.request_headers import browser_headers

#: Methods the convenience helpers exist for.
_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _merge_headers(*layers):
    merged: dict[str, str] = {}
    for layer in layers:
        for key, value in (layer or {}).items():
            lowered = str(key).lower()
            for existing in [name for name in merged if name.lower() == lowered]:
                del merged[existing]
            merged[str(key)] = "" if value is None else str(value)
    return merged


class PageNet:
    """The page's network API, in two flavours.

    ``get``/``post``/... are host requests on the context's transport; ``fetch`` and
    ``xhr`` run the page's own JavaScript APIs instead, so a page that replaced either
    one is the implementation reached.  Both carry the page's cookies and headers.
    """

    def __init__(self, page):
        self._page = page
        self._js_http = JsHttpController(page)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pyv8c-net")
        self._closed = False
        self._submit_lock = threading.Lock()
        #: Read once: the browser's headers cannot change after construction.
        self._browser_headers = dict(getattr(page.profile.browser, "default_headers", None) or {})
        #: Above the browser's and below a request's own; mutable on purpose.
        self.headers: dict[str, str] = {}

    @property
    def available(self):
        backend = self._page.network_backend
        return backend is not None and backend.http_available

    def _backend(self):
        backend = self._page.network_backend
        if backend is None or not backend.http_available:
            raise LoadFailure("page.net requires an HTTP engine; set http_engine= or real_network=True")
        if self._closed:
            raise HostStateError("page.net is closed")
        return backend

    def _default_headers(self, target, method):
        """The headers the page's own ``fetch`` would send for this request."""
        return browser_headers(
            self._page.environment,
            destination="empty",
            mode="cors",
            method=method,
            url=target,
            current_url=self._page.url,
            referrer_policy=self._page.navigation.referrer_policy,
            http_version=2 if urlsplit(str(target)).scheme == "https" else 1,
        )

    def _resolved_url(self, url, params):
        target = urljoin(self._page.url, str(url))
        if params:
            separator = "&" if "?" in target else "?"
            target = f"{target}{separator}{urlencode(params, doseq=True)}"
        return target

    def _body(self, data, json, headers):
        if json is not None:
            _header_set(headers, "Content-Type", "application/json")
            return json_module.dumps(json)
        if isinstance(data, dict):
            _header_set(headers, "Content-Type", "application/x-www-form-urlencoded")
            return urlencode(data, doseq=True)
        return data

    def build(self, method, url, *, headers=None, params=None, data=None, json=None,
              cookies=True, timeout=None, referer=None, options=None):
        """Turn a host call into one :class:`NetworkRequest`."""
        target = self._resolved_url(url, params)
        merged = _merge_headers(
            self._default_headers(target, method),
            self._browser_headers,
            self.headers,
            headers,
        )
        if referer is not None:
            _header_set(merged, "Referer", referer)
        body = self._body(data, json, merged)
        transport_options = dict(options or {})
        request = NetworkRequest(
            url=target,
            method=str(method).upper(),
            headers=merged,
            body=body,
            timeout=timeout,
            # A host request has no origin to protect and no CORS to satisfy, so
            # it starts from the jar; ``omit`` tells the transport not to add the
            # cookie header the caller asked to suppress.
            credentials="omit" if cookies is False else "include",
            mode="no-cors",
            redirect="follow",
            origin=None,
            options=transport_options,
        )
        cookie = self._cookie_header(request, cookies)
        if cookie:
            request.headers["Cookie"] = cookie
        return request

    def _cookie_header(self, request, cookies):
        """The ``Cookie`` header for *request*, honouring the *cookies* argument."""
        if cookies is False: return ""
        jar = getattr(self._page.network_backend, "cookies", None)
        from_jar = jar.header_for(request.url) if jar is not None else ""
        if not isinstance(cookies, dict):
            return from_jar
        overridden = {str(name) for name in cookies}
        kept = [
            part for part in from_jar.split("; ")
            if part and part.split("=", 1)[0] not in overridden
        ]
        # The caller's own cookies lead, an explicit value being the most
        # specific one there is, the way the jar leads with its most specific path.
        explicit = [f"{name}={value}" for name, value in cookies.items()]
        return "; ".join(explicit + kept)

    def request(self, method, url, **kwargs) -> NetworkResponse:
        """Send one request and return its :class:`NetworkResponse`."""
        backend = self._backend()
        return backend.request_sync(self.build(method, url, **kwargs))

    def request_async(self, method, url, **kwargs) -> Future:
        """Send a host request on this page's pool and return its task Future.

        Queued work can be cancelled; running requests cannot. Closing the pool
        cancels queued Futures, whose result() raises CancelledError.
        """
        backend = self._backend()
        request = self.build(method, url, **kwargs)
        # Return the pool's own Future: cancellation and shutdown then refer to
        # the same queued work.  A separate Future could stay pending forever
        # when shutdown cancelled its runner, or accept cancellation mid-request.
        with self._submit_lock:
            if self._closed:
                raise HostStateError("page.net is closed")
            return self._executor.submit(backend.request_sync, request)

    def fetch(self,
              url: str,
              *,
              method: str = "GET",
              headers: dict | None = None,
              body: str | None = None,
              credentials: str | None = None,
              timeout: float = 30) -> JsHttpResponse:
        """Run the page's own ``fetch`` and return what it answered.

        The realm performs the request, so a page that replaced ``fetch`` is the
        implementation reached and the page's rules apply: CORS, the interceptor,
        the cache.  ``page.net.get`` is the other choice: a host request on the
        context's transport, outside those rules.

        :param url: URL to request; a relative one resolves against the page.
        :param method: HTTP method, upper-cased.
        :param headers: request headers, as a plain dict.
        :param body: request body as text; ``None`` sends none.
        :param credentials: ``"omit"``, ``"same-origin"`` or ``"include"``, as the
            JS ``RequestInit`` names them.
        :param timeout: seconds to wait for the request to settle.
        :returns: the :class:`~pyv8c.platform.js_http.JsHttpResponse` snapshot.
        :raises LoadFailure: the page's ``fetch`` rejected.
        :raises TimeoutError: the request had not settled within *timeout*.
        :raises RuntimeError: the page is closed.
        """
        return self._js_http.fetch(url, method=method, headers=headers, body=body,
                                   credentials=credentials, timeout=timeout)

    def fetch_async(self,
                    url: str,
                    *,
                    method: str = "GET",
                    headers: dict | None = None,
                    body: str | None = None,
                    credentials: str | None = None) -> Future:
        """Start the page's own ``fetch``; the ``Future`` settles on its answer.

        :param url: URL to request; a relative one resolves against the page.
        :param method: HTTP method, upper-cased.
        :param headers: request headers, as a plain dict.
        :param body: request body as text; ``None`` sends none.
        :param credentials: as the JS ``RequestInit`` names them.
        :returns: a :class:`concurrent.futures.Future` holding the
            :class:`~pyv8c.platform.js_http.JsHttpResponse` snapshot, or the error.
        """
        return self._js_http.fetch_async(url, method=method, headers=headers, body=body,
                                         credentials=credentials)

    def xhr(self,
            url: str,
            *,
            method: str = "GET",
            headers: dict | None = None,
            body: str | None = None,
            credentials: str | None = None,
            timeout: float = 30) -> JsHttpResponse:
        """Run the page's own ``XMLHttpRequest`` and return what it answered.

        Like :meth:`fetch`, the page's constructor answers.  This implementation
        exposes no ``getAllResponseHeaders``, so the snapshot's ``headers`` is empty.

        :param url: URL to request; a relative one resolves against the page.
        :param method: HTTP method, upper-cased.
        :param headers: request headers, sent with ``setRequestHeader``.
        :param body: request body as text; ``None`` sends none.
        :param credentials: ``"include"`` turns on ``withCredentials``.
        :param timeout: seconds to wait for the request to settle.
        :returns: the :class:`~pyv8c.platform.js_http.JsHttpResponse` snapshot.
        :raises LoadFailure: the request failed.
        :raises TimeoutError: the request had not settled within *timeout*.
        :raises RuntimeError: the page is closed.
        """
        return self._js_http.xhr(url, method=method, headers=headers, body=body,
                                 credentials=credentials, timeout=timeout)

    def xhr_async(self,
                  url: str,
                  *,
                  method: str = "GET",
                  headers: dict | None = None,
                  body: str | None = None,
                  credentials: str | None = None) -> Future:
        """Start the page's own ``XMLHttpRequest``; the ``Future`` gets its answer.

        :param url: URL to request; a relative one resolves against the page.
        :param method: HTTP method, upper-cased.
        :param headers: request headers, sent with ``setRequestHeader``.
        :param body: request body as text; ``None`` sends none.
        :param credentials: ``"include"`` turns on ``withCredentials``.
        :returns: a :class:`concurrent.futures.Future` holding the
            :class:`~pyv8c.platform.js_http.JsHttpResponse` snapshot, or the error.
        """
        return self._js_http.xhr_async(url, method=method, headers=headers, body=body,
                                       credentials=credentials)

    def close(self):
        with self._submit_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)


def _install_methods():
    """Give ``PageNet`` its ``get``/``post``/... and ``*_async`` helpers."""
    for name in _METHODS:
        method = name.upper()

        def sync(self, url, _method=method, **kwargs):
            return self.request(_method, url, **kwargs)

        def asynchronous(self, url, _method=method, **kwargs):
            return self.request_async(_method, url, **kwargs)

        sync.__name__ = name
        sync.__doc__ = f"Send a {method} request."
        asynchronous.__name__ = f"{name}_async"
        asynchronous.__doc__ = f"Send a {method} request on the page's pool; returns a ``Future``."
        setattr(PageNet, name, sync)
        setattr(PageNet, f"{name}_async", asynchronous)


_install_methods()


__all__ = ["PageNet"]
