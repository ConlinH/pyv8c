"""Worker thread and message-loop implementation."""

import threading
import sys
import time
from collections import deque
from functools import partial

from pyv8c import pyv8

from ...foundation.arguments import is_nullish
from ...foundation.logging import logger
from ...foundation.agent_tasks import AgentTaskQueue, TaskContext
from ...foundation.error_policy import control_exceptions, is_host_failure
from ...foundation.lifecycle import ResourceScope
from ...foundation.host_errors import RealmBusyError
from ...foundation.rejections import RejectionTracker
from ...foundation.rejection_state import WeakPromiseState
from ...foundation.tasks import TaskSource
from ...foundation.foreground import ForegroundSource
from ..dom.events import PromiseRejectionEvent
from ..page_errors import PageError
from ..clone import structured_clone, TransferPlan
from ..fetch.events import ErrorEvent, MessageEvent

class WorkerRuntime:
    def __init__(self, owner, script_url):
        self.owner = owner
        self.services = owner._services
        self.script_url = str(script_url)
        self._stop = threading.Event()
        self._message_lock = threading.RLock()
        self._pending_messages = deque()
        self._operation_lock = threading.RLock()
        self._operation = None
        self._stopped_operations = set()
        self._lifecycle_scope = ResourceScope()
        self.executor = self.services.worker_services.create_executor("pyv8c-worker")
        # V8 reports a rejection before the turn that caused it ends, so the
        # candidates wait for this realm's own executor boundary to be
        # published; see foundation.rejections.
        self.rejections = None
        self._realm = None
        self._context = None
        self._scope = None
        self._agent_tasks = None
        self._foreground = None
        self._message_lane = None
        self._devtools = None
        self.debug_target = None
        # Capture parent configuration and initiate loading on its owner. The
        # worker executor stays available for termination while I/O is pending.
        self._config = None
        self._source = None
        self._source_error = None
        try:
            self._config = self.services.worker_services.worker_configuration()
            handle = self.services.resource_services.load_worker_source_async(
                self.script_url, self._source_ready)
            self._lifecycle_scope.add(handle.cancel)
            handle.add_done_callback(self._source_finished)
        except BaseException as exc:
            self._source_ready(None, exc)
        self.services.worker_services.register_runtime(self)

    def _source_finished(self, handle):
        # Owner invalidation suppresses delivery. It must still wake teardown
        # for a worker whose realm has never been initialized.
        if handle.cancelled and not self._stop.is_set():
            self.stop()

    def _source_ready(self, source, error):
        with self._message_lock:
            if self._stop.is_set():
                return
            if error is None:
                self.script_url = source.url
                source = source.text
            self._source, self._source_error = source, error
            self.executor.post(self._run, _task_lane=None)

    def _source_of_worker(self):
        """Read only the source handed off by the parent's networking task."""
        if self._source_error is not None:
            raise self._source_error
        return self._source

    def _run(self):
        if self._stop.is_set():
            return
        try:
            config = self._config
            self._config = None
            if config is None and self._source_error is not None:
                # Configuration failures remain host startup failures, rather
                # than being misreported as a site's unavailable script.
                raise self._source_error
            try:
                source = self._source_of_worker()
                self._source = None
            except BaseException as exc:  # a script that never arrives
                #: A worker whose script cannot be fetched is still an ``error`` on the
                #: Worker, as it is in a browser; why it could not be fetched is the
                #: host's to read.
                logger.warning("worker {} could not be fetched: {}", self.script_url, exc)
                self.services.task_services.post_task(
                    lambda: self.owner._receive_error(
                        PageError("Failed to load the worker's script")))
                self._stop.set()
                self._teardown_owner()
                return
            self._realm = self.services.worker_services.create_worker_realm(config)
            self._realm.script_url = self.script_url
            self._realm.stop_event = self._stop
            self._realm.services.task_services.bind_websocket_lifetime(self._lifecycle_scope)
            self._lifecycle_scope.add(self._realm.resource_ownership.close)
            # The worker drives its own task queue from this thread, so timers
            # and microtasks progress while the parent page is idle.
            self._realm.tasks.set_error_sink(self._report_error)
            self._scope = self._scope_class()(
                runtime=self,
                environment=config.environment,
                services=self._realm.services,
            )
            self._context = self._realm.initialize(self._scope)
            self._foreground = ForegroundSource(self._realm.isolate, self._run_foreground)
            self.executor.attach_foreground(self._foreground)
            self._realm.attach_rendering(self.executor, partial(self._invoke, _background=True))
            self._agent_tasks = AgentTaskQueue()
            self._realm.tasks.bind_scheduler(self._agent_tasks, partial(self._invoke, _background=True), TaskContext(self._realm))
            self.executor.attach_task_queue(self._agent_tasks, owns_checkpoint=True)
            self.executor.bind_command_queue(self._realm.tasks, self._invoke, owns_checkpoint=True)
            with self._message_lock:
                self._message_lane = self._realm.tasks
                while self._pending_messages:
                    self._enqueue_parent_message(self._pending_messages.popleft())
            self.rejections = RejectionTracker(
                self._dispatch_rejection, self._realm.tasks.post, WeakPromiseState(self._context))
            self._realm.callback_boundary = self._checkpoint
            self.executor.attach_checkpoint(self._checkpoint)
            self.executor.attach_deadline_source(self._context)
            # A worker global schedules its own work, so the failures V8 raises
            # on its own are reported from this realm, not the page's.
            self._realm.v8_context.set_error_hooks(
                self._report_uncaught, self.rejections.record,
            )
            devtools = config.devtools
            self._devtools = devtools
            if devtools is not None:
                devtools.register_worker(self, config.debug_parent)
                if self.debug_target is not None:
                    self.debug_target.before_script()
            if source:
                try:
                    self._invoke(self._context.exec_js, source, filename=self.script_url, _background=True)
                finally:
                    if self.debug_target is not None:
                        self.debug_target.after_script()
            if not self._stop.is_set():
                self._started()
        except BaseException as exc:  # a worker's failure is reported, not raised
            self._report_error(exc)
            self._stop.set()
            self._cleanup_owner()
            self._lifecycle_scope.close()
            self.executor.close()

    def _scope_class(self):
        """The global scope a worker of this kind runs its script in."""
        from .worker import DedicatedWorkerGlobalScope
        return DedicatedWorkerGlobalScope

    def _invoke(self, callback, *args, _background=False, **kwargs):
        """One worker task owns its budget through callback cleanup/checkpoint."""
        if self._stop.is_set():
            return None
        context = self._context
        operation = previous = None
        realm = self._realm
        previous_deadline, previous_ticket = realm.execution_deadline, realm.execution_ticket
        if context is not None and pyv8.current_context(self._realm.isolate) is None:
            budget = (self.executor.remaining_budget() if self.executor.has_operation
                      else self.executor.default_timeout)
            if budget is not None and budget <= 0:
                raise RealmBusyError('worker operation deadline expired before execution')
            ticket = context.begin_operation(budget or 0)
            operation = (context, ticket)
            realm.execution_ticket = ticket
            realm.execution_deadline = time.perf_counter() + budget if budget else None
            with self._operation_lock:
                previous = self._operation
                self._operation = operation
                # Stop may have arrived between the first state check and binding.
                if self._stop.is_set():
                    if context.terminate_operation(ticket):
                        self._stopped_operations.add(ticket)
        try:
            try:
                if not self._stop.is_set():
                    return callback(*args, **kwargs)
            finally:
                if not isinstance(sys.exception(), control_exceptions()):
                    self._checkpoint()
        except pyv8.JavaScriptTerminated:
            with self._operation_lock:
                stopped = operation is not None and operation[1] in self._stopped_operations
            if not _background or not stopped:
                raise
        finally:
            if operation is not None:
                with self._operation_lock:
                    self._operation = previous
                    context.end_operation(operation[1])
                    self._stopped_operations.discard(operation[1])
                realm.execution_deadline, realm.execution_ticket = previous_deadline, previous_ticket

    def import_scripts(self, urls):
        from urllib.parse import urljoin, urlsplit
        from ..page_errors import PageTypeError, NetworkError, dom_exception
        from ...foundation.host_errors import LoadFailure
        if self.owner._worker_type == 'module':
            raise PageTypeError('Module scripts do not support importScripts().')
        targets = []
        for url in urls:
            try:
                target = urljoin(self.script_url, url)
                parts = urlsplit(target)
                if not parts.scheme:
                    raise ValueError('relative URL has no base')
                parts.port
            except ValueError as error:
                raise dom_exception("SyntaxError", str(error)) from error
            targets.append(target)
        for target in targets:
            try:
                response = self._realm.services.resource_services.load_resource(target, {
                    'method': 'GET', 'resource_type': 'script', 'destination': 'script',
                    'initiator_url': self.script_url, 'credentials': 'same-origin'})
            except LoadFailure as error:
                raise NetworkError(str(error)) from error
            if not 200 <= response.status < 300:
                raise NetworkError('Worker script could not be fetched.')
            body = response.body
            source = body.decode('utf-8-sig', errors='replace') if isinstance(body, bytes) else str(body or '')
            self._context.exec_js(source, filename=response.url or target)

    def _checkpoint(self):
        realm = self._realm
        if (not self._stop.is_set() and realm is not None and realm.v8_context is not None
                and pyv8.current_context(realm.isolate) is None):
            realm.microtasks.checkpoint(timeout=self.executor.remaining_budget() or 0)
            if self.rejections is not None:
                self.rejections.flush()

    def _started(self):
        """What a worker of this kind does once its script has run."""

    def _dispatch_message(self, value, ports=()):
        if self._stop.is_set() or self._scope is None:
            return
        try:
            #: A message delivered to a worker carries no origin and is a real event.
            self._scope._dispatch_event(MessageEvent("message", {
                "data": value, "origin": "", "ports": ports, "isTrusted": True,
            }, services=self._realm.services if self._realm is not None else None))
        except control_exceptions():
            raise
        except BaseException as exc:  # a message error is reported, not raised
            self._report_error(exc)

    def _cleanup_owner(self):
        self._source = self._source_error = None
        self._config = None
        with self._message_lock:
            self._message_lane = None
            pending, self._pending_messages = self._pending_messages, deque()
        pending.clear()
        first_error = None
        if self._foreground is not None:
            self.executor.detach_foreground(self._foreground)
            self._foreground = None
        devtools = self._devtools
        if devtools is not None:
            try:
                devtools.unregister(self)
                self._devtools = None
            except BaseException as exc:
                first_error = exc
        if self.rejections is not None:
            self.rejections.close()
        if self._realm is not None:
            try:
                self._realm.close()
                self._realm = None
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if self._realm is None:
            self._context = None
            self._scope = None
            if self._agent_tasks is not None:
                self.executor.detach_task_queue(self._agent_tasks)
                self._agent_tasks.close()
                self._agent_tasks = None
        if first_error is not None:
            raise first_error
        self.services.task_services.post_task(self.owner._finish_lifecycle)

    def _run_foreground(self):
        try:
            return self._invoke(self._context._run_foreground_task, _background=True)
        finally:
            if not self._stop.is_set():
                self._checkpoint()

    def _report_uncaught(self, message, source, line, column, error):
        """Report an exception V8 raised inside this worker's realm.

        The global sees it first; the parent hears through the Worker object,
        which carries text and position because a value cannot cross realms.
        """
        message = str(message)
        filename = "" if is_nullish(source) else str(source)
        line, column = int(line), int(column)
        scope = self._scope
        if scope is not None:
            try:
                scope._dispatch_event(ErrorEvent("error", {"isTrusted": True, 
                    "message": message,
                    "filename": filename,
                    "lineno": line,
                    "colno": column,
                    "error": error,
                }))
            except control_exceptions():
                raise
            except BaseException:  # reporting must not break the worker
                logger.debug("failed to report a worker error", exc_info=True)
        self.services.task_services.post_task(
            lambda: self.owner._receive_uncaught(message, filename, line, column)
        )

    def _dispatch_rejection(self, event_type, promise, reason):
        """Fire a rejection event on the worker's own global scope."""
        scope = self._scope
        if scope is None or self._stop.is_set():
            return
        try:
            event = PromiseRejectionEvent(event_type, {
                "promise": promise, "reason": reason,
                "cancelable": event_type == "unhandledrejection",
            }, services=self._realm.services)
            event._trusted = True
            scope._dispatch_event(event)
        except control_exceptions():
            raise
        except BaseException:  # reporting must not break the worker
            logger.debug("failed to report a worker rejection", exc_info=True)

    def _report_error(self, exc):
        #: An ``error`` event is what a script does wrong; the host's own failure is
        #: the host's, so it is logged here and the page is not told about it.
        if is_host_failure(exc):
            logger.warning("worker {} failed in the host: {}", self.script_url, exc)
            return
        self.services.task_services.post_task(lambda: self.owner._receive_error(exc))

    def post_from_parent(self, value, transfer=()):
        if not self._stop.is_set():
            # The message is cloned before the list is applied, so a buffer it is about
            # to detach still carries its bytes.  The port moves out of the page now;
            # the realm it moves into is the worker's, which ``_run`` has already been
            # posted ahead of.
            #: The page's context, so binary data is read as the bytes it holds rather
            #: than element by element.
            plan = TransferPlan(transfer, caller='Worker')
            data = structured_clone(value, context=pyv8.current_context(), transfer=plan)
            ports = plan.commit(self.services)
            with self._message_lock:
                if self._stop.is_set():
                    return
                if self._message_lane is None:
                    self._pending_messages.append((data, ports))
                else:
                    self._enqueue_parent_message((data, ports))

    def _enqueue_parent_message(self, payload):
        # Caller holds the publication lock, including while draining startup
        # messages. A post after publication cannot overtake that backlog.
        lane = self._message_lane
        if self._stop.is_set() or lane is None:
            return
        lane.post_task(lambda: self._dispatch_from_parent(payload), TaskSource.POSTED_MESSAGE,
                       label='Worker message from parent')

    def _dispatch_from_parent(self, payload):
        value, ports = payload
        realm = self._realm
        for port in ports:
            port._services = realm.services if realm is not None else None
        self._dispatch_message(value, ports)

    def post_to_parent(self, value, transfer=()):
        # Cloned first: the list is about to detach whatever buffer it names.
        plan = TransferPlan(transfer, caller='WorkerGlobalScope')
        data = structured_clone(value, context=self._context, transfer=plan)
        ports = plan.commit(self.services)
        self.services.task_services.post_task(
            lambda: self.owner._receive_message(data, ports), TaskSource.POSTED_MESSAGE,
            label='Worker message to parent', document_bound=True)

    def stop(self):
        """Ask the worker to take itself down, and return without waiting for it.

        The teardown belongs to the worker's own thread.  Waiting for it here spends the
        worker's delay in the *caller's* realm, and a page's own teardown calls this from
        inside the page realm -- which is how a page ends up with its timers, port
        deliveries and pending callbacks held for as long as a worker takes to answer.
        External termination interrupts the current native operation ticket. A worker
        closing itself finishes its current callback without another checkpoint.
        Python callbacks executing on this owner must still return before cleanup;
        source loaders run separately and do not delay owner teardown.
        """
        if self.executor.closed and self._realm is None:
            return
        self._stop.set()
        self._lifecycle_scope.close()
        if not self.executor.is_owner_thread():
            with self._operation_lock:
                if self._operation is not None:
                    context, ticket = self._operation
                    if context.terminate_operation(ticket):
                        self._stopped_operations.add(ticket)
        target = self.debug_target
        if target is not None:
            target.wake_waiting_owner()
        # Posted, not called: the command is served once whatever the worker is doing
        # ends, and the two steps below run on the worker's thread, where they belong.
        self.executor.post(self._teardown_owner, _task_lane=None)

    def _teardown_owner(self):
        """The teardown itself, on the worker's own thread.

        Closing the executor from inside it is what keeps that join off the caller: the
        driver leaves its loop when this command returns and the thread ends by itself.
        """
        self._cleanup_owner()
        self.executor.close()
        self._lifecycle_scope.close()
        self.services.worker_services.discard_runtime(self)


class SharedWorkerRuntime(WorkerRuntime):
    """A shared worker: once its script has run it is handed the port it serves on."""

    def _scope_class(self):
        from .worker import SharedWorkerGlobalScope
        return SharedWorkerGlobalScope

    def _started(self):
        port = getattr(self.owner, "_worker_port", None)
        if self._scope is None or port is None:
            return
        # The far end of the page's port lives here, so its messages arrive on this
        # realm's queue; the page-side port flushes whatever it was given meanwhile.
        port._services = self._realm.services
        port._entangle(self.owner._port)
        self.owner._port._entangle(port)
        self._scope._dispatch_event(MessageEvent("connect", {"isTrusted": True, "ports": [port]}))

__all__ = ["WorkerRuntime", "SharedWorkerRuntime"]
