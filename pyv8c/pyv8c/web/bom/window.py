"""Window and the named-properties prototype it inherits.

A frame's window is the object another realm is handed, whatever the origins are; its
own access check decides what a cross-origin caller may reach.
"""
from pyv8c import pyv8


from ...features.runtime import LazyValueStore
from ...idl import interface
from ...foundation.arguments import (
    MISSING,
    dictionary,
    dom_string,
    event_handler,
    is_nullish,
    missing_error,
    usv_string,
)
from ...foundation.environment import BrowserEnvironment
from ..page_errors import DOMException, LegacyPropertyError, SecurityError, PageTypeError
from ..page_errors import js_error_value
from ...foundation.urls import origin_tuple, serialize_origin
from ...foundation.environment import environment_of
from ..dom.custom_elements import CustomElementRegistry
from ..dom.events import EventTarget
from ..dom.fonts import font_entries
from ..fetch.cache import CacheStorage
from ..fetch.request import Request
from ..fetch.response import Response, filtered_network_response
from .accessories import (
    CrashReportContext,
    External,
    LaunchQueue,
    Scheduler,
    SpeechSynthesis,
    StyleMedia,
    Viewport,
)
from .cookies import CookieStore
from .crypto import Crypto
from .media import MediaQueryList
from .navigation import Navigation
from .trusted_types import TrustedTypePolicyFactory
from .visual_viewport import VisualViewport
from .performance import Performance
from .picture_in_picture import DocumentPictureInPicture
from .scheduling import _SchedulingMixin
from .utils import deny_cross_origin, window_secure_context

__all__ = ["BarProp", "Window", "WindowProperties"]


#: The names a cross-origin caller may reach; the window's own access check answers.
_PROXY_SAFE_NAMES = frozenset((
    "window", "self", "top", "parent", "opener", "frames", "length", "closed",
    "location", "close", "focus", "blur", "postMessage",
))


@interface('WindowProperties', 'EventTarget')
class WindowProperties(EventTarget):
    """The named-properties object between Window and EventTarget prototypes.

    Its callbacks run against the owning Window and use a separate callback
    family from Window's own named interceptors.
    """
    __idl_overrides__ = {
        "interface": {
            #: This is the object a page's bare names resolve through.
            "named_properties_object": True,
            "length": 0,
        }
    }

    _NAMED_TAGS = frozenset(("embed", "form", "img", "object"))

    def _named_elements(self, name):
        document = self._services.document_services.document
        return [node for node in document.fn_querySelectorAll("*")
                if node.attributes.get("id") == name or (
                    node.localName in self._NAMED_TAGS and node.attributes.get("name") == name)]

    def __v8_property_name_get__(self, name):
        if not isinstance(name, str) or not name:
            return None
        self._require_same_origin()
        document = self._services.document_services.document
        # Child browsing-context names take precedence over matching elements.
        for frame in document.fn_querySelectorAll("iframe"):
            if frame.attributes.get("name") == name and frame._child_services is not None:
                return Window._accessor_window(self)._view(frame._child_services.window)
        matches = self._named_elements(name)
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        from ..dom.collections import HTMLCollection
        return HTMLCollection(owner=document, getter=lambda: self._named_elements(name))

    def __v8_property_name_query__(self, name):
        return 0 if self.__v8_property_name_get__(name) is not None else None

    def __v8_property_name_enum__(self):
        # Named-properties objects have no own keys, despite synthesized
        # [[GetOwnProperty]] descriptors for supported names.
        return []


@interface('BarProp', None)
class BarProp:
    """One of a window's bars: only ``visible`` is observable, and always true."""

    def __init__(self, name="", visible=True, **_):
        self._name, self._visible = str(name), bool(visible)

    def get_visible(self):
        return self._visible


class _WindowFeatureValues(LazyValueStore):
    """The Window's feature slots, built by their Feature on first access.

    A slot name routes to :meth:`FeatureRuntime.create_window_value` and the
    result is cached, so construction stays in the Feature and identity per Window.
    """

    __slots__ = ("_runtime", "_window")

    def __init__(self, runtime, window):
        super().__init__()
        self._runtime, self._window = runtime, window

    def _create_value(self, slot):
        return self._runtime.create_window_value(slot, self._window)


