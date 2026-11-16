"""Realm-free request sessions: only immutable config and ordinary payloads cross threads."""
from dataclasses import dataclass
from types import SimpleNamespace

from ...foundation.host_errors import LoadFailure
from ...foundation.environment import extension_resource
from ...foundation.urls import absolute_url, origin_tuple, serialize_origin, unloadable_scheme
from ...network.models import NetworkRequest, NetworkResponse
from ...network.redirects import Deliver, RedirectFailure, next_hop
from .local import data_url_response, extension_response
from .cors import (check_cors, network_preflight_request, preflight, preflight_request,
                   require_inspectable_redirects, validate_preflight)


def check_origin(origin, target_origin, mode):
    if mode == 'same-origin' and origin != target_origin:
        raise LoadFailure('same-origin request crossed an origin')


def plain_response(value, url):
    """Copy host/transport responses; never publish JS values or bound responses."""
    if isinstance(value, dict):
        body = value.get('body', '')
        headers = value.get('headers', {})
        status, status_text = value.get('status', 200), value.get('statusText', 'OK')
        redirected, kind = value.get('redirected', False), value.get('type', 'basic')
        redirect_crossed = value.get('redirectCrossed', False)
        url = value.get('url') or url
    elif isinstance(value, (str, bytes)) or value is None:
        body, headers, status, status_text, redirected, kind = str(value) if value is None else value, {}, 200, 'OK', False, 'basic'
        redirect_crossed = False
    else:
        body, headers = value.body, value.headers
        headers = getattr(headers, '_data', headers)
        status, status_text = value.status, value.status_text
        redirected, kind = value.redirected, value.type
        redirect_crossed = getattr(value, 'redirect_crossed',
                                   getattr(value, '_redirect_crossed', False))
        url = value.url or url
    if isinstance(body, (bytearray, memoryview)):
        body = bytes(body)
    if body is not None and not isinstance(body, (str, bytes)):
        raise LoadFailure('resource response body is not plain data')
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not (isinstance(v, str) or
        (isinstance(v, (list, tuple)) and all(isinstance(item, str) for item in v)))
        for k, v in headers.items()
    ):
        raise LoadFailure('resource response headers are not plain data')
    headers = {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in headers.items()}
    return NetworkResponse(url=str(url), body=body, status=int(status), status_text=str(status_text),
                           headers={k.lower(): v for k, v in headers.items()},
                           redirected=bool(redirected), type=str(kind),
                           redirect_crossed=bool(redirect_crossed))


def validate_source_scheme(environment, initiator, target, options):
    """Reject browser-owned schemes before invoking any host callback."""
    blocked = unloadable_scheme(target)
    if blocked is None:
        return
    if (str(target).lower().startswith('chrome-extension:')
            and (options.get('mode') != 'navigate' or options.get('browser_initiated'))
            and extension_resource(environment, target, source_url=initiator) is not None):
        return
    raise LoadFailure(blocked)


def select_source(*, profile, environment, initiator, target, options, origin,
                  cache_key, loader, backend):
    """Shared source precedence for page and Worker requests."""
    blocked = unloadable_scheme(target)
    # Extension resources are handled locally below; no other browser-owned
    # scheme may reach the cache, a custom loader, or a network engine.
    validate_source_scheme(environment, initiator, target, options)
    cached = profile.resource_cache.get(cache_key) if blocked is None else None
    if cached is not None:
        return 'payload', plain_response(cached, target)
    if target.startswith('about:blank'):
        return 'payload', NetworkResponse(url=target, body='')
    if target.startswith('blob:'):
        target_origin = tuple(profile._blob_origins.get(target, origin_tuple(target)))
        if options.get('mode') != 'navigate' and origin != target_origin:
            raise LoadFailure('Blob URL belongs to another origin')
        body, content_type = profile.resolve_blob_url(target)
        return 'payload', NetworkResponse(url=target, body=body, headers={'content-type': content_type})
    if target[:5].lower() == 'data:':
        return 'payload', plain_response(data_url_response(target, None), target)
    view = SimpleNamespace(environment=environment, url=initiator, services=None)
    extension = extension_response(view, target, options)
    if extension is not None:
        return 'payload', plain_response(extension, target)
    if blocked is not None:
        raise LoadFailure(blocked)
    if loader:
        return 'loader', loader
    if backend is not None and backend.http_available:
        return 'engine', backend
    raise LoadFailure('Network access requires an explicit resource_loader or real_network=True')


