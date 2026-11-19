"""The built-in FeatureCatalog.  Keep this module declaration-only."""

from .runtime import FeatureCatalog, FeatureSpec

from .core_registry import CORE_INTERFACES

INDEXEDDB_INTERFACES = (
    "IDBRecord", "IDBVersionChangeEvent", "IDBRequest", "IDBOpenDBRequest", "IDBKeyRange",
    "IDBCursor", "IDBCursorWithValue", "IDBTransaction", "IDBObjectStore", "IDBIndex", "IDBDatabase", "IDBFactory",
)
NAVIGATOR_INTERFACES = (
    "MimeType", "Plugin", "PluginArray", "MimeTypeArray", "Geolocation",
    "NetworkInformation", "BatteryManager", "DeprecatedStorageQuota", "Navigator",
    "NavigatorUAData", "MediaDeviceInfo", "InputDeviceInfo", "MediaDevices", "Permissions",
    "PermissionStatus",
    "MediaMetadata", "MediaSession", "ServiceWorkerContainer", "CredentialsContainer",
    "Bluetooth", "StorageManager", "Serial", "GPU", "WGSLLanguageFeatures",
)
FILESYSTEM_INTERFACES = (
    "DOMFileSystem", "Entry", "DirectoryEntry", "FileEntry", "DirectoryReader", "FileWriter", "File",
)
WORKER_INTERFACES = ("WorkerGlobalScope", "DedicatedWorkerGlobalScope", "Worker",
                     "SharedWorkerGlobalScope", "SharedWorker", "WorkerLocation")
AUDIO_INTERFACES = (
    "AudioBuffer", "AudioParam", "AudioNode", "AudioScheduledSourceNode", "OscillatorNode",
    "AudioBufferSourceNode",
    "AnalyserNode", "GainNode", "DynamicsCompressorNode", "AudioDestinationNode",
    "AudioListener", "PeriodicWave",
    "BiquadFilterNode", "ChannelMergerNode", "ChannelSplitterNode", "ConvolverNode",
    "ConstantSourceNode", "DelayNode", "IIRFilterNode", "PannerNode", "StereoPannerNode",
    "WaveShaperNode", "ScriptProcessorNode", "AudioProcessingEvent",
    "OfflineAudioCompletionEvent",
    "BaseAudioContext", "AudioContext", "OfflineAudioContext",
)
#: The realm stages these before its first script runs; a plan seeds from them.
REALM_SKELETON = (
    "Window", "EventTarget", "Event", "Node", "Document", "Element",
    "HTMLElement", "URL", "URLSearchParams", "DOMTokenList", "NodeList",
    "HTMLCollection", "NamedNodeMap",
)


CATALOG = FeatureCatalog((
    FeatureSpec("core", "pyv8c.features.core", CORE_INTERFACES,
                ("location", "screen", "localStorage", "sessionStorage", "history"),
                eager=True, atomic=False, skeleton=REALM_SKELETON),
    FeatureSpec("navigator", "pyv8c.web.bom.navigator", NAVIGATOR_INTERFACES, ("navigator",)),
    FeatureSpec("drag_data", "pyv8c.web.dom.drag_data",
                ("DataTransfer", "DataTransferItem", "DataTransferItemList"), requires=("core",)),
    FeatureSpec("compression_streams", "pyv8c.web.bom.compression_streams",
                ("CompressionStream", "DecompressionStream", "ReadableStream",
                 "WritableStream", "ReadableStreamDefaultController", "ReadableStreamDefaultReader",
                 "WritableStreamDefaultWriter", "WritableStreamDefaultController"), requires=("core",)),
    FeatureSpec("media_recorder", "pyv8c.web.dom.media_recorder",
                ("MediaRecorder",), requires=("core",)),
    FeatureSpec("credentials", "pyv8c.web.bom.credentials",
                ("Credential", "PublicKeyCredential"), requires=("core",)),
    FeatureSpec("indexeddb", "pyv8c.web.bom.indexeddb", INDEXEDDB_INTERFACES, ("indexedDB",), requires=("core",)),
    FeatureSpec("filesystem", "pyv8c.web.bom.filesystem", FILESYSTEM_INTERFACES, requires=("core",)),
    FeatureSpec("websocket", "pyv8c.web.websocket", ("WebSocket",), requires=("core",)),
    FeatureSpec("workers", "pyv8c.web.workers.worker", WORKER_INTERFACES, requires=("core",)),
    FeatureSpec("audio", "pyv8c.web.bom.audio", AUDIO_INTERFACES, requires=("core",)),
))
