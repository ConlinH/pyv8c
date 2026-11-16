"""The core DOM node types: ``Node``, ``DocumentFragment`` and ``Attr``.

``Node`` owns the tree algorithms (adoption, insertion, cloning, comparison);
``Element``/``Document``/``Text`` references are resolved lazily, since those
modules sit above this one.
"""

from pyv8c import pyv8
from ...idl import interface
from ...foundation.arguments import (
    MISSING,
    boolean,
    dictionary,
    dom_string,
    interface_value,
    is_nullish,
    missing_error,
)
from ..page_errors import HierarchyRequestError, NotFoundError, PageTypeError
from .collections import NodeList
from .events import EventTarget
from .mixins import _ParentNodeMixin
from .utils import XHTML_NAMESPACE, is_element


def _is_style_element(node):
    """Whether *node* supplies a stylesheet to the document.

    ``nodeType`` is checked first: it is a plain attribute on every node, while
    ``localName`` on a text node falls through to a raising ``__getattr__``.  Runs
    once per DOM mutation, so it must stay this cheap.
    """
    if getattr(node, "nodeType", None) != Node.ELEMENT_NODE:
        return False
    if node.localName == "style":
        return True
    return (node.localName == "link" and
            "stylesheet" in str(node.attributes.get("rel", "")).lower().split())


def _contains_style(node):
    """Whether *node* supplies or contains a stylesheet.

    Iterative to keep a deep subtree off the stack; on the parse path (elements
    adopted before their children) the walk is a single step.
    """
    if _is_style_element(node):
        return True
    children = list(getattr(node, "_children", ()))
    shadow = getattr(node, '_shadow_root', None)
    if shadow is not None:
        children.append(shadow)
    if not children:
        return False
    pending = list(children)
    while pending:
        current = pending.pop()
        if _is_style_element(current):
            return True
        pending.extend(getattr(current, "_children", ()))
        shadow = getattr(current, '_shadow_root', None)
        if shadow is not None:
            pending.append(shadow)
    return False


class _ChildList(list):
    """A node's children: every change moves the version a tree walk keys on.

    Building a node changes no tree, so a fresh list does not move the version --
    only a change to the list it belongs to does.
    """

    __slots__ = ('_owner',)

    def __init__(self, owner):
        self._owner = owner

    def append(self, value): self._owner._invalidate_tree(); super().append(value)
    def insert(self, index, value): self._owner._invalidate_tree(); super().insert(index, value)
    def remove(self, value): self._owner._invalidate_tree(); super().remove(value)
    def pop(self, index=-1): self._owner._invalidate_tree(); return super().pop(index)
    def clear(self): self._owner._invalidate_tree(); super().clear()
    def extend(self, values): self._owner._invalidate_tree(); super().extend(values)
    def __setitem__(self, index, value): self._owner._invalidate_tree(); super().__setitem__(index, value)
    def __delitem__(self, index): self._owner._invalidate_tree(); super().__delitem__(index)
    def __iadd__(self, values): self.extend(values); return self
    def __imul__(self, count): self._owner._invalidate_tree(); super().__imul__(count); return self
    def reverse(self): self._owner._invalidate_tree(); super().reverse()
    def sort(self, *args, **kwargs): self._owner._invalidate_tree(); super().sort(*args, **kwargs)


class _IdIndex:
    """Index only as far as a lookup needs, preserving cheap early matches."""
    __slots__ = ('_first', '_remaining')

    def __init__(self, descendants):
        self._first = {}
        self._remaining = descendants

    def get(self, value):
        found = self._first.get(value)
        if found is not None or self._remaining is None:
            return found
        for node in self._remaining:
            if node.nodeType == Node.ELEMENT_NODE:
                name = node.attributes.get('id')
                if name:
                    self._first.setdefault(name, node)
                    if name == value:
                        return self._first[name]
        self._remaining = None
        return None


def _subtree_nodes(node):
    """Every shadow-including descendant of *node*, depth first.

    A shadow tree hangs off its host without being among its children, yet the insertion
    steps reach it all the same: that is where an embedded widget keeps its own frame.
    """
    stack = list(getattr(node, "_children", ()) or ())
    own_shadow = getattr(node, "_shadow_root", None)
    if own_shadow is not None:
        stack.append(own_shadow)
    while stack:
        child = stack.pop()
        yield child
        stack.extend(getattr(child, "_children", ()) or ())
        shadow = getattr(child, "_shadow_root", None)
        if shadow is not None:
            stack.append(shadow)


