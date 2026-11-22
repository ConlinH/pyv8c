"""The default headers Chromium puts on a request, in the order it sends them.

Measured against Chrome 153 (``docs/pyv8c/request-headers.md``); ``Host`` and
``Content-Length`` belong to the transport.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from ...foundation.urls import origin_string
from ...foundation.sites import site_domain

#: What Chrome asks for, per request destination (``blink/common/loader/network_utils.cc``).
NAVIGATION_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
    "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
ACCEPT_BY_DESTINATION = {
    "document": NAVIGATION_ACCEPT,
    "iframe": NAVIGATION_ACCEPT,
    "style": "text/css,*/*;q=0.1",
    "image": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "script": "*/*",
    "audio": "*/*",
    "video": "*/*",
    "empty": "*/*",
}
DEFAULT_ACCEPT = "*/*"

#: ``zstd`` needs a transport build that can decode it; curl_cffi ships one.
ACCEPT_ENCODING = "gzip, deflate, br, zstd"

#: A media element takes its bytes uncompressed and by range, so a player can seek.
MEDIA_ACCEPT_ENCODING = "identity;q=1, *;q=0"
MEDIA_RANGE = "bytes=0-"
MEDIA_DESTINATIONS = frozenset({"audio", "video"})

NAVIGATION_DESTINATIONS = frozenset({"document", "iframe"})

#: The three orders Chrome uses; each differs in more than the headers it carries.
_NAVIGATION_ORDER = (
    "Connection",
    "Cache-Control",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "Upgrade-Insecure-Requests",
    "Content-Type",
    "User-Agent",
    "Origin",
    "Accept",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-User",
    "Sec-Fetch-Dest",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
)
_RESOURCE_ORDER = (
    "Connection",
    "sec-ch-ua-platform",
    "User-Agent",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "Accept",
    "Origin",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Dest",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
)
_MEDIA_ORDER = (
    "Connection",
    "sec-ch-ua-platform",
    "Accept-Encoding",
    "User-Agent",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "Accept",
    "Origin",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Dest",
    "Referer",
    "Accept-Language",
    "Range",
)

#: The name a caller's own headers follow, per family.
_SPLICE_AFTER = {"navigation": "User-Agent", "resource": "sec-ch-ua", "media": "sec-ch-ua"}

_SAFE_METHODS = ("GET", "HEAD")


def accept_language(languages) -> str:
    """Chrome's ``Accept-Language``: first entry implicit q=1.0, then 0.9/0.8… ."""
    tokens = [str(entry) for entry in (languages or ()) if str(entry)]
    if not tokens:
        return ""
    quality = 9
    parts = [tokens[0]]
    for token in tokens[1:]:
        parts.append(f"{token};q=0.{quality}")
        quality = max(quality - 1, 1)
    return ",".join(parts)


def client_hint_headers(client_hints, accepted=()) -> dict:
    """The three low-entropy hints Chrome sends by default, plus the ones a server asked for.

    Chrome adds a high-entropy hint to later requests to an origin only after that
    origin names it in ``Accept-CH``; ``accepted`` carries the names seen so far.
    """
    headers = {
        "sec-ch-ua": ", ".join(
            f'"{brand.brand}";v="{brand.version}"' for brand in client_hints.brands
        ),
        "sec-ch-ua-mobile": "?1" if client_hints.mobile else "?0",
        "sec-ch-ua-platform": f'"{client_hints.platform}"',
    }
    wanted = {str(name).lower() for name in accepted}
    full = getattr(client_hints, "full_version_list", None) or ()
    if "sec-ch-ua-full-version-list" in wanted and full:
        headers["sec-ch-ua-full-version-list"] = ", ".join(
            f'"{brand.brand}";v="{brand.version}"' for brand in full
        )
    for name, attribute in (("sec-ch-ua-arch", "architecture"),
                            ("sec-ch-ua-bitness", "bitness"),
                            ("sec-ch-ua-model", "model"),
                            ("sec-ch-ua-platform-version", "platform_version")):
        value = getattr(client_hints, attribute, None)
        if name in wanted and value is not None:
            headers[name] = f'"{value}"'
    if "sec-ch-ua-wow64" in wanted:
        headers["sec-ch-ua-wow64"] = "?1" if getattr(client_hints, "wow64", False) else "?0"
    return headers


