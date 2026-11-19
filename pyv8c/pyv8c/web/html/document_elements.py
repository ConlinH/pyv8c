"""HTML document resource and template elements."""
from pyv8c import pyv8
from ...idl import interface
from ...foundation.arguments import boolean, dom_string
from ..dom.nodes import DocumentFragment
from .utils import url_value, set_url_value
from .elements import HTMLElement


@interface('HTMLScriptElement', 'HTMLElement')
class HTMLScriptElement(HTMLElement):
    __idl_overrides__ = {
        "text": {
            "getter": "get_textContent",
            "setter": "set_textContent"
        }
    }
    def get_src(self): return url_value(self, "src")
    def set_src(self, value):
        from ..bom.trusted_types import trusted_string
        set_url_value(self, 'src', trusted_string(self._services, value, 'ScriptURL', 'HTMLScriptElement src'))
    def get_type(self): return self.attributes.get("type", "")
    def set_type(self, value):
        value = dom_string(value)
        self.attributes['type'] = str(value)
    def get_charset(self): return self.attributes.get("charset", "")
    def set_charset(self, value): self.attributes['charset'] = str(dom_string(value))
    def get_async(self): return "async" in self.attributes
    def set_async(self, value):
        value = boolean(value)
        if value: self.attributes["async"] = ""
        else: self.attributes.pop("async", None)
    def get_defer(self): return "defer" in self.attributes
    def set_defer(self, value):
        value = boolean(value)
        if value: self.attributes["defer"] = ""
        else: self.attributes.pop("defer", None)

    def _clone_shallow(self):
        clone = super()._clone_shallow()
        # HTML script cloning carries its already executed state to the clone.
        clone._script_executed = getattr(self, "_script_executed", False)
        return clone


@interface('HTMLStyleElement', 'HTMLElement')
class HTMLStyleElement(HTMLElement):
    def get_sheet(self):
        """The sheet this element owns, which is the one the list of its own tree holds
        for it -- the document's, or the shadow root's -- and null while nothing holds
        one: a sheet belongs to a tree that is in a document."""
        sheets_for = getattr(self.root_node(), "get_styleSheets", None)
        if sheets_for is None:
            return pyv8.Null
        sheets = sheets_for()
        for owner in sheets._owners():
            if owner is self:
                return sheets._sheet_for(owner)
        return pyv8.Null
    def get_media(self): return self.attributes.get("media", "")
    def set_media(self, value):
        value = dom_string(value)
        self.attributes['media'] = str(value)
    def get_type(self):
        """Plain reflection, not a filtered one: whatever the page wrote reads back
        as it was written, and an absent attribute is the empty string."""
        return str(self.attributes.get("type", ""))
    def set_type(self, value):
        self.attributes["type"] = str(dom_string(value))


@interface('HTMLMetaElement', 'HTMLElement')
class HTMLMetaElement(HTMLElement):
    """The HTML ``meta`` element's reflected string attributes."""

    def get_name(self): return self.attributes.get("name", "")
    def set_name(self, value):
        value = dom_string(value)
        self.attributes['name'] = dom_string(value)
    def get_httpEquiv(self): return self.attributes.get("http-equiv", "")
    def set_httpEquiv(self, value):
        value = dom_string(value)
        self.attributes['http-equiv'] = dom_string(value)
    def get_content(self): return self.attributes.get("content", "")
    def set_content(self, value):
        value = dom_string(value)
        self.attributes['content'] = dom_string(value)
    def get_media(self): return self.attributes.get("media", "")
    def set_media(self, value):
        value = dom_string(value)
        self.attributes['media'] = dom_string(value)
    def get_scheme(self): return self.attributes.get("scheme", "")
    def set_scheme(self, value):
        value = dom_string(value)
        self.attributes['scheme'] = dom_string(value)


@interface('HTMLTemplateElement', 'HTMLElement')
class HTMLTemplateElement(HTMLElement):
    def __init__(self, tag_name="template", services=None, **kw):
        super().__init__(tag_name, services=services, **kw)
        self.content = DocumentFragment(services=services)
    def get_content(self): return self.content

