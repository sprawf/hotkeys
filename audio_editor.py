"""Bundled audio editor, launched by Shift+F10.

Spawns the bundled portable build of Tenacity (an Audacity fork) as a
sibling process. Tenacity ships unmodified under GPLv3, this module
just launches it and relabels its top-level window so the front UI
reads "Audio Editor" instead of "Tenacity".

Design notes:
  • Mere aggregation, the bundled binary is unmodified on disk. We
    only send a WM_SETTEXT after the window appears, which is a normal
    OS interaction any process is allowed to do.
  • Frozen-aware path resolution mirrors whiteboard.py, in a frozen
    dist the bundle sits next to the exe under audio_editor_assets/.
  • Toggle semantics mirror whiteboard:
        no proc   → spawn
        foreground → minimize
        minimized → restore + foreground
        background → foreground
        dead       → respawn
  • The window title polls every 1.5s while alive. If Tenacity
    rewrites its own title (e.g. after loading a project), we
    reassert "Audio Editor" so the user never sees the upstream brand.
"""
from __future__ import annotations
import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Public-facing window title, what the user sees.
DISPLAY_TITLE = 'Audio Editor'

# Upstream brand words to scrub from any visible label, longest matches
# first so multi-word forms get rewritten before their substrings get a
# chance to (e.g. "Tenacity Manual" -> "Audio Editor Manual", not
# "Audio Editor Manual" via two passes that race). Tenacity is a fork
# of Audacity so Audacity references survive in legacy menu items, file
# dialogs, About box. Both names get scrubbed uniformly.
_BRAND_REWRITES = (
    ('Tenacity', DISPLAY_TITLE),
    ('Audacity', DISPLAY_TITLE),
)

# Poll interval for the title-keeper thread (seconds). Tight enough
# that a freshly-shown dialog (e.g. import error, About box, save-as)
# gets scrubbed before the user reads the upstream brand. The walker
# is cheap (Win32 message sends), 0.25s is fine.
_TITLE_POLL_S = 0.25

# How long to wait after spawn for the main window to appear.
_HWND_WAIT_S  = 15.0


def _bundled_root() -> Path:
    """Where bundled assets live, frozen-aware. Mirrors whiteboard.py."""
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _exe_path() -> Path:
    """Resolve the bundled audio-editor binary path."""
    return _bundled_root() / 'audio_editor_assets' / 'tenacity' / 'tenacity.exe'


def _portable_settings_dir() -> Path:
    """The Portable Settings folder sibling to tenacity.exe. Tenacity
    detects this folder at launch and uses it for tenacity.cfg, but
    TempDir (where SessionData / .aup3unsaved files land) is a separate
    setting that has to be pointed inside here too, otherwise unsaved
    projects leak into %LOCALAPPDATA%/Tenacity/SessionData and trigger
    a crash-recovery dialog on the next launch."""
    return _exe_path().parent / 'Portable Settings'


