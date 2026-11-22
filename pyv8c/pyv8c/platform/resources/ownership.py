"""Page request tickets, released independently from result delivery."""
from threading import RLock

from ...foundation.load_handle import LoadHandle


class ResourceOwnership:
    """One ResourceScope cleanup entry owns all active request tickets."""

    def __init__(self, scope):
        self._lock = RLock()
        self._tickets = set()
        self._closed = False
        scope.add(self.close)

    @property
    def active_count(self):
        with self._lock:
            return len(self._tickets)

    def _register(self, document_bound=False):
        with self._lock:
            ticket = _Ticket(self, document_bound)
            if self._closed:
                ticket.cancelled = True
                ticket.released = True
            else:
                self._tickets.add(ticket)
            return ticket

    def track(self, handle):
        ticket = self._register()
        ticket.bind(handle)
        return ticket

    def issue(self, start, url, request, callback, *, document_bound=False):
        ticket = self._register(document_bound)
        handle = LoadHandle()
        ticket.bind(handle)
        if handle.cancelled:
            return handle

        def complete(response, error):
            with self._lock:
                if ticket.cancelled or ticket.delivered:
                    return
                ticket.delivered = True
                ticket.release()
            callback(response, error)

        try:
            start(handle, url, request, complete)
            return handle
        except BaseException:
            ticket.cancel()
            raise

    def cancel_document_requests(self, *, include_realm=False):
        with self._lock:
            tickets = tuple(ticket for ticket in self._tickets if include_realm or ticket.document_bound)
        self._cancel(tickets)

    def close(self):
        with self._lock:
            self._closed = True
            tickets = tuple(self._tickets)
        self._cancel(tickets)

    @staticmethod
    def _cancel(tickets):
        first_error = None
        for ticket in tickets:
            try:
                ticket.cancel()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


class _Ticket:
    __slots__ = ("owner", "handle", "released", "delivered", "cancelled", "document_bound")

    def __init__(self, owner, document_bound):
        self.owner = owner
        self.handle = None
        self.released = False
        self.delivered = False
        self.cancelled = False
        self.document_bound = document_bound

    def release(self):
        with self.owner._lock:
            self.released = True
            self.owner._tickets.discard(self)

    def bind(self, handle):
        with self.owner._lock:
            self.handle = handle
            cancelled = self.cancelled or (self.owner._closed and not self.delivered)
            if cancelled:
                self.cancelled = True
                self.release()
        if cancelled:
            handle.cancel()
        else:
            handle.add_done_callback(self._terminal)

    def _terminal(self, handle):
        with self.owner._lock:
            self.cancelled = self.cancelled or handle.cancelled
            self.release()

    def cancel(self):
        with self.owner._lock:
            self.cancelled = True
            self.release()
            handle = self.handle
        if handle is not None:
            handle.cancel()
