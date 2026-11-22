"""AbortController and AbortSignal, offline, and the three things a signal cancels.

Every expectation here was read off Chrome 154 over a local server: the two reason
wordings ("signal is aborted without reason", "signal timed out"), the abort event
that neither bubbles nor cancels, and ``AbortSignal.any`` handing out one signal that
is several at once.  A page's own loader stands in for that server so the rules stay
covered on a machine with none.  What the corpus cases hold against Chrome are the
readings that need no waiting; the ones that do are here.
"""

import asyncio
import threading

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c.async_api import Browser

ORIGIN = "http://abort.test"
PAGE = "<!doctype html><title>abort</title><body><div id=host></div></body>"


async def _page(loader=None):
    browser = (await Browser(stub_logging="off", threaded_loaders=True,
                      resource_loader=loader or (lambda url, request: _response(url))).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content(PAGE, url=ORIGIN + "/", wait_until="commit"))
    return browser, page


def _response(url, status=200, body="ok"):
    return {"status": status, "url": str(url), "body": body,
            "headers": {"Content-Type": "text/plain"}}


async def test_a_signal_is_not_constructible_and_the_constructor_needs_new():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const out = [];
            try { new AbortSignal(); } catch (e) { out.push([e.name, e.message]); }
            try { AbortSignal(); } catch (e) { out.push([e.name, e.message]); }
            try { AbortController(); } catch (e) { out.push([e.name, e.message]); }
            return out;
        })()""") == [
            # Chrome prefixes the member: "Failed to construct 'AbortSignal': ...".
            ["TypeError", "Illegal constructor"],
            ["TypeError", "Illegal constructor"],
            ["TypeError", "Failed to construct 'AbortController': Please use the 'new'"
                          " operator, this DOM object constructor cannot be called as a"
                          " function."],
        ]
    finally:
        (await browser.close())


async def test_aborting_fires_one_plain_event_and_keeps_the_first_reason():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const c = new AbortController(), s = c.signal, seen = [];
            s.addEventListener('abort', (e) => seen.push([e.type, e.bubbles, e.cancelable,
                                                          e.composed, e.target === s]));
            c.abort();
            c.abort('second');
            return [seen, s.aborted, s.reason.name, s.reason.message, s.reason.code,
                    s.reason instanceof DOMException, String(s.reason)];
        })()""") == [[["abort", False, False, False, True]], True, "AbortError",
                     "signal is aborted without reason", 20, True,
                     "AbortError: signal is aborted without reason"]
    finally:
        (await browser.close())


async def test_a_reason_the_page_gave_travels_as_itself():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const out = [];
            const custom = new AbortController();
            custom.abort('why');
            out.push([custom.signal.reason === 'why', typeof custom.signal.reason]);
            const number = new AbortController();
            number.abort(5);
            out.push([number.signal.reason === 5, number.signal.reason instanceof DOMException]);
            const nothing = new AbortController();
            nothing.abort();
            out.push([nothing.signal.reason.name, nothing.signal.reason.message]);
            const explicit = new AbortController();
            explicit.abort(undefined);
            out.push([explicit.signal.reason.name, explicit.signal.reason.message]);
            const nulled = new AbortController();
            nulled.abort(null);
            out.push([nulled.signal.reason === null, nulled.signal.aborted]);
            return out;
        })()""") == [[True, "string"], [True, False], ["AbortError",
                     "signal is aborted without reason"], ["AbortError",
                     "signal is aborted without reason"], [True, True]]
    finally:
        (await browser.close())


async def test_onabort_is_a_prototype_slot_that_fires_once():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const c = new AbortController(), s = c.signal;
            const before = s.onabort;
            let n = 0;
            s.onabort = () => n++;
            const inPrototype = 'onabort' in s && !s.hasOwnProperty('onabort');
            c.abort();
            c.abort();
            s.onabort = null;
            return [typeof before, String(before), inPrototype, n, String(s.onabort)];
        })()""") == ["object", "null", True, 1, "null"]
    finally:
        (await browser.close())


