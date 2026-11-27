"""DOM collection views: live node lists and token lists.

They read through owner-supplied getters rather than the node model, which keeps
them importable by :mod:`dom.nodes` without a cycle.
"""
from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import MISSING, boolean, dom_string, integer, interface_value, is_nullish, missing_error
from ..page_errors import InvalidCharacterError, PageTypeError, dom_exception
from .mixins import LegacyNamedPropertyMixin, ReadOnlyIndexedPropertyMixin
from .utils import OBJECT_PROTOTYPE_NAMES, XHTML_NAMESPACE, camel_name, dash_name


#: The link types Blink implements, from ``rel_list.cc``: what ``relList.supports`` answers.
LINK_REL_TOKENS = frozenset((
    "preload", "preconnect", "dns-prefetch", "stylesheet", "icon", "alternate",
    "prefetch", "prerender", "next", "manifest", "apple-touch-icon",
    "apple-touch-icon-precomposed", "canonical", "modulepreload", "allowed-alt-sxg",
    "compression-dictionary",
))


#: The same for ``<a>``, ``<area>``, ``<form>`` and the SVG anchor.
HYPERLINK_REL_TOKENS = frozenset(("noreferrer", "noopener", "opener"))


def tag_name_ns_collection(target, interface, namespace, local_name):
    """The collection a ``getElementsByTagNameNS`` call answers with.

    ``Document`` and ``Element`` run the same algorithm; only the name they
    report in a missing-argument error differs.
    """
    if namespace is MISSING:
        raise missing_error(interface, 'getElementsByTagNameNS', 2, 0, constructor=False)
    if local_name is MISSING:
        raise missing_error(interface, 'getElementsByTagNameNS', 2, 1, constructor=False)
    if not is_nullish(namespace):
        namespace = dom_string(namespace)
    local_name = dom_string(local_name)
    if namespace not in (None, pyv8.Null, pyv8.Undefined, "", "*", XHTML_NAMESPACE):
        return HTMLCollection(owner=target, getter=lambda: [])
    return target.fn_getElementsByTagName(local_name)


class _ListItems:
    """The body of a live list: an owner-backed ``item(i)`` and its indexed access.

    It is a mixin rather than a base an interface inherits, because the *interface* graph is not
    the implementation graph: ``HTMLCollection`` and ``NamedNodeMap`` are **not** ``NodeList``
    subclasses (their baseline parent is ``None``, measured against Chrome 154 on 2026-10-07),
    and what an interface publishes through ``IdlSpec(iteration_kind=..., array_methods=...)`` is per interface.  Inheriting
    ``NodeList`` carried its iterator flags into both, so their prototypes answered
    ``entries``/``keys``/``values``/``forEach`` like a ``NodeList`` when Chrome publishes only
    ``Symbol.iterator`` there (``examples/5s/script/iterable_probe.py``).  ``RadioNodeList``
    really is a ``NodeList``, so it keeps that parent.
    """

    def __init__(self, owner=None, getter=None, values=None, **_):
        self._owner, self._getter, self._values = owner, getter, values
    def _items(self):
        items = self._getter() if self._getter else (self._values or ())
        return items if hasattr(items, "__len__") and hasattr(items, "__getitem__") else tuple(items)
    def _indexed_items(self): return self._items()
    def get_length(self): return len(self._items())
    def fn_item(self, index=MISSING, *_extra):
        if index is MISSING:
            #: The receiver names itself: Chrome says ``HTMLCollection.item``, not ``NodeList``.
            raise missing_error(type(self).__name__, 'item', 1, 0, constructor=False)
        index = integer(index)
        return self._item_at(index)
    def __iter__(self): return iter(self._items())
    def __getitem__(self, index): return self._items()[index]
    def __len__(self): return len(self._items())


@interface('NodeList', None)
class NodeList(_ListItems, ReadOnlyIndexedPropertyMixin):
    """A live view from a getter or a static snapshot from supplied values."""


