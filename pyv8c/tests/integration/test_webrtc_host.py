"""A configured webrtc.ip is one host candidate. GeoIP wiring does not use the network."""
import asyncio
import gzip
import json
import os
from concurrent.futures import Future
from urllib.parse import urljoin

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c.async_api import Browser
from pyv8c.foundation.environment import BrowserEnvironment, profile_path
from pyv8c.foundation.geoip import _DATABASE_URL
from pyv8c.network.models import NetworkResponse
from pyv8c.platform.browser import _geoip_caller_keys
from pyv8c.web.bom.webrtc import _host_candidate_line


def test_host_candidate_line_keeps_ipv6_unbracketed():
    assert _host_candidate_line("203.0.113.10") == (
        "candidate:1 1 udp 2122262783 203.0.113.10 49152 typ host")
    assert _host_candidate_line("2001:db8::1") == (
        "candidate:1 1 udp 2122262783 2001:db8::1 49152 typ host")


def test_geoip_caller_keys_follow_the_caller_not_a_profile_path():
    assert _geoip_caller_keys(None) == frozenset()
    assert _geoip_caller_keys(profile_path("darwin")) == frozenset()
    assert _geoip_caller_keys({"languages": ["fr-FR", "fr"]}) == frozenset({"languages"})
    environment = BrowserEnvironment.from_file(profile_path("darwin"))
    assert "timezone" in _geoip_caller_keys(environment)
    assert "languages" in _geoip_caller_keys(environment)


async def test_browser_geoip_uses_apply_and_skips_none(monkeypatch, tmp_path):
    database = tmp_path / "GeoLite2-City.mmdb"
    database.write_bytes(b"x")
    calls = []

    def fake(environment, geoip, *, geoip_db=None, proxy=None, caller_keys=()):
        calls.append({
            "geoip": geoip,
            "geoip_db": geoip_db,
            "proxy": proxy,
            "caller_keys": set(caller_keys),
        })
        return environment

    monkeypatch.setattr("pyv8c.foundation.geoip.apply_geoip", fake)
    idle = await Browser(real_network=False, stub_logging="off").__aenter__()
    try:
        assert calls == []
    finally:
        await idle.close()

    browser = await Browser(
        environment=profile_path("darwin"),
        geoip="1.1.1.1",
        geoip_db=database,
        proxy="http://127.0.0.1:9",
        real_network=False,
        stub_logging="off",
    ).__aenter__()
    try:
        assert calls == [{
            "geoip": "1.1.1.1",
            "geoip_db": database,
            "proxy": "http://127.0.0.1:9",
            "caller_keys": set(),
        }]
    finally:
        await browser.close()


def _watch(page):
    page.evaluate("""(() => {
        window.seen = [];
        const pc = new RTCPeerConnection();
        window.pc = pc;
        pc.createDataChannel("data");
        pc.onicecandidate = (event) => {
            const candidate = event.candidate;
            window.seen.push(candidate ? String(candidate.candidate) : null);
        };
        pc.setLocalDescription();
    })()""")


async def _gathered(page):
    _watch(page)
    await page.wait_for(
        "window.seen.length > 0 && window.seen[window.seen.length - 1] === null",
        timeout=2,
    )
    return json.loads(page.evaluate("""(() => JSON.stringify({
        seen: window.seen,
        sdp: window.pc.localDescription.sdp
    }))()"""))


class _IpifyEngine:
    """One profile's HTTP engine. Every engine shares the request log."""

    def __init__(self, seen, status):
        self.cookies = None
        self.seen = seen
        self.status = status

    def submit_request(self, request, handle, remaining):
        self.seen.append(request)
        future = Future()
        future.set_result(NetworkResponse(
            url=request.url, status=self.status[0], body="1.1.1.1"))
        return future

    def close(self):
        return None


def _patch_geoip(monkeypatch):
    calls = []

    def fake(environment, geoip, *, geoip_db=None, proxy=None, caller_keys=()):
        calls.append({
            "geoip": geoip,
            "geoip_db": geoip_db,
            "proxy": proxy,
            "caller_keys": set(caller_keys),
        })
        return environment

    def refuse(*_args, **_kwargs):
        raise AssertionError("ipify used a separate curl session")

    monkeypatch.setattr("pyv8c.foundation.geoip.apply_geoip", fake)
    monkeypatch.setattr("pyv8c.foundation.geoip._ipify_get", refuse)
    monkeypatch.setattr("pyv8c.foundation.geoip._geoip2", lambda: object())
    return calls


def _database(tmp_path):
    path = tmp_path / "GeoLite2-City.mmdb"
    path.write_bytes(b"x")
    return path


@pytest.mark.parametrize("kwargs", [
    {"real_network": False},
    {"http_engine": None},
])
def test_geoip_true_without_an_http_engine_is_refused(kwargs):
    with pytest.raises(TypeError, match="HTTP engine"):
        Browser(geoip=True, stub_logging="off", **kwargs)


