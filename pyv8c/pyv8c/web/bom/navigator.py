"""Navigator, geolocation, and browser-compatible plugin collections."""
import time
from urllib.parse import urljoin, urlsplit

from pyv8c import pyv8

from ...features.runtime import FeatureImplementation, LazyValueStore
from ...idl import interface
from ...foundation.arguments import (
    MISSING,
    boolean,
    callback_value,
    dictionary,
    dom_string,
    event_handler,
    integer,
    is_nullish,
    missing_error,
    sequence,
    usv_string,
)
from ...foundation.environment import BrowserEnvironment, MimeTypeView, environment_of
from ...foundation.arguments import IdlArgumentError
from ..page_errors import DOMException, PageTypeError, dom_exception
from ..page_errors import js_error_value
from ...foundation.realm import post_or_call
from ...foundation.urls import origin_string
from ..fileapi.blob import Blob
from ..dom.events import EventTarget
from ..media.source import device_kinds, media_facts
from ..dom.mixins import LegacyNamedPropertyMixin, ReadOnlyIndexedPropertyMixin
from .accessories import (DevicePosture, Ink, MediaCapabilities, NavigatorLogin,
                          NavigatorManagedData, Presentation, ProtectedAudience, Scheduling,
                          UserActivation, VirtualKeyboard, WindowControlsOverlay)
from .clipboard import Clipboard
from .devices import HID, USB, XRSystem
from .keyboard import Keyboard
from .locks import LockManager, WakeLock
from .storage import StorageBucketManager


#: The WGSL extensions Chrome reports, which the feature list is measured from.
_WGSL_LANGUAGE_FEATURES = (
    "buffer_view", "immediate_address_space", "linear_indexing",
    "packed_4x8_integer_dot_product", "pointer_composite_access",
    "readonly_and_readwrite_storage_textures", "subgroup_id", "subgroup_uniformity",
    "swizzle_assignment", "texture_and_sampler_let", "texture_formats_tier1",
    "uniform_buffer_standard_layout", "unrestricted_pointer_parameters",
)
from .url import URLSearchParams
from .utils import (is_secure_context, promise_receiver_error,
                    promise_rejected_in_caller_realm, settled as _settled,
                    window_secure_context)


class _LegacyNamedCollection(LegacyNamedPropertyMixin, ReadOnlyIndexedPropertyMixin):
    """Readonly indexed and named properties shared by plugin collections."""

    __idl_overrides__ = {
        "interface": {
            "length": 0,
            "name_del": "__v8_name_del__",
            "name_enum": "__v8_name_enum__",
            "name_get": "__v8_name_get__",
            "name_query": "__v8_name_query__",
            "name_set": "__v8_name_set__"
        }
    }

    def _item_argument(self, args, api_name):
        if not args:
            raise PageTypeError(
                f"Failed to execute 'item' on '{api_name}': 1 argument required, but only 0 present."
            )
        return integer(args[0], bits=32)

    def _named_argument(self, args, api_name):
        if not args:
            raise PageTypeError(
                f"Failed to execute 'namedItem' on '{api_name}': 1 argument required, but only 0 present."
            )
        return str(args[0])

    # The bodies depend only on ``_indexed_items``/``_named_item`` and the
    # registered interface name, so they live here once.
    def get_length(self): return len(self._indexed_items())
    def fn_item(self, *args):
        return self._item_at(self._item_argument(args, self._interface_name()))
    def fn_namedItem(self, *args):
        return self._named_item(self._named_argument(args, self._interface_name()))


@interface('MimeType', None)
class MimeType:
    def __init__(self, config=None, enabled_plugin=None, **_):
        self._config = config or MimeTypeView("application/pdf", "", "")
        self._enabled_plugin = enabled_plugin

    def get_type(self): return self._config.type
    def get_suffixes(self): return self._config.suffixes
    def get_description(self): return self._config.description
    def get_enabledPlugin(self): return self._enabled_plugin if self._enabled_plugin is not None else pyv8.Null


@interface('Plugin', None)
class Plugin(_LegacyNamedCollection):
    def __init__(self, config, **_):
        self._config = config
        self._mime_types = tuple(MimeType(mime, enabled_plugin=self) for mime in config.mime_types)

    def _indexed_items(self): return self._mime_types
    def _supported_names(self): return [item.get_type() for item in self._mime_types]
    def _named_item(self, name):
        return next((item for item in self._mime_types if item.get_type() == name), pyv8.Null)
    def get_name(self): return self._config.name
    def get_filename(self): return self._config.filename
    def get_description(self): return self._config.description


@interface('PluginArray', None)
class PluginArray(_LegacyNamedCollection):
    _prototype_names = _LegacyNamedCollection._prototype_names | {"refresh"}

    def __init__(self, configs=(), **_):
        self._plugins = tuple(Plugin(config) for config in configs)

    def _indexed_items(self): return self._plugins
    def _supported_names(self): return [item.get_name() for item in self._plugins]
    def _named_item(self, name):
        return next((item for item in self._plugins if item.get_name() == name), pyv8.Null)
    def fn_refresh(self, reload_documents=False, *_extra):
        if reload_documents is pyv8.Undefined:
            reload_documents = False
        if reload_documents is not pyv8.Undefined:
            reload_documents = boolean(reload_documents)
        return None


@interface('MimeTypeArray', None)
class MimeTypeArray(_LegacyNamedCollection):
    def __init__(self, plugins=(), **_):
        items, seen = [], set()
        for plugin in plugins:
            for config in plugin._config.mime_types:
                if config.type not in seen:
                    seen.add(config.type)
                    # Chrome keeps this object distinct from Plugin[index].
                    items.append(MimeType(config, enabled_plugin=plugin))
        self._mime_types = tuple(items)

    def _indexed_items(self): return self._mime_types
    def _supported_names(self): return [item.get_type() for item in self._mime_types]
    def _named_item(self, name):
        return next((item for item in self._mime_types if item.get_type() == name), pyv8.Null)


def create_plugin_collections(environment):
    """Create stable Navigator plugin and MIME collections for one realm."""
    environment = BrowserEnvironment.from_dict(environment)
    plugins = PluginArray(environment.plugins)
    return plugins, MimeTypeArray(plugins._plugins)


