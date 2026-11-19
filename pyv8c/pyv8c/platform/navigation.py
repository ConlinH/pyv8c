"""Navigation and HTML document parsing for a single Page."""

import re
import time

from pyv8c import pyv8
from concurrent.futures import Future
from dataclasses import dataclass

from ..foundation.host_errors import HostStateError
from ..foundation.tasks import TaskSource
from ..foundation.error_policy import control_exceptions
from ..foundation.urls import absolute_url
from ..network.redirects import reportable_hops
from ..web.bom.utils import page_base_url, page_secure_context
from ..web.dom.document import HTMLDocument
from ..web.bom.motion import INSECURE_ORIGIN_INTERFACES
from ..web.html.parser import IncrementalDOMParser
from ..foundation.content_policy import ContentPolicy


def hide_insecure_origin_interfaces(page):
    """Take the motion interfaces out of a realm whose document is not secure."""
    v8_context = getattr(page, "v8_context", None)
    if v8_context is None:
        return
    for name in INSECURE_ORIGIN_INTERFACES:
        v8_context.hide_global(name)


@dataclass(slots=True)
class NavigationOperation:
    future: Future
    handle: object = None
    timer: int = 0
    diagnostics: str = ''


class NavigationController:
    """Own navigation and parser state; runs on the owning Page thread."""

    def __init__(self, page):
        self.page = page
        self.generation = 0
        self.has_navigation = False
        self.pending = 0
        self.parser = None
        self.parser_generation = 0
        self.parsing = False
        self.parser_done = False
        self.operation = None
        self.cross_origin_isolated = bool(page.parent_page and
                                          page.parent_page.navigation.cross_origin_isolated)
        self.content_policy = ContentPolicy(page.parent_page.navigation.content_policy
                                            if page.parent_page else None)
        page._resource_scope.add(self.close)

    def navigation_allowed(self):
        return not self.has_navigation or self.page.lifecycle.dispatch_beforeunload()

    def initialize_empty_document(self):
        """Populate an iframe's initial about:blank without starting a navigation.

        The insertion's script can read and write this complete document immediately;
        src/srcdoc navigation is separate from creating this document.
        """
        IncrementalDOMParser(self.page.services, self.page.document, "", active=False).run()
        self.parser_done = True
        self.page.lifecycle.dom_content_loaded = True
        self.page.lifecycle.document_load_fired = True

    def commit_content(self, html, url, response=None, *, replace_initial=False):
        lifecycle = self.page.lifecycle
        if self.has_navigation:
            # Chrome's order for the document being replaced: pageswap, pagehide, the
            # document going hidden, then unload.
            lifecycle.dispatch_pageswap()
            lifecycle.dispatch_page_hide(False)
            lifecycle.dispatch_visibility_hidden()
            lifecycle.dispatch_unload()
        self.generation += 1
        self.page.services.task_services.cancel_animation_frames()
        lifecycle.cancel_state_waiters()
        # A later document replaces the realm; the first one is the page's own.
        previous_origin = self.page.origin
        self.page._update_origin(url)
        from .isolation import document_isolated
        isolated = document_isolated(self.page, url, response)
        policy = ContentPolicy(self.page.parent_page.navigation.content_policy
                               if response is None and self.page.parent_page and
                               str(url).startswith(('about:', 'blob:')) else None)
        if response is not None:
            policy.add(response.headers.fn_get('content-security-policy') or '')
        renew_realm = (self.has_navigation or replace_initial or
                       isolated != self.cross_origin_isolated or bool(policy.policies))
        self.cross_origin_isolated = isolated
        self.content_policy = policy
        self.page.services.realm_services.performance.clock.resolution_ms = 0.005 if isolated else 0.1
        if not renew_realm and previous_origin != self.page.origin:
            # Initial host content injection keeps installed globals/listeners.
            # Its explicit origin must still select the correct agent queue.
            page = self.page
            page.executor.after_callback()
            agent = page.execution_group.agent(page.origin)
            page.v8_context.set_microtask_queue(agent.microtasks)
            page.realm.agent = page.executor.agent = agent
        self.has_navigation = True
        self.parser_done = lifecycle.dom_content_loaded = lifecycle.document_load_fired = False
        lifecycle._load_started = False
        lifecycle._load_error = None
        self.page.document_resources.reset()
        self.page.scripts.reset()
        if renew_realm:
            # The replaced document's timers go with it; the queue belongs to the page
            # and outlives the document.
            self.page.tasks.cancel_timers()
            self.page._renew_document_realm(str(url))
        else:
            self.page.window._content_policy = policy
            values = self.page.window._window_values
            if 'trustedTypes' in values.cached_names:
                values.value('trustedTypes')._policy = policy
        if not page_secure_context(self.page, url):
            # Chrome exposes these only on a secure origin; a frame commits while its
            # parent's realm runs, so the names leave on a task of their own.
            page = self.page
            page.tasks.post(lambda: hide_insecure_origin_interfaces(page))
        self.page.window.location.href = str(url)
        self.page.window.sync_history_url(str(url))
        devtools = self.page.profile._devtools
        if devtools is not None:
            devtools.target_info_changed(self.page)
        # Realm replacement can read the old document while retiring its frames.
        # Clear after those reads, just before relinquishing this document.
        self.page.document._invalidate_tree()
        self.page.document = HTMLDocument(services=self.page.services, url=str(url),
                                          ready_state="loading")
        # A new document starts its own timeline; earlier records belong to the old one.
        hops = int(getattr(response, "_redirect_hops", 0) or 0)
        crossed = bool(getattr(response, "_redirect_crossed", False))
        counted = reportable_hops(hops, crossed)
        self.page.services.realm_services.performance.begin_navigation(
            url, redirects=counted,
            # A chain that reports nothing reports no window either, as Chrome does.
            redirects_span=float(getattr(response, "_redirect_span", 0.0) or 0.0) if counted else 0.0,
            started_at=getattr(response, "_navigation_started", None))
        #: A document's referrer is the URL that asked for it: the parent's for a frame,
        #: and none for a navigation the host started.
        self.page.document._referrer = str(getattr(response, "_document_referrer", "") or "")
        self.page.services.document = self.page.document
        self.page.services.task_services.prepare_animation_frames()
        # The new document is created after retiring the old Realm. Publish its
        # identity before parser callbacks or Web APIs can post document work.
        from ..foundation.agent_tasks import TaskContext
        self.page.executor.bind_agent(self.page.realm.agent, TaskContext(
            self.page.realm, self.page.document, self.generation))
        target = self.page._devtools_target
        if target is not None:
            target.wait_for_startup()
        self.parser_generation = self.generation
        self.parser = IncrementalDOMParser(self.page.services, self.page.document, str(html))
        self.parsing = True
        # HTML parsing runs on the caller thread until a parser-blocking external
        # script is hit; lifecycle notifications stay queued to the task boundary.
        self.resume_parser(self.parser_generation)
        return self.page

    def set_content(self, html, url="about:blank", *, replace_initial=False, response=None):
        if self.page.closed or self.page.profile.closed:
            raise HostStateError("Page is closed")
        if not self.navigation_allowed():
            return self.page
        return self.commit_content(html, url, response, replace_initial=replace_initial)

    @staticmethod
    def decode_html(response):
        body = response.body
        if not isinstance(body, bytes):
            return str(body)
        content_type = response.headers.fn_get("content-type")
        match = re.search(r"charset\s*=\s*['\"]?([^\s;'\"]+)", str(content_type), re.I)
        encoding = match.group(1) if match else "utf-8"
        try:
            return body.decode(encoding, "replace")
        except LookupError:
            return body.decode("utf-8", "replace")

    def goto(self, url, *, timeout=30, wait_until="load"):
        """Download and navigate to an HTML document."""
        return self._load(url, timeout=timeout, wait_until=wait_until, browser_initiated=True)

    def _load(self, url, *, timeout, wait_until, browser_initiated,
              method="GET", body=None, content_type=None, user_activated=False):
        """Load *url*, carrying whatever the request that asked for it must send.

        Only a browser-initiated navigation sends no ``Referer``; a user gesture
        is the other source of ``Sec-Fetch-User``.
        """
        if self.page.closed or self.page.profile.closed:
            raise HostStateError("Page is closed")
        states = {
            None: None,
            "commit": None,
            "domcontentloaded": lambda: self.page.lifecycle.dom_content_loaded,
            "load": lambda: self.page.lifecycle.document_load_fired,
        }
        if wait_until not in states:
            raise ValueError("wait_until must be 'commit', 'domcontentloaded', or 'load'")
        target = absolute_url(url, page_base_url(self.page))
        if not self.navigation_allowed():
            return None
        timeout_value = None if timeout is None else max(0.0, float(timeout))
        request = {
            "method": method, "mode": "navigate", "credentials": "include",
            "resource_type": "document", "destination": "document",
            "browser_initiated": browser_initiated,
            "user_activated": user_activated,
            "timeout": timeout_value,
        }
        if body is not None:
            request["body"] = body
        if content_type:
            request["content_type"] = content_type
        self.cancel()
        operation = NavigationOperation(Future())
        operation.future.navigation_operation = operation
        started = time.perf_counter()
        def timeout_message():
            outstanding = self.page.document_resources.outstanding()
            waiting = ', '.join(f'{count} {kind}' for kind, count in sorted(outstanding.items()))
            return (f'Navigation to {target} timed out after {timeout_value:g}s waiting for {wait_until}'
                    + (f'; still loading {waiting}' if waiting else '')
                    + f' (the document request took {time.perf_counter()-started:.1f}s)')
        operation.diagnostics = timeout_message() if timeout_value is not None else ''
        self.operation = operation
        def finish(response=None, error=None):
            if operation.future.done():
                return
            self.page.tasks.cancel(operation.timer)
            if error is not None:
                operation.future.set_exception(error)
            else:
                operation.future.set_result(response)
        def received(response, error):
            if operation.future.done() or self.operation is not operation:
                return
            if error is not None:
                finish(error=error)
                return
            try:
                #: The document's timeline starts where its request did, not where the
                #: body finished arriving.
                response._navigation_started = started
                self.commit_content(self.decode_html(response), response.url or target, response, replace_initial=True)
                if timeout_value is not None:
                    operation.diagnostics = timeout_message()
                state = self.page.lifecycle.wait_for_state(wait_until or 'commit', self.generation)
                def ready(done):
                    try:
                        done.result()
                    except BaseException as exc:
                        finish(error=exc)
                    else:
                        finish(response=response)
                state.add_done_callback(ready)
            except control_exceptions() as exc:
                finish(error=exc)
                raise
            except BaseException as exc:
                finish(error=exc)
        if timeout_value is not None:
            def expired():
                if not operation.future.done():
                    if operation.handle is not None:
                        operation.handle.cancel()
                    finish(error=HostStateError(timeout_message()))
            operation.timer = self.page.tasks.schedule(expired, timeout_value)
        try:
            operation.handle = self.page.resource_fetcher.load_resource_async(target, request, received)
        except BaseException as exc:
            finish(error=exc)
        return operation.future

    def cancel(self, expected=None):
        operation = self.operation
        if expected is not None and (operation is None or operation.future is not expected):
            return
        if operation is None or operation.future.done():
            return
        if operation.handle is not None:
            operation.handle.cancel()
        self.page.tasks.cancel(operation.timer)
        operation.future.set_exception(HostStateError('navigation was superseded or closed'))

    def close(self):
        self.cancel()
        self.page.lifecycle.cancel_state_waiters()

    def request_navigation(self, url, *, method="GET", body=None,
                           content_type=None, user_activated=False):
        """Queue a navigation a script, a link or a form submission asked for."""
        if self.page.closed or self.page.profile.closed:
            raise HostStateError("Page is closed")
        target = absolute_url(url, page_base_url(self.page))
        self.pending += 1
        request_id = self.pending

        def navigate():
            if request_id == self.pending and not self.page.closed:
                self._load(target, timeout=30, wait_until="load",
                           browser_initiated=False, method=method, body=body,
                           content_type=content_type,
                           user_activated=user_activated)

        self.page.tasks.post(navigate, TaskSource.NAVIGATION)
        return target

    def parser_is_blocked(self):
        return bool(self.page.scripts.dom_blocking_scripts)

    def write_markup(self, markup):
        """Insert JavaScript-written markup (HTML's ``document.write``).

        During parsing the markup joins the token stream at the writer's insertion
        point; afterwards ``document.write`` implies ``document.open()`` -- the
        document is emptied and parsed inside a fresh ``body``, keeping its URL,
        load state and event history.
        """
        parser = self.parser
        if parser is not None and not parser.done:
            return parser.write_markup(markup)
        if not self.open_document():
            return False
        body = self._open_document_body(self.page.document)
        self.parser = IncrementalDOMParser(self.page.services, body, str(markup))
        self.resume_parser(self.parser_generation)
        return True

    def open_document(self):
        """The document open steps: an empty document, reading ``loading`` as Chrome's does."""
        if self.page.closed or self.page.profile.closed:
            return False
        document = self.page.document
        self.generation += 1
        self.parser_generation = self.generation
        self.parser_done = False
        document.ready_state = "loading"
        self.page.document_resources.reset()
        self.page.scripts.reset()
        for child in list(document.get_childNodes() or ()):
            document.fn_removeChild(child)
        self.parser = None
        return True

    def _open_document_body(self, document):
        """Build the skeleton ``document.open()`` leaves for the new parse.

        A browser restarts the insertion modes, so written markup lands inside a
        fresh ``html``/``body``, not beside the document element.
        """
        element_class = self.element_class
        html = element_class("html")("html", services=self.page.services, attributes={})
        document.append_child(html)
        html.append_child(element_class("head")("head", services=self.page.services, attributes={}))
        body = element_class("body")("body", services=self.page.services, attributes={})
        html.append_child(body)
        return body

    def finish_document_parse(self):
        self.parser_done = True
        self.parsing = False
        identity = (self.generation, self.page.document)
        try:
            self.page.lifecycle.set_ready_state("interactive")
        except pyv8.JavaScriptTerminated as error:
            self.page.lifecycle._fail_load(identity, error)
            raise
        if self.page.lifecycle._current_document(identity):
            self.page.tasks.post(self.page.scripts.run_deferred_scripts, TaskSource.PARSER)

    def close_document(self):
        """The document close steps: stop the parser where it stands.

        A page that opened a document and wrote part of it reads ``complete`` at once,
        and the markup it never wrote is not parsed.
        """
        if self.page.closed or self.page.profile.closed:
            return False
        self.parser = None
        if not self.parser_done:
            self.finish_document_parse()
        self.page.document.ready_state = "complete"
        return True

    def resume_parser(self, generation=None):
        if self.page.closed or generation != self.generation or self.parser is None:
            return
        if self.parser_is_blocked():
            return
        self.parsing = True
        try:
            complete = self.parser.run()
        except control_exceptions():
            self.parsing = False
            raise
        except BaseException as exc:  # a script error is dispatched, not raised
            self.parsing = False
            self.page.lifecycle.dispatch_script_error(None, exc)
            complete = True
        if complete and self.parser is not None and self.parser.done:
            self.finish_document_parse()
        else:
            self.parsing = False

    def parser_script_start(self, node):
        node._parser_script = True
        node._resource_started = False
        node._resource_done = False
        node._resource_error = None
        node._resource_response = None
        # A script fetch starts when its start tag is seen, so async/defer/module
        # resources progress while the parser still collects their raw text.
        if getattr(node, "get_src", lambda: "")():
            self.page.document_resources.discover_element_resource(node)

    def parser_script_complete(self, node, source):
        scripts = self.page.scripts
        node._raw_text = source
        script_type = str(getattr(node, "get_type", lambda: "")() or "").lower()
        is_module = script_type == "module"
        is_async = bool(getattr(node, "get_async", lambda: False)())
        # async wins over defer: an asynchronous script is not in the list the document
        # waits for before DOMContentLoaded, and queueing it there wedges the lifecycle.
        is_defer = not is_async and (
            bool(getattr(node, "get_defer", lambda: False)()) or is_module)
        node._resource_async = is_async
        node._script_defer = is_defer
        if node.get_src():
            if is_defer:
                scripts.defer.append(node)
            elif not is_async:
                if not getattr(node, "_resource_queued", False):
                    scripts.dom_blocking_scripts += 1
                    scripts.queue.append(node)
                    node._resource_queued = True
            self.page.document_resources.discover_element_resource(node)
        elif source:
            node._inline_source = source
            if is_defer:
                node._resource_done = True
                scripts.defer.append(node)
            elif is_async:
                self.page.tasks.post(lambda n=node: scripts.execute_script(n, n._inline_source, self.page.url))
            else:
                scripts.execute_script(node, source, self.page.url)

    def element_class(self, tag):
        from ..web.html.registry import class_for_tag
        return class_for_tag(tag)
