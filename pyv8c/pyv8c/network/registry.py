"""Engine names, factories and option-key providers."""
import inspect
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, NewType, Protocol, runtime_checkable

from .cookies import CookieJar
from .models import NetworkRequest, NetworkResponse, NetworkUnavailableError, RequestHandle

KIND_METHODS = {"http": "submit_request", "ws": "websocket"}
KINDS = tuple(KIND_METHODS)

#: A name returned by :func:`register_transport`. Distinct from the built-in
#: library names ``curl_cffi`` and ``cronet``, which stay plain strings.
HttpEngineName = NewType("HttpEngineName", str)
#: A name returned by :func:`register_ws_transport`.
WebSocketEngineName = NewType("WebSocketEngineName", str)


@runtime_checkable
class HttpTransport(Protocol):
    """The HTTP surface a ready engine must expose."""

    cookies: CookieJar

    def submit_request(
        self, request: NetworkRequest, handle: RequestHandle, remaining: Callable[[], float | None],
    ) -> Future[NetworkResponse]:
        """Queue I/O without waiting and return its transport-owned completion.

        An inspectable engine returns the first wire response to a manual hop.
        Register interruption with the handle before starting I/O. Check
        ``remaining()`` before sending and enforce that deadline while running.
        Use ``pyv8c.RequestTimeout`` for expiry of this deadline. Settle the
        Future only after cleanup, including when interrupted.
        """

    def close(self) -> None:
        """Synchronously release the engine's resources and return None."""


@runtime_checkable
class WebSocketTransport(Protocol):
    """What the page's WebSocket API needs from a socket engine."""

    #: The context's cookie jar, so a handshake may carry a request's cookies.
    cookies: CookieJar

    def websocket(self, tasks, url, protocols, callbacks, options=None):
        """Open a WebSocket, delivering its events on the realm's task queue."""

    def close_websocket(self, websocket) -> None:
        """Close one WebSocket this engine opened."""

    def close(self) -> None:
        """Release every resource this engine owns."""


def require_http_close(engine):
    close = getattr(engine, "close", None)
    implementation = getattr(close, "__call__", None)
    if not callable(close) or any(
        inspect.iscoroutinefunction(method) or inspect.isasyncgenfunction(method)
        for method in (close, implementation)
    ):
        raise TypeError(f"HTTP engine {type(engine).__name__} must implement synchronous close()")
    return close


def close_http(engine):
    result = require_http_close(engine)()
    if inspect.iscoroutine(result):
        result.close()
    if result is not None:
        raise TypeError(f"HTTP engine {type(engine).__name__} close() must return None after cleanup")


def _typed_dict_of(annotation):
    """The TypedDict class *annotation* stands for, if it stands for one."""
    for candidate in (annotation, getattr(annotation, "__origin__", None)):
        if isinstance(candidate, type) and (
            hasattr(candidate, "__required_keys__") or hasattr(candidate, "__optional_keys__")
        ):
            return candidate
    return None


def _typed_dict_keys(annotation) -> frozenset[str] | None:
    """The keys of the TypedDict a client annotates its ``**kwargs`` with."""
    arguments = getattr(annotation, "__args__", ())
    if len(arguments) == 1 and _typed_dict_of(annotation) is None:
        # ``Unpack[SomeParams]`` is how a TypedDict reaches a ``**kwargs``.
        annotation = arguments[0]
    declared = _typed_dict_of(annotation)
    if declared is None:
        return None
    return frozenset(getattr(declared, "__annotations__", ())) or None


def signature_option_keys(target) -> frozenset[str] | None:
    """The keyword names *target* accepts, or ``None`` when open-ended."""
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return None
    names: set[str] = set()
    for name, parameter in parameters.items():
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.VAR_POSITIONAL):
            continue
        if parameter.kind is parameter.VAR_KEYWORD:
            keys = _typed_dict_keys(parameter.annotation)
            if keys is None:
                return None
            names |= keys
            continue
        names.add(name)
    return frozenset(names)


_FACTORIES: dict[str, dict[str, Callable[..., Any]]] = {kind: {} for kind in KINDS}
_OPTION_KEYS: dict[str, dict[str, Callable[[], frozenset[str] | None]]] = {kind: {} for kind in KINDS}
_ACCEPTS_PROXY: dict[str, dict[str, bool]] = {kind: {} for kind in KINDS}


def _kind(value: str) -> str:
    """The engine kind a caller named, checked against the two that exist."""
    kind = str(value).strip().lower()
    if kind not in _FACTORIES:
        raise ValueError(f"unknown engine kind {kind!r}; expected one of: {', '.join(KINDS)}")
    return kind


