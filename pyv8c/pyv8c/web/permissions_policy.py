"""``PermissionsPolicy``: the features a document or frame can control."""

from urllib.parse import urlsplit


from ..idl import interface
from ..foundation.arguments import MISSING, dom_string, missing_error


#: The schemes a document can have an origin of its own in; anything else is opaque.
_TUPLE_SCHEMES = ("http", "https", "ws", "wss", "ftp")


#: The features Chrome 154 reports, in the order ``features()`` answers with them.
FEATURES = (
    "geolocation", "ch-ua-full-version-list", "cross-origin-isolated", "screen-wake-lock",
    "on-device-speech-recognition", "translator", "publickey-credentials-get", "ch-ua-arch",
    "bluetooth", "compute-pressure", "ch-prefers-reduced-transparency", "deferred-fetch",
    "usb", "ch-save-data", "publickey-credentials-create", "deferred-fetch-minimal",
    "ch-downlink", "ch-ua-form-factors", "otp-credentials", "payment", "ch-ua",
    "ch-ua-model", "ch-ect", "autoplay", "camera", "language-detector",
    "private-state-token-issuance", "digital-credentials-get", "accelerometer",
    "ch-ua-platform-version", "idle-detection", "language-model", "interest-cohort",
    "ch-viewport-height", "captured-surface-control", "local-fonts", "ch-ua-platform",
    "midi", "ch-ua-full-version", "xr-spatial-tracking", "clipboard-read", "gamepad",
    "display-capture", "keyboard-map", "aria-notify", "local-network",
    "ch-ua-high-entropy-values", "ch-width", "ch-prefers-reduced-motion", "browsing-topics",
    "encrypted-media", "local-network-access", "gyroscope", "serial", "ch-rtt",
    "ch-ua-mobile", "window-management", "unload", "ch-dpr", "ch-prefers-color-scheme",
    "ch-ua-wow64", "fullscreen", "identity-credentials-get", "private-state-token-redemption",
    "hid", "summarizer", "ch-ua-bitness", "storage-access", "sync-xhr", "ch-device-memory",
    "ch-viewport-width", "picture-in-picture", "loopback-network", "magnetometer",
    "clipboard-write", "microphone",
)


@interface('PermissionsPolicy', None)
class PermissionsPolicy:
    """A top-level document and a frame it owns answer the same policy here: the
    features a page can reach without an ``allow`` attribute restricting them.
    """

    def fn_allowsFeature(self, feature=MISSING, *_extra):
        if feature is MISSING:
            raise missing_error('PermissionsPolicy', 'allowsFeature', 1, 0, constructor=False)
        name = str(dom_string(feature))
        realm = getattr(self._services, 'realm_services', None)
        return name in FEATURES and (realm is None or realm.permission_allowed(name))

    def fn_allowedFeatures(self, *_extra):
        return [name for name in FEATURES if self.fn_allowsFeature(name)]

    def fn_features(self, *_extra):
        return list(FEATURES)

    def __init__(self, url="", services=None, **_):
        self._url = str(url or "")
        self._services = services

    def fn_getAllowlistForFeature(self, feature=MISSING, *_extra):
        if feature is MISSING:
            raise missing_error('PermissionsPolicy', 'getAllowlistForFeature', 1, 0,
                                constructor=False)
        name = str(dom_string(feature))
        if not self.fn_allowsFeature(name):
            return []
        # The default allowlist of most features is 'self', which an opaque origin
        # cannot match, and Chrome answers the same way for an unknown name.
        if name not in FEATURES or urlsplit(self._url).scheme.lower() not in _TUPLE_SCHEMES:
            return []
        return ["self"]

__all__ = ["FEATURES", "PermissionsPolicy"]