def _ensure_portable_state() -> None:
    """Pre-launch housekeeping, runs before tenacity.exe is spawned:

      1. Disable the upstream welcome dialog by writing
         [GUI]ShowSplashScreen=0 into the portable cfg. The welcome
         dialog is wxHTMLWindow content (logo PNG + HTML body) that
         our runtime SetWindowText scrubber cannot reach, so the only
         way to keep the upstream brand off the splash is to suppress
         the dialog entirely. The cfg key matches the "Don't show
         this again at start up" checkbox the user sees in the dialog.

      2. Drain stale .aup3unsaved files from %LOCALAPPDATA%/Tenacity
         /SessionData so the Automatic Crash Recovery dialog stops
         appearing across runs.

    Both pieces keep the front UI fully scrubbed of the upstream name.
    """
    # ── 1. Cfg seeding: splash off + FFmpeg on ───────────────────────
    # _SEED_KEYS lists (section, key, desired_value) pairs we want to
    # keep pinned in the portable cfg before every launch. The walker
    # reasserts them only when missing or different, so user changes
    # outside of these keys are preserved.
    _SEED_KEYS = (
        # Suppress the upstream welcome splash, the wxHTMLWindow body
        # leaks the upstream brand and is unreachable to our scrubber.
        ('GUI', 'ShowSplashScreen', '0'),
        # Enable the bundled FFmpeg shared libs sitting next to
        # tenacity.exe (avformat-61.dll etc), needed for mkv / mp4 /
        # mov / m4a video import so users can drag a video and have
        # the editor auto-extract its audio track.
        ('FFmpeg', 'Enabled', '1'),
        # Pre-tick every "Don't show this warning again" checkbox so
        # the first-time-user dialogs (FirstProjectSave, MissingExtension,
        # MixMono, MixStereo, MixUnknownChannels) don't fire. These
        # dialogs all mention the upstream brand in their bodies and
        # are pure noise for someone using us as a simple editor.
        # Pre-tick every "Don't show this warning again" checkbox so
        # the first-time-user dialogs (FirstProjectSave, MissingExtension,
        # MixMono, MixStereo, MixUnknownChannels) don't fire. Verified
        # empirically: when the user ticks the checkbox, Tenacity writes
        # the NUMERIC value '0' (wxConfig bool encoding), not the string
        # 'false'. The string form is silently treated as "show" by
        # wxConfig::ReadBool's parsing of unknown values.
        ('Warnings', 'FirstProjectSave',    '0'),
        ('Warnings', 'MissingExtension',    '0'),
        ('Warnings', 'MixMono',             '0'),
        ('Warnings', 'MixStereo',           '0'),
        ('Warnings', 'MixUnknownChannels',  '0'),
        # Suppress the ".aup3 files are not currently associated with
        # Tenacity" popup on startup. Different Audacity/Tenacity forks
        # store this under different sections; setting all known
        # candidates covers every version we might ship.
        ('FileFormats', 'AssociateFilesOnStartup', '0'),
        ('Windows',     'AssociateFilesOnStartup', '0'),
        ('Warnings',    'FileAssociations',        '0'),
        ('Warnings',    'CheckAssociateFileTypes', '0'),
    )
    try:
        cfg = _portable_settings_dir() / 'tenacity.cfg'
        if not cfg.exists():
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text('', encoding='utf-8')
        text = cfg.read_text(encoding='utf-8', errors='ignore')
        import re as _re
        changed = False
        for section, key, want in _SEED_KEYS:
            section_header = f'[{section}]'
            line_pat = _re.compile(rf'(?mi)^{_re.escape(key)}\s*=\s*(.*)$')
            # Quick desired-state check, skip rewrite if value already set.
            desired = _re.compile(rf'(?mi)^{_re.escape(key)}\s*=\s*{want}\s*$')
            if desired.search(text):
                continue
            if line_pat.search(text):
                text = line_pat.sub(f'{key}={want}', text, count=1)
            elif section_header in text:
                text = text.replace(
                    f'{section_header}\n', f'{section_header}\n{key}={want}\n', 1)
            else:
                if text and not text.endswith('\n'):
                    text += '\n'
                text += f'{section_header}\n{key}={want}\n'
            changed = True
        if changed:
            cfg.write_text(text, encoding='utf-8')
            logger.info('audio editor: cfg seeded (splash off, FFmpeg on)')
    except Exception as e:
        logger.debug(f'cfg seeding skipped: {e}')

    # ── 2. Stale unsaved-project cleanup ──────────────────────────────
    try:
        local_appdata = os.environ.get('LOCALAPPDATA')
        if not local_appdata:
            return
        legacy = Path(local_appdata) / 'Tenacity' / 'SessionData'
        if not legacy.is_dir():
            return
        for stray in legacy.iterdir():
            name = stray.name.lower()
            if 'unsaved' in name:
                try:
                    stray.unlink()
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f'unsaved-cleanup skipped: {e}')


def _show_fatal(title: str, msg: str) -> None:
    """Native Win32 message box. Works without Tk."""
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
    except Exception:
        print(f'{title}: {msg}', file=sys.stderr)


# ─── Win32 helpers ────────────────────────────────────────────────────────

if sys.platform == 'win32':
    _user32   = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    SW_RESTORE  = 9
    SW_MINIMIZE = 6
    SW_SHOW     = 5

    _EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG   = 1
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE  = 0x00000040

    # Class-icon overrides. The window CLASS holds a default icon that
    # Windows paints before WM_SETICON propagates. Overriding the class
    # icons removes the brief "Tenacity icon" flash in the taskbar /
    # title bar / Alt+Tab strip.
    GCLP_HICON   = -14
    GCLP_HICONSM = -34

    # Signature for SetClassLongPtrW so ctypes returns a 64-bit value
    # cleanly on x64 (default int return would truncate on Windows x64).
    _user32.SetClassLongPtrW.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _user32.SetClassLongPtrW.restype = ctypes.c_void_p

    # Same protection for the HWND/HMENU-returning Win32 calls in the
    # rebrand walker. Without explicit restypes ctypes defaults to c_int
    # which silently truncates handle values on 64-bit Windows. The
    # GetParent comparison at line 582 is especially dangerous — a
    # truncated parent would compare unequal to a truncated `hwnd` even
    # when they're the same window.
    _user32.GetParent.restype           = ctypes.c_void_p
    _user32.GetParent.argtypes          = (ctypes.c_void_p,)
    _user32.GetMenu.restype             = ctypes.c_void_p
    _user32.GetMenu.argtypes            = (ctypes.c_void_p,)
    _user32.GetSubMenu.restype          = ctypes.c_void_p
    _user32.GetSubMenu.argtypes         = (ctypes.c_void_p, ctypes.c_int)
    _user32.GetMenuItemCount.argtypes   = (ctypes.c_void_p,)
    _user32.DrawMenuBar.argtypes        = (ctypes.c_void_p,)
    _user32.EnumChildWindows.argtypes   = (ctypes.c_void_p, _EnumWindowsProc, wintypes.LPARAM)
    _user32.GetClassNameW.argtypes      = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
    _user32.IsWindowVisible.argtypes    = (ctypes.c_void_p,)
    _user32.ShowWindow.argtypes         = (ctypes.c_void_p, ctypes.c_int)
    _user32.SetWindowTextW.argtypes     = (ctypes.c_void_p, ctypes.c_wchar_p)


