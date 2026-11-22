"""Typed document registrations; no DOM mutation or event dispatch."""
from dataclasses import dataclass
import itertools

from ...foundation.load_handle import LoadHandle

@dataclass(slots=True)
class DocumentResource:
    generation: int
    node: object
    kind: str
    handle: LoadHandle
    child_services: object = None
    child_generation: int | None = None


class DocumentResourceTracker:
    def __init__(self):
        self.entries = {}
        self._nodes = {}
        self.counter = itertools.count(1)

    def add(self, item):
        token = next(self.counter)
        self.entries[token] = item
        self._nodes.setdefault(id(item.node), set()).add(token)
        return token

    def get(self, token):
        return self.entries.get(token)

    def take(self, token):
        item = self.entries.pop(token, None)
        if item is not None:
            tokens = self._nodes[id(item.node)]
            tokens.remove(token)
            if not tokens:
                del self._nodes[id(item.node)]
        return item

    def clear(self):
        items = tuple(self.entries.values())
        self.entries.clear()
        self._nodes.clear()
        return items

    def for_node(self, node):
        return tuple((token, self.entries[token]) for token in self._nodes.get(id(node), ()))

    @property
    def has_pending(self):
        return bool(self.entries)

    def outstanding(self):
        counts = {}
        for item in self.entries.values():
            if not item.handle.done:
                counts[item.kind] = counts.get(item.kind, 0) + 1
        return counts

    def valid(self, token, generation):
        item = self.get(token)
        return item if item is not None and item.generation == generation and not item.handle.cancelled else None

    def frame_matches(self, token, generation, node, services, child):
        item = self.valid(token, generation)
        return item if (item is not None and item.node is node and child is not None
                        and not child.closed and item.child_services is services
                        and item.child_generation == child.navigation.generation) else None