async def test_the_statics_hand_out_signals_that_are_aborted_or_waiting():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const out = [];
            const quiet = AbortSignal.abort();
            out.push([quiet.aborted, quiet.reason.name, quiet.reason.message,
                      quiet.reason.code]);
            const custom = AbortSignal.abort('given');
            out.push([custom.aborted, custom.reason === 'given', typeof custom.reason]);
            const waiting = AbortSignal.timeout(60000);
            out.push([waiting.aborted, String(waiting.reason), waiting instanceof AbortSignal,
                      waiting instanceof EventTarget]);
            return out;
        })()""") == [[True, "AbortError", "signal is aborted without reason", 20],
                     [True, True, "string"], [False, "undefined", True, True]]
    finally:
        (await browser.close())


async def test_a_deadline_fires_with_chromes_timeout_reason():
    browser, page = (await _page())
    try:
        page.evaluate("""(() => {
            window.__timeout = null;
            const s = AbortSignal.timeout(1);
            s.onabort = () => {
                window.__timeout = [s.aborted, s.reason.name, s.reason.message,
                                    s.reason.code, s.reason instanceof DOMException];
            };
            return 'armed';
        })()""")
        (await page.wait_for("window.__timeout !== null", timeout=10))
        assert page.evaluate("window.__timeout") == [True, "TimeoutError", "signal timed out",
                                                    23, True]
    finally:
        (await browser.close())


async def test_a_deadline_conversion_is_checked_the_way_chrome_checks_it():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const out = [];
            for (const value of [undefined, NaN]) {
                try { AbortSignal.timeout(value); out.push('no-throw'); }
                catch (e) { out.push([e.name, e.message]); }
            }
            for (const value of [2 ** 53, -1, 2 ** 63]) {
                try { AbortSignal.timeout(value); out.push('no-throw'); }
                catch (e) { out.push([e.name, e.message]); }
            }
            for (const value of [1.5, '5', 2 ** 53 - 1]) {
                out.push([String(value), AbortSignal.timeout(value) instanceof AbortSignal]);
            }
            return out;
        })()""") == [
            ["TypeError", "Failed to execute 'timeout' on 'AbortSignal': Value is not of"
                          " type 'unsigned long long'."],
            ["TypeError", "Failed to execute 'timeout' on 'AbortSignal': Value is not of"
                          " type 'unsigned long long'."],
            ["TypeError", "Failed to execute 'timeout' on 'AbortSignal': Value is outside"
                          " the 'unsigned long long' value range."],
            ["TypeError", "Failed to execute 'timeout' on 'AbortSignal': Value is outside"
                          " the 'unsigned long long' value range."],
            ["TypeError", "Failed to execute 'timeout' on 'AbortSignal': Value is outside"
                          " the 'unsigned long long' value range."],
            ["1.5", True], ["5", True], ["9007199254740991", True],
        ]
    finally:
        (await browser.close())


