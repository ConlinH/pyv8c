"""Canvas encoding implementation."""

import math

from pyv8c import pyv8
from ....foundation.arguments import MISSING, dom_string
from .bitmap import bitmap_source_image, raster_size_supported


def normalize_encoding_type(kind):
    if kind is MISSING or kind is pyv8.Undefined:
        return 'image/png'
    requested = dom_string(kind).lower()
    return requested if requested in ('image/png', 'image/jpeg', 'image/webp') else 'image/png'


def normalize_encoding_quality(value):
    # The IDL argument is `any`: Chrome considers only primitive JS numbers.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        if math.isfinite(value) and 0 <= value <= 1:
            return value
    return None


def encode_canvas(canvas, kind, quality):
    """Encode the current surface without acquiring or changing its context."""
    if not raster_size_supported(canvas._width, canvas._height):
        return kind, None
    try:
        image = bitmap_source_image(canvas)
        return kind, image.encode(kind, quality) if image is not None else None
    except ValueError:
        return kind, None