# Cached HICON for the Hotkeys brand mark, loaded lazily on first use.
# Applied to every Tenacity-owned window so the upstream logo never
# shows in title bars, taskbar entries, or Alt-Tab thumbnails.
_brand_hicon: Optional[int] = None
_brand_hicon_attempted = False


def _get_brand_hicon() -> int:
    """Load the Hotkeys brand .ico once and return its HICON. Returns
    0 on failure (Win32 NULL = no icon swap)."""
    global _brand_hicon, _brand_hicon_attempted
    if _brand_hicon is not None:
        return _brand_hicon
    if _brand_hicon_attempted:
        return 0
    _brand_hicon_attempted = True
    if sys.platform != 'win32':
        return 0
    try:
        # The icon lives next to the user's data folder, written at
        # startup by App._save_brand_ico(). Fall back to the build-
        # time icon next to the source if AppData copy is missing.
        candidates = []
        local_appdata = os.environ.get('APPDATA')
        if local_appdata:
            candidates.append(
                Path(local_appdata) / 'Hotkeys' / 'app_icon.ico')
        candidates.append(_bundled_root() / 'build_icon.ico')
        for p in candidates:
            if p.exists():
                hicon = _user32.LoadImageW(
                    None, str(p), IMAGE_ICON, 0, 0,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if hicon:
                    _brand_hicon = hicon
                    return hicon
    except Exception as e:
        logger.debug(f'brand icon load failed: {e}')
    return 0


def _apply_brand_icon(hwnd: int) -> None:
    """Replace *hwnd*'s window icon with the Hotkeys brand mark on
    all four icon slots Windows checks:

      1. WM_SETICON ICON_SMALL  → title bar
      2. WM_SETICON ICON_BIG    → taskbar / Alt-Tab
      3. Class GCLP_HICONSM     → fallback before WM_SETICON propagates
      4. Class GCLP_HICON       → fallback before WM_SETICON propagates

    The class-icon overrides are what kill the brief Tenacity logo
    flash on launch. Without them, Windows paints the class icon
    first (which Tenacity registered with its own logo) and only
    swaps to our WM_SETICON value on the next paint cycle.
    """
    if sys.platform != 'win32' or not hwnd:
        return
    hicon = _get_brand_hicon()
    if not hicon:
        return
    try:
        _user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
        _user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hicon)
    except Exception:
        pass
    try:
        _user32.SetClassLongPtrW(hwnd, GCLP_HICON,   hicon)
        _user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, hicon)
    except Exception:
        pass


