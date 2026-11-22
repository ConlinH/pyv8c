"""Local wire evidence for the two built-in HTTP engines.

Everything here is a real server or a real socket, and every recorder owns its
own state: no module-level mutable globals, so ``--dist loadscope`` may put the
module on any worker and two engines may run side by side.

The recording server answers plain ``http://127.0.0.1:<port>`` only.  TLS
evidence comes from a raw TCP listener that reads one ClientHello and never
answers, so no certificate, no external network and no ``verify=False`` flag is
needed.  Chrome's absolute header baselines live in
``test_request_headers_on_the_wire.py`` and are deliberately not imported here.
"""
from __future__ import annotations

import gzip
import http.server
import json
import socket
import struct
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

from proxy_harness import RecordingProxy

__all__ = [
    "CAPABILITIES_REQUIRED",
    "ClientHello",
    "HelloRecorder",
    "RecordedRequest",
    "RecordingOrigin",
    "RecordingOrigins",
    "RecordingProxy",
    "TRANSPORT_PLACED",
    "framework_headers",
    "parse_client_hello",
]

#: Transport-owned headers dropped from normalised comparisons.
TRANSPORT_PLACED = frozenset({"host", "connection", "content-length"})

#: Shared ClientHello capabilities, not absolute fingerprint values.
CAPABILITIES_REQUIRED = frozenset({
    "tls-1.3", "tls-1.2",
    "alpn-h2", "alpn-http/1.1",
    "cipher-tls-aes-128-gcm-sha256",
    "cipher-tls-aes-256-gcm-sha384",
    "cipher-tls-chacha20-poly1305-sha256",
    "extension-supported-versions",
    "extension-alpn",
    "extension-key-share",
    "extension-signature-algorithms",
})

_EXTENSION_LABELS = {
    0x2b: "extension-supported-versions",
    0x10: "extension-alpn",
    0x33: "extension-key-share",
    0x0d: "extension-signature-algorithms",
    0x0a: "extension-supported-groups",
    0x2d: "extension-psk-key-exchange-modes",
    0x00: "extension-server-name",
    0x17: "extension-extended-master-secret",
    0x23: "extension-session-ticket",
}
_TLS13_CIPHERS = {
    0x1301: "cipher-tls-aes-128-gcm-sha256",
    0x1302: "cipher-tls-aes-256-gcm-sha384",
    0x1303: "cipher-tls-chacha20-poly1305-sha256",
}


def _grease(value: int) -> bool:
    """Reserved TLS code points a stack may sprinkle into any list or order."""
    return value > 0xFF and (value >> 8) == (value & 0xFF) and (value & 0x0F) == 0x0A


@dataclass(frozen=True)
class RecordedRequest:
    """One request as it arrived on the wire, headers still in arrival order."""

    port: int
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def sent(self) -> dict[str, str]:
        return {str(name).lower(): value for name, value in self.headers}

    def names(self) -> list[str]:
        return [str(name).lower() for name, _ in self.headers]


def framework_headers(request: RecordedRequest) -> dict[str, str]:
    """The headers the framework owns, with transport-placed and engine-injected
    ones removed for a normalised comparison between two different stacks.

    ``cookie`` is excluded because Cronet always injects an explicit (possibly
    empty) ``Cookie`` to suppress its own session jar; the jar's behaviour has
    its own wire assertions instead of a header-set equality claim.
    """
    engine_owned = {"cookie"}
    return {name: value for name, value in request.sent().items()
            if name not in TRANSPORT_PLACED and name not in engine_owned}


