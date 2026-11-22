"""Constructors, dispatch and handler slots for the DOM event model."""
import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")



@pytest.fixture
async def event_page(page):
    (await page.set_content("<div id='app'><input id='field'></div>", url="https://example.test/", wait_until="commit"))
    return page


async def test_mouse_and_keyboard_event_init_members(event_page):
    value = event_page.evaluate("""
      (() => {
        const e = new MouseEvent('click', {clientX: 10, clientY: 20, screenX: 1, screenY: 2,
            pageX: 11, pageY: 21, button: 1, buttons: 3, altKey: true, ctrlKey: true});
        const k = new KeyboardEvent('keydown', {key: 'a', code: 'KeyA', keyCode: 65, charCode: 97,
            repeat: true, shiftKey: true, isComposing: true, location: 1});
        return [
          e instanceof Event, e instanceof UIEvent, e instanceof MouseEvent,
          e.clientX, e.clientY, e.screenX, e.screenY, e.pageX, e.pageY, e.x, e.y, e.offsetX,
          e.button, e.buttons, e.altKey, e.ctrlKey, e.metaKey,
          e.which, e.getModifierState('Alt'), e.getModifierState('Meta'),
          Object.prototype.toString.call(e),
          k instanceof KeyboardEvent, k instanceof UIEvent,
          k.key, k.code, k.keyCode, k.charCode, k.repeat, k.shiftKey, k.isComposing, k.location,
          k.which, k.getModifierState('Shift'),
        ];
      })()
    """)
    assert value == [
        True, True, True,
        10, 20, 1, 2, 11, 21, 10, 20, 10,
        1, 3, True, True, False,
        2, True, False,
        "[object MouseEvent]",
        True, True,
        "a", "KeyA", 65, 97, True, True, True, 1,
        65, True,
    ]


async def test_mouse_event_defaults_and_related_target(event_page):
    assert event_page.evaluate("""
      (() => {
        const target = document.querySelector('#app');
        const empty = new MouseEvent('mousemove');
        const over = new MouseEvent('mouseover', {relatedTarget: target});
        const out = new MouseEvent('mouseout', {relatedTarget: target});
        return [empty.clientX, empty.clientY, empty.pageX, empty.button, empty.buttons,
                empty.relatedTarget === null, empty.which,
                over.relatedTarget === target, over.fromElement === target, over.toElement === null,
                out.toElement === target, out.fromElement === null];
      })()
    """) == [0, 0, 0, 0, 0, True, 1,
             True, True, True,
             True, True]


async def test_pointer_and_wheel_events_extend_mouse_event(event_page):
    assert event_page.evaluate("""
      (() => {
        const p = new PointerEvent('pointerdown', {pointerId: 7, pointerType: 'touch',
            pressure: 0.25, width: 12, height: 8, isPrimary: true, tiltX: 3});
        const w = new WheelEvent('wheel', {deltaY: 120, deltaMode: 0, clientX: 4});
        return [p instanceof PointerEvent, p instanceof MouseEvent,
                p.pointerId, p.pointerType, p.pressure, p.width, p.height, p.isPrimary, p.tiltX,
                p.clientX, p.which, p.getCoalescedEvents().length, p.getPredictedEvents().length,
                new PointerEvent('pointermove').pressure,
                w instanceof WheelEvent, w instanceof MouseEvent,
                w.deltaY, w.deltaMode, w.wheelDelta, w.clientX];
      })()
    """) == [True, True,
             7, "touch", 0.25, 12, 8, True, 3,
             0, 1, 0, 0,
             0.5,
             True, True,
             120, 0, -120, 4]


@pytest.mark.parametrize("expression, expected", [
    ("new FocusEvent('focus', {relatedTarget: 5}).relatedTarget", 5),
    ("new InputEvent('input', {data: 'x', inputType: 'insertText', isComposing: true}).data", "x"),
    ("new InputEvent('input', {data: 'x'}).inputType", ""),
    ("new CompositionEvent('compositionstart', {data: 'ni'}).data", "ni"),
    ("new PopStateEvent('popstate', {state: {n: 1}}).state.n", 1),
    ("new HashChangeEvent('hashchange', {oldURL: 'a', newURL: 'b'}).oldURL", "a"),
    ("new HashChangeEvent('hashchange', {newURL: 'b'}).newURL", "b"),
    ("new StorageEvent('storage', {key: 'k', oldValue: 'o', newValue: 'v', url: '/u'}).newValue", "v"),
    ("new AnimationEvent('animationstart', {animationName: 'spin', elapsedTime: 1.5}).elapsedTime", 1.5),
    ("new TransitionEvent('transitionend', {propertyName: 'opacity'}).propertyName", "opacity"),
    ("new PromiseRejectionEvent('unhandledrejection', {reason: 'boom'}).reason", "boom"),
    ("new PageTransitionEvent('pageshow', {persisted: true}).persisted", True),
    ("new PageTransitionEvent('pagehide').persisted", False),
    ("new ProgressEvent('progress', {lengthComputable: true, loaded: 5, total: 10}).total", 10),
    ("new ProgressEvent('progress').lengthComputable", False),
    ("new ErrorEvent('error', {message: 'm', filename: 'f.js', lineno: 3, colno: 4}).lineno", 3),
    ("new ErrorEvent('error').colno", 0),
    ("new CustomEvent('x', {detail: [1, 2]}).detail.length", 2),
    ("new UIEvent('x', {detail: 9}).detail", 9),
    ("new MessageEvent('message', {data: 'd', lastEventId: '7'}).lastEventId", "7"),
    ("new MessageEvent('message', {ports: []}).ports.length", 0),
    ("new CloseEvent('close', {code: 1000, reason: 'bye'}).code", 1000),
])
async def test_event_init_members_round_trip(event_page, expression, expected):
    assert event_page.evaluate(expression) == expected


