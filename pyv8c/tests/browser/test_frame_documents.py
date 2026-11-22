"""Frames whose document the element itself carries: ``srcdoc``, ``data:`` and ``blob:``.

Each one *is* its payload, so none of them is fetched from the network: Chromium decodes all
three in place.  A frame that had to be fetched instead would never finish its load, which is
how a ``data:`` frame used to leave the page waiting (measured against Chrome).

The ``data:`` and ``blob:`` frames report from a script in their own document.  The ``srcdoc``
one carries markup only and is read through ``contentDocument``; a ``srcdoc`` document *with* a
script is covered by ``test_a_script_inserted_frame_runs_its_own_document``, which is the case
that used to wedge both pages.
"""

import asyncio
import json
import threading
import time

import pytest

from pyv8c.async_api import Browser
from pyv8c.platform.execution import WindowExecutor


async def test_javascript_iframe_uses_its_inherited_document_without_a_resource_request():
    requests = []

    def intercept(url, request):
        requests.append((url, request.get('destination')))
        return None

    browser = (await Browser(real_network=False, resource_interceptor=intercept, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        before = page.evaluate("""(() => {
          const div = document.createElement('div');
          div.innerHTML = '<iframe src="javascript:;"></iframe>';
          window.frame = div.children[0];
          window.frameEvents = [];
          frame.addEventListener('load', () => frameEvents.push('load'));
          frame.addEventListener('error', () => frameEvents.push('error'));
          const detached = [frame.isConnected, frame.contentWindow === null];
          document.body.appendChild(div);
          return detached;
        })()""")
        assert before == [False, True]
        (await page.run_until_idle(timeout=3))
        assert page.evaluate("""({url: frame.contentDocument.URL,
          href: frame.contentWindow.location.href,
          origin: frame.contentWindow.origin,
          events: frameEvents})""") == {
            'url': 'about:blank', 'href': 'about:blank',
            'origin': 'https://example.test', 'events': ['load'],
        }
        assert requests == []
    finally:
        (await browser.close())


async def test_javascript_iframe_string_result_replaces_inherited_document():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe src="javascript:&#39;&lt;p&gt;ok&lt;/p&gt;&#39;"></iframe></body>',
                         url='https://example.test/', wait_until="commit"))
        (await page.run_until_idle(timeout=3))
        assert page.evaluate("""(() => {
          const frame = document.querySelector('iframe');
          return [frame.contentDocument.URL, frame.contentDocument.body.textContent,
                  frame.contentWindow.location.href];
        })()""") == ['about:blank', 'ok', 'about:blank']
    finally:
        (await browser.close())


async def test_connected_iframe_javascript_src_does_not_duplicate_load():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        page.evaluate("""(() => {
          window.events = [];
          const frame = document.createElement('iframe');
          window.frame = frame;
          frame.addEventListener('load', () => events.push('load'));
          document.body.appendChild(frame);
        })()""")
        (await page.run_until_idle(timeout=3))
        assert page.evaluate('events') == ['load']
        page.evaluate("frame.src = 'javascript:;' ")
        (await page.run_until_idle(timeout=3))
        assert page.evaluate("[events.length, frame.contentDocument.URL]") == [1, 'about:blank']
    finally:
        (await browser.close())


async def test_window_document_getter_accepts_same_origin_frame_receiver():
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        assert page.evaluate("""
            (() => {
              const getter = Object.getOwnPropertyDescriptor(window, 'document').get;
              const frame = document.createElement('iframe');
              document.body.appendChild(frame);
              let illegal;
              try { getter.call({}); } catch (error) {
                illegal = [error.name, error.message];
              }
              return [getter.call(frame.contentWindow) === frame.contentDocument,
                      getter.call(window) === document,
                      getter.call(null) === document,
                      illegal];
            })()
        """) == [True, True, True, ['TypeError', 'Illegal invocation']]
    finally:
        (await browser.close())


async def test_event_target_methods_accept_same_origin_frame_document():
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        assert page.evaluate("""
            (() => {
              const frame = document.createElement('iframe');
              document.body.appendChild(frame);
              const target = frame.contentDocument;
              let calls = 0;
              const listener = () => calls++;
              EventTarget.prototype.addEventListener.call(target, 'sample', listener);
              EventTarget.prototype.dispatchEvent.call(target, new Event('sample'));
              EventTarget.prototype.removeEventListener.call(target, 'sample', listener);
              EventTarget.prototype.dispatchEvent.call(target, new Event('sample'));
              return calls;
            })()
        """) == 1
    finally:
        (await browser.close())