@interface('Geolocation', None)
class Geolocation:
    def __init__(self, services=None, environment=None, **_):
        self._services = services
        self._environment = BrowserEnvironment.from_dict(environment)
        self._watchers = {}
        self._next_watch = 1

    def _error(self, code, message): return {"code": code, "message": message}

    def _position(self):
        config = self._environment.geolocation
        if config.latitude is None or config.longitude is None:
            return None
        return {
            "coords": {
                "latitude": config.latitude, "longitude": config.longitude,
                "accuracy": config.accuracy if config.accuracy is not None else 0.0,
                "altitude": None, "altitudeAccuracy": None, "heading": None, "speed": None,
            },
            "timestamp": int(time.time() * 1000),
        }

    def _deliver(self, success, error, watch_id=None):
        def callback():
            if watch_id is not None and watch_id not in self._watchers:
                return
            if self._environment.geolocation.permission != "granted":
                if error is not None:
                    error(self._error(1, "User denied Geolocation"))
                return
            position = self._position()
            if position is None:
                if error is not None:
                    error(self._error(2, "Unable to determine the position"))
                return
            success(position)
        if self._services is not None:
            self._services.task_services.post_task(callback)

    def fn_getCurrentPosition(self, success=MISSING, error=None, options=None, *_extra):
        if success is MISSING:
            raise missing_error('Geolocation', 'getCurrentPosition', 1, 0, constructor=False)
        if success is not pyv8.Undefined:
            success = callback_value(success)
        if error is pyv8.Undefined:
            error = None
        if error is not pyv8.Undefined:
            if not is_nullish(error):
                error = callback_value(error)
        if options is pyv8.Undefined:
            options = None
        if options is not pyv8.Undefined:
            options = dictionary(options)
        if not callable(success):
            raise PageTypeError("success callback must be callable")
        if not is_nullish(error) and not callable(error):
            raise PageTypeError("error callback must be callable")
        self._deliver(success, error)

    def fn_watchPosition(self, success=MISSING, error=None, options=None, *_extra):
        if success is MISSING:
            raise missing_error('Geolocation', 'watchPosition', 1, 0, constructor=False)
        if success is not pyv8.Undefined:
            success = callback_value(success)
        if error is pyv8.Undefined:
            error = None
        if error is not pyv8.Undefined:
            if not is_nullish(error):
                error = callback_value(error)
        if options is pyv8.Undefined:
            options = None
        if options is not pyv8.Undefined:
            options = dictionary(options)
        if not callable(success):
            raise PageTypeError("success callback must be callable")
        if not is_nullish(error) and not callable(error):
            raise PageTypeError("error callback must be callable")
        ident = self._next_watch
        self._next_watch += 1
        self._watchers[ident] = (success, error)
        self._deliver(success, error, ident)
        return ident

    def fn_clearWatch(self, ident=MISSING, *_extra):
        if ident is MISSING:
            raise missing_error('Geolocation', 'clearWatch', 1, 0, constructor=False)
        ident = integer(ident, signed=True)
        self._watchers.pop(int(ident), None)


@interface('NetworkInformation', 'EventTarget')
class NetworkInformation(EventTarget):
    __idl_overrides__ = {
        "interface": {
            "length": 0
        }
    }

    def __init__(self, config, services=None, **_):
        super().__init__(services=services)
        self._config = config

    def get_onchange(self): return self._get_handler("onchange")
    def set_onchange(self, value): self._set_handler("onchange", event_handler(value))
    def get_effectiveType(self): return self._config.effective_type
    def get_rtt(self): return self._config.rtt
    def get_downlink(self): return self._config.downlink
    def get_saveData(self): return self._config.save_data
    def get_type(self): return self._config.type
    def get_downlinkMax(self): return self._config.downlink_max
    def get_ontypechange(self): return self._get_handler("ontypechange")
    def set_ontypechange(self, value): self._set_handler("ontypechange", event_handler(value))


@interface('BatteryManager', 'EventTarget')
class BatteryManager(EventTarget):
    def __init__(self, config, services=None, **_):
        super().__init__(services=services)
        self._config = config

    def get_charging(self): return self._config.charging
    def get_chargingTime(self): return self._config.charging_time
    def get_dischargingTime(self): return self._config.discharging_time
    def get_level(self): return self._config.level
    def get_onchargingchange(self): return self._get_handler("onchargingchange")
    def set_onchargingchange(self, value): self._set_handler("onchargingchange", event_handler(value))
    def get_onchargingtimechange(self): return self._get_handler("onchargingtimechange")
    def set_onchargingtimechange(self, value): self._set_handler("onchargingtimechange", event_handler(value))
    def get_ondischargingtimechange(self): return self._get_handler("ondischargingtimechange")
    def set_ondischargingtimechange(self, value): self._set_handler("ondischargingtimechange", event_handler(value))
    def get_onlevelchange(self): return self._get_handler("onlevelchange")
    def set_onlevelchange(self, value): self._set_handler("onlevelchange", event_handler(value))


@interface('DeprecatedStorageQuota', None)
class DeprecatedStorageQuota:
    def __init__(self, config, services=None, **_):
        self._config = config
        self._services = services

    @staticmethod
    def _optional_callback(value):
        if value is pyv8.Undefined or is_nullish(value):
            return None
        return callback_value(value)

    def fn_queryUsageAndQuota(self, success=MISSING, error=None, *_extra):
        if success is MISSING:
            raise missing_error('DeprecatedStorageQuota', 'queryUsageAndQuota', 1, 0, constructor=False)
        success = callback_value(success)
        self._optional_callback(error)
        post_or_call(self._services, success, self._config.usage, self._config.quota)

    def fn_requestQuota(self, requested=MISSING, success=None, error=None, *_extra):
        if requested is MISSING:
            raise missing_error('DeprecatedStorageQuota', 'requestQuota', 1, 0, constructor=False)
        requested = integer(requested, bits=64)
        success = self._optional_callback(success)
        self._optional_callback(error)
        post_or_call(self._services, success, min(requested, self._config.quota))


#: The three members every ``getHighEntropyValues`` result carries.
_LOW_ENTROPY_HINTS = frozenset(("brands", "mobile", "platform"))


@interface('NavigatorUAData', None)
class NavigatorUAData:
    """``navigator.userAgentData``; not ``[SameObject]``, so every read hands
    out a new one.
    """

    def __init__(self, environment=None, services=None, **_):
        self._services = services
        self._data = BrowserEnvironment.from_dict(environment).client_hints

    def get_brands(self): return _frozen(self._services, self._pairs(self._data.brands))
    def get_mobile(self): return self._data.mobile
    def get_platform(self): return self._data.platform

    def fn_toJSON(self, *_extra):
        return {"brands": self._pairs(self._data.brands),
                "mobile": self._data.mobile, "platform": self._data.platform}

    def fn_getHighEntropyValues(self, hints=MISSING, *_extra):
        """The hints asked for, and the low-entropy three either way."""
        invalid = promise_receiver_error(self, NavigatorUAData, 'getHighEntropyValues')
        if invalid is not None:
            return invalid
        services = self._services
        promise, resolve, reject = services.realm_services.make_promise()

        def settle():
            try:
                result = self._high_entropy(hints)
            except BaseException as error:
                # The hints convert when the promise settles, so a bad argument
                # rejects rather than throwing.
                reject(js_error_value(error))
                return
            resolve(result)

        services.task_services.schedule(settle, 0, repeat=False)
        return promise

    def _high_entropy(self, hints):
        requested = frozenset(sequence(hints, lambda hint: str(dom_string(hint))))
        #: Alphabetical, the order Chrome's own result carries.
        values = {
            "architecture": self._data.architecture,
            "bitness": self._data.bitness,
            "brands": self._pairs(self._data.brands),
            "formFactors": list(self._data.form_factors),
            "fullVersionList": self._pairs(self._data.full_version_list),
            "mobile": self._data.mobile,
            "model": self._data.model,
            "platform": self._data.platform,
            "platformVersion": self._data.platform_version,
            "uaFullVersion": self._full_version(),
            "wow64": self._data.wow64,
        }
        return {name: value for name, value in values.items()
                if name in requested or name in _LOW_ENTROPY_HINTS}

    def _full_version(self):
        """The build's own version: Google Chrome's entry, or Chromium's on a Chromium build."""
        for brand in ("Google Chrome", "Chromium"):
            version = next((entry.version for entry in self._data.full_version_list
                            if entry.brand == brand), "")
            if version:
                return version
        return ""

    def _pairs(self, entries):
        return [{"brand": entry.brand, "version": entry.version} for entry in entries]


