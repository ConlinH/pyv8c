"""Explicit conversion primitives and direct binding contracts."""

import ast
import inspect
import math
from pathlib import Path

import pytest

pyv8 = pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c.foundation.arguments import (  # noqa: E402
    IdlArgumentError,
    boolean,
    byte_string,
    dom_string,
    integer,
    missing_error,
    usv_string,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (pyv8.Undefined, "undefined"),
        (None, "null"),
        (pyv8.Null, "null"),
        (True, "true"),
        (False, "false"),
        ([1, None, False], "1,,false"),
        (math.nan, "NaN"),
        (-0.0, "0"),
    ],
)
async def test_string_conversion(value, expected):
    assert dom_string(value) == expected


async def test_integer_conversion_uses_wrapping_or_explicit_range():
    assert integer(-1) == 2**32 - 1
    assert integer(2**32 - 1, signed=True) == -1
    assert integer("0x10") == 16
    assert integer(math.inf) == 0
    with pytest.raises(IdlArgumentError):
        integer(-1, enforce_range=True)


async def test_boolean_uses_javascript_truthiness():
    assert boolean({}) and boolean([])
    assert not boolean(math.nan)
    assert not boolean(pyv8.Undefined)


async def test_string_subtypes():
    with pytest.raises(IdlArgumentError):
        byte_string("中")
    assert usv_string(chr(0xD800)) == chr(0xFFFD)


async def test_missing_error_carries_the_browser_wording():
    """The class is this layer's own; a realm sees it as the TypeError it inherits."""
    error = missing_error("Storage", "setItem", 2, 1)
    assert isinstance(error, IdlArgumentError)
    assert str(error) == "Failed to execute 'setItem' on 'Storage': 2 arguments required, but only 1 present."


async def test_ordinary_callbacks_are_bound_without_validation_wrapper():
    from pyv8c.async_api import Browser
    from pyv8c.web.dom.element import Element

    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        bindings = browser.bindings
        config = bindings.class_config_for("Element")
        callback = next(method.callback for method in config.methods if method.name == "setAttribute")
        assert callback is Element.fn_setAttribute
        setter = next(attribute.setter for attribute in config.attributes if attribute.name == "id")
        assert setter is Element.set_id
        # Exposure is one plan per context type, not a per-reader derivation.
        assert bindings.plan("Window") is bindings.plan("Window")
    finally:
        (await browser.close())


async def test_setters_take_one_required_value_without_missing_checks():
    import pyv8c

    checked = 0
    for path in (Path(pyv8c.__file__).parent / "web").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("set_"):
                continue
            assert not node.args.defaults, (path, node.name)
            assert node.args.vararg is None, (path, node.name)
            assert not any(isinstance(child, ast.Name) and child.id == "MISSING" for child in ast.walk(node)), (
                path,
                node.name,
            )
            checked += 1
    # Keep the structural floor below optional, separately maintained modules.
    assert checked >= 62


async def test_direct_python_setter_missing_value_is_a_python_call_error():
    from pyv8c.web.bom.window import Window

    assert tuple(inspect.signature(Window.set_onmessage).parameters) == ("self", "value")
    with pytest.raises(TypeError, match="missing.*value"):
        Window.set_onmessage(object())


async def test_javascript_setters_still_receive_and_convert_undefined():
    from pyv8c.async_api import Browser

    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        assert page.evaluate("""(() => {
            const node = document.createElement('div');
            node.id = undefined;
            node.hidden = undefined;
            onmessage = () => {};
            const assigned = typeof onmessage;
            onmessage = undefined;
            return [node.id, node.hidden, assigned, onmessage === null];
        })()""") == ["undefined", False, "function", True]
    finally:
        (await browser.close())
