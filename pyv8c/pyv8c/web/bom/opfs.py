"""Origin-private, profile-lifetime file trees with real directory and file operations."""
from dataclasses import dataclass, field
import threading
import time

from pyv8c import pyv8

from ...features.runtime import FeatureImplementation
from ...foundation.arguments import (MISSING, IdlArgumentError, boolean, dictionary,
                                     dom_string, integer, is_nullish, missing_error)
from ...idl import interface
from ..fileapi.blob import Blob, _array_buffer_bytes
from ..fileapi.file import File
from ..page_errors import DOMException, PageTypeError, dom_exception
from .compression_streams import WritableStream
from .utils import (promise_receiver_error, promise_rejected_in_caller_realm,
                    is_secure_context, settled, window_secure_context)


@dataclass(eq=False)
class _Entry:
    name: str
    kind: str
    parent: object = None
    children: dict = field(default_factory=dict)
    data: bytearray = field(default_factory=bytearray)
    modified: int = field(default_factory=lambda: int(time.time() * 1000))
    removed: bool = False
    sync_handles: int = 0
    sync_exclusive: bool = False
    writable_handles: int = 0


class _Bucket:
    def __init__(self):
        self.root = _Entry('', 'directory')
        self.lock = threading.RLock()


def bucket_directory(services):
    origin = services.realm_services.origin
    document = getattr(services, 'document_services', None)
    window = getattr(document, 'window', None)
    secure = (window_secure_context(window) if window is not None
              else is_secure_context(services.realm_services.url))
    if not secure or not origin or origin[0] == 'opaque':
        raise dom_exception('SecurityError', 'Storage directory access is denied.')
    bucket = services.resource_services.file_system_state(_Bucket)
    return FileSystemDirectoryHandle(bucket, bucket.root, services=services)


def _name(value, method):
    if value is MISSING:
        raise missing_error('FileSystemDirectoryHandle', method, 1, 0)
    name = dom_string(value)
    if not name or name in ('.', '..') or any(c in name for c in ('/', '\\', '\x00')):
        raise PageTypeError(f"Failed to execute '{method}' on 'FileSystemDirectoryHandle': Name is not allowed.")
    return name


def _operation(handle, method, callback):
    try:
        with handle._bucket.lock:
            if handle._entry.removed:
                raise dom_exception('NotFoundError', 'The requested file or directory could not be found.')
            result = callback()
        return settled(handle._services, value=result)
    except (PageTypeError, IdlArgumentError, DOMException, pyv8.JSException) as error:
        return promise_rejected_in_caller_realm(error, handle._services)


@interface('FileSystemHandle', None)
class FileSystemHandle:
    def __init__(self, bucket, entry, *, services=None, **_):
        self._bucket, self._entry, self._services = bucket, entry, services

    def get_kind(self): return self._entry.kind
    def get_name(self): return self._entry.name

    def fn_isSameEntry(self, other=MISSING, *_extra):
        def compare():
            if other is MISSING:
                raise missing_error('FileSystemHandle', 'isSameEntry', 1, 0)
            if not isinstance(other, FileSystemHandle):
                raise PageTypeError("parameter 1 is not of type 'FileSystemHandle'.")
            return self._entry is other._entry and self._bucket is other._bucket
        return _operation(self, 'isSameEntry', compare)

    def fn_queryPermission(self, descriptor=None, *_extra):
        return _operation(self, 'queryPermission', lambda: 'granted')

    fn_requestPermission = fn_queryPermission


