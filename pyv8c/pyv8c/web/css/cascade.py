"""Stylesheet collection, cascade and computed CSS values."""
import math
import re
from functools import lru_cache
from ...foundation.environment import environment_of
from ...foundation.font_sizes import (font_size_px as _font_size_px,
                                     font_size_term as _font_size_term,
                                     px_text as _px_text)
from ...foundation.system_fonts import normalize
from ..page_errors import PageSyntaxError
from ..dom.utils import MATHML_NAMESPACE
from .cssom import CSSStyleDeclaration
from .properties import INHERITED_PROPERTIES, initial_values_for
from .colors import SYSTEM_COLORS
from .parser import _box_values, parse_declarations, valid_color
from .stylesheet import stylesheet_source
from .colors import NAMED_COLORS
from .selectors import parse as parse_selector, matches_group
from .values import resolve as resolve_computed_values, length_math, absolute_length_px, variables


def _environment_for(element):
    return environment_of(getattr(element, "_services", None))


def _number(value, base=0.0):
    match = re.match(r"^(-?(?:\d+(?:\.\d*)?|\.\d+))", str(value).strip())
    return float(match.group(1)) if match else base


@lru_cache(maxsize=4096)
def _px(value, reference=0.0):
    value = str(value).strip().lower()
    if value.startswith(("calc(", "min(", "max(", "clamp(")):
        computed = length_math(value, reference)
        if computed is not None:
            return computed
    if value.endswith("px"): return math.trunc(_number(value) * 64) / 64
    if value.endswith("%"): return reference * _number(value) / 100.0
    if value in ("0", "0px"): return 0.0
    absolute = absolute_length_px(value)
    if absolute is not None:
        return math.trunc(absolute * 64) / 64
    return _number(value, 0.0)


def _root_font_size(element, cache=None):
    """The root element's computed size, which is what ``rem`` is relative to."""
    document = getattr(element, "ownerDocument", None)
    get_root = getattr(document, "get_documentElement", None) if document is not None else None
    root = get_root() if get_root is not None else None
    if root is None or root is element:
        return 16.0
    if cache is not None and root in cache:
        return _px(cache[root].get("font-size", "16px"), 16) or 16
    return _px(computed_style(root).get("font-size", "16px"), 16) or 16

def _inline_declarations(element):
    text = str(element.attributes.get("style", ""))
    declarations = _parsed_inline(text)
    precise = getattr(element, '_inline_math_values', None)
    if precise is None or precise[0] != text or not precise[1]:
        return declarations
    return tuple((name, f'calc({precise[1][name][1]:.17g}px)', priority)
                 if name in precise[1] and value == precise[1][name][0]
                 else (name, value, priority)
                 for name, value, priority in declarations)


@lru_cache(maxsize=2048)
def _parsed_inline(raw):
    return tuple(parse_declarations(raw))


def _font_longhands(value):
    match = re.search(r'(?<![\w.-])((?:\d+(?:\.\d*)?|\.\d+)(?:px|pt|pc|em|rem|%|in|cm|mm)|'
                      r'xx-small|x-small|small|medium|large|x-large|xx-large|smaller|larger)'
                      r'(?:\s*/\s*([^\s]+))?\s+(.+)$', str(value), re.I)
    if match is None:
        return {}
    prefix = str(value)[:match.start()].lower().split()
    return {'font-size': match[1], 'line-height': match[2] or 'normal',
            'font-family': match[3],
            'font-style': 'italic' if 'italic' in prefix else 'oblique' if 'oblique' in prefix else 'normal',
            'font-weight': next((token for token in prefix if token in ('bold', 'bolder', 'lighter')
                                 or re.fullmatch(r'\d{1,4}', token)), '400'),
            'font-variant': 'small-caps' if 'small-caps' in prefix else 'normal',
            'font-stretch': next((token for token in prefix if token.endswith('condensed')
                                 or token.endswith('expanded')), 'normal')}


def _specificity(group):
    """Count the parsed selector, so escaped punctuation remains an identifier."""
    counts = [0, 0, 0]
    compounds, _relations = group
    for simple in compounds:
        counts[0] += len(simple.ids)
        counts[1] += len(simple.classes) + len(simple.attrs)
        counts[2] += simple.tag != '*'
        for name, argument in simple.pseudos:
            if name in ('is', 'not', 'where'):
                nested = tuple(_specificity(part) for part in parse_selector(argument or ''))
                if name != 'where':
                    for index, count in enumerate(max(nested, default=(0, 0, 0))):
                        counts[index] += count
            else:
                counts[1] += 1
    return tuple(counts)