@dataclass(frozen=True, slots=True)
class ResourceRequestSession:
    """Snapshot on the creator owner; contains no Page, DOM or Realm callback."""
    profile: object
    environment: object
    origin: tuple
    base_url: str
    loader: object
    interceptor: object
    backend: object
    cache_enabled: bool
    site_for_cookies: str | None = None

    def load(self, url, options, cancelled):
        from .fetcher import ResourceFetchController
        options = dict(options or {})
        initiator = str(options.get('initiator_url') or self.base_url)
        target = absolute_url(url, initiator)
        origin = self.origin
        view = SimpleNamespace(environment=self.environment, url=initiator, profile=self.profile)
        mode = ResourceFetchController._request_mode(options)
        prepare = ResourceFetchController._request_with_environment_headers
        prepared = prepare(options, view, target, initiator, origin)
        cache = self.profile.resource_cache
        key = cache.key(target, prepared, enabled=self.cache_enabled, origin=serialize_origin(origin))

        def active():
            if cancelled() or self.profile.closed:
                raise LoadFailure('resource owner closed or request cancelled')

        active()
        validate_source_scheme(self.environment, initiator, target, options)
        if self.interceptor is not None:
            decision = self.interceptor(target, dict(prepared))
            active()
            if decision is not None and decision is not True:
                return plain_response({'body': ''} if decision is False else decision, target)
        kind, source = select_source(profile=self.profile, environment=self.environment,
                                     initiator=initiator, target=target, options=options, origin=origin,
                                     cache_key=key, loader=self.loader, backend=self.backend)
        if kind == 'payload':
            return source
        method, body = str(options.get('method', 'GET')).upper(), options.get('body')
        headers = ResourceFetchController.header_dict(options.get('headers'))
        follows = kind == 'engine' and bool(getattr(source.http, 'FOLLOWS_ITSELF', False))
        require_inspectable_redirects(follows, mode, method, headers,
                                      options.get('content_type') if body is not None else None)
        if follows and mode != 'navigate' and options.get('redirect', 'follow') != 'follow':
            raise LoadFailure('this engine follows redirects by itself and cannot be told not to, so '
                              f"redirect: {options.get('redirect')!r} cannot be honoured")
        hops = 0
        redirect_crossed = False
        while True:
            active()
            target_origin = tuple(self.profile._blob_origins.get(target, origin_tuple(target)))
            check_origin(origin, target_origin, mode)
            names = preflight(origin, target, mode, method, headers,
                              options.get('content_type') if body is not None else None)
            if names is not None:
                probe = preflight_request(origin, method, names, options.get('timeout'))
                if kind == 'loader':
                    value = source(target, probe)
                    preflight_response = plain_response(value, target)
                else:
                    if follows:
                        raise LoadFailure('transport cannot prevent preflight redirects')
                    preflight_response = plain_response(source.request_sync(
                        network_preflight_request(target, origin, probe)), target)
                active()
                validate_preflight(preflight_response.status, preflight_response.headers, origin,
                                   options.get('credentials'), method, names)
            hop_options = {**options, 'method': method, 'body': body, 'headers': headers}
            if hops and body is None:
                hop_options.pop('content_type', None)
            request = prepare(hop_options, view, target, initiator, origin)
            if kind == 'loader':
                value = source(target, request)
                response = plain_response({**value, 'url': target} if isinstance(value, dict) else value, target)
            else:
                response = plain_response(source.request_sync(NetworkRequest(
                    url=target, method=method, body=body, headers=request['headers'],
                    timeout=request.get('timeout'), credentials=request.get('credentials', 'same-origin'),
                    mode=mode, redirect=str(options.get('redirect', 'follow')) if follows else 'manual',
                    origin=serialize_origin(origin), site_for_cookies=self.site_for_cookies)), target)
            active()
            response_origin = origin_tuple(response.url)
            if mode == 'cors' and response_origin != origin:
                check_cors(response.headers, origin, options.get('credentials'))
            check_origin(origin, response_origin, mode)
            if follows and response.redirected:
                if options.get('redirect', 'follow') != 'follow':
                    raise LoadFailure('transport followed a redirect that must be inspected')
                decision = Deliver()
            else:
                try:
                    decision = next_hop(status=response.status, method=method, url=target, headers=headers,
                                        body=body, location=response.headers.get('location', ''), hops=hops,
                                        mode=mode, redirect=str(options.get('redirect', 'follow')))
                except RedirectFailure as error:
                    raise LoadFailure(str(error)) from error
            if not isinstance(decision, Deliver):
                redirect_crossed = redirect_crossed or decision.cross_origin
                target, method, body, headers = decision.url, decision.method, decision.body, decision.headers
                hops += 1
                continue
            if decision.opaque:
                return NetworkResponse(url=target, status=0, status_text='', body='', type='opaqueredirect')
            response.redirected = bool(hops or response.redirected)
            response.redirect_crossed = redirect_crossed or response.redirect_crossed
            if options.get('mode') == 'no-cors' and (origin != response_origin or redirect_crossed):
                response.status, response.status_text, response.body = 0, '', None
                response.headers, response.type, response.url, response.redirected = {}, 'opaque', '', False
            active()
            self.profile.remember_client_hints(response.url or target, response.headers)
            cache.put(key, {'body': response.body, 'status': response.status,
                            'statusText': response.status_text, 'url': response.url,
                            'headers': response.headers, 'redirected': response.redirected, 'type': response.type,
                            'redirectCrossed': response.redirect_crossed})
            return response
