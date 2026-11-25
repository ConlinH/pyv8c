"""GeoIP overlay. These tests never open a real database and never use the network."""
import builtins
import gzip
import os
from types import SimpleNamespace

import pytest

from pyv8c.foundation import geoip
from pyv8c.foundation.environment import BrowserEnvironment, profile_path
from pyv8c.foundation.geoip import (
    GeoIpPlace,
    accuracy_meters,
    apply_geoip,
    cache_directory,
    database_for_ip,
    database_path,
    ensure_database,
    lookup,
    public_ip,
    supplied_database,
)
from pyv8c.foundation.geoip_languages import _LANGUAGES, languages_for_country


@pytest.fixture(autouse=True)
def _no_geoip_network_or_env(monkeypatch):
    monkeypatch.delenv("PYV8C_GEOIP_DB", raising=False)

    def refuse(*_args, **_kwargs):
        raise AssertionError("GeoIP test tried to reach the network")

    monkeypatch.setattr(geoip, "_ipify_get", refuse)


def _database(tmp_path):
    path = tmp_path / "GeoLite2-City.mmdb"
    path.write_bytes(b"not a database")
    return path


def _city(country, timezone, latitude, longitude):
    return SimpleNamespace(
        country=SimpleNamespace(iso_code=country),
        location=SimpleNamespace(
            time_zone=timezone, latitude=latitude, longitude=longitude),
    )


class _Missing(Exception):
    pass


class _Reader:
    def __init__(self, action):
        self.action = action
        self.closed = False
        self.path = None

    def city(self, ip):
        if isinstance(self.action, Exception):
            raise self.action
        return self.action

    def close(self):
        self.closed = True


def _install_reader(monkeypatch, reader):
    module = SimpleNamespace(
        database=SimpleNamespace(Reader=lambda path: _open(reader, path)),
        errors=SimpleNamespace(AddressNotFoundError=_Missing),
    )
    monkeypatch.setattr(geoip, "_geoip2", lambda: module)
    return reader


def _open(reader, path):
    reader.path = path
    return reader


@pytest.mark.parametrize("code,pair", [
    ("US", ("en-US", "en")),
    ("us", ("en-US", "en")),
    ("CN", ("zh-CN", "zh")),
    ("JP", ("ja-JP", "ja")),
    ("DE", ("de-DE", "de")),
    ("VN", ("vi-VN", "vi")),
    ("IN", ("hi-IN", "hi")),
    ("ID", ("id-ID", "id")),
    ("TW", ("zh-TW", "zh")),
    ("HK", ("zh-HK", "zh")),
    ("SG", ("en-SG", "en")),
    ("PH", ("fil-PH", "fil")),
    ("BY", ("ru-BY", "ru")),
    ("BE", ("nl-BE", "nl")),
    ("CH", ("de-CH", "de")),
    ("NG", ("en-NG", "en")),
    ("ZA", ("en-ZA", "en")),
    ("XK", ("sq-XK", "sq")),
])
def test_language_anchors(code, pair):
    assert languages_for_country(code) == pair


def test_language_table_covers_common_countries():
    assert len(_LANGUAGES) >= 240
    for country, (tag, short) in _LANGUAGES.items():
        assert country.isalpha() and country.isupper()
        assert tag.startswith(short + "-")


def test_unknown_country_is_an_error():
    with pytest.raises(ValueError, match="no language"):
        languages_for_country("ZZ")


def test_accuracy_uses_the_shorter_coordinate():
    assert accuracy_meters(40, -74) == 111000
    assert accuracy_meters(40.0, -74.0) == 111000
    assert accuracy_meters(40.71, -74.01) == 1110
    assert accuracy_meters(1.23456, 2.34567) == 10


