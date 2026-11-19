"""Platform-owned service objects handed to web-facing interface code.

``PageServices`` assembles the six capability facets web code consumes; web
packages reach platform functionality only through them, never through the Page.
"""

import time
from functools import partial
from dataclasses import dataclass
from typing import Any

from ..foundation.clock import Clock
from ..foundation.tasks import TaskSource
from ..foundation.urls import origin_string, serialize_origin
from .resources.request_headers import same_site
from .resources.request_headers import ACCEPT_ENCODING, accept_language
from .sessions import BindingSession
from .state import Ownership


@dataclass(frozen=True)
class WorkerContext:
    """Everything a worker thread needs from its parent page."""

    browser: Any
    session: Any
    bindings: Any
    environment: Any
    trace: bool
    trace_scripts: tuple[str, ...] | None
    expose_unimplemented: bool
    stub_logging: str
    devtools: Any | None
    debug_parent: Any | None
    creator_origin: tuple
    resource_session: Any
    cross_origin_isolated: bool = False


class _TaskSource:
    """Task-queue access shared by the capabilities that post to the loop.

    A worker realm drives its own queue with the same capabilities a page does.
    """

    def __init__(self, tasks):
        self._tasks = tasks

    @property
    def closed(self):
        return self._tasks.closed

    def post_task(self, callback, source=TaskSource.DOM, *, label=None, document_bound=False):
        self._tasks.post_task(callback, source, label=label, document_bound=document_bound)


class _PerformanceCapability:
    """What this realm measured, as records the web layer turns into entries.

    The platform times a document and the resources it fetched, and knows nothing about
    a ``PerformanceEntry``: a record is a plain dict, and the entry the web layer builds
    from it reads that dict live, so a navigation still growing reports what it reached.
    """

    def __init__(self):
        self._records = []
        self._listeners = []
        self.event_counts = {}
        self.clock = Clock()

    def begin_navigation(self, url, redirects=0, redirects_span=0.0, *, started_at=None):
        """Start a new document's timeline: its navigation record, and nothing else.

        *redirects* is how many hops the document's own request followed and
        *redirects_span* how long they took, which the record reports as the window
        before the document starts; the hops lie before this timeline's origin.
        """
        self.reset()
        self.clock.reset(monotonic=started_at)
        received_at = self.clock.now_ms()
        self.record("visibility-state", name="visible", start=0.0)
        #: The request was issued when the timeline began, and the response was read by
        #: the time this document commits: the phases in between are the network's.
        return self.record("navigation", name=str(url), start=0.0,
                           fetchStart=0.0, domainLookupStart=0.0, domainLookupEnd=0.0,
                           connectStart=0.0, connectEnd=0.0, requestStart=0.0,
                           responseStart=received_at, responseEnd=received_at,
                           domLoading=received_at,
                           redirectCount=int(redirects),
                           redirectStart=0.0, redirectEnd=round(float(redirects_span), 3))

    def reset(self):
        del self._records[:]
        del self._listeners[:]
        self.event_counts.clear()

    def record(self, kind, **facts):
        record = {"kind": kind, **facts}
        self._records.append(record)
        for listener in tuple(self._listeners):
            listener()
        return record

    def note(self, **milestones):
        """Update the document's own record with the milestones it reached."""
        record = self.navigation()
        if record is not None:
            record.update(milestones)

    def records(self, kind=""):
        if not kind:
            return tuple(self._records)
        return tuple(record for record in self._records if record["kind"] == kind)

    def navigation(self):
        for record in self._records:
            if record["kind"] == "navigation":
                return record
        return None

    def clear_resources(self):
        self._records = [record for record in self._records if record["kind"] != "resource"]

    def subscribe(self, listener):
        """Ask to be told when a record lands, so entries can be delivered."""
        if listener not in self._listeners:
            self._listeners.append(listener)


def parse_json_in(context, window, text):
    """V8's own ``JSON.parse``: the parse stays in the isolate, so ``null`` arrives as ``null``.

    A Python parse hands every value back across the boundary, where the conversion
    spells Python ``None`` as ``undefined`` -- a property holding ``null`` would come
    back missing -- and the failure would be the host parser's wording, not V8's.
    """
    if context is None or window is None:
        import json
        return json.loads(text)
    json_object = context.js_obj(window)["JSON"]
    return json_object["parse"]._invoke(json_object, text)


