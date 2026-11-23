"""Run the persistent profile through the public APIs and a local HTTP server."""

import asyncio
import http.server
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pyv8c.pyv8")

import pyv8c
from pyv8c import async_api, sync_api
from pyv8c.foundation.host_errors import HostStateError


@pytest.fixture
def cookie_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            if self.path == "/login":
                self.send_header("Set-Cookie", "sid=private; Path=/; HttpOnly; Max-Age=3600")
                self.send_header("Set-Cookie", "session=temporary; Path=/")
            if self.path == "/probe":
                requests.append(self.headers.get("Cookie", ""))
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>profile</title>")

        def log_message(self, *_):
            pass

    requests = []
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("persist_session_cookies", [False, True])
async def test_first_request_restores_cookies(tmp_path, cookie_server, persist_session_cookies):
    url, requests = cookie_server
    async with async_api.Browser(proxy="", stub_logging="off") as browser:
        profile = browser.new_profile(user_data_dir=tmp_path,
                                      persist_session_cookies=persist_session_cookies)
        page = profile.new_page()
        await page.goto(f"{url}/login", wait_until="load")
        page.evaluate("document.cookie = 'visible=stored; Path=/; Max-Age=3600'")
        page.evaluate("localStorage.setItem('login', 'saved'); sessionStorage.setItem('tab', 'temp')")

    async with async_api.Browser(proxy="", stub_logging="off") as browser:
        profile = browser.new_profile(user_data_dir=str(tmp_path),
                                      persist_session_cookies=persist_session_cookies)
        page = profile.new_page()
        await page.goto(f"{url}/probe", wait_until="load")
        expected = ["sid=private", "visible=stored"]
        if persist_session_cookies:
            expected.insert(1, "session=temporary")
        assert requests == ["; ".join(expected)]
        assert page.evaluate("document.cookie") == "; ".join(expected[1:])
        assert page.evaluate("localStorage.login") == "saved"
        assert page.evaluate("sessionStorage.getItem('tab') === null")
        profile.cookies.clear()
        await profile.flush()

    async with async_api.Browser(proxy="", stub_logging="off") as browser:
        profile = browser.new_profile(user_data_dir=tmp_path, persist_session_cookies=True)
        assert profile.cookies.header_for(url) == ""


def test_sync_flush_preserves_origin_isolation_and_storage_operations(tmp_path):
    origin = "https://site.test/"
    with sync_api.Browser(real_network=False, stub_logging="off") as browser:
        with browser.new_profile(user_data_dir=tmp_path) as profile:
            page = profile.new_page()
            page.set_content("<body></body>", url=origin)
            page.evaluate("localStorage.token = 'one'; localStorage.setItem('remove', 'value')")
            old_area = page._impl.window.localStorage
            peer = profile.new_page()
            peer.set_content("<body></body>", url=origin)
            assert peer.evaluate("localStorage.token") == "one"
            peer.evaluate("delete localStorage.remove; localStorage.token = 'two'")
            for url in ("http://site.test/", "https://other.test/", "https://site.test:8443/"):
                page.set_content("<body></body>", url=url)
                assert page.evaluate("localStorage.getItem('token') === null")
                page.evaluate("localStorage.token = 'different origin'")
            assert old_area.fn_getItem("token") == "two"
            assert peer.evaluate("localStorage.token") == "two"
            profile.flush()

            other = browser.new_profile()
            page = other.new_page()
            page.set_content("<body></body>", url=origin)
            assert page.evaluate("localStorage.getItem('token') === null")
            other.flush()
            with pytest.raises(ValueError, match="requires user_data_dir"):
                browser.new_profile(persist_session_cookies=True)

    with sync_api.Browser(real_network=False, stub_logging="off") as browser:
        profile = browser.new_profile(user_data_dir=tmp_path)
        page = profile.new_page()
        page.set_content("<body></body>", url=origin)
        assert page.evaluate("localStorage.token") == "two"
        assert page.evaluate("!('remove' in localStorage)")
        page.evaluate("localStorage.clear()")
        profile.flush()

    with sync_api.Browser(real_network=False, stub_logging="off") as browser:
        profile = browser.new_profile(user_data_dir=tmp_path)
        page = profile.new_page()
        page.set_content("<body></body>", url=origin)
        assert page.evaluate("localStorage.length") == 0


async def test_initial_origin_change_rebinds_previously_read_storage(browser):
    page = browser.new_profile().new_page()
    page.evaluate("globalThis.oldStorage = localStorage; localStorage.token = 'blank'")
    await page.set_content("<body></body>", url="https://site.test/")
    assert page.evaluate("oldStorage.token") == "blank"
    assert page.evaluate("localStorage.getItem('token') === null")


async def test_restored_cookie_expiry_policy_and_session_opt_out(tmp_path, monkeypatch):
    import pyv8c.platform.profile_storage as storage_module

    url = "https://site.test/child"
    async with async_api.Browser(real_network=False) as browser:
        profile = browser.new_profile(user_data_dir=tmp_path, persist_session_cookies=True)
        profile.cookies.add_from_response(url, "first=1; Path=/; Max-Age=3600")
        profile.cookies.add_from_response(url, "scope=1; Path=/child; Max-Age=3600; Secure; SameSite=Strict")
        profile.cookies.add_from_response(url, "session=1; Path=/")
        profile.cookies.add_from_response(url, "expire=1; Path=/; Max-Age=20")
    restored_at = storage_module.time.time() + 25
    with monkeypatch.context() as patch:
        patch.setattr(storage_module, "time", SimpleNamespace(time=lambda: restored_at))
        async with async_api.Browser(real_network=False) as browser:
            profile = browser.new_profile(user_data_dir=tmp_path)
            assert profile.cookies.header_for(url) == "scope=1; first=1"
            assert profile.cookies.header_for("http://site.test/child") == "first=1"
            assert profile.cookies.header_for("https://sub.site.test/child") == ""
            assert profile.cookies.header_for(url, site_for_cookies="https://other.test/") == ""
    async with async_api.Browser(real_network=False) as browser:
        profile = browser.new_profile(user_data_dir=tmp_path, persist_session_cookies=True)
        assert profile.cookies.header_for(url) == "scope=1; first=1"


