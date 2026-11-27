"""The SVG geometry interfaces: a rect and the animated lengths its numbers are read
through.

A presentation attribute is the whole truth: a rect's ``width`` is an
``SVGAnimatedLength`` over the attribute of that name, and the ``SVGLength`` behind it
reports the number as written, its unit, and -- for absolute units -- user units.
"""

import re
import struct
import math

from ...idl import interface
from ...foundation.arguments import MISSING, dom_string, number, integer, missing_error
from ...foundation.environment import environment_of
from ..page_errors import IndexSizeError, PageTypeError, NotSupportedError
from ..css.cssom import CSSStyleDeclaration
from ..css.typed import StylePropertyMap
from ..dom.element import Element
from ..css.cascade import computed_style
from ..dom.matrix import identity, _multiply, _rotation, _scaling, _translation, _TWO_D
from ..dom.point import point_init


__all__ = ["SVGAnimatedLength", "SVGElement", "SVGGeometryElement", "SVGGraphicsElement",
           "SVGLength", "SVGRectElement"]


#: The unit types an SVGLength reports, named as the baseline names its constants.
_UNKNOWN, _NUMBER, _PERCENTAGE, _EMS, _EXS, _PX, _CM, _MM, _IN, _PT, _PC = range(11)


#: A length as written: a number, an optional unit, nothing else.
_LENGTH = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-z%]*)$",
                     re.IGNORECASE)


#: What an absolute unit is worth in user units, with the type it reports.
_ABSOLUTE = {"": (_NUMBER, 1.0), "px": (_PX, 1.0), "in": (_IN, 96.0),
             "cm": (_CM, 96.0 / 2.54), "mm": (_MM, 96.0 / 25.4),
             "pt": (_PT, 96.0 / 72.0), "pc": (_PC, 16.0)}
#: The units that mean nothing until something is rendered against them.
_RELATIVE = {"%": _PERCENTAGE, "em": _EMS, "ex": _EXS}


def parse_length(text):
    """``(amount, unitType, specified)`` for a presentation attribute; a missing or
    unparseable one is zero, which is what Chrome reports for those.
    """
    raw = "" if text is None else str(text).strip()
    match = _LENGTH.match(raw)
    if match is None:
        return 0.0, _NUMBER, "0"
    amount, unit = float(match.group(1)), match.group(2).lower()
    if unit in _ABSOLUTE:
        return amount, _ABSOLUTE[unit][0], raw
    if unit in _RELATIVE:
        return amount, _RELATIVE[unit], raw
    return 0.0, _NUMBER, "0"


def user_units(amount, unit_type):
    """The same length in user units, the way ``SVGLength.value`` reports it."""
    entry = next((known for known in _ABSOLUTE.values() if known[0] == unit_type), None)
    if entry is None:
        raise NotSupportedError(
            "Failed to read the 'value' property from 'SVGLength': Could not resolve"
            " relative length.")
    return amount * entry[1]


def _suffix(unit_type):
    """The unit an attribute is written with once a value has been set on it."""
    for unit, (known, _) in _ABSOLUTE.items():
        if known == unit_type:
            return unit
    return ""


@interface('SVGElement', 'Element')
class SVGElement(Element):
    """An SVG element carries an inline style like an HTML one does."""

    def get_style(self):
        """The same inline style object an HTML element hands out."""
        value = getattr(self, "_style_declaration", None)
        if value is None:
            value = self._style_declaration = CSSStyleDeclaration(element=self)
        return value

    def get_attributeStyleMap(self):
        value = getattr(self, "_attribute_style_map", None)
        if value is None:
            value = self._attribute_style_map = StylePropertyMap(element=self)
        return value

    def set_style(self, value):
        self.get_style().set_cssText(value)



@interface('SVGGraphicsElement', 'SVGElement')
class SVGGraphicsElement(SVGElement):
    pass

    def _rect_bounds(self):
        if self.localName != 'rect' or not self._svg_rendered():
            return None
        values = [user_units(*parse_length(self.attributes.get(name))[:2])
                  for name in ('x', 'y', 'width', 'height')]
        return values if values[2] > 0 and values[3] > 0 else None


    def _svg_rendered(self):
        if not self.isConnected:
            return False
        node = self
        while isinstance(node, Element):
            if computed_style(node).get('display') == 'none':
                return False
            node = node.parentElement
        return True

    def _own_bounds(self):
        return self._rect_bounds()


    def fn_getBBox(self, *_extra):
        if not self._svg_rendered():
            return SVGRect()
        bounds = self._own_bounds()
        if bounds is not None:
            return SVGRect(*bounds)
        boxes = [node._own_bounds() for node in self._descendants()
                 if isinstance(node, SVGGraphicsElement)]
        boxes = [box for box in boxes if box is not None]
        if not boxes:
            return SVGRect()
        x, y = min(b[0] for b in boxes), min(b[1] for b in boxes)
        return SVGRect(x, y, max(b[0] + b[2] for b in boxes) - x,
                       max(b[1] + b[3] for b in boxes) - y)


    def fn_getCTM(self, *_extra):
        """Map local user coordinates through ancestor transforms to the SVG viewport."""
        result = identity()
        node = self
        while isinstance(node, SVGGraphicsElement):
            result = _multiply(_transform_matrix(node.attributes.get('transform', '')), result)
            if node.localName == 'svg':
                result = _multiply(_view_box_matrix(node), result)
                break
            node = node.parentElement
        return SVGMatrix([result[index] for index in _TWO_D])



