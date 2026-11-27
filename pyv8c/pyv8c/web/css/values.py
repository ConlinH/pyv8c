"""Computed values for custom properties, numeric colors and CSS transforms.

This handles numeric calc expressions and absolute transform lengths. Relative
lengths still belong to layout and are left unresolved here.
"""
import ast
import math
import operator
import re
import struct


_ABSOLUTE_LENGTH = re.compile(
    r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(px|in|cm|mm|q|pt|pc)$', re.I)
_ABSOLUTE_FACTORS = {
    'px': 1.0, 'in': 96.0, 'cm': 96.0 / 2.54,
    'mm': 96.0 / 25.4, 'q': 96.0 / 101.6,
    'pt': 96.0 / 72.0, 'pc': 16.0,
}
_ABSOLUTE_LENGTH_PROPERTIES = frozenset({
    'width', 'height', 'min-width', 'min-height', 'max-width', 'max-height',
    'left', 'right', 'top', 'bottom', 'flex-basis', 'column-width',
    'letter-spacing', 'word-spacing', 'line-height', 'text-indent',
    'outline-width',
    *(f'{kind}-{side}' for kind in ('margin', 'padding')
      for side in ('top', 'right', 'bottom', 'left')),
    *(f'border-{side}-width' for side in ('top', 'right', 'bottom', 'left')),
})


def absolute_length_px(value):
    """An absolute CSS length in px; unrelated values stay with their own parser."""
    match = _ABSOLUTE_LENGTH.fullmatch(str(value).strip())
    if match is None:
        return None
    return float(match[1]) * _ABSOLUTE_FACTORS[match[2].lower()]


def _layout_px(value):
    # Blink stores used geometry in 1/64 CSS-pixel units, truncated toward zero.
    return math.trunc(value * 64) / 64


def _css_float(value):
    """Blink stores evaluated calculation lengths as single-precision pixels."""
    try:
        return struct.unpack('=f', struct.pack('=f', value))[0]
    except OverflowError:
        return math.copysign(3.4028234663852886e38, value)


def _computed_absolute_length(name, value):
    pixels = absolute_length_px(value)
    if pixels is None and str(value).strip().lower().startswith('calc(') and '%' not in value:
        pixels = length_math(value)
    if pixels is None or str(value).strip().lower().endswith('px'):
        return value
    if name in ('width', 'height'):
        pixels = _layout_px(_css_float(pixels) if str(value).strip().lower().startswith('calc(')
                            else pixels)
    return f'{pixels}px'


def serialize_computed_length(name, value):
    """Expose Blink's four-decimal CSSOM text without losing layout precision."""
    if name not in _ABSOLUTE_LENGTH_PROPERTIES or not str(value).endswith('px'):
        return value
    match = _ABSOLUTE_LENGTH.fullmatch(str(value))
    if match is None:
        return value
    text = f'{float(match[1]):.6g}'
    return f'{text}px'

from ..dom.matrix import identity, _float32, _multiply, _rotation, _scaling, _translation, _is_2d, _TWO_D


def _parts(text, separator=','):
    start = depth = 0
    parts = []
    for index, char in enumerate(text):
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == separator and not depth:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return parts


def variables(text, values, seen=()):
    match = re.search(r'var\(', text, re.I)
    if match is None:
        return text
    if len(seen) > 32:
        return None
    begin, depth = match.end(), 1
    for end in range(begin, len(text)):
        depth += (text[end] == '(') - (text[end] == ')')
        if depth:
            continue
        arguments = _parts(text[begin:end])
        name = arguments[0]
        if name in seen:
            return None
        value = values.get(name)
        if value is None:
            value = ','.join(arguments[1:]) if len(arguments) > 1 else None
        if value is None:
            return None
        value = variables(str(value), values, (*seen, name))
        if value is None:
            return None
        return variables(text[:match.start()] + value + text[end+1:], values, seen)
    return None


_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}


def _blink_sin_cos(degrees):
    """Blink's SinCosDegrees range reduction for CSS sin()/cos()."""
    if -90_000_000.0 < degrees < 90_000_000.0:
        multiple = degrees / 45.0
        octant = int(multiple)
        if multiple == octant:
            diagonal = math.sqrt(2.0) / 2.0
            return ((0.0, 1.0), (diagonal, diagonal), (1.0, 0.0),
                    (diagonal, -diagonal), (0.0, -1.0),
                    (-diagonal, -diagonal), (-1.0, 0.0),
                    (-diagonal, diagonal))[octant & 7]
        if degrees < 0:
            octant -= 1
        degrees -= octant * 45.0
        if octant & 1:
            degrees = 45.0 - degrees
        radians = degrees * (math.pi / 180.0)
        sine, cosine = math.sin(radians), math.cos(radians)
        if (octant + 1) & 2:
            sine, cosine = cosine, sine
        if octant & 4:
            sine = -sine
        if (octant + 2) & 4:
            cosine = -cosine
        return sine, cosine
    radians = math.fmod(degrees, 360.0) * (math.pi / 180.0)
    return math.sin(radians), math.cos(radians)


