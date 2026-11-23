"""Synchronous Browser / Profile / Page API on the caller's thread.

One Browser owns one persistent event loop. Blocking operations cooperatively
run that loop; evaluate and DOM operations enter the shared Runtime directly.
Use async_api from an existing asyncio loop. Host sleep does not pump page
tasks; Page.sleep() waits while keeping the page loop running.
"""

import asyncio
import time

from .foundation.host_errors import HostStateError
from .foundation.sync_driver import SyncDriver
from .platform.browser import Browser as _BrowserCore
from .platform.page import EMPTY_DOCUMENT as _EMPTY_DOCUMENT
from . import pyv8

__all__ = ("Browser", "Profile", "Page", "DevToolsHandle")


def _check_wait():
    # Direct evaluate runs without an asyncio loop. Its Python callbacks still
    # must not start a loop while a native JS stack (in any isolate) is active.
    if pyv8.current_context() is not None:
        raise HostStateError("cannot suspend a host wait inside a running JS stack")


def _call_input(page, driver, operation, /, *args, **kwargs):
    driver.check()
    page._check()
    generation = page.navigation.generation

    async def dispatch():
        page._check_document_generation(generation)
        return await operation(*args, **kwargs)

    return driver.call(dispatch)


class _Owned:
    def __init__(self):
        raise TypeError("Create profiles and pages through Browser / Profile factories")

    def __enter__(self):
        self._driver.call(self._impl.__aenter__)
        return self

    def __exit__(self, *exc):
        self.close()


class Browser:
    """A synchronous application scope, owning its loop and shared Runtime."""

    def __init__(self, **options):
        SyncDriver.check_no_loop()
        _check_wait()
        self._impl = _BrowserCore(**options)
        self._driver = SyncDriver(self, before_wait=_check_wait)
        self._driver._objects[self._impl] = self

    def __enter__(self):
        self._driver.open()
        try:
            self._driver.call(self._impl.__aenter__)
        except BaseException as error:
            try:
                if self._impl._runtime.owner is None:
                    self._driver.finish()
                else:
                    self.close()
            except BaseException as cleanup_error:
                error.add_note(f"Browser startup cleanup failed: {cleanup_error!r}")
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def closed(self):
        return self._impl.closed

    @property
    def environment(self):
        return self._impl.environment

    @property
    def startup_stats(self):
        return self._impl.startup_stats

    @property
    def profiles(self):
        self._driver.check()
        return [self._driver.wrap(profile, Profile) for profile in self._impl.profiles]

    def new_profile(self, **options):
        self._driver.check()
        return self._driver.wrap(self._impl.new_profile(**options), Profile)

    def new_page(self, **options):
        self._driver.check()
        return self._driver.wrap(self._impl.new_page(**options), Page)

    def close(self):
        if self._impl.closed and self._driver._runner is None:
            # Idempotence still respects thread ownership and rejects nested loops.
            self._impl._runtime.check_thread()
            SyncDriver.check_no_loop()
            return
        self._driver.close_scope(self._impl._begin_close)
        # Failed or interrupted cleanup keeps the loop alive for an explicit retry.
        self._driver.finish()


class Profile(_Owned):
    """An isolated session owned by its synchronous Browser."""

    @property
    def browser(self):
        return self._browser

    @property
    def pages(self):
        self._driver.check()
        return [self._driver.wrap(page, Page) for page in self._impl.pages]

    @property
    def cookies(self):
        return self._impl.cookies

    @property
    def closed(self):
        return self._impl.closed

    def new_page(self, **options):
        self._driver.check()
        return self._driver.wrap(self._impl.new_page(**options), Page)

    def flush(self):
        """Commit this profile's pending cookies and local storage."""
        self._driver.call(self._impl.flush)

    def start_devtools(self, page, *, options=None):
        self._driver.check()
        if not isinstance(page, Page) or page._driver is not self._driver:
            raise HostStateError("Page does not belong to this profile")
        return self._driver.wrap(self._impl.start_devtools(page._impl, options=options), DevToolsHandle)

    def close(self):
        if self._impl.browser.closed:
            self._impl.browser._runtime.check_thread()
            SyncDriver.check_no_loop()
            return
        self._driver.close_scope(self._impl._begin_close)