@interface('SVGGeometryElement', 'SVGGraphicsElement')
class SVGGeometryElement(SVGGraphicsElement):
    pass

    def fn_isPointInFill(self, point=None, *_extra):
        if isinstance(point, SVGPoint):
            px, py = point.get_x(), point.get_y()
        else:
            px, py, _, _ = point_init(point)
        bounds = self._rect_bounds()
        if bounds is None:
            return False
        x, y, width, height = bounds
        if not x <= px <= x + width or not y <= py <= y + height:
            return False
        rx = max(0.0, min(width / 2, parse_length(self.attributes.get('rx', self.attributes.get('ry')))[0]))
        ry = max(0.0, min(height / 2, parse_length(self.attributes.get('ry', self.attributes.get('rx')))[0]))
        if not rx or not ry:
            return True
        dx = max(x + rx - px, 0, px - (x + width - rx))
        dy = max(y + ry - py, 0, py - (y + height - ry))
        return (dx / rx) ** 2 + (dy / ry) ** 2 <= 1


@interface('SVGTextContentElement', 'SVGGraphicsElement')
class SVGTextContentElement(SVGGraphicsElement):
    """Horizontal SVG text measured by the same font shaper as document text."""

    def _text_layout(self):
        from ..dom import font_measurement
        if not self._svg_rendered():
            return '', None, None, (0.0, 0.0)
        style = computed_style(self)
        text = self.get_textContent()
        if style.get('white-space', 'normal') not in ('pre', 'pre-wrap', 'break-spaces'):
            text = re.sub(r'[\t\n\r ]+', ' ', text).strip(' ')
        environment = environment_of(self._services)
        run, _face = font_measurement._shaped(text, style, environment, layout_units=True)
        metrics = font_measurement._run_metrics(run, style) if run is not None else None
        metrics = metrics or font_measurement._font_metrics(style, environment)
        position = tuple(parse_length(str(self.attributes.get(name, '0')).split()[0])[0]
                         for name in ('x', 'y'))
        return text, run, metrics, position

    def _own_bounds(self):
        text, run, metrics, (x, y) = self._text_layout()
        if not text or run is None or metrics is None:
            return None
        return x, y - metrics.ascent, run.width, metrics.ascent + metrics.descent

    def fn_getNumberOfChars(self, *_extra):
        return len(self._text_layout()[0])

    def fn_getComputedTextLength(self, *_extra):
        _text, run, _metrics, _position = self._text_layout()
        return _svg_float(run.width) if run is not None else 0.0

    def _character(self, index, method):
        if index is MISSING:
            raise missing_error('SVGTextContentElement', method, 1, 0, constructor=False)
        index = integer(index)
        text, run, metrics, position = self._text_layout()
        if index >= len(text) or run is None:
            raise IndexSizeError(f"Failed to execute '{method}' on 'SVGTextContentElement': The charnum provided ({index}) is greater than or equal to the maximum bound ({len(text)}).")
        return index, run, metrics, position

    def fn_getExtentOfChar(self, charnum=MISSING, *_extra):
        index, run, metrics, (x, y) = self._character(charnum, 'getExtentOfChar')
        return SVGRect(_svg_float(x + run.prefix[index]), _svg_float(y - metrics.ascent),
                       _svg_float(run.prefix[index + 1] - run.prefix[index]),
                       _svg_float(metrics.ascent + metrics.descent))

    def fn_getStartPositionOfChar(self, charnum=MISSING, *_extra):
        index, run, _metrics, (x, y) = self._character(charnum, 'getStartPositionOfChar')
        return SVGPoint(_svg_float(x + run.prefix[index]), _svg_float(y))

    def fn_getEndPositionOfChar(self, charnum=MISSING, *_extra):
        index, run, _metrics, (x, y) = self._character(charnum, 'getEndPositionOfChar')
        return SVGPoint(_svg_float(x + run.prefix[index + 1]), _svg_float(y))

    def fn_getSubStringLength(self, charnum=MISSING, nchars=MISSING, *_extra):
        if charnum is MISSING or nchars is MISSING:
            raise missing_error('SVGTextContentElement', 'getSubStringLength', 2,
                                0 if charnum is MISSING else 1, constructor=False)
        index, run, _metrics, _position = self._character(charnum, 'getSubStringLength')
        end = min(len(run.prefix) - 1, index + integer(nchars))
        return _svg_float(run.prefix[end] - run.prefix[index])


