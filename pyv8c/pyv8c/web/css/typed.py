"""CSS Typed OM: the value objects, and the map an inline style exposes.

The map reads and writes the same ``style`` attribute ``CSSStyleDeclaration`` does, so
the two views of one element never disagree.
"""
import re

from pyv8c import pyv8
from ...idl import interface
from ...foundation.arguments import MISSING, dom_string, missing_error
from ..page_errors import PageTypeError
from . import properties, parser
from ..dom.utils import dash_name


#: ``<number>`` with an optional unit, the values Typed OM answers as a ``CSSUnitValue``.
_NUMBER = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))"
                     r"(px|%|em|rem|ex|ch|vw|vh|vmin|vmax|cm|mm|in|pt|pc|q|"
                     r"deg|grad|rad|turn|s|ms|hz|khz|dpi|dpcm|dppx|fr)?$", re.I)


def _unit_value(text):
    """The ``CSSUnitValue`` *text* names, or ``None`` when it does not name one."""
    match = _NUMBER.match(str(text).strip())
    if match is None:
        return None
    return CSSUnitValue(float(match.group(1)), match.group(2) or "number")


def _value(text):
    """What ``get`` answers for one declaration."""
    return _unit_value(text) or CSSStyleValue(text)


def _property(value, method, interface_name, wording="Invalid propertyName"):
    """A property name a Typed OM call accepts, as Chrome validates it.

    Custom properties keep their spelling; a real one has to be written the way CSS
    writes it, which is why ``marginTop`` is refused where ``margin-top`` is not.
    """
    name = str(dom_string(value))
    if name.startswith("--"):
        if properties.supported(name):
            return name
    elif properties.supported(name) and name == dash_name(name).lstrip("-").lower():
        return name
    raise PageTypeError(f"Failed to execute '{method}' on '{interface_name}': {wording}: {name}")


def _declaration(name, value, method):
    """The declaration text for one property, or a ``TypeError`` as Chrome raises it."""
    declarations = parser.parse_declarations(f"{name}: {dom_string(value)}")
    if not declarations:
        raise PageTypeError(f"Failed to execute '{method}' on 'StylePropertyMap':"
                          " Invalid type for property")
    return str(declarations[0][1])




@interface('CSSStyleValue', None)
class CSSStyleValue:
    def __init__(self, text="", **_):
        self._text = str(text)

    def fn_toString(self, *_extra):
        return self._serialize()

    def _serialize(self):
        return self._text

    @staticmethod
    def fn_parse(_realm, property=MISSING, value=MISSING, *_extra):
        if property is MISSING:
            raise missing_error('CSSStyleValue', 'parse', 2, 0, constructor=False)
        if value is MISSING:
            raise missing_error('CSSStyleValue', 'parse', 2, 1, constructor=False)
        name = _property(property, 'parse', 'CSSStyleValue')
        return _value(_declaration(name, value, 'parse'))

    @staticmethod
    def fn_parseAll(_realm, property=MISSING, value=MISSING, *_extra):
        if property is MISSING:
            raise missing_error('CSSStyleValue', 'parseAll', 2, 0, constructor=False)
        if value is MISSING:
            raise missing_error('CSSStyleValue', 'parseAll', 2, 1, constructor=False)
        name = _property(property, 'parseAll', 'CSSStyleValue')
        text = _declaration(name, value, 'parseAll')
        return [] if not text else [_value(text)]


@interface('CSSNumericValue', 'CSSStyleValue')
class CSSNumericValue(CSSStyleValue):
    pass


@interface('CSSUnitValue', 'CSSNumericValue')
class CSSUnitValue(CSSNumericValue):
    def __init__(self, value=0.0, unit="number", **_):
        self._value = float(value)
        self._unit = str(unit)

    def get_value(self):
        return self._value

    def get_unit(self):
        return self._unit

    def _serialize(self):
        return f"{self._value:g}" if self._unit == "number" else f"{self._value:g}{self._unit}"


