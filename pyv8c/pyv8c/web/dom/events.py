"""DOM event model: events, event-handler slots and the target dispatch pipeline."""
import time

from pyv8c import pyv8
from ...idl import interface
from ...foundation.arguments import (
    MISSING,
    abort_signal,
    boolean,
    callback_value,
    dictionary,
    dom_string,
    event_constructor_arguments,
    event_handler,
    event_init,
    event_listener,
    integer,
    interface_value,
    is_nullish,
    missing_error,
    number,
    sequence,
)
from ...foundation.clock import clock_of
from ...foundation.arguments import IdlArgumentError
from ..page_errors import PageTypeError, dom_exception
from ...foundation.logging import logger
from .collections import ReadOnlyIndexedPropertyMixin

_DOCUMENT_NODE = 9  # DOM standard nodeType of Document; hardcoded to avoid a dom.nodes import cycle.


def _event_tree_root(node):
    """The root of *node*'s tree: a shadow root for shadow content, the document otherwise."""
    root = getattr(node, 'root_node', None)
    return root() if root is not None else node


def _inside_event_tree(node, root):
    """Whether *node* sits in *root*'s tree, following the host chain outwards."""
    while node is not None:
        current = _event_tree_root(node)
        if current is root:
            return True
        node = getattr(current, '_host', None)
    return False


def _retarget_event(node, current):
    """The target *current* may see for an event dispatched at *node*.

    An event crossing a shadow boundary is retargeted to the host, which is what lets a
    listener outside the tree learn that something in it was hit without learning what.
    """
    root = _event_tree_root(node)
    while getattr(root, '_host', None) is not None:
        if _inside_event_tree(current, root):
            break
        node = root._host
        root = _event_tree_root(node)
    return node


#: What the binding answers when the realm a handler was built in is gone -- the
#: document that owned it was replaced while its task was still queued.  Nobody is left
#: to hear the call, which is the same silence a promise settling into a gone realm gets.
_GONE_REALM = ("no active V8 isolate", "belongs to is closed")


def _js_call(fn, this, *args):
    if isinstance(fn, pyv8.JSObject):
        try:
            if isinstance(this, EventTarget):
                this = this._event_target_view()
            return fn.call(this, *args)
        except RuntimeError as error:
            if not any(phrase in str(error) for phrase in _GONE_REALM):
                raise
            logger.debug("an event handler of a replaced document was not called: {}", error)
            return None
    return fn(*args)


#: Exceptions that must interrupt the process rather than become a page's error.
from ...foundation.error_policy import control_exceptions as _control_exceptions


def _same_listener(left, right):
    # A JavaScript function can arrive through different Python handle wrappers.
    # Python callbacks still use identity; page callbacks use V8's identity.
    return left is right or (isinstance(left, pyv8.JSObject)
                             and isinstance(right, pyv8.JSObject) and left == right)


def _report_listener_exception(target, exc):
    """Report an exception a listener raised, as "report the exception" says.

    The DOM invoked the listener, so nothing can catch it: it goes to the realm's
    error reporting and dispatching continues; a gone realm drops it.
    """
    realm = getattr(target._services, "realm_services", None)
    reporter = getattr(realm, "report_exception", None) if realm is not None else None
    if reporter is not None:
        reporter(exc)


def _cleanup_listener(target):
    import sys
    if isinstance(sys.exception(), _control_exceptions()):
        return
    realm = getattr(target._services, 'realm_services', None)
    cleanup = getattr(realm, 'cleanup_after_callback', None)
    if cleanup is not None:
        cleanup()


def _modifier_state(event, key):
    """Shared ``getModifierState`` table used by mouse and keyboard events."""
    return {
        "Control": event.ctrlKey,
        "Shift": event.shiftKey,
        "Alt": event.altKey,
        "Meta": event.metaKey,
    }.get(dom_string(key), False)


def _nullable_string(value):
    """Web IDL ``DOMString?``: ``null``/``undefined`` are preserved as null."""
    return pyv8.Null if is_nullish(value) else dom_string(value)


def _nullable_event_init_interface(value, expected, constructor, dictionary_name, member):
    if is_nullish(value):
        return pyv8.Null
    try:
        return interface_value(value, expected, constructor, 'constructor', 2)
    except (IdlArgumentError, PageTypeError) as error:
        raise PageTypeError(f"Failed to construct '{constructor}': Failed to read the "
                            f"'{member}' property from '{dictionary_name}': "
                            f"Failed to convert value to '{expected}'.") from error


def _event_clock(services):
    """The realm's clock: the event's own, the running one's, or the shared fallback."""
    if services is None:
        # A page-made event has no realm at construction; the read happens in one.
        from ..bom.window import Window
        window = Window._accessor_window()
        services = getattr(window, "_services", None)
    return clock_of(services)


def _stamped(services, created):
    """*created* (a ``perf_counter`` reading) as a reading of the realm's clock."""
    clock = _event_clock(services)
    elapsed = (time.perf_counter() - created) * 1000.0
    return round((clock.now_ms() - elapsed) / clock.RESOLUTION_MS) * clock.RESOLUTION_MS


