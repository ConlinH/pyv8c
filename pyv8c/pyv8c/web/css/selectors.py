import re
from dataclasses import dataclass
from functools import lru_cache

from pyv8c._native import dom

from ..page_errors import PageSyntaxError
from ..dom.element import Element, _AttributeMap
from ..dom.events import EventTarget, EVENT_TARGET_SETATTR
from ..dom.nodes import Node, ShadowRoot
from ..dom.query_cache import publish

_DEFAULT_ATTRIBUTE_ACCESS = (Element.attributes, object.__getattribute__, EventTarget.__getattr__)


# Selector parser failures cross the Python/V8 binding boundary; raising the
# project's marked error type lets V8 materialize a native SyntaxError a page can
# catch, rather than a Python traceback.
SyntaxError = PageSyntaxError


@dataclass
class Simple:
    tag: str = "*"
    ids: tuple = ()
    classes: tuple = ()
    attrs: tuple = ()
    pseudos: tuple = ()


_CSS_SPACE = ' \t\r\n\f'


def _selector_units(text):
    """Keep a CSS escape and its optional hex terminator in one lexical unit."""
    index = 0
    while index < len(text):
        start = index
        index += 1
        if text[start] == '\\' and index < len(text):
            digits = re.match(r'[0-9a-fA-F]{1,6}', text[index:])
            if digits:
                index += digits.end()
                if index < len(text) and text[index] in _CSS_SPACE:
                    index += 2 if text.startswith('\r\n', index) else 1
            else:
                index += 2 if text.startswith('\r\n', index) else 1
        yield start, text[start:index]


def _trim_selector(text):
    # str.strip() would remove an escaped final space or a hex terminator.
    units = list(_selector_units(text))
    while units and units[-1][1] in _CSS_SPACE:
        units.pop()
    start = next((i for i, unit in units if unit not in _CSS_SPACE), len(text))
    return text[start:units[-1][0] + len(units[-1][1])] if units else ''


def _decode_escape(unit, *, string=False):
    digits = re.match(r'[0-9a-fA-F]{1,6}', unit[1:])
    if digits:
        code = int(digits.group(), 16)
        return chr(code) if 0 < code <= 0x10ffff and not 0xd800 <= code <= 0xdfff else '\ufffd'
    if len(unit) == 1:
        return '\ufffd'
    if unit[1] in '\r\n\f':
        if string:
            return ''
        raise SyntaxError('Invalid escape in selector identifier')
    return unit[1].replace('\0', '\ufffd')


def _selector_name(text, start):
    parts, end = [], start
    for offset, unit in _selector_units(text[start:]):
        if unit.startswith('\\'):
            parts.append(_decode_escape(unit))
        elif unit.isalnum() or unit in '_-' or ord(unit) >= 0x80:
            parts.append(unit)
        else:
            break
        end = start + offset + len(unit)
    if end == start:
        raise SyntaxError('Expected selector identifier')
    return ''.join(parts), end


def _balanced(text, start, opening="(", closing=")"):
    depth = 0; quote = None
    for offset, ch in _selector_units(text[start:]):
        i = start + offset
        if ch.startswith('\\'):
            continue
        if quote:
            if ch == quote: quote = None
        elif ch in "'\"": quote = ch
        elif ch == opening: depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0: return text[start + 1:i], i + 1
    raise SyntaxError("Invalid selector")


def _groups(text):
    result, start, depth, quote = [], 0, 0, None
    for i, ch in _selector_units(text):
        if ch.startswith('\\'):
            continue
        if quote:
            if ch == quote: quote = None
        elif ch in "'\"": quote = ch
        elif ch in "[(": depth += 1
        elif ch in "])": depth -= 1
        elif ch == "," and depth == 0:
            if not _trim_selector(text[start:i]): raise SyntaxError("Empty selector group")
            result.append(_trim_selector(text[start:i])); start = i + 1
        if depth < 0: raise SyntaxError("Unbalanced selector")
    if quote or depth or not _trim_selector(text[start:]): raise SyntaxError("Invalid selector")
    result.append(_trim_selector(text[start:])); return result