def _record_mutation(target, kind, **details):
    """Report one change to the document's mutation observers."""
    from .mutation import record_mutation
    record_mutation(target, kind, **details)


def _bump_style_version(node):
    """Note that the document's ``<style>`` derived data may have changed.

    Layout caches the parsed sheet on the document, so a mutation that can change
    what a ``<style>`` holds invalidates the cache.  Callers pass the node before
    detaching it; everything else costs one ``localName`` read.
    """
    parent = getattr(node, "parentNode", None)
    if not (_is_style_element(parent) or _contains_style(node)):
        return
    #: A sheet is bound to the markup it was made from.  Rewriting the owner's text or
    #: moving it out of the document detaches the sheet -- it keeps the rules it already
    #: answered -- and the owner gets a new sheet the next time one is asked for.
    owners = (parent,) if _is_style_element(parent) else (node, *_subtree_nodes(node))
    for owner in owners:
        if not _is_style_element(owner):
            continue
        sheet = getattr(owner, "_style_sheet", None)
        if sheet is not None:
            sheet._detach()
            owner._style_sheet = None
    document = getattr(node, "ownerDocument", None)
    if document is not None:
        document.style_version = getattr(document, "style_version", 0) + 1


def _adopt_subtree(node, services):
    """DOM's adopt: *node* and its shadow-including descendants join *services*.

    ``ownerDocument``, ``baseURI`` and every task the subtree schedules follow the
    services, so a node created in one document and inserted into another answers as
    Chrome's does.  A node already in that document -- the common case -- costs one
    identity test.
    """
    if services is None or getattr(node, "_services", None) is services:
        return
    stack = [node]
    while stack:
        current = stack.pop()
        if getattr(current, "_services", None) is not services:
            current._services = services
            _bump_style_version(current)
        stack.extend(current._children)
        shadow = getattr(current, "_shadow_root", None)
        if shadow is not None:
            stack.append(shadow)


