"""Retain computed styles across attribute edits while discarding affected nodes."""

from .cascade import rules_for


def retain_after_attribute_edit(document, element, old_version, layout, partial):
    """Return styles still valid after a scalar edit in the current DOM tree."""
    version = (old_version, document.style_version)
    styles = {}
    if layout is not None and layout.version[:2] == version:
        styles.update(layout.styles)
    if partial is not None and partial[0] == version:
        styles.update(partial[1])
    if not styles:
        return None

    # Ancestor matching and inherited values can propagate through an entire
    # subtree, including descendants of a shadow host. Sibling combinators
    # can also affect later siblings and whatever inherits from them.
    pending = [element]
    parent = element.parentNode
    if parent is not None and rules_for(element).has_sibling_dependencies:
        for sibling in reversed(parent._children):
            if sibling is element:
                break
            pending.append(sibling)
    while pending and styles:
        node = pending.pop()
        styles.pop(node, None)
        pending.extend(node._children)
        shadow = getattr(node, '_shadow_root', None)
        if shadow is not None:
            pending.append(shadow)
    return ((old_version + 1, version[1]), styles) if styles else None
