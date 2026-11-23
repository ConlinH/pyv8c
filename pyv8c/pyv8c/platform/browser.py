"""Browser and profile: the lifecycle that owns a Page."""
from __future__ import annotations

import os
import asyncio
import math
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pyv8c import pyv8  # patch target for native V8Context instrumentation

from ..engine.bindings import Idl, Bindings
from ..foundation.close_timing import close_phase
from ..foundation.environment import BrowserEnvironment
from ..foundation.error_policy import control_exceptions
from ..foundation.host_errors import HostStateError, LoadFailure
from ..foundation.urls import origin_string, origin_tuple
from ..network.assembly import assemble_backend
from ..network.proxy import normalize_proxy
from ..network.registry import KIND_METHODS, HttpTransport, WebSocketTransport
from ..network.websocket_io import WebSocketIO
from ..network.websockets import WebSocketsTransport
from .bootstrap import bootstrap_runtime
from ..foundation.futures import consume_future
from .runtime import Runtime, RuntimeState
from .page import Page
from .profile_storage import ProfileStorage
from .resources.cache import ResourceCache
from .resources.cors import PreflightCache

#: With ``threaded_loaders``, loader and interceptor callbacks run here instead of on
#: the page's owner thread, or a slow one would freeze the realm.
_LOADER_WORKERS = 4
_TRACE_SCRIPTS_INHERIT = object()
_READY_ENGINE_LOCK = threading.Lock()
_READY_ENGINE_CLAIMS = {}


def _claim_ready_engines(engines, token):
    with _READY_ENGINE_LOCK:
        for engine in engines:
            claim = _READY_ENGINE_CLAIMS.get(id(engine))
            if claim is not None and claim[1] is not token:
                raise HostStateError('A ready network engine is already owned by another profile; use a factory')
        for engine in engines:
            _READY_ENGINE_CLAIMS[id(engine)] = (engine, token)


def _release_ready_engines(token):
    with _READY_ENGINE_LOCK:
        for key, (_, owner) in list(_READY_ENGINE_CLAIMS.items()):
            if owner is token:
                del _READY_ENGINE_CLAIMS[key]


def _cleanup_failed_engines(engines, browser, token):
    """Release only newly constructed engines not owned by another profile."""
    for engine in engines:
        if engine is browser.http_engine or engine is browser.ws_engine:
            continue
        with _READY_ENGINE_LOCK:
            claim = _READY_ENGINE_CLAIMS.get(id(engine))
            belongs_elsewhere = claim is not None and claim[1] is not token
        if not belongs_elsewhere:
            try:
                close = getattr(engine, 'close', None)
                if callable(close): close()
            except Exception:
                pass


def _trace_script_patterns(value):
    """Freeze a source-URL filter without treating a string as characters."""
    if value is None:
        return None
    if isinstance(value, str):
        patterns = (value,)
    else:
        try:
            patterns = tuple(value)
        except TypeError as exc:
            raise TypeError("trace_scripts must be a string or iterable of strings") from exc
    if not patterns or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
        raise ValueError("trace_scripts must contain non-empty string patterns")
    return patterns