#: Chrome 153's ``getSupportedConstraints()`` answer.
_CONSTRAINTS = (
    "aspectRatio", "autoGainControl", "brightness", "channelCount", "colorTemperature",
    "contrast", "deviceId", "displaySurface", "echoCancellation", "exposureCompensation",
    "exposureMode", "exposureTime", "facingMode", "focusDistance", "focusMode", "frameRate",
    "groupId", "height", "iso", "latency", "noiseSuppression", "pan", "pointsOfInterest",
    "resizeMode", "restrictOwnAudio", "sampleRate", "sampleSize", "saturation", "sharpness",
    "suppressLocalAudioPlayback", "tilt", "torch", "voiceIsolation", "whiteBalanceMode",
    "width", "zoom",
)


#: What the shipped profile last answered for; ``permissions.*`` replaces them when collected.
PERMISSION_NAMES = frozenset((
    "accelerometer", "background-fetch", "background-sync", "camera",
    "captured-surface-control", "clipboard-read", "clipboard-write", "display-capture",
    "geolocation", "gyroscope", "idle-detection", "keyboard-lock", "local-fonts",
    "magnetometer", "microphone", "midi", "notifications", "payment-handler",
    "periodic-background-sync", "persistent-storage", "pointer-lock", "push",
    "screen-wake-lock", "storage-access", "window-management",
))
_PERMISSION_GRANTED = frozenset((
    "accelerometer", "background-fetch", "background-sync", "clipboard-write", "gyroscope",
    "keyboard-lock", "magnetometer", "payment-handler", "pointer-lock", "screen-wake-lock",
    "storage-access",
))


#: The name ``PermissionStatus.name`` reports, which is not always the one queried.
_PERMISSION_SPEC_NAMES = {
    "accelerometer": "sensors", "background-fetch": "background_fetch",
    "background-sync": "background_sync", "camera": "video_capture",
    "clipboard-read": "clipboard_read", "clipboard-write": "clipboard_write",
    "display-capture": "display_capture", "gyroscope": "sensors",
    "idle-detection": "idle_detection", "local-fonts": "local_fonts",
    "magnetometer": "sensors", "microphone": "audio_capture",
    "payment-handler": "payment_handler", "periodic-background-sync": "periodic_background_sync",
    "persistent-storage": "durable_storage", "screen-wake-lock": "screen_wake_lock",
}


#: The ``MediaSessionAction`` values Chrome 153 accepts.
_SESSION_ACTIONS = frozenset((
    "enterpictureinpicture", "hangup", "nextslide", "nexttrack", "pause", "play",
    "previoustrack", "previousslide", "seekbackward", "seekforward", "seekto", "skipad",
    "stop", "togglecamera", "togglemicrophone",
))
_PLAYBACK_STATES = frozenset(("none", "paused", "playing"))
_SCRIPT_MIME_TYPES = ("application/ecmascript", "application/javascript",
                      "text/ecmascript", "text/javascript")


def _frozen(services, values):
    """A fresh frozen array, the way a sequence member hands one out."""
    return services.realm_services.v8_context.freeze(list(values))


