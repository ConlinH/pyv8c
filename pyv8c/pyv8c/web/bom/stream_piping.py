"""Default writable sinks and the asynchronous ReadableStream pipe pump."""
from collections import deque

from pyv8c import pyv8

from ...foundation.arguments import MISSING, dictionary, is_nullish
from ...idl import interface
from ..page_errors import PageTypeError, js_error_value


def _then(value, resolve, reject):
    if isinstance(value, pyv8.JSPromise):
        value._get('then')._invoke(value, resolve, reject)
    else:
        resolve(value)


def _call(callback, receiver, *args):
    if callback is None or callback is pyv8.Undefined:
        return None
    return callback.call(receiver, *args) if isinstance(callback, pyv8.JSObject) else callback(*args)


@interface('WritableStreamDefaultController', None)
class WritableStreamDefaultController:
    def __init__(self, pipe):
        self._pipe = pipe

    def fn_error(self, reason=pyv8.Undefined, *_extra):
        self._pipe.error(reason)

    def get_signal(self):
        return self._pipe.signal


class SinkPipe:
    """Serialize writes and wait for a user sink's start/write/close promises."""
    def __init__(self, sink, services):
        from .abort import AbortSignal
        self.services = services
        self.sink = sink
        members = dictionary(sink) if not is_nullish(sink) else {}
        self.callbacks = {name: members.get(name) for name in ('start', 'write', 'close', 'abort')}
        for name, callback in self.callbacks.items():
            if not is_nullish(callback) and not callable(callback):
                raise PageTypeError(f'The provided {name} callback is not a function.')
        self.signal = AbortSignal(services=services)
        self.controller = WritableStreamDefaultController(self)
        self.queue = deque()
        self.started = False
        self.busy = False
        self.finished = False
        self.closing = False
        self.failed = MISSING
        result = _call(self.callbacks['start'], sink, self.controller)
        _then(result, self._started, self.error)

    def _started(self, *_):
        self.started = True
        self._drain()

    def error(self, reason):
        if self.failed is not MISSING or self.finished:
            return
        self.failed = reason
        while self.queue:
            _name, _value, _resolve, reject = self.queue.popleft()
            reject(js_error_value(reason))

    def _enqueue(self, name, value=MISSING):
        promise, resolve, reject = self.services.realm_services.make_promise()
        if self.failed is not MISSING or self.finished or self.closing:
            reject(js_error_value(self.failed if self.failed is not MISSING else PageTypeError('The stream is closed.')))
            return promise
        if name == 'close':
            self.closing = True
        self.queue.append((name, value, resolve, reject))
        self._drain()
        return promise

    def _drain(self):
        if not self.started or self.busy or not self.queue or self.failed is not MISSING:
            return
        name, value, resolve, reject = self.queue.popleft()
        self.busy = True

        def fulfilled(*_):
            self.busy = False
            if name == 'close':
                self.finished = True
            resolve(pyv8.Undefined)
            self._drain()

        def rejected(reason):
            self.busy = False
            self.error(reason)
            reject(js_error_value(reason))

        try:
            args = (value, self.controller) if name == 'write' else ()
            result = _call(self.callbacks[name], self.sink, *args)
            _then(result, fulfilled, rejected)
        except (pyv8.JSException, PageTypeError) as error:
            rejected(js_error_value(error))

    def write(self, value):
        return self._enqueue('write', value)

    def close(self):
        return self._enqueue('close')

    def abort_stream(self, reason):
        promise, resolve, reject = self.services.realm_services.make_promise()
        if self.finished or self.failed is not MISSING:
            resolve(pyv8.Undefined)
            return promise
        self.signal._abort(reason)
        self.error(reason)
        try:
            result = _call(self.callbacks['abort'], self.sink, reason)
            _then(result, lambda *_: resolve(pyv8.Undefined), reject)
        except (pyv8.JSException, PageTypeError) as error:
            reject(js_error_value(error))
        return promise


class PipeTransfer:
    """Hold both locks until reads, writes and the selected shutdown action settle."""
    def __init__(self, source, destination, *, prevent_abort, prevent_cancel, prevent_close, signal):
        self.source = source
        self.destination = destination
        self.prevent_abort = prevent_abort
        self.prevent_cancel = prevent_cancel
        self.prevent_close = prevent_close
        self.signal = signal
        self.promise, self.resolve, self.reject = source._services.realm_services.make_promise()
        self.reader = source.fn_getReader()
        self.writer = destination.fn_getWriter()
        self.finished = False
        self.shutting_down = False

    def start(self):
        if self.signal is not None:
            self.signal._subscribe(self._on_abort)
        self.source._services.realm_services.enqueue_microtask(self._read)
        return self.promise

    def _finish(self, reason=MISSING):
        if self.finished:
            return
        self.finished = True
        self.reader.fn_releaseLock()
        self.writer.fn_releaseLock()
        if self.signal is not None:
            try:
                self.signal._subscribers.remove(self._on_abort)
            except ValueError:
                pass
        if reason is MISSING:
            self.resolve(pyv8.Undefined)
        else:
            self.reject(js_error_value(reason))

    def _read(self, *_):
        if self.finished or self.shutting_down:
            return
        _then(self.reader.fn_read(), self._chunk, self._source_error)

    def _chunk(self, item):
        if self.finished or self.shutting_down:
            return
        item = dictionary(item)
        if item.get('done'):
            self.shutting_down = True
            if self.prevent_close:
                self._finish()
            else:
                _then(self.writer.fn_close(), lambda *_: self._finish(), self._finish)
        else:
            _then(self.writer.fn_write(item.get('value', pyv8.Undefined)), self._read, self._destination_error)

    def _source_error(self, reason):
        if self.finished or self.shutting_down:
            return
        self.shutting_down = True
        if self.prevent_abort:
            self._finish(reason)
        else:
            _then(self.destination._pipe.abort_stream(reason), lambda *_: self._finish(reason), self._finish)

    def _destination_error(self, reason):
        if self.finished or self.shutting_down:
            return
        self.shutting_down = True
        if self.prevent_cancel:
            self._finish(reason)
        else:
            _then(self.source._pipe.cancel_stream(reason), lambda *_: self._finish(reason), self._finish)

    def _on_abort(self):
        self.source._services.realm_services.enqueue_microtask(self._abort)

    def _abort(self):
        if self.finished or self.shutting_down:
            return
        self.shutting_down = True
        reason = self.signal._reason
        actions = []
        if not self.prevent_abort:
            actions.append(lambda: self.destination._pipe.abort_stream(reason))
        if not self.prevent_cancel:
            actions.append(lambda: self.source._pipe.cancel_stream(reason))

        def next_action(*_):
            if not actions:
                self._finish(reason)
            else:
                _then(actions.pop(0)(), next_action, self._finish)
        next_action()
