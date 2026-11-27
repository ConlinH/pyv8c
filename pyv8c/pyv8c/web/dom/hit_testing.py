"""Which element a point lands on, and the stack of elements behind it.

The boxes ``getBoundingClientRect`` reports are the ones hit-tested, so a point
outside the viewport reports ``null`` where Chrome's ``elementFromPoint`` does.
``visibility:hidden``/``pointer-events:none`` skip the element itself but keep
its descendants reachable; the topmost element wins and the rest of the stack is
deepest first.  Measured against Chrome 153.
"""

from pyv8c import pyv8

from .layout import layout_snapshot


#: Viewport used when a document has no environment to ask, matching ``dom.layout``.
_DEFAULT_VIEWPORT = (1280, 720)


def viewport_size(document):
    """``(width, height)`` of the document's viewport."""
    services = getattr(document, "_services", None)
    window = getattr(getattr(services, 'document_services', None), 'window', None)
    if window is not None:
        return window.innerWidth, window.innerHeight
    realm = getattr(services, "realm_services", None)
    viewport = getattr(getattr(realm, "environment", None), "viewport", None)
    return (
        getattr(viewport, "width", None) or _DEFAULT_VIEWPORT[0],
        getattr(viewport, "height", None) or _DEFAULT_VIEWPORT[1],
    )


def elements_from_point(document, x, y, *, deep=False):
    """The elements under a point, topmost first, as ``elementsFromPoint`` reports them."""
    width, height = viewport_size(document)
    if not (0 <= x < width and 0 <= y < height):
        return []
    root = document.get_documentElement()
    if root is None or root is pyv8.Null:
        return []
    boxes, _ = layout_snapshot(document)
    found = boxes.elements_at(x, y) if boxes else []
    if not any(node is root for node in found):
        # Chromium reports the root for every point in the viewport, whatever
        # its content box measured.
        found.append(root)
    if not deep:
        retargeted = []
        for node in found:
            tree = node.root_node()
            while getattr(tree, '_host', None) is not None:
                node = tree._host
                tree = node.root_node()
            if node not in retargeted:
                retargeted.append(node)
        return retargeted
    return found


def element_from_point(document, x, y):
    """The topmost element under a point, or ``None`` when the point is outside."""
    stack = elements_from_point(document, x, y)
    return stack[0] if stack else None


def deep_element_from_point(document, x, y):
    """The internal event target before document-level shadow retargeting."""
    stack = elements_from_point(document, x, y, deep=True)
    return stack[0] if stack else None

__all__ = [
    "element_from_point", "elements_from_point",
    "viewport_size",
]
