"""Base HTML element interfaces and the simple element class factory."""
import re
import math

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import boolean, dom_string, integer, is_nullish
from ..page_errors import dom_exception
from ..dom.character_data import Text
from ..dom.collections import DOMStringMap
from ..css.cssom import CSSStyleDeclaration
from ..css.typed import StylePropertyMap
from ..dom.element import Element
from . import attributes as html_props


# HTML WindowEventHandlers plus the six Window-reflecting body handlers.
_WINDOW_BODY_HANDLERS = frozenset((
    'onafterprint', 'onbeforeprint', 'onbeforeunload', 'onhashchange',
    'onlanguagechange', 'onmessage', 'onmessageerror', 'onoffline', 'ononline',
    'onpageswap', 'onpagehide', 'onpagereveal', 'onpageshow', 'onpopstate',
    'onrejectionhandled', 'onstorage', 'onunhandledrejection', 'onunload',
    'onblur', 'onerror', 'onfocus', 'onload', 'onresize', 'onscroll',
))

def _unrendered(node):
    """Whether a descendant is laid out away, taking its subtree out of innerText.

    Reading ``innerText`` on such an element itself still reports its text, the way
    Chrome falls back to the text content for something it does not render.
    """
    from ..css.cascade import computed_style

    if not isinstance(node, Element):
        return False
    name = str(getattr(node, "localName", "") or "").lower()
    if name in ("script", "style", "link", "meta", "head", "template", "title", "noscript"):
        return True
    if node.attributes.get("hidden") is not None:
        return True
    return str(computed_style(node).get("display", "")) == "none"