def _number_or_none(value):
    """The number *value* holds, or ``None`` when it holds something else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return None if value != value or value in (float("inf"), float("-inf")) else float(value)


@interface('MediaDeviceInfo', None)
class MediaDeviceInfo:
    def __init__(self, kind="audiooutput", *, device_id="", label="", group_id="", **_):
        self._kind, self._device_id = kind, device_id
        self._label, self._group_id = label, group_id

    def get_deviceId(self): return self._device_id
    def get_kind(self): return self._kind
    def get_label(self): return self._label
    def get_groupId(self): return self._group_id
    def fn_toJSON(self, *_extra):
        return {"deviceId": self._device_id, "kind": self._kind,
                "label": self._label, "groupId": self._group_id}


@interface('InputDeviceInfo', 'MediaDeviceInfo')
class InputDeviceInfo(MediaDeviceInfo):
    """An input device: ``enumerateDevices`` makes these for the microphones and cameras.

    ``getCapabilities()`` answers ``{}`` without permission, which is what Chrome reports
    before a page has been granted the device.
    """

    def fn_getCapabilities(self, *_extra): return {}


#: The kinds ``enumerateDevices`` answers with an ``InputDeviceInfo``.
_INPUT_KINDS = ("audioinput", "videoinput")


@interface('MediaDevices', 'EventTarget')
class MediaDevices(EventTarget):
    def __init__(self, services=None, **_):
        super().__init__(services=services)
        self._services = services

    def get_ondevicechange(self): return self._get_handler("ondevicechange")
    def set_ondevicechange(self, value):
        self._set_handler("ondevicechange", event_handler(value))

    def fn_enumerateDevices(self, *_extra):
        """The devices the machine lists, which a profile collected: nothing without one."""
        invalid = promise_receiver_error(self, MediaDevices, 'enumerateDevices')
        if invalid is not None:
            return invalid
        devices = []
        for kind in device_kinds(self._services):
            info = InputDeviceInfo(kind=kind) if kind in _INPUT_KINDS else MediaDeviceInfo(kind=kind)
            devices.append(info)
        return _settled(self._services, value=devices)

    def fn_getSupportedConstraints(self, *_extra):
        facts = media_facts(self._services)
        names = facts.constraints if facts is not None and facts.constraints else _CONSTRAINTS
        return {name: True for name in names}

    def fn_getUserMedia(self, constraints=None, *_extra):
        """No device is ever opened here, so the answer is the permission a prompt would give.

        Chrome rejects this promise (rather than throwing) and leaves it unsettled while a
        permission prompt is up; a page that cannot be prompted sees the same ``NotAllowedError``
        a denied prompt produces.
        """
        try:
            options = {} if is_nullish(constraints) else dictionary(constraints)
            if not options.get("audio") and not options.get("video"):
                raise PageTypeError("Failed to execute 'getUserMedia' on 'MediaDevices': "
                                  "At least one of audio and video must be requested")
        except (PageTypeError, IdlArgumentError) as refused:
            return _settled(self._services, error=refused)
        return _settled(self._services,
                        error=dom_exception("NotAllowedError", "Permission denied"))

    def fn_getDisplayMedia(self, options=None, *_extra):
        return _settled(self._services,
                        error=dom_exception("NotAllowedError", "Permission denied"))

    def fn_setCaptureHandleConfig(self, config=None, *_extra):
        if not is_nullish(config):
            dictionary(config)
        return None


@interface('PermissionStatus', 'EventTarget')
class PermissionStatus(EventTarget):
    def __init__(self, name="", state="prompt", services=None, **_):
        super().__init__(services=services)
        self._name, self._state = name, state

    def get_name(self): return self._name
    def get_state(self): return self._state
    def get_onchange(self): return self._get_handler("onchange")
    def set_onchange(self, value): self._set_handler("onchange", event_handler(value))


@interface('Permissions', None)
class Permissions:
    def __init__(self, services=None, environment=None, **_):
        self._services = services
        self._environment = BrowserEnvironment.from_dict(environment)
        resolved = self._environment.resolved()
        self._permission_names = (PERMISSION_NAMES if resolved['permissions.names'] is None
                                  else self._environment.permissions.names)
        self._permission_granted = (_PERMISSION_GRANTED if resolved['permissions.granted'] is None
                                    else self._environment.permissions.granted)
        self._permission_spec_names = (_PERMISSION_SPEC_NAMES if resolved['permissions.spec_names'] is None
                                      else self._environment.permissions.spec_names)
        self._clipboard_read_status_name = None

    def fn_query(self, descriptor=MISSING, *_extra):
        invalid = promise_receiver_error(self, Permissions, 'query')
        if invalid is not None:
            return invalid
        try:
            status = self._query(descriptor)
        except (PageTypeError, IdlArgumentError, pyv8.JSException, DOMException) as error:
            #: A conversion or descriptor failure rejects in the realm of the function
            #: that was called -- a borrowed operation answers in the realm it came from.
            return promise_rejected_in_caller_realm(error, self._services)
        return _settled(self._services, value=status)

    def _query(self, descriptor):
        if descriptor is MISSING:
            raise missing_error('Permissions', 'query', 1, 0, constructor=False)
        if (is_nullish(descriptor)
                or not (isinstance(descriptor, (pyv8.JSObject, dict, list, tuple))
                        or callable(descriptor))):
            raise PageTypeError(
                "Failed to execute 'query' on 'Permissions': parameter 1 is not of type 'object'.")
        names = self._permission_names
        clipboard_read_status = False
        state_override = None
        values = dictionary(descriptor)
        name = self._permission_name(values, names)
        if name == "push":
            # Blink converts the derived PushPermissionDescriptor after the initial
            # PermissionDescriptor, reading its inherited name again.
            self._permission_name(values, names)
            if not boolean(values.get("userVisibleOnly")):
                raise dom_exception(
                    "NotSupportedError",
                    "Failed to execute 'query' on 'Permissions': Push Permission without "
                    "userVisibleOnly:true isn't supported yet.")
            name = "notifications"
        elif name in ("clipboard-read", "clipboard-write"):
            # The derived ClipboardPermissionDescriptor repeats its inherited name
            # before reading allowWithoutGesture.
            self._permission_name(values, names)
            without_gesture = boolean(values.get("allowWithoutGesture"))
            clipboard_read_status = name == "clipboard-read" or without_gesture
        elif name == "top-level-storage-access":
            # Blink converts the derived descriptor after the base descriptor.
            self._permission_name(values, names)
            requested = values.get("requestedOrigin", pyv8.Undefined)
            from .url import URL
            try:
                target = URL._resolve(usv_string(requested))
            except (PageTypeError, IdlArgumentError, ValueError):
                raise PageTypeError(
                    "Failed to execute 'query' on 'Permissions': The requested origin is invalid.") from None
            # Opaque origins cannot be trusted, even though a data document can
            # itself inherit a secure context from its creator.
            if urlsplit(target).scheme.lower() == 'data' or not is_secure_context(target):
                state_override = 'denied'
        spec_names = self._permission_spec_names
        status_name = spec_names.get(name, name)
        state_name = name
        if clipboard_read_status:
            # Chrome shares the status backing the read permission and an
            # unsanitized write, retaining the name from the first query.
            if self._clipboard_read_status_name is None:
                self._clipboard_read_status_name = status_name
            status_name = self._clipboard_read_status_name
            state_name = "clipboard-read"
        return PermissionStatus(
            status_name, state_override or self._state(state_name), services=self._services)

    @staticmethod
    def _permission_name(values, names):
        name = values.get("name")
        if name is None or name is pyv8.Undefined:
            raise PageTypeError(
                "Failed to execute 'query' on 'Permissions': Failed to read the 'name' property "
                "from 'PermissionDescriptor': Required member is undefined.")
        try:
            name = dom_string(name)
        except pyv8.JSException as error:
            original = getattr(error, "original", None)
            reason = (str(original._get("message"))
                      if isinstance(original, pyv8.JSObject) else "")
            if reason == "Cannot convert a Symbol value to a string":
                error = PageTypeError(
                    "Failed to execute 'query' on 'Permissions': Failed to read the 'name' "
                    "property from 'PermissionDescriptor': " + reason)
            raise error
        if name not in names:
            # The enum check rejects the promise rather than throwing at the call.
            raise PageTypeError(
                "Failed to execute 'query' on 'Permissions': Failed to read the 'name' property "
                "from 'PermissionDescriptor': The provided value "
                f"'{name}' is not a valid enum value of type PermissionName.")
        return name

    def _state(self, name):
        feature = {'audio_capture': 'microphone', 'video_capture': 'camera'}.get(name, name)
        realm = getattr(self._services, 'realm_services', None)
        if realm is not None and not realm.permission_allowed(feature):
            return 'denied'
        if name == "geolocation":
            return self._environment.geolocation.permission
        if name in ("background-fetch", "storage-access"):
            parent_window = getattr(self._services, "parent_window", None)
            if callable(parent_window) and parent_window() is not None:
                return "prompt"
        if name in self._permission_granted:
            return "granted"
        return "denied" if name == "periodic-background-sync" else "prompt"


@interface('MediaMetadata', None)
class MediaMetadata:
    def __init__(self, init=None, *, _realm=None, **_):
        self._services = _realm
        self._title = self._artist = self._album = ""
        self._images = []
        if not is_nullish(init):
            values = dictionary(init)
            self._title = str(dom_string(values.get("title", "")))
            self._artist = str(dom_string(values.get("artist", "")))
            self._album = str(dom_string(values.get("album", "")))
            if not is_nullish(values.get("artwork")):
                self.set_artwork(values["artwork"])

    def get_title(self): return self._title
    def set_title(self, value): self._title = str(dom_string(value))
    def get_artist(self): return self._artist
    def set_artist(self, value): self._artist = str(dom_string(value))
    def get_album(self): return self._album
    def set_album(self, value): self._album = str(dom_string(value))

    def get_artwork(self):
        """A fresh frozen array per read, the way Chrome hands one out."""
        return self._freeze(self._images)

    def set_artwork(self, value):
        base = self._base_url()
        self._images = [{"src": urljoin(base, str(dom_string(image.get("src", "")))),
                         "sizes": str(dom_string(image.get("sizes", ""))),
                         "type": str(dom_string(image.get("type", "")))}
                        for image in sequence(value, dictionary)]

    def get_chapterInfo(self):
        return self._freeze([])

    def _base_url(self):
        realm = getattr(self._services, "realm_services", None)
        return str(getattr(realm, "url", "") or "")

    def _freeze(self, values):
        return _frozen(self._services, values) if self._services is not None else values


@interface('MediaSession', None)
class MediaSession:
    def __init__(self, services=None, **_):
        self._services = services
        self._metadata = None
        self._playback_state = "none"
        self._handlers = {}

    def get_metadata(self):
        return pyv8.Null if self._metadata is None else self._metadata

    def set_metadata(self, value):
        if is_nullish(value):
            self._metadata = None
            return
        if not isinstance(value, MediaMetadata):
            raise PageTypeError("Failed to set the 'metadata' property on 'MediaSession': "
                              "Failed to convert value to 'MediaMetadata'.")
        self._metadata = value

    def get_playbackState(self): return self._playback_state

    def set_playbackState(self, value):
        state = str(dom_string(value))
        if state in _PLAYBACK_STATES:
            self._playback_state = state

    def fn_setActionHandler(self, action=MISSING, handler=MISSING, *_extra):
        if action is MISSING:
            raise missing_error('MediaSession', 'setActionHandler', 2, 0, constructor=False)
        if handler is MISSING:
            raise missing_error('MediaSession', 'setActionHandler', 2, 1, constructor=False)
        name = str(dom_string(action))
        if name not in _SESSION_ACTIONS:
            raise PageTypeError("Failed to execute 'setActionHandler' on 'MediaSession': "
                              f"The provided value '{name}' is not a valid enum value of "
                              "type MediaSessionAction.")
        if is_nullish(handler):
            self._handlers.pop(name, None)
            return None
        if not callable(handler):
            raise PageTypeError("Failed to execute 'setActionHandler' on 'MediaSession': "
                              "parameter 2 is not of type 'Function'.")
        self._handlers[name] = callback_value(handler)
        return None

    def fn_setCameraActive(self, active=MISSING, *_extra):
        if active is MISSING:
            raise missing_error('MediaSession', 'setCameraActive', 1, 0, constructor=False)
        self._camera = boolean(active)
        return None

    def fn_setMicrophoneActive(self, active=MISSING, *_extra):
        if active is MISSING:
            raise missing_error('MediaSession', 'setMicrophoneActive', 1, 0, constructor=False)
        self._microphone = boolean(active)
        return None

    def fn_setPositionState(self, state=pyv8.Undefined, *_extra):
        if is_nullish(state):
            return None
        values = dictionary(state)
        duration = _number_or_none(values.get("duration"))
        if duration is None or duration < 0:
            raise PageTypeError("Failed to execute 'setPositionState' on 'MediaSession': "
                              "The provided duration cannot be less than zero.")
        position = _number_or_none(values.get("position")) or 0.0
        if position > duration:
            raise PageTypeError("Failed to execute 'setPositionState' on 'MediaSession': "
                              "The provided position cannot be greater than the duration.")
        rate = _number_or_none(values.get("playbackRate")) or 1.0
        if rate <= 0:
            raise PageTypeError("Failed to execute 'setPositionState' on 'MediaSession': "
                              "The provided playback rate is not in the supported range.")
        return None


@interface('ServiceWorkerContainer', 'EventTarget')
class ServiceWorkerContainer(EventTarget):
    def __init__(self, services=None, **_):
        super().__init__(services=services)
        self._services = services
        self._ready = None

    def get_controller(self): return pyv8.Null

    def get_ready(self):
        # Nothing is ever registered here, so this promise never settles.
        if self._ready is None:
            self._ready = self._services.realm_services.make_promise()[0]
        return self._ready

    def get_oncontrollerchange(self): return self._get_handler("oncontrollerchange")
    def set_oncontrollerchange(self, value):
        self._set_handler("oncontrollerchange", event_handler(value))
    def get_onmessage(self): return self._get_handler("onmessage")
    def set_onmessage(self, value): self._set_handler("onmessage", event_handler(value))
    def get_onmessageerror(self): return self._get_handler("onmessageerror")
    def set_onmessageerror(self, value):
        self._set_handler("onmessageerror", event_handler(value))

    def fn_getRegistration(self, url=None, *_extra):
        if not is_nullish(url):
            dom_string(url)
        return _settled(self._services, value=pyv8.Undefined)

    def fn_getRegistrations(self, *_extra):
        return _settled(self._services, value=[])

    def fn_startMessages(self, *_extra): return None

    def fn_register(self, url=MISSING, options=None, *_extra):
        if url is MISSING:
            raise missing_error('ServiceWorkerContainer', 'register', 1, 0, constructor=False)
        if not is_nullish(options):
            dictionary(options)
        base = str(getattr(self._services.realm_services, "url", "") or "")
        target = urljoin(base, str(dom_string(url)))
        script_origin, origin = origin_string(target), origin_string(base)
        if script_origin != origin:
            return _settled(self._services, error=dom_exception(
                "SecurityError",
                "Failed to register a ServiceWorker: The origin of the provided scriptURL "
                f"('{script_origin}') does not match the current origin ('{origin}')."))
        scope = target[:target.rfind("/") + 1]
        content_type = self._script_type(target)
        if content_type is None:
            return _settled(self._services, error=PageTypeError(
                "Failed to register a ServiceWorker: The script could not be fetched."))
        if not any(content_type.startswith(name) for name in _SCRIPT_MIME_TYPES):
            return _settled(self._services, error=dom_exception(
                "SecurityError",
                f"Failed to register a ServiceWorker for scope ('{scope}') with script "
                f"('{target}'): The script has an unsupported MIME type ('{content_type}')."))
        return _settled(self._services, error=dom_exception(
            "NotSupportedError", "Service workers are not implemented here."))

    def _script_type(self, target):
        """The script's MIME type, or ``None`` when fetching it failed."""
        request = {"method": "GET", "mode": "same-origin", "destination": "serviceworker",
                   "resource_type": "script"}
        try:
            response = self._services.resource_services.load_resource(target, request)
        except BaseException:
            return None
        headers = getattr(response, "headers", None)
        if headers is None:
            return ""
        return str(headers.fn_get("content-type") or "").split(";")[0].strip().lower()