def test_database_path_requires_a_file(monkeypatch, tmp_path):
    missing = tmp_path / "missing.mmdb"
    with pytest.raises(FileNotFoundError):
        database_path(None)
    with pytest.raises(FileNotFoundError):
        database_path("  ")
    monkeypatch.setenv("PYV8C_GEOIP_DB", " ")
    with pytest.raises(FileNotFoundError):
        database_path(None)
    monkeypatch.setenv("PYV8C_GEOIP_DB", str(missing))
    with pytest.raises(FileNotFoundError):
        database_path(None)
    database = _database(tmp_path)
    other = tmp_path / "other.mmdb"
    other.write_bytes(b"y")
    monkeypatch.setenv("PYV8C_GEOIP_DB", str(other))
    assert database_path(None) == other
    assert database_path(database) == database
    with pytest.raises(TypeError, match="geoip_db must be a path"):
        database_path(1)


def test_cache_directory_follows_xdg_then_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_directory() == tmp_path / "xdg" / "pyv8c"
    monkeypatch.setenv("XDG_CACHE_HOME", "  ")
    monkeypatch.setattr(geoip.Path, "home", classmethod(lambda cls: tmp_path))
    assert cache_directory() == tmp_path / ".cache" / "pyv8c"


def test_supplied_database_is_none_only_when_unnamed(monkeypatch, tmp_path):
    database = _database(tmp_path)
    cached = tmp_path / "pyv8c" / "GeoLite2-City.mmdb"
    cached.parent.mkdir()
    cached.write_bytes(b"cached")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert supplied_database(None) is None
    assert supplied_database(database) == database
    with pytest.raises(FileNotFoundError):
        supplied_database("  ")
    monkeypatch.setenv("PYV8C_GEOIP_DB", " ")
    assert supplied_database(None) is None
    monkeypatch.setenv("PYV8C_GEOIP_DB", str(database))
    assert supplied_database(None) == database


def test_database_for_ip_uses_the_cache_and_does_not_download(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="geoip=True"):
        database_for_ip(None)
    cached = tmp_path / "pyv8c" / "GeoLite2-City.mmdb"
    cached.parent.mkdir()
    cached.write_bytes(b"cached")
    assert database_for_ip(None) == cached
    explicit = _database(tmp_path)
    assert database_for_ip(explicit) == explicit


def _cached(tmp_path, payload, mtime):
    path = tmp_path / "GeoLite2-City.mmdb"
    path.write_bytes(payload)
    os.utime(path, (mtime, mtime))
    return path


async def test_fresh_cache_skips_the_download(tmp_path):
    path = _cached(tmp_path, b"cached", 1_000_000)

    async def fetch(_url):
        raise AssertionError("downloaded")

    found = await ensure_database(fetch, directory=tmp_path, now=1_000_000 + geoip._MAX_AGE_SECONDS)
    assert found == path
    assert path.read_bytes() == b"cached"


async def test_stale_cache_is_replaced_by_the_download(monkeypatch, tmp_path):
    path = _cached(tmp_path, b"old", 1_000_000)
    seen = []
    monkeypatch.setattr(geoip, "_accept_database", lambda _path: None)

    async def fetch(url):
        seen.append(url)
        return gzip.compress(b"new")

    found = await ensure_database(
        fetch, directory=tmp_path, now=1_000_000 + geoip._MAX_AGE_SECONDS + 1)
    assert seen == [geoip._DATABASE_URL]
    assert found == path
    assert path.read_bytes() == b"new"
    assert [item.name for item in tmp_path.iterdir()] == ["GeoLite2-City.mmdb"]


async def test_stale_refresh_failure_keeps_the_old_file(tmp_path):
    path = _cached(tmp_path, b"old", 1_000_000)

    async def fetch(_url):
        raise OSError("down")

    found = await ensure_database(
        fetch, directory=tmp_path, now=1_000_000 + geoip._MAX_AGE_SECONDS + 1)
    assert found == path
    assert path.read_bytes() == b"old"


async def test_missing_download_failure_raises_and_writes_nothing(tmp_path):
    async def fetch(_url):
        raise OSError("down")

    with pytest.raises(OSError, match="down"):
        await ensure_database(fetch, directory=tmp_path, now=0)
    assert list(tmp_path.iterdir()) == []


