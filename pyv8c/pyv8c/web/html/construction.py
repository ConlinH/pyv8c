"""Native construction of ordinary parser runs, between observable boundaries."""

from html import unescape
from types import MethodType

from ..._native import dom
from ..dom.character_data import Text
from ..dom.element import Element
from ..dom.events import EVENT_TARGET_SETATTR
from ..dom.nodes import Node
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


def build_batch(parser):
    """Return the first unconsumed token; special constructs stay with the parser.

    Construction still calls the regular Python classes. The native kernel owns
    only ordinary token dispatch and fresh-node insertion, with the same cache
    invalidation on each insertion. It never batches across script/resource work.
    """
    current = parser.current
    if (
        not parser.building
        or parser.mode not in ("in-body", "after-body")
        or not parser.services.document_services.parsing
        or getattr(current, "_namespace", None) != XHTML_NAMESPACE
        or current.localName in _CONTEXT_BOUNDARIES
        or type(current).append_child is not _APPEND
        or Node._adopt_child is not _ADOPT
        or Text.__init__ is not _TEXT_INIT
    ):
        return parser.index
    from .registry import TAG_CLASSES

    parser.services.document_services.check_parser_operation()
    classes = {
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
    return dom.build_batch(
        parser.tokens,
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
    )
