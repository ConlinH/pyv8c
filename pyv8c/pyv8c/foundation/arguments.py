"""Explicit conversion primitives, called by implementations in their required order."""

import math
import re
from typing import Any

from pyv8c import pyv8


class IdlArgumentError(pyv8.JsTypeError):
    """An IDL coercion or arity failure: what a member reports for a value it was handed
    and cannot convert.

    Inheriting the binding's own ``TypeError`` is what a realm shows it as, and a caller
    catching the builtin still catches it, since that is what the binding inherits too.
    """


MISSING = object()


def missing_error(interface, member, required, present, *, constructor=False):
    prefix = f"Failed to construct '{interface}'" if constructor else f"Failed to execute '{member}' on '{interface}'"
    noun = "argument" if required == 1 else "arguments"
    return IdlArgumentError(f"{prefix}: {required} {noun} required, but only {present} present.")


def is_nullish(value):
    return value is None or value is pyv8.Null or value is pyv8.Undefined


def abort_signal(value):
    """*value* as an ``AbortSignal``, or ``None`` for a nullish one.

    Every surface that reads a signal out of a dictionary converts it here so they
    all fail alike.  The match is by interface name, because the interface itself is
    built on top of the layer that asks.
    """
    if is_nullish(value):
        return None
    if _matches_interface(value, "AbortSignal"):
        return value
    raise IdlArgumentError("Failed to convert value to 'AbortSignal'.")

def _matches_interface(value, expected):
    """Whether *value* is one of the interfaces *expected* names, answered once per type."""
    key = (type(value), expected)
    matched = _INTERFACE_CHECKS.get(key)
    if matched is None:
        matched = any(getattr(getattr(base, "__idl_spec__", None), "name", None) == expected
                      for base in type(value).__mro__)
        _INTERFACE_CHECKS[key] = matched
    return matched


def dom_string(value):
    if type(value) is str:
        return value
    if isinstance(value, (pyv8.JSObject, pyv8.JSValue)):
        return value._to_string()
    if isinstance(value, str):
        return value
    if value is None or value is pyv8.Null:
        return "null"
    if value is pyv8.Undefined:
        return "undefined"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (list, tuple)):
        return ",".join("" if is_nullish(item) else dom_string(item) for item in value)
    if isinstance(value, dict):
        callback = value.get("toString")
        if callable(callback):
            return dom_string(callback())
        return "[object Object]"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"
        if value.is_integer():
            return str(int(value))
    if getattr(type(value), "__idl_spec__", None) is not None:
        #: A native callback unwraps a Web object to its Python owner, but IDL
        #: conversion runs on the value the script passed -- the JS wrapper -- so an
        #: overridden ``toString``/``Symbol.toPrimitive`` is honoured and its error
        #: reaches the caller.  ``str(owner)`` would put a Python repr into the page.
        context = pyv8.current_context()
        if context is not None:
            wrapper = context.js_obj(value)
            if wrapper is not None:
                return wrapper._to_string()
    return str(value)


def usv_string(value):
    return dom_string(value).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def byte_string(value, *, prefix=""):
    result = dom_string(value)
    if any(ord(character) > 255 for character in result):
        raise IdlArgumentError(prefix + "String contains non ISO-8859-1 code point.")
    return result


def boolean(value):
    if isinstance(value, pyv8.JSValue):
        return value._to_boolean()
    if is_nullish(value):
        return False
    if isinstance(value, (str, int, float, bool)):
        return bool(value) and not (isinstance(value, float) and math.isnan(value))
    return True


def number(value):
    if isinstance(value, (pyv8.JSObject, pyv8.JSValue)):
        return value._to_number()
    if value is None or value is pyv8.Null:
        return 0.0
    if value is pyv8.Undefined:
        return math.nan
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, dict):
        callback = value.get("valueOf")
        if callable(callback):
            converted = callback()
            if not isinstance(converted, (dict, list, tuple, pyv8.JSObject)):
                return number(converted)
    text = dom_string(value).strip()
    if not text:
        return 0.0
    try:
        if text.lower().startswith(("0x", "0b", "0o")):
            return float(int(text, 0))
        return float(text)
    except ValueError:
        return math.nan


