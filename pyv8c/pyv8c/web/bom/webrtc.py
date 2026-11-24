"""``RTCRtpReceiver.getCapabilities``: the codecs this browser says it can receive.

The tables are what Chrome reports on a desktop build, which is the reference the
rest of the profile is read from; a different build answers with its own list.
"""

import base64
import os

from pyv8c import pyv8
from ...idl import interface
from ...foundation.arguments import (MISSING, dictionary, dom_string, integer, is_nullish,
                                     missing_error)
from ...foundation.environment import environment_of
from ..page_errors import InvalidStateError, PageTypeError, dom_exception
from ..page_errors import js_error_value
from .utils import promise_receiver_error
from ..dom.events import (Event, EventTarget, _install_handler_slots,
                          event_constructor_arguments, event_init)


#: ``(mimeType, clockRate, channels, sdpFmtpLine)`` per codec, in the order Chrome lists
#: them; ``channels`` is ``None`` for a direction that reports none.
AUDIO_CODECS = (
    ("audio/opus", 48000, 2, "minptime=10;useinbandfec=1"),
    ("audio/red", 48000, 2, ""),
    ("audio/G722", 8000, 1, ""),
    ("audio/PCMU", 8000, 1, ""),
    ("audio/PCMA", 8000, 1, ""),
    ("audio/CN", 8000, 1, ""),
    ("audio/telephone-event", 48000, 1, ""),
    ("audio/telephone-event", 8000, 1, ""),
)


def _h264(profile):
    """The two packetization modes Chrome lists for one H264 profile."""
    return tuple(("video/H264", 90000, None,
                  f"level-asymmetry-allowed=1;packetization-mode={mode}"
                  f";profile-level-id={profile}")
                 for mode in (1, 0))


#: The same for the video direction, which reports no channel count.  The H264 entries
#: are split around the AV1 ones because that is the order Chrome lists them in.
VIDEO_CODECS = (
    ("video/VP8", 90000, None, ""),
    ("video/rtx", 90000, None, ""),
    *(("video/VP9", 90000, None, f"profile-id={profile}") for profile in (0, 2, 1, 3)),
    *_h264("42001f"), *_h264("42e01f"), *_h264("4d001f"), *_h264("f4001f"),
    ("video/AV1", 90000, None, "level-idx=5;profile=0;tier=0"),
    ("video/AV1", 90000, None, "level-idx=5;profile=1;tier=0"),
    *_h264("64001f"),
    ("video/H265", 90000, None, "level-id=150;profile-id=1;tier-flag=0;tx-mode=SRST"),
    ("video/H265", 90000, None, "level-id=150;profile-id=2;tier-flag=0;tx-mode=SRST"),
    ("video/red", 90000, None, ""),
    ("video/ulpfec", 90000, None, ""),
    ("video/flexfec-03", 90000, None, "repair-window=10000000"),
)


#: The header extensions, which are the same for both directions but for the first.
AUDIO_EXTENSIONS = (
    "urn:ietf:params:rtp-hdrext:ssrc-audio-level",
    "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
    "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01",
    "urn:ietf:params:rtp-hdrext:sdes:mid",
)
VIDEO_EXTENSIONS = (
    "urn:ietf:params:rtp-hdrext:toffset",
    "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
    "urn:3gpp:video-orientation",
    "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01",
    "http://www.webrtc.org/experiments/rtp-hdrext/playout-delay",
    "http://www.webrtc.org/experiments/rtp-hdrext/video-content-type",
    "http://www.webrtc.org/experiments/rtp-hdrext/video-timing",
    "http://www.webrtc.org/experiments/rtp-hdrext/color-space",
    "urn:ietf:params:rtp-hdrext:sdes:mid",
    "urn:ietf:params:rtp-hdrext:sdes:rtp-stream-id",
    "urn:ietf:params:rtp-hdrext:sdes:repaired-rtp-stream-id",
)


def _codec(name, clock_rate, channels, sdp):
    """One codec capability, with the members Chrome writes and in its order."""
    entry = {}
    if channels is not None:
        entry["channels"] = channels
    entry["clockRate"] = clock_rate
    entry["mimeType"] = name
    if sdp:
        entry["sdpFmtpLine"] = sdp
    return entry


def _capabilities(codecs, extensions):
    return {"codecs": [_codec(*codec) for codec in codecs],
            "headerExtensions": [{"direction": "sendrecv", "uri": uri}
                                 for uri in extensions]}


@interface('RTCRtpReceiver', None)
class RTCRtpReceiver:
    @staticmethod
    def fn_getCapabilities(_realm, kind=MISSING, *_extra):
        if kind is MISSING:
            raise missing_error('RTCRtpReceiver', 'getCapabilities', 1, 0, constructor=False)
        direction = str(dom_string(kind))
        if direction == "audio":
            return _capabilities(AUDIO_CODECS, AUDIO_EXTENSIONS)
        if direction == "video":
            return _capabilities(VIDEO_CODECS, VIDEO_EXTENSIONS)
        return pyv8.Null


