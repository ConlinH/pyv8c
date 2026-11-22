"""Worker-owned requests; transport threads exchange only plain data with the owner."""
from contextvars import copy_context
from dataclasses import replace
from functools import partial
from threading import Event, get_ident
from time import perf_counter

from pyv8c import pyv8

from ...foundation.arguments import is_nullish
from ...foundation.host_errors import LoadFailure, RequestTimeout
from ...foundation.error_policy import control_exceptions
from ...foundation.tasks import TaskSource
from ...foundation.urls import origin_tuple
from ...network.models import NetworkRequest
from ...web.fileapi.blob import Blob, _array_buffer_bytes
from .response import web_response


class WorkerResourceCapability:
    supports_sync_xhr_timeout = True

    def __init__(self, worker):
        self.worker = worker
        self._session = None
        self._callbacks = {}
        # Only the Worker owner reads or releases these V8-bound closures.
        worker._resource_scope.add(self._callbacks.clear)

    @property
    def session(self):
        if self._session is None:
            script_url = self.worker.script_url
            self._session = replace(
                self.worker.config.resource_session, base_url=script_url,
                origin=origin_tuple(script_url, inherited=self.worker.config.creator_origin),
            )
        return self._session

    @property
    def origin(self):
        return self.session.origin

    def file_system_state(self, factory):
        key = self.worker.config.file_system_key or (self.origin, self.origin)
        return self.session.profile.file_system_state(key, factory)

    def load_resource_async(self, url, request, callback):
        return self.worker.resource_ownership.issue(self._start, url, request, callback)

    def _start(self, handle, url, request, callback):
        self._callbacks[handle] = callback
        handle.add_done_callback(self._terminal)
        try:
            options = self._options(request)
            load = self.session.prepare(str(url), options)
        except control_exceptions():
            self._callbacks.pop(handle, None)
            handle.finish()
            raise
        except BaseException as error:
            self._post_result(handle, None, error)
        else:
            self._submit(handle, load, partial(self._post_result, handle))

    def _options(self, request):
        options = dict(request or {})
        options.setdefault('initiator_url', self.worker.script_url)
        headers = options.get('headers')
        options['headers'] = dict(getattr(headers, '_data', headers) or {})
        body = options.get('body')
        binary = _array_buffer_bytes(body)
        options['body'] = (None if is_nullish(body) else binary if binary is not None
                           else body if isinstance(body, (str, bytes)) else Blob([body])._data)
        timeout = options.get('timeout')
        options['timeout'] = None if is_nullish(timeout) else float(timeout)
        return options

    def _submit(self, handle, load, received, *, blocking=False):
        tasks = self.worker.tasks
        task_services = self.worker.services.task_services
        session = self.session
        browser, background = self.worker.config.browser, self.worker.config.background
        request_context = copy_context()
        owner = get_ident()
        stop_event = self.worker.stop_event

        def owner_gone():
            return tasks.closed or stop_event.is_set()

        steps = session.steps(load, handle, owner_gone)
        tasks.begin_loader()

        def finish(payload, error):
            steps.close()
            try:
                if isinstance(error, RequestTimeout) or error is None:
                    failure = error
                else:
                    failure = LoadFailure(str(error))
                if not handle.done:
                    received(payload, failure)
            finally:
                tasks.complete_loader()

        def advance(done=None):
            if handle.done or owner_gone():
                finish(None, None)
                return
            try:
                value = None if done is None else done.result()
            except BaseException as error:
                finish(None, error)
                return
            try:
                step = steps.send(value)
                if isinstance(step, NetworkRequest):
                    job = session.submit(task_services, load, step, blocking=blocking,
                                         bind=handle.bind_transport)
                    handle.observe_transport(job, job.result, step_done)
                else:
                    def call():
                        if handle.done or owner_gone():
                            raise LoadFailure("Worker request cancelled")
                        return step.call()

                    future = background.submit(browser.loader_pool, request_context.copy().run, call,
                                               kind="worker-resource")
                    handle.bind_transport(future)
                    handle.observe_transport(future, future, step_done)
            except StopIteration as terminal:
                finish(terminal.value, None)
            except BaseException as error:
                finish(None, error)

        def step_done(done):
            if get_ident() != owner:
                request_context.copy().run(advance, done)
                return
            # Already-completed futures invoke callbacks inline. Keep response
            # processing off the Worker owner in that case as well.
            try:
                background.submit(browser.loader_pool, request_context.copy().run,
                                  advance, done, kind="worker-resource-step")
            except BaseException as error:
                finish(None, error)

        advance()

    def load_resource(self, url, request=None):
        """Wait for plain transport data, never for another Realm's owner queue."""
        options = self._options(request)
        deadline = self.worker.execution_deadline
        load = self.session.prepare(str(url), options)
        completed, answer = Event(), {}
        def start(handle, _url, _options, callback):
            handle.add_done_callback(lambda done: completed.set() if done.cancelled else None)
            def received(payload, error):
                if handle.finish():
                    callback(payload, error)
            self._submit(handle, load, received, blocking=True)
        def received(payload, error):
            answer.update(payload=payload, error=error)
            completed.set()
        handle = self.worker.resource_ownership.issue(start, str(url), options, received)
        try:
            while True:
                if self.worker.stop_event.is_set() or (deadline is not None and perf_counter() >= deadline):
                    self.worker.v8_context.terminate_operation(self.worker.execution_ticket)
                    raise pyv8.JavaScriptTerminated()
                if completed.is_set():
                    break
                try:
                    budget = load.remaining()
                except RequestTimeout:
                    if completed.is_set():
                        break
                    raise
                completed.wait(.01 if budget is None else min(.01, budget))
            if handle.cancelled:
                raise pyv8.JavaScriptTerminated()
            if answer['error'] is not None:
                raise answer['error']
            return web_response(answer['payload'], self.worker.services)
        finally:
            if not handle.done:
                handle.cancel()

    def _terminal(self, handle):
        if handle.cancelled:
            self.worker.tasks.post_task(partial(self._discard, handle), TaskSource.NETWORKING,
                                        label='Worker fetch cleanup')

    def _discard(self, handle):
        self._callbacks.pop(handle, None)

    def _post_result(self, handle, payload, error):
        if handle.done:
            return
        try:
            ticket = self.worker.tasks.post_task(
                partial(self._deliver, handle, payload, error), TaskSource.NETWORKING,
                label='Worker fetch response')
        except BaseException:
            handle.cancel()
            raise
        if ticket is None:
            handle.cancel()

    def _deliver(self, handle, payload, error):
        callback = self._callbacks.pop(handle, None)
        if callback is None or not handle.finish():
            return
        response = None
        if error is None:
            try:
                response = web_response(payload, self.worker.services)
            except control_exceptions():
                raise
            except BaseException as exc:
                error = exc
        callback(response, error)