@interface('CredentialsContainer', None)
class CredentialsContainer:
    def __init__(self, services=None, **_):
        self._services = services

    def fn_get(self, options=None, *_extra):
        _values, kinds = self._kinds(options)
        if not kinds:
            return _settled(self._services, error=dom_exception(
                "NotSupportedError", "No credential type was specified in the request."))
        if "publicKey" in kinds:
            return _settled(self._services, error=dom_exception(
                "NotSupportedError", "WebAuthn is not implemented here."))
        # Nothing is stored here either, so both credential kinds find nothing.
        return _settled(self._services, value=pyv8.Null)

    def fn_create(self, options=None, *_extra):
        values, kinds = self._kinds(options)
        if len(kinds) != 1:
            return _settled(self._services, error=dom_exception(
                "NotSupportedError",
                "Only exactly one of 'password', 'federated', and 'publicKey' credential "
                "types are currently supported."))
        if kinds[0] == "password" and "id" not in dictionary(values["password"]):
            raise PageTypeError(
                "Failed to execute 'create' on 'CredentialsContainer': Failed to read the "
                "'password' property from 'CredentialCreationOptions': Failed to read the "
                "'id' property from 'CredentialData': Required member is undefined.")
        return _settled(self._services, error=dom_exception(
            "NotSupportedError", "Credential creation is not implemented here."))

    def fn_preventSilentAccess(self, *_extra):
        return _settled(self._services, value=pyv8.Undefined)

    def fn_store(self, credential=MISSING, *_extra):
        if credential is MISSING:
            raise missing_error('CredentialsContainer', 'store', 1, 0, constructor=False)
        # No Credential interface is modelled, so nothing can be one.
        raise PageTypeError("Failed to execute 'store' on 'CredentialsContainer': "
                          "parameter 1 is not of type 'Credential'.")

    def _kinds(self, options):
        values = {} if is_nullish(options) else dictionary(options)
        return values, [name for name in ("password", "federated", "publicKey")
                        if values.get(name)]


