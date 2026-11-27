"""``DOMRect``: the box ``getBoundingClientRect`` reports, and the edges read off it."""
from ...idl import interface
from ...foundation.arguments import MISSING, dictionary, is_nullish
from .point import double
from .collections import _ListItems
from .mixins import ReadOnlyIndexedPropertyMixin


def rect_values(value):
    """The four numbers of a ``DOMRectInit``, its defaults filled in."""
    if is_nullish(value):
        return 0.0, 0.0, 0.0, 0.0
    if isinstance(value, DOMRectReadOnly):
        return value._x, value._y, value._width, value._height
    options = dictionary(value)
    return (double(options.get("x", 0.0)), double(options.get("y", 0.0)),
            double(options.get("width", 0.0)), double(options.get("height", 0.0)))


@interface('DOMRectReadOnly', None)
class DOMRectReadOnly:
    def __init__(self, x=MISSING, y=MISSING, width=MISSING, height=MISSING, *_extra, **_):
        if type(x) is type(y) is type(width) is type(height) is float:
            self._x, self._y, self._width, self._height = x, y, width, height
            return
        self._x = double(x)
        self._y = double(y)
        self._width = double(width)
        self._height = double(height)

    def get_x(self): return self._x
    def get_y(self): return self._y
    def get_width(self): return self._width
    def get_height(self): return self._height
    # The four edges are readings rather than storage, so an edge follows whatever the
    # position and size it is derived from currently say.
    def get_top(self): return self._y
    def get_right(self): return self._x + self._width
    def get_bottom(self): return self._y + self._height
    def get_left(self): return self._x

    def fn_toJSON(self, *_extra):
        return {"x": self._x, "y": self._y, "width": self._width, "height": self._height,
                "top": self.get_top(), "right": self.get_right(),
                "bottom": self.get_bottom(), "left": self.get_left()}

    @staticmethod
    def fn_fromRect(_realm, init=None, *_extra):
        return DOMRect(*rect_values(init))


@interface('DOMRect', 'DOMRectReadOnly')
class DOMRect(DOMRectReadOnly):
    # Declared: the baseline does not say they are writable, so ``set_<name>`` makes them so.

    @staticmethod
    def fn_fromRect(_realm, init=None, *_extra):
        return DOMRect(*rect_values(init))

    def set_x(self, value): self._x = double(value)
    def set_y(self, value): self._y = double(value)
    def set_width(self, value): self._width = double(value)
    def set_height(self, value): self._height = double(value)

@interface('DOMRectList', None)
class DOMRectList(_ListItems, ReadOnlyIndexedPropertyMixin):
    """当前布局提供的边框矩形快照，不包含行内碎片。"""


__all__ = ["DOMRect", "DOMRectReadOnly", "DOMRectList", "rect_values"]
