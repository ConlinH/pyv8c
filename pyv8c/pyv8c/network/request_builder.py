"""The one place a transport-level request is finalised."""
from __future__ import annotations

from time import perf_counter

from ..foundation.host_errors import RequestTimeout
from .models import NetworkRequest, with_cookie_header


def request_clock(timeout):
    """One deadline for queueing, preflight and all redirect hops."""
    deadline = None if timeout is None else perf_counter() + max(0, float(timeout))

    def remaining():
        if deadline is None:
            return None
        budget = deadline - perf_counter()
        if budget <= 0:
            raise RequestTimeout("resource request exceeded its deadline")
        return budget

    return remaining


def finalize_request(*, url, method="GET", headers=None, body=None, timeout=None,
                     credentials="same-origin", mode="cors", redirect="follow",
                     origin=None, site_for_cookies=None, top_level_navigation=False,
                     options=None, allow_redirects=None, jar=None, cookie_override=None):
    """Build one immutable :class:`NetworkRequest` for a transport.

    Redirect policy is removed from transport-specific options.
    """
    transport_options = dict(options or {})
    option_redirects = transport_options.pop("allow_redirects", None)
    if allow_redirects is None:
        allow_redirects = option_redirects
    request = NetworkRequest(
        url=url,
        method=str(method).upper(),
        headers=dict(headers or {}),
        body=body,
        timeout=timeout,
        credentials=credentials,
        mode=mode,
        redirect=redirect,
        allow_redirects=allow_redirects,
        origin=origin,
        site_for_cookies=site_for_cookies,
        top_level_navigation=top_level_navigation,
        options=transport_options,
        cookie_override=tuple((str(name), str(value)) for name, value in (cookie_override or {}).items()),
    )
    return with_cookie_header(request, jar)
