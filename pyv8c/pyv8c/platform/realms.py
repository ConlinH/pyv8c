"""Platform-owned V8 realms for pages and workers."""

import json
import time

from pyv8c import pyv8


from ..engine.trace import log_trace
from ..foundation import promises
from ..foundation.lifecycle import ResourceScope
from .resources.ownership import ResourceOwnership
from ..foundation.tasks import TaskQueue, TaskSource
from ..foundation.agent_tasks import TaskContext
from ..foundation.rejections import RejectionTracker
from ..foundation.rejection_state import WeakPromiseState
from ..foundation.tracing import silent_trace
from ..web.bom.chrome import install_window_chrome
from ..web.bom.legacy_factories import register_legacy_factories
from .services import WorkerRealmServices
from .sessions import BindingSession


def _retain_object(realm, value):
    context = realm.v8_context
    if context is not None:
        roots = getattr(realm, '_object_roots', None)
        if roots is None:
            # Context.js_obj deliberately returns a weak view of a host pair.
            # A JS object is instead a strong, realm-owned edge to its wrappers.
            roots = realm._object_roots = context.to_js({})
            realm._object_root_ids = set()
        identities = realm._object_root_ids
        if id(value) not in identities:
            roots[str(id(value))] = value
            identities.add(id(value))
    return value


def _release_objects(realm):
    realm._object_roots = None
    getattr(realm, '_object_root_ids', set()).clear()


def _v8_context_kwargs(session, context_type, trace, on_materialize=None, trace_callback=None,
                       traceable=False, trace_scripts=None, exposure_type=None, isolate=None,
                       reuse_global=None, ordered_global=False):
    """Shared native V8Context arguments assembled from one binding session.

    ``isolate`` and ``reuse_global`` go in only when the caller has one: the worker
    path calls ``Isolate.context()``, which takes its isolate from the receiver and
    carries no window across navigations.
    """
    kwargs = {
        "trace": trace,
        "traceable": traceable,
        "trace_scripts": trace_scripts,
        "trace_callback": (log_trace if trace_callback is None else trace_callback) if trace or traceable else None,
        "ctx_type": context_type,
        "lazy_factory": session.lazy_factory(exposure_type or context_type, on_materialize=on_materialize),
    }
    if isolate is not None:
        kwargs["isolate"] = isolate
    if reuse_global is not None:
        kwargs["reuse_global"] = reuse_global
    if ordered_global:
        kwargs["ordered_global"] = True
    return kwargs


def _ordered_window_items(session, plan, factories, aliases):
    """Combine the Window members and exposed constructors into one order."""
    spec = session.class_config_for("Window")
    items = {}
    for group, kind in ((spec.attributes, "attribute"), (spec.methods, "method")):
        for member in group:
            if member.location == 0:
                items.setdefault(member.name, kind)
    for name in (*factories, *aliases, *plan.installs):
        items.setdefault(name, "constructor")
    items["chrome"] = "host"
    observed = session.bindings.idl.observed_window_order or ()
    for name in ("Temporal", "WebAssembly"):
        if name in observed:
            items[name] = "builtin"
    rank = {name: index for index, name in enumerate(observed)}
    return tuple((kind, name) for name, kind in sorted(items.items(),
                 key=lambda item: rank.get(item[0], len(rank))))


class _RealmTaskHost:
    """The only two questions a worker's task queue may ask its realm.

    The adapter keeps ``foundation``'s queue from reaching the realm object.
    """

    def __init__(self, realm):
        self._realm = realm

    def is_closed(self):
        return self._realm.is_closed()

    def parser_blocked(self):
        # A worker has no parser to gate its task sources on.
        return False


def _publish_window_name(context, page):
    """Write the window's name as host work, so no trace event reports it."""
    with silent_trace(context):
        page.window._publish_name()


