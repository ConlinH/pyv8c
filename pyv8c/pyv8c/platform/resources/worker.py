"""Worker-owned requests; transport threads exchange only plain data with the owner."""
from dataclasses import replace
from functools import partial
from threading import Event
from time import perf_counter

from pyv8c import pyv8

from ...foundation.arguments import is_nullish
from ...foundation.host_errors import LoadFailure
from ...foundation.error_policy import control_exceptions
from ...foundation.tasks import TaskSource
from ...foundation.urls import origin_tuple
from ...web.fetch.response import Response
from ...web.fileapi.blob import Blob, _array_buffer_bytes
from .handles import LoadHandle


class WorkerResourceCapability:
    def __init__(self, worker):
        self.worker = worker
        self._session = None
        self._callbacks = {}
        # Only the Worker owner reads or releases these V8-bound closures.
        worker._resource_scope.add(self._callbacks.clear)

    @property
    def session(self):
        if self._session is None:
            self._session = replace(self.worker.config.resource_session, origin=origin_tuple(
                self.worker.script_url, inherited=self.worker.config.creator_origin))
        return self._session

    @property
    def origin(self):
        return self.session.origin

    def load_resource_async(self, url, request, callback):
        return self.worker.resource_ownership.issue(self._start, url, request, callback)

    def _start(self, url, request, callback):
        handle = LoadHandle()
        self._callbacks[handle] = callback
        handle.add_done_callback(self._terminal)
        try:
            options = self._options(request)
        except control_exceptions():
            self._callbacks.pop(handle, None)
            handle.finish()
            raise
        except BaseException as error:
            self._post_result(handle, None, error)
        else:
            self._submit(handle, str(url), options,
                         partial(self._post_result, handle))
        return handle

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

    def _submit(self, handle, url, options, received):
        tasks = self.worker.tasks
        session = self.session  # Freeze the origin on the Worker owner before submission.
        def run():
            try:
                payload = session.load(url, options,
                                            lambda: handle.done or tasks.closed)
            except BaseException as error:
                received(None, LoadFailure(str(error)))
            else:
                received(payload, None)
        tasks.begin_loader()
        try:
            future = self.worker.config.browser.loader_pool.submit(run)
        except BaseException as error:
            tasks.complete_loader()
            received(None, LoadFailure(str(error)))
        else:
            future.add_done_callback(lambda done: tasks.complete_loader())
            handle.bind_transport(future)

    def load_resource(self, url, request=None):
        """Wait for plain transport data, never for another Realm's owner queue."""
        options = self._options(request)
        deadline = self.worker.execution_deadline
        if deadline is not None:
            remaining = max(0, deadline - perf_counter())
            options['timeout'] = min(options['timeout'], remaining) if options['timeout'] else remaining
        completed, answer = Event(), {}
        def start(url, options, callback):
            handle = LoadHandle()
            handle.add_done_callback(lambda done: completed.set() if done.cancelled else None)
            def received(payload, error):
                if handle.finish():
                    callback(payload, error)
            self._submit(handle, url, options, received)
            return handle
        def received(payload, error):
            answer.update(payload=payload, error=error)
            completed.set()
        handle = self.worker.resource_ownership.issue(start, str(url), options, received)
        try:
            while not completed.wait(.01):
                if self.worker.stop_event.is_set() or (deadline is not None and perf_counter() >= deadline):
                    self.worker.v8_context.terminate_operation(self.worker.execution_ticket)
                    raise pyv8.JavaScriptTerminated()
            if handle.cancelled or self.worker.stop_event.is_set():
                raise pyv8.JavaScriptTerminated()
            if answer['error'] is not None:
                raise answer['error']
            return answer['payload']
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
                response = Response(payload.body, {
                    'url': payload.url, 'status': payload.status, 'statusText': payload.status_text,
                    'headers': payload.headers, 'redirected': payload.redirected, 'type': payload.type,
                }, services=self.worker.services)
                response._redirect_crossed = payload.redirect_crossed
            except control_exceptions():
                raise
            except BaseException as exc:
                error = exc
        callback(response, error)
