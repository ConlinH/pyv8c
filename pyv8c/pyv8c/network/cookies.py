"""Cookie storage and matching for page and network contexts.

One :class:`CookieJar` per context is the single store both sides share, making a
document and its requests one session; matching follows RFC 6265's host/path/secure
rules, with a wire view and a script-visible view.
"""
import re
import time
from dataclasses import dataclass, replace
from datetime import timezone
from email.utils import parsedate_to_datetime
from threading import RLock
from urllib.parse import urlsplit

from ..foundation.logging import logger
from ..foundation.sites import is_public_suffix, site_domain
from ..foundation.urls import canonical_host

_INVALID_COOKIE_NAME_CHARS = frozenset("()<>@,;:") | {"\\", "[", "]", "?", "=", "{", "}", " ", "\t"}

#: A comma starts a new joined cookie only when ``token=`` follows before any ``;`` or ``,``.
_SET_COOKIE_SEPARATOR = re.compile(r",(?=\s*[^;=,\s]+\s*=)")


def _canonical_host(host):
    try:
        return canonical_host(host or '')
    except UnicodeError:
        return ''


def split_set_cookie(header):
    text = str(header)
    if "," not in text:
        return [text]
    return _SET_COOKIE_SEPARATOR.split(text)


@dataclass
class Cookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    host_only: bool = True
    http_only: bool = False
    same_site: str = ""
    expires: float | None = None
    creation_index: int = 0


