"""WebSocket ownership shared by Window and Worker realms."""
from contextlib import contextmanager

from ..foundation.error_policy import control_exceptions
from ..foundation.urls import serialize_origin
from .resources.request_headers import ACCEPT_ENCODING, accept_language


class WebSocketServices:
    def __init__(self, profile, environment, tasks, scope, origin, cookie_site):
        self._profile = profile
        self._environment = environment
        self._tasks = tasks
        self._origin = origin
        self._cookie_site = cookie_site
        self._sockets = {}
        self._generation = 0
        self._departing = False
        scope.add(self.retire)

    @property
    def available(self):
        backend = self._profile.network_backend
        return not self._departing and backend is not None and backend.ws_available

    def open(self, url, protocols, callbacks, *, retired):
        backend = self._profile.network_backend
        generation = self._generation

        def document_callback(callback):
            def deliver(*args):
                if self._generation == generation:
                    callback(*args)
            return deliver

        socket = backend.websocket(
            self._tasks, url, protocols,
            {name: document_callback(callback) for name, callback in callbacks.items()},
            self._handshake(),
        )
        self._sockets[socket] = retired
        return socket

    def _handshake(self):
        # The engine owns the Upgrade headers. WebSockets don't send Fetch metadata.
        origin = serialize_origin(self._origin())
        return {
            "origin": origin,
            "site_for_cookies": self._cookie_site(),
            "headers": {
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
                "User-Agent": self._environment.user_agent,
                "Accept-Encoding": ACCEPT_ENCODING,
                "Accept-Language": accept_language(self._environment.languages),
            },
        }

    def discard(self, websocket):
        self._sockets.pop(websocket, None)

    @contextmanager
    def retiring_document(self):
        """Retire before publishing any navigation state or running unload scripts."""
        previous = self._departing
        self._departing = True
        try:
            self.retire()
            yield
        finally:
            self._departing = previous

    def retire(self):
        """Revoke late events before retiring the old document's or worker's sockets."""
        self._generation += 1
        backend = self._profile.network_backend
        first_error = None
        for websocket, retired in tuple(self._sockets.items()):
            retired()
            try:
                if backend is not None:
                    backend.close_websocket(websocket)
            except control_exceptions():
                raise
            except Exception as error:
                if first_error is None:
                    first_error = error
            else:
                self.discard(websocket)
        if first_error is not None:
            raise first_error
