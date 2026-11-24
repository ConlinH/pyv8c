"""Immutable browser environment: one flat table of dotted keys.

The defaults are a profile collected from a real Chrome
(``data/chrome-environment.json``). ``BrowserEnvironment`` and any partial mapping
overlay that Windows profile. A ``Browser`` with no environment reads that same
Windows file whole. ``profile_path`` names a packaged file for a platform, or
this host when the platform is omitted; pass that path as ``environment`` to
load it. On darwin the packaged file is ``chrome-environment-darwin.json`` when
it is present. A caller overrides any key by name, and the keys that follow
``user_agent`` move with it. The profile's ``bindings`` section belongs to the
engine, so this module skips it like the other envelope keys.
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields as dataclass_fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote, urlsplit
import posixpath

from .system_fonts import FontRecord
from .urls import origin_tuple

SCHEMA_VERSION = 4


def environment_of(services):
    """The environment a realm's services carry, or ``None`` outside one."""
    realm = getattr(services, "realm_services", None)
    return getattr(realm, "environment", None) if realm is not None else None
#: Keys of a profile that are not settings: the envelope, and what another layer reads.
ENVELOPE_KEYS = ("schema", "source", "bindings")
PROFILE = Path(__file__).parents[1] / "data" / "chrome-environment.json"
_HOST_PROFILES = {
    "win32": PROFILE,
    "darwin": PROFILE.with_name("chrome-environment-darwin.json"),
}


def profile_path(platform=None):
    """The packaged environment for *platform*, or this host when omitted.

    An unknown platform, or a host file that is not packaged, is the Windows profile.
    ``Browser`` does not call this. Pass the path it returns as ``environment``
    to load that file whole.
    """
    name = sys.platform if platform is None else platform
    chosen = _HOST_PROFILES.get(name, PROFILE)
    if chosen.is_file():
        return chosen
    return PROFILE


#: The brands whose version is the browser's own major version; the rest are GREASE entries Chrome
#: renames each release, so they are read from the profile rather than spelled here.
_VERSIONED_BRANDS = ("Google Chrome", "Chromium")


def _text(value):
    if value is None:
        raise TypeError("must be a string")
    return str(value)


