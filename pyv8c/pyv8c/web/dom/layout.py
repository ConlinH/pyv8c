"""Sync document layout inputs to Rust and read its geometry snapshots."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from ..css.cascade import _cascade, _document_style, computed_style, document_rules, has_running_animations, rules_for
from ..css.values import affine_length
from .character_data import Text
from .element import Element
from .font_measurement import _cached_face_path, _environment_for, _face_chain, _families


#: The default object size a replaced element falls back to when its content has no
#: intrinsic size of its own, which is what an empty iframe, canvas or video measures.
_DEFAULT_OBJECT_SIZE = {
    "iframe": (300.0, 150.0), "canvas": (300.0, 150.0), "video": (300.0, 150.0),
    "object": (300.0, 150.0), "svg": (300.0, 150.0),
}

_DIMENSION_HINT = re.compile(r'\s*([0-9]+)')
_ROTATION = re.compile(
    r'(?:^|;)\s*transform\s*:\s*rotate\(\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))deg\s*\)\s*(?:;|$)',
    re.IGNORECASE,
)
_PROPERTIES = frozenset((
    'display', 'position', 'top', 'right', 'bottom', 'left', 'box-sizing',
    'width', 'height', 'min-width', 'min-height', 'max-width', 'max-height', 'aspect-ratio',
    'overflow', 'overflow-x', 'overflow-y', 'gap', 'row-gap', 'column-gap',
    'font-size', 'font-family', 'font-style', 'font-weight', 'line-height', 'letter-spacing',
    'word-spacing', 'white-space', 'word-break', 'overflow-wrap', 'text-align', 'direction',
    'vertical-align', 'order', 'align-items', 'align-self', 'align-content', 'justify-items',
    'visibility', 'pointer-events', 'z-index', 'transform', 'transform-origin',
    'justify-self', 'justify-content', 'appearance',
))
_PREFIXES = ('margin', 'padding', 'border', 'flex', 'grid')
_CALCULATED_SIZES = ('width', 'height', 'min-width', 'min-height', 'max-width', 'max-height', 'flex-basis')
_TEXT_PROPERTIES = frozenset((
    'font-size', 'font-family', 'font-style', 'font-weight', 'line-height', 'letter-spacing',
    'word-spacing', 'white-space', 'word-break', 'overflow-wrap', 'text-align', 'direction',
))


def _style_of(element, rules, cache):
    if element in cache:
        return cache[element]
    return _cascade(element, rules_for(element, rules), cache)


@dataclass(frozen=True)
class _LayoutSnapshot:
    version: tuple
    boxes: Mapping
    styles: dict


def _viewport_width(element):
    try:
        window = element._services.document_services.window
    except AttributeError:
        window = None
    if window is not None:
        return window._viewport_size()
    realm = getattr(getattr(element, "_services", None), "realm_services", None)
    viewport = getattr(getattr(realm, "environment", None), "viewport", None)
    width = getattr(viewport, "width", None) or 1280
    height = getattr(viewport, "height", None) or 720
    return float(width), float(height)


def _child_elements(element):
    root = getattr(element, '_shadow_root', None)
    parent = root if root is not None else element
    return [child for child in getattr(parent, "_children", ())
            if isinstance(child, (Element, Text))]


def document_has_layout(document):
    """An embedding display:none subtree suppresses the child document's boxes.

    Check embedding styles before consulting the child cache: a parent style
    change does not increment the child's layout version.
    """
    try:
        frame = document._services.window._frame_element
    except AttributeError:
        return True
    while frame is not None:
        parent_document = frame.ownerDocument if frame.isConnected else None
        node = frame
        while isinstance(node, Element):
            style = _document_style(node, parent_document) if parent_document is not None else computed_style(node)
            if style.get('display') == 'none':
                return False
            node = node.parentElement or getattr(node.parentNode, '_host', None)
        parent = getattr(getattr(frame, '_services', None), 'window', None)
        frame = getattr(parent, '_frame_element', None)
    return True


def layout_snapshot(document, *, element=None):
    """Read or publish the current document's Rust geometry and computed styles."""
    if document is None or not document_has_layout(document):
        return {}, {}
    engine = document.__dict__.get('_native_layout_document')
    stamp = None if engine is None else engine.stamp
    layout_version = document.layout_version
    # Insertion and removal invalidate the document's layout version.
    root = (engine.root if stamp is not None and stamp[0] == layout_version
            else document.get_documentElement())
    if not isinstance(root, Element):
        return {}, {}
    version = (document.layout_version, document.style_version, _viewport_width(root))
    animated = has_running_animations(document)
    cached = document._layout_snapshot
    if (not animated and cached is not None and stamp == cached.version == version
            and not engine.capturing):
        return cached.boxes, cached.styles
    # The published node map contains only nodes captured in this document's tree.
    # A detached element also must not start a new solve on a cold read.
    if element is not None and not element.isConnected:
        return {}, {}
    if engine is None:
        engine = document._native_layout_document = DocumentLayout()
    return engine.capture(document, root, version, document_rules(document), animated)


