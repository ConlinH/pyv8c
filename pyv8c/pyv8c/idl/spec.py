"""What a class states about itself, and how that becomes a native interface description.

A class answers members by convention -- ``fn_<name>`` for an operation, ``get_<name>`` and
``set_<name>`` for an attribute -- and takes place, arity, flags and writability from the IDL
document.  A class states only what the document cannot decide::

    @interface("AbortSignal", "EventTarget")
    class AbortSignal(EventTarget):
        __idl_overrides__ = {"abort": {"callback": "fn_static_abort"}}

Interface-level answers -- the interceptor hooks a collection needs, for one -- go under the
``"interface"`` key of that same mapping::

    __idl_overrides__ = {"interface": {"name_get": "__v8_name_get__"}}

This is the only module that knows the native ``IdlSpec``/``MethodSpec``/``AttributeSpec``
builders, and the only place an override is checked: an unknown member, an unknown field, or a
field the member's kind cannot answer is refused here, before a page ever sees it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
from weakref import WeakKeyDictionary

from pyv8c.pyv8 import AttributeSpec, IdlSpec, MethodSpec
from pyv8c.pyv8.flag import FlagLocation

__all__ = ["INTERFACE_FIELDS", "InterfaceSpec", "idl_spec", "members_of",
           "standalone", "stated_fields", "stated_key", "unresolved_members"]



#: What a class may state about the interface itself, keyed as the native builder spells them.
INTERFACE_FIELDS = (
    "array_methods", "iteration_kind", "iteration_methods", "call", "code_like", "constants_value", "constructor_adapter",
    "constructor_behavior", "cross_origin_check", "cross_origin_index_del",
    "cross_origin_index_enum", "cross_origin_index_get", "cross_origin_index_query",
    "cross_origin_index_set", "cross_origin_name_del", "cross_origin_name_enum",
    "cross_origin_name_get", "cross_origin_name_query", "cross_origin_name_set",
    "definition_kind", "has_constructor",
    "hidden", "immutable", "index_del", "index_enum", "index_get", "index_query", "index_set",
    "iteration_source", "length", "name", "name_del", "name_enum", "name_get", "name_query", "name_set",
    "named_properties_object", "named_properties_non_masking", "parent_class", "shares_parent_prototype",
    "skip_inherit", "to_string_tag", "undetectable",
    "unconstructable", "global_this",
)

#: The convention prefix each kind of member answers to when nothing is stated for it.
_PREFIX = {"operation": "fn", "attribute": "get"}
#: The fields each kind of member can answer.
_FIELDS = {
    "operation": ("callback", "length", "side_effect", "location", "property_flags",
                  "receiver_check", "cross_origin_check"),
    "attribute": ("getter", "setter", "length", "side_effect", "location", "property_flags",
                  "receiver_check", "cross_origin_get_check", "cross_origin_set_check"),
}

#: The property attribute each word stands for, spelled as the native flag type spells them.
PROPERTY_WORDS = {"read_only": 1, "dont_enum": 2, "dont_delete": 4}
#: The two answers a check can give, spelled out so a class does not import the native enums.
CHECK_WORDS = {"check": 0, "do_not_check": 1}


@dataclass(frozen=True)
class InterfaceSpec:
    """What a class declares about its interface: its name, and the IDL document's parent.

    Realm exposure is deliberately not declared here: the document owns that field, so a
    second copy could only drift out of step with it.
    """

    name: str
    parent: str | None


def interface(name, parent=None):
    """Declare one class's interface name and IDL parent."""

    def decorate(cls):
        cls.__idl_spec__ = InterfaceSpec(name, parent)
        return cls

    return decorate


def stated_fields(cls) -> dict:
    """The interface-level answers one class states, empty when it states none."""
    return _interface_fields(cls)


def stated_key(cls):
    """A hashable identity for what a class states, so an edited class is a different key.

    Remembered per class: a class of an event target states a hundred and thirty-five slots, and
    every interface that inherits it asks for this key again.  A replaced mapping is a new
    answer, which is what the identity check catches.
    """
    stated = cls.__dict__.get("__idl_overrides__")
    found = _STATED.get(cls)
    if found is not None and found[0] is stated:
        return found[1]
    key = () if stated is None else _freeze(stated)
    _STATED[cls] = (stated, key)
    return key


def _freeze(value):
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(item) for item in value)
    if callable(value) or isinstance(value, type):
        return repr(value)      # a hook lives on a class, which the key already carries
    return value