def _integer(value):
    if isinstance(value, bool):
        raise TypeError("must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise TypeError("must be an integer") from None


def _number(value):
    if isinstance(value, bool):
        raise TypeError("must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise TypeError("must be a number") from None


def _optional_number(value):
    """A simulated coordinate: absent until a caller gives one."""
    return None if value is None else _number(value)


def _optional_text(value):
    """An optional string: absent, or a non-empty string. An empty string is refused."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("must be a string")
    if value == "":
        raise ValueError("must not be empty")
    return value


def _tokens(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = (value,)
    return tuple(str(item) for item in value)


def _keyboard_layout(value):
    """A ``key code -> character`` table: what the machine's layout prints."""
    if not isinstance(value, Mapping):
        raise TypeError("must be a mapping of key codes to characters")
    return MappingProxyType({str(key): str(item) for key, item in value.items()})


def _media_containers(value):
    """``container -> codec prefixes``: what the reference browser decodes, per container."""
    if not isinstance(value, Mapping):
        raise TypeError("must be a mapping of containers to codec prefixes")
    return MappingProxyType({str(name): _tokens(prefixes) for name, prefixes in value.items()})


def _named_tokens(value):
    """``name -> tokens``: the same shape as ``media.containers``, for other fact tables."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("must be a mapping of names to token lists")
    return MappingProxyType({str(name): _tokens(tokens) for name, tokens in value.items()})


#: Chrome's extension ids: 32 characters of ``a``..``p``.
_EXTENSION_ID = re.compile(r"[a-p]{32}\Z")


def _extensions(value):
    """``id -> declared resources``: what each installed extension exposes to a page.

    The declaration is Chrome's own shape -- the MV2 list of patterns, or a list of
    ``{resources, matches}`` -- so a profile carries the browser's answer, not ours.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("must map extension ids to their declared resources")
    accepted = {}
    for identifier, declared in value.items():
        identifier = str(identifier)
        if not _EXTENSION_ID.match(identifier):
            raise ValueError(f"{identifier!r} is not an extension id")
        accepted[identifier] = _extension_entries(declared)
    return MappingProxyType(accepted)


def _extension_entries(declared):
    """One extension's entries, each ``{"resources": patterns, "matches": origins}``."""
    if isinstance(declared, (str, Mapping)):
        declared = (declared,)
    if not isinstance(declared, (list, tuple)) or not declared:
        raise TypeError("must list the resources an extension exposes")
    entries = []
    for entry in declared:
        if isinstance(entry, str):
            resources, matches = (entry,), ("<all_urls>",)
        elif isinstance(entry, Mapping):
            resources = _tokens(entry.get("resources")) or ()
            if not resources:
                raise TypeError("an entry needs at least one resource pattern")
            matches = _tokens(entry.get("matches")) or ("<all_urls>",)
        else:
            raise TypeError("entries must be resource patterns or {resources, matches}")
        entries.append(MappingProxyType({"resources": resources, "matches": matches}))
    return tuple(entries)


def extension_resource(environment, url, *, source_url=""):
    """The resource *url* names, when this browser's extensions expose it to *source_url*.

    ``None`` is what Chrome also answers when nothing can serve it: the id is unknown,
    the extension is not installed or enabled, the path is not one it exposes, or the
    page asking is not one its ``matches`` names.
    """
    if environment is None:
        return None
    declared = environment.extensions
    if not declared:
        return None
    parts = urlsplit(str(url))
    if parts.scheme.lower() != "chrome-extension":
        return None
    decoded = unquote(parts.path or "/")
    if "\x00" in decoded or "\\" in decoded:
        return None
    path = posixpath.normpath("/" + decoded.lstrip("/")).lstrip("/")
    for entry in declared.get((parts.hostname or "").lower(), ()):
        if source_url and not any(_source_matches(pattern, source_url) for pattern in entry["matches"]):
            continue
        if any(_resource_matches(str(pattern).lstrip("/"), path) for pattern in entry["resources"]):
            return path
    return None


def _resource_matches(pattern, path):
    """Chrome's resource patterns: a literal path, or one whose ``*`` stands for anything."""
    if pattern == path:
        return True
    if "*" not in pattern:
        return False
    parts = pattern.split("*")
    if not path.startswith(parts[0]):
        return False
    at = len(parts[0])
    for part in parts[1:-1]:
        found = path.find(part, at)
        if found < 0:
            return False
        at = found + len(part)
    return path.endswith(parts[-1]) and at <= len(path) - len(parts[-1])


def _source_matches(pattern, url):
    """Whether a page on *url* may read the resource: Chrome's match patterns, coarsely."""
    if pattern in ("<all_urls>", "*://*/*"):
        return True
    origin = origin_tuple(url)
    if origin[0] == "opaque":
        return False
    scheme, host = origin[0], origin[1]
    left, separator, right = str(pattern).partition("://")
    if not separator or left not in ("*", scheme):
        return False
    expected = right.split("/", 1)[0].lower()
    if expected == "*":
        return True
    if expected.startswith("*."):
        return host == expected[2:] or host.endswith(expected[1:])
    return host == expected


def _enum(*allowed):
    def validate(value):
        text = str(value).lower()
        if text not in allowed:
            raise ValueError(f"must be one of {', '.join(allowed)}")
        return text
    return validate


def _brands(value):
    brands = tuple(_brand(entry) for entry in value)
    return brands


def _brand(entry):
    if isinstance(entry, Mapping):
        brand, version = entry.get("brand"), entry.get("version")
    else:
        brand, version = entry
    if brand is None or version is None:
        raise TypeError("entries need both a brand and a version")
    return {"brand": str(brand), "version": str(version)}


def _plugins(value):
    if isinstance(value, Mapping):
        value = (value,)
    plugins = tuple(_plugin(entry) for entry in value)
    if len({plugin["name"] for plugin in plugins}) != len(plugins):
        raise ValueError("must not repeat a plugin name")
    return plugins


def _plugin(entry):
    if not isinstance(entry, Mapping):
        raise TypeError("entries must be mappings")
    mime_types = tuple(_mime_type(mime) for mime in entry.get("mime_types", ()))
    if len({mime["type"] for mime in mime_types}) != len(mime_types):
        raise ValueError("must not repeat a mime type")
    return {
        "name": _text(entry.get("name")),
        "filename": str(entry.get("filename", "")),
        "description": str(entry.get("description", "")),
        "mime_types": mime_types,
    }


def _mime_type(entry):
    return {
        "type": _text(entry.get("type")),
        "suffixes": str(entry.get("suffixes", "")),
        "description": str(entry.get("description", "")),
    }


def _voices(value):
    """The synthesiser's voices, in the order the browser lists them."""
    if value is None:
        return None
    voices = tuple(_voice(entry) for entry in value)
    if len({voice["name"] for voice in voices}) != len(voices):
        raise ValueError("must not repeat a voice name")
    return voices


def _voice(entry):
    if not isinstance(entry, Mapping):
        raise TypeError("entries must be mappings")
    return {
        "name": _text(entry.get("name")),
        "lang": str(entry.get("lang", "")),
        "local_service": bool(entry.get("local_service", False)),
        "default": bool(entry.get("default", False)),
    }



def _json_object(value):
    """A JSON object kept as text in the file, as the dict it holds."""
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return dict(value) if isinstance(value, dict) else None


def _text_mapping(value):
    """``name -> text``: a table a browser reports, such as the permission spec names."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("must be a mapping of names to text")
    return MappingProxyType({str(key): str(item) for key, item in value.items()})


#: The four names a font face carries, and the two places one is measured from.
_FONT_NAMES = ("postscript_name", "full_name", "family", "style")


def _font_records(value):
    """``local()``-visible faces, when a profile carries a machine's font set.

    A page reads four names off a face, so those are what such a profile holds; the file
    and face index are what measuring text needs, and a profile that cannot know them
    leaves them out, which answers for existence and not for metrics.
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes, Mapping)):
        raise TypeError("must be a list of font records")
    records = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise TypeError("every font record must be a mapping")
        names = {name: str(entry.get(name) or "") for name in _FONT_NAMES}
        if not names["postscript_name"] and not names["full_name"]:
            raise ValueError("a font record needs a PostScript name or a full name")
        records.append(FontRecord(
            path=str(entry.get("path") or ""),
            index=_integer(entry.get("index") or 0),
            **names,
        ))
    return tuple(records)


