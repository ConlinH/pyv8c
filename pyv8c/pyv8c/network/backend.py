"""One context's network stack and its in-flight accounting."""
from dataclasses import replace
from threading import RLock

from ..foundation.close_timing import close_phase
from ..foundation.error_policy import control_exceptions
from ..foundation.host_errors import HostStateError
from ..foundation.logging import logger
from . import host_http
from .http_scheduler import HttpScheduler
from .models import NetworkUnavailableError
from .registry import close_http


def _distinct(*parts):
    """The parts, each once: one engine may be serving both roles."""
    seen = []
    for part in parts:
        if part is not None and all(part is not other for other in seen):
            seen.append(part)
    return seen


class NetworkBackend:
    """The engines of one context; an absent one refuses the calls that need it."""

    def __init__(self, http=None, ws=None, **additional):
        self._http = http
        self._ws = ws
        self._additional = dict(additional)
        self._cookies = None
        self._closed = False
        self._closing = False
        # Engines whose close() failed, kept so a retry touches only those.
        self._failed = ()
        self._lock = RLock()
        self._http_scheduler = None

    @property
    def http_available(self) -> bool: return self._http is not None

    @property
    def ws_available(self) -> bool: return self._ws is not None

    @property
    def http(self):
        """The HTTP engine, or ``None`` when only sockets were assembled."""
        return self._http

    @property
    def ws(self):
        """The WebSocket engine, or ``None`` when only requests were assembled."""
        return self._ws

    def engine(self, kind):
        """Return the engine for a registered role, if one was assembled."""
        if kind == "http":
            return self._http
        if kind == "ws":
            return self._ws
        return self._additional.get(kind)

    @property
    def _engine_instances(self):
        """Assembled engines, with a shared instance listed once."""
        return _distinct(self._http, self._ws, *self._additional.values())

    @property
    def cookies(self):
        """The context's cookie jar, which both engines share."""
        return self._cookies

    @cookies.setter
    def cookies(self, jar):
        self._cookies = jar
        for part in self._engine_instances:
            part.cookies = jar

    def submit(self, tasks, group, request, remaining, *, navigation=False, blocking=False,
               host=False, bind=None):
        """Submit a request or one resource hop under Profile admission."""
        with self._lock:
            if self._closed or self._closing or self._failed:
                raise HostStateError("network backend is closed")
            if self._http_scheduler is None:
                limit = getattr(self._http_engine(), "http_concurrency", 8)
                reserved = min(2, max(0, limit - 2))
                normal = limit - reserved
                # Smaller pools share ordinary capacity with blocking work.
                blocking_slots = int(limit >= 10)
                per_page = min(4, max(1, normal - 1))
                per_origin = min(16, max(min(normal, per_page + 1), normal - 2))
                self._http_scheduler = HttpScheduler(
                    self._start, limit=limit, navigation_slots=reserved, blocking_slots=blocking_slots,
                    per_page=per_page, per_origin=per_origin)
            scheduler = self._http_scheduler
        return scheduler.submit(tasks, group, request, remaining, navigation=navigation, blocking=blocking,
                                host=host, bind=bind)

    def _start(self, job):
        request = job.request
        if job.lane == "blocking":
            request = replace(request, blocking_io=True)
        return self._http_engine().submit_request(request, job.handle, job.remaining)

    async def request(self, tasks, group, request, remaining):
        """Send a host request through this Profile."""
        self._http_engine()
        return await host_http.request(self, tasks, group, request, remaining)

    def _http_engine(self):
        if self._http is None:
            raise NetworkUnavailableError(
                "this context has no HTTP engine; set http_engine= or real_network=True"
            )
        return self._http

    def websocket(self, tasks, url, protocols, callbacks, options=None):
        return self._ws_engine().websocket(tasks, url, protocols, callbacks, options)

    def close_websocket(self, websocket):
        """Close one socket; a context with no socket engine has none to close."""
        engine = self._ws
        if engine is not None:
            engine.close_websocket(websocket)

    def _ws_engine(self):
        if self._ws is None:
            raise NetworkUnavailableError(
                "this context has no WebSocket engine; set ws_engine= or real_network=True"
            )
        return self._ws

    def close(self):
        """Close both engines; a failure keeps the backend for a retry."""
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            parts = self._failed or self._engine_instances
        succeeded = []
        first_error = None
        try:
            if self._http_scheduler is not None:
                self._http_scheduler.close()
            for part in parts:
                try:
                    with close_phase(part, "transport"):
                        if part is self._http:
                            close_http(part)
                        else:
                            close = getattr(part, "close", None)
                            if callable(close):
                                close()
                except control_exceptions():
                    raise
                except BaseException as exc:
                    logger.debug("failed to close a network engine", exc_info=True)
                    if first_error is None:
                        first_error = exc
                else:
                    succeeded.append(part)
        finally:
            with self._lock:
                self._closing = False
                # Anything not confirmed closed stays owned for the next try,
                # including parts a control exception interrupted mid-loop.
                self._failed = tuple(
                    part for part in parts
                    if all(part is not done for done in succeeded)
                )
                if not self._failed and first_error is None:
                    self._closed = True
                    self._http_scheduler = None
                    self._http = self._ws = None
                    self._additional.clear()
        if first_error is not None:
            raise first_error


__all__ = ["NetworkBackend"]