def layout(element):
    """Return an element's border box, or the zero box for an unrendered element."""
    from ..._native._native import EMPTY_BOX

    return layout_snapshot(element.ownerDocument)[0].get(element, EMPTY_BOX)


def offset_parent(element):
    """The element whose padding edge anchors this element's offset."""
    boxes, _ = layout_snapshot(element.ownerDocument)
    return boxes.offset_parent(element) if boxes else None


class NativeBoxes(Mapping):
    """Resolve geometry against this snapshot's keys without allocating Python boxes."""

    def __init__(self, keys, nodes, snapshot):
        self.keys_by_node = keys
        self.nodes_by_key = nodes
        self.snapshot = snapshot

    def __getitem__(self, node):
        key = self.keys_by_node.get(node)
        value = None if key is None else self.snapshot.get(key)
        if value is None:
            raise KeyError(node)
        return value

    def __iter__(self):
        return (node for node in self.keys_by_node if self.client_rect(node) is not None)

    def __len__(self):
        return len(self.snapshot)

    def client_rect(self, node):
        key = self.keys_by_node.get(node)
        return None if key is None else self.snapshot.client_rect(key)

    def frame_point(self, node, x, y):
        key = self.keys_by_node.get(node)
        return None if key is None else self.snapshot.frame_point(key, x, y)

    def offset_parent(self, node):
        key = self.keys_by_node.get(node)
        return None if key is None else self.nodes_by_key.get(self.snapshot.offset_parent(key))

    def elements_at(self, x, y):
        return [self.nodes_by_key[key] for key in self.snapshot.hit_ids(x, y)]


