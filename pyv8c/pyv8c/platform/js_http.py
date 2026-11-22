"""Host calls that run the page's own ``fetch`` and ``XMLHttpRequest``.

Reached through ``page.net.fetch``/``page.net.xhr``.  The realm's JavaScript performs
the request, so a page that replaced either API -- instrumentation, a test harness --
is the implementation that answers, and the page's own rules apply: CORS, the
resource interceptor, the cache.
"""
from __future__ import annotations

import json
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import RLock
from weakref import ref

from pyv8c import pyv8

from ..foundation.host_errors import LoadFailure, HostStateError


@dataclass
class JsHttpResponse:
    """What the page's ``fetch``/``XMLHttpRequest`` answered, read host-side."""

    url: str
    status: int
    status_text: str = ""
    headers: dict = field(default_factory=dict)
    body: str = ""
    redirected: bool = False

    @property
    def ok(self) -> bool:
        """Whether the status is a success status."""
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        """The body as text."""
        return self.body

    def json(self):
        """The body parsed as JSON, raising the usual ``json`` errors."""
        return json.loads(self.body)


# Resolve with plain data. The API lookup happens on every invocation.
_DRIVER = """(async (kind, url, init) => {
  try {
    if (kind === 'fetch') {
      const response = await fetch(url, init);
      const headers = [];
      if (response.headers && typeof response.headers.entries === 'function') {
        for (const pair of response.headers.entries()) headers.push([String(pair[0]), String(pair[1])]);
      }
      const status = Number(response.status) || 0;
      const result = {ok: true, status: status - status === 0 ? status : 0,
              statusText: String(response.statusText == null ? '' : response.statusText),
              url: String(response.url || url), redirected: !!response.redirected,
              headers: headers};
      result.text = (await response.text()) ?? '';
      return result;
    }
    const answer = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.onreadystatechange = () => {
        if (xhr.readyState !== 4) return;
        const status = Number(xhr.status) || 0;
        resolve({status: status - status === 0 ? status : 0,
                 statusText: String(xhr.statusText == null ? '' : xhr.statusText),
                 url: String(xhr.responseURL || url),
                 text: String(xhr.responseText == null ? '' : xhr.responseText)});
      };
      xhr.onerror = () => reject({name: 'LoadFailure', message: 'XMLHttpRequest failed'});
      xhr.open(init.method, url);
      if (init.credentials === 'include') xhr.withCredentials = true;
      for (const [name, value] of init.headers) xhr.setRequestHeader(name, value);
      if ('body' in init) xhr.send(init.body);
      else xhr.send();
    });
    return {ok: true, status: answer.status, statusText: answer.statusText,
            url: answer.url, headers: [], text: answer.text,
            redirected: answer.url !== new URL(url, location.href).href};
  } catch (error) {
    return {ok: false, name: (error && error.name) || 'Error',
            message: String((error && error.message) || error)};
  }
})"""


def _script_data(value, parents=None):
    """Pass data into the page without exposing any host objects."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if value is None:
        return pyv8.Null
    if not isinstance(value, (dict, list, tuple)):
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
    if parents is None:
        parents = set()
    identity = id(value)
    if identity in parents:
        raise ValueError("Circular reference detected")
    parents.add(identity)
    try:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    if not (key is None or isinstance(key, (int, float))):
                        raise TypeError("header keys must be strings, numbers, booleans or None")
                    key = json.dumps(key)
                result[key] = _script_data(item, parents)
            return result
        return [_script_data(item, parents) for item in value]
    finally:
        parents.remove(identity)


def _arguments(kind, method, headers, body, credentials):
    init = {"method": str(method or "GET").upper()}
    if kind == "xhr":
        init["headers"] = [(str(name), str(value)) for name, value in (headers or {}).items()]
    elif headers:
        init["headers"] = _script_data(dict(headers))
    if body is not None:
        init["body"] = str(body)
    if credentials:
        init["credentials"] = str(credentials)
    return init


def _answer(value):
    return value.to_python()


class JsHttpController:
    """Runs one page's ``fetch``/``XMLHttpRequest`` on behalf of the host."""

    def __init__(self, page):
        self._page = page
        self._lock = RLock()
        self._pending = set()
        self._closed = False
        self._cancelling = False

    def _release(self, future):
        with self._lock:
            self._pending.discard(future)

    def _claim(self, future):
        with self._lock:
            if future not in self._pending:
                return False
            self._pending.remove(future)
        return future.set_running_or_notify_cancel()

    def cancel_pending(self):
        with self._lock:
            if self._cancelling:
                return
            self._cancelling = True
            pending = tuple(self._pending)
            self._pending.clear()
        try:
            for future in pending:
                if future.set_running_or_notify_cancel():
                    future.set_exception(HostStateError('HTTP request document was replaced or closed'))
        finally:
            with self._lock:
                self._cancelling = False

    def close(self):
        with self._lock:
            self._closed = True
        self.cancel_pending()


    def start_fetch(self, url, *, method="GET", headers=None, body=None,
                    credentials=None, timeout=30) -> Future:
        """Start the page's ``fetch``; the ``Future`` settles on its answer."""
        return self._future("fetch", url, method, headers, body, credentials, timeout)


    def start_xhr(self, url, *, method="GET", headers=None, body=None,
                  credentials=None, timeout=30) -> Future:
        """Start the page's ``XMLHttpRequest``; the ``Future`` settles on its answer."""
        return self._future("xhr", url, method, headers, body, credentials, timeout)

    def _invoke(self, kind, url, init):
        realm = self._page.realm
        if realm.http_driver is None:
            realm.http_driver = realm.v8_context.exec_js(_DRIVER)
        return realm.http_driver(kind, url, init)

    def _start(self, kind, url, init, deliver, reject):
        promise = self._page.scripts.run(self._invoke, kind, url, init)
        promise.then(deliver, reject)

    def _future(self, kind, url, method, headers, body, credentials, timeout):
        """A ``Future`` the realm queue settles when the driver answers."""
        page = self._page
        future = Future()
        with self._lock:
            closed = self._closed or self._cancelling
            if not closed:
                self._pending.add(future)
        if closed:
            future.set_exception(HostStateError('page.net is closed'))
            return future
        owner = ref(self)

        def release(done):
            controller = owner()
            if controller is not None:
                controller._release(done)

        future.add_done_callback(release)

        def deliver(payload):
            if not self._claim(future):
                return
            try:
                future.set_result(self._response(url, _answer(payload)))
            except BaseException as exc:
                future.set_exception(exc)

        def reject(error):
            if self._claim(future):
                try:
                    failure = LoadFailure(str(error))
                except BaseException as exc:
                    failure = exc
                future.set_exception(failure)

        def start():
            with self._lock:
                if future not in self._pending:
                    return
            init = _arguments(kind, method, headers, body, credentials)
            self._start(kind, _script_data(url), init, deliver, reject)

        try:
            page.realm.executor.call(start, timeout=timeout)
        except BaseException as exc:
            if self._claim(future):
                future.set_exception(exc)
        return future

    @staticmethod
    def _response(url, payload):
        """The snapshot object one answer becomes, or the error it carries."""
        if not payload.get("ok"):
            raise LoadFailure(f"{payload.get('name', 'Error')}: {payload.get('message', '')}")
        headers = {str(name): str(value) for name, value in payload.get("headers") or ()}
        return JsHttpResponse(
            url=str(payload.get("url") or url),
            status=int(payload.get("status") or 0),
            status_text=str(payload.get("statusText") or ""),
            headers=headers,
            body=str(payload.get("text") or ""),
            redirected=bool(payload.get("redirected")),
        )
