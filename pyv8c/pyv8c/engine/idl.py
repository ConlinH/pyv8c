"""One IDL collection: its document, the profile that narrows it, and the lookups over both.

A document is decoded once per file version and shared between Browsers; each interface is
decoded once per collection into what a realm asks about it (parent chain, members, interface
object, exposure) and a realm is planned from those answers.  The shipped document is a
compressed archive, expanded into a sidecar beside itself and cached in a binary form on first
use.  Nothing here knows the native binding, which keeps it importable by the offline extractor.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import pickle
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

from ..foundation.logging import logger

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
#: The one artifact this build reads: a compressed document (a single JSON member) written by the
#: offline extractor.  The sidecar ``idl.json`` and the ``.pyv8ccache`` beside it are caches this
#: reader writes on first use, and ``.gitignore`` keeps both out of a release.
DEFAULT_IDL_PATH = PACKAGE_ROOT / "idl" / "idl.zip"
_HOST_IDLS = {
    "win32": DEFAULT_IDL_PATH,
    "darwin": DEFAULT_IDL_PATH.with_name("idl-158.0.8127.36.zip"),
}
#: The profile a collection run writes; the surface is a section of it.
PROFILE_PATH = PACKAGE_ROOT / "data" / "chrome-environment.json"


def idl_path(platform=None):
    """The packaged IDL for *platform*, or this host when omitted.

    ``Idl.load`` with no path still reads the baseline zip. A ``Browser`` pairs the
    packaged Mac profile with the darwin archive when ``idl_path`` is omitted;
    every other ``Browser`` reads the baseline zip. An unknown platform, or a
    host file that is not packaged, is that same baseline.
    """
    name = sys.platform if platform is None else platform
    chosen = _HOST_IDLS.get(name, DEFAULT_IDL_PATH)
    if chosen.is_file():
        return chosen
    return DEFAULT_IDL_PATH


def _document_label(target):
    """A path inside this package stays ``pyv8c:``; anything else is absolute."""
    if target.is_relative_to(PACKAGE_ROOT):
        return f"pyv8c:{target.relative_to(PACKAGE_ROOT).as_posix()}"
    return str(target)


#: What an interface borrows from ``Array.prototype``, as the bits ``IdlSpec.array`` installs.
#: ``pyv8.ArrayProto`` names the same two; reading a document must not need the native extension,
#: so they are restated here and a guard test keeps the two spellings equal.
class ArrayProto:
    iterator = 1
    iterable = 31


#: Which interface object an IDL definition builds, as ``IdlSpec.definition_kind`` states it.
#: The names are Blink's (``WrapperTypeInfo``) and ``pyv8.DefinitionKind`` names the same three;
#: ``kInterface`` is what a document that states nothing means.
class DefinitionKind:
    kInterface = 0
    kIdlNamespace = 1
    kWrapperTypeNoPrototype = 2


#: The values the two fields above accept, and what a message may name.
ARRAY_PROTOS = (ArrayProto.iterator, ArrayProto.iterable)
DEFINITION_KINDS = (DefinitionKind.kInterface, DefinitionKind.kIdlNamespace,
                    DefinitionKind.kWrapperTypeNoPrototype)


#: The version of the document this build reads and writes; another is refused by name rather
#: than read half-way.  Regenerate with ``tools/extract_idl.py``.
IDL_VERSION = 10


#: The exposure labels the IDL words worker realms with.
_WORKER_LABELS = ("Worker", "DedicatedWorker", "SharedWorker")

#: ``Idl.load(profile=...)``: the shipped profile, a given one, or ``None`` for no narrowing.
_SHIPPED = object()


@dataclass(frozen=True)
class Profile:
    """What this reference browser installs, measured against one IDL document.

    A profile only narrows: a member it records as omitted is not installed, a name it does not
    carry is not exposed, and an array value it does not state borrows nothing.  A profile
    measured against another document cannot narrow this one; its independent Window
    order observation can still guide the installation order.
    """

    browser: str
    #: The single observation supplies both installation order and exposure.
    own_order: tuple[str, ...] = ()
    #: ``interface -> prototype members the document declares and this browser omits``.
    omitted_prototype: Mapping[str, frozenset] = MappingProxyType({})
    #: The same, for members the document puts on the interface object itself.
    omitted_interface: Mapping[str, frozenset] = MappingProxyType({})
    #: ``interface -> the bits this release publishes``; nothing recorded borrows nothing.
    iteration_methods: Mapping[str, int] = MappingProxyType({})
    #: The document those omissions were measured against; empty when the writer did not say.
    against: str = ""
    def __post_init__(self):
        order = self.own_order
        if (not isinstance(order, (list, tuple)) or
                any(not isinstance(name, str) or not name for name in order) or
                len(set(order)) != len(order)):
            raise ValueError("bindings.own_order must be a list of unique non-empty strings")
        object.__setattr__(self, "own_order", tuple(order))

    @cached_property
    def exposed(self):
        """The membership view of the ordered Window observation."""
        return frozenset(self.own_order)

    @classmethod
    def load(cls, path=None) -> "Profile | None":
        """The profile a collection run wrote, or ``None``, read once per file version."""
        target = Path(path) if path is not None else PROFILE_PATH
        try:
            stat = target.stat()
        except OSError:
            return None
        stamp = (str(target), stat.st_mtime_ns, stat.st_size)
        if stamp not in _PROFILES:
            _PROFILES[stamp] = _read_profile(target)
        return _PROFILES[stamp]

    def exposes(self, name) -> bool:
        """Whether this browser carries one Window name at all."""
        return name in self.exposed

    def omits(self, interface, location, name) -> bool:
        """Whether this browser leaves one member of one interface out."""
        if location == "prototype":
            return name in (self.omitted_prototype.get(interface) or ())
        if location == "interface":
            return name in (self.omitted_interface.get(interface) or ())
        return False

    @cached_property
    def content_key(self):
        """A hashable identity for this profile: its content, not its object."""
        return (self.browser, self.own_order,
                _name_groups(self.omitted_prototype), _name_groups(self.omitted_interface),
                tuple(sorted(self.iteration_methods.items())))


def _name_groups(mapping) -> tuple:
    """``interface -> names`` as a hashable, order-independent pair of pairs."""
    return tuple(sorted((str(name), tuple(sorted(names))) for name, names in mapping.items()))


def _omitted(omits, place) -> Mapping:
    """``interface -> omitted names`` for one place; a malformed entry keeps it whole."""
    found = {}
    for name, names in ((omits or {}).get(place) or {}).items():
        if isinstance(names, list) and names:
            found[str(name)] = frozenset(str(entry) for entry in names)
    return MappingProxyType(found)


def _iteration_methods(section) -> Mapping:
    """``interface -> bits`` from the profile; only the two the field states are kept."""
    found = {}
    for name, bits in ((section or {}).get("iteration_methods") or {}).items():
        if type(bits) is int and 0 <= bits <= 31:
            found[str(name)] = bits
    return MappingProxyType(found)


_PROFILES: dict = {}


def _read_profile(target) -> "Profile | None":
    """One read of the profile file, as :meth:`Profile.load` wants; ``None`` when it says nothing."""
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    section = payload.get("bindings") if isinstance(payload, Mapping) else None
    if not section:
        return None
    if not isinstance(section, Mapping):
        raise ValueError("bindings must be an object")
    from ..foundation.environment import SCHEMA_VERSION
    if isinstance(payload, Mapping) and "schema" in payload and payload["schema"] != SCHEMA_VERSION:
        raise ValueError(f"environment schema {payload['schema']!r} is not version {SCHEMA_VERSION}")
    if "exposed" in section:
        raise ValueError("bindings.exposed is no longer supported; use bindings.own_order")
    order = section.get("own_order")
    if not isinstance(order, list) or not order:
        raise ValueError("bindings.own_order must be a non-empty list of unique names")
    omits = (section or {}).get("omits")
    return Profile(
        browser=str((payload.get("source") or {}).get("browser") or ""),
        own_order=order,
        omitted_prototype=_omitted(omits, "prototype"),
        omitted_interface=_omitted(omits, "interface"),
        iteration_methods=_iteration_methods(section),
        against=str((section or {}).get("against") or ""),
    )


def _report_stale_profile(profile, digest) -> None:
    """Say once that a profile belongs to another document, so nobody wonders why it is ignored."""
    if digest[:12] in _REPORTED:
        return
    _REPORTED.add(digest[:12])
    logger.warning("pyv8c: the profile was measured against IDL {} but this build reads {} --"
                   " its IDL-dependent surface is ignored; its Window order remains available",
                   (profile.against or "?")[:12], digest[:12])


_REPORTED: set = set()


def write_idl(output_path: Path, document: dict, *, compress: bool = False) -> Path:
    """Write the :data:`IDL_VERSION` document; these bytes are the ones the digest covers.

    A ``.zip`` path (or *compress*) writes those same bytes as the single member
    ``<archive stem>.json``, which is the artifact a release ships; the reader expands a
    sidecar copy of it beside the archive on first use.
    """
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    output_path = Path(output_path)
    if not (compress or output_path.suffix.lower() == ".zip"):
        output_path.write_bytes(payload)
        return output_path
    archive_path = output_path if output_path.suffix.lower() == ".zip" else output_path.with_suffix(".zip")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(archive_path.with_suffix(".json").name, payload)
    return archive_path


#: The binary cache header.  A cached payload is only read back for byte-identical source that
#: already passed every check in :func:`_read_document`; the magic follows the payload shape, and
#: anything unreadable is rebuilt from the document rather than reported.
_CACHE_MAGIC = b"pyv8c-idl/1"
_CACHE_SUFFIX = ".pyv8ccache"

#: Decoded documents, keyed by the stat that identifies one file version.
_DOCUMENTS: dict = {}
#: The views built from them, keyed by that document and the profile in force.
_VIEWS: dict = {}
#: Sidecars this process could not write, so the warning is said once per file.
_REPORTED_SIDECAR: set = set()


def _read_cache(path, digest):
    """``(metadata, interfaces)`` from the binary cache, or ``None``."""
    try:
        with open(path, "rb") as handle:
            if handle.read(len(_CACHE_MAGIC)) != _CACHE_MAGIC:
                return None
            header = pickle.load(handle)
            if not isinstance(header, dict) or header.get("digest") != digest:
                return None
            if header.get("version") != IDL_VERSION:
                return None
            payload = pickle.load(handle)
    except Exception:
        return None
    if not isinstance(payload, tuple) or len(payload) != 2:
        return None
    metadata, interfaces = payload
    if not isinstance(metadata, dict) or not isinstance(interfaces, dict):
        return None
    return metadata, interfaces


def _replace_sidecar(path, write):
    """Publish a complete sidecar atomically; concurrent readers never see a prefix."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            write(handle)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _write_cache(path, digest, metadata, interfaces) -> bool:
    """Best-effort cache write: a startup never depends on it."""
    def write(handle):
        handle.write(_CACHE_MAGIC)
        pickle.dump({"digest": digest, "version": IDL_VERSION}, handle,
                    protocol=pickle.HIGHEST_PROTOCOL)
        pickle.dump((metadata, interfaces), handle, protocol=pickle.HIGHEST_PROTOCOL)
    try:
        _replace_sidecar(path, write)
    except Exception:
        return False
    return True