async def test_nullable_event_members_default_to_null(event_page):
    from pyv8c import pyv8

    for expression in (
        "new InputEvent('input').data",
        "new InputEvent('input').dataTransfer",
        "new PopStateEvent('popstate').state",
        "new StorageEvent('storage', {key: 'k'}).oldValue",
        "new FocusEvent('focus').relatedTarget",
    ):
        assert event_page.evaluate(expression) is pyv8.Null, expression


async def test_before_unload_event_is_not_constructible(event_page):
    assert event_page.evaluate("""
      (() => {
        try { new BeforeUnloadEvent('beforeunload'); return 'allowed'; }
        catch (e) { return e.name; }
      })()
    """) == "TypeError"
    assert event_page.evaluate("typeof BeforeUnloadEvent") == "function"


async def test_legacy_init_event_and_init_custom_event(event_page):
    assert event_page.evaluate("""
      (() => {
        const e = new Event('first');
        e.initEvent('second', true, true);
        const c = new CustomEvent('before');
        c.initCustomEvent('after', true, false, {n: 2});
        const missing = [];
        for (const fn of [() => e.initEvent(), () => c.initCustomEvent()]) {
          try { fn(); missing.push(false); } catch (err) { missing.push(err.name); }
        }
        return [e.type, e.bubbles, e.cancelable, c.type, c.cancelable, c.detail.n, missing];
      })()
    """) == ["second", True, True, "after", False, 2, ["TypeError", "TypeError"]]


async def test_dispatch_reaches_listener_and_handler_slot(event_page):
    assert event_page.evaluate("""
      (() => {
        const el = document.querySelector('#field');
        const order = [];
        el.addEventListener('click', () => order.push('listener'));
        el.onclick = () => order.push('handler');
        el.dispatchEvent(new MouseEvent('click', {clientX: 3}));
        el.onclick = 5;
        const cleared = el.onclick === null;
        el.onclick = () => order.push('again');
        el.dispatchEvent(new MouseEvent('click'));
        return [order, cleared, typeof el.onclick];
      })()
    """) == [["listener", "handler", "listener", "again"], True, "function"]


async def test_handler_slot_bubbles_to_document_delegate(event_page):
    assert event_page.evaluate("""
      (() => {
        const seen = [];
        document.addEventListener('keydown', e => seen.push([e.key, e.target.id]));
        document.addEventListener('input', e => seen.push([e.data, e.target.id]));
        const el = document.querySelector('#field');
        el.dispatchEvent(new KeyboardEvent('keydown', {key: 'a', bubbles: true}));
        el.dispatchEvent(new InputEvent('input', {data: 'z', bubbles: true}));
        return seen;
      })()
    """) == [["a", "field"], ["z", "field"]]


async def test_listener_options_keep_their_meaning(event_page):
    assert event_page.evaluate("""
      (() => {
        const el = document.querySelector('#app');
        const order = [];
        el.addEventListener('y', () => order.push('once'), {once: true});
        el.dispatchEvent(new Event('y'));
        el.dispatchEvent(new Event('y'));
        el.addEventListener('z', () => order.push('bubble'));
        el.addEventListener('z', () => order.push('capture'), true);
        el.dispatchEvent(new Event('z'));
        return order;
      })()
    """) == ["once", "capture", "bubble"]


async def test_handler_slots_are_prototype_accessors_that_start_out_null(event_page):
    assert event_page.evaluate("""
      (() => {
        const body = document.body;
        const unset = [body.onclick, body.oninput, body.onkeydown, body.onwheel,
                       body.onpointerdown, body.onanimationstart, body.ontransitionend,
                       window.onhashchange, window.onpopstate, window.onstorage,
                       document.onvisibilitychange, document.onfreeze, document.onresume];
        const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'onfocus');
        const element = document.querySelector('#field');
        const handler = () => {};
        element.onfocus = handler;
        return [unset.every(value => value === null),
                typeof descriptor.get, typeof descriptor.set,
                descriptor.get.call(element) === handler,
                Object.prototype.hasOwnProperty.call(element, 'onfocus')];
      })()
    """) == [True, "function", "function", True, False]