class Profile:
    """A browser profile: its own cookies, blob URLs and network stack.

    Pages created here share that state, so a document, the ``fetch`` calls it
    makes and a ``page.net`` request are one session as far as a server is
    concerned; ``profile.pages`` lists the pages still open and
    ``profile.cookies`` is the jar behind them.  Profiles of one browser share
    nothing else -- cookies, cached responses and engines are per profile --
    except for a disk cache directory when one was configured.
    """

    def __init__(self, browser, *, storage, proxy=None, trace=None, traceable=None, trace_callback=None,
                 trace_scripts=_TRACE_SCRIPTS_INHERIT, _engine_token=None):
        self.browser, self.pages = browser, []
        self._state_lock = threading.RLock()
        self._closing = False
        self._close_task = None
        self._storage = storage
        self.cookies = storage.cookies
        #: Its own proxy setting, or the browser's while it was given none.
        self.proxy = browser.proxy if proxy is None else proxy
        #: Trace settings for this profile alone; ``None`` keeps the browser's.
        self.trace = browser.trace if trace is None else bool(trace)
        self.traceable = (browser.traceable if traceable is None else bool(traceable)) or self.trace
        self.trace_callback = browser.trace_callback if trace_callback is None else trace_callback
        self.trace_scripts = (browser.trace_scripts if trace_scripts is _TRACE_SCRIPTS_INHERIT
                              else _trace_script_patterns(trace_scripts))
        self.resource_cache_dir = browser.cache_dir if browser.cache_enabled else None
        self.resource_cache = ResourceCache(self)
        self.preflight_cache = PreflightCache()
        self._client_hints_lock = threading.Lock()
        self._accept_ch_hints = {}
        token = _engine_token if _engine_token is not None else object()
        self.network_backend = assemble_backend(browser, proxy=self.proxy)
        if self.network_backend is not None:
            engines = self.network_backend._engine_instances
            try:
                _claim_ready_engines(engines, token)
                if isinstance(self.network_backend.ws, WebSocketsTransport):
                    self.network_backend.ws._share_io(browser._websocket_io)
                self.network_backend.cookies = self.cookies
            except BaseException:
                _cleanup_failed_engines(engines, browser, token)
                _release_ready_engines(token)
                raise
        self._ready_engine_token = token
        self._blob_urls = {}
        self._blob_origins = {}
        self._databases = {}
        self._file_systems = {}
        self.closed = False
        self._devtools = None

    def _ensure_open(self):
        if self.closed or self._closing or self.browser._closed or self.browser._closing:
            raise HostStateError("Profile is closed")

    def file_system_state(self, key, factory):
        """Share passive storage data, never realm-bound handles, within a partition."""
        with self._state_lock:
            self._ensure_open()
            if key not in self._file_systems:
                self._file_systems[key] = factory()
            return self._file_systems[key]

    def create_blob_url(self, blob, realm=None):
        """Mint a ``blob:`` URL whose origin is the creating realm's origin.

        The blob inherits the *creator* origin rather than one derived from the
        serialized URL: two opaque origins must stay distinct, so a fresh
        ``origin_tuple`` would make ``blob:null/...`` documents look unrelated
        to the page that created them.
        """
        self._ensure_open()
        url_source = "about:blank" if realm is None else getattr(realm, "url", "about:blank")
        url = "blob:" + origin_string(url_source) + "/" + str(uuid.uuid4())
        self._blob_urls[url] = (bytes(blob._data), blob.get_type())
        creator_origin = None if realm is None else getattr(realm, "origin", None)
        self._blob_origins[url] = creator_origin if creator_origin is not None else origin_tuple(url_source)
        return url

    def resolve_blob_url(self, url):
        self._ensure_open()
        if url not in self._blob_urls:
            raise LoadFailure("revoked or unknown Blob URL")
        return self._blob_urls[url]

    def revoke_blob_url(self, url):
        self._ensure_open()
        self._blob_urls.pop(url, None)
        self._blob_origins.pop(url, None)

    # Profile-level services exposed to realm objects without exposing storage.
    @property
    def cookie_jar(self):
        return self.cookies

    def accepted_client_hints(self, url):
        origin = origin_string(url)
        if origin == 'null': return ()
        with self._client_hints_lock:
            return self._accept_ch_hints.get(origin, ())

    def remember_client_hints(self, url, headers):
        origin = origin_string(url)
        if origin == 'null': return
        entry = next((value for name, value in headers.items() if str(name).lower() == 'accept-ch'), None)
        if entry is None: return
        hints = frozenset(name.strip().lower() for name in str(entry).split(',') if name.strip())
        with self._client_hints_lock:
            self._accept_ch_hints[origin] = hints

    def new_page(self,
                 *,
                 trace: bool | None = None,
                 trace_scripts=_TRACE_SCRIPTS_INHERIT,
                 environment: dict | BrowserEnvironment | None = None) -> Page:
        """Create a page in this profile and return it.

        The page starts with an empty document. Navigate with
        ``await page.goto(url)`` after construction.

        :param trace: start this page traced; ``None`` inherits the Browser
            setting.
        :param trace_scripts: source-URL Glob patterns for this page; omitted
            inherits the profile, while ``None`` removes its filter.
        :param environment: keys overlaying the Browser environment for this page
            only.
        :returns: the new :class:`Page <pyv8c.platform.page.Page>`, appended to
            :attr:`pages`.
        :raises InvalidStateError: the profile or its browser is closed.
        """
        self.browser._runtime.check_owner()
        self._ensure_open()
        selected_scripts = (self.trace_scripts if trace_scripts is _TRACE_SCRIPTS_INHERIT
                            else _trace_script_patterns(trace_scripts))
        page = self._create_page(trace=trace, trace_scripts=selected_scripts,
                    environment=environment)
        with self._state_lock:
            accepted = not (self.closed or self._closing or self.browser._closed or self.browser._closing)
            if accepted:
                self.pages.append(page)
        if not accepted:
            page._retire()
            raise HostStateError('Profile is closed or closing')
        return page

    def _create_page(self, **kwargs):
        return Page(self, **kwargs)

    def _discard_page(self, page):
        with self._state_lock:
            try:
                self.pages.remove(page)
            except ValueError:
                pass

    def start_devtools(self,
                       page: Page,
                       *,
                       options: pyv8.DevToolsOptions | None = None) -> pyv8.DevToolsHandle:
        """Attach a DevTools server to *page* and return its handle.

        One server per profile, driving a single debugger client; the supported
        protocol domains and the startup handshake are in ``docs/pyv8c/devtools.md``.

        :param page: a page of this profile that is still open.
        :param options: :class:`pyv8.DevToolsOptions`; the defaults bind
            ``127.0.0.1`` on a system-chosen port and pause once, at the next
            host statement after the handshake.
        :returns: the handle -- ``url``, ``wait_for_connection``,
            ``wait_for_ready``, ``close``, and a context manager.
        :raises InvalidStateError: the profile is closed, the page belongs to
            another profile or is closed, or a server is already attached.
        """
        self.browser._runtime.check_owner()
        self._ensure_open()
        if page not in self.pages or page.closed:
            raise HostStateError("Page does not belong to this profile")
        if self._devtools is not None:
            raise HostStateError("Profile already has DevTools attached")
        from .devtools import start_devtools
        return start_devtools(self, page, options=options)

    def _retire_pages(self):
        self.browser._runtime.check_owner(allow_closing=True)
        self._closing = True
        first_error = None
        for page in tuple(self.pages):
            try:
                page._retire()
            except control_exceptions():
                raise
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def close(self):
        kernel = self.browser._runtime
        kernel.check_thread()
        kernel.check_loop()
        task = self._begin_close()
        if task is not None:
            await asyncio.shield(task)

    async def flush(self):
        """Commit cookies and local storage changed before this call."""
        kernel = self.browser._runtime
        kernel.check_owner()
        kernel.check_loop()
        self._ensure_open()
        await self._storage.flush()

    def _begin_close(self):
        kernel = self.browser._runtime
        kernel.check_thread()
        if self.closed:
            return
        kernel.check_owner(allow_closing=True)
        task = self._close_task
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            self._retire_pages()
            task = self._close_task = kernel.loop.create_task(self._finish_close(tuple(self.pages)))
            task.add_done_callback(consume_future)
        return task

    async def __aenter__(self):
        self.browser._runtime.check_owner()
        self.browser._runtime.check_loop()
        self._ensure_open()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _finish_close(self, pages):
        first_error = None
        try:
            with close_phase(self, "pages"):
                results = await asyncio.gather(*(page.close() for page in pages), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and first_error is None:
                    first_error = result
            if first_error is not None:
                raise first_error
            for name in ("_devtools", "network_backend"):
                resource = getattr(self, name)
                if resource is not None:
                    try:
                        with close_phase(self, name):
                            if name == "network_backend":
                                await asyncio.to_thread(resource.close)
                            else:
                                resource.close()
                    except control_exceptions():
                        raise
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                    else:
                        setattr(self, name, None)
            if first_error is not None:
                raise first_error
            with close_phase(self, "profile-storage"):
                await self._storage.close()
            self.closed = True
            self.resource_cache.clear_memory()
            self.preflight_cache.clear()
            token = getattr(self, '_ready_engine_token', None)
            if token is not None:
                _release_ready_engines(token)
                self._ready_engine_token = None
            if self in self.browser.profiles:
                self.browser.profiles.remove(self)
        finally:
            self._blob_urls.clear()
            self._blob_origins.clear()
            self._databases.clear()
            self._file_systems.clear()


class Browser:
    """A browser: the configuration every profile and page starts from.

    One instance owns the native runtime, the interface baseline and the
    environment its profiles derive from.  :meth:`new_profile` creates an
    isolated session and :meth:`close` closes every session it handed out; the
    engines a profile needs are built when that profile is created.

    JS objects belong to the thread that owns their realm (see ``docs/pyv8c/usage.md``).
    """

    def __init__(self, *,
                 environment: dict | BrowserEnvironment | None = None,
                 real_network: bool = True,
                 resource_loader: Callable | None = None,
                 resource_interceptor: Callable | None = None,
                 threaded_loaders: bool = False,
                 frame_rate: float = 60.0,
                 http_engine: Literal["cronet", "curl_cffi"] | HttpTransport | Callable | None = "curl_cffi",
                 http_options: dict | None = None,
                 ws_engine: Literal["websockets"] | WebSocketTransport | Callable | None = "websockets",
                 ws_options: dict | None = None,
                 proxy: str | None = None,
                 default_headers: dict | None = None,
                 traceable: bool = False,
                 trace: bool = False,
                 trace_callback: Callable | None = None,
                 trace_scripts=None,
                 cache: bool = True,
                 cache_dir: str | os.PathLike | None = None,
                 expose_unimplemented: bool = True,
                 stub_logging: Literal["on", "verbose", "off"] = "off",
                 interface_overrides: dict | None = None,
                 gpu_enabled: bool = True,
                 webgl_backend: Literal["simulated", "angle"] = "simulated",
                 angle_library_dir: str | os.PathLike | None = None,
                 idl_path: str | os.PathLike | None = None,
                 baseline_mismatch: Literal["error", "warn"] = "warn"):
        """Configure one browser.

        Every value is read here: native templates and the interface baseline are
        assembled in this call and are not reconfigured later, so another baseline
        means another Browser.

        :param environment: flat "dotted key" dict or
            :class:`~pyv8c.foundation.environment.BrowserEnvironment`; keys left
            out come from the bundled Chrome profile.  Profiles and pages may
            overlay it.
        :param real_network: assemble ``http_engine`` and ``ws_engine``; ``False``
            keeps this browser offline, and a ready engine handed to either of them
            is then refused rather than dropped.  The default engines are core
            dependencies.
        :param resource_loader: synchronous ``loader(url, request) -> Response``
            answering every load this browser performs; what it answers never
            reaches a transport.  The offline way to serve a page.
        :param resource_interceptor: ``interceptor(url, request)`` consulted
            before each load: ``None``/``True`` loads normally, ``False`` answers
            with an empty 200, anything else (``Response``, dict, str) is the
            response.
        :param threaded_loaders: run ``resource_loader`` and
            ``resource_interceptor`` on a bounded worker pool instead of the page's
            owner thread, so a callback blocked on its own I/O does not freeze the
            realm.  A callback running there may not touch the page, ``page.evaluate``
            and this host's own cache included.
        :param frame_rate: virtual rendering clock frequency in Hz; defaults to
            60. Iframes share their page tree's clock; workers drive their own.
        :param http_engine: the HTTP engine -- a registered name (built with
            ``http_options``), a ready object with ``submit_request``, or a factory
            called once per profile; ``None`` leaves the role empty.  Further
            engines can be added with
            :func:`~pyv8c.network.registry.register_transport`.
        :param http_options: options handed to the HTTP engine; a key it does not
            accept is refused when a profile is created rather than reaching it.
        :param ws_engine: the WebSocket engine -- a registered name (built with
            ``ws_options``), a ready object with ``websocket``, or a factory called
            once per profile; ``None`` leaves the role empty.  Further engines can
            be added with
            :func:`~pyv8c.network.registry.register_ws_transport`.
        :param ws_options: options handed to the WebSocket engine, same rule.
        :param proxy: the ``http://`` or ``https://`` proxy every request and
            WebSocket handshake of this browser's profiles goes through, with
            any credentials in the URL (``http://user:pass@host:port``).  Left
            out, traffic goes directly even when the environment names a proxy;
            ``""`` asks for the same thing explicitly.  A profile may override
            it with :meth:`new_profile`, and SOCKS is not supported.
        :param default_headers: headers every ``page.net`` request starts from.
        :param trace: start each page with tracing on.
        :param trace_callback: receives trace events instead of Loguru.
        :param trace_scripts: one or more full script-URL Glob patterns.  ``None``
            reports all scripts; ``*`` and ``?`` are wildcards.
        :param traceable: allow a page to switch tracing on later (see
            :attr:`Page.trace <pyv8c.platform.page.Page.trace>`); ``trace=True``
            implies it.
        :param cache: enable the per-profile resource cache.
        :param cache_dir: keep that cache on disk under this directory; without
            it resource responses are not cached.
        :param expose_unimplemented: let the exposure plan include baseline
            interfaces with no hand-written implementation (they answer with
            stubs).
        :param stub_logging: ``"off"`` (the default) reports nothing, ``"on"``
            reports each unimplemented member once, and ``"verbose"`` also reports
            the placeholder handlers the baseline required.
        :param interface_overrides: hooks that answer members of the baseline
            instead of their implementation, as
            ``{"InterfaceName": {"memberName": hook}}``.  A hook is a plain function
            that receives what the member's position passes -- ``(receiver, *args)``
            for a prototype or instance method, ``(*args)`` for one on the interface
            itself, ``(receiver)`` for an attribute read and ``(receiver, value)``
            for a write -- and what it returns is what the script sees.  An entry may
            name the slot instead, as ``{"get": hook, "set": hook}``; a write hook
            also makes the member writable.  An unknown interface, an unknown member,
            a slot that member cannot answer, a write hook for a member the baseline
            calls read-only, a bound method and anything not callable are refused
            here.  Constructors, constants and the named/indexed handlers are not
            covered.
        :param webgl_backend: select the default simulated backend or the
            experimental ANGLE WebGL 1 subset. Install ``pyv8c[angle]`` for ANGLE.
            The experimental backend is not suitable for untrusted content.
        :param gpu_enabled: expose GPU-backed canvas contexts and adapter requests.
            False makes WebGL/WebGPU context creation and WebGPU adapter requests
            return null, while keeping CPU Canvas 2D available.
        :param angle_library_dir: use ANGLE libraries from this directory instead
            of comfy-angle; for compatibility, specifying it also selects ANGLE.
        :param idl_path: Chromium interface baseline JSON or ZIP to
            use instead of the bundled one.
        :param baseline_mismatch: ``"warn"`` (the default) logs a difference
            between hand-written implementations and the chosen baseline once and
            skips it, which is how another Chromium version's baseline is run;
            ``"error"`` fails at assembly instead, which is what keeps a mistake in
            one's own implementation from passing silently.
        """
        if trace_callback is not None and not callable(trace_callback):
            raise TypeError("trace_callback must be callable or None")
        self.trace_callback = trace_callback
        self.trace_scripts = _trace_script_patterns(trace_scripts)
        started = time.perf_counter_ns()
        self._runtime = Runtime()
        self._close_task = None
        self._default_profile = None
        self._state_lock = threading.Lock()
        self._ready_engine_reserved = False
        # pyv8 starts its global V8 runtime on first use, not at import, so the
        # first realm created below carries that cost.
        self.profiles = []
        self.resource_loader = resource_loader
        self.resource_interceptor = resource_interceptor
        self.threaded_loaders = bool(threaded_loaders)
        self.frame_rate = float(frame_rate)
        if not math.isfinite(self.frame_rate) or self.frame_rate <= 0:
            raise ValueError('frame_rate must be finite and positive')
        self._loader_pool = None
        self._loader_lock = threading.Lock()
        self.cache_enabled = bool(cache)
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir is not None else None
        #: ``False`` assembles no engines; a ready engine is then refused.
        self.real_network = bool(real_network)
        self.http_engine = http_engine
        self.ws_engine = ws_engine
        self._websocket_io = WebSocketIO()
        #: Options for the two engines; a key an engine does not accept is refused
        #: when a profile is created rather than passed on unread.
        self.http_options = dict(http_options or {})
        self.ws_options = dict(ws_options or {})
        #: The proxy profiles start from: ``None`` is direct, ``""`` asks for it.
        self.proxy = normalize_proxy(proxy)
        #: Headers every host request starts from, on top of the environment ones.
        self.default_headers = dict(default_headers or {})
        self.trace = bool(trace)
        self.traceable = bool(traceable) or self.trace
        self.environment = BrowserEnvironment.from_dict(environment)
        self.expose_unimplemented = bool(expose_unimplemented)
        self.stub_logging = str(stub_logging or "off")
        self._startup_stats = {}
        if not isinstance(gpu_enabled, bool):
            raise TypeError('gpu_enabled must be a bool')
        self.gpu_enabled = gpu_enabled
        self._graphics_backend = None
        if webgl_backend not in ("simulated", "angle"):
            raise ValueError("webgl_backend must be 'simulated' or 'angle'")
        if webgl_backend == "angle" or angle_library_dir is not None:
            if angle_library_dir is None:
                try:
                    import comfy_angle
                except ModuleNotFoundError as error:
                    if error.name != 'comfy_angle':
                        raise
                    raise ImportError("ANGLE backend requires comfy-angle; install pyv8c[angle]") from error
                angle_library_dir = comfy_angle.get_lib_dir()
            from ..web.graphics.webgl.backends.angle import AngleBackend
            self._graphics_backend = AngleBackend(angle_library_dir)
        snapshot = Idl.load(idl_path, timings=self._startup_stats)
        loaded = time.perf_counter_ns()
        self.bindings = Bindings(
            snapshot, runtime=bootstrap_runtime(),
            stub_logging=self.stub_logging,
            expose_unimplemented=self.expose_unimplemented,
            baseline_mismatch=baseline_mismatch,
            interface_overrides=interface_overrides,
        )
        validation_finished = validation_started = loaded
        self.idl_path = snapshot.path
        self.idl_info = snapshot.info
        plan = self.bindings.plan("Window")
        self._startup_stats.update({
            "validation_ms": (validation_finished - validation_started) / 1_000_000,
            "browser_init_ms": (validation_finished - started) / 1_000_000,
            "known_interfaces": len(plan.installs),
            "core_interfaces": len(plan.eager),
            "startup_interfaces": len(plan.ready),
            "deferred_interfaces": len(plan.lazy),
            "materialized_interfaces": 0,
            "binding_binder_ms": 0.0,
            "profile_create_ms": 0.0,
            "deferred_global_declare_ms": 0.0,
            "deferred_globals": 0,
            "deferred_materialized": 0,
            "baseline_discrepancies": len(self.bindings.discrepancies),
        })

    @property
    def startup_stats(self) -> MappingProxyType:
        """Read-only snapshot of this Browser's startup timings and counts.

        Baseline read/cache/compile/decode, validation and browser-init
        milliseconds, profile-creation cost, and how many interfaces the Window
        exposure plan knows, materialized or still deferred.  Set
        ``PYV8C_STARTUP_STATS=0`` to record nothing.
        """
        return MappingProxyType(dict(self._startup_stats))

    def _record_startup(self, **values):
        if os.environ.get("PYV8C_STARTUP_STATS", "1") != "0":
            self._startup_stats.update(values)

    def new_profile(self, *, proxy: str | None = None, trace: bool | None = None,
                    traceable: bool | None = None,
                    trace_callback: Callable | None = None,
                    trace_scripts=_TRACE_SCRIPTS_INHERIT,
                    user_data_dir: str | os.PathLike[str] | None = None,
                    persist_session_cookies: bool = False) -> Profile:
        """Create an isolated profile: its own cookies, blobs and network stack.

        :param proxy: where this profile's traffic goes, overriding
            :attr:`proxy` for it alone: left out it keeps the browser's setting,
            a URL sends it through that proxy instead, and ``""`` sends it
            directly even when the browser has one.  Profiles of one browser may
            therefore leave by different exits at the same time.
        :param trace: report trace events for this profile alone; ``None`` keeps
            the browser setting.
        :param traceable: let its pages switch tracing on later, same defaulting.
        :param trace_callback: receive this profile's events instead of the
            browser's callback.
        :param trace_scripts: override the browser's source-URL patterns;
            omitted inherits them, while ``None`` removes the filter.
        :param user_data_dir: optionally restore cookies and local storage from
            an exclusively held directory, before returning the profile.
        :param persist_session_cookies: also restore and save session cookies;
            requires a user data directory.
        :returns: the new :class:`Profile`, appended to :attr:`profiles`.
        :raises InvalidStateError: the browser is closed.
        :raises ValueError: the proxy value is not an ``http``/``https`` URL.
        """
        self._runtime.check_owner()
        with self._state_lock:
            if self._closed or self._closing:
                raise HostStateError("Browser is closed or closing")
        if trace_callback is not None and not callable(trace_callback):
            raise TypeError("trace_callback must be callable or None")
        if not isinstance(persist_session_cookies, bool):
            raise TypeError("persist_session_cookies must be a bool")
        if persist_session_cookies and user_data_dir is None:
            raise ValueError("persist_session_cookies requires user_data_dir")
        ready = []
        if self.real_network:
            for role, method in KIND_METHODS.items():
                value = getattr(self, f"{role}_engine")
                if (value is not None and not isinstance(value, (str, type))
                        and (not callable(value) or callable(getattr(value, method, None)))
                        and all(value is not existing for existing in ready)):
                    ready.append(value)
        uses_ready = bool(ready)
        token = object()
        if uses_ready:
            with self._state_lock:
                if self._closed or self._closing:
                    raise HostStateError('Browser is closed or closing')
                if self._ready_engine_reserved:
                    raise HostStateError('A ready network engine can serve only one profile; use a factory for additional profiles')
                self._ready_engine_reserved = True
        storage = None
        try:
            if uses_ready: _claim_ready_engines(ready, token)
            storage = ProfileStorage(self._runtime.loop, user_data_dir, persist_session_cookies)
            profile = self._create_profile(proxy=None if proxy is None else normalize_proxy(proxy),
                              trace=trace, traceable=traceable, trace_callback=trace_callback,
                              trace_scripts=trace_scripts, storage=storage, _engine_token=token)
        except BaseException:
            if storage is not None:
                storage.release()
            _release_ready_engines(token)
            if uses_ready:
                with self._state_lock: self._ready_engine_reserved = False
            raise
        with self._state_lock:
            accepted = not (self._closed or self._closing)
            if accepted:
                self.profiles.append(profile)
        if not accepted:
            profile._retire_pages()
            storage.release()
            raise HostStateError("Browser is closed or closing")
        storage.schedule()
        return profile

    def _create_profile(self, **kwargs):
        return Profile(self, **kwargs)

    @property
    def loader_pool(self):
        """The bounded pool the page's loader callbacks run on, created on first use."""
        with self._loader_lock:
            if self._closed:
                raise HostStateError("Browser is closed")
            if self._loader_pool is None:
                self._loader_pool = ThreadPoolExecutor(
                    max_workers=_LOADER_WORKERS, thread_name_prefix="pyv8c-loader")
            return self._loader_pool

    @property
    def _closed(self):
        return self._runtime.closed

    @property
    def _closing(self):
        return self._runtime.state in (RuntimeState.CLOSING, RuntimeState.CLOSE_FAILED)

    @property
    def closed(self):
        return self._runtime.closed

    async def __aenter__(self):
        self._runtime.open()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def new_page(self, **options):
        self._runtime.check_owner()
        if self._default_profile is None or self._default_profile.closed:
            self._default_profile = self.new_profile()
        return self._default_profile.new_page(**options)

    async def close(self):
        self._runtime.check_thread()
        self._runtime.check_loop()
        task = self._begin_close()
        if task is not None:
            await asyncio.shield(task)

    def _begin_close(self):
        self._runtime.check_thread()
        if self.closed:
            return self._close_task
        if self._close_task is None or (
            self._close_task.done() and (self._close_task.cancelled() or self._close_task.exception() is not None)
        ):
            self._runtime.begin_close()
            profiles = tuple(self.profiles)
            try:
                first_error = None
                retired = []
                for profile in profiles:
                    try:
                        with close_phase(profile, "retire-pages"):
                            profile._retire_pages()
                    except control_exceptions():
                        raise
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                    else:
                        retired.append(profile)
            except BaseException:
                self._runtime.fail_close()
                raise
            self._close_task = self._runtime.loop.create_task(self._finish_close(retired, first_error))
            self._close_task.add_done_callback(consume_future)
        return self._close_task

    async def _finish_close(self, profiles, first_error):
        try:
            with close_phase(self, "profiles"):
                results = await asyncio.gather(*(profile.close() for profile in profiles), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and first_error is None:
                    first_error = result
            if first_error is not None:
                raise first_error
            if self._websocket_io.started:
                with close_phase(self, "websocket-io"):
                    await asyncio.to_thread(self._websocket_io.close)
            with self._loader_lock:
                pool = self._loader_pool
            if pool is not None:
                self._runtime._retire_io_pool(pool)
            with close_phase(self, "runtime"):
                await self._runtime.close()
        except BaseException:
            self._runtime.fail_close()
            raise
