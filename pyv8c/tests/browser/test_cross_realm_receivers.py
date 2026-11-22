"""A member borrowed from one realm answers for a same-origin object of another.

Chrome's receiver check asks whether the receiver is an instance of the interface,
not whether it came from this realm's template: ``Node.prototype.appendChild``
called on an iframe's body, or ``window.document``'s getter called on its window,
works while the frames are same-origin. A cross-origin receiver still throws, and
so does an object that is not an instance at all.
"""

from pyv8c.async_api import Browser

FRAME = """
(() => {
  const frame = document.createElement('iframe');
  frame.srcdoc = '<body>x</body>';
  document.body.appendChild(frame);
  return true;
})()
"""


async def _page():
    browser = (await Browser(cache=False, real_network=False, stub_logging="off").__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
    return browser, page


async def test_attributes_accept_a_same_origin_frame_receiver():
    browser, page = (await _page())
    try:
        assert page.evaluate(FRAME) is True
        assert await page.run_until_idle(timeout=1)
        assert page.evaluate("""(() => {
            const frame = document.querySelector('iframe');
            const childWindow = frame.contentWindow;
            const childDocument = frame.contentDocument;
            const documentGetter =
                Object.getOwnPropertyDescriptor(window, 'document').get;
            const ownerGetter =
                Object.getOwnPropertyDescriptor(Node.prototype, 'ownerDocument').get;
            return [documentGetter.call(childWindow) === childDocument,
                    documentGetter.call(window) === document,
                    ownerGetter.call(childDocument.body) === childDocument,
                    Node.prototype.lookupNamespaceURI.call(childDocument.body, null)];
        })()""") == [True, True, True, "http://www.w3.org/1999/xhtml"]
    finally:
        (await browser.close())


async def test_methods_accept_a_same_origin_frame_receiver():
    browser, page = (await _page())
    try:
        assert page.evaluate(FRAME) is True
        assert await page.run_until_idle(timeout=1)
        assert page.evaluate("""(() => {
            const childDocument = document.querySelector('iframe').contentDocument;
            const body = childDocument.body;
            const calls = [];
            const listener = () => calls.push('event');
            EventTarget.prototype.addEventListener.call(childDocument, 'sample', listener);
            EventTarget.prototype.dispatchEvent.call(childDocument, new Event('sample'));
            EventTarget.prototype.removeEventListener.call(childDocument, 'sample', listener);

            const element = document.createElement('b');
            Node.prototype.appendChild.call(body, element);
            const appended = body.childNodes.length;
            const lastIsElement = body.lastChild.isSameNode(element);
            body.textContent = 'text';

            return [calls.length,
                    Document.prototype.querySelector.call(childDocument, 'body') === body,
                    Document.prototype.createElement.call(childDocument, 'div').localName,
                    String(Element.prototype.getAttribute.call(body, 'id')),
                    appended, lastIsElement, body.textContent];
        })()""") == [1, True, "div", "null", 2, True, "text"]
    finally:
        (await browser.close())


async def test_event_prototype_chain_stays_in_the_constructing_frame():
    browser, page = (await _page())
    try:
        assert page.evaluate(FRAME) is True
        assert await page.run_until_idle(timeout=1)
        assert page.evaluate("""(() => {
            const child = document.querySelector('iframe').contentWindow;
            const event = new child.MouseEvent('x');
            return [event instanceof child.MouseEvent,
                    event instanceof child.UIEvent,
                    event instanceof child.Event,
                    event instanceof MouseEvent,
                    event instanceof Event,
                    Object.getPrototypeOf(child.MouseEvent.prototype) === child.UIEvent.prototype,
                    Object.getPrototypeOf(child.UIEvent.prototype) === child.Event.prototype];
        })()""") == [True, True, True, False, False, True, True]
    finally:
        (await browser.close())


async def test_a_window_method_accepts_a_same_origin_frame_receiver():
    browser, page = (await _page())
    try:
        assert page.evaluate(FRAME) is True
        assert await page.run_until_idle(timeout=1)
        assert page.evaluate("""(() => {
            const childWindow = document.querySelector('iframe').contentWindow;
            const childDocument = childWindow.document;
            return [window.getComputedStyle.call(childWindow, childDocument.body).display,
                    Performance.prototype.setResourceTimingBufferSize
                        .call(childWindow.performance, 100) === undefined];
        })()""") == ["block", True]
    finally:
        (await browser.close())


async def test_insertion_adopts_the_subtree_into_the_frame_document():
    browser, page = (await _page())
    try:
        assert page.evaluate(FRAME) is True
        assert await page.run_until_idle(timeout=1)
        assert page.evaluate("""(() => {
            const childDocument = document.querySelector('iframe').contentDocument;
            const wrapper = document.createElement('div');
            const child = document.createElement('span');
            wrapper.appendChild(child);
            const sameDocument = wrapper.ownerDocument === document;
            childDocument.body.appendChild(wrapper);
            return [sameDocument,
                    wrapper.ownerDocument === childDocument,
                    child.ownerDocument === childDocument,
                    wrapper.parentNode === childDocument.body,
                    child.baseURI === childDocument.baseURI];
        })()""") == [True, True, True, True, True]
    finally:
        (await browser.close())


async def test_a_shared_template_outlives_the_frame_that_built_it():
    """The template lives with the isolate, so its callbacks must too.

    A callback bound to the realm that first materialised the interface would be
    released with it, and the next frame to borrow the member would be told the V8
    context is closed.
    """
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const frame = document.createElement('iframe');
            frame.srcdoc = '<body>x</body>';
            document.body.appendChild(frame);
            const append = frame.contentWindow.Node.prototype.appendChild;
            frame.remove();
            const element = document.createElement('b');
            try {
              append.call(document.body, element);
              return ['ok', document.body.lastChild === element];
            } catch (error) {
              return [error.name, error.message];
            }
        })()""") == ["ok", True]
    finally:
        (await browser.close())


async def test_a_borrowed_member_survives_the_frame_navigating():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const frame = document.createElement('iframe');
            frame.srcdoc = '<body>one</body>';
            document.body.appendChild(frame);
            window.__append = frame.contentWindow.Node.prototype.appendChild;
            frame.srcdoc = '<body>two</body>';
            return true;
        })()""") is True
        (await page.wait_for(
            "document.querySelector('iframe').contentDocument.body.textContent === 'two'",
            timeout=5,
        ))
        assert page.evaluate("""(() => {
            const element = document.createElement('b');
            try {
              window.__append.call(document.body, element);
              return ['ok', document.body.lastChild === element];
            } catch (error) {
              return [error.name, error.message];
            }
        })()""") == ["ok", True]
    finally:
        (await browser.close())