@interface('Node', 'EventTarget')
class Node(EventTarget):
    #: ``nodeType`` reads as a constant: it is not enumerated on an instance.
    __idl_overrides__ = {"members": {"nodeType": {"property_flags": "dont_enum"}}}

    ELEMENT_NODE, ATTRIBUTE_NODE, TEXT_NODE, COMMENT_NODE = 1, 2, 3, 8
    CDATA_SECTION_NODE, ENTITY_REFERENCE_NODE, ENTITY_NODE = 4, 5, 6
    PROCESSING_INSTRUCTION_NODE, NOTATION_NODE = 7, 12
    DOCUMENT_NODE, DOCUMENT_TYPE_NODE, DOCUMENT_FRAGMENT_NODE = 9, 10, 11
    DOCUMENT_POSITION_DISCONNECTED = 1
    DOCUMENT_POSITION_PRECEDING = 2
    DOCUMENT_POSITION_FOLLOWING = 4
    DOCUMENT_POSITION_CONTAINS = 8
    DOCUMENT_POSITION_CONTAINED_BY = 16
    DOCUMENT_POSITION_IMPLEMENTATION_SPECIFIC = 32

    def __init__(self, services=None, node_type=0, node_name="", value=None, **kw):
        super().__init__(services=services, **kw)
        self.nodeType, self.nodeName, self.nodeValue = node_type, node_name, value
        self.parentNode = None
        self._children = _ChildList(self)
        self._child_nodes_view = NodeList(owner=self, getter=lambda: self._children)

    @property
    def parentElement(self):
        return self.parentNode if is_element(self.parentNode) else None
    @property
    def firstChild(self): return self._children[0] if self._children else pyv8.Null
    @property
    def lastChild(self): return self._children[-1] if self._children else pyv8.Null

    @property
    def ownerDocument(self):
        """The document this node belongs to; ``None`` for a Document itself.  A
        detached node keeps its creating document, hence the realm lookup."""
        if self.nodeType == Node.DOCUMENT_NODE:
            return None
        document_services = getattr(getattr(self, "_services", None), "document_services", None)
        return getattr(document_services, "document", None)

    @property
    def isConnected(self):
        """Whether the shadow-including root is a Document: a tree inside a shadow root
        is connected through its host, even though its own root is that shadow root."""
        from .document import Document

        node = self
        while node is not None:
            root = node.root_node()
            if isinstance(root, Document):
                return True
            # ``host`` is the shadow root's IDL attribute; the real link is the private one.
            host = getattr(root, "_host", None)
            if host is None:
                return False
            node = host
        return False

    @property
    def baseURI(self):
        document = self if self.nodeType == Node.DOCUMENT_NODE else self.ownerDocument
        return getattr(document, "url", "") if document is not None else ""

    def root_node(self):
        """The topmost ancestor, i.e. the root of this node's tree."""
        node = self
        while node.parentNode is not None:
            node = node.parentNode
        return node

    def _sibling(self, step):
        """The adjacent sibling in ``step`` direction, or ``None``."""
        parent = self.parentNode
        if parent is None:
            return None
        siblings = parent._children
        try:
            index = siblings.index(self) + step
        except ValueError:
            return None
        return siblings[index] if 0 <= index < len(siblings) else None

    @property
    def previousSibling(self): return self._sibling(-1)
    @property
    def nextSibling(self): return self._sibling(1)

    def _ancestors(self):
        """Yield ``self`` followed by its ancestors, nearest first."""
        node = self
        while node is not None:
            yield node
            node = node.parentNode

    def _walk_descendants(self):
        """Yield every descendant of ``self`` in tree order."""
        for child in self._children:
            yield child
            if isinstance(child, Node):
                yield from child._walk_descendants()

    def _descendants(self):
        """Yield every descendant of ``self`` in tree order."""
        for child in self._children:
            yield child
            if isinstance(child, Node):
                yield from child._walk_descendants()

    def _descendant_snapshot(self):
        """The same walk as a tuple, reused until the tree changes.

        A query reads the whole tree and filters it, and a page runs the same query
        far more often than it changes the tree.  A caller that stops at the first
        match wants ``_descendants`` instead, which stays lazy -- materialising the
        tuple for one of those turned parsing into a quadratic walk.
        """
        cached = self.__dict__.get("_descendants_cache")
        if cached is None:
            cached = tuple(self._walk_descendants())
            self.__dict__["_descendants_cache"] = cached
        return cached

    def _tag_index_snapshot(self):
        """Descendant elements grouped by local name, reused until the tree changes.

        A tag query reads the same groups far more often than it changes the tree,
        and filtering the whole snapshot for one name visits every text node besides.
        Built from the descendant snapshot, so it keeps tree order and the same
        shadow-including reach; a local name never changes, so only tree edits
        invalidate it.
        """
        cached = self.__dict__.get("_tag_index_cache")
        if cached is None:
            index = {}
            for node in self._descendant_snapshot():
                if node.nodeType == Node.ELEMENT_NODE:
                    index.setdefault(node.localName, []).append(node)
            cached = index
            self.__dict__["_tag_index_cache"] = cached
        return cached

    def _invalidate_tree(self):
        # Only this subtree and its ancestors changed. Discard cached references
        # immediately, including removed nodes, rather than retaining old snapshots.
        node = self
        while node is not None:
            node.__dict__.pop('_descendants_cache', None)
            node.__dict__.pop('_tag_index_cache', None)
            node.__dict__.pop('_id_index', None)
            node = node.parentNode
        self._invalidate_layout()

    def _invalidate_id_index(self):
        node = self
        while node is not None:
            node.__dict__.pop('_id_index', None)
            node = node.parentNode

    def _invalidate_layout(self):
        document = self if self.nodeType == Node.DOCUMENT_NODE else self.ownerDocument
        if document is not None and hasattr(document, "layout_version"):
            document.layout_version += 1
            document._layout_snapshot = None

    def _find_element_by_id(self, value):
        if not value:
            return None
        index = self.__dict__.get('_id_index')
        if index is None:
            index = _IdIndex(self._descendants())
            self.__dict__['_id_index'] = index
        return index.get(value)

    def get_nodeType(self): return self.nodeType
    def get_nodeName(self): return self.nodeName
    def get_baseURI(self): return self.baseURI
    def get_isConnected(self): return self.isConnected
    def get_ownerDocument(self): return self.ownerDocument or pyv8.Null
    def get_previousSibling(self): return self.previousSibling or pyv8.Null
    def get_nextSibling(self): return self.nextSibling or pyv8.Null
    def get_parentNode(self): return self.parentNode or pyv8.Null
    def get_parentElement(self): return self.parentElement or pyv8.Null
    def get_childNodes(self): return self._child_nodes_view
    def get_firstChild(self): return self.firstChild
    def get_lastChild(self): return self.lastChild
    def get_textContent(self):
        """This subtree's text: a leaf reports its own data, a tree its children's.  A
        comment contributes nothing, though its own ``textContent`` is its data."""
        from .character_data import Comment

        if not self._children:
            value = getattr(self, "nodeValue", None)
            return "" if value is None else str(value)
        return "".join(
            "" if isinstance(child, Comment) else child.get_textContent() or ""
            for child in self._children
        )
    def set_textContent(self, value):
        from .character_data import Text

        if not is_nullish(value):
            value = dom_string(value)
        self._replace_children(())
        # An empty replacement leaves no child behind (the spec drops empty text nodes).
        if value is not None and str(value) != "":
            self.append_child(Text(str(value), services=self._services))
    def get_nodeValue(self): return self.nodeValue if self.nodeValue is not None else pyv8.Null
    def set_nodeValue(self, value):
        if not is_nullish(value):
            value = dom_string(value)
        previous = self.nodeValue
        self.nodeValue = value
        self._invalidate_layout()
        _bump_style_version(self)
        if previous != value:
            _record_mutation(self, "characterData", old_value=previous)

    @staticmethod
    def _notify_removed(node):
        for descendant in (node, *tuple(node._descendants())):
            callback = getattr(descendant, "_on_removed_from_document", None)
            if callback is not None:
                callback()

    @staticmethod
    def _detach_from_parent(node):
        parent = node.parentNode
        if parent is not None:
            children = parent._children
            position = children.index(node) if node in children else None
            previous = children[position - 1] if position else None
            next_ = (children[position + 1]
                     if position is not None and position + 1 < len(children) else None)
            try:
                children.remove(node)
            except ValueError:
                pass
            node.parentNode = None
            if position is not None:
                _record_mutation(parent, "childList", removed=[node],
                                 previous=previous, next_=next_)

    def _ensure_adoptable(self, node):
        if not isinstance(node, Node): raise PageTypeError("The new child is not a Node")
        # HierarchyRequestError fires only when ``node`` is an inclusive ancestor of
        # ``self``; re-inserting an existing descendant is a plain move.
        if node is self or node.fn_contains(self):
            raise HierarchyRequestError("The new child element contains the parent.")

    def _adopt_child(self, node, index=None):
        self._ensure_adoptable(node)
        self._detach_from_parent(node)
        _adopt_subtree(node, getattr(self, "_services", None))
        node.parentNode = self
        if index is None:
            self._children.append(node)
            position = len(self._children) - 1
        else:
            position = index if index >= 0 else max(len(self._children) + index, 0)
            position = min(position, len(self._children))
            self._children.insert(position, node)
        for inserted in (node, *_subtree_nodes(node)):
            callback = getattr(inserted, '_on_tree_inserted', None)
            if callback is not None:
                callback()
        _bump_style_version(node)
        services = getattr(self, "_services", None)
        document_services = getattr(services, "document_services", None)
        if document_services is not None and not document_services.parsing:
            _record_mutation(self, "childList", added=[node],
                             previous=self._children[position - 1] if position else None,
                             next_=(self._children[position + 1]
                                    if position + 1 < len(self._children) else None))
        if document_services is not None:
            callback = getattr(document_services, "node_adopted", None)
            framed = getattr(node, "localName", "") == "iframe"
            # The parser runs its own hooks for the nodes it builds; a node the page
            # inserts while the parser still runs is not the parser's to handle, and a
            # script inserted that way runs in both engines.
            parser_built = getattr(node, "_parser_created", False)
            if callback is not None and (not document_services.parsing or framed
                                         or not parser_built):
                callback(node)
                # A subtree can arrive in one insertion, so its detached frames and scripts
                # said nothing: the steps run per shadow-including descendant.
                if node.isConnected:
                    for descendant in _subtree_nodes(node):
                        callback(descendant)
        return node

    def _replace_children(self, nodes):
        removed = list(self._children)
        for child in removed:
            _bump_style_version(child)
            child.parentNode = None
            self._notify_removed(child)
        if removed:
            _record_mutation(self, "childList", removed=removed)
        self._children.clear()
        for node in nodes:
            self._adopt_child(node)

    def append_child(self, node):
        if isinstance(node, DocumentFragment):
            for child in list(node._children):
                self._adopt_child(child)
            return node
        return self._adopt_child(node)
    def fn_appendChild(self, node=MISSING, *_extra):
        if node is MISSING:
            raise missing_error('Node', 'appendChild', 1, 0, constructor=False)
        node = interface_value(node, 'Node', 'Node', 'appendChild', 1, nullable=False)
        return self.append_child(node)
    def fn_insertBefore(self, node=MISSING, reference=MISSING, *_extra):
        if node is MISSING:
            raise missing_error('Node', 'insertBefore', 2, 0, constructor=False)
        if reference is MISSING:
            raise missing_error('Node', 'insertBefore', 2, 1, constructor=False)
        node = interface_value(node, 'Node', 'Node', 'insertBefore', 1, nullable=False)
        reference = interface_value(reference, 'Node', 'Node', 'insertBefore', 2, nullable=True)
        if reference in (None, pyv8.Null, pyv8.Undefined): return self.append_child(node)
        if reference not in self._children: raise NotFoundError("The reference node is not a child.")
        index = self._children.index(reference)
        if isinstance(node, DocumentFragment):
            for child in list(node._children):
                self._adopt_child(child, index)
                index += 1
            return node
        if node.parentNode is self and self._children.index(node) < index:
            index -= 1
        return self._adopt_child(node, index)
    def fn_removeChild(self, node=MISSING, *_extra):
        if node is MISSING:
            raise missing_error('Node', 'removeChild', 1, 0, constructor=False)
        node = interface_value(node, 'Node', 'Node', 'removeChild', 1, nullable=False)
        if node not in self._children: raise NotFoundError("The node to be removed is not a child.")
        _bump_style_version(node)
        self._children.remove(node); node.parentNode = None
        self._notify_removed(node)
        return node
    def fn_replaceChild(self, new=MISSING, old=MISSING, *_extra):
        if new is MISSING:
            raise missing_error('Node', 'replaceChild', 2, 0, constructor=False)
        if old is MISSING:
            raise missing_error('Node', 'replaceChild', 2, 1, constructor=False)
        new = interface_value(new, 'Node', 'Node', 'replaceChild', 1, nullable=False)
        old = interface_value(old, 'Node', 'Node', 'replaceChild', 2, nullable=False)
        if not isinstance(new, Node) or not isinstance(old, Node):
            raise PageTypeError("Failed to execute 'replaceChild' on 'Node': parameter is not of type 'Node'.")
        if old not in self._children:
            #: Chrome's message, prefix and wording (measured side by side 2026-10-06).
            raise NotFoundError("Failed to execute 'replaceChild' on 'Node': "
                                "The node to be replaced is not a child of this node.")
        i = self._children.index(old)
        self.fn_removeChild(old)
        if isinstance(new, DocumentFragment):
            for child in list(new._children):
                self._adopt_child(child, i)
                i += 1
            return old
        if new.parentNode is self and self._children.index(new) < i:
            i -= 1
        self._adopt_child(new, i)
        return old
    def fn_cloneNode(self, deep=False, *_extra):
        if deep is pyv8.Undefined:
            deep = False
        if deep is not pyv8.Undefined:
            deep = boolean(deep)
        clone = self._clone_shallow()
        if deep:
            for child in self._children: clone.append_child(child.fn_cloneNode(True))
        return clone
    def _clone_shallow(self): return Node(services=self._services, node_type=self.nodeType, node_name=self.nodeName, value=self.nodeValue)
    def fn_compareDocumentPosition(self, other=MISSING, *_extra):
        if other is MISSING:
            raise missing_error('Node', 'compareDocumentPosition', 1, 0, constructor=False)
        other = interface_value(other, 'Node', 'Node', 'compareDocumentPosition', 1, nullable=False)
        """Bitmask describing ``other``'s position relative to ``self``."""
        if not isinstance(other, Node):
            raise PageTypeError("Failed to execute 'compareDocumentPosition' on 'Node': parameter 1 is not of type 'Node'.")
        if other is self:
            return 0
        if self.root_node() is not other.root_node():
            # Trees have no defined relative order; the standard explicitly
            # leaves the preceding/following choice to the implementation.
            return (Node.DOCUMENT_POSITION_DISCONNECTED
                    | Node.DOCUMENT_POSITION_IMPLEMENTATION_SPECIFIC
                    | Node.DOCUMENT_POSITION_FOLLOWING)
        self_path = list(self._ancestors())
        other_path = list(other._ancestors())
        if other in self_path:
            return Node.DOCUMENT_POSITION_CONTAINS | Node.DOCUMENT_POSITION_PRECEDING
        if self in other_path:
            return Node.DOCUMENT_POSITION_CONTAINED_BY | Node.DOCUMENT_POSITION_FOLLOWING
        # Neither contains the other: order is set by the branches leaving their common ancestor.
        common = next(node for node in self_path if node in other_path)
        self_branch = self_path[self_path.index(common) - 1]
        other_branch = other_path[other_path.index(common) - 1]
        siblings = common._children
        if self_branch in siblings and other_branch in siblings:
            return (Node.DOCUMENT_POSITION_FOLLOWING
                    if siblings.index(self_branch) < siblings.index(other_branch)
                    else Node.DOCUMENT_POSITION_PRECEDING)
        return (Node.DOCUMENT_POSITION_IMPLEMENTATION_SPECIFIC
                | Node.DOCUMENT_POSITION_FOLLOWING)

    def fn_getRootNode(self, options=None, *_extra):
        options = dictionary(options)
        boolean(options.get("composed", False))
        return self.root_node()

    def fn_isSameNode(self, other=MISSING, *_extra):
        if other is MISSING:
            raise missing_error('Node', 'isSameNode', 1, 0, constructor=False)
        other = interface_value(other, 'Node', 'Node', 'isSameNode', 1, nullable=True)
        return other is self

    def fn_isDefaultNamespace(self, namespace=MISSING, *_extra):
        if namespace is MISSING:
            raise missing_error('Node', 'isDefaultNamespace', 1, 0, constructor=False)
        if not is_nullish(namespace):
            namespace = dom_string(namespace)
        return namespace == XHTML_NAMESPACE

    def fn_lookupNamespaceURI(self, prefix=MISSING, *_extra):
        if prefix is MISSING:
            raise missing_error('Node', 'lookupNamespaceURI', 1, 0, constructor=False)
        if not is_nullish(prefix):
            prefix = dom_string(prefix)
        if prefix in (None, pyv8.Null, pyv8.Undefined, ""):
            return XHTML_NAMESPACE
        return pyv8.Null

    def fn_lookupPrefix(self, namespace=MISSING, *_extra):
        if namespace is MISSING:
            raise missing_error('Node', 'lookupPrefix', 1, 0, constructor=False)
        if not is_nullish(namespace):
            namespace = dom_string(namespace)
        return pyv8.Null

    def fn_normalize(self, *_extra):
        """Merge adjacent text nodes and drop empty ones, recursively."""
        from .character_data import Text

        previous = None
        for child in list(self._children):
            if isinstance(child, Text):
                if not child.nodeValue:
                    self._children.remove(child); child.parentNode = None; continue
                if isinstance(previous, Text):
                    previous.nodeValue = (previous.nodeValue or "") + (child.nodeValue or "")
                    self._children.remove(child); child.parentNode = None
                    continue
            previous = child
        for child in list(self._children):
            if isinstance(child, Node):
                child.fn_normalize()

    def fn_contains(self, other=MISSING, *_extra):
        if other is MISSING:
            raise missing_error('Node', 'contains', 1, 0, constructor=False)
        other = interface_value(other, 'Node', 'Node', 'contains', 1, nullable=True)
        return other is self or any(c.fn_contains(other) for c in self._children)
    def fn_hasChildNodes(self, *_extra):
        return bool(self._children)
    def fn_isEqualNode(self, other=MISSING, *_extra):
        if other is MISSING:
            raise missing_error('Node', 'isEqualNode', 1, 0, constructor=False)
        other = interface_value(other, 'Node', 'Node', 'isEqualNode', 1, nullable=True)
        return isinstance(other, Node) and self.outer_html() == other.outer_html()
    def outer_html(self): return self.get_textContent()


