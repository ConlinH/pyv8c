"""Fill a browser environment from a public IP and a local GeoIP city database.

``Browser(geoip=True)`` asks the first profile's HTTP session for the exit
address, then overlays the time zone, languages, coordinates and WebRTC host
address that address maps to. When the caller names no database, that same
session downloads a full GeoLite2-City database into the local cache first.
``apply_geoip(True)`` still probes with curl_cffi for a caller that already
holds a proxy URL; the browser does not use that path. A failed lookup raises
instead of leaving the profile's own time zone in place.
"""
from __future__ import annotations

import asyncio
import gzip
import ipaddress
import os
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from .geoip_languages import languages_for_country

_IPIFY = "https://api.ipify.org"
_TIMEOUT = 10
_DEGREE_METERS = 111000
_MIN_ACCURACY = 10.0
_DATABASE_URL = "https://cdn.jsdelivr.net/npm/geolite2-city/GeoLite2-City.mmdb.gz"
_DATABASE_NAME = "GeoLite2-City.mmdb"
_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class GeoIpPlace:
    """One city-database hit, reduced to the fields the environment stores."""

    ip: str
    country: str
    timezone: str
    latitude: float
    longitude: float


def database_path(geoip_db):
    """The mmdb to open: ``geoip_db``, or ``PYV8C_GEOIP_DB`` when that is omitted.

    A blank value is missing. The file is not created and not downloaded, and
    the cache is not consulted.
    """
    if geoip_db is None:
        geoip_db = os.environ.get("PYV8C_GEOIP_DB")
    if geoip_db is None or (isinstance(geoip_db, str) and geoip_db.strip() == ""):
        raise FileNotFoundError(
            "GeoIP database was not found; set geoip_db or PYV8C_GEOIP_DB "
            "to a GeoLite2-City.mmdb file")
    if not isinstance(geoip_db, (str, os.PathLike)):
        raise TypeError("geoip_db must be a path")
    path = Path(geoip_db).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"GeoIP database was not found: {path}")
    return path


def cache_directory():
    """``$XDG_CACHE_HOME/pyv8c``, or ``~/.cache/pyv8c`` when that variable is blank."""
    root = os.environ.get("XDG_CACHE_HOME")
    if root is not None and root.strip() != "":
        base = Path(root).expanduser()
    else:
        base = Path.home() / ".cache"
    return base / "pyv8c"


def cache_file(directory=None):
    """The cached city database. ``directory`` replaces :func:`cache_directory`."""
    root = cache_directory() if directory is None else Path(directory)
    return root / _DATABASE_NAME


def supplied_database(geoip_db):
    """The caller's mmdb, or ``None`` when neither the argument nor the env names one.

    A blank argument is an error. A blank environment variable counts as unset.
    A named file that does not exist is ``FileNotFoundError``. ``None`` means
    the caller did not choose a file; it does not look at the cache.
    """
    if geoip_db is not None:
        return database_path(geoip_db)
    env = os.environ.get("PYV8C_GEOIP_DB")
    if env is None or str(env).strip() == "":
        return None
    return database_path(None)


def database_for_ip(geoip_db):
    """Database for an address passed in directly: the caller's file, or the cache.

    Construction has no page session, so a missing cache is not downloaded.
    """
    supplied = supplied_database(geoip_db)
    if supplied is not None:
        return supplied
    cached = cache_file()
    if cached.is_file():
        return cached
    raise FileNotFoundError(
        "GeoIP database was not found; set geoip_db or PYV8C_GEOIP_DB "
        "to a GeoLite2-City.mmdb file, or use geoip=True to download one")


def _is_fresh(path, *, now, max_age):
    return now - path.stat().st_mtime <= max_age


def _gunzip(payload):
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise OSError("GeoIP database download was not bytes")
    try:
        return gzip.decompress(payload)
    except (EOFError, OSError, zlib.error) as error:
        raise OSError("GeoIP database download was not a gzip file") from error


def _accept_database(path):
    """Open ``path`` as a city database and close it. An invalid file raises."""
    geoip2 = _geoip2()
    reader = geoip2.database.Reader(os.fspath(path))
    try:
        reader.metadata()
    finally:
        reader.close()