def _payload(target, cache_target, stat, cache_stat) -> bytes:
    """The JSON bytes of one document: a plain file, or the one member of an archive.

    An archive expands once beside itself, and every later startup reads that sidecar instead of
    unzipping; the two are compared by modification time, so an archive written after its sidecar
    wins.
    """
    if target.suffix.lower() != ".zip":
        return target.read_bytes()
    if cache_stat is not None and cache_stat.st_mtime_ns >= stat.st_mtime_ns:
        return cache_target.read_bytes()
    with zipfile.ZipFile(target) as archive:
        members = [info for info in archive.infolist()
                   if not info.is_dir() and Path(info.filename).suffix.lower() == ".json"]
        if len(members) != 1:
            raise ValueError(f"the IDL archive must contain exactly one JSON member: {target}")
        member = Path(members[0].filename)
        if member.is_absolute() or ".." in member.parts:
            raise ValueError(f"the IDL archive contains an unsafe member path: {members[0].filename}")
        raw = archive.read(members[0])
    try:
        _replace_sidecar(cache_target, lambda handle: handle.write(raw))
    except OSError as exc:
        _report_unwritable_sidecar(cache_target, exc)
    return raw


def _report_unwritable_sidecar(target, exc) -> None:
    """Say once that an archive cannot expand beside itself, so nobody wonders at the cost."""
    if str(target) in _REPORTED_SIDECAR:
        return
    _REPORTED_SIDECAR.add(str(target))
    logger.warning(f"pyv8c: cannot write the IDL sidecar {target} ({exc}) -- the archive is read"
                   " and unzipped on every startup; run tools/extract_idl.py where it can be"
                   " written")