@interface('FileSystemDirectoryHandle', 'FileSystemHandle')
class FileSystemDirectoryHandle(FileSystemHandle):
    def _get(self, name, options, kind, method):
        def get():
            key = _name(name, method)
            create = boolean(dictionary(options).get('create', False))
            entry = self._entry.children.get(key)
            if entry is None:
                if not create:
                    raise dom_exception('NotFoundError', 'The requested file or directory could not be found.')
                entry = self._entry.children[key] = _Entry(key, kind, self._entry)
            elif entry.kind != kind:
                raise dom_exception('TypeMismatchError', 'The entry is not of the requested type.')
            cls = FileSystemDirectoryHandle if kind == 'directory' else FileSystemFileHandle
            return cls(self._bucket, entry, services=self._services)
        return _operation(self, method, get)

    def fn_getFileHandle(self, name=MISSING, options=None, *_extra):
        return self._get(name, options, 'file', 'getFileHandle')

    def fn_getDirectoryHandle(self, name=MISSING, options=None, *_extra):
        return self._get(name, options, 'directory', 'getDirectoryHandle')

    def fn_removeEntry(self, name=MISSING, options=None, *_extra):
        def remove():
            key = _name(name, 'removeEntry')
            recursive = boolean(dictionary(options).get('recursive', False))
            entry = self._entry.children.get(key)
            if entry is None:
                raise dom_exception('NotFoundError', 'The requested file or directory could not be found.')
            if entry.children and not recursive:
                raise dom_exception('InvalidModificationError', 'The directory is not empty.')
            pending = [entry]
            while pending:
                item = pending.pop()
                item.removed = True
                pending.extend(item.children.values())
            del self._entry.children[key]
            return pyv8.Undefined
        return _operation(self, 'removeEntry', remove)

    def fn_resolve(self, possible_descendant=MISSING, *_extra):
        def resolve():
            if not isinstance(possible_descendant, FileSystemHandle):
                raise PageTypeError("parameter 1 is not of type 'FileSystemHandle'.")
            if possible_descendant._bucket is not self._bucket:
                return pyv8.Null
            entry = possible_descendant._entry
            path = []
            while entry is not None and entry is not self._entry:
                path.append(entry.name)
                entry = entry.parent
            return list(reversed(path)) if entry is self._entry else pyv8.Null
        return _operation(self, 'resolve', resolve)


@interface('FileSystemFileHandle', 'FileSystemHandle')
class FileSystemFileHandle(FileSystemHandle):
    # Blink installs this Worker-only operation after creating the prototype.
    __idl_context_members__ = {'Worker': ('createSyncAccessHandle',)}

    def fn_getFile(self, *_extra):
        return _operation(self, 'getFile', lambda: File([bytes(self._entry.data)], self._entry.name,
                         {'lastModified': self._entry.modified}, _realm=self._services))

    def fn_createWritable(self, options=None, *_extra):
        def create():
            if self._entry.sync_handles:
                raise dom_exception('NoModificationAllowedError', 'The file is locked by an access handle.')
            return FileSystemWritableFileStream(self._bucket, self._entry,
                boolean(dictionary(options).get('keepExistingData', False)), services=self._services)
        return _operation(self, 'createWritable', create)

    def fn_createSyncAccessHandle(self, options=None, *_extra):
        def create():
            mode = dom_string(dictionary(options).get('mode', 'readwrite'))
            if mode not in ('read-only', 'readwrite', 'readwrite-unsafe'):
                raise PageTypeError('Invalid FileSystemSyncAccessHandle mode.')
            if (self._entry.writable_handles or self._entry.sync_exclusive
                    or (mode == 'readwrite' and self._entry.sync_handles)):
                raise dom_exception('NoModificationAllowedError', 'The file is already locked.')
            self._entry.sync_handles += 1
            self._entry.sync_exclusive = mode == 'readwrite'
            return FileSystemSyncAccessHandle(self._bucket, self._entry, mode)
        return _operation(self, 'createSyncAccessHandle', create)


@interface('FileSystemWritableFileStream', 'WritableStream')
class FileSystemWritableFileStream(WritableStream):
    def __init__(self, bucket, entry, keep_existing=False, *, services=None, **_):
        self._bucket, self._entry = bucket, entry
        self._buffer = bytearray(entry.data) if keep_existing else bytearray()
        self._position = 0
        self._released = False
        entry.writable_handles += 1
        super().__init__({'write': self._write_command, 'close': self._commit,
                          'abort': self._release}, services=services)

    def _write_command(self, command, *_):
        kind = 'write'
        if isinstance(command, (dict, pyv8.JSObject)) and not isinstance(command, Blob):
            raw = _array_buffer_bytes(command)
            if raw is None:
                options = dictionary(command)
                kind = dom_string(options.get('type', pyv8.Undefined))
                if kind == 'seek':
                    self._position = integer(options.get('position', MISSING), bits=64)
                    return
                if kind == 'truncate':
                    size = integer(options.get('size', MISSING), bits=64)
                    self._resize(size)
                    self._position = min(self._position, size)
                    return
                if kind != 'write' or 'data' not in options:
                    raise PageTypeError('Invalid write command.')
                if 'position' in options and not is_nullish(options.get('position')):
                    self._position = integer(options['position'], bits=64)
                command = options['data']
        if isinstance(command, Blob):
            data = command._data
        elif isinstance(command, str):
            data = command.encode('utf-8')
        else:
            data = _array_buffer_bytes(command)
            if data is None:
                if isinstance(command, (bytes, bytearray, pyv8.JSTypeArray)):
                    data = bytes(command)
                else:
                    raise PageTypeError('The data is not a BufferSource, Blob, or string.')
        end = self._position + len(data)
        if end > len(self._buffer):
            self._resize(end)
        self._buffer[self._position:end] = data
        self._position = end

    def _resize(self, size):
        if size < len(self._buffer):
            del self._buffer[size:]
        else:
            self._buffer.extend(b'\x00' * (size - len(self._buffer)))

    def _commit(self):
        try:
            with self._bucket.lock:
                if self._entry.removed:
                    raise dom_exception('NotFoundError', 'The file could not be found.')
                self._entry.data = self._buffer.copy()
                self._entry.modified = int(time.time() * 1000)
        finally:
            self._release()

    def _release(self, *_):
        with self._bucket.lock:
            if not self._released:
                self._released = True
                self._entry.writable_handles -= 1

    def fn_write(self, data=MISSING, *_extra):
        if self.get_locked():
            return promise_rejected_in_caller_realm(PageTypeError('The stream is locked.'), self._services)
        return self._pipe.write(data)

    def fn_seek(self, position=MISSING, *_extra):
        return self.fn_write({'type': 'seek', 'position': position})

    def fn_truncate(self, size=MISSING, *_extra):
        return self.fn_write({'type': 'truncate', 'size': size})