async def test_bad_gzip_does_not_replace_a_stale_file(tmp_path):
    path = _cached(tmp_path, b"old", 1_000_000)

    async def fetch(_url):
        return b"not gzip"

    found = await ensure_database(
        fetch, directory=tmp_path, now=1_000_000 + geoip._MAX_AGE_SECONDS + 1)
    assert found.read_bytes() == b"old"
    assert [item.name for item in tmp_path.iterdir()] == ["GeoLite2-City.mmdb"]


async def test_invalid_database_is_discarded(monkeypatch, tmp_path):
    def reject(_path):
        raise OSError("invalid")

    monkeypatch.setattr(geoip, "_accept_database", reject)

    async def fetch(_url):
        return gzip.compress(b"nope")

    with pytest.raises(OSError, match="invalid"):
        await ensure_database(fetch, directory=tmp_path, now=0)
    assert list(tmp_path.iterdir()) == []


def test_none_does_not_look_anything_up():
    environment = BrowserEnvironment()
    assert apply_geoip(environment, None, geoip_db="/no/such/file") is environment


@pytest.mark.parametrize("value", [False, 1, 1.2, object()])
def test_geoip_rejects_anything_but_true_or_text(value):
    with pytest.raises(TypeError, match="^geoip must be True or an IP address$"):
        apply_geoip(BrowserEnvironment(), value)


def test_private_and_documentation_addresses_skip_the_reader(monkeypatch, tmp_path):
    database = _database(tmp_path)

    def fail():
        raise AssertionError("reader package imported")

    monkeypatch.setattr(geoip, "_geoip2", fail)
    for address in ("10.0.0.1", "203.0.113.10", "not-an-ip", "2001:db8::1"):
        with pytest.raises(ValueError, match="not a public IP"):
            lookup(address, database)
        with pytest.raises(ValueError, match="not a public IP"):
            apply_geoip(BrowserEnvironment(), address, geoip_db=database)


def test_missing_database_is_reported_before_the_package(monkeypatch):
    def fail():
        raise AssertionError("reader package imported")

    monkeypatch.setattr(geoip, "_geoip2", fail)
    with pytest.raises(FileNotFoundError, match="GeoIP database"):
        apply_geoip(BrowserEnvironment(), True)


def test_missing_package_is_reported_before_the_probe(monkeypatch, tmp_path):
    database = _database(tmp_path)

    def probe(_proxy):
        raise AssertionError("probed")

    monkeypatch.setattr(geoip, "public_ip", probe)
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "geoip2" or name.startswith("geoip2."):
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ImportError, match=r"pyv8c\[geoip\]"):
        apply_geoip(BrowserEnvironment(), True, geoip_db=database)


def test_public_ip_forces_direct_or_uses_the_proxy(monkeypatch):
    seen = {}

    def get(url, *, proxies, timeout):
        seen["url"] = url
        seen["proxies"] = dict(proxies)
        seen["timeout"] = timeout
        body = "8.8.8.8\n" if proxies["http"] else "1.1.1.1"
        return SimpleNamespace(status_code=200, text=body)

    monkeypatch.setattr(geoip, "_ipify_get", get)
    assert public_ip(None) == "1.1.1.1"
    assert seen == {
        "url": "https://api.ipify.org",
        "proxies": {"http": "", "https": ""},
        "timeout": 10,
    }
    assert public_ip("") == "1.1.1.1"
    assert public_ip("http://127.0.0.1:9") == "8.8.8.8"
    assert seen["proxies"] == {
        "http": "http://127.0.0.1:9",
        "https": "http://127.0.0.1:9",
    }


