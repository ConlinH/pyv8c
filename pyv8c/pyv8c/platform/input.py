"""The page's mouse: where it is, which buttons are down, and what that fires.

A mouse is a state machine rather than a single "click" call: moving crosses
element boundaries, pressing focuses, and a press and a release on different
elements still make one ``click`` at their nearest common ancestor.  Every
sequence here was observed on the wire from Chrome 153 instead of derived from
the specification:

* a move onto a new element fires ``pointerout``/``pointerover`` and then
  ``mouseout``/``mouseover``, followed by ``pointermove`` and ``mousemove``;
* a press is ``pointerdown``, ``mousedown`` and then focus, in that order --
  and a cancelled ``mousedown`` leaves focus where it was;
* a release is ``pointerup``, ``mouseup`` and then ``click``, whose activation
  behavior runs when no listener cancelled the event; a second click close
  enough in time and place reports ``detail`` 2 and is followed by ``dblclick``;
* a disabled form control receives the pointer events and none of the others,
  which is how the mouse and ``click()`` both treat it;
* ``mouseenter``/``mouseleave`` and their pointer twins do not bubble or cross a
  shadow root, but an ancestor can still observe them while capturing.

The controller owns the position, the button mask and the click counter, and
every entry point runs on the page's owner thread like the rest of the DOM
surface.
"""

from __future__ import annotations

import math
import asyncio
import time
from contextlib import asynccontextmanager

from pyv8c import pyv8

from ..foundation.host_errors import HostStateError
from ..web.dom.activation import accepts_mouse_events, prepare_activation, complete_activation
from ..web.dom.focus import focus_on_mousedown
from ..web.dom.hit_testing import border_box, deep_element_from_point as element_from_point

#: The DOM's button index and the ``buttons`` mask bit, per button name.
BUTTON_INDEX = {"left": 0, "middle": 1, "right": 2}
BUTTON_BIT = {"left": 1, "middle": 4, "right": 2}

#: The primary mouse pointer, as Chrome describes it on pointer events.
POINTER_ID = 1

#: Chromium's own window for reporting ``detail`` 2 and firing ``dblclick``.
MULTI_CLICK_INTERVAL = 0.5
MULTI_CLICK_DISTANCE = 4.0


def _ancestors(node):
    """*node* and its ancestors, itself first, walking out of a shadow tree."""
    chain = []
    current = node
    while current is not None:
        chain.append(current)
        parent = getattr(current, "parentElement", None)
        current = parent if parent is not None else getattr(getattr(current, 'parentNode', None), '_host', None)
    return chain


def _leaving(previous, target):
    """The elements a leave event reaches, the element being left first."""
    staying = _ancestors(target) if target is not None else []
    return [node for node in _ancestors(previous)
            if not any(node is other for other in staying)]


def _entering(previous, target):
    """The elements an enter event reaches, the outermost one first."""
    leaving = _ancestors(previous) if previous is not None else []
    chain = [node for node in _ancestors(target)
             if not any(node is other for other in leaving)]
    return list(reversed(chain))


def _common_ancestor(first, second):
    """The nearest element containing both, which is where a press-release clicks."""
    if first is None or second is None:
        return first or second
    below = _ancestors(second)
    for node in _ancestors(first):
        if any(node is other for other in below):
            return node
    return None


def _pixel(value):
    """Chrome's rounding for a mouse event's coordinate: halves away from zero."""
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def describe(element):
    """A short ``<tag id="...">`` label for error messages."""
    if element is None:
        return "nothing"
    tag = getattr(element, "localName", None) or "node"
    identifier = getattr(element, "attributes", {}).get("id")
    return f"<{tag} id={identifier!r}>" if identifier else f"<{tag}>"


#: Positions a human path is sampled at when the caller names no count.
HUMAN_SAMPLES = 24


def _straight_points(start, end, samples):
    """*samples* positions evenly spaced from *start* to *end*, the last one at *end*."""
    start_x, start_y = start
    end_x, end_y = end
    return [(start_x + (end_x - start_x) * step / samples,
             start_y + (end_y - start_y) * step / samples) for step in range(1, samples + 1)]


