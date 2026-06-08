"""Bulletproof keyboard hotkey listener.

Drop-in replacement for the parts of the `keyboard` PyPI library that
register global hotkeys via `WH_KEYBOARD_LL`. Built because the
`keyboard` library's hook periodically dies mid-session — its callback
runs Python on the hook thread (modifier-state tracking + hotkey table
lookup + callback dispatch), so under GIL contention or any heavy
main-thread work the callback can exceed Windows'
`LowLevelHooksTimeout` (default 300 ms). Once one callback misses the
window, Windows silently uninstalls the WHOLE LL hook chain — leaving
every hotkey dead until the app reinstalls. PrtSc never suffered this
because we already installed it in our own dedicated listener thread
whose callback only checks `if vkCode == VK_SNAPSHOT` and returns.

This module generalises that pattern to ALL hotkeys.

Design rules (in order of importance):

1. Hook callback is microscopic. It captures vkCode + WM_KEYDOWN/UP +
   updates a modifier bitmask + checks the hotkey table by O(1) dict
   lookup + posts a tuple to a Queue. It NEVER runs Python user code.
   That guarantees the callback returns to Windows in <1 ms, well
   inside LowLevelHooksTimeout.
2. Hook runs in its own dedicated thread with its own GetMessageW
   pump (same as `screenshot.start_prtsc_listener`).
3. A separate worker thread reads the Queue and fires the user's
   callback. If the callback runs slow (e.g. blocks on disk / network),
   the hook is unaffected.
4. The public API mirrors `keyboard.add_hotkey()` so swapping is a
   ~5-line change at every callsite.

API:
    kbhook.add_hotkey(combo, callback) -> handle
    kbhook.remove_hotkey(handle)
    kbhook.unhook_all()
    kbhook.start()                       # auto-called on first add
    kbhook.stop()                        # for clean shutdown

Combo strings accept the same format as `keyboard.add_hotkey()` for the
parts we use in this app:

    'alt+shift+w'   'ctrl+enter'   'f1'   'shift+f4'   'ctrl+alt+d'
    'escape'        'ctrl+f1'      'win+space'

Modifiers: ctrl alt shift win  (in any order, case-insensitive).
Keys: any name in _KEY_NAME_TO_VK below, or a single character a-z/0-9.

The format is intentionally a SUBSET of what the `keyboard` library
accepts — we don't need the full grammar, just the shapes this app
uses today.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as _wt
import logging
import queue
import threading
from typing import Callable

logger = logging.getLogger(__name__)


# ── Win32 plumbing ───────────────────────────────────────────────────────────

_user32 = ctypes.windll.user32
_kernel = ctypes.windll.kernel32

_WH_KEYBOARD_LL = 13
_WM_KEYDOWN    = 0x0100
_WM_SYSKEYDOWN = 0x0104
_WM_KEYUP      = 0x0101
_WM_SYSKEYUP   = 0x0105

_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(
    _LRESULT, ctypes.c_int, _wt.WPARAM, _wt.LPARAM,
)

_user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, _wt.WPARAM, _wt.LPARAM,
]
_user32.CallNextHookEx.restype = _LRESULT
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('vkCode',      _wt.DWORD),
        ('scanCode',    _wt.DWORD),
        ('flags',       _wt.DWORD),
        ('time',        _wt.DWORD),
        ('dwExtraInfo', ctypes.c_size_t),
    ]


# ── Modifier bitmask + key tables ────────────────────────────────────────────

_MOD_CTRL  = 0x01
_MOD_ALT   = 0x02
_MOD_SHIFT = 0x04
_MOD_WIN   = 0x08

# vkCode → bit in the modifier mask. Both L and R variants count.
_VK_TO_MOD = {
    0x11: _MOD_CTRL,  0xA2: _MOD_CTRL,  0xA3: _MOD_CTRL,  # CONTROL / L / R
    0x12: _MOD_ALT,   0xA4: _MOD_ALT,   0xA5: _MOD_ALT,   # MENU / L / R
    0x10: _MOD_SHIFT, 0xA0: _MOD_SHIFT, 0xA1: _MOD_SHIFT, # SHIFT / L / R
    0x5B: _MOD_WIN,   0x5C: _MOD_WIN,                     # LWIN / RWIN
}

# Name → vkCode table for combo parsing. Covers everything this app
# registers today; extend as needed.
_KEY_NAME_TO_VK = {
    'escape': 0x1B, 'esc': 0x1B,
    'enter':  0x0D, 'return': 0x0D,
    'tab':    0x09, 'space': 0x20,
    'backspace': 0x08, 'delete': 0x2E,
    'home':   0x24, 'end':   0x23,
    'pageup': 0x21, 'pagedown': 0x22,
    'up':     0x26, 'down':  0x28, 'left': 0x25, 'right': 0x27,
    'printscreen': 0x2C, 'prtsc': 0x2C, 'snapshot': 0x2C,
    'insert': 0x2D,
}
for _i, _n in enumerate('f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12'.split()):
    _KEY_NAME_TO_VK[_n] = 0x70 + _i
for _i, _c in enumerate('0123456789'):
    _KEY_NAME_TO_VK[_c] = 0x30 + _i
for _i, _c in enumerate('abcdefghijklmnopqrstuvwxyz'):
    _KEY_NAME_TO_VK[_c] = 0x41 + _i


def _parse_combo(combo: str) -> tuple[int, int]:
    """'alt+shift+w' → (modifier_mask, vkcode). Raises ValueError on bad input."""
    parts = [p.strip().lower() for p in combo.split('+') if p.strip()]
    if not parts:
        raise ValueError(f'empty hotkey combo: {combo!r}')
    mod = 0
    key_vk = None
    for p in parts:
        if p in ('ctrl', 'control'):
            mod |= _MOD_CTRL
        elif p in ('alt', 'menu'):
            mod |= _MOD_ALT
        elif p == 'shift':
            mod |= _MOD_SHIFT
        elif p in ('win', 'windows', 'super'):
            mod |= _MOD_WIN
        else:
            if key_vk is not None:
                raise ValueError(
                    f'hotkey {combo!r} names multiple non-modifier keys; '
                    f'second was {p!r}')
            vk = _KEY_NAME_TO_VK.get(p)
            if vk is None:
                raise ValueError(f'unknown key name in hotkey {combo!r}: {p!r}')
            key_vk = vk
    if key_vk is None:
        raise ValueError(f'hotkey {combo!r} has only modifiers, no key')
    return mod, key_vk


# ── Hook state (singleton, module-level) ─────────────────────────────────────

_state_lock = threading.Lock()
_modifier_mask = 0
# Hotkey table: (mod, vk) -> list of handles. Multiple callbacks may bind
# the same combo; we fire them all on match (legacy `keyboard` semantics).
_hotkeys: dict[tuple[int, int], list[int]] = {}
_handles: dict[int, tuple[tuple[int, int], Callable[[], None]]] = {}
_next_handle = [1]

# Dispatch queue + worker thread. The hook callback only ENQUEUES; the
# worker thread invokes user callbacks. Keeps the hook callback in the
# <1 ms budget even if a user callback is heavy.
_dispatch_q: queue.Queue = queue.Queue(maxsize=256)

_started = False
_hook_ref = [None]   # mutable container shared with the listener thread
_listener_thread = None
_worker_thread   = None

# When True, the hook becomes a pure pass-through: it does NOT update the
# modifier mask, does NOT look up the hotkey table, does NOT suppress.
# Toggled from the tray menu so a user can temporarily reclaim conflicting
# F-keys / Ctrl+combos for the foreground app (Chrome devtools, IDEs, etc.)
# without having to quit and relaunch us.
_paused = False


# ── Hook procedure (runs on listener thread, must return FAST) ───────────────

def _hook_proc(nCode, wParam, lParam):
    global _modifier_mask
    if nCode < 0:
        return _user32.CallNextHookEx(_hook_ref[0], nCode, wParam, lParam)
    # When paused, every key flows straight through — no mask updates,
    # no table lookup, no suppression. The hook stays installed so
    # resume is instant. Modifier mask is reset on pause/resume edges
    # in set_paused() to avoid a stuck-modifier on toggle.
    if _paused:
        return _user32.CallNextHookEx(_hook_ref[0], nCode, wParam, lParam)
    matched = False  # if True, suppress the key (don't pass to foreground app)
    try:
        kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode
        if wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
            mod_bit = _VK_TO_MOD.get(vk)
            if mod_bit is not None:
                _modifier_mask |= mod_bit
            else:
                # Non-modifier keydown — check the hotkey table.
                key = (_modifier_mask, vk)
                handles = _hotkeys.get(key)
                if handles:
                    # Snapshot the list (we hold no lock here; mutations
                    # via add/remove are atomic dict operations and we
                    # can tolerate a brief stale read).
                    for h in tuple(handles):
                        entry = _handles.get(h)
                        if entry is not None:
                            cb = entry[1]
                            try:
                                _dispatch_q.put_nowait(cb)
                                matched = True
                            except queue.Full:
                                # Dispatch queue full means the worker
                                # is stuck. Don't block the hook;
                                # the watchdog (if any) will recover.
                                # Still mark as matched so we suppress —
                                # better to swallow one keystroke than
                                # let the foreground app fire its action.
                                matched = True
        elif wParam in (_WM_KEYUP, _WM_SYSKEYUP):
            mod_bit = _VK_TO_MOD.get(vk)
            if mod_bit is not None:
                _modifier_mask &= ~mod_bit
            # Key releases ALWAYS pass through (never suppress), so the
            # foreground app's key-state tracking stays consistent even
            # if we swallowed the matching key-down.
    except Exception:
        # The hook MUST return — never let a Python exception propagate
        # into the Win32 callback path.
        pass
    if matched:
        # Returning non-zero tells Windows "we handled this; don't pass
        # it to the next hook or the foreground window." This is what
        # makes our F1/F12/Ctrl+Enter/etc. NOT also trigger Chrome's,
        # Blender's, AutoCAD's same-combo shortcut.
        return 1
    return _user32.CallNextHookEx(_hook_ref[0], nCode, wParam, lParam)


# Keep the WINFUNCTYPE-wrapped function alive at module scope so
# Windows doesn't end up calling freed memory.
_hook_proc_ref = _HOOKPROC(_hook_proc)


# ── Listener thread (installs hook + pumps messages) ─────────────────────────

def _listener_main():
    _hook_ref[0] = _user32.SetWindowsHookExW(
        _WH_KEYBOARD_LL, _hook_proc_ref, None, 0)
    if not _hook_ref[0]:
        logger.error('kbhook: SetWindowsHookExW failed; hotkeys dead')
        return
    logger.info('kbhook: WH_KEYBOARD_LL installed in dedicated thread')
    msg = _wt.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))


# ── Worker thread (drains dispatch queue + invokes user callbacks) ───────────

def _worker_main():
    while True:
        cb = _dispatch_q.get()
        if cb is None:
            return   # shutdown signal
        try:
            cb()
        except Exception:
            logger.exception('kbhook: user callback raised')


# ── Public API ───────────────────────────────────────────────────────────────

def start() -> None:
    """Spawn the listener + worker threads if not already running.
    Called automatically by add_hotkey() on first use. Idempotent."""
    global _started, _listener_thread, _worker_thread
    if _started:
        return
    _worker_thread = threading.Thread(
        target=_worker_main, daemon=True, name='kbhook-worker')
    _worker_thread.start()
    _listener_thread = threading.Thread(
        target=_listener_main, daemon=True, name='kbhook-listener')
    _listener_thread.start()
    _started = True


def add_hotkey(combo: str, callback: Callable[[], None]) -> int:
    """Register `combo` -> `callback`. Returns a handle for remove_hotkey()."""
    mod, vk = _parse_combo(combo)
    with _state_lock:
        h = _next_handle[0]
        _next_handle[0] += 1
        _handles[h] = ((mod, vk), callback)
        _hotkeys.setdefault((mod, vk), []).append(h)
    start()
    return h


def remove_hotkey(handle: int) -> None:
    """Unregister a single hotkey by its handle. No-op if already gone."""
    with _state_lock:
        entry = _handles.pop(handle, None)
        if entry is None:
            return
        key = entry[0]
        lst = _hotkeys.get(key)
        if lst is not None:
            try:
                lst.remove(handle)
            except ValueError:
                pass
            if not lst:
                _hotkeys.pop(key, None)


def unhook_all() -> None:
    """Remove every registered hotkey. The hook itself stays installed."""
    with _state_lock:
        _hotkeys.clear()
        _handles.clear()


def set_paused(paused: bool) -> None:
    """When `paused` is True, the hook stops matching/suppressing entirely
    — every key flows to the foreground app unchanged. Use this so the
    user can temporarily reclaim F-keys / Ctrl+combos for Chrome devtools
    or other apps without having to restart Hotkeys.

    Resets the modifier-mask cache on every toggle, otherwise a key
    pressed *during* the pause window would be invisible to us and leave
    the cached mask out-of-sync with the real keyboard state after resume.
    """
    global _paused, _modifier_mask
    _paused = bool(paused)
    _modifier_mask = 0
    logger.info(f'kbhook: paused={_paused}')


def is_paused() -> bool:
    return _paused


def stop() -> None:
    """Tear down the listener + worker. For clean app shutdown only."""
    global _started
    if not _started:
        return
    # Drain hook
    if _hook_ref[0]:
        try:
            _user32.UnhookWindowsHookEx(_hook_ref[0])
        except Exception:
            pass
        _hook_ref[0] = None
    # Signal worker to exit
    try:
        _dispatch_q.put_nowait(None)
    except queue.Full:
        pass
    _started = False