def test_public_ip_reports_transport_failure(monkeypatch):
    def status(url, *, proxies, timeout):
        return SimpleNamespace(status_code=503, text="down")

    monkeypatch.setattr(geoip, "_ipify_get", status)
    with pytest.raises(OSError, match="HTTP 503"):
        public_ip(None)

    def broken(url, *, proxies, timeout):
        raise ConnectionError("down")

    monkeypatch.setattr(geoip, "_ipify_get", broken)
    with pytest.raises(ConnectionError):
        public_ip("http://127.0.0.1:9")

    def wrapped(url, *, proxies, timeout):
        raise RuntimeError("curl")

    monkeypatch.setattr(geoip, "_ipify_get", wrapped)
    with pytest.raises(OSError, match="could not look up the public IP"):
        public_ip(None)

    def garbage(url, *, proxies, timeout):
        return SimpleNamespace(status_code=200, text="nope")

    monkeypatch.setattr(geoip, "_ipify_get", garbage)
    with pytest.raises(ValueError, match="not a public IP"):
        public_ip(None)


def test_lookup_closes_the_reader_and_maps_the_record(monkeypatch, tmp_path):
    database = _database(tmp_path)
    reader = _install_reader(monkeypatch, _Reader(
        _city("US", "America/New_York", 40.71, -74.01)))
    place = lookup("1.1.1.1", database)
    assert reader.closed is True
    assert reader.path == str(database)
    assert place == GeoIpPlace("1.1.1.1", "US", "America/New_York", 40.71, -74.01)


def test_lookup_turns_a_missing_record_into_value_error(monkeypatch, tmp_path):
    reader = _install_reader(monkeypatch, _Reader(_Missing("absent")))
    with pytest.raises(ValueError, match="no GeoIP record"):
        lookup("8.8.8.8", _database(tmp_path))
    assert reader.closed is True


def test_lookup_closes_when_the_record_is_incomplete(monkeypatch, tmp_path):
    database = _database(tmp_path)
    incomplete = (
        _city(None, "America/New_York", 40.71, -74.01),
        _city("US", None, 40.71, -74.01),
        _city("US", "America/New_York", None, -74.01),
    )
    for city in incomplete:
        reader = _install_reader(monkeypatch, _Reader(city))
        with pytest.raises(ValueError, match="GeoIP record"):
            lookup("1.1.1.1", database)
        assert reader.closed is True


def test_other_reader_errors_propagate_and_the_reader_closes(monkeypatch, tmp_path):
    reader = _install_reader(monkeypatch, _Reader(RuntimeError("bad database")))
    with pytest.raises(RuntimeError, match="bad database"):
        lookup("1.1.1.1", _database(tmp_path))
    assert reader.closed is True


def test_probe_failure_leaves_the_profile_timezone(monkeypatch, tmp_path):
    database = _database(tmp_path)
    monkeypatch.setattr(geoip, "_geoip2", lambda: object())

    def status(url, *, proxies, timeout):
        return SimpleNamespace(status_code=503, text="")

    monkeypatch.setattr(geoip, "_ipify_get", status)
    environment = BrowserEnvironment.from_file(profile_path("darwin"))
    assert environment.resolved()["timezone"] == "Asia/Shanghai"
    with pytest.raises(OSError):
        apply_geoip(environment, True, geoip_db=database, caller_keys=())
    assert environment.resolved()["timezone"] == "Asia/Shanghai"


def test_apply_replaces_a_profile_file(monkeypatch, tmp_path):
    database = _database(tmp_path)
    reader = _install_reader(monkeypatch, _Reader(
        _city("us", "America/New_York", 40.71, -74.01)))
    environment = BrowserEnvironment.from_file(profile_path("darwin"))
    updated = apply_geoip(environment, "1.1.1.1", geoip_db=database, caller_keys=())
    resolved = updated.resolved()
    assert resolved["timezone"] == "America/New_York"
    assert resolved["languages"] == ("en-US", "en")
    assert resolved["language"] == "en-US"
    assert resolved["geolocation.latitude"] == 40.71
    assert resolved["geolocation.longitude"] == -74.01
    assert resolved["geolocation.accuracy"] == 1110
    assert resolved["geolocation.permission"] == "granted"
    assert resolved["webrtc.ip"] == "1.1.1.1"
    assert environment.resolved()["timezone"] == "Asia/Shanghai"
    assert reader.closed is True


