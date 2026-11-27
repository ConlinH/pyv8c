"""Textual node types: ``CharacterData``, ``Text``, ``Comment``, ``DocumentType``."""

from html import escape

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import MISSING, dom_string, integer, missing_error
from .mixins import _ChildNodeMixin, _NonDocumentTypeChildNodeMixin
from .nodes import Node, _bump_style_version


#: Elements whose text serializes literally rather than escaped: ``noscript`` (scripting is
#: always on), but not ``textarea``/``title``, which Chrome escapes on the way out.
_LITERAL_TEXT_TAGS = frozenset({"script", "style", "noscript", "xmp", "iframe",
                                "noembed", "noframes", "plaintext"})


@interface('CharacterData', 'Node')
class CharacterData(_ChildNodeMixin, _NonDocumentTypeChildNodeMixin, Node):
    def __init__(self, data='', *_extra, services=None, **kw):
        if data is pyv8.Undefined:
            data = ''
        if data is not pyv8.Undefined:
            data = dom_string(data)
        if services is pyv8.Undefined:
            services = None
        # Ordinary text nodes have no children in the parser's output.
        super().__init__(services=services, node_type=Node.TEXT_NODE, node_name='#text', value=str(data),
                         _defer_children=type(self) is Text, **kw)
    def get_data(self): return self.nodeValue or ""
    def set_data(self, value):
        self.nodeValue = str(value)
        self._invalidate_layout()
        _bump_style_version(self)
    def get_length(self): return len(self.nodeValue or "")
    def fn_substringData(self, offset=MISSING, count=MISSING, *_extra):
        if offset is MISSING:
            raise missing_error('CharacterData', 'substringData', 2, 0, constructor=False)
        if count is MISSING:
            raise missing_error('CharacterData', 'substringData', 2, 1, constructor=False)
        offset = integer(offset)
        count = integer(count)
        start, end = int(offset), int(offset) + int(count)
        return (self.nodeValue or "")[start:end]
    def fn_appendData(self, value=MISSING, *_extra):
        if value is MISSING:
            raise missing_error('CharacterData', 'appendData', 1, 0, constructor=False)
        value = dom_string(value)
        self.nodeValue = (self.nodeValue or '') + str(value)
        self._invalidate_layout()
        _bump_style_version(self)
    def fn_insertData(self, offset=MISSING, value=MISSING, *_extra):
        if offset is MISSING:
            raise missing_error('CharacterData', 'insertData', 2, 0, constructor=False)
        if value is MISSING:
            raise missing_error('CharacterData', 'insertData', 2, 1, constructor=False)
        offset = integer(offset)
        value = dom_string(value)
        data, start = self.nodeValue or "", int(offset)
        self.nodeValue = data[:start] + str(value) + data[start:]
        self._invalidate_layout()
        _bump_style_version(self)
    def fn_deleteData(self, offset=MISSING, count=MISSING, *_extra):
        if offset is MISSING:
            raise missing_error('CharacterData', 'deleteData', 2, 0, constructor=False)
        if count is MISSING:
            raise missing_error('CharacterData', 'deleteData', 2, 1, constructor=False)
        offset = integer(offset)
        count = integer(count)
        data, start = self.nodeValue or "", int(offset)
        self.nodeValue = data[:start] + data[start + int(count):]
        self._invalidate_layout()
        _bump_style_version(self)
    def fn_replaceData(self, offset=MISSING, count=MISSING, value=MISSING, *_extra):
        if offset is MISSING:
            raise missing_error('CharacterData', 'replaceData', 3, 0, constructor=False)
        if count is MISSING:
            raise missing_error('CharacterData', 'replaceData', 3, 1, constructor=False)
        if value is MISSING:
            raise missing_error('CharacterData', 'replaceData', 3, 2, constructor=False)
        offset = integer(offset)
        count = integer(count)
        value = dom_string(value)
        data, start = self.nodeValue or "", int(offset)
        self.nodeValue = data[:start] + str(value) + data[start + int(count):]
        self._invalidate_layout()
        _bump_style_version(self)
    def _clone_shallow(self):
        # Keep the concrete flavour so clones expose ``data`` and the right node type.
        return type(self)(self.nodeValue or "", services=self._services)
    def get_textContent(self): return self.nodeValue or ""
    def set_textContent(self, value): self.set_data(value)
    def outer_html(self):
        parent = getattr(self, "parentNode", None)
        if str(getattr(parent, "localName", "") or "").lower() in _LITERAL_TEXT_TAGS:
            return self.nodeValue or ""
        return escape(self.nodeValue or "")


@interface('Text', 'CharacterData')
class Text(CharacterData):
    pass


@interface('Comment', 'CharacterData')
class Comment(CharacterData):
    def __init__(self, data='', *_extra, services=None, **kw):
        if data is pyv8.Undefined:
            data = ''
        if data is not pyv8.Undefined:
            data = dom_string(data)
        if services is pyv8.Undefined:
            services = None
        super().__init__(data, services=services, **kw)
        self.nodeType, self.nodeName = (Node.COMMENT_NODE, '#comment')
    def outer_html(self): return "<!--" + (self.nodeValue or "") + "-->"


@interface('DocumentType', 'Node')
class DocumentType(_ChildNodeMixin, Node):
    def __init__(self, name="html", public_id="", system_id="", services=None, **kw):
        super().__init__(services=services, node_type=Node.DOCUMENT_TYPE_NODE, node_name=name, value=None, **kw)
        self._public_id, self._system_id = str(public_id), str(system_id)
    def get_name(self): return self.nodeName
    def get_publicId(self): return self._public_id
    def get_systemId(self): return self._system_id
    def outer_html(self): return "<!DOCTYPE " + self.nodeName + ">"