@dataclass(frozen=True)
class Field:
    """One setting: its dotted key, how a given value is checked, whether a browser reports it."""

    name: str
    validate: Callable[[object], object]
    collected: bool = False
    default: object = None


#: The keys a browser reports for itself, plus the simulation-only ones it cannot.
FIELDS: tuple[Field, ...] = (
    Field("user_agent", _text, True),
    Field("app_version", _text, True),
    Field("vendor", _text, True),
    Field("platform", _text, True),
    Field("language", _text, True),
    Field("languages", _tokens, True),
    #: Browser UI strings can differ from navigator.languages.
    Field("form_controls.submit_label", _text, default="Submit"),
    Field("form_controls.reset_label", _text, default="Reset"),
    Field("form_controls.file_button_label", _text, default="Choose File"),
    Field("form_controls.file_empty_label", _text, default="No file chosen"),
    Field("form_controls.time_font_family", _text, default="monospace"),
    Field("on_line", bool, True),
    Field("hardware_concurrency", _integer, True),
    Field("device_memory", _number, True),
    Field("max_touch_points", _integer, True),
    Field("webdriver", bool, True),
    Field("timezone", _text, True),
    Field("viewport.width", _integer, True),
    Field("viewport.height", _integer, True),
    Field("viewport.device_scale_factor", _number, True),
    Field("viewport.mobile", bool, default=False),
    Field("viewport.touch", bool, default=False),
    #: ``outerWidth - innerWidth`` and its vertical counterpart: window decorations, so the
    #: defaults below are what the shipped profile reported before these keys existed.
    Field("viewport.outer_inset_width", _integer, True, default=16),
    Field("viewport.outer_inset_height", _integer, True, default=95),
    Field("viewport.screen_x", _integer, True, default=0),
    Field("viewport.screen_y", _integer, True, default=0),
    Field("screen.width", _integer, True),
    Field("screen.height", _integer, True),
    Field("screen.avail_width", _integer, True),
    Field("screen.avail_height", _integer, True),
    Field("screen.color_depth", _integer, True),
    Field("screen.pixel_depth", _integer, True),
    Field("geolocation.permission", _enum("prompt", "denied", "granted"), default="prompt"),
    Field("geolocation.latitude", _optional_number),
    Field("geolocation.longitude", _optional_number),
    Field("geolocation.accuracy", _optional_number),
    #: Host ICE candidate address. Absent means gathering reports no candidate.
    Field("webrtc.ip", _optional_text),
    Field("connection.effective_type", _enum("slow-2g", "2g", "3g", "4g"), True),
    Field("connection.rtt", _integer, True),
    Field("connection.downlink", _number, True),
    Field("connection.save_data", bool, True),
    Field("connection.type", _enum("bluetooth", "cellular", "ethernet", "mixed", "none",
                                   "other", "unknown", "wifi", "wimax"), default="unknown"),
    Field("connection.downlink_max", _number, default=float("inf")),
    Field("battery.charging", bool, True),
    Field("battery.charging_time", _number, True),
    Field("battery.discharging_time", _number, True),
    Field("battery.level", _number, True),
    Field("storage.temporary.usage", _integer, True),
    Field("storage.temporary.quota", _integer, True),
    Field("storage.persistent.usage", _integer, default=0),
    Field("storage.persistent.quota", _integer, default=0),
    Field("bluetooth.available", bool, default=True),
    Field("client_hints.brands", _brands, True),
    Field("client_hints.full_version_list", _brands, True),
    Field("client_hints.mobile", bool, True),
    Field("client_hints.platform", _text, True),
    Field("client_hints.platform_version", _text, True),
    Field("client_hints.architecture", _text, True),
    Field("client_hints.bitness", _text, True),
    Field("client_hints.wow64", bool, True),
    Field("client_hints.model", _text, True),
    Field("client_hints.form_factors", _tokens, True),
    Field("plugins", _plugins, True),
    Field("webgl.vendor", _text, True),
    Field("webgl.renderer", _text, True),
    Field("keyboard.layout", _keyboard_layout, True),
    Field("media.containers", _media_containers, True),
    Field("media.self_contained", _tokens, True),
    Field("media.hardware_decode", _tokens, True),
    Field("media.webrtc", _named_tokens, True),
    Field("media.hardware_encode", _tokens, True),
    Field("media.devices", _tokens, True),
    #: ``getSupportedConstraints()``: which constraint names this build answers for.
    Field("media.constraints", _tokens, True),
    #: The voices ``speechSynthesis.getVoices()`` lists: installed on the machine, not built in.
    Field("speech.voices", _voices, True),
    Field("permissions.names", _tokens, True),
    Field("permissions.granted", _tokens, True),
    #: The name ``PermissionStatus.name`` reports, which is not the one queried.
    Field("permissions.spec_names", _text_mapping, True),
    Field("gpu.wgsl_features", _tokens, True),
    Field("gpu.adapter", _json_object, True),
    #: The installed extensions a page can read out of: ``id -> declared resources``.
    Field("extensions", _extensions, True),
    #: The faces this profile stands in for; absent means "ask this machine".
    Field("fonts.records", _font_records, default=None),
    #: The browser's configured default family for unstyled document text.
    Field("fonts.default_family", _text, default="sans-serif"),
)