def _blink_trig(name, radians):
    degrees = radians * (180.0 / math.pi)
    if name == 'sin':
        return _blink_sin_cos(degrees)[0]
    if name == 'cos':
        return _blink_sin_cos(degrees)[1]
    if -90_000_000.0 < degrees < 90_000_000.0:
        multiple = degrees / 45.0
        if multiple == int(multiple):
            return (0.0, 1.0, math.inf, -1.0, 0.0, 1.0,
                    -math.inf, -1.0)[int(multiple) & 7]
    return math.tan(degrees * (math.pi / 180.0))


def _arithmetic(text):
    tree = ast.parse(text.strip(), mode='eval')
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError('expression is too large')
    def value(node):
        if isinstance(node, ast.Expression):
            return value(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in ('pi', 'e'):
            return getattr(math, node.id)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            return _BINARY[type(node.op)](value(node.left), value(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return value(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            arguments = [value(arg) for arg in node.args]
            if node.func.id in ('min', 'max') and arguments:
                return (min if node.func.id == 'min' else max)(arguments)
            if node.func.id == 'clamp' and len(arguments) == 3:
                low, preferred, high = arguments
                return max(low, min(preferred, high))
            if node.func.id in ('sin', 'cos', 'tan') and len(arguments) == 1:
                return _blink_trig(node.func.id, arguments[0])
            if node.func.id == 'sqrt' and len(arguments) == 1:
                return math.sqrt(arguments[0])
        raise ValueError('not a numeric expression')
    result = value(tree)
    if not math.isfinite(result):
        raise ValueError('non-finite result')
    return result


def length_math(text, reference=0.0):
    """Absolute-pixel and percentage math, once custom properties are resolved."""
    expression = re.sub(r'calc\(', '(', str(text), flags=re.I)
    expression = re.sub(
        r'((?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(px|%)',
        lambda match: (match[1] if match[2].lower() == 'px' else
                       f'({match[1]}*{float(reference)!r}/100)'),
        expression, flags=re.I,
    )
    try:
        return _arithmetic(expression)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
        return None


def affine_length(text):
    """Compile length arithmetic to absolute pixels and a containing-size coefficient."""
    expression = re.sub(r'calc\(', '(', text, flags=re.I)
    expression = re.sub(
        r'((?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(px|%)',
        lambda m: m[1] if m[2].lower() == 'px' else f'({m[1]}/100*basis)',
        expression, flags=re.I,
    )

    def coefficients(node):
        if isinstance(node, ast.Name) and node.id == 'basis':
            return 0.0, 1.0
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            pixels, ratio = coefficients(node.operand)
            return (-pixels, -ratio) if isinstance(node.op, ast.USub) else (pixels, ratio)
        if isinstance(node, ast.BinOp):
            left, left_ratio = coefficients(node.left)
            right, right_ratio = coefficients(node.right)
            if isinstance(node.op, ast.Add):
                return left + right, left_ratio + right_ratio
            if isinstance(node.op, ast.Sub):
                return left - right, left_ratio - right_ratio
            if isinstance(node.op, ast.Mult) and not (left_ratio and right_ratio):
                return left * right, left * right_ratio + right * left_ratio
            if isinstance(node.op, ast.Div) and not right_ratio:
                return left / right, left_ratio / right
        return _arithmetic(ast.unparse(node)), 0.0

    try:
        tree = ast.parse(expression.strip(), mode='eval')
        if sum(1 for _ in ast.walk(tree)) <= 128:
            result = coefficients(tree.body)
            if all(map(math.isfinite, result)):
                return result
    except (ValueError, SyntaxError, ArithmeticError, RecursionError):
        pass
    return None


def numeric_calcs(text):
    # Work from the innermost calc outward, without evaluating arbitrary code.
    for _ in range(32):
        start = text.lower().rfind('calc(')
        if start == -1:
            return text
        depth, end = 1, start + 5
        while end < len(text) and depth:
            depth += (text[end] == '(') - (text[end] == ')')
            end += 1
        if depth:
            return text
        try:
            result = _arithmetic(text[start+5:end-1])
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
            return text
        text = text[:start] + format(result, '.15g') + text[end:]
    return text


def numeric_color(text):
    match = re.fullmatch(r'(rgb|rgba)\(([^()]*)\)', text.strip(), re.I)
    if match is None:
        return text
    arguments = _parts(match[2])
    if len(arguments) not in (3, 4):
        return text
    try:
        colors = [max(0, min(255, math.floor(float(arg.rstrip('%')) *
                  (2.55 if arg.endswith('%') else 1) + .5))) for arg in arguments[:3]]
        alpha = max(0, min(1, float(arguments[3]))) if len(arguments) == 4 else 1
    except (ValueError, OverflowError):
        return text
    if alpha == 1:
        return 'rgb(' + ', '.join(map(str, colors)) + ')'
    return 'rgba(' + ', '.join(map(str, colors)) + ', ' + format(alpha, '.6g') + ')'


def _scalar(text, angle=False):
    match = re.fullmatch(r'([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-z]*)', text, re.I)
    if match is None:
        raise ValueError('unsupported transform value')
    value, unit = float(match[1]), match[2].lower()
    units = {'': 1, 'deg': 1, 'turn': 360, 'grad': .9, 'rad': 180/math.pi} if angle else {'': 1, 'px': 1}
    if unit not in units or not math.isfinite(value):
        raise ValueError('unsupported transform unit')
    return value * units[unit]


def transform(text):
    text = text.strip()
    if not text or text == 'none':
        return text
    functions = list(re.finditer(r'([a-zA-Z0-9]+)\(([^()]*)\)', text))
    if not functions or re.sub(r'([a-zA-Z0-9]+)\(([^()]*)\)', '', text).strip():
        return text
    result, three_d = identity(), False
    try:
        for function in functions:
            name = function[1].lower()
            arguments = _parts(function[2]) if ',' in function[2] else function[2].split()
            values = [_scalar(arg, angle=name.startswith('rotate') and
                      (name != 'rotate3d' or i == 3) or name.startswith('skew'))
                      for i, arg in enumerate(arguments)]
            matrix = identity()
            three_d |= name.endswith(('3d', 'z')) or name in ('rotatex', 'rotatey', 'perspective')
            if name == 'matrix' and len(values) == 6:
                for index, value in zip(_TWO_D, values):
                    matrix[index] = value
            elif name == 'matrix3d' and len(values) == 16:
                matrix = values
            elif name == 'perspective' and len(values) == 1:
                matrix[11] = -1 / max(1, values[0])
            elif name == 'rotate3d' and len(values) == 4:
                matrix = _rotation(*(_float32(value) for value in values[:3]), values[3])
            elif name in ('rotate', 'rotatex', 'rotatey', 'rotatez') and len(values) == 1:
                matrix = _rotation(int(name == 'rotatex'), int(name == 'rotatey'),
                                   int(name in ('rotate', 'rotatez')), values[0])
            elif name == 'scale3d' and len(values) == 3:
                matrix = _scaling(*values)
            elif name == 'scale' and len(values) in (1, 2):
                matrix = _scaling(values[0], values[-1], 1)
            elif name in ('scalex','scaley','scalez') and len(values) == 1:
                scales = [1, 1, 1]; scales['xyz'.index(name[-1])] = values[0]
                matrix = _scaling(*scales)
            elif name == 'translate3d' and len(values) == 3:
                matrix = _translation(*values)
            elif name == 'translate' and len(values) in (1, 2):
                matrix = _translation(values[0], values[1] if len(values) == 2 else 0, 0)
            elif name in ('translatex','translatey','translatez') and len(values) == 1:
                offsets = [0, 0, 0]; offsets['xyz'.index(name[-1])] = values[0]
                matrix = _translation(*offsets)
            elif name in ('skew','skewx','skewy') and len(values) in (1, 2):
                if name != 'skewy':
                    matrix[4] = math.tan(math.radians(values[0]))
                if name != 'skewx':
                    matrix[1] = math.tan(math.radians(values[-1] if name == 'skewy' or len(values) == 2 else 0))
            else:
                return text
            result = _multiply(result, matrix)
    except (ValueError, OverflowError):
        return text
    three_d |= not _is_2d(result)
    numbers = result if three_d else [result[index] for index in _TWO_D]
    return ('matrix3d(' if three_d else 'matrix(') + ', '.join(
        format(0 if abs(value) < 1e-12 else value, '.6g') for value in numbers) + ')'


def resolve(values, properties=None):
    result = dict(values)
    for name in values if properties is None else properties:
        raw = values[name]
        if name.startswith('--') or not isinstance(raw, str):
            continue
        value = variables(raw, values) if 'var(' in raw.lower() else raw
        if value is None:
            result[name] = ''
            continue
        if 'calc(' in value.lower():
            value = numeric_calcs(value)
        if name == 'color' or name.endswith('-color'):
            value = numeric_color(value)
        elif name == 'font-weight':
            value = {'normal': '400', 'bold': '700'}.get(value.lower(), value)
        elif name == 'font-family':
            families = [part.strip() for part in value.split(',')]
            value = ', '.join(
                f'"{part}"' if ' ' in part and not part.startswith(('"', "'"))
                else part for part in families)
        elif name == 'transform':
            value = transform(value)
        elif name in _ABSOLUTE_LENGTH_PROPERTIES:
            value = _computed_absolute_length(name, value)
        result[name] = value
    return result