def _simple(text):
    if not text: raise SyntaxError("Expected compound selector")
    i = 0; tag = "*"
    if text[0] == '*':
        i = 1
    elif text[0].isalpha() or text[0] in '_-\\' or ord(text[0]) >= 0x80:
        tag, i = _selector_name(text, 0); tag = tag.lower()
    ids, classes, attrs, pseudos = [], [], [], []
    while i < len(text):
        kind = text[i]
        if kind in "#.":
            name, i = _selector_name(text, i + 1)
            (ids if kind == "#" else classes).append(name); continue
        if kind == "[":
            value, i = _balanced(text, i, "[", "]")
            m = re.match(r"^\s*([\w:-]+)\s*(?:(\^=|\$=|\*=|~=|\|=|=)\s*(.*?)\s*)?$", value)
            if not m: raise SyntaxError("Invalid attribute selector")
            val = m.group(3)
            if val is not None and len(val) >= 2 and val[0] in "'\"" and val[-1] == val[0]: val = val[1:-1]
            val = ''.join(_decode_escape(unit, string=True) if unit.startswith('\\') else unit
                          for _, unit in _selector_units(val or ''))
            attrs.append((m.group(1).lower(), m.group(2), val)); continue
        if kind == ":":
            name, i = _selector_name(text, i + 1)
            name = name.lower(); arg = None
            if i < len(text) and text[i] == "(": arg, i = _balanced(text, i)
            if name not in {"first-child", "last-child", "nth-child", "first-of-type", "last-of-type", "nth-of-type", "only-child", "only-of-type", "not", "is", "where", "empty", "root", "checked", "enabled", "disabled"}: raise SyntaxError("Unsupported pseudo-class: " + name)
            if name in {"checked", "enabled", "disabled"} and arg is not None: raise SyntaxError(f":{name} does not accept an argument")
            pseudos.append((name, arg)); continue
        raise SyntaxError("Invalid selector token")
    return Simple(tag, tuple(ids), tuple(classes), tuple(attrs), tuple(pseudos))


def _parse_group(text):
    compounds, relations, buf, depth, quote, pending_space = [], [], "", 0, None, False
    for _, ch in _selector_units(text):
        if ch.startswith('\\'):
            if pending_space and compounds and len(relations) < len(compounds):
                relations.append(' '); pending_space = False
            buf += ch
        elif quote:
            buf += ch
            if ch == quote: quote = None
        elif ch in "'\"": quote, buf = ch, buf + ch
        elif ch in "[(": depth, buf = depth + 1, buf + ch
        elif ch in "])": depth, buf = depth - 1, buf + ch
        elif depth == 0 and ch in _CSS_SPACE:
            if _trim_selector(buf): compounds.append(_simple(_trim_selector(buf))); buf = ""; pending_space = True
        elif depth == 0 and ch in ">+~":
            if _trim_selector(buf): compounds.append(_simple(_trim_selector(buf))); buf = ""
            if not compounds: raise SyntaxError("Combinator without selector")
            relations.append(ch); pending_space = False
        else:
            if pending_space and compounds and len(relations) < len(compounds): relations.append(" "); pending_space = False
            buf += ch
    if quote or depth: raise SyntaxError("Invalid selector")
    if _trim_selector(buf): compounds.append(_simple(_trim_selector(buf)))
    if not compounds or len(relations) != len(compounds) - 1: raise SyntaxError("Invalid combinator")
    return compounds, relations


# A stylesheet can contain more selectors than a small query-only cache. Keep
# their ASTs across element walks instead of reparsing every rule on each node.
@lru_cache(maxsize=4096)
def _parse_cached(selector):
    if not isinstance(selector, str) or not selector.strip(): raise SyntaxError("Invalid selector")
    return tuple(_parse_group(group) for group in _groups(selector))


def parse(selector):
    if not isinstance(selector, str):
        raise SyntaxError("Invalid selector")
    return _parse_cached(selector)


def _sibling_position(node, of_type=False):
    parent = node.parentNode
    if parent is None:
        return 0, 0
    position = count = 0
    for child in parent._children:
        if not isinstance(child, Element) or (of_type and child.localName != node.localName):
            continue
        count += 1
        if child is node:
            position = count
    return position, count


def _previous_element(node, predicate=None):
    parent = node.parentNode
    if parent is None:
        return None
    previous = None
    for sibling in parent._children:
        if sibling is node:
            return previous
        if isinstance(sibling, Element) and (predicate is None or predicate(sibling)):
            previous = sibling
    return None


def _nth(pos, expr):
    expr = (expr or "").strip().lower().replace(" ", "")
    if expr == "odd": return pos % 2 == 1
    if expr == "even": return pos % 2 == 0
    try: return pos == int(expr)
    except ValueError: pass
    m = re.match(r"([+-]?\d*)n([+-]\d+)?$", expr)
    if not m: raise SyntaxError("Invalid nth expression")
    a = -1 if m.group(1) == "-" else 1 if m.group(1) in ("", "+") else int(m.group(1)); b = int(m.group(2) or 0)
    return pos == b if a == 0 else (pos - b) * a >= 0 and (pos - b) % a == 0

