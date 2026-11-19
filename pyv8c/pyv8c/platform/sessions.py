"""One realm's binding state: the classes it materialised, and the specs behind them.

The native realm asks its ``lazy_factory`` about a name it has no template for, and the
answer carries the specs that name needs -- staged before the template that reads them.
"""

from __future__ import annotations

from types import MappingProxyType

from pyv8c.pyv8 import IdlSpec


class BindingSession:
    """V8Context-local registration state for one V8 realm."""

    def __init__(self, bindings, realm=None):
        self.bindings = bindings
        self.realm = realm
        self._configs: dict[type, IdlSpec] = {}
        self._materialized: set[str] = set()
        #: Legacy Window aliases: the name Blink installs, and the interface it names.
        self._aliases: dict[str, str] = {}
        self._context_type = 'Window'

    def detach(self):
        """Let go of everything this realm built, so a closed realm keeps nothing."""
        self._configs.clear()
        self._materialized.clear()
        self.realm = None

    def interface_names(self, context_type="Window"):
        return self.bindings.plan(context_type).installs

    def class_for(self, name):
        return self.bindings.class_for(name)

    @property
    def materialized_interfaces(self):
        return tuple(sorted(self._materialized))

    @property
    def configs(self):
        """Read-only view of the specs this realm materialized."""
        return MappingProxyType(self._configs)

    def prepare(self, context_type="Window"):
        """Stage the interfaces a realm builds before its first script runs.

        The plan calls it staged, so this keeps that work off the first script's path.
        """
        self._context_type = context_type
        self._stage(self.bindings.plan(context_type).eager)

    def _config(self, name, realm):
        if self._context_type == 'Window':
            return self.bindings.class_config_for(name, realm)
        return self.bindings.class_config_for(name, realm, self._context_type)

    def materialize(self, name):
        """Stage what *name* needs, and answer the class behind it."""
        self._stage((name,))
        return self.class_for(name)

    def class_config_for(self, name):
        self.materialize(name)
        return self._configs[self.class_for(name)]

    def add_extra_class(self, name, cls):
        """Register a class this realm answers under a name the baseline does not describe."""
        if getattr(self, "_extras", None) is None:
            self._extras = {}
        self._extras[str(name)] = cls

    def add_alias(self, alias, target):
        """Answer *alias* with the interface *target*, specs and all.

        A legacy alias is the interface's constructor under another name, not a
        class of its own; answering with a bare class would leave the realm
        without the specs the target's template needs.
        """
        self._aliases[str(alias)] = str(target)

    def lazy_factory(self, context_type="Window", on_materialize=None):
        """The realm's one source of specs, in the shape the native V8Context calls.

        A name it cannot describe answers ``None``, which the native side remembers, so a
        refusal costs one call; a name it can answers the class plus its chain's specs.
        ``on_materialize`` hears each name the realm built something for.
        """

        def answer(name):
            name = self._aliases.get(str(name), str(name))
            extra = getattr(self, "_extras", {}).get(name)
            if extra is not None:
                if on_materialize is not None:
                    on_materialize(name)
                return extra
            if not self.bindings.describes(name):
                return None
            cls = self.class_for(name)
            chain = self._stage((name,))
            if on_materialize is not None:
                on_materialize(name)
            # Each ancestor has its own spec. A plain child may inherit a realm-bound
            # constructor or method, so sharing the child's decision is insufficient.
            specs = {interface_name: self._configs[self.class_for(interface_name)]
                     for interface_name in chain}
            plain = {}
            for interface_name, realm_spec in specs.items():
                plain_spec = self._config(interface_name, None)
                plain[interface_name] = (realm_spec is plain_spec
                                         and getattr(plain_spec, "iteration_source", None) is None)
            # py_class_new inherits parent_class or the Python class's last base.
            # Follow that exact chain: a plain child cannot retain a realm-bound
            # parent template, including an internal base absent from the IDL.
            names_by_class = {self.class_for(interface_name): interface_name
                              for interface_name in specs}
            shared = {}

            def can_share(interface_name):
                if interface_name not in shared:
                    cls = self.class_for(interface_name)
                    parent = specs[interface_name].parent_class or cls.__bases__[-1]
                    parent_name = names_by_class.get(parent)
                    shared[interface_name] = (plain[interface_name]
                                              and (parent is object or
                                                   (parent_name is not None and can_share(parent_name))))
                return shared[interface_name]

            for interface_name in specs:
                can_share(interface_name)
            return (cls,
                    specs,
                    shared)

        answer.__name__ = "__pyv8c_lazy_interface"
        return answer

    def _stage(self, names):
        """Stage the specs for *names* and their ancestors, innermost first.

        Answers the chain it covered: an overlay has to reach the native realm before the
        template that reads it, however long ago the chain was staged.
        """
        staged = {}
        chain = []
        materialized = set()
        for name in names:
            for group_name in self.bindings.runtime.interface_group(name):
                for interface_name in reversed(tuple(self.bindings.idl.ancestors(group_name))):
                    cls = self.class_for(interface_name)
                    if cls not in self._configs and cls not in staged:
                        staged[cls] = self._config(interface_name, self.realm)
                    chain.append(interface_name)
                    materialized.add(interface_name)
        self._configs.update(staged)
        self._materialized.update(materialized)
        return chain
