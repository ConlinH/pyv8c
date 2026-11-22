"""Assembling one context's engines from the options the Browser was built with."""
from __future__ import annotations

from .backend import NetworkBackend
from .proxy import PROXY_OPTION, RAW_OPTION_KEYS
from .registry import KINDS, KIND_METHODS, accepts_proxy, close_http, create_transport, require_http_close


def _options(browser, role):
    return dict(getattr(browser, f"{role}_options")) or None


def _configured_raw_proxy(options):
    return sorted(key for key in RAW_OPTION_KEYS if key in (options or {}))


def _with_proxy(role, name, options, proxy):
    """Merge the proxy in; ``""`` pins a direct connection."""
    configured = _configured_raw_proxy(options)
    if configured and proxy is not None:
        listed = ", ".join(repr(key) for key in configured)
        raise TypeError(
            f"proxy= and {role}_options[{listed}] were both given for {role} engine "
            f"{name!r}; keep one -- the other would be overwritten"
        )
    if not accepts_proxy(name, role):
        if proxy is not None:
            raise TypeError(
                f"{role} engine {name!r} cannot be sent through a proxy; configure it "
                f"with {role}_options instead, or name an engine that declares one"
            )
        return options
    if configured:
        # The engine's own proxy keys are its business; it knows how to honor them.
        return options
    return {**(options or {}), PROXY_OPTION: "" if proxy is None else proxy}


def _serves(candidate, role):
    return callable(getattr(candidate, KIND_METHODS[role], None))


def _engine(browser, role, value, proxy):
    """One role's engine: a name, a ready object, or a factory."""
    if value is None:
        return None
    if isinstance(value, str):
        options = _with_proxy(role, value, _options(browser, role), proxy)
        return create_transport(value, kind=role, options=options or None, strict=True)
    if proxy is not None:
        raise TypeError(
            f"proxy cannot configure a ready {role}_engine; name a registered engine "
            "so the setting has somewhere to go"
        )
    if getattr(browser, f"{role}_options"):
        raise TypeError(
            f"{role}_options cannot configure a ready {role}_engine; name a "
            "registered engine to pass options to it"
        )
    factory = isinstance(value, type) or (callable(value) and not _serves(value, role))
    return value() if factory else value


def _refuse_unconsumed_options(browser):
    """Reject options no engine would read, instead of dropping them quietly."""
    given = [role for role in KINDS if getattr(browser, f"{role}_options")]
    if given:
        listed = " and ".join(f"{role}_options" for role in given)
        raise TypeError(
            f"{listed} was given but no network engine is configured; "
            "set real_network=True, http_engine or ws_engine"
        )


def _refuse_ready_engines(browser):
    """A name is only a preference; a ready engine would be dropped, so it is refused."""
    for role in KINDS:
        value = getattr(browser, f"{role}_engine")
        if value is not None and not isinstance(value, str):
            raise TypeError(
                f"real_network=False assembles no engines, so {role}_engine has "
                "nothing to serve; set real_network=True to use it"
            )


def _refuse_shared_options(browser, first_role, role, value):
    """One engine cannot be configured twice; say which options to move."""
    if getattr(browser, f"{role}_options"):
        raise TypeError(
            f"{first_role}_engine and {role}_engine both give {value!r}, which is built once, "
            f"so {role}_options would be ignored; give that role its own engine "
            f"or move those settings into {first_role}_options"
        )


def _refuse_proxy_without_engine(proxy):
    """A configured proxy with nothing to carry it is a mistake, not a no-op."""
    if proxy is not None:
        raise TypeError(
            "proxy was given but no network engine is configured; "
            "set real_network=True, http_engine or ws_engine"
        )


def assemble_backend(browser, proxy=None):
    """Assemble one context's engines from what the Browser was built with."""
    if not browser.real_network:
        _refuse_ready_engines(browser)
        _refuse_proxy_without_engine(proxy)
        _refuse_unconsumed_options(browser)
        return None

    engines = {}
    configured = {}
    created = []
    try:
        for role in KINDS:
            value = getattr(browser, f"{role}_engine")
            if value is None:
                if getattr(browser, f"{role}_options"):
                    raise TypeError(f"{role}_options was given but no {role}_engine is configured")
                engines[role] = None
                continue
            shared = next((first for first, previous in configured.items()
                           if value is previous or
                           (isinstance(value, str) and value == previous)), None)
            if shared is not None:
                _refuse_shared_options(browser, shared, role, value)
                engine = engines[shared]
                if not _serves(engine, role):
                    raise TypeError(f"shared {role}_engine must implement {KIND_METHODS[role]}")
                engines[role] = engine
            else:
                engine = _engine(browser, role, value, proxy)
                if isinstance(value, (str, type)) or (callable(value) and not _serves(value, role)):
                    created.append(engine)
                if not _serves(engine, role):
                    raise TypeError(
                        f"{role}_engine must be a registered engine name, an object with "
                        f"{KIND_METHODS[role]}, or a factory for one"
                    )
                if role == "http":
                    require_http_close(engine)
                engines[role] = engine
                configured[role] = value
    except BaseException:
        for engine in reversed(created):
            try:
                close_http(engine)
            except Exception:
                pass
        raise

    if all(engine is None for engine in engines.values()):
        _refuse_proxy_without_engine(proxy)
        _refuse_unconsumed_options(browser)
        return None
    return NetworkBackend(**engines)
