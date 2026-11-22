"""Document resource ownership, readiness and element completion."""
import time

from pyv8c import pyv8
from ...foundation.logging import logger
from ...foundation.load_handle import LoadHandle
from .tracker import DocumentResource, DocumentResourceTracker
from .discovery import NodeSnapshot, discover
from .completion import ResourceCompletion


class DocumentResourceController:
    def __init__(self, page, controller, frames):
        self.page, self.controller, self.frames = page, controller, frames
        self.tracker = DocumentResourceTracker()
        self.completion = ResourceCompletion(page)

    @property
    def has_pending(self):
        return self.tracker.has_pending

    def reset(self):
        """Cancel what the previous document still had in flight, and drop it.

        A cancelled load never completes, so its entry, left behind, would keep counting
        as outstanding -- which is how one image still loading when a navigation committed
        held the next document's ``load`` for the whole navigation budget.
        """
        items = self.tracker.clear()
        first_error = None
        for item in items:
            try:
                self._cancel(item)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _cancel(self, item):
        item.handle.cancel()

    def outstanding(self):
        return self.tracker.outstanding()

    def cancel_node(self, node):
        for token, item in self.tracker.for_node(node):
            self.tracker.take(token)
            self._cancel(item)

    def restart_element_resource(self, node):
        """A load-bearing write replaced the URL: drop the load in flight, then discover."""
        self.cancel_node(node)
        node._resource_started = False
        self.discover_element_resource(node)

    def discover_element_resource(self, node):
        page = self.page
        if page.closed or getattr(node, "_resource_started", False):
            return
        spec = discover(NodeSnapshot.capture(node, page.services.sandbox_tokens))
        if spec is None:
            return
        node._resource_started = True
        if spec.kind == "script":
            node._resource_async = spec.async_script
            if not spec.async_script and not getattr(node, "_script_defer", False) and not getattr(node, "_parser_script", False):
                page.scripts.dom_blocking_scripts += 1
                page.scripts.queue.append(node)
        self.track_document_resource(node, spec.url, dict(spec.options), spec.kind)

    def track_document_resource(self, node, url, request, kind):
        page = self.page
        # Where the entry's timing starts: the request is issued from here.
        node._resource_request_start = page.services.realm_services.performance.clock.now_ms()
        #: The same instant on the shared clock, so the child document's own timeline can
        #: start where its request did.
        node._resource_monotonic_start = time.perf_counter()
        generation = page.navigation.generation
        handle = LoadHandle()
        item = DocumentResource(generation, node, kind, handle)
        token = self.tracker.add(item)

        def terminal(completed):
            if completed.cancelled:
                page.tasks.post(lambda: self._release_cancelled(token, item))
        handle.add_done_callback(terminal)

        def done(response, error):
            if error is not None and not handle.cancelled:
                # The page only ever hears Chrome's wording, so the reason lands here.
                logger.debug("resource failed: {} ({})", url, error)
            if not handle.cancelled:
                page.tasks.post(lambda: self.complete_document_resource(token, generation, response, error, handle))

        # Document ownership survives transport completion until element completion.
        page.resource_ownership.track(handle)
        try:
            handle.bind_transport(self.controller.load_resource_async(url, request, done))
        except BaseException:
            self.tracker.take(token)
            self._cancel(item)
            raise
        # Chromium starts the load in its own task, and fires this once.
        page.tasks.post(lambda: page.lifecycle.dispatch_progress_event(node, "loadstart"))
        return handle

    def begin_frame_document(self, node, child):
        """A carried document has a load barrier even without a transport."""
        page = self.page
        handle = LoadHandle()
        item = DocumentResource(page.navigation.generation, node, 'iframe', handle,
                                child_services=child.services)
        token = self.tracker.add(item)
        page.resource_ownership.track(handle)
        def terminal(completed):
            if completed.cancelled:
                page.tasks.post(lambda: self._release_cancelled(token, item))
        handle.add_done_callback(terminal)
        return token, item

    def _release_cancelled(self, token, item):
        if self.tracker.get(token) is not item:
            return
        self.tracker.take(token)
        if not self.page.closed and item.generation == self.page.navigation.generation:
            self.page.lifecycle.maybe_fire_document_load()

    @staticmethod
    def _response_length(response):
        try:
            return len(getattr(response, "body", None) or b"")
        except TypeError:
            return 0

    def record_resource_timing(self, node, kind, response):
        """Hand the realm one ``resource`` record: the URL, the initiator and the span."""
        length = self._response_length(response)
        clock = self.page.services.realm_services.performance.clock
        start = getattr(node, "_resource_request_start", None) or clock.now_ms()
        self.page.services.realm_services.performance.record(
            "resource",
            name=str(getattr(response, "url", "") or ""),
            initiator=str(kind),
            start=start,
            end=clock.now_ms(),
            transferSize=length,
            encodedBodySize=length,
            decodedBodySize=length,
            contentType=self._response_content_type(response),
        )

    @staticmethod
    def _response_content_type(response):
        headers = getattr(response, "headers", None)
        getter = getattr(headers, "fn_get", None) if headers is not None else None
        if getter is None:
            return ""
        value = getter("content-type")
        return "" if value is None or value is pyv8.Null else str(value)

    def complete_document_resource(self, token, generation, response, error, handle=None):
        page = self.page
        item = self.tracker.valid(token, generation)
        if item is None or generation != page.navigation.generation or page.closed or (handle is not None and handle.cancelled):
            return
        node, kind, _handle = item.node, item.kind, item.handle
        if error is None and response is not None:
            self.record_resource_timing(node, kind, response)
        if kind == "iframe" and not error:
            response._navigation_started = getattr(node, "_resource_monotonic_start", None)
            # ``when_loaded`` resolves on the child realm's owner thread, and
            # touching page state must happen on this Page's owner thread.
            def child_loaded(ok):
                page.tasks.post(lambda: self.finish_iframe_resource(token, generation, node, ok))
            child = self.frames.load_iframe_response(node, response, child_loaded)
            item.child_services = child.services
            item.child_generation = child.navigation.generation
            return
        self.tracker.take(token)
        _handle.finish()
        self.completion.complete(node, kind, response, error, self._response_length)

    def finish_iframe_resource(self, token, generation, node, ok):
        page = self.page
        if generation != page.navigation.generation or page.closed:
            return
        services = getattr(node, "_child_services", None)
        child = getattr(getattr(services, "_page_realm", None), "page", None)
        item = self.tracker.frame_matches(token, generation, node, services, child)
        if item is None:
            return
        self.tracker.take(token)
        item.handle.finish()
        self.frames.dispatch_frame_completion(node, ok)