def request_destination(options):
    if explicit := options.get("destination"):
        return str(explicit)
    resource_type = str(options.get("resource_type") or "")
    if resource_type in ("document", "iframe", "script", "style", "image"):
        return resource_type
    return "video" if resource_type == "media" else "empty"


def resource_mode(destination, options):
    if destination in NAVIGATION_DESTINATIONS:
        return "navigate"
    stated = options.get("mode")
    if stated is not None:
        return str(stated)
    return "no-cors" if destination in ("script", "style", "image", "audio", "video") else "cors"


def fetch_mode(destination, request_mode=None) -> str:
    """``Sec-Fetch-Mode``: a navigation navigates, CORS fetches fetch, the rest no-cors."""
    if destination in NAVIGATION_DESTINATIONS:
        return "navigate"
    if request_mode is not None:
        return str(request_mode)
    return "cors" if destination == "empty" else "no-cors"


def same_site(left, right) -> bool:
    """Whether two URLs share a scheme and a registrable domain.

    Registrable domains use the installed Public Suffix List snapshot, including
    private suffixes. The snapshot may differ from a particular Chrome build.
    """
    left_parts, right_parts = _parts(left), _parts(right)
    if not left_parts or not right_parts:
        return False
    left_scheme, left_host = left_parts
    right_scheme, right_host = right_parts
    if left_scheme != right_scheme:
        return False
    left_site, right_site = site_domain(left_host), site_domain(right_host)
    return bool(left_site and right_site and left_site == right_site)


def site_relation(url, current_url) -> str:
    """``same-origin``, ``same-site`` or ``cross-site`` between two URLs."""
    if not url or not current_url:
        return "cross-site"
    if origin_string(url) == origin_string(current_url):
        return "same-origin"
    return "same-site" if same_site(url, current_url) else "cross-site"


REFERRER_POLICIES = frozenset(('no-referrer', 'no-referrer-when-downgrade', 'same-origin',
    'origin', 'strict-origin', 'origin-when-cross-origin', 'strict-origin-when-cross-origin', 'unsafe-url'))


def referrer_policy(value, default='strict-origin-when-cross-origin'):
    result = default
    for token in str(value or '').split(','):
        candidate = token.strip().lower()
        if candidate in REFERRER_POLICIES:
            result = candidate
    return result


def referer_for(url, current_url, policy='strict-origin-when-cross-origin') -> str:
    """``Referer`` under ``strict-origin-when-cross-origin``: full URL, else origin.

    Only an http(s) URL can be a referrer: a worker started from a ``blob:`` script reports that
    blob as its URL, and a blob is never put on the wire as a referrer -- Chrome sends no
    ``Referer`` at all for such a request (measured against Chrome 154, 2026-10-07, on the
    challenge's own worker fetch of ``/pat/…``; the engine used to send the owner page's URL).
    """
    if not url or not current_url:
        return ""
    source = urlsplit(str(current_url))
    if source.scheme.lower() not in ("http", "https"):
        return ""
    policy = referrer_policy(policy)
    same = origin_string(url) == origin_string(current_url)
    downgrade = source.scheme.lower() == 'https' and urlsplit(str(url)).scheme.lower() != 'https'
    if policy == 'no-referrer' or (policy == 'same-origin' and not same):
        return ''
    if downgrade and policy in ('no-referrer-when-downgrade', 'strict-origin', 'strict-origin-when-cross-origin'):
        return ''
    full = urlunsplit((source.scheme, source.netloc.rsplit('@', 1)[-1], source.path or '/', source.query, ''))
    if policy in ('unsafe-url', 'no-referrer-when-downgrade') or (same and policy not in ('origin', 'strict-origin')):
        return full
    origin = origin_string(current_url)
    return "" if origin == "null" else origin + "/"


def include_origin(url, current_url, mode, method, current_origin=None) -> bool:
    """Whether an ``Origin`` header belongs here: an unsafe method, or CORS.

    Measured: a same-origin GET fetch carries none, a same-origin POST does, and
    so does a cross-origin GET or form POST.
    """
    if not current_url:
        return False
    if str(method).upper() not in _SAFE_METHODS:
        return True
    return mode == "cors" and origin_string(url) != (current_origin if current_origin is not None else origin_string(current_url))


