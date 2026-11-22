"""Cancellation and completion for a request with late transport binding."""
from functools import partial
from threading import RLock


class LoadHandle:
    """One terminal transition, with transport binding before or after cancel."""

    __slots__ = ("_lock", "_state", "_transport", "_callbacks", "_publication_claimed")

    def __init__(self):
        self._lock = RLock()
        self._state = "pending"
        self._transport = None
        self._callbacks = []
        self._publication_claimed = False

    def add_done_callback(self, callback):
        with self._lock:
            if self._state == "pending":
                self._callbacks.append(callback)
                return
        callback(self)

    @property
    def cancelled(self):
        with self._lock:
            return self._state == "cancelled"

    @property
    def done(self):
        with self._lock:
            return self._state != "pending"

    def bind_transport(self, transport):
        with self._lock:
            cancelled = self._state == "cancelled"
            if self._state == "pending":
                self._transport = transport
        if cancelled and transport is not None:
            transport.cancel()

    def unbind_transport(self, transport):
        with self._lock:
            if self._transport is transport:
                self._transport = None

    def observe_transport(self, transport, future, receive):
        """Detach a completed hop before handing its outcome to the request."""
        future.add_done_callback(partial(self._receive, transport, receive))

    def _receive(self, transport, receive, future):
        self.unbind_transport(transport)
        receive(future)

    def claim_publication(self):
        """Choose publication or cancellation before starting commit I/O."""
        with self._lock:
            if self._state != "pending" or self._publication_claimed:
                return False
            self._publication_claimed = True
            return True

    def cancel(self):
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "cancelled"
            transport, self._transport = self._transport, None
            callbacks, self._callbacks = self._callbacks, []
        try:
            if transport is not None:
                transport.cancel()
        finally:
            for callback in callbacks:
                callback(self)
        return True

    def finish(self):
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "finished"
            self._transport = None
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)
        return True


