"""Shared windowing helper, keeps every "open a window" path centered on
the primary monitor's WORK area (screen minus the taskbar) so the title bar
is never under the system menu and the bottom edge is never behind the
taskbar.

Used by:
  • whiteboard.py, sizes the pywebview window on cold launch
  • main.py, sizes/centers the Quick Notes window on Restore All Defaults

Falls back to raw screen dimensions on non-Windows or if the Win32 call
fails for any reason.
"""
from __future__ import annotations
import sys


def get_work_area() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the primary monitor's work area
    (screen minus taskbar). Falls back to a 1920×1040 guess if the Win32
    probe is unavailable."""
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):
                return (rect.left, rect.top,
                        rect.right - rect.left, rect.bottom - rect.top)
        except Exception:
            pass
    # Reasonable fallback when Win32 isn't reachable
    return (0, 0, 1920, 1040)


def center_on_work_area(want_w: int, want_h: int) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) for a window of intended size (want_w, want_h)
    centered on the primary monitor's work area. If the work area is
    smaller than the request, the returned w/h are shrunk so no edge is
    ever occluded, callers should use the returned w/h, not the input.
    """
    wa_x, wa_y, wa_w, wa_h = get_work_area()
    w = min(want_w, wa_w)
    h = min(want_h, wa_h)
    x = wa_x + (wa_w - w) // 2
    y = wa_y + (wa_h - h) // 2
    return x, y, w, h