def _find_main_hwnd_by_pid(pid: int, timeout_s: float = _HWND_WAIT_S) -> Optional[int]:
    """Find the main top-level window owned by *pid* and pre-emptively
    rebrand every Tenacity-titled window we observe along the way.

    The polling-only approach left a visible flash: Tenacity creates
    the window with title "Tenacity", calls ShowWindow, the OS paints
    "Tenacity" on screen, THEN our walker finds and renames it. Even
    with 5 ms polling there's ≥ 1 paint cycle showing the upstream
    name.

    This version scans for windows owned by *pid* regardless of
    visibility, and the moment any of them contains "Tenacity" or
    "Audacity" in its title we rename it via SetWindowTextW. If we
    catch the main window during its hidden creation phase (before
    Tenacity calls ShowWindow), the user never sees the upstream
    title at all — the first painted frame shows "Audio Editor".

    Returns the most likely main window (largest visible title) once
    one becomes visible, or any best candidate at timeout.
    """
    if sys.platform != 'win32':
        return None
    start = time.time()
    end = start + timeout_s
    best_invisible: Optional[tuple[int, str]] = None

    while time.time() < end:
        visible_candidates: list[tuple[int, str]] = []
        invisible_candidates: list[tuple[int, str]] = []

        def _cb(hwnd: int, _: int) -> bool:
            owner_pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value != pid:
                return True
            # Top-level only (no parent owner). Splash / tooltip are
            # typically owned, so this filters them.
            if _user32.GetWindow(hwnd, 4):  # GW_OWNER == 4
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            visible = bool(_user32.IsWindowVisible(hwnd))
            (visible_candidates if visible else invisible_candidates).append(
                (hwnd, buf.value))
            return True

        try:
            _user32.EnumWindows(_EnumWindowsProc(_cb), 0)
        except Exception:
            pass

        # Pre-emptive rebrand: rewrite any Tenacity/Audacity title now,
        # even if the window is still hidden. The moment Tenacity shows
        # the window, the first paint will already say "Audio Editor".
        # Also swap the window icon to the Hotkeys brand mark so the
        # title bar + taskbar entry never show the upstream logo.
        for hwnd, title in (visible_candidates + invisible_candidates):
            if 'Tenacity' in title or 'Audacity' in title:
                try:
                    _user32.SetWindowTextW(hwnd, DISPLAY_TITLE)
                except Exception:
                    pass
            _apply_brand_icon(hwnd)

        # Return only once we have a visible candidate (= the user-
        # facing main window is on screen).
        if visible_candidates:
            visible_candidates.sort(key=lambda c: -len(c[1]))
            return visible_candidates[0][0]

        # Track the longest-titled invisible window in case we hit the
        # timeout before anything goes visible.
        if invisible_candidates:
            invisible_candidates.sort(key=lambda c: -len(c[1]))
            best_invisible = invisible_candidates[0]

        # Tight poll for the first second (when the window is in
        # flight), then loosen to keep CPU cost trivial.
        elapsed = time.time() - start
        time.sleep(0.005 if elapsed < 1.0 else 0.1)

    return best_invisible[0] if best_invisible else None


def _set_window_title(hwnd: int, new_title: str) -> bool:
    if sys.platform != 'win32' or not hwnd:
        return False
    try:
        return bool(_user32.SetWindowTextW(hwnd, new_title))
    except Exception:
        return False


def _rebrand_text(s: str) -> str:
    """Replace every upstream brand token in *s* with our DISPLAY_TITLE.
    Returns the (possibly unchanged) string."""
    if not s:
        return s
    out = s
    for old, new in _BRAND_REWRITES:
        if old in out:
            out = out.replace(old, new)
    return out