class StyleRules:
    """A tree's rules indexed by required terms of each rightmost compound.

    Candidates retain whole selector lists and source order. Matching still
    decides applicability and specificity using the current tree and attributes.
    """

    __slots__ = ('_entries', '_buckets', '_universal')

    def __init__(self, entries):
        self._entries = tuple(entries)
        buckets, universal = {}, set()
        for position, (selectors, _declarations, _counts) in enumerate(self._entries):
            for compounds, _relations in selectors:
                simple = compounds[-1]
                if simple.ids:
                    key = ('id', simple.ids[0])
                elif simple.classes:
                    key = ('class', simple.classes[0])
                elif simple.tag != '*':
                    key = ('tag', simple.tag)
                else:
                    universal.add(position)
                    continue
                buckets.setdefault(key, set()).add(position)
        self._buckets = {key: frozenset(positions) for key, positions in buckets.items()}
        self._universal = frozenset(universal)

    def candidates(self, element):
        # Nothing can be excluded when every rule has an unanchored group.
        if len(self._universal) == len(self._entries):
            return self._entries
        attributes = element.attributes
        positions = set(self._universal)
        positions.update(self._buckets.get(('tag', element.localName), ()))
        positions.update(self._buckets.get(('id', attributes.get('id', '')), ()))
        for name in str(attributes.get('class', '')).split():
            positions.update(self._buckets.get(('class', name), ()))
        return (self._entries[position] for position in sorted(positions))


_EMPTY_RULES = StyleRules(())