@interface('HTMLElement', 'Element')
class HTMLElement(Element):
    #: The attributes this interface reflects in a shape of its own, from a table measured on
    #: Chrome; see web.html.attributes for what each shape does. Reading them changes nothing.
    __idl_overrides__ = {name: {"side_effect": 1} for name in (
        "accessKey", "autocapitalize", "autocorrect", "autofocus", "dir", "draggable",
        "editContext", "enterKeyHint", "focusGroup", "focusGroupStart", "inert", "inputMode",
        "lang", "outerText", "popover", "spellcheck", "translate", "virtualKeyboardPolicy",
        "writingSuggestions",
    )}

    #: The tags whose boundaries innerText turns into newlines. Chrome decides this
    #: from the rendered display, which this tree does not have, so the block-level
    #: HTML elements stand in for it.
    _BLOCK_LEVEL = frozenset((
        "address", "article", "aside", "blockquote", "details", "dialog", "dd", "div",
        "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "li", "main", "nav", "ol",
        "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
        "ul"))

    #: What the contenteditable attribute reads back as. An empty attribute and
    #: "true" both mean true, and anything the IDL enum does not hold -- an unknown
    #: keyword or no attribute at all -- reads as "inherit".
    _EDITABLE_VALUES = {"": "true", "true": "true", "false": "false",
                        "plaintext-only": "plaintext-only", "inherit": "inherit"}

    def _handler_target(self, name):
        if self.localName in ('body', 'frameset') and name in _WINDOW_BODY_HANDLERS:
            document = self.ownerDocument
            return document._browsing_window() if document is not None else None
        return self

    def _attribute_changed_here(self, name, old, new):
        super()._attribute_changed_here(name, old, new)
        if self.localName in ('body', 'frameset') and name in _WINDOW_BODY_HANDLERS:
            if new is None:
                self._set_handler(name, pyv8.Null)
            else:
                self._services.document_services.install_inline_handler(self, name, new)

    def get_contentEditable(self):
        value = self.attributes.get("contenteditable")
        if value is None:
            return "inherit"
        return self._EDITABLE_VALUES.get(str(value).lower(), "inherit")

    def set_contentEditable(self, value):
        """Set the keyword, or refuse what the IDL enum does not hold.

        Chrome throws a SyntaxError for anything but the four keywords, writes the
        keyword in lower case, and takes the attribute away for "inherit".
        """
        text = str(dom_string(value))
        keyword = text.lower()
        if keyword not in ("true", "false", "plaintext-only", "inherit"):
            raise dom_exception(
                "SyntaxError",
                "Failed to set the 'contentEditable' property on 'HTMLElement': The value"
                f" provided ('{text}') is not one of 'true', 'false', 'plaintext-only', or"
                " 'inherit'.")
        if keyword == "inherit":
            self.attributes.pop("contenteditable", None)
        else:
            self.attributes["contenteditable"] = keyword

    def get_isContentEditable(self):
        """Whether this element is editable: the document's design mode, or the
        nearest contenteditable at or above it. A detached tree is included, as in
        Chrome, while design mode reaches only what is in a document.
        """
        document = self.ownerDocument
        if self.isConnected and getattr(document, "design_mode", "off") == "on":
            return True
        node = self
        while node is not None:
            if isinstance(node, HTMLElement):
                value = node.attributes.get("contenteditable")
                if value is not None:
                    keyword = self._EDITABLE_VALUES.get(str(value).lower(), "inherit")
                    if keyword != "inherit":
                        return keyword != "false"
            node = node.parentNode
        return False

    def get_innerText(self):
        """The text as it would be shown: whitespace collapsed, a block boundary and a
        ``br`` written as newlines, and the ends trimmed -- except in ``pre`` and
        ``textarea``, where the spaces at either end are content rather than layout.
        """
        text = self._rendered_text(self, self.isConnected)
        if str(getattr(self, "localName", "") or "").lower() in ("pre", "textarea"):
            return text
        return text.strip()

    def _rendered_text(self, node, laid_out):
        """The text of *node* and its descendants as it would be shown.

        Chrome decides the line breaks from what it rendered, so a block boundary
        only becomes a newline once the element is in a document: a tree nobody
        attached reads as one run of text, which is what a detached element's
        innerText does in Chrome too. Whitespace collapses unless the element keeps
        it, as ``pre`` and ``textarea`` do. What the page lays out away -- a script
        or style element, a hidden one, anything ``display: none`` reaches -- takes
        its whole subtree out of the result.
        """
        if isinstance(node, Text):
            return node.get_data()
        name = str(getattr(node, "localName", "") or "").lower()
        if name == "br":
            return "\n" if laid_out else ""
        joined = "".join(self._rendered_text(child, laid_out)
                         for child in getattr(node, "_children", ())
                         if not (laid_out and _unrendered(child)))
        if laid_out and name in self._BLOCK_LEVEL:
            joined = "\n" + joined + "\n"
        if name in ("pre", "textarea"):
            return joined
        collapsed = re.sub(r"[ \t\r\f\v]+", " ", joined)
        return re.sub(r" *\n *", "\n", collapsed)

    def set_innerText(self, value):
        """Replace the children with the text, one line break at a time.

        The text is taken as written -- markup in it is not parsed -- and a newline
        becomes a ``br`` rather than a character in the text, which is what makes the
        setter and the getter round-trip. Null clears the element.
        """
        from .registry import class_for_tag

        self._replace_children(())
        if is_nullish(value):
            return
        for index, line in enumerate(re.split(r"\r\n|\r|\n", str(dom_string(value)))):
            if index:
                self.append_child(class_for_tag("br")("br", services=self._services))
            if line:
                self.append_child(Text(line, services=self._services))

    def get_nonce(self): return self._attr("nonce")
    def set_nonce(self, value): self._set_string("nonce", value)
    def get_title(self): return self.attributes.get("title", "")
    def set_title(self, v):
        v = dom_string(v)
        self.attributes['title'] = str(v)
    def get_hidden(self): return "hidden" in self.attributes
    def set_hidden(self, v):
        v = boolean(v)
        if v: self.attributes["hidden"] = ""
        else: self.attributes.pop("hidden", None)
    def _attr(self, name):
        """The value of a content attribute as a string, ``""`` when absent."""
        value = self.attributes.get(name)
        return "" if value is None else str(value)
    def _set_string(self, name, value):
        self.attributes[name] = str(dom_string(value))
    def _bool(self, name): return name in self.attributes
    def _set_bool(self, name, value):
        if boolean(value): self.attributes[name] = ""
        else: self.attributes.pop(name, None)
    def set_style(self, value):
        """``style`` is ``[PutForwards=cssText]``: an assignment lands on the declaration."""
        self.get_style().set_cssText(dom_string(value))
    def get_style(self):
        value = getattr(self, "_style_declaration", None)
        if value is None:
            value = self._style_declaration = CSSStyleDeclaration(element=self)
        return value
    def get_attributeStyleMap(self):
        value = getattr(self, "_attribute_style_map", None)
        if value is None:
            value = self._attribute_style_map = StylePropertyMap(element=self)
        return value
    def get_dataset(self):
        value = getattr(self, "_dataset_map", None)
        if value is None:
            value = self._dataset_map = DOMStringMap(self)
        return value
    def get_offsetParent(self):
        from ..dom.layout import offset_parent
        return offset_parent(self) or pyv8.Null
    def _offset_box(self):
        """This element's box, and where its ``offsetParent`` measures from."""
        from ..._native._native import EMPTY_BOX
        from ..dom.layout import layout_snapshot

        boxes, _ = layout_snapshot(self.ownerDocument)
        box = boxes.get(self, EMPTY_BOX)
        parent = boxes.offset_parent(self) if boxes else None
        parent_box = boxes.get(parent, EMPTY_BOX)
        return box, parent_box.padding_box_left, parent_box.padding_box_top

    def get_offsetTop(self):
        box, _left, top = self._offset_box()
        return int(round(box.top - top))
    def get_offsetLeft(self):
        box, left, _top = self._offset_box()
        return int(round(box.left - left))
    def get_offsetWidth(self):
        from ..dom.layout import layout
        return math.floor(layout(self).width + 0.5)
    def get_offsetHeight(self):
        from ..dom.layout import layout
        return math.floor(layout(self).height + 0.5)

    def get_tabIndex(self):
        """The element's tab index: the attribute when present, else Chromium's default
        -- 0 for a focusable area, -1 otherwise; an unparseable value reads as 0."""
        from ..dom.focus import focusable
        value = self.attributes.get("tabindex")
        if value is None:
            return 0 if focusable(self) else -1
        digits = str(value).strip().lstrip("+-")
        return int(digits) if digits.isdigit() else 0

    def set_tabIndex(self, value):
        self.attributes["tabindex"] = str(integer(value, signed=True))

    def fn_focus(self, *_extra):
        """Focus this element when it is a focusable area; Chrome ignores the call on
        anything else, so the check lives in :func:`dom.focus.focus`."""
        from ..dom.focus import focus
        focus(self)

    def fn_blur(self, *_extra):
        """Drop focus, when this element is the one holding it."""
        from ..dom.focus import blur
        document = self.ownerDocument
        if document is not None and getattr(document, "_focused_element", None) is self:
            blur(document)

    def fn_click(self, *_extra):
        """Run the synthetic click activation steps a script asked for: an untrusted
        ``click`` at the origin, then the behavior unless a listener cancelled it."""
        from ..dom.activation import click_element
        click_element(self)


#: The accessors for the reflected attributes above; ``html_props`` gives the class one
#: getter/setter pair per member, and the convention binds them to the members it states.
html_props.install(HTMLElement)


@interface('HTMLUnknownElement', 'HTMLElement')
class HTMLUnknownElement(HTMLElement):
    """The interface Chromium exposes for tags with no dedicated class: obsolete
    tags, custom element names and anything else unmodelled.  ``allow_new=false``,
    so the constructor throws; the host still builds instances through ``__init__``.
    """


def html_class(name):
    return interface(name, "HTMLElement")(type(name, (HTMLElement,), {"__module__": __name__}))
