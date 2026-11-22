"""A proxy to point a context at, and a record of what it was asked to carry.

Tests need evidence that traffic really left through the proxy, so this is a
working proxy rather than a mock.  It answers the two shapes a client sends it:

* an absolute request line -- ``GET http://host:port/path HTTP/1.1`` -- which is
  how a plain ``http://`` request looks on the wire, forwarded to the origin;
* ``CONNECT host:port``, which is how ``https://`` and every WebSocket handshake
  reach a proxy, tunnelled byte for byte after a ``200 Connection Established``.

Every request line is written down, so a test can assert what the proxy carried
(and what it did not).  Nothing here depends on pyv8c: it is a server.
"""
from __future__ import annotations

import http.client
import socket
import threading

#: How long a tunnel stays open with no traffic before it is given up on.
TUNNEL_TIMEOUT = 30.0


class RecordingProxy:
    """One proxy server: ``.url`` is what a context is pointed at.

    ``.requests`` holds the request line of every request it carried, in order,
    and ``.tunnels`` the authority of every ``CONNECT`` it opened.
    """

    def __init__(self, *, refuse_connect: bool = False):
        self.requests: list[str] = []
        self.tunnels: list[str] = []
        self.refused: list[str] = []
        self.authorizations: list[str] = []
        self._refuse_connect = refuse_connect
        self._lock = threading.Lock()
        self._running = True
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """The proxy URL to configure a browser with."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def authenticated_url(self) -> str:
        """The same proxy with credentials in the URL."""
        return f"http://user:secret@127.0.0.1:{self.port}"

    def carried(self) -> list[str]:
        """A copy of the request lines, safe to read while the proxy runs."""
        with self._lock:
            return list(self.requests)

    def close(self) -> None:
        self._running = False
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- the server --------------------------------------------------------

    def _accept(self):
        while self._running:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve, args=(connection,), daemon=True,
            ).start()

    def _serve(self, connection):
        tunnelled = False
        try:
            request = _read_head(connection)
            if not request:
                return
            line = request.split(b"\r\n", 1)[0].decode("latin-1")
            with self._lock:
                self.requests.append(line)
                authorization = _header_value(request, "proxy-authorization")
                if authorization is not None:
                    self.authorizations.append(authorization)
            method, target = line.split(" ", 2)[:2]
            if method.upper() == "CONNECT":
                tunnelled = self._connect(connection, request, target)
            else:
                self._forward(connection, request, target)
        except OSError:
            pass
        finally:
            # A tunnel's sockets are closed by the relay that owns them; this
            # connection is only ours to close when nothing was tunnelled.
            if not tunnelled:
                try:
                    connection.close()
                except OSError:
                    pass

    def _connect(self, connection, request, authority):
        """Open a tunnel to *authority*, or refuse it so a test can watch."""
        if self._refuse_connect:
            with self._lock:
                self.refused.append(authority)
            connection.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            return False
        host, _, port = authority.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)), timeout=TUNNEL_TIMEOUT)
        except OSError:
            with self._lock:
                self.refused.append(authority)
            connection.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            return False
        with self._lock:
            self.tunnels.append(authority)
        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        (_pump(connection, upstream))
        return True

    def _forward(self, connection, request, target):
        """Forward an absolute-URI request to the origin and relay the answer."""
        head, _, body = request.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, uri, version = lines[0].decode("latin-1").split(" ", 2)
        path = "/" + uri.split("//", 1)[-1].partition("/")[2] if "//" in uri else uri
        host = uri.split("//", 1)[-1].partition("/")[0]
        headers = [line for line in lines[1:] if not line.lower().startswith(b"proxy-")]
        upstream = http.client.HTTPConnection(host, timeout=TUNNEL_TIMEOUT)
        try:
            upstream.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for header in headers:
                name, _, value = header.partition(b":")
                upstream.putheader(name.decode("latin-1"), value.strip().decode("latin-1"))
            upstream.endheaders(body or None)
            response = upstream.getresponse()
            payload = response.read()
            connection.sendall(
                f"HTTP/1.1 {response.status} {response.reason}\r\n".encode("latin-1")
            )
            for name, value in response.getheaders():
                if name.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                connection.sendall(f"{name}: {value}\r\n".encode("latin-1"))
            connection.sendall(
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("latin-1")
            )
            connection.sendall(payload)
        finally:
            upstream.close()


def _header_value(head, name):
    """The value of the first header called *name*, or ``None`` when absent."""
    for line in head.split(b"\r\n")[1:]:
        header, separator, value = line.partition(b":")
        if separator and header.strip().lower() == name.encode("latin-1"):
            return value.strip().decode("latin-1")
    return None


def _read_head(connection, limit=1 << 20):
    """One request's head, including any body the headers announced."""
    data = b""
    while b"\r\n\r\n" not in data and len(data) < limit:
        chunk = connection.recv(4096)
        if not chunk:
            return data
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip() or 0)
    while len(rest) < length:
        chunk = connection.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head + b"\r\n\r\n" + rest


def _pump(client, upstream):
    """Shuttle bytes both ways until either side is done.

    One direction runs here and the other in a thread, so the caller's frame
    stays alive for as long as the tunnel is in use -- the reason this blocks
    rather than returning once both relays are started.
    """
    threading.Thread(target=_relay, args=(upstream, client), daemon=True).start()
    _relay(client, upstream)


def _relay(source, sink):
    try:
        while True:
            chunk = source.recv(8192)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        for endpoint in (source, sink):
            try:
                endpoint.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
