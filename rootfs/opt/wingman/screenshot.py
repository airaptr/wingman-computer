#!/usr/bin/env python3
"""Capture the composed X11 screen, without rebuilding ARGB child windows."""
import ctypes
import struct
import sys
import zlib


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
    ]


def read_screen(display_name):
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x11.XDefaultScreen.restype = ctypes.c_int
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XDisplayWidth.restype = ctypes.c_int
    x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XDisplayHeight.restype = ctypes.c_int
    x11.XGetImage.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int,
    ]
    x11.XGetImage.restype = ctypes.POINTER(XImage)
    x11.XDestroyImage.argtypes = [ctypes.POINTER(XImage)]
    x11.XDestroyImage.restype = ctypes.c_int
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int

    display = x11.XOpenDisplay(display_name.encode())
    if not display:
        raise OSError("the X11 display is unavailable")
    try:
        screen = x11.XDefaultScreen(display)
        width, height = x11.XDisplayWidth(display, screen), x11.XDisplayHeight(display, screen)
        if width <= 0 or height <= 0:
            raise ValueError("the screen dimensions are invalid")
        # xwd's multi-visual reconstruction replaces transparent dock pixels with black.
        image = x11.XGetImage(display, x11.XDefaultRootWindow(display), 0, 0,
                              width, height, ctypes.c_ulong(-1).value, 2)
        if not image:
            raise OSError("the composed screen could not be read")
        try:
            info = image.contents
            if (info.format != 2 or info.xoffset != 0
                    or (info.width, info.height) != (width, height)
                    or info.bits_per_pixel not in (24, 32) or info.byte_order not in (0, 1)
                    or info.bytes_per_line < width * (info.bits_per_pixel // 8) or not info.data
                    or (info.red_mask, info.green_mask, info.blue_mask) != (0xFF0000, 0xFF00, 0xFF)):
                raise ValueError("the X11 screen has an unsupported pixel format")
            return (width, height, info.bits_per_pixel, info.bytes_per_line, info.byte_order,
                    ctypes.string_at(info.data, info.bytes_per_line * height))
        finally:
            x11.XDestroyImage(image)
    finally:
        x11.XCloseDisplay(display)


def encode_png(width, height, bpp, stride, byte_order, data):
    if width <= 0 or height <= 0 or bpp not in (24, 32) or byte_order not in (0, 1):
        raise ValueError("the screen pixel layout is unsupported")
    step = bpp // 8
    if stride < width * step or len(data) != stride * height:
        raise ValueError("the screen pixel data is incomplete")
    rows = []
    for y in range(height):
        line = data[y * stride:y * stride + width * step]
        if byte_order == 0:
            b, g, r = line[0::step], line[1::step], line[2::step]
        else:
            offset = step - 3
            r, g, b = line[offset::step], line[offset + 1::step], line[offset + 2::step]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = r, g, b
        rows.append(b"\x00" + rgb)

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    try:
        sys.stdout.buffer.write(encode_png(*read_screen(sys.argv[1] if len(sys.argv) > 1 else ":1")))
    except (OSError, ValueError) as error:
        sys.exit(f"screen capture failed: {error}")
