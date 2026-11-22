"""Resource request policy shared by navigation, Window and Worker."""
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from time import perf_counter
from urllib.parse import urlsplit

from ...foundation.host_errors import LoadFailure, RequestTimeout
from ...foundation.urls import absolute_url, origin_tuple, serialize_origin
from ...network.models import NetworkResponse
from ...network.redirects import Deliver, RedirectFailure, next_hop
from ...network.request_builder import finalize_request, request_clock
from .cors import (check_cors, cross_origin_preflight, network_preflight_request, preflight, preflight_request,
                   require_inspectable_redirects, validate_preflight)
from .local import resolve_local_response, validate_source_scheme
from .request_headers import browser_headers, request_destination, resource_mode
from .response import loader_response, plain_response


def check_origin(origin, target_origin, mode):
    if mode == 'same-origin' and origin != target_origin:
        raise LoadFailure('same-origin request crossed an origin')


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    target: str
    initiator: str
    options: dict
    prepared: dict
    mode: str
    destination: str
    extension_path: str | None
    cache_key: object
    remaining: Callable[[], float | None]


@dataclass(frozen=True, slots=True)
class HostWork:
    call: Callable[[], object]
    cache: bool = False


@dataclass(frozen=True, slots=True)
class ResourceRequestSession:
    """Profile request settings captured on the initiating Realm's owner."""

    profile: object
    environment: object
    origin: tuple
    base_url: str
    loader: object
    interceptor: object
    backend: object
    cache_enabled: bool
    group: int
    site_for_cookies: str
    partition: str
    top_level: bool = False
    owner_streams: bool = False
    referrer_policy: str = 'strict-origin-when-cross-origin'

    def prepare_headers(self, options, url, initiator, mode, destination):
        return {**options, 'headers': browser_headers(
            self.environment, destination=destination, mode=mode,
            method=str(options.get('method', 'GET')).upper(), url=url, current_url=initiator,
            current_origin=serialize_origin(self.origin),
            referrer_policy=options.get('referrer_policy') or self.referrer_policy,
            browser_initiated=bool(options.get('browser_initiated')),
            user_activated=bool(options.get('user_activated')),
            content_type=options.get('content_type'),
            http_version=2 if urlsplit(url).scheme == 'https' else 1,
            extra=options['headers'], accepted_hints=self.profile.accepted_client_hints(url),
        )}

    def prepare(self, url, options, *, initiator_url=None):
        options = dict(options or {})
        remaining = request_clock(options.get('timeout'))
        headers = options.get('headers')
        headers = getattr(headers, '_data', headers)
        options['headers'] = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
        initiator = str(options.get('initiator_url') or initiator_url or self.base_url)
        target = absolute_url(url, self.base_url)
        dest = request_destination(options)
        mode = resource_mode(dest, options)
        extension_path = validate_source_scheme(self.environment, initiator, target, mode=mode,
                                                browser_initiated=options.get('browser_initiated'))
        prepared = self.prepare_headers(options, target, initiator, mode, dest)
        key = self.profile.resource_cache.key(
            target, {**prepared, 'mode': mode}, enabled=self.cache_enabled,
            origin=serialize_origin(self.origin))
        # A disk response must not bypass authorization on a later cross-origin hop.
        if key is not None and mode == 'cors' and cross_origin_preflight(
                str(options.get('method', 'GET')).upper(), options['headers'],
                options.get('content_type') if options.get('body') is not None else None) is not None:
            key = None
        return ResourceRequest(target, initiator, options, prepared, mode, dest, extension_path, key, remaining)

    def local_response(self, load):
        return resolve_local_response(self.profile, load.target, self.origin, load.mode, load.extension_path)

    def _origin(self, url):
        inherited = self.profile._blob_origins.get(url)
        return origin_tuple(url) if inherited is None else tuple(inherited)

    def _response_policy(self, response, load, crossed=False):
        response_origin = self._origin(response.url)
        same = response_origin == self.origin
        check_origin(self.origin, response_origin, load.mode)
        if load.mode == 'cors' and not same:
            check_cors(response.headers, self.origin, load.options.get('credentials'))
        if load.mode == 'no-cors' and load.destination == 'empty' and (not same or crossed):
            response.status, response.status_text, response.body = 0, '', None
            response.headers, response.type, response.url, response.redirected = {}, 'opaque', '', False
        return response

    def submit(self, tasks, load, request, *, blocking=False, bind=None):
        return self.backend.submit(tasks, self.group, request, load.remaining,
                                   navigation=load.mode == 'navigate', blocking=blocking, bind=bind)

    def steps(self, load, handle, owner_gone, *, captured=None):
        """Advance the request with a host result or one raw transport reply at a time."""
        options, origin, mode = load.options, self.origin, load.mode
        target = load.target
        credentials = options.get('credentials') or 'same-origin'
        cache = self.profile.resource_cache
        key = load.cache_key

        def active():
            if handle.done or owner_gone() or self.profile.closed:
                raise LoadFailure('resource owner changed or request cancelled')
            load.remaining()

        def claim_publication():
            if owner_gone() or self.profile.closed:
                return False
            try:
                load.remaining()
            except RequestTimeout:
                return False
            return handle.claim_publication()

        active()
        if self.interceptor is not None:
            decision = yield HostWork(partial(self.interceptor, target, dict(load.prepared)))
            active()
            if decision is not None and decision is not True:
                value = {'body': '', 'headers': {'content-type': 'text/plain'}} if decision is False else decision
                return self._response_policy(loader_response(value, target, allow_stream=self.owner_streams), load)
        if key is not None:
            cached = cache.get_memory(key)
            if cached is None:
                cached = yield HostWork(partial(cache.get, key), cache=True)
                active()
            if cached is not None:
                return plain_response(cached, target)
        if isinstance(captured, Exception):
            raise captured
        local = self.local_response(load) if captured is None else captured
        if local is not None:
            return local
        loader, backend = self.loader, self.backend
        if not loader and (backend is None or not backend.http_available):
            raise LoadFailure('Network access requires an explicit resource_loader or real_network=True')

        method, body, headers = str(options.get('method', 'GET')).upper(), options.get('body'), options['headers']
        follows = not loader and bool(getattr(backend.http, 'FOLLOWS_ITSELF', False))
        redirect = str(options.get('redirect', 'follow'))
        require_inspectable_redirects(follows, mode, method, headers,
                                      options.get('content_type') if body is not None else None)
        if follows and mode != 'navigate' and redirect != 'follow':
            raise LoadFailure(f'this engine follows redirects by itself, so redirect: {redirect!r} cannot be honoured')

        hops = 0
        redirect_crossed = False
        started = redirected_at = perf_counter()
        while True:
            active()
            check_origin(origin, self._origin(target), mode)
            names = preflight(origin, target, mode, method, headers,
                              options.get('content_type') if body is not None else None)
            if names is not None:
                preflights = self.profile.preflight_cache
                probe_key = (preflights.key(self.partition, origin, target, credentials, method, names)
                             if not loader else None)
                if not preflights.contains(probe_key):
                    probe = preflight_request(origin, method, names, options.get('timeout'))
                    if loader:
                        value = yield HostWork(partial(loader, target, probe))
                        probe_response = loader_response(value, target)
                    else:
                        if follows:
                            raise LoadFailure('transport cannot prevent preflight redirects')
                        value = yield network_preflight_request(target, origin, probe)
                        probe_response = plain_response(value, target)
                    active()
                    if probe_response.redirected:
                        raise LoadFailure('transport followed a preflight redirect')
                    validate_preflight(probe_response.status, probe_response.headers, origin,
                                       credentials, method, names)
                    preflights.remember(probe_key, probe_response.headers)
            if hops:
                hop_options = {**options, 'method': method, 'body': body, 'headers': headers}
                if body is None:
                    hop_options.pop('content_type', None)
                request = self.prepare_headers(hop_options, target, load.initiator, mode, load.destination)
            else:
                request = dict(load.prepared)
            active()
            if loader:
                value = yield HostWork(partial(loader, target, request))
                response = loader_response(value, target, allow_stream=self.owner_streams)
            else:
                wire = finalize_request(
                    url=target, method=method, body=body, headers=request['headers'],
                    timeout=request.get('timeout'), credentials=credentials,
                    mode=mode, redirect=redirect if follows else 'manual',
                    origin=serialize_origin(origin), site_for_cookies=self.site_for_cookies,
                    top_level_navigation=mode == 'navigate' and self.top_level,
                    jar=backend.cookies)
                if key is not None and any(name.lower() in ('cookie', 'authorization') for name in wire.headers):
                    key = None
                value = yield wire
                response = plain_response(value, target)
                if 'set-cookie' in response.headers:
                    key = None
            active()
            response_origin = self._origin(response.url)
            if mode == 'cors' and response_origin != origin:
                check_cors(response.headers, origin, credentials)
            check_origin(origin, response_origin, mode)
            if follows and response.redirected:
                decision = Deliver()
            else:
                try:
                    decision = next_hop(status=response.status, method=method, url=target, headers=headers,
                                        body=body, location=response.headers.get('location', ''), hops=hops,
                                        mode=mode, redirect=redirect)
                except RedirectFailure as error:
                    raise LoadFailure(str(error)) from error
            if not isinstance(decision, Deliver):
                redirect_crossed = redirect_crossed or decision.cross_origin
                target, method, body, headers = decision.url, decision.method, decision.body, decision.headers
                hops += 1
                redirected_at = perf_counter()
                continue
            if decision.opaque:
                return NetworkResponse(url=target, status=0, status_text='', body='', type='opaqueredirect')
            response.redirected = bool(hops or response.redirected)
            response.redirect_crossed |= redirect_crossed
            if hops:
                response.redirect_hops = hops
                response.redirect_span = (redirected_at - started) * 1000
            if mode == 'no-cors':
                response = self._response_policy(response, load, response.redirect_crossed)
            self.profile.remember_client_hints(response.url or target, response.headers)
            if key is not None and isinstance(response.body, (str, bytes)):
                yield HostWork(partial(cache.put, key, {
                    'body': response.body, 'status': response.status, 'statusText': response.status_text,
                    'url': response.url or target, 'headers': response.headers, 'redirected': response.redirected,
                    'type': response.type, 'redirectCrossed': response.redirect_crossed,
                }, claim_publication), cache=True)
                active()
            return response