_FORM_CONTROL_TAGS = {"button", "input", "select", "textarea", "option", "optgroup", "fieldset"}


def _is_disabled(node):
    if node.localName not in _FORM_CONTROL_TAGS:
        return False
    if "disabled" in getattr(node, "attributes", {}):
        return True
    parent = node.parentNode
    while isinstance(parent, Element):
        if parent.localName in {"optgroup", "fieldset"} and "disabled" in getattr(parent, "attributes", {}):
            return True
        parent = parent.parentNode
    return False


def _is_checked(node):
    if node.localName == "option":
        return "selected" in getattr(node, "attributes", {})
    if node.localName != "input":
        return False
    input_type = str(getattr(node, "attributes", {}).get("type", "text")).lower()
    return input_type in {"checkbox", "radio"} and "checked" in node.attributes


def _matches_attribute(actual, op, wanted):
    """Whether *actual* satisfies ``[name<op>wanted]``."""
    if op == "=":
        return actual == wanted
    if op in {"^=", "$=", "*="} and not wanted:
        return False
    if op == "^=":
        return actual.startswith(wanted)
    if op == "$=":
        return actual.endswith(wanted)
    if op == "*=":
        return wanted in actual
    if op == "~=":
        return wanted in actual.split()
    return actual == wanted or actual.startswith(wanted + "-")


def _class_set(node, attributes):
    """The classes of *node*, split once per value of its class attribute.

    A query walks the same tree over and over, and the split used to happen again
    for every node of every scan.
    """
    class_name = attributes.get("class", "")
    cached = node.__dict__.get("_selector_classes")
    if cached is None or cached[0] != class_name:
        cached = (class_name, frozenset(class_name.split()))
        node.__dict__["_selector_classes"] = cached
    return cached[1]


def _simple_match(node, simple):
    if not isinstance(node, Element) or (simple.tag != "*" and node.localName != simple.tag): return False
    attributes = node.attributes
    for value in simple.ids:
        if attributes.get("id", "") != value: return False
    if simple.classes:
        classes = _class_set(node, attributes)
        for value in simple.classes:
            if value not in classes: return False
    for name, op, wanted in simple.attrs:
        actual = attributes.get(name)
        if actual is None: return False
        if op and not _matches_attribute(actual, op, wanted): return False
    for name, arg in simple.pseudos:
        if name in {"first-child", "last-child", "nth-child", "first-of-type", "last-of-type", "nth-of-type", "only-child", "only-of-type"}:
            pos, sibling_count = _sibling_position(node, name.endswith("of-type"))
        if name in {"only-child", "only-of-type"} and sibling_count != 1: return False
        if name == "first-child" and pos != 1 or name == "last-child" and pos != sibling_count: return False
        if name in ("nth-child", "nth-of-type") and not _nth(pos, arg): return False
        if name in ("first-of-type", "last-of-type") and (pos != 1 if name.startswith("first") else pos != sibling_count): return False
        if name == "empty" and getattr(node, "_children", ()): return False
        if name == "root" and getattr(node.parentNode, "nodeType", None) not in (None, 9): return False
        if name == "checked" and not _is_checked(node): return False
        if name == "disabled" and not _is_disabled(node): return False
        if name == "enabled" and (node.localName not in _FORM_CONTROL_TAGS or _is_disabled(node)): return False
        if name in ("not", "is", "where"):
            result = any(matches_group(node, group) for group in parse(arg or ""))
            if (name == "not" and result) or (name != "not" and not result): return False
    return True


def matches_group(node, ast):
    """Match one parsed selector group without retaining DOM results."""
    compounds, relations = ast
    return _match_at(node, compounds, relations, len(compounds) - 1)


def _match_at(current, compounds, relations, index):
    if current is None or not _simple_match(current, compounds[index]):
        return False
    if index == 0:
        return True
    relation = relations[index - 1]
    if relation == ">":
        return _match_at(current.parentNode, compounds, relations, index - 1)
    if relation == "+":
        return _match_at(_previous_element(current), compounds, relations, index - 1)
    if relation == "~":
        parent = current.parentNode
        if parent is None:
            return False
        # Test every earlier sibling, including its complete selector prefix.
        for sibling in parent._children:
            if sibling is current:
                break
            if _match_at(sibling, compounds, relations, index - 1):
                return True
        return False
    current = current.parentNode
    while current is not None:
        if _match_at(current, compounds, relations, index - 1):
            return True
        current = current.parentNode
    return False


def matches(node, selector): return any(matches_group(node, group) for group in parse(selector))


