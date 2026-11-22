"""The failures only the host sees: what a page is neither shown nor able to cause.

These are the other side of the line the error family draws.  None of them inherits a
type of the family the binding publishes, so however a page reaches the code that raises
one, it never arrives as an error there: the host catches it or the run ends, and the
reason is logged.  What a page hears instead -- a bare ``TypeError`` from ``fetch``, an
``error`` event on the element that asked -- is the browser's own wording, which the web
layer produces.
"""


class HostFailure(RuntimeError):
    """A failure of the host's own rather than the page's doing.

    Outside the family by inheritance, so a JavaScript boundary refuses to show it:
    what a page may observe is what a browser would let it observe, and no browser
    fails a script because the host's own scheduler ran out of patience.
    """


class RealmBusyError(HostFailure):
    """The realm owner thread did not pick up a command in time.

    Raised instead of blocking forever when a realm is wedged (paused
    inspector, runaway script, blocked teardown) so callers and test runs
    fail with a diagnosable error rather than hanging.
    """


class HostStateError(HostFailure):
    """The host asked an object for something its state cannot give: a page or context
    that is closed, a browser with no engine to ask, a selector that matches nothing.

    Invisible by inheritance, like the rest of the family: what a host learns is the
    class and the message, and no realm is ever shown it.
    """


class LoadFailure(HostFailure):
    """A load the page cannot have: a scheme a page may not use, a CORS answer it may
    not read, a transport that failed, a load with no loader to ask.

    Invisible by inheritance, because what a page hears for such a load is the
    browser's own wording -- a bare ``TypeError`` from ``fetch``, an ``error`` event on
    the element that asked -- and producing that is the web layer's to do, from this.
    """


class RequestTimeout(TimeoutError):
    """The framework's HTTP request deadline expired."""


__all__ = ["HostFailure", "HostStateError", "RealmBusyError", "LoadFailure", "RequestTimeout"]