class Page(_Owned):
    """One synchronous page; live DOM/JS handles stay on the owner thread."""

    @property
    def profile(self):
        return self._driver.wrap(self._impl.profile, Profile)

    @property
    def closed(self):
        return self._impl.closed

    @property
    def url(self):
        self._driver.check()
        return self._impl.url

    @property
    def origin(self):
        self._driver.check()
        return self._impl.origin

    @property
    def environment(self):
        return self._impl.environment

    @property
    def window(self):
        self._driver.check()
        return self._impl.window

    @property
    def document(self):
        self._driver.check()
        return self._impl.document

    @property
    def trace(self):
        return self._impl.trace

    @trace.setter
    def trace(self, enabled):
        self._driver.check()
        self._impl.trace = enabled

    @property
    def net(self):
        return self._driver.wrap(self._impl.net, PageNet)

    @property
    def mouse(self):
        self._driver.check()
        return self._driver.wrap(self._impl.mouse, MouseInput)

    def close(self):
        if self._impl.profile.browser.closed:
            self._impl._runtime.check_thread()
            SyncDriver.check_no_loop()
            return
        self._driver.close_scope(self._impl._begin_close)

    def set_content(self, html=_EMPTY_DOCUMENT, url='about:blank', *, timeout=30, wait_until='load'):
        'Inject markup and wait for the selected document lifecycle state.'
        self._driver.call(self._impl.set_content, html, url, timeout=timeout, wait_until=wait_until)
        return self

    def goto(self, url, *, timeout=30, wait_until='load'):
        return self._driver.call(self._impl.goto, url, timeout=timeout, wait_until=wait_until)

    def wait_for_load_state(self, state='load', timeout=30):
        return self._driver.call(self._impl.wait_for_load_state, state, timeout=timeout)

    def wait_for(self, expression, timeout=30):
        return self._driver.call(self._impl.wait_for, expression, timeout=timeout)

    def wait_for_selector(self, selector, *, deep=False, timeout=30):
        return self._driver.call(self._impl.wait_for_selector, selector,
                                 deep=deep, timeout=timeout)

    def evaluate(self, script, filename=None, *, timeout=30):
        "Run *script* in the page's realm and return its result as Python."
        self._driver.check()
        return self._impl.evaluate(script, filename, timeout=timeout)

    def evaluate_handle(self, script, filename=None):
        'Run *script* and return the live V8 result instead of a Python copy.'
        self._driver.check()
        return self._impl.evaluate_handle(script, filename)

    def run_until_idle(self, timeout=1):
        return self._driver.call(self._impl.run_until_idle, timeout=timeout)

    def sleep(self, seconds):
        "Wait *seconds* while the page's event loop keeps running."
        return self._driver.call(self._impl.sleep, seconds)

    def dispatch_event(self, event_type, *, target='window', selector=None, event_class=None, init=None):
        'Dispatch a host-trusted event and report whether it was not cancelled.'
        self._driver.check()
        return self._impl.dispatch_event(event_type, target=target, selector=selector, event_class=event_class, init=init)

    def query_selector(self, selector, *, deep=False):
        'Return the host-side node matching a CSS selector.'
        self._driver.check()
        return self._impl.query_selector(selector, deep=deep)

    def set_visibility(self, hidden):
        "Set the document's visibility and fire the events that follow."
        self._driver.check()
        return self._impl.set_visibility(hidden)

    def click(self, selector, *, button='left', click_count=1, force=False, delay=None, duration=None, deep=False):
        'Click the element *selector* matches, the way a user would.'
        return _call_input(self._impl, self._driver, self._impl.click, selector,
                           button=button, click_count=click_count, force=force, delay=delay,
                           duration=duration, deep=deep)

    def dblclick(self, selector, *, button='left', force=False, delay=None, duration=None, deep=False):
        'Double-click the element *selector* matches.'
        return _call_input(self._impl, self._driver, self._impl.dblclick, selector,
                           button=button, force=force, delay=delay, duration=duration, deep=deep)

    def hover(self, selector, *, force=False, duration=None, curve=None, seed=None, deep=False):
        'Move the mouse to the centre of the element *selector* matches.'
        return _call_input(self._impl, self._driver, self._impl.hover, selector,
                           force=force, duration=duration, curve=curve, seed=seed, deep=deep)