class _RealmCapabilityBase(_TaskSource):
    """Scripting and interface staging shared by page and worker realms."""

    def __init__(self, tasks, performance):
        super().__init__(tasks)
        #: The realm's timeline: what the platform timed for this document.
        self.performance = performance

    @property
    def _binding_session(self):
        raise NotImplementedError

    @property
    def _scripting(self):
        raise NotImplementedError

    @property
    def _json_window(self):
        return None

    @property
    def v8_context(self):
        raise NotImplementedError

    @property
    def url(self):
        raise NotImplementedError

    @property
    def origin(self):
        raise NotImplementedError

    def materialize_interface(self, name):
        return self._binding_session.materialize(name)

    def make_promise(self):
        return self._scripting.make_promise()

    def enqueue_microtask(self, callback):
        """Queue *callback* on the realm's V8 microtask queue.

        Promise reactions share it, which gives the one FIFO order the HTML
        specification asks for.
        """
        return self._scripting.enqueue_microtask(callback)

    def cleanup_after_callback(self):
        return self._cleanup_after_callback()

    def evaluate_script(self, source):
        return self._evaluate_script(source)

    def _evaluate_script(self, source):
        raise NotImplementedError

    def _cleanup_after_callback(self):
        raise NotImplementedError

    def promise_resolve(self, value):
        return self._scripting.promise_resolve(value)

    def parse_json(self, text):
        return parse_json_in(self.v8_context, self._json_window, text)

    def retain_object(self, value):
        return self._scripting.retain_object(value)

    def report_exception(self, exc):
        """Report an uncaught exception through this realm's task queue."""
        self._tasks.report_error(exc)

    def heap_statistics(self):
        """V8's heap numbers for this realm's isolate, in bytes."""
        v8_context = self.v8_context
        return v8_context.heap_statistics() if v8_context is not None else {}

    def memory_snapshot(self):
        from ..foundation.heap_memory import HeapMemoryCache
        cache = getattr(self, '_memory_cache', None)
        if cache is None:
            cache = self._memory_cache = HeapMemoryCache()
        return cache.snapshot(self.heap_statistics, precise=self.cross_origin_isolated)

    @property
    def cross_origin_isolated(self):
        return False


class _RealmCapability(_RealmCapabilityBase):
    """Realm identity, scripting and task posting (implements RealmServices)."""

    def __init__(self, owner):
        super().__init__(owner._tasks, _PerformanceCapability())
        self.performance.record("visibility-state", name="visible", start=0.0)
        self._owner = owner

    @property
    def _binding_session(self):
        return self._owner._page_realm.binding_session

    @property
    def _scripting(self):
        return self._owner._scripts

    @property
    def _json_window(self):
        return self._owner.window

    def _evaluate_script(self, source):
        return self._owner._scripts.evaluate(source)

    def _cleanup_after_callback(self):
        self._owner._page_realm.executor.after_callback()

    @property
    def url(self):
        return self._owner.window.location.href

    @property
    def environment(self):
        return self._owner.environment

    @property
    def origin(self):
        return self._owner._origin

    @property
    def v8_context(self):
        return getattr(self._owner._page_realm, "v8_context", None)

    @property
    def cross_origin_isolated(self):
        return self._owner._navigation.cross_origin_isolated

    def memory_snapshot(self):
        cache = self._owner._page_realm.page.execution_group.heap_memory
        return cache.snapshot(self.heap_statistics, precise=self.cross_origin_isolated)

    def ancestor_origins(self):
        """The origins of this realm's ancestors, nearest first.

        This is what ``Location.ancestorOrigins`` reports, so the chain is walked from
        the parent outwards.
        """
        origins = []
        page = getattr(getattr(self._owner, "_page_realm", None), "page", None)
        page = getattr(page, "parent_page", None)
        while page is not None:
            origins.append(serialize_origin(page.origin))
            page = getattr(page, "parent_page", None)
        return origins

    def interface_class(self, name):
        """The class this realm bound for *name*, or ``None`` when the baseline
        declares no such interface.  A foreign-namespace element is built from it, so
        the SVG and MathML interface modules need not exist to answer ``instanceof``.
        """
        session = self._owner._page_realm.binding_session
        if name not in session.bindings.idl.interfaces:
            return None
        return session.materialize(name)

    def window_property(self, operation, name, value=None):
        """Operate on a same-origin Window in its owning realm."""
        realm = self._owner._page_realm
        def apply():
            target = realm.v8_context.js_obj(self._owner.window)
            if operation == "get":
                try:
                    return target[name]
                except (KeyError, AttributeError):
                    # Reading a JavaScript object by key raises KeyError; only
                    # the attribute form raises AttributeError.
                    from pyv8c import pyv8
                    return pyv8.Undefined
            if operation == "set":
                target[name] = value
            elif operation == "delete":
                del target[name]
            elif operation == "query":
                return 0 if name in dir(target) else None
        return realm.executor.call(apply)


