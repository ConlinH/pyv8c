"""CSSStyleDeclaration and its live inline and computed views."""
from pyv8c import pyv8
from pyv8c.pyv8.flag import V8Attribute
from ...idl import interface
from ...foundation.arguments import MISSING, dom_string, integer, missing_error
from ..dom.utils import OBJECT_PROTOTYPE_NAMES, dash_name as _dash_name
from .values import length_math, serialize_computed_length
from .properties import _CAMEL_NAMES, supported
from .parser import (_NAME_RE, _BOX_SIDES, _property_name, _normalize_value,
                     _valid_width, _valid_font, _color_declaration, _canonical_absolute_length,
                     _expand_box_conflicts, _serialize_declarations, _box_values,
                     parse_declarations)

_OBJECT_NAMES = OBJECT_PROTOTYPE_NAMES | {
    "cssText", "length", "parentRule", "cssFloat", "getPropertyPriority",
    "getPropertyValue", "item", "removeProperty", "setProperty",
}

# Defaults exposed by the computed view. Keep these out of the cascade so
# they cannot override declarations or affect layout shorthand precedence.
_COMPUTED_DEFAULTS = {
    'max-width': 'none', 'max-height': 'none',
    'z-index': 'auto', 'transform': 'none',
    'clear': 'none', 'content': 'normal', 'box-shadow': 'none',
    'table-layout': 'auto', 'aspect-ratio': 'auto', 'vertical-align': 'baseline',
    'background-image': 'none', 'background-repeat': 'repeat',
    'background-position': '0% 0%', 'background-size': 'auto',
    'background-attachment': 'scroll', 'background-clip': 'border-box',
    'background-origin': 'padding-box',
    'top': 'auto', 'right': 'auto', 'bottom': 'auto', 'left': 'auto',
    'float': 'none', 'transition-duration': '0s', 'animation-duration': '0s',
    'flex-direction': 'row',
    'border-radius': '0px', 'overflow-x': 'visible', 'overflow-y': 'visible',
    'text-decoration-line': 'none', 'outline-width': '3px', 'outline-style': 'none',
    'align-items': 'normal', 'justify-content': 'normal', 'gap': 'normal', 'inset': 'auto',
    **{f'{kind}-{side}': '0px' for kind in ('margin', 'padding') for side in _BOX_SIDES},
}