def _interface_fields(cls) -> dict:
    stated = cls.__dict__.get("__idl_overrides__") or {}
    if "interface" not in stated:
        return {}
    fields = dict(stated["interface"] or {})
    _refuse(cls, "interface", fields, INTERFACE_FIELDS, "the interface")
    return fields


def _refuse(cls, member, stated, allowed, what) -> None:
    unknown = sorted(set(stated) - set(allowed))
    if unknown:
        raise TypeError(f"{cls.__name__} states {', '.join(unknown)} for {what};"
                        f" {member} answers {', '.join(allowed)}")


def _kind(idl, interface_name, member_name):
    """Which kind of member one override is for, as the document states it.

    None when the document does not carry the member at all: whether that is a mistake or an
    older document is the bindings' call to make, so the answer goes to them (see
    :func:`unresolved_members`).
    """
    for kind in ("operation", "attribute"):
        if idl.member(interface_name, member_name, kind) is not None:
            return kind
    return None


def _identity(cls, interface_name):
    """The interface a class's own declarations are about.

    A class that declares ``@interface`` states members of *that* interface, wherever it is
    mixed in -- ``EventTarget``'s handler slots are about event targets, not about every class
    that inherits them.  A mixin states nothing of its own, so it is judged against whichever
    interface is being compiled.
    """
    declaration = getattr(cls, "__idl_spec__", None)
    return getattr(declaration, "name", None) or interface_name


def _member_specs(cls, idl, interface_name) -> tuple:
    """``(methods, attributes)`` built from one class's own member overrides."""
    methods, attributes = [], []
    own = _identity(cls, interface_name)
    for member_name, stated in (_member_statements(cls)).items():
        kind = _kind(idl, own, member_name)
        if kind is None:
            continue
        fields = dict(stated or {})
        _refuse(cls, member_name, fields, _FIELDS[kind], f"{own}.{member_name}")
        member = idl.member(own, member_name, kind)
        length = fields.pop("length", None)
        side_effect = fields.pop("side_effect", None)
        location = _location(fields.pop("location", None) or member.location)
        flags = _stated_flags(cls, member_name, fields)
        if kind == "operation":
            methods.append(MethodSpec(
                name=member_name, callback=fields.get("callback", f"fn_{member_name}"),
                length=int(length if length is not None else (member.length or 0)),
                location=location,
                **({"side_effect": int(side_effect)} if side_effect is not None else {}),
                **flags))
        else:
            attributes.append(AttributeSpec(
                name=member_name, getter=fields.get("getter", f"get_{member_name}"),
                setter=fields.get("setter"), location=location,
                **({"side_effect": int(side_effect)} if side_effect is not None else {}),
                **flags))
    return tuple(methods), tuple(attributes)


def _stated_flags(cls, member_name, fields) -> dict:
    """The flag keywords a class states for one member, in the words this surface takes."""
    stated = {}
    if "property_flags" in fields:
        stated["property_flags"] = _property_flags(cls, member_name, fields.pop("property_flags"))
    for key in ("receiver_check", "cross_origin_check", "cross_origin_get_check",
                "cross_origin_set_check"):
        if key in fields:
            stated[key] = _check(cls, member_name, key, fields.pop(key))
    return stated


def _property_flags(cls, member_name, value) -> int:
    """The mask a class states, either as the number itself or as words."""
    if isinstance(value, int):
        return int(value)
    words = (value,) if isinstance(value, str) else tuple(value)
    mask = 0
    for word in words:
        if word not in PROPERTY_WORDS:
            raise TypeError(f"{cls.__name__}.{member_name} states property flag {word!r};"
                            f" the words are {', '.join(sorted(PROPERTY_WORDS))}")
        mask |= PROPERTY_WORDS[word]
    return mask


def _check(cls, member_name, key, value) -> int:
    if isinstance(value, int):
        return int(value)
    if value not in CHECK_WORDS:
        raise TypeError(f"{cls.__name__}.{member_name} states {key}={value!r};"
                        f" the words are {', '.join(sorted(CHECK_WORDS))}")
    return CHECK_WORDS[value]


def _member_statements(cls) -> Mapping:
    stated = cls.__dict__.get("__idl_overrides__") or {}
    if "members" in stated or "interface" in stated:
        return stated.get("members") or {}
    return stated


def _location(location) -> int:
    return {"instance": FlagLocation.kInstance, "prototype": FlagLocation.kPrototype,
            "interface": FlagLocation.kInterface}.get(location, FlagLocation.kPrototype)


#: Declarations belong to one document/profile view, without keeping that view alive.
#: A registered base's declarations describe its own interface, not each descendant.
@dataclass
class _Declarations:
    specs: dict = field(default_factory=dict)
    members: dict = field(default_factory=dict)
    unresolved: dict = field(default_factory=dict)


_DECLARATIONS = WeakKeyDictionary()
_STATED: dict = {}


def _declarations(idl):
    found = _DECLARATIONS.get(idl)
    if found is None:
        found = _Declarations()
        _DECLARATIONS[idl] = found
    return found


def idl_spec(cls, idl, interface_name) -> IdlSpec:
    """The native description of *cls*: its own interface-level overrides and member specs."""
    key = (cls, _identity(cls, interface_name), stated_key(cls))
    cache = _declarations(idl).specs
    found = cache.get(key)
    if found is None:
        methods, attributes = members_of(cls, idl, interface_name)
        found = cache[key] = IdlSpec(methods=methods, attributes=attributes,
                                   **_interface_fields(cls))
    return found


def members_of(cls, idl, interface_name) -> tuple:
    """``(methods, attributes)`` one class states for *interface_name*, in one pass."""
    key = (cls, _identity(cls, interface_name), stated_key(cls))
    cache = _declarations(idl).members
    found = cache.get(key)
    if found is None:
        found = cache[key] = _member_specs(cls, idl, interface_name)
    return found


def standalone(cls) -> type:
    """Describe a class the document does not carry, from what the class states itself.

    Such a class has no IDL interface to lean on, so it states every member it has and the
    kind comes from the body it writes: ``fn_<member>`` is an operation, ``get_<member>`` and
    ``set_<member>`` are an attribute.  The description goes onto the class as the native
    layer reads it, which is the only way an object with no interface name gets one.
    """
    methods, attributes = [], []
    for member_name, stated in _member_statements(cls).items():
        fields = dict(stated or {})
        kind = _stated_kind(cls, member_name, fields)
        _refuse(cls, member_name, fields, _FIELDS[kind], f"{cls.__name__}.{member_name}")
        flags = _stated_flags(cls, member_name, fields)
        common = {}
        if "length" in fields:
            common["length"] = int(fields.pop("length"))
        if "side_effect" in fields:
            common["side_effect"] = int(fields.pop("side_effect"))
        if "location" in fields:
            common["location"] = _location(fields.pop("location"))
        if kind == "operation":
            methods.append(MethodSpec(name=member_name,
                                      callback=fields.get("callback", f"fn_{member_name}"),
                                      **common, **flags))
        else:
            attributes.append(AttributeSpec(name=member_name,
                                            getter=fields.get("getter", f"get_{member_name}"),
                                            setter=fields.get("setter"), **common, **flags))
    cls.__v8_idl_spec__ = IdlSpec(methods=tuple(methods), attributes=tuple(attributes),
                                  **_interface_fields(cls))
    return cls


def unresolved_members(cls, idl, interface_name) -> tuple:
    """``(interface, member)`` for what *cls* and its bases state and the document does not carry.

    They are reported rather than installed: a member nothing can resolve is either a typo or
    an older document, and which one it is decides whether a page may run at all.  The pair
    names the interface the declaration belongs to, so one declaration is one report however
    many interfaces end up compiling the class that states it.
    """
    names = {}
    for base in cls.__mro__:
        for pair in _unresolved_in(base, idl, interface_name):
            names.setdefault(pair, None)
    return tuple(names)


def _unresolved_in(base, idl, interface_name) -> tuple:
    """What one class of the chain states and the document does not carry, answered once."""
    own = _identity(base, interface_name)
    key = (base, own, stated_key(base))
    cache = _declarations(idl).unresolved
    found = cache.get(key)
    if found is None:
        found = cache[key] = tuple(
            (own, member_name) for member_name in _member_statements(base)
            if _kind(idl, own, member_name) is None)
    return found


def _stated_kind(cls, member_name, fields) -> str:
    """Which kind a member of a document-less class is: what the class answers it with."""
    if "callback" in fields:
        return "operation"
    if "getter" in fields or "setter" in fields:
        return "attribute"
    if any(f"fn_{member_name}" in base.__dict__ for base in cls.__mro__):
        return "operation"
    if any(f"get_{member_name}" in base.__dict__ or f"set_{member_name}" in base.__dict__
           for base in cls.__mro__):
        return "attribute"
    raise TypeError(
        f"{cls.__name__}.{member_name} states nothing that answers it: write"
        f" ``fn_{member_name}`` for an operation, or ``get_{member_name}``/``set_{member_name}``"
        " for an attribute")