def _get_window_text(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ''
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


if sys.platform == 'win32':
    # Win32 menu item info struct, lets us read and write menu text
    # via Get/SetMenuItemInfoW. wxWidgets-rendered menus expose their
    # display strings through this same API.
    class _MENUITEMINFOW(ctypes.Structure):
        _fields_ = [
            ('cbSize',        wintypes.UINT),
            ('fMask',         wintypes.UINT),
            ('fType',         wintypes.UINT),
            ('fState',        wintypes.UINT),
            ('wID',           wintypes.UINT),
            ('hSubMenu',      wintypes.HMENU),
            ('hbmpChecked',   wintypes.HBITMAP),
            ('hbmpUnchecked', wintypes.HBITMAP),
            ('dwItemData',    ctypes.c_void_p),
            ('dwTypeData',    wintypes.LPWSTR),
            ('cch',           wintypes.UINT),
            ('hbmpItem',      wintypes.HBITMAP),
        ]

    _MIIM_STRING       = 0x0040
    _MF_BYPOSITION     = 0x0400

# Top-level menu titles to remove entirely from the menu bar. The Help
# menu's whole subtree (About, Manual, Wiki, Diagnostics) carries the
# upstream brand and has no useful function for our wrapping use case,
# stripping the top-level entry removes every leaf in one move and also
# makes the About box unreachable through normal navigation.
_MENUS_TO_REMOVE = ('Help',)

# Win32 child-control class names to keep hidden inside the main
# window. wxStatusBar wraps the standard 'msctls_statusbar32' control,
# the slim 23-pixel strip at the bottom showing transport state
# ("Stopped.", "Recording.", etc). Hidden every poll because wx
# re-shows it on certain layout events.
_CONTROL_CLASSES_TO_HIDE = ('msctls_statusbar32',)

# Window-text identifiers for child panels to keep hidden. The "ToolDock"
# at the bottom of the main window hosts the Selection / Time / Snap-To /
# Project Rate toolbars, a dense strip that adds visual noise on first
# open and is rarely needed by casual users. We hide the whole dock so
# the editor opens with just transport + tracks visible.
_CONTROL_TEXTS_TO_HIDE = ('ToolDock',)


def _get_menu_text(hmenu: int, pos: int) -> str:
    """Read the display string of the menu item at *pos* in *hmenu*."""
    if sys.platform != 'win32' or not hmenu:
        return ''
    try:
        # First call with empty buffer to learn the required length.
        length = _user32.GetMenuStringW(hmenu, pos, None, 0, _MF_BYPOSITION)
        if length <= 0:
            return ''
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetMenuStringW(hmenu, pos, buf, length + 1, _MF_BYPOSITION)
        return buf.value
    except Exception:
        return ''


def _set_menu_text(hmenu: int, pos: int, new_text: str) -> bool:
    """Rewrite the display string of the menu item at *pos*."""
    if sys.platform != 'win32' or not hmenu:
        return False
    try:
        mii = _MENUITEMINFOW()
        mii.cbSize = ctypes.sizeof(_MENUITEMINFOW)
        mii.fMask = _MIIM_STRING
        mii.dwTypeData = new_text
        mii.cch = len(new_text)
        return bool(_user32.SetMenuItemInfoW(hmenu, pos, True, ctypes.byref(mii)))
    except Exception:
        return False


def _normalize_menu_label(s: str) -> str:
    """Strip the wx menu syntax (accelerator '&' prefix, '...' suffix,
    tab-separated shortcut hint) so we can compare against simple names
    like 'Help'."""
    if not s:
        return ''
    # Drop everything after the tab (the shortcut hint).
    s = s.split('\t', 1)[0]
    # Drop accelerator markers.
    s = s.replace('&', '')
    # Drop trailing ellipsis variants.
    for suffix in ('…', '...'):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.strip()


def _rebrand_menu_recursive(hmenu: int) -> None:
    """Walk every item in *hmenu* and every nested submenu, rewriting
    upstream brand tokens in display strings AND deleting any top-level
    entries listed in _MENUS_TO_REMOVE. Win32 menus live in the user32
    HMENU tree, separate from HWNDs, so they need their own walk."""
    if sys.platform != 'win32' or not hmenu:
        return
    try:
        # First pass, from the back so deletes don't shift indices we
        # still need. Delete top-level entries that match _MENUS_TO_REMOVE.
        count = _user32.GetMenuItemCount(hmenu)
        for i in range(count - 1, -1, -1):
            try:
                label = _normalize_menu_label(_get_menu_text(hmenu, i))
                if label in _MENUS_TO_REMOVE:
                    _user32.DeleteMenu(hmenu, i, _MF_BYPOSITION)
            except Exception:
                continue

        # Second pass, rewrite remaining items + recurse into submenus.
        count = _user32.GetMenuItemCount(hmenu)
        if count <= 0:
            return
        for i in range(count):
            try:
                cur = _get_menu_text(hmenu, i)
                new = _rebrand_text(cur)
                if new != cur:
                    _set_menu_text(hmenu, i, new)
                sub = _user32.GetSubMenu(hmenu, i)
                if sub:
                    _rebrand_menu_recursive(sub)
            except Exception:
                continue
    except Exception:
        pass


def _rebrand_window_and_children(hwnd: int) -> None:
    """Walk *hwnd*, its menu bar, and every child control, replacing
    upstream brand tokens in any user-visible label. Cheap, idempotent,
    safe to call on every poll, SetWindowTextW / SetMenuItemInfoW only
    fire when the text actually differs from what we want."""
    if sys.platform != 'win32' or not hwnd:
        return
    try:
        cur = _get_window_text(hwnd)
        new = _rebrand_text(cur)
        if new != cur:
            _user32.SetWindowTextW(hwnd, new)

        # Menu bar (and every submenu) is outside the HWND tree.
        try:
            hmenu = _user32.GetMenu(hwnd)
            if hmenu:
                _rebrand_menu_recursive(hmenu)
                # Tell the window to repaint its menu bar so changes
                # show up immediately rather than on next mouse-over.
                _user32.DrawMenuBar(hwnd)
        except Exception:
            pass

        def _child_cb(child_hwnd: int, _: int) -> bool:
            try:
                c_cur = _get_window_text(child_hwnd)
                c_new = _rebrand_text(c_cur)
                if c_new != c_cur:
                    _user32.SetWindowTextW(child_hwnd, c_new)
                # Hide rules apply ONLY to direct children of the top-
                # level frame, not deeper descendants. Tenacity nests a
                # ToolDock inside the Top Panel that holds Transport +
                # Tools + Meters, with the SAME window text "ToolDock"
                # as the bottom strip we want to hide. Without this
                # parent check the recursive enumeration would catch
                # both and the user loses Play/Stop/Record.
                if _user32.GetParent(child_hwnd) != hwnd:
                    return True
                should_hide = False
                if _CONTROL_CLASSES_TO_HIDE:
                    cls_buf = ctypes.create_unicode_buffer(64)
                    _user32.GetClassNameW(child_hwnd, cls_buf, 64)
                    if cls_buf.value in _CONTROL_CLASSES_TO_HIDE:
                        should_hide = True
                if not should_hide and _CONTROL_TEXTS_TO_HIDE:
                    if c_cur in _CONTROL_TEXTS_TO_HIDE:
                        should_hide = True
                if should_hide and _user32.IsWindowVisible(child_hwnd):
                    _user32.ShowWindow(child_hwnd, 0)  # SW_HIDE
            except Exception:
                pass
            return True

        _user32.EnumChildWindows(hwnd, _EnumWindowsProc(_child_cb), 0)
    except Exception:
        pass


def _auto_dismiss_warning_dialog(dialog_hwnd: int) -> bool:
    """If *dialog_hwnd* is a Tenacity "Warning" dialog with a
    'Don't show this warning again' checkbox, tick the checkbox and
    click OK. Returns True if dismissed.

    Belt-and-suspenders complement to cfg-level suppression: even if
    the cfg seed didn't take effect (timing, value format edge case,
    new warning key we haven't catalogued), this kills the dialog
    within one walker poll AND writes the suppression flag so future
    cfg-driven suppression takes hold.

    We only dismiss when the dialog clearly identifies itself as a
    "don't show again" optional warning. Modal dialogs without that
    checkbox (real errors that need user input) are left alone.
    """
    if sys.platform != 'win32' or not dialog_hwnd:
        return False
    try:
        # Look for the checkbox and OK button among the dialog's
        # direct children. wxWidgets renders them as standard Win32
        # Button controls with type-defining text.
        ck = ctypes.c_void_p(0)
        ok = ctypes.c_void_p(0)

        def _walk(child_hwnd: int, _: int) -> bool:
            try:
                txt = _get_window_text(child_hwnd)
                low = (txt or '').lower()
                # Checkbox label is the well-known phrase. Match
                # liberally to cover variations across Tenacity/
                # Audacity versions.
                if ('show this warning again' in low
                        or 'don\'t show this' in low):
                    if ck.value is None or ck.value == 0:
                        ck.value = child_hwnd
                elif txt == 'OK' or txt == '&OK':
                    if ok.value is None or ok.value == 0:
                        ok.value = child_hwnd
            except Exception:
                pass
            return True

        _user32.EnumChildWindows(dialog_hwnd, _EnumWindowsProc(_walk), 0)
        if not ck.value or not ok.value:
            return False
        # BM_CLICK = 0x00F5, simulates a real mouse click that fires
        # the button's WM_COMMAND and writes the cfg suppression.
        _user32.SendMessageW(ck.value, 0x00F5, 0, 0)
        time.sleep(0.05)
        _user32.SendMessageW(ok.value, 0x00F5, 0, 0)
        logger.info('audio editor: auto-dismissed warning dialog')
        return True
    except Exception as e:
        logger.debug(f'auto-dismiss skipped: {e}')
        return False


def _rebrand_all_owned_windows(pid: int) -> None:
    """Find every top-level window owned by *pid* (main, dialogs, popups)
    and rebrand their text + children. Used by the title-keeper so newly
    appearing dialogs (crash recovery, errors, save-as) get scrubbed
    within ~1 poll interval of their appearance.

    Also auto-dismisses any "Warning" dialog that carries a 'Don't
    show this warning again' checkbox, ticking the checkbox before
    OK so the cfg gets the suppression flag for future launches."""
    if sys.platform != 'win32':
        return
    seen: list[tuple[int, str]] = []

    def _cb(hwnd: int, _: int) -> bool:
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            owner_pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value == pid:
                seen.append((hwnd, _get_window_text(hwnd)))
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_EnumWindowsProc(_cb), 0)
    except Exception:
        return

    for h, title in seen:
        _rebrand_window_and_children(h)
        _apply_brand_icon(h)
        # Dismiss optional warning dialogs proactively.
        if title == 'Warning':
            _auto_dismiss_warning_dialog(h)


