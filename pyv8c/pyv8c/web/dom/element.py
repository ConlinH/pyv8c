"""The DOM Element interface shared by HTML, SVG and MathML."""
import re
import math
import struct
from html import escape

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import MISSING, boolean, dictionary, dom_string, interface_value, is_nullish, missing_error
from ..page_errors import InvalidCharacterError, NotFoundError, NotSupportedError, PageSyntaxError, PageTypeError
from .character_data import Text
from .collections import (
    DOMTokenList,
    HTMLCollection,
    NamedNodeMap,
    tag_name_ns_collection,
)
from . import aria
from .events import _install_handler_slots
from .mixins import _ChildNodeMixin, _NonDocumentTypeChildNodeMixin, _ParentNodeMixin
from .mutation import record_mutation
from .nodes import Attr, DocumentFragment, Node, ShadowRoot
from .utils import _VALID_NAME_RE, XHTML_NAMESPACE


#: ``insertAdjacent*`` positions, in the order Chromium validates them.
_ADJACENT_POSITIONS = ("beforebegin", "afterbegin", "beforeend", "afterend")


def _ascii_upper(value):
    return value.translate(str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))


def _ascii_lower(value):
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


# HTML void elements serialize without children or an end tag, programmatic nodes too.
_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


class _AttributeMap(dict):
    """Internal writes invalidate the owning tree's ID lookup too."""
    __slots__ = ('_owner',)
    def __init__(self, owner, values):
        super().__init__(values)
        self._owner = owner

    def _changed(self, key):
        self._owner._invalidate_layout()
        if (getattr(self._owner, "localName", "") in ("style", "link")
                and key in ("rel", "href", "media", "disabled")):
            document = self._owner.ownerDocument
            if document is not None:
                document.style_version += 1

    def __setitem__(self, key, value):
        if self.get(key) != value: self._changed(key)
        if key == 'id' and self.get(key) != value:
            self._owner._invalidate_id_index()
        super().__setitem__(key, value)
        nodes = self._owner.__dict__.get('_attribute_nodes')
        if nodes is not None:
            cached = nodes.get(key)
            if cached is not None: cached.nodeValue = str(value)

    def _detach_node(self, key):
        nodes = self._owner.__dict__.get('_attribute_nodes')
        if nodes is not None:
            cached = nodes.pop(key, None)
            if cached is not None: cached.ownerElement = None

    def __delitem__(self, key):
        self._changed(key)
        if key == 'id': self._owner._invalidate_id_index()
        super().__delitem__(key)
        self._detach_node(key)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items(): self[key] = value

    def setdefault(self, key, default=None):
        if key not in self: self[key] = default
        return self[key]

    def pop(self, key, *default):
        if key in self: self._changed(key)
        if key == 'id' and key in self: self._owner._invalidate_id_index()
        value = super().pop(key, *default)
        self._detach_node(key)
        return value

    def popitem(self):
        if self:
            self._changed(next(reversed(self)))
        key, value = super().popitem()
        if key == 'id': self._owner._invalidate_id_index()
        self._detach_node(key)
        return key, value

    def clear(self):
        for key in self:
            self._changed(key)
        if 'id' in self: self._owner._invalidate_id_index()
        super().clear()
        cached = self._owner.__dict__.get('_attribute_nodes')
        if cached:
            for node in cached.values(): node.ownerElement = None
            cached.clear()

    def __ior__(self, values): self.update(values); return self