def _install_database(payload, path):
    """Write a gzip city database over ``path`` only after it opens as one."""
    data = _gunzip(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".GeoLite2-City.", suffix=".partial", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.write_bytes(data)
        _accept_database(temporary)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


async def ensure_database(fetch, *, directory=None, now=None, max_age=_MAX_AGE_SECONDS):
    """Return a city database, calling ``fetch`` with the gzip URL when needed.

    ``fetch`` returns the response body. A file younger than ``max_age`` seconds
    is returned without calling it. A failed refresh keeps the previous file.
    A missing file that cannot be installed raises the download error.
    """
    path = cache_file(directory)
    moment = time.time() if now is None else now
    if path.is_file() and _is_fresh(path, now=moment, max_age=max_age):
        return path
    try:
        payload = await fetch(_DATABASE_URL)
        return await asyncio.to_thread(_install_database, payload, path)
    except Exception:
        if path.is_file():
            return path
        raise


def _geoip2():
    """Import the optional reader. Called only once a lookup is actually going to run.

    ``geoip2`` does not load its submodules from ``__init__``, so ``import geoip2``
    alone leaves ``database`` and ``errors`` missing.
    """
    try:
        import geoip2.database
        import geoip2.errors
        import geoip2
    except ImportError as error:
        raise ImportError(
            "GeoIP lookup requires the geoip2 package; install pyv8c[geoip]"
        ) from error
    return geoip2


def _require_public(text):
    """The compressed form of a global address, or ``ValueError``."""
    raw = str(text).strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        raise ValueError(f"not a public IP address: {text!r}") from None
    if not address.is_global:
        raise ValueError(f"not a public IP address: {address}")
    return str(address)


def _probe_proxies(proxy):
    """Proxies for one ipify request that do not inherit ``HTTP_PROXY``.

    curl_cffi reads the environment when the request omits proxies. An empty
    string on both schemes is what forces this machine's own exit. A configured
    proxy is sent for both schemes. This mapping stays here so foundation does
    not import the network layer.
    """
    if proxy:
        return {"http": proxy, "https": proxy}
    return {"http": "", "https": ""}


def _ipify_get(url, *, proxies, timeout):
    from curl_cffi import requests

    return requests.get(url, proxies=proxies, timeout=timeout)


def public_ip(proxy):
    """The exit address: through ``proxy``, or this machine when ``proxy`` is empty.

    Transport failure, including a non-200 response, is ``OSError``. A body that
    is not a public IP is ``ValueError``.
    """
    try:
        response = _ipify_get(_IPIFY, proxies=_probe_proxies(proxy), timeout=_TIMEOUT)
    except OSError:
        raise
    except Exception as error:
        raise OSError("could not look up the public IP") from error
    status = getattr(response, "status_code", None)
    if status != 200:
        raise OSError(f"public IP lookup returned HTTP {status}")
    body = getattr(response, "text", "")
    if not isinstance(body, str):
        body = "" if body is None else str(body)
    return _require_public(body.strip())


def _record(ip, city):
    country = getattr(getattr(city, "country", None), "iso_code", None)
    location = getattr(city, "location", None)
    timezone = getattr(location, "time_zone", None) if location is not None else None
    latitude = getattr(location, "latitude", None) if location is not None else None
    longitude = getattr(location, "longitude", None) if location is not None else None
    if not country:
        raise ValueError(f"GeoIP record for {ip} has no country")
    if not timezone:
        raise ValueError(f"GeoIP record for {ip} has no time zone")
    if latitude is None or longitude is None:
        raise ValueError(f"GeoIP record for {ip} has no coordinates")
    return GeoIpPlace(
        ip=ip,
        country=str(country),
        timezone=str(timezone),
        latitude=float(latitude),
        longitude=float(longitude),
    )


def lookup(ip, path):
    """Open the city database for one public address and close it before returning.

    A private or non-address is rejected before the reader package is imported.
    ``AddressNotFoundError`` becomes ``ValueError``. Other reader errors propagate.
    """
    public = _require_public(ip)
    geoip2 = _geoip2()
    reader = geoip2.database.Reader(os.fspath(path))
    try:
        try:
            city = reader.city(public)
        except geoip2.errors.AddressNotFoundError as error:
            raise ValueError(f"no GeoIP record for {public}") from error
        return _record(public, city)
    finally:
        reader.close()


def _decimal_places(value):
    """Digits after the point in ``format(value, "f")``, ignoring trailing zeros."""
    text = format(float(value), "f").rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def accuracy_meters(latitude, longitude):
    """Meters of one degree, reduced by the shorter coordinate's decimal places.

    One degree is about 111000 meters. Each extra digit divides that by ten,
    and the result never drops below 10 meters.
    """
    places = min(_decimal_places(latitude), _decimal_places(longitude))
    return max(_MIN_ACCURACY, _DEGREE_METERS / 10**places)


def place_overlay(place):
    """Environment keys for one place. Coordinates grant geolocation permission."""
    full, short = languages_for_country(place.country)
    return {
        "timezone": place.timezone,
        "languages": (full, short),
        "language": full,
        "geolocation.latitude": place.latitude,
        "geolocation.longitude": place.longitude,
        "geolocation.accuracy": accuracy_meters(place.latitude, place.longitude),
        "geolocation.permission": "granted",
        "webrtc.ip": place.ip,
    }


def _without_caller(overlay, caller_keys):
    """Drop keys the caller already set.

    ``languages`` also blocks ``language``, because the resolved language is
    derived from the first language and a GeoIP language would cover it again.
    ``language`` alone blocks only itself, so the caller's tag wins and the
    GeoIP language list remains.
    """
    blocked = set(caller_keys)
    if "languages" in blocked:
        blocked.add("language")
    return {key: value for key, value in overlay.items() if key not in blocked}


def apply_geoip(environment, geoip, *, geoip_db=None, proxy=None, caller_keys=()):
    """Overlay ``environment`` from ``geoip``, or return it unchanged when ``geoip`` is ``None``.

    ``True`` probes ``proxy``. A string is the address and skips the probe.
    The database path and the reader package are checked before any request.
    """
    if geoip is None:
        return environment
    if geoip is not True and not isinstance(geoip, str):
        raise TypeError("geoip must be True or an IP address")
    path = database_path(geoip_db)
    if isinstance(geoip, str):
        ip = _require_public(geoip)
    _geoip2()
    if geoip is True:
        ip = public_ip(proxy)
    place = lookup(ip, path)
    return environment.overlay(_without_caller(place_overlay(place), caller_keys))