def register_transport(name: str, factory: Callable[..., Any], *,
                       option_keys: Callable[[], frozenset[str] | None] | None = None,
                       proxy: bool = False) -> HttpEngineName:
    """Make *factory* available as an HTTP engine name.

    The return value is what ``Browser(http_engine=...)`` accepts for this
    registration. A plain string in that parameter is only ``curl_cffi`` or
    ``cronet``. *option_keys* is lazy and ``None`` leaves the engine
    unvalidated; interruption is registered by the engine for each submission.
    """
    return HttpEngineName(_register("http", name, factory, option_keys, proxy))


def register_ws_transport(name: str, factory: Callable[..., Any], *,
                          option_keys: Callable[[], frozenset[str] | None] | None = None,
                          proxy: bool = False) -> WebSocketEngineName:
    """Make *factory* available as a WebSocket engine name; see :func:`register_transport`.

    The return value is what ``Browser(ws_engine=...)`` accepts for this
    registration. A plain string in that parameter is only ``websockets``.
    """
    return WebSocketEngineName(_register("ws", name, factory, option_keys, proxy))


def _register(kind: str, name: str, factory: Callable[..., Any],
              option_keys: Callable[[], frozenset[str] | None] | None,
              proxy: bool = False) -> str:
    key = str(name).strip()
    if not key:
        raise ValueError("a transport name must not be empty")
    if not callable(factory):
        raise TypeError("a transport factory must be callable")
    if option_keys is not None and not callable(option_keys):
        raise TypeError("a transport option-keys provider must be callable")
    _FACTORIES[kind][key] = factory
    if option_keys is None:
        _OPTION_KEYS[kind].pop(key, None)
    else:
        _OPTION_KEYS[kind][key] = option_keys
    _ACCEPTS_PROXY[kind][key] = bool(proxy)
    return key


def transport_names(kind: str = "http") -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES[_kind(kind)]))


def accepted_option_keys(name: str, kind: str = "http") -> frozenset[str] | None:
    """The accepted option keys, or ``None`` when the engine declares none."""
    provider = _OPTION_KEYS[_kind(kind)].get(str(name).strip())
    return None if provider is None else provider()


def accepts_proxy(name: str, kind: str = "http") -> bool:
    """Whether the *kind* engine registered as *name* honours a proxy."""
    return _ACCEPTS_PROXY[_kind(kind)].get(str(name).strip(), False)


def create_transport(name: str, *, kind: str = "http", options: dict | None = None,
                     strict: bool = False):
    """Build the *kind* engine registered as *name*."""
    which = _kind(kind)
    key = str(name).strip()
    factory = _FACTORIES[which].get(key)
    if factory is None:
        known = ", ".join(transport_names(which)) or "none"
        raise NetworkUnavailableError(
            f"unknown {which} engine {key!r}; registered engines: {known}"
        )
    if strict and options:
        accepted = accepted_option_keys(key, which)
        if accepted is not None:
            rejected = sorted(set(options) - accepted)
            if rejected:
                names = ", ".join(repr(item) for item in rejected)
                noun = "option" if len(rejected) == 1 else "options"
                listed = ", ".join(sorted(accepted)) or "none"
                raise TypeError(
                    f"unknown {noun} {names} for {which} engine {key!r}; accepted: {listed}"
                )
    return factory(options=options)


def _cronet(*, options=None):
    from .cronet import CronetTransport

    return CronetTransport(options=options)


def _curl_cffi(*, options=None):
    from .curl_cffi import CurlCffiTransport

    return CurlCffiTransport(options=options)


def _websockets(*, options=None):
    from .websockets import WebSocketsTransport

    return WebSocketsTransport(options=options)


def _cronet_keys():
    from .cronet import CronetTransport

    return CronetTransport.accepted_option_keys()


def _curl_cffi_keys():
    from .curl_cffi import CurlCffiTransport

    return CurlCffiTransport.accepted_option_keys()


def _websockets_keys():
    from .websockets import WebSocketsTransport

    return WebSocketsTransport.accepted_option_keys()


register_transport("curl_cffi", _curl_cffi, option_keys=_curl_cffi_keys, proxy=True)
# cycronet names its proxy option ``proxies``.
register_transport("cronet", _cronet, option_keys=_cronet_keys, proxy=True)
register_ws_transport("websockets", _websockets, option_keys=_websockets_keys, proxy=True)


__all__ = [
    "KINDS",
    "HttpEngineName",
    "HttpTransport",
    "WebSocketEngineName",
    "WebSocketTransport",
    "accepted_option_keys",
    "accepts_proxy",
    "create_transport",
    "register_transport",
    "register_ws_transport",
    "signature_option_keys",
    "transport_names",
]