def _curve_points(curve, start, end, samples, seed):
    """The positions a hand passes through, ending exactly at *end*.

    A real reach bows sideways and starts and stops slowly.  The same *seed* replays
    the same path, which is what keeps a test that asks for one reproducible.
    """
    if curve != "human":
        raise ValueError(f"unknown curve {curve!r}; expected 'human'")
    import random

    start_x, start_y = start
    end_x, end_y = end
    delta_x, delta_y = end_x - start_x, end_y - start_y
    reach = math.hypot(delta_x, delta_y)
    samples = max(2, int(samples or HUMAN_SAMPLES))
    if not reach:
        return [(end_x, end_y)] * samples
    rng = random.Random(seed)
    # The control point sits off the straight line: that offset is the bow.
    bow = rng.uniform(-0.15, 0.15) * reach
    control_x = start_x + delta_x / 2 - delta_y / reach * bow
    control_y = start_y + delta_y / 2 + delta_x / reach * bow
    wobble = min(1.0, reach * 0.01)
    points = []
    for index in range(1, samples + 1):
        eased = 0.5 - 0.5 * math.cos(math.pi * index / samples)
        inverse = 1.0 - eased
        point_x = (inverse * inverse * start_x + 2 * inverse * eased * control_x
                   + eased * eased * end_x)
        point_y = (inverse * inverse * start_y + 2 * inverse * eased * control_y
                   + eased * eased * end_y)
        if index < samples:
            # Every position but the landing one wanders, the way a hand's does.
            point_x += rng.uniform(-wobble, wobble)
            point_y += rng.uniform(-wobble, wobble)
        points.append((point_x, point_y))
    return points


