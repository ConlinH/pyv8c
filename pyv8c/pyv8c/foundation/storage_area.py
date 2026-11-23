"""Thread-safe string storage shared by same-origin document wrappers."""

from threading import RLock


class StorageArea:
    def __init__(self, data=None, *, lock=None, on_change=None):
        self._data = {} if data is None else data
        self._lock = RLock() if lock is None else lock
        self._on_change = on_change

    def __len__(self):
        with self._lock:
            return len(self._data)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            if self._data.get(key) != value:
                self._data[key] = value
                self._changed()

    def remove(self, key):
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._changed()

    def clear(self):
        with self._lock:
            if self._data:
                self._data.clear()
                self._changed()

    def keys(self):
        with self._lock:
            return list(self._data)

    def snapshot(self):
        with self._lock:
            return dict(self._data)

    def _changed(self):
        if self._on_change is not None:
            self._on_change()