_BY_NAME = {field.name: field for field in FIELDS}

#: The keys a collected profile carries, exactly.
COLLECTED_KEYS: tuple[str, ...] = tuple(field.name for field in FIELDS if field.collected)

_PROFILE = {name: value for name, value in
            json.loads(PROFILE.read_text(encoding="utf-8")).items()
            if name not in ENVELOPE_KEYS}

DEFAULTS = MappingProxyType({
    field.name: field.validate(_PROFILE[field.name]) if field.name in _PROFILE else field.default
    for field in FIELDS
})


@dataclass(frozen=True)
class ViewportView:
    width: int
    height: int
    device_scale_factor: float
    mobile: bool
    touch: bool
    #: The window's own decorations around the viewport, in CSS pixels.
    outer_inset_width: int
    outer_inset_height: int
    screen_x: int
    screen_y: int


@dataclass(frozen=True)
class ScreenView:
    width: int
    height: int
    avail_width: int
    avail_height: int
    color_depth: int
    pixel_depth: int


@dataclass(frozen=True)
class GeolocationView:
    permission: str
    latitude: float | None
    longitude: float | None
    accuracy: float | None


@dataclass(frozen=True)
class ConnectionView:
    effective_type: str
    rtt: int
    downlink: float
    save_data: bool
    type: str
    downlink_max: float