async def test_node_owner_document_getter_accepts_same_origin_frame_node():
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        assert page.evaluate("""
            (() => {
              const frame = document.createElement('iframe');
              document.body.appendChild(frame);
              const getter = Object.getOwnPropertyDescriptor(
                  Node.prototype, 'ownerDocument').get;
              return getter.call(frame.contentDocument.body) === frame.contentDocument;
            })()
        """) is True
    finally:
        (await browser.close())


async def test_node_namespace_lookup_accepts_same_origin_frame_node():
    browser = (await Browser(stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body></body>', url='https://example.test/', wait_until="commit"))
        assert page.evaluate("""
            (() => {
              const frame = document.createElement('iframe');
              document.body.appendChild(frame);
              return Node.prototype.lookupNamespaceURI.call(
                  frame.contentDocument.body, null);
            })()
        """) == 'http://www.w3.org/1999/xhtml'
    finally:
        (await browser.close())


async def test_restricted_stop_cancels_srcdoc_binding_before_owner_cleanup(monkeypatch):
    reached_bind, release_bind, stopped = threading.Event(), threading.Event(), threading.Event()
    original_bind = WindowExecutor.bind_agent
    def pause_frame_rebind(self, agent, context):
        page = self.tasks._host._page
        if page.parent_page is not None and self.agent is not None and not reached_bind.is_set():
            reached_bind.set()
            assert release_bind.wait(5)
        return original_bind(self, agent, context)
    monkeypatch.setattr(WindowExecutor, "bind_agent", pause_frame_rebind)
    async with Browser(cache=False, real_network=False, stub_logging="off") as runtime:
        page = (await runtime.new_page().set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        group = page.execution_group
        errors = []
        def control():
            try:
                assert reached_bind.wait(5)
                group.request_stop()
                stopped.set()
            except BaseException as error:
                errors.append(error)
            finally:
                release_bind.set()
        controller = threading.Thread(target=control)
        controller.start()
        try:
            page.evaluate("""const frame=document.createElement('iframe');
                frame.srcdoc='<body>x</body>';document.body.appendChild(frame)""")
            assert await asyncio.to_thread(stopped.wait, 5)
            child = next(iter(page._owned.frames))
            assert group.stopping
            await runtime.close()
            assert page.closed and child.closed and group.closed and not group.members
            assert not page.tasks.drain_task_errors() and not child.tasks.drain_task_errors()
        finally:
            release_bind.set()
            await asyncio.to_thread(controller.join, 5)
        assert not controller.is_alive() and not errors


async def test_initial_frame_window_does_not_wait_for_its_network_document():
    """The insertion can return while the child document is blocked in a loader.

    The corpus guards the initial window's shape; this guards thread ownership,
    an in-flight cross-origin navigation and access checks on the saved window.
    """
    release = threading.Event()
    requested = threading.Event()

    def loader(url, request):
        requested.set()
        assert release.wait(5), "the inserting script waited for the frame's request"
        return {"body": "<body>remote</body>", "headers": {"content-type": "text/html"}}

    browser = (await Browser(real_network=False, resource_loader=loader, threaded_loaders=True,
                      stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("""<body><script>
            const frame = document.createElement('iframe');
            frame.src = 'https://other.test/child';
            document.body.appendChild(frame);
            window.__savedWindow = frame.contentWindow;
            window.__initial = [__savedWindow.document.URL,
                                __savedWindow.document.readyState,
                                frame.contentDocument === __savedWindow.document];
        </script></body>""", url="https://parent.test/", wait_until="commit"))
        assert (await asyncio.to_thread(requested.wait, 1))
        assert page.evaluate("__initial") == ["about:blank", "complete", True]
        release.set()
        (await page.wait_for("document.querySelector('iframe').contentDocument === null", timeout=2))
        assert page.evaluate("""(() => {
            const frame = document.querySelector('iframe');
            let name = '';
            try { __savedWindow.document; } catch (error) { name = error.name; }
            return [__savedWindow === frame.contentWindow, frame.contentDocument === null, name];
        })()""") == [True, True, "SecurityError"]
    finally:
        release.set()
        (await browser.close())




async def _pump(page, seconds=0.4):
    """Let the queue turn: ``run_until_idle`` raises while a long task runs, so ask for none."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            (await page.run_until_idle(timeout=0))
        except BaseException:  # noqa: BLE001 - a JS error on the way is not this test's
            pass
        (await asyncio.sleep(0.01))


def _reporting(label):
    """A frame document that tells the parent it ran, and is named after its body."""
    return ("<!doctype html><body>" + label + "<script>"
            "parent.postMessage('" + label + ":' + document.body.textContent, '*')"
            "</script></body>")


def _literal(label):
    r"""The frame document as a JavaScript string that never spells ``</script>``.

    ``\/`` is a valid JSON escape that reads the same to JavaScript, so the page's own script
    tag is not closed early by the document it carries.
    """
    return json.dumps(_reporting(label)).replace("</", "<\\/")


#: The frames that report for themselves, and how each one is given its document.
REPORTING = {
    "data": "f.src = 'data:text/html;charset=utf-8,' + encodeURIComponent(INNER);",
    "base64": "f.src = 'data:text/html;charset=utf-8;base64,' + btoa(INNER);",
    "blob": "f.src = URL.createObjectURL(new Blob([INNER], {type: 'text/html'}));",
}

#: The markup a ``srcdoc`` frame carries: no script, so its load finishes.
SRCDOC = "<!doctype html><body>srcdoc<p>plain</p></body>"

PAGE_HTML = """<!doctype html><body><script>
var out = {};
window.__out = out;
window.addEventListener('message', function (event) {
    var parts = String(event.data).split(':');
    out[parts[0]] = parts.slice(1).join(':');
});
function frame(label, setup) {
    var element = document.createElement('iframe');
    element.onload = function () { out[label + '-load'] = true; };
    setup(element);
    document.body.appendChild(element);
}
var INNER = __SRCDOC__;
frame('srcdoc', function (f) { f.srcdoc = INNER; });
__FRAMES__
</script></body>""".replace("__SRCDOC__", json.dumps(SRCDOC))\
               .replace("__FRAMES__", "\n".join(
                   "INNER = " + _literal(label) + ";\nframe('" + label
                   + "', function (f) { " + setup + " });"
                   for label, setup in REPORTING.items()))


#: A frame a *script* inserts while its own turn is open, whose document carries a script too.
#: Committing that document inline entered the frame's context for its own script while the
#: inserting turn was still on the isolate, and the two waited for each other (measured: the
#: stack stopped inside ``load_iframe_markup``).
SCRIPT_INSERTED_PAGE = """<!doctype html><body><script>
var out = {};
window.__out = out;
window.addEventListener('message', function (event) { out.ran = String(event.data); });
var frame = document.createElement('iframe');
frame.onload = function () { out.load = true; };
frame.srcdoc = __INNER__;
document.body.appendChild(frame);
</script></body>""".replace("__INNER__", _literal("inner"))


@pytest.fixture
async def frame_page():
    browser = (await Browser(stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    try:
        (await page.set_content(PAGE_HTML, url="https://frames.test/", wait_until="commit"))
        (await page.wait_for(f"Object.keys(__out).length === {len(REPORTING) * 2 + 1}"))
        yield page
    finally:
        await browser.close()


async def test_a_script_inserted_frame_runs_its_own_document():
    """The insertion happens inside a script's turn, so the frame commits on a task."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(SCRIPT_INSERTED_PAGE, url="https://frames.test/", wait_until="commit"))
        (await _pump(page))
        seen = json.loads(page.evaluate("JSON.stringify(window.__out)"))
        assert seen.get("ran") == "inner:inner", seen
        assert seen.get("load") is True, seen
    finally:
        await browser.close()


@pytest.mark.parametrize("label", sorted(REPORTING))
async def test_a_frame_runs_the_document_it_carries(frame_page, label):
    seen = json.loads(frame_page.evaluate("JSON.stringify(window.__out)"))
    assert seen.get(label) == label, seen
    assert seen.get(label + "-load") is True, seen


async def test_the_whole_page_finished_loading(frame_page):
    """Every frame's load fired: a frame that never finishes would leave this page waiting."""
    seen = json.loads(frame_page.evaluate("JSON.stringify(window.__out)"))
    loaded = [key[: -len("-load")] for key in seen if key.endswith("-load")]
    assert sorted(loaded) == sorted([*REPORTING, "srcdoc"]), seen


async def test_a_srcdoc_frame_loads_the_markup_it_carries(frame_page):
    assert frame_page.evaluate("""(() => {
        const frame = document.querySelector('iframe');
        return frame.contentDocument.body.textContent;
    })()""") == "srcdocplain"