@interface('DocumentFragment', 'Node')
class DocumentFragment(_ParentNodeMixin, Node):
    def __init__(self, *_extra, services=None, **kw):
        if services is pyv8.Undefined:
            services = None
        super().__init__(services=services, node_type=Node.DOCUMENT_FRAGMENT_NODE, node_name="#document-fragment", **kw)
    def get_children(self):
        from .collections import HTMLCollection
        collection = self.__dict__.get('_children_collection')
        if collection is None:
            collection = self._children_collection = HTMLCollection(
                owner=self, getter=self._element_children)
        return collection
    def fn_getElementById(self, value=MISSING, *_extra):
        if value is MISSING:
            raise missing_error('DocumentFragment', 'getElementById', 1, 0, constructor=False)
        return self._find_element_by_id(str(dom_string(value))) or pyv8.Null
    def _document(self):
        """The document the fragment belongs to; the selector engine lives above this
        module, so the match is delegated to the document."""
        return getattr(getattr(self._services, "document_services", None), "document", None)
    def fn_querySelector(self, selector=MISSING, *_extra):
        if selector is MISSING:
            raise missing_error('DocumentFragment', 'querySelector', 1, 0, constructor=False)
        selector = dom_string(selector)
        document = self._document()
        return document.query_selector(selector, self) if document is not None else pyv8.Null
    def fn_querySelectorAll(self, selector=MISSING, *_extra):
        if selector is MISSING:
            raise missing_error('DocumentFragment', 'querySelectorAll', 1, 0, constructor=False)
        selector = dom_string(selector)
        document = self._document()
        return document.query_selector_all(selector, self) if document is not None else []
    def _clone_shallow(self): return DocumentFragment(services=self._services)