#: What Chrome reports for a connection with no transport: the page can open a
#: channel, but nothing leaves, because there is no peer to reach.
CONFIGURATION = (
    ("alwaysNegotiateDataChannels", False), ("bundlePolicy", "balanced"),
    ("certificates", ()), ("encodedInsertableStreams", False),
    ("iceCandidatePoolSize", 0), ("iceServers", ()), ("iceTransportPolicy", "all"),
    ("rtcpMuxPolicy", "require"),
)

_PEER_HANDLERS = (
    "onnegotiationneeded", "onicecandidate", "onsignalingstatechange",
    "oniceconnectionstatechange", "onconnectionstatechange", "onicegatheringstatechange",
    "onicecandidateerror", "ontrack", "ondatachannel", "onaddstream", "onremovestream",
)

_CHANNEL_HANDLERS = (
    "onopen", "onbufferedamountlow", "onerror", "onclosing", "onclose", "onmessage",
)


def _settled_promise(services, value):
    realm = getattr(services, "realm_services", None)
    return realm.promise_resolve(value) if realm is not None else value


def _rejected_promise(services, error):
    realm = getattr(services, "realm_services", None)
    if realm is None:
        return error
    promise, _resolve, reject = realm.make_promise()
    reject(js_error_value(error))
    return promise


def _invalid_peer_receiver(method):
    resolver = pyv8.PromiseResolver(pyv8.current_context())
    promise = resolver.promise()
    resolver.reject(PageTypeError(
        f"Failed to execute '{method}' on 'RTCPeerConnection': Illegal invocation"))
    return promise


_HOST_PRIORITY = 2122262783
_HOST_PORT = 49152


def _host_candidate_line(address):
    """One host UDP candidate. The address is not wrapped in brackets, IPv6 included."""
    return f"candidate:1 1 udp {_HOST_PRIORITY} {address} {_HOST_PORT} typ host"


def _optional_candidate_text(value):
    if is_nullish(value):
        return None
    return str(dom_string(value))


def _optional_candidate_int(value):
    if is_nullish(value):
        return None
    return int(value)


def _configured_webrtc_ip(services):
    """The environment's host address, or ``None`` when gathering should stay empty."""
    environment = environment_of(services)
    if environment is None:
        return None
    value = environment.resolved().get("webrtc.ip")
    if isinstance(value, str) and value:
        return value
    return None


def _offer_sdp():
    """The description Chrome writes for a connection with one data channel.

    A browser without a transport still describes the session it would offer. The lines
    are that description; the per-session parts -- the origin number, the ICE
    credentials and the DTLS fingerprint -- are drawn fresh, as a browser draws them.
    Nothing behind them can complete a negotiation.
    """
    fingerprint = ":".join(f"{byte:02X}" for byte in os.urandom(32))
    ufrag = base64.b64encode(os.urandom(3)).decode()
    pwd = base64.b64encode(os.urandom(18)).decode()
    origin = int.from_bytes(os.urandom(8), "big") >> 1
    return "\r\n".join((
        "v=0",
        f"o=- {origin} 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=extmap-allow-mixed",
        "a=msid-semantic: WMS",
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
        "c=IN IP4 0.0.0.0",
        f"a=ice-ufrag:{ufrag}",
        f"a=ice-pwd:{pwd}",
        "a=ice-options:trickle",
        f"a=fingerprint:sha-256 {fingerprint}",
        "a=setup:actpass",
        "a=mid:0",
        "a=sctp-port:5000",
        "a=max-message-size:262144",
    )) + "\r\n"


@interface('RTCIceCandidate', None)
class RTCIceCandidate:
    """One ICE candidate. Members this class does not define stay baseline stubs."""

    def __init__(self, candidate_init=MISSING, *_extra, **_kw):
        options = {} if is_nullish(candidate_init) or candidate_init is MISSING \
            else dictionary(candidate_init)
        candidate = options.get("candidate")
        self._candidate = "" if is_nullish(candidate) else str(dom_string(candidate))
        self._sdp_mid = _optional_candidate_text(options.get("sdpMid"))
        self._sdp_m_line_index = _optional_candidate_int(options.get("sdpMLineIndex"))
        self._address = _optional_candidate_text(options.get("address"))
        self._port = _optional_candidate_int(options.get("port"))
        self._protocol = _optional_candidate_text(options.get("protocol"))
        self._type = _optional_candidate_text(options.get("type"))

    def get_candidate(self):
        return self._candidate

    def get_sdpMid(self):
        return pyv8.Null if self._sdp_mid is None else self._sdp_mid

    def get_sdpMLineIndex(self):
        return pyv8.Null if self._sdp_m_line_index is None else self._sdp_m_line_index

    def get_address(self):
        return pyv8.Null if self._address is None else self._address

    def get_port(self):
        return pyv8.Null if self._port is None else self._port

    def get_protocol(self):
        return pyv8.Null if self._protocol is None else self._protocol

    def get_type(self):
        return pyv8.Null if self._type is None else self._type

    def fn_toJSON(self, *_extra):
        return {
            "candidate": self._candidate,
            "sdpMid": self.get_sdpMid(),
            "sdpMLineIndex": self.get_sdpMLineIndex(),
        }