@interface('Event', None)
class Event:

    def __init__(self, event_type=MISSING, options=None, *_extra,
                 services=pyv8.Undefined, _realm=None, **_):
        event_type, options = event_constructor_arguments('Event', event_type, options)
        init = event_init(options)
        self.type = event_type
        self.bubbles = boolean(init.get("bubbles", False))
        self.cancelable = boolean(init.get("cancelable", False))
        self.target = self.currentTarget = None; self.eventPhase = 0; self.defaultPrevented = False
        self._stop = self._immediate = False; self._path = []; self._passive = False
        self._dispatching = False
        self._legacy_target_override = None
        self._composed = boolean(init.get("composed", False))
        self._services = None if services is pyv8.Undefined else (services or _realm)
        #: When the event was made, and its stamp once a realm's clock has answered.
        self._created = time.perf_counter()
        self._time_stamp = None if self._services is None else _stamped(self._services, self._created)
        # Trust is host state, never an EventInit member.
        self._trusted = bool(init.get("isTrusted", False)) if type(options) is dict else False
        self._persisted = bool(init.get("persisted", False)) if type(options) is dict else False
        # Enter and leave events reach their target alone (see _dispatch_event).
        self._non_propagating = False
    def get_type(self): return self.type
    def get_target(self):
        return self.target._event_target_view() if self.target is not None else pyv8.Null
    def get_currentTarget(self):
        return self.currentTarget._event_target_view() if self.currentTarget is not None else pyv8.Null
    def get_bubbles(self): return self.bubbles
    def get_cancelable(self): return self.cancelable
    def get_defaultPrevented(self): return self.defaultPrevented
    def get_eventPhase(self): return self.eventPhase
    def get_cancelBubble(self): return self._stop
    def set_cancelBubble(self, value):
        value = boolean(value)
        if value:
            self._stop = True
    def get_timeStamp(self):
        if self._time_stamp is None:
            self._time_stamp = _stamped(self._services, self._created)
        return self._time_stamp
    def get_isTrusted(self): return self._trusted
    def get_composed(self): return self._composed
    def get_persisted(self): return self._persisted
    def get_returnValue(self): return not self.defaultPrevented
    def set_returnValue(self, value):
        value = boolean(value)
        if not value and self.cancelable: self.defaultPrevented = True
    def fn_preventDefault(self, *_extra):
        if self.cancelable and not self._passive: self.defaultPrevented = True
    def fn_stopPropagation(self, *_extra):
        self._stop = True
    def fn_stopImmediatePropagation(self, *_extra):
        self._stop = self._immediate = True
    def fn_composedPath(self, *_extra):
        visible = []
        for target in self._path:
            root = _event_tree_root(target)
            hidden = False
            # A closed tree's internals stay hidden even from the path: a listener on the
            # host reads the host, not the node inside it.
            while getattr(root, '_host', None) is not None:
                if (getattr(root, '_mode', None) == 'closed'
                        and not _inside_event_tree(self.currentTarget, root)):
                    hidden = True
                    break
                root = _event_tree_root(root._host)
            if not hidden:
                visible.append(target._event_target_view())
        return visible
    def fn_initEvent(self, event_type=MISSING, bubbles=False, cancelable=False, *_extra):
        if self._dispatching:
            return
        if event_type is MISSING:
            raise missing_error('Event', 'initEvent', 3, 0, constructor=False)
        if bubbles is pyv8.Undefined:
            bubbles = False
        if cancelable is pyv8.Undefined:
            cancelable = False
        self.type = dom_string(event_type)
        self.bubbles = boolean(bubbles)
        self.cancelable = boolean(cancelable)

    def get_srcElement(self): return self.get_target()