def _cascade(element, rules, cache=None):
    if cache is not None and element in cache:
        return cache[element]
    values = initial_values_for(element)
    if 'hidden' in element.attributes and str(element.attributes.get('hidden', '')).lower() != 'until-found':
        values['display'] = 'none'
    parent = getattr(element, "parentElement", None)
    if parent is None:
        root = element.root_node()
        parent = getattr(root, '_host', None)
    if parent is None:
        environment = _environment_for(element)
        family = getattr(getattr(environment, 'fonts', None), 'default_family', None)
        if family:
            values['font-family'] = f'"{family}"' if ' ' in family else family
    parent_size = 16.0
    if parent is not None:
        parent_rules = rules if parent.root_node() is element.root_node() else rules_for(parent)
        inherited = _cascade(parent, parent_rules, cache)
        # Inherit every inherited property, not only those with an initial value here.
        values.update({
            name: inherited.get(name, values.get(name, ""))
            for name in INHERITED_PROPERTIES
        })
        values.update({name: value for name, value in inherited.items() if name.startswith("--")})
        parent_size = _px(inherited.get("font-size", "16px"), 16) or 16
    if str(getattr(element, 'localName', '')).lower() == 'input':
        input_type = getattr(element, 'get_type', lambda: '')()
        if input_type in ('button', 'submit', 'reset', 'color', 'checkbox', 'radio', 'range', 'file',
                          'text', 'search', 'password', 'tel', 'email', 'url', 'time'):
            values.update({'font-family': 'Arial', 'font-size': '13.3333px',
                           'box-sizing': 'border-box'})
            if input_type in ('button', 'submit', 'reset'):
                values.update({'padding': '1px 6px', 'border-width': '2px'})
            elif input_type == 'color':
                values.update({'padding': '1px 2px', 'border-width': '1px'})
            elif input_type in ('text', 'search', 'password', 'tel', 'email', 'url'):
                values.update({'padding': '1px 2px', 'border-width': '2px'})
            elif input_type == 'time':
                values.update({'font-family': 'monospace', 'padding': '0px',
                               'padding-left': '1px', 'border-width': '2px'})
            else:
                values.update({'padding': '0px', 'border-width': '0px'})
    # MathML's presentational hint has lower priority than author CSS.
    math_color = (element.attributes.get('mathcolor')
                  if getattr(element, '_namespace', None) == MATHML_NAMESPACE else None)
    if math_color and valid_color(math_color):
        values['color'] = str(math_color).strip().lower()
    # Last applied wins, so the order is CSS's: important, inline, specificity, source.
    matched = []
    order = 0
    for selectors, declarations, counts in rules.candidates(element):
        best = None
        for part, count in zip(selectors, counts):
            if (best is None or count > best) and _selector_matches(element, part):
                best = count
        if best is None:
            continue
        for name, value, priority in declarations:
            matched.append(((priority == "important"), False, best, order, name, value))
            order += 1
    for name, value, priority in _inline_declarations(element):
        matched.append(((priority == "important"), True, (0, 0, 0), order, name, value))
        order += 1
    ordered = sorted(matched, key=lambda item: item[:4])
    for *_, name, value in ordered:
        if name.startswith('--'):
            values[name] = value
    for _important, _inline, _count, _order, name, value in ordered:
        if name.startswith('--'):
            continue
        values[name] = value
        if name == 'font':
            values.update(_font_longhands(value))
        elif name in ('margin', 'padding'):
            resolved = variables(value, values) if 'var(' in value.lower() else value
            parts = _box_values(resolved) if resolved is not None else None
            if parts is not None:
                for side, part in zip(('top', 'right', 'bottom', 'left'), parts):
                    values[name + '-' + side] = part
        elif name in ('border-width', 'border-style'):
            parts = _box_values(value)
            if parts is not None:
                suffix = name.split('-', 1)[1]
                for side, part in zip(('top', 'right', 'bottom', 'left'), parts):
                    values[f'border-{side}-{suffix}'] = part
        elif name == 'border' or name in ('border-top', 'border-right', 'border-bottom', 'border-left'):
            # Border's omitted style is none; its omitted width is medium (3px).
            tokens = str(value).lower().split()
            styles = {'none', 'hidden', 'dotted', 'dashed', 'solid', 'double',
                      'groove', 'ridge', 'inset', 'outset'}
            border_style = next((token for token in tokens if token in styles), 'none')
            width = next((token for token in tokens if re.fullmatch(
                r'(?:0|(?:\d+(?:\.\d*)?|\.\d+)(?:px|em|rem|pt|pc|in|cm|mm|q))', token)), None)
            if width is None:
                width = next(({'thin': '1px', 'medium': '3px', 'thick': '5px'}[token]
                              for token in tokens if token in ('thin', 'medium', 'thick')), '3px')
            sides = ('top', 'right', 'bottom', 'left') if name == 'border' else (name.split('-')[1],)
            for side in sides:
                values[f'border-{side}-width'] = width
                values[f'border-{side}-style'] = border_style
    # Chrome's default fixed font is 13px, even when the surrounding text is 16px.
    # An explicit font-size declaration still wins over this UA default.
    changed = {item[-2] for item in matched}
    def inherited_size_declared(node):
        while node is not None:
            if any(name in ('font-size', 'font') for name, _, _ in _inline_declarations(node)):
                return True
            for selectors, declarations, _counts in rules.candidates(node):
                if (any(name in ('font-size', 'font') for name, _, _ in declarations)
                        and any(_selector_matches(node, selector) for selector in selectors)):
                    return True
            node = getattr(node, 'parentElement', None)
        return False

    if ("font-family" in changed and "font-size" not in changed
            and normalize(values.get("font-family", "")) in ("monospace", "ui-monospace")
            and (parent is None or normalize(inherited.get("font-family", ""))
                 not in ("monospace", "ui-monospace"))
            and not inherited_size_declared(parent)):
        values["font-size"] = "13px"
    # A computed size is absolute in every browser, so a relative one is resolved here,
    # where the parent's own computed size -- and the root's -- are at hand.
    # Initial values and inherited computed values are already resolved. Only
    # declarations applied here need conversion, with the full map as var input.
    if matched:
        values = resolve_computed_values(values, changed)
    for name in ("color", "background-color", "border-top-color", "border-right-color",
                 "border-bottom-color", "border-left-color", "outline-color", "caret-color"):
        value = values.get(name)
        if isinstance(value, str) and value.lower() in SYSTEM_COLORS:
            values[name] = SYSTEM_COLORS[value.lower()]
        elif isinstance(value, str) and value.lower() in NAMED_COLORS:
            color = NAMED_COLORS[value.lower()]
            alpha = (color >> 24) & 255
            if alpha == 0:
                values[name] = 'rgba(0, 0, 0, 0)'
            elif alpha == 255:
                values[name] = f'rgb({(color >> 16) & 255}, {(color >> 8) & 255}, {color & 255})'
    values["font-size"] = _px_text(_font_size_px(values.get("font-size", "16px"),
                                                 parent_size, _root_font_size(element, cache)))
    # Chrome exposes the normal word-spacing computed value as zero length.
    if str(values.get('word-spacing', '')).lower() in ('normal', 'initial'):
        values['word-spacing'] = '0px'
    # A length that counts on the font is resolved here, where both sizes are known:
    # ``10em`` under ``font-size: 20px`` is 200px, and a property left as "10em" would
    # otherwise be read as 10.  Chrome's computed value is absolute for all of these.
    font_size = _px(values["font-size"], 16)
    root_size = _root_font_size(element, cache)
    for name, value in tuple(values.items()):
        if not _font_relative_property(name):
            continue
        terms = str(value).split()
        resolved_any = False
        for index, term in enumerate(terms):
            if not _FONT_RELATIVE_TERM.fullmatch(term):
                continue
            resolved = _font_size_term(term, font_size, root_size)
            if resolved is not None:
                terms[index] = _px_text(resolved)
                resolved_any = True
        if resolved_any:
            values[name] = ' '.join(terms)
    if getattr(element, '_animations', None):
        from ..dom.animation import animated_style
        animated_style(element, values)
    if cache is not None:
        cache[element] = values
    return values