async def test_any_follows_its_members_and_keeps_their_reason():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const out = [];
            const quiet = AbortSignal.any([]);
            out.push([quiet.aborted, String(quiet.reason), quiet instanceof AbortSignal]);
            const first = new AbortController();
            const combined = AbortSignal.any([first.signal, new AbortController().signal]);
            let fired = 0;
            combined.onabort = () => fired++;
            first.abort('z');
            out.push([combined.aborted, combined.reason === first.signal.reason,
                      String(combined.reason), fired, combined.signal === undefined]);
            const already = new AbortController();
            already.abort('x');
            const late = AbortSignal.any([new AbortController().signal, already.signal]);
            out.push([late.aborted, String(late.reason)]);
            return out;
        })()""") == [[False, "undefined", True], [True, True, "z", 1, True], [True, "x"]]
    finally:
        (await browser.close())


async def test_any_refuses_what_is_not_a_signal_in_chromes_words():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const out = [];
            try { AbortSignal.any(); } catch (e) { out.push([e.name, e.message]); }
            try { AbortSignal.any(1); } catch (e) { out.push([e.name, e.message]); }
            try { AbortSignal.any([1]); } catch (e) { out.push([e.name, e.message]); }
            try { AbortSignal.any([null]); } catch (e) { out.push([e.name, e.message]); }
            return out;
        })()""") == [
            ["TypeError", "Failed to execute 'any' on 'AbortSignal': 1 argument required,"
                          " but only 0 present."],
            ["TypeError", "Failed to execute 'any' on 'AbortSignal': The provided value"
                          " cannot be converted to a sequence."],
            ["TypeError", "Failed to execute 'any' on 'AbortSignal': Failed to convert"
                          " value to 'AbortSignal'."],
            ["TypeError", "Failed to execute 'any' on 'AbortSignal': Failed to convert"
                          " value to 'AbortSignal'."],
        ]
    finally:
        (await browser.close())


async def test_the_receiver_is_checked_on_the_members_that_need_their_own():
    browser, page = (await _page())
    try:
        assert page.evaluate("""(() => {
            const out = [];
            try { AbortController.prototype.abort.call({}); out.push('no-throw'); }
            catch (e) { out.push([e.name, e.message]); }
            try { AbortSignal.prototype.throwIfAborted.call({}); out.push('no-throw'); }
            catch (e) { out.push([e.name, e.message]); }
            const getter = Object.getOwnPropertyDescriptor(AbortController.prototype, 'signal').get;
            try { getter.call({}); out.push('no-throw'); }
            catch (e) { out.push([e.name, e.message]); }
            return out;
        })()""") == [["TypeError", "Illegal invocation"], ["TypeError", "Illegal invocation"],
                     ["TypeError", "Illegal invocation"]]
    finally:
        (await browser.close())


async def test_a_fetch_the_page_aborted_rejects_with_the_reason():
    browser, page = (await _page())
    try:
        page.evaluate("""(() => {
            window.__early = window.__custom = window.__late = null;
            const before = new AbortController();
            before.abort();
            fetch('/data', {signal: before.signal}).catch(
                (e) => window.__early = [e.name, e.message, e instanceof DOMException]);
            const custom = new AbortController();
            fetch('/data', {signal: custom.signal}).catch(
                (e) => window.__custom = [e.name, e.message]);
            custom.abort(new Error('stop'));
            const midflight = new AbortController();
            fetch('/data', {signal: midflight.signal}).catch(
                (e) => window.__late = [e.name, e.message]);
            setTimeout(() => midflight.abort(), 0);
            return 'asked';
        })()""")
        (await page.run_until_idle(timeout=5))
        assert page.evaluate("window.__early") == ["AbortError",
                                                   "signal is aborted without reason", True]
        assert page.evaluate("window.__custom") == ["Error", "stop"]
        assert page.evaluate("window.__late") == ["AbortError",
                                                  "signal is aborted without reason"]
    finally:
        (await browser.close())


async def test_an_aborted_fetch_never_delivers_the_response():
    started = threading.Event()
    released = threading.Event()

    def loader(url, request):
        started.set()
        released.wait(5)
        return _response(url)

    browser, page = (await _page(loader))
    try:
        page.evaluate("""(() => {
            window.__settled = 'pending';
            const controller = new AbortController();
            fetch('/slow', {signal: controller.signal}).then(
                () => window.__settled = 'resolved',
                () => window.__settled = 'rejected');
            setTimeout(() => controller.abort(), 0);
            return 'asked';
        })()""")
        (await page.run_until_idle(timeout=2))
        assert (await asyncio.to_thread(started.wait, 5)), "the loader never ran"
        released.set()
        (await page.run_until_idle(timeout=5))
        assert page.evaluate("window.__settled") == "rejected"
    finally:
        released.set()
        (await browser.close())