def _read_file(target, label) -> tuple:
    """Return one document, decoded once per file version."""
    suffix = target.suffix.lower()
    if suffix not in (".zip", ".json"):
        raise ValueError(f"the IDL document must be a .zip or a .json file: {target}")
    cache_target = target.with_suffix(".json") if suffix == ".zip" else target
    stat = target.stat()
    cache_stat = cache_target.stat() if cache_target.is_file() else None
    stamp = (str(target), stat.st_size, stat.st_mtime_ns,
             cache_stat.st_size if cache_stat else -1,
             cache_stat.st_mtime_ns if cache_stat else -1)
    cached = _DOCUMENTS.get(stamp)
    if cached is not None:
        timings = {"idl_read_ms": 0.0, "idl_decode_ms": 0.0, "idl_cached": True,
                   "idl_binary_cache": False}
        return cached, timings
    started = time.perf_counter_ns()
    raw = _payload(target, cache_target, stat, cache_stat)
    decoded = time.perf_counter_ns()
    digest = sha256(raw).hexdigest()
    binary = target.with_suffix(_CACHE_SUFFIX)
    payload = _read_cache(binary, digest) if binary.is_file() else None
    if payload is not None:
        # Byte-identical source that already passed every check: skip the parse and the checks.
        metadata, interfaces = payload
        parsed = time.perf_counter_ns()
    else:
        try:
            document = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError(f"the IDL document must be UTF-8 JSON: {target}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid IDL JSON: {target}") from exc
        metadata, interfaces = _read_document(document)
        parsed = time.perf_counter_ns()
        _write_cache(binary, digest, metadata, interfaces)
    # Never mutated, and a top-level read-only view avoids copying the 2.6 MB document.
    found = (label, digest, MappingProxyType(metadata), MappingProxyType(interfaces))
    _DOCUMENTS.clear()
    _DOCUMENTS[stamp] = found
    timings = {
        "idl_read_ms": (decoded - started) / 1_000_000,
        "idl_decode_ms": (parsed - decoded) / 1_000_000,
        "idl_cached": False,
        "idl_binary_cache": payload is not None,
    }
    return found, timings


