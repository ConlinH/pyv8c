"""DOM members a real page asked for, plus the URL and stylesheet plumbing.

The member tests all came out of a live page that reported them as
unimplemented.  The URL attributes share one resolution helper, and the
stylesheet tests guard the cascade entry points that keep ``layout`` from
rescanning the document on every read.
"""

import asyncio
import json

import pytest
from loguru import logger

from pyv8c.async_api import Browser

CONTENT = """
<html><head><title>  Live   Page </title>
<style>
  .item { color: red; }
  div { font-size: 20px; }
  #sized { width: 40px; height: 10px; }
</style></head>
<body>
  <form name="login"><input name="user"><input name="user"></form>
  <p name="user"></p>
  <div class="item"><span id="sized">x</span></div>
  <div class="item" style="color: blue">inline</div>
  <link rel="stylesheet" media="">
  <textarea></textarea>
  <select><option value="a">A</option><option>B</option></select>
  <a id="link" href="/dir/target?q=1#frag">link</a>
  <script>window.__current = document.currentScript && document.currentScript.tagName;</script>
</body></html>
"""


@pytest.fixture
async def page():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content(CONTENT, url="https://dom.test/dir/page", wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    try:
        yield page
    finally:
        (await browser.close())


async def test_document_implementation_factories(page):
    assert page.evaluate("document.implementation === document.implementation") is True
    assert page.evaluate("document.implementation.hasFeature()") is True
    assert page.evaluate("""
        (function () {
            const created = document.implementation.createHTMLDocument('T');
            return [created.title, created.documentElement.tagName, created.head.tagName,
                    created.body.tagName, created.URL];
        })()
    """) == ["T", "HTML", "HEAD", "BODY", "about:blank"]
    assert page.evaluate("""
        (function () {
            const doctype = document.implementation.createDocumentType('html', 'p', 's');
            const doc = document.implementation.createDocument(null, 'root');
            return [doctype.name, doctype.publicId, doctype.systemId,
                    doc.documentElement.tagName, doc.childNodes.length];
        })()
    """) == ["html", "p", "s", "ROOT", 1]


async def test_current_script_is_set_only_while_a_script_runs(page):
    assert page.evaluate("window.__current") == "SCRIPT"
    assert page.evaluate("""
        (function () {
            let seen = 'unset';
            document.body.addEventListener('probe', () => { seen = String(document.currentScript); });
            document.body.dispatchEvent(new Event('probe'));
            return seen;
        })()
    """) == "null"
    assert page.evaluate("String(document.currentScript)") == "null"


async def test_document_fragment_queries_its_own_subtree(page):
    assert page.evaluate("""
        (function () {
            const fragment = document.createDocumentFragment();
            const item = document.createElement('div');
            item.className = 'item';
            fragment.appendChild(item);
            fragment.appendChild(document.createElement('span'));
            return [fragment.querySelectorAll('.item').length,
                    fragment.querySelector('span').tagName,
                    fragment.querySelectorAll('div').length];
        })()
    """) == [1, "SPAN", 1]


async def test_link_media_and_anchor_url_members(page):
    assert page.evaluate("""
        (function () {
            const link = document.querySelector('link');
            link.media = 'print';
            return [link.media, link.getAttribute('media')];
        })()
    """) == ["print", "print"]
    # href/pathname/search/hash/protocol: corpus ``anchor_href_resolves_against_the_document``.
    assert page.evaluate("""
        (function () {
            const anchor = document.querySelector('#link');
            return [anchor.hostname, anchor.origin, String(anchor)];
        })()
    """) == ["dom.test", "https://dom.test", "https://dom.test/dir/target?q=1#frag"]
    # Assigning a component rewrites the href attribute, as URLUtils requires.
    assert page.evaluate("""
        (function () {
            const anchor = document.createElement('a');
            anchor.setAttribute('href', 'https://u:p@h.test:81/a?x=1#h');
            const credentials = [anchor.username, anchor.password, anchor.host, anchor.port];
            anchor.pathname = '/b';
            anchor.hash = '#z';
            const rewritten = anchor.href;
            anchor.hostname = 'other.test';
            anchor.port = '';
            return [credentials, rewritten, anchor.href];
        })()
    """) == [["u", "p", "h.test:81", "81"], "https://u:p@h.test:81/b?x=1#z", "https://u:p@other.test/b?x=1#z"]
    assert page.evaluate("String(document.createElement('a').protocol)") == ":"
    assert page.evaluate("document.createElement('a').origin") == ""


async def test_textarea_option_and_select_values(page):
    assert page.evaluate("""
        (function () {
            const area = document.querySelector('textarea');
            const empty = area.defaultValue;
            area.defaultValue = 'raw text';
            return [empty, area.defaultValue, area.textContent];
        })()
    """) == ["", "raw text", "raw text"]
    assert page.evaluate("""
        (function () {
            const select = document.querySelector('select');
            const [first, second] = select.querySelectorAll('option');
            const initial = [select.value, first.selected, second.selected, first.value, second.value];
            select.value = 'B';
            const afterB = [select.value, first.selected, second.selected];
            select.value = 'missing';
            return [initial, afterB, [select.value, first.selected, second.selected]];
        })()
    """) == [["a", True, False, "a", "B"], ["B", False, True], ["", False, False]]


async def test_a_mutation_observer_reports_child_and_attribute_changes(page):
    page.evaluate("""(() => {
        window.__records = [];
        const target = document.createElement('div');
        document.body.appendChild(target);
        new MutationObserver(records => {
          window.__records.push(...records.map(
              record => [record.type, record.target.tagName, record.addedNodes.length]));
        }).observe(target, {childList: true, attributes: true});
        target.appendChild(document.createElement('span'));
        target.setAttribute('data-x', '1');
        target.removeAttribute('data-x');
    })()""")
    assert page.evaluate("window.__records") == [
        ["childList", "DIV", 1], ["attributes", "DIV", 0], ["attributes", "DIV", 0],
    ]


async def test_a_mutation_observer_hears_only_what_it_asked_for(page):
    """Records are queued, not delivered: ``takeRecords`` takes them first."""
    assert page.evaluate("""(() => {
        const heard = [];
        const target = document.createElement('div');
        document.body.appendChild(target);
        const observer = new MutationObserver(records => {
          heard.push(...records.map(record => record.type));
        });
        observer.observe(target, {attributes: true});
        target.appendChild(document.createElement('i'));
        target.setAttribute('data-y', '2');
        const taken = observer.takeRecords().map(record => record.type);
        observer.disconnect();
        target.setAttribute('data-z', '3');
        return [taken, observer.takeRecords().length, heard.length];
    })()""") == [["attributes"], 0, 0]


async def test_xpath_walks_the_document(page):
    # Predicates and snapshots: corpus ``xpath_predicates_and_snapshots``.
    (await page.set_content('<p class="x">one</p><p class="x">two</p><p>three</p>',
                     url="https://secure.test/", wait_until="commit"))
    assert page.evaluate("""(() => {
        const href = document.evaluate("//a/@href", document, null,
            XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const second = document.createExpression("//p[2]", null)
            .evaluate(document, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        return [href.snapshotLength, second.singleNodeValue.textContent];
    })()""") == [0, "two"]


async def test_a_frames_window_keeps_its_identity_across_a_navigation(page):
    (await page.set_content('<body><iframe id="f" src="about:blank"></iframe></body>',
                     url="https://frame.test/", wait_until="commit"))
    assert page.evaluate("""(() => {
        window.__saved = document.getElementById('f').contentWindow;
        return window.__saved === document.getElementById('f').contentWindow;
    })()""") is True
    page.evaluate("document.getElementById('f').srcdoc = '<p>second</p>'")
    await page.wait_for("document.getElementById('f').contentDocument.body.textContent==='second'", timeout=1)
    assert page.evaluate("""(() => {
        const frame = document.getElementById('f');
        return [window.__saved === frame.contentWindow,
                window.__saved.document === frame.contentDocument,
                frame.contentDocument.body.textContent];
    })()""") == [True, True, "second"]


async def test_an_inserted_frame_answers_content_window_at_once(page):
    """A frame gets its window where it is inserted, not on the task after the turn.

    A browser has the initial about:blank window the moment the frame joins the document,
    and a page reads ``contentWindow`` right then -- an inline challenge bootstrap does,
    and a null there throws inside it.  Only the document the frame
    asks for arrives later.
    """
    (await page.set_content("""<body><script>
        const frame = document.createElement('iframe');
        frame.src = 'about:blank';
        document.body.appendChild(frame);
        window.__inline = [String(frame.contentWindow), document.readyState,
                           frame.contentDocument !== null,
                           frame.contentWindow.document !== null];
    </script></body>""", url="https://frames.test/", wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("__inline") == ['[object Window]', 'loading', True, True]
    assert page.evaluate("""(() => {
        const frame = document.querySelector('iframe');
        return [frame.contentWindow.location.href, typeof frame.contentDocument.body,
                typeof frame.contentWindow.document.readyState];
    })()""") == ['about:blank', 'object', 'string']


async def test_a_navigation_gives_the_page_a_new_window_and_keeps_the_context(page):
    (await page.set_content("<body>first</body>", url="https://nav.test/one", wait_until="commit"))
    page.evaluate("""(() => {
        window.__mark = 'first';
        window.name = 'kept';
        sessionStorage.setItem('k', 'v');
    })()""")
    (await page.set_content("<body>second</body>", url="https://nav.test/two", wait_until="commit"))
    assert page.evaluate("""(() => {
        return [window.__mark === undefined, window.name,
                sessionStorage.getItem('k'), location.href,
                window.top === window, window.parent === window,
                window.window === window, document.body.textContent];
    })()""") == [True, "kept", "v", "https://nav.test/two", True, True, True, "second"]


async def test_a_frames_top_and_parent_follow_their_pages_navigations(page):
    (await page.set_content('<body><iframe src="about:blank"></iframe></body>',
                     url="https://top.test/first", wait_until="commit"))
    child = next(iter(page._owned.frames))
    assert child.evaluate("[parent.location.href, top.location.href]") ==\
        ["https://top.test/first", "https://top.test/first"]
    (await page.set_content("<body></body>", url="https://top.test/second", wait_until="commit"))
    assert child.evaluate("[parent.location.href, top.location.href]") ==\
        ["https://top.test/second", "https://top.test/second"]


async def test_low_frequency_child_objects_are_created_on_first_read(page):
    (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
    screen = page.window.screen
    assert screen._values.cached_names == frozenset()
    assert page.evaluate("screen.orientation === screen.orientation") is True
    assert screen._values.cached_names == {"orientation"}

    navigator = page.window.navigator
    assert navigator._features.cached_names == frozenset()
    assert page.evaluate("navigator.locks === navigator.locks") is True
    assert navigator._features.cached_names == {"locks"}


async def test_a_lock_request_runs_its_callback(page):
    (await page.set_content("<body></body>", url="https://secure.test/", wait_until="commit"))
    page.evaluate("""window.__held = [];
        navigator.locks.request('res', lock => {
          window.__held.push(lock.name, lock.mode);
          return 'done';
        });""")
    # Web Locks dispatches the callback as a task; another evaluation is not a
    # guarantee that the task has run yet.
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__held") == ["res", "exclusive"]
    assert page.evaluate("typeof navigator.locks.query") == "function"


async def test_canvas_text_baseline_round_trips(page):
    assert page.evaluate("""
        (function () {
            const context = document.createElement('canvas').getContext('2d');
            const initial = context.textBaseline;
            context.textBaseline = 'middle';
            const set = context.textBaseline;
            context.textBaseline = 'not-a-baseline';
            const ignored = context.textBaseline;
            context.save();
            context.textBaseline = 'top';
            context.restore();
            return [initial, set, ignored, context.textBaseline];
        })()
    """) == ["alphabetic", "middle", "middle", "middle"]


async def test_webgl_reads_back_the_colour_it_cleared_to(page):
    assert page.evaluate("""(() => {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl');
        const empty = new Uint8Array(4);
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, empty);
        gl.clearColor(0.25, 0.5, 0.75, 1);
        gl.clear(gl.COLOR_BUFFER_BIT);
        const pixels = new Uint8Array(4);
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        return [Array.from(empty).join(','), Array.from(pixels).join(','),
                Array.from(gl.getParameter(gl.COLOR_CLEAR_VALUE)).join(','), gl.getError()];
    })()""") == ["0,0,0,0", "64,128,191,255", "0.25,0.5,0.75,1", 0]


async def test_webgl_color_mask_holds_back_the_channels_it_covers(page):
    assert page.evaluate("""(() => {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl');
        gl.colorMask(true, false, true, false);
        gl.clearColor(1, 1, 1, 1);
        gl.clear(gl.COLOR_BUFFER_BIT);
        const pixels = new Uint8Array(4);
        gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        return [Array.from(pixels).join(','),
                Array.from(gl.getParameter(gl.COLOR_WRITEMASK)).join(',')];
    })()""") == ["255,0,255,0", "true,false,true,false"]


async def test_webgl_draw_path_answers_without_an_error(page):
    assert page.evaluate("""(() => {
        const gl = document.createElement('canvas').getContext('webgl');
        const shader = gl.createShader(gl.VERTEX_SHADER);
        gl.shaderSource(shader, 'void main() {}');
        gl.compileShader(shader);
        const program = gl.createProgram();
        gl.attachShader(program, shader);
        gl.linkProgram(program);
        gl.useProgram(program);
        const buffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0]), gl.STATIC_DRAW);
        const attribute = gl.getAttribLocation(program, 'a');
        gl.enableVertexAttribArray(attribute);
        gl.vertexAttribPointer(attribute, 2, gl.FLOAT, false, 0, 0);
        gl.vertexAttrib2f(attribute, 1, 2);
        gl.uniform2f(gl.getUniformLocation(program, 'u'), 1, 2);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        gl.drawElements(gl.TRIANGLES, 3, gl.UNSIGNED_SHORT, 0);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
        return gl.getError();
    })()""") == 0


async def test_feature_policy_needs_a_feature_name(page):
    assert page.evaluate("""(() => {
        try { document.featurePolicy.allowsFeature(); return 'no throw'; }
        catch (error) { return error.name + ': ' + error.message; }
    })()""").startswith("TypeError: Failed to execute 'allowsFeature' on 'PermissionsPolicy'")


async def test_attribute_style_map_refuses_what_chrome_refuses(page):
    """The writable map validates names and values, the read-only view only names."""
    assert page.evaluate("""(() => {
        const element = document.createElement('div');
        const map = element.attributeStyleMap;
        const answers = [];
        for (const [label, call] of [
            ['set', () => map.set('nope-nope', 'x')],
            ['set camelCase', () => map.set('marginTop', '4px')],
            ['append', () => map.append('color', 'blue')],
            ['delete', () => map.delete('nope-nope')],
            ['get', () => map.get()],
            ['get invalid', () => map.get('nope-nope')],
            ['has invalid', () => map.has('nope-nope')],
            ['getAll invalid', () => map.getAll('nope-nope')],
            ['get missing', () => String(map.get('margin-top'))],
            ['has missing', () => map.has('margin-top')],
            ['set custom', () => { map.set('--probe', '1'); return element.getAttribute('style'); }],
            ['get custom', () => String(map.get('--probe'))],
        ]) {
            try { answers.push(label + ': ' + call()); }
            catch (error) { answers.push(label + ': ' + error.message); }
        }
        return answers;
    })()""") == [
        "set: Failed to execute 'set' on 'StylePropertyMap': Invalid propertyName: nope-nope",
        "set camelCase: Failed to execute 'set' on 'StylePropertyMap':"
        " Invalid propertyName: marginTop",
        "append: Failed to execute 'append' on 'StylePropertyMap':"
        " Property does not support multiple values",
        "delete: Failed to execute 'delete' on 'StylePropertyMap': Invalid property name: nope-nope",
        "get: Failed to execute 'get' on 'StylePropertyMapReadOnly': 1 argument required,"
        " but only 0 present.",
        "get invalid: Failed to execute 'get' on 'StylePropertyMapReadOnly':"
        " Invalid propertyName: nope-nope",
        "has invalid: Failed to execute 'has' on 'StylePropertyMapReadOnly':"
        " Invalid propertyName: nope-nope",
        "getAll invalid: Failed to execute 'getAll' on 'StylePropertyMapReadOnly':"
        " Invalid propertyName: nope-nope",
        "get missing: undefined",
        "has missing: false",
        "set custom: --probe: 1;",
        "get custom: 1",
    ]


async def test_a_transferred_message_port_arrives_in_the_events_ports(page):
    """A transfer moves the port: the sender's reference is neutered, the twin stays
    entangled with the port the receiving side finds in ``event.ports``.
    """
    page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        window.__neutered = false;
        channel.port2.onmessage = () => { window.__neutered = true; };
        onmessage = event => {
            window.__seen.push(event.data, event.ports.length,
                               event.ports[0] instanceof MessagePort);
            const moved = event.ports[0];
            moved.onmessage = reply => window.__seen.push(reply.data);
            moved.start();
            channel.port1.postMessage('from-port1');
        };
        postMessage('init', '*', [channel.port2]);
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == ["init", 1, True, "from-port1"]
    assert page.evaluate("window.__neutered") is False


async def test_the_exception_prototype_behaves_like_chromium(page):
    """``DOMException``'s surface, as Chromium's own assertions state it.

    Every assertion below is a case in
    ``WebIDL/ecmascript-binding/es-exceptions/DOMException-custom-bindings.any.js`` or
    ``DOMException-is-error.any.js`` -- with one exception: a script-made ``DOMException``
    carries no ``stack``, which Chromium records as an expected failure.
    """
    assert page.evaluate(
        "a = 1; try { String(DOMException.prototype) } catch (error) { a = 2 }; a") == 2
    answer = page.evaluate("""(() => {
        const e = new DOMException("message", "name");
        const proto = DOMException.prototype;
        const brand = (name) => {
            const getter = Object.getOwnPropertyDescriptor(proto, name).get;
            try { getter.call({}); return "no-throw"; } catch (error) { return error.constructor.name; }
        };
        const applies = (call) => {
            try { return call(); } catch (error) { return error.constructor.name; }
        };
        return [
            Object.getPrototypeOf(proto) === Error.prototype,
            Object.getPrototypeOf(DOMException) === Function.prototype,
            e.hasOwnProperty("name"), e.hasOwnProperty("message"), e.hasOwnProperty("code"),
            proto.hasOwnProperty("toString"),
            typeof e.toString,
            String(Object.getOwnPropertyDescriptor(proto, "message").get.call(e)),
            brand("name"), brand("code"),
            applies(() => proto.toString()),
            Object.prototype.toString.call(e),
            e.toString(),
            typeof e.stack,
            [typeof new Error("x").stack, typeof new TypeError("x").stack],
            Error.isError(e), Error.isError(new Error("x")), Error.isError({}),
        ];
    })()""")
    assert answer == [True, True, False, False, False, False, "function", "message",
                      "TypeError", "TypeError", "TypeError", "[object DOMException]",
                      "name: message", "undefined", ["string", "string"],
                      True, True, False]


async def test_a_plain_object_is_cloned_without_a_word_of_complaint(page):
    """Most of what a message carries is plain data, which is no structured kind at all.

    The realm answers ``null`` for it and the clone takes the plain snapshot; that
    answer is not a failure, so it leaves nothing in the log.  It used to be read as
    one, and every plain object a page posted logged an error that was not there.
    """
    messages, token = [], logger.add(
        lambda message: messages.append(str(message).strip()), format="{message}")
    try:
        page.evaluate("""(() => {
            const channel = new MessageChannel();
            window.__seen = null;
            channel.port2.onmessage = event => { window.__seen = event.data; };
            channel.port1.postMessage({plain: {deep: [1, 'two', false]}, more: true});
        })()""")
        (await page.run_until_idle(timeout=1))
    finally:
        logger.remove(token)
    assert page.evaluate("window.__seen") == {"plain": {"deep": [1, "two", False]}, "more": True}
    assert not any("could not be read" in line for line in messages), messages


async def test_a_posted_message_is_a_copy_taken_when_it_is_posted(page):
    """A port carries a snapshot, as a browser does.

    What the sender does to its object after the call is not seen on the other side,
    and binary data is copied rather than shared.  A reference passed along would
    instead let one realm read the other's object -- and outlive the realm it came
    from, which is how a port that outlived its worker used to hand over something
    no isolate owned any more.
    """
    page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        channel.port2.onmessage = event => {
            const data = event.data;
            window.__seen.push(data.nested.n, data.list.join(','),
                               new Uint8Array(data.buffer)[0]);
        };
        const payload = {nested: {n: 1}, list: [1, 2],
                         buffer: new Uint8Array([7, 8]).buffer};
        channel.port1.postMessage(payload);
        payload.nested.n = 99;
        payload.list.push(3);
        new Uint8Array(payload.buffer)[0] = 42;
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [1, "1,2", 7]


async def test_a_port_that_outlives_its_worker_still_delivers(monkeypatch):
    """A message a worker posted through a port arrives after that worker is gone.

    The page holds its own queue shut for the moment the worker needs to post and
    close, so the message is processed when the realm that posted it no longer
    exists.  What arrives is the page's own copy of the data; a reference into the
    worker's realm would leave this message undeliverable, which is what it used to.
    """
    worker_source = ("onmessage = function (event) {"
                     "  event.ports[0].postMessage({n: 7, list: [1, 2]});"
                     "  self.close();"
                     "};")
    body = ("<!doctype html><body><script>"
            "var channel = new MessageChannel();"
            "window.__seen = [];"
            "window.__error = '';"
            "channel.port1.onmessage = function (event) {"
            "  try { window.__seen.push(event.data.n, event.data.list.join(',')); }"
            "  catch (error) { window.__error = error.name + ': ' + error.message; }"
            "};"
            "var worker = new Worker(URL.createObjectURL("
            "new Blob([__SOURCE__], {type: 'text/javascript'})));"
            "</script></body>")
    body = body.replace("__SOURCE__", json.dumps(worker_source).replace("</", "<\\/"))

    def loader(url, request):
        return {"body": body, "headers": {"content-type": "text/html"}}

    import threading
    from pyv8c.web.workers.runtime import WorkerRuntime
    ready, closed = threading.Event(), threading.Event()
    original_cleanup = WorkerRuntime._cleanup_owner
    monkeypatch.setattr(WorkerRuntime, '_started', lambda runtime: ready.set())
    def cleanup(runtime):
        original_cleanup(runtime)
        assert runtime._realm is None
        closed.set()
    monkeypatch.setattr(WorkerRuntime, '_cleanup_owner', cleanup)
    browser = (await Browser(stub_logging="off", real_network=False, resource_loader=loader).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto("http://port.test/"))
        assert (await asyncio.to_thread(ready.wait, 2))
        def post_while_parent_queue_is_blocked():
            page.evaluate("worker.postMessage('go', [channel.port2])")
            assert closed.wait(2), "worker realm must close before parent delivery"
        page.executor.call(post_while_parent_queue_is_blocked)
        (await page.wait_for("window.__seen.length===2 || window.__error!==''", timeout=2))
        assert page.evaluate("window.__error") == ""
        assert page.evaluate("window.__seen") == [7, "1,2"]
    finally:
        (await browser.close())


async def test_post_message_carries_binary_data(page):
    """An ``ArrayBuffer`` a page posts arrives as one, with its bytes.

    A clone that walks enumerable properties alone finds nothing on a buffer, so it
    used to arrive as an empty object; a browser hands the other realm the bytes.
    """
    page.evaluate("""(() => {
        window.__seen = [];
        onmessage = event => {
            const data = event.data;
            window.__seen.push(data instanceof ArrayBuffer, data.byteLength,
                               Array.from(new Uint8Array(data)).join(','));
        };
        postMessage(new Uint8Array([1, 2, 3, 4]).buffer, '*');
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [True, 4, "1,2,3,4"]


async def test_a_transfer_list_refuses_what_it_cannot_move(page):
    assert page.evaluate("""(() => {
        const answers = [];
        const channel = new MessageChannel();
        for (const call of [() => postMessage('x', '*', [{}]),
                            () => { postMessage('a', '*', [channel.port2]);
                                    postMessage('b', '*', [channel.port2]); }]) {
            try { call(); answers.push('no throw'); }
            catch (error) { answers.push(error.name + ': ' + error.message); }
        }
        return answers;
    })()""") == [
        "DataCloneError: Failed to execute 'postMessage' on 'Window':"
        " Value at index 0 does not have a transferable type.",
        "DataCloneError: Failed to execute 'postMessage' on 'Window':"
        " Port at index 0 is already neutered.",
    ]


async def test_a_transfer_list_hands_over_an_arraybuffer(page):
    """A transferred buffer is handed over rather than copied: the realm that gave it
    up has nothing left of it, and the other end reads a buffer with those bytes."""
    assert page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        channel.port2.onmessage = event => {
            const data = event.data;
            window.__seen.push(data instanceof ArrayBuffer, data.byteLength,
                               Array.from(new Uint8Array(data)).join(','));
        };
        const buffer = new Uint8Array([1, 2, 3, 4]).buffer;
        channel.port1.postMessage(buffer, [buffer]);
        return buffer.byteLength;
    })()""") == 0
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [True, 4, "1,2,3,4"]


async def test_a_posted_view_arrives_as_the_same_kind_of_view(page):
    """What crosses is the kind of value that was posted, not only its bytes.

    A browser hands the other realm the same view over its own copy of the bytes, and an
    offset view keeps looking through the same buffer it looked through: two bytes seen
    from offset 1 of a four-byte buffer arrive that way, not as a two-byte buffer.
    """
    page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        const describe = (data) => [
            Object.prototype.toString.call(data),
            data.byteOffset === undefined ? -1 : data.byteOffset,
            data.byteLength === undefined ? -1 : data.byteLength,
            data.buffer === undefined ? -1 : data.buffer.byteLength,
        ];
        channel.port2.onmessage = event => {
            const data = event.data;
            window.__seen.push(...describe(data.words), ...describe(data.half),
                               ...describe(data.view), ...describe(data.window),
                               data.list.length,
                               ...describe(data.list[0]));
        };
        const buffer = new Uint8Array([1, 2, 3, 4]).buffer;
        channel.port1.postMessage({words: new Int32Array([300, -2]),
                                   half: new Float64Array([1.5]),
                                   view: new Uint8Array(buffer, 1, 2),
                                   window: new DataView(new Uint8Array([1, 2, 3]).buffer),
                                   list: [new Uint8Array([5, 6])]});
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [
        "[object Int32Array]", 0, 8, 8,
        "[object Float64Array]", 0, 8, 8,
        "[object Uint8Array]", 1, 2, 4,
        "[object DataView]", 0, 3, 3,
        1,
        "[object Uint8Array]", 0, 2, 2,
    ]


async def test_a_posted_map_set_date_and_regexp_arrive_as_themselves(page):
    """A browser's clone keeps the kind of value it was given.

    A ``Map`` arrives a ``Map`` with its entries, a ``Set`` a ``Set``, a ``Date`` the
    moment it stood for, a ``RegExp`` its pattern and flags -- and what is nested inside
    them is built the same way, so a ``Set`` inside a ``Map`` is a ``Set`` too.  None of
    these used to cross at all: the message failed with a ``DataCloneError``.
    """
    page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        const tag = item => Object.prototype.toString.call(item);
        channel.port2.onmessage = event => {
            const data = event.data;
            window.__seen.push(tag(data.map), data.map instanceof Map, data.map.get('a'),
                               data.map.get('b').n, data.map.size,
                               tag(data.set), data.set.has(1), data.set.size,
                               tag(data.date), data.date.getTime(),
                               tag(data.re), data.re.source, data.re.flags,
                               tag(data.nested.get('inner')), data.nested.get('inner').has('x'));
        };
        channel.port1.postMessage({
            map: new Map([['a', 1], ['b', {n: 2}]]),
            set: new Set([1, 2]),
            date: new Date(1700000000000),
            re: /ab+c/gi,
            nested: new Map([['inner', new Set(['x'])]]),
        });
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [
        "[object Map]", True, 1, 2, 2,
        "[object Set]", True, 2,
        "[object Date]", 1700000000000,
        "[object RegExp]", "ab+c", "gi",
        "[object Set]", True,
    ]


async def test_views_of_one_buffer_share_it_after_crossing(page):
    """One buffer is one buffer on the other side.

    A browser clones a buffer once and builds every view of it over that clone, so a
    view of it, a second view of it and the buffer itself all answer with the same
    object -- even one nested inside a ``Map`` -- while another buffer that happens to
    hold the same bytes stays a buffer of its own.
    """
    page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        const buffer = new Uint8Array([1, 2, 3, 4]).buffer;
        const other = new Uint8Array([1, 2, 3, 4]).buffer;
        channel.port2.onmessage = event => {
            const data = event.data;
            window.__seen.push(data.view.buffer === data.bare,
                               data.again.buffer === data.bare,
                               data.nested.get('inner').buffer === data.bare,
                               data.separate.buffer === data.bare,
                               data.view.byteOffset, data.view.byteLength);
        };
        channel.port1.postMessage({bare: buffer,
                                   view: new Uint8Array(buffer, 1, 2),
                                   again: new Uint8Array(buffer),
                                   separate: new Uint8Array(other),
                                   nested: new Map([['inner', new DataView(buffer)]])});
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [True, True, True, False, 1, 2]


async def test_a_posted_error_arrives_as_the_same_kind_of_error(page):
    """An error crosses as the error it was: the same kind, the same name, the same
    message.  A ``DOMException`` keeps the name its legacy ``code`` follows from, and
    the ports a message carries are a frozen array, as a browser hands them over."""
    page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        const one = item => [Object.prototype.toString.call(item), item instanceof Error,
                             item.name, item.message, typeof item.stack];
        channel.port2.onmessage = event => {
            const data = event.data;
            window.__seen.push(...one(data.error), ...one(data.type_error), ...one(data.dom),
                               data.dom.code, Object.isFrozen(event.ports));
        };
        channel.port1.postMessage({error: new Error('boom'),
                                   type_error: new TypeError('bad type'),
                                   dom: new DOMException('nope', 'NotFoundError')});
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [
        "[object Error]", True, "Error", "boom", "string",
        "[object Error]", True, "TypeError", "bad type", "string",
        "[object DOMException]", True, "NotFoundError", "nope", "undefined",
        8, True,
    ]


async def test_what_cannot_be_cloned_says_which_value_it_was(page):
    """Chrome names the value it refuses: a weak map or a promise by its kind, and a
    function by its own source.  The error the host raises for it carries a stack, as a
    browser's own ``DOMException`` does -- unlike one a page makes with ``new``."""
    assert page.evaluate("""(() => {
        const channel = new MessageChannel();
        const answers = [];
        for (const value of [new WeakMap(), Promise.resolve(1), () => 1]) {
            try { channel.port1.postMessage(value); answers.push('no throw'); }
            catch (error) { answers.push(error.message, typeof error.stack); }
        }
        return answers;
    })()""") == ["#<WeakMap> could not be cloned.", "string",
                "#<Promise> could not be cloned.", "string",
                "() => 1 could not be cloned.", "string"]


async def test_a_posted_file_arrives_as_a_file(page):
    """A ``File`` crosses as the file it was, not as the blob it is made of: its bytes,
    and the name and timestamp that make it more than a blob.  A blob beside it stays a
    blob, and one nested inside a ``Map`` is a file where it arrives."""
    page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        const one = item => [Object.prototype.toString.call(item), item instanceof File,
                             item instanceof Blob,
                             item.name === undefined ? '(no name)' : item.name,
                             item.type, item.size,
                             item.lastModified === undefined ? -1 : item.lastModified];
        channel.port2.onmessage = event => {
            const data = event.data;
            window.__seen.push(...one(data.file), ...one(data.blob),
                               ...one(data.nested.get('inner')));
        };
        const stamp = 1700000000000;
        channel.port1.postMessage({
            file: new File(['hello'], 'a.txt', {type: 'text/plain', lastModified: stamp}),
            blob: new Blob(['hi'], {type: 'text/plain'}),
            nested: new Map([['inner', new File(['xyz'], 'b.bin', {lastModified: stamp})]]),
        });
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [
        "[object File]", True, True, "a.txt", "text/plain", 5, 1700000000000,
        "[object Blob]", False, True, "(no name)", "text/plain", 2, -1,
        "[object File]", True, True, "b.bin", "", 3, 1700000000000,
    ]


async def test_a_posted_view_carries_the_bytes_it_holds(page):
    """A view is the bytes it holds, not the numbers it shows.

    An ``Int32Array`` of ``[300, -2]`` is eight bytes, and a ``DataView`` holds whatever
    it looks at; reading a view element by element instead gave two truncated bytes for
    the first and nothing at all for the second.  The bytes are read back through a view
    built on the receiving side, so how the data arrived is not what is asserted.
    """
    page.evaluate("""(() => {
        const channel = new MessageChannel();
        window.__seen = [];
        const bytesOf = value => (value instanceof ArrayBuffer ? value : value.buffer);
        channel.port2.onmessage = event => {
            const data = event.data;
            window.__seen.push(new Int32Array(bytesOf(data.words))[0],
                               new Int32Array(bytesOf(data.words))[1],
                               new Float64Array(bytesOf(data.half))[0],
                               Array.from(new Uint8Array(bytesOf(data.window))).join(','));
        };
        channel.port1.postMessage({words: new Int32Array([300, -2]),
                                   half: new Float64Array([1.5]),
                                   window: new DataView(new Uint8Array([1, 2, 3]).buffer)});
    })()""")
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("window.__seen") == [300, -2, 1.5, "1,2,3"]


async def test_a_transfer_list_refuses_a_buffer_that_is_already_detached(page):
    assert page.evaluate("""(() => {
        const channel = new MessageChannel();
        const buffer = new Uint8Array([1, 2]).buffer;
        channel.port1.postMessage(buffer, [buffer]);
        try { channel.port1.postMessage('second', [buffer]); return 'no throw'; }
        catch (error) { return error.name + ': ' + error.message; }
    })()""") == ("DataCloneError: Failed to execute 'postMessage' on 'MessagePort':"
                 " ArrayBuffer at index 0 is already detached.")


async def test_is_type_supported_answers_the_codecs_the_build_carries(page):
    assert page.evaluate("""(() => [
        MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
        MediaSource.isTypeSupported('video/mp4; codecs="mp4v.20.8"'),
        MediaSource.isTypeSupported('video/mp4'),
        MediaSource.isTypeSupported('audio/mpeg'),
        MediaSource.isTypeSupported('nonsense'),
        MediaSource.isTypeSupported(''),
    ])()""") == [True, False, False, True, False, False]


async def test_is_type_supported_needs_a_type(page):
    assert page.evaluate("""(() => {
        try { MediaSource.isTypeSupported(); return 'no throw'; }
        catch (error) { return error.name + ': ' + error.message; }
    })()""").startswith("TypeError: Failed to execute 'isTypeSupported' on 'MediaSource'")


async def test_rtp_receiver_capabilities_are_the_reference_builds(page):
    assert page.evaluate("""(() => {
        const audio = RTCRtpReceiver.getCapabilities('audio');
        const video = RTCRtpReceiver.getCapabilities('video');
        return [audio.codecs.length, video.codecs.length,
                audio.headerExtensions.length, video.headerExtensions.length,
                audio.codecs[0].mimeType, video.codecs[0].mimeType,
                Object.keys(audio.codecs[0]).join(','),
                String(RTCRtpReceiver.getCapabilities('bogus'))];
    })()""") == [8, 23, 4, 11, "audio/opus", "video/VP8",
                 "channels,clockRate,mimeType,sdpFmtpLine", "null"]


async def test_a_dom_matrix_parses_serializes_and_transforms(page):
    assert page.evaluate("""(() => {
        const ids = m => m.toString();
        const identity = new DOMMatrix();
        return [Object.prototype.toString.call(identity), ids(identity),
                identity.is2D, identity.isIdentity,
                ids(new DOMMatrix([1, 2, 3, 4, 5, 6])),
                ids(new DOMMatrix('matrix(2, 0, 0, 2, 10, 20)')),
                ids(new DOMMatrix([1, 2, 3, 4, 5, 6]).translate(10, 20)),
                ids(new DOMMatrix().rotate(90)), ids(new DOMMatrix().rotate(45)),
                ids(new DOMMatrix([2, 0, 0, 4, 6, 8]).inverse()),
                ids(new DOMMatrix().translate(0.1, 0.2)),
                Array.from(new DOMMatrix([1, 2, 3, 4, 5, 6]).toFloat32Array()).join(',')];
    })()""") == [
        "[object DOMMatrix]", "matrix(1, 0, 0, 1, 0, 0)", True, True,
        "matrix(1, 2, 3, 4, 5, 6)", "matrix(2, 0, 0, 2, 10, 20)",
        "matrix(1, 2, 3, 4, 75, 106)", "matrix(0, 1, -1, 0, 0, 0)",
        "matrix(0.7071067811865476, 0.7071067811865476,"
        " -0.7071067811865476, 0.7071067811865476, 0, 0)",
        "matrix(0.5, 0, 0, 0.25, -3, -2)",
        "matrix(1, 0, 0, 1, 0.10000000149011612, 0.20000000298023224)",
        "1,2,0,0,3,4,0,0,0,0,1,0,5,6,0,1",
    ]


async def test_a_dom_matrix_keeps_its_2d_flag_and_fills_a_point(page):
    assert page.evaluate("""(() => {
        const sticky = new DOMMatrix();
        sticky.multiplySelf(new DOMMatrix('matrix3d(1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1)'));
        const written = new DOMMatrix();
        written.a = 5;
        written.m13 = 9;
        const point = new DOMMatrix([2, 0, 0, 3, 10, 20]).transformPoint({x: 1, y: 2});
        return [sticky.is2D, sticky.isIdentity, written.is2D,
                Object.prototype.toString.call(point),
                [point.x, point.y, point.z, point.w].join(','),
                [new DOMPoint().x, new DOMPoint().z, new DOMPoint().w].join(','),
                JSON.stringify(new DOMPoint(1, 2, 3, 4).toJSON()),
                String(new DOMPoint(1, 2).matrixTransform(new DOMMatrix([2, 0, 0, 3, 10, 20])))];
    })()""") == [False, True, False, "[object DOMPoint]", "12,26,0,1", "0,0,1",
                 '{"x":1,"y":2,"z":3,"w":4}', "[object DOMPoint]"]


async def test_a_dom_parser_builds_a_document_that_belongs_to_no_window(page):
    assert page.evaluate("""(() => {
        const doc = new DOMParser().parseFromString(
            '<!DOCTYPE html><body><p id=x>hi</p></body>', 'text/html');
        return [Object.prototype.toString.call(doc), doc.contentType, doc.compatMode,
                doc.URL === location.href, doc.defaultView === null,
                doc.getElementById('x').textContent, doc.doctype.name,
                doc instanceof HTMLDocument, getComputedStyle(doc.body).display,
                doc.createElement('p').constructor.name];
    })()""") == ["[object HTMLDocument]", "text/html", "CSS1Compat", True, True, "hi",
                 "html", True, "", "HTMLParagraphElement"]


async def test_a_dom_parser_refuses_a_type_outside_the_enum(page):
    assert page.evaluate("""(() => {
        const answers = [];
        for (const call of [() => new DOMParser().parseFromString('<a/>', 'text/plain'),
                            () => new DOMParser().parseFromString('<a/>')]) {
            try { call(); answers.push('no throw'); }
            catch (error) { answers.push(error.name + ': ' + error.message); }
        }
        return answers;
    })()""") == [
        "TypeError: Failed to execute 'parseFromString' on 'DOMParser': The provided"
        " value 'text/plain' is not a valid enum value of type SupportedType.",
        "TypeError: Failed to execute 'parseFromString' on 'DOMParser': 2 arguments"
        " required, but only 1 present.",
    ]


async def test_an_xml_parse_keeps_namespaces_and_reports_its_own_error(page):
    assert page.evaluate("""(() => {
        const good = new DOMParser().parseFromString(
            '<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>', 'image/svg+xml');
        const broken = new DOMParser().parseFromString('<root><x></root>', 'text/xml');
        return [Object.prototype.toString.call(good), good.contentType,
                String(good.documentElement.namespaceURI),
                Object.prototype.toString.call(good.documentElement.firstElementChild),
                broken.documentElement.localName,
                broken.getElementsByTagName('parsererror').length];
    })()""") == ["[object XMLDocument]", "image/svg+xml", "http://www.w3.org/2000/svg",
                 "[object SVGCircleElement]", "root", 1]


async def test_stylesheet_cascade_and_layout_still_apply(page):
    """The cascade reads the sheet once per entry point, not once per element."""
    assert page.evaluate("getComputedStyle(document.querySelector('.item')).color") == "rgb(255, 0, 0)"
    # Inherited through the parent chain.
    assert page.evaluate("getComputedStyle(document.getElementById('sized')).fontSize") == "20px"
    # An inline declaration outranks the sheet.
    assert page.evaluate("getComputedStyle(document.querySelector('.item[style]')).color") == "rgb(0, 0, 255)"
    assert page.evaluate("""
        (function () {
            const sized = document.getElementById('sized');
            return [sized.offsetWidth, sized.offsetHeight,
                    document.documentElement.clientWidth - document.body.offsetWidth];
        })()
    """) == [40, 10, 16]


async def test_url_attributes_resolve_and_write_the_attribute(page):
    """The URL attributes read back absolute and keep the attribute written."""
    assert page.evaluate("""
        (function () {
            const build = (tag, name, value) => {
                const node = document.createElement(tag);
                node.setAttribute(name, value);
                return node;
            };
            return [
                build('img', 'src', 'pics/a.png').src,
                build('script', 'src', '/js/app.js').src,
                build('link', 'href', '../style.css').href,
                build('input', 'src', 'icon.png').src,
                build('iframe', 'src', 'frame.html').src,
                build('img', 'src', 'https://cdn.test/x.png').src,
                build('img', 'src', '').src,
                build('img', 'src', 'pics/a.png').getAttribute('src'),
                build('link', 'href', '../style.css').getAttribute('href'),
            ];
        })()
    """) == [
        "https://dom.test/dir/pics/a.png",
        "https://dom.test/js/app.js",
        "https://dom.test/style.css",
        "https://dom.test/dir/icon.png",
        "https://dom.test/dir/frame.html",
        "https://cdn.test/x.png",
        "",
        "pics/a.png",
        "../style.css",
    ]
    # Assigning through the IDL member writes the attribute, not the resolved URL.
    assert page.evaluate("""
        (function () {
            const link = document.createElement('link');
            link.href = 'now.css';
            const input = document.createElement('input');
            input.src = 'pic.png';
            const anchor = document.createElement('a');
            anchor.href = '/deep/page';
            return [link.getAttribute('href'), link.href,
                    input.getAttribute('src'), input.src,
                    anchor.getAttribute('href'), anchor.href];
        })()
    """) == ["now.css", "https://dom.test/dir/now.css",
             "pic.png", "https://dom.test/dir/pic.png",
             "/deep/page", "https://dom.test/deep/page"]


STYLE_CONTENT = """
<html><head><style>#target { color: red }</style></head>
<body><div id="target">x</div></body></html>
"""


@pytest.fixture
async def style_page():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content(STYLE_CONTENT, url="https://dom.test/dir/page", wait_until="commit"))
    (await page.run_until_idle(timeout=1))
    try:
        yield page
    finally:
        (await browser.close())


async def test_stylesheet_cache_follows_runtime_changes(style_page):
    """The sheet is parsed once per change, not once per measurement."""
    from pyv8c.web.css.cascade import document_rules

    document = style_page.document

    def color():
        return style_page.evaluate("getComputedStyle(document.getElementById('target')).color")

    assert color() == "rgb(255, 0, 0)"
    cached = document_rules(document)
    assert document_rules(document) is cached, "an unchanged document reuses its sheet"

    # A style element added at runtime.
    style_page.evaluate("""
        (function () {
            const style = document.createElement('style');
            style.textContent = '#target { color: green }';
            document.head.appendChild(style);
            window.__style = style;
        })()
    """)
    assert document_rules(document) is not cached
    assert color() == "rgb(0, 128, 0)"

    # Editing the rules the element holds, through the tree and through a text node.
    style_page.evaluate("window.__style.textContent = '#target { color: purple }'")
    assert color() == "rgb(128, 0, 128)"
    style_page.evaluate("window.__style.firstChild.nodeValue = '#target { color: teal }'")
    assert color() == "rgb(0, 128, 128)"

    # Removing it brings the original rule back.
    style_page.evaluate("window.__style.parentNode.removeChild(window.__style)")
    assert color() == "rgb(255, 0, 0)"

    # A subtree carrying a style element that is only inserted afterwards.
    style_page.evaluate("""
        (function () {
            const holder = document.createElement('div');
            holder.innerHTML = '<style>#target { color: blue }</style>';
            document.head.appendChild(holder);
        })()
    """)
    assert color() == "rgb(0, 0, 255)"
