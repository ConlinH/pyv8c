"""``window.performance``: the clock, and the entries this document measured."""

import math

from pyv8c import pyv8
from pyv8c.pyv8 import AttributeSpec

from ...idl import interface
from ...foundation.arguments import MISSING, dictionary, dom_string, is_nullish, missing_error
from ...foundation.clock import clock_of
from ..page_errors import PageTypeError, dom_exception
from ..dom.events import EventTarget


#: The PerformanceTiming fields; only navigationStart is known here, the rest read 0.
_TIMING_FIELDS = (
    "navigationStart", "unloadEventStart", "unloadEventEnd", "redirectStart", "redirectEnd",
    "fetchStart", "domainLookupStart", "domainLookupEnd", "connectStart", "connectEnd",
    "secureConnectionStart", "requestStart", "responseStart", "responseEnd", "domLoading",
    "domInteractive", "domContentLoadedEventStart", "domContentLoadedEventEnd", "domComplete",
    "loadEventStart", "loadEventEnd",
)


def _attribute(name, getter=None, setter=None, side_effect=1):
    return AttributeSpec(name=name, getter=getter or "get_" + name, setter=setter,
                         side_effect=side_effect)


def _now(services=None):
    """Milliseconds since ``timeOrigin``, the unit an entry reports."""
    return clock_of(services).now_ms()


def _double(value, message):
    if isinstance(value, bool) or is_nullish(value):
        raise PageTypeError(message)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PageTypeError(message) from exc


# Chrome 155's Event Timing map contains these event types even before any input.
# Its order is observable through keys(), entries() and Symbol.iterator.
EVENT_COUNT_TYPES = (
    "pointerdown", "touchend", "input", "keydown", "mouseleave", "mouseenter",
    "drop", "beforeinput", "pointerenter", "dragend", "pointercancel",
    "compositionupdate", "mousedown", "dragleave", "dragover", "mouseup",
    "pointerover", "lostpointercapture", "mouseover", "gotpointercapture",
    "dblclick", "keyup", "keypress", "pointerup", "compositionstart", "auxclick",
    "dragstart", "touchstart", "compositionend", "pointerout", "dragenter",
    "touchcancel", "click", "contextmenu", "mouseout", "pointerleave",
)
EVENT_COUNT_NAMES = frozenset(EVENT_COUNT_TYPES)