def _host_candidate(address):
    return RTCIceCandidate({
        "candidate": _host_candidate_line(address),
        "sdpMid": "0",
        "sdpMLineIndex": 0,
        "address": address,
        "port": _HOST_PORT,
        "protocol": "udp",
        "type": "host",
    })


@interface('RTCSessionDescription', None)
class RTCSessionDescription:
    """What ``createOffer`` resolves with: the type and its description."""

    def __init__(self, description=MISSING, *_extra, **_kw):
        options = {} if is_nullish(description) or description is MISSING \
            else dictionary(description)
        self._type = str(options.get("type", ""))
        self._sdp = str(options.get("sdp", ""))

    def get_type(self): return self._type
    def set_type(self, value): self._type = str(dom_string(value))
    def get_sdp(self): return self._sdp
    def set_sdp(self, value): self._sdp = str(dom_string(value))
    def fn_toJSON(self, *_extra): return {"type": self._type, "sdp": self._sdp}


@interface('RTCPeerConnectionIceEvent', 'Event')
class RTCPeerConnectionIceEvent(Event):
    """One ICE candidate, and the null candidate that says gathering is over."""

    def __init__(self, event_type=MISSING, options=None, *_extra, **kw):
        event_type, options = event_constructor_arguments(
            'RTCPeerConnectionIceEvent', event_type, options)
        super().__init__(event_type, options, **kw)
        candidate = event_init(options).get("candidate", pyv8.Null)
        self._candidate = pyv8.Null if candidate is None else candidate

    def get_candidate(self): return self._candidate


def _applied_description(description):
    """The description to apply; a missing one stands for the implicit offer."""
    if description is MISSING or is_nullish(description):
        return RTCSessionDescription({"type": "offer", "sdp": _offer_sdp()})
    if isinstance(description, RTCSessionDescription):
        return description
    options = dictionary(description)
    return RTCSessionDescription({"type": options.get("type", ""),
                                  "sdp": options.get("sdp", "")})


@interface('RTCDataChannel', 'EventTarget')
class RTCDataChannel(EventTarget):
    """A channel that is ``connecting`` and never opens: there is nowhere to send.

    ``priority`` is left to the baseline, which answers ``undefined`` on this build.
    """

    def __init__(self, label="", services=None, _realm=None, **_):
        if services is pyv8.Undefined:
            services = None
        super().__init__(services=services or _realm)
        self._label = str(label)
        self._ready_state = "connecting"
        self._binary_type = "arraybuffer"
        self._low_threshold = 0

    def get_label(self): return self._label
    def get_ordered(self): return True
    def get_maxPacketLifeTime(self): return pyv8.Null
    def get_maxRetransmits(self): return pyv8.Null
    def get_protocol(self): return ""
    def get_negotiated(self): return False
    def get_id(self): return pyv8.Null
    def get_readyState(self): return self._ready_state
    def get_bufferedAmount(self): return 0
    def get_bufferedAmountLowThreshold(self): return self._low_threshold
    def get_binaryType(self): return self._binary_type
    def get_reliable(self): return True

    def set_bufferedAmountLowThreshold(self, value):
        self._low_threshold = integer(value)
    def set_binaryType(self, value):
        self._binary_type = str(dom_string(value))

    def fn_send(self, data=MISSING, *_extra):
        if data is MISSING:
            raise missing_error('RTCDataChannel', 'send', 1, 0, constructor=False)
        if self._ready_state != "open":
            raise InvalidStateError("RTCDataChannel.readyState is not 'open'")
    def fn_close(self, *_extra):
        """Chrome reports ``closing`` here: the close procedure has not finished."""
        if self._ready_state != "closed":
            self._ready_state = "closing"


#: Accessors only: the baseline says which of these the channel publishes.
_install_handler_slots(RTCDataChannel, _CHANNEL_HANDLERS)


