"""Canvas gradient implementation."""

from ....idl import interface
from ....foundation.arguments import MISSING, missing_error
from ...page_errors import IndexSizeError
from .colors import number_text, parses_as_color, canvas_color


@interface('CanvasGradient', None)
class CanvasGradient:
    """What ``createRadialGradient`` and ``createLinearGradient`` hand back."""

    def __init__(self, coordinates, transform, **_):
        self._coordinates, self._transform = coordinates, transform
        self._stops = []

    def fn_addColorStop(self, offset=MISSING, color=MISSING, *_extra):
        """Record one stop, or refuse the value the way Chromium's parser does."""
        from ...page_errors import dom_exception
        for position, value in enumerate((offset, color)):
            if value is MISSING:
                raise missing_error('CanvasGradient', 'addColorStop', 2, position, constructor=False)
        value = float(offset)
        if not 0.0 <= value <= 1.0:
            raise IndexSizeError(
                "Failed to execute 'addColorStop' on 'CanvasGradient':"
                f" The provided value ({number_text(value)}) is outside the range (0.0, 1.0).")
        if not parses_as_color(color):
            raise dom_exception(
                "SyntaxError",
                "Failed to execute 'addColorStop' on 'CanvasGradient':"
                f" The value provided ('{color}') could not be parsed as a color.")
        self._stops.append((value, canvas_color(color)))
        self._stops.sort(key=lambda stop: stop[0])
