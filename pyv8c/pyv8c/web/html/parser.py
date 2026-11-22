import re
from html import unescape
from html.parser import HTMLParser

from ..dom.character_data import Comment, Text
from ..dom.element import Element
from ..dom.nodes import _bump_style_version
from ..dom.utils import MATHML_NAMESPACE, SVG_NAMESPACE
from .document_elements import HTMLTemplateElement
from .construction import build_batch


class _TreeBuilderMixin:
    """Node-tree construction shared by the two DOM parser front-ends.

    :class:`DOMParser` streams from :mod:`html.parser`; :class:`IncrementalDOMParser`
    replays a token list so it can suspend on a parser-blocking script.  Both build
    the same tree, so the shared rules (auto-closing, attribute normalization, inline
    handlers, raw-text elements, the end-tag algorithm) live here once.
    """

    void = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
    #: A start tag closes the open elements named in its set, which is what lets the end tags
    #: browsers tolerate be left out.
    auto_close = {
        "li": {"li", "p"}, "dt": {"dt", "dd", "p"}, "dd": {"dt", "dd", "p"},
        **{tag: {"p"} for tag in (
            "address", "article", "aside", "blockquote", "details", "div", "dl",
            "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
            "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol",
            "p", "pre", "section", "table", "ul")},
    }
    #: Tags whose character data is raw text: the rest of the content is taken verbatim.
    #: ``noscript`` is in here because scripting is always on, and with scripting on a
    #: browser keeps its content as one text node instead of parsing it.
    raw_text_tags = {"script", "style", "textarea", "title", "noscript"}

    #: Tags a browser puts in the head while it is still building one.
    head_tags = frozenset({
        "base", "basefont", "bgsound", "link", "meta", "title", "noscript",
        "noframes", "style", "template", "script",
    })

    def _append(self, node):
        """Put *node* where the parse belongs: a fragment's innermost element."""
        self.target.append_child(node)

    @property
    def current(self): return self.stack[-1]
    @property
    def target(self):
        # ``<template>`` builds its own content fragment instead of itself.
        return self.current.content if isinstance(self.current, HTMLTemplateElement) else self.current

    @classmethod
    def _is_raw_text_container(cls, node):
        return getattr(node, "localName", "") in cls.raw_text_tags

    def _pop_auto_closing(self, tag):
        closers = self.auto_close.get(tag)
        if closers is None:
            return
        # The parsing-context root (stack[0]) is never auto-closed: for a fragment it
        # is the innerHTML/outerHTML context element, already closed outside the fragment.
        # A list item can sit inside a p, so one start tag may close more than one.
        while (len(self.stack) > 1 and isinstance(self.current, Element)
               and self.current.localName in closers):
            self.stack.pop()

    def _create_element(self, tag, attrs):
        """Build the element for ``tag``, install inline handlers and attach it."""
        current = self.current
        space = getattr(current, '_namespace', None)
        name = getattr(current, 'localName', '')
        integration = (space == MATHML_NAMESPACE and (
            name in ('mi', 'mo', 'mn', 'ms', 'mtext') and tag not in ('mglyph', 'malignmark')
            or name == 'annotation-xml' and str(current.attributes.get('encoding', '')).lower()
            in ('text/html', 'application/xhtml+xml')))
        if tag == 'math' and (space != MATHML_NAMESPACE or integration):
            namespace = MATHML_NAMESPACE
        elif tag == 'svg':
            namespace = SVG_NAMESPACE
        elif space == MATHML_NAMESPACE and not integration:
            namespace = SVG_NAMESPACE if tag == 'svg' else MATHML_NAMESPACE
        elif space == SVG_NAMESPACE and name not in ('foreignobject', 'desc', 'title'):
            namespace = SVG_NAMESPACE
        else:
            namespace = None
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if namespace is None:
            node = self.services.document_services.element_class(tag)(
                tag, services=self.services, attributes=attributes)
        else:
            document = self.services.document_services.document
            node = document._new_element_ns(namespace, None, tag, tag)
            node.attributes.update(attributes)
        node._raw_text = ""
        # Marks the parser as the builder: a page-inserted node is adopted by the
        # document's own hooks instead of the parser's.
        node._parser_created = True
        for name, value in attrs:
            name = str(name).lower()
            if name.startswith("on"):
                self.services.document_services.install_inline_handler(node, name, value or "")
        self._append(node)
        return node

    def _close_element(self, tag):
        """Run the end-tag algorithm for ``tag``; True when a node matched."""
        for index in range(len(self.stack) - 1, 0, -1):
            node = self.stack[index]
            if not isinstance(node, Element) or node.localName != tag:
                continue
            source = getattr(node, "_raw_text", "")
            if tag == "script":
                self.services.document_services.parser_script_complete(node, source)
            if source and (tag == "script" or tag in self.raw_text_tags):
                node.append_child(Text(source, services=self.services))
                if tag != "script":
                    # Drop the parse-time copy now the text lives in the tree; scripts
                    # keep theirs, since re-inserting one re-runs that source.
                    node._raw_text = ""
            del self.stack[index:]
            return True
        return False


