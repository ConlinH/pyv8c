"""HTTP submissions for tests that control when a transport finishes."""

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from threading import Event

from pyv8c.network.models import RequestHandle


def resolved(value):
    future = Future()
    future.set_result(value)
    return future


def submit(transport, request):
    """Observe a direct transport submission in tests of the wire itself."""
    return transport.submit_request(request, RequestHandle(), lambda: request.timeout)


class GatedTransport:
    """Let tests deliberately hold or release transport work on a fixture thread."""

    def __init__(self, respond, *, executor=None, interrupt=None):
        self._respond = respond
        self._executor = executor or ThreadPoolExecutor(max_workers=4)
        self._cancel = interrupt

    def submit_request(self, request, handle, remaining):
        registered = Event()

        def send():
            registered.wait()
            if handle.cancelled:
                raise CancelledError
            return self._respond(replace(request, timeout=remaining()))

        future = self._executor.submit(copy_context().run, send)
        try:
            handle.attach_abort(future.cancel if self._cancel is None else
                                lambda: self._cancel(request))
        finally:
            registered.set()
        return future

    def close(self):
        self._executor.shutdown(wait=True, cancel_futures=True)
