"""Shared request preparation, source selection and redirect drivers."""
from dataclasses import dataclass
from functools import cached_property
import time
from urllib.parse import urlsplit

from pyv8c import pyv8
from ...foundation.error_policy import control_exceptions
from ...foundation.host_errors import LoadFailure
from ...foundation.realm import WorkerSource
from ...foundation.urls import absolute_url, origin_tuple, serialize_origin
from ...web.bom.utils import page_base_url
from ...web.fetch.headers import Headers
from ...web.fetch.response import Response
from ...network.models import NetworkRequest
from ...network.redirects import Deliver, RedirectFailure, next_hop
from .request_headers import browser_headers, fetch_mode, same_site
from .callbacks import CallbackRunner
from .handles import LoadHandle
from .session import select_source as select_request_source, check_origin, validate_source_scheme
from .cors import (check_cors as validate_cors, network_preflight_request, preflight,
                   preflight_request, require_inspectable_redirects, validate_preflight)


@dataclass(slots=True)
class LoadRequest:
    target: str
    options: dict
    prepared: dict
    initiator_url: str
    origin: tuple
    generation: int
    cache_key: object
    owner_realm: object = None


@dataclass(frozen=True, slots=True)
class LoadContext:
    origin: tuple
    owner_realm: object
    force_pool: bool = False


@dataclass(slots=True)
class HopState:
    url: str
    method: str
    body: object
    headers: dict
    mode: str
    redirect: str
    hops: int = 0
    crossed: bool = False
    started: float = 0.0
    redirected_at: float = 0.0


