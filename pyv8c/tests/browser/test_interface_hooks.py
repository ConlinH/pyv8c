"""Hooks that answer a baseline member instead of its implementation.

A hook replaces the callable its member is compiled from, so it works on a member that
has an implementation and on one that never had any, while the member's position,
`length` and read-only state stay the baseline's.  What a hook receives is what the
native side hands a real implementation, which is what the argument counts here pin:
a prototype member is called with the receiver first, one on the interface itself
without it.  Nothing is patched onto the implementation classes, so another Runtime
built from the same classes is untouched.
"""
import pytest
from loguru import logger

from pyv8c.async_api import Browser
from pyv8c.web.dom import DOMTokenList

URL = "http://hooks.test/"


async def _page(browser, html="<input id=i>"):
    page = browser.new_profile().new_page()
    (await page.set_content(html, url=URL, wait_until="commit"))
    return page


async def test_a_hook_answers_a_member_that_never_had_an_implementation():
    browser = (await Browser(stub_logging="off", interface_overrides={
        "MediaSource": {"addSourceBuffer": lambda *_: "buffered!"},
    }).__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("new MediaSource().addSourceBuffer('video/mp4')") == "buffered!"
    finally:
        (await browser.close())


async def test_a_hooked_member_is_no_longer_reported_as_unimplemented():
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        hooked = (await Browser(stub_logging="on", interface_overrides={
            "MediaSource": {"addSourceBuffer": lambda *_: "buffered!"},
        }).__aenter__())
        try:
            (((await _page(hooked)))).evaluate("new MediaSource().addSourceBuffer('video/mp4')")
        finally:
            (await hooked.close())
        assert [message for message in messages if "interface override: MediaSource.addSourceBuffer" in message]
        assert not [message for message in messages if "addSourceBuffer() 待实现" in message]

        plain = (await Browser(stub_logging="on").__aenter__())
        try:
            (((await _page(plain)))).evaluate("new MediaSource().addSourceBuffer('video/mp4')")
        finally:
            (await plain.close())
        assert [message for message in messages if "MediaSource.addSourceBuffer() 待实现" in message]
    finally:
        logger.remove(token)


async def test_a_hook_replaces_a_member_that_has_an_implementation():
    browser = (await Browser(stub_logging="off", interface_overrides={
        "Navigator": {"hardwareConcurrency": lambda *_: 42},
    }).__aenter__())
    try:
        assert (((await _page(browser)))).evaluate("navigator.hardwareConcurrency") == 42
    finally:
        (await browser.close())


async def test_a_prototype_member_is_called_with_the_receiver_and_one_on_the_interface_is_not():
    browser = (await Browser(stub_logging="off", interface_overrides={
        "Element": {"setAttribute": lambda *args: len(args)},
        "URL": {"canParse": lambda *args: len(args)},
        "Navigator": {"hardwareConcurrency": lambda *args: len(args)},
    }).__aenter__())
    try:
        page = (((await _page(browser))))
        # ``el.setAttribute('a', 'b')`` reaches a real implementation as (self, 'a', 'b').
        assert page.evaluate("document.body.setAttribute('a', 'b')") == 3
        # A getter is read as (self).
        assert page.evaluate("navigator.hardwareConcurrency") == 1
        # A member on the interface itself carries no receiver.
        assert page.evaluate("URL.canParse('/x')") == 1
    finally:
        (await browser.close())


async def test_a_write_hook_takes_the_write_away_from_the_implementation():
    written = []
    browser = (await Browser(stub_logging="off", interface_overrides={
        "HTMLInputElement": {"valueAsNumber": {"set": lambda self, value: written.append(value)}},
    }).__aenter__())
    try:
        page = (((await _page(browser))))
        page.evaluate("document.getElementById('i').valueAsNumber = 5")
        # The real setter would have written "5" into the control; it never ran.
        assert page.evaluate("document.getElementById('i').value") == ""
        assert [float(value) for value in written] == [5.0]
    finally:
        (await browser.close())


async def test_the_same_member_answers_differently_in_two_browsers():
    hooked = (await Browser(stub_logging="off", interface_overrides={
        "Navigator": {"hardwareConcurrency": lambda *_: 42},
    }).__aenter__())
    plain = (await Browser(stub_logging="off").__aenter__())
    try:
        assert (((await _page(hooked)))).evaluate("navigator.hardwareConcurrency") == 42
        assert (((await _page(plain)))).evaluate("navigator.hardwareConcurrency") != 42
    finally:
        (await hooked.close())
        (await plain.close())


async def test_a_hook_is_visible_in_a_worker_realm_too():
    browser = (await Browser(stub_logging="off", interface_overrides={
        "Navigator": {"hardwareConcurrency": lambda *_: 42},
    }).__aenter__())
    try:
        page = (((await _page(browser))))
        page.evaluate("""
            (() => {
                const source = new Blob(["postMessage(navigator.hardwareConcurrency)"],
                                        {type: "text/javascript"});
                const worker = new Worker(URL.createObjectURL(source));
                worker.onmessage = (event) => { window.__worker = event.data; };
            })()
        """)
        (await page.wait_for("typeof window.__worker !== 'undefined'", timeout=10))
        assert page.evaluate("window.__worker") == 42
    finally:
        (await browser.close())


async def test_a_hook_answers_the_member_without_touching_the_implementation_class():
    before = frozenset(DOMTokenList.__dict__)
    browser = (await Browser(stub_logging="off", interface_overrides={
        "DOMTokenList": {"contains": lambda *_: True},
        "MediaSource": {"addSourceBuffer": lambda *_: None},
    }).__aenter__())
    try:
        page = (((await _page(browser))))
        assert page.evaluate("document.createElement('div').classList.contains('x')") is True
        assert frozenset(DOMTokenList.__dict__) == before
        # A hook is an answer, not a baseline difference.
        assert browser.bindings.discrepancies == ()
    finally:
        (await browser.close())


async def test_a_configuration_that_cannot_be_answered_fails_while_the_browser_is_built():
    with pytest.raises(TypeError, match="is read-only in the interface baseline"):
        (await Browser(stub_logging="off", interface_overrides={
            "Navigator": {"userAgent": {"set": lambda *_: None}},
        }).__aenter__())
    with pytest.raises(TypeError, match="is not a member of Navigator"):
        (await Browser(stub_logging="off", interface_overrides={"Navigator": {"nope": lambda *_: None}}).__aenter__())
