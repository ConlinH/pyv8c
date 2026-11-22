"""A failure of the host's own scheduling is not something a script may observe.

The executor's guard raises :class:`RealmBusyError` when a realm will not answer, and the
bridge hands whatever escapes a Python callback to whoever called in -- including a page.
No browser ever fails a script for the host's own reason, so ``host_boundary`` is where it
stops: JavaScript being on the stack *is* the boundary, and what the script loses in
visibility the log keeps.  The same rule holds where an exception is not thrown at a
caller at all -- a task that raised, and the error events a realm reports.
"""
import json

import pytest
from loguru import logger

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c import pyv8
from pyv8c.pyv8 import IdlSpec

from pyv8c.async_api import Browser
from pyv8c.web.page_errors import PageError, PageTypeError, js_error_value
from pyv8c.foundation.host_errors import RealmBusyError
from pyv8c.foundation.error_policy import host_boundary, is_host_failure

WEDGED = "realm executor did not complete '_cleanup_owner' within 5s (already running)"


class Window:
    """The smallest global a native V8Context needs."""

    __v8_idl_spec__ = IdlSpec(global_this=True)


@pytest.fixture
def context():
    instance = pyv8.V8Context(Window())
    try:
        yield instance
    finally:
        instance.close()


def sink():
    """Capture log lines, where a failure hidden from a script has to show up instead."""
    messages = []
    return messages, logger.add(lambda message: messages.append(str(message).strip()),
                                format="{message}")


async def test_a_rejection_reason_is_what_a_realm_may_be_shown():
    #: A declared error crosses as itself -- the native resolver builds the JavaScript error
    #: when it settles -- and a non-exception reason stays what it is.  A failure of the
    #: host's own is not something a realm may be handed, so the page gets a plain error
    #: that says nothing about the host, and the host's own reason stays in the log.
    declared = PageTypeError("declared reason")
    assert js_error_value(declared) is declared
    assert js_error_value("plain reason") == "plain reason"

    carried = js_error_value(RuntimeError("late rejection"))
    assert isinstance(carried, PageError)
    assert str(carried) == "The operation failed"
    assert "late rejection" not in str(carried)


async def test_a_failure_outside_the_family_is_the_hosts_own():
    assert is_host_failure(RealmBusyError(WEDGED)) is True
    # The guard has always been a RuntimeError, and callers catch it as one.
    assert isinstance(RealmBusyError(WEDGED), RuntimeError)
    # What no family member covers is the host's too: no realm is shown it, and a
    # page's error reporting answers to the same rule.
    assert is_host_failure(RuntimeError(WEDGED)) is True
    # A declaration is what makes a failure the page's to observe.
    assert is_host_failure(PageError("the script's own failure")) is False
    assert is_host_failure("a thrown value") is False


async def test_a_host_failure_while_scripting_is_logged_and_the_script_carries_on(context):
    messages, token = sink()

    def close():
        with host_boundary("WorkerGlobalScope.close"):
            raise RealmBusyError(WEDGED)
        return "carried on"

    context.expose(close=close)
    try:
        assert context.exec_js("close()") == "carried on"
    finally:
        logger.remove(token)
    assert len(messages) == 1, messages
    assert "WorkerGlobalScope.close" in messages[0]
    assert "_cleanup_owner" in messages[0]


async def test_the_same_failure_the_host_asked_for_still_reaches_the_host():
    """No script is on the stack, so there is nothing to keep it out of."""
    with pytest.raises(RealmBusyError, match="_cleanup_owner"):
        with host_boundary("WorkerGlobalScope.close"):
            raise RealmBusyError(WEDGED)


async def test_a_realm_pyv8_cannot_answer_for_says_so_itself():
    """A worker's realm runs on its own isolate, where no context is the current one."""
    messages, token = sink()
    try:
        with host_boundary("WorkerGlobalScope.close", from_script=True):
            raise RealmBusyError(WEDGED)
        with pytest.raises(RealmBusyError, match="_cleanup_owner"):
            with host_boundary("WorkerGlobalScope.close", from_script=False):
                raise RealmBusyError(WEDGED)
    finally:
        logger.remove(token)
    assert len(messages) == 1, messages


async def test_a_scripts_own_failure_is_still_thrown_at_the_script(context):
    def fail():
        with host_boundary("WorkerGlobalScope.close"):
            raise PageError("the script's own failure")

    context.expose(fail=fail)
    assert context.exec_js(
        "(function () { try { fail(); } catch (error) {"
        " return error.name + ':' + error.message; } })()"
    ) == "Error:the script's own failure"


async def test_a_host_failure_in_a_task_is_not_a_page_error():
    """Where nothing is thrown at a caller, the realm still hears only its own failures."""
    messages, token = sink()

    def host_failure():
        raise RealmBusyError(WEDGED)

    def script_failure():
        raise PageError("the script's own failure")

    browser = (await Browser(stub_logging="off", real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<html><body></body></html>", wait_until="commit"))
        page.evaluate("window.__errors = [];"
                      " window.addEventListener('error',"
                      "   (event) => __errors.push(String(event.message)))")
        page.tasks.post(host_failure)
        page.tasks.post(script_failure)
        (await page.run_until_idle(0.3))
        seen = json.loads(page.evaluate("JSON.stringify(window.__errors)"))
    finally:
        logger.remove(token)
        (await browser.close())
    assert len(seen) == 1 and "the script's own failure" in seen[0], seen
    assert any("the host could not finish" in line for line in messages), messages