def _read_document(document) -> tuple:
    """Decode the metadata and interfaces, or reject invalid data."""
    if not isinstance(document, dict):
        raise ValueError("the IDL document root must be an object")
    metadata = document.get("idl")
    interfaces = document.get("interfaces")
    if not isinstance(metadata, dict) or not isinstance(interfaces, dict):
        raise ValueError("the IDL document must state an 'idl' object and an 'interfaces' object")
    version = metadata.get("version")
    if version != IDL_VERSION:
        raise ValueError(f"unsupported IDL version {version!r}; expected {IDL_VERSION} --"
                         " regenerate it with tools/extract_idl.py")
    if not interfaces:
        raise ValueError("the IDL document must contain interfaces")
    for name, spec in interfaces.items():
        if not isinstance(spec, dict):
            raise ValueError(f"invalid interface entry {name!r} in the IDL document")
        parent = spec.get("parent")
        if parent is not None and (not isinstance(parent, str) or parent not in interfaces):
            raise ValueError(f"interface {name!r} refers to unknown parent {parent!r}")
        iteration = spec.get("iteration")
        if iteration is not None:
            if (not isinstance(iteration, dict) or iteration.get("kind") not in ("indexed", "pair", "set") or
                iteration.get("default") != ("entries" if iteration.get("kind") == "pair" else "values") or
                not isinstance(iteration.get("methods"), list) or
                any(method not in ("entries", "keys", "values", "forEach") for method in iteration["methods"]) or
                not isinstance(iteration.get("iterator"), bool)):
                raise ValueError(f"invalid iteration metadata for {name!r}")
        if "array" in (spec.get("template") or {}):
            raise ValueError(f"legacy array metadata for {name!r}; regenerate the IDL")
        for entries in (spec.get('members') or {}).values():
            for member in entries:
                if member.get('install_phase', 'template') not in ('template', 'template_late', 'object'):
                    raise ValueError(f"invalid member installation phase for {name!r}.{member.get('name')}")
        kind = (spec.get("template") or {}).get("definition_kind")
        if kind is not None and kind not in DEFINITION_KINDS:
            raise ValueError(f"interface {name!r} states definition_kind={kind!r}; this version"
                             f" states one of {DEFINITION_KINDS} or nothing -- regenerate it"
                             " (tools/extract_idl.py)")
    visiting, visited = set(), set()

    def visit(name):
        if name in visiting:
            raise ValueError(f"interface parent cycle detected at {name!r}")
        if name in visited:
            return
        visiting.add(name)
        parent = interfaces[name].get("parent")
        if parent is not None:
            visit(parent)
        visiting.remove(name)
        visited.add(name)

    for name in interfaces:
        visit(name)
    return metadata, interfaces