@interface('CSSKeywordValue', 'CSSStyleValue')
class CSSKeywordValue(CSSStyleValue):
    def __init__(self, value=MISSING, **_):
        if value is MISSING:
            raise missing_error('CSSKeywordValue', 'CSSKeywordValue', 1, 0, constructor=True)
        self._value = str(dom_string(value))

    def get_value(self):
        return self._value

    def _serialize(self):
        return self._value


@interface('StylePropertyMapReadOnly', None)
class StylePropertyMapReadOnly:
    #: ``keys``/``values``/``entries`` and ``Symbol.iterator`` hand out pairs from here.
    __idl_overrides__ = {
        "interface": {

            "length": 0,
        }
    }

    def __init__(self, element=None, computed=False, **_):
        self._element = element
        self._computed = bool(computed)

    def _declarations(self):
        if self._computed:
            if self._element is None or not self._element.isConnected:
                return ()
            from .cascade import computed_style
            values = computed_style(self._element)
            return tuple((name, value, '') for name, value in sorted(values.items()))
        raw = str(self._element.attributes.get("style", "")) if self._element is not None else ""
        return parser.parse_declarations(raw)

    def _entry(self, name):
        for declared, value, _priority in self._declarations():
            if declared == name:
                return value
        return None

    def _style(self):
        return self._element.get_style()

    def get_size(self):
        return len(self._declarations())

    def fn_has(self, property=MISSING, *_extra):
        if property is MISSING:
            raise missing_error('StylePropertyMapReadOnly', 'has', 1, 0, constructor=False)
        return self._entry(_property(property, 'has', 'StylePropertyMapReadOnly')) is not None

    def fn_get(self, property=MISSING, *_extra):
        if property is MISSING:
            raise missing_error('StylePropertyMapReadOnly', 'get', 1, 0, constructor=False)
        text = self._entry(_property(property, 'get', 'StylePropertyMapReadOnly'))
        return pyv8.Undefined if text is None else _value(text)

    def fn_getAll(self, property=MISSING, *_extra):
        if property is MISSING:
            raise missing_error('StylePropertyMapReadOnly', 'getAll', 1, 0, constructor=False)
        text = self._entry(_property(property, 'getAll', 'StylePropertyMapReadOnly'))
        return [] if text is None else [_value(text)]

    def __v8_iteration_source__(self):
        items = iter(tuple((name, [_value(text)]) for name, text, _priority in self._declarations()))
        return lambda: next(items, None)



@interface('StylePropertyMap', 'StylePropertyMapReadOnly')
class StylePropertyMap(StylePropertyMapReadOnly):
    __idl_overrides__ = {
        "interface": {

            "length": 0,
        }
    }

    def fn_set(self, property=MISSING, value=MISSING, *_extra):
        if property is MISSING:
            raise missing_error('StylePropertyMap', 'set', 2, 0, constructor=False)
        if value is MISSING:
            raise missing_error('StylePropertyMap', 'set', 2, 1, constructor=False)
        name = _property(property, 'set', 'StylePropertyMap')
        self._style().fn_setProperty(name, _declaration(name, value, 'set'))

    def fn_append(self, property=MISSING, value=MISSING, *_extra):
        if property is MISSING:
            raise missing_error('StylePropertyMap', 'append', 2, 0, constructor=False)
        if value is MISSING:
            raise missing_error('StylePropertyMap', 'append', 2, 1, constructor=False)
        name = _property(property, 'append', 'StylePropertyMap')
        _declaration(name, value, 'append')
        # Without a per-property grammar this backend cannot tell a list-valued property
        # from a single-value one, and Chrome refuses the ones that cannot hold a list.
        raise PageTypeError("Failed to execute 'append' on 'StylePropertyMap':"
                          " Property does not support multiple values")

    def fn_delete(self, property=MISSING, *_extra):
        if property is MISSING:
            raise missing_error('StylePropertyMap', 'delete', 1, 0, constructor=False)
        self._style().fn_removeProperty(
            _property(property, 'delete', 'StylePropertyMap', 'Invalid property name'))

    def fn_clear(self, *_extra):
        self._style().set_cssText("")

__all__ = ["CSSKeywordValue", "CSSNumericValue", "CSSStyleValue", "CSSUnitValue",
           "StylePropertyMap", "StylePropertyMapReadOnly"]