def integer(value, *, bits=32, signed=False, enforce_range=False):
    result = number(value)
    limit = 2**bits
    if enforce_range:
        minimum = -(limit // 2) if signed else 0
        maximum = limit // 2 - 1 if signed else limit - 1
        if not math.isfinite(result) or not minimum <= math.trunc(result) <= maximum:
            raise IdlArgumentError("Value is outside the permitted range.")
        return math.trunc(result)
    if not math.isfinite(result) or result == 0:
        return 0
    result = math.trunc(result) % limit
    return result - limit if signed and result >= limit // 2 else result


class Dictionary(dict):
    """A lazy view: only an implementation's explicit get reads a JS member."""

    def __init__(self, value):
        self.value = value

    def get(self, name, default=None):
        result = self.value._get(name)
        return default if result is pyv8.Undefined else result

    def __getitem__(self, name):
        return self.value._get(name)

    def __contains__(self, name):
        return self.value._get(name) is not pyv8.Undefined

    def items(self):
        return ((name, self.value._get(name)) for name in self.value._keys())


def materialize(value):
    """Explicit data snapshot, for algorithms such as structured cloning."""
    return value.to_python() if isinstance(value, pyv8.JSObject) else value


def dictionary(value, *, name="Object", prefix=""):
    if is_nullish(value):
        return {}
    if isinstance(value, pyv8.JSObject):
        return Dictionary(value)
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)) or callable(value):
        return {}
    raise IdlArgumentError(f"{prefix}The provided value is not of type '{name}'.")


def event_init(options):
    """An event constructor's EventInit dictionary as a readable mapping.

    ``undefined``/``null`` mean an empty EventInit; a plain Python dict passes through
    so the host can drive the private ``isTrusted``/``persisted`` channels.
    """
    if is_nullish(options):
        return {}
    if isinstance(options, dict):
        return options
    return dictionary(options)


def event_constructor_arguments(interface, event_type, options):
    """Validate ``(type, init)`` for an event constructor."""
    if event_type is MISSING:
        raise missing_error(interface, 'constructor', 1, 0, constructor=True)
    return dom_string(event_type), event_init(options)


def sequence(value, convert=None, *, iterator_method=MISSING):
    if isinstance(value, (str, dict)) or is_nullish(value):
        raise IdlArgumentError("The object must have a callable @@iterator property.")
    if isinstance(value, pyv8.JSObject):
        method = value._iterator_method() if iterator_method is MISSING else iterator_method
        if not callable(method):
            raise IdlArgumentError("The object must have a callable @@iterator property.")
        iterator = method._invoke(value)
        if not isinstance(iterator, pyv8.JSObject):
            raise IdlArgumentError("Iterator result is not an object.")
        next_value = iterator._get("next")
        if not callable(next_value):
            raise IdlArgumentError("Iterator next is not callable.")
        result: list[Any] = []
        while True:
            step = next_value._invoke(iterator)
            if not isinstance(step, pyv8.JSObject):
                raise IdlArgumentError("Iterator result is not an object.")
            if boolean(step._get("done")):
                return result
            item = step._get("value")
            result.append(convert(item) if convert else item)
    try:
        result = list(value)
    except TypeError as exc:
        raise IdlArgumentError("The provided value cannot be converted to a sequence.") from exc
    return [convert(item) for item in result] if convert else result


#: ``__idl_spec__`` is fixed when a class is defined, so the answer cannot change for a
#: type; the walk this replaces ran on every DOM call, and was the parser's hottest.
_INTERFACE_CHECKS: dict[tuple[type, str], bool] = {}


def interface_value(value, expected, interface, method, position, *, nullable=False):
    if nullable and is_nullish(value):
        return None
    if _matches_interface(value, expected):
        return value
    raise IdlArgumentError(
        f"Failed to execute '{method}' on '{interface}': parameter {position} is not of type '{expected}'."
    )


def enum_value(value, allowed):
    result = dom_string(value)
    if result not in allowed:
        raise IdlArgumentError(f"The provided value '{result}' is not a valid enum value.")
    return result


def callback_value(value):
    if not callable(value):
        raise IdlArgumentError("The provided callback is not a function.")
    return value


def event_listener(value):
    if is_nullish(value):
        return None
    if callable(value) or isinstance(value, (dict, pyv8.JSObject)):
        return value
    raise IdlArgumentError("The provided value is not of type 'Object'.")


def event_handler(value):
    return value if callable(value) else pyv8.Null


def header_name(value):
    value = byte_string(value)
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value):
        raise IdlArgumentError("Invalid header name")
    return value.lower()


def header_value(value):
    value = byte_string(value).strip(" \t\r\n")
    if any(character in value for character in "\r\n\0"):
        raise IdlArgumentError("Invalid header value")
    return value