class ResourceFetchController:
    _NO_CORS_DESTINATIONS = ("script", "style", "image", "audio", "video")

    def __init__(self, page):
        self.page = page
        self.cache = page.profile.resource_cache

    @cached_property
    def callbacks(self):
        # PageServices is assembled after the controllers, before the first load.
        return CallbackRunner(self.page)

    def convert_response(self, value, target, *, loader=False):
        response = self._response_from_loader(value, target) if loader else self.response_object(value)
        response._services = self.page.services
        return response

    def _intercepted_response(self, decision, target):
        """The response one interceptor decision stands for."""
        if decision is False:
            return Response("", {
                "status": 200, "url": target,
                "headers": {"content-type": "text/plain"},
            }, services=self.page.services)
        response = self.convert_response(decision, target, loader=True)
        return response

    @staticmethod
    def _destination(request):
        """The Fetch destination a request dict stands for; ``resource_type`` names
        the loader's bucket, so a caller that knows better passes the destination."""
        explicit = request.get("destination")
        if explicit:
            return str(explicit)
        resource_type = str(request.get("resource_type") or "")
        if resource_type in ("document", "iframe", "script", "style", "image"):
            return resource_type
        if resource_type == "media":
            return "video"
        return "empty"

    @staticmethod
    def _request_with_environment_headers(request, page, target, initiator_url=None, origin=None):
        """The request dict with the headers the browser would send for it."""
        request = dict(request or {})
        destination = ResourceFetchController._destination(request)
        # An https URL negotiates h2, where Chrome sends no Connection header.
        http_version = 2 if urlsplit(str(target)).scheme == "https" else 1
        #: A worker's request reports the worker's own (often ``blob:``) URL in
        #: ``Referer``/``Sec-Fetch-Site``/``Origin``, not the owner document's.
        request["headers"] = browser_headers(
            page.environment,
            destination=destination,
            mode=fetch_mode(destination, request.get("mode") or (
                "no-cors" if destination in ResourceFetchController._NO_CORS_DESTINATIONS else "cors")),
            method=str(request.get("method", "GET")).upper(),
            url=target,
            current_url=initiator_url or request.get("initiator_url") or page.url,
            current_origin=None if origin is None else serialize_origin(origin),
            browser_initiated=bool(request.get("browser_initiated")),
            user_activated=bool(request.get("user_activated")),
            content_type=request.get("content_type"),
            http_version=http_version,
            extra=ResourceFetchController.header_dict(request.get("headers")),
            accepted_hints=(page.profile.accepted_client_hints(target)
                            if getattr(page, 'profile', None) is not None else ()),
        )
        return request

    @staticmethod
    def _response_from_loader(result, target):
        """Wrap a resource_loader result into a Response."""
        if isinstance(result, Response):
            if not result.url:
                result.url = target
            return result
        if isinstance(result, dict):
            return Response(result.get("body", ""), {**result, "url": target})
        return Response(str(result), {"url": target})

    @staticmethod
    def header_dict(headers):
        if isinstance(headers, Headers):
            return dict(headers._data)
        if isinstance(headers, dict):
            return {str(k): str(v) for k, v in headers.items()}
        return {}

    def response_object(self, raw):
        if isinstance(raw, Response):
            return raw
        response = Response(
            raw.body,
            {
                "status": raw.status, "statusText": raw.status_text, "url": raw.url,
                "headers": raw.headers, "redirected": raw.redirected, "type": raw.type,
            },
            services=self.page.services,
        )
        response._redirect_crossed = bool(getattr(raw, 'redirect_crossed', False))
        return response

    @staticmethod
    def _request_mode(request):
        """The mode a request stands for: what it states, else what its destination makes
        it, which is how the request's own headers were built."""
        if ResourceFetchController._destination(request) in ("document", "iframe"):
            return "navigate"
        stated = request.get("mode")
        if stated is not None:
            return str(stated)
        if ResourceFetchController._destination(request) in ResourceFetchController._NO_CORS_DESTINATIONS:
            return "no-cors"
        return "cors"

    def decide(self, response, carrier):
        """What the response asks the load path to do next."""
        location = response.headers.fn_get("location")
        if location is pyv8.Null or location is pyv8.Undefined:
            location = ""
        return next_hop(
            status=response.status,
            method=carrier.method,
            url=carrier.url,
            headers=carrier.headers,
            body=carrier.body,
            location=location,
            hops=carrier.hops,
            mode=carrier.mode,
            redirect=carrier.redirect,
        )

    def advance(self, carrier, decision):
        carrier.url, carrier.method, carrier.body = decision.url, decision.method, decision.body
        carrier.headers = decision.headers
        carrier.hops += 1
        carrier.crossed = carrier.crossed or decision.cross_origin
        carrier.redirected_at = time.perf_counter()

    def refuse_followed(self, carrier):
        """An engine that follows by itself cannot be told to stop, so the modes that
        forbid following are refused rather than quietly obeyed."""
        if carrier.mode == "navigate" or carrier.redirect == "follow":
            return
        raise LoadFailure(
            "this engine follows redirects by itself and cannot be told not to, so"
            f" redirect: {carrier.redirect!r} cannot be honoured"
        )

    def opaque_redirect(self, url):
        """What ``redirect: "manual"`` reads as: no status, no headers, no body."""
        return Response("", {"status": 0, "url": url, "type": "opaqueredirect"},
                        services=self.page.services)

    def engine_redirect(self, backend, request):
        """What an engine is told about following: this layer's job, unless the engine
        documents that following cannot be turned off."""
        if getattr(backend.http, "FOLLOWS_ITSELF", False):
            if self._request_mode(request) != "navigate" and request.get("redirect", "follow") != "follow":
                raise LoadFailure("this engine follows redirects by itself and cannot be told not to, so"
                                  f" redirect: {request.get('redirect')!r} cannot be honoured")
            return str(request.get("redirect", "follow"))
        return "manual"

    def load_worker_source_async(self, script_url, callback):
        def complete(response, error):
            source = None
            if error is None:
                try:
                    source = WorkerSource(self.worker_source(response, script_url),
                                          response.url or script_url)
                except control_exceptions():
                    raise
                except BaseException as exc:
                    error = exc
            callback(source, error)
        return self.load_resource_async(script_url, {
            "method": "GET", "mode": "same-origin", "destination": "worker",
            "resource_type": "script",
        }, complete, context=LoadContext(origin=tuple(self.page.origin),
                                        owner_realm=self.page.realm, force_pool=True))

    @staticmethod
    def worker_source(response, script_url):
        if response.status and not 200 <= response.status < 300:
            #: A worker whose script does not arrive is a failed load, the way a browser
            #: reads it; the web layer turns that into the Worker's ``error``.  A script
            #: a page made itself -- ``blob:``, ``data:`` -- carries no status and is
            #: not an answer that went wrong.
            raise LoadFailure(f"the worker script {script_url} answered {response.status}")
        return response.body_text()

    def prepare(self, url, options, *, context=None):
        page = self.page
        if page.closed or page.profile.closed:
            raise LoadFailure("Page is closed")
        target = absolute_url(url, page_base_url(page))
        given = dict(options or {})
        initiator = str(given.get("initiator_url") or page.url)
        origin = tuple(page.origin)
        if given.get("initiator_url") and urlsplit(initiator).scheme in ("http", "https", "blob"):
            origin = tuple(page.profile._blob_origins.get(initiator, origin_tuple(initiator)))
        if context is not None:
            origin = context.origin
        prepared = self._request_with_environment_headers(given, page, target, initiator, origin)
        key = self.cache.key(target, prepared, enabled=page.cache_enabled, origin=serialize_origin(origin))
        return LoadRequest(target=target, options=given, prepared=prepared, initiator_url=initiator,
                           origin=origin, generation=page.navigation.generation, cache_key=key,
                           owner_realm=None if context is None else context.owner_realm)

    def active(self, load, handle=None):
        return (not self.page.closed and not self.page.profile.closed
                and (load.owner_realm is self.page.realm if load.owner_realm is not None
                     else load.generation == self.page.navigation.generation)
                and (handle is None or not handle.done))

    def select_source(self, load):
        page = self.page
        kind, value = select_request_source(
            profile=page.profile, environment=page.environment, initiator=load.initiator_url,
            target=load.target, options={**load.options, 'mode': self._request_mode(load.options)},
            origin=load.origin, cache_key=load.cache_key, loader=page.resource_loader,
            backend=page.network_backend)
        return ('response', self.response_object(value)) if kind == 'payload' else (kind, value)

    def store(self, load, response):
        # A response may ask for high-entropy client hints; Chrome adds them to later
        # requests to that origin, so the profile remembers what was asked for.
        try:
            headers = getattr(getattr(response, "headers", None), "_data", None) or {}
            profile = getattr(self.page, "profile", None)
            if profile is not None:
                profile.remember_client_hints(response.url or load.target, headers)
        except Exception:
            # Header bookkeeping must never fail a load.
            pass
        if load.cache_key is None:
            return
        payload = {"body": response.body, "status": response.status,
                   "statusText": response.status_text, "url": response.url or load.target,
                   "headers": dict(response.headers._data), "redirected": response.redirected,
                   "type": response.type,
                   "redirectCrossed": bool(getattr(response, '_redirect_crossed', False))}
        self.cache.put(load.cache_key, payload)

    def network_request(self, target, request, load, backend):
        require_inspectable_redirects(getattr(backend.http, 'FOLLOWS_ITSELF', False),
                                      self._request_mode(load.options), str(request.get('method', 'GET')),
                                      self.header_dict(load.options.get('headers')),
                                      load.options.get('content_type') if request.get('body') is not None else None)
        site = serialize_origin(load.origin)
        parent = self.page.parent_page
        while parent is not None:
            if not same_site(site, serialize_origin(parent.origin)):
                site = 'null'
                break
            parent = parent.parent_page
        return NetworkRequest(url=target, method=str(request.get("method", "GET")).upper(),
                              headers=self.header_dict(request.get("headers")), body=request.get("body"),
                              timeout=request.get("timeout"), credentials=request.get("credentials", "same-origin"),
                              mode=self._request_mode(load.options), redirect=self.engine_redirect(backend, load.options),
                              origin=serialize_origin(load.origin), site_for_cookies=site,
                              top_level_navigation=(self._request_mode(load.options) == 'navigate'
                                                    and self.page.parent_page is None))

    def hop_carrier(self, load):
        options = load.options
        return HopState(url=load.target, method=str(options.get("method", "GET")).upper(),
                        body=options.get("body"), headers=self.header_dict(options.get("headers")),
                        mode=self._request_mode(options), redirect=str(options.get("redirect", "follow")),
                        started=time.perf_counter())

    def hop_request(self, load, carrier):
        if not carrier.hops:
            return dict(load.prepared)
        options = {**load.options, "method": carrier.method, "body": carrier.body, "headers": carrier.headers}
        if carrier.body is None:
            options.pop('content_type', None)
        return self._request_with_environment_headers(options, self.page, carrier.url, load.initiator_url, load.origin)

    def same_origin_response(self, url, load):
        inherited = self.page.profile._blob_origins.get(str(url))
        return load.origin == (tuple(inherited) if inherited is not None else origin_tuple(url))

    def check_cors(self, response, load):
        validate_cors(response.headers._data, load.origin, load.options.get('credentials'))

    def check_request_origin(self, target, load):
        target_origin = tuple(self.page.profile._blob_origins.get(str(target), origin_tuple(target)))
        check_origin(load.origin, target_origin, self._request_mode(load.options))

    def apply_response_policy(self, response, load, *, redirect_crossed=False):
        mode = self._request_mode(load.options)
        same_origin = self.same_origin_response(response.url, load)
        if mode == "navigate" or (same_origin and not (mode == "no-cors" and redirect_crossed)):
            return response
        self.check_request_origin(response.url, load)
        if mode == "no-cors" and self._destination(load.options) == "empty":
            # A script, style or image loader still consumes the bytes of a
            # cross-origin no-CORS response.  Only a script-visible fetch gets
            # the opaque view.
            response.status, response.status_text, response.body = 0, "", None
            response.headers, response.type, response.url = Headers(), "opaque", ""
        elif mode == "cors":
            self.check_cors(response, load)
        return response

    def step(self, response, load, carrier, engine_follows):
        """Shared transition for sync and async: policy, redirect decision, delivery."""
        if carrier.mode == "cors" and not self.same_origin_response(response.url, load):
            self.check_cors(response, load)
        self.check_request_origin(response.url, load)
        if engine_follows and response.redirected:
            self.refuse_followed(carrier)
            decision = Deliver()
        else:
            try:
                decision = self.decide(response, carrier)
            except RedirectFailure as exc:
                raise LoadFailure(str(exc)) from exc
        if not isinstance(decision, Deliver):
            self.check_request_origin(decision.url, load)
            self.advance(carrier, decision)
            return None
        if decision.opaque:
            return self.opaque_redirect(carrier.url)
        response = self.apply_response_policy(response, load, redirect_crossed=carrier.crossed)
        if carrier.hops:
            response.redirected = response.type != 'opaque'
            response._redirect_hops = carrier.hops
            response._redirect_crossed = carrier.crossed
            response._redirect_span = (carrier.redirected_at - carrier.started) * 1000.0
        return response

    def send_sync(self, load, carrier, kind, source):
        self.check_request_origin(carrier.url, load)
        names = preflight(load.origin, carrier.url, carrier.mode, carrier.method, carrier.headers,
                          load.options.get('content_type') if carrier.body is not None else None)
        if names is not None:
            probe = preflight_request(load.origin, carrier.method, names, load.options.get('timeout'))
            if kind == 'loader':
                value = self.callbacks.call_sync(lambda: source(carrier.url, probe), probe.get('timeout'))
                response = self.convert_response(value, carrier.url, loader=True)
            else:
                if getattr(source.http, 'FOLLOWS_ITSELF', False):
                    raise LoadFailure('transport cannot prevent preflight redirects')
                response = self.response_object(source.request_sync(
                    network_preflight_request(carrier.url, load.origin, probe)))
            validate_preflight(response.status, response.headers._data, load.origin,
                               load.options.get('credentials'), carrier.method, names)
            if not self.active(load):
                raise LoadFailure('Resource owner changed during CORS preflight')
        request = self.hop_request(load, carrier)
        if kind == "loader":
            value = self.callbacks.call_sync(lambda: source(carrier.url, request), request.get("timeout"))
            response = self.convert_response(value, carrier.url, loader=True)
            return response
        return self.response_object(source.request_sync(self.network_request(carrier.url, request, load, source)))

    def load_resource(self, url, request=None):
        load = self.prepare(url, request)
        validate_source_scheme(self.page.environment, load.initiator_url, load.target,
                               {**load.options, 'mode': self._request_mode(load.options)})
        interceptor = self.page.resource_interceptor
        if callable(interceptor):
            decision = self.callbacks.call_sync(lambda: interceptor(load.target, dict(load.prepared)), load.options.get("timeout"))
            if not self.active(load):
                raise LoadFailure("Resource owner changed while intercepting")
            if decision is not None and decision is not True:
                return self.apply_response_policy(
                    self._intercepted_response(decision, load.target), load)
        kind, source = self.select_source(load)
        if kind == "response":
            return source
        carrier = self.hop_carrier(load)
        follows = kind == "engine" and bool(getattr(source.http, "FOLLOWS_ITSELF", False))
        while self.active(load):
            response = self.send_sync(load, carrier, kind, source)
            if not self.active(load):
                break
            delivered = self.step(response, load, carrier, follows)
            if delivered is not None:
                self.store(load, delivered)
                return delivered
        raise LoadFailure("Resource owner changed while loading")

    def load_resource_async(self, url, request, callback, *, context=None):
        from functools import partial
        start = self._start_async if context is None else partial(self._start_async, context=context)
        return self.page.resource_ownership.issue(start, url, request, callback)

    def _start_async(self, url, request, callback, *, context=None):
        return _AsyncLoad(self, callback, context=context).start(url, request)