LOCATIONS = ("instance", "prototype", "interface")


@dataclass(frozen=True)
class IdlMember:
    """One declared member, as the document states it.

    ``flags``, ``receiver``, ``cross_origin`` and ``side_effect`` keep the shorthand the
    document writes; what they stand for is decided where the binding is built.
    ``main_world`` is the one world fact a runtime document keeps. ``install_phase``
    distinguishes initial template, later template and context-dependent members.
    """

    name: str
    kind: str
    location: str
    flags: str
    property_flags: int | None
    receiver: str
    cross_origin: str
    side_effect: str
    length: int | None
    value: int | None
    #: ``True``/``False`` when the document states the setter, ``None`` when it is silent.
    setter_state: bool | None
    main_world: bool
    install_phase: str

    @property
    def is_static(self) -> bool:
        return self.location == "interface"


def _member(location, entry) -> IdlMember:
    length = entry.get("length")
    stated_flags = entry.get("property_flags")
    return IdlMember(
        name=str(entry.get("name") or ""),
        kind=str(entry.get("kind") or ""),
        location=location,
        flags=str(entry.get("flags") or ""),
        property_flags=stated_flags if isinstance(stated_flags, int) else None,
        receiver=str(entry.get("receiver") or ""),
        cross_origin=str(entry.get("cross_origin") or ""),
        side_effect=str(entry.get("side_effect") or ""),
        length=int(length) if isinstance(length, int) else None,
        value=(entry.get("value") if isinstance(entry.get("value"), int) else None),
        setter_state=(bool(entry["setter"]) if "setter" in entry else None),
        # A snapshot that predates the bit states no world name at all, which is itself
        # the main world: the omission meant exactly that.
        main_world=bool(entry.get("main_world", True)),
        install_phase=str(entry.get("install_phase") or "template"),
    )


