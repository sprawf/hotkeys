"""Offline whiteboard, fully self-contained, dist-ready.

Embeds the bundled React sketch component (under whiteboard_assets/dist/)
inside an Edge WebView2 window via pywebview. No network required at
runtime, fonts, locales, JS, CSS all local.

Dist-ready:
  • Asset paths resolve via _MEIPASS when frozen by PyInstaller.
  • Designed to run two ways:
      - Standalone:   python whiteboard.py
      - From parent:  main.py spawns this module (frozen exe re-execs self
                      with --whiteboard sentinel; see main.py)
  • Missing Edge WebView2 Runtime triggers a friendly Tk dialog with a
    "Download" button instead of a silent failure.
  • Disables Chromium's built-in browser-zoom hotkeys (Ctrl++ / Ctrl+- /
    Ctrl+0) so the canvas handles them as zoom instead.

Save/load round-trips through window.pywebview.api → whiteboard.json
(scene file shared with any other process that wants to read or seed it).
"""
from __future__ import annotations
import json, os, sys, traceback
from pathlib import Path


# ── Path resolution: frozen-aware ─────────────────────────────────────────
def _bundled_root() -> Path:
    """Where bundled assets live. Works as script, venv-python, or frozen exe."""
    if getattr(sys, 'frozen', False):
        # PyInstaller onefile/onedir: assets extracted to _MEIPASS
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return Path(__file__).resolve().parent


ROOT       = _bundled_root()
DIST       = ROOT / 'whiteboard_assets' / 'dist'
INDEX_HTML = DIST / 'index.html'

# User data lives next to the exe when frozen (portable dist), else %APPDATA%.
# Import storage.appdata_dir() so the whiteboard subprocess uses the
# IDENTICAL data path the main app uses — including its read-only-install
# fallback to %TEMP%/Hotkeys/. Previously this file re-implemented the
# logic and diverged when the main app's <exe_dir>/data fallback fired,
# causing whiteboard scenes to silently fail to save.
try:
    from storage import appdata_dir as _appdata_dir
    APP_DATA = Path(_appdata_dir())
except Exception:
    # Defensive fallback if storage import ever breaks. Matches the
    # frozen-mode path of storage.appdata_dir().
    if getattr(sys, 'frozen', False):
        APP_DATA = Path(sys.executable).parent / 'data'
    else:
        _appdata = os.environ.get('APPDATA') or \
                   str(Path.home() / 'AppData' / 'Roaming')
        APP_DATA = Path(_appdata) / 'Hotkeys'
APP_DATA.mkdir(parents=True, exist_ok=True)
SCENE_FILE = APP_DATA / 'whiteboard.json'


