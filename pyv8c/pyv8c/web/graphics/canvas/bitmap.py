"""Canvas bitmap implementation."""

from pyv8c import pyv8
from pyv8c._native import RasterCanvas, RasterImage
from ....idl import interface
from ....foundation.arguments import MISSING, IdlArgumentError, dictionary, integer, missing_error


def signature_error(interface, member):
    """What the bindings raise when no overload accepts the arguments they were given."""
    return IdlArgumentError(
        f"Failed to execute '{member}' on '{interface}':"
        " No function was found that matched the signature provided.")


@interface('ImageData', None)
class ImageData:
    """A rectangle of RGBA pixels, as ``getImageData`` hands it back."""

    def __init__(self, pixels=b"", width=0, height=0, **_):
        self._width, self._height = int(width), int(height)
        # A typed array passed to the Web constructor stays live and retains its
        # identity. Iterating the Python wrapper yields indices, not pixel values.
        self._data = (pixels if isinstance(pixels, pyv8.JSTypeArray)
                      else pyv8.JSTypeArray(list(pixels), pyv8.ArrayType.Uint8ClampedArray))

    def get_width(self): return self._width
    def get_height(self): return self._height
    def get_data(self): return self._data
    def get_colorSpace(self): return "srgb"
    def get_pixelFormat(self): return "rgba-unorm8"


@interface('ImageBitmap', None)
class ImageBitmap:
    """A decoded image: ``drawImage`` draws it, and ``close`` empties it."""

    def __init__(self, image=None, width=0, height=0, **_):
        self._image = image
        self._width, self._height = int(width), int(height)

    def get_width(self): return self._width
    def get_height(self): return self._height
    def fn_close(self, *_):
        """Emptied, as Chromium empties it: every later read reports 0."""
        self._image = None
        self._width = self._height = 0


_IMAGE_BITMAP_SOURCE = ("HTMLImageElement or SVGImageElement or HTMLVideoElement"
                        " or HTMLCanvasElement or Blob or ImageData or ImageBitmap"
                        " or OffscreenCanvas or VideoFrame")


def _is_bitmap_source(value):
    """Whether ``createImageBitmap`` may take this value."""
    return type(value).__name__ in ("HTMLImageElement", "SVGImageElement", "HTMLVideoElement",
                                    "HTMLCanvasElement", "Blob", "ImageData", "ImageBitmap",
                                    "OffscreenCanvas", "VideoFrame")


def raster_size_supported(width, height):
    return 0 < width <= 65535 and 0 < height <= 65535 and width * height <= 32768 * 8192


def bitmap_source_image(source):
    """The pixels behind an image source, or ``None`` when the engine has none for it."""
    if isinstance(source, ImageBitmap):
        return source._image
    context = getattr(source, "_context2d", None)
    if context is not None:
        return context._surface.snapshot() if context._surface is not None else None
    renderer = getattr(source, "_bitmap", None)
    if renderer is not None and renderer._bitmap is not None:
        return renderer._bitmap._image
    if type(source).__name__ in ("HTMLCanvasElement", "OffscreenCanvas"):
        width, height = source._width, source._height
        if raster_size_supported(width, height):
            webgl = getattr(source, "_webgl", None) or getattr(source, "_webgl2", None)
            if webgl is not None:
                pixels = webgl._backend().surface_pixels(webgl)
                if pixels is not None:
                    return RasterImage.from_rgba(width, height, pixels)
            return RasterCanvas(width, height).snapshot()
    if isinstance(source, ImageData):
        if source._width and source._height:
            pixels = bytes(int(source._data[index]) for index in range(4 * source._width * source._height))
            return RasterImage.from_rgba(source._width, source._height, pixels)
    for name in ("_bytes", "_body", "_buffer"):
        data = getattr(source, name, None)
        if isinstance(data, (bytes, bytearray)) and data:
            return RasterImage.decode(bytes(data))
    return None


def _resize_size(options, size):
    """The size ``resizeWidth``/``resizeHeight`` ask for, keeping the aspect ratio."""
    width, height = size
    asked = [options.get("resizeWidth"), options.get("resizeHeight")]
    wanted = [int(value) if value is not None else 0 for value in asked]
    if wanted[0] <= 0 and wanted[1] <= 0: return size
    if wanted[0] <= 0: wanted[0] = max(1, round(width * wanted[1] / height))
    if wanted[1] <= 0: wanted[1] = max(1, round(height * wanted[0] / width))
    return (wanted[0], wanted[1])


def create_image_bitmap(interface, image, arguments, services):
    """Chromium's ``createImageBitmap``: a promise, or a throw before one is made."""
    from ...page_errors import InvalidStateError, PageRangeError
    if image is MISSING:
        raise missing_error(interface, 'createImageBitmap', 1, 0, constructor=False)
    if not _is_bitmap_source(image):
        raise IdlArgumentError(
            f"Failed to execute 'createImageBitmap' on '{interface}':"
            f" The provided value is not of type '({_IMAGE_BITMAP_SOURCE})'.")
    rest, options = list(arguments), {}
    if len(rest) in (1, 5):
        options = dictionary(rest.pop())
    if len(rest) == 4:
        sx, sy, sw, sh = (integer(value, signed=True) for value in rest)
        if sw < 0: sx, sw = sx + sw, -sw
        if sh < 0: sy, sh = sy + sh, -sh
        if sw == 0 or sh == 0:
            raise PageRangeError(
                f"Failed to execute 'createImageBitmap' on '{interface}':"
                f" The crop rect {'height' if sw else 'width'} is 0.")
    elif rest:
        raise signature_error(interface, 'createImageBitmap')
    else:
        sx = sy = 0
        sw = sh = None
    bitmap = bitmap_source_image(image)
    if bitmap is None:
        raise InvalidStateError(
            f"Failed to execute 'createImageBitmap' on '{interface}':"
            " The image source is not usable.")
    if bitmap.size[0] <= 0 or bitmap.size[1] <= 0:
        raise InvalidStateError(
            f"Failed to execute 'createImageBitmap' on '{interface}':"
            f" The image source's {'width' if bitmap.size[0] <= 0 else 'height'} is 0.")
    if sw is not None: bitmap = bitmap.crop((sx, sy, sw, sh))
    size = _resize_size(options, bitmap.size)
    if size != bitmap.size: bitmap = bitmap.resize(size)
    result = ImageBitmap(bitmap, bitmap.size[0], bitmap.size[1])
    realm = getattr(services, "realm_services", None)
    if realm is None: return result
    promise, resolve, _reject = realm.make_promise()
    resolve(result)
    return promise