class _AsyncLoad:
    """One request owns its stages; each hop owns its reply guard."""
    __slots__ = ("fetcher", "callback", "handle", "tasks", "load", "carrier",
                 "kind", "source", "follows", "stage", "_in_completion",
                 "_interception_finished", "_local_source", "_local_error", "context")

    def __init__(self, fetcher, callback, *, context=None):
        self.fetcher, self.callback = fetcher, callback
        self.context = context
        self.handle = LoadHandle()
        self.tasks = fetcher.page.tasks
        self.load = self.carrier = self.source = None
        self.kind = None
        self.follows = False
        self.stage = 0
        self._in_completion = False
        self._interception_finished = False
        self._local_source = self._local_error = None

    def enqueue(self, callback, *args):
        """The sole boundary from raw results to owner-side processing."""
        from functools import partial
        from ...foundation.tasks import TaskSource
        if self.handle.done:
            return
        try:
            ticket = self.tasks.post_task(
                partial(self._run_completion, callback, args), TaskSource.NETWORKING,
                label="resource-completion")
        except BaseException:
            self.handle.cancel()
            raise
        if ticket is None:
            self.handle.cancel()

    def _run_completion(self, callback, args):
        if self.handle.done:
            return
        previous = self._in_completion
        self._in_completion = True
        try:
            callback(*args)
        finally:
            self._in_completion = previous

    def active(self):
        if not self.fetcher.active(self.load, self.handle):
            self.handle.cancel()
            return False
        return True

    def deliver(self, response, error):
        if self._in_completion:
            self.settle(response, error)
        else:
            self.enqueue(self.settle, response, error)

    def settle(self, response, error):
        page = self.fetcher.page
        if (page.closed or page.profile.closed or
                (self.load is not None and not self.fetcher.active(self.load))):
            self.handle.cancel()
        if self.handle.finish():
            self.callback(response, error)

    def fail(self, error):
        self.deliver(None, error)

    def bind(self, transport, stage):
        # Inline callbacks can advance before the previous dispatch returns.
        if stage == self.stage or self.handle.cancelled:
            self.handle.bind_transport(transport)

    def start(self, url, request):
        from functools import partial
        try:
            self.load = (self.fetcher.prepare(url, request) if self.context is None else
                         self.fetcher.prepare(url, request, context=self.context))
            validate_source_scheme(self.fetcher.page.environment, self.load.initiator_url,
                                   self.load.target, {**self.load.options,
                                   'mode': self.fetcher._request_mode(self.load.options)})
        except control_exceptions():
            raise
        except BaseException as error:
            if self.fetcher.page.closed or self.fetcher.page.profile.closed:
                self.handle.cancel()
            else:
                self.fail(error)
            return self.handle
        interceptor = self.fetcher.page.resource_interceptor
        # Capture page-owned URLs before a same-turn revoke. Only local lookup
        # happens here; host interception and delivery remain asynchronous.
        if (self.load.prepared.get('destination') == 'worker' and
                self.load.target.startswith(('blob:', 'data:'))):
            try:
                self._local_source = self.fetcher.select_source(self.load)
            except control_exceptions():
                raise
            except BaseException as error:
                self._local_error = error
        if callable(interceptor):
            stage = self.stage
            transport = self.fetcher.callbacks.dispatch(
                partial(interceptor, self.load.target, dict(self.load.prepared)),
                partial(self.enqueue, self.intercepted),
                force_pool=self.force_pool())
            self.bind(transport, stage)
        else:
            self.begin()
        return self.handle

    def force_pool(self):
        return ((self.context is not None and self.context.force_pool) or
                self.load.prepared.get('mode') == 'navigate' or
                self.load.prepared.get('destination') == 'worker')

    def intercepted(self, decision, error):
        if self._interception_finished:
            return
        self._interception_finished = True
        if not self.active():
            return
        if error is not None:
            self.deliver(None, error)
        elif decision is None or decision is True:
            self.begin()
        else:
            try:
                response = self.fetcher.apply_response_policy(
                    self.fetcher._intercepted_response(decision, self.load.target), self.load)
            except control_exceptions():
                raise
            except BaseException as error:
                self.deliver(None, error)
            else:
                self.deliver(response, None)

    def immediate(self, response):
        if self.active():
            self.deliver(response, None)

    def begin(self):
        if not self.active():
            return
        try:
            if self._local_error is not None:
                raise self._local_error
            self.kind, self.source = self._local_source or self.fetcher.select_source(self.load)
            if self.kind == "response":
                if self._in_completion:
                    self.immediate(self.source)
                else:
                    self.enqueue(self.immediate, self.source)
                return
            self.carrier = self.fetcher.hop_carrier(self.load)
            self.follows = self.kind == "engine" and bool(getattr(self.source.http, "FOLLOWS_ITSELF", False))
            self.send()
        except control_exceptions():
            raise
        except BaseException as error:
            self.fail(error)

    def send(self):
        from functools import partial
        if not self.active():
            return
        self.stage += 1
        stage = self.stage
        try:
            self.fetcher.check_request_origin(self.carrier.url, self.load)
            names = preflight(self.load.origin, self.carrier.url, self.carrier.mode,
                              self.carrier.method, self.carrier.headers,
                              self.load.options.get('content_type') if self.carrier.body is not None else None)
            if names is not None:
                probe = preflight_request(self.load.origin, self.carrier.method, names,
                                          self.load.options.get('timeout'))
                callback = partial(self.enqueue, self.preflight_landed, stage, names)
                if self.kind == 'loader':
                    transport = self.fetcher.callbacks.dispatch(
                        partial(self.source, self.carrier.url, probe), callback,
                        force_pool=self.force_pool())
                else:
                    if self.follows:
                        raise LoadFailure('transport cannot prevent preflight redirects')
                    request = network_preflight_request(self.carrier.url, self.load.origin, probe)
                    transport = self.source.request_async(self.fetcher.page.services.task_services,
                                                          request, callback)
                self.bind(transport, stage)
                return
            self.send_actual(stage)
        except control_exceptions():
            raise
        except BaseException as error:
            self.fail(error)

    def preflight_landed(self, stage, names, value, error):
        if stage != self.stage or not self.active():
            return
        try:
            if error is not None:
                raise error
            response = self.fetcher.convert_response(value, self.carrier.url, loader=self.kind == 'loader')
            validate_preflight(response.status, response.headers._data, self.load.origin,
                               self.load.options.get('credentials'), self.carrier.method, names)
            self.stage += 1
            self.send_actual(self.stage)
        except control_exceptions():
            raise
        except BaseException as error:
            self.fail(error)

    def send_actual(self, stage):
        from functools import partial
        try:
            prepared = self.fetcher.hop_request(self.load, self.carrier)
            reply = _HopReply(self, self.carrier.url, stage)
            if self.kind == "loader":
                transport = self.fetcher.callbacks.dispatch(
                    partial(self.source, self.carrier.url, prepared), reply.enqueue,
                    force_pool=self.force_pool())
            else:
                request = self.fetcher.network_request(self.carrier.url, prepared, self.load, self.source)
                transport = self.source.request_async(self.fetcher.page.services.task_services,
                                                      request, reply.enqueue)
            self.bind(transport, stage)
        except control_exceptions():
            raise
        except BaseException as error:
            self.fail(error)

    def landed(self, response, error):
        if not self.active():
            return
        if error is not None:
            self.deliver(None, error)
            return
        try:
            delivered = self.fetcher.step(response, self.load, self.carrier, self.follows)
            if delivered is None:
                self.send()
                return
            if not self.active():
                return
            self.fetcher.store(self.load, delivered)
        except control_exceptions():
            raise
        except BaseException as error:
            self.deliver(None, error)
        else:
            self.deliver(delivered, None)


class _HopReply:
    __slots__ = ("owner", "target", "stage", "answered")

    def __init__(self, owner, target, stage):
        self.owner, self.target, self.stage = owner, target, stage
        self.answered = False

    def enqueue(self, value, error):
        self.owner.enqueue(self.receive, value, error)

    def receive(self, value, error):
        owner = self.owner
        if self.answered or self.stage != owner.stage:
            return
        self.answered = True
        if not owner.active():
            return
        try:
            response = None if error else owner.fetcher.convert_response(
                value, self.target, loader=owner.kind == "loader")
        except control_exceptions():
            raise
        except BaseException as error:
            owner.landed(None, error)
        else:
            owner.landed(response, error)
