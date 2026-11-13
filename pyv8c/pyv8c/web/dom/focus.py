"""Which element has focus, and the events that moving it fires.

Focusable: a non-disabled form control (not ``type=hidden``), an ``a``/``area``
with ``href``, an ``iframe``, or anything carrying ``tabindex``.  A click
focuses the nearest focusable ancestor, else clears focus, and does so on
``mousedown``.  ``activeElement`` is the body before anything was focused.
Measured on Chrome 153.
"""

from pyv8c import pyv8

from .events import FocusEvent


#: Local names that take focus without needing a ``tabindex``.
_FOCUSABLE_TAGS = frozenset(("button", "iframe", "select", "textarea"))


#: Local names whose ``href`` makes them focusable.
_LINK_TAGS = frozenset(("a", "area"))


def _is_form_control(element):
    return getattr(element, "localName", "") in ("button", "input", "select", "textarea")


def _disabled(element):
    return _is_form_control(element) and "disabled" in getattr(element, "attributes", {})


def focusable(element):
    """Whether ``focus()`` does anything on *element*, as Chrome decides it.

    A disconnected element is not focusable, and a disabled control is inert
    even when it carries ``tabindex``.
    """
    if element is None or element is pyv8.Null or not getattr(element, "isConnected", False):
        return False
    if _disabled(element):
        return False
    attributes = getattr(element, "attributes", {})
    if "tabindex" in attributes:
        return True
    tag = getattr(element, "localName", "")
    if tag == "input":
        return str(element.get_type()) != "hidden"
    if tag in _FOCUSABLE_TAGS:
        return True
    if tag in _LINK_TAGS:
        return bool(attributes.get("href"))
    return False


def active_element(document):
    """What ``document.activeElement`` reports: the focused element while it is
    connected, else the body, the root, or ``null``."""
    focused = getattr(document, "_focused_element", None)
    if focused is not None and getattr(focused, "isConnected", False):
        return focused
    for fallback in (document.get_body(), document.get_documentElement()):
        if fallback is not None and fallback is not pyv8.Null:
            return fallback
    return pyv8.Null


def _fire(target, event_type, *, bubbles, related):
    #: Focus events cross shadow boundaries in Chrome, which the two-sided probe measured
    #: (``composed`` true for ``focusin``, and for ``focus``/``blur`` too).
    event = FocusEvent(event_type, {"bubbles": bubbles, "cancelable": False,
                                    "composed": True})
    if related is not None and related is not pyv8.Null:
        event.relatedTarget = related
    event._trusted = True
    target._dispatch_event(event)


def _move_focus(document, element):
    """Give *document* a new focused element, firing the transition.

    ``blur``/``focusout`` go to the element losing focus and ``focus``/
    ``focusin`` to the one taking it; with nothing previously focused, only the
    second pair fires.
    """
    if element is not None:
        window = getattr(getattr(document, '_services', None), 'window', None)
        frame = getattr(window, '_frame_element', None)
        if frame is not None:
            _move_focus(frame.ownerDocument, frame)
    previous = getattr(document, "_focused_element", None)
    if previous is element:
        return True
    document._focused_element = element
    if previous is not None:
        _fire(previous, "blur", bubbles=False, related=element)
        _fire(previous, "focusout", bubbles=True, related=element)
    if element is not None:
        _fire(element, "focus", bubbles=False, related=previous)
        _fire(element, "focusin", bubbles=True, related=previous)
    return True


def focus(element):
    """Focus *element* if it is a focusable area; returns whether it took focus."""
    document = getattr(element, "ownerDocument", None)
    if document is None or not focusable(element):
        return False
    visible = getattr(element, 'fn_checkVisibility', None)
    if visible is not None and not visible({'visibilityProperty': True}):
        return False
    return _move_focus(document, element)


def blur(document):
    """Clear the document's focus, as ``blur()`` on the focused element would."""
    if getattr(document, "_focused_element", None) is None:
        return False
    return _move_focus(document, None)


def focus_on_mousedown(node):
    """The focus a press lands on: the nearest focusable ancestor at or above
    *node*, else nothing (moving focus off whatever had it)."""
    document = getattr(node, "ownerDocument", None)
    if document is None:
        return False
    candidate = node
    while candidate is not None and candidate is not pyv8.Null:
        if focusable(candidate):
            return _move_focus(document, candidate)
        candidate = getattr(candidate, "parentElement", None)
    return _move_focus(document, None)

__all__ = ["active_element", "blur", "focus", "focus_on_mousedown", "focusable"]