class _AttributeProjection:
    """One bounded, replaceable projection per document or shadow scope."""

    def __init__(self, nodes, tag, keys):
        self.nodes = nodes
        self.tag = tag
        self.keys = keys
        self.native = None
        self.attempted = False
        self.offsets = None
        self.dirty = set()

    def invalidate(self, node, key):
        if (self.native is not None and (key is None or key in self.keys)
                and node.localName == self.tag):
            self.dirty.add(node)
            if len(self.dirty) > 64:
                self.native = None
                self.offsets = None
                self.attempted = False
                self.dirty.clear()

    def _row(self, node):
        attributes = node.attributes
        if type(attributes) is _AttributeMap:
            return tuple(attributes.get(key) for key in self.keys)
        return None

    def match(self, simple, ordinary_type):
        if not self.attempted:
            self.attempted = True
            if not all(type(node) is ordinary_type and node.localName == simple.tag
                       for node in self.nodes):
                return None
            rows = tuple(self._row(node) for node in self.nodes)
            self.native = dom.create_attribute_projection(rows, len(self.keys))
        if self.native is None:
            return None
        if self.dirty:
            if self.offsets is None:
                self.offsets = {node: i for i, node in enumerate(self.nodes)}
            for node in self.dirty:
                offset = self.offsets.get(node)
                if offset is not None and not dom.update_attribute_projection(
                    self.native, offset, self._row(node)
                ):
                    self.native = None
                    break
            self.dirty.clear()
        if self.native is None:
            return None
        return dom.match_attribute_projection(self.native, self.nodes, simple.attrs)


def _match_attribute_projection(root, candidates, simple):
    if len(candidates) > 16384 or not (root.nodeType == Node.DOCUMENT_NODE or isinstance(root, ShadowRoot)):
        return None
    from ..html.elements import HTMLElement

    if ((getattr(HTMLElement, "attributes", None), HTMLElement.__getattribute__,
         getattr(HTMLElement, "__getattr__", None))
            != _DEFAULT_ATTRIBUTE_ACCESS or HTMLElement.__setattr__ is not EVENT_TARGET_SETATTR
            or hasattr(HTMLElement, "localName") or hasattr(HTMLElement, "_attribute_map")):
        root.__dict__.pop('_selector_projection', None)
        return None
    keys = tuple(dict.fromkeys(name for name, _op, _wanted in simple.attrs))
    projection = root.__dict__.get('_selector_projection')
    if projection is None or projection.nodes is not candidates or projection.keys != keys:
        # Wait for a second query before paying to project the whole scope.
        publish(root, "_selector_projection", _AttributeProjection(candidates, simple.tag, keys),
                use_setter=True)
        return None
    return projection.match(simple, HTMLElement)


def select(root, selector, *, first=False):
    """Match descendants in tree order; the query root is never a result.

    Use the rightmost required ID or tag to narrow a single group's candidates.
    Cold first-result queries keep the lazy walk.
    """
    ast = parse(selector)
    candidates = None
    if len(ast) == 1:
        group = ast[0]
        simple = group[0][-1]
        if (first and len(group[0]) == 1 and simple.tag == '*' and len(simple.ids) == 1
                and not (simple.classes or simple.attrs or simple.pseudos)):
            return root._find_element_by_id(simple.ids[0])
        if simple.ids:
            candidates = root._id_candidates(simple.ids[0], finish=not first)
        if candidates is None and simple.tag != '*':
            tags = root.__dict__.get('_tag_index_cache') if first else root._tag_index_snapshot()
            if tags is not None:
                candidates = tags.get(simple.tag, ())
        if candidates is None:
            candidates = root._descendants() if first else root._descendant_snapshot()
        if len(group[0]) == 1:
            if (not first and simple.tag != "*" and not (simple.ids or simple.classes or simple.pseudos)
                    and 2 <= len(simple.attrs) <= 16 and len(candidates) >= 256
                    and all(op != "~=" for _name, op, _wanted in simple.attrs)):
                projected = _match_attribute_projection(root, candidates, simple)
                if projected is not None:
                    return projected
                native = dom.match_attribute_batch(candidates, simple.tag, simple.attrs,
                                                   Element, _AttributeMap, _DEFAULT_ATTRIBUTE_ACCESS)
                if native is not None:
                    return native
            selected = (node for node in candidates if _simple_match(node, simple))
        else:
            selected = (node for node in candidates if matches_group(node, group))
    else:
        candidates = root._descendants() if first else root._descendant_snapshot()
        tags = {group[0][-1].tag for group in ast}
        if '*' not in tags:
            candidates = (node for node in candidates
                          if isinstance(node, Element) and node.localName in tags)
        selected = (node for node in candidates if any(matches_group(node, group) for group in ast))
    return next(selected, None) if first else list(selected)