class DocumentLayout:
    def __init__(self):
        from ..._native._native import LayoutDocument

        self.engine = LayoutDocument()
        self.version = 0
        self.keys = WeakKeyDictionary()
        self.next_key = 1
        self.records = {}
        self.geometry_keys = {}
        self.geometry_nodes = {}
        self.styles = {}
        self.stamp = None
        self.root = None
        self.journal_end = None
        self.full = True
        self.dirty = set()
        self.capturing = False

    def invalidate(self, node, previous, scalar):
        if self.journal_end != previous:
            self.full = True
        self.journal_end = previous + 1
        if scalar:
            self.dirty.add(node)
        else:
            self.full = True

    def _key(self, node):
        key = self.keys.get(node)
        if key is None:
            self.keys[node] = key = self.next_key
            self.next_key += 1
        return key

    def _affected(self):
        affected = set()
        pending = list(self.dirty)
        for node in self.dirty:
            parent = node.parentNode
            if parent is not None and rules_for(node).has_sibling_dependencies:
                siblings = parent._children
                pending.extend(siblings[siblings.index(node) + 1:])
        while pending:
            node = pending.pop()
            if node not in affected:
                affected.add(node)
                if not isinstance(node, Text):
                    pending.extend(_child_elements(node))
        return affected

    def _fonts(self, element, style, cache, text=''):
        families = tuple(_families(style))
        weight = style.get('font-weight', '400')
        italic = style.get('font-style', 'normal')
        environment = _environment_for(element)
        text_presentation = '\ufe0e' in text
        key = (id(environment), families, weight, italic, text_presentation)
        if key not in cache:
            cache[key] = [(path, getattr(face, 'index', 0))
                          for face in _face_chain(families, environment, weight, italic,
                                                  text_presentation=text_presentation)
                          if (path := _cached_face_path(face)) is not None]
        return cache[key]

    def _record(self, node, parent, slot, rules, styles, fonts):
        element = node if not isinstance(node, Text) else node.parentElement
        if element is None:
            element = parent
        style = _style_of(element, rules, styles)
        if isinstance(node, Text):
            props = {name: value for name, value in style.items() if name in _TEXT_PROPERTIES}
        else:
            props = {name: value for name, value in style.items()
                     if name in _PROPERTIES or name in _PREFIXES or name.startswith(
                         ('margin-', 'padding-', 'border-', 'flex-', 'grid-'))}
        tag = getattr(node, 'localName', '')
        text = node.nodeValue if isinstance(node, Text) else None
        if not isinstance(node, Text):
            for name in _CALCULATED_SIZES:
                value = props.get(name, '')
                if '%' in value and value.lower().startswith('calc('):
                    if (terms := affine_length(value)) is not None:
                        props[f'-layout-calc-{name}'] = f'{terms[0]} {terms[1]}'
            if parent is not None and node.parentElement is None:
                props['-shadow-boundary'] = '1'
            if props.get('transform', '').startswith('matrix('):
                angle = _ROTATION.search(node.attributes.get('style', ''))
                if angle is not None:
                    props['-transform-rotate'] = angle[1]
        intrinsic = _DEFAULT_OBJECT_SIZE.get(tag)
        faces = []
        if text and style.get('display') != 'none':
            faces = self._fonts(element, style, fonts, text)
        elif tag == 'br':
            faces = self._fonts(element, style, fonts)
        elif tag == 'canvas':
            intrinsic = node.get_width(), node.get_height()
        elif tag == 'img':
            intrinsic = node._natural_size()
            for name in ('width', 'height'):
                match = _DIMENSION_HINT.match(node.attributes.get(name, ''))
                if props.get(name, 'auto') in ('', 'auto') and match is not None:
                    props[name] = match[1] + 'px'
        elif tag == 'input':
            control = node.get_type()
            if control in ('checkbox', 'radio', 'color', 'range'):
                intrinsic = {'checkbox': (13, 13), 'radio': (13, 13),
                             'color': (44, 23), 'range': (129, 16)}[control]
                if props.get('appearance') == 'none' and control in ('checkbox', 'radio'):
                    intrinsic = 0, 0
            else:
                environment = _environment_for(element)
                props['-control-type'] = control
                props['-control-size'] = str(node.get_size())
                value = node.get_value()
                if control in ('submit', 'reset') and 'value' not in node.attributes and not getattr(node, '_dirty_value', False):
                    value = (getattr(environment, f'form_controls.{control}_label') if environment is not None
                             else 'Submit' if control == 'submit' else 'Reset')
                props['-control-value'] = str(value)
                if control == 'file':
                    props['-control-button-label'] = getattr(environment, 'form_controls.file_button_label') if environment else 'Choose File'
                    props['-control-empty-label'] = getattr(environment, 'form_controls.file_empty_label') if environment else 'No file chosen'
                if control == 'time':
                    props['-control-step'] = node._attr('step') or ''
                    props['-control-min'] = node._attr('min') or ''
                    if environment is not None and style.get('font-family') == 'monospace':
                        style = dict(style, **{'font-family': getattr(environment, 'form_controls.time_font_family')})
                faces = self._fonts(element, style, fonts, str(value))
        return (self._key(node), self._key(parent) if parent is not None else 0, slot,
                tag, text, props, intrinsic, faces)

    def capture(self, document, root, version, rules, animated):
        # A reentrant read needs an isolated engine and must not publish over the caller.
        if self.capturing:
            return DocumentLayout()._capture(document, root, version, rules, animated, publish=False)
        self.capturing = True
        try:
            return self._capture(document, root, version, rules, animated, publish=True)
        finally:
            self.capturing = False

    def _capture(self, document, root, version, rules, animated, *, publish):
        full = (self.full or animated or self.stamp is None or self.stamp == version
                or self.stamp[1] != version[1] or self.journal_end != version[0])
        affected = set() if full else self._affected()
        styles = {}
        if not full:
            styles.update(self.styles)
            for node in affected:
                styles.pop(node, None)
        partial = document._computed_style_snapshot
        if not animated and partial is not None and partial[0] == version[:2]:
            styles.update(partial[1])

        fonts = {}
        previous = self.records
        if full:
            records = {}
            parents = {}
            pending = [(root, None, 0)]
            while pending:
                node, parent, slot = pending.pop()
                row = self._record(node, parent, slot, rules, styles, fonts)
                records[node] = row
                parents[node] = (parent, slot)
                if row[4] is None:
                    pending.extend((child, node, index) for index, child in reversed(list(enumerate(_child_elements(node)))))
            candidates = records
            removed = [old[0] for node, old in previous.items() if node not in records]
            keys = {node: row[0] for node, row in records.items() if row[4] is None}
            nodes = {key: node for node, key in keys.items()}
        else:
            candidates = {node: self._record(node, *self.parents[node], rules, styles, fonts)
                          for node in affected if node in previous}
            removed = []
            keys = self.geometry_keys
            nodes = self.geometry_nodes

        upserts = [row for node, row in candidates.items() if not self.version or row != previous.get(node)]
        try:
            geometry = self.engine.solve(self.version, version[2], upserts, removed, not self.version)
        except ValueError as error:
            if str(error) != 'layout version mismatch':
                raise
            self.engine = type(self.engine)()
            self.version = 0
            self.full = True
            return self._capture(document, root, version, rules, animated, publish=publish)
        self.version = geometry.version
        result = NativeBoxes(keys, nodes, geometry)
        live = (document.layout_version, document.style_version, _viewport_width(root))
        if live != version:
            # The old native state cannot back a later patch based on the live tree.
            self.engine = type(self.engine)()
            self.version = 0
            self.full = True
            return result, styles

        if full:
            self.parents = parents
            self.records = records
            self.geometry_keys = keys
            self.geometry_nodes = nodes
        else:
            previous.update(candidates)
        self.stamp = version
        self.root = root
        self.journal_end = version[0]
        self.styles = styles
        self.dirty.clear()
        self.full = False
        if publish and not animated:
            document._layout_snapshot = _LayoutSnapshot(version, result, styles)
            if partial is not None and document._computed_style_snapshot is partial:
                document._computed_style_snapshot = None
        return result, styles
