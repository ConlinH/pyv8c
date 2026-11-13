"""``window.chrome``, the object Chromium builds out of its own performance metrics.

Three details are mirrored from Chromium: the object is reused rather than replaced, ``app``
is a lazy accessor, and a timing object's fields are accessors with an empty setter, so a
write is ignored and a read keeps the value the call captured.  The values come from the
realm's timeline -- the records ``performance.timing`` reports -- as they do in Chromium.

The global is writable, enumerable and configurable, and its members are native
functions (``String(chrome.csi)`` reports native code).  It is built by :data:`_BOOTSTRAP`
in JavaScript rather than by a host class, so its prototype stays ``Object.prototype``.
"""
import math

from pyv8c import pyv8

from ...foundation.arguments import IdlArgumentError
from ...foundation.clock import clock_of
from ...foundation.tracing import silent_trace
from .performance import navigation_record

__all__ = ["ChromeBindings", "install_window_chrome"]


# CSI "tran" and ``loadTimes().navigationType``, from Chromium's GetCSITransitionType and
# GetNavigationType.
_TRANSITIONS = {"navigate": 15, "reload": 16, "back_forward": 6}
_TRANSITION_OTHER = 15
_NAVIGATION_TYPES = {"navigate": "Other", "reload": "Reload", "back_forward": "BackForward"}
#: The milestones nothing here measures (Chromium reads ResponseStart and FirstPaint) and the
#: offsets always reported for them; 0 until reached, as Blink's own metric reads.
_SYNTHETIC_OFFSETS = {
    "commitLoadTime": 22.5,
    "firstPaintTime": 62.1,
}
_LOAD_TIME_FIELDS = (
    "requestTime", "startLoadTime", "commitLoadTime", "finishDocumentLoadTime",
    "finishLoadTime", "firstPaintTime", "firstPaintAfterLoadTime",
    "navigationType", "wasFetchedViaSpdy", "wasNpnNegotiated",
    "npnNegotiatedProtocol", "wasAlternateProtocolAvailable", "connectionInfo",
)
_APP_FUNCTIONS = (
    "getDetails", "getIsInstalled", "installState", "runningState",
)
#: What the extensions layer validates an ``app`` call against: the greatest number of
#: arguments the API's schema declares, and that schema's parameter list as the bindings
#: spell it in the message they report.  A call past the limit never reaches the
#: implementation -- the layer refuses it first, and the page sees a ``TypeError``.
_APP_MAX_ARGUMENTS = {
    "getDetails": (0, ""),
    "getIsInstalled": (0, ""),
    "installState": (1, "optional function callback"),
    "runningState": (0, ""),
}
# Temporary global handing the native functions to the bootstrap; the bootstrap
# deletes it in the same synchronous step.
_BRIDGE_GLOBAL = "__pyv8cChromeHost__"

_BOOTSTRAP = """
(() => {
    const host = globalThis.__BRIDGE__;
    // A field is an accessor with an empty setter: a write is ignored, a read answers the
    // snapshot the call took.
    const build = (values) => {
        const fields = {};
        for (const name of Object.keys(values)) {
            const value = values[name];
            Object.defineProperty(fields, name, {
                get() { return value; }, set() {}, enumerable: true, configurable: true,
            });
        }
        return fields;
    };
    // The object is created once and every later layer adds to what is there.
    let chrome = globalThis.chrome;
    if (!chrome || (typeof chrome !== 'object' && typeof chrome !== 'function')) {
        chrome = {};
        Object.defineProperty(globalThis, 'chrome', {
            value: chrome, writable: true, enumerable: true, configurable: true,
        });
    }
    chrome.loadTimes = host.loadTimes;
    chrome.csi = host.csi;
    // ``app`` is a lazy accessor, as the extensions layer installs it.
    Object.defineProperty(chrome, 'app', {
        configurable: true, enumerable: true,
        get() {
            const app = {
                isInstalled: false,
                getDetails: host.getDetails,
                getIsInstalled: host.getIsInstalled,
                installState: host.installState,
                runningState: host.runningState,
                InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'},
                RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'},
            };
            Object.defineProperty(chrome, 'app', {
                value: app, writable: true, enumerable: true, configurable: true,
            });
            return app;
        },
    });
    delete globalThis.__BRIDGE__;
    return build;
})();
""".replace("__BRIDGE__", _BRIDGE_GLOBAL)