#: The window's own bar properties, one name each.
BAR_NAMES = ("locationbar", "menubar", "toolbar", "statusbar", "scrollbars", "personalbar")


def _singleton_values(window):
    """The ``[SameObject]`` objects a page reads off the window itself."""
    def bar(name):
        return lambda: BarProp(name)

    return LazyValueStore({
        "performance": lambda: Performance(services=window._services),
        "crypto": lambda: Crypto(window_secure_context(window),
                                 services=window._services),
        "caches": lambda: CacheStorage(services=window._services),
        "customElements": lambda: CustomElementRegistry(services=window._services),
        "visualViewport": lambda: VisualViewport(window=window),
        "viewport": Viewport,
        "external": External,
        "crashReport": CrashReportContext,
        "launchQueue": LaunchQueue,
        "speechSynthesis": lambda: SpeechSynthesis(services=window._services),
        "documentPictureInPicture": lambda: DocumentPictureInPicture(
            services=window._services),
        "styleMedia": lambda: StyleMedia(window),
        "scheduler": lambda: Scheduler(services=window._services),
        "cookieStore": lambda: CookieStore(services=window._services),
        "navigation": lambda: Navigation(window=window),
        "trustedTypes": TrustedTypePolicyFactory,
        **{name: bar(name) for name in BAR_NAMES},
    })


def _fetch_failure(error):
    """What a page's ``fetch`` rejects with: a bare ``TypeError``, as Chrome answers.

    A load the page cannot have -- a scheme it may not use, a CORS answer it may not
    read, a transport that failed -- is the host's own outcome, so the page hears what a
    browser tells it and the reason stays in the log.
    """
    from ...foundation.error_policy import is_host_failure

    if is_host_failure(error) or (isinstance(error, DOMException)
                                  and error.get_name() == "NetworkError"):
        from ...foundation.logging import logger

        # The reason stays in the log; the page hears only Chrome's wording.
        logger.debug("fetch failed: {}", error)
        return PageTypeError("Failed to fetch")
    return error


def fetch_like(services, interface_name, request, options, initiator_url=None):
    """``fetch`` as a window and a worker global both run it: a promise either way.

    The two globals differ only in the interface they name in an argument error, so the steps
    live here rather than in either of them -- a worker whose ``fetch`` were missing entirely
    is a realm the page cannot be told about, which is what it used to be.

    The realm's resource capability owns any cross-thread handoff. *initiator_url*
    is the worker's own URL, so the request the load builds is attributed to
    the realm that asked rather than to the page that owns it.
    """
    realm = services.realm_services
    loader = services.resource_services
    promise, resolve, reject = realm.make_promise()
    try:
        if request is MISSING:
            raise missing_error(interface_name, "fetch", 1, 0)
        if not isinstance(request, Request):
            request = usv_string(request)
        options = dictionary(
            options, name="RequestInit",
            prefix=f"Failed to execute 'fetch' on '{interface_name}': ")
        req = Request(request, options, services=services)
    except BaseException as exc:
        realm.post_task(lambda exc=exc: reject(js_error_value(exc)))
        return promise
    if req._signal is not None and req._signal.get_aborted():
        reason = req._signal._reason
        reject(js_error_value(reason) if isinstance(reason, BaseException) else reason)
        return promise
    def deliver(response, error):
        """Hand the answer to the realm that asked, which is not always the one that built it.

        A response carries the realm services of whoever built it, and a worker reads its own
        isolate: a value built there comes back as "belongs to another isolate".  The body
        crosses as bytes, and the object is rebuilt where it is read.
        """
        if error is not None:
            reject(js_error_value(_fetch_failure(error)))
            return
        redirect_crossed = getattr(response, '_redirect_crossed', False)
        if services is not getattr(response, "_services", None):
            try:
                response = Response(response.body, {
                    "headers": response.headers, "status": response.status,
                    "statusText": response.status_text, "url": response.url,
                    "redirected": response.redirected, "type": response.type,
                }, services=services)
            except BaseException as exc:  # a rebuild that fails must be seen, not swallowed
                reject(js_error_value(exc))
                return
        resolve(filtered_network_response(response, origin=realm.origin, initial_url=req.get_url(),
                                          credentials=req._credentials,
                                          redirect_crossed=redirect_crossed))

    handle = loader.load_resource_async(req.get_url(), {
        "method": req.get_method(), "headers": req.get_headers(), "body": req._body,
        "timeout": req._timeout, "credentials": req._credentials, "mode": req._mode,
        "redirect": req._redirect, "resource_type": "fetch",
        "initiator_url": initiator_url,
    }, deliver)
    #: A fetch the caller aborted: drop the load and reject with the reason.
    def abort():
        handle.cancel()
        reason = req._signal._reason
        if isinstance(reason, BaseException):
            reason = js_error_value(reason)
        reject(reason)

    if req._signal is not None:
        req._signal._subscribe(abort)
    return promise


