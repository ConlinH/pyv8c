"""Byte streams used by the CompressionStream and DecompressionStream APIs.

The compressor is incremental; readers receive Uint8Array chunks as the writer
produces them. A write that emits a chunk waits for the readable side to take it,
matching the transform stream's backpressure in Chromium.
"""

from collections import deque
import zlib

from pyv8c import pyv8

from ...features.runtime import FeatureImplementation
from ...foundation.arguments import (MISSING, IdlArgumentError, abort_signal, boolean,
                                     dictionary, dom_string, is_nullish, missing_error)
from ...idl import interface
from ..page_errors import PageRangeError, PageTypeError, js_error_value
from .utils import promise_receiver_error, promise_rejected_in_caller_realm


_FORMATS = {'gzip': 31, 'deflate': 15, 'deflate-raw': -15}


def _promise(services):
    return services.realm_services.make_promise()


def _bytes(value):
    if value is MISSING:
        raise PageTypeError('A chunk must be a BufferSource.')
    try:
        if isinstance(value, pyv8.JSTypeArray):
            return bytes(value)
        if isinstance(value, pyv8.JSObject) and value._buffer_kind() is not None:
            return bytes(value)
        raise PageTypeError('A chunk must be a BufferSource.')
    except (TypeError, ValueError, AttributeError, IndexError):
        raise PageTypeError('A chunk must be a BufferSource.') from None


class _SourcePipe:
    """A user-provided default stream source and its pending reads."""
    def __init__(self, source, services):
        self.services = services
        self.source = source
        self.members = dictionary(source) if not is_nullish(source) else {}
        self.queue = deque()
        self.readers = deque()
        self.finished = False
        self.disturbed = False
        self.failed = MISSING
        self.pulling = False
        self.pull_again = False
        self.started = False
        self.pull_scheduled = False
        self.controller = ReadableStreamDefaultController(self)
        stream_type = self.members.get('type', pyv8.Undefined)
        self.pull = self.members.get('pull')
        self.cancel = self.members.get('cancel')
        start = self.members.get('start')
        if stream_type is not pyv8.Undefined and dom_string(stream_type) != 'bytes':
            raise PageRangeError("The provided value is not a valid enum value of type ReadableStreamType.")
        for name, callback in (('start', start), ('pull', self.pull), ('cancel', self.cancel)):
            if callback is not None and callback is not pyv8.Undefined and not callable(callback):
                raise PageTypeError(f'The provided {name} callback is not a function.')
        if start is not None and start is not pyv8.Undefined:
            result = self._call(start)
            self._settle(result, self._started, self.error)
        else:
            self._started()

    def _call(self, callback):
        if isinstance(callback, pyv8.JSObject):
            return callback.call(self.source, self.controller)
        return callback(self.controller)

    def _settle(self, result, fulfilled, rejected):
        if isinstance(result, pyv8.JSPromise):
            result._get('then')._invoke(result, fulfilled, rejected)
        else:
            fulfilled()

    def _started(self, *_):
        self.started = True
        self._schedule_pull()

    def _schedule_pull(self):
        if (not self.started or self.pull_scheduled or (not self.readers and len(self.queue) >= 1)
                or self.finished or self.failed is not MISSING):
            return
        if self.pulling:
            self.pull_again = True
            return
        pull = self.pull
        if pull is None or pull is pyv8.Undefined:
            return
        self.pull_scheduled = True
        self.services.realm_services.enqueue_microtask(self._pull)

    def _pull(self):
        self.pull_scheduled = False
        if (not self.started or self.pulling or (not self.readers and len(self.queue) >= 1)
                or self.finished or self.failed is not MISSING):
            return
        self.pulling = True
        try:
            result = self._call(self.pull)
            self._settle(result, self._pulled, self._pull_failed)
        except (pyv8.JSException, PageTypeError) as error:
            self._pull_failed(js_error_value(error))

    def _pulled(self, *_):
        self.pulling = False
        if self.pull_again:
            self.pull_again = False
            self._schedule_pull()

    def _pull_failed(self, error):
        self.pulling = False
        self.error(error)

    def enqueue(self, value):
        if self.finished or self.failed is not MISSING:
            raise PageTypeError('Cannot enqueue into a closing or closed stream.')
        if self.readers:
            _reader, resolve, _reject = self.readers.popleft()
            resolve({'value': value, 'done': False})
        else:
            self.queue.append(value)
        self._schedule_pull()

    def close(self):
        if self.finished or self.failed is not MISSING:
            raise PageTypeError('Cannot close a closing or closed stream.')
        self.finished = True
        if not self.queue:
            while self.readers:
                _reader, resolve, _reject = self.readers.popleft()
                resolve({'value': pyv8.Undefined, 'done': True})

    def error(self, error):
        if self.finished or self.failed is not MISSING:
            return
        self.failed = error
        self.queue.clear()
        while self.readers:
            _reader, _resolve, reject = self.readers.popleft()
            reject(error)

    def release(self, reader):
        remaining = deque()
        while self.readers:
            owner, resolve, reject = self.readers.popleft()
            if owner is reader:
                reject(js_error_value(PageTypeError('Releasing reader')))
            else:
                remaining.append((owner, resolve, reject))
        self.readers = remaining

    def read(self, reader):
        self.disturbed = True
        promise, resolve, reject = _promise(self.services)
        if self.queue:
            resolve({'value': self.queue.popleft(), 'done': False})
            self._schedule_pull()
        elif self.failed is not MISSING:
            reject(self.failed)
        elif self.finished:
            resolve({'value': pyv8.Undefined, 'done': True})
        else:
            self.readers.append((reader, resolve, reject))
            self._schedule_pull()
        return promise

    def cancel_stream(self, reason):
        promise, resolve, reject = _promise(self.services)
        self.queue.clear()
        self.finished = True
        while self.readers:
            _reader, read_resolve, _reject = self.readers.popleft()
            read_resolve({'value': pyv8.Undefined, 'done': True})
        callback = self.cancel
        try:
            result = (callback.call(self.source, reason) if isinstance(callback, pyv8.JSObject)
                      else callback(reason)) if callback is not None and callback is not pyv8.Undefined else None
            self._settle(result, lambda *_: resolve(pyv8.Undefined), reject)
        except (pyv8.JSException, PageTypeError) as error:
            reject(js_error_value(error))
        return promise