class _TaskCapability(_TaskSource):
    """Event-loop task sources (implements TaskServices)."""

    def __init__(self, tasks, frame_factory):
        super().__init__(tasks)
        self._frame_factory = frame_factory
        self._frames = None

    def request_animation_frame(self, callback):
        self.prepare_animation_frames()
        return self._frames.request(callback)

    def prepare_animation_frames(self):
        if self._frames is None:
            self._frames = self._frame_factory()

    def cancel_animation_frame(self, ident):
        if self._frames is not None:
            self._frames.cancel(ident)

    def cancel_animation_frames(self):
        if self._frames is not None:
            self._frames.close()
            self._frames = None

    def schedule(self, callback, delay, repeat=False):
        return self._tasks.schedule(callback, delay, repeat)

    def schedule_html_timer(self, callback, delay, repeat=False):
        return self._tasks.schedule_html_timer(callback, delay, repeat)

    def cancel(self, ident):
        self._tasks.cancel(ident)

    def begin_network(self):
        self._tasks.begin_network()

    def complete_network(self):
        self._tasks.complete_network()

    def begin_worker(self):
        self._tasks.begin_worker()

    def complete_worker(self):
        self._tasks.complete_worker()

    def begin_loader(self):
        self._tasks.begin_loader()

    def complete_loader(self):
        self._tasks.complete_loader()


class WorkerRealmServices:
    """The capability facets a worker hands to its own global scope.

    A worker realm owns its task queue and its V8 context, so timers behave as
    they do on a page; what the scope needs from its parent travels separately.
    """

    def __init__(self, tasks, runtime):
        from .resources.worker import WorkerResourceCapability
        self.graphics_backend = getattr(getattr(runtime.config, 'browser', None), '_graphics_backend', None)
        self.task_services = _TaskCapability(tasks, runtime.animation_frame_lane)
        runtime._resource_scope.add(self.task_services.cancel_animation_frames)
        self.realm_services = _WorkerRealmCapability(tasks, runtime)
        self.resource_services = WorkerResourceCapability(runtime)


class _WorkerRealmCapability(_RealmCapabilityBase):
    """Worker scripting: the worker's own context, with no page behind it."""

    def __init__(self, tasks, runtime):
        super().__init__(tasks, _PerformanceCapability())
        self._runtime = runtime

    @property
    def _binding_session(self):
        return self._runtime.config.session

    @property
    def _scripting(self):
        return self._runtime

    def _evaluate_script(self, source):
        return self._runtime.evaluate_script(source)

    def _cleanup_after_callback(self):
        self._runtime.cleanup_after_callback()

    @property
    def v8_context(self):
        return self._runtime.v8_context

    @property
    def url(self):
        return self._runtime.script_url

    @property
    def cross_origin_isolated(self):
        return self._runtime.config.cross_origin_isolated

    @property
    def origin(self):
        return self._runtime.services.resource_services.origin