async def test_geoip_true_uses_the_profile_session_once(monkeypatch, tmp_path):
    database = _database(tmp_path)
    calls = _patch_geoip(monkeypatch)
    seen = []
    status = [200]

    def factory():
        return _IpifyEngine(seen, status)

    browser = await Browser(
        geoip=True,
        geoip_db=database,
        http_engine=factory,
        ws_engine=None,
        stub_logging="off",
    ).__aenter__()
    try:
        await browser.new_profile_async()
        await browser.new_profile_async()
    finally:
        await browser.close()

    assert len(seen) == 1
    request = seen[0]
    assert request.url == "https://api.ipify.org"
    assert request.method == "GET"
    assert request.credentials == "omit"
    assert calls == [{
        "geoip": "1.1.1.1",
        "geoip_db": database,
        "proxy": None,
        "caller_keys": set(),
    }]
    assert browser._geoip_pending is None


async def test_geoip_probe_failure_retries_on_the_next_profile(monkeypatch, tmp_path):
    database = _database(tmp_path)
    calls = _patch_geoip(monkeypatch)
    seen = []
    status = [500]

    def factory():
        return _IpifyEngine(seen, status)

    browser = await Browser(
        geoip=True,
        geoip_db=database,
        http_engine=factory,
        ws_engine=None,
        stub_logging="off",
    ).__aenter__()
    try:
        with pytest.raises(OSError, match="HTTP 500"):
            await browser.new_profile_async()
        assert calls == []
        assert browser._geoip_pending is not None
        assert browser.profiles == []
        status[0] = 200
        await browser.new_profile_async()
    finally:
        await browser.close()

    assert len(seen) == 2
    assert calls == [{
        "geoip": "1.1.1.1",
        "geoip_db": database,
        "proxy": None,
        "caller_keys": set(),
    }]
    assert browser._geoip_pending is None


def test_blank_geoip_db_is_refused_before_a_profile():
    with pytest.raises(FileNotFoundError):
        Browser(
            geoip=True,
            geoip_db="  ",
            http_engine=lambda: object(),
            stub_logging="off",
        )


async def test_string_ip_uses_an_existing_cache_without_a_request(monkeypatch, tmp_path):
    cached = tmp_path / "GeoLite2-City.mmdb"
    cached.write_bytes(b"x")
    monkeypatch.setattr("pyv8c.foundation.geoip.cache_file", lambda directory=None: cached)
    calls = []

    def fake(environment, geoip, *, geoip_db=None, proxy=None, caller_keys=()):
        calls.append(geoip_db)
        return environment

    monkeypatch.setattr("pyv8c.foundation.geoip.apply_geoip", fake)
    browser = await Browser(
        geoip="1.1.1.1", real_network=False, stub_logging="off").__aenter__()
    try:
        assert calls == [cached]
    finally:
        await browser.close()


def test_string_ip_without_a_database_does_not_download(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pyv8c.foundation.geoip.cache_file",
        lambda directory=None: tmp_path / "missing.mmdb")
    with pytest.raises(FileNotFoundError, match="geoip=True"):
        Browser(geoip="1.1.1.1", real_network=False, stub_logging="off")


class _QueuedEngine:
    """HTTP engine whose responses are consumed in order. Requests are recorded."""

    def __init__(self, seen, responses):
        self.cookies = None
        self.seen = seen
        self.responses = responses

    def submit_request(self, request, handle, remaining):
        self.seen.append(request)
        status, body, headers = self.responses.pop(0)
        future = Future()
        budget = remaining()
        if request.timeout is not None:
            assert 0 < budget <= request.timeout
        response = NetworkResponse(url=request.url, status=status, body=body, headers=headers)
        asyncio.get_running_loop().call_soon(future.set_result, response)
        return future

    def close(self):
        return None


def _patch_cache(monkeypatch, directory):
    monkeypatch.setattr("pyv8c.foundation.geoip.cache_directory", lambda: directory)
    monkeypatch.setattr("pyv8c.foundation.geoip._accept_database", lambda _path: None)


async def test_geoip_download_deadline_cancels_stalled_transport(monkeypatch):
    from pyv8c.platform import browser as platform_browser

    handles = []

    class StalledEngine:
        def submit_request(self, request, handle, remaining):
            assert 0 < remaining() <= 0.050001
            handles.append(handle)
            return Future()

    monkeypatch.setattr(platform_browser, "_DATABASE_TIMEOUT", 0.05)
    with pytest.raises(TimeoutError):
        await platform_browser._download_database(StalledEngine(), _DATABASE_URL)
    assert len(handles) == 1 and handles[0].cancelled