class ChromeBindings:
    """Python side of the ``chrome`` object: it answers what the realm's timeline holds."""

    def __init__(self, services=None):
        self._services = services
        #: Built in the realm, so a timing object's fields are accessors; the values are ours.
        self._fields = None

    def bind_fields(self, builder):
        """Take the field builder the realm's bootstrap returned."""
        self._fields = builder

    def _now(self):
        """The document's origin, how far it has come, and its navigation record."""
        clock = clock_of(self._services)
        #: ``csi.startE`` is ``performance.timeOrigin`` exactly, fraction included.
        return clock.time_origin(), clock.now_ms(), (navigation_record(self._services) or {})

    def _milestone(self, record, name, origin, elapsed):
        """Epoch milliseconds of a milestone, or 0 while the document has not reached it.

        The record keeps offsets from the origin; one nothing measured falls back to the
        offset this object has always reported.
        """
        offset = record.get(name)
        if offset is None:
            offset = _SYNTHETIC_OFFSETS.get(name)
        if offset is None:
            return 0.0
        offset = float(offset)
        if offset > elapsed:
            return 0.0
        #: Read on the same whole-millisecond clock ``performance.timing`` uses.
        return float(math.floor(origin) + math.floor(offset))

    def loadTimes(self, *_arguments):
        origin, elapsed, record = self._now()
        #: The legacy whole-millisecond clock, so ``requestTime`` matches ``navigationStart``.
        origin = float(math.floor(origin))
        values = {
            "requestTime": origin,
            "startLoadTime": origin,
            "commitLoadTime": self._milestone(record, "responseStart", origin, elapsed),
            "finishDocumentLoadTime": self._milestone(
                record, "domContentLoadedEventEnd", origin, elapsed),
            "finishLoadTime": self._milestone(record, "loadEventEnd", origin, elapsed),
            "firstPaintTime": self._milestone(record, "firstPaintTime", origin, elapsed),
            "firstPaintAfterLoadTime": 0.0,
            "navigationType": _NAVIGATION_TYPES.get(str(record.get("type")), "Other"),
            "wasFetchedViaSpdy": True,
            "wasNpnNegotiated": True,
            "npnNegotiatedProtocol": "h2",
            "wasAlternateProtocolAvailable": False,
            "connectionInfo": "h2",
        }
        return self._fields({field: values[field] for field in _LOAD_TIME_FIELDS})

    def csi(self, *_arguments):
        origin, elapsed, record = self._now()
        return self._fields({
            "startE": origin,
            "onloadT": self._milestone(record, "domContentLoadedEventEnd", origin, elapsed),
            "pageT": elapsed,
            "tran": _TRANSITIONS.get(str(record.get("type")), _TRANSITION_OTHER),
        })

    # Chromium's legacy metrics use anonymous, constructible FunctionTemplates.
    # Declare their JS identity without renaming the Python methods.
    loadTimes.__v8_idl_spec__ = pyv8.IdlSpec(name="", constructor_behavior=1)
    csi.__v8_idl_spec__ = pyv8.IdlSpec(name="", constructor_behavior=1)

    def _app_call(self, name, arguments):
        limit, parameters = _APP_MAX_ARGUMENTS[name]
        if len(arguments) > limit:
            raise IdlArgumentError(f"Error in invocation of app.{name}({parameters}): ")

    def getDetails(self, *arguments):
        self._app_call("getDetails", arguments)
        return pyv8.Null

    def getIsInstalled(self, *arguments):
        self._app_call("getIsInstalled", arguments)
        return False

    def installState(self, *arguments):
        self._app_call("installState", arguments)
        return "not_installed"

    def runningState(self, *arguments):
        self._app_call("runningState", arguments)
        return "cannot_run"


def install_window_chrome(context, services=None):
    """Install ``window.chrome`` for one Window realm, before any page script as
    Chromium does; worker globals do not get one.
    """
    bindings = ChromeBindings(services)
    bridge = {name: getattr(bindings, name) for name in ("loadTimes", "csi", *_APP_FUNCTIONS)}
    # Installing the object is host work: no trace event may be reported for the
    # bridge global or the descriptor, matching Chromium's own V8 extras.
    with silent_trace(context):
        context.glob[_BRIDGE_GLOBAL] = bridge
        builder = context.exec_js(_BOOTSTRAP, filename="chrome.js")
    bindings.bind_fields(builder)