@interface('EventCounts', None)
class EventCounts:
    """A live, read-only Event Timing map for one document timeline."""

    def __init__(self, counts=None, **_):
        self._counts = counts if counts is not None else {}

    def get_size(self): return len(EVENT_COUNT_TYPES)

    def fn_get(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('EventCounts', 'get', 1, 0, constructor=False)
        name = dom_string(name)
        return self._counts.get(name, 0) if name in EVENT_COUNT_NAMES else pyv8.Undefined

    def fn_has(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('EventCounts', 'has', 1, 0, constructor=False)
        return dom_string(name) in EVENT_COUNT_NAMES

    def __v8_iteration_source__(self):
        index = 0
        def next_pair():
            nonlocal index
            if index >= len(EVENT_COUNT_TYPES):
                return None
            name = EVENT_COUNT_TYPES[index]
            index += 1
            return name, self._counts.get(name, 0)
        return next_pair


@interface('PerformanceTiming', None)
class PerformanceTiming:
    """Navigation timings: when the clock started, and nothing measured after it."""

    def __init__(self, *_extra, services=pyv8.Undefined, _realm=None, **_):
        if services is pyv8.Undefined:
            services = None
        self._services = services or _realm
        self._clock = clock_of(self._services)

    def fn_toJSON(self, *_extra):
        return {field: getattr(self, 'get_' + field)() for field in _TIMING_FIELDS}


def timeline_of(services):
    """The realm's timeline, or ``None`` when these services own no realm."""
    realm = getattr(services, "realm_services", services)
    return getattr(realm, "performance", None)


def navigation_record(services):
    """The record this document's navigation was measured into, if there is one.

    Shared: the timings below and ``window.chrome`` read the same numbers, as they do in
    Chromium.
    """
    timeline = timeline_of(services)
    return None if timeline is None else timeline.navigation()


def _timing_reader(field):
    def read(self):
        #: Legacy ``PerformanceTiming`` is whole epoch milliseconds; a field it lacks
        #: was never reached and reads 0.
        origin = math.floor(self._clock.time_origin())
        if field == "navigationStart":
            return float(origin)
        record = navigation_record(self._services)
        if record is None or field not in record:
            return 0.0
        if field in ('redirectStart', 'redirectEnd') and not record.get('redirectCount', 0):
            return 0.0
        return float(origin + math.floor(float(record[field])))
    return read

for _field in _TIMING_FIELDS:
    setattr(PerformanceTiming, "get_" + _field, _timing_reader(_field))


@interface('PerformanceNavigation', None)
class PerformanceNavigation:
    """This navigation: a normal load, with no redirect behind it."""

    TYPE_NAVIGATE, TYPE_RELOAD, TYPE_BACK_FORWARD, TYPE_RESERVED = 0, 1, 2, 255

    def __init__(self, *_extra, services=pyv8.Undefined, _realm=None, **_):
        if services is pyv8.Undefined:
            services = None
        self._services = services or _realm

    def get_type(self): return self.TYPE_NAVIGATE

    def get_redirectCount(self):
        record = navigation_record(self._services)
        return int(record.get("redirectCount", 0)) if record is not None else 0

    def fn_toJSON(self, *_extra):
        return {'type': self.get_type(), 'redirectCount': self.get_redirectCount()}


@interface('MemoryInfo', None)
class MemoryInfo:
    def __init__(self, *_extra, services=pyv8.Undefined, _realm=None, **_):
        if services is pyv8.Undefined:
            services = None
        self._services = services or _realm
        self._snapshot = (self._services.realm_services.memory_snapshot()
                          if self._services is not None else {})

    def _statistics(self):
        return self._snapshot

    def get_jsHeapSizeLimit(self): return self._statistics().get("limit", 0)
    def get_totalJSHeapSize(self): return self._statistics().get("total", 0)
    def get_usedJSHeapSize(self): return self._statistics().get("used", 0)


#: Resource timing fields with the value a phase takes when it never happened: the
#: specification has a skipped phase read as ``fetchStart`` (or 0, for a secure one).
_RESOURCE_DEFAULTS = {
    "initiatorType": "",
    "nextHopProtocol": "http/1.1",
    "deliveryType": "",
    "workerStart": 0.0,
    "redirectStart": 0.0,
    "redirectEnd": 0.0,
    "fetchStart": 0.0,
    "domainLookupStart": 0.0,
    "domainLookupEnd": 0.0,
    "connectStart": 0.0,
    "connectEnd": 0.0,
    "secureConnectionStart": 0.0,
    "requestStart": 0.0,
    "responseStart": 0.0,
    "responseEnd": 0.0,
    "transferSize": 0.0,
    "encodedBodySize": 0.0,
    "decodedBodySize": 0.0,
    "responseStatus": 0.0,
    "contentType": "",
    "contentEncoding": "",
    "renderBlockingStatus": "non-blocking",
    "finalResponseHeadersStart": 0.0,
    "firstInterimResponseStart": 0.0,
    "workerCacheLookupStart": 0.0,
    "workerMatchedSourceType": "",
    "workerFinalSourceType": "",
    "workerRouterEvaluationStart": 0.0,
    "serverTiming": (),
}


#: What a navigation adds: the milestones this document reached.
_NAVIGATION_DEFAULTS = {
    "unloadEventStart": 0.0,
    "unloadEventEnd": 0.0,
    "domInteractive": 0.0,
    "domContentLoadedEventStart": 0.0,
    "domContentLoadedEventEnd": 0.0,
    "domComplete": 0.0,
    "loadEventStart": 0.0,
    "loadEventEnd": 0.0,
    "type": "navigate",
    "redirectCount": 0,
    "criticalCHRestart": 0.0,
    "activationStart": 0.0,
    "confidence": "low",
    "notRestoredReasons": pyv8.Null,
}


@interface('PerformanceEntry', None)
class PerformanceEntry:
    """One measurement: what it was called, which kind it is, and when it happened."""

    #: The extra attributes this kind of entry reports.
    fields = ()

    #: Which of those ``toJSON`` writes: Chrome serializes the timing fields only, so a
    #: mark reports its name and nothing else.
    serialized = ()

    def __init__(self, name="", entry_type="", start_time=0.0, duration=0.0,
                 navigation_id=1, values=None, **_):
        self._name = str(name)
        self._entry_type = str(entry_type)
        self._start_time = float(start_time)
        self._duration = float(duration)
        self._navigation_id = int(navigation_id)
        self._values = dict(values or {})
        #: Where this entry falls in the buffer: an observer delivers in this order.
        self._sequence = 0

    def get_name(self): return self._name
    def get_entryType(self): return self._entry_type
    def get_startTime(self): return self._start_time
    def get_duration(self): return self._duration

    def _value(self, field):
        if field in self._values:
            return self._values[field]
        return self._defaults().get(field, 0.0)

    def _defaults(self):
        return {}

    def fn_toJSON(self, *_extra):
        reported = {"name": self._name, "entryType": self._entry_type,
                    "startTime": self._start_time, "duration": self._duration,
                    "navigationId": self._navigation_id}
        for field in self.serialized:
            reported[field] = self._value(field)
        return reported


def _field_reader(field):
    def read(self):
        return self._value(field)
    return read


@interface('VisibilityStateEntry', 'PerformanceEntry')
class VisibilityStateEntry(PerformanceEntry):
    """A document visibility transition in the performance timeline."""


@interface('PerformanceMark', 'PerformanceEntry')
class PerformanceMark(PerformanceEntry):
    """A mark a page made, with whatever detail it attached."""

    fields = ("detail",)

    #: A mark serializes like the base entry: ``detail`` is not part of it.
    serialized = ()

    def __init__(self, name=MISSING, options=None, *, startTime=None, detail=pyv8.Undefined,
                 navigation_id=1, services=None, **_):
        message = "Failed to construct 'PerformanceMark': "
        if name is MISSING:
            raise missing_error('PerformanceMark', 'constructor', 1, 0, constructor=True)
        name = dom_string(name)
        if not is_nullish(options):
            options = dictionary(options, name="PerformanceMarkOptions", prefix=message)
            if startTime is None:
                startTime = options.get("startTime", pyv8.Undefined)
            if detail is pyv8.Undefined:
                detail = options.get("detail", pyv8.Undefined)
        started = _now(services) if is_nullish(startTime) else _double(
            startTime, message + "The provided value is not of type 'double'.")
        super().__init__(name, "mark", started, 0.0, navigation_id, {"detail": detail})

    def get_detail(self): return self._value("detail")


@interface('PerformanceMeasure', 'PerformanceEntry')
class PerformanceMeasure(PerformanceEntry):
    """The span between two marks, or between a mark and now."""

    fields = ("detail",)

    #: A measure serializes like the base entry: ``detail`` is not part of it.
    serialized = ()

    def __init__(self, name="", start_time=0.0, duration=0.0, detail=pyv8.Undefined,
                 navigation_id=1, **_):
        super().__init__(name, "measure", start_time, duration, navigation_id,
                         {"detail": detail})

    def get_detail(self): return self._value("detail")


@interface('PerformanceResourceTiming', 'PerformanceEntry')
class PerformanceResourceTiming(PerformanceEntry):
    """A resource the document fetched, timed from its request to its last byte."""

    fields = tuple(_RESOURCE_DEFAULTS)
    serialized = tuple(_RESOURCE_DEFAULTS)

    def _defaults(self):
        return _RESOURCE_DEFAULTS


@interface('PerformanceNavigationTiming', 'PerformanceResourceTiming')
class PerformanceNavigationTiming(PerformanceResourceTiming):
    """The document itself: its milestones, and the fetch that brought it here."""

    fields = tuple(_RESOURCE_DEFAULTS) + tuple(_NAVIGATION_DEFAULTS)
    serialized = tuple(_RESOURCE_DEFAULTS) + tuple(_NAVIGATION_DEFAULTS)

    def _defaults(self):
        return {**_RESOURCE_DEFAULTS, **_NAVIGATION_DEFAULTS}

for _entry_class, _entry_fields in ((PerformanceResourceTiming, _RESOURCE_DEFAULTS),
                                    (PerformanceNavigationTiming, _NAVIGATION_DEFAULTS)):
    for _entry_field in _entry_fields:
        setattr(_entry_class, "get_" + _entry_field, _field_reader(_entry_field))


def _filtered(entries, name, entry_type):
    """The entries matching a name and a type, in the order Chrome reports them."""
    if is_nullish(name) or name is None:
        name = None
    else:
        name = dom_string(name)
    kind = None if is_nullish(entry_type) or entry_type is None else dom_string(entry_type)
    matched = [entry for entry in entries
               if (name is None or entry.get_name() == name)
               and (kind is None or entry.get_entryType() == kind)]
    return sorted(matched, key=lambda entry: entry.get_startTime())


@interface('PerformanceObserverEntryList', None)
class PerformanceObserverEntryList:
    """The entries one observer callback was handed."""

    def __init__(self, entries=(), **_):
        self._entries = tuple(entries)

    def fn_getEntries(self, *_extra):
        return list(self._entries)

    def fn_getEntriesByName(self, name=MISSING, type_=pyv8.Undefined, *_extra):
        if name is MISSING:
            raise missing_error('PerformanceObserverEntryList', 'getEntriesByName', 1, 0,
                                constructor=False)
        return _filtered(self._entries, name, type_)

    def fn_getEntriesByType(self, type_=MISSING, *_extra):
        if type_ is MISSING:
            raise missing_error('PerformanceObserverEntryList', 'getEntriesByType', 1, 0,
                                constructor=False)
        return _filtered(self._entries, None, type_)


@interface('Performance', 'EventTarget')
class Performance(EventTarget):
    #: The navigation id every entry reports.  Private: a bare class attribute becomes page
    #: surface, and Chrome's ``performance`` has no ``navigation_id``.
    _navigation_id = 1

    def __init__(self, *_extra, services=pyv8.Undefined, _realm=None, **_):
        resolved_services = _realm if services is pyv8.Undefined else (services or _realm)
        super().__init__(services=resolved_services)
        self._entries = []
        self._record_entries = {}
        self._observers = []
        self._delivery_posted = False
        self._next_sequence = 1
        self._resource_buffer_size = 250

    def _stamp(self, entry):
        """Number an entry, so an observer can tell what it has already delivered."""
        entry._sequence = self._next_sequence
        self._next_sequence += 1
        return entry

    def get_timeOrigin(self):
        """Epoch milliseconds at high resolution: Chrome reports a fraction here too."""
        return clock_of(self._services).time_origin()

    def get_timing(self):
        if getattr(self, "_timing", None) is None:
            self._timing = PerformanceTiming(services=self._services)
        return self._timing

    def get_navigation(self):
        if getattr(self, "_navigation", None) is None:
            self._navigation = PerformanceNavigation(services=self._services)
        return self._navigation

    def get_memory(self):
        # Chrome hands out a fresh MemoryInfo for every read.
        from .window import Window
        window = Window._accessor_window()
        return MemoryInfo(services=getattr(window, "_services", None))

    def get_eventCounts(self):
        view = getattr(self, "_event_counts", None)
        if view is None:
            timeline = self._timeline()
            view = self._event_counts = EventCounts(
                None if timeline is None else timeline.event_counts)
        return view

    # -- the timeline the platform keeps ------------------------------------------

    def _timeline(self):
        """The realm's timing records, or ``None`` when this instance has no realm."""
        services = self._services
        realm = getattr(services, "realm_services", services)
        return getattr(realm, "performance", None)

    def _timed_entries(self):
        timeline = self._timeline()
        if timeline is None:
            return []
        built = []
        for record in timeline.records():
            key = id(record)
            cached = self._record_entries.get(key)
            if cached is not None and cached[0] is record:
                built.append(cached[1])
                continue
            entry = self._stamp(self._entry_from_record(record))
            self._record_entries[key] = (record, entry)
            built.append(entry)
        return built

    def _entry_from_record(self, record):
        if record.get("kind") == "navigation":
            return self._navigation_entry(record)
        if record.get("kind") == "visibility-state":
            return VisibilityStateEntry(record.get("name", "visible"), "visibility-state",
                                        record.get("start", 0.0), 0.0, self._navigation_id)
        start = float(record.get("start", 0.0))
        end = float(record.get("end", record.get("start", 0.0)))
        size = record.get("decodedBodySize", 0)
        initiator = str(record.get("initiator", ""))
        values = {
            "initiatorType": initiator,
            "fetchStart": start,
            "domainLookupStart": start,
            "domainLookupEnd": start,
            "connectStart": start,
            "connectEnd": start,
            "requestStart": start,
            "responseStart": end,
            "responseEnd": end,
            "finalResponseHeadersStart": end,
            "transferSize": size,
            "encodedBodySize": size,
            "decodedBodySize": size,
            "contentType": str(record.get("contentType", "")),
            "responseStatus": record.get("responseStatus", 0),
            "serverTiming": [],
            "renderBlockingStatus": "blocking" if initiator in ("script", "style") else "non-blocking",
        }
        return PerformanceResourceTiming(record.get("name", ""), "resource", start,
                                        end - start, self._navigation_id, values)

    def _navigation_entry(self, record):
        start = float(record.get("start", 0.0))
        values = {field: float(record[field]) for field in _NAVIGATION_DEFAULTS
                  if field in record and isinstance(_NAVIGATION_DEFAULTS[field], float)}
        for field in ("type", "redirectCount", "confidence", "notRestoredReasons"):
            if field in record:
                values[field] = record[field]
        ended = values.get("loadEventEnd") or 0.0
        values.setdefault("fetchStart", start)
        values.setdefault("requestStart", start)
        # The document is its own resource, and says so.
        values.setdefault("initiatorType", "navigation")
        return PerformanceNavigationTiming(record.get("name", ""), "navigation", start,
                                           max(ended - start, 0.0), self._navigation_id, values)

    def _all_entries(self):
        """Every entry of this document: what the page measured, then what the realm did."""
        return self._entries + self._timed_entries()

    # -- what the page measured ---------------------------------------------------

    def fn_getEntries(self, *_extra):
        return _filtered(self._all_entries(), None, None)

    def fn_getEntriesByName(self, name=MISSING, type_=pyv8.Undefined, *_extra):
        if name is MISSING:
            raise missing_error('Performance', 'getEntriesByName', 1, 0, constructor=False)
        return _filtered(self._all_entries(), name, type_)

    def fn_getEntriesByType(self, type_=MISSING, *_extra):
        if type_ is MISSING:
            raise missing_error('Performance', 'getEntriesByType', 1, 0, constructor=False)
        return _filtered(self._all_entries(), None, type_)

    def fn_now(self, *_extra):
        return clock_of(self._services).now_ms()

    def fn_mark(self, name=MISSING, options=pyv8.Undefined, *_extra):
        if name is MISSING:
            raise missing_error('Performance', 'mark', 1, 0, constructor=False)
        entry = self._stamp(PerformanceMark(name, options, navigation_id=self._navigation_id))
        self._entries.append(entry)
        self._offer((entry,))
        # Chrome hands the entry back, where the specification says undefined.
        return entry

    def fn_measure(self, name=MISSING, start_or_options=pyv8.Undefined, end=pyv8.Undefined,
                   *_extra):
        message = "Failed to execute 'measure' on 'Performance': "
        if name is MISSING:
            raise missing_error('Performance', 'measure', 1, 0, constructor=False)
        name = dom_string(name)
        timing = "The provided value is not of type 'double'."
        if isinstance(start_or_options, (str, int, float)) or is_nullish(start_or_options):
            start_option, end_option, duration_option, detail = (
                start_or_options, end, pyv8.Undefined, pyv8.Undefined)
        else:
            options = dictionary(start_or_options, name="PerformanceMeasureOptions",
                                 prefix=message)
            start_option = options.get("start", pyv8.Undefined)
            end_option = options.get("end", pyv8.Undefined)
            duration_option = options.get("duration", pyv8.Undefined)
            detail = options.get("detail", pyv8.Undefined)
        started = self._measure_instant(start_option, message, 0.0)
        if not is_nullish(duration_option):
            given = _double(duration_option, message + timing)
            finished = started + given
            if is_nullish(start_option) and not is_nullish(end_option):
                # ``{end, duration}`` counts back from the end.
                started = self._measure_instant(end_option, message, 0.0) - given
                finished = started + given
        else:
            finished = self._measure_instant(end_option, message, _now(self._services))
        entry = self._stamp(PerformanceMeasure(name, started, max(finished - started, 0.0),
                                              detail, self._navigation_id))
        self._entries.append(entry)
        self._offer((entry,))
        # Chrome hands the entry back, where the specification says undefined.
        return entry

    def _measure_instant(self, value, message, default):
        """When a measure's start or end reads: a mark's time, a number, or the default."""
        if is_nullish(value):
            return default
        if isinstance(value, str):
            mark = next((entry for entry in self._entries
                         if entry.get_entryType() == "mark" and entry.get_name() == value), None)
            if mark is None:
                raise dom_exception(
                    "SyntaxError",
                    message + f"The mark '{value}' does not exist.")
            return mark.get_startTime()
        return _double(value, message + "The provided value is not of type 'double'.")

    def fn_clearMarks(self, name=pyv8.Undefined, *_extra):
        self._clear("mark", name)

    def fn_clearMeasures(self, name=pyv8.Undefined, *_extra):
        self._clear("measure", name)

    def _clear(self, entry_type, name):
        """Drop every entry of one kind, or only those a name picks out."""
        telling = not is_nullish(name)
        wanted = dom_string(name) if telling else ""
        self._entries = [entry for entry in self._entries
                         if entry.get_entryType() != entry_type
                         or (telling and entry.get_name() != wanted)]

    def fn_clearResourceTimings(self, *_extra):
        timeline = self._timeline()
        if timeline is not None:
            timeline.clear_resources()
        self._record_entries = {key: held for key, held in self._record_entries.items()
                                if held[1].get_entryType() != "resource"}

    def fn_setResourceTimingBufferSize(self, size=MISSING, *_extra):
        if size is MISSING:
            raise missing_error('Performance', 'setResourceTimingBufferSize', 1, 0,
                                constructor=False)
        self._resource_buffer_size = int(_double(
            size, "Failed to execute 'setResourceTimingBufferSize' on 'Performance': "
                  "The provided value is not of type 'unsigned long'."))

    # -- delivery -----------------------------------------------------------------

    def _offer(self, entries):
        """Hand the entries that just appeared to the observers that want them."""
        if not self._observers:
            return
        for observer in tuple(self._observers):
            observer.offer(entries)

    def _watch_timeline(self):
        """Start listening to the records the platform makes for this realm."""
        timeline = self._timeline()
        if timeline is not None:
            timeline.subscribe(self._on_record)

    def _on_record(self):
        self._offer(tuple(self._timed_entries()))

    def schedule_delivery(self):
        if self._delivery_posted:
            return
        self._delivery_posted = True
        services = self._services
        if services is None:
            self._flush_delivery()
            return
        services.realm_services.post_task(self._flush_delivery)

    def _flush_delivery(self):
        self._delivery_posted = False
        for observer in tuple(self._observers):
            observer.deliver()

    def add_observer(self, observer):
        if observer not in self._observers:
            self._observers.append(observer)
            self._watch_timeline()

    def remove_observer(self, observer):
        if observer in self._observers:
            self._observers.remove(observer)

    def get_onresourcetimingbufferfull(self):
        return self._get_handler("onresourcetimingbufferfull")

    def set_onresourcetimingbufferfull(self, value):
        self._set_handler("onresourcetimingbufferfull", value)


#: What Chrome reports for ``PerformanceObserver.supportedEntryTypes``.
_SUPPORTED_ENTRY_TYPES = (
    "element", "event", "first-input", "interaction-contentful-paint",
    "largest-contentful-paint", "layout-shift", "long-animation-frame", "longtask", "mark",
    "measure", "navigation", "paint", "resource", "soft-navigation", "visibility-state",
)


@interface('PerformanceObserver', None)
class PerformanceObserver:
    """The observer a page builds: entries it asked for, handed over once per task."""

    def __init__(self, callback=MISSING, *_extra, services=pyv8.Undefined, _realm=None, **_):
        if callback is MISSING:
            raise missing_error('PerformanceObserver', 'constructor', 1, 0, constructor=True)
        if not callable(callback):
            raise PageTypeError("Failed to construct 'PerformanceObserver': parameter 1 is not "
                              "of type 'Function'.")
        self._callback = callback
        self._services = services if services is not pyv8.Undefined else _realm
        self._types = ()
        self._registration = None
        self._pending = []
        self._watermark = 0
        # Resolved here, while the page runs: a delivery happens on a task, where there
        # is no current context left to ask for the window.
        from .window import Window
        window = Window._accessor_window()
        self._performance = window._window_value("performance") if window is not None else None

    @staticmethod
    def get_supportedEntryTypes(*_args):
        return list(_SUPPORTED_ENTRY_TYPES)

    def fn_observe(self, options=MISSING, *_extra):
        message = "Failed to execute 'observe' on 'PerformanceObserver': "
        if options is MISSING or is_nullish(options):
            raise PageTypeError(message + "An observe() call must include either entryTypes or "
                                        "type arguments.")
        options = dictionary(options, name="PerformanceObserverInit", prefix=message)
        entry_types = options.get("entryTypes", pyv8.Undefined)
        typed = options.get("type", pyv8.Undefined)
        like_types, like_type = not is_nullish(entry_types), not is_nullish(typed)
        if like_types and like_type:
            raise PageTypeError(message + "An observe() call must not include both entryTypes "
                                        "and type arguments.")
        if not like_types and not like_type:
            raise PageTypeError(message + "An observe() call must include either entryTypes or "
                                        "type arguments.")
        style = "entryTypes" if like_types else "type"
        previous = self._registration
        if previous is not None and previous != style:
            # Chrome refuses to let one observer switch styles, and disconnect() does not
            # reset that; the wording differs per direction.
            lead = "This observer" if previous == "entryTypes" else "This PerformanceObserver"
            raise dom_exception(
                "InvalidModificationError",
                message + f"{lead} has performed observe({{{previous}:...}}, therefore it "
                          f"cannot perform observe({{{style}:...}})")
        self._registration = style
        if like_types:
            if isinstance(entry_types, str) or not hasattr(entry_types, "__iter__"):
                raise PageTypeError(message + "The provided value is not of type 'sequence'.")
            self._types = tuple(dom_string(item) for item in entry_types)
        else:
            self._types = (dom_string(typed),)
        performance = self._performance
        if performance is None:
            return None
        performance.add_observer(self)
        if options.get("buffered", pyv8.Undefined) is True and like_type:
            # ``buffered`` reaches back: everything already in the buffer is delivered.
            self.offer([entry for entry in performance._all_entries()
                        if self.wants(entry.get_entryType())])
        return None

    def fn_disconnect(self, *_extra):
        performance = self._performance
        if performance is not None:
            performance.remove_observer(self)
        self._pending = []
        self._types = ()
        return None

    def fn_takeRecords(self, *_extra):
        pending, self._pending = self._pending, []
        return list(pending)

    def wants(self, entry_type):
        return entry_type in self._types

    def offer(self, entries):
        """Take the entries that are new to this observer, and queue one delivery."""
        if self._performance is None:
            return
        matched = [entry for entry in entries
                   if self.wants(entry.get_entryType()) and entry._sequence > self._watermark]
        if not matched:
            return
        self._watermark = max(entry._sequence for entry in matched)
        self._pending.extend(matched)
        self._performance.schedule_delivery()

    def deliver(self):
        pending, self._pending = self._pending, []
        if not pending or not callable(self._callback):
            return
        self._callback(PerformanceObserverEntryList(pending))

__all__ = [
    "Performance", "PerformanceEntry", "PerformanceMark", "PerformanceMeasure",
    "PerformanceObserver", "PerformanceObserverEntryList", "PerformanceResourceTiming",
    "PerformanceNavigationTiming", "VisibilityStateEntry",
]