async def test_geoip_true_downloads_once_into_the_cache(monkeypatch, tmp_path):
    calls = _patch_geoip(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    seen = []
    payload = gzip.compress(b"city-db")
    responses = [
        (302, b"", {"location": "GeoLite2-City.mmdb"}),
        (200, payload, {}),
        (200, "1.1.1.1", {}),
    ]

    def factory():
        return _QueuedEngine(seen, responses)

    browser = await Browser(
        geoip=True, http_engine=factory, ws_engine=None, stub_logging="off").__aenter__()
    try:
        await browser.new_profile_async()
        await browser.new_profile_async()
    finally:
        await browser.close()

    cached = tmp_path / "GeoLite2-City.mmdb"
    assert cached.read_bytes() == b"city-db"
    assert [request.url for request in seen] == [
        _DATABASE_URL,
        urljoin(_DATABASE_URL, "GeoLite2-City.mmdb"),
        "https://api.ipify.org",
    ]
    download = seen[0]
    assert download.method == "GET"
    assert download.credentials == "omit"
    assert download.redirect == "manual"
    assert download.timeout == 120
    assert seen[1].timeout == 120
    assert seen[2].timeout is None
    assert calls == [{
        "geoip": "1.1.1.1",
        "geoip_db": cached,
        "proxy": None,
        "caller_keys": set(),
    }]
    assert browser._geoip_pending is None


async def test_fresh_cache_is_not_downloaded_again(monkeypatch, tmp_path):
    calls = _patch_geoip(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    cached = tmp_path / "GeoLite2-City.mmdb"
    cached.write_bytes(b"cached")
    seen = []
    responses = [(200, "1.1.1.1", {})]

    def factory():
        return _QueuedEngine(seen, responses)

    browser = await Browser(
        geoip=True, http_engine=factory, ws_engine=None, stub_logging="off").__aenter__()
    try:
        await browser.new_profile_async()
    finally:
        await browser.close()

    assert [request.url for request in seen] == ["https://api.ipify.org"]
    assert cached.read_bytes() == b"cached"
    assert calls[0]["geoip_db"] == cached


async def test_stale_refresh_failure_keeps_the_cached_file(monkeypatch, tmp_path):
    calls = _patch_geoip(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    cached = tmp_path / "GeoLite2-City.mmdb"
    cached.write_bytes(b"old")
    os.utime(cached, (0, 0))
    seen = []
    responses = [(500, b"", {}), (200, "1.1.1.1", {})]

    def factory():
        return _QueuedEngine(seen, responses)

    browser = await Browser(
        geoip=True, http_engine=factory, ws_engine=None, stub_logging="off").__aenter__()
    try:
        await browser.new_profile_async()
    finally:
        await browser.close()

    assert [request.url for request in seen] == [_DATABASE_URL, "https://api.ipify.org"]
    assert cached.read_bytes() == b"old"
    assert calls[0]["geoip_db"] == cached
    assert browser._geoip_pending is None


async def test_missing_download_retries_on_the_next_profile(monkeypatch, tmp_path):
    calls = _patch_geoip(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    seen = []
    responses = [(500, b"", {})]

    def factory():
        return _QueuedEngine(seen, responses)

    browser = await Browser(
        geoip=True, http_engine=factory, ws_engine=None, stub_logging="off").__aenter__()
    try:
        with pytest.raises(OSError, match="HTTP 500"):
            await browser.new_profile_async()
        assert calls == []
        assert browser._geoip_pending is not None
        assert browser.profiles == []
        assert list(tmp_path.iterdir()) == []
        responses.append((200, gzip.compress(b"city-db"), {}))
        responses.append((200, "1.1.1.1", {}))
        await browser.new_profile_async()
    finally:
        await browser.close()

    assert (tmp_path / "GeoLite2-City.mmdb").read_bytes() == b"city-db"
    assert len(calls) == 1
    assert browser._geoip_pending is None


async def test_too_many_database_redirects_stay_pending(monkeypatch, tmp_path):
    calls = _patch_geoip(monkeypatch)
    _patch_cache(monkeypatch, tmp_path)
    seen = []
    responses = [(302, b"", {"Location": "https://cdn.example/next"})] * 6

    def factory():
        return _QueuedEngine(seen, responses)

    browser = await Browser(
        geoip=True, http_engine=factory, ws_engine=None, stub_logging="off").__aenter__()
    try:
        with pytest.raises(OSError, match="too many redirects"):
            await browser.new_profile_async()
        assert calls == []
        assert browser._geoip_pending is not None
        assert len(seen) == 6
        assert list(tmp_path.iterdir()) == []
    finally:
        await browser.close()


async def test_host_candidate_follows_webrtc_ip():
    browser = await Browser(real_network=False, stub_logging="off").__aenter__()
    try:
        plain = browser.new_profile().new_page()
        await plain.set_content(
            "<body></body>", url="https://example.test/", wait_until="commit")
        plain_report = await _gathered(plain)
        assert plain_report["seen"] == [None]
        assert "c=IN IP4 0.0.0.0" in plain_report["sdp"]
        assert "IN IP4 127.0.0.1" in plain_report["sdp"]

        host = browser.new_profile().new_page(
            environment={"webrtc.ip": "203.0.113.10"})
        await host.set_content(
            "<body></body>", url="https://example.test/", wait_until="commit")
        host_report = await _gathered(host)
        line = _host_candidate_line("203.0.113.10")
        assert host_report["seen"] == [line, None]
        assert "203.0.113.10" not in host_report["sdp"]
        assert "c=IN IP4 0.0.0.0" in host_report["sdp"]
        assert "IN IP4 127.0.0.1" in host_report["sdp"]
    finally:
        await browser.close()