@interface('Bluetooth', 'EventTarget')
class Bluetooth(EventTarget):
    def __init__(self, services=None, environment=None, **_):
        super().__init__(services=services)
        self._services = services
        self._environment = BrowserEnvironment.from_dict(environment)

    def fn_getAvailability(self, *_extra):
        return _settled(self._services, value=bool(self._environment.bluetooth.available))

    def fn_requestDevice(self, options=None, *_extra):
        if not is_nullish(options):
            dictionary(options)
        # Chrome waits for a chooser here; with none to show, the request is refused.
        return _settled(self._services, error=dom_exception(
            "NotFoundError", "User cancelled the requestDevice() chooser."))


@interface('StorageManager', None)
class StorageManager:
    def __init__(self, services=None, environment=None, **_):
        self._services = services
        self._environment = BrowserEnvironment.from_dict(environment)

    def fn_estimate(self, *_extra):
        temporary = self._environment.storage.temporary
        return _settled(self._services, value={"quota": temporary.quota,
                                               "usage": temporary.usage,
                                               "usageDetails": {}})

    def fn_persisted(self, *_extra):
        return _settled(self._services, value=False)

    def fn_persist(self, *_extra):
        return _settled(self._services, value=False)

    def fn_getDirectory(self, *_extra):
        invalid = promise_receiver_error(self, StorageManager, 'getDirectory')
        if invalid is not None:
            return invalid
        from .opfs import bucket_directory
        try:
            directory = bucket_directory(self._services)
        except (PageTypeError, DOMException) as error:
            return promise_rejected_in_caller_realm(error, self._services)
        return _settled(self._services, value=directory)


def _navigator_values(navigator):
    """The sub-objects a Navigator hands out, built on first read."""
    environment = navigator._environment
    services = navigator._services

    def collections():
        collection, mime_types = create_plugin_collections(environment)
        values._cache("plugins", collection)
        values._cache("mimeTypes", mime_types)
        return collection, mime_types

    def plugins():
        return collections()[0]

    def mime_types():
        return collections()[1]

    values = LazyValueStore({
        "connection": lambda: NetworkInformation(environment.connection, services=services),
        "scheduling": lambda: Scheduling(),
        "locks": lambda: LockManager(services=services),
        "gpu": lambda: GPU(services=services),
        "battery": lambda: BatteryManager(environment.battery, services=services),
        "geolocation": lambda: Geolocation(services=services, environment=environment),
        "webkitTemporaryStorage": lambda: DeprecatedStorageQuota(
            environment.storage.temporary, services=services),
        "webkitPersistentStorage": lambda: DeprecatedStorageQuota(
            environment.storage.persistent, services=services),
        "plugins": plugins,
        "mimeTypes": mime_types,
        "mediaDevices": lambda: MediaDevices(services=services),
        "permissions": lambda: Permissions(services=services, environment=environment),
        "mediaSession": lambda: MediaSession(services=services),
        "serviceWorker": lambda: ServiceWorkerContainer(services=services),
        "credentials": lambda: CredentialsContainer(services=services),
        "bluetooth": lambda: Bluetooth(services=services, environment=environment),
        "storage": lambda: StorageManager(services=services, environment=environment),
        "serial": lambda: Serial(services=services),
        "userActivation": lambda: UserActivation(services=services),
        "devicePosture": lambda: DevicePosture(services=services),
        "protectedAudience": lambda: ProtectedAudience(),
        "presentation": lambda: Presentation(),
        "virtualKeyboard": lambda: VirtualKeyboard(services=services),
        "windowControlsOverlay": lambda: WindowControlsOverlay(services=services),
        "clipboard": lambda: Clipboard(services=services),
        "keyboard": lambda: Keyboard(services=services, environment=environment),
        "wakeLock": lambda: WakeLock(services=services),
        "mediaCapabilities": lambda: MediaCapabilities(services=services),
        "storageBuckets": lambda: StorageBucketManager(services=services),
        "usb": lambda: USB(services=services),
        "hid": lambda: HID(services=services),
        "xr": lambda: XRSystem(services=services),
        "ink": lambda: Ink(services=services),
        "login": lambda: NavigatorLogin(services=services),
        "managed": lambda: NavigatorManagedData(services=services),
    })
    return values


#: The content types a beacon body carries, named the way Chrome names them.
_BEACON_TEXT = "text/plain;charset=UTF-8"
_BEACON_FORM = "application/x-www-form-urlencoded;charset=UTF-8"


def _beacon_body(data):
    """``(body, contentType)`` for what a page handed ``sendBeacon``.

    A Blob brings its own bytes and its own type, a URLSearchParams is form-encoded,
    and everything else is text -- which is why an object travels as ``[object Object]``.
    Nothing at all is no body and no type.
    """
    if is_nullish(data):
        return None, None
    if isinstance(data, Blob):
        return data._data, data.get_type()
    if isinstance(data, URLSearchParams):
        return data.fn_toString(), _BEACON_FORM
    return usv_string(data), _BEACON_TEXT


@interface('Serial', 'EventTarget')
class Serial(EventTarget):
    """The Web Serial entry point; the ports behind it are the baseline's business."""

    def get_onconnect(self): return self._get_handler("onconnect")
    def set_onconnect(self, value): self._set_handler("onconnect", value)
    def get_ondisconnect(self): return self._get_handler("ondisconnect")
    def set_ondisconnect(self, value): self._set_handler("ondisconnect", value)


