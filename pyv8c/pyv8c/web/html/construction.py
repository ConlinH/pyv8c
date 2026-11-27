"""Native construction of ordinary parser runs, between observable boundaries."""

from html import unescape
from types import MethodType

from ..._native import dom
from ..dom.character_data import Text
from ..dom.element import Element, _AttributeMap
from ..dom.events import EVENT_TARGET_SETATTR, EventTarget
from ..dom.nodes import Node, ShadowRoot, _ChildList
from ..dom.query_cache import coalesce_invalidations
from ..dom.utils import XHTML_NAMESPACE

# Nodes with parser/resource/namespace/handler work remain explicit parser steps.
_BOUNDARIES = frozenset(
    {
        "html",
        "head",
        "body",
        "script",
        "style",
        "title",
        "textarea",
        "noscript",
        "template",
        "svg",
        "math",
        "iframe",
        "img",
        "link",
        "audio",
        "video",
        "source",
        "meta",
        "base",
        "basefont",
        "bgsound",
        "noframes",
    }
)
_ELEMENT_INIT = Element.__init__
_TEXT_INIT = Text.__init__
_APPEND = Node.append_child
_ADOPT = Node._adopt_child
_TREE_METHODS = (_APPEND, _ADOPT, MethodType)
_CONTEXT_BOUNDARIES = _BOUNDARIES - {"body"}
_FRAGMENT_INITIALIZERS = (_AttributeMap.__init__, Node.__init__, EventTarget.__init__, _ChildList.append)


def build_batch(parser):
    """Return the first unconsumed token; special constructs stay with the parser.

    Construction still calls the regular Python classes. The native kernel owns
    only ordinary token dispatch and fresh-node insertion, with the same cache
    invalidation on each insertion. It never batches across script/resource work.
    """
    current = parser.current
    document_services = parser.services.document_services
    if (
        not parser.building
        or parser.mode not in ("in-body", "after-body")
        or not document_services.parsing
        or getattr(current, "_namespace", None) != XHTML_NAMESPACE
        or current.localName in _CONTEXT_BOUNDARIES
        or type(current).append_child is not _APPEND
        or Node._adopt_child is not _ADOPT
        or Text.__init__ is not _TEXT_INIT
    ):
        return parser.index
    has_standard_factory = getattr(document_services, "has_standard_element_factory", None)
    if has_standard_factory is None or not has_standard_factory():
        return parser.index
    document_services.check_parser_operation()
    return _build(parser, parser.tokens, _ordinary_classes())


def _ordinary_classes():
    from .registry import TAG_CLASSES

    return {
        tag: cls
        for tag, cls in TAG_CLASSES.items()
        if tag not in _BOUNDARIES
        and cls.__init__ is _ELEMENT_INIT
        and cls.__new__ is object.__new__
        and cls._on_tree_inserted is None
        and cls._shadow_root is None
        and cls.__setattr__ is EVENT_TARGET_SETATTR
        and cls.append_child is _APPEND
        and cls._adopt_child is _ADOPT
    }


def fragment_classes(parent, services):
    document_services = getattr(services, "document_services", None)
    can_batch = getattr(document_services, "can_batch_fragment", None)
    if (
        can_batch is None
        or parent._services is not services
        or parent.ownerDocument is not document_services.document
        or not (type(parent) is ShadowRoot or (
            getattr(parent, "_namespace", None) == XHTML_NAMESPACE
            and parent.localName not in _CONTEXT_BOUNDARIES
        ))
        or type(parent).append_child is not _APPEND
        or Node._adopt_child is not _ADOPT
        or Text.__init__ is not _TEXT_INIT
        or (_AttributeMap.__init__, Node.__init__, EventTarget.__init__, _ChildList.append)
           != _FRAGMENT_INITIALIZERS
        or not can_batch()
    ):
        return None
    return _ordinary_classes()


def build_fragment_batch(parser, tokens, classes):
    parser.services.document_services.check_parser_operation()
    return _build(parser, tokens, classes, fragment=True)


def build_fragment(parser, html, offset):
    parser.services.document_services.check_parser_operation()
    with coalesce_invalidations():
        return dom.build_fragment(
            html, offset, parser.stack, parser.services, parser.classes,
            parser.void, parser.auto_close, Text, _ELEMENT_INIT, _TREE_METHODS,
        )


def _build(parser, tokens, classes, *, fragment=False):
    return dom.build_batch(
        tokens,
        parser.index,
        parser.stack,
        parser.services,
        classes,
        parser.void,
        parser.auto_close,
        Text,
        unescape,
        _ELEMENT_INIT,
        _TREE_METHODS,
        parser,
        fragment,
    )
