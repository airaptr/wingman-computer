#!/usr/bin/env python3
"""Exit successfully only after an X11 compositor owns the screen selection."""
import ctypes
import os
import sys

x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
x11.XInternAtom.restype = ctypes.c_ulong
x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XGetSelectionOwner.restype = ctypes.c_ulong
x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
display = x11.XOpenDisplay(os.environ.get("DISPLAY", ":1").encode())
if not display:
    sys.exit(1)
owner = x11.XGetSelectionOwner(display, x11.XInternAtom(display, b"_NET_WM_CM_S0", 0))
x11.XCloseDisplay(display)
sys.exit(0 if owner else 1)