@dataclass(frozen=True)
class BatteryView:
    charging: bool
    charging_time: float
    discharging_time: float
    level: float


@dataclass(frozen=True)
class QuotaView:
    usage: int
    quota: int


@dataclass(frozen=True)
class StorageView:
    temporary: QuotaView
    persistent: QuotaView


@dataclass(frozen=True)
class BluetoothView:
    available: bool


@dataclass(frozen=True)
class Brand:
    brand: str
    version: str


@dataclass(frozen=True)
class ClientHintsView:
    brands: tuple[Brand, ...]
    full_version_list: tuple[Brand, ...]
    mobile: bool
    platform: str
    platform_version: str
    architecture: str
    bitness: str
    wow64: bool
    model: str
    form_factors: tuple[str, ...]


@dataclass(frozen=True)
class MimeTypeView:
    type: str
    suffixes: str
    description: str


@dataclass(frozen=True)
class PluginView:
    name: str
    filename: str
    description: str
    mime_types: tuple[MimeTypeView, ...]


@dataclass(frozen=True)
class WebGLView:
    """What ``WEBGL_debug_renderer_info`` answers: the machine's GL identity."""

    vendor: str
    renderer: str


@dataclass(frozen=True)
class KeyboardView:
    """``navigator.keyboard.getLayoutMap()``: the machine's own layout, empty when
    a profile has not collected one."""

    layout: Mapping[str, str]


@dataclass(frozen=True)
class MediaView:
    """What the reference browser decodes, and which of it in hardware.

    The answers ``isTypeSupported``/``decodingInfo``/``canPlayType`` give are computed from
    these two facts, so they cannot disagree with each other.
    """

    containers: Mapping[str, tuple[str, ...]]
    self_contained: tuple[str, ...]
    hardware_decode: tuple[str, ...]
    #: ``video/VP8`` style content types the WebRTC path carries, per direction.
    webrtc: Mapping[str, tuple[str, ...]]
    hardware_encode: tuple[str, ...]
    #: The capture devices the machine lists, by kind, in the order it lists them.
    devices: tuple[str, ...]
    #: The constraint names ``getSupportedConstraints()`` answers for.
    constraints: tuple[str, ...]


@dataclass(frozen=True)
class SpeechView:
    """The synthesiser's voices: installed on the machine, so a profile is the only source."""

    voices: tuple


@dataclass(frozen=True)
class PermissionsView:
    """What ``navigator.permissions`` answers for, and which of those start granted."""

    names: tuple
    granted: tuple
    spec_names: Mapping[str, str]


@dataclass(frozen=True)
class GpuView:
    """``navigator.gpu`` as the reference browser reports it."""

    wgsl_features: tuple
    #: The adapter ``requestAdapter()`` answers with: ``features``/``limits``/``info``.
    adapter: dict | None = None


@dataclass(frozen=True)
class FontsView:
    """The faces a page can see: the profile's own, or ``None`` for this machine's."""

    records: tuple[FontRecord, ...] | None
    default_family: str

    def resolved(self):
        """The records to answer with, asking this machine when the profile named none."""
        from .system_fonts import records

        return records() if self.records is None else self.records


def _platform_name(user_agent, fallback):
    for token, name in (
        ("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"),
        ("iPad", "iOS"), ("CrOS", "Chrome OS"), ("Macintosh", "macOS"),
        ("Linux", "Linux"),
    ):
        if token in user_agent:
            return name
    return fallback


def _derive(values):
    """The keys that follow the user agent, so a changed one moves them too."""
    user_agent = str(values["user_agent"])
    version = re.search(r"Chrome/(\d+)", user_agent)
    major = version.group(1) if version is not None else ""
    brands = tuple(
        {"brand": str(entry["brand"]),
         "version": major if str(entry["brand"]) in _VERSIONED_BRANDS else str(entry["version"])}
        for entry in values["client_hints.brands"]
    )
    return {
        "app_version": user_agent.removeprefix("Mozilla/"),
        "language": str(tuple(values["languages"])[0]),
        "client_hints.brands": brands,
        "client_hints.mobile": "Mobile" in user_agent,
        "client_hints.platform": _platform_name(user_agent, str(values["client_hints.platform"])),
    }