@interface('Window', 'WindowProperties')
class Window(_SchedulingMixin, WindowProperties):
    __idl_overrides__ = {
        "interface": {
            #: A window is the global object itself, which its own ``self`` reads back.
            "global_this": True,
            "cross_origin_check": "__v8_cross_origin_check__",
            "cross_origin_index_del": "__v8_cross_origin_index_del__",
            "cross_origin_index_get": "__v8_cross_origin_index_get__",
            "cross_origin_index_set": "__v8_cross_origin_index_set__",
            "cross_origin_name_del": "__v8_cross_origin_name_del__",
            "cross_origin_name_enum": "__v8_cross_origin_name_enum__",
            "cross_origin_name_get": "__v8_cross_origin_name_get__",
            "cross_origin_name_query": "__v8_cross_origin_name_query__",
            "cross_origin_name_set": "__v8_cross_origin_name_set__",
            "index_del": "__v8_index_del__",
            "index_enum": "__v8_index_enum__",
            "index_get": "__v8_index_get__",
            "index_set": "__v8_index_set__",
            "length": 0
        }
    }
    # Ordinary members use Web IDL bindings; dynamic names live on
    # WindowProperties, not a generic Python interceptor on Window.
    _context_origins = {}
    def __init__(self, services=None, feature_runtime=None, **kw):
        super().__init__(services=services, **kw); self._services = services
        self._origin = getattr(services, "_origin", ("opaque", object()))
        self._location_cache = None
        #: Set for a window a frame owns; the element is what survives navigation.
        self._frame_element = None
        environment = getattr(getattr(services, "realm_services", None), "environment", None)
        self._environment = BrowserEnvironment.from_dict(environment)
        viewport = self._environment.viewport
        if feature_runtime is None:
            raise RuntimeError("Window requires the Browser FeatureRuntime")
        self._features = _WindowFeatureValues(feature_runtime, self)
        self._window_values = _singleton_values(self)
        self.location = self._features.value("location")
        self.innerWidth, self.innerHeight = viewport.width, viewport.height
        #: A mobile viewport has no window decorations around it.
        inset = (0, 0) if viewport.mobile else (viewport.outer_inset_width,
                                                viewport.outer_inset_height)
        self.outerWidth = viewport.width + inset[0]
        self.outerHeight = viewport.height + inset[1]
        self.devicePixelRatio = viewport.device_scale_factor

    @property
    def navigator(self):
        return self._features.value("navigator")

    def _viewport_dimension(self, axis):
        frame = self._frame_element
        if frame is None:
            return self._inner_width if axis == 'width' else self._inner_height
        from ..dom.layout import positioned_box
        box = positioned_box(frame)
        sides = ('left', 'right') if axis == 'width' else ('top', 'bottom')
        extent = box.width if axis == 'width' else box.height
        return max(0, int(round(extent - sum(box.border.get(side, 0) + box.padding.get(side, 0)
                                           for side in sides))))

    @property
    def innerWidth(self): return self._viewport_dimension('width')
    @innerWidth.setter
    def innerWidth(self, value): self._inner_width = value
    @property
    def innerHeight(self): return self._viewport_dimension('height')
    @innerHeight.setter
    def innerHeight(self, value): self._inner_height = value

    @property
    def screen(self):
        return self._features.value("screen")

    @property
    def localStorage(self):
        return self._features.value("localStorage")

    @property
    def sessionStorage(self):
        return self._features.value("sessionStorage")

    @property
    def history(self):
        return self._features.value("history")

    @property
    def indexedDB(self):
        return self._features.value("indexedDB")

    def sync_history_url(self, url):
        """Re-point the current history entry at a committed document URL.

        A navigation replaces the entry rather than pushing one, and a page that
        never reads ``history`` does not materialize one here.
        """
        if "history" in self._features.cached_names:
            self.history.replace_same_document(str(url))

    def __v8_cross_origin_check__(self, accessor):
        return accessor is self or getattr(accessor, "_origin", None) == self._origin

    def _event_target_view(self):
        # Another Realm must see the browsing context's WindowProxy, rather
        # than materializing a second wrapper for this Python Window.
        return getattr(self._services, "global_proxy", None) or self

    def _location_view(self):
        """The Location this window hands out: tied to the document it was made with,
        so a stale one refuses (see ``Location._require_active_window``)."""
        view = self._location_cache
        if view is None:
            from .navigation import Location
            view = self._location_cache = Location(
                href=self.location.href, services=self._services, window=self)
        return view

    def _cross_origin_member(self, name):
        """What a cross-origin caller reads for one of Blink's allowed names."""
        if name in ("window", "self", "frames"):
            return self._services.global_proxy or self
        if name == "top":
            return self._view(self._top_target())
        if name == "parent":
            return self._view(self._parent_target())
        if name == "location":
            return self._location_view()
        if name == "opener": return self.get_opener()
        if name == "length": return self.get_length()
        if name == "closed": return self.get_closed()
        if name in ("close", "focus", "blur", "postMessage"):
            return getattr(self, "fn_" + name)
        if name == "then": return pyv8.Undefined
        return None

    def __v8_cross_origin_denied__(self, accessor):
        """V8 refused an access before any handler saw it: raise what a browser reports."""
        deny_cross_origin()

    def __v8_cross_origin_name_get__(self, name):
        """One name a cross-origin caller reads: allowlisted, or refused."""
        if isinstance(name, str):
            value = self._cross_origin_member(name)
            if value is not None:
                return value
        deny_cross_origin()

    def __v8_cross_origin_name_set__(self, name, value):
        """What a cross-origin caller may assign: a navigation, and ``opener``."""
        if name == "location":
            self.set_location(value)
            return True
        if name == "opener":
            return True
        deny_cross_origin()

    def __v8_cross_origin_name_query__(self, name):
        if isinstance(name, str) and name in _PROXY_SAFE_NAMES:
            return pyv8.V8Attribute.kNone
        deny_cross_origin()

    def __v8_cross_origin_name_del__(self, name):
        deny_cross_origin()

    def __v8_cross_origin_name_enum__(self):
        return list(_PROXY_SAFE_NAMES)

    def __v8_cross_origin_index_get__(self, index):
        frames = self._frame_windows()
        if 0 <= index < len(frames):
            return self._view(frames[index])
        deny_cross_origin()

    def __v8_cross_origin_index_set__(self, index, value):
        deny_cross_origin()

    def __v8_cross_origin_index_del__(self, index):
        deny_cross_origin()

    @classmethod
    def _bind_context(cls, context, window):
        cls._context_origins[id(context)] = (window, window._origin)

    @classmethod
    def _unbind_context(cls, context):
        cls._context_origins.pop(id(context), None)

    @classmethod
    def _accessor_window(cls, fallback=None):
        entry = cls._context_origins.get(id(pyv8.current_context()))
        return entry[0] if entry is not None else fallback

    @classmethod
    def _calling_window(cls, fallback=None):
        """The caller's window: the incumbent context, not the one the call entered."""
        context = pyv8.incumbent_context()
        entry = cls._context_origins.get(id(context)) if context is not None else None
        return entry[0] if entry is not None else fallback

    def _accessor_origin(self):
        current = pyv8.current_context()
        entry = self._context_origins.get(id(current))
        return entry[0]._origin if entry is not None else self._origin

    def _same_origin(self):
        return self._accessor_origin() == self._origin

    def _require_same_origin(self):
        if not self._same_origin():
            deny_cross_origin()

    def get_window(self): return self
    def get_self(self): return self
    def get_parent(self): return self._view(self._parent_target())
    def get_top(self): return self._view(self._top_target())

    def _parent_target(self):
        parent = self._services.parent_window()
        return self if parent is None else parent

    def _top_target(self):
        top = self._services.top_window()
        return self if top is None else top

    def _view(self, target):
        """The object another realm sees for ``target``: that window itself, whose own
        access check decides what a cross-origin caller may reach."""
        if target is self:
            return self
        return getattr(target._services, "global_proxy", None) or target

    def __v8_index_get__(self, index):
        frames = self._frame_windows()
        if 0 <= index < len(frames):
            return self._view(frames[index])
        return None

    def __v8_index_set__(self, index, value):
        """A slot holds a live window: nothing may be assigned over it."""
        raise LegacyPropertyError.indexed(
            "set", index, "Window", "Indexed property setter is not supported."
        )

    def __v8_index_del__(self, index):
        """A frame slot refuses deletion; a slot holding nothing is already gone."""
        return not 0 <= index < len(self._frame_windows())

    def __v8_index_enum__(self):
        """The child frames, in the order their slots are indexed."""
        return len(self._frame_windows())

    def _frame_windows(self):
        return tuple(node._child_services.window
                     for node in self._services.document_services.document.fn_querySelectorAll("iframe")
                     if node._child_services is not None)

    def get_document(self): self._require_same_origin(); return self._services.document_services.document
    def get_frames(self): return self
    def get_length(self): return len(self._frame_windows())
    def get_closed(self): return bool(getattr(self._services, "closed", False))
    def get_opener(self): return pyv8.Null
    def get_onmessage(self): self._require_same_origin(); return self._get_handler("onmessage")
    def set_onmessage(self, value):
        if not is_nullish(value):
            value = event_handler(value)
        self._require_same_origin()
        self._set_handler('onmessage', value)
    def get_location(self):
        """This window's own ``Location``: the object itself, which the retired realm
        keeps answering for; a cross-origin caller is handed :meth:`_location_view`."""
        return self.location
    def set_location(self, value): self.location.set_href(value)
    def get_navigator(self): self._require_same_origin(); return self.navigator
    def get_clientInformation(self): self._require_same_origin(); return self.navigator
    def get_screen(self): self._require_same_origin(); return self.screen
    def get_localStorage(self): self._require_same_origin(); return self.localStorage
    def get_sessionStorage(self): self._require_same_origin(); return self.sessionStorage
    def get_history(self): self._require_same_origin(); return self.history
    def get_indexedDB(self): self._require_same_origin(); return self.indexedDB
    def get_innerWidth(self): self._require_same_origin(); return self.innerWidth
    def get_innerHeight(self): self._require_same_origin(); return self.innerHeight
    def get_outerWidth(self): self._require_same_origin(); return self.outerWidth
    def get_outerHeight(self): self._require_same_origin(); return self.outerHeight
    def get_devicePixelRatio(self): self._require_same_origin(); return self.devicePixelRatio
    def get_offscreenBuffering(self):
        """Legacy; a browser answers true."""
        return True
    def get_name(self):
        """The name a page gave this window; empty until it sets one."""
        return str(getattr(self, "_window_name", "") or "")
    def set_name(self, value):
        self._require_same_origin()
        self._window_name = str(value)
    def _publish_name(self):
        """Assign the name over the one a fresh global object already carries."""
        self._services.realm_services.window_property("set", "name", self.get_name())
    def _window_value(self, name):
        """The one object this window hands out for a ``[SameObject]`` member."""
        value = self._window_values.value(name)
        if self._services is not None:
            self._services.realm_services.retain_object(value)
        return value

    def get_origin(self):
        self._require_same_origin()
        return serialize_origin(self._origin)

    def get_scrollX(self):
        """How far the view has scrolled: nothing here scrolls one."""
        self._require_same_origin()
        return 0

    def get_scrollY(self): self._require_same_origin(); return 0
    def get_pageXOffset(self): return self.get_scrollX()
    def get_pageYOffset(self): return self.get_scrollY()

    def get_screenX(self):
        """The configured horizontal position of the browser window."""
        self._require_same_origin()
        return self._environment.viewport.screen_x

    def get_screenY(self):
        self._require_same_origin()
        return self._environment.viewport.screen_y
    def get_screenLeft(self): return self.get_screenX()
    def get_screenTop(self): return self.get_screenY()

    def get_isSecureContext(self):
        """Whether the document is in a secure context, the one that created it included.

        An ``about:blank`` or ``about:srcdoc`` document carries no scheme of its own, so a
        browser inherits the answer from its creator: an inline frame inside an ``https`` page
        is a secure context, and Chrome exposes ``navigator.userActivation`` and its thirty
        relatives there.  Judged by its own URL alone the frame would answer ``false``.
        """
        self._require_same_origin()
        return window_secure_context(self)

    def get_crossOriginIsolated(self):
        """Whether the document is cross-origin isolated: no COOP/COEP is set."""
        self._require_same_origin()
        return False

    def get_originAgentCluster(self): self._require_same_origin(); return False
    def get_credentialless(self): self._require_same_origin(); return False

    def get_status(self):
        """The window's status bar text, which starts empty and only a page sets."""
        self._require_same_origin()
        return getattr(self, "_status", "")

    def set_status(self, value):
        self._require_same_origin()
        self._status = dom_string(value)

    def get_frameElement(self):
        """The frame a child window lives in; a top-level window has none."""
        self._require_same_origin()
        parent = self._parent_target()
        if parent is self or parent._origin != self._accessor_origin():
            return pyv8.Null
        document = parent._services.document_services.document
        return next((frame for frame in document.fn_querySelectorAll("iframe")
                     if frame._child_services is not None
                     and frame._child_services.window is self), pyv8.Null)

    def get_performance(self):
        self._require_same_origin()
        return self._window_value("performance")

    def get_crypto(self):
        self._require_same_origin()
        return self._window_value("crypto")

    def get_customElements(self):
        self._require_same_origin()
        return self._window_value("customElements")

    def get_visualViewport(self):
        self._require_same_origin()
        return self._window_value("visualViewport")

    def get_viewport(self): self._require_same_origin(); return self._window_value("viewport")
    def get_external(self): self._require_same_origin(); return self._window_value("external")
    def get_crashReport(self): self._require_same_origin(); return self._window_value("crashReport")
    def get_launchQueue(self): self._require_same_origin(); return self._window_value("launchQueue")
    def get_speechSynthesis(self): self._require_same_origin(); return self._window_value("speechSynthesis")

    def get_documentPictureInPicture(self):
        """Picture-in-picture is secure-context only: an insecure page has none."""
        self._require_same_origin()
        if not window_secure_context(self):
            return pyv8.Undefined
        return self._window_value("documentPictureInPicture")

    def get_styleMedia(self):
        self._require_same_origin()
        return self._window_value("styleMedia")

    def get_scheduler(self):
        self._require_same_origin()
        return self._window_value("scheduler")

    def get_navigation(self):
        self._require_same_origin()
        return self._window_value("navigation")

    def get_trustedTypes(self):
        self._require_same_origin()
        return self._window_value("trustedTypes")

    def get_fence(self):
        """An unfenced page has no fence, which Chrome reports as null."""
        self._require_same_origin()
        return pyv8.Null

    #: Chrome knows these names and answers undefined -- they gate on a secure
    #: context, or are gone -- so the value is the same and the log is quiet.
    def get_event(self): self._require_same_origin(); return pyv8.Undefined
    def get_caches(self):
        """Cache Storage is secure-context only, so an insecure page has none."""
        self._require_same_origin()
        if not window_secure_context(self):
            return pyv8.Undefined
        return self._window_value("caches")
    def get_cookieStore(self):
        self._require_same_origin()
        if not window_secure_context(self):
            return pyv8.Undefined
        return self._window_value("cookieStore")

    def get_locationbar(self): self._require_same_origin(); return self._window_value("locationbar")
    def get_menubar(self): self._require_same_origin(); return self._window_value("menubar")
    def get_toolbar(self): self._require_same_origin(); return self._window_value("toolbar")
    def get_statusbar(self): self._require_same_origin(); return self._window_value("statusbar")
    def get_scrollbars(self): self._require_same_origin(); return self._window_value("scrollbars")
    def get_personalbar(self): self._require_same_origin(); return self._window_value("personalbar")

    def fn_matchMedia(self, query=MISSING, *_extra):
        """The query list for *query*, answered against this window's viewport."""
        if query is MISSING:
            raise missing_error('Window', 'matchMedia', 1, 0, constructor=False)
        self._require_same_origin()
        screen = self._environment.screen
        return MediaQueryList(dom_string(query), self.innerWidth, self.innerHeight,
                              screen_width=screen.width, screen_height=screen.height,
                              dpr=self.devicePixelRatio, color_depth=screen.color_depth)
    def fn_createImageBitmap(self, image=MISSING, *arguments):
        from ..graphics.canvas.bitmap import create_image_bitmap
        return create_image_bitmap('Window', image, arguments, getattr(self, '_services', None))

    def fn_fetch(self, request=MISSING, options=None, *_extra):
        return fetch_like(self._services, "Window", request, options)

    def fn_atob(self, data=MISSING, *_extra):
        from .utils import base64_like
        return base64_like('Window', 'atob', data)

    def fn_structuredClone(self, value=MISSING, options=None, *_extra):
        """``structuredClone``: the value serialized here and deserialized here.

        Chromium's own implementation is exactly that round trip -- ``SerializeMessageByMove``
        then ``Deserialize`` (``UniversalGlobalScope::structuredClone``) -- and the transfer
        list is applied between the two, which is what leaves the buffers it names detached on
        this side while their bytes travel in the wire.
        """
        from ..clone import clone_value
        return clone_value(self._services, 'Window', value, options)

    def fn_btoa(self, data=MISSING, *_extra):
        from .utils import base64_like
        return base64_like('Window', 'btoa', data)

    def fn_queryLocalFonts(self, options=None, *_extra):
        """The machine's fonts, behind the gates Chromium puts in front of them.

        The policy gate throws where Chromium throws; the answer the browser process would
        give rejects the promise instead.  A hidden page, a permission the user denied and
        a pending permission with no gesture behind it are three different answers, and the
        last is what a probe without a click receives -- the same refusal a real browser
        gave a page asking this way.  Granting ``local-fonts`` in the profile is how a page
        gets the list.
        """
        settings = {} if is_nullish(options) else dictionary(options)
        from ...foundation.arguments import sequence
        selected = settings.get("postscriptNames")
        if not is_nullish(selected):
            # The dictionary hands back live JavaScript values, so the list is read here.
            selected = tuple(str(name) for name in sequence(selected))
        else:
            selected = None
        document = self.get_document()
        policy = getattr(document, "_feature_policy", None)
        if policy is not None and not policy.fn_allowsFeature("local-fonts"):
            raise SecurityError('Access to the feature "local fonts" is disallowed by '
                                "permissions policy.")
        promise, resolve, reject = self._services.realm_services.make_promise()
        if getattr(document, "hidden", False):
            reject(js_error_value(SecurityError("Page needs to be visible.")))
            return promise
        state = self.get_navigator().get_permissions()._state("local-fonts")
        if state == "denied":
            # A denied permission is an empty list in Chromium, not a rejection.
            resolve([])
            return promise
        activation = self.get_navigator().get_userActivation()
        if state != "granted" and not getattr(activation, "get_hasBeenActive", bool)():
            reject(js_error_value(SecurityError("User activation is required.")))
            return promise
        resolve(font_entries(environment_of(self._services), selected))
        return promise

    def fn_close(self, *_extra): return None
    def fn_focus(self, *_extra): return None
    def fn_blur(self, *_extra): return None

    def fn_postMessage(self, message=MISSING, target_origin='/', transfer=MISSING, *_extra):
        if message is MISSING:
            raise missing_error('Window', 'postMessage', 1, 0, constructor=False)
        if target_origin is pyv8.Undefined:
            target_origin = '/'
        from ...foundation.arguments import sequence
        if transfer is not MISSING:
            target_origin = dom_string(target_origin)
            transfer = sequence(transfer)
        else:
            transfer = ()
        # The sender is the window that called, which need not be this one.
        self._post_message(Window._calling_window(self) or self, message, target_origin, transfer)

    def fn_webkitRequestFileSystem(self, type=MISSING, size=MISSING, success=MISSING, error=pyv8.Undefined, *_extra):
        if type is MISSING: raise missing_error("Window", "webkitRequestFileSystem", 4, 0, constructor=False)
        if size is MISSING: raise missing_error("Window", "webkitRequestFileSystem", 4, 1, constructor=False)
        if success is MISSING: raise missing_error("Window", "webkitRequestFileSystem", 4, 2, constructor=False)
        self._require_same_origin()
        # The API is an Optional Feature: loading it commits its interface group
        # to this realm before the first instance is handed back.
        self._services.realm_services.materialize_interface("DOMFileSystem")
        from .filesystem import request_file_system
        return request_file_system(self, type, size, success, error)

    def fn_requestIdleCallback(self, callback=MISSING, options=pyv8.Undefined, *_extra):
        """Run one idle callback later, with a real ``IdleDeadline``.

        The callback must never run synchronously: it goes through the same
        owner-task HTML timer queue as ``setTimeout``.  This member used to be
        an ``Undefined`` stub, so the SDK never received an idle deadline.
        """
        if callback is MISSING:
            raise missing_error("Window", "requestIdleCallback", 1, 0,
                                constructor=False)
        from ...foundation.arguments import callback_value, integer
        from .scheduling import IdleDeadline
        fn = callback_value(callback)
        settings = ({} if is_nullish(options) or options is MISSING
                    or not isinstance(options, (dict, pyv8.JSObject))
                    else dictionary(options))
        timeout = integer(settings.get('timeout', 0)) if 'timeout' in settings else None
        import time
        scheduled = time.monotonic()
        def run_idle():
            expired = timeout is not None and (time.monotonic() - scheduled) * 1000 >= timeout
            fn(IdleDeadline(did_timeout=expired, window_ms=0 if expired else 50))
        return self._services.task_services.schedule_html_timer(
            run_idle, 0.001, repeat=False)

    def fn_cancelIdleCallback(self, ident=MISSING, *_extra):
        from ...foundation.arguments import integer
        value = 0 if ident is MISSING else int(integer(ident, signed=True))
        self._services.task_services.cancel(value)

    def fn_getComputedStyle(self, element=MISSING, pseudo=pyv8.Undefined, *_extra):
        if element is MISSING:
            raise missing_error("Window", "getComputedStyle", 1, 0, constructor=False)
        from ..dom.element import Element
        if not isinstance(element, Element):
            raise TypeError("The provided value is not an Element")
        from ..css.cascade import computed_style_object
        from ..dom.layout import layout_snapshot
        document = getattr(element, "ownerDocument", None)
        # Nothing is rendered in a tree no window owns, and CSSOM answers empty
        # values for one: that is what a document built by ``DOMParser`` is.
        rendered = (element.isConnected and document is not None
                    and document.get_defaultView() is not pyv8.Null)
        return computed_style_object(element, rendered=rendered, layout_snapshot=layout_snapshot)

    def _post_message(self, source, message, target_origin="/", transfer=()):
        from ..page_errors import PageSyntaxError
        from ..clone import structured_clone, TransferPlan
        from ..fetch.events import MessageEvent

        # Null is an empty WindowPostMessageOptions dictionary.
        if target_origin is None or target_origin is pyv8.Null or target_origin is pyv8.Undefined:
            target_origin = {}
        if isinstance(target_origin, (dict, pyv8.JSObject)):
            from ...foundation.arguments import sequence
            options = dictionary(target_origin)
            target_origin = dom_string(options.get("targetOrigin", "/"))
            transfer = sequence(options.get("transfer", ()))
        target_origin = dom_string(target_origin)
        if target_origin == "/":
            expected_origin = source._origin
        elif target_origin == "*":
            expected_origin = None
        else:
            expected_origin = origin_tuple(target_origin)
            if expected_origin[0] == "opaque":
                raise PageSyntaxError("Invalid target origin in postMessage")
        # A transfer happens at the call, as Chrome does it: a port whose target
        # refuses the message is moved out of this realm all the same.
        #: The message is cloned before the list is applied, so a buffer the list is
        #: about to detach still carries its bytes; the sender's own context turns
        #: binary data into bytes rather than into an object with nothing enumerable on
        #: it, which is how an ArrayBuffer used to arrive as ``{}``.
        plan = TransferPlan(transfer)
        data = structured_clone(message, context=pyv8.current_context(), caller="Window", transfer=plan)
        ports = plan.commit(self._services)
        serialized = serialize_origin(source._origin)

        def deliver():
            if self.get_closed() or (expected_origin is not None and expected_origin != self._origin):
                return
            self._services.realm_services.materialize_interface("MessageEvent")
            event = MessageEvent("message", {"data": data, "origin": serialized,
                                           "source": self._view(source), "ports": ports,
                                           "isTrusted": True})
            self._dispatch_event(event)
        from ...foundation.tasks import TaskSource
        self._services.realm_services.post_task(deliver, TaskSource.POSTED_MESSAGE,
                                                label='Window.postMessage', document_bound=True)