@interface('FileSystemSyncAccessHandle', None)
class FileSystemSyncAccessHandle:
    """An exclusive or shared Worker handle backed by the bucket's byte storage."""
    def __init__(self, bucket, entry, mode='readwrite'):
        self._bucket, self._entry, self._mode = bucket, entry, mode
        self._position = 0
        self._closed = False

    def get_mode(self): return self._mode

    def _check(self, *, writing=False):
        if self._closed:
            raise dom_exception('InvalidStateError', 'The access handle is closed.')
        if writing and self._mode == 'read-only':
            raise dom_exception('NoModificationAllowedError', 'The access handle is read-only.')

    def _at(self, options):
        raw = dictionary(options).get('at', self._position)
        value = integer(raw, bits=64)
        return value

    def fn_write(self, buffer=MISSING, options=None, *_extra):
        self._check(writing=True)
        if buffer is MISSING:
            raise missing_error('FileSystemSyncAccessHandle', 'write', 1, 0)
        data = _array_buffer_bytes(buffer)
        if data is None:
            raise PageTypeError('The buffer is not a BufferSource.')
        position = self._at(options)
        end = position + len(data)
        with self._bucket.lock:
            if end > len(self._entry.data):
                self._entry.data.extend(b'\x00' * (end - len(self._entry.data)))
            self._entry.data[position:end] = data
            self._entry.modified = int(time.time() * 1000)
        self._position = end
        return len(data)

    def fn_read(self, buffer=MISSING, options=None, *_extra):
        self._check()
        if buffer is MISSING:
            raise missing_error('FileSystemSyncAccessHandle', 'read', 1, 0)
        info = buffer._array_buffer_view_info() if isinstance(buffer, pyv8.JSObject) else None
        if info is None:
            raise PageTypeError('The buffer is not an ArrayBufferView.')
        position = self._at(options)
        with self._bucket.lock:
            data = bytes(self._entry.data[position:position + info[1]])
        buffer._write_buffer_bytes(data + buffer._bytes()[len(data):])
        self._position = position + len(data)
        return len(data)

    def fn_getSize(self, *_extra):
        self._check()
        return len(self._entry.data)

    def fn_truncate(self, size=MISSING, *_extra):
        self._check(writing=True)
        if size is MISSING:
            raise missing_error('FileSystemSyncAccessHandle', 'truncate', 1, 0)
        size = integer(size, bits=64)
        with self._bucket.lock:
            if size < len(self._entry.data):
                del self._entry.data[size:]
            else:
                self._entry.data.extend(b'\x00' * (size - len(self._entry.data)))
        self._position = min(self._position, size)

    def fn_flush(self, *_extra):
        self._check(writing=True)
        # Writes already update the bucket; no external file descriptor is used.

    def fn_close(self, *_extra):
        with self._bucket.lock:
            if not self._closed:
                self._closed = True
                self._entry.sync_handles -= 1
                if self._mode == 'readwrite':
                    self._entry.sync_exclusive = False

FEATURE = FeatureImplementation({
    'FileSystemHandle': FileSystemHandle,
    'FileSystemDirectoryHandle': FileSystemDirectoryHandle,
    'FileSystemFileHandle': FileSystemFileHandle,
    'FileSystemWritableFileStream': FileSystemWritableFileStream,
    'FileSystemSyncAccessHandle': FileSystemSyncAccessHandle,
})