@interface('ShadowRoot', 'DocumentFragment')
class ShadowRoot(DocumentFragment):
    """The tree a host keeps to itself: it is a document fragment, so nothing of it
    shows through the host's children, text or markup, and the mode decides whether
    the page can reach it again through ``host.shadowRoot``.
    """

    def __init__(self, mode="open", host=None, services=None, **_):
        super().__init__(services=services)
        self._mode, self._host = mode, host

    def get_mode(self): return self._mode
    def get_activeElement(self):
        """The element focused inside this tree, or null: no body fallback for a shadow root."""
        document = getattr(getattr(self._services, "document_services", None), "document", None)
        if document is None: return pyv8.Null
        from .focus import active_element
        focused = active_element(document)
        node = focused
        while node is not None:
            if node is self: return focused
            node = getattr(node, "parentNode", None)
        return pyv8.Null
    def get_host(self): return pyv8.Null if self._host is None else self._host
    def get_styleSheets(self):
        from ..css.stylesheet import StyleSheetList
        view = getattr(self, "_style_sheets", None)
        if view is None:
            view = self._style_sheets = StyleSheetList(self._document(), root=self)
        return view
    def get_innerHTML(self): return "".join(child.outer_html() for child in self._children)
    def set_innerHTML(self, value):
        self._replace_children(())
        if self._services:
            self._services.document_services.document._parse_fragment(
                str(dom_string(value)), self)

    def _clone_shallow(self):
        return ShadowRoot(self._mode, self._host, services=self._services)


@interface('Attr', 'Node')
class Attr(Node):
    def __init__(self, name="", value="", ownerElement=None, services=None, **kw):
        super().__init__(services=services, node_type=Node.ATTRIBUTE_NODE, node_name=str(name), value=str(value), **kw)
        self.ownerElement = ownerElement
    def get_name(self): return self.nodeName
    def get_value(self): return self.nodeValue or ""
    def set_value(self, value):
        value = dom_string(value)
        self.nodeValue = str(value)
        if self.ownerElement is not None:
            self.ownerElement._set_attribute(self.nodeName, self.nodeValue)
    def get_specified(self): return True
    # HTML attributes carry no namespace, so both namespace accessors are null.
    def get_namespaceURI(self): return pyv8.Null
    def get_prefix(self): return pyv8.Null
    def get_localName(self): return self.nodeName
    def get_ownerElement(self): return self.ownerElement or pyv8.Null
