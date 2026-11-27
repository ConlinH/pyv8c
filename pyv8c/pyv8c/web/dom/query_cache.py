"""Tree-order query caches and their structural invalidation boundary.

Cache storage lives on each query root. Shadow roots form separate trees;
the ancestor walk never follows their host link.
"""

from contextlib import contextmanager
from threading import local


class _TreeInvalidationState(local):
    seen = None


_tree_invalidation = _TreeInvalidationState()


@contextmanager
def coalesce_invalidations():
    """Clear each ancestor once, unless a nested read republishes a cache."""
    previous = _tree_invalidation.seen
    if previous is not None:
        previous.clear()
    _tree_invalidation.seen = {}
    try:
        yield
    finally:
        _tree_invalidation.seen = previous


def _cache_published():
    # Python code re-entering the parser can read and repopulate a cleared cache.
    seen = _tree_invalidation.seen
    if seen is not None:
        seen.clear()


def publish(node, key, value, *, use_setter=False):
    if use_setter:
        setattr(node, key, value)
    else:
        node.__dict__[key] = value
    _cache_published()


def invalidate_ancestors(node):
    # Only this subtree and its ancestors changed. Discard cached references
    # immediately, including removed nodes, rather than retaining old snapshots.
    seen = _tree_invalidation.seen
    while node is not None:
        if seen is not None:
            identity = id(node)
            if identity in seen:
                break
            # Keep visited nodes alive so their identities cannot be reused.
            seen[identity] = node
        state = node.__dict__
        state.pop('_descendants_cache', None)
        state.pop('_tag_index_cache', None)
        state.pop('_id_index', None)
        state.pop('_selector_projection', None)
        node = node.parentNode


class IdIndex:
    """Scan in tree order, updating the portion already indexed after ID edits."""
    __slots__ = ('_by_id', '_remaining', '_cursor')

    def __init__(self, descendants):
        self._by_id = {}
        self._remaining = descendants
        self._cursor = None

    def _scan(self, until=None):
        from .nodes import Node

        if self._remaining is None:
            return
        for node in self._remaining:
            if node.nodeType == Node.ELEMENT_NODE:
                name = node.attributes.get('id')
                if name:
                    self._by_id.setdefault(name, []).append(node)
                    if name == until:
                        self._cursor = node
                        return
        self._remaining = None
        self._cursor = None

    def get(self, value):
        if value not in self._by_id:
            self._scan(until=value)
        nodes = self._by_id.get(value)
        return nodes[0] if nodes else None

    def candidates(self, value, *, finish):
        if finish:
            self._scan()
        if self._remaining is None:
            return tuple(self._by_id.get(value, ()))
        return None

    def change(self, node, old, new):
        from .nodes import Node

        if self._remaining is not None:
            # Attribute edits leave the traversal intact. A node after the
            # cursor will be indexed with its new ID when the walk reaches it.
            cursor = self._cursor
            if cursor is None or (
                Node.fn_compareDocumentPosition(cursor, node) & Node.DOCUMENT_POSITION_FOLLOWING
            ):
                return True
        if old:
            nodes = self._by_id.get(old, ())
            if node not in nodes:
                return False
            nodes.remove(node)
            if not nodes:
                del self._by_id[old]
        if new:
            nodes = self._by_id.setdefault(new, [])
            for offset, other in enumerate(nodes):
                if Node.fn_compareDocumentPosition(node, other) & Node.DOCUMENT_POSITION_FOLLOWING:
                    nodes.insert(offset, node)
                    break
            else:
                nodes.append(node)
        return True
