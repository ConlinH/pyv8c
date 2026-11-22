"""The Page facade: one document, its controllers, and the API around them."""

from __future__ import annotations

import asyncio

import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING, Literal

from pyv8c import pyv8
from ..foundation.logging import logger
from ..foundation.futures import bridge_future, consume_future

from ..foundation.arguments import is_nullish
from ..foundation.host_errors import HostStateError, RealmBusyError
from ..foundation.error_policy import control_exceptions, is_host_failure
from ..foundation.lifecycle import ResourceScope
from ..foundation.background import BackgroundWork
from ..foundation.close_timing import close_phase
from ..foundation.tasks import TaskQueue
from ..foundation.urls import origin_tuple
from ..web.bom.window import Window
from ..web.dom.document import HTMLDocument
from ..web.dom.nodes import Node
from ..web.fetch.response import Response
from .lifecycle import DocumentLifecycle
from .navigation import NavigationController
from .net import PageNet
from .realms import PageRealm
from .execution import WindowExecutionGroup
from .resources.fetcher import ResourceFetchController
from .resources.frames import FrameResourceController
from .resources.documents import DocumentResourceController
from .resources.ownership import ResourceOwnership
from .scripting import ScriptController
from .services import PageServices
from .state import Ownership

if TYPE_CHECKING:
    from .input import MouseInput


EMPTY_DOCUMENT = "<!DOCTYPE html><html><head></head><body></body></html>"


def _deadline(timeout):
    return None if timeout is None else time.perf_counter() + max(0.0, float(timeout))


def _remaining(deadline):
    return None if deadline is None else max(0.0, deadline - time.perf_counter())


def _snapshot_data(value, seen):
    """Detach values before returning Python data across the owner boundary."""
    if type(value).__name__ in ('TypedArray', 'JSTypeArray'):
        return bytes(value._bytes())
    if isinstance(value, pyv8.JSObject):
        # Non-copyable JS values remain live handles after to_python(). Capture
        # their representation on the owner thread; evaluate_handle() preserves
        # the handle for callers that need to operate on it.
        return repr(value)
    if isinstance(value, (dict, list, tuple)):
        if id(value) in seen:
            return seen[id(value)]
        result = {} if isinstance(value, dict) else []
        seen[id(value)] = result
        if isinstance(value, dict):
            result.update((key, _snapshot_data(item, seen)) for key, item in value.items())
        else:
            result.extend(_snapshot_data(item, seen) for item in value)
        return result
    return value


class _TaskHostAdapter:
    """Minimal host surface handed to the page's TaskQueue.

    The queue may only ask its host the two TaskHost questions, so holding a
    ``Page`` there would let foundation reach the whole platform object.
    """

    def __init__(self, page):
        self._page = page

    def is_closed(self):
        return self._page.closed or bool(getattr(getattr(self._page, 'execution_group', None), 'stopping', False))

    def parser_blocked(self):
        return self._page.navigation.parser_is_blocked()


class _ContextBootstrapProxy:
    """Placeholder published while native V8Context installs templates."""

    def __init__(self):
        self.args = ()
        self.kw = {}

    def expose(self, *_args, **_kwargs):
        return None

    def exec_js(self, *_args, **_kwargs):
        return None

    def close(self):
        return None


