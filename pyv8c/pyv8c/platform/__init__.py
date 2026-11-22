"""Browser, profile, and page orchestration."""

from importlib import import_module

__all__ = ["Browser", "Profile", "Page",
           "DevToolsHandle", "DevToolsOptions"]

# DevTools is re-exported, not imported: pyv8's DevTools service brings a
# websocket server and asyncio with it, which a browser that never debugs should
# not pay for at import.
_LAZY_EXPORTS = {
    "Browser": ".browser", "Profile": ".browser", "Page": ".page",
    "DevToolsHandle": ".devtools", "DevToolsOptions": ".devtools",
}


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