class PageNet(_Owned):
    """Synchronous host HTTP and page fetch/XHR, using the same deadline rules."""

    @property
    def available(self):
        self._driver.check()
        return self._impl.available

    @property
    def headers(self):
        self._driver.check()
        return self._impl.headers

    @headers.setter
    def headers(self, value):
        self._driver.check()
        self._impl.headers = value

    def build(self, method, url, *, headers=None, params=None, data=None, json=None, cookies=True, timeout=None, referer=None, options=None):
        'Turn a host call into one :class:`NetworkRequest`.'
        self._driver.check()
        return self._impl.build(method, url, headers=headers, params=params, data=data, json=json, cookies=cookies, timeout=timeout, referer=referer, options=options)

    def request(self, method, url, *, timeout=None, **kwargs):
        'Send a host request without blocking the Page owner thread.'
        return self._driver.call(self._impl.request, method, url, timeout=timeout, **kwargs)

    def fetch(self, url, *, method='GET', headers=None, body=None, credentials=None, timeout=30):
        "Run the page's own ``fetch`` and return what it answered."
        return self._driver.call(self._impl.fetch, url, method=method, headers=headers, body=body, credentials=credentials, timeout=timeout)

    def xhr(self, url, *, method='GET', headers=None, body=None, credentials=None, timeout=30):
        "Run the page's own ``XMLHttpRequest`` and return what it answered."
        return self._driver.call(self._impl.xhr, url, method=method, headers=headers, body=body, credentials=credentials, timeout=timeout)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def head(self, url, **kwargs):
        return self.request("HEAD", url, **kwargs)

    def options(self, url, **kwargs):
        return self.request("OPTIONS", url, **kwargs)


class MouseInput(_Owned):
    """Mouse gestures driven by the Browser's cooperative loop."""

    @property
    def position(self):
        return self._impl.position

    @property
    def pressed(self):
        return self._impl.pressed

    def _gesture(self, operation, /, *args, **kwargs):
        return _call_input(self._impl._page, self._driver, operation, *args, **kwargs)

    def move(self, x, y, *, steps=None, duration=None, curve=None, seed=None):
        return self._gesture(self._impl.move, x, y, steps=steps, duration=duration, curve=curve, seed=seed)

    def down(self, *, button='left', click_count=None):
        return self._gesture(self._impl.down, button=button, click_count=click_count)

    def up(self, *, button='left', click_count=None):
        return self._gesture(self._impl.up, button=button, click_count=click_count)

    def click(self, x=None, y=None, *, button='left', click_count=None, delay=None):
        return self._gesture(self._impl.click, x, y, button=button, click_count=click_count, delay=delay)

    def dblclick(self, x=None, y=None, *, button='left', delay=None):
        return self._gesture(self._impl.dblclick, x, y, button=button, delay=delay)

    def move_to_element(self, element, *, force=False, duration=None, curve=None, seed=None):
        return self._gesture(self._impl.move_to_element, element,
                             force=force, duration=duration, curve=curve, seed=seed)

    def click_element(self, element, *, button='left', click_count=1, force=False, delay=None, duration=None):
        return self._gesture(self._impl.click_element, element, button=button,
                             click_count=click_count, force=force, delay=delay, duration=duration)


class DevToolsHandle(_Owned):
    """A browser debugger whose handshake waits also advance its owner loop."""

    @property
    def host(self):
        return self._impl.host

    @property
    def port(self):
        return self._impl.port

    @property
    def url(self):
        return self._impl.url

    def __enter__(self):
        self._driver.check()
        return self

    def wait_for_connection(self, timeout=None):
        return self._driver.call(self._wait, False, timeout=timeout)

    def wait_for_ready(self, timeout=None):
        return self._driver.call(self._wait, True, timeout=timeout)

    async def _wait(self, ready, timeout):
        deadline = None if timeout is None else time.perf_counter() + max(0.0, float(timeout))
        probe = self._impl.wait_for_ready if ready else self._impl.wait_for_connection
        while True:
            if probe(0):
                return True
            if (self._impl._closed or self._impl._router.transport.stopped.is_set()
                    or (ready and (self._impl._root.closed or self._impl._root.disconnected))):
                return False
            remaining = None if deadline is None else deadline - time.perf_counter()
            if remaining is not None and remaining <= 0:
                return False
            await asyncio.sleep(.001 if remaining is None else min(.001, remaining))

    def close(self):
        self._driver.check_access()
        return self._impl.close()