class _ResourceCapability:
    """Resource loading and ownership (implements ResourceServices)."""

    def __init__(self, owner):
        self._owner = owner

    def file_system_key(self):
        page = self._owner._page_realm.page
        return (tuple(page.top_page.origin), tuple(self._owner._origin))

    def file_system_state(self, factory):
        return self._owner._profile.file_system_state(self.file_system_key(), factory)

    def load_resource(self, url, request=None):
        return self._owner._resource_fetcher.load_resource(url, request)

    def load_resource_async(self, url, request, callback):
        return self._owner._resource_fetcher.load_resource_async(url, request, callback)

    def load_iframe(self, iframe):
        return self._owner._frame_resources.load_iframe(iframe)

    def ensure_iframe_context(self, iframe):
        return self._owner._frame_resources.ensure_iframe_context(iframe)

    def discard_iframe_context(self, iframe):
        return self._owner._frame_resources.discard_iframe_context(iframe)

    def load_iframe_markup(self, iframe, markup):
        return self._owner._frame_resources.load_iframe_markup(iframe, markup)

    def load_worker_source_async(self, script_url, callback):
        return self._owner._resource_fetcher.load_worker_source_async(script_url, callback)

    def discover_element_resource(self, node):
        return self._owner._document_resources.discover_element_resource(node)

    def restart_element_resource(self, node):
        return self._owner._document_resources.restart_element_resource(node)



class _DocumentCapability:
    """Document, window and parser hooks (implements DocumentServices)."""

    def __init__(self, owner):
        self._owner = owner

    @property
    def window(self):
        return self._owner.window

    @property
    def document(self):
        return self._owner.document

    def element_class(self, tag):
        return self._owner._navigation.element_class(tag)

    def install_inline_handler(self, node, name, source):
        return self._owner._scripts.install_inline_handler(node, name, source)

    def parser_script_start(self, node):
        return self._owner._navigation.parser_script_start(node)

    def add_content_security_policy(self, text):
        self._owner._navigation.content_policy.add(text)
        factory = self._owner.window._window_value('trustedTypes')
        self._owner._page_realm.v8_context.set_code_generation_handler(factory.code_generation)

    def parser_script_complete(self, node, source):
        return self._owner._navigation.parser_script_complete(node, source)

    def parser_is_blocked(self):
        return self._owner._navigation.parser_is_blocked()

    def write_markup(self, markup):
        return self._owner._navigation.write_markup(markup)

    def open_document(self):
        return self._owner._navigation.open_document()

    def close_document(self):
        return self._owner._navigation.close_document()

    def motion_event_listener(self, event_type):
        page = getattr(getattr(self._owner, "_page_realm", None), "page", None)
        if page is not None:
            page.lifecycle.motion_listener_added(str(event_type))

    def notify_user_activation(self):
        """Notify the active window, ancestors and same-origin descendants."""
        page = getattr(getattr(self._owner, "_page_realm", None), "page", None)
        if (page is None or page.closed or page.window is not self.window
                or page.document is not self.document):
            return
        now = time.monotonic()
        current = page
        while current is not None:
            current.window._last_activation_time = now
            current = current.parent_page
        pending = list(page._owned.frames)
        while pending:
            child = pending.pop()
            if child.closed:
                continue
            pending.extend(child._owned.frames)
            if child.origin == page.origin:
                child.window._last_activation_time = now

    def node_adopted(self, node):
        if getattr(node, "localName", "") == "iframe":
            if not node._is_connected():
                return None
            # The initial about:blank exists in the inserting turn, even inside an
            # inline script. PageRealm enters a shared isolate on this thread;
            # only the requested document's navigation is deferred.
            self._owner._frame_resources.ensure_iframe_context(node)
            if (node.get_src()
                    and not getattr(node, "_resource_started", False)):
                self._owner._document_resources.discover_element_resource(node)
            return None
        return self._owner._scripts.node_adopted(node)

    def request_navigation(self, url, **options):
        return self._owner._navigation.request_navigation(url, **options)

    @property
    def parsing(self):
        return self._owner._navigation.parsing

    def has_storage_access(self):
        """Default, ungranted access: a secure first-party ancestry has access.

        Cross-site documents have no Storage Access permission grant in this
        runtime. Use the same site calculation as the network request headers.
        """
        from ..web.bom.utils import page_secure_context

        page = self._owner._page_realm.page
        origin = serialize_origin(page.origin)
        if origin == "null" or not page_secure_context(page):
            return False
        return all(same_site(origin, ancestor)
                   for ancestor in self._owner.realm_services.ancestor_origins())