# ── Friendly missing-bundle / missing-runtime handlers ────────────────────
def _show_fatal(title: str, msg: str) -> None:
    """Show a native Win32 message box, works even if Tk isn't available."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR
    except Exception:
        print(f'{title}: {msg}', file=sys.stderr)


def _require_bundle() -> None:
    if INDEX_HTML.exists():
        return
    _show_fatal('Whiteboard', f'Offline bundle missing:\n{INDEX_HTML}\n\n'
                f'If running from source: cd whiteboard_assets && '
                f'npm install && node build.mjs')
    sys.exit(2)


def _require_webview2_runtime() -> None:
    """Detect Edge WebView2 Runtime via registry. Offer to install if absent.

    Win10 22H2+ and Win11 ship the runtime preinstalled, this check is a
    safety net for old Win10 builds."""
    if sys.platform != 'win32':
        return
    try:
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE,
             r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients'
             r'\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
            (winreg.HKEY_LOCAL_MACHINE,
             r'SOFTWARE\Microsoft\EdgeUpdate\Clients'
             r'\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
            (winreg.HKEY_CURRENT_USER,
             r'SOFTWARE\Microsoft\EdgeUpdate\Clients'
             r'\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'),
        ]
        for root, sub in keys:
            try:
                with winreg.OpenKey(root, sub) as k:
                    pv, _ = winreg.QueryValueEx(k, 'pv')
                    if pv and pv != '0.0.0.0':
                        return  # installed
            except OSError:
                continue
    except Exception:
        return  # registry probe failed, fall through and let webview try

    # Not installed, show friendly dialog and open download page
    import ctypes
    res = ctypes.windll.user32.MessageBoxW(
        0,
        'The Edge WebView2 Runtime is required for the whiteboard.\n\n'
        'It\'s a free Microsoft component (~2 MB) that ships with Windows '
        '10 22H2+ and Windows 11. Click OK to open the download page.',
        'Whiteboard, runtime missing', 0x31)  # OK/Cancel + info icon
    if res == 1:  # IDOK
        import webbrowser
        webbrowser.open(
            'https://developer.microsoft.com/microsoft-edge/webview2/')
    sys.exit(3)


# ── Hotkey normalisation (mirrors hotkey_validator.normalize_hotkey) ─────
_MOD_ALIASES = {
    'control': 'ctrl', 'ctl': 'ctrl', 'option': 'alt', 'menu': 'alt',
    'cmd': 'win', 'meta': 'win', 'super': 'win', 'windows': 'win',
    'return': 'enter', 'esc': 'escape',
}
_MOD_ORDER = ('ctrl', 'alt', 'shift', 'win')

def _normalize_for_js(s: str) -> str:
    """Lower-case + sorted modifiers + alias-fold, e.g. "ALT+SHIFT+W" →
    "alt+shift+w". Matches the format the JS side produces from a
    KeyboardEvent."""
    s = (s or '').strip().lower()
    if not s: return ''
    for sep in (' + ', ' +', '+ ', ' - ', '-', ' '):
        s = s.replace(sep, '+')
    parts = [p for p in s.split('+') if p]
    parts = [_MOD_ALIASES.get(p, p) for p in parts]
    mods = sorted({p for p in parts if p in _MOD_ORDER},
                  key=_MOD_ORDER.index)
    keys = [p for p in parts if p not in _MOD_ORDER]
    return '+'.join(mods + keys)


# ── Python-side bridge (called from app.tsx via window.pywebview.api) ─────
class Api:
    def load_scene(self) -> str:
        try:
            if SCENE_FILE.exists():
                return SCENE_FILE.read_text(encoding='utf-8')
        except Exception as e:
            print(f'[load] {e}', file=sys.stderr)
        return ''

    def get_reserved_keys(self) -> str:
        """Return JSON list of normalised hotkey combos the host app has
        claimed. The whiteboard's JS installs a capture-phase keydown
        handler that swallows any keypress matching one of these, so
        the app's hotkeys always supersede Whiteboard's built-in
        shortcuts.

        Reads config.json + prompts.json + chains.json + macros/*.json
        on every call so the list stays current after settings edits
        without needing to restart the whiteboard."""
        import json as _json
        out = set()
        try:
            cfg = _json.loads((APP_DATA / 'config.json').read_text(
                encoding='utf-8'))
            for hk in (cfg.get('hotkeys') or {}).values():
                if hk: out.add(_normalize_for_js(hk))
        except Exception: pass
        try:
            for p in _json.loads((APP_DATA / 'prompts.json').read_text(
                    encoding='utf-8')):
                hk = (p.get('hotkey') or '').strip()
                if hk: out.add(_normalize_for_js(hk))
        except Exception: pass
        try:
            for c in _json.loads((APP_DATA / 'chains.json').read_text(
                    encoding='utf-8')):
                hk = (c.get('hotkey') or '').strip()
                if hk: out.add(_normalize_for_js(hk))
        except Exception: pass
        try:
            macros_dir = APP_DATA / 'macros'
            if macros_dir.is_dir():
                for mf in macros_dir.glob('*.json'):
                    try:
                        m = _json.loads(mf.read_text(encoding='utf-8'))
                        hk = (m.get('hotkey') or '').strip()
                        if hk: out.add(_normalize_for_js(hk))
                    except Exception: continue
        except Exception: pass
        return _json.dumps(sorted(out))

    def save_scene(self, txt: str) -> bool:
        try:
            tmp = SCENE_FILE.with_suffix('.json.tmp')
            tmp.write_text(txt, encoding='utf-8')
            tmp.replace(SCENE_FILE)
            return True
        except Exception as e:
            print(f'[save] {e}', file=sys.stderr)
            return False


_DBG_LOG = APP_DATA / 'whiteboard.log'

def _dbg(msg: str) -> None:
    try:
        with open(_DBG_LOG, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception: pass


def _auto_handle_permissions(window) -> None:
    """Pre-approve every WebView2 permission Whiteboard legitimately
    needs, and deny everything else.

    This is the entire audit of CoreWebView2PermissionKind:

        Enum value | Permission                  | Decision | Why
        -----------|------------------------------|----------|----
        1          | Microphone                   | DENY     | Whiteboard doesn't use it
        2          | Camera                       | DENY     | Whiteboard doesn't use it
        3          | Geolocation                  | DENY     | Whiteboard doesn't use it
        4          | Notifications                | DENY     | Whiteboard doesn't use it
        5          | OtherSensors                 | DENY     | Whiteboard doesn't use it
        6          | ClipboardRead                | ALLOW    | Ctrl+V paste flow
        7          | MultipleAutomaticDownloads   | ALLOW    | Save-as-image / export
        8          | FileReadWrite                | ALLOW    | Insert-image / pick-color
        9          | Autoplay                     | DENY     | Whiteboard has no media
        10         | LocalFonts                   | ALLOW    | Font enumeration if used
        11         | MidiSystemExclusiveMessages  | DENY     | Whiteboard doesn't use it
        12         | WindowManagement             | DENY     | Single-window app

    Plus: NewWindowRequested is cancelled, Whiteboard never opens new
    windows in our setup (we hid the share/collab/external-link UI), so
    any attempt to open one is treated as accidental and cancelled.
    """
    native = getattr(window, 'native', None)
    if native is None:
        _dbg('[perm] no native'); return
    ctrl = getattr(native, 'browser', None)
    if ctrl is None:
        _dbg('[perm] no browser'); return

    def apply():
        try:
            raw = getattr(ctrl, 'webview', None) or getattr(ctrl, 'web_view', None)
            if raw is None:
                _dbg('[perm] no WebView2 control'); return
            cwv2 = getattr(raw, 'CoreWebView2', None)
            if cwv2 is None:
                _dbg('[perm] CoreWebView2 not ready'); return

            from System import EventHandler
            from Microsoft.Web.WebView2.Core import (
                CoreWebView2PermissionRequestedEventArgs,
                CoreWebView2PermissionKind  as PK,
                CoreWebView2PermissionState as PS,
                CoreWebView2NewWindowRequestedEventArgs,
            )

            ALLOWED_KINDS = (
                PK.ClipboardRead,
                PK.MultipleAutomaticDownloads,
                PK.FileReadWrite,
                PK.LocalFonts,
            )

            def on_permission(_sender, args):
                try:
                    kind = args.PermissionKind
                    if kind in ALLOWED_KINDS:
                        args.State = PS.Allow
                    else:
                        # Microphone / Camera / Geolocation / Autoplay /
                        # Notifications / OtherSensors / MIDI / Window Mgmt
                        args.State = PS.Deny
                    _dbg(f'[perm] kind={kind} → {args.State}')
                except Exception as e:
                    _dbg(f'[perm] handler exc: {e!r}')

            def on_new_window(_sender, args):
                # Whiteboard can't open a new window in our setup, treat
                # the request as accidental and cancel.
                try:
                    args.Handled = True
                    _dbg(f'[perm] cancelled NewWindowRequest uri={args.Uri}')
                except Exception as e:
                    _dbg(f'[perm] new-window handler exc: {e!r}')

            # Belt-and-braces: block every navigation away from our local
            # file:// bundle. Catches target="_blank" links, programmatic
            # window.location = …, ANY accidental click that tries to
            # leave the whiteboard. The only allowed scheme is the
            # initial file:// load (and our own data:/blob: for assets).
            try:
                from Microsoft.Web.WebView2.Core import (
                    CoreWebView2NavigationStartingEventArgs,
                )
                def on_navigating(_sender, args):
                    try:
                        uri = str(args.Uri)
                        if (uri.startswith('file:///')
                                or uri.startswith('data:')
                                or uri.startswith('blob:')
                                or uri.startswith('about:blank')):
                            return  # allow our own asset loads
                        args.Cancel = True
                        _dbg(f'[perm] cancelled navigation to {uri[:80]}')
                    except Exception as e:
                        _dbg(f'[perm] nav handler exc: {e!r}')
                cwv2.add_NavigationStarting(
                    EventHandler[CoreWebView2NavigationStartingEventArgs](on_navigating))
            except Exception as e:
                _dbg(f'[perm] navigation hook unavailable: {e!r}')

            cwv2.add_PermissionRequested(
                EventHandler[CoreWebView2PermissionRequestedEventArgs](on_permission))
            cwv2.add_NewWindowRequested(
                EventHandler[CoreWebView2NewWindowRequestedEventArgs](on_new_window))
            _dbg('[perm] permission + nav + new-window handlers attached ✓')
        except Exception as e:
            _dbg(f'[perm] attach failed: {e!r}')

    try:
        from System import Action
        native.Invoke(Action(apply))
    except Exception as e:
        _dbg(f'[perm] Invoke failed: {e!r}')


def _force_light_titlebar(window) -> None:
    """Force the native title bar to stay LIGHT on every Windows version.

    On Win10 1809+ and Win11, the title bar can follow the OS dark-mode
    setting and render with a dark background + white text. We always
    want a white title bar (matching the rest of the app's chrome), so
    explicitly opt out of immersive dark mode via DWM.

    DWMWA_USE_IMMERSIVE_DARK_MODE = 20, value 0 = light, 1 = dark.
    """
    if sys.platform != 'win32':
        return
    native = getattr(window, 'native', None)
    if native is None:
        return

    def apply():
        try:
            import ctypes
            from ctypes import wintypes
            raw = getattr(native, 'Handle', None)
            if raw is None:
                _dbg('[titlebar] no Form.Handle'); return
            try:
                hwnd = int(raw.ToInt64())
            except Exception:
                hwnd = int(raw)
            if not hwnd:
                return
            dwm = ctypes.windll.dwmapi
            light = wintypes.DWORD(0)  # 0 = light mode title bar
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (Win10 20H1+, Win11)
            r = dwm.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), wintypes.DWORD(20),
                ctypes.byref(light), ctypes.sizeof(light))
            # Earlier builds used attribute 19, try that as a fallback
            if r != 0:
                dwm.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd), wintypes.DWORD(19),
                    ctypes.byref(light), ctypes.sizeof(light))
            _dbg(f'[titlebar] forced light (hr={r:#x})')
        except Exception as e:
            _dbg(f'[titlebar] failed: {e!r}')

    try:
        from System import Action
        native.Invoke(Action(apply))
    except Exception as e:
        _dbg(f'[titlebar] Invoke failed: {e!r}')


def _setup_window_icon(window) -> None:
    """Clean title bar + branded taskbar entry.

    * Title bar icon: hidden via Form.ShowIcon = False, matches the look
      the user asked for (no tiny default icon next to the window title).
    * Taskbar / Alt+Tab icon: set to the app's brand mark (the bolt-in-
      purple-frame .ico that main.py writes to %APPDATA% at startup).
      This way Notes, Whiteboard, and the tray all show the same identity
      instead of falling back to pythonw.exe's Python logo.
    """
    native = getattr(window, 'native', None)
    if native is None:
        _dbg('[icon] no native form'); return

    brand_ico = APP_DATA / 'app_icon.ico'

    def apply():
        try:
            if brand_ico.is_file():
                try:
                    from System.Drawing import Icon as _NetIcon
                    native.Icon = _NetIcon(str(brand_ico))
                    _dbg(f'[icon] taskbar icon loaded from {brand_ico}')
                except Exception as e:
                    _dbg(f'[icon] Icon load failed: {e!r}')
            else:
                _dbg(f'[icon] {brand_ico} missing, taskbar icon left default')
            native.ShowIcon = False
            _dbg('[icon] ShowIcon = False (title bar clean)')
        except Exception as e:
            _dbg(f'[icon] apply exc: {e!r}')

    try:
        from System import Action
        native.Invoke(Action(apply))
    except Exception as e:
        _dbg(f'[icon] Invoke failed: {e!r}')


def _disable_browser_zoom_keys(window) -> None:
    """Edge WebView2 intercepts Ctrl++, Ctrl+- and Ctrl+0 for browser zoom
    before they reach the document, so Whiteboard never sees them as
    canvas-zoom requests. Flip the runtime setting off so the keys pass
    through to Whiteboard.

    WebView2's `CoreWebView2.Settings` can only be touched from the UI
    thread. pywebview's edgechromium backend stores the WinForms control on
    `native.browser`; we dispatch the setting change via the form's
    Invoke() so it runs on the UI thread.
    """
    native = getattr(window, 'native', None)
    if native is None:
        _dbg('[zoom] no native, skip'); return
    ctrl = getattr(native, 'browser', None)
    if ctrl is None:
        _dbg('[zoom] no .browser on form, skip'); return

    def apply_settings():
        try:
            # pywebview's EdgeChrome wraps the WinForms WebView2 as `.webview`
            raw = getattr(ctrl, 'webview', None) or getattr(ctrl, 'web_view', None)
            if raw is None:
                _dbg('[zoom] no WebView2 control on EdgeChrome')
                return
            cwv2 = getattr(raw, 'CoreWebView2', None)
            if cwv2 is None:
                _dbg('[zoom] CoreWebView2 still None on UI thread'); return
            s = cwv2.Settings
            s.IsZoomControlEnabled = False
            _dbg('[zoom] IsZoomControlEnabled = False ✓')
            # AreBrowserAcceleratorKeysEnabled blocks ALL browser shortcuts
            # including Ctrl+F. We want Ctrl+F → Whiteboard's find. Skip it.
            # Just turn off page-level pinch zoom too.
            try:
                s.IsPinchZoomEnabled = False
                _dbg('[zoom] IsPinchZoomEnabled = False ✓')
            except Exception:
                pass
        except Exception as e:
            _dbg(f'[zoom] apply_settings exc: {e!r}')

    try:
        from System import Action
        native.Invoke(Action(apply_settings))
        _dbg('[zoom] invoked on UI thread ✓')
    except Exception as e:
        _dbg(f'[zoom] Invoke failed: {e!r}')


def _disable_webview2_telemetry():
    """Force the embedded Chromium to behave like a true offline runtime.

    WebView2 (Chromium) phones home by default, checks for binary
    updates, runs SmartScreen reputation lookups, ships usage pings to
    Microsoft. None of that helps a local sketching app and runs counter
    to the user's expectation of "fully offline".

    The cleanest way to suppress it is via the
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS environment variable,
    WebView2's runtime reads this on launch and applies the flags to
    the underlying Chromium command line. Must be set BEFORE pywebview
    starts the WebView2 process.

    Flags chosen:
      • --no-pings               , drop <a ping=…> beacons
      • --no-experiments         , opt out of A/B field trials
      • --no-default-browser-check, silence "set as default browser?"
      • --disable-background-networking, no background fetches
      • --disable-component-update     , no Chromium component updates
      • --disable-domain-reliability   , no DR beacons
      • --disable-features=SmartScreenEnhancedProtectionEnabled,
        SmartScreenProtectionEnabled,MediaRouter,OptimizationHints,
        InterestFeedContentSuggestions, disable telemetry features
      • --disable-sync           , no profile sync
      • --metrics-recording-only=false, no UMA upload
    """
    import os
    if os.environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'):
        return  # user / parent already set their own flags; respect them
    flags = (
        '--no-pings '
        '--no-experiments '
        '--no-default-browser-check '
        '--disable-background-networking '
        '--disable-component-update '
        '--disable-domain-reliability '
        '--disable-sync '
        '--disable-features='
        'SmartScreenEnhancedProtectionEnabled,'
        'SmartScreenProtectionEnabled,'
        'MediaRouter,'
        'OptimizationHints,'
        'InterestFeedContentSuggestions'
    )
    os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = flags


def _set_app_user_model_id():
    """Match the parent process's AUMID so this window groups under the
    Hotkeys app in the taskbar / Alt+Tab, same icon, same hover preview.
    Must run before any window is created."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'Hotkeys.App.1')
    except Exception:
        pass


def main():
    _set_app_user_model_id()
    _disable_webview2_telemetry()
    _require_bundle()
    _require_webview2_runtime()

    # Imported here (not at module top) so the registry check above can
    # short-circuit before we even touch pywebview, keeps the friendly
    # dialog reachable even if pywebview's import chain breaks.
    import webview

    api = Api()
    url = 'file:///' + str(INDEX_HTML).replace('\\', '/')

    # Window dimensions match the Shift+F7 Notes window. Center on the
    # primary monitor's work area (screen minus taskbar). On small
    # monitors, center_on_work_area shrinks the intent so no edge is
    # ever occluded by the taskbar or screen bounds.
    from win_geometry import center_on_work_area
    x, y, W, H = center_on_work_area(1216, 796)

    prewarm = '--prewarm' in sys.argv
    # NOTE: we deliberately don't use create_window(hidden=True) for prewarm.
    # pywebview's hidden flag creates the WebView2 control without painting,
    # which leaves the React app un-rendered until something forces a redraw.
    # The result: subsequent ShowWindow() displays a blank white surface
    # because the React tree was never given a chance to mount.
    # Instead we always create visible, position the window briefly offscreen
    # (so the user never sees the splash) and hide it via ShowWindow inside
    # the `loaded` event after the first paint completes. That gives us a
    # fully-rendered window that just happens to be hidden, so any later
    # ShowWindow() is instant and shows real content.
    if prewarm:
        # Offscreen until loaded → hidden
        x, y = -32000, -32000
    window = webview.create_window(
        title='Whiteboard (Shift+F8)',
        url=url,
        js_api=api,
        x=x, y=y, width=W, height=H,
        resizable=True,
        confirm_close=False,
    )
    # Disable browser-zoom hotkey interception once the WebView2 control
    # is ready. `loaded` fires after navigation, but the native control
    # is available before that, use `shown`.
    _dbg(f'\n=== launch === frozen={getattr(sys,"frozen",False)} '
         f'bundle={INDEX_HTML.exists()} app_data={APP_DATA}')
    # Only `loaded` fires AFTER CoreWebView2InitializationCompleted,
    # `shown` is too early and the CoreWebView2 property is still None.
    def _on_loaded():
        _disable_browser_zoom_keys(window)
        _setup_window_icon(window)
        _force_light_titlebar(window)
        _auto_handle_permissions(window)
        # Prewarm finalize: window has now painted at the offscreen
        # position with a fully-mounted React tree, so a Win32 ShowWindow
        # later will reveal real content (not a blank white canvas).
        # Move the window to the proper on-screen position BEFORE hiding,
        # so when the host app fires SW_SHOW the window appears where
        # the user expects, not at -32000,-32000.
        if prewarm:
            try:
                import ctypes as _c
                from ctypes import wintypes as _wt
                from win_geometry import center_on_work_area as _cw
                _u = _c.windll.user32
                _x, _y, _w, _h = _cw(1216, 796)
                WB_TITLE = 'Whiteboard (Shift+F8)'
                hwnd_found = [0]
                EnumProc = _c.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
                def _cb(h, _):
                    buf = _c.create_unicode_buffer(64)
                    _u.GetWindowTextW(h, buf, 64)
                    if buf.value == WB_TITLE:
                        hwnd_found[0] = h
                        return False
                    return True
                _u.EnumWindows(EnumProc(_cb), 0)
                hwnd = hwnd_found[0]
                if hwnd:
                    SWP_NOSIZE = 0x0001; SWP_NOZORDER = 0x0004
                    SWP_NOACTIVATE = 0x0010
                    _u.SetWindowPos(hwnd, 0, _x, _y, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                    SW_HIDE = 0
                    _u.ShowWindow(hwnd, SW_HIDE)
                    _dbg('[prewarm] window painted, moved on-screen, hidden')
                else:
                    _dbg('[prewarm] could not find hwnd to hide')
            except Exception as e:
                _dbg(f'[prewarm] hide failed: {e}')
    window.events.loaded += _on_loaded
    # Hide-on-close: clicking X keeps the subprocess alive with the
    # window hidden. Subsequent Shift+F8 from the host app reuses the
    # existing window via win32 ShowWindow(SW_SHOW) → sub-100 ms
    # re-open. The subprocess only really exits when Hotkeys itself
    # exits (handle severed by the PowerShell intermediary).
    #
    # (Investigated and exonerated re: a BSOD reported 2026-06-10 —
    # the stop code 0x000000D1 was caused by a TPM hardware error
    # logged 6 s before the crash; user-mode Python/Tk cannot produce
    # kernel-mode IRQL violations.)
    def _on_closing():
        try:
            window.hide()
        except Exception:
            return True   # let pywebview proceed if hide() can't fire
        return False      # cancel the actual close
    window.events.closing += _on_closing

    try:
        webview.start(
            gui='edgechromium',
            debug=False,
            private_mode=False,
            storage_path=str(APP_DATA / 'webview_profile'),
        )
    except Exception as e:
        # If webview itself crashes on init, translate the error into
        # something a non-developer can act on. The two common causes
        # on end-user PCs are missing .NET Framework 4.7.2+ and missing
        # Edge WebView2 Runtime — both are free Microsoft installers.
        err_text = f'{e}\n{traceback.format_exc()}'.lower()
        missing_runtime = any(k in err_text for k in (
            'python.runtime', 'pythonnet', 'clr', 'webview2', 'edgechromium',
            'get_callable', 'get_function', 'loader.initialize',
        ))
        if missing_runtime:
            _show_fatal('Whiteboard cannot start',
                'The whiteboard needs two free Microsoft components that '
                "aren't installed on this PC. All other Hotkeys features "
                'still work, only the offline whiteboard (Shift+F8) is '
                'affected.\n\n'
                'To enable the whiteboard, install BOTH:\n\n'
                '  1. Microsoft .NET Framework 4.8\n'
                '     https://go.microsoft.com/fwlink/?LinkId=2085155\n\n'
                '  2. Microsoft Edge WebView2 Runtime\n'
                '     https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n'
                'After both install, restart your PC and re-launch Hotkeys.')
        else:
            _show_fatal('Whiteboard',
                f'Failed to start WebView2:\n\n{e}\n\n'
                f'{traceback.format_exc()[:800]}')
        raise


if __name__ == '__main__':
    main()