class _Handler(http.server.BaseHTTPRequestHandler):
    """Records every hop, then answers the scene the test asked for."""

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path
        owner: RecordingOrigin = self.server.owner
        owner.record(RecordedRequest(
            port=self.server.server_port, method=self.command, path=path,
            headers=tuple(self.headers.items()), body=body,
        ))
        self._answer(path, body)

    def _reply(self, body: bytes, *, status=200, content_type="application/json",
               cookies=(), location=None, extra_headers=()):
        self.send_response(status)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if location is not None:
            self.send_header("Location", location)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _echo(self, body):
        echo = {
            "method": self.command,
            "cookie": self.headers.get("Cookie"),
            "ua": self.headers.get("User-Agent"),
            "language": self.headers.get("Accept-Language"),
            "parity": self.headers.get("X-Parity"),
            "referer": self.headers.get("Referer"),
            "accept": self.headers.get("Accept"),
            "encoding": self.headers.get("Accept-Encoding"),
            "destination": self.headers.get("Sec-Fetch-Dest"),
            "mode": self.headers.get("Sec-Fetch-Mode"),
            "site": self.headers.get("Sec-Fetch-Site"),
            "hints": self.headers.get("Sec-CH-UA"),
            "content_type": self.headers.get("Content-Type"),
            "body": body.decode("utf-8", "replace"),
        }
        self._reply(json.dumps(echo).encode())

    def _answer(self, path, body):
        if path == "/page":
            self._reply(b"<!doctype html><title>wire</title><body>ok</body>",
                        content_type="text/html; charset=utf-8")
            return
        if path in ("/echo", "/hop2"):
            self._echo(body)
            return
        if path == "/worker-import.js":
            self._reply(b"globalThis.imported = 42;", content_type="text/javascript")
            return
        if path == "/set":
            self._reply(b'{"set":true}', cookies=["a=1; Path=/", "b=2; Path=/"])
            return
        if path == "/many":
            self._reply(b'{"many":true}', cookies=["m1=1; Path=/", "m2=2; Path=/"])
            return
        if path == "/gzip":
            self._reply(gzip.compress(b"compressed body"),
                        content_type="text/plain; charset=utf-8",
                        extra_headers=[("Content-Encoding", "gzip")])
            return
        if path == "/nocontent":
            self._reply(b"", status=204)
            return
        if path == "/redirect":
            self._reply(b"redirect-body-must-not-leak", status=302, location="/echo")
            return
        if path == "/scoped/start":
            self._reply(b"", status=302, location="/echo",
                        cookies=["fresh=1; Path=/", "fresh_scoped=1; Path=/scoped"])
            return
        if path == "/hop1":
            owner: RecordingOrigin = self.server.owner
            assert owner.cross is not None, "the cross-origin fixture was not wired"
            self._reply(b"first-hop-body-must-not-leak", status=302,
                        location=f"{owner.cross}/hop2")
            return
        if path == "/final":
            self._reply(b'{"final":true}')
            return
        self._reply(b'{"ok":true}')

    def log_message(self, *_args):
        pass


class RecordingOrigin:
    """One HTTP origin whose records belong to this instance alone.

    ``host`` may name another address in the loopback /8 block, so two origins
    can genuinely differ in cookie domain as well as in port.
    """

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.cross: str | None = None
        self._records: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._server = http.server.ThreadingHTTPServer((host, 0), _Handler)
        self._server.daemon_threads = True
        self._server.owner = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self._server.server_port}"

    @property
    def port(self) -> int:
        return self._server.server_port

    def record(self, request: RecordedRequest) -> None:
        with self._lock:
            self._records.append(request)

    def records(self) -> tuple[RecordedRequest, ...]:
        with self._lock:
            return tuple(self._records)

    def sent(self, path: str, method: str = "GET") -> RecordedRequest:
        for item in reversed(self.records()):
            if item.path == path and item.method == method:
                return item
        seen = sorted({f"{r.method} {r.path}" for r in self.records()})
        raise AssertionError(f"no {method} {path} was sent; got {seen}")

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class RecordingOrigins:
    """Two origins that differ in host *and* port, so cookies are cross-domain.

    ``127.0.0.2`` is another address in the loopback /8 block: no DNS, no
    external network, and no shared cookie domain with ``127.0.0.1``.  Some
    sandboxes only expose ``127.0.0.1``; there the second origin falls back to a
    different port and ``cross_domain`` says the cookie-domain assertion cannot
    be made.
    """

    def __init__(self):
        self.same = RecordingOrigin("127.0.0.1")
        try:
            self.cross = RecordingOrigin("127.0.0.2")
        except OSError:
            self.cross = RecordingOrigin("127.0.0.1")
        self.cross_domain = self.same.host != self.cross.host
        self.same.cross = self.cross.url
        self.cross.cross = self.same.url

    def close(self) -> None:
        self.same.close()
        self.cross.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


@dataclass(frozen=True)
class ClientHello:
    """The structural parts of one TLS ClientHello; no absolute fingerprint."""

    record_version: str
    client_version: str
    cipher_suites: tuple[int, ...]
    extension_types: tuple[int, ...]
    supported_versions: tuple[int, ...]
    alpn: tuple[str, ...]
    sni: tuple[str, ...]

    def capabilities(self) -> frozenset[str]:
        """A normalised, order-free and GREASE-free summary of what was offered."""
        caps: set[str] = set()
        versions = {value for value in self.supported_versions if not _grease(value)}
        if 0x0304 in versions:
            caps.add("tls-1.3")
        if 0x0303 in versions:
            caps.add("tls-1.2")
        protocols = set(self.alpn)
        if "h2" in protocols:
            caps.add("alpn-h2")
        if "http/1.1" in protocols:
            caps.add("alpn-http/1.1")
        suites = {value for value in self.cipher_suites if not _grease(value)}
        for code, label in _TLS13_CIPHERS.items():
            if code in suites:
                caps.add(label)
        present = {value for value in self.extension_types if not _grease(value)}
        for code, label in _EXTENSION_LABELS.items():
            if code in present:
                caps.add(label)
        return frozenset(caps)

    def digest(self) -> str:
        """The regression baseline: capabilities only, never raw bytes."""
        return "|".join(sorted(self.capabilities()))