def test_true_probes_before_lookup_and_passes_the_proxy(monkeypatch, tmp_path):
    database = _database(tmp_path)
    order = []

    def package():
        order.append("package")
        return object()

    def probe(proxy):
        order.append(("probe", proxy))
        return "8.8.8.8"

    def found(ip, path):
        order.append(("lookup", ip, path))
        return GeoIpPlace("8.8.8.8", "US", "America/New_York", 40, -74)

    monkeypatch.setattr(geoip, "_geoip2", package)
    monkeypatch.setattr(geoip, "public_ip", probe)
    monkeypatch.setattr(geoip, "lookup", found)
    updated = apply_geoip(
        BrowserEnvironment(), True, geoip_db=database, proxy="http://127.0.0.1:9")
    assert order == [
        "package",
        ("probe", "http://127.0.0.1:9"),
        ("lookup", "8.8.8.8", database),
    ]
    assert updated.resolved()["timezone"] == "America/New_York"
    assert updated.resolved()["webrtc.ip"] == "8.8.8.8"


def test_an_address_skips_the_probe(monkeypatch, tmp_path):
    database = _database(tmp_path)

    def probe(_proxy):
        raise AssertionError("probed")

    monkeypatch.setattr(geoip, "public_ip", probe)
    _install_reader(monkeypatch, _Reader(
        _city("CN", "Asia/Shanghai", 31.2, 121.5)))
    updated = apply_geoip(BrowserEnvironment(), "1.1.1.1", geoip_db=database)
    assert updated.resolved()["timezone"] == "Asia/Shanghai"
    assert updated.resolved()["languages"] == ("zh-CN", "zh")
    assert updated.resolved()["language"] == "zh-CN"


def test_caller_languages_keep_the_derived_language(monkeypatch, tmp_path):
    _install_reader(monkeypatch, _Reader(
        _city("US", "America/New_York", 40.71, -74.01)))
    base = BrowserEnvironment({"languages": ["fr-FR", "fr"]})
    updated = apply_geoip(
        base, "8.8.8.8", geoip_db=_database(tmp_path), caller_keys=("languages",))
    resolved = updated.resolved()
    assert resolved["languages"] == ("fr-FR", "fr")
    assert resolved["language"] == "fr-FR"
    assert resolved["timezone"] == "America/New_York"


def test_caller_language_keeps_the_geoip_list(monkeypatch, tmp_path):
    _install_reader(monkeypatch, _Reader(
        _city("US", "America/New_York", 40.71, -74.01)))
    base = BrowserEnvironment({"language": "fr-FR"})
    updated = apply_geoip(
        base, "8.8.8.8", geoip_db=_database(tmp_path), caller_keys=("language",))
    resolved = updated.resolved()
    assert resolved["language"] == "fr-FR"
    assert resolved["languages"] == ("en-US", "en")


def test_caller_permission_wins(monkeypatch, tmp_path):
    _install_reader(monkeypatch, _Reader(
        _city("US", "America/New_York", 40.71, -74.01)))
    base = BrowserEnvironment({"geolocation.permission": "denied"})
    updated = apply_geoip(
        base, "8.8.8.8", geoip_db=_database(tmp_path),
        caller_keys=("geolocation.permission",))
    resolved = updated.resolved()
    assert resolved["geolocation.permission"] == "denied"
    assert resolved["geolocation.latitude"] == 40.71


def test_webrtc_ip_is_optional_text():
    assert BrowserEnvironment().resolved()["webrtc.ip"] is None
    environment = BrowserEnvironment({"webrtc.ip": "203.0.113.10"})
    assert environment.resolved()["webrtc.ip"] == "203.0.113.10"
    with pytest.raises(ValueError, match="webrtc.ip"):
        BrowserEnvironment({"webrtc.ip": ""})
    with pytest.raises(TypeError, match="webrtc.ip"):
        BrowserEnvironment({"webrtc.ip": 1})