class _WorkerCapability:
    """Worker spawning and tracking (implements WorkerServices)."""

    def __init__(self, owner):
        self._owner = owner

    def worker_configuration(self):
        from .resources.session import ResourceRequestSession
        browser = self._owner._profile.browser
        site = serialize_origin(self._owner._origin)
        if any(not same_site(site, ancestor) for ancestor in self._owner.realm_services.ancestor_origins()):
            site = 'null'
        session = BindingSession(browser.bindings, self._owner)
        return WorkerContext(
            browser=browser,
            session=session,
            bindings=browser.bindings,
            environment=self._owner.environment,
            trace=bool(self._owner.trace),
            trace_scripts=self._owner._page_realm.page.trace_scripts,
            expose_unimplemented=browser.expose_unimplemented,
            stub_logging=browser.stub_logging,
            devtools=self._owner._profile._devtools,
            debug_parent=self._owner._page_realm.page._devtools_target,
            creator_origin=tuple(self._owner._origin),
            cross_origin_isolated=self._owner.realm_services.cross_origin_isolated,
            resource_session=ResourceRequestSession(
                profile=self._owner._profile, environment=self._owner.environment,
                origin=tuple(self._owner._origin), base_url=self._owner.realm_services.url,
                loader=browser.resource_loader, interceptor=browser.resource_interceptor,
                backend=self._owner._profile.network_backend, cache_enabled=browser.cache_enabled,
                site_for_cookies=site, file_system_key=self._owner.resource_services.file_system_key(),),
        )

    def create_worker_realm(self, config):
        from .realms import WorkerRealm
        return WorkerRealm(self._owner, config)

    def track_worker(self, worker):
        self._owner._ownership.track("workers", worker)
        self._owner._register_owned("workers", worker, worker.fn_terminate)

    def discard_worker(self, worker):
        self._owner._ownership.discard("workers", worker)
        self._owner._unregister_owned("workers", worker)


class _ContextCapability:
    """Profile-level services: blobs, cookies, sockets (implements ContextServices)."""

    def __init__(self, owner):
        self._owner = owner

    def create_blob_url(self, blob, realm=None):
        return self._owner._profile.create_blob_url(blob, realm=realm)

    def revoke_blob_url(self, url):
        self._owner._profile.revoke_blob_url(url)

    def resolve_blob_url(self, url):
        return self._owner._profile.resolve_blob_url(url)

    @property
    def cookie_jar(self):
        return self._owner._profile.cookies

    @property
    def _databases(self):
        return self._owner._profile._databases

    @property
    def network_available(self):
        return self._owner._profile.network_backend is not None

    @property
    def http_available(self):
        backend = self._owner._profile.network_backend
        return backend is not None and backend.http_available

    @property
    def ws_available(self):
        backend = self._owner._profile.network_backend
        return backend is not None and backend.ws_available

    def websocket(self, url, protocols, callbacks):
        backend = self._owner._profile.network_backend
        if backend is None:
            return None
        return backend.websocket(
            self._owner.task_services, url, protocols, callbacks, self._handshake(),
        )

    def _handshake(self):
        """What the page contributes to a WebSocket handshake; Chrome sends no client
        hint or ``Sec-Fetch-*`` on one, and the engine owns the ``Sec-WebSocket-*``."""
        environment = self._owner.environment
        options = {
            "headers": {
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
                "User-Agent": environment.user_agent,
                "Accept-Encoding": ACCEPT_ENCODING,
                "Accept-Language": accept_language(environment.languages),
            },
        }
        origin = origin_string(self._owner.realm_services.url)
        if origin != "null":
            options["origin"] = origin
        site = serialize_origin(self._owner._origin)
        if any(not same_site(site, ancestor) for ancestor in self._owner.realm_services.ancestor_origins()):
            site = 'null'
        options['site_for_cookies'] = site
        return options

    def close_websocket(self, websocket):
        backend = self._owner._profile.network_backend
        if backend is not None:
            backend.close_websocket(websocket)

    def track_websocket(self, websocket):
        self._owner._ownership.track("websockets", websocket)
        self._owner._register_owned(
            "websockets", websocket,
            lambda ws=websocket: self.close_websocket(ws),
        )

    def discard_websocket(self, websocket):
        self._owner._ownership.discard("websockets", websocket)
        self._owner._unregister_owned("websockets", websocket)

    def discard_frame(self, frame):
        self._owner._ownership.discard("frames", frame)
        self._owner._unregister_owned("frames", frame)

    def track_frame(self, frame):
        self._owner._ownership.track("frames", frame)
        self._owner._register_owned("frames", frame, frame.close)
        try:
            frame._parent_scope_cleanup = lambda child=frame: self.discard_frame(child)
        except AttributeError:
            pass