@interface('ReadableStreamDefaultController', None)
class ReadableStreamDefaultController:
    def __init__(self, pipe):
        self._pipe = pipe

    def get_desiredSize(self):
        if self._pipe.failed is not MISSING:
            return pyv8.Null
        if self._pipe.finished:
            return 0
        return 1 - len(self._pipe.queue)

    def fn_enqueue(self, chunk=pyv8.Undefined, *_extra):
        self._pipe.enqueue(chunk)

    def fn_close(self, *_extra):
        self._pipe.close()

    def fn_error(self, error=pyv8.Undefined, *_extra):
        self._pipe.error(error)


class _BytePipe:
    def __init__(self, fmt, decompress, services):
        self.services = services
        self.decompress = decompress
        self.codec = (zlib.decompressobj(_FORMATS[fmt]) if decompress
                      else zlib.compressobj(wbits=_FORMATS[fmt]))
        self.gzip_header = fmt == 'gzip' and not decompress
        self.queue = deque()
        self.readers = deque()
        self.finished = False
        self.disturbed = False
        self.failed = None

    def _fail(self, error):
        if isinstance(error, zlib.error):
            detail = str(error).split(': ', 1)[-1]
            error = PageTypeError(f'The compressed data was not valid: {detail}.')
        self.failed = error
        while self.queue:
            _chunk, (_resolve, reject) = self.queue.popleft()
            reject(js_error_value(error))
        while self.readers:
            _resolve, reject = self.readers.popleft()
            reject(js_error_value(error))
        return error

    def _emit(self, chunk, ack):
        if not chunk:
            ack[0](None)
            return
        if self.gzip_header and len(chunk) >= 10:
            # Chromium writes OS=10 (unknown) into the gzip header.
            chunk = chunk[:9] + b'\x0a' + chunk[10:]
            self.gzip_header = False
        item = (pyv8.JSTypeArray(list(chunk), pyv8.ArrayType.Uint8Array), ack)
        if self.readers:
            resolve, _reject = self.readers.popleft()
            resolve({'value': item[0], 'done': False})
            ack[0](None)
        else:
            self.queue.append(item)

    def read(self):
        self.disturbed = True
        promise, resolve, reject = _promise(self.services)
        if self.queue:
            value, ack = self.queue.popleft()
            resolve({'value': value, 'done': False})
            ack[0](None)
        elif self.failed is not None:
            reject(js_error_value(self.failed))
        elif self.finished:
            resolve({'value': pyv8.Undefined, 'done': True})
        else:
            self.readers.append((resolve, reject))
        return promise

    def write(self, value):
        promise, resolve, reject = _promise(self.services)
        if self.failed is not None or self.finished:
            reject(js_error_value(self.failed or PageTypeError('The stream is closed.')))
            return promise
        try:
            output = self.codec.decompress(_bytes(value)) if hasattr(self.codec, 'decompress') else self.codec.compress(_bytes(value))
            self._emit(output, (resolve, reject))
        except (PageTypeError, zlib.error) as error:
            reject(js_error_value(self._fail(error)))
        return promise

    def close(self):
        promise, resolve, reject = _promise(self.services)
        if self.failed is not None or self.finished:
            reject(js_error_value(self.failed or PageTypeError('The stream is closed.')))
            return promise
        try:
            output = self.codec.flush()
            if self.decompress and not self.codec.eof:
                raise PageTypeError('Compressed input was truncated.')
            self.finished = True
            self._emit(output, (resolve, reject))
            while self.readers:
                reader_resolve, _reject = self.readers.popleft()
                reader_resolve({'value': pyv8.Undefined, 'done': True})
        except (PageTypeError, zlib.error) as error:
            reject(js_error_value(self._fail(error)))
        return promise

    def abort_stream(self, reason):
        promise, resolve, _reject = _promise(self.services)
        self._fail(reason)
        resolve(pyv8.Undefined)
        return promise

    cancel_stream = abort_stream


