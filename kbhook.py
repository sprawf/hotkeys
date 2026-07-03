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
import time
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

# vkCodes whose matching combos still fire our callback BUT are NEVER
# suppressed — the keystroke is always allowed to reach the foreground
# app too. Reserved for keys that have universal cross-app meaning we
# must not steal. Escape closes dialogs / cancels in every app on the
# planet; eating it would mean opening any window after a Hotkeys
# Escape-bound action would leave the user unable to close it.
_PASSTHROUGH_KEYS = {0x1B}  # VK_ESCAPE

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

# Timestamp of the most recent _hook_proc invocation. Bumped inside the
# hook callback on every key event Windows delivers. If this stops
# updating while Windows itself is still receiving input (per
# GetLastInputInfo), the OS has silently unhooked us and we must
# reinstall. Init to time.monotonic() at import so is_hook_alive() has
# a valid starting reference before the first key event.
_last_hook_tick = time.monotonic()

# ── LASTINPUTINFO for GetLastInputInfo() ─────────────────────────────────
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', _wt.UINT), ('dwTime', _wt.DWORD)]

_user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
_user32.GetLastInputInfo.restype = _wt.BOOL
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel32.GetTickCount.restype = _wt.DWORD

# When True, the hook stops matching/suppressing/dispatching, but still
# tracks the modifier mask so it can recognise "a registered hotkey was
# pressed while paused" and tell the app to show a reminder toast.
# Toggled from the tray menu so a user can temporarily reclaim conflicting
# F-keys / Ctrl+combos for the foreground app (Chrome devtools, IDEs, etc.)
# without having to quit and relaunch us.
_paused = False

# Optional callback invoked when a registered hotkey matches WHILE paused.
# Set from main.py to show a throttled toast nudging the user to click
# "Resume hotkeys" in the tray. Receives no args.
_on_paused_match: Callable[[], None] | None = None


# ── Hook procedure (runs on listener thread, must return FAST) ───────────────

def _hook_proc(nCode, wParam, lParam):
    global _modifier_mask, _last_hook_tick
    # Liveness heartbeat: any invocation, even nCode<0 pass-through,
    # proves Windows is still delivering keyboard events to our hook.
    # Read via time.monotonic() so it's monotonic across DST/system-clock
    # changes and safe under the sub-1ms budget rule.
    _last_hook_tick = time.monotonic()
    if nCode < 0:
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
                    if _paused:
                        # Pressed a registered combo while paused. Don't
                        # dispatch and don't suppress — let the key flow
                        # through. But ping the optional UX callback so
                        # main.py can show a throttled "hotkeys are
                        # paused" toast / pill. Without this, paused
                        # hotkeys are silently dead and the user can't
                        # tell whether they typed wrong or we're broken.
                        if _on_paused_match is not None:
                            try:
                                _dispatch_q.put_nowait(_on_paused_match)
                            except queue.Full:
                                pass
                    else:
                        # Snapshot the list (we hold no lock here;
                        # mutations via add/remove are atomic dict
                        # operations and we can tolerate a brief stale
                        # read).
                        # Keys in _PASSTHROUGH_KEYS still fire the
                        # callback but never get suppressed — Escape
                        # is the canonical case: every other app on
                        # the system uses it to close their own UI.
                        suppress_this = vk not in _PASSTHROUGH_KEYS
                        for h in tuple(handles):
                            entry = _handles.get(h)
                            if entry is not None:
                                cb = entry[1]
                                try:
                                    _dispatch_q.put_nowait(cb)
                                    if suppress_this:
                                        matched = True
                                except queue.Full:
                                    # Dispatch queue full means the
                                    # worker is stuck. Don't block the
                                    # hook; the watchdog (if any) will
                                    # recover. Still mark as matched
                                    # so we suppress — better to
                                    # swallow one keystroke than let
                                    # the foreground app fire its
                                    # action.
                                    if suppress_this:
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
    """When `paused` is True, the hook stops dispatching callbacks and
    suppressing keys, so every keystroke flows to the foreground app
    unchanged. The hook still tracks the modifier mask + recognises
    registered combos — that's how we know to ping `on_paused_match`
    when a user presses a hotkey while paused, so the app can show
    them a reminder instead of just silently swallowing it.

    Resets the modifier-mask cache on every toggle, otherwise a key
    pressed *during* the resume edge would leave the cached mask
    out-of-sync with the real keyboard state.
    """
    global _paused, _modifier_mask
    _paused = bool(paused)
    _modifier_mask = 0
    logger.info(f'kbhook: paused={_paused}')