#: Names Chrome hides in a realm that is not cross-origin isolated: the engine hands
#: ``SharedArrayBuffer`` to every context, so such a realm takes it back.
_NON_ISOLATED_HIDDEN = ("SharedArrayBuffer",)


def _declare_realm_interfaces(context, *, context_type, bindings, isolated=False):
    """Declare the realm's plan; the realm answers each name itself."""
    plan = bindings.plan(context_type)
    context.install(plan.installs)
    for name in (() if isolated else _NON_ISOLATED_HIDDEN):
        context.hide_global(name)
    #: Every interface is still a lazy constructor after startup, including the
    #: ones the context installed while its template was built.
    return len(plan.installs)


class WorkerRealm:
    """Own the worker isolate, context, task queue and binding session."""

    def __init__(self, page, config):
        self.page = page
        self.config = config
        #: Written by the runtime that starts the worker; the realm only reads it.
        self.script_url = ""
        self.execution_deadline = None
        self.execution_ticket = None
        self.stop_event = None
        self.isolate = None
        self.v8_context = None
        self.scope = None
        self.scripts = None
        # A worker global owns its task queue and drives it from its own owner
        # thread, so timers, microtasks and posted messages run there.
        self.tasks = TaskQueue(_RealmTaskHost(self))
        self.tasks.set_task_context(TaskContext(self))
        self._resource_scope = ResourceScope()
        self.resource_ownership = ResourceOwnership(self._resource_scope)
        self.services = WorkerRealmServices(self.tasks, self)
        self.services.realm_services.performance.clock.resolution_ms = (
            0.005 if config.cross_origin_isolated else 0.1)
        self.callback_boundary = None

    def is_closed(self):
        return self.isolate is None

    def attach_rendering(self, executor, invoke):
        from ..foundation.rendering import RenderingScheduler
        self._rendering = RenderingScheduler(frame_rate=self.config.browser.frame_rate)
        self._frame_invoke = invoke
        executor.attach_rendering(self._rendering)
        self._resource_scope.add(self._rendering.close)

    def animation_frame_lane(self):
        return self._rendering.add(
            invoke=self._frame_invoke, active=lambda: not self.tasks.closed,
            timestamp=self.services.realm_services.performance.clock.at,
            report_error=self.services.realm_services.report_exception)

    def initialize(self, scope):
        self.scope = scope
        config = self.config
        session = config.session
        # Worker constructors and static operations must use the worker's own
        # services, including its microtask queue, not the creator page's.
        session.realm = self.services
        session.prepare("Worker")
        self.isolate = pyv8.Isolate(thread_bound=True)
        self.isolate.set_host_datetime(config.environment.timezone, config.environment.language)
        from ..web.fetch.response import wasm_response_payload
        self.isolate._set_wasm_response_handler(wasm_response_payload)
        self.microtasks = pyv8.MicrotaskQueue(self.isolate)
        self.v8_context = self.isolate.context(
            scope, microtask_queue=self.microtasks, **_v8_context_kwargs(session, "Worker", config.trace,
                                     trace_callback=config.browser.trace_callback,
                                     traceable=config.browser.traceable,
                                     trace_scripts=config.trace_scripts),
        )
        _declare_realm_interfaces(
            self.v8_context,
            context_type="Worker",
            bindings=config.bindings,
            isolated=config.cross_origin_isolated,
        )
        self.v8_context.enable_console()
        return self.v8_context

    # The worker's global scope reaches these through WorkerRealmServices, as a
    # page reaches its own through PageServices.  The realm owns the context, so
    # it is also what decides how a promise behaves once that context is gone.

    def evaluate_script(self, source):
        try:
            return self.v8_context.exec_js(source)
        finally:
            self.cleanup_after_callback()

    def cleanup_after_callback(self):
        if self.callback_boundary is not None:
            self.callback_boundary()
        elif self.v8_context is not None and pyv8.current_context(self.isolate) is None:
            self.microtasks.checkpoint()

    def enqueue_microtask(self, callback):
        return promises.enqueue_microtask(self.v8_context, callback)

    def make_promise(self):
        return promises.make_promise(self.v8_context)

    def promise_resolve(self, value):
        return promises.promise_resolve(self.v8_context, value)

    def retain_object(self, value):
        return _retain_object(self, value)

    def close(self):
        first_error = None

        def attempt(callback):
            nonlocal first_error
            try:
                callback()
                return True
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                return False

        attempt(self._resource_scope.close)
        attempt(self.tasks.close)
        _release_objects(self)
        if self.v8_context is not None and attempt(self.v8_context.close):
            self.v8_context = None
        attempt(self.config.session.detach)
        if self.v8_context is None:
            self.scope = None
        if self.v8_context is None and getattr(self, "microtasks", None) is not None:
            if attempt(self.microtasks.close):
                self.microtasks = None
        if self.isolate is not None and attempt(self.isolate.close):
            self.isolate = None
        if first_error is not None:
            raise first_error


