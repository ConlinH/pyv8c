"""Cancellable curl_cffi bridge on the shared async delivery."""
import asyncio
import importlib
import sys
import time
from math import ceil

from .async_delivery import AsyncTransport
from .models import (
    NetworkRequest,
    NetworkResponse,
    NetworkUnavailableError,
)
from .proxy import PROXY_OPTION, curl_proxies
from .registry import signature_option_keys

_EXTRA_KWARGS = (
    "impersonate", "ja3", "akamai", "perk", "extra_fp",
    "proxies", "proxy", "proxy_auth", "verify", "cert",
    "http_version", "interface", "doh_url",
)


def _request_kwargs(request, *, proxies):
    """The one set of curl_cffi keywords for every call."""
    options = request.options or {}
    kwargs = {
        "headers": dict(request.headers),
        "discard_cookies": True,
        "allow_redirects": False,
    }
    if request.body is not None:
        kwargs["data"] = request.body
    if request.timeout is not None:
        # libcurl truncates to milliseconds. The framework timer owns the deadline.
        kwargs["timeout"] = (ceil(request.timeout * 1000) + 1) / 1000
    if proxies is not None:
        # Empty per-request values are the only ones that override HTTP_PROXY.
        kwargs["proxies"] = dict(proxies)
    for name in _EXTRA_KWARGS:
        if options.get(name) is not None:
            kwargs[name] = options[name]
    return kwargs


def _curl_cffi_module():
    try:
        return importlib.import_module("curl_cffi.requests")
    except ImportError as exc:
        raise NetworkUnavailableError(
            "http_engine='curl_cffi' requires the optional 'curl_cffi' package"
        ) from exc


def _async_session_type(module):
    session_type = getattr(module, "AsyncSession", None)
    if session_type is None:
        raise NetworkUnavailableError(
            "installed curl_cffi does not expose requests.AsyncSession"
        )
    return session_type


class CurlCffiTransport(AsyncTransport):
    """HTTP transport backed by one curl_cffi ``AsyncSession``."""

    bridge_option_keys = frozenset({"default_headers", PROXY_OPTION})

    @property
    def http_concurrency(self):
        return min(10, self.session.max_clients)

    @classmethod
    def accepted_option_keys(cls) -> frozenset[str] | None:
        session_keys = signature_option_keys(_async_session_type(_curl_cffi_module()))
        if session_keys is None:
            return None
        return session_keys | cls.bridge_option_keys

    def __init__(self, *, options=None):
        options = dict(options or {})
        self.default_headers = bool(options.pop("default_headers", False))
        self.proxies = curl_proxies(options.pop(PROXY_OPTION, None))
        super().__init__(options=options, loop_name="pyv8c-curl-loop")

    def _open_session(self):
        module = _curl_cffi_module()
        session_type = _async_session_type(module)
        self.curl_cffi = module
        options = dict(self.options)
        options.setdefault("default_headers", self.default_headers)
        try:
            self.session = session_type(loop=self.loop, **options)
        except TypeError as exc:
            # No key list to check against: name where the option came from.
            raise TypeError(
                f"http_options was refused by the curl_cffi AsyncSession: {exc}"
            ) from exc

    def _create_loop(self):
        if sys.platform == "win32":
            return asyncio.SelectorEventLoop()
        return super()._create_loop()

    async def _single_call(self, request: NetworkRequest) -> NetworkResponse:
        kwargs = _request_kwargs(request, proxies=self.proxies)
        started = time.perf_counter()
        raw = await self.session.request(
            str(request.method).upper(), request.url, **kwargs)
        response = self._response(raw, request.url, time.perf_counter() - started)
        if response.redirected:
            raise NetworkUnavailableError("curl_cffi followed a redirect despite allow_redirects=False")
        return response

    @staticmethod
    def _headers(raw):
        """Response headers with lower-case names and every Set-Cookie kept."""
        headers = getattr(raw, "headers", None)
        if headers is None:
            return {}
        collected: dict[str, list[str]] = {}
        multi = getattr(headers, "multi_items", None)
        if callable(multi):
            try:
                items = list(multi())
            except Exception:  # fall back to the plain view
                items = []
        else:
            items = []
        if not items:
            source = headers.items() if hasattr(headers, "items") else headers
            items = [(key, value) for key, value in source]
        for key, value in items:
            collected.setdefault(str(key).lower(), []).append(str(value))
        return {key: values if key == "set-cookie" else ", ".join(values)
                for key, values in collected.items()}

    @classmethod
    def _response(cls, raw, url, fallback_elapsed):
        elapsed = getattr(raw, "elapsed", None)
        total_seconds = getattr(elapsed, "total_seconds", None)
        body = getattr(raw, "content", None)
        return NetworkResponse(
            str(getattr(raw, "url", None) or url),
            int(getattr(raw, "status_code", 200)),
            str(getattr(raw, "reason", "") or ""),
            cls._headers(raw),
            getattr(raw, "text", "") if body is None else body,
            bool(getattr(raw, "history", ())),
            "basic",
            float(total_seconds()) if callable(total_seconds) else float(fallback_elapsed),
        )


__all__ = ["CurlCffiTransport"]