@dataclass(frozen=True)
class IdlInterface:
    """One entry of a collection, with what the runtime asks for computed once."""

    name: str
    parent: str | None
    tag: str
    exposed_in: frozenset
    has_interface_object: bool
    template: Mapping
    members: Mapping
    constants: tuple
    #: The constructor's declared shape; ``constructor_stated`` is False when the
    #: document said nothing, which leaves the hand-written declaration in charge.
    constructor_length: int | None
    constructor_new: bool
    constructor_call: bool
    constructor_stated: bool
    #: The bits in force: this release's measured ones, else the document's.  ``None``: nothing.
    iteration: Mapping | None
    iteration_mask: int
    raw: Mapping

    def own_members(self, location) -> tuple:
        return self.members.get(location, ())


class Idl:
    """One IDL collection: the document, the profile that narrows it, and its lookups.

    A document is decoded once per file version, and one view answers every Browser that reads
    that document through the same profile, so a second Browser reuses the whole lookup face.
    """

    def __init__(self, path, digest, metadata, interfaces, profile=None, order_profile=None):
        self.path = path
        self.digest = digest
        self.metadata = metadata
        self.interfaces = interfaces
        order_profile = order_profile if order_profile is not None else profile
        if profile is not None and profile.against and profile.against != digest:
            # IDL-dependent exposure and omissions do not apply to another document.
            _report_stale_profile(profile, digest)
            profile = None
        self.profile = profile
        self.order_profile = order_profile
        self.observed_window_order = (order_profile.own_order or None) if order_profile is not None else None
        self._iteration_methods = profile.iteration_methods if profile is not None else MappingProxyType({})
        self._interfaces: dict[str, IdlInterface] = {}
        self._positions: dict[str, int] | None = None
        self._children: dict[str, tuple] | None = None
        self._descendants: dict[str, tuple] = {}
        self._ancestors: dict[str, tuple] = {}
        self._members: dict[tuple, IdlMember | None] = {}
        #: ``interface -> (kind, name) -> IdlMember``: what :meth:`_own_member_index` answers without
        #: rebuilding the member list and re-sorting it on every walk up the parent chain.
        self._member_index: dict[str, dict] = {}
        self._descendant_members: dict[str, dict] = {}
        self._declared: dict[tuple, bool] = {}
        self._exposed: dict[tuple, tuple] = {}
        self._aliases: dict[str, str] | None = None

    @classmethod
    def load(cls, path=None, profile=_SHIPPED, *, timings=None) -> "Idl":
        """The document at *path*, narrowed by *profile*: the shipped one unless told otherwise."""
        if path is None:
            target = DEFAULT_IDL_PATH
        else:
            target = Path(path).expanduser().resolve()
        label = _document_label(target)
        if not target.is_file():
            raise FileNotFoundError(f"the IDL document does not exist: {target}")
        (label, digest, metadata, interfaces), load_timings = _read_file(target, label)
        if timings is not None:
            timings.update(load_timings)
        chosen = Profile.load() if profile is _SHIPPED else profile
        observation = chosen
        if chosen is not None and chosen.against and chosen.against != digest:
            _report_stale_profile(chosen, digest)
            chosen = None
        key = (digest, chosen.content_key if chosen is not None else None,
               observation.content_key if observation is not None else None)

        view = _VIEWS.get(key)
        if view is None:
            view = cls(label, digest, metadata, interfaces, chosen, observation)
            _VIEWS.clear()
            _VIEWS[key] = view
        return view

    @property
    def info(self):
        """What a Browser reports about the document it read: its metadata and its size."""
        metadata = dict(self.metadata)
        return {**metadata, "idl": metadata, "path": self.path, "sha256": self.digest,
                "window_order_source": ("environment" if self.observed_window_order is not None
                                        else "fallback"),
                "interface_count": len(self.interfaces)}

    @cached_property
    def surface_key(self):
        """A hashable identity for the profile in force: its content, not its object.

        A compiled specification is keyed on this, so profiles that agree in content share
        their specs and two profiles that differ in one name never do.
        """
        return (self.profile.content_key if self.profile is not None else None,
                self.order_profile.content_key if self.order_profile is not None else None)

    def names(self) -> tuple:
        """Every interface name, in document order."""
        return tuple(self.interfaces)

    def raw(self, name):
        return self.interfaces[name]

    def interface(self, name) -> IdlInterface:
        cached = self._interfaces.get(name)
        if cached is None:
            cached = self._build(name, self.interfaces[name])
            self._interfaces[name] = cached
        return cached

    def _iteration_mask(self, name, entry):
        """The bits in force: the measured release first, then the document's.

        The profile applies only when it was taken against this document, as the omissions do.
        """
        iteration = entry.get("iteration")
        if not iteration: return 0
        bits = {"entries": 2, "keys": 4, "values": 8, "forEach": 16}
        declared = (1 if iteration["iterator"] else 0) | sum(bits[method] for method in set(iteration["methods"]))
        measured = self._iteration_methods.get(name)
        return declared & measured if measured is not None else declared

    def _build(self, name, entry) -> IdlInterface:
        members = entry.get("members") or {}
        constructor = entry.get("constructor") or {}
        length = constructor.get("length")
        return IdlInterface(
            name=name,
            parent=entry.get("parent"),
            tag=str(entry.get("tag") or ""),
            exposed_in=frozenset(entry.get("exposed_in") or ()),
            has_interface_object=bool(constructor.get("exposed")),
            template=entry.get("template") or {},
            members=MappingProxyType({
                location: tuple(_member(location, item) for item in (members.get(location) or ()))
                for location in LOCATIONS
            }),
            constants=tuple(entry.get("constants") or ()),
            constructor_length=int(length) if isinstance(length, int) else None,
            constructor_new=bool(constructor.get("allow_new")),
            constructor_call=bool(constructor.get("allow_call")),
            constructor_stated=("allow_new" in constructor or "allow_call" in constructor),
            iteration=MappingProxyType(entry["iteration"]) if entry.get("iteration") else None,
            iteration_mask=self._iteration_mask(name, entry),
            raw=entry,
        )

    def ancestors(self, name) -> tuple:
        """The interface and its ancestors, innermost first."""
        cached = self._ancestors.get(name)
        if cached is None:
            chain, current, seen = [], name, set()
            while current and current not in seen:
                seen.add(current)
                chain.append(current)
                current = self.interfaces[current].get("parent")
            cached = tuple(chain)
            self._ancestors[name] = cached
        return cached

    def children(self, name) -> tuple:
        if self._children is None:
            index = {}
            for candidate, entry in self.interfaces.items():
                parent = entry.get("parent")
                if parent:
                    index.setdefault(parent, []).append(candidate)
            self._children = {key: tuple(value) for key, value in index.items()}
        return self._children.get(name, ())

    def descendants(self, name) -> tuple:
        cached = self._descendants.get(name)
        if cached is None:
            seen, pending = set(), list(self.children(name))
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                pending.extend(self.children(current))
            positions = self.positions()
            cached = tuple(sorted(seen, key=positions.__getitem__))
            self._descendants[name] = cached
        return cached

    def positions(self) -> dict:
        if self._positions is None:
            self._positions = {name: index for index, name in enumerate(self.interfaces)}
        return self._positions

    def own_members(self, name) -> tuple:
        """``(location, member)`` pairs declared directly on one interface, profile applied."""
        interface = self.interface(name)
        members = tuple(
            (location, member)
            for location in LOCATIONS
            for member in interface.own_members(location)
        )
        if self.profile is None:
            return members
        if name == "Window":
            # The window's own names cover every location it carries.
            return tuple(pair for pair in members if self.profile.exposes(pair[1].name))
        # Per location: a name this browser omits from the prototype says nothing about the
        # interface object, which is why the omission is asked for one location at a time.
        return tuple(pair for pair in members
                     if not self.profile.omits(name, pair[0], pair[1].name))

    def member(self, name, public_name, kind) -> IdlMember | None:
        """The member an interface answers for a public name, inherited or not."""
        key = (name, public_name, kind)
        if key in self._members:
            return self._members[key]
        found = None
        for current in self.ancestors(name):
            match = self._own_member_index(current).get((kind, public_name))
            if match is not None:
                found = match
                break
        if found is None and not self._omits_anywhere(name, public_name):
            # A descendant can carry a WorkerGlobalScope member, not one this browser omits.
            index = self._descendant_members.get(name)
            if index is None:
                index = {}
                for candidate in self.descendants(name):
                    for member_key, member in self._own_member_index(candidate).items():
                        index.setdefault(member_key, member)
                self._descendant_members[name] = index
            found = index.get((kind, public_name))
        self._members[key] = found
        return found

    def _omits_anywhere(self, name, public_name) -> bool:
        """Whether the profile leaves one public name out of any location of an interface."""
        if self.profile is None:
            return False
        return any(self.profile.omits(name, location, public_name) for location in LOCATIONS)

    def _own_member_index(self, name) -> dict:
        index = self._member_index.get(name)
        if index is None:
            index = {}
            for _location, member in self.own_members(name):
                held = index.get((member.kind, member.name))
                # The stable sort this replaces put the main-world declaration first and
                # otherwise kept the first one.
                if held is None or (member.main_world and not held.main_world):
                    index[(member.kind, member.name)] = member
            self._member_index[name] = index
        return index

    def declares(self, name, public_name, kind) -> bool:
        """Whether the document declares this member anywhere near the interface."""
        key = (name, public_name, kind)
        found = self._declared.get(key)
        if found is None:
            found = any(
                member.name == public_name and member.kind == kind
                for current in (*self.ancestors(name), *self.descendants(name))
                for location in LOCATIONS
                for member in self.interface(current).own_members(location)
            )
            self._declared[key] = found
        return found

    def installs(self, context_type, name) -> bool:
        """Whether the reference browser installs this Window name at all."""
        return self.profile is None or context_type != "Window" or self.profile.exposes(name)

    def window_aliases(self) -> dict:
        """``alias -> interface`` for the Window aliases the reference browser installs."""
        cached = self._aliases
        if cached is None:
            exposed = set(self.exposed("Window"))
            cached = {
                alias: name
                for name, entry in self.interfaces.items()
                if name in exposed
                for alias in (entry.get("aliases") or ())
                if self.installs("Window", alias) and alias not in exposed
            }
            self._aliases = cached
        return cached

    def exposed(self, context_type) -> tuple:
        """Names this document exposes for *context_type*, in document order."""
        cached = self._exposed.get(context_type)
        if cached is None:
            # One realm type serves every worker kind, so it answers to all the labels;
            # otherwise a worker misses what only ever says ``DedicatedWorker``.
            labels = _WORKER_LABELS if context_type == "Worker" else (context_type,)
            cached = tuple(
                name for name, entry in self.interfaces.items()
                if any(label in (entry.get("exposed_in") or ()) for label in labels)
                and self.installs(context_type, name)
            )
            self._exposed[context_type] = cached
        return cached