def run_profile_process(directory, code):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(pyv8c.__file__).resolve().parents[1])
    return subprocess.run([sys.executable, "-c", code, str(directory)], cwd=directory,
                          env=env, capture_output=True, text=True, timeout=20)


def test_cross_process_exclusion_and_crash_recovery(tmp_path):
    child = """
import os
import sys
from pyv8c.sync_api import Browser
from pyv8c.foundation.host_errors import HostStateError

with Browser(real_network=False, stub_logging="off") as browser:
    try:
        profile = browser.new_profile(user_data_dir=sys.argv[1])
    except HostStateError as error:
        assert "already in use" in str(error)
        print("locked")
    else:
        page = profile.new_page()
        page.set_content("<body></body>", url="https://site.test/")
        page.evaluate("localStorage.status = 'committed'")
        profile.flush()
        page.evaluate("localStorage.status = 'not committed'")
        os._exit(31)
"""
    with sync_api.Browser(real_network=False) as browser:
        profile = browser.new_profile(user_data_dir=tmp_path)
        with pytest.raises(HostStateError, match="already in use"):
            browser.new_profile(user_data_dir=tmp_path / ".." / tmp_path.name)
        result = run_profile_process(tmp_path, child)
        assert (result.returncode, result.stdout.strip()) == (0, "locked"), result.stderr
        profile.close()
    result = run_profile_process(tmp_path, child)
    assert result.returncode == 31, result.stderr
    with sync_api.Browser(real_network=False) as browser:
        profile = browser.new_profile(user_data_dir=tmp_path)
        page = profile.new_page()
        page.set_content("<body></body>", url="https://site.test/")
        assert page.evaluate("localStorage.status") == "committed"


@pytest.mark.parametrize("unknown_schema", [False, True])
async def test_bad_database_is_preserved_and_lock_released(browser, tmp_path, unknown_schema):
    state = tmp_path / "state.sqlite3"
    if unknown_schema:
        db = sqlite3.connect(state)
        db.execute("PRAGMA user_version = 999")
        db.close()
    else:
        state.write_bytes(b"not a database")
    original = state.read_bytes()
    with pytest.raises(HostStateError, match="schema|Cannot read"):
        browser.new_profile(user_data_dir=tmp_path)
    assert state.read_bytes() == original
    assert not browser.profiles
    state.unlink()
    assert browser.new_profile(user_data_dir=tmp_path)


async def test_profile_creation_failure_releases_directory(browser, tmp_path, monkeypatch):
    def fail(**_):
        raise RuntimeError("simulated profile construction failure")

    with monkeypatch.context() as patch:
        patch.setattr(browser, "_create_profile", fail)
        with pytest.raises(RuntimeError, match="construction failure"):
            browser.new_profile(user_data_dir=tmp_path)
    assert not browser.profiles
    assert browser.new_profile(user_data_dir=tmp_path)


async def test_failed_close_can_retry_without_losing_data(browser, tmp_path, monkeypatch):
    profile = browser.new_profile(user_data_dir=tmp_path)
    page = profile.new_page()
    await page.set_content("<body></body>", url="https://site.test/")
    page.evaluate("localStorage.status = 'retry'")

    def fail():
        raise OSError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(profile._storage, "_write_snapshot", fail)
        with pytest.raises(HostStateError, match="Cannot save"):
            await profile.close()
        assert not profile.closed
        with pytest.raises(HostStateError, match="already in use"):
            browser.new_profile(user_data_dir=tmp_path)
    await profile.close()
    restored = browser.new_profile(user_data_dir=tmp_path).new_page()
    await restored.set_content("<body></body>", url="https://site.test/")
    assert restored.evaluate("localStorage.status") == "retry"


async def test_autosave_keeps_mutations_arriving_during_write(browser, tmp_path, monkeypatch):
    profile = browser.new_profile(user_data_dir=tmp_path)
    page = profile.new_page()
    await page.set_content("<body></body>", url="https://site.test/")
    storage = profile._storage
    first_saved = asyncio.Event()
    second_saved = asyncio.Event()
    release_first = threading.Event()
    loop = asyncio.get_running_loop()
    write_snapshot = storage._write_snapshot

    def pause_first_write():
        revision = write_snapshot()
        if not first_saved.is_set():
            loop.call_soon_threadsafe(first_saved.set)
            assert release_first.wait(5)
        else:
            loop.call_soon_threadsafe(second_saved.set)
        return revision

    monkeypatch.setattr(storage, "_write_snapshot", pause_first_write)
    page.evaluate("localStorage.phase = 'first'")
    try:
        await asyncio.wait_for(first_saved.wait(), 5)
        profile.cookies.add_from_response("https://site.test/", "queued=1; Max-Age=3600")
        page.evaluate("localStorage.phase = 'second'")
    finally:
        release_first.set()
    await asyncio.wait_for(second_saved.wait(), 5)
    await profile.flush()
    await profile.close()
    restored = browser.new_profile(user_data_dir=tmp_path)
    page = restored.new_page()
    await page.set_content("<body></body>", url="https://site.test/")
    assert page.evaluate("localStorage.phase") == "second"
    assert restored.cookies.header_for("https://site.test/") == "queued=1"
