"""The HTML local-name to interface-class registry.  It sits above the element
modules, which only need the node model below."""

from .canvas import HTMLCanvasElement
from .elements import HTMLElement, HTMLUnknownElement, html_class
from .controls import HTMLButtonElement, HTMLLabelElement
from .table import HTMLTableElement, HTMLTableRowElement, HTMLTableCellElement
from .image import HTMLImageElement
from .links import HTMLAnchorElement, HTMLLinkElement
from .document_elements import HTMLMetaElement, HTMLScriptElement, HTMLStyleElement, HTMLTemplateElement
from .forms import HTMLFormElement
from .iframe import HTMLIFrameElement
from .inputs import (
    HTMLInputElement,
    HTMLOptionElement,
    HTMLSelectElement,
    HTMLTextAreaElement,
)
from .media import HTMLAudioElement, HTMLMediaElement, HTMLVideoElement
from .tag_interfaces import TAG_INTERFACES


#: IdlInterface name -> implementation class, for every modelled interface.
HTML_CLASSES = {
    **{name: html_class(name) for name in (
        "HTMLHtmlElement", "HTMLHeadElement", "HTMLBodyElement", "HTMLDivElement",
        "HTMLSpanElement")},
    #: The interfaces the measured tag table asks for that have nothing hand-written.
    **{name: html_class(name) for name in (
        "HTMLAreaElement", "HTMLBRElement", "HTMLBaseElement", "HTMLDListElement",
        "HTMLDataElement", "HTMLDataListElement", "HTMLDetailsElement", "HTMLDialogElement",
        "HTMLDirectoryElement", "HTMLEmbedElement", "HTMLFieldSetElement", "HTMLFontElement",
        "HTMLFrameElement", "HTMLFrameSetElement", "HTMLHRElement", "HTMLHeadingElement",
        "HTMLLIElement", "HTMLLegendElement", "HTMLMapElement", "HTMLMarqueeElement",
        "HTMLMenuElement", "HTMLMeterElement", "HTMLModElement", "HTMLOListElement",
        "HTMLObjectElement", "HTMLOptGroupElement", "HTMLOutputElement",
        "HTMLParagraphElement", "HTMLParamElement", "HTMLPictureElement", "HTMLPreElement",
        "HTMLProgressElement", "HTMLQuoteElement", "HTMLSlotElement", "HTMLSourceElement",
        "HTMLTableCaptionElement", "HTMLTableColElement", "HTMLTableSectionElement",
        "HTMLTimeElement", "HTMLTitleElement", "HTMLTrackElement", "HTMLUListElement")},
    "HTMLElement": HTMLElement,
    # Interfaces with members of their own carry a hand-written class.
    "HTMLFormElement": HTMLFormElement,
    "HTMLButtonElement": HTMLButtonElement,
    "HTMLLabelElement": HTMLLabelElement,
    "HTMLMediaElement": HTMLMediaElement,
    "HTMLAudioElement": HTMLAudioElement,
    "HTMLVideoElement": HTMLVideoElement,
    "HTMLTableElement": HTMLTableElement,
    "HTMLTableRowElement": HTMLTableRowElement,
    "HTMLTableCellElement": HTMLTableCellElement,
    "HTMLAnchorElement": HTMLAnchorElement,
    "HTMLTextAreaElement": HTMLTextAreaElement,
    "HTMLOptionElement": HTMLOptionElement,
    "HTMLSelectElement": HTMLSelectElement,
    "HTMLUnknownElement": HTMLUnknownElement,
    "HTMLImageElement": HTMLImageElement, "HTMLScriptElement": HTMLScriptElement, "HTMLCanvasElement": HTMLCanvasElement,
    "HTMLLinkElement": HTMLLinkElement, "HTMLStyleElement": HTMLStyleElement,
    "HTMLMetaElement": HTMLMetaElement, "HTMLTemplateElement": HTMLTemplateElement,
    "HTMLIFrameElement": HTMLIFrameElement, "HTMLInputElement": HTMLInputElement,
    # Chromium also resolves the historical lowercase spelling.
    "HTMLIframeElement": HTMLIFrameElement,
}


#: Local name -> implementation class, from the measured tag table.
TAG_CLASSES = {tag: HTML_CLASSES[name]
               for name, tags in TAG_INTERFACES.items() for tag in tags}


def _unknown_or_custom(name):
    """What Chrome answers a name it has no interface for.

    A hyphenated name is a valid custom element name, so an undefined one is a plain
    ``HTMLElement``; anything else is the :class:`HTMLUnknownElement` it exposes.
    """
    return HTML_CLASSES["HTMLElement" if "-" in name else "HTMLUnknownElement"]


def class_for_tag(tag):
    """The implementation class for an HTML local name."""
    name = str(tag).lower()
    return TAG_CLASSES.get(name) or _unknown_or_custom(name)


_DEFAULT_CLASS_FOR_TAG = class_for_tag


def uses_standard_factory():
    return class_for_tag is _DEFAULT_CLASS_FOR_TAG


def class_for_local_name(local_name):
    """The class for a local name *as written*: ``createElementNS`` does not lower-case
    the name it was given, so ``DIV`` is an unknown element to it.
    """
    name = str(local_name)
    return TAG_CLASSES.get(name) or _unknown_or_custom(name)


__all__ = ["HTML_CLASSES", "TAG_CLASSES", "class_for_local_name", "class_for_tag"]