def _is_window(hwnd: int) -> bool:
    if sys.platform != 'win32' or not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(hwnd))
    except Exception:
        return False


def _is_iconic(hwnd: int) -> bool:
    if sys.platform != 'win32' or not hwnd:
        return False
    try:
        return bool(_user32.IsIconic(hwnd))
    except Exception:
        return False


def _is_foreground(hwnd: int) -> bool:
    if sys.platform != 'win32' or not hwnd:
        return False
    try:
        return _user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _show(hwnd: int, cmd: int) -> None:
    if sys.platform != 'win32' or not hwnd:
        return
    try:
        _user32.ShowWindow(hwnd, cmd)
    except Exception:
        pass


# ─── Launcher ─────────────────────────────────────────────────────────────


class AudioEditorLauncher:
    """Owns the lifecycle of one bundled Tenacity instance.

    Lives as a singleton on the parent App. Public API is just `toggle()`,
    which implements the same open/minimize/restore semantics the
    whiteboard hotkey uses.
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._hwnd: int = 0
        self._title_thread: Optional[threading.Thread] = None
        self._stop_title_thread = threading.Event()
        # True between _spawn() entry and the title-keeper finding the
        # main window. Prevents a second Shift+F10 during this window
        # from launching a duplicate Tenacity process.
        self._spawning: bool = False

    # ── Public ────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Tear down everything this launcher owns — the spawned audio
        editor process, the title-keeper thread, the hint and loading
        overlays. Called by App._quit() so quitting Hotkeys via the
        tray does NOT leave a Tenacity window or process behind.
        Idempotent and exception-safe."""
        # Stop the title-keeper polling first so it doesn't fight us.
        try:
            self._stop_title_thread.set()
        except Exception:
            pass
        # Kill the child editor process if it's still running.
        try:
            if self._proc is not None and self._proc.poll() is None:
                try: self._proc.terminate()
                except Exception: pass
                # Brief wait for graceful exit, then force.
                try: self._proc.wait(timeout=1.5)
                except Exception:
                    try: self._proc.kill()
                    except Exception: pass
        except Exception:
            pass
        self._proc = None
        self._hwnd = 0

    def toggle(self) -> None:
        """Shift+F10 entry point.

        Spawn / minimize / restore / foreground / respawn, mirrors
        whiteboard semantics so muscle memory carries across the app.
        """
        # Already spawning? Bounce, the user double-tapped during the
        # 1-3s window between launch and main-window appearance.
        # Without this guard a fast second press would start a second
        # Tenacity process before the first one's hwnd is registered.
        if self._spawning:
            logger.info('audio editor toggle: ignored (spawn in progress)')
            return

        # Process still alive?
        alive = self._proc is not None and self._proc.poll() is None

        # Window still alive?
        win_alive = alive and _is_window(self._hwnd)

        if win_alive:
            iconic = _is_iconic(self._hwnd)
            is_fg  = _is_foreground(self._hwnd)
            if iconic:
                logger.info('audio editor toggle: restore (was iconic)')
                _show(self._hwnd, SW_RESTORE)
                self._force_foreground(self._hwnd)
            elif is_fg:
                logger.info('audio editor toggle: minimize (was foreground)')
                _show(self._hwnd, SW_MINIMIZE)
            else:
                logger.info('audio editor toggle: foreground (was background)')
                self._force_foreground(self._hwnd)
            return

        # Either dead or never spawned, launch fresh.
        logger.info('audio editor toggle: spawn (no live window)')
        self._spawn()

    # ── Internals ─────────────────────────────────────────────────────────

    def _spawn(self) -> None:
        exe = _exe_path()
        if not exe.exists():
            _show_fatal(
                DISPLAY_TITLE,
                f'Audio editor bundle is missing.\n\nExpected at:\n{exe}\n\n'
                f'Reinstall the app or rebuild the dist.')
            logger.error(f'audio editor exe missing at {exe}')
            return

        # Keep all unsaved-project state inside the dist + drain legacy
        # AppData crumbs so the crash-recovery dialog stops nagging.
        _ensure_portable_state()

        # Tell any previous title-keeper to stop, and wait briefly so it
        # exits before we install a new one. Without this an old thread
        # keeps polling against a dead PID after a kill+respawn.
        self._stop_title_thread.set()
        old_thread = self._title_thread
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=2.0)
        self._stop_title_thread.clear()

        # Reset hwnd so toggle does not mistake a dead window for live.
        self._hwnd = 0

        flags = 0
        if sys.platform == 'win32':
            # DETACHED_PROCESS is REQUIRED. Without it, Python GC's finalizer
            # for the Popen object touches the still-running child's handle
            # and triggers ACCESS_VIOLATION 0xc0000005 in python312.dll a few
            # seconds after spawn. The AV-heuristic concern (combo flagged by
            # AVG) is moot for signed builds and is handled by per-folder
            # exceptions for unsigned local installs.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

        # No "Loading…" overlay any more — the pre-emptive scan in
        # _find_main_hwnd_by_pid rebrands Tenacity-titled windows
        # while they're still hidden, so the visible flash is
        # already down to a few ms. Showing a card for the full 3-5s
        # boot would have hung around far longer than the flash it
        # was trying to mask.
        try:
            self._spawning = True
            self._proc = subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            logger.info(f'Launched audio editor pid={self._proc.pid}')
            # Attach to the cleanup Job Object so this child dies if
            # the main process is force-killed. Best-effort.
            if _CLEANUP_ASSIGNER is not None:
                try:
                    _CLEANUP_ASSIGNER(self._proc.pid)
                except Exception as e:
                    logger.warning(f'audio editor cleanup-job assign failed: {e}')
        except Exception as e:
            _show_fatal(DISPLAY_TITLE, f'Failed to launch audio editor:\n{e}')
            logger.error(f'audio editor launch failed: {e}')
            self._proc = None
            self._spawning = False
            return

        # Kick off the window-finder + title-keeper in a background thread.
        # We can't block the caller, Shift+F10 must return immediately.
        t = threading.Thread(
            target=self._title_keeper_loop,
            daemon=True,
            name='AudioEditor-TitleKeeper',
        )
        self._title_thread = t
        t.start()

    def _title_keeper_loop(self) -> None:
        """Find the main window once it appears, then keep its title
        pinned to DISPLAY_TITLE as long as the process lives.

        Tenacity rewrites its own title on project load / save (e.g. to
        "Project Name - Tenacity"). We re-assert our brand on every
        poll so the upstream name never leaks into the front UI.
        """
        if self._proc is None:
            return
        pid = self._proc.pid

        hwnd = _find_main_hwnd_by_pid(pid)
        if not hwnd:
            logger.warning('audio editor window did not appear within timeout')
            self._spawning = False
            return
        self._hwnd = hwnd
        _rebrand_all_owned_windows(pid)
        _apply_brand_icon(hwnd)
        self._spawning = False
        logger.info(f'audio editor window hwnd={hwnd}, brand rewrites applied')

        while not self._stop_title_thread.is_set():
            # Process dead? Break, walker exits, next toggle respawns.
            if self._proc is None or self._proc.poll() is not None:
                break
            # Original main window may have been destroyed and replaced
            # (Tenacity recreates its main frame on certain operations,
            # like opening a project or reloading plugins). Re-find a
            # live main window owned by this PID rather than giving up.
            if not _is_window(self._hwnd):
                new_hwnd = _find_main_hwnd_by_pid(pid, timeout_s=2.0)
                if not new_hwnd:
                    # No window AND process still alive, user probably
                    # closed the editor window without exiting; break so
                    # next toggle respawns cleanly.
                    break
                self._hwnd = new_hwnd
                logger.info(f'audio editor main window replaced, hwnd={new_hwnd}')
            # Re-scrub upstream brand in the main window AND any newly
            # appeared dialogs (crash recovery, error popups, About box,
            # save-as etc). The walk is cheap, idempotent, and only fires
            # WM_SETTEXT on controls whose text actually needs changing.
            _rebrand_all_owned_windows(pid)
            time.sleep(_TITLE_POLL_S)

        # Process / window gone, clear state so next toggle respawns.
        self._hwnd = 0

