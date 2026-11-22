"""What pyv8c puts on the wire, per scene, held to Chrome 153's baseline.

The servers record headers in arrival order, so the expected lists pin the wire order
and not only the set.  The two exceptions are libcurl's: it puts ``Host`` first and
moves a caller-supplied ``Connection`` to the end (``docs/pyv8c/request-headers.md``).
"""

import asyncio

import base64
import hashlib
import http.server
import socket
import threading
from urllib.parse import urlparse

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytest.importorskip("curl_cffi", reason="the curl_cffi engine is not installed")
pytestmark = pytest.mark.native

from pyv8c.async_api import Browser

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
from _shared_web_pages import PAGE, PNG

NAVIGATION = ["host", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
              "upgrade-insecure-requests", "user-agent", "accept", "sec-fetch-site",
              "sec-fetch-mode", "sec-fetch-user", "sec-fetch-dest", "accept-encoding",
              "accept-language", "connection"]
FRAME = ["host", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
         "upgrade-insecure-requests", "user-agent", "accept", "sec-fetch-site",
         "sec-fetch-mode", "sec-fetch-dest", "referer", "accept-encoding",
         "accept-language", "connection"]
SUBRESOURCE = ["host", "sec-ch-ua-platform", "user-agent", "sec-ch-ua",
               "sec-ch-ua-mobile", "accept", "sec-fetch-site", "sec-fetch-mode",
               "sec-fetch-dest", "referer", "accept-encoding", "accept-language",
               "connection"]
MEDIA = ["host", "sec-ch-ua-platform", "accept-encoding", "user-agent", "sec-ch-ua",
         "sec-ch-ua-mobile", "accept", "sec-fetch-site", "sec-fetch-mode",
         "sec-fetch-dest", "referer", "accept-language", "range", "connection"]
POSTED = ["host", "sec-ch-ua-platform", "user-agent", "sec-ch-ua", "content-type",
          "sec-ch-ua-mobile", "accept", "origin", "sec-fetch-site", "sec-fetch-mode",
          "sec-fetch-dest", "referer", "accept-encoding", "accept-language",
          "content-length", "connection"]
CROSS_ORIGIN = ["host", "sec-ch-ua-platform", "user-agent", "sec-ch-ua",
                "sec-ch-ua-mobile", "accept", "origin", "sec-fetch-site",
                "sec-fetch-mode", "sec-fetch-dest", "referer", "accept-encoding",
                "accept-language", "connection"]