@interface('RTCPeerConnection', 'EventTarget')
class RTCPeerConnection(EventTarget):
    """A connection that never negotiates: every state reads as a fresh one."""

    def __init__(self, configuration=MISSING, *_extra, services=None, _realm=None, **kw):
        if services is pyv8.Undefined:
            services = None
        super().__init__(services=services or _realm, **kw)
        self._services = services or _realm
        self._signaling_state = "stable"
        self._connection_state = "new"
        self._ice_gathering_state = "new"
        self._local_description = None
        self._pending_local_description = None

    def get_localDescription(self):
        return self._local_description if self._local_description is not None else pyv8.Null
    def get_currentLocalDescription(self):
        return (self._local_description if self._local_description is not None
                and self._pending_local_description is None else pyv8.Null)
    def get_pendingLocalDescription(self):
        return (self._pending_local_description if self._pending_local_description is not None
                else pyv8.Null)
    def get_remoteDescription(self): return pyv8.Null
    def get_currentRemoteDescription(self): return pyv8.Null
    def get_pendingRemoteDescription(self): return pyv8.Null
    def get_signalingState(self): return self._signaling_state
    def get_iceGatheringState(self): return self._ice_gathering_state
    def get_iceConnectionState(self): return "new"
    def get_connectionState(self): return self._connection_state
    def get_canTrickleIceCandidates(self): return pyv8.Null
    def get_sctp(self): return pyv8.Null

    def fn_createDataChannel(self, label=MISSING, options=MISSING, *_extra):
        if label is MISSING:
            raise missing_error('RTCPeerConnection', 'createDataChannel', 1, 0,
                                constructor=False)
        return RTCDataChannel(label=str(dom_string(label)), services=self._services)

    def fn_createOffer(self=None, options=MISSING, *_extra):
        """Offering does not move the connection on its own, as Chrome's state shows."""
        if not isinstance(self, RTCPeerConnection):
            return _invalid_peer_receiver('createOffer')
        return _settled_promise(self._services,
                                RTCSessionDescription({"type": "offer", "sdp": _offer_sdp()}))

    def fn_createAnswer(self=None, options=MISSING, *_extra):
        """There is no remote offer to answer, which Chrome reports the same way."""
        if not isinstance(self, RTCPeerConnection):
            return _invalid_peer_receiver('createAnswer')
        return _rejected_promise(self._services, dom_exception(
            "InvalidStateError",
            "Failed to execute 'createAnswer' on 'RTCPeerConnection': "
            "Cannot create an answer in the 'stable' state."))

    def fn_setLocalDescription(self=None, description=MISSING, *_extra):
        """Applying a description moves the state, then gathering starts and ends.

        There is no transport. Gathering still completes, and reports one host
        candidate first when the environment names ``webrtc.ip``.
        """
        invalid = promise_receiver_error(self, RTCPeerConnection, 'setLocalDescription')
        if invalid is not None:
            return invalid
        if not isinstance(self, RTCPeerConnection):
            return _invalid_peer_receiver('setLocalDescription')
        applied = _applied_description(description)
        self._local_description = applied
        self._pending_local_description = applied
        self._signaling_state = ("have-local-offer" if applied.get_type() == "offer"
                                 else "stable")
        realm = getattr(self._services, "realm_services", None)
        if realm is not None:
            realm.post_task(self._gathering_steps)
        return _settled_promise(self._services, None)

    def _gathering_steps(self):
        """The events Chrome fires once a description is in place, in its order.

        A configured host address is one candidate between gathering and complete.
        The empty candidate still marks the end, and the SDP ``c=`` line stays
        ``0.0.0.0``.
        """
        self._dispatch_event(Event("signalingstatechange"))
        self._ice_gathering_state = "gathering"
        self._dispatch_event(Event("icegatheringstatechange"))
        address = _configured_webrtc_ip(self._services)
        if address is not None:
            self._dispatch_event(RTCPeerConnectionIceEvent(
                "icecandidate",
                {"isTrusted": True, "candidate": _host_candidate(address)}))
        self._ice_gathering_state = "complete"
        self._dispatch_event(Event("icegatheringstatechange"))
        self._dispatch_event(RTCPeerConnectionIceEvent("icecandidate",
                                                        {"isTrusted": True, "candidate": pyv8.Null}))

    def fn_close(self, *_extra):
        """Chrome closes both states at once, whatever the connection was doing."""
        self._signaling_state = "closed"
        self._connection_state = "closed"

    def fn_getConfiguration(self, *_extra):
        return {name: list(value) if isinstance(value, tuple) else value
                for name, value in CONFIGURATION}

    def fn_getSenders(self, *_extra): return []
    def fn_getReceivers(self, *_extra): return []
    def fn_getTransceivers(self, *_extra): return []
    def fn_getLocalStreams(self, *_extra): return []
    def fn_getRemoteStreams(self, *_extra): return []


#: Accessors only: the baseline says which of these the connection publishes.
_install_handler_slots(RTCPeerConnection, _PEER_HANDLERS)


__all__ = ["RTCRtpReceiver", "RTCDataChannel", "RTCPeerConnection",
           "RTCIceCandidate", "RTCSessionDescription", "RTCPeerConnectionIceEvent"]
