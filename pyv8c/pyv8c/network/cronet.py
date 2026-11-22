"""Cancellable cycronet bridge on the shared async delivery."""
import importlib
import inspect

from .async_delivery import AsyncTransport
from .models import (
    NetworkRequest,
    NetworkResponse,
    NetworkUnavailableError,
)
from .proxy import PROXY_OPTION
from .registry import signature_option_keys


def _cycronet_module():
    """The optional cycronet package, or an error naming what is missing."""
    try:
        return importlib.import_module("cycronet")
    except ImportError as exc:
        raise NetworkUnavailableError("the 'cronet' engine requires the optional 'cycronet' package") from exc


def _cycronet_client_type(module):
    client_type = getattr(module, "AsyncCronetClient", None)
    if client_type is None:
        raise NetworkUnavailableError("installed cycronet does not expose AsyncCronetClient")
    return client_type


class CronetTransport(AsyncTransport):
    """Shared HTTP transport with independent capacity for priority requests."""

    # Cronet owns its connection queue; allow a bounded window of work ahead
    # so each finished connection can pick up a request without an owner turn.
    http_concurrency = 22

    @classmethod
    def accepted_option_keys(cls) -> frozenset[str] | None:
        client_keys = signature_option_keys(_cycronet_client_type(_cycronet_module()))
        if client_keys is None:
            return None
        return client_keys | {PROXY_OPTION}

    def __init__(self, *, options=None):
        options = dict(options or {})
        proxy = options.pop(PROXY_OPTION, None)
        if proxy and "proxies" not in options:
            options["proxies"] = proxy
        self._priority_session = None
        super().__init__(options=options, loop_name="pyv8c-cronet-loop")

    def _open_session(self):
        module = _cycronet_module()
        self.cycronet = module
        self.session = self._new_session()

    def _new_session(self):
        client_type = _cycronet_client_type(self.cycronet)
        try:
            return client_type(**self.options)
        except TypeError as exc:
            raise TypeError(
                f"http_options was refused by cycronet AsyncCronetClient: {exc}"
            ) from exc

    def _map_error(self, request, exc):
        return None

    @staticmethod
    def _response(raw, url):
        headers = getattr(raw, "headers", {}) or {}
        if hasattr(headers, "items"):
            headers = {
                str(k).lower(): ([str(item) for item in v] if isinstance(v, (list, tuple)) else str(v))
                for k, v in headers.items()
            }
        else:
            headers = {}
        raw_headers = getattr(raw, '_headers', None)
        if hasattr(raw_headers, 'items'):
            for name, values in raw_headers.items():
                if str(name).lower() == 'set-cookie' and isinstance(values, (list, tuple)):
                    headers['set-cookie'] = [str(value) for value in values]
        body = getattr(raw, "content", None)
        if body is None:
            body = getattr(raw, "text", "")
        if callable(body):
            body = body()
        return NetworkResponse(
            str(getattr(raw, "url", None) or url),
            int(getattr(raw, "status_code", getattr(raw, "status", 200))),
            str(getattr(raw, "reason", "")), headers, body,
            bool(getattr(raw, "history", ())),
        )

    async def _single_call(self, request: NetworkRequest) -> NetworkResponse:
        session = self.session
        if request.mode == "navigate" or request.blocking_io:
            # Cronet also caps connections per origin inside each session.
            if self._priority_session is None:
                self._priority_session = self._new_session()
            session = self._priority_session
        headers = dict(request.headers)
        if "cookie" not in {str(name).lower() for name in headers}:
            # An empty header suppresses the client's own session jar.
            headers["Cookie"] = ""
        kwargs = {"headers": headers, "allow_redirects": False}
        if request.body is not None:
            kwargs["data"] = request.body
        if request.timeout is not None:
            try:
                parameters = inspect.signature(session.request).parameters.values()
                accepts_timeout = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == "timeout"
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                accepts_timeout = True
            if accepts_timeout:
                kwargs["timeout"] = request.timeout
        raw = await session.request(str(request.method).upper(), request.url, **kwargs)
        response = self._response(raw, request.url)
        if response.redirected:
            raise NetworkUnavailableError('cycronet followed a redirect despite allow_redirects=False')
        return response

    async def _close_session(self):
        if self._priority_session is not None:
            await self._priority_session.close()
            self._priority_session = None
        await super()._close_session()


__all__ = ["CronetTransport"]