class Page:
    """One document and the host API around it.

    A page owns a realm -- a native V8 context with its Window and Document -- and
    is the supported way to reach it:     :meth:`evaluate` runs script,
    :attr:`window` and :attr:`document` are the host-side handles, :attr:`net`
    is the network API (``get``/``post`` and ``fetch``/``xhr``, described on
    :class:`~pyv8c.platform.net.PageNet`), :attr:`mouse` moves the pointer and
    clicks (with :meth:`click` and :meth:`hover` for the common case), and
    :attr:`url`/:attr:`origin` describe the current document.  Whatever a
    script sees is served inside the realm.

    Navigating replaces that realm, so a JS handle taken before a :meth:`goto` or
    :meth:`set_content` must not be used after it.

    These methods and live JS handles belong to the application owner thread
    and reject calls from foreign threads (see
    ``docs/pyv8c/usage.md``).
    """

    def __init__(self, profile, url="about:blank", *, trace=None, trace_scripts=None,
                 environment=None, parent=None, origin=None):
        self._runtime = profile.browser._runtime
        self._runtime.check_owner()
        self.profile = profile
        self.parent_page = parent
        self.top_page = parent.top_page if parent is not None else self
        #: The frame tree's execution-group Isolate, shared by its frames.
        #: None is used only before group initialization and after release.
        self.isolate = None
        #: The window object of this browsing context, the same one across navigations.
        self.global_proxy = None
        #: Realms earlier documents ran in, kept open for the references a page holds.
        self._retired_realms = []
        self.origin = tuple(origin) if origin is not None else origin_tuple(
            url, inherited=getattr(parent, "origin", None)
        )
        self.origin = profile._blob_origins.get(str(url), self.origin)
        self._opaque_origin = origin is not None and self.origin[0] == "opaque"
        self.environment = profile.browser.environment.overlay(environment)
        self.resource_interceptor = profile.browser.resource_interceptor
        # Overriding the browser default here leaves the other pages' setting alone.
        self._trace = profile.trace if trace is None else bool(trace)
        self.trace_scripts = trace_scripts
        self.closed = False
        self._retiring = False
        self._resource_scope = ResourceScope()
        self._background = BackgroundWork()
        self._child_pages = set()
        self._close_task = None
        self.resource_ownership = ResourceOwnership(self._resource_scope)
        self._close_lock = threading.Lock()
        self._closing = False
        self.navigation = NavigationController(self)
        self.lifecycle = DocumentLifecycle(self)
        self.scripts = ScriptController(self)
        self.resource_fetcher = ResourceFetchController(self)
        self.frame_resources = FrameResourceController(self, self.resource_fetcher)
        self.document_resources = DocumentResourceController(self, self.resource_fetcher, self.frame_resources)
        self.tasks = TaskQueue(_TaskHostAdapter(self))
        # An uncaught exception must surface as an ErrorEvent on the window.
        self._reporting_error = False
        self.tasks.set_error_sink(self.report_exception)
        self._owned = Ownership()
        # Web-facing objects see this narrow surface, never the Page itself.
        self.services = PageServices(
            profile, self.environment, self.trace, self._owned,
            self.navigation, self.scripts, self.resource_fetcher, self.frame_resources, self.document_resources, self.tasks,
            self._resource_scope, self._background,
        )
        self.services._origin = self.origin
        self.document = HTMLDocument(services=self.services, url=url, ready_state="complete")
        self.resource_loader = profile.browser.resource_loader
        self.cache_enabled = profile.browser.cache_enabled
        self.window = Window(
            services=self.services,
            feature_runtime=profile.browser.bindings.runtime,
        )
        self.services.window = self.window
        self.window._content_policy = self.navigation.content_policy
        self.services.document = self.document
        self.window.location.href = str(url)
        self.net = PageNet(self)
        # Built on first use: the module reaches the layout model, which would
        # drag font measurement into a startup that does not need it yet.
        self._mouse = None
        # Native template installation can inspect the global object before
        # V8Context.__init__ returns, so publish a sentinel until the realm exists.
        self.v8_context = _ContextBootstrapProxy()
        self._devtools_target = None
        #: One owner thread for the page's lifetime; navigations hand it to the
        #: next realm instead of starting another.
        self.execution_group = (parent.execution_group if parent is not None else
                                self._create_execution_group())
        self.isolate = self.execution_group.isolate
        self.executor = self.execution_group.endpoint(self.tasks)
        try:
            self.realm = PageRealm(self, executor=self.executor)
            self.services.bind_realm(self.realm)
            self.realm.executor.attach_rejection_tracker(self.realm.rejections)
            self.v8_context = self.realm.v8_context
            Window._bind_context(self.v8_context, self.window)
            if parent is None:
                self.executor.call(self.navigation.initialize_empty_document)
        except BaseException:
            if getattr(self, "realm", None) is not None:
                try:
                    self._retire()
                except BaseException:
                    with profile._state_lock:
                        profile.pages.append(self)
                    raise
            else:
                self.tasks.close()
                self.execution_group.executor.call(self.executor.close)
            raise
        if parent is not None:
            parent._child_pages.add(self)

    @property
    def trace(self) -> bool:
        """Whether trace events are reported for this page."""
        return self._trace

    def _create_execution_group(self):
        return self._runtime.create_execution_group(self.profile.browser.frame_rate)


    @trace.setter
    def trace(self, enabled: bool) -> None:
        """Switch tracing for this page.

        :param enabled: whether trace events should be reported.
        :raises TypeError: *enabled* is not a ``bool``.
        :raises RuntimeError: the Browser was not created traceable (the native
            context refuses the switch), or the page is already closed.
        """
        self._check()
        if not isinstance(enabled, bool):
            raise TypeError("trace must be a bool")

        def update():
            if self.closed or self.profile.closed:
                raise RuntimeError("page is closed")
            self.v8_context.trace = enabled
            self._trace = enabled
            self.services.trace = enabled

        self.realm.executor.call(update)

    def _update_origin(self, url):
        if self._opaque_origin:
            return
        if str(url) == self.url and self.origin[0] == "opaque":
            return
        self.origin = origin_tuple(url, inherited=getattr(self.parent_page, "origin", None))
        self.origin = self.profile._blob_origins.get(str(url), self.origin)
        self.services._origin = self.origin
        self.window._origin = self.origin
        # The native access-check callback resolves the accessing realm from this
        # table, and navigation can change the origin after the V8Context exists.
        entry = Window._context_origins.get(id(self.v8_context))
        if entry is not None:
            Window._context_origins[id(self.v8_context)] = (entry[0], self.origin)

    # One entry point for everything that notices an exception -- a task that
    # raised, a listener that threw, an isolate hook -- so all of them surface on
    # the window as an ``error`` event instead of vanishing.
    def report_exception(self, error=None, *, message=None, filename="", line=0,
                         column=0, value=None):
        """Mirror an uncaught exception onto the realm's window.

        Either a Python *error*, which brings its own text and (usually) the
        JavaScript value behind it, or -- for an exception that never reached
        Python -- the *message*, position and thrown *value* V8 captured.

        The guard keeps a page handler that throws while handling this from
        re-entering the reporter.
        """
        executor = getattr(self, 'executor', None)
        if executor is not None and not executor.is_owner_thread():
            return executor.call(self.report_exception, error, message=message,
                                 filename=filename, line=line, column=column, value=value)
        if self.closed or self._reporting_error:
            return
        #: A script error is the page's; a failure of the host's own scheduling is
        #: not, so the window hears nothing and the reason stays in the log.
        if error is not None and is_host_failure(error):
            logger.warning("the host could not finish: {}", error)
            return
        self._reporting_error = True
        try:
            self.lifecycle.dispatch_script_error(
                None, error, message=message, filename=filename, line=line,
                column=column, value=value,
            )
        except control_exceptions():
            raise
        except BaseException:  # reporting a script error must not raise
            logger.debug("failed to report an exception", exc_info=True)
        finally:
            self._reporting_error = False

    def report_uncaught_message(self, message, source, line, column, error):
        """Report the pieces of an exception V8 raised on its own.

        This is the isolate's uncaught-exception hook: a microtask job that
        threw never returns to a Python frame, so V8 hands over the message, the
        throw site and the exception value it captured.
        """
        if is_nullish(source):
            source = ""
        self.report_exception(
            message=str(message), filename=str(source), line=int(line),
            column=int(column), value=error,
        )

    def report_rejection(self, event_type, promise, reason):
        """Mirror a rejection notice onto the window's rejection events."""
        if self.closed:
            return
        try:
            self.lifecycle.dispatch_rejection(event_type, promise, reason)
        except control_exceptions():
            raise
        except BaseException:  # reporting a rejection must not raise
            logger.debug("failed to report a rejection", exc_info=True)

    @property
    def binding_session(self):
        return self.realm.binding_session

    @property
    def mouse(self) -> MouseInput:
        """The page's mouse: position, buttons and the events a move produces.

        Built on first read -- see :class:`~pyv8c.platform.input.MouseInput` --
        and shared for the life of the page, so the position carries over from
        one move to the next.
        """
        self._check()
        if self._mouse is None:
            from .input import MouseInput
            self._mouse = MouseInput(self)
        return self._mouse

    def _window_name_of(self, previous_window, previous_realm):
        """The browsing context's name, which a page may have set from script."""
        v8_context = previous_realm.v8_context
        if v8_context is not None:
            try:
                return str(v8_context.exec_js("String(this.name)"))
            except BaseException:
                pass
        return getattr(previous_window, "_window_name", "")

    def _retire_document_workers(self):
        for worker in tuple(self._owned.workers):
            worker.fn_terminate()

    def _renew_document_realm(self, url):
        """Give the page the Window and realm a new document comes with.

        Only browsing-context state comes along; a new realm is a new global
        object, so the last document's properties go with the last document.
        """
        previous_window, previous_realm = self.window, self.realm
        previous_proxy = self.global_proxy
        previous_realm.rejections.close()
        window = Window(
            services=self.services,
            feature_runtime=self.profile.browser.bindings.runtime,
        )
        window._frame_element = previous_window._frame_element
        window._window_name = self._window_name_of(previous_window, previous_realm)
        for slot in ("history", "localStorage", "sessionStorage"):
            if slot in previous_window._features.cached_names:
                window._features._cache(slot, previous_window._features.value(slot))
        self.window = window
        window._content_policy = self.navigation.content_policy
        self.services.window = window
        try:
            new_realm = PageRealm(self, executor=self.executor)
        except BaseException:
            self.window = previous_window
            self.services.window = previous_window
            self.services.bind_realm(previous_realm)
            self.global_proxy = previous_proxy
            raise
        self.realm = new_realm
        self.services.bind_realm(self.realm)
        # Existing resolvers retain their original context; new promises must
        # be created in the committed realm rather than the previous document.
        self.scripts.clear_js_helpers()
        self.realm.executor.attach_rejection_tracker(self.realm.rejections)
        self.v8_context = self.realm.v8_context
        Window._bind_context(self.v8_context, window)
        if self._devtools_target is not None:
            self._devtools_target.rebind_context(self.v8_context)
        Window._unbind_context(previous_realm.v8_context)
        previous_realm.http_driver = None
        #: Kept for the references a page holds; retired with the page (``_close_owner``).
        if previous_realm.v8_context is not None:
            previous_realm.v8_context.detach()
        self._retired_realms.append(previous_realm)
        return window

    @property
    def url(self):
        self._runtime.check_owner(allow_closing=True)
        return self.window.location.href


    @url.setter
    def url(self, value):
        raise HostStateError("navigation is awaited; use await page.goto(url)")



    # Web-facing code is served by ``self.services``; controllers reach each
    # other through the Page attributes, so only user-facing forwards remain.

    async def set_content(self, html: str = EMPTY_DOCUMENT, url: str = "about:blank",
                          *, timeout=30, wait_until="load") -> Page:
        """Inject markup and await the selected document lifecycle state."""
        self._check_wait()
        if wait_until not in ("commit", "domcontentloaded", "load"):
            raise ValueError("wait_until must be 'commit', 'domcontentloaded', or 'load'")
        deadline = _deadline(timeout)
        self.realm.executor.call(self.navigation.set_content, html, url, timeout=_remaining(deadline))
        if wait_until != "commit":
            await self.wait_for_load_state(wait_until, timeout=_remaining(deadline))
        return self

    async def goto(self, url, *, timeout=30, wait_until="load"):
        self._check_wait()
        deadline = _deadline(timeout)
        operation = self.realm.executor.call(
            self._goto, url, timeout, wait_until, timeout=_remaining(deadline)
        )
        if not isinstance(operation, Future):
            return operation
        waiting = bridge_future(operation)
        waiting.add_done_callback(consume_future)
        scope = asyncio.timeout(_remaining(deadline))
        try:
            async with scope:
                return await asyncio.shield(waiting)
        except TimeoutError:
            if not scope.expired():
                raise
            self.executor.call(self.navigation.cancel, operation, observer_cancelled=True)
            raise HostStateError(operation.navigation_operation.diagnostics) from None
        except asyncio.CancelledError:
            self.executor.call(self.navigation.cancel, operation, observer_cancelled=True)
            raise


    def _goto(self, url, timeout, wait_until):
        """The navigation itself, named so a timeout says which call it was."""
        return self.navigation.goto(url, timeout=timeout, wait_until=wait_until)

    async def wait_for_load_state(self, state="load", timeout=30):
        self._check_wait()
        deadline = _deadline(timeout)
        scope = asyncio.timeout(_remaining(deadline))
        future = None
        def register_waiter():
            nonlocal future
            future = self.lifecycle.wait_for_state(state)
        try:
            async with scope:
                self.executor.call(register_waiter, timeout=_remaining(deadline))
                await bridge_future(future)
        except TimeoutError:
            if not scope.expired():
                raise
            raise TimeoutError(f"document did not reach {state} within {timeout}s") from None
        finally:
            if future is not None and not future.done():
                future.cancel()


    async def wait_for(self, expression, timeout=30):
        self._check_wait()
        deadline = _deadline(timeout)
        while True:
            remaining = _remaining(deadline)
            try:
                if self.evaluate(f"Boolean(({expression}))", timeout=remaining):
                    return
            except RealmBusyError:
                if deadline is not None and time.perf_counter() >= deadline:
                    raise TimeoutError(
                        "condition did not become true within its deadline"
                    ) from None
                raise
            if deadline is not None and time.perf_counter() >= deadline:
                raise TimeoutError("condition did not become true within its deadline")
            await asyncio.sleep(min(0.01, remaining) if remaining is not None else 0.01)


    def evaluate(self,
                 script: str,
                 filename: str | None = None, *, timeout: float | None = 30) -> object:
        """Run *script* in the page's realm and return its result as Python.

        *script* is source, not an expression only: ``"1 + 1"`` works, and so does
        a statement list that ends in a value.  A promise is **not** awaited --
        wait with :meth:`wait_for` for the expected condition.

        :param script: JavaScript source.
        :param filename: name this script appears under in stack traces.
        :returns: the result converted to Python; a JS object becomes a ``dict``.
            Non-copyable JavaScript objects become their string representation. Use
            :meth:`evaluate_handle` to keep a live object instead.
        :raises pyv8.JSException: the script threw.
        :raises RuntimeError: the page is closed.
        """
        self._check()
        return self.realm.executor.call(self._evaluate_data, script, filename,
                                        timeout=timeout, _owns_checkpoint=True)

    def _evaluate_data(self, script, filename):
        # ScriptController completes the script checkpoint before we copy values.
        # Copying an object may itself run JS getters; their jobs belong to a
        # second boundary. Primitive results need no second native checkpoint.
        value = self.scripts.evaluate(script, filename=filename)
        if isinstance(value, (pyv8.JSObject, dict, list, tuple)):
            try:
                value = value.to_python() if isinstance(value, pyv8.JSObject) else value
                return _snapshot_data(value, {})
            finally:
                self.executor.after_callback()
        return _snapshot_data(value, {})

    def evaluate_handle(self,
                        script: str,
                        filename: str | None = None) -> pyv8.JSObject:
        """Run *script* and return the live V8 result instead of a Python copy.

        The way to reach values Python cannot copy -- a DOM node, a ``Map``, a
        function -- at the cost of holding a JS object: it belongs to the realm it
        came from, so navigation invalidates it and only the owner thread may
        touch it.

        :param script: JavaScript source.
        :param filename: name this script appears under in stack traces.
        :returns: a ``pyv8.JSObject``.
        :raises pyv8.JSException: the script threw.
        :raises RuntimeError: the page is closed.
        """
        self._check()
        return self.realm.executor.call(
            self.scripts.evaluate, script, filename=filename, _owns_checkpoint=True,
        )

    async def run_until_idle(self, timeout=1):
        self._check_wait()
        if self.closed:
            raise HostStateError("Page is closed")
        deadline = time.perf_counter() + max(0, float(timeout))
        while True:
            self._check_wait()
            if self.execution_group.is_idle(self.realm.agent):
                return True
            if time.perf_counter() >= deadline:
                return False
            await asyncio.sleep(0.001)

    async def sleep(self, seconds):
        """Wait *seconds* while this page's event loop keeps running.

        Unlike a host ``time.sleep``, awaited page tasks can make progress
        during the wait. No script runs and no condition is checked, so this
        cannot raise ``JavaScriptTerminated``; use :meth:`wait_for` to wait
        on a business condition.
        """
        self._check_wait()
        await asyncio.sleep(max(0.0, float(seconds)))


    def dispatch_event(self,
                       event_type: str,
                       *,
                       target: str | object = "window",
                       selector: str | None = None,
                       event_class: str | None = None,
                       init: dict | None = None) -> bool:
        """Dispatch a host-trusted event and report whether it was not cancelled.

        Unlike a script-created event, the dispatched one is marked trusted, which
        is what lets page code react to it the way it reacts to a real one.

        :param event_type: the event's type (``"click"``, ``"custom"``, ...).
        :param target: ``"window"`` (default), ``"document"``, or a DOM node from
            :meth:`query_selector`.  Mutually exclusive with *selector*.
        :param selector: resolve the target from the current document instead.
        :param event_class: interface to build the event from; inferred from
            *event_type* when omitted (``"click"`` becomes ``MouseEvent``).
        :param init: plain dict of EventInit members; the ``bubbles``/
            ``cancelable`` defaults a browser would use are filled in when absent.
        :returns: ``True`` when the event was not cancelled, ``False`` when a
            listener called ``preventDefault``.
        :raises HostStateError: *selector* matches nothing.
        :raises TypeError: *event_class* is not an ``Event`` interface.
        """
        self._check()
        return self.realm.executor.call(
            self.lifecycle.dispatch_event, event_type,
            target=target, selector=selector, event_class=event_class, init=init,
        )

    def _deep_roots(self):
        """Yield ``(page, root)`` in staged order: the page's light tree, then
        its shadow trees (open and closed) in tree order, then descendant
        frames recursively.  This matches docs/pyv8c/usage.md; it is not a
        single flattened tree order.

        A query root is a Document or a ShadowRoot; ``select(root, ...)`` already
        walks a fragment's descendants, so handing it a ShadowRoot reaches a closed
        tree that page script cannot see.  The page is returned alongside the root
        because a hit node must be driven by the mouse of the realm that owns it.
        """
        from ..web.html.iframe import HTMLIFrameElement

        def tree_walk(root):
            """Descendants in tree order, entering every shadow tree in place.

            ``_subtree_nodes`` uses a LIFO stack and reverses siblings, so it
            cannot define which match is "first".  ``_descendants`` keeps tree
            order; shadow trees are entered at their host, as insertion does.
            """
            for node in root._descendants():
                yield node
                shadow = getattr(node, '_shadow_root', None)
                if shadow is not None:
                    yield from tree_walk(shadow)

        def walk(page, seen):
            if id(page) in seen:
                return
            seen.add(id(page))
            yield page, page.document
            shadows = []
            frames = []
            for node in tree_walk(page.document):
                if getattr(node, '_shadow_root', None) is not None:
                    shadows.append(node._shadow_root)
                if isinstance(node, HTMLIFrameElement):
                    frames.append(node)
            for shadow in shadows:
                yield page, shadow
            # Frame order comes from the DOM, not from _owned.frames: child
            # pages mount asynchronously, so that set has no tree order.
            for iframe in frames:
                services = getattr(iframe, '_child_services', None)
                child = services._page_realm.page if services is not None else None
                if child is not None and not child.closed:
                    yield from walk(child, seen)

        yield from walk(self, set())

    def _resolve_deep(self, selector, *, deep):
        """Return ``(node, owning_page)`` for the first match, or ``None``.

        Runs inside the owner's executor call.  With ``deep=False`` this is just
        the light tree of the current document, matching the historical behaviour
        of :meth:`query_selector`.  With ``deep=True`` it walks the staged roots
        from :meth:`_deep_roots` and returns the first CSS match, paired with the
        page whose mouse may drive it.  A malformed selector raises PageSyntaxError.
        """
        from ..web.css.selectors import select

        roots = self._deep_roots() if deep else ((self, self.document),)
        for page, root in roots:
            node = select(root, selector, first=True)
            if node is not None:
                return node, page
        return None

    def query_selector(self, selector: str, *, deep: bool = False) -> Node | None:
        """Return the host-side node matching a CSS selector.

        :param selector: CSS selector.
        :param deep: when true, search in staged order -- the current light tree,
            then shadow trees (open and closed), then descendant frames; the
            first match wins.  See docs/pyv8c/usage.md.
        :returns: the DOM node handle, or ``None`` when nothing matches.
        :raises PageSyntaxError: *selector* is not valid CSS.
        """
        self._check()
        def resolve():
            found = self._resolve_deep(str(selector), deep=deep)
            return None if found is None else found[0]

        return self.realm.executor.call(resolve)

    async def wait_for_selector(self, selector: str, *, deep: bool = False,
                                timeout: float | None = 30) -> Node | None:
        """Wait until *selector* matches and return the node.

        Uses the same resolution as :meth:`query_selector`, including ``deep``,
        so a widget inside a shadow tree or a frame can be awaited directly.
        """
        self._check_wait()
        deadline = _deadline(timeout)
        while True:
            remaining = _remaining(deadline)
            try:
                node = self.query_selector(selector, deep=deep)
                if node is not None:
                    return node
            except RealmBusyError:
                if deadline is not None and time.perf_counter() >= deadline:
                    raise TimeoutError(
                        f'selector {selector!r} did not match within {timeout}s'
                    ) from None
                raise
            if deadline is not None and time.perf_counter() >= deadline:
                raise TimeoutError(f'selector {selector!r} did not match within {timeout}s')
            await asyncio.sleep(min(0.01, remaining) if remaining is not None else 0.01)

    def set_visibility(self, hidden: bool) -> bool:
        """Set the document's visibility and fire the events that follow.

        :param hidden: ``True`` hides the document (``visibilitychange``, then
            ``freeze``); ``False`` shows it (``visibilitychange``, then
            ``resume``).
        :returns: ``True`` when the state changed.  A redundant call changes
            nothing and returns ``False``, matching the browser's
            "only on transition" behaviour.
        """
        self._check()
        return self.realm.executor.call(self.lifecycle.set_visibility, hidden)

    def _require_element(self, selector: str, *, deep: bool = False):
        """The node *selector* matches, or the error a missing one is worth.

        Returns ``(node, owning_page)`` so input is dispatched by the realm that
        owns the node -- a frame's node cannot be clicked by the parent's mouse.
        """
        found = self._resolve_deep(str(selector), deep=deep)
        if found is None:
            raise HostStateError(f"No element matches the selector {selector!r}")
        return found

    async def click(self,
              selector: str,
              *,
              button: str = "left",
              click_count: int = 1,
              force: bool = False,
              delay: float | None = None,
              duration: float | None = None,
              deep: bool = False) -> None:
        """Click the element *selector* matches, the way a user would.

        The mouse is moved to the element's centre and pressed and released
        there, so the page sees the whole sequence a real click produces: the
        boundary and move events on the way, focus, and finally ``click`` --
        with any activation behavior the element has (a link navigates, a
        checkbox toggles, a submit button submits, a label reaches its control).

        :param selector: a CSS selector, resolved against the current document.
        :param button: ``"left"``, ``"middle"`` or ``"right"``.
        :param click_count: the ``detail`` the press and the click report.
        :param force: click the centre even when another element is on top of
            it, which is the one thing that would otherwise refuse the click.
        :param delay: seconds the button stays down before it is released.
        :param duration: seconds the approach move is spread over.
        :param deep: when true, search shadow trees and descendant frames for the
            selector; the matching node is clicked by the realm that owns it.
        :raises HostStateError: *selector* matches nothing.
        :raises InvalidStateError: the element has no box to click, or its
            centre is covered by another element and *force* is false.
        """
        self._check()
        node, owner = self.realm.executor.call(
            lambda: self._require_element(selector, deep=deep)
        )
        return await owner.mouse.click_element(
            node, button=button, click_count=click_count,
            force=force, delay=delay, duration=duration,
        )

    async def dblclick(self, selector: str, *, button: str = "left", force: bool = False,
                 delay: float | None = None, duration: float | None = None,
                 deep: bool = False) -> None:
        """Double-click the element *selector* matches.

        Two clicks counted as one run, so the second reports ``detail`` 2 and a
        ``dblclick`` follows it.  *deep* has the same meaning as in :meth:`click`.
        """
        self._check()
        node, owner = self.realm.executor.call(
            lambda: self._require_element(selector, deep=deep)
        )
        generation = owner.navigation.generation
        await owner.mouse.move_to_element(node, force=force, duration=duration)
        owner._check_document_generation(generation)
        return await owner.mouse.dblclick(button=button, delay=delay)

    async def hover(self, selector: str, *, force: bool = False,
              duration: float | None = None, curve: str | None = None,
              seed: int | None = None, deep: bool = False) -> None:
        """Move the mouse to the centre of the element *selector* matches.

        The element is left hovered, which is what the boundary events report:
        a page watching ``mouseover`` sees the move arrive.  *deep* has the same
        meaning as in :meth:`click`.
        """
        self._check()
        node, owner = self.realm.executor.call(
            lambda: self._require_element(selector, deep=deep)
        )
        return await owner.mouse.move_to_element(
            node, force=force, duration=duration, curve=curve, seed=seed,
        )

    async def close(self) -> None:
        """Dispose the realm and await its owned background work."""
        self._runtime.check_thread()
        self._runtime.check_loop()
        task = self._begin_close()
        if task is not None:
            await asyncio.shield(task)

    def _begin_close(self):
        self._runtime.check_thread()
        if self.closed:
            if self._runtime.closed:
                return
            task = self._close_task
            if task is not None and task.done() and not task.cancelled() and task.exception() is None:
                return
        self._runtime.check_owner(allow_closing=True)
        if not self.closed:
            self._retire()
        return self._ensure_close_task()

    def _ensure_close_task(self):
        if self._close_task is None or (
            self._close_task.done() and (self._close_task.cancelled() or self._close_task.exception() is not None)
        ):
            self._close_task = self._runtime.loop.create_task(self._finish_close())
            self._close_task.add_done_callback(consume_future)
        return self._close_task

    async def _finish_close(self):
        # Finalization may start on a JS/Inspector stack.
        await asyncio.sleep(0)
        with close_phase(self, "child-pages"):
            results = await asyncio.gather(*(child.close() for child in tuple(self._child_pages)),
                                           return_exceptions=True)
        first_error = next((result for result in results if isinstance(result, BaseException)), None)
        try:
            with close_phase(self, "background"):
                await self._background.wait()
        except Exception as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error
        self.profile._discard_page(self)
        if self.parent_page is not None:
            self.parent_page._child_pages.discard(self)

    async def __aenter__(self):
        self._check_wait()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def _retire(self) -> None:
        """Close the page and everything it owns.

        Idempotent on the Runtime owner; reentrant cleanup does not close twice.  Frames,
        workers, WebSockets, pending resources, DevTools registration and the
        realm itself are released.  The first failure is remembered and re-raised
        after the rest was attempted, which leaves the page able to retry.  A
        closed page refuses further work: its realm executor raises
        ``RuntimeError``.
        """
        if threading.get_ident() != self._runtime.owner:
            raise HostStateError("Page APIs belong to the Browser owner thread")
        if self.closed:
            return
        self._runtime.check_owner(allow_closing=True)
        self._retiring = True
        with close_phase(self, "interrupt-requests"):
            self._background.begin_retirement()
        with self._close_lock:
            if self.closed:
                return
            # A reentrant close already runs the owner-thread teardown, so the
            # scope stays OPEN and a failed owner call can be retried.
            if self._closing:
                return
            self._closing = True
        try:
            with close_phase(self, "retire-owner"):
                return self.realm.executor.close_page(self._close_owner)
        finally:
            with self._close_lock:
                self._closing = False
            # The scope closes after all owner-thread cleanup; a dispatch that
            # timed out before starting leaves it OPEN for a later close().
            if self.closed:
                try:
                    self._resource_scope.close()
                    if self.execution_group.closed:
                        self.execution_group.executor.close()
                finally:
                    # Every entry point (public close, CDP, frame retirement)
                    # publishes the same owned completion task.
                    self._ensure_close_task()

    def _close_owner(self):
        if self.closed:
            return
        first_error = None

        def attempt(callback, *args):
            nonlocal first_error
            try:
                callback(*args)
                return True
            except control_exceptions():
                # Execution control must unwind: a termination or interrupt is
                # not a failed cleanup step, and no further page JS may run.
                raise
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                return False

        if not getattr(self, "_unload_dispatched", False):
            self._unload_dispatched = True
            if self.navigation.has_navigation:
                with close_phase(self, "beforeunload"):
                    attempt(self.lifecycle.dispatch_beforeunload)
                with close_phase(self, "pagehide"):
                    attempt(self.lifecycle.dispatch_page_hide, False)
                with close_phase(self, "unload"):
                    attempt(self.lifecycle.dispatch_unload)
        # Drop cached JS scripts before the V8 context goes away.
        self.scripts.clear_js_helpers()
        self.scripts.reset()
        angle = self.services.graphics_backend
        if angle is not None:
            attempt(angle.close_page, self.services)
        attempt(self.services.task_services.cancel_animation_frames)
        attempt(self.net._retire)
        attempt(self.tasks.close)
        attempt(self.executor.cancel_pending)
        attempt(self.document_resources.reset)
        attempt(self.document._invalidate_tree)
        self.realm.rejections.close()
        self.lifecycle.load_waiters.clear()
        for frame in self._owned.drain("frames"):
            if attempt(frame._retire):
                self.services.context_services.discard_frame(frame)
            else:
                self._owned.track("frames", frame)
        attempt(self.services.websocket_services.retire)
        for worker in self._owned.drain("workers"):
            if attempt(worker.fn_terminate):
                self.services.worker_services.discard_worker(worker)
            else:
                self._owned.track("workers", worker)
        # Descendants can still hold the shared isolate's Inspector pause loop.
        # Close them before unregistering this target, so its disposal mailbox
        # is drained before this executor stops accepting commands.
        if self._devtools_target is not None:
            if self.profile._devtools is not None:
                if attempt(self.profile._devtools.unregister, self):
                    self._devtools_target = None
            else:
                self._devtools_target = None
        if first_error is not None:
            # Keep the realm owner available to retry failed native resources.
            raise first_error
        #: Retired realms close with the page, before the isolate they were built on.
        pending, self._retired_realms = self._retired_realms, []
        for realm in pending:
            if not attempt(realm.close):
                self._retired_realms.append(realm)
        if first_error is not None:
            raise first_error
        Window._unbind_context(self.v8_context)
        self.realm.close()
        self.document._invalidate_tree()
        self.executor.close()
        self.isolate = None
        self.v8_context = None
        self.global_proxy = None
        self.closed = True
        parent_cleanup = getattr(self, "_parent_scope_cleanup", None)
        if callable(parent_cleanup):
            parent_cleanup()
            self._parent_scope_cleanup = None
        # Idempotent: closes callbacks that were not drained, so cleanup still
        # happens when another thread's close() timed out while waiting.
        self._resource_scope.close()

    def _check(self):
        self._runtime.check_owner()
        if self._retiring:
            raise HostStateError("Page is closed or closing")


    def _check_wait(self):
        self._check()
        if asyncio.get_running_loop() is not self._runtime.loop:
            raise HostStateError("waits belong to the Browser event loop")
        if self.closed:
            raise HostStateError("Page is closed")
        if pyv8.current_context(self.isolate) is not None:
            raise HostStateError("cannot suspend a host wait inside a running JS stack")

    def _check_document_generation(self, generation):
        self._check_wait()
        if self.navigation.generation != generation:
            raise HostStateError("document changed during input")

