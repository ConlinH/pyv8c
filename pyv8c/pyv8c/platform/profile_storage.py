"""Profile-owned web storage and its optional SQLite backing."""

import asyncio
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from threading import RLock

from ..foundation.host_errors import HostStateError
from ..foundation.logging import logger
from ..foundation.storage_area import StorageArea
from ..network.cookies import Cookie, CookieJar


_SCHEMA_VERSION = 1
_AUTOSAVE_DELAY = 0.25
_DISK_ERRORS = (OSError, sqlite3.Error, ValueError, TypeError, OverflowError)
_WEB_SCHEMES = {"http", "https"}


def _read_database(path):
    # Open an existing database only. A writable connection can recover a
    # rollback journal left by a process that died during its last write.
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=0)) as db:
        version, = db.execute("PRAGMA user_version").fetchone()
        if version != _SCHEMA_VERSION:
            raise HostStateError(f"Unsupported profile database schema version: {version}")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Corrupt profile database")
        (snapshot,), = db.execute("SELECT payload FROM profile_state WHERE id = 1")
        return json.loads(snapshot)


def _initialize_database(path):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".profile-", suffix=".sqlite3")
    os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute("BEGIN")
            db.execute("CREATE TABLE profile_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("INSERT INTO profile_state VALUES (1, ?)", ('{"cookies":[],"origins":[]}',))
            db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_cookie(record):
    cookie = Cookie(**record)
    if any(type(value) is not str for value in
           (cookie.name, cookie.value, cookie.domain, cookie.path, cookie.same_site)):
        raise ValueError("Invalid stored cookie fields")
    if (not cookie.name or not cookie.domain or not cookie.path.startswith("/")
            or any(char in cookie.name or char in cookie.value for char in "\r\n\0")
            or any(type(flag) is not bool for flag in
                   (cookie.secure, cookie.http_only, cookie.host_only))
            or type(cookie.creation_index) is not int or cookie.creation_index < 1
            or (cookie.expires is not None and
                (type(cookie.expires) not in (int, float) or not math.isfinite(cookie.expires)))):
        raise ValueError("Invalid stored cookie")
    return cookie


class ProfileStorage:
    def __init__(self, loop, directory, persist_session_cookies):
        self._loop = loop
        self._lock = RLock()
        self._areas = {}
        self._persist_session_cookies = persist_session_cookies
        self._file_lock = None
        self._timer = self._task = None
        self._revision = self._saved = 0
        self._notified = self._closing = False
        changed = self._changed if directory is not None else None
        self.cookies = CookieJar(lock=self._lock, on_change=changed)
        if directory is not None:
            self._open(Path(directory))

    def _open(self, directory):
        from filelock import FileLock, Timeout

        try:
            directory = directory.resolve()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = FileLock(directory / "profile.lock", timeout=0)
            lock.acquire()
        except Timeout:
            raise HostStateError("Profile directory is already in use") from None
        except (OSError, RuntimeError, ValueError):
            raise HostStateError("Cannot lock profile directory") from None

        self._file_lock = lock
        self._path = directory / "state.sqlite3"
        opened = False
        try:
            if self._path.is_symlink():
                raise ValueError("Profile database is a symbolic link")
            if self._path.exists():
                self._restore(_read_database(self._path))
            else:
                _initialize_database(self._path)
            opened = True
        except _DISK_ERRORS:
            raise HostStateError("Cannot read profile database") from None
        finally:
            if not opened:
                self.release()

    def _restore(self, snapshot):
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("cookies"), list)
                or not isinstance(snapshot.get("origins"), list)):
            raise ValueError("Invalid profile snapshot")
        now = time.time()
        cookies = []
        for record in snapshot["cookies"]:
            cookie = _load_cookie(record)
            if ((cookie.expires is not None and cookie.expires > now)
                    or (cookie.expires is None and self._persist_session_cookies)):
                cookies.append(cookie)
        self.cookies._restore(cookies)
        for origin, values in snapshot["origins"]:
            scheme, host, port = origin
            if (scheme not in _WEB_SCHEMES or not isinstance(host, str) or not host
                    or type(port) is not int or not 0 <= port <= 65535
                    or not isinstance(values, dict)
                    or any(not isinstance(value, str) for value in values.values())):
                raise ValueError("Invalid profile origin storage")
            self._areas[(scheme, host, port)] = StorageArea(
                values, lock=self._lock, on_change=self._changed)
        if len(cookies) != len(snapshot["cookies"]):
            self._revision = 1

    def local_storage_area(self, origin):
        with self._lock:
            if origin not in self._areas:
                changed = self._changed if self._file_lock and origin[0] in _WEB_SCHEMES else None
                self._areas[origin] = StorageArea(lock=self._lock, on_change=changed)
            return self._areas[origin]

    def _changed(self):
        # Called under the shared lock by both the realm and transport threads.
        if self._file_lock is not None:
            self._revision += 1
            if not self._closing and not self._notified:
                self._notified = True
                self._loop.call_soon_threadsafe(self.schedule)

    def schedule(self):
        with self._lock:
            self._notified = False
            if (self._file_lock is not None and not self._closing
                    and self._revision > self._saved and self._timer is None and self._task is None):
                self._timer = self._loop.call_later(_AUTOSAVE_DELAY, self._start_write)

    def _write_snapshot(self):
        with self._lock:
            revision = self._revision
            cookies = self.cookies.snapshot(include_session=self._persist_session_cookies)
            origins = [(origin, area.snapshot()) for origin, area in self._areas.items()
                       if origin[0] in _WEB_SCHEMES and len(area)]
        payload = json.dumps({"cookies": [asdict(cookie) for cookie in cookies], "origins": origins})
        with closing(sqlite3.connect(f"{self._path.as_uri()}?mode=rw", uri=True, timeout=0)) as db, db:
            if db.execute("UPDATE profile_state SET payload = ? WHERE id = 1", (payload,)).rowcount != 1:
                raise ValueError("Missing profile state")
        return revision

    async def _write(self):
        try:
            revision = await asyncio.to_thread(self._write_snapshot)
        except _DISK_ERRORS as error:
            logger.warning("Profile state write failed ({})", type(error).__name__)
            raise HostStateError("Cannot save profile state") from None
        with self._lock:
            self._saved = revision

    def _start_write(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._task = self._loop.create_task(self._write())
        self._task.add_done_callback(self._write_finished)
        return self._task

    def _write_finished(self, task):
        if self._task is task:
            self._task = None
        if not task.cancelled() and task.exception() is None:
            self.schedule()

    async def flush(self):
        if self._file_lock is None:
            return
        with self._lock:
            revision = self._revision
        while self._saved < revision:
            task = self._task if self._task is not None else self._start_write()
            await asyncio.shield(task)

    async def close(self):
        with self._lock:
            self._closing = True
        while True:
            await self.flush()
            with self._lock:
                if self._saved == self._revision:
                    self.release()
                    return

    def release(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        with self._lock:
            if self._file_lock is not None:
                self._file_lock.release()
                self._file_lock = None