@interface('ReadableStream', None)
class ReadableStream:
    def __init__(self, source=None, *_extra, services=None, _realm=None, **_):
        self._services = services or _realm
        self._pipe = source if isinstance(source, _BytePipe) else _SourcePipe(source, self._services)
        self._reader = None

    def get_locked(self): return self._reader is not None

    def fn_getReader(self, *_extra):
        if self._reader is not None:
            raise PageTypeError('ReadableStream is locked')
        self._reader = ReadableStreamDefaultReader(self)
        return self._reader

    def fn_pipeTo(self, destination=MISSING, options=None, *_extra):
        invalid = promise_receiver_error(self, ReadableStream, 'pipeTo')
        if invalid is not None:
            return invalid
        try:
            if destination is MISSING:
                raise missing_error('ReadableStream', 'pipeTo', 1, 0)
            if not isinstance(destination, WritableStream):
                raise PageTypeError("Failed to execute 'pipeTo' on 'ReadableStream': "
                                    "parameter 1 is not of type 'WritableStream'.")
            settings = dictionary(options)
            prevent_abort = boolean(settings.get('preventAbort', False))
            prevent_cancel = boolean(settings.get('preventCancel', False))
            prevent_close = boolean(settings.get('preventClose', False))
            signal = abort_signal(settings.get('signal'))
            if self.get_locked():
                raise PageTypeError('Cannot pipe a locked stream')
            if destination.get_locked():
                raise PageTypeError('Cannot pipe to a locked stream')
        except (IdlArgumentError, PageTypeError, pyv8.JSException) as error:
            return promise_rejected_in_caller_realm(error, self._services)
        from .stream_piping import PipeTransfer
        return PipeTransfer(self, destination, prevent_abort=prevent_abort,
                            prevent_cancel=prevent_cancel, prevent_close=prevent_close,
                            signal=signal).start()