#: The properties whose values are read as lengths, so a font-relative term in them has
#: to be resolved.  ``border-width`` covers the four sides through the prefix rule below.
_FONT_RELATIVE_PROPERTIES = frozenset({
    'width', 'height', 'min-width', 'max-width', 'min-height', 'max-height',
    'top', 'right', 'bottom', 'left', 'letter-spacing', 'word-spacing', 'text-indent',
    'line-height', 'margin', 'padding', 'border-width',
})

_FONT_RELATIVE_TERM = re.compile(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:rem|em|pt|pc|in|cm|mm|q)', re.I)


def _font_relative_property(name):
    """Whether a declaration of *name* carries lengths that ``em``/``rem`` can size."""
    if name in _FONT_RELATIVE_PROPERTIES:
        return True
    if name.startswith(('margin-', 'padding-')):
        return True
    return name.startswith('border-') and name.endswith('-width')


@lru_cache(maxsize=4096)
def _stylesheet_selector(selector):
    """Cache a whole selector list and its per-group specificity, or its failure."""
    try:
        return tuple((group, _specificity(group)) for group in parse_selector(selector))
    except PageSyntaxError:
        return ()


def _selector_matches(element, group):
    """Match a cached stylesheet AST against the current element state."""
    return matches_group(element, group)


def document_rules(document):
    """The document's ``<style>`` rules, cached on ``style_version`` as
    an indexed rule set -- specificity counted once, not per node."""
    if document is None:
        return _EMPTY_RULES
    version = getattr(document, "style_version", 0)
    cached = getattr(document, "_rules_cache", None)
    if cached is not None and cached[0] == version:
        return cached[1]
    rules = _parse_style_elements(document)
    document._rules_cache = (version, rules)
    return rules


def rules_for(element, fallback=None):
    """Style rules for the element's tree, without crossing a shadow boundary."""
    root = element.root_node()
    if getattr(root, '_host', None) is not None:
        document = getattr(element, 'ownerDocument', None)
        version = getattr(document, 'style_version', 0)
        cached = getattr(root, '_rules_cache', None)
        if cached is None or cached[0] != version:
            cached = (version, _parse_style_elements(root))
            root._rules_cache = cached
        return cached[1]
    return fallback if fallback is not None else document_rules(getattr(element, 'ownerDocument', None))


def _parse_style_elements(document):
    rules = []
    for node in document._descendants():
        source = stylesheet_source(node)
        if not source:
            continue
        for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
            parsed = _stylesheet_selector(selector)
            if parsed:
                groups, counts = zip(*parsed)
                rules.append((groups, parse_declarations(block), counts))
    return StyleRules(rules)


def computed_style(element):
    return _cascade(element, rules_for(element))


def computed_style_object(element, *, rendered=True, layout_snapshot):
    """The cascade as a declaration block: the same interface, and nothing may write it back."""
    if not rendered:
        return CSSStyleDeclaration(element=element, computed={})
    document = getattr(element, 'ownerDocument', None)
    boxes, styles = layout_snapshot(document) if document is not None else ({}, {})
    values = dict(styles.get(element) or computed_style(element))
    # Supply this initial value only in the computed view: inserting longhands
    # into the cascade itself can mask user-agent and author shorthands.
    if 'background' not in values:
        values.setdefault('background-color', 'rgba(0, 0, 0, 0)')
    box = boxes.get(element)
    if values.get('display') == 'inline' or (box is None and element.isConnected):
        values.setdefault('width', 'auto')
        values.setdefault('height', 'auto')
    if (box is not None and element.isConnected and values.get('display') != 'none'
            and (values.get('display') != 'inline'
                 or str(getattr(element, 'localName', '')).lower() in
                 ('img', 'canvas', 'iframe', 'video', 'audio', 'input', 'object', 'embed'))):
        for name, extent, sides in (
            ('width', box.width, ('left', 'right')),
            ('height', box.height, ('top', 'bottom')),
        ):
            if values.get('box-sizing', 'content-box') != 'border-box':
                extent -= sum(box.padding.get(side, 0) + box.border.get(side, 0)
                              for side in sides)
            values[name] = f'{max(0.0, extent):.12g}px'
    return CSSStyleDeclaration(element=element, computed=values)