def is_paused() -> bool:
    return _paused


def set_on_paused_match(cb: Callable[[], None] | None) -> None:
    """Register a callback invoked (via the worker thread) every time a
    registered hotkey combo is pressed while paused. Use this to surface
    a throttled toast so the user understands why their hotkey did
    nothing. Pass None to clear. The callback should be cheap and
    thread-safe; ours just enqueues a Tk after() call."""
    global _on_paused_match
    _on_paused_match = cb


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


# ── Liveness detection + self-heal ────────────────────────────────────────

def is_hook_alive(idle_grace_sec: float = 30.0) -> bool:
    """Return True if the WH_KEYBOARD_LL hook is provably still installed.

    Detection approach: compare our own last-hook-callback timestamp
    against Windows' own last-input timestamp (GetLastInputInfo, which
    reports the last time the OS itself saw ANY keyboard/mouse input,
    system-wide).

    - If Windows saw input very recently AND our hook saw the same
      recent input, hook is alive.
    - If Windows saw input recently but our hook did NOT (older by more
      than a few seconds), Windows silently unhooked us, hook is dead.
    - If Windows has NOT seen any input in `idle_grace_sec`, we can't
      distinguish a dead hook from a genuinely idle user, so we
      OPTIMISTICALLY say alive. The next real key press will either
      arrive (proving alive) or fail to arrive (next tick this function
      will report dead).

    Called from the main.py watchdog loop; must be cheap.
    """
    if not _started or _hook_ref[0] is None:
        return False

    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not _user32.GetLastInputInfo(ctypes.byref(lii)):
        # Can't tell — treat as alive to avoid false-positive reinstalls
        return True

    now_tick = _kernel32.GetTickCount()
    # DWORD tick difference is intentional 32-bit modular subtraction so
    # rollover after ~49.7 days doesn't lie about the age.
    ms_since_windows_input = (now_tick - lii.dwTime) & 0xFFFFFFFF
    sec_since_windows_input = ms_since_windows_input / 1000.0

    if sec_since_windows_input > idle_grace_sec:
        # User is idle. Can't distinguish dead hook from real idleness.
        return True

    sec_since_hook_callback = time.monotonic() - _last_hook_tick

    # Windows saw input in the last N seconds. Our hook must have too
    # (with a small tolerance for the timestamp race).
    return sec_since_hook_callback < sec_since_windows_input + 2.0


def reinstall_hook() -> bool:
    """Force the OS-level WH_KEYBOARD_LL hook to be re-installed.

    Called by the watchdog when is_hook_alive() returns False. Unhooks
    the current (dead) hook and installs a fresh one, WITHOUT tearing
    down the listener/worker threads or losing registered hotkeys.

    Returns True on success, False on failure.

    The listener thread's GetMessageW loop is unaffected — it stays
    blocked, and any future messages from the new hook get pumped
    through it the same way.
    """
    global _last_hook_tick
    old = _hook_ref[0]
    if old:
        try:
            _user32.UnhookWindowsHookEx(old)
        except Exception:
            pass
        _hook_ref[0] = None
    try:
        new_hook = _user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, _hook_proc_ref, None, 0)
    except Exception as e:
        logger.error(f'kbhook: reinstall SetWindowsHookExW raised: {e}')
        return False
    if not new_hook:
        logger.error('kbhook: reinstall SetWindowsHookExW returned NULL')
        return False
    _hook_ref[0] = new_hook
    _last_hook_tick = time.monotonic()   # reset heartbeat baseline
    logger.warning('kbhook: OS hook was dead, reinstalled it.')
    return True