def browser_headers(environment, *, destination="empty", mode=None, method="GET",
                    url="", current_url="", browser_initiated=False,
                    user_activated=False, content_type=None, http_version=1,
                    extra=None, current_origin=None, accepted_hints=(), referrer_policy='strict-origin-when-cross-origin') -> dict:
    """The headers Chromium would send for one request, in Chromium's order.

    ``browser_initiated`` marks a browser-started navigation, which has no
    initiator: no ``Referer``, ``Sec-Fetch-Site: none`` and ``Sec-Fetch-User``,
    whose other source is the user gesture ``user_activated`` records.
    ``http_version`` decides ``Connection``, which only HTTP/1 carries.
    ``extra`` replaces defaults and is spliced where Chrome puts a script-set
    header.
    """
    hints = client_hint_headers(environment.client_hints, accepted_hints)
    referer = "" if browser_initiated else referer_for(url, current_url, referrer_policy)
    origin = origin_string(current_url) if current_origin is None else current_origin
    site = "none" if browser_initiated else site_relation(url, origin if current_origin is not None else current_url)
    resolved_mode = mode or fetch_mode(destination)
    media = destination in MEDIA_DESTINATIONS
    navigation = destination in NAVIGATION_DESTINATIONS
    unsafe = str(method).upper() not in _SAFE_METHODS
    values = {
        "Accept": ACCEPT_BY_DESTINATION.get(destination, DEFAULT_ACCEPT),
        "Accept-Encoding": MEDIA_ACCEPT_ENCODING if media else ACCEPT_ENCODING,
        "Accept-Language": accept_language(environment.languages),
        "User-Agent": environment.user_agent,
        "Sec-Fetch-Dest": destination,
        "Sec-Fetch-Mode": resolved_mode,
        "Sec-Fetch-Site": site,
        "Referer": referer,
        "Content-Type": content_type or "",
        "Range": MEDIA_RANGE if media else "",
        "Cache-Control": "max-age=0" if navigation and unsafe else "",
        "Upgrade-Insecure-Requests": "1" if navigation else "",
        "Sec-Fetch-User": ("?1" if resolved_mode == "navigate"
                           and (browser_initiated or user_activated) else ""),
        "Origin": (origin
                   if include_origin(url, current_url, resolved_mode, method, current_origin) else ""),
        "Connection": "keep-alive" if int(http_version or 1) < 2 else "",
        **hints,
    }

    extra = {str(name): "" if value is None else str(value) for name, value in (extra or {}).items()}
    provided = {name.lower() for name in extra}
    values = {name: value for name, value in values.items() if name.lower() not in provided}

    if destination in NAVIGATION_DESTINATIONS:
        family = "navigation"
    else:
        family = "media" if media else "resource"
    order = {"navigation": _NAVIGATION_ORDER, "media": _MEDIA_ORDER}.get(family, _RESOURCE_ORDER)
    ordered: dict[str, str] = {}
    for name in order:
        if values.get(name):
            ordered[name] = values[name]
        if name == _SPLICE_AFTER[family]:
            ordered.update(extra)
    for name, value in list(extra.items()) + list(values.items()):
        if name not in ordered and value:
            ordered[name] = value
    return ordered


def _parts(url):
    """``(scheme, host)`` of a URL, or ``None`` when it cannot be compared."""
    try:
        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower()
        return (parts.scheme.lower(), host) if host else None
    except ValueError:
        return None


__all__ = [
    "ACCEPT_BY_DESTINATION", "ACCEPT_ENCODING", "DEFAULT_ACCEPT",
    "MEDIA_ACCEPT_ENCODING", "MEDIA_DESTINATIONS", "MEDIA_RANGE",
    "NAVIGATION_ACCEPT", "NAVIGATION_DESTINATIONS",
    "accept_language", "browser_headers", "client_hint_headers", "fetch_mode",
    "include_origin", "referer_for", "same_site", "site_relation",
]
