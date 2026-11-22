"""Transport-neutral request and response values."""
import json as json_module
from dataclasses import dataclass, field, replace
from threading import RLock

from ..foundation.urls import same_origin
from .cookies import split_set_cookie


class NetworkUnavailableError(RuntimeError):
    """Raised when a transport is asked for and its dependency is missing."""


class RequestHandle:
    """Thread-safe cancellation signal for one transport submission."""

    def __init__(self):
        self._cancelled = False
        self._lock = RLock()
        self._abort = None

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    def attach_abort(self, callback):
        """Register the interrupt, invoking it immediately after an early cancel."""
        with self._lock:
            if not self._cancelled:
                self._abort = callback
                return
        self._interrupt(callback)

    def detach_abort(self):
        """The transport has returned and its interrupt is no longer valid."""
        with self._lock:
            self._abort = None

    def cancel(self):
        """Interrupt transport once, leaving completion owned by the transport."""
        with self._lock:
            self._cancelled = True
            callback = self._abort
            self._abort = None
        return self._interrupt(callback)

    @staticmethod
    def _interrupt(callback):
        if callback is None:
            return False
        try:
            return callback() is not False
        except Exception:
            return False


@dataclass(frozen=True)
class NetworkRequest:
    """One transport-level request, finalised before a transport sees it."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: object = None
    timeout: float | None = None
    credentials: str = "same-origin"
    mode: str = "cors"
    redirect: str = "follow"
    allow_redirects: bool | None = None
    origin: str | None = None
    site_for_cookies: str | None = None
    top_level_navigation: bool = False
    options: dict[str, object] = field(default_factory=dict)
    generated_cookie: bool = False
    cookie_override: tuple[tuple[str, str], ...] = ()
    blocking_io: bool = False


def permits_credentials(request):
    return (request.credentials == 'include' or
            (request.credentials == 'same-origin' and
             (not request.origin or same_origin(request.origin, request.url))))


def cookie_header(jar, request) -> str:
    """The ``Cookie`` header *request* may carry from *jar*, if any."""
    if jar is None or not permits_credentials(request):
        return ""
    site = request.site_for_cookies if request.site_for_cookies is not None else request.origin
    return jar.header_for(request.url, site_for_cookies=site,
                          top_level_navigation=request.top_level_navigation, method=request.method)


def with_cookie_header(request, jar):
    """Return *request* with its ``Cookie`` header decided once, at build time.

    *override* pairs lead and replace same-named entries from the jar.
    """
    headers = dict(request.headers)
    if any(str(name).lower() == "cookie" for name in headers):
        return request
    value = cookie_header(jar, request)
    override = dict(request.cookie_override)
    if override:
        kept = [part for part in value.split("; ")
                if part and part.split("=", 1)[0] not in override]
        explicit = [f"{name}={item}" for name, item in override.items()]
        value = "; ".join(explicit + kept)
    if not value:
        return request
    headers["Cookie"] = value
    return replace(request, headers=headers, generated_cookie=True)


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
    elapsed: float | None = None
    redirect_crossed: bool = False
    redirect_hops: int = 0
    redirect_span: float = 0.0

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
        """The cookies the response set, as ``name -> value``."""
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