@interface('Element', 'Node')
class Element(_ParentNodeMixin, _ChildNodeMixin, _NonDocumentTypeChildNodeMixin, Node):
    """Base implementation for HTML and XML elements."""

    def fn_checkVisibility(self, options=pyv8.Undefined, *_extra):
        """Whether this element is rendered, as Chrome's ``checkVisibility`` answers it.

        A detached node, ``display:none``, ``visibility:hidden/collapse`` or the
        ``hidden`` attribute are false.  ``checkOpacity`` / ``checkVisibilityCSS``
        opt into the two extra checks.  Nothing here fakes occlusion: that needs a
        real compositor, which this host does not have.
        """
        from ...foundation.arguments import dictionary, is_nullish
        if not getattr(self, "isConnected", False):
            return False
        settings = ({} if is_nullish(options) or options is MISSING
                    or not isinstance(options, (dict, pyv8.JSObject))
                    else dictionary(options))
        from .layout import document_has_layout
        if not document_has_layout(self.ownerDocument):
            return False
        from ..css.cascade import computed_style
        node = self
        while isinstance(node, Element):
            values = computed_style(node)
            if values.get('display') == 'none':
                return False
            if node is self and values.get('display') == 'contents':
                return False
            if values.get('content-visibility') == 'hidden' and node is not self:
                return False
            if (settings.get('checkOpacity') or settings.get('opacityProperty')):
                try:
                    if float(str(values.get('opacity', '1')).strip() or 1) == 0:
                        return False
                except (TypeError, ValueError):
                    pass
            if node is self and (settings.get('checkVisibilityCSS') or settings.get('visibilityProperty')):
                if values.get('visibility') in ('hidden', 'collapse'):
                    return False
            node = node.parentElement
        return True

    def fn_animate(self, keyframes=MISSING, options=pyv8.Undefined, *_extra):
        """Start one Web Animation and hand back its Animation object.

        Chrome returns a running, *pending* Animation with a KeyframeEffect and a
        null startTime.  Previously this member was an ``Undefined`` stub, which
        the Kasada SDK recorded as a nested null in its pre-encode array.
        """
        from .animation import Animation
        return Animation(target=self, keyframes=keyframes, options=options,
                         services=getattr(self, "_services", None))

    # Chrome reads ``role`` back as the attribute or null, and never validates it.
    def get_role(self):
        value = self.attributes.get("role")
        return pyv8.Null if value is None else str(value)
    def set_role(self, value): self.attributes["role"] = str(dom_string(value))

    #: What Chrome will put a shadow tree on: these tags, and any autonomous custom
    #: element, which is a name with a hyphen in it.
    _SHADOW_HOSTS = frozenset((
        "article", "aside", "blockquote", "body", "div", "footer", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "main", "nav", "p", "section", "span"))

    def get_shadowRoot(self):
        """The tree again, or null: a closed one is the host's own business."""
        root = getattr(self, "_shadow_root", None)
        if root is None or root.get_mode() != "open":
            return pyv8.Null
        return root
    def fn_attachShadow(self, init=MISSING, *_extra):
        """The one tree a host gets. A closed root is still handed back here; it is
        only ``host.shadowRoot`` that stops reporting it.
        """
        options = init if isinstance(init, dict) else (
            dictionary(init) if isinstance(init, pyv8.JSObject) else None)
        if options is None:
            raise PageTypeError("Failed to execute 'attachShadow' on 'Element': The provided"
                              " value is not of type 'ShadowRootInit'.")
        mode = options.get("mode", pyv8.Undefined)
        if is_nullish(mode):
            raise PageTypeError("Failed to execute 'attachShadow' on 'Element': Failed to"
                              " read the 'mode' property from 'ShadowRootInit': Required"
                              " member is undefined.")
        mode = str(dom_string(mode))
        if mode not in ("open", "closed"):
            raise PageTypeError("Failed to execute 'attachShadow' on 'Element': Failed to"
                              " read the 'mode' property from 'ShadowRootInit': The"
                              f" provided value '{mode}' is not a valid enum value of type"
                              " ShadowRootMode.")
        if getattr(self, "_shadow_root", None) is not None:
            raise NotSupportedError("Failed to execute 'attachShadow' on 'Element': Shadow"
                                    " root cannot be created on a host which already hosts"
                                    " a shadow tree.")
        name = str(self.localName).lower()
        if name not in self._SHADOW_HOSTS and "-" not in name:
            raise NotSupportedError("Failed to execute 'attachShadow' on 'Element': This"
                                    " element does not support attachShadow")
        root = self._shadow_root = ShadowRoot(mode, self, services=self._services)
        self._invalidate_layout()
        return root

    def __init__(self, tag_name="div", services=None, attributes=None, namespace=None,
                 prefix=None, local_name=None, **kw):
        """Build one element with the name it was created under.

        *namespace* is ``None`` on the HTML path -- ``createElement`` and the parser,
        which lower-case the name they were given -- and the namespace URI on the XML
        path, where the name is kept as written and only an HTML-namespace element
        reports it in upper case.
        """
        qualified = str(tag_name)
        if namespace is None:
            local = _ascii_lower(qualified if local_name is None else str(local_name))
            node_name = _ascii_upper(local)
            self._namespace, self._prefix = XHTML_NAMESPACE, None
        else:
            local = qualified if local_name is None else str(local_name)
            node_name = _ascii_upper(qualified) if namespace == XHTML_NAMESPACE else qualified
            self._namespace = None if is_nullish(namespace) else str(namespace)
            self._prefix = None if is_nullish(prefix) else str(prefix)
        super().__init__(services=services, node_type=Node.ELEMENT_NODE, node_name=node_name, **kw)
        # A fresh node cannot have cached lookups yet. Wrap once without the
        # replacement setter or a redundant temporary dictionary on the parse path.
        self.localName = local
        self._attribute_map = _AttributeMap(self, attributes or {})

    @property
    def attributes(self): return self._attribute_map

    @attributes.setter
    def attributes(self, values):
        self._invalidate_id_index()
        self._invalidate_layout()
        cached = self.__dict__.get('_attribute_nodes')
        if cached:
            for node in cached.values(): node.ownerElement = None
            cached.clear()
        self._attribute_map = _AttributeMap(self, values)

    def get_namespaceURI(self): return self._namespace or pyv8.Null
    def get_prefix(self): return self._prefix or pyv8.Null
    def get_localName(self): return self.localName
    def get_tagName(self): return self.nodeName
    def get_id(self): return self.attributes.get("id", "")
    def set_id(self, value):
        value = dom_string(value)
        self.attributes['id'] = str(value)
    def get_className(self): return self.attributes.get("class", "")
    def set_className(self, value):
        value = dom_string(value)
        self.attributes['class'] = str(value)
    def get_innerHTML(self): return "".join(c.outer_html() for c in self._children)
    def set_innerHTML(self, value):
        value = dom_string(value)
        self._replace_children(())
        if self._services: self._services.document_services.document._parse_fragment(str(value), self)
    def get_outerHTML(self): return self.outer_html()
    def set_outerHTML(self, value):
        value = dom_string(value)
        if self.parentNode: self.parentNode.fn_replaceChild(self._services.document_services.document._parse_one(str(value)), self)
    def get_children(self):
        collection = self.__dict__.get('_children_collection')
        if collection is None:
            collection = self._children_collection = HTMLCollection(
                owner=self, getter=lambda: [c for c in self._children if isinstance(c, Element)])
        return collection
    def get_slot(self): return str(self.attributes.get("slot", ""))
    def set_slot(self, value): self.attributes["slot"] = str(dom_string(value))
    def get_part(self):
        value = getattr(self, "_part_list", None)
        if value is None: value = self._part_list = DOMTokenList(self, "part")
        return value
    def set_part(self, value):
        # PutForwards, so an assignment lands on the token list's own value.
        self.get_part().set_value(dom_string(value))
    def get_assignedSlot(self): return pyv8.Null
    # Nothing here scrolls and nothing lays a border out, so the four read as a page
    # reads them on an element that is not rendered: Chrome answers 0 for each.
    def get_scrollTop(self): return 0
    def set_scrollTop(self, value): return None
    def get_scrollLeft(self): return 0
    def set_scrollLeft(self, value): return None
    def get_clientTop(self): return 0
    def get_clientLeft(self): return 0
    def get_elementTiming(self): return str(self.attributes.get("elementtiming", ""))
    def set_elementTiming(self, value):
        self.attributes["elementtiming"] = str(dom_string(value))
    def get_currentCSSZoom(self): return 1
    def get_customElementRegistry(self):
        """The registry ``window.customElements`` hands out, since it is one per window."""
        from ..bom.window import Window
        document = self.ownerDocument
        view = document.get_defaultView() if document is not None else None
        if not isinstance(view, Window):
            return pyv8.Null
        return view._window_value("customElements")
    def get_activeViewTransition(self): return pyv8.Null

    def get_classList(self):
        value = getattr(self, "_class_list", None)
        if value is None: value = self._class_list = DOMTokenList(self)
        return value
    def get_attributes(self):
        value = getattr(self, "_attributes_view", None)
        if value is None: value = self._attributes_view = NamedNodeMap(self)
        return value
    def get_clientWidth(self):
        from .layout import document_has_layout, layout
        document = self.ownerDocument
        if not document_has_layout(document):
            return 0
        if (self.localName == 'html' and self.isConnected and document is not None
                and document.get_documentElement() is self):
            return document.get_defaultView().get_innerWidth()
        box = layout(self)
        width = box.padding_box_width
        # Chromium exposes the editable viewport, excluding padding, for text
        # input controls. Other elements include padding in their client width.
        if (self.localName == 'input' and
                getattr(self, 'get_type', lambda: '')() in
                ('text', 'search', 'password', 'tel', 'url', 'email', 'number')):
            width = max(0.0, width - box.padding.get('left', 0.0) - box.padding.get('right', 0.0))
        return int(round(width))
    def get_clientHeight(self):
        from .layout import document_has_layout, layout
        document = self.ownerDocument
        if not document_has_layout(document):
            return 0
        if (self.localName == 'html' and self.isConnected and document is not None
                and document.get_documentElement() is self):
            return document.get_defaultView().get_innerHeight()
        return int(round(layout(self).padding_box_height))
    def get_scrollWidth(self):
        """Everything inside the box, overflow included, never less than the client box."""
        from .layout import layout
        box = layout(self)
        inside = box.overflow_right - box.border.get("left", 0.0) - box.border.get("right", 0.0)
        return max(int(round(box.padding_box_width)), int(round(inside)))
    def get_scrollHeight(self):
        from .layout import layout
        box = layout(self)
        inside = box.overflow_bottom - box.border.get("top", 0.0) - box.border.get("bottom", 0.0)
        return max(int(round(box.padding_box_height)), int(round(inside)))
    def fn_getBoundingClientRect(self, *_extra):
        from .layout import layout_snapshot, positioned_box
        from .rect import DOMRect
        box = positioned_box(self)
        left, top, width, height = box.left, box.top, box.width, box.height
        document = getattr(self, 'ownerDocument', None)
        if document is not None and self.isConnected:
            boxes, styles = layout_snapshot(document)
            if self not in boxes or styles.get(self, {}).get('display') == 'none':
                return DOMRect(left, top, width, height)
            corners = [(left, top), (left + width, top),
                       (left, top + height), (left + width, top + height)]
            node = self
            while node is not None:
                values = styles.get(node, {})
                transform = str(values.get('transform', 'none'))
                node_box = boxes.get(node)
                if node_box is not None and transform.startswith('matrix(') and transform.endswith(')'):
                    try:
                        a, b, c, d, e, f = (float(part.strip()) for part in transform[7:-1].split(','))
                    except ValueError:
                        pass
                    else:
                        raw = str(node.attributes.get('style', ''))
                        angle = re.search(r'(?:^|;)\s*transform\s*:\s*rotate\(\s*'
                                          r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))deg\s*\)\s*(?:;|$)', raw, re.I)
                        if (angle is not None
                                and abs(a - math.cos(math.radians(float(angle[1])))) < 1e-5
                                and abs(b - math.sin(math.radians(float(angle[1])))) < 1e-5):
                            sine = math.sin(math.radians(float(angle[1])))
                            cosine = math.cos(math.radians(float(angle[1])))
                            a, b, c, d, e, f = cosine, sine, -sine, cosine, 0.0, 0.0
                        origin = str(values.get('transform-origin', '50% 50%')).split()
                        if len(origin) == 1:
                            if origin[0].lower() in ('top', 'bottom'):
                                origin.insert(0, '50%')
                            else:
                                origin.append('50%')
                        if (origin[0].lower() in ('top', 'bottom')
                                or origin[1].lower() in ('left', 'right')):
                            origin[0], origin[1] = origin[1], origin[0]
                        def coordinate(value, size):
                            keywords = {'left': '0%', 'top': '0%', 'center': '50%',
                                        'right': '100%', 'bottom': '100%'}
                            value = keywords.get(value.lower(), value)
                            try:
                                return float(value[:-1]) * size / 100 if value.endswith('%') else float(value.removesuffix('px'))
                            except ValueError:
                                return size / 2
                        cx = node_box.left + coordinate(origin[0], node_box.width)
                        cy = node_box.top + coordinate(origin[1], node_box.height)
                        corners = [(cx + a * (x-cx) + c * (y-cy) + e,
                                    cy + b * (x-cx) + d * (y-cy) + f) for x, y in corners]
                node = getattr(node, 'parentElement', None)
            xs, ys = zip(*corners)
            left, top = min(xs), min(ys)
            width, height = max(xs) - left, max(ys) - top
            width = struct.unpack('f', struct.pack('f', width))[0]
            height = struct.unpack('f', struct.pack('f', height))[0]
        return DOMRect(left, top, width, height)
    def fn_getAttribute(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('Element', 'getAttribute', 1, 0, constructor=False)
        name = dom_string(name)
        return self.attributes.get(str(name).lower(), pyv8.Null)
    def fn_setAttribute(self, name=MISSING, value=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('Element', 'setAttribute', 2, 0, constructor=False)
        if value is MISSING:
            raise missing_error('Element', 'setAttribute', 2, 1, constructor=False)
        self._set_attribute(dom_string(name), dom_string(value))
    def fn_removeAttribute(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('Element', 'removeAttribute', 1, 0, constructor=False)
        name = dom_string(name)
        lowered = str(name).lower()
        previous = self.attributes.pop(lowered, None)
        if previous is not None:
            record_mutation(self, "attributes", attribute_name=lowered, old_value=previous)
            self._attribute_changed_here(lowered, previous, None)
    def fn_hasAttribute(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('Element', 'hasAttribute', 1, 0, constructor=False)
        name = dom_string(name)
        return str(name).lower() in self.attributes
    def fn_hasAttributes(self, *_extra):
        return bool(self.attributes)
    def fn_getAttributeNames(self, *_extra):
        return list(self.attributes)
    def fn_toggleAttribute(self, name=MISSING, force=pyv8.Undefined, *_extra):
        if name is MISSING:
            raise missing_error('Element', 'toggleAttribute', 1, 0, constructor=False)
        name = dom_string(name)
        if force is not pyv8.Undefined:
            force = boolean(force)
        name = str(name).lower()
        if force in (None, pyv8.Undefined, pyv8.Null):
            force = name not in self.attributes
        force = bool(force)
        if force: self.attributes[name] = ""
        else: self.attributes.pop(name, None)
        return force
    # Only the HTML namespace exists, so the *NS variants behave like their plain
    # counterparts for a null/HTML namespace and match nothing for a foreign one.
    def fn_getAttributeNS(self, namespace=MISSING, local_name=MISSING, *_extra):
        if namespace is MISSING:
            raise missing_error('Element', 'getAttributeNS', 2, 0, constructor=False)
        if local_name is MISSING:
            raise missing_error('Element', 'getAttributeNS', 2, 1, constructor=False)
        if not is_nullish(namespace):
            namespace = dom_string(namespace)
        local_name = dom_string(local_name)
        return self.fn_getAttribute(local_name)
    def fn_setAttributeNS(self, namespace=MISSING, name=MISSING, value=MISSING, *_extra):
        if namespace is MISSING:
            raise missing_error('Element', 'setAttributeNS', 3, 0)
        if name is MISSING:
            raise missing_error('Element', 'setAttributeNS', 3, 1)
        if value is MISSING:
            raise missing_error('Element', 'setAttributeNS', 3, 2)
        if not is_nullish(namespace):
            # Only the HTML namespace exists, but WebIDL converts the argument anyway.
            dom_string(namespace)
        self._set_attribute(dom_string(name), dom_string(value))

    def _set_attribute(self, name, value):
        """Write one attribute; both spellings convert their arguments first.

        The notification lives here rather than in the two spellings, because the
        native dispatch resolves a member on the class that declared it: a subclass
        override of ``fn_setAttribute`` is never reached.
        """
        if not _VALID_NAME_RE.match(name):
            raise InvalidCharacterError("The attribute name provided is invalid.")
        lowered = name.lower()
        attributes = self.attributes
        previous = attributes.get(lowered)
        written = str(value)
        if previous != written:
            attributes[lowered] = written
        record_mutation(self, "attributes", attribute_name=lowered,
                        old_value=previous)
        self._attribute_changed_here(lowered, previous, written)

    def _attribute_changed_here(self, name, old, new):
        """Tell an element that reflects the attribute, when it wants to know."""
        changed = getattr(self, "_attribute_changed", None)
        if changed is not None and old != new:
            changed(name, old, new)

    def _restart_resource(self):
        """Either write of a load-bearing URL: the layer drops what was loading."""
        services = getattr(self, "_services", None)
        if services:
            services.resource_services.restart_element_resource(self)
    def fn_removeAttributeNS(self, namespace=MISSING, local_name=MISSING, *_extra):
        if namespace is MISSING:
            raise missing_error('Element', 'removeAttributeNS', 2, 0, constructor=False)
        if local_name is MISSING:
            raise missing_error('Element', 'removeAttributeNS', 2, 1, constructor=False)
        if not is_nullish(namespace):
            namespace = dom_string(namespace)
        local_name = dom_string(local_name)
        self.fn_removeAttribute(local_name)
    def fn_hasAttributeNS(self, namespace=MISSING, local_name=MISSING, *_extra):
        if namespace is MISSING:
            raise missing_error('Element', 'hasAttributeNS', 2, 0, constructor=False)
        if local_name is MISSING:
            raise missing_error('Element', 'hasAttributeNS', 2, 1, constructor=False)
        if not is_nullish(namespace):
            namespace = dom_string(namespace)
        local_name = dom_string(local_name)
        return self.fn_hasAttribute(local_name)
    def fn_getAttributeNode(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('Element', 'getAttributeNode', 1, 0, constructor=False)
        name = dom_string(name)
        name = str(name).lower()
        if name not in self.attributes: return pyv8.Null
        return Attr(name, self.attributes[name], ownerElement=self, services=self._services)
    def fn_setAttributeNode(self, attr=MISSING, *_extra):
        if attr is MISSING:
            raise missing_error('Element', 'setAttributeNode', 1, 0, constructor=False)
        attr = interface_value(attr, 'Attr', 'Element', 'setAttributeNode', 1, nullable=False)
        if not isinstance(attr, Attr): raise PageTypeError("parameter 1 is not of type 'Attr'.")
        previous = self.fn_getAttributeNode(attr.get_name())
        self.attributes[str(attr.get_name()).lower()] = attr.get_value()
        return previous
    def fn_removeAttributeNode(self, attr=MISSING, *_extra):
        if attr is MISSING:
            raise missing_error('Element', 'removeAttributeNode', 1, 0, constructor=False)
        attr = interface_value(attr, 'Attr', 'Element', 'removeAttributeNode', 1, nullable=False)
        if not isinstance(attr, Attr): raise PageTypeError("parameter 1 is not of type 'Attr'.")
        name = str(attr.get_name()).lower()
        if name not in self.attributes: raise NotFoundError("The attribute was not found.")
        return Attr(name, self.attributes.pop(name), ownerElement=self, services=self._services)
    def _descendant_elements(self):
        return [node for node in self._descendant_snapshot() if isinstance(node, Element)]
    def fn_getElementsByTagName(self, tag_name=MISSING, *_extra):
        if tag_name is MISSING:
            raise missing_error('Element', 'getElementsByTagName', 1, 0, constructor=False)
        tag_name = dom_string(tag_name)
        wanted = str(tag_name).lower()
        if wanted == "*":
            return HTMLCollection(owner=self, getter=self._descendant_elements)
        return HTMLCollection(owner=self, getter=lambda: list(self._tag_index_snapshot().get(wanted, ())))
    def fn_getElementsByClassName(self, class_names=MISSING, *_extra):
        if class_names is MISSING:
            raise missing_error('Element', 'getElementsByClassName', 1, 0, constructor=False)
        class_names = dom_string(class_names)
        wanted = set(str(class_names).split())
        return HTMLCollection(owner=self, getter=lambda: [
            node for node in self._descendant_elements()
            if wanted.issubset(set(node.get_className().split()))
        ])
    def fn_getElementsByTagNameNS(self, namespace=MISSING, local_name=MISSING, *_extra):
        return tag_name_ns_collection(self, 'Element', namespace, local_name)
    def _insert_adjacent(self, where, nodes):
        where = str(where).lower()
        if where not in _ADJACENT_POSITIONS:
            raise PageSyntaxError(
                f"The value provided ('{where}') is not one of "
                + ", ".join(f"'{p}'" for p in _ADJACENT_POSITIONS) + "."
            )
        if where in ("afterbegin", "beforeend"):
            for offset, node in enumerate(nodes):
                self._adopt_child(node, offset if where == "afterbegin" else None)
            return
        parent = self.parentNode
        if parent is None:
            return
        for node in nodes:
            # Re-locate ``self`` each step: adopting a sibling detaches it and shifts the list.
            index = parent._children.index(self) + (0 if where == "beforebegin" else 1)
            parent._adopt_child(node, index)
    def fn_insertAdjacentElement(self, where=MISSING, element=MISSING, *_extra):
        if where is MISSING:
            raise missing_error('Element', 'insertAdjacentElement', 2, 0, constructor=False)
        if element is MISSING:
            raise missing_error('Element', 'insertAdjacentElement', 2, 1, constructor=False)
        where = dom_string(where)
        element = interface_value(element, 'Element', 'Element', 'insertAdjacentElement', 2, nullable=False)
        if not isinstance(element, Element):
            raise PageTypeError("parameter 2 is not of type 'Element'")
        self._insert_adjacent(where, [element])
        return element
    def fn_insertAdjacentText(self, where=MISSING, data=MISSING, *_extra):
        if where is MISSING:
            raise missing_error('Element', 'insertAdjacentText', 2, 0, constructor=False)
        if data is MISSING:
            raise missing_error('Element', 'insertAdjacentText', 2, 1, constructor=False)
        where = dom_string(where)
        data = dom_string(data)
        self._insert_adjacent(where, [Text(str(data), services=self._services)])
    def fn_insertAdjacentHTML(self, position=MISSING, html=MISSING, *_extra):
        if position is MISSING:
            raise missing_error('Element', 'insertAdjacentHTML', 2, 0, constructor=False)
        if html is MISSING:
            raise missing_error('Element', 'insertAdjacentHTML', 2, 1, constructor=False)
        position = dom_string(position)
        html = dom_string(html)
        document = self._services.document_services.document
        fragment = DocumentFragment(services=self._services)
        document._parse_fragment(str(html), fragment)
        self._insert_adjacent(position, list(fragment._children))
    def fn_webkitMatchesSelector(self, selector=MISSING, *_extra):
        if selector is MISSING:
            raise missing_error('Element', 'webkitMatchesSelector', 1, 0, constructor=False)
        selector = dom_string(selector)
        return self.fn_matches(selector)
    def fn_querySelector(self, selector=MISSING, *_extra):
        if selector is MISSING:
            raise missing_error('Element', 'querySelector', 1, 0, constructor=False)
        selector = dom_string(selector)
        return self._services.document_services.document.query_selector(selector, self) if self._services else pyv8.Null
    def fn_querySelectorAll(self, selector=MISSING, *_extra):
        if selector is MISSING:
            raise missing_error('Element', 'querySelectorAll', 1, 0, constructor=False)
        selector = dom_string(selector)
        return self._services.document_services.document.query_selector_all(selector, self) if self._services else []
    def fn_matches(self, selector=MISSING, *_extra):
        if selector is MISSING:
            raise missing_error('Element', 'matches', 1, 0, constructor=False)
        selector = dom_string(selector)
        return self._services.document_services.document.matches(self, selector) if self._services else False
    def fn_closest(self, selector=MISSING, *_extra):
        if selector is MISSING:
            raise missing_error('Element', 'closest', 1, 0, constructor=False)
        selector = dom_string(selector)
        n = self
        while isinstance(n, Element):
            if n.fn_matches(selector): return n
            n = n.parentElement
        return pyv8.Null
    @property
    def _reflected(self):
        """What a member was last given and then had taken away, per attribute.

        Chrome answers such a member with the word it was given rather than with the
        attribute or the default, which is the only way popover reads back as null.
        """
        store = self.__dict__.get("_reflected_store")
        if store is None:
            store = self.__dict__["_reflected_store"] = {}
        return store

    def _write(self, name, value, keep=None):
        """Set or remove one content attribute.

        A nullish value takes the attribute away unless *keep* says which of the two
        ends it stands for, which is how a boolean attribute spells itself: the true
        end is an empty attribute, the false end is no attribute.
        """
        if keep is not None:
            if keep:
                self.attributes[name] = ""
            else:
                self.attributes.pop(name, None)
        elif is_nullish(value):
            self.attributes.pop(name, None)
        else:
            self.attributes[name] = str(dom_string(value))

    def _replace_with_text(self, value):
        """Take this element out of its parent and leave a text node in its place."""
        parent = self.parentNode
        if parent is None:
            return
        if is_nullish(value):
            parent.removeChild(self)
            return
        parent.fn_replaceChild(Text(str(dom_string(value)), services=self._services), self)

    def _resolve_ids(self, ids):
        """The elements *ids* names, in the order given, skipping what is not there.

        The idref ARIA members answer with elements rather than with the string, and
        an id that names nothing contributes nothing -- which is why a tree outside a
        document resolves to an empty answer.
        """
        document = self.ownerDocument
        if document is None or not self.isConnected:
            return []
        by_id = {}
        for node in document._descendants():
            if isinstance(node, Element) and node.get_id():
                by_id.setdefault(node.get_id(), node)
        found = []
        for identifier in ids:
            target = by_id.get(identifier)
            if target is not None:
                found.append(target)
        return found

    def outer_html(self):
        attrs = "".join(f' {k}="{escape(str(v), quote=True)}"' for k, v in self.attributes.items())
        if self.localName in _VOID_ELEMENTS:
            return f"<{self.localName}{attrs}>"
        return f"<{self.localName}{attrs}>{self.get_innerHTML()}</{self.localName}>"
    def _clone_shallow(self):
        options = dict(services=self._services, attributes=self.attributes.copy(),
                       namespace=self._namespace, prefix=self._prefix, local_name=self.localName)
        # Baseline-only SVG classes have a placeholder constructor. As in
        # Document.createElementNS, initialize their inherited Element model directly.
        if getattr(type(self), '__v8_idl_stub__', False):
            clone = type(self).__new__(type(self))
            Element.__init__(clone, self.nodeName, **options)
            return clone
        return type(self)(self.nodeName, **options)


    # A click runs the behavior of the nearest ancestor that has one (see
    # dom.activation); most elements have none, hence the default pair below.

    def _activatable(self):
        """Whether a click at or below this element runs a behavior here."""
        return False

    def _run_activation(self, event=None):
        """What a click does to this element beyond dispatching the event."""
        return None

    def fn_getClientRects(self, *_extra):
        from .rect import DOMRectList
        from .layout import document_has_layout
        if not self.isConnected or not document_has_layout(self.ownerDocument):
            return DOMRectList(values=())
        node = self
        while isinstance(node, Element):
            if _unrendered(node):
                return DOMRectList(values=())
            node = node.parentElement
        # Layout currently provides a single border box, without line fragments.
        return DOMRectList(values=(self.fn_getBoundingClientRect(),))



#: The clipboard handlers belong to Element, not to HTMLElement, and the ARIA members
#: do too, so both are installed on Element rather than in the table every target
#: shares. The ARIA table is data measured from Chrome, which is why it lives in its
#: own module.
#: Accessors only: the baseline says which of these Element publishes.
_install_handler_slots(Element, ("onbeforecopy", "onbeforecut", "onbeforepaste",
                                 "onfullscreenchange", "onfullscreenerror",
                                 "onwebkitfullscreenchange", "onwebkitfullscreenerror"))
aria.install(Element)


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