@interface("CSSStyleDeclaration", None)
class CSSStyleDeclaration:
    #: Indexed handlers describe host data; iteration exposure belongs to the IDL baseline.
    __idl_overrides__ = {
        "interface": {
            "index_del": "__v8_index_del__",
            "index_enum": "__v8_index_enum__",
            "index_get": "__v8_index_get__",
            "index_query": "__v8_index_query__",
            "index_set": "__v8_index_set__",
            "length": 0,
            "name_del": "__v8_name_del__",
            "name_enum": "__v8_name_enum__",
            "name_get": "__v8_name_get__",
            "name_query": "__v8_name_query__",
            "name_set": "__v8_name_set__"
        }
    }
    def __init__(self, element=None, block=None, parent_rule=None, computed=None, **_):
        self._element = element
        self._parent_rule = parent_rule
        #: A rule's style is a declaration block rather than an element's attribute, so
        #: the two read from different places and only one of them is ever set.
        self._block = None if block is None else parse_declarations(str(block))
        #: A computed style reads the cascade instead, and nothing writes it back.
        self._computed_values = None if computed is None else dict(computed)
        self._inline_cache = None
    def _declarations(self):
        if self._block is not None:
            return list(self._block)
        if self._computed_values is not None:
            return [(name, serialize_computed_length(name, value), "")
                    for name, value in self._computed_values.items()
                    if value not in ("", None)]
        raw = str(self._element.attributes.get("style", "")) if self._element is not None else ""
        cached = self._inline_cache
        if cached is None or cached[0] != raw:
            cached = self._inline_cache = (raw, tuple(parse_declarations(raw)))
        return list(cached[1])
    def _write(self, declarations):
        if self._computed_values is not None:
            return
        if self._block is not None:
            self._block = list(declarations)
            if self._parent_rule is not None:
                sheet = self._parent_rule._owner_sheet()
                if sheet is not None:
                    sheet.rules_changed()
            return
        if self._element is None: return
        previous_text = str(self._element.attributes.get('style', ''))
        previous_math = getattr(self._element, '_inline_math_values', None)
        retained_math = (previous_math[1] if previous_math is not None
                         and previous_math[0] == previous_text else {})
        text = _serialize_declarations(declarations)
        # Chromium leaves the attribute behind and empties it, so removing the last
        # declaration reads back as "" rather than null.
        if text: self._element.attributes["style"] = text
        elif "style" in self._element.attributes: self._element.attributes["style"] = ""
        # Attribute writes through setAttribute remain visible: _declarations
        # checks the exact text before reusing this declaration block.
        self._inline_cache = (text, tuple(declarations))
        retained_math = {name: pair for name, pair in retained_math.items()
                         if any(n == name and v == pair[0] for n, v, _ in declarations)}
        self._element._inline_math_values = (text, retained_math)
    def _set(self, name, value, priority=""):
        name = _property_name(name)
        if not name: return
        priority = str(dom_string(priority)).strip().lower()
        if priority and priority != "important": return
        value = _normalize_value(value); priority = "important" if priority == "important" else ""
        if (name == 'width' and value and not _valid_width(value)
                or name == 'font' and value and not _valid_font(value)):
            return
        value = _color_declaration(name, value)
        if value is None:
            return
        exact_math = (length_math(value) if name in ('width', 'height')
                      and value.lower().startswith('calc(') and '%' not in value else None)
        value = _canonical_absolute_length(name, value)
        declarations = [item for item in self._declarations() if item[0] != name]
        if value: declarations.append((name, value, priority))
        self._write(_expand_box_conflicts(declarations))
        if self._element is not None and exact_math is not None:
            text, math_values = self._element._inline_math_values
            math_values[name] = (value, exact_math)
    def _value(self, name):
        name = _property_name(name)
        declarations = self._declarations()
        direct = next((v for n, v, _ in declarations if n == name), None)
        if direct is not None:
            return direct
        if name in ('margin', 'padding'):
            items = [next(((v, p) for n, v, p in declarations if n == name + '-' + side), None)
                     for side in _BOX_SIDES]
            if all(item is not None for item in items) and len({item[1] for item in items}) == 1:
                top, right, bottom, left = (item[0] for item in items)
                if left == right:
                    if bottom == top:
                        return top if right == top else f'{top} {right}'
                    return f'{top} {right} {bottom}'
                return f'{top} {right} {bottom} {left}'
        for shorthand in ('padding', 'margin'):
            if name.startswith(shorthand + '-') and name[len(shorthand)+1:] in _BOX_SIDES:
                value = next((v for n, v, _ in declarations if n == shorthand), None)
                parts = _box_values(value) if value is not None else None
                if parts is not None:
                    return parts[_BOX_SIDES.index(name[len(shorthand)+1:])]
        if self._computed_values:
            if name in ('min-width', 'min-height'):
                parent = getattr(self._element, 'parentElement', None)
                if parent is not None:
                    from .cascade import computed_style
                    if computed_style(parent).get('display') in ('flex', 'inline-flex', 'grid', 'inline-grid'):
                        return 'auto'
                return '0px'
            if name.startswith('background-') and 'background' in self._computed_values:
                return ''
            if name in ('top', 'right', 'bottom', 'left') and 'inset' in self._computed_values:
                return ''
            if name == 'transition-duration' and 'transition' in self._computed_values:
                return ''
            if name == 'animation-duration' and 'animation' in self._computed_values:
                return ''
            if name == 'flex-direction' and 'flex-flow' in self._computed_values:
                return ''
            blocked_by = {
                'border-radius': ('border-top-left-radius', 'border-top-right-radius',
                                  'border-bottom-left-radius', 'border-bottom-right-radius'),
                'overflow-x': ('overflow',), 'overflow-y': ('overflow',),
                'text-decoration-line': ('text-decoration',),
                'outline-width': ('outline',), 'outline-style': ('outline',),
                'align-items': ('place-items',), 'justify-content': ('place-content',),
                'gap': ('row-gap', 'column-gap'),
                'inset': ('top', 'right', 'bottom', 'left'),
            }
            if any(key in self._computed_values for key in blocked_by.get(name, ())):
                return ''
            if name == 'outline-color' and not any(key.startswith('outline') for key in self._computed_values):
                return self._value('color')
            if name.startswith('border-') and not any(
                    key in ('border', 'border-width', 'border-style', 'border-color')
                    or any(key == f'border-{side}' or key.startswith(f'border-{side}-')
                           for side in _BOX_SIDES) for key in self._computed_values):
                if name.endswith('-style'):
                    return 'none'
                if name.endswith('-width'):
                    return '0px'
                if name.endswith('-color'):
                    return self._value('color')
            return _COMPUTED_DEFAULTS.get(name, "")
        return ""
    def get_cssText(self):
        return _serialize_declarations(self._declarations())
    def set_cssText(self, value):
        self._write(parse_declarations(dom_string(value)))
        if self._element is not None and self._computed_values is None:
            self._element._inline_math_values = (
                str(self._element.attributes.get('style', '')), {})
    def _indexed_names(self):
        names = []
        for name, value, _ in self._declarations():
            if self._block is None and name in ('padding', 'margin') and _box_values(value) is not None:
                names.extend(f'{name}-{side}' for side in _BOX_SIDES)
            else:
                names.append(name)
        return names
    def get_length(self): return len(self._indexed_names())
    def get_parentRule(self): return self._parent_rule or pyv8.Null
    def get_cssFloat(self): return self._value("float")
    def set_cssFloat(self, value): self._set("float", value)
    def fn_getPropertyPriority(self, name=MISSING, *_extra):
        if name is MISSING: raise missing_error("CSSStyleDeclaration", "getPropertyPriority", 1, 0)
        name = _property_name(name)
        declarations = self._declarations()
        direct = next((p for n, _, p in declarations if n == name), None)
        if direct is not None:
            return direct
        for shorthand in ('padding', 'margin'):
            if name.startswith(shorthand + '-') and name[len(shorthand)+1:] in _BOX_SIDES:
                return next((p for n, v, p in declarations
                             if n == shorthand and _box_values(v) is not None), "")
        return ""
    def fn_getPropertyValue(self, name=MISSING, *_extra):
        if name is MISSING: raise missing_error("CSSStyleDeclaration", "getPropertyValue", 1, 0)
        return self._value(name)
    def fn_item(self, index=MISSING, *_extra):
        if index is MISSING: raise missing_error("CSSStyleDeclaration", "item", 1, 0)
        index = integer(index); names = self._indexed_names()
        return names[index] if 0 <= index < len(names) else ""
    def fn_removeProperty(self, name=MISSING, *_extra):
        if name is MISSING: raise missing_error("CSSStyleDeclaration", "removeProperty", 1, 0)
        name = _property_name(name)
        old = self._value(name)
        declarations = self._declarations()
        for shorthand in ('margin', 'padding'):
            if name.startswith(shorthand + '-') and name[len(shorthand)+1:] in _BOX_SIDES:
                declarations = _expand_box_conflicts(declarations, force=shorthand)
        remove = {name}
        if name in ('margin', 'padding'):
            remove.update(name + '-' + side for side in _BOX_SIDES)
        self._write([item for item in declarations if item[0] not in remove]); return old
    def fn_setProperty(self, name=MISSING, value=MISSING, priority="", *_extra):
        if name is MISSING: raise missing_error("CSSStyleDeclaration", "setProperty", 2, 0)
        if value is MISSING: raise missing_error("CSSStyleDeclaration", "setProperty", 2, 1)
        self._set(name, value, priority)
    def _named(self, name):
        if not isinstance(name, str) or name in _OBJECT_NAMES: return None
        dashed = _dash_name(name)
        if not _NAME_RE.match(dashed): return None
        value = self._value(dashed)
        if value: return value
        # A supported but undeclared property reads as ""; an unknown name stays undefined.
        return "" if supported(dashed) else None
    def _exists(self, name):
        """Whether the engine resolves this name to a property at all."""
        dashed = _dash_name(name)
        if not _NAME_RE.match(dashed): return False
        if supported(dashed): return True
        declared = _property_name(dashed)
        return any(_property_name(declared_name) == declared for declared_name, _, _ in self._declarations())
    def __v8_name_get__(self, name): return self._named(name)
    def __v8_name_set__(self, name, value):
        if not isinstance(name, str) or name in _OBJECT_NAMES: return None
        dashed = _dash_name(name)
        if not _NAME_RE.match(dashed): return None
        self._set(dashed, value); return True
    def __v8_name_query__(self, name):
        if not isinstance(name, str) or name in _OBJECT_NAMES: return None
        return 0 if self._exists(name) else None
    def __v8_name_del__(self, name):
        if isinstance(name, str) and name not in _OBJECT_NAMES: self._set(_dash_name(name), "")
        return True
    def __v8_name_enum__(self):
        """Every supported property, in the engine's own order.

        Declaring one changes no position and a custom property is not listed at all.
        """
        return list(_CAMEL_NAMES)
    def __v8_index_get__(self, index): return self.fn_item(index)
    def __v8_index_set__(self, index, value): return None
    def __v8_index_query__(self, index): return V8Attribute.ReadOnly if self.fn_item(index) else None
    def __v8_index_del__(self, index): return None
    def __v8_index_enum__(self): return self.get_length()


