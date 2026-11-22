"""Profile-owned resource cache with a bounded in-memory tier over disk."""
import base64
import hashlib
import json
import os
import re
import tempfile
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from ...foundation.logging import logger
from ...foundation.urls import DEFAULT_PORTS

_MEMORY_BYTES = 16 * 1024 * 1024
_MEMORY_ENTRIES = 256


class ResourceCache:
    def __init__(self, profile):
        self.profile = profile
        self._entries = OrderedDict()
        self._size = 0
        self._memory_lock = Lock()
        self._disk_lock = Lock()

    def key(self, target, request, *, enabled, origin):
        if not enabled or self.profile.resource_cache_dir is None or origin == "null":
            return None
        if urlsplit(target).scheme not in ("http", "https"):
            return None
        method = str(request.get("method", "GET")).upper()
        if (method not in ("GET", "HEAD") or request.get("redirect", "follow") != "follow"
                or str(request.get("cache", "default")) in ("no-store", "reload", "no-cache")):
            return None
        headers = {str(name).lower(): str(value) for name, value in request["headers"].items()}
        # This persistent cache is shared across profile restarts.  It must not
        # reuse authenticated bodies or mix request variants and initiating origins.
        jar = self.profile.cookies
        if "authorization" in headers or "cookie" in headers or jar.header_for(target):
            return None
        return (
            method,
            target,
            str(request.get("mode", "cors")),
            str(request.get("credentials", "same-origin")),
            origin,
            tuple(sorted(headers.items())),
        )

    def get_memory(self, key):
        with self._memory_lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry[0]

    def clear_memory(self):
        with self._memory_lock:
            self._entries.clear()
            self._size = 0

    def _remember(self, key, payload):
        body = payload["body"]
        size = len(body) * (4 if isinstance(body, str) else 1) if body is not None else 0
        size += 4 * (len(repr(key)) + len(repr(payload["headers"])) + len(payload["url"]))
        if size > _MEMORY_BYTES:
            return
        with self._memory_lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._size -= previous[1]
            self._entries[key] = payload, size
            self._size += size
            while self._size > _MEMORY_BYTES or len(self._entries) > _MEMORY_ENTRIES:
                _, (_, evicted) = self._entries.popitem(last=False)
                self._size -= evicted

    def get(self, key):
        with self._disk_lock:
            cached = self.get_memory(key)
            if cached is None:
                cached = self._read_disk_cache(self.profile, key)
                if cached is not None:
                    self._remember(key, cached)
        if cached is not None:
            logger.debug("resource cache hit: {}", key[1])
        return cached

    @staticmethod
    def _disk_cache_path(profile, key):
        cache_dir = profile.resource_cache_dir
        if cache_dir is None:
            return None
        digest = hashlib.sha256(json.dumps(key, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        target = str(key[1]) if len(key) > 1 else ""
        parsed = urlsplit(target)
        domain = parsed.hostname or "unknown"
        try:
            port = parsed.port
        except ValueError:
            port = None
        default_port = DEFAULT_PORTS.get(parsed.scheme.lower())
        if port is not None and port != default_port:
            domain = f"{domain}_{port}"
        domain = re.sub(r"[^A-Za-z0-9._-]+", "_", domain).strip("._") or "unknown"
        return Path(cache_dir) / domain / (digest + ".json")

    def _read_disk_cache(self, profile, key):
        path = self._disk_cache_path(profile, key)
        if path is None or not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            # Older cache records do not say whether a redirect crossed an
            # origin. Refetch them rather than exposing potentially CORS-filtered
            # response headers as though the whole chain were same-origin.
            if record.get("redirected") and "redirectCrossed" not in record:
                return None
            body = base64.b64decode(record["body"])
            if record.get("body_encoding") != "bytes":
                body = body.decode("utf-8")
            payload = {
                "body": body,
                "status": int(record.get("status", 200)),
                "statusText": str(record.get("statusText", "")),
                "url": str(record.get("url", "")),
                "headers": dict(record.get("headers", {})),
                "redirected": bool(record.get("redirected", False)),
                "redirectCrossed": bool(record.get("redirectCrossed", False)),
                "type": str(record.get("type", "basic")),
            }
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return payload

    def put(self, key, payload, claim_publication):
        # An opaque-redirect response stands for a redirect the page may not read, so
        # what it "contains" is no answer at all.
        if key is None or payload["type"] in ("opaque", "opaqueredirect"):
            return
        headers = {name.lower(): value for name, value in payload["headers"].items()}
        directives = {part.strip().split("=", 1)[0].lower()
                      for part in str(headers.get("cache-control", "")).split(",")}
        if (directives & {"no-store", "no-cache", "private"}
                or "set-cookie" in headers or str(headers.get("vary", "")).strip() == "*"
                or not 200 <= payload["status"] < 300):
            return
        profile = self.profile
        if profile.cookies.header_for(key[1]):
            return
        with self._disk_lock:
            path = self._disk_cache_path(profile, key)
            temporary = None
            ready = False
            try:
                try:
                    if path is not None:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        body = payload["body"]
                        body_encoding = "bytes" if isinstance(body, (bytes, bytearray, memoryview)) else "text"
                        body_bytes = bytes(body) if body_encoding == "bytes" else str(body).encode("utf-8")
                        record = {name: value for name, value in payload.items() if name != "body"}
                        record.update({
                            "body": base64.b64encode(body_bytes).decode("ascii"),
                            "body_encoding": body_encoding,
                        })
                        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                            temporary = stream.name
                            json.dump(record, stream, ensure_ascii=True, separators=(",", ":"))
                        ready = True
                except (OSError, TypeError, ValueError):
                    pass

                # The request lock is released before any disk operation. The
                # tracked cache job owns publication and cleanup once claimed.
                if not claim_publication():
                    return
                if ready:
                    try:
                        os.replace(temporary, path)
                    except OSError:
                        pass
                    else:
                        temporary = None
                self._remember(key, payload)
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass


