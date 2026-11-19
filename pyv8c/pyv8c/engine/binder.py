"""Turn baseline declarations and hand-written classes into native IdlSpecs.

The one module that knows both sides: it decodes a member's shorthand into the flags the
binding wants, compiles the realm-independent structure once, fills undeclared members
with safe shells, and rebinds only the callbacks that need a realm.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass

from pyv8c import pyv8
from pyv8c.pyv8 import AttributeSpec, ConstantSpec, IdlSpec, MethodSpec
from pyv8c.pyv8.flag import (
    FlagCrossOriginCheck,
    FlagLocation,
    FlagReceiverCheck,
    SideEffectType,
    V8Attribute,
)

from ..foundation.logging import logger
from ..idl import idl_spec, members_of, stated_fields, stated_key, unresolved_members


# (baseline handler key, __v8_*__ method name) per interceptor family.
HANDLER_FAMILIES = (
    (
        "named",
        (
            ("get", "__v8_name_get__"),
            ("set", "__v8_name_set__"),
            ("query", "__v8_name_query__"),
            ("del", "__v8_name_del__"),
            ("enum", "__v8_name_enum__"),
        ),
    ),
    (
        "indexed",
        (
            ("get", "__v8_index_get__"),
            ("set", "__v8_index_set__"),
            ("query", "__v8_index_query__"),
            ("del", "__v8_index_del__"),
            ("enum", "__v8_index_enum__"),
        ),
    ),
)
CROSS_ORIGIN_FAMILIES = (
    (
        "named",
        (
            ("get", "__v8_cross_origin_name_get__"),
            ("set", "__v8_cross_origin_name_set__"),
            ("query", "__v8_cross_origin_name_query__"),
            ("del", "__v8_cross_origin_name_del__"),
            ("enum", "__v8_cross_origin_name_enum__"),
        ),
    ),
    (
        "indexed",
        (
            ("get", "__v8_cross_origin_index_get__"),
            ("set", "__v8_cross_origin_index_set__"),
            ("query", "__v8_cross_origin_index_query__"),
            ("del", "__v8_cross_origin_index_del__"),
            ("enum", "__v8_cross_origin_index_enum__"),
        ),
    ),
)


#: Every named/indexed callback, plain and cross-origin, that may be stubbed when the baseline declares it.
HANDLER_CALLBACKS = frozenset(
    callback for _family, mapping in HANDLER_FAMILIES + CROSS_ORIGIN_FAMILIES
    for _key, callback in mapping
)


# Template fields the baseline states that map straight onto the binding's flags.
TEMPLATE_FLAGS = (
    ("immutable_proto", "immutable"),
    ("undetectable", "undetectable"),
    ("global_this", "global_this"),
    ("unconstructable", "unconstructable"),
)

_LOCATION_FLAGS = {
    "instance": FlagLocation.kInstance,
    "prototype": FlagLocation.kPrototype,
    "interface": FlagLocation.kInterface,
}
_SIDE_EFFECTS = {
    "HasNoEffect": SideEffectType.kHasNoSideEffect,
    "HasNoSideEffect": SideEffectType.kHasNoSideEffect,
    "HasSideEffectToReceiver": SideEffectType.kHasSideEffectToReceiver,
}
_PROPERTY_FLAGS = (
    ("ReadOnly", V8Attribute.ReadOnly),
    ("DontWrite", V8Attribute.ReadOnly),
    ("DontEnum", V8Attribute.DontEnum),
    ("DontDelete", V8Attribute.DontDelete),
)


def property_flags(member) -> int:
    """The property attributes of one member.

    A compact snapshot states the mask; an older one only states the flag text.
    """
    if member.property_flags is not None:
        return member.property_flags
    result = V8Attribute.kNone
    for name, bit in _PROPERTY_FLAGS:
        if name in member.flags:
            result |= bit
    return result


#: Those flags are a pure function of a member's shorthand, and ten thousand members reuse a
#: handful of combinations.  Callers splat the dictionary into a spec, so a shared one is never
#: written to; a caller that starts mutating it would have to copy it here.
_FLAGS_CACHE: dict = {}
_CROSS_ORIGIN_CACHE: dict = {}


def member_flags(member) -> dict:
    """The flags every member spec carries, decoded from the member's shorthand."""
    key = (member.location, member.flags, member.property_flags, member.receiver,
           member.side_effect)
    found = _FLAGS_CACHE.get(key)
    if found is None:
        found = _FLAGS_CACHE[key] = dict(
            location=_LOCATION_FLAGS.get(member.location, FlagLocation.kPrototype),
            property_flags=property_flags(member),
            receiver_check=(FlagReceiverCheck.kDoNotCheck if member.receiver == "DoNotCheck"
                            else FlagReceiverCheck.kCheck),
            side_effect=_SIDE_EFFECTS.get(member.side_effect, SideEffectType.kHasSideEffect),
        )
    return found


def cross_origin_check(member) -> int:
    """Whether one member is guarded by the cross-origin access check."""
    key = member.cross_origin
    found = _CROSS_ORIGIN_CACHE.get(key)
    if found is None:
        found = _CROSS_ORIGIN_CACHE[key] = (
            FlagCrossOriginCheck.kDoNotCheck if key == "DoNotCheck"
            else FlagCrossOriginCheck.kCheck)
    return found


#: ``fn_<member>``/``get_<member>`` (+``set_``): how a class answers a baseline member
#: without declaring it.  Existence, place, arity and flags stay the baseline's.
CONVENTION = {"operation": ("fn", None), "attribute": ("get", "set")}


def apply_template_flags(config, template):
    """The interface-wide facts one baseline template states.

    ``array`` is not one of them: it decides which members exist, so the compiler reads it where
    it installs the iterator.  ``definition_kind`` is a number the binding's own field takes, so
    it travels as it stands.
    """
    for source, target in TEMPLATE_FLAGS:
        if template.get(source):
            config[target] = 1 if target == "immutable" else True
    kind = template.get("definition_kind")
    if kind is not None:
        config["definition_kind"] = kind
    return config


def mark_stub(callback):
    """Mark generated placeholders for Python diagnostics only."""
    callback.__v8_idl_stub__ = True
    return callback


class _Stubs:
    """Build the safe, logging shells used for unimplemented members."""

    #: The level that reports placeholder handlers, which run on ordinary property probes.
    HANDLER_LEVEL = "verbose"

    def __init__(self, stub_logging="off"):
        self.stub_logging = str(stub_logging or "off").lower()
        self.reports_handlers = self.stub_logging == self.HANDLER_LEVEL
        #: A reporting handler speaks once per interface and callback, not per probe.
        self._reported = set()

    def _log(self, message, *args):
        if self.stub_logging != "off":
            logger.info(message, *args)

    def method(self, interface_name, member_name, *, static=False):
        def callback(*args):
            self._log("pyv8c: {}.{}() 待实现", interface_name, member_name)
            return pyv8.Undefined
        callback.__name__ = f"_pyv8c_stub_method_{interface_name}_{member_name}"
        return mark_stub(callback)

    def getter(self, interface_name, member_name):
        def getter(*_args):
            self._log("pyv8c: {}.{} getter 待实现", interface_name, member_name)
            return pyv8.Undefined
        getter.__name__ = f"_pyv8c_stub_get_{interface_name}_{member_name}"
        return mark_stub(getter)

    def setter(self, interface_name, member_name):
        def setter(*args):
            self._log("pyv8c: {}.{} setter 待实现", interface_name, member_name)
        setter.__name__ = f"_pyv8c_stub_set_{interface_name}_{member_name}"
        return mark_stub(setter)

    def constructor(self, interface_name):
        def constructor(_self, *_args, **_kwargs):
            self._log("pyv8c: {} constructor 待实现", interface_name)
        constructor.__name__ = interface_name
        constructor.__v8_idl_stub__ = True
        return constructor

    def call_handler(self, interface_name):
        def call_handler(*_args):
            return pyv8.Undefined
        call_handler.__name__ = f"_pyv8c_stub_call_{interface_name}"
        return mark_stub(call_handler)

    def handler(self, interface_name, method_name):
        def handler(*args):
            reported = (interface_name, method_name)
            if self.reports_handlers and reported not in self._reported:
                self._reported.add(reported)
                self._log("pyv8c: {}.{} 待实现", interface_name, method_name)
            # None tells V8 the interceptor skipped it; enum interceptors report an empty tuple.
            if method_name.endswith("enum__"):
                return ()
            return None
        handler.__name__ = f"_pyv8c_stub_{method_name}_{interface_name}"
        return mark_stub(handler)


def _parameter_names(callback) -> tuple:
    """The parameter names of one callback, read off ``__code__`` where that is the answer.

    Only the wrapper asks, and only for the first name, but it asks once per member of every
    interface.  ``__code__`` is exactly what a signature says unless something has redefined
    the signature -- a binding, a ``__wrapped__`` chain, a stated ``__signature__`` -- and
    those keep the general answer.
    """
    code = getattr(callback, "__code__", None)
    if code is None or hasattr(callback, "__signature__") or hasattr(callback, "__wrapped__"):
        return tuple(inspect.signature(callback).parameters)
    names = code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]
    # A bound method's ``self`` is already bound away, as a signature reports it.
    return names[1:] if inspect.ismethod(callback) else names


#: Parameter names are a pure function of a callback, and the same callbacks answer every
#: interface, so one table answers them all -- the wrapper asks once per member otherwise.
_PARAMETERS: dict = {}


def _parameters_for(callback):
    names = _PARAMETERS.get(callback)
    if names is None:
        names = _PARAMETERS[callback] = _parameter_names(callback)
    return names


class _Callbacks:
    """Bind implementation callbacks directly, injecting a realm only when requested."""

    def __init__(self, bindings):
        self.bindings = bindings

    def method(self, cls, interface_name, public_name, callback_name, *, static, realm=None):
        raw = self._raw_callback(cls, callback_name)
        parameters = _parameters_for(raw)
        inject_realm = static and realm is not None and parameters[:1] == ("_realm",)

        if not inject_realm:
            return raw

        def callback(*args):
            return raw(realm, *args)

        callback.__name__ = f"_pyv8c_binding_{interface_name}_{public_name}"
        return callback

    def setter(self, cls, interface_name, public_name, callback_name):
        return self._raw_callback(cls, callback_name)

    def constructor_adapter(self, interface_name, stub, realm=None):
        implementation = self.bindings.class_for(interface_name)
        inject_realm = "_realm" in _parameters_for(implementation)

        def construct(cls, args, kwargs):
            options = dict(kwargs)
            if realm is not None and inject_realm:
                options["_realm"] = realm
            return cls(*args, **options)

        construct.__name__ = f"_pyv8c_construct_{interface_name}"
        if stub:
            construct.__v8_idl_stub__ = True
        return construct

    @staticmethod
    def find_callback(owner, name):
        """The callable a name resolves to along the MRO, or ``None``.

        What the convention asks: a name nothing defines -- or that a class shadows with
        something that is not callable -- means this class does not implement that member,
        which is what the stub is for.
        """
        for base in owner.__mro__:
            if name in base.__dict__:
                raw = base.__dict__[name]
                raw = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
                if callable(raw):
                    return raw
        return None

    @staticmethod
    def _raw_callback(cls, name):
        if callable(name):
            return name
        for base in cls.__mro__:
            if name in base.__dict__:
                raw = base.__dict__[name]
                return raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
        raise TypeError(f"callback {cls.__name__}.{name} is missing")


@dataclass(frozen=True)
class CompiledInterface:
    spec: IdlSpec
    realm_methods: tuple
    realm_constructor: bool


#: Compiled interfaces, shared by interchangeable bindings (same baseline, profile and
#: classes, no hooks, quiet stubs): the first page builds 10361 member specs, the next one
#: in the process builds none.  A compile that reported a mismatch is not stored, so warn
#: mode still warns once per bindings.
_COMPILED: dict = {}
_COMPILED_LIMIT = 20000


def compile_key(bindings, name, cls):
    """What a compiled interface depends on: the baseline, the profile, the name and the class.

    The profile's part of the key is its content: an ``id()`` would silently depend on the
    surface object outliving the cache, and read as a hit once that address is reused.
    """
    return (getattr(bindings.idl, "digest", ""), bindings.idl.surface_key, name, cls,
            stated_key(cls))


class Binder:
    """Compile one interface bindings's classes, once each."""

    def __init__(self, bindings, *, stub_logging="off", lock=None):
        self.bindings = bindings
        self.stubs = _Stubs(stub_logging)
        self.wrappers = _Callbacks(bindings)
        # ``None`` when nothing is hooked, so the member loop pays a check, not a lookup.
        self.overrides = getattr(bindings, "overrides", None) or None
        self._compiled = {}
        #: How many interfaces this bindings built itself, the rest coming from ``_COMPILED``.
        self.compiled_misses = 0
        # The bindings shares its lock: a compile reaches back into it for classes.
        self._lock = lock or threading.RLock()

    def _fills_stub(self, callback_name, registration):
        """Whether a handler the class does not declare is filled with a stub.

        Every named/indexed callback the baseline declares is filled, so a forgotten
        handler reports itself instead of dropping the interceptor; the cross-origin
        access check is excluded, since a placeholder there would deny guarded reads.
        """
        return callback_name in HANDLER_CALLBACKS or registration is None

    @staticmethod
    def _late_prototype_members(entries):
        before, after = [], []
        for location, member in entries:
            if location != 'prototype' or member.kind not in ('attribute', 'operation') or not member.name:
                continue
            if member.install_phase == 'template_late':
                before.append(member.name)
            elif member.install_phase == 'object':
                after.append(member.name)
        return tuple(dict.fromkeys(before + ['constructor'] + after)) if before or after else ()

    def _members(self, cls, name, parent, field, public_names):
        """What the class and its bases state for one kind of member, nearest base winning."""
        members = {}
        inherited = False
        for base in cls.__mro__:
            inherited = inherited or base is parent
            stated = members_of(base, self.bindings.idl, name)[0 if field == "methods" else 1]
            if not stated:
                continue
            for member in stated:
                if not inherited or member.name in public_names:
                    members.setdefault(member.name, member)
        return members

    def _writer(self, cls, interface_name, public_name, declared, member, setter_prefix):
        """The callback that answers a write, or ``None`` for a member nothing answers.

        The baseline decides whether a member can be written at all.  Where it does not say, a
        declared setter or a convention ``set_<member>`` is what makes it writable; a member that
        answers no write keeps the shell the read-only path always had, so a class that writes
        nothing is not silently made writable.
        """
        if declared is not None:
            writable = member.setter_state
            if writable is None:
                writable = declared.setter is not None
            if not writable:
                return None
            if declared.setter is not None:
                return self.wrappers.setter(cls, interface_name, public_name, declared.setter)
            # A declaration that names no setter still leaves the write to the convention, the
            # same way a member nothing declares does.
            name = f"{setter_prefix}_{public_name}"
            if self.wrappers.find_callback(cls, name) is not None:
                return self.wrappers.setter(cls, interface_name, public_name, name)
            return self.stubs.setter(interface_name, public_name)
        name = f"{setter_prefix}_{public_name}"
        if member.setter_state is not False and self.wrappers.find_callback(cls, name) is not None:
            return self.wrappers.setter(cls, interface_name, public_name, name)
        if member.setter_state:
            return self.stubs.setter(interface_name, public_name)
        return None

    def compile(self, name, context_type="Window"):
        with self._lock:
            cls = self.bindings.class_for(name)
            contextual = tuple(getattr(cls, '__idl_context_members__', {}).get(context_type, ()))
            local_key = (name, contextual) if contextual else name
            cached = self._compiled.get(local_key)
            if cached is not None:
                return cached
            # A hook answers members itself and stub logging keeps its own set: both stay uncached.
            sharing = self.overrides is None and self.bindings.stub_logging == "off"
            key = compile_key(self.bindings, name, cls) + (contextual,) if sharing else None
            compiled = _COMPILED.get(key) if key is not None else None
            if compiled is None:
                before = len(self.bindings.discrepancies)
                compiled = self._compile(name, contextual) if contextual else self._compile(name)
                if key is not None and len(self.bindings.discrepancies) == before:
                    if len(_COMPILED) >= _COMPILED_LIMIT:
                        # Oldest first: a dict keeps its insertion order, and clearing every
                        # entry would make the next page recompile all of them.
                        for stale in list(_COMPILED)[:len(_COMPILED) // 2]:
                            del _COMPILED[stale]
                    _COMPILED[key] = compiled
                    self.compiled_misses += 1
            if getattr(compiled.spec.iteration_source, "__v8_iteration_unimplemented__", False):
                self.bindings.record_unimplemented_iteration_source(name)
            self._compiled[local_key] = compiled
            return compiled

    def _compile(self, name, contextual=()):
        bindings = self.bindings
        cls = bindings.class_for(name)
        interface = bindings.idl.interface(name)
        parent_name = interface.parent
        parent = bindings.class_for(parent_name) if parent_name else object
        implementation = bindings.runtime.interface(name)
        registration = getattr(implementation, "__idl_spec__", None)
        if registration is not None:
            if registration.parent and registration.parent not in bindings.runtime.catalog.interfaces:
                raise TypeError(f"{name} refers to unknown parent {registration.parent}")
            if parent_name is not None and registration.parent != parent_name:
                if not bindings.report_mismatch(
                        f"{name} parent {registration.parent!r} is not {parent_name!r} in the interface baseline"):
                    raise TypeError(f"{name} parent does not match baseline")
            if registration.parent and bindings.class_for(registration.parent) not in cls.__mro__[1:]:
                if not bindings.report_mismatch(f"{name} must explicitly inherit {registration.parent}"):
                    raise TypeError(f"{name} must explicitly inherit {registration.parent}")
        for owner, member_name in unresolved_members(cls, bindings.idl, name):
            # A declared member the document does not carry: an older document or a mistake.
            message = f"{owner}.{member_name} is not present in the interface baseline"
            if not bindings.report_mismatch(message):
                raise TypeError(f"{owner}.{member_name} is not present in Chromium interface baseline")
        source = idl_spec(cls, bindings.idl, name)
        entries = bindings.idl.own_members(name)
        if contextual:
            # A Window observation cannot suppress an operation that the implementation
            # explicitly declares for another context. It must still exist in the IDL.
            present = {member.name for _location, member in entries}
            extra = []
            declared = set(contextual)
            for location in ('instance', 'interface', 'prototype'):
                for member in interface.own_members(location):
                    if member.name in declared:
                        declared.discard(member.name)
                        if member.name not in present:
                            extra.append((location, member))
            if declared:
                raise ValueError(f'{name}: context members are absent from the IDL: {sorted(declared)}')
            entries = entries + tuple(extra)
        public_methods = {member.name for _, member in entries if member.kind == "operation"}
        public_attributes = {member.name for _, member in entries if member.kind == "attribute"}
        declared_methods = self._members(cls, name, parent, "methods", public_methods)
        declared_attributes = self._members(cls, name, parent, "attributes", public_attributes)
        methods = []
        attributes = []
        realm_methods = []
        seen = set()
        overrides = self.overrides
        for _location, member in entries:
            public_name = member.name
            kind = member.kind
            if not public_name or kind not in ("operation", "attribute") or (kind, public_name) in seen:
                continue
            seen.add((kind, public_name))
            if interface.iteration and kind == "operation" and public_name in interface.iteration["methods"]:
                declared_methods.pop(public_name, None)
                continue
            common = member_flags(member)
            prefix, setter_prefix = CONVENTION[kind]
            override = overrides.for_member(name, public_name) if overrides is not None else None
            if override is not None:
                logger.debug("interface override: {}.{} is answered by the host", name, public_name)
            if kind == "operation":
                declared = declared_methods.pop(public_name, None)
                static = member.is_static
                if override is not None and override.call is not None:
                    callback = override.call
                elif declared is not None:
                    raw = self.wrappers._raw_callback(cls, declared.callback)
                    callback = self.wrappers.method(cls, name, public_name, raw, static=static)
                    if static and _parameters_for(raw)[:1] == ("_realm",):
                        realm_methods.append((len(methods), raw))
                else:
                    # ``fn_<member>``: the class implements it, and finding nothing leaves the stub.
                    raw = self.wrappers.find_callback(cls, f"{prefix}_{public_name}")
                    if raw is None:
                        callback = self.stubs.method(name, public_name, static=static)
                    else:
                        callback = self.wrappers.method(cls, name, public_name, raw, static=static)
                        if static and _parameters_for(raw)[:1] == ("_realm",):
                            realm_methods.append((len(methods), raw))
                length = member.length if member.length is not None else (declared.length if declared else 0)
                methods.append(
                    MethodSpec(name=public_name, callback=callback, length=int(length),
                               cross_origin_check=cross_origin_check(member), **common)
                )
            else:
                declared = declared_attributes.pop(public_name, None)
                if override is not None and override.get is not None:
                    getter = override.get
                elif declared is not None:
                    getter = self.wrappers._raw_callback(cls, declared.getter)
                else:
                    # One member at a time, as with operations: ``get_<member>`` or the stub.
                    getter = self.wrappers.find_callback(cls, f"{prefix}_{public_name}")
                    if getter is None:
                        getter = self.stubs.getter(name, public_name)
                setter = None
                if override is not None and override.set is not None:
                    # A hook that answers the write is what makes the member writable.
                    setter = override.set
                else:
                    setter = self._writer(cls, name, public_name, declared, member, setter_prefix)
                crossing = cross_origin_check(member)
                attributes.append(
                    AttributeSpec(name=public_name, getter=getter, setter=setter, replaceable=bool(declared and declared.replaceable), cross_origin_get_check=crossing, cross_origin_set_check=crossing, **common)
                )
        for public_name, declared in declared_methods.items():
            if bindings.idl.member(name, public_name, "operation") is None:
                if bindings.idl.declares(name, public_name, "operation"):
                    # The baseline has it, the browser does not install it.
                    continue
                if not bindings.report_mismatch(
                        f"{name}.{public_name} is not present in the interface baseline"):
                    raise TypeError(f"{name}.{public_name} is not present in Chromium interface baseline")
                continue
            raw = self.wrappers._raw_callback(cls, declared.callback)
            static = declared.location == FlagLocation.kInterface
            callback = self.wrappers.method(cls, name, public_name, raw, static=static)
            if static and _parameters_for(raw)[:1] == ("_realm",):
                realm_methods.append((len(methods), raw))
            methods.append(declared.replace(callback=callback))
        for public_name, declared in declared_attributes.items():
            if bindings.idl.member(name, public_name, "attribute") is None:
                if bindings.idl.declares(name, public_name, "attribute"):
                    continue
                if not bindings.report_mismatch(
                        f"{name}.{public_name} is not present in the interface baseline"):
                    raise TypeError(f"{name}.{public_name} is not present in Chromium interface baseline")
                continue
            getter = self.wrappers._raw_callback(cls, declared.getter)
            setter = None
            if declared.setter is not None:
                setter = self.wrappers.setter(cls, name, public_name, declared.setter)
            attributes.append(declared.replace(getter=getter, setter=setter))
        template = interface.template
        enabled = set()
        handlers = template.get("handlers") or {}
        cross = handlers.get("cross_origin") or {}
        for family, mapping in HANDLER_FAMILIES:
            enabled.update(callback for key, callback in mapping if key in (handlers.get(family) or {}))
        for family, mapping in CROSS_ORIGIN_FAMILIES:
            enabled.update(callback for key, callback in mapping if key in (cross.get(family) or {}))
        if template.get("access_check") or cross.get("named") or cross.get("indexed"):
            enabled.add("__v8_cross_origin_check__")
        handler_names = [
            callback for _, mapping in HANDLER_FAMILIES + CROSS_ORIGIN_FAMILIES for _, callback in mapping
        ] + ["__v8_cross_origin_check__"]
        options = {}
        declarations = tuple(stated_fields(base) for base in cls.__mro__)
        for fields in declarations:
            if 'named_properties_non_masking' in fields:
                options['named_properties_non_masking'] = fields['named_properties_non_masking']
                break
        for callback_name in handler_names:
            key = callback_name[5:-2]
            reference = next(
                (fields[key] for fields in declarations if fields.get(key)), None)
            if reference is not None:
                options[key] = self.wrappers._raw_callback(cls, reference)
            elif callback_name in enabled and self._fills_stub(callback_name, registration):
                options[key] = self.stubs.handler(name, callback_name)
        constants = {entry.name: entry for entry in source.constants_value}
        #: Constants come from the baseline alone: harvesting plain ``int`` class attributes put
        #: implementation state (``AudioNode.inputs``, ``ScriptProcessorNode.channel_count``) on
        #: 46 interface objects, where Chrome lets a subclass inherit ``Node.ELEMENT_NODE``.
        for constant in interface.constants:
            if constant.get("resolved") and isinstance(constant.get("value"), int):
                constants[constant["name"]] = ConstantSpec(name=constant["name"], value=constant["value"])
        for _location, member in entries:
            if member.kind == "constant" and member.value is not None:
                constants[member.name] = ConstantSpec(name=member.name, value=member.value)
        options.update(
            methods=tuple(methods),
            attributes=tuple(attributes),
            constants_value=tuple(constants.values()),
            prototype_late_members=self._late_prototype_members(entries),
            to_string_tag=interface.tag or source.to_string_tag or name,
            #: Exposure belongs to the baseline, as ``FeatureImplementation`` says: a class that
            #: stated ``has_constructor=0`` used to lose ``X.prototype.constructor``, so the page
            #: read ``Object`` there and the constructor arity with it.
            has_constructor=1 if interface.has_interface_object else 0,
        )
        if parent_name is not None:
            # A parent that is not an interface has no template: the binding reads the Python base.
            options["parent_class"] = parent
        if source.call is not None:
            options["call"] = self.wrappers._raw_callback(cls, source.call)
        elif any("__call__" in base.__dict__ for base in cls.__mro__):
            options["call"] = self.wrappers._raw_callback(cls, "__call__")
        elif template.get("call_handler"):
            options["call"] = self.stubs.call_handler(name)
        apply_template_flags(options, template)
        #: ``definition_kind`` is the document's word for which interface object this is: Blink
        #: builds a namespace from an ``ObjectTemplate`` (``SetupIDLNamespaceTemplate``) and gives
        #: a callback interface no prototype (``SetupIDLCallbackInterfaceTemplate``).

        iteration = interface.iteration
        if iteration:
            options["iteration_kind"] = {"indexed": 1, "pair": 2, "set": 3}[iteration["kind"]]
            if iteration["kind"] == "indexed":
                options["array_methods"] = interface.iteration_mask
            else:
                options["iteration_methods"] = interface.iteration_mask
                callback = self.wrappers.find_callback(cls, "__v8_iteration_source__")
                if callback is None:
                    def callback(*_args, _name=name, _stubs=self.stubs):
                        _stubs._log("pyv8c: {} iteration source 待实现", _name)
                        raise NotImplementedError(f"{_name} iteration source is not implemented")
                    callback.__v8_iteration_unimplemented__ = True
                options["iteration_source"] = callback
        if interface.constructor_stated and not interface.constructor_new:
            #: An interface object with no constructor has length 0, whatever a stated arity
            #: says: 548 of 548 in the reference reading, which is where SnapEvent and
            #: FencedFrameConfig were the last two arity differences.
            options["length"] = 0
        elif interface.constructor_length is not None:
            options["length"] = interface.constructor_length
        if interface.constructor_stated:
            options["constructor_behavior"] = (1 if interface.constructor_new else 0) | (
                2 if interface.constructor_call else 0
            )
        realm_constructor = False
        if options.get("constructor_behavior", source.constructor_behavior):
            options["constructor_adapter"] = self.wrappers.constructor_adapter(name, registration is None)
            realm_constructor = "_realm" in _parameters_for(cls)
        return CompiledInterface(source.replace(**options), tuple(realm_methods), realm_constructor)

    def bind(self, name, realm=None, context_type="Window"):
        compiled = self.compile(name, context_type)
        if realm is None or not (compiled.realm_methods or compiled.realm_constructor):
            return compiled.spec
        cls = self.bindings.class_for(name)
        changes = {}
        if compiled.realm_methods:
            methods = list(compiled.spec.methods)
            for index, raw in compiled.realm_methods:
                item = methods[index]
                callback = self.wrappers.method(cls, name, item.name, raw, static=True, realm=realm)
                methods[index] = item.replace(callback=callback)
            changes["methods"] = tuple(methods)
        if compiled.realm_constructor:
            changes["constructor_adapter"] = self.wrappers.constructor_adapter(
                name, self.bindings.runtime.interface(name) is None, realm
            )
        return compiled.spec.replace(**changes)