@interface('Navigator', None)
class Navigator:
    def __init__(self, user_agent=None, services=None, environment=None, worker=False, **_):
        self._services = services
        self._environment = BrowserEnvironment.from_dict(environment)
        self._worker = bool(worker)
        if user_agent is not None:
            self._environment = self._environment.overlay({"user_agent": user_agent})
        self._features = _navigator_values(self)

    def get_userAgent(self): return self._environment.user_agent
    def get_userAgentData(self):
        """A fresh UA-CH object per read, and only in a secure context.

        ``worker=True``: this one belongs to a worker realm as well, where the question is the
        script's own URL rather than any page's -- passing it asked the worker-only rule and
        left the worker without the object.
        """
        services = self._services
        if services is None or not self._secure(worker=True, services=services):
            return pyv8.Undefined
        return NavigatorUAData(environment=self._environment, services=services)
    # Frozen compatibility values, as Chrome reports them.
    def get_appName(self): return "Netscape"
    def get_appCodeName(self): return "Mozilla"
    def get_product(self): return "Gecko"
    def get_productSub(self): return "20030107"
    def get_appVersion(self): return self._environment.app_version
    def get_platform(self): return self._environment.platform
    def get_language(self): return self._environment.language
    def get_languages(self): return list(self._environment.languages)
    def get_onLine(self): return self._environment.on_line
    def get_hardwareConcurrency(self): return self._environment.hardware_concurrency
    def get_cpuPerformance(self):
        """Chromium's platform tier: 1 low, 2 mid, 3 high, 4 ultra, 0 unknown."""
        cores = self._environment.hardware_concurrency or 0
        if not cores: return 0
        return 1 if cores <= 2 else 2 if cores <= 4 else 3 if cores <= 8 else 4
    def fn_getGamepads(self, *_extra):
        """Four slots, all null until a device connects."""
        return [pyv8.Null] * 4
    def get_deviceMemory(self): return self._environment.device_memory
    def get_maxTouchPoints(self): return self._environment.max_touch_points
    def get_vendor(self): return self._environment.vendor
    def get_vendorSub(self): return ""
    #: This browser keeps cookies and shows PDFs, the way Chrome reports both.
    def get_cookieEnabled(self): return True
    def get_pdfViewerEnabled(self): return True
    def get_locks(self):
        return self._features.value("locks") if self._secure() else pyv8.Undefined
    def get_serial(self):
        return self._features.value("serial") if self._secure() else pyv8.Undefined
    def fn_sendBeacon(self, url=MISSING, data=None, *_extra):
        """Queue *data* as a POST to *url* and answer whether it was queued.

        The request is not waited for: a beacon is on its way once it is queued, which is
        why the answer is a boolean and there is nothing to hand back.
        """
        if url is MISSING:
            raise missing_error('Navigator', 'sendBeacon', 1, 0, constructor=False)
        services = self._services
        target = urljoin(services.realm_services.url, usv_string(url))
        if urlsplit(target).scheme not in ("http", "https"):
            raise PageTypeError("Failed to execute 'sendBeacon' on 'Navigator': Beacons are"
                              " only supported over HTTP(S).")
        body, content_type = _beacon_body(data)
        services.resource_services.load_resource_async(target, {
            "method": "POST", "body": body, "resource_type": "beacon",
            "headers": {"content-type": content_type} if content_type else {},
        }, lambda response, error: None)
        return True
    def get_doNotTrack(self): return pyv8.Null
    def get_webdriver(self): return self._environment.webdriver
    def get_connection(self): return self._features.value("connection")
    def get_scheduling(self): return self._features.value("scheduling")
    def get_webkitTemporaryStorage(self):
        if self._worker:
            return pyv8.Undefined
        return self._features.value("webkitTemporaryStorage")
    def get_webkitPersistentStorage(self):
        if self._worker:
            return pyv8.Undefined
        return self._features.value("webkitPersistentStorage")
    def get_geolocation(self):
        if self._services is None or self._worker:
            return pyv8.Undefined
        return self._features.value("geolocation")
    def get_plugins(self):
        return pyv8.Undefined if self._worker else self._features.value("plugins")
    def get_mimeTypes(self):
        return pyv8.Undefined if self._worker else self._features.value("mimeTypes")
    def _realm_url(self):
        realm = getattr(self._services, "realm_services", None)
        return str(getattr(realm, "url", "") or "")
    def _secure(self, *, worker=False, services=None):
        """Whether Chrome exposes a secure-context member here.

        The document's own URL answers for an ordinary ``https`` page; a frame whose address
        carries no scheme of its own (``about:blank``, ``about:srcdoc``) inherits the answer
        from the page that created it, exactly as ``isSecureContext`` does -- Cloudflare's
        widget reads thirty of these members and gets ``undefined`` when this says no.
        """
        services = self._services if services is None else services
        if services is None or (self._worker and not worker):
            return False
        window = getattr(services, "window", None)
        if window is None:
            return is_secure_context(self._realm_url())
        return window_secure_context(window, self._realm_url())
    def get_gpu(self):
        """Chrome exposes WebGPU in a secure context, in a worker as well as a page."""
        return self._features.value("gpu") if self._secure(worker=True) else pyv8.Undefined
    def get_mediaDevices(self):
        return self._features.value("mediaDevices") if self._secure() else pyv8.Undefined
    def get_mediaSession(self):
        return pyv8.Undefined if self._worker else self._features.value("mediaSession")
    def get_serviceWorker(self):
        return self._features.value("serviceWorker") if self._secure() else pyv8.Undefined
    def get_credentials(self):
        return self._features.value("credentials") if self._secure() else pyv8.Undefined
    def get_bluetooth(self):
        return self._features.value("bluetooth") if self._secure() else pyv8.Undefined
    def get_storage(self):
        return self._features.value("storage") if self._secure(worker=True) else pyv8.Undefined
    def get_permissions(self):
        if self._services is None:
            return pyv8.Undefined
        return self._features.value("permissions")
    #: The members below are secure-context only, and each reports the idle state.
    def get_userActivation(self):
        return self._features.value("userActivation") if self._secure() else pyv8.Undefined
    def get_devicePosture(self):
        return self._features.value("devicePosture") if self._secure() else pyv8.Undefined
    def get_protectedAudience(self):
        return self._features.value("protectedAudience") if self._secure() else pyv8.Undefined
    def get_presentation(self):
        return self._features.value("presentation") if self._secure() else pyv8.Undefined
    def get_virtualKeyboard(self):
        return self._features.value("virtualKeyboard") if self._secure() else pyv8.Undefined
    def get_windowControlsOverlay(self):
        return self._features.value("windowControlsOverlay") if self._secure() else pyv8.Undefined
    def get_deprecatedRunAdAuctionEnforcesKAnonymity(self):
        return False if self._secure() else pyv8.Undefined
    def get_clipboard(self):
        return self._features.value("clipboard") if self._secure() else pyv8.Undefined
    def get_keyboard(self):
        return self._features.value("keyboard") if self._secure() else pyv8.Undefined
    def get_wakeLock(self):
        return self._features.value("wakeLock") if self._secure() else pyv8.Undefined
    def get_mediaCapabilities(self):
        return self._features.value("mediaCapabilities") if self._secure() else pyv8.Undefined
    def get_storageBuckets(self):
        return self._features.value("storageBuckets") if self._secure() else pyv8.Undefined
    def get_usb(self):
        return self._features.value("usb") if self._secure() else pyv8.Undefined
    def get_hid(self):
        return self._features.value("hid") if self._secure() else pyv8.Undefined
    def get_xr(self):
        return self._features.value("xr") if self._secure() else pyv8.Undefined
    def get_ink(self):
        return self._features.value("ink") if self._secure() else pyv8.Undefined
    def get_login(self):
        return self._features.value("login") if self._secure() else pyv8.Undefined
    def get_managed(self):
        return self._features.value("managed") if self._secure() else pyv8.Undefined
    def fn_javaEnabled(self, *_extra):
        return pyv8.Undefined if self._worker else False
    def fn_getBattery(self=None, *_extra):
        if not isinstance(self, Navigator):
            resolver = pyv8.PromiseResolver(pyv8.current_context())
            promise = resolver.promise()
            resolver.reject(PageTypeError(
                "Failed to execute 'getBattery' on 'Navigator': Illegal invocation"))
            return promise
        battery = self._features.value("battery")
        realm = getattr(self._services, "realm_services", None)
        return realm.promise_resolve(battery) if realm is not None else battery


