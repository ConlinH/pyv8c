"""Script execution, inline handlers and JS helpers for a Page."""

import re
from collections import deque
from urllib.parse import urljoin

from pyv8c import pyv8

from ..foundation.host_errors import HostStateError
from ..foundation.tasks import TaskSource
from ..foundation.error_policy import control_exceptions


class ScriptController:
    """Own script-queue state and execution; runs on the owning Page thread."""

    #: Compiled ad-hoc scripts kept so a repeated evaluate skips compilation.
    COMPILE_CACHE_LIMIT = 32

    def __init__(self, page):
        self.page = page
        self.dom_blocking_scripts = 0
        self.queue = deque()
        self.defer = deque()
        self.module_cache = {}
        # (source, filename) -> pyv8.Script, most recently used last.
        self._compiled = {}
        # The realm this controller settles promises in; pinned so that a call in flight
        # keeps belonging to the document that made it, and dropped with that document.
        self._realm_owner = None

    def reset(self):
        self.dom_blocking_scripts = 0
        self.queue.clear()
        self.defer.clear()
        self.module_cache.clear()
        # Compiled scripts die with the context they were created in.
        self._compiled.clear()

    def clear_js_helpers(self):
        self._realm_owner = None

    @property
    def _realm(self):
        """The realm this controller borrows its promises from.

        The realm owns the context, so it is also what answers once that context is gone: a
        promise or a microtask built by the replaced document reaches the realm it was made
        in, not whichever realm the page has by then.
        """
        realm = self._realm_owner
        if realm is None:
            realm = self._realm_owner = self.page.realm
        return realm

    def make_promise(self):
        return self._realm.make_promise()

    def promise_resolve(self, value):
        return self._realm.promise_resolve(value)

    def retain_object(self, value):
        return self._realm.retain_object(value)

    def enqueue_microtask(self, callback):
        """Queue *callback* as a microtask, in FIFO with promise reactions."""
        return self._realm.enqueue_microtask(callback)

    def evaluate(self, script, filename=None):
        if self.page.closed or self.page.profile.closed:
            raise HostStateError("Page is closed")
        target = self.page._devtools_target
        if target is not None:
            target.before_script()
        try:
            return self.page.v8_context.exec_js(self._compiled_script(script, filename))
        finally:
            if target is not None:
                target.after_script()
            self.page.executor.after_callback()

    def _compiled_script(self, script, filename):
        key = (str(script), filename)
        compiled = self._compiled.get(key)
        if compiled is not None:
            self._compiled[key] = compiled
            return compiled
        compiled = pyv8.Script(self.page.v8_context, str(script), filename)
        self._compiled[key] = compiled
        if len(self._compiled) > self.COMPILE_CACHE_LIMIT:
            self._compiled.pop(next(iter(self._compiled)))
        return compiled

    def execute_script(self, node, source, filename=None):
        """Run one classic script with ``document.currentScript`` set to *node*.

        The IDL attribute is non-null only while the script is on the stack.
        """
        if self._sandboxed("allow-scripts"):
            return False
        if not getattr(node, 'attributes', {}).get('src'):
            nonce = str(getattr(node, 'attributes', {}).get('nonce', ''))
            if not self.page.navigation.content_policy.allows_inline(nonce):
                return False
        if getattr(node, "_script_executed", False):
            return True
        node._script_executed = True
        document = self.page.document
        previous = document.current_script
        document.current_script = node
        try:
            if str(getattr(node, "get_type", lambda: "")() or "").lower() == "module":
                self.execute_module(source, filename or self.page.url, set())
            else:
                self.evaluate(source, filename=filename or self.page.url)
            return True
        except control_exceptions():
            raise
        except BaseException as exc:
            document.current_script = previous
            # The exception carries the position, so the page gets the throw site.
            self.page.lifecycle.dispatch_script_error(
                node, exc,
                filename=str(getattr(exc, "filename", "") or ""),
                line=int(getattr(exc, "line", 0) or 0),
                column=int(getattr(exc, "column", 0) or 0),
            )
            return False
        finally:
            document.current_script = previous
            self.page.executor.after_callback()

    def execute_module(self, source, filename, trail):
        """Execute static module imports in dependency-first order.

        V8's public extension exposes classic script execution only; this adapter
        covers the static-import subset.
        """
        key = str(filename)
        if key in self.module_cache:
            return
        if key in trail:
            return  # cyclic static imports are already evaluating
        trail = set(trail)
        trail.add(key)
        imports = re.findall(r"(^|;)\s*import\s+([\s\S]*?)\s+from\s+(['\"])([^'\"]+)\3\s*;?", source)
        side_effects = re.findall(r"(?:^|;)\s*import\s*(['\"])([^'\"]+)\1\s*;?", source)
        for _prefix, _clause, _quote, specifier in imports:
            dep_url = urljoin(key, specifier)
            dep = self.page.resource_fetcher.load_resource(
                dep_url, {"method": "GET", "mode": "cors", "destination": "script",
                          "resource_type": "script"},
            )
            self.execute_module(dep.body_text(), dep.url or dep_url, trail)
        for _quote, specifier in side_effects:
            dep_url = urljoin(key, specifier)
            dep = self.page.resource_fetcher.load_resource(
                dep_url, {"method": "GET", "mode": "cors", "destination": "script",
                          "resource_type": "script"},
            )
            self.execute_module(dep.body_text(), dep.url or dep_url, trail)
        transformed = re.sub(r"(?:^|;)\s*import\s+([\s\S]*?)\s+from\s+(['\"])([^'\"]+)\2\s*;?", "", source)
        transformed = re.sub(r"(?:^|;)\s*import\s*(['\"])([^'\"]+)\1\s*;?", "", transformed)
        transformed = re.sub(r"\bexport\s+default\s+", "", transformed)
        transformed = re.sub(r"\bexport\s+(?=(?:async\s+)?(?:function|class|const|let|var)\b)", "", transformed)
        transformed = re.sub(r"\bexport\s*\{[^}]*\}\s*;?", "", transformed)
        # Imported names are global lexical bindings from dependency evaluation; alias
        # forms need a local declaration in this script.
        for _prefix, clause, _quote, _specifier in imports:
            for item in clause.strip().strip("{}").split(","):
                item = item.strip()
                if not item or item == "*":
                    continue
                if item.startswith("*"):
                    continue
                parts = re.split(r"\s+as\s+", item)
                if len(parts) == 2:
                    transformed = f"const {parts[1].strip()} = {parts[0].strip()};\n{transformed}"
        self.evaluate(transformed, filename=key)
        self.module_cache[key] = True

    def _sandboxed(self, token):
        """True when this realm is sandboxed without granting *token*."""
        tokens = self.page.services.sandbox_tokens
        return tokens is not None and token not in tokens

    def node_adopted(self, node):
        if getattr(node, "localName", "") == "script":
            self.dynamic_script_updated(node)

    def dynamic_script_updated(self, node):
        page = self.page
        if page.closed or getattr(node, "_script_executed", False) or (getattr(node, "_resource_started", False) and getattr(node, "_dynamic_started", False)):
            return
        if self._sandboxed("allow-scripts"):
            return
        # A detached script does not fetch or execute; adoption retries once connected.
        parent = getattr(node, "parentNode", None)
        connected = False
        while parent is not None:
            if parent is page.document:
                connected = True
                break
            parent = getattr(parent, "parentNode", None)
        if not connected:
            return
        node._dynamic_started = True
        node._parser_script = False
        node._script_defer = False
        node._resource_started = False
        if node.get_src():
            # Dynamically inserted classic scripts are async by default; async=false
            # keeps insertion order.
            node._resource_async = bool(node.get_async())
            if not node._resource_async:
                self.dom_blocking_scripts += 1
                self.queue.append(node)
            #: A classic script is a no-cors load; a module script, and one that says
            #: ``crossorigin``, asks for CORS.
            script_request = {"method": "GET", "resource_type": "script"}
            if str(node.attributes.get("type", "")).lower() == "module" \
                    or node.attributes.get("crossorigin") is not None:
                script_request["mode"] = "cors"
            page.document_resources.track_document_resource(node, node.get_src(), script_request, "script")
        else:
            # A script's source is its text content: _raw_text only holds the parse-time
            # copy, which a script built with textContent has none of.
            source = node.get_textContent() or getattr(node, "_raw_text", "")
            if source:
                module = str(node.get_type() or "").lower() == "module"
                if module:
                    page.tasks.post(
                        lambda n=node, text=source: self.execute_script(n, text, page.url))
                else:
                    self.execute_script(node, source, page.url)

    def install_inline_handler(self, node, name, source):
        if self._sandboxed("allow-scripts"):
            return
        try:
            name = str(name).lower()
            target = node._handler_target(name)
            if target is None:
                return
            parameters = 'event,source,lineno,colno,error' \
                if name == 'onerror' and target is self.page.window else 'event'
            fn = self.page.v8_context.exec_js('(function(' + parameters + '){' + str(source) + '\n})')
            node._set_handler(name, fn)
        except control_exceptions():
            raise
        except BaseException as exc:
            self.page.lifecycle.dispatch_script_error(node, exc)

    def drain_script_queue(self):
        page = self.page
        while self.queue and getattr(self.queue[0], "_resource_done", False):
            node = self.queue.popleft()
            response, error = node._resource_response, node._resource_error
            if error:
                page.lifecycle.dispatch_script_error(node, error)
            elif response is None:
                self.execute_script(node, getattr(node, "_inline_source", ""), page.url)
            else:
                self.execute_script(node, response.body_text(), response.url or page.url)
                page.lifecycle.dispatch_page_event("load", node, {"bubbles": False})
            if error or response is not None:
                page.lifecycle.dispatch_progress_event(node, "loadend")
            self.dom_blocking_scripts = max(0, self.dom_blocking_scripts - 1)
        if not self.dom_blocking_scripts and page.navigation.parser is not None and not page.navigation.parser.done:
            page.tasks.post(
                lambda generation=page.navigation.generation: page.navigation.resume_parser(generation),
                TaskSource.PARSER,
            )
        if page.navigation.parser_done:
            self.run_deferred_scripts()

    def run_deferred_scripts(self):
        page = self.page
        if page.closed or not page.navigation.parser_done:
            return
        identity = (page.navigation.generation, page.document)
        while self.defer:
            if not page.lifecycle._current_document(identity):
                return
            node = self.defer[0]
            if not getattr(node, "_resource_done", False):
                return
            self.defer.popleft()
            response, error = node._resource_response, node._resource_error
            if error:
                page.lifecycle.dispatch_script_error(node, error)
            else:
                source = getattr(node, "_inline_source", "")
                if response is not None:
                    source = response.body_text()
                self.execute_script(node, source, (response.url if response is not None else page.url) or page.url)
                if not page.lifecycle._current_document(identity):
                    return
                if response is not None:
                    page.lifecycle.dispatch_page_event("load", node, {"bubbles": False})
                    page.lifecycle.dispatch_progress_event(node, "loadend")
        page.lifecycle.maybe_fire_dom_content_loaded()
