"""Host-side HTTP for one page: ``page.net.get(...)`` and friends."""
from __future__ import annotations

import asyncio
import json as json_module
from urllib.parse import urlencode, urljoin, urlsplit

from ..foundation.futures import bridge_future
from ..foundation.host_errors import HostStateError, LoadFailure
from ..foundation.urls import header_set as _header_set
from ..network.models import NetworkResponse
from .js_http import JsHttpController, JsHttpResponse
from ..network.request_builder import finalize_request, request_clock
from .resources.request_headers import browser_headers

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
    """Host HTTP calls and the page's own fetch/XHR helpers."""

    def __init__(self, page):
        self._page = page
        self._js_http = JsHttpController(page)
        self._closed = False
        self._closing = False
        self._close_complete = False
        self._browser_headers = dict(getattr(page.profile.browser, "default_headers", None) or {})
        self.headers: dict[str, str] = {}

    @property
    def available(self):
        backend = self._page.profile.network_backend
        return backend is not None and backend.http_available

    def _backend(self):
        backend = self._page.profile.network_backend
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
            current_url=self._page.window.location.href,
            referrer_policy=self._page.navigation.referrer_policy,
            http_version=2 if urlsplit(str(target)).scheme == "https" else 1,
        )

    def _resolved_url(self, url, params):
        # Loader callbacks may issue host requests off the owner.
        target = urljoin(self._page.window.location.href, str(url))
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
        return finalize_request(
            url=target,
            method=method,
            headers=merged,
            body=body,
            timeout=timeout,
            credentials="omit" if cookies is False else "include",
            mode="no-cors",
            redirect="follow",
            origin=None,
            options=options,
            jar=getattr(self._page.profile.network_backend, "cookies", None),
            cookie_override=cookies if isinstance(cookies, dict) else None,
        )

    async def request(self, method, url, *, timeout=None, **kwargs) -> NetworkResponse:
        """Send a host request without blocking the Page owner thread."""
        self._page._check_wait()
        remaining = request_clock(timeout)
        remaining()
        backend = self._backend()
        request = self.build(method, url, timeout=timeout, **kwargs)

        return await backend.request(self._page.services.task_services, id(self._page.top_page),
                                     request, remaining)

    async def fetch(self,
              url: str,
              *,
              method: str = "GET",
              headers: dict | None = None,
              body: str | None = None,
              credentials: str | None = None,
              timeout: float = 30) -> JsHttpResponse:
        """Run the page's own ``fetch``; a page that replaced it is reached."""
        return await self._wait_http("fetch", url, timeout, method=method,
                                     headers=headers, body=body, credentials=credentials)


    async def xhr(self,
            url: str,
            *,
            method: str = "GET",
            headers: dict | None = None,
            body: str | None = None,
            credentials: str | None = None,
            timeout: float = 30) -> JsHttpResponse:
        """Run the page's own XHR; the snapshot's headers are empty."""
        return await self._wait_http("xhr", url, timeout, method=method,
                                     headers=headers, body=body, credentials=credentials)


    async def _wait_http(self, kind, url, timeout, **options):
        self._page._check_wait()
        scope = asyncio.timeout(None if timeout is None else max(0.0, float(timeout)))
        future = None
        try:
            async with scope:
                deadline = scope.when()
                remaining = None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())
                future = getattr(self._js_http, "start_" + kind)(url, timeout=remaining, **options)
                return await bridge_future(future)
        except TimeoutError:
            if scope.expired():
                raise TimeoutError(
                    f"the page's {kind}({url}) did not settle in {timeout} seconds"
                ) from None
            raise
        finally:
            if future is not None and not future.done():
                future.cancel()

    def cancel_document_requests(self):
        self._js_http.cancel_pending()

    def _retire(self):
        if self._close_complete or self._closing:
            return
        self._closed = self._closing = True
        try:
            self._js_http.close()
            self._close_complete = True
        finally:
            self._closing = False


def _install_methods():
    """All host HTTP convenience methods share the asyncio contract."""
    for name in _METHODS:
        method = name.upper()

        async def request(self, url, _method=method, **kwargs):
            return await self.request(_method, url, **kwargs)

        request.__name__ = name
        request.__doc__ = f"Await a {method} request."
        setattr(PageNet, name, request)


_install_methods()


__all__ = ["PageNet"]