class PageServices:
    """Composition root assembling the six realm capabilities.

    Holds controllers, the owning profile and late-bound window/document
    references -- never the Page itself, which web code cannot reach through.
    """

    def __init__(self, profile, environment, trace, ownership: Ownership,
                 navigation, scripts, resource_fetcher, frame_resources, document_resources, tasks, scope):
        self._profile = profile
        self.graphics_backend = profile.browser._graphics_backend
        self.environment = environment
        self.trace = trace
        self._ownership = ownership
        # The exact callback object is kept, so a resource that completes
        # normally can unregister instead of leaving a closure behind.
        self._registrations = {}
        self._navigation = navigation
        self._scripts = scripts
        self._resource_fetcher = resource_fetcher
        self._frame_resources = frame_resources
        self._document_resources = document_resources
        self._tasks = tasks
        # Held directly: the task queue lives in foundation and must not be a
        # back door to the Page.
        self._scope = scope
        # Late-bound by Page once window/document/realm exist.
        self.window = None
        self.document = None
        self._page_realm = None
        self._origin = ("opaque", object())
        #: Granted ``sandbox`` tokens, or ``None`` when the realm is not sandboxed.
        self.sandbox_tokens = None

        # Narrow capabilities handed to web packages.
        self.realm_services = _RealmCapability(self)
        self.task_services = _TaskCapability(tasks, self._animation_frame_lane)
        scope.add(self.task_services.cancel_animation_frames)
        self.resource_services = _ResourceCapability(self)
        self.document_services = _DocumentCapability(self)
        self.worker_services = _WorkerCapability(self)
        self.context_services = _ContextCapability(self)

    def _register_owned(self, kind, resource, callback):
        key = (kind, id(resource))
        previous = self._registrations.get(key)
        if previous is not None:
            return previous[1]
        if self._scope.add(callback):
            self._registrations[key] = (resource, callback)
        return callback

    def _unregister_owned(self, kind, resource):
        key = (kind, id(resource))
        registration = self._registrations.pop(key, None)
        if registration is not None:
            self._scope.remove(registration[1])

    def bind_realm(self, page_realm):
        self._page_realm = page_realm

    def _animation_frame_lane(self):
        realm = self._page_realm
        page = realm.page
        def active():
            current = page
            while current is not None:
                if current.closed or current.document.hidden:
                    return False
                frame = current.window._frame_element
                if frame is not None and (not frame._is_connected()
                                          or frame._child_services is not current.services):
                    return False
                current = current.parent_page
            return self._page_realm is realm
        def order():
            branches = []
            current = page
            while current.parent_page is not None:
                node = current.window._frame_element
                path = []
                while node is not None and node.parentNode is not None:
                    path.append(node.parentNode._children.index(node))
                    node = node.parentNode
                branches.append(tuple(reversed(path)))
                current = current.parent_page
            return tuple(reversed(branches))
        return page.execution_group.rendering.add(
            invoke=partial(realm.executor.invoke, _background=True), active=active,
            timestamp=self.realm_services.performance.clock.at,
            report_error=self.realm_services.report_exception,
            order=order, owner=realm.executor)

    def parent_window(self):
        """The window of the page this one is framed by; ``None`` at the top."""
        page = self._realm_page()
        parent = None if page is None else page.parent_page
        return None if parent is None else parent.window

    def top_window(self):
        """The window of this page's top-level page; ``None`` when it is that page."""
        page = self._realm_page()
        top = None if page is None else page.top_page
        return None if top is None or top is page else top.window

    def _realm_page(self):
        realm = self._page_realm
        return None if realm is None else realm.page

    @property
    def global_proxy(self):
        """The window object this realm hands out, the same one across navigations."""
        page = self._realm_page()
        return None if page is None else page.global_proxy


__all__ = ["PageServices", "WorkerContext", "WorkerRealmServices"]
