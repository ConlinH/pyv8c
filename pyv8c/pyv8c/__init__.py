"""Public runtime surface for pyv8c.

Applications choose ``pyv8c.sync_api`` or ``pyv8c.async_api`` explicitly.  Web interface
implementation classes are Feature internals: they stay in the package that owns
them, and the root exports only the errors an application
catches, and the network extension points.
"""

from __future__ import annotations

from importlib import import_module

from ._version import __version__

_EXPORTS = {
    "DevToolsHandle": ".platform",
    "DevToolsOptions": ".platform",
    "BrowserEnvironment": ".foundation.environment",
    # The engine roles, how to register one, and the values engines exchange.
    "HttpTransport": ".network.registry",
    "WebSocketTransport": ".network.registry",
    "register_transport": ".network.registry",
    "register_ws_transport": ".network.registry",
    "transport_names": ".network.registry",
    "NetworkBackend": ".network.backend",
    "NetworkRequest": ".network.models",
    "NetworkResponse": ".network.models",
    "NetworkUnavailableError": ".network.models",
    "HostFailure": ".foundation.host_errors",
    "RequestTimeout": ".foundation.host_errors",
}
for _name in (
    "PageError", "PageTypeError", "PageRangeError", "PageReferenceError", "PageSyntaxError", "PageURIError", "PageEvalError",
    "DOMException",
):
    _EXPORTS[_name] = ".web.page_errors"

__all__ = ("__version__", *tuple(_EXPORTS))


def __getattr__(name):
    try:
        module = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