async def test_a_plain_subclass_is_not_shared_under_a_realm_bound_ancestor():
    """``CustomEvent``'s spec is realm-independent while ``Event``'s is not.

    Sharing the child would chain its prototype into the frame that built it first,
    so the constructor is kept per realm and ``instanceof`` stays inside the frame.
    """
    browser, page = (await _page())
    try:
        assert page.evaluate(FRAME) is True
        assert await page.run_until_idle(timeout=1)
        assert page.evaluate("""(() => {
            const childWindow = document.querySelector('iframe').contentWindow;
            const event = new childWindow.CustomEvent('sample', {detail: 7});
            return [event instanceof childWindow.CustomEvent,
                    event instanceof childWindow.Event,
                    event instanceof Event,
                    event.detail];
        })()""") == [True, True, False, 7]
    finally:
        (await browser.close())


async def test_an_object_that_is_not_an_instance_is_refused():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const getter = Object.getOwnPropertyDescriptor(window, 'document').get;
            const failures = [];
            for (const call of [() => getter.call({}),
                                () => Element.prototype.getAttribute.call(document, 'id')]) {
              try { call(); failures.push('accepted'); }
              catch (error) { failures.push([error.name, error.message]); }
            }
            return failures;
        })()""") == [["TypeError", "Illegal invocation"],
                     ["TypeError", "Illegal invocation"]]
    finally:
        (await browser.close())


async def test_a_cross_origin_receiver_is_refused():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const frame = document.createElement('iframe');
            frame.sandbox = 'allow-scripts';
            frame.srcdoc = '<body>x</body>';
            document.body.appendChild(frame);
            const crossWindow = frame.contentWindow;
            const outcomes = [String(frame.contentDocument)];
            for (const call of [
                () => Object.getOwnPropertyDescriptor(window, 'document').get.call(crossWindow),
                () => window.getComputedStyle.call(crossWindow, null),
                () => EventTarget.prototype.addEventListener.call(crossWindow, 'x', () => {}),
            ]) {
              try { call(); outcomes.push('accepted'); }
              catch (error) { outcomes.push(error.name); }
            }
            return outcomes;
        })()""") == ["null", "SecurityError", "SecurityError", "SecurityError"]
    finally:
        (await browser.close())
