"""Transport-neutral request and response values shared by every backend.

A backend owns how bytes travel, never what they mean, so a context can swap
cycronet for curl_cffi (or for a host-supplied backend) without the resource
loader noticing.  ``request.options`` carries settings only some transports
understand; a backend ignores the ones it has no equivalent for.
"""
import json as json_module
from dataclasses import dataclass, field
from threading import RLock

from ..foundation.urls import same_origin
from .cookies import split_set_cookie


class NetworkUnavailableError(RuntimeError):
    """Raised when a transport is asked for and its dependency is missing."""


class RequestHandle:
    """Thread-safe cancellation handle for a network future.

    The lock covers the future swap: a completion landing between the read of
    :attr:`cancelled` and the swap must not cancel the wrong work.
    """

    def __init__(self):
        self._cancelled = False
        self._lock = RLock()
        self.future = None

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    def cancel(self):
        with self._lock:
            self._cancelled = True
            future = self.future
        if future is not None:
            future.cancel()


@dataclass
class NetworkRequest:
    """One transport-level request.

    Resource loading constructs these by keyword, independently of field order.
    """

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: object = None
    timeout: float | None = None
    credentials: str = "same-origin"
    mode: str = "cors"
    #: ``"follow"`` lets the engine run the chain; any other mode asks for one hop,
    #: because the caller inspects the hops itself.
    redirect: str = "follow"
    origin: str | None = None
    #: The schemeful site of the initiating document and its ancestors.
    site_for_cookies: str | None = None
    top_level_navigation: bool = False
    #: Transport extensions (impersonate, proxies, verify, ...); unknown keys are ignored.
    options: dict[str, object] = field(default_factory=dict)


def permits_credentials(request):
    return (request.credentials == 'include' or
            (request.credentials == 'same-origin' and
             (not request.origin or same_origin(request.origin, request.url))))


def cookie_header(jar, request) -> str:
    """The ``Cookie`` header *request* may carry from *jar*, if any.

    The rule every engine applies: ``"omit"`` sends none, ``"include"`` anywhere,
    ``"same-origin"`` (the default) only where the origins match.  A host-level
    request has no origin, so nothing about it can be cross-origin.
    """
    if jar is None or not permits_credentials(request):
        return ""
    site = request.site_for_cookies if request.site_for_cookies is not None else request.origin
    return jar.header_for(request.url, site_for_cookies=site,
                          top_level_navigation=request.top_level_navigation, method=request.method)


@dataclass
class NetworkResponse:
    """One transport-level response, with the conveniences callers share."""

    url: str
    status: int = 200
    status_text: str = "OK"
    headers: dict[str, str] = field(default_factory=dict)
    body: object = ""
    redirected: bool = False
    type: str = "basic"
    #: Wall-clock seconds spent, when the transport reports it.
    elapsed: float | None = None
    #: Whether an inspected redirect chain crossed an origin.
    redirect_crossed: bool = False

    @property
    def ok(self): return 200 <= int(self.status) < 300

    @property
    def content(self):
        body = self.body
        if isinstance(body, bytes):
            return body
        if body is None:
            return b""
        return str(body).encode("utf-8", "replace")

    @property
    def text(self):
        """The body decoded with the charset the response declares."""
        body = self.body
        if isinstance(body, str):
            return body
        return self.content.decode(self._charset(), "replace")

    def _charset(self):
        content_type = ""
        for name, value in (self.headers or {}).items():
            if str(name).lower() == "content-type":
                content_type = value if isinstance(value, str) else ", ".join(value)
                break
        for parameter in content_type.split(";")[1:]:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "charset":
                return value.strip().strip('"') or "utf-8"
        return "utf-8"

    def json(self):
        """The body parsed as JSON, raising the usual ``json`` errors."""
        return json_module.loads(self.text)

    @property
    def cookies(self):
        """The cookies the response set, as ``name -> value``.

        Split the way the jar splits its own, not with a naive ``split(",")``.
        """
        header = self.headers.get("set-cookie") if isinstance(self.headers, dict) else None
        if not header:
            return {}
        values = header if isinstance(header, (list, tuple)) else [header]
        parsed = {}
        for value in values:
            for cookie in split_set_cookie(value):
                pair = cookie.split(";", 1)[0].strip()
                name, separator, text = pair.partition("=")
                if separator and name.strip():
                    parsed[name.strip()] = text
        return parsed


__all__ = [
    "NetworkRequest",
    "NetworkResponse",
    "NetworkUnavailableError",
    "RequestHandle",
    "cookie_header",
]