@interface('NamedNodeMap', None)
class NamedNodeMap(_ListItems, ReadOnlyIndexedPropertyMixin):
    """Live attribute collection owned by an Element.

    A sibling of ``NodeList``, not a subclass: see :class:`_ListItems`.
    """
    __idl_overrides__ = {
        "interface": {
            "length": 0,
            "name_get": "__v8_name_get__",
            "name_query": "__v8_name_query__"
        }
    }
    def __init__(self, element=None, **_):
        self._element = element
        super().__init__(getter=self._attribute_items)
    def __v8_name_get__(self, name):
        # A named read: the attribute of that name, and nothing (undefined) when the
        # element has none, which is what Chrome answers for an absent name.
        return next((item for item in self._attribute_items() if item.get_name() == str(name)),
                    None)
    def __v8_name_query__(self, name):
        return 0 if any(item.get_name() == str(name) for item in self._attribute_items()) else None
    def _attribute_items(self):
        from .nodes import Attr
        cache = self._element.__dict__.setdefault('_attribute_nodes', {})
        items = []
        for name, value in self._element.attributes.items():
            node = cache.get(name)
            if node is None:
                node = cache[name] = Attr(name, value, ownerElement=self._element,
                                         services=self._element._services)
            items.append(node)
        return items
    def fn_getNamedItem(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('NamedNodeMap', 'getNamedItem', 1, 0, constructor=False)
        name = dom_string(name)
        return next((item for item in self._items() if item.get_name() == str(name)), pyv8.Null)
    def fn_setNamedItem(self, attr=MISSING, *_extra):
        if attr is MISSING:
            raise missing_error('NamedNodeMap', 'setNamedItem', 1, 0, constructor=False)
        attr = interface_value(attr, 'Attr', 'NamedNodeMap', 'setNamedItem', 1, nullable=False)
        from .nodes import Attr
        if not isinstance(attr, Attr): raise PageTypeError("parameter is not of type Attr")
        old = self.fn_getNamedItem(attr.get_name())
        if old is attr: return old
        if attr.ownerElement is not None and attr.ownerElement is not self._element:
            from ..page_errors import dom_exception
            raise dom_exception('InUseAttributeError', 'The attribute is already in use.')
        cache = self._element.__dict__.setdefault('_attribute_nodes', {})
        cache.pop(attr.get_name(), None)
        if old is not pyv8.Null: old.ownerElement = None
        self._element.attributes[attr.get_name()] = attr.get_value()
        attr.ownerElement = self._element
        cache[attr.get_name()] = attr
        return old
    def fn_removeNamedItem(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('NamedNodeMap', 'removeNamedItem', 1, 0, constructor=False)
        name = dom_string(name)
        from ..page_errors import NotFoundError
        name = str(name)
        if name not in self._element.attributes: raise NotFoundError("The attribute was not found.")
        old = self.fn_getNamedItem(name)
        self._element.attributes.pop(name)
        return old


def _node_id(node):
    """The ``id`` of any node-like object, without importing the node model."""
    getter = getattr(node, "get_id", None)
    value = getter() if callable(getter) else ""
    return str(value) if value else ""


def _all_node_name(node):
    tag = str(getattr(node, "localName", "")).lower()
    if tag not in {"a", "button", "embed", "form", "frame", "frameset",
                   "iframe", "img", "input", "map", "meta", "object",
                   "select", "textarea"}:
        return ""
    return str(getattr(node, "attributes", {}).get("name") or "")


def _collection_node_name(node):
    """The HTML ``name`` attribute used by HTMLCollection named lookup."""
    return str(getattr(node, "attributes", {}).get("name") or "")


def _supported_names(items, name_getter):
    """Return unique IDs and names in tree order, as required by Web IDL."""
    names = []
    seen = set()
    for node in items:
        for value in (_node_id(node), name_getter(node)):
            if value and value not in seen:
                seen.add(value)
                names.append(value)
    return names


@interface('HTMLCollection', None)
class HTMLCollection(_ListItems, LegacyNamedPropertyMixin, ReadOnlyIndexedPropertyMixin):
    """``document.forms`` and friends: a live list that also answers by name.

    A sibling of ``NodeList``, not a subclass: see :class:`_ListItems`.  What it publishes
    through ``array`` is the iterator alone, so its two subclasses inherit that too.
    """
    __idl_overrides__ = {
        "interface": {
            "length": 0,
            "name_del": "__v8_name_del__",
            "name_enum": "__v8_name_enum__",
            "name_get": "__v8_name_get__",
            "name_query": "__v8_name_query__",
            "name_set": "__v8_name_set__"
        }
    }
    def fn_namedItem(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('HTMLCollection', 'namedItem', 1, 0, constructor=False)
        name = dom_string(name)
        value = str(name)
        if not value:
            return pyv8.Null
        return next((n for n in self._items()
                     if _node_id(n) == value or _collection_node_name(n) == value), pyv8.Null)

    def _supported_names(self):
        return _supported_names(self._items(), _collection_node_name)

    def _named_item(self, name):
        return self.fn_namedItem(name)


def _array_index_name(value):
    """Return a canonical array index, or ``None`` for a normal property name."""
    if not value or any(ch < "0" or ch > "9" for ch in value) or (len(value) > 1 and value[0] == "0"):
        return None
    try:
        index = int(value)
    except ValueError:
        return None
    return index if index < 2 ** 32 - 1 else None


# ``HTMLAllCollection`` is declared ``[LegacyUnenumerableNamedProperties]``:
# the named properties exist and are reachable, but their descriptors are
# non-enumerable (so they are omitted by ``Object.keys`` and ``for-in``).
@interface('HTMLAllCollection', None)
class HTMLAllCollection(LegacyNamedPropertyMixin, ReadOnlyIndexedPropertyMixin):
    """The legacy ``document.all`` view: undetectable (``typeof`` is ``"undefined"``
    and the object is falsy), callable and live.  It has no IDL parent, so it
    carries the full named/indexed interceptor set rather than extending
    :class:`HTMLCollection`.
    """
    __idl_overrides__ = {
        "interface": {
            "length": 0,
            "name_del": "__v8_name_del__",
            "name_enum": "__v8_name_enum__",
            "name_get": "__v8_name_get__",
            "name_query": "__v8_name_query__",
            "name_set": "__v8_name_set__"
        }
    }
    def __init__(self, owner=None, getter=None, **_):
        self._owner, self._getter = owner, getter

    def _items(self):
        items = self._getter() if self._getter else ()
        return items if isinstance(items, list) else list(items)

    def _matches(self, name):
        """Elements whose ``id`` or ``name`` equals ``name``, in tree order."""
        if not name:
            return []
        return [n for n in self._items() if _node_id(n) == name or _all_node_name(n) == name]

    def _named(self, name):
        # Chromium: the single match, a live collection for several, else null.
        matches = self._matches(name)
        if not matches:
            return pyv8.Null
        if len(matches) == 1:
            return matches[0]
        return HTMLCollection(owner=self._owner, getter=lambda: self._matches(name))

    def get_length(self): return len(self._items())

    def fn_item(self, *args):
        if not args or args[0] is pyv8.Undefined:
            return pyv8.Null
        name = dom_string(args[0])
        index = _array_index_name(name)
        if index is None:
            return self._named(name)
        items = self._items()
        return items[index] if 0 <= index < len(items) else pyv8.Null

    def fn_namedItem(self, *args):
        if not args:
            raise PageTypeError(
                "Failed to execute 'namedItem' on 'HTMLAllCollection': 1 argument required, but only 0 present."
            )
        return self._named(dom_string(args[0]))

    def __call__(self, *args):
        if not args or args[0] is pyv8.Undefined:
            return pyv8.Null
        name = dom_string(args[0])
        index = _array_index_name(name)
        if index is not None:
            items = self._items()
            return items[index] if index < len(items) else pyv8.Null
        return self._named(name)

    def _supported_names(self):
        return _supported_names(self._items(), _all_node_name)

    def _named_item(self, name):
        return self._named(name)

    def _indexed_items(self): return self._items()

    def __iter__(self): return iter(self._items())
    def __len__(self): return len(self._items())


@interface('DOMTokenList', None)
class DOMTokenList(ReadOnlyIndexedPropertyMixin):
    _ASCII_WHITESPACE = " \t\n\f\r"

    def __init__(self, element=None, attr="class", supported_tokens=None, **_):
        self._element = element
        self._attr = attr
        self._supported_tokens = (
            None if supported_tokens is None
            else frozenset(str(token).lower() for token in supported_tokens)
        )

    def _raw_value(self):
        return str(self._element.attributes.get(self._attr, ""))

    def _tokens(self):
        value = self._raw_value()
        tokens = []
        current = []
        for character in value:
            if character in self._ASCII_WHITESPACE:
                if current:
                    token = "".join(current)
                    if token not in tokens:
                        tokens.append(token)
                    current = []
            else:
                current.append(character)
        if current:
            token = "".join(current)
            if token not in tokens:
                tokens.append(token)
        return tokens

    def _indexed_items(self): return self._tokens()

    def _validate(self, token, method=None):
        token = str(token)
        prefix = f"Failed to execute '{method}' on 'DOMTokenList': " if method else ""
        if not token:
            raise dom_exception("SyntaxError", prefix + "The token provided must not be empty.")
        if any(ch in self._ASCII_WHITESPACE for ch in token):
            detail = "The token provided contains HTML space characters, which are not valid in tokens."
            if method in ("add", "remove", "toggle", "replace"):
                detail = f"The token provided ('{token}') contains HTML space characters, which are not valid in tokens."
            raise InvalidCharacterError(prefix + detail)
        return token

    def _update(self, values):
        values = list(dict.fromkeys(values))
        old = self._raw_value()
        if values or self._attr in self._element.attributes:
            self._element.attributes[self._attr] = " ".join(values)
        new = self._raw_value()
        changed = getattr(self._element, "_attribute_changed", None)
        if changed is not None and old != new:
            changed(self._attr, old, new)

    def get_length(self): return len(self._tokens())
    def get_value(self): return self._raw_value()
    def set_value(self, value):
        value = dom_string(value)
        old = self._raw_value()
        self._element.attributes[self._attr] = str(value)
        changed = getattr(self._element, "_attribute_changed", None)
        if changed is not None and old != str(value):
            changed(self._attr, old, str(value))
    def fn_item(self, index=MISSING, *_extra):
        if index is MISSING:
            raise missing_error('DOMTokenList', 'item', 1, 0, constructor=False)
        index = integer(index)
        return self._item_at(index)
    def fn_contains(self, token=MISSING, *_extra):
        if token is MISSING:
            raise missing_error('DOMTokenList', 'contains', 1, 0, constructor=False)
        token = dom_string(token)
        return token in self._tokens()

    def fn_add(self, *tokens):
        tokens = [dom_string(token) for token in tokens]
        tokens = [self._validate(token, "add") for token in tokens]
        self._update(self._tokens() + tokens)

    def fn_remove(self, *tokens):
        tokens = [dom_string(token) for token in tokens]
        tokens = set(self._validate(token, "remove") for token in tokens)
        self._update(token for token in self._tokens() if token not in tokens)

    def fn_toggle(self, token=MISSING, force=pyv8.Undefined, *_extra):
        if token is MISSING:
            raise missing_error('DOMTokenList', 'toggle', 1, 0, constructor=False)
        token = dom_string(token)
        if force is not pyv8.Undefined:
            force = boolean(force)
        token = self._validate(token, "toggle")
        values = self._tokens()
        present = token in values
        if present:
            if force is pyv8.Undefined or not force:
                self._update(value for value in values if value != token)
                return False
            return True
        if force is pyv8.Undefined or force:
            self._update(values + [token])
            return True
        return False

    def fn_replace(self, old=MISSING, new=MISSING, *_extra):
        if old is MISSING:
            raise missing_error('DOMTokenList', 'replace', 2, 0, constructor=False)
        if new is MISSING:
            raise missing_error('DOMTokenList', 'replace', 2, 1, constructor=False)
        old = dom_string(old)
        new = dom_string(new)
        old, new = self._validate(old, "replace"), self._validate(new, "replace")
        values = self._tokens()
        if old not in values:
            return False
        if old != new:
            if new in values:
                values.remove(old)
            else:
                values[values.index(old)] = new
        self._update(values)
        return True

    def fn_supports(self, token=MISSING, *_extra):
        if token is MISSING:
            raise missing_error('DOMTokenList', 'supports', 1, 0, constructor=False)
        token = dom_string(token)
        if self._supported_tokens is None:
            raise PageTypeError("DOMTokenList has no supported tokens.")
        return str(token).lower() in self._supported_tokens

    def fn_toString(self, *_extra):
        return self.get_value()
    def __iter__(self): return iter(self._tokens())


#: The attribute prefix ``dataset`` maps over.
_DATA_PREFIX = "data-"


@interface('DOMStringMap', None)
class DOMStringMap:
    """``element.dataset``: the ``data-*`` attributes as a writable name map.

    The attributes stay the source of truth -- this holds no state of its own -- so
    ``dataset.fooBar = 'x'`` and ``setAttribute('data-foo-bar', 'x')`` are the same
    write seen from two sides.  A name reads back only when writing it would produce
    the same attribute, which is what keeps an attribute like ``data-foo-`` from
    looking readable.
    """

    __idl_overrides__ = {
        "interface": {
            "length": 0,
            "name_del": "__v8_name_del__",
            "name_enum": "__v8_name_enum__",
            "name_get": "__v8_name_get__",
            "name_query": "__v8_name_query__",
            "name_set": "__v8_name_set__"
        }
    }

    def __init__(self, element=None, **_):
        self._element = element

    def _attributes(self):
        element = self._element
        return getattr(element, "attributes", {}) if element is not None else {}

    def _attribute_name(self, name):
        """The attribute a map name reads and writes, or ``""`` when it has none."""
        if not isinstance(name, str) or not name or name in OBJECT_PROTOTYPE_NAMES:
            return ""
        return _DATA_PREFIX + dash_name(name)

    def _visible_names(self):
        """The names present now: the attributes that spell back to themselves."""
        names = []
        for attribute in self._attributes():
            if not attribute.startswith(_DATA_PREFIX):
                continue
            name = camel_name(attribute[len(_DATA_PREFIX):])
            if name and _DATA_PREFIX + dash_name(name) == attribute:
                names.append(name)
        return names

    def __v8_name_get__(self, name):
        attribute = self._attribute_name(name)
        attributes = self._attributes()
        if not attribute or attribute not in attributes:
            return None
        return str(attributes[attribute])

    def __v8_name_set__(self, name, value):
        attribute = self._attribute_name(name)
        if not attribute:
            return None
        self._attributes()[attribute] = str(dom_string(value))
        return True

    def __v8_name_query__(self, name):
        attribute = self._attribute_name(name)
        return 0 if attribute and attribute in self._attributes() else None

    def __v8_name_del__(self, name):
        attribute = self._attribute_name(name)
        if not attribute:
            return None
        self._attributes().pop(attribute, None)
        return True

    def __v8_name_enum__(self):
        return self._visible_names()


@interface('DOMStringList', None)
class DOMStringList(ReadOnlyIndexedPropertyMixin):
    """The legacy string list: IndexedDB's store and index names, and the origins
    ``Location.ancestorOrigins`` reports, both hand one of these out.
    """

    def __init__(self, names=(), **_):
        self._names = tuple(str(name) for name in names)

    def _indexed_items(self):
        return self._names

    def get_length(self):
        return len(self._names)

    def fn_item(self, *args):
        index = int(args[0]) if args else 0
        return self._names[index] if 0 <= index < len(self._names) else pyv8.Null

    def fn_contains(self, *args):
        return str(args[0]) in self._names if args else False


@interface('HTMLOptionsCollection', 'HTMLCollection')
class HTMLOptionsCollection(HTMLCollection):
    """``select.options``: the select's options as a live list.  It adds only the
    selected index, which is answered by the select itself."""

    def get_selectedIndex(self):
        owner = self._owner
        return owner.get_selectedIndex() if owner is not None else -1

    def set_selectedIndex(self, index):
        owner = self._owner
        if owner is not None:
            owner.set_selectedIndex(index)


@interface('HTMLFormControlsCollection', 'HTMLCollection')
class HTMLFormControlsCollection(HTMLCollection):
    """``form.elements``: the form's listed elements as a live list.  Which elements
    belong is the form's rule, so the form supplies the getter."""
