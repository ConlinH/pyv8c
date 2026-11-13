"""``caches``: the Cache API, kept in memory and keyed by origin.

A page and its workers share the caches of their origin, the way Chrome's do. Nothing
here survives the process, so a page that reloads finds what it stored and one that
restarts does not -- which is the one difference a page could notice.

An entry is stored as plain data rather than as the ``Request`` and ``Response`` objects
it came from, because the realm that reads an entry back is usually not the one that
wrote it, and a value built in one isolate does not belong to another.
"""

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import MISSING, dictionary, missing_error
from ...foundation.arguments import IdlArgumentError
from ..page_errors import PageTypeError
from ..page_errors import js_error_value
from ...foundation.urls import origin_string
from .request import Request
from .response import Response, filtered_network_response


#: ``origin -> cache name -> entries``: the caches each origin has opened.
_STORES = {}


#: The methods a cache can hold: everything else refuses to be stored.
_STORABLE_METHODS = ("GET",)


def _store_for(services):
    """The caches of the realm's origin, which its workers reach as well."""
    url = str(getattr(getattr(services, "realm_services", None), "url", "") or "")
    return _STORES.setdefault(origin_string(url), {})


def _settle(services, value=None, error=None):
    """Settle a caller's promise, or hand the value straight back with no realm."""
    realm = getattr(services, "realm_services", None)
    if realm is None:
        if error is not None:
            raise error
        return value
    promise, resolve, reject = realm.make_promise()
    if error is None:
        resolve(value)
    else:
        reject(js_error_value(error))
    return promise


def _options(value):
    if value is pyv8.Undefined or value is None:
        return {}
    return value if isinstance(value, dict) else dictionary(value)


def _url_of(value):
    """The URL of a ``Request``, a ``URL`` or a string."""
    if isinstance(value, str):
        return value
    url = getattr(value, "_url", None) or getattr(value, "href", None)
    return str(url if url is not None else value)


def _method_of(request):
    return str(getattr(request, "_method", "GET") or "GET").upper()


def _body_of(response):
    body = getattr(response, "body", None)
    if body is None:
        return None
    return body if isinstance(body, bytes) else str(body).encode("utf-8")


def _entry_of(request, response):
    """One stored pair, as data no realm owns."""
    headers = response.get_headers()
    return {
        "url": str(getattr(request, "_url", "") or ""),
        "method": _method_of(request),
        "body": _body_of(response),
        "status": int(getattr(response, "status", 200)),
        "status_text": str(getattr(response, "status_text", "") or ""),
        "response_url": str(getattr(response, "url", "") or ""),
        "type": str(getattr(response, "type", "default") or "default"),
        "redirected": bool(getattr(response, "redirected", False)),
        "headers": dict(headers._data),
        "headers_immutable": headers._immutable,
    }


def _request_from(entry, services):
    return Request(entry["url"], {"method": entry["method"]}, services=services)


def _response_from(entry, services):
    response = Response(entry["body"], {
        "status": entry["status"], "statusText": entry["status_text"],
        "url": entry["response_url"], "type": entry["type"],
        "redirected": entry["redirected"]}, services=services)
    response.headers._data.update(entry["headers"])
    response.headers._immutable = entry.get("headers_immutable", False)
    return response


def _key_of(url, options):
    """The URL a lookup compares: the entry's own, minus its fragment."""
    if options.get("ignoreSearch"):
        url = url.split("?", 1)[0]
    return url.split("#", 1)[0]


def _key_of_request(request, options):
    return _key_of(str(getattr(request, "_url", "") or ""), options)


def _matches(entry, key, options):
    if _key_of(entry["url"], options) != key:
        return False
    return bool(options.get("ignoreMethod")) or entry["method"] == "GET"


def _as_request(value, services):
    """The request a stored pair holds, refused unless a cache may store it."""
    request = value if isinstance(value, Request) else Request(_url_of(value),
                                                               services=services)
    if _method_of(request) not in _STORABLE_METHODS:
        raise PageTypeError("Failed to execute 'put' on 'Cache': Request method"
                          f" '{_method_of(request)}' is unsupported.")
    return request