class MouseInput:
    """One page's mouse, reached as ``page.mouse``.

    ``position`` and ``pressed`` are plain reads of host-side state and may be
    read from any thread. Event operations require the Runtime owner thread.
    """

    def __init__(self, page):
        self._page = page
        self._gesture_lock = asyncio.Lock()
        self._x = 0.0
        self._y = 0.0
        self._movement_x = 0
        self._movement_y = 0
        self._moved = False
        self._bits = 0
        self._hovered = None
        #: The pointer starts outside the page, so its first move inside reports the
        #: document picking it up, as a window does when a cursor arrives.
        self._inside_page = False
        self._pressed = None
        self._press_count = 1
        self._count = 0
        self._last_click = None

    @property
    def position(self) -> tuple[float, float]:
        """The mouse position as ``(x, y)`` in viewport coordinates."""
        return (self._x, self._y)

    @property
    def pressed(self) -> frozenset[str]:
        """The buttons currently held down, as a set of names."""
        return frozenset(name for name, bit in BUTTON_BIT.items() if self._bits & bit)

    @asynccontextmanager
    async def _gesture(self):
        self._page._check_wait()
        generation = self._page.navigation.generation
        async with self._gesture_lock:
            self._page._check_document_generation(generation)
            yield generation

    async def move(self, x: float, y: float, *, steps=None, duration=None, curve=None, seed=None):
        async with self._gesture():
            await self._move(float(x), float(y), steps, duration, curve, seed)

    async def _move(self, x, y, steps, duration, curve, seed):
        generation = self._page.navigation.generation
        if not duration:
            return self._call(self._move_to, x, y, steps, curve=curve, seed=seed)
        start, end = (self._x, self._y), (x, y)
        points = (_curve_points(curve, start, end, steps, seed) if curve is not None
                  else _straight_points(start, end, max(1, int(steps or 1))))
        interval = max(0.0, float(duration)) / len(points)
        for point_x, point_y in points:
            await asyncio.sleep(interval)
            self._page._check_document_generation(generation)
            # A paced move is re-hit-tested after each suspension.
            self._call(self._move_once, point_x, point_y)

    async def down(self, *, button="left", click_count=None):
        async with self._gesture():
            self._call(self._down, self._button(button), click_count)

    async def up(self, *, button="left", click_count=None):
        async with self._gesture():
            self._call(self._up, self._button(button), click_count)

    async def click(self, x=None, y=None, *, button="left", click_count=None, delay=None):
        async with self._gesture():
            await self._click_sequence(x, y, self._button(button), click_count, delay)

    async def _click_sequence(self, x, y, button, click_count, delay, *, element=None, force=False):
        generation = self._page.navigation.generation
        def dispatch(action):
            if element is not None:
                return self._point_in(element, force, action)
            return action(x, y, None)
        if not delay:
            return self._call(dispatch, lambda x, y, known: self._click(x, y, button, click_count, known))
        def press(x, y, known):
            if x is not None and y is not None:
                self._move_to(float(x), float(y), 1, known)
            self._down(button, click_count)
        self._call(dispatch, press)
        try:
            await asyncio.sleep(max(0.0, float(delay)))
            self._page._check_document_generation(generation)
            self._call(self._up, button, click_count)
        except BaseException:
            # A cancelled press cannot synthesize a click on a replacement document.
            self._pressed = None
            self._bits &= ~BUTTON_BIT[button]
            raise

    async def dblclick(self, x=None, y=None, *, button="left", delay=None):
        async with self._gesture() as generation:
            button = self._button(button)
            await self._click_sequence(x, y, button, 1, delay)
            self._page._check_document_generation(generation)
            await self._click_sequence(None, None, button, 2, delay)

    async def move_to_element(self, element, *, force=False, duration=None, curve=None, seed=None):
        async with self._gesture() as generation:
            if not duration:
                return self._call(self._point_in, element, force,
                    lambda x, y, known: self._move_to(x, y, None, known, curve=curve, seed=seed))
            x, y, _ = self._call(self._point_in, element, force, lambda *point: point)
            self._page._check_document_generation(generation)
            await self._move(x, y, None, duration, curve, seed)

    async def click_element(self, element, *, button="left", click_count=1,
                            force=False, delay=None, duration=None):
        async with self._gesture() as generation:
            button = self._button(button)
            if duration:
                x, y, _ = self._call(self._point_in, element, force, lambda *point: point)
                self._page._check_document_generation(generation)
                await self._move(x, y, None, duration, None, None)
                self._page._check_document_generation(generation)
            # Revalidate the element at dispatch. Geometry and the first input
            # event share one native boundary, including after a queued gesture.
            await self._click_sequence(None, None, button, int(click_count), delay,
                                       element=element, force=force)

    def _reset_document(self):
        # Viewport coordinates survive navigation; DOM receivers and a press
        # begun in the retired document do not.
        self._hovered = self._pressed = self._last_click = None
        self._bits = 0
        self._inside_page = False

    def _call(self, action, *args, **kwargs):
        self._page._check()
        return self._page.realm.executor.call(action, *args, **kwargs)

    def _button(self, name):
        if name not in BUTTON_INDEX:
            raise ValueError(
                f"unknown mouse button {name!r}; expected one of "
                f"{', '.join(sorted(BUTTON_INDEX))}"
            )
        return name

    def _hit(self, x, y):
        """``(hit, target)`` for a point: what is under it, and what an event lands on."""
        document = self._page.document
        hit = element_from_point(document, x, y)
        if hit is not None:
            return hit, hit
        root = document.get_documentElement()
        return None, None if root is None or root is pyv8.Null else root

    def _target(self):
        """The element the mouse is over, or the root when it is outside the viewport."""
        return self._hit(self._x, self._y)[1]

    def _screen_position(self):
        """Translate frame-local coordinates to the top-level viewport origin."""
        x, y = self._x, self._y
        page = self._page
        while page.parent_page is not None:
            frame = page.window._frame_element
            if frame is None:
                break
            box = border_box(frame)
            x += box.left + box.border.get('left', 0.0) + box.padding.get('left', 0.0)
            y += box.top + box.border.get('top', 0.0) + box.padding.get('top', 0.0)
            page = page.parent_page
        viewport = page.window._environment.viewport
        return x + viewport.screen_x, y + viewport.screen_y

    def _emit(self, event_type, target, *, button=0, detail=0, related=None,
              bubbles=True, cancelable=True, event_class=None, propagates=True,
              coalesced=(), boundary_capture=False):
        """Dispatch one event and hand it back, or ``None`` when there is no target."""
        if target is None:
            return None
        boundary = event_type in ('mouseenter', 'mouseleave', 'pointerenter', 'pointerleave')
        if (event_type in ('mouseenter', 'mouseleave') and not boundary_capture
                and not self._has_boundary_listener(target, event_type)):
            #: Chromium does not create an unused mouse enter/leave event, which also
            #: keeps them out of ``performance.eventCounts``; the pointer twins are
            #: created whether or not anyone listens.
            return None
        if boundary:
            propagates = boundary_capture or self._has_boundary_listener(
                target, event_type, capture_only=True)
        screen_x, screen_y = self._screen_position()
        options = {
            #: Enter/leave stay inside their tree; every other input event is composed
            #: and therefore crosses shadow boundaries, as a driven-input probe measured.
            "composed": not boundary,
            "bubbles": bubbles, "cancelable": cancelable, "detail": detail,
            "button": button, "buttons": self._bits,
            "clientX": self._x, "clientY": self._y,
            # Screen coordinates include the configured window position.
            "screenX": screen_x, "screenY": screen_y,
            "movementX": self._movement_x if event_type.endswith("move") else 0,
            "movementY": self._movement_y if event_type.endswith("move") else 0,
        }
        if related is not None:
            options["relatedTarget"] = related
        interface = event_class or ("PointerEvent" if (event_type.startswith("pointer") or event_type == "click") else "MouseEvent")
        if interface == "PointerEvent":
            options.update({"pointerId": POINTER_ID, "pointerType": "mouse", "isPrimary": event_type != "click", "pressure": 0.5 if self._bits else 0})
            if coalesced:
                options["coalesced"] = tuple(coalesced)
            if event_type.startswith("pointer") and not event_type.endswith(("down", "up")):
                # No button transition is -1 on a pointer event; the mouse event
                # beside it keeps the 0 Chromium gives it.
                options["button"] = -1
        if interface != "PointerEvent" or event_type == "click":
            # A mouse event reports whole pixels; the pointer event beside it keeps
            # the fraction the layout measured.
            for name in ("clientX", "clientY", "screenX", "screenY"):
                options[name] = math.floor(options[name])
        generation = self._page.navigation.generation
        event = self._page.lifecycle.dispatch_page_event(
            event_type, target, options, event_class=interface, propagates=propagates,
        )
        # Event handlers can replace the document synchronously, even in an
        # unpaced gesture. Stop before the next event or default action.
        if self._page.navigation.generation != generation:
            raise HostStateError("document changed during input")
        return event

    def _has_boundary_listener(self, target, event_type, *, capture_only=False):
        """Whether a boundary event at *target* would reach anyone.

        Blink's fast path skips the ancestor walk when nothing can hear it: a capturing
        registration on a DOM node enables the walk, while one on the Window alone does
        not -- the Window can still observe the walk another listener enabled.
        """
        node = target
        while node is not None:
            for registration in getattr(node, '_listeners', {}).get(event_type, ()):
                _listener, capture, _once, _passive, signal, _order = registration
                if ((capture or (node is target and not capture_only))
                        and (signal is None or not getattr(signal, 'aborted', False))):
                    return True
            if (not capture_only and node is target
                    and callable(getattr(node, '_handlers', {}).get('on' + event_type))):
                return True
            node = getattr(node, 'parentNode', None)
        return False

    def _move_to(self, x, y, steps, known=None, *, curve=None, seed=None):
        start, end = (self._x, self._y), (float(x), float(y))
        points = (_curve_points(curve, start, end, steps, seed) if curve is not None
                  else _straight_points(start, end, max(1, int(steps or 1))))
        batch = []
        for index, (point_x, point_y) in enumerate(points, start=1):
            pointer = self._move_once(point_x, point_y, known if index == len(points) else None, tuple(batch))
            if pointer is not None:
                batch.append(pointer)

    def _move_once(self, x, y, known=None, coalesced=()):
        # Chrome reports whole pixels, and the first move after the cursor arrives
        # reports no movement at all.
        if self._moved:
            self._movement_x, self._movement_y = _pixel(x - self._x), _pixel(y - self._y)
        else:
            self._movement_x = self._movement_y = 0
            self._moved = True
        self._x, self._y = x, y
        # One hit test per position: it walks the whole tree, so both answers come from it.
        hit, target = self._hit(x, y) if known is None else known
        self._cross(target, hit is not None)
        pointer = self._emit("pointermove", target, coalesced=coalesced)
        self._emit("mousemove", target)
        return pointer

    def _cross(self, target, inside=True):
        """Fire the boundary events a move from the hovered element to *target* makes."""
        previous = self._hovered
        page_changed = inside != self._inside_page
        if target is previous and not page_changed:
            return
        self._inside_page = inside
        self._hovered = target
        for kind, interface in (("pointer", "PointerEvent"), ("mouse", "MouseEvent")):
            #: Snapshot the capturing listeners for the whole boundary sequence: a once
            #: listener may be removed by its first event, which must not truncate the rest.
            leave_capture = self._has_boundary_listener(previous, f'{kind}leave', capture_only=True)
            if previous is not None and target is not previous:
                self._emit(f"{kind}out", previous, related=target, event_class=interface)
                for node in _leaving(previous, target):
                    self._emit(f"{kind}leave", node, related=target, event_class=interface,
                               bubbles=False, cancelable=False, propagates=False,
                               boundary_capture=leave_capture)
            if target is not None and target is not previous:
                self._emit(f"{kind}over", target, related=previous, event_class=interface)
            enter_capture = self._has_boundary_listener(target, f'{kind}enter', capture_only=True)
            # The document reports the cursor arriving in and leaving the page: its
            # enter is the outermost one, and its leave the last.
            if page_changed and inside:
                self._page_event(kind, interface, "enter", boundary_capture=enter_capture)
            if target is not None and target is not previous:
                for node in _entering(previous, target):
                    self._emit(f"{kind}enter", node, related=previous, event_class=interface,
                               bubbles=False, cancelable=False, propagates=False,
                               boundary_capture=enter_capture)
            if page_changed and not inside:
                self._page_event(kind, interface, "leave", boundary_capture=leave_capture)

    def _page_event(self, kind, interface, phase, *, boundary_capture=False):
        self._emit(f"{kind}{phase}", self._page.document, event_class=interface,
                   bubbles=False, cancelable=False, propagates=False,
                   boundary_capture=boundary_capture)

    def _count_for(self, target, click_count):
        """The ``detail`` this press reports, and the run it belongs to."""
        if click_count is not None:
            self._count = max(1, int(click_count))
        else:
            now = time.monotonic()
            previous = self._last_click
            if (previous is not None
                    and now - previous[0] <= MULTI_CLICK_INTERVAL
                    and abs(self._x - previous[1]) <= MULTI_CLICK_DISTANCE
                    and abs(self._y - previous[2]) <= MULTI_CLICK_DISTANCE
                    and previous[3] is target):
                self._count += 1
            else:
                self._count = 1
        self._last_click = (time.monotonic(), self._x, self._y, target)
        return self._count

    def _down(self, button, click_count):
        target = self._target()
        count = self._count_for(target, click_count)
        self._bits |= BUTTON_BIT[button]
        self._pressed, self._press_count = target, count
        self._emit("pointerdown", target, button=BUTTON_INDEX[button])
        if not accepts_mouse_events(target):
            return
        event = self._emit("mousedown", target, button=BUTTON_INDEX[button], detail=count)
        # Focus is all a cancelled press takes away; the click still fires.
        if event is None or not event.defaultPrevented:
            focus_on_mousedown(target)

    def _up(self, button, click_count):
        target = self._target()
        count = self._press_count if click_count is None else max(1, int(click_count))
        pressed = self._pressed
        self._pressed = None
        self._bits &= ~BUTTON_BIT[button]
        self._emit("pointerup", target, button=BUTTON_INDEX[button])
        if not accepts_mouse_events(target):
            return
        self._emit("mouseup", target, button=BUTTON_INDEX[button], detail=count)
        clicked = _common_ancestor(pressed, target) or target
        if button != "left":
            self._emit("auxclick", clicked, button=BUTTON_INDEX[button], detail=count)
            if button == "right":
                self._emit("contextmenu", clicked, button=BUTTON_INDEX[button])
            return
        prepared = prepare_activation(clicked)
        event = self._emit("click", clicked, detail=count)
        complete_activation(clicked, event, prepared)
        if count == 2:
            self._emit("dblclick", clicked, detail=count)

    def _click(self, x, y, button, click_count, known=None):
        if x is not None and y is not None:
            self._move_to(float(x), float(y), 1, known)
        self._down(button, click_count)
        self._up(button, click_count)

    def _point_in(self, element, force, action):
        """The centre of *element*, once it is the element a click would reach there."""
        if element.ownerDocument is not self._page.document or not element.isConnected:
            raise HostStateError("input element belongs to a retired or detached document")
        box = border_box(element)
        if box.width <= 0 or box.height <= 0:
            raise HostStateError(
                f"{describe(element)} has no box to click: it is not rendered"
            )
        x, y = box.left + box.width / 2, box.top + box.height / 2
        # Nothing runs between this read and the action, so the landing the check saw is
        # the one the action starts from.
        known = None if force else self._hit(x, y)
        if known is not None:
            hit = known[0]
            covered = hit is not element and not any(
                node is element for node in _ancestors(hit)
            )
            if covered:
                raise HostStateError(
                    f"{describe(element)} is covered at its centre ({x:g}, {y:g})"
                    f" by {describe(hit)}; pass force=True to click there anyway"
                )
        return action(x, y, known)



__all__ = ["MouseInput"]