class PageRealm:
    """Own the V8 context and binding session for one page."""

    def __init__(self, page, executor):
        self.page = page
        self.v8_context = None
        self.rejections = None
        self.http_driver = None
        self.executor = executor
        browser = page.profile.browser
        # Web objects built through this session receive PageServices, not the
        # platform Page.
        self.binding_session = BindingSession(browser.bindings, page.services)
        page.services.bind_realm(self)
        try:
            self.executor.call(self._initialize)
        except BaseException:
            # A stopped lane's invoke() may decline new work. Cleanup must still
            # run on the owner thread after a partially constructed V8 context.
            self.executor.group.executor.call(self._close_context)
            raise

    def _initialize(self):
        page = self.page
        browser = page.profile.browser
        # An iframe shares its parent's isolate, so its own environment must not
        # replace the time zone and language already installed for that isolate.
        if page.parent_page is None:
            page.execution_group.isolate.set_host_datetime(
                page.environment.timezone, page.environment.language)
        binding_started = time.perf_counter_ns()
        self.binding_session.prepare("Window")
        plan = self.binding_session.bindings.plan("Window")
        #: Legacy factories are registered before the context's lazy properties.
        factory_names = register_legacy_factories(
            self.binding_session, table=browser.bindings.idl, document=page.document)
        aliases = browser.bindings.idl.window_aliases()
        alias_names = tuple(
            alias for alias, target in aliases.items()
            if target in plan.installs)
        for alias in alias_names:
            self.binding_session.add_alias(alias, aliases[alias])
        #: Every alias target is built with the skeleton, as the old eager alias
        #: value was; only the alias name itself is now lazy.
        for target in aliases.values():
            if browser.bindings.describes(target):
                self.binding_session.materialize(target)
        ordered_items = _ordered_window_items(self.binding_session, plan, factory_names, alias_names)
        binding_prepared = time.perf_counter_ns()
        #: A frame's realm is not the top page's: the label says which it is.
        context_type = "FRAME" if page.parent_page is not None else "TOP"
        self.agent = page.execution_group.agent(page.origin)
        page.services.task_services.prepare_animation_frames()
        self.v8_context = pyv8.V8Context(page.window, **_v8_context_kwargs(
            self.binding_session, context_type, page.trace,
            exposure_type="Window",
            on_materialize=self._record_deferred_materialized,
            trace_callback=page.profile.trace_callback,
            traceable=page.profile.traceable,
            trace_scripts=page.trace_scripts,
            isolate=page.execution_group.isolate,
            reuse_global=page.global_proxy,
            ordered_global=True,
        ), microtask_queue=self.agent.microtasks)
        #: Kept so the next document is handed the same window object.
        page.global_proxy = self.v8_context.glob
        self.executor.attach_deadline_source(self.v8_context)
        self.executor.bind_agent(self.agent, TaskContext(self, page.document, page.navigation.generation))
        context_created = time.perf_counter_ns()
        with silent_trace(self.v8_context):
            existing = set(json.loads(self.v8_context.exec_js("JSON.stringify(Object.getOwnPropertyNames(globalThis))")))
            pending = [(kind, name) for kind, name in ordered_items
                       if (kind == "builtin" and name in existing) or name not in existing]
            if pending:
                self.v8_context.install_ordered(pending)
            install_window_chrome(self.v8_context, page.services)
            self.v8_context.enable_console()
            if page.navigation.content_policy.policies:
                factory = page.window._window_values.value('trustedTypes')
                self.v8_context.set_code_generation_handler(factory.code_generation)
            for name in (() if page.navigation.cross_origin_isolated else _NON_ISOLATED_HIDDEN):
                self.v8_context.hide_global(name)
        deferred_globals = len(plan.installs)
        deferred_declared_at = time.perf_counter_ns()
        _publish_window_name(self.v8_context, page)
        if browser.bindings.idl.observed_window_order is not None:
            with silent_trace(self.v8_context):
                names = self.v8_context.exec_js("JSON.stringify(Object.getOwnPropertyNames(globalThis))")
            browser.bindings.check_window_order(json.loads(names), (name for _, name in ordered_items))
        else:
            browser.bindings.check_window_order(())
        chrome_installed_at = time.perf_counter_ns()
        # The realm owns the context and the page's script controller borrows it,
        # so the isolate's error hooks and every Web API promise share one realm;
        # the hooks route exceptions V8 raises on its own.
        self.rejections = RejectionTracker(
            page.report_rejection,
            lambda callback: page.tasks.post(callback, TaskSource.DOM),
            WeakPromiseState(self.v8_context),
        )
        self.executor.attach_rejection_tracker(self.rejections)
        self.v8_context.set_error_hooks(
            page.report_uncaught_message, self.rejections.record,
        )
        browser._record_startup(
            binding_binder_ms=(binding_prepared - binding_started) / 1_000_000,
            profile_create_ms=(context_created - binding_prepared) / 1_000_000,
            deferred_global_declare_ms=(deferred_declared_at - context_created) / 1_000_000,
            chrome_install_ms=(chrome_installed_at - deferred_declared_at) / 1_000_000,
            core_interfaces=len(self.binding_session.materialized_interfaces),
            materialized_interfaces=len(self.binding_session.materialized_interfaces),
            known_interfaces=len(plan.installs),
            deferred_globals=deferred_globals,
        )

    def _record_deferred_materialized(self, name):
        """Keep startup stats in sync with on-demand interface materialisation."""
        self.page.profile.browser._record_startup(
            materialized_interfaces=len(self.binding_session.materialized_interfaces),
            deferred_materialized=len(self.binding_session.materialized_interfaces)
            - len(self.binding_session.bindings.plan("Window").ready),
        )

    def enqueue_microtask(self, callback):
        return promises.enqueue_microtask(self.v8_context, callback)

    def make_promise(self):
        return promises.make_promise(self.v8_context)

    def promise_resolve(self, value):
        return promises.promise_resolve(self.v8_context, value)

    def retain_object(self, value):
        return self.executor.call(_retain_object, self, value)

    def close(self):
        """Close the document-scoped parts: the context and the binding session.

        ``self.v8_context`` going to ``None`` is what tells a late caller -- a promise of the
        replaced document, a microtask of the old realm -- that there is nothing to settle.
        """
        if self.executor.closed:
            return
        self.executor.call(self._close_context)

    def _close_context(self):
        self.http_driver = None
        if self.rejections is not None:
            self.rejections.close()
        _release_objects(self)
        v8_context = self.v8_context
        try:
            close = getattr(v8_context, "close", None)
            if callable(close):
                close()
        finally:
            self.executor.detach_deadline_source(v8_context)
            self.v8_context = None
            self.binding_session.detach()


__all__ = ["PageRealm", "WorkerRealm"]
