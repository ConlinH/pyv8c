"""The host members the Kasada parity work repaired must have real bodies.

The IDL baseline already declared every one of them: the defect was that the
binder found no ``fn_`` / ``get_`` / ``set_`` callback and silently installed an
``Undefined`` stub.  These assertions freeze the compile-layer half of the fix;
``tests/browser/test_host_parity_fixes.py`` freezes the Chrome-shaped values.
"""
import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c.engine.bindings import Idl, Bindings  # noqa: E402
from pyv8c.platform.bootstrap import bootstrap_runtime  # noqa: E402


@pytest.fixture
def bindings():
    return Bindings(Idl.load(), runtime=bootstrap_runtime(), stub_logging="off")


OPERATIONS = (
    ("Element", "animate", "fn_animate"),
    ("Element", "checkVisibility", "fn_checkVisibility"),
    ("Window", "requestIdleCallback", "fn_requestIdleCallback"),
    ("Window", "cancelIdleCallback", "fn_cancelIdleCallback"),
    ("IdleDeadline", "timeRemaining", "fn_timeRemaining"),
    ("Animation", "play", "fn_play"),
    ("KeyframeEffect", "getKeyframes", "fn_getKeyframes"),
    ("CanvasRenderingContext2D", "getLineDash", "fn_getLineDash"),
    ("CanvasRenderingContext2D", "setLineDash", "fn_setLineDash"),
)

ATTRIBUTES = (
    ("CanvasRenderingContext2D", "lineCap", "get_lineCap"),
    ("CanvasRenderingContext2D", "lineJoin", "get_lineJoin"),
    ("AudioParam", "maxValue", "get_maxValue"),
    ("Animation", "playState", "get_playState"),
    ("Animation", "pending", "get_pending"),
    ("KeyframeEffect", "target", "get_target"),
    ("IdleDeadline", "didTimeout", "get_didTimeout"),
    ("HTMLVideoElement", "videoWidth", "get_videoWidth"),
    ("HTMLVideoElement", "videoHeight", "get_videoHeight"),
    ("HTMLVideoElement", "width", "get_width"),
    ("HTMLVideoElement", "height", "get_height"),
    ("HTMLVideoElement", "poster", "get_poster"),
    ("HTMLVideoElement", "playsInline", "get_playsInline"),
    ("HTMLVideoElement", "disablePictureInPicture", "get_disablePictureInPicture"),
    ("HTMLImageElement", "currentSrc", "get_currentSrc"),
    ("HTMLImageElement", "decoding", "get_decoding"),
    ("HTMLImageElement", "isMap", "get_isMap"),
    ("HTMLImageElement", "loading", "get_loading"),
    ("HTMLImageElement", "referrerPolicy", "get_referrerPolicy"),
    ("HTMLImageElement", "sizes", "get_sizes"),
    ("HTMLImageElement", "srcset", "get_srcset"),
    ("HTMLImageElement", "useMap", "get_useMap"),
    ("HTMLImageElement", "x", "get_x"),
    ("HTMLImageElement", "y", "get_y"),
    ("CanvasRenderingContext2D", "direction", "get_direction"),
    ("CanvasRenderingContext2D", "imageSmoothingQuality", "get_imageSmoothingQuality"),
)


@pytest.mark.parametrize("interface,member,callback_name", OPERATIONS)
def test_operation_has_a_body_not_a_stub(bindings, interface, member, callback_name):
    compiled = bindings._binder.compile(interface)
    callbacks = {m.name: m.callback for m in compiled.spec.methods}
    callback = callbacks[member]
    assert callback.__name__ == callback_name
    assert not getattr(callback, "__v8_idl_stub__", False)


@pytest.mark.parametrize("interface,member,getter_name", ATTRIBUTES)
def test_attribute_getter_has_a_body_not_a_stub(bindings, interface, member, getter_name):
    compiled = bindings._binder.compile(interface)
    attributes = {a.name: a for a in compiled.spec.attributes}
    getter = attributes[member].getter
    assert getter.__name__ == getter_name
    assert not getattr(getter, "__v8_idl_stub__", False)


def test_canvas_line_attributes_are_writable(bindings):
    compiled = bindings._binder.compile("CanvasRenderingContext2D")
    attributes = {a.name: a for a in compiled.spec.attributes}
    for name, setter_name in (("lineCap", "set_lineCap"), ("lineJoin", "set_lineJoin")):
        setter = attributes[name].setter
        assert setter is not None
        assert setter.__name__ == setter_name
        assert not getattr(setter, "__v8_idl_stub__", False)


def test_core_feature_exports_the_new_interfaces(bindings):
    """Registering the classes is half the fix; catalog and core must agree."""
    runtime = bindings.runtime
    for name in ("Animation", "AnimationEffect", "KeyframeEffect", "IdleDeadline"):
        assert runtime.interface(name) is not None
    assert runtime.catalog.for_interface("PublicKeyCredential").name == "credentials"