@interface('WritableStream', None)
class WritableStream:
    def __init__(self, sink=None, *_extra, services=None, _realm=None, **_):
        self._services = services or _realm
        from .stream_piping import SinkPipe
        self._pipe = sink if isinstance(sink, _BytePipe) else SinkPipe(sink, self._services)
        self._writer = None

    def get_locked(self): return self._writer is not None

    def fn_getWriter(self, *_extra):
        if self._writer is not None:
            raise PageTypeError('WritableStream is locked')
        self._writer = WritableStreamDefaultWriter(self)
        return self._writer

    def fn_close(self, *_extra):
        invalid = promise_receiver_error(self, WritableStream, 'close')
        if invalid is not None:
            return invalid
        if self.get_locked():
            return promise_rejected_in_caller_realm(PageTypeError('Cannot close a locked stream'), self._services)
        return self._pipe.close()

    def fn_abort(self, reason=pyv8.Undefined, *_extra):
        invalid = promise_receiver_error(self, WritableStream, 'abort')
        if invalid is not None:
            return invalid
        if self.get_locked():
            return promise_rejected_in_caller_realm(PageTypeError('Cannot abort a locked stream'), self._services)
        return self._pipe.abort_stream(reason)


@interface('ReadableStreamDefaultReader', None)
class ReadableStreamDefaultReader:
    def __init__(self, stream=MISSING, *_extra, **_):
        if stream is MISSING:
            raise missing_error('ReadableStreamDefaultReader', 'constructor', 1, 0, constructor=True)
        if not isinstance(stream, ReadableStream):
            raise PageTypeError('The first argument is not a ReadableStream.')
        if stream._reader is not None:
            raise PageTypeError('ReadableStream is locked')
        self._stream = stream
        stream._reader = self

    def fn_read(self, *_extra):
        if self._stream is None:
            raise PageTypeError('Reader has been released')
        pipe = self._stream._pipe
        return pipe.read(self) if isinstance(pipe, _SourcePipe) else pipe.read()

    def fn_releaseLock(self, *_extra):
        if self._stream is not None:
            if isinstance(self._stream._pipe, _SourcePipe):
                self._stream._pipe.release(self)
            self._stream._reader = None
            self._stream = None


@interface('WritableStreamDefaultWriter', None)
class WritableStreamDefaultWriter:
    def __init__(self, stream=MISSING, *_extra, **_):
        if stream is MISSING:
            raise missing_error('WritableStreamDefaultWriter', 'constructor', 1, 0, constructor=True)
        if not isinstance(stream, WritableStream):
            raise PageTypeError('The first argument is not a WritableStream.')
        if stream._writer is not None:
            raise PageTypeError('WritableStream is locked')
        self._stream = stream
        stream._writer = self

    def fn_write(self, chunk=MISSING, *_extra):
        if self._stream is None:
            raise PageTypeError('Writer has been released')
        return self._stream._pipe.write(chunk) if self._stream._pipe is not None else _resolved(self._stream._services)

    def fn_close(self, *_extra):
        if self._stream is None:
            raise PageTypeError('Writer has been released')
        return self._stream._pipe.close() if self._stream._pipe is not None else _resolved(self._stream._services)

    def fn_releaseLock(self, *_extra):
        if self._stream is not None:
            self._stream._writer = None
            self._stream = None


def _resolved(services):
    promise, resolve, _reject = _promise(services)
    resolve(None)
    return promise


class _CompressionBase:
    def __init__(self, format=MISSING, *_extra, services=None, _realm=None, **_):
        name = type(self).__name__
        if format is MISSING:
            raise missing_error(name, 'constructor', 1, 0, constructor=True)
        fmt = dom_string(format)
        if fmt not in _FORMATS:
            raise PageTypeError(f"Failed to construct '{name}': Unsupported compression format: '{fmt}'")
        self._services = services or _realm
        pipe = _BytePipe(fmt, name == 'DecompressionStream', self._services)
        self._readable = ReadableStream(pipe, services=self._services)
        self._writable = WritableStream(pipe, services=self._services)

    def get_readable(self): return self._readable
    def get_writable(self): return self._writable


@interface('CompressionStream', None)
class CompressionStream(_CompressionBase):
    pass


@interface('DecompressionStream', None)
class DecompressionStream(_CompressionBase):
    pass


from .stream_piping import WritableStreamDefaultController

FEATURE = FeatureImplementation({
    'CompressionStream': CompressionStream,
    'DecompressionStream': DecompressionStream,
    'ReadableStream': ReadableStream,
    'ReadableStreamDefaultController': ReadableStreamDefaultController,
    'WritableStream': WritableStream,
    'WritableStreamDefaultController': WritableStreamDefaultController,
    'ReadableStreamDefaultReader': ReadableStreamDefaultReader,
    'WritableStreamDefaultWriter': WritableStreamDefaultWriter,
})
