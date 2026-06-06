"""Small Win32 helpers shared across the app.

The original reason this module exists: Tk's `winfo_id()` returns the
inner widget HWND, NOT the OS top-level window. For an
`overrideredirect(True)` borderless Toplevel, the inner HWND is a
child of the real top-level window — Win32 calls applied to the child
silently no-op OR hit the wrong window. The fix is to walk
`GetAncestor(hwnd, GA_ROOT=2)` up to the actual top-level before
calling any window-level API.

This bug has bitten us at least four times (Quick Notes maximize,
Quick Notes rounded corners, AskPill lift, audio editor hint overlay
lift). Centralising the fix in one helper kills the pattern: every
future Win32 call routes through `top_level_hwnd(widget)` and gets
the right HWND by construction.

If you're calling SetWindowPos / DwmSetWindowAttribute /
SetWindowDisplayAffinity / GetWindowLong / etc on a Tk widget,
**use top_level_hwnd(widget) instead of widget.winfo_id()**. Even
when the widget IS a top-level (no overrideredirect), the helper is
correct — GetAncestor on a top-level returns itself.
"""
from __future__ import annotations

import sys


def top_level_hwnd(widget) -> int:
    """Return the OS top-level HWND for any Tk widget.

    Walks GetAncestor(GA_ROOT) so it works correctly for:
      • normal Toplevels (winfo_id() may or may not be the top-level
        HWND depending on Tk / CTk wrapping)
      • overrideredirect Toplevels (winfo_id() is the inner child HWND
        and is the WRONG window for almost every Win32 API)
      • inner frames / canvases inside any window

    Returns the original `winfo_id()` as a fallback if GA_ROOT can't be
    resolved (Linux/Mac, or some corner cases where ctypes fails).
    Never raises.
    """
    try:
        child_hwnd = widget.winfo_id()
    except Exception:
        return 0
    if sys.platform != 'win32':
        return child_hwnd
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # Cache argtypes/restype on the module-level function so repeated
        # calls don't re-pay the cost of setting them.
        if not getattr(user32.GetAncestor, '_hk_typed', False):
            user32.GetAncestor.restype  = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor._hk_typed = True
        GA_ROOT = 2
        root = user32.GetAncestor(child_hwnd, GA_ROOT)
        return int(root) if root else child_hwnd
    except Exception:
        return child_hwnd