class DOMParser(_TreeBuilderMixin, HTMLParser):
    """Streaming parser for ``innerHTML``/``outerHTML`` fragments."""

    def __init__(self, services, root):
        super().__init__(convert_charrefs=False); self.services, self.stack = services, [root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self._pop_auto_closing(tag)
        node = self._create_element(tag, attrs)
        if tag not in self.raw_text_tags and (tag != "iframe" or node.isConnected):
            self.services.resource_services.discover_element_resource(node)
        if tag not in self.void: self.stack.append(node)

    def handle_startendtag(self, tag, attrs): self.handle_starttag(tag, attrs); self.handle_endtag(tag)
    def handle_endtag(self, tag): self._close_element(tag.lower())
    def handle_data(self, data):
        if not data: return
        if self._is_raw_text_container(self.current):
            self.current._raw_text = getattr(self.current, "_raw_text", "") + data
            _bump_style_version(self.current)
            return
        self._append(Text(data, services=self.services))
    def handle_entityref(self, name): self.handle_data(unescape("&" + name + ";"))
    def handle_charref(self, name): self.handle_data(unescape("&#" + name + ";"))
    def handle_comment(self, data): self._append(Comment(data, services=self.services))
    def handle_decl(self, decl):
        if decl.lower().startswith("doctype"):
            from ..dom.character_data import DocumentType
            parts = decl.split(None, 1)
            name = parts[1].strip() if len(parts) > 1 else "html"
            self._append(DocumentType(name=name, services=self.services))


_DOCTYPE_START = re.compile(r"<!doctype\b", re.I)
_END_TAG = re.compile(r"</\s*([A-Za-z][\w:-]*)[^>]*>")
_START_TAG = re.compile(r"<([A-Za-z][\w:-]*)")
_RAW_END = {tag: re.compile(r"</\s*" + re.escape(tag) + r"\s*>", re.I)
            for tag in _TreeBuilderMixin.raw_text_tags}


def _iter_tokens(source):
    """Yield tokens with start-tag attributes decoded once, without suffix copies."""
    pos, size, raw = 0, len(source), None
    # Use HTMLParser's tolerant attribute tokenizer. A quote
    # inside an attribute name or an unquoted value does not open a quoted
    # value; toggling on every quote can swallow subsequent scripts and tags.
    tag_tokenizer = _StartTagTokenizer()
    tag_tokenizer.rawdata = source
    while pos < size:
        if raw:
            match = _RAW_END[raw].search(source, pos)
            if not match:
                yield ("data", source[pos:]); return
            end = match.start()
            if end > pos: yield ("data", source[pos:end])
            yield ("end", raw); pos = match.end(); raw = None; continue
        if source.startswith("<!--", pos):
            end = source.find("-->", pos + 4)
            if end < 0: yield ("comment", source[pos + 4:]); return
            yield ("comment", source[pos + 4:end]); pos = end + 3; continue
        if source[pos] != "<":
            end = source.find("<", pos)
            end = size if end < 0 else end
            yield ("data", source[pos:end]); pos = end; continue
        if _DOCTYPE_START.match(source, pos):
            end = source.find(">", pos + 2)
            if end < 0: yield ("data", source[pos:]); return
            yield ("decl", source[pos + 2:end]); pos = end + 1; continue
        close = _END_TAG.match(source, pos)
        if close:
            yield ("end", close.group(1)); pos = close.end(); continue
        opening = _START_TAG.match(source, pos)
        if not opening:
            yield ("data", "<"); pos += 1; continue
        tag_tokenizer.tag = None
        tag_tokenizer.attrs = []
        end = tag_tokenizer.parse_starttag(pos)
        if end < 0: yield ("data", source[pos:]); return
        closed = source[end - 2:end] == '/>'
        tag = opening.group(1).lower()
        yield ("start", (tag_tokenizer.tag, tag_tokenizer.attrs, closed)); pos = end
        if tag in _TreeBuilderMixin.raw_text_tags and not closed: raw = tag


class _StartTagTokenizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False); self.tag = None; self.attrs = []
    def handle_starttag(self, tag, attrs): self.tag, self.attrs = tag.lower(), attrs
    def handle_startendtag(self, tag, attrs): self.handle_starttag(tag, attrs)


