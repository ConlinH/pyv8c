"""Canvas offscreen implementation."""

from pyv8c import pyv8
from ....idl import interface
from ....foundation.arguments import MISSING, dictionary, missing_error
from ...dom.events import EventTarget
from ..webgl.context import WebGL2RenderingContext, WebGLRenderingContext
from .context2d import CanvasRenderingContext2D
from .contexts import requested_context, creation_settings, ImageBitmapRenderingContext, GPUCanvasContext
from .encoding import normalize_encoding_type, normalize_encoding_quality, encode_canvas


@interface('OffscreenCanvasRenderingContext2D', 'CanvasRenderingContext2D')
class OffscreenCanvasRenderingContext2D(CanvasRenderingContext2D):
    """The 2D context an ``OffscreenCanvas`` hands out: the same surface, no element."""


@interface('OffscreenCanvas', 'EventTarget')
class OffscreenCanvas(EventTarget):
    """A canvas with no element behind it: the page keeps the surface for itself."""

    __idl_overrides__ = {
        "interface": {
            "length": 2
        }
    }

    def __init__(self, width=MISSING, height=MISSING, *args, services=pyv8.Undefined,
                 _realm=None, **kwargs):
        if services is pyv8.Undefined:
            services = None
        super().__init__(*args, services=services or _realm, **kwargs)
        if width is MISSING:
            raise missing_error('OffscreenCanvas', 'constructor', 2, 0, constructor=True)
        if height is MISSING:
            raise missing_error('OffscreenCanvas', 'constructor', 2, 1, constructor=True)
        self._width = max(0, int(float(width)))
        self._height = max(0, int(float(height)))
        self._context2d = self._webgl = self._webgl2 = self._context_kind = None

    def _resize(self):
        if self._context2d: self._context2d._reset()
    def get_width(self): return self._width
    def set_width(self, value): self._width = max(0, int(float(value))); self._resize()
    def get_height(self): return self._height
    def set_height(self, value): self._height = max(0, int(float(value))); self._resize()
    def get_oncontextlost(self): return self._get_handler("oncontextlost")
    def set_oncontextlost(self, value): self._set_handler("oncontextlost", value)
    def get_oncontextrestored(self): return self._get_handler("oncontextrestored")
    def set_oncontextrestored(self, value): self._set_handler("oncontextrestored", value)

    def fn_getContext(self, kind='', options=None, *_):
        requested = requested_context(kind)
        if requested is None:
            return pyv8.Null
        if (requested in ('webgl', 'webgl2', 'webgpu') and
                not getattr(self._services, 'gpu_enabled', True)):
            return pyv8.Null
        if self._context_kind not in (None, requested): return pyv8.Null
        if requested == '2d':
            if self._context2d is None:
                settings = creation_settings(type(self).__name__, options)
                self._context2d = OffscreenCanvasRenderingContext2D(self)
                self._context2d._creation = settings
            self._context_kind = requested
            return self._context2d
        self._context_kind = requested
        if requested == 'webgl2':
            if self._webgl2 is None: self._webgl2 = WebGL2RenderingContext(self)
            return self._webgl2
        if requested == 'bitmaprenderer':
            if getattr(self, '_bitmap', None) is None:
                self._bitmap = ImageBitmapRenderingContext(self)
            return self._bitmap
        if requested == 'webgpu':
            if getattr(self, '_gpu', None) is None:
                self._gpu = GPUCanvasContext(self)
            return self._gpu
        if self._webgl is None: self._webgl = WebGLRenderingContext(self)
        return self._webgl

    def fn_convertToBlob(self, options=None, *_):
        """Encode the existing surface without changing its context type."""
        options = dictionary(options)
        if not self._width or not self._height:
            from ...page_errors import dom_exception, js_error_value
            realm = getattr(getattr(self, '_services', None), 'realm_services', None)
            if realm is None:
                return None
            promise, _resolve, reject = realm.make_promise()
            reject(js_error_value(dom_exception('IndexSizeError', 'The canvas has no pixels.')))
            return promise
        kind = normalize_encoding_type(options.get('type', MISSING))
        quality = normalize_encoding_quality(options.get('quality'))
        kind, data = encode_canvas(self, kind, quality)
        if data is None:
            from ...page_errors import dom_exception, js_error_value
            realm = getattr(getattr(self, '_services', None), 'realm_services', None)
            if realm is None:
                return None
            promise, _resolve, reject = realm.make_promise()
            reject(js_error_value(dom_exception('EncodingError', 'The canvas could not be encoded.')))
            return promise
        from ...fileapi.blob import Blob
        return self._promise(Blob([data], {"type": kind}))

    def fn_transferToImageBitmap(self, *_):
        """No ``ImageBitmap`` here, so the call answers with nothing to hand over."""
        return None
    def fn_clearElementGeometry(self, *_): return None
    def fn_getElementTransform(self, *_): return None
    def fn_updateElementGeometry(self, *_): return None

    def _promise(self, value):
        realm = getattr(getattr(self, "_services", None), "realm_services", None)
        if realm is None:
            return None
        promise, resolve, _reject = realm.make_promise()
        resolve(value)
        return promise