@dataclass(frozen=True)
class RealmPlan:
    """What one realm class installs, and what of it is built before its first script runs."""

    context_type: str
    #: Every name the realm answers, installed on the global and built on first read.
    installs: tuple[str, ...]
    #: Built before the first script runs: the skeleton, always a subset of ``installs``.
    eager: tuple[str, ...]
    #: Built by the time the realm is ready: the eager set, its ancestors and the alias targets.
    ready: tuple[str, ...] = ()

    @property
    def lazy(self) -> tuple[str, ...]:
        """The names left for a first read: everything the realm answers and has not built."""
        eager = set(self.eager)
        return tuple(name for name in self.installs if name not in eager)


def build_plan(idl, context_type, *, implements=(), expose_unimplemented=True, eager=()) -> RealmPlan:
    """Plan one realm class from a collection.

    The names a realm installs and the skeleton it builds first are one object, so a realm and
    its statistics read the same plan. Window order is resolved by its realm once
    the environment, interface members and host globals are available.
    """
    installs = tuple(sorted(
        name for name in idl.exposed(context_type)
        if expose_unimplemented or name in implements
    ))
    return RealmPlan(
        context_type=context_type,
        installs=installs,
        eager=tuple(name for name in installs if name in set(eager) or name == "Window"),
    )