RECORDS: list[dict] = []
LOCK = threading.Lock()
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
HINTS = '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"'


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        with LOCK:
            RECORDS.append({
                "port": self.server.server_port,
                "method": self.command,
                "path": urlparse(self.path).path,
                "headers": list(self.headers.items()),
                "body": body.decode("utf-8", "replace"),
            })
        payload, content_type = {
            "/page": (PAGE.encode(), "text/html; charset=utf-8"),
            "/frame.html": (b"<html><body>frame</body></html>", "text/html; charset=utf-8"),
            "/sub.css": (b"body{color:red}", "text/css"),
            "/sub.js": (b"console.log(1)", "application/javascript"),
            "/img.png": (PNG, "image/png"),
        }.get(self.path, (b"{}", "application/json"))
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class _SocketServer(threading.Thread):
    """Answers one WebSocket handshake, recording the request that asked for it."""

    def __init__(self):
        super().__init__(daemon=True)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.port = self.socket.getsockname()[1]
        self.received = threading.Event()

    def run(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            try:
                connection.settimeout(5)
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                lines = raw.decode("latin-1").split("\r\n")
                headers = []
                for line in lines[1:]:
                    if line and ":" in line:
                        name, _, value = line.partition(":")
                        headers.append((name.strip(), value.strip()))
                with LOCK:
                    RECORDS.append({
                        "port": self.port, "method": "GET", "path": "/socket",
                        "headers": headers, "body": "",
                    })
                key = dict((name.lower(), value) for name, value in headers).get("sec-websocket-key", "")
                accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
                connection.sendall((
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode())
                self.received.set()
                connection.close()
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass


@pytest.fixture(scope="module")
async def wire():
    """Two HTTP origins (same host, different port) and one socket server."""
    servers = []
    for _ in range(2):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    sockets = _SocketServer()
    sockets.start()
    try:
        same_origin = f"http://127.0.0.1:{servers[0].server_port}"
        cross_origin = f"http://127.0.0.1:{servers[1].server_port}"
        ws = f"ws://127.0.0.1:{sockets.port}/socket"
        browser = (await Browser(real_network=True, stub_logging="off").__aenter__())
        try:
            page = browser.new_profile().new_page()
            (await page.goto(f"{same_origin}/page", wait_until="load"))
            (await page.net.fetch("/js/fetch-get"))
            (await page.net.fetch("/js/fetch-post", method="POST", body='{"a": 1}',
                       headers={"content-type": "application/json"}))
            (await page.net.fetch(f"{cross_origin}/js/cross-simple"))
            (await page.net.xhr("/js/xhr-get"))
            page.evaluate(f"new WebSocket('{ws}')")
            assert (await asyncio.to_thread(sockets.received.wait, 5)), "the WebSocket handshake did not arrive"
        finally:
            (await browser.close())
        with LOCK:
            records = list(RECORDS)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        try:
            sockets.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sockets.socket.close()
        (await asyncio.to_thread(sockets.join, 3))
        assert not sockets.is_alive()
    yield {"records": records, "http": same_origin, "cross": cross_origin}


def sent(wire, path, method="GET"):
    """The headers one recorded request carried, lower-cased, in wire order."""
    for record in wire["records"]:
        if record["method"] == method and record["path"] == path:
            return {name.lower(): value for name, value in record["headers"]}
    raise AssertionError(f"no {method} {path} was sent; got {sorted({r['path'] for r in wire['records']})}")


def names(headers):
    return list(headers)


async def test_navigation_matches_chrome(wire):
    headers = sent(wire, "/page")
    assert names(headers) == NAVIGATION
    assert headers["user-agent"] == CHROME_UA
    assert headers["sec-fetch-site"] == "none"
    assert headers["sec-fetch-user"] == "?1"
    assert headers["sec-fetch-dest"] == "document"
    assert headers["upgrade-insecure-requests"] == "1"
    assert headers["accept"] == (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    )
    assert headers["accept-language"] == "zh-CN,zh;q=0.9"
    assert headers["accept-encoding"] == "gzip, deflate, br, zstd"
    assert headers["sec-ch-ua"] == HINTS
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert headers["sec-ch-ua-platform"] == '"Windows"'


async def test_subresources_match_chrome(wire):
    for path, destination, accept in (
        ("/sub.css", "style", "text/css,*/*;q=0.1"),
        ("/sub.js", "script", "*/*"),
        ("/img.png", "image", "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
    ):
        headers = sent(wire, path)
        assert names(headers) == SUBRESOURCE, path
        assert headers["accept"] == accept, path
        assert headers["sec-fetch-dest"] == destination, path
        assert headers["sec-fetch-mode"] == "no-cors", path
        assert headers["sec-fetch-site"] == "same-origin", path
        assert headers["referer"] == f"{wire['http']}/page", path
        assert "origin" not in headers, path


async def test_a_frame_is_a_navigation_without_user_activation(wire):
    headers = sent(wire, "/frame.html")
    assert names(headers) == FRAME
    assert headers["sec-fetch-dest"] == "iframe"
    assert headers["sec-fetch-mode"] == "navigate"
    assert headers["sec-fetch-site"] == "same-origin"
    assert "sec-fetch-user" not in headers


async def test_media_asks_by_range_uncompressed(wire):
    headers = sent(wire, "/clip.mp4")
    assert names(headers) == MEDIA
    assert headers["sec-fetch-dest"] == "video"
    assert headers["accept-encoding"] == "identity;q=1, *;q=0"
    assert headers["range"] == "bytes=0-"


async def test_fetch_and_xhr_match_chrome(wire):
    for path in ("/js/fetch-get", "/js/xhr-get"):
        headers = sent(wire, path)
        assert names(headers) == SUBRESOURCE, path
        assert headers["accept"] == "*/*", path
        assert headers["sec-fetch-mode"] == "cors", path
        assert headers["sec-fetch-dest"] == "empty", path
        assert "origin" not in headers, path


async def test_a_posted_fetch_carries_origin_and_the_body_length(wire):
    headers = sent(wire, "/js/fetch-post", "POST")
    assert names(headers) == POSTED
    assert headers["content-type"] == "application/json"
    assert headers["origin"] == wire["http"]
    assert headers["content-length"] == "8"


async def test_a_cross_origin_fetch_only_tells_the_origin(wire):
    headers = sent(wire, "/js/cross-simple")
    assert names(headers) == CROSS_ORIGIN
    assert headers["origin"] == wire["http"]
    assert headers["sec-fetch-site"] == "same-site"
    assert headers["referer"] == f"{wire['http']}/"


async def test_a_compressed_response_is_still_readable():
    """The header advertises compression, so the transport has to keep decoding it."""
    import gzip
    import json

    compressed = gzip.compress(json.dumps({"ok": True}).encode())

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        browser = (await Browser(real_network=True, stub_logging="off").__aenter__())
        try:
            page = browser.new_profile().new_page()
            await page.goto(f"http://127.0.0.1:{server.server_port}/")
            assert (await page.net.fetch("/data")).json() == {"ok": True}
        finally:
            (await browser.close())
    finally:
        server.shutdown()
        (await asyncio.to_thread(thread.join))
        server.server_close()


async def test_the_websocket_handshake_carries_the_environment(wire):
    headers = sent(wire, "/socket")
    assert headers["origin"] == wire["http"]
    assert headers["user-agent"] == CHROME_UA
    assert headers["accept-language"] == "zh-CN,zh;q=0.9"
    assert headers["accept-encoding"] == "gzip, deflate, br, zstd"
    assert headers["pragma"] == "no-cache"
    assert headers["cache-control"] == "no-cache"
    # Chrome sends no client hint and no Sec-Fetch-* on a socket handshake.
    assert "sec-ch-ua" not in headers
    assert "sec-fetch-dest" not in headers
    assert headers["sec-websocket-version"] == "13"