@interface('WGSLLanguageFeatures', None)
class WGSLLanguageFeatures:
    """The WGSL extensions this build understands, measured from Chrome.

    Set iteration publishes each extension name as both key and value.
    """

    def __init__(self, names=_WGSL_LANGUAGE_FEATURES, **_):
        self._names = tuple(str(name) for name in names)

    def get_size(self): return len(self._names)

    def fn_has(self, name=MISSING, *_extra):
        if name is MISSING:
            raise missing_error('WGSLLanguageFeatures', 'has', 1, 0, constructor=False)
        return str(dom_string(name)) in self._names

    def __v8_iteration_source__(self):
        items = iter(tuple((name, name) for name in self._names))
        return lambda: next(items, None)



@interface('GPU', None)
class GPU:
    """The entry point to WebGPU, as a browser without a backend reports it."""

    def __init__(self, services=None, **_):
        self._services = services
        self._features = None

    def get_wgslLanguageFeatures(self):
        if self._features is None:
            environment = environment_of(self._services)
            gpu = getattr(environment, "gpu", None) if environment is not None else None
            names = gpu.wgsl_features if gpu is not None and gpu.wgsl_features else None
            self._features = WGSLLanguageFeatures(names=names or _WGSL_LANGUAGE_FEATURES)
        return self._features

    def fn_getPreferredCanvasFormat(self, *_extra):
        """The spec's default, and what Chrome reports on this platform."""
        return "bgra8unorm"

    def fn_requestAdapter(self, *_extra):
        """The adapter the reference machine reports, shaped as the API describes one."""
        invalid = promise_receiver_error(self, GPU, 'requestAdapter')
        if invalid is not None:
            return invalid
        realm = getattr(self._services, "realm_services", None)
        if realm is None:
            return pyv8.Null
        environment = environment_of(self._services)
        gpu = getattr(environment, "gpu", None) if environment is not None else None
        adapter_data = getattr(gpu, "adapter", None) or {}
        features = adapter_data.get("features") or _GPU_ADAPTER_FEATURES
        limits = adapter_data.get("limits") or _GPU_ADAPTER_LIMITS
        info = adapter_data.get("info") or _GPU_ADAPTER_INFO
        context = realm.v8_context
        adapter = context.freeze({
            "features": set(features),
            "limits": context.freeze(dict(limits)),
            "info": context.freeze(dict(info)),
            "isFallbackAdapter": False,
        })
        return realm.promise_resolve(adapter)


#: What chrome-win64 154's adapter reports; captured from a real context.
_GPU_ADAPTER_FEATURES = (
    "depth32float-stencil8", "rg11b10ufloat-renderable", "texture-formats-tier1",
    "bgra8unorm-storage", "texture-compression-bc", "dual-source-blending",
    "core-features-and-limits", "float32-filterable", "indirect-first-instance",
    "float32-blendable", "depth-clip-control", "texture-compression-bc-sliced-3d",
    "shader-f16", "texture-formats-tier2", "timestamp-query", "clip-distances",
    "primitive-index", "texture-component-swizzle", "subgroups",
)
_GPU_ADAPTER_LIMITS = {
    "maxTextureDimension1D": 16384, "maxTextureDimension2D": 16384,
    "maxTextureDimension3D": 2048, "maxTextureArrayLayers": 2048, "maxBindGroups": 4,
    "maxBindGroupsPlusVertexBuffers": 24, "maxBindingsPerBindGroup": 1000,
    "maxDynamicUniformBuffersPerPipelineLayout": 10,
    "maxDynamicStorageBuffersPerPipelineLayout": 8,
    "maxSampledTexturesPerShaderStage": 48, "maxSamplersPerShaderStage": 16,
    "maxStorageBuffersPerShaderStage": 16, "maxStorageTexturesPerShaderStage": 8,
    "maxUniformBuffersPerShaderStage": 12, "maxUniformBufferBindingSize": 65536,
    "maxStorageBufferBindingSize": 2147483644, "minUniformBufferOffsetAlignment": 256,
    "minStorageBufferOffsetAlignment": 256, "maxVertexBuffers": 8,
    "maxBufferSize": 2147483648, "maxVertexAttributes": 30,
    "maxVertexBufferArrayStride": 2048, "maxInterStageShaderVariables": 28,
    "maxColorAttachments": 8, "maxColorAttachmentBytesPerSample": 128,
    "maxComputeWorkgroupStorageSize": 32768, "maxComputeInvocationsPerWorkgroup": 1024,
    "maxComputeWorkgroupSizeX": 1024, "maxComputeWorkgroupSizeY": 1024,
    "maxComputeWorkgroupSizeZ": 64, "maxComputeWorkgroupsPerDimension": 65535,
    "maxImmediateSize": 64, "maxStorageBuffersInFragmentStage": 16,
    "maxStorageTexturesInFragmentStage": 8, "maxStorageBuffersInVertexStage": 16,
    "maxStorageTexturesInVertexStage": 8,
}
_GPU_ADAPTER_INFO = {
    "vendor": "nvidia", "architecture": "ampere", "device": "", "description": "",
    "subgroupMinSize": 32, "subgroupMaxSize": 32, "isFallbackAdapter": False,
}

__all__ = [
    "Navigator", "NavigatorUAData", "Geolocation", "Plugin", "PluginArray", "MimeType",
    "MimeTypeArray", "NetworkInformation", "BatteryManager", "DeprecatedStorageQuota",
    "MediaDeviceInfo", "MediaDevices", "Permissions", "PermissionStatus", "MediaMetadata",
    "MediaSession", "ServiceWorkerContainer", "CredentialsContainer", "Bluetooth",
    "StorageManager", "GPU", "WGSLLanguageFeatures",
]


FEATURE = FeatureImplementation(
    {
        "MimeType": MimeType,
        "Plugin": Plugin,
        "PluginArray": PluginArray,
        "MimeTypeArray": MimeTypeArray,
        "Geolocation": Geolocation,
        "NetworkInformation": NetworkInformation,
        "BatteryManager": BatteryManager,
        "DeprecatedStorageQuota": DeprecatedStorageQuota,
        "Navigator": Navigator,
        "GPU": GPU,
        "WGSLLanguageFeatures": WGSLLanguageFeatures,
        "NavigatorUAData": NavigatorUAData,
        "MediaDeviceInfo": MediaDeviceInfo,
        "InputDeviceInfo": InputDeviceInfo,
        "MediaDevices": MediaDevices,
        "Permissions": Permissions,
        "PermissionStatus": PermissionStatus,
        "MediaMetadata": MediaMetadata,
        "MediaSession": MediaSession,
        "ServiceWorkerContainer": ServiceWorkerContainer,
        "CredentialsContainer": CredentialsContainer,
        "Bluetooth": Bluetooth,
        "StorageManager": StorageManager,
        "Serial": Serial,
    },
    {
        "navigator": lambda window: Navigator(
            services=window._services,
            environment=getattr(getattr(window._services, "realm_services", None), "environment", None),
        ),
    },
)