class CookieJar:
    """The context's single cookie store."""

    def __init__(self, *, lock=None, on_change=None):
        self._cookies: dict[tuple[str, str, str], Cookie] = {}
        self._lock = RLock() if lock is None else lock
        self._on_change = on_change
        self._creation_index = 0

    def snapshot(self, *, include_session=False):
        """Copy cookies with their original expiry and ordering."""
        with self._lock:
            now = time.time()
            return [replace(cookie) for cookie in self._cookies.values()
                    if not self._expired(cookie, now)
                    and (include_session or cookie.expires is not None)]

    def _restore(self, cookies):
        """Initialize the jar from validated records before opening the profile."""
        with self._lock:
            self._cookies = {(item.domain, item.path, item.name): replace(item) for item in cookies}
            self._creation_index = max(
                (item.creation_index for item in self._cookies.values()), default=0)

    def add_from_response(self, url, header):
        if not header: return
        if isinstance(header, (list, tuple)):
            for value in header: self.add_from_response(url, value)
            return
        parsed_url = urlsplit(url)
        for value in split_set_cookie(header):
            self._add_one_from_response(parsed_url, value)

    def _add_one_from_response(self, parsed_url, header):
        """Store one ``Set-Cookie`` value.

        The attributes are read here rather than handed to
        ``http.cookies.SimpleCookie``: that parser is written for a ``Cookie:``
        *request* header, and a ``Set-Cookie`` whose attribute shape it does not
        anticipate makes it stop early, dropping every cookie in the line without a
        word.  A Cloudflare challenge's ``cf_clearance`` was lost that way
        (``Expires=Wed, 03 Nov 2027 ...`` , measured 2026-11-03) while the header
        itself was well formed.  The grammar below is the one
        :meth:`set_from_document` already reads, except that ``HttpOnly`` is allowed
        here -- a script may not read it, the wire still sends it.  ``Partitioned``
        (CHIPS) is read and ignored rather than dropped: it narrows a cookie's
        visibility, it does not make the line unreadable.
        """
        pieces = str(header).split(";")
        pair = pieces[0]
        if "=" not in pair:
            return
        name, value = (part.strip() for part in pair.split("=", 1))
        if not name or any(char in _INVALID_COOKIE_NAME_CHARS for char in name):
            return
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        options = {
            "domain": _canonical_host(parsed_url.hostname),
            "path": self._default_path(parsed_url.path),
            "secure": False, "host_only": True, "http_only": False,
            "same_site": "", "expires": None, "max_age": None,
        }
        for attribute in pieces[1:]:
            key, separator, raw_value = attribute.strip().partition("=")
            key, raw_value = key.lower(), raw_value.strip()
            if key == "domain" and separator:
                domain = _canonical_host(raw_value.lstrip("."))
                if not domain:
                    return
                options["domain"], options["host_only"] = domain, False
            elif key == "path" and separator and raw_value.startswith("/"):
                options["path"] = raw_value
            elif key == "secure":
                options["secure"] = True
            elif key == "httponly":
                options["http_only"] = True
            elif key == "samesite" and separator:
                options["same_site"] = raw_value.lower()
            elif key == "expires" and separator:
                options["expires"] = self._parse_expires(raw_value)
            elif key == "max-age" and separator:
                options["max_age"] = self._parse_max_age(raw_value)
        self._store(parsed_url, name, value, **options, source="network")

    @staticmethod
    def _default_path(path):
        path = str(path or "/")
        if not path.startswith("/") or path.count("/") <= 1: return "/"
        return path[:path.rfind("/")] or "/"

    @staticmethod
    def _allows_cookies(parsed_url): return parsed_url.scheme in ("http", "https") and bool(parsed_url.hostname)

    @staticmethod
    def _parse_expires(value):
        if not value: return None
        try:
            parsed = parsedate_to_datetime(str(value))
            return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        except (TypeError, ValueError, OverflowError): return None

    @staticmethod
    def _parse_max_age(value):
        if value in (None, ""): return None
        try: return int(str(value).strip())
        except (TypeError, ValueError): return None

    @staticmethod
    def _domain_matches(host, domain, host_only):
        if not domain: return not host
        return host == domain if host_only else host == domain or host.endswith("." + domain)

    @staticmethod
    def _path_matches(request_path, cookie_path):
        return request_path == cookie_path or (request_path.startswith(cookie_path) and (cookie_path.endswith("/") or request_path[len(cookie_path):].startswith("/")))

    def _expired(self, cookie, now=None): return cookie.expires is not None and cookie.expires <= (time.time() if now is None else now)

    def _purge_expired(self):
        now = time.time()
        removed = False
        for key, cookie in list(self._cookies.items()):
            if self._expired(cookie, now):
                del self._cookies[key]
                removed = True
        if removed and self._on_change is not None:
            self._on_change()

    def _store(self, parsed_url, name, value, *, domain, path, secure, host_only,
               http_only=False, same_site="", expires=None, max_age=None, source="script"):
        if any(char in name or char in value for char in "\r\n\0"): return False
        if not self._allows_cookies(parsed_url): return False
        host = _canonical_host(parsed_url.hostname)
        if not host: return False
        if domain and not self._domain_matches(host, domain, False): return False
        if not host_only and is_public_suffix(domain):
            if domain != host: return False
            host_only = True
        if secure and parsed_url.scheme not in ("https", "wss") and host not in ('localhost', '127.0.0.1', '::1'): return False
        if same_site.lower() == "none" and not secure: return False
        if name.startswith("__Secure-") and not secure: return False
        if name.startswith("__Host-") and (not secure or not host_only or path != "/"): return False
        if max_age is not None: expires = time.time() + min(max_age, 400 * 86400) if max_age > 0 else 0
        key = (domain, path, name)
        with self._lock:
            self._purge_expired(); previous = self._cookies.get(key)
            if parsed_url.scheme != 'https' and not secure:
                for item in self._cookies.values():
                    if (item.secure and item.name == name
                            and (self._domain_matches(domain, item.domain, False)
                                 or self._domain_matches(item.domain, domain, False))
                            and self._path_matches(path, item.path)):
                        return False
            if source == "script" and previous is not None and previous.http_only: return False
            if expires is not None and expires <= time.time():
                changed = previous is not None; self._cookies.pop(key, None)
            else:
                self._creation_index += 1
                item = Cookie(name, value, domain, path, secure, host_only, http_only, same_site, expires, previous.creation_index if previous else self._creation_index)
                changed = previous != item; self._cookies[key] = item
            if changed and self._on_change is not None:
                self._on_change()
        if changed:
            logger.debug("cookie changed")
        return changed

    def set_from_document(self, url, assignment):
        parsed_url = urlsplit(str(url))
        if not self._allows_cookies(parsed_url): return False
        pair, *attributes = str(assignment).split(";")
        if "=" not in pair: return False
        name, value = (part.strip() for part in pair.split("=", 1))
        if not name or any(char in _INVALID_COOKIE_NAME_CHARS for char in name): return False
        options = {"domain": _canonical_host(parsed_url.hostname), "path": self._default_path(parsed_url.path), "secure": False, "host_only": True, "http_only": False, "same_site": "", "expires": None, "max_age": None}
        for attribute in attributes:
            key, separator, raw_value = attribute.strip().partition("="); key = key.lower(); raw_value = raw_value.strip()
            if key == "domain" and separator:
                domain = _canonical_host(raw_value.lstrip("."))
                if not domain or not self._domain_matches(_canonical_host(parsed_url.hostname), domain, False): return False
                options["domain"], options["host_only"] = domain, False
            elif key == "path" and separator and raw_value.startswith("/"): options["path"] = raw_value
            elif key == "secure": options["secure"] = True
            elif key == "httponly": return False
            elif key == "samesite" and separator: options["same_site"] = raw_value.lower()
            elif key == "expires" and separator: options["expires"] = self._parse_expires(raw_value)
            elif key == "max-age" and separator: options["max_age"] = self._parse_max_age(raw_value)
        return self._store(parsed_url, name, value, **options)

    def header_for(self, url, *, site_for_cookies=None, top_level_navigation=False, method='GET'):
        return self._for_url(url, include_http_only=True, site_for_cookies=site_for_cookies,
                             top_level_navigation=top_level_navigation, method=method)
    def document_for(self, url): return self._for_url(url, include_http_only=False)

    def _for_url(self, url, *, include_http_only, site_for_cookies=None, top_level_navigation=False, method='GET'):
        parsed = urlsplit(url)
        if not self._allows_cookies(parsed): return ""
        host, path, secure = _canonical_host(parsed.hostname), parsed.path or "/", parsed.scheme in ("https", "wss")
        same_site = True
        if site_for_cookies is not None:
            source = urlsplit(str(site_for_cookies))
            target_scheme = {'ws': 'http', 'wss': 'https'}.get(parsed.scheme, parsed.scheme)
            source_scheme = {'ws': 'http', 'wss': 'https'}.get(source.scheme, source.scheme)
            source_site, target_site = site_domain(source.hostname or ''), site_domain(host)
            same_site = bool(source.hostname and source_scheme == target_scheme
                             and source_site and target_site and source_site == target_site)
        safe_navigation = top_level_navigation and str(method).upper() in ('GET', 'HEAD', 'OPTIONS', 'TRACE')
        with self._lock:
            self._purge_expired()
            matched = [
                cookie for cookie in self._cookies.values()
                if (include_http_only or not cookie.http_only)
                and self._domain_matches(host, cookie.domain, cookie.host_only)
                and self._path_matches(path, cookie.path)
                and (not cookie.secure or secure)
                and (same_site or cookie.same_site == 'none'
                     or (cookie.same_site != 'strict' and safe_navigation))
            ]
            matched.sort(key=lambda cookie: (-len(cookie.path), cookie.creation_index))
            return "; ".join(f"{cookie.name}={cookie.value}" for cookie in matched)

    def clear(self):
        with self._lock:
            if self._cookies:
                self._cookies.clear()
                if self._on_change is not None:
                    self._on_change()


__all__ = ["Cookie", "CookieJar"]