@interface('Cache', None)
class Cache:
    """One named cache: its entries, and the lookups over them."""

    def __init__(self, entries=None, services=None, **_):
        self._entries = entries if entries is not None else []
        self._services = services

    def _find(self, value, options):
        key = _key_of_request(_as_request(value, self._services), options)
        return [entry for entry in self._entries if _matches(entry, key, options)]

    def _store(self, request, response):
        entry = _entry_of(request, response)
        self._entries[:] = [stored for stored in self._entries
                            if _key_of(stored["url"], {}) != _key_of(entry["url"], {})]
        self._entries.append(entry)
        return entry

    def fn_put(self, request=MISSING, response=MISSING, *_extra):
        if request is MISSING:
            raise missing_error('Cache', 'put', 2, 0, constructor=False)
        if response is MISSING:
            raise missing_error('Cache', 'put', 2, 1, constructor=False)
        if not isinstance(response, Response):
            return _settle(self._services, error=PageTypeError(
                "Failed to execute 'put' on 'Cache': parameter 2 is not of type"
                " 'Response'."))
        try:
            self._store(_as_request(request, self._services), response)
        except (PageTypeError, IdlArgumentError) as refusal:
            return _settle(self._services, error=refusal)
        return _settle(self._services)

    def fn_match(self, request=MISSING, options=pyv8.Undefined, *_extra):
        if request is MISSING:
            raise missing_error('Cache', 'match', 1, 0, constructor=False)
        found = self._find(request, _options(options))
        if not found:
            return _settle(self._services, pyv8.Undefined)
        return _settle(self._services, _response_from(found[0], self._services))

    def fn_matchAll(self, request=pyv8.Undefined, options=pyv8.Undefined, *_extra):
        if request is pyv8.Undefined or request is None:
            entries = list(self._entries)
        else:
            entries = self._find(request, _options(options))
        return _settle(self._services,
                       [_response_from(entry, self._services) for entry in entries])

    def fn_keys(self, request=pyv8.Undefined, options=pyv8.Undefined, *_extra):
        if request is pyv8.Undefined or request is None:
            entries = list(self._entries)
        else:
            entries = self._find(request, _options(options))
        return _settle(self._services,
                       [_request_from(entry, self._services) for entry in entries])

    def fn_delete(self, request=MISSING, options=pyv8.Undefined, *_extra):
        if request is MISSING:
            raise missing_error('Cache', 'delete', 1, 0, constructor=False)
        found = self._find(request, _options(options))
        for entry in found:
            self._entries.remove(entry)
        return _settle(self._services, bool(found))

    # -- what a cache fetches for itself ---------------------------------------
    def _load(self, url, finish):
        resource = getattr(self._services, "resource_services", None)
        if resource is None:
            finish(None, PageTypeError("Cache: no resource loader in this realm"))
            return
        resource.load_resource_async(url, {"method": "GET", "headers": [], "body": None,
                                           "resource_type": "cache"}, finish)

    def _store_fetched(self, urls, resolve, reject, index=0):
        if index >= len(urls):
            resolve(pyv8.Undefined)
            return

        def finish(response, error):
            if error is not None:
                reject(js_error_value(error))
                return
            if not response.get_ok():
                reject(js_error_value(PageTypeError(
                    "Failed to execute 'add' on 'Cache': Request failed with status"
                    f" {response.status}.")))
                return
            try:
                request = Request(urls[index], services=self._services)
                response = filtered_network_response(
                    response, origin=self._services.realm_services.origin,
                    initial_url=request.get_url(),
                    redirect_crossed=getattr(response, '_redirect_crossed', False))
                self._store(request, response)
            except (PageTypeError, IdlArgumentError) as refusal:
                reject(js_error_value(refusal))
                return
            self._store_fetched(urls, resolve, reject, index + 1)

        self._load(urls[index], finish)

    def _fetch_into(self, values):
        realm = getattr(self._services, "realm_services", None)
        if realm is None:
            raise PageTypeError("Cache: no realm to fetch from")
        promise, resolve, reject = realm.make_promise()
        self._store_fetched([_url_of(value) for value in values], resolve, reject)
        return promise

    def fn_add(self, request=MISSING, *_extra):
        if request is MISSING:
            raise missing_error('Cache', 'add', 1, 0, constructor=False)
        return self._fetch_into([request])

    def fn_addAll(self, requests=MISSING, *_extra):
        if requests is MISSING:
            raise missing_error('Cache', 'addAll', 1, 0, constructor=False)
        values = [requests] if isinstance(requests, str) else list(requests)
        if not values:
            return _settle(self._services, error=PageTypeError(
                "Failed to execute 'addAll' on 'Cache': at least one request is"
                " required."))
        return self._fetch_into(values)


@interface('CacheStorage', None)
class CacheStorage:
    """The named caches of the realm's origin."""

    def __init__(self, services=None, **_):
        self._services = services

    def _caches(self):
        return _store_for(self._services)

    def _cache(self, name):
        return Cache(self._caches().setdefault(str(name), []), services=self._services)

    def fn_open(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('CacheStorage', 'open', 1, 0, constructor=False)
        if not str(name):
            return _settle(self._services, error=PageTypeError(
                "Failed to execute 'open' on 'CacheStorage': Cache name cannot be"
                " empty."))
        return _settle(self._services, self._cache(name))

    def fn_has(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('CacheStorage', 'has', 1, 0, constructor=False)
        return _settle(self._services, str(name) in self._caches())

    def fn_delete(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('CacheStorage', 'delete', 1, 0, constructor=False)
        return _settle(self._services, self._caches().pop(str(name), None) is not None)

    def fn_keys(self, *_extra):
        return _settle(self._services, list(self._caches()))

    def fn_match(self, request=MISSING, options=pyv8.Undefined, *_extra):
        if request is MISSING:
            raise missing_error('CacheStorage', 'match', 1, 0, constructor=False)
        settings = _options(options)
        for name in list(self._caches()):
            found = self._cache(name)._find(request, settings)
            if found:
                return _settle(self._services, _response_from(found[0], self._services))
        return _settle(self._services, pyv8.Undefined)

__all__ = ["Cache", "CacheStorage"]
