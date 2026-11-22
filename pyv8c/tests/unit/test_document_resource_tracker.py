from types import SimpleNamespace

from pyv8c.foundation.load_handle import LoadHandle
from pyv8c.platform.resources.tracker import DocumentResource, DocumentResourceTracker


def test_node_index_multiple_records_take_and_clear():
    tracker = DocumentResourceTracker()
    node = object()
    tokens = [tracker.add(DocumentResource(1, node, "img", LoadHandle())) for _ in range(3)]
    other = object()
    tracker.add(DocumentResource(1, other, "iframe", LoadHandle()))
    assert len(tracker.for_node(node)) == 3
    tracker.take(tokens[1])
    assert len(tracker.for_node(node)) == 2
    assert tracker.take(tokens[1]) is None
    tracker.take(tokens[0])
    tracker.take(tokens[2])
    assert not tracker.for_node(node)
    assert id(node) not in tracker._nodes
    assert len(tracker.clear()) == 1
    assert not tracker.entries and not tracker._nodes


def test_generation_cancel_and_frame_identity_validation():
    tracker = DocumentResourceTracker()
    node, services = object(), object()
    child = SimpleNamespace(closed=False, navigation=SimpleNamespace(generation=3))
    item = DocumentResource(2, node, "iframe", LoadHandle(),
                            child_services=services, child_generation=3)
    token = tracker.add(item)
    assert tracker.frame_matches(token, 2, node, services, child) is item
    assert tracker.valid(token, 1) is None
    assert tracker.frame_matches(token, 2, node, object(), child) is None
    child.navigation.generation = 4
    assert tracker.frame_matches(token, 2, node, services, child) is None
    item.handle.cancel()
    assert tracker.valid(token, 2) is None