class IncrementalDOMParser(_TreeBuilderMixin):
    """Resumable parser used for parser-blocking script execution."""

    def __init__(self, services, root, source, active=True):
        self.services, self.root = services, root
        #: A parse outside a browsing context (``DOMParser``) runs no script and
        #: fetches nothing, which is how Chrome reads one.
        self.active = active
        self.tokens = list(_iter_tokens(str(source)))
        self.index, self.stack, self.done = 0, [root], False
        #: A document parse builds html/head/body as it reads; a fragment parse has no modes.
        self.building = getattr(root, "nodeType", None) == 9
        self.mode = "before-html" if self.building else None
        self.html = self.head = self.body = None

    def _depth(self):
        """The stack depth at which the current mode is placing a node."""
        if self.mode == "before-html":
            return 1
        if self.mode in ("before-head", "after-head"):
            return 2
        return 3

    def _enter(self):
        """Point the stack at this mode's container, unless something is open."""
        if len(self.stack) > self._depth():
            return
        if self.mode in ("in-body", "after-body") and self.body is not None:
            self.stack = [self.root, self.html, self.body]
        elif self.mode == "in-head" and self.head is not None:
            self.stack = [self.root, self.html, self.head]
        elif self.html is not None:
            self.stack = [self.root, self.html]
        else:
            self.stack = [self.root]

    def _append(self, node):
        if not self.building or len(self.stack) > self._depth():
            self.target.append_child(node)
            return
        self._place(node)

    def _make(self, parent, tag):
        node = self.services.document_services.element_class(tag)(
            tag, services=self.services, attributes={},
        )
        parent.append_child(node)
        return node

    def _place(self, node):
        """Where a top-level node goes: what a browser's insertion modes put there."""
        kind, name = getattr(node, "nodeType", None), getattr(node, "localName", None)
        text = str(node.nodeValue or "") if kind == 3 else None
        if kind in (8, 10):                     # a comment or a doctype
            self._enter()
            self.target.append_child(node)
            return
        if text is not None and not text.strip():
            if self.mode in ("before-html", "before-head"):
                return                          # whitespace before the head is dropped
            if self.mode == "in-head":
                self._enter()
                self.target.append_child(node)
                return
            if self.mode == "after-head":
                self.stack = [self.root, self.html]
                self.html.append_child(node)
                return
        if name == "html":
            if self.html is not None:
                return                          # a second root is ignored
            self.stack = [self.root]
            self.root.append_child(node)
            self.html, self.mode = node, "before-head"
            return
        if name == "head":
            if self.head is not None:
                return
            self._open_html()
            self.stack = [self.root, self.html]
            self.html.append_child(node)
            self.head, self.mode = node, "in-head"
            return
        if name == "body":
            if self.body is not None:
                return
            self._open_head()
            self.stack = [self.root, self.html]
            self.html.append_child(node)
            self.body, self.mode = node, "in-body"
            return
        if name in self.head_tags and self.mode in (
                "before-html", "before-head", "in-head", "after-head"):
            self._open_head()
            self.stack = [self.root, self.html, self.head]
            self.head.append_child(node)
            return
        self._open_body()
        self.stack = [self.root, self.html, self.body]
        self.body.append_child(node)

    def _open_html(self):
        if self.html is None:
            self.html = self._make(self.root, "html")
        if self.mode == "before-html":
            self.mode = "before-head"

    def _open_head(self):
        self._open_html()
        if self.head is None:
            self.head = self._make(self.html, "head")
        self.mode = "in-head"

    def _open_body(self):
        self._open_head()
        if self.body is None:
            self.body = self._make(self.html, "body")
        self.mode = "in-body"

    def _start(self, token):
        tag, attrs, closed = token
        if not tag: return
        self._pop_auto_closing(tag)
        node = self._create_element(tag, attrs)
        if self.active:
            if (tag == 'meta' and self.building and self.mode == 'in-head' and
                    str(node.attributes.get('http-equiv', '')).lower() == 'content-security-policy'):
                self.services.document_services.add_content_security_policy(
                    node.attributes.get('content', ''))
            if tag == "script": self.services.document_services.parser_script_start(node)
            elif tag not in self.raw_text_tags: self.services.resource_services.discover_element_resource(node)
        # An ignored duplicate (a second html/head/body) never reached the tree.
        if tag not in self.void and not closed and node.parentNode is not None:
            self.stack.append(node)

    def _end(self, tag):
        tag = str(tag).lower()
        self._close_element(tag)
        if not self.building:
            return
        if tag == "head" and self.head is not None and self.mode == "in-head":
            self.mode = "after-head"
        elif tag == "body" and self.body is not None:
            # Content after ``</body>`` still goes into the body, per the insertion modes.
            self.mode = "after-body"
        elif tag == "html" and self.html is not None:
            self.mode = "after-body"

    def write_markup(self, markup):
        """Splice written markup into the input stream (``document.write``): the tokens
        go in front of the unconsumed ones, so written nodes are created exactly like
        the surrounding document (a written ``<script>`` still blocks the parser)."""
        text = str(markup)
        if not text or self.done:
            return False
        self.tokens[self.index:self.index] = list(_iter_tokens(text))
        return True

    def run(self):
        if self.done:
            return True
        while self.index < len(self.tokens):
            start = self.index
            end = build_batch(self)
            if end != start:
                self.index = end
                if self.services.document_services.parser_is_blocked(): return False
                continue
            kind, value = self.tokens[self.index]
            self.tokens[self.index] = None
            self.index += 1
            if self.building:
                self._enter()
            if kind == "start": self._start(value)
            elif kind == "end": self._end(value)
            elif kind == "data":
                if self._is_raw_text_container(self.current):
                    self.current._raw_text = getattr(self.current, "_raw_text", "") + value
                    _bump_style_version(self.current)
                else: self._append(Text(unescape(value), services=self.services))
            elif kind == "comment": self._append(Comment(value, services=self.services))
            elif kind == "decl":
                from ..dom.character_data import DocumentType
                parts = value.split(None, 1); self._append(DocumentType(name=parts[1].strip() if len(parts) > 1 else "html", services=self.services))
            if self.services.document_services.parser_is_blocked(): return False
        if self.building:
            # A document whose markup placed nothing still gets the skeleton, as the last step.
            self._open_body()
        self.done = True
        self.tokens.clear()
        self.index = 0
        return True


def parse_fragment(html, services, parent):
    parser = DOMParser(services, parent); parser.feed(html); parser.close(); return parent