def parse_client_hello(data: bytes) -> ClientHello:
    """Read a TLS record header, handshake header and the ClientHello extensions."""
    if len(data) < 5 or data[0] != 0x16:
        raise AssertionError(f"not a TLS handshake record: {data[:8].hex()}")
    record_length = struct.unpack("!H", data[3:5])[0]
    record = data[5:5 + record_length]
    if len(record) < 4 or record[0] != 0x01:
        raise AssertionError(f"not a ClientHello: {record[:8].hex()}")
    handshake_length = int.from_bytes(record[1:4], "big")
    hello = record[4:4 + handshake_length]
    if len(hello) < 2 + 32:
        raise AssertionError("ClientHello was truncated before its version")
    offset = 2 + 32
    session_length = hello[offset]
    offset += 1 + session_length
    suites_length = struct.unpack("!H", hello[offset:offset + 2])[0]
    offset += 2
    suites = tuple(struct.unpack("!" + "H" * (suites_length // 2),
                                hello[offset:offset + suites_length]))
    offset += suites_length
    compression_length = hello[offset]
    offset += 1 + compression_length
    extensions: dict[int, bytes] = {}
    if offset + 2 <= len(hello):
        extensions_end = min(offset + 2 + struct.unpack("!H", hello[offset:offset + 2])[0],
                             len(hello))
        offset += 2
        while offset + 4 <= extensions_end:
            kind = struct.unpack("!H", hello[offset:offset + 2])[0]
            size = struct.unpack("!H", hello[offset + 2:offset + 4])[0]
            offset += 4
            extensions[kind] = hello[offset:offset + size]
            offset += size

    versions: tuple[int, ...] = ()
    raw_versions = extensions.get(0x2b)
    if raw_versions and len(raw_versions) >= 3:
        count = raw_versions[0]
        versions = tuple(struct.unpack("!" + "H" * (count // 2), raw_versions[1:1 + count]))

    alpn: list[str] = []
    raw_alpn = extensions.get(0x10)
    if raw_alpn and len(raw_alpn) >= 2:
        end = min(2 + struct.unpack("!H", raw_alpn[0:2])[0], len(raw_alpn))
        cursor = 2
        while cursor < end:
            size = raw_alpn[cursor]
            alpn.append(raw_alpn[cursor + 1:cursor + 1 + size].decode("latin-1"))
            cursor += 1 + size

    sni: list[str] = []
    raw_sni = extensions.get(0x00)
    if raw_sni and len(raw_sni) >= 2:
        end = min(2 + struct.unpack("!H", raw_sni[0:2])[0], len(raw_sni))
        cursor = 2
        while cursor + 3 <= end:
            size = struct.unpack("!H", raw_sni[cursor + 1:cursor + 3])[0]
            sni.append(raw_sni[cursor + 3:cursor + 3 + size].decode("latin-1", "replace"))
            cursor += 3 + size

    return ClientHello(
        record_version=data[1:3].hex(),
        client_version=hello[0:2].hex(),
        cipher_suites=suites,
        extension_types=tuple(extensions),
        supported_versions=versions,
        alpn=tuple(alpn),
        sni=tuple(sni),
    )


class HelloRecorder:
    """A TLS listener that records ClientHellos and deliberately never answers.

    The caller's request is expected to fail: the point of the exercise is the
    connection-establishment evidence, not a completed HTTPS exchange.  No
    certificate and no external host is involved.
    """

    def __init__(self):
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port = self._socket.getsockname()[1]
        self._records: list[bytes] = []
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._running = True
        self._error: str | None = None
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/"

    def _accept(self):
        while self._running:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                break
            try:
                connection.settimeout(3)
                data = self._read_record(connection)
                with self._lock:
                    self._records.append(data)
                self._arrived.set()
            except Exception as exc:  # a half-open socket is still evidence
                self._error = repr(exc)
            finally:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self._socket.close()

    @staticmethod
    def _read_record(connection) -> bytes:
        data = b""
        while len(data) < 5:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
        if len(data) >= 5:
            length = struct.unpack("!H", data[3:5])[0]
            while len(data) < 5 + length:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
        return data

    def wait(self, timeout=5.0) -> ClientHello:
        assert self._arrived.wait(timeout), (
            f"no ClientHello reached the TLS listener; recorder error: {self._error}")
        with self._lock:
            data = self._records[0]
        return parse_client_hello(data)

    def close(self) -> None:
        self._running = False
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
