"""Image sizes from the header alone: the fact ``naturalWidth`` answers with.

Only the header is needed, so these are crafted byte strings rather than whole images -- which
is also the point: this build decodes nothing, and the size is not something a decoder decides.
"""
import struct

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")

from pyv8c.web.dom.images import image_size  # noqa: E402


def _png(width, height):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)


def _gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" + b"\x00" * 4


def _jpeg(width, height):
    return (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
            + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9)


def _webp_vp8x(width, height):
    """``VP8X``: flags (1 byte) and reserved (3) come before the canvas size, which is one less
    than the image size in each direction."""
    return (b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + struct.pack("<I", 10)
            + b"\x00" * 4
            + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


def _svg(attributes):
    return ("<?xml version='1.0'?>\n<svg xmlns='http://www.w3.org/2000/svg' "
            + attributes + "></svg>").encode()


def test_a_png_reports_its_own_size():
    assert image_size(_png(640, 480)) == (640, 480)


def test_a_gif_reports_its_own_size():
    assert image_size(_gif(17, 9)) == (17, 9)


def test_a_jpeg_walks_to_its_frame_header():
    assert image_size(_jpeg(320, 200)) == (320, 200)


def test_a_webp_reports_the_size_it_stores():
    assert image_size(_webp_vp8x(300, 250)) == (300, 250)


def test_an_svg_reports_its_declared_size():
    assert image_size(_svg("width='99' height='20'")) == (99, 20)


def test_an_svg_without_dimensions_falls_back_to_its_viewbox():
    assert image_size(_svg("viewBox='0 0 64 48'")) == (64, 48)


def test_an_svg_says_nothing_when_it_declares_nothing():
    assert image_size(_svg("")) == (0, 0)


def test_bytes_that_are_not_an_image_report_nothing():
    assert image_size(b"not an image at all, really") == (0, 0)
    assert image_size(b"") == (0, 0)
    assert image_size(None) == (0, 0)
