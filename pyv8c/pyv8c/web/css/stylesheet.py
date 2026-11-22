"""Style sheets and their document-owned collections."""
from urllib.parse import urljoin
from pyv8c import pyv8
from ...idl import interface
from ...foundation.arguments import MISSING, boolean, dom_string, integer, missing_error
from ..page_errors import IndexSizeError
from ..dom.mixins import LegacyNamedPropertyMixin, ReadOnlyIndexedPropertyMixin


def stylesheet_source(node):
    """The applied CSS text, including edits made through the owner's CSSOM."""
    sheet = node.__dict__.get('_style_sheet')
    if sheet is not None and sheet._modified:
        return '\n'.join(rule.get_cssText() for rule in sheet.get_cssRules()._rules)
    return _stylesheet_markup(node)


def _stylesheet_markup(node):
    """The original CSS text an owner element carries.

    A ``<style>`` holds its own text; a ``<link rel=stylesheet>`` holds what the
    fetch stored on it, which is nothing until the load settles.
    """
    name = str(getattr(node, "localName", "") or "").lower()
    if name == "link":
        rel = str(getattr(node, "attributes", {}).get("rel", "")).lower().split()
        if "stylesheet" not in rel:
            return None
        return getattr(node, "_stylesheet_text", "") or ""
    if name == "style":
        # The element's text is authoritative; ``_raw_text`` only covers a style the
        # parser never closed (see ``parser._close_element``).
        return node.get_textContent() or getattr(node, "_raw_text", "")
    return None


def _inline_owner(node):
    """Whether *node* carries its CSS text rather than loading it from a URL."""
    return str(getattr(node, "localName", "") or "").lower() == "style"


@interface('StyleSheet', None)
class StyleSheet:
    """One stylesheet as a page sees it: where it came from, and what it is for."""

    def __init__(self, owner=None, document=None, **_):
        self._owner, self._document = owner, document

    def get_type(self): return "text/css"
    def get_href(self):
        """Where a linked sheet came from; a ``<style>`` element has no URL."""
        if self._owner is None or str(getattr(self._owner, "localName", "")).lower() != "link":
            return pyv8.Null
        href = self._owner.attributes.get("href")
        if href is None:
            return pyv8.Null
        return urljoin(getattr(self._document, "url", "") or "", str(href))
    def get_title(self):
        title = None if self._owner is None else self._owner.attributes.get("title")
        return pyv8.Null if title is None else str(title)
    def get_disabled(self):
        return bool(self._owner is not None and "disabled" in self._owner.attributes)
    def set_disabled(self, value):
        if self._owner is None:
            return
        if boolean(value):
            self._owner.attributes["disabled"] = ""
        else:
            self._owner.attributes.pop("disabled", None)
    def get_ownerNode(self):
        return pyv8.Null if self._owner is None else self._owner
    def get_parentStyleSheet(self):
        return pyv8.Null


@interface('CSSStyleSheet', 'StyleSheet')
class CSSStyleSheet(StyleSheet):
    """A sheet of CSS rules: the owner it came from, and the rules themselves."""

    def __init__(self, owner=None, document=None, **_):
        super().__init__(owner, document)
        #: An inline sheet is bound to the markup it was made from.  Rewriting the
        #: owner's text gives the owner a new sheet, and this one keeps answering the
        #: rules it already parsed (see ``_detach``).
        self._source = (_stylesheet_markup(owner)
                        if owner is not None and _inline_owner(owner) else None)
        self._detached = False
        self._modified = False

    def _owner_sheet(self): return self

    def _detach(self):
        """Cut the sheet from its owner, keeping the rules it already answers.

        Chrome detaches the original sheet when its owner's text is replaced or the
        owner leaves the document: ``ownerNode`` reads null, the rules stay, and the
        element answers a different sheet the next time one is asked for.
        """
        if self._detached:
            return
        if self._source is None:
            owner = getattr(self, "_owner", None)
            text = "" if owner is None else _stylesheet_markup(owner)
            self._source = "" if text is None else text
        self.get_cssRules()
        self._owner, self._document = None, None
        self._detached = True

    def _text(self):
        if self._source is not None:
            return self._source
        owner = getattr(self, "_owner", None)
        text = "" if owner is None else _stylesheet_markup(owner)
        return "" if text is None else text

    def rules_changed(self):
        """Publish a CSSOM edit to this sheet's owning tree."""
        self._modified = True
        if self._document is not None:
            self._document.style_version += 1

    def get_cssRules(self):
        """The sheet's rules, read from the text the sheet was built from.

        A rule the page inserted survives until the sheet's own text changes -- at
        which point the owner answers a new sheet (see ``_detach``), so a rule added
        here stays with this object.
        """
        from .rules import CSSRuleList, build_rules
        text = self._text()
        if getattr(self, "_rule_text", None) != text:
            self._rule_text = text
            self._rules = build_rules(text, self)
        return CSSRuleList(self._rules)

    def fn_insertRule(self, rule=MISSING, index=0, *_extra):
        from .rules import insert_rule
        if rule is MISSING:
            raise missing_error('CSSStyleSheet', 'insertRule', 2, 0, constructor=False)
        return insert_rule(self, str(dom_string(rule)), integer(index))

    def fn_deleteRule(self, index=MISSING, *_extra):
        from .rules import RANGE_MESSAGE
        if index is MISSING:
            raise missing_error('CSSStyleSheet', 'deleteRule', 1, 0, constructor=False)
        rules = self.get_cssRules()._rules
        position = integer(index)
        if not 0 <= position < len(rules):
            raise IndexSizeError(RANGE_MESSAGE)
        del rules[position]
        self.rules_changed()
        return None


@interface('StyleSheetList', None)
class StyleSheetList(ReadOnlyIndexedPropertyMixin, LegacyNamedPropertyMixin):
    """The document's sheets, live: ``<style>`` elements and the ``<link>`` elements
    that are stylesheets, in document order."""

    def __init__(self, document=None, root=None, **_):
        self._document = document
        self._root = document if root is None else root

    def _owners(self):
        owners = []
        for node in self._root._descendants():
            name = str(getattr(node, "localName", "") or "").lower()
            rel = str(getattr(node, "attributes", {}).get("rel", "")).lower().split()
            if name == "style" or (name == "link" and "stylesheet" in rel):
                #: A shadow root keeps its tree while its host is elsewhere; the list
                #: only holds sheets whose owner hangs off a document.
                if getattr(node, "isConnected", True):
                    owners.append(node)
        return owners
    def _sheet_for(self, node):
        """The sheet for one owner, kept on the node so a second read is the same
        object, as it is in Chrome.
        """
        sheet = getattr(node, "_style_sheet", None)
        if sheet is None:
            sheet = node._style_sheet = CSSStyleSheet(node, self._document)
        return sheet
    def get_length(self): return len(self._owners())
    def _indexed_items(self): return [self._sheet_for(node) for node in self._owners()]
    def fn_item(self, index=MISSING, *_extra):
        if index is MISSING:
            raise missing_error('StyleSheetList', 'item', 1, 0, constructor=False)
        items = self._indexed_items()
        position = int(index)
        return items[position] if 0 <= position < len(items) else pyv8.Null
    def _supported_names(self):
        return [str(node.attributes["id"]) for node in self._owners()
                if node.attributes.get("id")]
    def _named_item(self, name):
        for node in self._owners():
            if node.attributes.get("id") == name:
                return self._sheet_for(node)
        return pyv8.Null