def _resolved(explicit):
    values = dict(DEFAULTS)
    values.update(explicit)
    values.update(_derive(values))
    values.update(explicit)
    return values


def json_value(value):
    """A non-finite number travels as its own name: ``Infinity`` is not JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        if value != value:
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        # A view is its fields: what ``dumps(resolved=True)`` writes for one.
        return {field.name: json_value(getattr(value, field.name))
                for field in dataclass_fields(value)}
    return value


def _accepted(data):
    """Validate a mapping of dotted keys into the values it sets."""
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise TypeError("environment must be a mapping of dotted keys")
    accepted = {}
    for name, value in data.items():
        if name in ENVELOPE_KEYS:
            if name == "schema" and value != SCHEMA_VERSION:
                raise ValueError(f"environment schema {value!r} is not version {SCHEMA_VERSION}")
            continue
        field = _BY_NAME.get(name)
        if field is None:
            raise TypeError(f"unknown environment key: {name}")
        try:
            accepted[name] = field.validate(value)
        except (TypeError, ValueError) as error:
            raise type(error)(f"{name} {error}") from None
    return accepted


class BrowserEnvironment:
    """One flat ``dotted.key -> value`` configuration, frozen once built.

    ``to_dict()`` gives back only the keys this environment was given; the rest follow
    the shipped profile or the user agent, which ``resolved()`` and the views read.
    """

    __slots__ = (
        "_explicit", "_resolved",
        "viewport", "screen", "geolocation", "connection", "battery", "storage",
        "bluetooth", "client_hints", "plugins", "webgl", "keyboard", "media",
        "speech", "permissions", "gpu", "fonts",
    )

    def __init__(self, data=None, *, base=None):
        explicit = dict(base._explicit) if base is not None else {}
        if isinstance(data, BrowserEnvironment):
            explicit.update(data._explicit)
        else:
            explicit.update(_accepted(data))
        values = _resolved(explicit)
        for kind in ("temporary", "persistent"):
            if values[f"storage.{kind}.usage"] > values[f"storage.{kind}.quota"]:
                raise ValueError(f"storage.{kind}.usage must not exceed storage.{kind}.quota")
        object.__setattr__(self, "_explicit", MappingProxyType(explicit))
        object.__setattr__(self, "_resolved", MappingProxyType(values))
        object.__setattr__(self, "viewport", ViewportView(
            values["viewport.width"], values["viewport.height"],
            values["viewport.device_scale_factor"], values["viewport.mobile"],
            values["viewport.touch"], values["viewport.outer_inset_width"],
            values["viewport.outer_inset_height"], values["viewport.screen_x"],
            values["viewport.screen_y"],
        ))
        object.__setattr__(self, "screen", ScreenView(
            values["screen.width"], values["screen.height"],
            values["screen.avail_width"], values["screen.avail_height"],
            values["screen.color_depth"], values["screen.pixel_depth"],
        ))
        object.__setattr__(self, "geolocation", GeolocationView(
            values["geolocation.permission"], values["geolocation.latitude"],
            values["geolocation.longitude"], values["geolocation.accuracy"],
        ))
        object.__setattr__(self, "connection", ConnectionView(
            values["connection.effective_type"], values["connection.rtt"],
            values["connection.downlink"], values["connection.save_data"],
            values["connection.type"], values["connection.downlink_max"],
        ))
        object.__setattr__(self, "battery", BatteryView(
            values["battery.charging"], values["battery.charging_time"],
            values["battery.discharging_time"], values["battery.level"],
        ))
        object.__setattr__(self, "storage", StorageView(
            QuotaView(values["storage.temporary.usage"], values["storage.temporary.quota"]),
            QuotaView(values["storage.persistent.usage"], values["storage.persistent.quota"]),
        ))
        object.__setattr__(self, "bluetooth", BluetoothView(values["bluetooth.available"]))
        object.__setattr__(self, "client_hints", ClientHintsView(
            tuple(Brand(**entry) for entry in values["client_hints.brands"]),
            tuple(Brand(**entry) for entry in values["client_hints.full_version_list"]),
            values["client_hints.mobile"], values["client_hints.platform"],
            values["client_hints.platform_version"], values["client_hints.architecture"],
            values["client_hints.bitness"], values["client_hints.wow64"],
            values["client_hints.model"], tuple(values["client_hints.form_factors"]),
        ))
        object.__setattr__(self, "plugins", tuple(
            PluginView(entry["name"], entry["filename"], entry["description"],
                       tuple(MimeTypeView(**mime) for mime in entry["mime_types"]))
            for entry in values["plugins"]
        ))
        object.__setattr__(self, "webgl", WebGLView(
            values["webgl.vendor"], values["webgl.renderer"],
        ))
        layout = values["keyboard.layout"]
        object.__setattr__(self, "keyboard", KeyboardView(
            MappingProxyType({}) if layout is None else layout))
        containers = values["media.containers"]
        webrtc = values["media.webrtc"]
        object.__setattr__(self, "media", MediaView(
            MappingProxyType({}) if containers is None else containers,
            tuple(values["media.self_contained"] or ()),
            tuple(values["media.hardware_decode"] or ()),
            MappingProxyType({}) if webrtc is None else webrtc,
            tuple(values["media.hardware_encode"] or ()),
            tuple(values["media.devices"] or ()),
            tuple(values["media.constraints"] or ()),
        ))
        object.__setattr__(self, "speech", SpeechView(
            tuple(values["speech.voices"] or ())))
        object.__setattr__(self, "permissions", PermissionsView(
            tuple(values["permissions.names"] or ()),
            tuple(values["permissions.granted"] or ()),
            MappingProxyType({}) if values["permissions.spec_names"] is None
            else values["permissions.spec_names"],
        ))
        object.__setattr__(self, "gpu", GpuView(
            tuple(values["gpu.wgsl_features"] or ()), values["gpu.adapter"]))
        object.__setattr__(self, "fonts", FontsView(
            values["fonts.records"], values["fonts.default_family"]))

    def __setattr__(self, name, value):
        raise AttributeError("BrowserEnvironment is immutable")

    def __getattr__(self, name):
        """A resolved key is also readable as an attribute."""
        values = object.__getattribute__(self, "_resolved")
        if name in values:
            return values[name]
        raise AttributeError(f"unknown environment key: {name}")

    @classmethod
    def from_dict(cls, data=None, *, base=None):
        """Build from a mapping of dotted keys, an instance, or nothing."""
        if isinstance(data, cls) and base is None:
            return data
        return cls(data, base=base)

    @classmethod
    def loads(cls, text, *, base=None):
        return cls.from_dict(json.loads(text), base=base)

    @classmethod
    def from_file(cls, path, *, base=None):
        return cls.loads(Path(path).read_text(encoding="utf-8"), base=base)

    def overlay(self, data=None):
        """This configuration with *data* applied on top of it."""
        return type(self).from_dict(data, base=self)

    def to_dict(self):
        """Only the keys this environment was given, in table order."""
        return {name: self._explicit[name] for name in _BY_NAME if name in self._explicit}

    def resolved(self):
        """Every key, with the profile and the user agent filled in."""
        return dict(self._resolved)

    def dumps(self, *, resolved=False, source=None):
        document = {"schema": SCHEMA_VERSION}
        if source is not None:
            document["source"] = dict(source)
        values = self.resolved() if resolved else self.to_dict()
        document.update({name: json_value(value) for name, value in values.items()})
        return json.dumps(document, indent=2) + "\n"

    def to_file(self, path, *, resolved=True, source=None):
        Path(path).write_text(self.dumps(resolved=resolved, source=source), encoding="utf-8")

    def __eq__(self, other):
        if not isinstance(other, BrowserEnvironment):
            return NotImplemented
        return self._resolved == other._resolved

    def __repr__(self):
        keys = ", ".join(f"{name}={value!r}" for name, value in self.to_dict().items())
        return f"BrowserEnvironment({keys})"


__all__ = [
    "BrowserEnvironment", "Field", "FIELDS", "SCHEMA_VERSION", "COLLECTED_KEYS", "PROFILE",
    "profile_path",
    "ViewportView", "ScreenView", "GeolocationView", "ConnectionView", "BatteryView",
    "StorageView", "QuotaView", "BluetoothView", "ClientHintsView", "Brand", "PluginView",
    "MimeTypeView", "WebGLView", "KeyboardView", "MediaView", "SpeechView", "PermissionsView",
    "GpuView", "FontsView", "environment_of", "extension_resource", "json_value",
]