_LAUNCHER: Optional['AudioEditorLauncher'] = None
# Optional callback (set by main.py at boot) that adds a spawned child
# pid to the app's Win32 cleanup Job Object so a force-kill of the main
# process reaps the Tenacity child too. Without this set, the editor
# orphans when the main app crashes — the exact bug the Job Object was
# added to prevent for whiteboard. Default no-op keeps standalone runs
# of this module working.
_CLEANUP_ASSIGNER = None


def set_cleanup_assigner(fn) -> None:
    """main.py calls this once after creating the cleanup Job Object so
    every Audio Editor spawn from this point gets reaped on app exit."""
    global _CLEANUP_ASSIGNER
    _CLEANUP_ASSIGNER = fn


def get_launcher() -> 'AudioEditorLauncher':
    """Module-level singleton accessor. The launcher owns one Tenacity
    child process for the app's lifetime; spinning up a second one per
    Shift+F10 press would orphan the previous window and double the
    RAM footprint. Lazy so importing this module doesn't construct it."""
    global _LAUNCHER
    if _LAUNCHER is None:
        _LAUNCHER = AudioEditorLauncher()
    return _LAUNCHER


def toggle(tk_root=None) -> None:
    """Top-level convenience. main.py calls this from Shift+F10.

    *tk_root* is accepted for backward compatibility with the old
    drop-hint overlay (now removed) and is otherwise unused; we just
    forward the call through to the singleton launcher."""
    get_launcher().toggle()
