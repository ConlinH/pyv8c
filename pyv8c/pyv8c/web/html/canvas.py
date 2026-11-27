"""The HTML canvas tag and its rendering context selection."""
import base64
import re

from pyv8c import pyv8
from ...idl import interface
from ...foundation.arguments import MISSING, integer
from .elements import HTMLElement
from ..graphics.canvas.context2d import CanvasRenderingContext2D
from ..graphics.canvas.contexts import GPUCanvasContext, ImageBitmapRenderingContext, requested_context, creation_settings
from ..graphics.canvas.encoding import normalize_encoding_type, normalize_encoding_quality, encode_canvas
from ..graphics.webgl.context import WebGL2RenderingContext, WebGLRenderingContext


@interface('HTMLCanvasElement', 'HTMLElement')
class HTMLCanvasElement(HTMLElement):
    def __init__(self, tag_name="canvas", services=None, **kw):
        super().__init__(tag_name, services=services, **kw)
        self._width = self._dimension(self.attributes.get('width'), 300)
        self._height = self._dimension(self.attributes.get('height'), 150)
        self._context2d=None
        self._context_kind=None; self._webgl=None; self._webgl2=None; self._bitmap=None; self._gpu=None

    def _resize(self):
        if self._context2d: self._context2d._reset()
    def get_width(self): return self._width
    def set_width(self, v):
        self.attributes['width'] = str(integer(v))
        self._width = self._dimension(self.attributes['width'], 300)
        self._resize()

    def get_height(self): return self._height
    def set_height(self, v):
        self.attributes['height'] = str(integer(v))
        self._height = self._dimension(self.attributes['height'], 150)
        self._resize()

    def fn_getContext(self, kind='', options=None, *_):
        requested = requested_context(kind)
        if requested is None: return pyv8.Null
        if (requested in ('webgl', 'webgl2', 'webgpu') and
                not getattr(self._services, 'gpu_enabled', True)):
            return pyv8.Null
        # The first context a canvas answers for fixes its type, as it does in Chrome.
        if self._context_kind not in (None, requested): return pyv8.Null
        if requested == '2d':
            if self._context2d is None:
                settings = creation_settings(type(self).__name__, options)
                self._context2d=CanvasRenderingContext2D(self)
                self._context2d._creation = settings
            self._context_kind = requested
            return self._context2d
        self._context_kind = requested
        if requested == 'webgl2':
            if self._webgl2 is None: self._webgl2=WebGL2RenderingContext(self)
            return self._webgl2
        if requested == 'bitmaprenderer':
            if self._bitmap is None: self._bitmap=ImageBitmapRenderingContext(self)
            return self._bitmap
        if requested == 'webgpu':
            if self._gpu is None: self._gpu=GPUCanvasContext(self)
            return self._gpu
        if self._webgl is None: self._webgl=WebGLRenderingContext(self)
        return self._webgl
    def fn_toDataURL(self, kind=MISSING, quality=MISSING, *_):
        mime = normalize_encoding_type(kind)
        mime, encoded = encode_canvas(self, mime, normalize_encoding_quality(quality))
        if encoded is None:
            return 'data:,'
        return 'data:' + mime + ';base64,' + base64.b64encode(encoded).decode('ascii')

    @staticmethod
    def _dimension(value, default):
        match = re.match(r'^[\t\n\f\r ]*\+?(\d+)', str(value or ''))
        if match is None:
            return default
        value = int(match[1])
        return value if value < 2**31 else default


    def _attribute_changed(self, name, old, new):
        if name in ('width', 'height'):
            setattr(self, '_' + name, self._dimension(new, 300 if name == 'width' else 150))
            self._resize()


    def _attribute_changed_here(self, name, old, new):
        if name in ('width', 'height'):
            self._attribute_changed(name, old, new)
        else:
            super()._attribute_changed_here(name, old, new)