@interface('UIEvent', 'Event')
class UIEvent(Event):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('UIEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.detail = integer(init.get("detail", 0), signed=True)
        self.sourceCapabilities = _nullable_event_init_interface(
            init.get("sourceCapabilities", pyv8.Null), 'InputDeviceCapabilities',
            type(self).__name__, 'UIEventInit', 'sourceCapabilities')
        self.view = init.get("view", pyv8.Null)
    def get_view(self): return self.view
    def get_detail(self): return self.detail
    def get_sourceCapabilities(self): return self.sourceCapabilities
    def get_which(self): return self.detail


@interface('MouseEvent', 'UIEvent')
class MouseEvent(UIEvent):
    __idl_overrides__ = {
        "which": {
            "side_effect": 1
        }
    }

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('MouseEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.screenX = number(init.get("screenX", 0))
        self.screenY = number(init.get("screenY", 0))
        self.clientX = number(init.get("clientX", 0))
        self.clientY = number(init.get("clientY", 0))
        # No scrolling, so page and viewport coordinates coincide unless given.
        # Owner-free name: ``web`` must not name a host page object.
        self._document_x = number(init.get("pageX", self.clientX))
        self._document_y = number(init.get("pageY", self.clientY))
        self.x = number(init.get("x", self.clientX))
        self.y = number(init.get("y", self.clientY))
        self.offsetX = number(init.get("offsetX", self.clientX))
        self.offsetY = number(init.get("offsetY", self.clientY))
        self.movementX = number(init.get("movementX", 0))
        self.movementY = number(init.get("movementY", 0))
        self.ctrlKey = boolean(init.get("ctrlKey", False))
        self.shiftKey = boolean(init.get("shiftKey", False))
        self.altKey = boolean(init.get("altKey", False))
        self.metaKey = boolean(init.get("metaKey", False))
        self.button = integer(init.get("button", 0), bits=16, signed=True)
        self.buttons = integer(init.get("buttons", 0), bits=16)
        self.relatedTarget = init.get("relatedTarget", pyv8.Null)

    def get_screenX(self): return self.screenX
    def get_screenY(self): return self.screenY
    def get_clientX(self): return self.clientX
    def get_clientY(self): return self.clientY
    def get_pageX(self): return self._document_x
    def get_pageY(self): return self._document_y
    def get_x(self): return self.x
    def get_y(self): return self.y
    def get_offsetX(self): return self.offsetX
    def get_offsetY(self): return self.offsetY
    def get_movementX(self): return self.movementX
    def get_movementY(self): return self.movementY
    def get_ctrlKey(self): return self.ctrlKey
    def get_shiftKey(self): return self.shiftKey
    def get_altKey(self): return self.altKey
    def get_metaKey(self): return self.metaKey
    def get_button(self): return self.button
    def get_buttons(self): return self.buttons
    def get_relatedTarget(self): return self.relatedTarget
    # Legacy aliases: Blink exposes relatedTarget only for the matching type.
    def get_fromElement(self): return self.relatedTarget if self.type == "mouseover" else pyv8.Null
    def get_toElement(self): return self.relatedTarget if self.type == "mouseout" else pyv8.Null
    def get_which(self): return self.button + 1
    def fn_getModifierState(self, key=MISSING, *_extra):
        if key is MISSING:
            raise missing_error('MouseEvent', 'getModifierState', 1, 0, constructor=False)
        return _modifier_state(self, key)

    def _layer_coordinates(self):
        """Layer-relative coordinates use the nearest positioned ancestor's origin."""
        from ..css.cascade import computed_style
        from .layout import positioned_box
        node = self.target
        while node is not None:
            if getattr(node, 'nodeType', None) == 1:
                style = computed_style(node)
                if style.get('position', 'static') != 'static':
                    box = positioned_box(node)
                    return int(self.clientX-box.left), int(self.clientY-box.top)
            parent = getattr(node, 'parentNode', None)
            node = parent if parent is not None else getattr(node, '_host', None)
        return int(self._document_x), int(self._document_y)


    def get_layerX(self): return self._layer_coordinates()[0]


    def get_layerY(self): return self._layer_coordinates()[1]



@interface('KeyboardEvent', 'UIEvent')
class KeyboardEvent(UIEvent):
    __idl_overrides__ = {
        "which": {
            "side_effect": 1
        }
    }

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('KeyboardEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.key = dom_string(init.get("key", ""))
        self.code = dom_string(init.get("code", ""))
        self.location = integer(init.get("location", 0), bits=32)
        self.ctrlKey = boolean(init.get("ctrlKey", False))
        self.shiftKey = boolean(init.get("shiftKey", False))
        self.altKey = boolean(init.get("altKey", False))
        self.metaKey = boolean(init.get("metaKey", False))
        self.repeat = boolean(init.get("repeat", False))
        self.isComposing = boolean(init.get("isComposing", False))
        # Legacy numeric aliases stay init-driven; no key-code table is derived.
        self.charCode = integer(init.get("charCode", 0), bits=32)
        self.keyCode = integer(init.get("keyCode", 0), bits=32)

    def get_key(self): return self.key
    def get_code(self): return self.code
    def get_location(self): return self.location
    def get_ctrlKey(self): return self.ctrlKey
    def get_shiftKey(self): return self.shiftKey
    def get_altKey(self): return self.altKey
    def get_metaKey(self): return self.metaKey
    def get_repeat(self): return self.repeat
    def get_isComposing(self): return self.isComposing
    def get_charCode(self): return self.charCode
    def get_keyCode(self): return self.keyCode
    def get_which(self): return self.keyCode
    def fn_getModifierState(self, key=MISSING, *_extra):
        if key is MISSING:
            raise missing_error('KeyboardEvent', 'getModifierState', 1, 0, constructor=False)
        return _modifier_state(self, key)


@interface('CustomEvent', 'Event')
class CustomEvent(Event):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('CustomEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.detail = init.get("detail", pyv8.Null)

    def get_detail(self): return self.detail
    def fn_initCustomEvent(self, event_type=MISSING, bubbles=False, cancelable=False, detail=pyv8.Null, *_extra):
        if event_type is MISSING:
            raise missing_error('CustomEvent', 'initCustomEvent', 4, 0, constructor=False)
        if bubbles is pyv8.Undefined:
            bubbles = False
        if cancelable is pyv8.Undefined:
            cancelable = False
        self.fn_initEvent(event_type, bubbles, cancelable)
        self.detail = detail


@interface('FocusEvent', 'UIEvent')
class FocusEvent(UIEvent):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('FocusEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        self.relatedTarget = event_init(options).get("relatedTarget", pyv8.Null)

    def get_relatedTarget(self): return self.relatedTarget


@interface('InputEvent', 'UIEvent')
class InputEvent(UIEvent):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('InputEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.data = _nullable_string(init.get("data", pyv8.Null))
        self.dataTransfer = _nullable_event_init_interface(
            init.get("dataTransfer", pyv8.Null), 'DataTransfer',
            'InputEvent', 'InputEventInit', 'dataTransfer')
        self.inputType = dom_string(init.get("inputType", ""))
        self.isComposing = boolean(init.get("isComposing", False))
        ranges = init.get('targetRanges', pyv8.Undefined)
        if ranges is pyv8.Undefined:
            self._target_ranges = ()
        else:
            try:
                self._target_ranges = tuple(sequence(ranges, lambda value: interface_value(
                    value, 'StaticRange', 'InputEvent', 'constructor', 2)))
            except (IdlArgumentError, PageTypeError) as error:
                reason = ("Failed to convert value to 'StaticRange'."
                          if 'StaticRange' in str(error) else
                          'The provided value cannot be converted to a sequence.')
                raise PageTypeError("Failed to construct 'InputEvent': Failed to read the "
                                    "'targetRanges' property from 'InputEventInit': " + reason) from error

    def get_data(self): return self.data
    def get_isComposing(self): return self.isComposing
    def get_inputType(self): return self.inputType
    def get_dataTransfer(self): return self.dataTransfer
    def fn_getTargetRanges(self, *_extra): return list(self._target_ranges)


@interface('CompositionEvent', 'UIEvent')
class CompositionEvent(UIEvent):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('CompositionEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        self.data = dom_string(event_init(options).get("data", ""))

    def get_data(self): return self.data


@interface('PointerEvent', 'MouseEvent')
class PointerEvent(MouseEvent):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('PointerEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.pointerId = integer(init.get("pointerId", 0), signed=True)
        self.width = number(init.get("width", 1))
        self.height = number(init.get("height", 1))
        self.pressure = number(init.get("pressure", 0.5))
        self.tiltX = integer(init.get("tiltX", 0), signed=True)
        self.tiltY = integer(init.get("tiltY", 0), signed=True)
        self.azimuthAngle = number(init.get("azimuthAngle", 0))
        self.altitudeAngle = number(init.get("altitudeAngle", 0))
        self.tangentialPressure = number(init.get("tangentialPressure", 0))
        self.twist = integer(init.get("twist", 0), signed=True)
        self.pointerType = dom_string(init.get("pointerType", ""))
        self.isPrimary = boolean(init.get("isPrimary", False))
        self.persistentDeviceId = integer(init.get("persistentDeviceId", 0), signed=True)
        #: The samples this event stands for, its own reading last.  Only a move carries
        #: them, which is the shape Chrome reports.
        self._coalesced = (tuple(init.get("coalesced") or ())
                           + ((self,) if str(event_type).endswith("move") else ()))

    def get_pointerId(self): return self.pointerId
    def get_width(self): return self.width
    def get_height(self): return self.height
    def get_pressure(self): return self.pressure
    def get_tiltX(self): return self.tiltX
    def get_tiltY(self): return self.tiltY
    def get_azimuthAngle(self): return self.azimuthAngle
    def get_altitudeAngle(self): return self.altitudeAngle
    def get_tangentialPressure(self): return self.tangentialPressure
    def get_twist(self): return self.twist
    def get_pointerType(self): return self.pointerType
    def get_isPrimary(self): return self.isPrimary
    def get_persistentDeviceId(self): return self.persistentDeviceId
    def fn_getCoalescedEvents(self, *_extra):
        """The samples this event stands for, its own reading last.

        Chrome reports a list of one even when nothing was merged; the host's input
        path fills it with the samples a burst of positions arrived as.
        """
        return list(self._coalesced)

    # No prediction pipeline: Chrome reports an empty list here too.
    def fn_getPredictedEvents(self, *_extra): return []


@interface('WheelEvent', 'MouseEvent')
class WheelEvent(MouseEvent):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('WheelEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.deltaX = number(init.get("deltaX", 0))
        self.deltaY = number(init.get("deltaY", 0))
        self.deltaZ = number(init.get("deltaZ", 0))
        self.deltaMode = integer(init.get("deltaMode", 0), bits=32)
        # Legacy Blink aliases: positive when the content scrolls up/left.
        self.wheelDeltaX = number(init.get("wheelDeltaX", -self.deltaX))
        self.wheelDeltaY = number(init.get("wheelDeltaY", -self.deltaY))
        self.wheelDelta = number(init.get("wheelDelta", -self.deltaY))
        self.momentum = boolean(init.get("momentum", False))

    def get_deltaX(self): return self.deltaX
    def get_deltaY(self): return self.deltaY
    def get_deltaZ(self): return self.deltaZ
    def get_deltaMode(self): return self.deltaMode
    def get_wheelDeltaX(self): return self.wheelDeltaX
    def get_wheelDeltaY(self): return self.wheelDeltaY
    def get_wheelDelta(self): return self.wheelDelta
    def get_momentum(self): return self.momentum


@interface('PopStateEvent', 'Event')
class PopStateEvent(Event):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('PopStateEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        state = init.get("state", pyv8.Null)
        # ``None`` is the host's "no state"; a JS ``undefined`` stays undefined.
        self.state = pyv8.Null if state is None else state
        self.has_ua_visual_transition = boolean(init.get("hasUAVisualTransition", False))

    def get_state(self): return self.state
    def get_hasUAVisualTransition(self): return self.has_ua_visual_transition


@interface('HashChangeEvent', 'Event')
class HashChangeEvent(Event):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('HashChangeEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.oldURL = dom_string(init.get("oldURL", ""))
        self.newURL = dom_string(init.get("newURL", ""))

    def get_oldURL(self): return self.oldURL
    def get_newURL(self): return self.newURL


@interface('StorageEvent', 'Event')
class StorageEvent(Event):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('StorageEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.key = _nullable_string(init.get("key", pyv8.Null))
        self.oldValue = _nullable_string(init.get("oldValue", pyv8.Null))
        self.newValue = _nullable_string(init.get("newValue", pyv8.Null))
        self.url = dom_string(init.get("url", ""))
        self.storageArea = init.get("storageArea", pyv8.Null)

    def get_key(self): return self.key
    def get_oldValue(self): return self.oldValue
    def get_newValue(self): return self.newValue
    def get_url(self): return self.url
    def get_storageArea(self): return self.storageArea


@interface('WebGLContextEvent', 'Event')
class WebGLContextEvent(Event):
    """``webglcontextlost`` and its siblings: what a lost context reports."""

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('WebGLContextEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        self.statusMessage = dom_string(event_init(options).get("statusMessage", ""))

    def get_statusMessage(self): return self.statusMessage


@interface('AnimationEvent', 'Event')
class AnimationEvent(Event):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('AnimationEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.animationName = dom_string(init.get("animationName", ""))
        self.elapsedTime = number(init.get("elapsedTime", 0))
        self.pseudoElement = dom_string(init.get("pseudoElement", ""))
        self.animation = init.get("animation", pyv8.Null)
        self.pseudoTarget = init.get("pseudoTarget", pyv8.Null)

    def get_animationName(self): return self.animationName
    def get_elapsedTime(self): return self.elapsedTime
    def get_pseudoElement(self): return self.pseudoElement
    def get_animation(self): return self.animation
    def get_pseudoTarget(self): return self.pseudoTarget


@interface('TransitionEvent', 'Event')
class TransitionEvent(Event):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('TransitionEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.propertyName = dom_string(init.get("propertyName", ""))
        self.elapsedTime = number(init.get("elapsedTime", 0))
        self.pseudoElement = dom_string(init.get("pseudoElement", ""))
        self.animation = init.get("animation", pyv8.Null)
        self.pseudoTarget = init.get("pseudoTarget", pyv8.Null)

    def get_propertyName(self): return self.propertyName
    def get_elapsedTime(self): return self.elapsedTime
    def get_pseudoElement(self): return self.pseudoElement
    def get_animation(self): return self.animation
    def get_pseudoTarget(self): return self.pseudoTarget


@interface('PromiseRejectionEvent', 'Event')
class PromiseRejectionEvent(Event):

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('PromiseRejectionEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self.promise = init.get("promise", pyv8.Null)
        self.reason = init.get("reason", pyv8.Null)

    def get_promise(self): return self.promise
    def get_reason(self): return self.reason


@interface('PageTransitionEvent', 'Event')
class PageTransitionEvent(Event):
    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('PageTransitionEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        self._persisted = boolean(event_init(options).get("persisted", False))

    def get_persisted(self): return self._persisted


@interface('PageRevealEvent', 'Event')
class PageRevealEvent(Event):
    """``pagereveal``: fired once when the page is first presented."""

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('PageRevealEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        transition = event_init(options).get("viewTransition")
        # A page with no view transition running has none, which Chrome reports as null.
        self._view_transition = pyv8.Null if is_nullish(transition) else transition

    def get_viewTransition(self): return self._view_transition


@interface('PageSwapEvent', 'Event')
class PageSwapEvent(Event):
    """``pageswap``: the last event before a document is replaced."""

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('PageSwapEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        transition = init.get("viewTransition")
        activation = init.get("activation")
        self._view_transition = pyv8.Null if is_nullish(transition) else transition
        self._activation = pyv8.Null if is_nullish(activation) else activation

    def get_viewTransition(self): return self._view_transition
    def get_activation(self): return self._activation


@interface('BeforeUnloadEvent', 'Event')
class BeforeUnloadEvent(Event):
        # ``allow_new=false`` in the baseline: non-constructible, but the host
        # still builds one through ``__init__``.

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('BeforeUnloadEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        self._return_value = ""

    def get_returnValue(self): return self._return_value
    def set_returnValue(self, value):
        value = dom_string(value)
        self._return_value = value
        if value:
            self.defaultPrevented = True


@interface('Touch', None)
class Touch:
    """One contact point, as a ``TouchEvent`` carries it."""

    def __init__(self, init=MISSING, *_extra, **_):
        if init is MISSING:
            raise missing_error('Touch', 'Touch', 1, 0, constructor=True)
        options = dictionary(init, name="TouchInit")
        self._identifier = integer(options.get("identifier", 0), signed=True)
        self._target = options.get("target", pyv8.Null)
        self._screen_x = number(options.get("screenX", 0))
        self._screen_y = number(options.get("screenY", 0))
        self._client_x = number(options.get("clientX", 0))
        self._client_y = number(options.get("clientY", 0))
        #: No scrolling, so page and client coordinates coincide; ``web`` stays out of the host page.
        self._document_x = number(options.get("pageX", self._client_x))
        self._document_y = number(options.get("pageY", self._client_y))
        self._radius_x = number(options.get("radiusX", 0))
        self._radius_y = number(options.get("radiusY", 0))
        self._rotation = number(options.get("rotationAngle", 0))
        self._force = number(options.get("force", 0))

    def get_identifier(self): return self._identifier
    def get_target(self): return self._target
    def get_screenX(self): return self._screen_x
    def get_screenY(self): return self._screen_y
    def get_clientX(self): return self._client_x
    def get_clientY(self): return self._client_y
    def get_pageX(self): return self._document_x
    def get_pageY(self): return self._document_y
    def get_radiusX(self): return self._radius_x
    def get_radiusY(self): return self._radius_y
    def get_rotationAngle(self): return self._rotation
    def get_force(self): return self._force


@interface('TouchList', None)
class TouchList(ReadOnlyIndexedPropertyMixin):
    """The contacts one ``touches``/``targetTouches``/``changedTouches`` reports."""

    def __init__(self, touches=(), **_):
        self._touches = tuple(touches)

    def _indexed_items(self): return self._touches
    def get_length(self): return len(self._touches)
    def fn_item(self, *args):
        index = int(args[0]) if args and not is_nullish(args[0]) else 0
        return self._touches[index] if 0 <= index < len(self._touches) else pyv8.Null


def _touch_list(value):
    """A ``TouchList`` from whatever a caller passed: a list, or one already built."""
    if isinstance(value, TouchList):
        return value
    if value is None or value is pyv8.Undefined or value is pyv8.Null:
        return TouchList()
    try:
        items = [item for item in value]
    except TypeError:
        return TouchList()
    return TouchList(items)


@interface('TouchEvent', 'UIEvent')
class TouchEvent(UIEvent):
    """Touch input: the three touch lists, and the modifier keys a touch carries.

    Chrome 154's interface baseline carries no ``initTouchEvent``, so none is declared: a page
    that calls it gets the same ``TypeError`` there as here.
    """

        #: No ``initTouchEvent``: Chrome 154's baseline does not carry it, so it would be a
        #: difference rather than a repair.

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments('TouchEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        init = event_init(options)
        self._touches = _touch_list(init.get("touches"))
        self._target_touches = _touch_list(init.get("targetTouches"))
        self._changed_touches = _touch_list(init.get("changedTouches"))
        self._alt_key = boolean(init.get("altKey", False))
        self._meta_key = boolean(init.get("metaKey", False))
        self._ctrl_key = boolean(init.get("ctrlKey", False))
        self._shift_key = boolean(init.get("shiftKey", False))

    def get_touches(self): return self._touches
    def get_targetTouches(self): return self._target_touches
    def get_changedTouches(self): return self._changed_touches
    def get_altKey(self): return self._alt_key
    def get_metaKey(self): return self._meta_key
    def get_ctrlKey(self): return self._ctrl_key
    def get_shiftKey(self): return self._shift_key


@interface('EventTarget', None)
class EventTarget:
    # Every ``on*`` slot is generated from ``_HANDLER_SLOTS`` at module bottom.

    def _event_target_view(self):
        """The JavaScript identity exposed by event attributes and callbacks."""
        return self

    def __init__(self, *_extra, services=None, **_):
        if services is pyv8.Undefined:
            services = None
        self._services = services
        self._listeners = {}
        self._listener_order = 0
        self._handlers = {}

    def _handler_target(self, name):
        return self

    def _get_handler(self, name):
        target = self._handler_target(name)
        return pyv8.Null if target is None else target._handlers.get(name, pyv8.Null)

    def _set_handler(self, name, value):
        target = self._handler_target(name)
        if target is not None:
            target._handlers[name] = event_handler(value)

    def fn_addEventListener(self, event_type=MISSING, listener=MISSING, options=False, *_extra):
        if event_type is MISSING:
            raise missing_error('EventTarget', 'addEventListener', 2, 0, constructor=False)
        if listener is MISSING:
            raise missing_error('EventTarget', 'addEventListener', 2, 1, constructor=False)
        event_type = dom_string(event_type)
        if not is_nullish(listener):
            listener = event_listener(listener)
        if options is pyv8.Undefined:
            options = False
        if isinstance(options, pyv8.JSObject):
            options = dictionary(options)
        capture = boolean(options.get("capture", False)) if isinstance(options, dict) else boolean(options)
        once = bool(options.get("once", False)) if isinstance(options, dict) else False
        passive = bool(options.get("passive", False)) if isinstance(options, dict) else False
        signal = None
        if isinstance(options, dict):
            try:
                signal = abort_signal(options.get("signal"))
            except (PageTypeError, IdlArgumentError) as exc:
                raise PageTypeError(
                    "Failed to execute 'addEventListener' on 'EventTarget': Failed to read"
                    " the 'signal' property from 'AddEventListenerOptions': Failed to"
                    " convert value to 'AbortSignal'.") from exc
        if is_nullish(listener):
            return
        bucket = self._listeners.setdefault(str(event_type), [])
        if not any(_same_listener(item[0], listener) and item[1] == capture for item in bucket):
            self._listener_order += 1; bucket.append((listener, capture, once, passive, signal, self._listener_order))
            if signal is not None:
                # Chrome takes the listener back out when the signal fires; the check
                # in ``_invoke`` only covers a signal that had already aborted here.
                signal._subscribe(self._drop_listener(str(event_type), listener, capture))
            self._motion_listener_added(str(event_type))

    def _drop_listener(self, event_type, listener, capture):
        """What a signal runs to take one registration back out of its bucket."""
        def drop():
            bucket = self._listeners.get(event_type)
            if bucket:
                self._listeners[event_type] = [
                    item for item in bucket
                    if not (_same_listener(item[0], listener) and item[1] == capture)]
        return drop

    def _motion_listener_added(self, event_type):
        """Tell the realm that a motion listener appeared on the window.

        Chrome starts delivering these readings as soon as a page listens, and only
        the realm knows where the document is in its life, so the decision is its.
        """
        if event_type not in MOTION_EVENT_TYPES:
            return
        document_services = getattr(self._services, "document_services", None)
        if getattr(document_services, "window", None) is not self:
            return
        notify = getattr(document_services, "motion_event_listener", None)
        if notify is not None:
            notify(event_type)

    def fn_removeEventListener(self, event_type=MISSING, listener=MISSING, options=False, *_extra):
        if event_type is MISSING:
            raise missing_error('EventTarget', 'removeEventListener', 2, 0, constructor=False)
        if listener is MISSING:
            raise missing_error('EventTarget', 'removeEventListener', 2, 1, constructor=False)
        event_type = dom_string(event_type)
        if not is_nullish(listener):
            listener = event_listener(listener)
        if options is pyv8.Undefined:
            options = False
        if isinstance(options, pyv8.JSObject):
            options = dictionary(options)
        capture = boolean(options.get("capture", False)) if isinstance(options, dict) else boolean(options)
        bucket = self._listeners.get(str(event_type), [])
        self._listeners[str(event_type)] = [x for x in bucket if not (_same_listener(x[0], listener) and x[1] == capture)]

    def fn_dispatchEvent(self, event=MISSING, *_extra):
        if event is MISSING:
            raise missing_error('EventTarget', 'dispatchEvent', 1, 0, constructor=False)
        event = interface_value(event, 'Event', 'EventTarget', 'dispatchEvent', 1, nullable=False)
        if not isinstance(event, Event):
            raise PageTypeError("Failed to execute 'dispatchEvent' on 'EventTarget': parameter 1 is not of type 'Event'.")
        if event._dispatching:
            raise dom_exception("InvalidStateError", "The event is already being dispatched.")
        #: The public DOM method always clears trust, including when script redispatches an
        #: event that came from the input controller: ``isTrusted`` answers whether this
        #: delivery came from the browser, and this one did not.  Host code keeps its own
        #: deliveries trusted by calling ``_dispatch_event`` directly.
        event._trusted = False
        return self._dispatch_event(event)

    def _dispatch_event(self, event=MISSING, *_extra):
        """Deliver *event* as it stands, trust included; every host dispatch uses this."""
        if event is MISSING:
            raise missing_error('EventTarget', 'dispatchEvent', 1, 0, constructor=False)
        event = interface_value(event, 'Event', 'EventTarget', 'dispatchEvent', 1, nullable=False)
        if not isinstance(event, Event):
            raise PageTypeError("Failed to execute 'dispatchEvent' on 'EventTarget': parameter 1 is not of type 'Event'.")
        if event._services is None and self._services is not None:
            # A host-built event is stamped where it is delivered: its clock is the target's.
            event._services = self._services
        if event._dispatching:
            raise dom_exception("InvalidStateError", "The event is already being dispatched.")
        if event._trusted:
            from ..bom.performance import EVENT_COUNT_NAMES
            if event.type in EVENT_COUNT_NAMES:
                timeline = getattr(getattr(self._services, "realm_services", None),
                                   "performance", None)
                if timeline is not None:
                    counts = timeline.event_counts
                    counts[event.type] = counts.get(event.type, 0) + 1
        if event._trusted:
            kind = event.type
            activation_input = (
                kind in ("mousedown", "touchend")
                or (kind == "pointerdown" and getattr(event, "pointerType", "") == "mouse")
                or (kind == "pointerup" and getattr(event, "pointerType", "") != "mouse")
                or (kind == "keydown" and getattr(event, "key", "") != "Escape"
                    and not any(getattr(event, name, False)
                                for name in ("ctrlKey", "altKey", "metaKey")))
            )
            if activation_input:
                facet = getattr(self._services, "document_services", None)
                notify = getattr(facet, "notify_user_activation", None)
                if notify is not None:
                    notify()
        # An event begins a new script observation boundary. Host-backed state may
        # have changed since the preceding event without a DOM mutation; keep
        # repeated reads within this dispatch cheap, but start it with fresh boxes.
        document = getattr(getattr(self, "_services", None), "document_services", None)
        document = getattr(document, "document", None)
        if document is not None:
            document._layout_snapshot = None
        event._dispatching = True
        try:
            original_target = event._legacy_target_override or self
            event.target = original_target
            # A non-propagating event reaches its target alone: a host caller may restrict
            # an event to its target, and a boundary event never bubbles.
            path = [self]
            if not event._non_propagating:
                node = self
                while node is not None:
                    previous = node
                    node = getattr(node, "parentNode", None)
                    if node is None and event._composed:
                        #: A composed event leaves its tree through the host of the root it
                        #: was dispatched in; a non-composed one stops at that root.
                        node = getattr(previous, '_host', None)
                    if node is None and getattr(path[-1], "nodeType", None) == _DOCUMENT_NODE:
                        node = getattr(getattr(getattr(path[-1], "_services", None), "document_services", None), "window", None)
                    if node is not None:
                        path.append(node)
            event._path = list(path)
            kind = event.type
            on_kind = "on" + kind
            # Most nodes on a dispatch path listen for nothing. Check before the
            # phase writes and retargeting: those are only ever observed by a
            # listener, so an inactive node costs two dict reads and nothing else.
            saw_active = False
            for target in reversed(path[1:]):
                if not (target._listeners.get(kind) or target._handlers.get(on_kind)):
                    continue
                saw_active = True
                event.eventPhase, event.currentTarget = 1, target
                #: Each ancestor sees the target its own tree is allowed to name.
                event.target = _retarget_event(original_target, target)
                target._invoke(event, capture=True)
                if event._stop:
                    return not event.defaultPrevented
            event.target = original_target
            if self._listeners.get(kind) or self._handlers.get(on_kind):
                saw_active = True
                event.eventPhase, event.currentTarget = 2, self
                self._invoke(event, capture=True)
                if not event._immediate: self._invoke(event, capture=False)
            if not saw_active:
                # No callback ran, so nothing could have added a listener or
                # prevented the default mid-dispatch: leave the end state the
                # phase walks would have left, without the per-node writes.
                if event.bubbles and len(path) > 1:
                    event.target = _retarget_event(original_target, path[-1])
                return not event.defaultPrevented
            if event.bubbles and not event._stop:
                last_bubble = None
                for target in path[1:]:
                    last_bubble = target
                    if not (target._listeners.get(kind) or target._handlers.get(on_kind)):
                        continue
                    event.eventPhase, event.currentTarget = 3, target
                    event.target = _retarget_event(original_target, target)
                    target._invoke(event, capture=False)
                    if event._stop: break
                if last_bubble is not None:
                    event.target = _retarget_event(original_target, last_bubble)
            logger.debug("dispatch {} on {} (trusted={}, listeners={})", kind,
                         type(self).__name__, event._trusted,
                         len(self._listeners.get(kind, ())))
            event.eventPhase, event.currentTarget = 0, None
            return not event.defaultPrevented
        finally:
            event.eventPhase, event.currentTarget = 0, None
            event._dispatching = False
            event._legacy_target_override = None
            event._stop = event._immediate = event._passive = False
            event._path = []

    def _invoke(self, event, capture):
        listeners = self._listeners.get(event.type)
        handler = self._handlers.get("on" + event.type)
        # A dispatch visits every node on the path, and most of them listen for
        # nothing: this is the check that keeps those visits to two dict reads.
        if listeners is None and handler is None:
            return
        for registration in list(listeners or ()):
            listener, registered_capture, once, passive, signal, _order = registration
            # A previous callback may remove or remove-and-readd this registration.
            current = self._listeners.get(event.type, ())
            if current is not listeners and registration not in current:
                continue
            if registered_capture != capture:
                continue
            if signal is not None and getattr(signal, "aborted", False): continue
            if once:
                self._listeners[event.type] = [entry for entry in current if entry is not registration]
            event._passive = passive
            try:
                if callable(listener):
                    _js_call(listener, self, event)
                else:
                    callback = listener._get("handleEvent") if isinstance(listener, pyv8.JSObject) else listener.get("handleEvent")
                    _js_call(callback_value(callback), listener, event)
            except _control_exceptions():
                raise
            except BaseException as exc:  # reported, then dispatching continues
                # Escaping here would make dispatchEvent throw and skip later listeners.
                _report_listener_exception(self, exc)
            finally:
                event._passive = False
                _cleanup_listener(self)
            if event._immediate:
                return
        # EventHandler slots may hold non-callable objects; Blink invokes their function.
        if not capture and callable(handler):
            try:
                window_error = False
                if event.type == 'error' and self.__class__.__name__ == 'Window':
                    from ..fetch.events import ErrorEvent
                    window_error = isinstance(event, ErrorEvent)
                if window_error:
                    result = _js_call(handler, self, event.message, event.filename,
                                      event.lineno, event.colno, event.error)
                    if result is True:
                        event.fn_preventDefault()
                else:
                    result = _js_call(handler, self, event)
                    if result is False:
                        event.fn_preventDefault()
                    if event.type == "beforeunload" and result not in (None, pyv8.Null, pyv8.Undefined, ""):
                        event.defaultPrevented = True
            except _control_exceptions():
                raise
            except BaseException as exc:  # reported, not propagated
                # The handler slot is a listener with a shorter spelling: report the same way.
                _report_listener_exception(self, exc)

            finally:
                _cleanup_listener(self)

    def __getattr__(self, name):
        if name.startswith("on"):
            return self._get_handler(name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        # A plain index beats str.startswith, and almost every assignment fails it.
        if name[:2] == "on" and name != "one" and "_handlers" in self.__dict__:
            self._set_handler(name, value)
            return
        object.__setattr__(self, name, value)


#: Types Chrome delivers on its own for a listening page, from the device sensors.
MOTION_EVENT_TYPES = ("devicemotion", "deviceorientation", "deviceorientationabsolute")


# Event-handler attributes shared by every EventTarget subclass.  Only names this release's
# IDL declares are listed, so nothing here can be a declaration that never installs;
# composition slots are absent (the IDL declares those only on ``EditContext``).
_HANDLER_SLOTS = (
    "onload", "onerror", "onreadystatechange", "onpageshow", "onpagehide",
    "onunload", "onbeforeunload",
    "onclick", "ondblclick", "onauxclick", "oncontextmenu",
    "onmousedown", "onmouseup", "onmousemove", "onmouseover", "onmouseout",
    "onmouseenter", "onmouseleave",
    "onfocus", "onblur", "oninput", "onbeforeinput", "onchange",
    "onselect", "oninvalid", "onsubmit", "onreset",
    "onkeydown", "onkeyup", "onkeypress",
    "onpointerdown", "onpointerup", "onpointermove", "onpointerover", "onpointerout",
    "onpointerenter", "onpointerleave", "onpointercancel",
    "ongotpointercapture", "onlostpointercapture", "onwheel", "onscroll",
    "onhashchange", "onpopstate", "onstorage", "onmessageerror",
    "onvisibilitychange", "onfreeze", "onresume",
    "onloadstart", "onloadend", "onprogress", "onabort", "ontimeout",
    "onanimationstart", "onanimationend", "onanimationiteration", "onanimationcancel",
    "ontransitionstart", "ontransitionend", "ontransitionrun", "ontransitioncancel",
    "oncopy", "oncut", "onpaste", "onselectstart", "onselectionchange",
    "onunhandledrejection", "onrejectionhandled",
    "oncanplay", "oncanplaythrough", "ondurationchange", "onemptied", "onended",
    "onloadeddata", "onloadedmetadata", "onpause", "onplay", "onplaying",
    "onratechange", "onseeked", "onseeking", "onstalled", "onsuspend",
    "ontimeupdate", "onvolumechange", "onwaiting",
    "ondrag", "ondragend", "ondragenter", "ondragleave", "ondragover", "ondragstart",
    "ondrop",
    "ondevicemotion", "ondeviceorientation", "ondeviceorientationabsolute",
    "ononline", "onoffline", "onlanguagechange",
    "onafterprint", "onbeforeprint", "onbeforetoggle", "onbeforematch",
    "onbeforeinstallprompt", "onappinstalled", "onpageswap", "onpagereveal",
    "oncommand", "oncomplete",
    "onresize", "onscrollend", "onscrollsnapchange", "onscrollsnapchanging",
    "ongamepadconnected", "ongamepaddisconnected",
    "oncontextlost", "oncontextrestored",
    "oncancel", "onclose", "oncuechange", "onformdata", "onmousewheel",
    "onpointerrawupdate", "onsearch", "onsecuritypolicyviolation", "onslotchange",
    "ontimezonechange", "ontoggle", "onbeforexrselect",
    "oncontentvisibilityautostatechange",
    "onwebkitanimationstart", "onwebkitanimationend", "onwebkitanimationiteration",
    "onwebkittransitionend",
)


def _install_handler_slots(cls, names):
    """Create one getter/setter pair per event-handler attribute.

    ``_raw_callback`` resolves a callback by *name* from the class ``__mro__``, so
    one generic accessor cannot serve several attribute names; generated per slot
    once at import time.  The convention binds them, so nothing is declared here.
    """
    for name in names:
        def getter(self, _name=name):
            return self._get_handler(_name)

        def setter(self, value, _name=name):
            self._set_handler(_name, value)

        getter.__name__ = "get_" + name
        setter.__name__ = "set_" + name
        setattr(cls, getter.__name__, getter)
        setattr(cls, setter.__name__, setter)


#: The slots an event target answers are instance members here: they belong to each object, not
#: to ``EventTarget.prototype``.  No interface in the document states them on ``EventTarget`` --
#: it answers them through the objects that carry them -- so the class that installs them is the
#: one that says where they live.
_install_handler_slots(EventTarget, _HANDLER_SLOTS)
EventTarget.__idl_overrides__ = {
    "members": {name: {"location": "instance"} for name in _HANDLER_SLOTS},
    "interface": {"length": 0},
}