@interface('SVGTextPositioningElement', 'SVGTextContentElement')
class SVGTextPositioningElement(SVGTextContentElement):
    pass


@interface('SVGTextElement', 'SVGTextPositioningElement')
class SVGTextElement(SVGTextPositioningElement):
    pass


@interface('SVGTSpanElement', 'SVGTextPositioningElement')
class SVGTSpanElement(SVGTextPositioningElement):
    pass


def _svg_float(value):
    return struct.unpack('f', struct.pack('f', value))[0]



@interface('SVGLength', None)
class SVGLength:
    """One length of an SVG attribute, as written and in user units."""

    SVG_LENGTHTYPE_UNKNOWN = _UNKNOWN
    SVG_LENGTHTYPE_NUMBER = _NUMBER
    SVG_LENGTHTYPE_PERCENTAGE = _PERCENTAGE
    SVG_LENGTHTYPE_EMS = _EMS
    SVG_LENGTHTYPE_EXS = _EXS
    SVG_LENGTHTYPE_PX = _PX
    SVG_LENGTHTYPE_CM = _CM
    SVG_LENGTHTYPE_MM = _MM
    SVG_LENGTHTYPE_IN = _IN
    SVG_LENGTHTYPE_PT = _PT
    SVG_LENGTHTYPE_PC = _PC

    def __init__(self, *_extra, element=None, name="", **_):
        self._element, self._name = element, str(name)

    def _specified(self):
        text = self._element.attributes.get(self._name) if self._element is not None else None
        return parse_length(text)

    def _write(self, text):
        if self._element is not None:
            self._element.fn_setAttribute(self._name, text)

    def get_unitType(self): return self._specified()[1]
    def get_value(self): return user_units(*self._specified()[:2])
    def get_valueInSpecifiedUnits(self): return self._specified()[0]
    def get_valueAsString(self): return self._specified()[2]
    def set_value(self, value):
        """``value`` is in user units; the attribute keeps the unit it had."""
        self._write("%g%s" % (float(number(value)), _suffix(self._specified()[1])))
    def set_valueInSpecifiedUnits(self, value):
        """``valueInSpecifiedUnits`` is in the attribute's own unit."""
        self._write("%g%s" % (float(number(value)), _suffix(self._specified()[1])))
    def set_valueAsString(self, value): self._write(dom_string(value))


@interface('SVGAnimatedLength', None)
class SVGAnimatedLength:
    """The animated value behind a geometry attribute.

    Nothing animates here, so both name the attribute -- and they are still two objects.
    """

    def __init__(self, *_extra, element=None, name="", **_):
        self._element, self._name = element, str(name)
        self._lengths = {}

    def get_baseVal(self): return self._length("base")
    def get_animVal(self): return self._length("anim")

    def _length(self, slot):
        length = self._lengths.get(slot)
        if length is None:
            length = self._lengths[slot] = SVGLength(element=self._element, name=self._name)
        return length


@interface('SVGRectElement', 'SVGGeometryElement')
class SVGRectElement(SVGGeometryElement):
    """The rectangle, whose six numbers are all presentation attributes."""

    # The six numbers are ``get_<name>`` over baseline members, so the convention binds them.

    def _animated(self, name):
        """The animated value for one of the six, held so a second read is the same
        object, which is what Chrome hands back.
        """
        lengths = getattr(self, "_lengths", None)
        if lengths is None:
            lengths = self._lengths = {}
        animated = lengths.get(name)
        if animated is None:
            animated = lengths[name] = SVGAnimatedLength(element=self, name=name)
        return animated

    def get_x(self): return self._animated("x")
    def get_y(self): return self._animated("y")
    def get_width(self): return self._animated("width")
    def get_height(self): return self._animated("height")
    def get_rx(self): return self._animated("rx")
    def get_ry(self): return self._animated("ry")


@interface('SVGMatrix', None)
class SVGMatrix:
    """The mutable double-precision matrix returned by legacy SVG methods."""

    def __init__(self, values=None, **_):
        self._values = list(values if values is not None else (1, 0, 0, 1, 0, 0))



@interface('SVGPoint', None)
class SVGPoint:
    """The legacy mutable float point, distinct from DOMPoint."""

    def __init__(self, x=0, y=0, **_):
        self.set_x(x)
        self.set_y(y)

    def get_x(self): return self._x
    def get_y(self): return self._y
    def set_x(self, value): self._x = _svg_number(value, 'x', 'SVGPoint')
    def set_y(self, value): self._y = _svg_number(value, 'y', 'SVGPoint')



@interface('SVGRect', None)
class SVGRect:
    """The legacy four-float rectangle returned by SVG geometry APIs."""
    def __init__(self, x=0, y=0, width=0, height=0, **_):
        self.set_x(x);self.set_y(y);self.set_width(width);self.set_height(height)

    @staticmethod
    def _float(value, member):
        return _svg_number(value, member, 'SVGRect')

    def get_x(self): return self._x
    def get_y(self): return self._y
    def get_width(self): return self._width
    def get_height(self): return self._height
    def set_x(self, value): self._x = self._float(value, 'x')
    def set_y(self, value): self._y = self._float(value, 'y')
    def set_width(self, value): self._width = self._float(value, 'width')
    def set_height(self, value): self._height = self._float(value, 'height')



@interface('SVGSVGElement', 'SVGGraphicsElement')
class SVGSVGElement(SVGGraphicsElement):
    def fn_createSVGPoint(self, *_extra):
        return SVGPoint()



def _matrix_getter(index):
    def get(self): return self._values[index]
    return get



def _matrix_setter(index, name):
    def set(self, value): self._values[index] = _svg_number(value, name, 'SVGMatrix', precision='double')
    return set



def _svg_number(value, member, interface, *, precision='float'):
    value = float(number(value))
    converted = value
    if precision == 'float':
        try:
            converted = struct.unpack('f', struct.pack('f', value))[0]
        except OverflowError:
            converted = math.inf
    if not math.isfinite(converted):
        raise PageTypeError(f"Failed to set the '{member}' property on '{interface}': "
                            f"The provided {precision} value is non-finite.")
    return converted



def _transform_matrix(text):
    """SVG transform attributes use unitless lengths and optional rotation centers."""
    result = identity()
    for match in re.finditer(r'([a-zA-Z]+)\s*\(([^()]*)\)', str(text)):
        try:
            args = [float(value) for value in re.split(r'[\s,]+', match[2].strip()) if value]
        except ValueError:
            return identity()
        name = match[1]
        if name == 'matrix' and len(args) == 6:
            matrix = identity()
            for index, value in zip(_TWO_D, args):
                matrix[index] = value
        elif name == 'translate' and len(args) in (1, 2):
            matrix = _translation(args[0], args[1] if len(args) == 2 else 0, 0)
        elif name == 'scale' and len(args) in (1, 2):
            matrix = _scaling(args[0], args[-1], 1)
        elif name == 'rotate' and len(args) in (1, 3):
            matrix = _rotation(0, 0, 1, args[0])
            if len(args) == 3:
                matrix = _multiply(_translation(args[1], args[2], 0),
                                   _multiply(matrix, _translation(-args[1], -args[2], 0)))
        elif name in ('skewX', 'skewY') and len(args) == 1:
            matrix = identity()
            matrix[4 if name == 'skewX' else 1] = math.tan(math.radians(args[0]))
        else:
            return identity()
        result = _multiply(result, matrix)
    return result



def _view_box_matrix(element):
    raw = str(element.fn_getAttribute('viewBox') or '').strip()
    if not raw:
        return identity()
    try:
        x, y, width, height = [float(value) for value in re.split(r'[\s,]+', raw)]
    except ValueError:
        return identity()
    lengths = [parse_length(element.attributes.get(name, default))[:2]
               for name, default in (('width', '300'), ('height', '150'))]
    if any(unit_type in _RELATIVE.values() for _, unit_type in lengths):
        return identity()
    vw, vh = [user_units(*length) for length in lengths]
    if width <= 0 or height <= 0:
        return identity()
    sx, sy = vw / width, vh / height
    aspect = str(element.fn_getAttribute('preserveAspectRatio') or 'xMidYMid meet').split()
    alignment = aspect[0] if aspect else 'xMidYMid'
    tx = ty = 0.0
    if alignment != 'none':
        sx = sy = max(sx, sy) if 'slice' in aspect else min(sx, sy)
        tx = (vw - width * sx) * (1 if 'xMax' in alignment else .5 if 'xMid' in alignment else 0)
        ty = (vh - height * sy) * (1 if 'YMax' in alignment else .5 if 'YMid' in alignment else 0)
    return _multiply(_translation(tx, ty, 0),
                     _multiply(_scaling(sx, sy, 1), _translation(-x, -y, 0)))



for _index, _name in enumerate("abcdef"):
    setattr(SVGMatrix, "get_" + _name, _matrix_getter(_index))
    setattr(SVGMatrix, "set_" + _name, _matrix_setter(_index, _name))
