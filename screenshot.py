"""
Lightshot-style screenshot tool.

Press PrtSc → full-screen overlay:
  • Outside selection: dimmed
  • Inside selection: original brightness (bright cut-out)
  • Dashed white border + resize handles
  • Size badge (WxH) above selection
  • Right toolbar: pen, line, arrow, rect, marker, text, color, undo
  • Bottom bar: Copy / Save / ✕
  • Esc or right-click (no selection) → cancel

PrtSc uses WH_KEYBOARD_LL because RegisterHotKey can't intercept VK_SNAPSHOT.
Singleton guard prevents double-overlays on rapid PrtSc presses.
"""
import ctypes
import ctypes.wintypes
import io
import math
import threading
import tkinter as tk
from tkinter import filedialog, colorchooser

from PIL import Image, ImageDraw, ImageGrab, ImageTk
import win32clipboard
import win32con
import win32gui

from theme import (SURFACE, SURF2, SURF3, BORDER, BORDER2,
                   ACCENT, TEXT_P, TEXT_S, FONT_FAMILY)

# ── Win32 PrtSc listener ─────────────────────────────────────────────────────

_VK_SNAPSHOT   = 0x2C
_WM_KEYDOWN    = 0x0100
_WM_SYSKEYDOWN = 0x0104
_user32        = ctypes.windll.user32
_LRESULT       = ctypes.c_ssize_t

_user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
]
_user32.CallNextHookEx.restype = _LRESULT

# HWND-returning calls in _get_foreground_window_info() need explicit
# restypes so they don't truncate to 32-bit. The PrtSc telemetry path
# passes the result into other Win32 calls — a truncated HWND there
# yields garbage process names and titles for the rare hi-bit window.
_user32.GetForegroundWindow.restype       = ctypes.c_void_p
_user32.GetWindowTextLengthW.argtypes     = (ctypes.c_void_p,)
_user32.GetWindowTextW.argtypes           = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
_user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.DWORD))
ctypes.windll.kernel32.OpenProcess.restype  = ctypes.c_void_p
ctypes.windll.kernel32.OpenProcess.argtypes = (ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)
ctypes.windll.kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

_HOOKPROC = ctypes.WINFUNCTYPE(
    _LRESULT, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
)

class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode',      ctypes.wintypes.DWORD),
                ('scanCode',    ctypes.wintypes.DWORD),
                ('flags',       ctypes.wintypes.DWORD),
                ('time',        ctypes.wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


def start_prtsc_listener(callback) -> None:
    """Install WH_KEYBOARD_LL and fire callback() on every VK_SNAPSHOT press.

    The hook itself enqueues onto a private worker queue and returns
    immediately — same pattern as kbhook.py. The hook body is restricted
    to a vkCode comparison + put_nowait, which keeps it microseconds
    even under disk pressure / AVG inspection. The worker thread handles
    the foreground-window introspection (OpenProcess + GetModuleBaseName,
    can be slow), the logging (file I/O, can stall), and the user
    callback() (can do anything). None of that is allowed in the hook
    context — Windows uninstalls a WH_KEYBOARD_LL hook whose callback
    exceeds the LowLevelHooksTimeout (300 ms by default).
    """
    import queue as _q
    import threading as _th
    _hook_ref = [None]
    _worker_q: _q.Queue = _q.Queue(maxsize=64)

    def _worker():
        import logging
        _log = logging.getLogger(__name__)
        while True:
            sentinel = _worker_q.get()
            if sentinel is None:
                return
            try:
                fg = _get_foreground_window_info()
            except Exception:
                fg = 'foreground=?'
            _log.info(f'[HOOK] PrtSc hook fired  •  {fg}')
            try:
                callback()
            except Exception as e:
                _log.warning(f'PrtSc callback raised: {e}')
            _log.info('[HOOK] PrtSc callback returned')
    _th.Thread(target=_worker, daemon=True,
               name='prtsc-hook-worker').start()

    def _hook_proc(nCode, wParam, lParam):
        if nCode >= 0 and wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
            try:
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == _VK_SNAPSHOT:
                    try:
                        _worker_q.put_nowait(1)
                    except _q.Full:
                        # Worker is backed up — drop this event rather
                        # than block the hook. Single missed PrtSc is
                        # vastly preferable to a dead hook.
                        pass
            except Exception:
                pass
        return _user32.CallNextHookEx(_hook_ref[0], nCode, wParam, lParam)

    def _listen():
        import logging
        proc = _HOOKPROC(_hook_proc)
        _hook_ref[0] = _user32.SetWindowsHookExW(13, proc, None, 0)
        if not _hook_ref[0]:
            logging.getLogger(__name__).warning(
                'SetWindowsHookEx for Print Screen failed, PrtSc unavailable')
            return
        msg = ctypes.wintypes.MSG()
        # GetMessageW blocks until a message arrives, the OS wakes this thread
        # whenever a WH_KEYBOARD_LL callback needs to fire, so there is no
        # polling delay.  The old PeekMessageW + sleep(0.01) loop worked but
        # added up to 10 ms latency before the hook callback ran, which could
        # compound with the keyboard library's own LL hook under load.
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=_listen, daemon=True, name='prtsc-listener').start()


# ── Independent PrtSc keylogger (diagnostic) ─────────────────────────────────
#
# This polls GetAsyncKeyState(VK_SNAPSHOT) directly on a background thread,
# bypassing the WH_KEYBOARD_LL chain entirely. The point: when our hook
# DOESN'T fire on a PrtSc press, we still want to know whether the OS saw
# the keypress. If this poller fires and our hook doesn't, the diagnosis is
# unambiguous — our hook was suppressed (UIPI, foreground app elevated,
# anti-cheat driver, Windows shell hotkey hijack, etc).
#
# Every press also captures the foreground window (process name + title) so
# we can correlate "PrtSc didn't work" with what was in front at the time.

def _get_foreground_window_info() -> str:
    """Return 'pid=X exe=name.exe title="…"' for whatever window has
    focus right now. Used to annotate every PrtSc telemetry line."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return 'foreground=none'
        # Window title
        try:
            length = _user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        except Exception:
            title = '?'
        # Owning PID
        pid = ctypes.wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # Process name
        exe = '?'
        try:
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                     False, pid.value)
            if h:
                try:
                    psapi = ctypes.windll.psapi
                    buf2 = ctypes.create_unicode_buffer(1024)
                    n = psapi.GetModuleBaseNameW(h, None, buf2, 1024)
                    if n:
                        exe = buf2.value
                except Exception:
                    pass
                kernel32.CloseHandle(h)
        except Exception:
            pass
        return f'pid={pid.value} exe={exe} title="{title[:60]}"'
    except Exception as e:
        return f'foreground=unavailable ({e})'


def start_prtsc_keylogger() -> None:
    """Independent diagnostic. Polls VK_SNAPSHOT directly at 25 ms; logs
    every detected press alongside the foreground window. Runs forever
    as a daemon thread. Cheap: GetAsyncKeyState is a single syscall that
    returns instantly, so the poll thread eats negligible CPU.
    """
    import logging
    log = logging.getLogger(__name__)

    def _loop():
        # GetAsyncKeyState returns a SHORT whose bits we care about:
        #   • 0x8000 — key is CURRENTLY pressed
        #   • 0x0001 — key was pressed AT LEAST ONCE since last call (auto
        #     cleared by Windows when read). This is the safety net for
        #     fast taps shorter than our poll interval — if the user
        #     presses+releases PrtSc between two snapshots, the high bit
        #     would never show "down" but 0x0001 will still be set.
        # We poll at 8 ms so even a 1-frame tap can't slip through.
        last_down = False
        kernel32 = ctypes.windll.kernel32
        while True:
            try:
                state = _user32.GetAsyncKeyState(_VK_SNAPSHOT)
                down  = bool(state & 0x8000)
                tapped = bool(state & 0x0001)
                if (down and not last_down) or tapped:
                    fg = _get_foreground_window_info()
                    flags = ('DOWN' if down else '') + \
                            ('+TAPPED' if tapped else '')
                    log.info(f'[KEYLOGGER] PrtSc {flags or "?"} '
                             f'(raw=0x{state & 0xFFFF:04x})  •  {fg}')
                last_down = down
            except Exception as e:
                log.warning(f'PrtSc keylogger poll failed: {e}')
            kernel32.Sleep(8)

    threading.Thread(target=_loop, daemon=True,
                     name='prtsc-keylogger').start()
    logging.getLogger(__name__).info(
        'PrtSc keylogger started (25 ms poll via GetAsyncKeyState).')


# ── Singleton guard ───────────────────────────────────────────────────────────
_overlay_lock    = threading.Lock()
_overlay_active  = [False]
_pending_overlay: list = [None]   # active ScreenshotOverlay instance (for cancellation)
# Time the singleton was claimed. The watchdog uses this to avoid
# resetting a flag that was JUST set: a legitimate grab + overlay
# construct takes roughly 100-200 ms on a 1080p single monitor and up
# to ~1 s on a busy 4K multi-mon. Anything shorter than the grace
# window is presumed to be a grab still in flight and must not be
# reset, otherwise the next PrtSc starts a duplicate overlay.
_overlay_claim_ts: list = [0.0]
_OVERLAY_GRACE_SECS = 3.0


# ── Clipboard helper ──────────────────────────────────────────────────────────

def _put_image_on_clipboard(img: Image.Image) -> None:
    output = io.BytesIO()
    img.convert('RGB').save(output, 'BMP')
    data = output.getvalue()[14:]   # strip BMP file header → DIB
    output.close()
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()


# ── Visual constants (tuned to match Lightshot) ───────────────────────────────

_DIM_FACTOR   = 0.55          # brightness of dimmed area outside selection
_BORDER_CLR   = '#ffffff'     # selection border / handle color
_BORDER_W     = 1
_BORDER_DASH  = (5, 3)        # dash pattern for border
_HANDLE_R     = 5             # handle square half-size (px)

_BADGE_BG     = '#1a1a1a'
_BADGE_FG     = '#ffffff'

# Right toolbar, app dark theme, Lightshot-identical shape/dimensions
_TB_BG        = SURF2        # '#1e1e1e'  dark panel
_TB_BORDER    = BORDER2      # '#383838'  border ring
_TB_FG        = TEXT_P       # '#f0f0f0'  white icons on dark bg
_TB_ACTIVE_BG = ACCENT       # '#7c3aed'  purple highlight for selected tool
_TB_HOVER_BG  = SURF3        # '#282828'  subtle hover tint
_TB_W         = 40           # inner panel ~40px (Lightshot = 48px total incl. border)
_TB_BTN_H     = 28           # ~28px per button (Lightshot ≈ 25px; slight increase for usability)
_TB_RADIUS    = 9
_TB_GAP       = 3

# Bottom action bar, same dark theme
_AB_BG        = SURF2        # '#1e1e1e'
_AB_FG        = TEXT_P       # '#f0f0f0'
_AB_HOVER     = SURF3        # '#282828'
_AB_BTN_W     = 36
_AB_H         = 30
_AB_RADIUS    = 7
_AB_GAP       = 3


class ScreenshotOverlay:
    """Full-screen Lightshot-style screenshot overlay with annotation tools."""

    _TOOLS = ('select', 'marker', 'line', 'arrow', 'rect', 'pen', 'text')

    def __init__(self, root=None, on_done=None,
                 on_extract_text=None, on_translate=None,
                 on_translate_google=None,
                 on_translate_offline_ar=None,
                 on_scan=None,
                 _preloaded_shot=None, _preloaded_dim=None):
        # on_extract_text(img: PIL.Image) — fires on context-menu "Extract text".
        # on_translate(img: PIL.Image)    — fires on context-menu "Translate to English".
        # Both run AFTER the overlay closes, so the caller can pop a result window
        # without competing for the topmost flag.
        # take_screenshot() sets _overlay_active BEFORE spawning the grab
        # thread, so we only apply the singleton guard here for the legacy
        # direct-call path (no preloaded images).
        if _preloaded_shot is None:
            with _overlay_lock:
                if _overlay_active[0]:
                    return
                import time as _time
                _overlay_active[0] = True
                _overlay_claim_ts[0] = _time.monotonic()

        self._on_done        = on_done
        self._on_extract_text = on_extract_text
        self._on_translate    = on_translate
        self._on_translate_google = on_translate_google
        self._on_translate_offline_ar = on_translate_offline_ar
        self._on_scan         = on_scan
        self._own_root       = (root is None)  # True if we created our own tk.Tk()
        self._root_ref       = root            # None means we'll create our own
        self._preloaded_shot = _preloaded_shot
        self._preloaded_dim  = _preloaded_dim

        # Selection
        self._sx = self._sy = self._cx = self._cy = 0
        self._dragging  = False
        self._has_sel   = False

        # Annotation state
        # 'select' is the default — it lets the user resize the selection
        # via the 8 handles or move it by dragging the body, without
        # accidentally drawing pen strokes. Switch to pen/marker/etc.
        # from the toolbar when ready to annotate.
        self._tool        = 'select'
        # Resize / move state populated by _on_ldown when the user grabs
        # a handle or the inside of a selection while the select tool is
        # active.
        self._resize_handle = None   # 'tl' | 't' | 'tr' | 'r' | 'br' | 'b' | 'bl' | 'l' | None
        self._move_origin   = None   # (origin_x, origin_y, sx, sy, cx, cy) snapshot at press
        self._tool_colors = {   # per-tool default colours
            # 'select' doesn't draw anything but keeps a placeholder
            # so the colour picker has something to show / switch from
            # when the user switches to a drawing tool.
            'select': '#ff0000',
            'pen':    '#ff0000',
            'line':   '#ff0000',
            'arrow':  '#ff0000',
            'rect':   '#ff0000',
            'marker': '#fff200',
            'text':   '#000000',
        }
        self._color = self._tool_colors.get(self._tool, '#ff0000')
        self._annotations = []   # canvas item ids (for undo)
        self._ann_data    = []   # dicts for PIL rendering
        self._drawing     = False
        self._draw_p0     = (0, 0)
        self._draw_live   = None  # canvas item id being dragged
        self._pen_pts     = []    # accumulated points for pen stroke

        # Canvas item ids
        self._sel_photo   = None
        self._sel_image_canvas_id = None  # id of the sel canvas image
        self._sel_crop    = None  # PIL of the selection region (cache)
        self._dim_photo   = None

        # Bounding boxes for toolbar / action-bar (set during _redraw)
        # Used to suppress canvas-level click/cursor when mouse is over them
        self._toolbar_bbox    = None   # (x1,y1,x2,y2)
        self._actionbar_bbox  = None   # (x1,y1,x2,y2)

        # Tooltip state
        self._tooltip_win = None
        self._tooltip_job = None

        try:
            self._build()
        except Exception:
            with _overlay_lock:
                _overlay_active[0] = False

    # ─────────────────────────────────────────────────────────────────────────
    # Build
    # ─────────────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        if self._preloaded_shot is not None:
            # Fast path: take_screenshot() already grabbed and dimmed the screen
            # in a background thread so the main thread was never blocked.
            self._shot = self._preloaded_shot
            dim_img    = self._preloaded_dim
        else:
            # Legacy / fallback path: grab and dim synchronously on this thread.
            self._shot = ImageGrab.grab(all_screens=True)
            # ImageEnhance.Brightness uses PIL's C-level implementation and is
            # ~10× faster than image.point(lambda p: ...) for large screenshots.
            from PIL import ImageEnhance
            dim_img = ImageEnhance.Brightness(self._shot).enhance(_DIM_FACTOR)

        if self._root_ref is None:
            # Fallback: create our own Tk root (legacy threaded path).
            # In modern use this branch should never run — the main app
            # always passes its root via _root_ref. If we see this in
            # production, the second tk.Tk() would create a second Tcl
            # interpreter in one process (undefined behavior: grab and
            # event-loop conflicts). Log loudly so it's visible.
            import logging as _lg
            _lg.getLogger(__name__).warning(
                'screenshot: legacy tk.Tk() fallback engaged — '
                '_root_ref was None. Caller should pass an existing root.'
            )
            self._root = tk.Tk()
            self._root.withdraw()
        else:
            # Main-thread path: reuse the caller's root
            self._root = self._root_ref

        # Virtual desktop bounds
        try:
            vx = _user32.GetSystemMetrics(76)
            vy = _user32.GetSystemMetrics(77)
            vw = _user32.GetSystemMetrics(78)
            vh = _user32.GetSystemMetrics(79)
        except Exception:
            vx = vy = 0
            vw = self._root.winfo_screenwidth()
            vh = self._root.winfo_screenheight()

        self._vx, self._vy, self._vw, self._vh = vx, vy, vw, vh

        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.geometry(f'{vw}x{vh}+{vx}+{vy}')
        win.configure(bg='black')
        self._win = win

        canvas = tk.Canvas(win, bg='black', highlightthickness=0,
                           width=vw, height=vh, cursor='crosshair')
        canvas.pack(fill='both', expand=True)
        self._canvas = canvas

        # Layer 0: static dimmed background (never redrawn)
        self._dim_photo = ImageTk.PhotoImage(dim_img, master=canvas)
        canvas.create_image(0, 0, anchor='nw', image=self._dim_photo, tags=('bg',))

        # Bindings
        canvas.bind('<ButtonPress-1>',   self._on_ldown)
        canvas.bind('<B1-Motion>',       self._on_ldrag)
        canvas.bind('<ButtonRelease-1>', self._on_lup)
        canvas.bind('<ButtonPress-3>',   self._on_rclick)
        canvas.bind('<Motion>',          self._on_motion)
        # Escape bound on both win and canvas, canvas steals keyboard focus
        # once the mouse moves over it, so win-only binding misses most presses.
        win.bind(   '<Escape>',          lambda e: self._cancel())
        canvas.bind('<Escape>',          lambda e: self._cancel())
        win.bind('<Control-a>',          lambda e: self._select_all())
        win.bind('<Control-c>',          lambda e: self._copy())
        win.bind('<Control-s>',          lambda e: self._save())
        win.bind('<Control-z>',          lambda e: self._undo())

        # Force the overlay onto the screen and to the top of z-order
        # BEFORE asking for focus. Tray menus / app context menus auto-
        # dismiss the moment focus leaves them — without the explicit
        # deiconify + update_idletasks pair, our overlay can get caught
        # in a "constructed but never mapped" race with the dismissing
        # menu, and Tk reports it as fine while the user sees nothing.
        # Same priming pattern as overlay.py / explain_pill.py.
        try:
            win.deiconify()
            win.update_idletasks()
            win.lift()
        except Exception:
            pass
        win.focus_force()   # grab focus immediately so Escape works from the first frame
        self._root.after(50, lambda: win.focus_force())

        # Steal the event grab from any modal dialog (e.g. RecorderSetupDialog)
        # so that mouse ButtonPress/Motion/Release events reach the canvas.
        # Without this, a grab held by another window (grab_set) swallows all
        # mouse events even though the cursor correctly shows as a crosshair.
        self._prev_grab = None
        try:
            self._prev_grab = self._root.grab_current()
        except Exception:
            pass
        try:
            win.grab_set()
        except Exception:
            pass

        if self._own_root:
            # We own this root, run our own mainloop then clean up.
            # Mainloop exits via _close() → root.quit().
            # Safe to destroy here, not from inside the event handler, which
            # would corrupt tkinter's _default_root global and kill the main
            # CTk app. Destroying here lets Tk clean up its own WH_KEYBOARD_LL
            # hooks properly, preventing them going orphaned and disrupting the
            # keyboard library's hook chain (which breaks Alt+Shift+E, F-keys).
            self._root.mainloop()
            try:
                self._root.destroy()
            except Exception:
                pass
        # else: main app's event loop handles everything; just return

    # ─────────────────────────────────────────────────────────────────────────
    # Selection coords
    # ─────────────────────────────────────────────────────────────────────────

    def _sel(self):
        """Return (x1,y1,x2,y2) in canvas coords, or None."""
        x1, y1 = min(self._sx, self._cx), min(self._sy, self._cy)
        x2, y2 = max(self._sx, self._cx), max(self._sy, self._cy)
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        return x1, y1, x2, y2

    def capture_for_ask(self) -> 'Image.Image | None':
        """Return the selected region (with annotations) as a PIL Image, or None.

        Safe to call from a background thread, only reads PIL data, no Tkinter.
        Intended for Shift+F4 → OCR → answer-pill while overlay is still open.
        """
        return self._render()

    def _inside_sel(self, x, y) -> bool:
        s = self._sel()
        if not s:
            return False
        x1, y1, x2, y2 = s
        return x1 <= x <= x2 and y1 <= y <= y2

    # ─────────────────────────────────────────────────────────────────────────
    # Redraw selection visuals
    # ─────────────────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        c = self._canvas
        c.delete('sel', 'handle', 'badge', 'toolbar', 'actionbar')
        self._toolbar_bbox   = None
        self._actionbar_bbox = None
        self._hide_tooltip()

        s = self._sel()
        if not s:
            self._has_sel = False
            return
        self._has_sel = True
        x1, y1, x2, y2 = s

        # ── Bright crop (undimmed area inside selection) ──────────────────
        # We cache the canvas image id so the marker tool can swap in a
        # multiply-blended preview on the fly. That keeps the live
        # drawing visually identical to the saved render — same PIL
        # multiply driving both, instead of Tk-canvas-line for live and
        # PIL-multiply for save (which never matched).
        crop = self._shot.crop((x1, y1, x2, y2))
        self._sel_crop = crop  # base for multiply previews
        self._sel_photo = ImageTk.PhotoImage(crop, master=c)
        self._sel_image_canvas_id = c.create_image(
            x1, y1, anchor='nw', image=self._sel_photo, tags=('sel',))

        # ── Dashed white border ───────────────────────────────────────────
        c.create_rectangle(x1, y1, x2, y2,
                           outline=_BORDER_CLR, width=_BORDER_W,
                           dash=_BORDER_DASH, fill='', tags=('sel',))

        # ── 8 resize handles ──────────────────────────────────────────────
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        for hx, hy in [(x1, y1), (mx, y1), (x2, y1),
                       (x2, my), (x2, y2), (mx, y2),
                       (x1, y2), (x1, my)]:
            c.create_rectangle(hx - _HANDLE_R, hy - _HANDLE_R,
                               hx + _HANDLE_R, hy + _HANDLE_R,
                               fill=_BORDER_CLR, outline='#808080',
                               width=1, tags=('handle',))

        # ── Size badge ────────────────────────────────────────────────────
        txt  = f'{x2 - x1}x{y2 - y1}'
        bx   = x1
        by   = max(0, y1 - 22)
        # Approximate character width for Segoe UI 9 ≈ 7px/char + padding
        tw   = len(txt) * 7 + 14
        th   = 18
        c.create_rectangle(bx, by, bx + tw, by + th,
                           fill=_BADGE_BG, outline='', tags=('badge',))
        c.create_text(bx + 7, by + th // 2,
                      text=txt, fill=_BADGE_FG,
                      font=('Segoe UI', 9), anchor='w', tags=('badge',))

        # ── Right annotation toolbar ──────────────────────────────────────
        self._draw_toolbar(x1, y1, x2, y2)

        # ── Bottom action bar ─────────────────────────────────────────────
        self._draw_actionbar(x1, y1, x2, y2)

        # Keep annotation items above the bright-crop but below toolbars
        c.tag_raise('annotation')
        c.tag_raise('handle')
        c.tag_raise('badge')
        c.tag_raise('toolbar')
        c.tag_raise('actionbar')

    # ─────────────────────────────────────────────────────────────────────────
    # Tooltip
    # ─────────────────────────────────────────────────────────────────────────

    def _schedule_tooltip(self, text: str, sx: int, sy: int,
                          anchor: str = 'right') -> None:
        """Show tooltip after a short delay.
        anchor='right'  → tooltip right edge at sx (use for right-side toolbar)
        anchor='center' → tooltip centred at sx (use for bottom action bar)
        anchor='left'   → tooltip left edge at sx (use for left-side toolbar)
        """
        self._cancel_tooltip()
        self._tooltip_job = self._root.after(
            420, lambda: self._show_tooltip(text, sx, sy, anchor))

    def _cancel_tooltip(self) -> None:
        if self._tooltip_job:
            try:
                self._root.after_cancel(self._tooltip_job)
            except Exception:
                pass
            self._tooltip_job = None

    def _hide_tooltip(self) -> None:
        self._cancel_tooltip()
        if self._tooltip_win:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None

    def _show_tooltip(self, text: str, sx: int, sy: int,
                      anchor: str = 'right') -> None:
        """Create the tooltip Toplevel, positioned by anchor."""
        self._hide_tooltip()
        if not self._win:
            return
        tip = tk.Toplevel(self._root)
        tip.overrideredirect(True)
        tip.attributes('-topmost', True)
        tip.configure(bg=BORDER2)
        lbl = tk.Label(tip, text=text,
                       bg=SURFACE, fg=TEXT_P,
                       font=(FONT_FAMILY, 9),
                       padx=8, pady=5)
        lbl.pack(padx=1, pady=1)
        tip.update_idletasks()
        tw = tip.winfo_reqwidth()
        th = tip.winfo_reqheight()

        if anchor == 'right':
            # sx is right edge of tooltip → left edge = sx - tw
            fx = sx - tw
        elif anchor == 'center':
            # sx is horizontal centre
            fx = sx - tw // 2
        else:  # 'left'
            fx = sx

        # fy: vertically centred on sy
        fy = sy - th // 2

        # Clamp to virtual desktop
        fx = max(self._vx, min(fx, self._vx + self._vw - tw - 4))
        fy = max(self._vy, min(fy, self._vy + self._vh - th - 4))
        tip.geometry(f'+{fx}+{fy}')
        self._tooltip_win = tip

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _rounded_rect(canvas, x1: int, y1: int, x2: int, y2: int,
                      r: int, fill: str, outline: str,
                      width: int = 1, tags: tuple = ()) -> int:
        """Draw a rounded rectangle on canvas using a smooth polygon."""
        pts = [
            x1+r, y1,   x2-r, y1,
            x2,   y1,   x2,   y1+r,
            x2,   y2-r, x2,   y2,
            x2-r, y2,   x1+r, y2,
            x1,   y2,   x1,   y2-r,
            x1,   y1+r, x1,   y1,
        ]
        return canvas.create_polygon(pts, smooth=True,
                                     fill=fill, outline=outline,
                                     width=width, tags=tags)

    @staticmethod
    def _draw_tb_icon(c, tid: str, cx: int, cy: int,
                      fg: str, bg: str, color: str) -> list:
        """Draw a toolbar icon at (cx,cy). Returns list of canvas item ids.
        Shapes reverse-engineered from Lightshot pixel-map analysis."""
        t = ('toolbar',)
        ids = []

        if tid == 'select':
            # Standard arrow cursor icon — diagonal pointer with a notch.
            ids += [c.create_polygon(
                cx-6, cy-6,
                cx-6, cy+6,
                cx-2, cy+2,
                cx+1, cy+6,
                cx+3, cy+5,
                cx, cy+1,
                cx+5, cy+1,
                fill=fg, outline='', tags=t)]
            # Outline so the icon stays visible on the purple active bg.
            ids += [c.create_line(
                cx-6, cy-6, cx+5, cy+1,
                fill=bg if bg != fg else '#ffffff', width=1, tags=t)]

        elif tid == 'pen':
            # Pencil: diagonal body SW→NE, sharp tip at NE, flat eraser at SW
            # Main diagonal stroke
            ids += [c.create_line(cx-5, cy+5, cx+3, cy-3,
                                  fill=fg, width=2, capstyle='butt', tags=t)]
            # Sharp tip (small filled polygon at NE)
            ids += [c.create_polygon(cx+2, cy-3, cx+6, cy-7, cx+6, cy-2,
                                     fill=fg, outline='', tags=t)]
            # Flat eraser body (filled rect at SW)
            ids += [c.create_rectangle(cx-8, cy+4, cx-5, cy+8,
                                       fill=fg, outline='', tags=t)]
            # Horizontal cap line between pencil body and eraser
            ids += [c.create_line(cx-8, cy+4, cx-5, cy+4,
                                  fill=fg, width=1, tags=t)]

        elif tid == 'line':
            # Simple clean diagonal, no embellishments
            ids += [c.create_line(cx-7, cy+7, cx+7, cy-7,
                                  fill=fg, width=2, capstyle='round', tags=t)]

        elif tid == 'arrow':
            # Diagonal body + filled arrowhead block at NE end (not tkinter arrow=last)
            # Body (stops short of arrowhead)
            ids += [c.create_line(cx-7, cy+6, cx+2, cy-3,
                                  fill=fg, width=2, capstyle='butt', tags=t)]
            # Filled arrowhead triangle pointing NE
            ids += [c.create_polygon(cx+0, cy-7, cx+7, cy-7, cx+7, cy+0,
                                     fill=fg, outline='', tags=t)]

        elif tid == 'rect':
            # Hollow rectangle with 2px border, matching Lightshot proportions
            ids += [c.create_rectangle(cx-8, cy-7, cx+8, cy+7,
                                       outline=fg, width=2, fill='', tags=t)]

        elif tid == 'marker':
            # Thick diagonal stroke (~5px) like pen direction
            ids += [c.create_line(cx-5, cy+5, cx+5, cy-5,
                                  fill=fg, width=5, capstyle='butt', tags=t)]
            # Horizontal underline bar, KEY distinguishing feature vs pen/line
            ids += [c.create_line(cx-7, cy+8, cx+7, cy+8,
                                  fill=fg, width=2, capstyle='butt', tags=t)]

        elif tid == 'text':
            # Serif 'T': crossbar with end ticks, vertical stem, base serif
            # Crossbar (horizontal, wide)
            ids += [c.create_line(cx-7, cy-5, cx+7, cy-5,
                                  fill=fg, width=2, tags=t)]
            # Crossbar end ticks (vertical marks at each end)
            ids += [c.create_line(cx-7, cy-7, cx-7, cy-3,
                                  fill=fg, width=1, tags=t)]
            ids += [c.create_line(cx+7, cy-7, cx+7, cy-3,
                                  fill=fg, width=1, tags=t)]
            # Vertical stem
            ids += [c.create_line(cx, cy-4, cx, cy+7,
                                  fill=fg, width=2, tags=t)]
            # Base serif
            ids += [c.create_line(cx-3, cy+7, cx+3, cy+7,
                                  fill=fg, width=2, tags=t)]

        elif tid == 'color':
            # Filled color swatch (current annotation color)
            ids += [c.create_rectangle(cx-8, cy-8, cx+8, cy+8,
                                       fill=color, outline=fg, width=1, tags=t)]
            # Small notch at lower-right corner (Lightshot's characteristic detail)
            ids += [c.create_rectangle(cx+4, cy+4, cx+8, cy+8,
                                       fill=bg, outline='', tags=t)]

        elif tid == 'undo':
            # CCW curved arc opening at lower-right, straight arm + arrow tip
            ids += [c.create_arc(cx-7, cy-6, cx+7, cy+4,
                                 start=30, extent=230,
                                 outline=fg, width=2, style='arc', tags=t)]
            # Straight arm from right end of arc going down
            ids += [c.create_line(cx+6, cy-1, cx+6, cy+5,
                                  fill=fg, width=2, tags=t)]
            # Small arrowhead hook at bottom of arm (points left)
            ids += [c.create_line(cx+3, cy+3, cx+6, cy+5,
                                  fill=fg, width=2, tags=t)]

        return ids

    def _draw_toolbar(self, x1, y1, x2, y2) -> None:
        c = self._canvas
        tools = [
            ('select', 'Select / Move / Resize selection'),
            ('marker', 'Marker (highlight)'),
            ('line',   'Line'),
            ('arrow',  'Arrow'),
            ('rect',   'Rectangle'),
            ('pen',    'Pen (freehand drawing)'),
            ('text',   'Text'),
            ('color',  'Annotation colour'),
            ('undo',   'Undo last annotation'),
        ]
        n       = len(tools)
        total_h = n * _TB_BTN_H + 4

        # Position to the right; flip left if near screen edge
        tx = x2 + _TB_GAP
        flipped = False
        if tx + _TB_W > self._vw - 4:
            tx = x1 - _TB_W - _TB_GAP
            flipped = True
        ty = max(0, min(y2 - total_h, self._vh - total_h - 4))

        # Store bounding box so click/cursor handlers can check it
        self._toolbar_bbox = (tx, ty, tx + _TB_W, ty + total_h)

        # Panel background, rounded corners like Lightshot
        self._rounded_rect(c, tx, ty, tx + _TB_W, ty + total_h,
                           _TB_RADIUS, fill=_TB_BG, outline=_TB_BORDER,
                           width=1, tags=('toolbar',))

        for i, (tid, tip_text) in enumerate(tools):
            bx1 = tx + 2
            by1 = ty + 2 + i * _TB_BTN_H
            bx2 = tx + _TB_W - 2
            by2 = by1 + _TB_BTN_H - 2
            icx = (bx1 + bx2) // 2
            icy = (by1 + by2) // 2

            active = (tid == self._tool and tid not in ('color', 'undo'))
            bg = _TB_ACTIVE_BG if active else _TB_BG

            bg_id = c.create_rectangle(bx1, by1, bx2, by2,
                                        fill=bg, outline='',
                                        tags=('toolbar',))

            # Subtle horizontal separator between buttons (skip first)
            if i > 0:
                c.create_line(tx + 5, by1, tx + _TB_W - 5, by1,
                              fill=BORDER, tags=('toolbar',))

            # Bind icon items too, fill='' hit-rects are inert to canvas events,
            # so clicks on icon shapes (lines, polygons) would be swallowed without
            # a binding on the icon ids themselves.
            icon_ids = self._draw_tb_icon(c, tid, icx, icy, _TB_FG, bg, self._color)

            # Tooltip: appears to the LEFT of toolbar (right edge flush to toolbar)
            # If toolbar was flipped to left side, tooltip appears to the RIGHT
            if not flipped:
                tip_sx     = tx - 6        # right edge of tooltip
                tip_anchor = 'right'
            else:
                tip_sx     = tx + _TB_W + 6   # left edge of tooltip
                tip_anchor = 'left'
            tip_sy = icy   # vertically centred on button

            for item in [bg_id] + icon_ids:
                c.tag_bind(item, '<ButtonPress-1>',
                           lambda e, t=tid: self._tool_click(t))
                c.tag_bind(item, '<Enter>',
                           lambda e, b=bg_id, a=active,
                                  ts=tip_text, sx=tip_sx, sy=tip_sy, an=tip_anchor: (
                               c.itemconfig(b, fill=_TB_ACTIVE_BG if a else _TB_HOVER_BG),
                               self._schedule_tooltip(ts, sx, sy, an)))
                c.tag_bind(item, '<Leave>',
                           lambda e, b=bg_id, a=active: (
                               c.itemconfig(b, fill=_TB_ACTIVE_BG if a else _TB_BG),
                               self._hide_tooltip()))

    @staticmethod
    def _draw_ab_icon(c, name: str, cx: int, cy: int, fg: str, bg: str) -> list:
        """Draw a compact action-bar icon centred at (cx, cy) using canvas primitives.
        Returns list of canvas item ids so callers can bind events to them."""
        t = ('actionbar',)
        ids = []

        if name == 'print':
            # Printer: body rectangle + paper tray slot on top + output slot in body
            ids += [c.create_rectangle(cx-8, cy-1, cx+8, cy+7,
                               outline=fg, width=1, fill=bg, tags=t)]
            # Paper feed tray (narrower, above printer)
            ids += [c.create_rectangle(cx-5, cy-7, cx+5, cy-1,
                               outline=fg, width=1, fill=bg, tags=t)]
            # Output slot / paper indicator (filled dark rect in body)
            ids += [c.create_rectangle(cx-4, cy+2, cx+4, cy+5,
                               outline='', fill=fg, tags=t)]

        elif name == 'copy':
            # Two overlapping pages (Lightshot copy icon)
            # Back page (offset up-right)
            ids += [c.create_rectangle(cx-3, cy-5, cx+8, cy+7,
                               outline=fg, width=1, fill=bg, tags=t)]
            # Front page (offset down-left)
            ids += [c.create_rectangle(cx-8, cy-8, cx+3, cy+4,
                               outline=fg, width=1, fill=bg, tags=t)]
            # Dog-ear fold at top-right of front page
            ids += [c.create_line(cx+0, cy-8, cx+3, cy-5, fill=fg, width=1, tags=t)]
            ids += [c.create_line(cx+0, cy-8, cx+0, cy-5, fill=fg, width=1, tags=t)]
            ids += [c.create_line(cx+3, cy-5, cx+0, cy-5, fill=fg, width=1, tags=t)]

        elif name == 'save':
            # Floppy disk: outer body, label stripe, write-protect notch, hub slot
            ids += [c.create_rectangle(cx-8, cy-8, cx+8, cy+8,
                               outline=fg, width=1, fill=bg, tags=t)]
            # Label (filled stripe across top ~60%)
            ids += [c.create_rectangle(cx-7, cy-7, cx+7, cy-1,
                               outline='', fill=fg, tags=t)]
            # Write-protect notch (gap at top-right of label)
            ids += [c.create_rectangle(cx+3, cy-7, cx+7, cy-1,
                               outline='', fill=bg, tags=t)]
            # Hub slot (small rectangle at bottom-center)
            ids += [c.create_rectangle(cx-3, cy+1, cx+3, cy+7,
                               outline=fg, width=1, fill='', tags=t)]

        elif name == 'close':
            m = 6
            ids += [c.create_line(cx-m, cy-m, cx+m, cy+m,
                          fill=fg, width=2, capstyle='round', tags=t)]
            ids += [c.create_line(cx+m, cy-m, cx-m, cy+m,
                          fill=fg, width=2, capstyle='round', tags=t)]

        return ids

    def _draw_actionbar(self, x1, y1, x2, y2) -> None:
        c = self._canvas
        buttons = [
            ('copy',  'Copy to clipboard', self._copy),
            ('save',  'Save as file',      self._save),
            ('close', 'Cancel',            self._cancel),
        ]
        n       = len(buttons)
        total_w = n * _AB_BTN_W + 2   # +2 for outer border

        # Right-align to selection right edge, like Lightshot
        bx = max(x1, x2 - total_w)
        if bx + total_w > self._vw - 4:
            bx = self._vw - total_w - 4
        by = min(y2 + _AB_GAP, self._vh - _AB_H - 4)

        # Store bounding box so click/cursor handlers can check it
        self._actionbar_bbox = (bx, by, bx + total_w, by + _AB_H)

        # Panel background, rounded corners like Lightshot
        self._rounded_rect(c, bx, by, bx + total_w, by + _AB_H,
                           _AB_RADIUS, fill=_AB_BG, outline=_TB_BORDER,
                           width=1, tags=('actionbar',))

        for i, (name, tip_text, cmd) in enumerate(buttons):
            ax1 = bx + 1 + i * _AB_BTN_W
            ax2 = ax1 + _AB_BTN_W
            ay1, ay2 = by + 1, by + _AB_H - 1
            cx  = (ax1 + ax2) // 2
            cy  = (ay1 + ay2) // 2

            btn = c.create_rectangle(ax1, ay1, ax2, ay2,
                                     fill=_AB_BG, outline='',
                                     tags=('actionbar',))
            # Vertical separator between buttons
            if i > 0:
                c.create_line(ax1, by + 5, ax1, by + _AB_H - 5,
                              fill=_TB_BORDER, tags=('actionbar',))

            # Draw icon; bind its item ids too so clicks on the icon shapes fire
            ab_icon_ids = self._draw_ab_icon(c, name, cx, cy, _AB_FG, _AB_BG)

            # Tooltip centred above the button, just above the bar
            tip_sx = cx            # horizontal centre of button
            tip_sy = by - 10       # just above the action bar

            for item in [btn] + ab_icon_ids:
                c.tag_bind(item, '<ButtonPress-1>', lambda e, f=cmd: f())
                c.tag_bind(item, '<Enter>',
                           lambda e, b=btn, ts=tip_text, sx=tip_sx, sy=tip_sy: (
                               c.itemconfig(b, fill=_AB_HOVER),
                               self._schedule_tooltip(ts, sx, sy, 'center')))
                c.tag_bind(item, '<Leave>',
                           lambda e, b=btn: (
                               c.itemconfig(b, fill=_AB_BG),
                               self._hide_tooltip()))

    # ─────────────────────────────────────────────────────────────────────────
    # Input handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _in_ui(self, x, y) -> bool:
        """True if (x,y) is inside the toolbar or action bar."""
        for bbox in (self._toolbar_bbox, self._actionbar_bbox):
            if bbox and bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
                return True
        return False

    # Handle-position helper. Returns ('tl', 't', 'tr', 'r', 'br', 'b',
    # 'bl', 'l') or None. Each handle is a fixed dot at a corner or
    # midpoint — we test point-distance to each, NOT a band along the
    # entire edge (which would falsely catch any click near the border).
    def _hit_handle(self, x: int, y: int):
        if not self._has_sel:
            return None
        x1, y1 = min(self._sx, self._cx), min(self._sy, self._cy)
        x2, y2 = max(self._sx, self._cx), max(self._sy, self._cy)
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        r = _HANDLE_R + 5   # generous slop for easier grabbing
        # Corners
        if abs(x - x1) <= r and abs(y - y1) <= r: return 'tl'
        if abs(x - x2) <= r and abs(y - y1) <= r: return 'tr'
        if abs(x - x1) <= r and abs(y - y2) <= r: return 'bl'
        if abs(x - x2) <= r and abs(y - y2) <= r: return 'br'
        # Edge midpoints — each is a single point, NOT the whole edge.
        if abs(x - mx) <= r and abs(y - y1) <= r: return 't'
        if abs(x - mx) <= r and abs(y - y2) <= r: return 'b'
        if abs(y - my) <= r and abs(x - x1) <= r: return 'l'
        if abs(y - my) <= r and abs(x - x2) <= r: return 'r'
        return None

    # Resize cursors per handle.
    _RESIZE_CURSORS = {
        'tl': 'size_nw_se', 'br': 'size_nw_se',
        'tr': 'size_ne_sw', 'bl': 'size_ne_sw',
        't':  'size_ns',    'b':  'size_ns',
        'l':  'size_we',    'r':  'size_we',
    }

    def _on_ldown(self, event) -> None:
        # Ignore clicks that land on toolbar / action-bar panels
        if self._in_ui(event.x, event.y):
            return
        # First: handle resize via the 8 handles (any tool — so the user
        # can resize without going back to the select tool).
        if self._has_sel:
            h = self._hit_handle(event.x, event.y)
            if h is not None:
                self._resize_handle = h
                # Normalise sx/sy/cx/cy to TL/BR for predictable edge
                # adjustment during drag.
                x1, y1 = min(self._sx, self._cx), min(self._sy, self._cy)
                x2, y2 = max(self._sx, self._cx), max(self._sy, self._cy)
                self._sx, self._sy, self._cx, self._cy = x1, y1, x2, y2
                return
        # Inside the selection: behaviour depends on active tool.
        if self._has_sel and self._inside_sel(event.x, event.y):
            if self._tool == 'select':
                # Move the whole selection (don't draw annotations).
                self._move_origin = (
                    event.x, event.y,
                    self._sx, self._sy, self._cx, self._cy)
                return
            # Drawing tools: produce an annotation.
            self._drawing  = True
            self._draw_p0  = (event.x, event.y)
            self._pen_pts  = [(event.x, event.y)]
            self._draw_live = None
            return
        # Otherwise start (or restart) selection
        self._sx = self._cx = event.x
        self._sy = self._cy = event.y
        self._dragging = True
        # Clear old annotations if re-selecting
        self._canvas.delete('annotation')
        self._annotations.clear()
        self._ann_data.clear()
        self._sel_crop = None
        self._sel_image_canvas_id = None
        self._redraw()

    def _on_ldrag(self, event) -> None:
        if self._resize_handle is not None:
            # Resize the appropriate edge / corner of the selection.
            h = self._resize_handle
            if 'l' in h: self._sx = event.x
            if 'r' in h: self._cx = event.x
            if 't' in h: self._sy = event.y
            if 'b' in h: self._cy = event.y
            # _sel_crop must invalidate so the next redraw reads the
            # right pixel region from the underlying screenshot.
            self._sel_crop = None
            self._sel_image_canvas_id = None
            self._redraw()
            return
        if self._move_origin is not None:
            ox, oy, sx0, sy0, cx0, cy0 = self._move_origin
            dx = event.x - ox
            dy = event.y - oy
            # Clamp so the selection stays within the visible canvas.
            new_x1 = min(sx0, cx0) + dx
            new_x2 = max(sx0, cx0) + dx
            new_y1 = min(sy0, cy0) + dy
            new_y2 = max(sy0, cy0) + dy
            if new_x1 < 0: dx -= new_x1
            if new_y1 < 0: dy -= new_y1
            if new_x2 > self._vw: dx -= new_x2 - self._vw
            if new_y2 > self._vh: dy -= new_y2 - self._vh
            self._sx = sx0 + dx
            self._sy = sy0 + dy
            self._cx = cx0 + dx
            self._cy = cy0 + dy
            self._sel_crop = None
            self._sel_image_canvas_id = None
            self._redraw()
            return
        if self._drawing:
            self._live_draw(event.x, event.y)
            return
        if self._dragging:
            self._cx = event.x
            self._cy = event.y
            self._redraw()

    def _on_lup(self, event) -> None:
        if self._resize_handle is not None:
            self._resize_handle = None
            return
        if self._move_origin is not None:
            self._move_origin = None
            return
        if self._drawing:
            self._commit_draw(event.x, event.y)
            self._drawing = False
            return
        if self._dragging:
            self._cx = event.x
            self._cy = event.y
            self._dragging = False
            self._redraw()

    def _on_motion(self, event) -> None:
        if self._in_ui(event.x, event.y):
            self._canvas.config(cursor='arrow')
            return
        # Hover over a resize handle → show the appropriate resize cursor
        if self._has_sel:
            h = self._hit_handle(event.x, event.y)
            if h is not None:
                self._canvas.config(cursor=self._RESIZE_CURSORS.get(h, 'crosshair'))
                return
        if self._has_sel and self._inside_sel(event.x, event.y):
            cursors = {
                'select': 'fleur',   # move cursor (4-arrow)
                'pen': 'pencil',
                'text': 'xterm',
            }
            self._canvas.config(cursor=cursors.get(self._tool, 'crosshair'))
        else:
            self._canvas.config(cursor='crosshair')

    def _on_rclick(self, event) -> None:
        # Right-click on toolbar / action-bar: ignore
        if self._in_ui(event.x, event.y):
            return
        # Always show a context menu, Copy/Save appear only when a selection
        # exists, but the Exit option is ALWAYS present as a failsafe so the
        # overlay can never be "stuck" without an escape route.
        self._show_context_menu(event.x_root, event.y_root)

    def _show_context_menu(self, rx: int, ry: int) -> None:
        has_sel = bool(self._sel())
        menu = tk.Menu(self._win, tearoff=0,
                       bg=SURFACE,
                       fg=TEXT_P,
                       activebackground=ACCENT,
                       activeforeground='#ffffff',
                       disabledforeground=TEXT_S,
                       selectcolor=TEXT_P,
                       font=(FONT_FAMILY, 10),
                       borderwidth=1,
                       relief='flat')
        if has_sel:
            menu.add_command(label='  Copy',   command=self._copy)
            menu.add_command(label='  Save',   command=self._save)
            if self._on_scan is not None:
                menu.add_command(label='  📄  Scan document',
                                 command=self._scan_action)
            if self._on_translate is not None:
                menu.add_command(label='  🌐  Translate to English (AI)',
                                 command=self._translate_action)
            if self._on_translate_google is not None:
                menu.add_command(label='  🔵  Translate to English (Google)',
                                 command=self._translate_google_action)
            if self._on_translate_offline_ar is not None:
                menu.add_command(label='  🟢  Translate to English (Offline Arabic OCR)',
                                 command=self._translate_offline_ar_action)
            menu.add_separator()
        # Exit is always present, this is the last-resort escape if Esc/Del
        # are somehow not responding (e.g. focus was stolen by another app).
        menu.add_command(label='  Exit screenshot', command=self._cancel)
        try:
            menu.tk_popup(rx, ry)
        finally:
            menu.grab_release()

    # ─────────────────────────────────────────────────────────────────────────
    # Tool selection
    # ─────────────────────────────────────────────────────────────────────────

    def _tool_click(self, tool_id: str) -> None:
        if tool_id == 'undo':
            self._undo()
            return
        if tool_id == 'color':
            self._pick_color()
            return
        self._tool  = tool_id
        self._color = self._tool_colors[tool_id]
        self._redraw()

    def _pick_color(self) -> None:
        # Unbind Escape + lower overlay so the native color dialog is reachable
        # and Escape to cancel the dialog doesn't propagate to the overlay
        self._win.unbind('<Escape>')
        self._win.attributes('-topmost', False)
        self._win.lower()

        # The native color dialog briefly flashes at the top-left before Windows
        # positions it. Poll for the window and snap it to screen-center so fast
        # the user never sees the initial position.
        _title = 'Pick annotation colour'
        _vx, _vy, _vw, _vh = self._vx, self._vy, self._vw, self._vh

        def _center_dialog():
            import time
            for _ in range(60):          # poll every 10 ms, up to ~600 ms
                time.sleep(0.01)
                hwnd = win32gui.FindWindow(None, _title)
                if hwnd and win32gui.IsWindowVisible(hwnd):
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    w, h = r - l, b - t
                    x = _vx + (_vw - w) // 2
                    y = _vy + (_vh - h) // 2
                    win32gui.SetWindowPos(
                        hwnd, 0, x, y, 0, 0,
                        win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
                        | win32con.SWP_NOACTIVATE)
                    break

        threading.Thread(target=_center_dialog, daemon=True).start()

        result = colorchooser.askcolor(color=self._color, parent=self._win,
                                       title=_title)
        self._win.attributes('-topmost', True)
        self._win.lift()
        self._win.bind('<Escape>', lambda e: self._cancel())
        if result and result[1]:
            self._color = result[1]
            self._tool_colors[self._tool] = result[1]
        self._redraw()

    # ─────────────────────────────────────────────────────────────────────────
    # Annotation drawing
    # ─────────────────────────────────────────────────────────────────────────

    def _live_draw(self, ex, ey) -> None:
        c   = self._canvas
        sx, sy = self._draw_p0
        clr = self._color

        if self._tool == 'pen':
            self._pen_pts.append((ex, ey))
            if self._draw_live:
                c.delete(self._draw_live)
            if len(self._pen_pts) >= 2:
                self._draw_live = c.create_line(
                    self._pen_pts, fill=clr, width=2,
                    smooth=True, capstyle='round', joinstyle='round',
                    tags=('annotation',))
            return

        if self._tool == 'marker':
            # Live preview goes through the SAME PIL multiply pipeline
            # as the final save — no Tk canvas line involved. We paint
            # the marker layer (white + tinted strokes), multiply it
            # with the sel crop, swap the result into the sel canvas
            # image. Result: dragging the marker over text shows the
            # text crisp under a soft yellow band, identical to the
            # final saved PNG, with no halftone dottedness.
            self._pen_pts.append((ex, ey))
            self._refresh_marker_preview()
            return

        if self._draw_live:
            c.delete(self._draw_live)
            self._draw_live = None

        if self._tool == 'line':
            self._draw_live = c.create_line(sx, sy, ex, ey,
                                            fill=clr, width=2,
                                            capstyle='round', tags=('annotation',))
        elif self._tool == 'arrow':
            self._draw_live = c.create_line(sx, sy, ex, ey,
                                            fill=clr, width=2,
                                            arrow='last', arrowshape=(10, 13, 4),
                                            tags=('annotation',))
        elif self._tool == 'rect':
            self._draw_live = c.create_rectangle(
                min(sx, ex), min(sy, ey), max(sx, ex), max(sy, ey),
                outline=clr, width=2, fill='', tags=('annotation',))

        # Keep toolbars on top
        if self._draw_live:
            c.tag_raise('toolbar')
            c.tag_raise('actionbar')
            c.tag_raise('handle')
            c.tag_raise('badge')

    def _commit_draw(self, ex, ey) -> None:
        sx, sy = self._draw_p0
        clr    = self._color

        if self._tool == 'text':
            self._show_text_popup(sx, sy)
            return

        if self._tool == 'marker':
            # No canvas line for markers — the multiply preview already
            # painted into self._sel_photo. Just record the stroke.
            # _annotations uses a sentinel so undo can route to "remove
            # last marker ann_data + refresh preview" while still
            # popping in stroke order.
            if len(self._pen_pts) >= 2:
                self._annotations.append('marker_sentinel')
                self._ann_data.append({
                    'tool': 'marker', 'color': clr,
                    'points': list(self._pen_pts),
                })
            self._draw_live = None
            self._pen_pts   = []
            return

        if self._tool == 'pen':
            if self._draw_live and len(self._pen_pts) >= 2:
                self._annotations.append(self._draw_live)
                self._ann_data.append({
                    'tool': self._tool, 'color': clr,
                    'points': list(self._pen_pts),
                })
            self._draw_live = None
            self._pen_pts   = []
            return

        if self._draw_live:
            self._annotations.append(self._draw_live)
            self._ann_data.append({
                'tool': self._tool, 'color': clr,
                'coords': (sx, sy, ex, ey),
            })
            self._draw_live = None

    def _show_text_popup(self, x, y) -> None:
        """Float a tiny entry widget at (x,y) for text annotation."""
        popup = tk.Toplevel(self._win)
        popup.overrideredirect(True)
        popup.attributes('-topmost', True)
        px = self._vx + (self._vw - 240) // 2
        py = self._vy + (self._vh - 34) // 2
        popup.geometry(f'240x34+{px}+{py}')
        popup.configure(bg=BORDER2)           # thin border frame
        entry = tk.Entry(popup,
                         bg='#ffffff', fg='#000000',
                         insertbackground='#000000',
                         highlightthickness=0,
                         font=(FONT_FAMILY, 14), relief='flat', bd=4)
        entry.pack(fill='both', expand=True, padx=1, pady=1)
        entry.focus_set()

        def _commit(e=None):
            txt = entry.get().strip()
            popup.destroy()
            if txt:
                item_id = self._canvas.create_text(
                    x, y, text=txt, fill=self._color,
                    font=('Segoe UI', 16, 'bold'), anchor='nw',
                    tags=('annotation',))
                self._annotations.append(item_id)
                self._ann_data.append({
                    'tool': 'text', 'color': self._color,
                    'coords': (x, y, x, y), 'text': txt,
                })
                self._canvas.tag_raise('toolbar')
                self._canvas.tag_raise('actionbar')

        entry.bind('<Return>', _commit)
        entry.bind('<Escape>', lambda e: popup.destroy())

    def _undo(self) -> None:
        if not self._annotations:
            return
        last = self._annotations.pop()
        if self._ann_data:
            self._ann_data.pop()
        if last == 'marker_sentinel':
            # Marker isn't a canvas item — it lives in the multiply
            # preview. Refresh that preview so the popped stroke
            # vanishes from the sel image.
            self._refresh_marker_preview()
        else:
            try:
                self._canvas.delete(last)
            except Exception:
                pass

    def _refresh_marker_preview(self) -> None:
        """Rebuild the sel canvas image from scratch: base × all
        committed marker strokes × current in-progress stroke. One
        PIL multiply pipeline drives BOTH the live drawing experience
        and the final saved render — same yellow, every time.

        Cheap to call on every motion event: PIL multiply is implemented
        in C and runs in ~5ms on a typical 1000x500 selection.
        """
        from PIL import ImageChops
        if self._sel_image_canvas_id is None or self._sel_crop is None:
            return
        s = self._sel()
        if not s:
            return
        x1, y1, x2, y2 = s
        w, h = x2 - x1, y2 - y1

        # Build the marker layer from canonical truth: every marker
        # entry in _ann_data plus the in-flight stroke.
        layer = Image.new('RGB', (w, h), (255, 255, 255))
        ld = ImageDraw.Draw(layer)
        for ann in self._ann_data:
            if ann.get('tool') == 'marker':
                pts = ann.get('points', [])
                if len(pts) >= 2:
                    rpts = [(px - x1, py - y1) for px, py in pts]
                    ld.line(rpts, fill=self._marker_tint_rgb(ann['color']),
                            width=16, joint='curve')
        # In-progress stroke (during drag, before commit)
        if self._tool == 'marker' and len(self._pen_pts) >= 2:
            rpts = [(px - x1, py - y1) for px, py in self._pen_pts]
            ld.line(rpts, fill=self._marker_tint_rgb(self._color),
                    width=16, joint='curve')

        composed = ImageChops.multiply(self._sel_crop, layer)
        self._sel_photo = ImageTk.PhotoImage(composed, master=self._canvas)
        try:
            self._canvas.itemconfigure(self._sel_image_canvas_id,
                                       image=self._sel_photo)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Render final image (screenshot + annotations flattened)
    # ─────────────────────────────────────────────────────────────────────────

    def _render(self) -> 'Image.Image | None':
        s = self._sel()
        if not s:
            return None
        x1, y1, x2, y2 = s
        img  = self._shot.crop((x1, y1, x2, y2)).copy()
        draw = ImageDraw.Draw(img, 'RGBA')

        # Marker strokes are accumulated on a separate near-white layer
        # and multiplied with the base at the end. Multiply keeps black
        # text fully black (black * anything = black) which is what
        # Lightshot does — a real highlighter never colours the ink it
        # crosses over. _MARKER_STRENGTH controls how strongly the
        # background is tinted; the rest stays white so non-marker
        # pixels pass through unchanged.
        marker_layer = None

        for ann in self._ann_data:
            tool  = ann['tool']
            color = ann['color']

            if tool == 'pen':
                pts = [(px - x1, py - y1) for px, py in ann['points']]
                if len(pts) >= 2:
                    draw.line(pts, fill=color, width=2, joint='curve')

            elif tool in ('line', 'arrow'):
                ax1, ay1, ax2, ay2 = ann['coords']
                rx1, ry1 = ax1 - x1, ay1 - y1
                rx2, ry2 = ax2 - x1, ay2 - y1
                draw.line([(rx1, ry1), (rx2, ry2)], fill=color, width=2)
                if tool == 'arrow':
                    dx, dy = rx2 - rx1, ry2 - ry1
                    length = math.hypot(dx, dy)
                    if length > 0:
                        angle = math.atan2(dy, dx)
                        for da in (0.4, -0.4):
                            hx = rx2 - 12 * math.cos(angle + da)
                            hy = ry2 - 12 * math.sin(angle + da)
                            draw.line([(rx2, ry2), (int(hx), int(hy))],
                                      fill=color, width=2)

            elif tool == 'rect':
                ax1, ay1, ax2, ay2 = ann['coords']
                draw.rectangle([ax1 - x1, ay1 - y1, ax2 - x1, ay2 - y1],
                               outline=color, width=2)

            elif tool == 'marker':
                pts = ann.get('points', [])
                if len(pts) >= 2:
                    rpts = [(px - x1, py - y1) for px, py in pts]
                    if marker_layer is None:
                        marker_layer = Image.new('RGB', img.size,
                                                 (255, 255, 255))
                    mdraw = ImageDraw.Draw(marker_layer)
                    mdraw.line(rpts, fill=self._marker_tint_rgb(color),
                               width=16, joint='curve')

            elif tool == 'text':
                ax1, ay1 = ann['coords'][:2]
                try:
                    from PIL import ImageFont
                    font = ImageFont.truetype('segoeuib.ttf', 16)
                except Exception:
                    from PIL import ImageFont
                    font = ImageFont.load_default()
                draw.text((ax1 - x1, ay1 - y1), ann.get('text', ''),
                          fill=color, font=font)

        out = img.convert('RGB')
        if marker_layer is not None:
            from PIL import ImageChops
            out = ImageChops.multiply(out, marker_layer)
        return out

    @staticmethod
    def _hex_to_rgb(hex_color: str):
        h = hex_color.lstrip('#')
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

    # Marker translucency for the multiply-blend layer. 0.0 leaves the
    # background untouched, 1.0 paints full colour (electric yellow).
    # Pixel-matched against Lightshot screenshots: their marker reads
    # as ~0.10-0.13 — almost an off-white wash. Multiply preserves
    # black text fully (black × anything = black) so the underlying
    # text stays crisp on light backgrounds.
    _MARKER_STRENGTH = 0.20

    @classmethod
    def _marker_tint_rgb(cls, hex_color: str) -> tuple[int, int, int]:
        """RGB tuple to paint onto the multiply layer. Shifts `hex_color`
        toward white by (1 - _MARKER_STRENGTH); white areas of the layer
        leave the base untouched, and the tinted stroke gently colours
        whatever's underneath without dimming text."""
        r, g, b = cls._hex_to_rgb(hex_color)
        s = cls._MARKER_STRENGTH
        return (int(255 - (255 - r) * s),
                int(255 - (255 - g) * s),
                int(255 - (255 - b) * s))

    @classmethod
    def _lighten_for_preview(cls, hex_color: str) -> str:
        """Hex string of the same tint used in the final render, for
        the Tk canvas live preview. Tk has no per-item alpha so we
        pre-mix toward white using the same strength as the multiply
        layer — WYSIWYG over light backgrounds."""
        r, g, b = cls._marker_tint_rgb(hex_color)
        return f'#{r:02x}{g:02x}{b:02x}'

    # ─────────────────────────────────────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────────────────────────────────────

    def _print(self) -> None:
        """Save to a temp PNG and send to the default printer via ShellExecute."""
        img = self._render()
        if not img:
            return
        import tempfile, os, threading
        tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        tmp.close()
        try:
            img.save(tmp.name)
            # 'print' verb opens the OS print dialog for the image
            ctypes.windll.shell32.ShellExecuteW(
                None, 'print', tmp.name, None, None, 0)
            # ShellExecute is async — the print dialog can stay open for
            # minutes. Schedule deletion 5 min out so the temp doesn't
            # accumulate in %TEMP% per print. If user is still printing
            # at the 5 min mark, Windows handles the in-use case (silent
            # PermissionError caught below).
            def _gc(path=tmp.name):
                try: os.unlink(path)
                except Exception: pass
            threading.Timer(300, _gc).start()
        except Exception as e:
            print(f'Screenshot print error: {e}')
            try: os.unlink(tmp.name)
            except Exception: pass
        # Don't close overlay, let user continue after printing

    def _copy(self) -> None:
        img = self._render()
        if not img:
            return
        try:
            _put_image_on_clipboard(img)
        except Exception as e:
            print(f'Screenshot copy error: {e}')
            return
        self._close()

    def _save(self) -> None:
        img = self._render()
        if not img:
            return

        def _do():
            # Lower overlay so the file dialog is accessible
            self._win.unbind('<Escape>')
            self._win.attributes('-topmost', False)
            self._win.lower()

            path = filedialog.asksaveasfilename(
                defaultextension='.png',
                filetypes=[('PNG image', '*.png'), ('JPEG image', '*.jpg *.jpeg')],
                title='Save screenshot',
                parent=self._win,
            )
            if path:
                img.save(path)
                try:
                    _put_image_on_clipboard(img)
                except Exception:
                    pass

            # Restore overlay (always, whether saved or cancelled)
            self._win.attributes('-topmost', True)
            self._win.lift()
            self._win.bind('<Escape>', lambda e: self._cancel())

        # Schedule on the screenshot mainloop thread (same as _pick_color)
        self._root.after(0, _do)

    def _extract_text_action(self) -> None:
        """Context-menu "Extract text" — render the selection, hand it to the
        OCR callback, then close the overlay so the result popup gets focus.

        Kept around because the in-popup Extract text button (inside the
        Scan preview) routes through the same callback path; only the
        context-menu surfacing was removed."""
        img = self._render()
        cb = self._on_extract_text
        self._close()
        if img and cb:
            try: cb(img)
            except Exception as e:
                print(f'Screenshot extract-text callback error: {e}')

    def _scan_action(self) -> None:
        """Context-menu "Scan document" — render the selection and hand it
        to the scan callback, which applies CamScanner-style cleanup
        (corner detection + perspective transform + enhance) and opens
        a preview popup with mode toggle + Save/Copy/Extract buttons."""
        img = self._render()
        cb = self._on_scan
        self._close()
        if img and cb:
            try: cb(img)
            except Exception as e:
                print(f'Screenshot scan callback error: {e}')

    def _translate_action(self) -> None:
        """Context-menu "Translate to English (AI)" — same flow as extract, but
        routes through the translate callback (which OCRs THEN translates via LLM)."""
        img = self._render()
        cb = self._on_translate
        self._close()
        if img and cb:
            try: cb(img)
            except Exception as e:
                print(f'Screenshot translate callback error: {e}')

    def _translate_google_action(self) -> None:
        """Context-menu "Translate to English (Google)" — OCR then translate via
        the Google Translate web endpoint (deep-translator scrape). Lets the
        user compare Google's neural output against the LLM translation."""
        img = self._render()
        cb = self._on_translate_google
        self._close()
        if img and cb:
            try: cb(img)
            except Exception as e:
                print(f'Screenshot translate-google callback error: {e}')

    def _translate_offline_ar_action(self) -> None:
        """Context-menu "Translate to English (Offline Arabic OCR)" — uses
        Tesseract with ara+eng language packs for the OCR step, then
        Google line-by-line for translation. Significantly better for
        dense Arabic text than the hosted vision-LLM path because
        Tesseract Arabic was trained on millions of Arabic documents
        with proper ligature + RTL handling."""
        img = self._render()
        cb = self._on_translate_offline_ar
        self._close()
        if img and cb:
            try: cb(img)
            except Exception as e:
                print(f'Screenshot translate-offline-ar callback error: {e}')

    def _select_all(self) -> None:
        self._sx, self._sy = 0, 0
        self._cx, self._cy = self._vw - 1, self._vh - 1
        self._has_sel = True
        self._redraw()

    def _clear_sel(self) -> None:
        self._sx = self._sy = self._cx = self._cy = 0
        self._has_sel = False
        self._canvas.delete('sel', 'handle', 'badge', 'toolbar', 'actionbar')

    def _cancel(self) -> None:
        self._close()

    def _close(self) -> None:
        self._hide_tooltip()
        with _overlay_lock:
            _pending_overlay[0] = None   # L-4: clear both flags under the same lock
            _overlay_active[0] = False
        try:
            self._win.grab_release()
        except Exception:
            pass
        try:
            self._win.destroy()
        except Exception:
            pass
        # Restore grab to whatever had it before we stole it (e.g. RecorderSetupDialog)
        try:
            prev = getattr(self, '_prev_grab', None)
            if prev is not None and prev.winfo_exists():   # M-1: guard destroyed windows
                prev.grab_set()
        except Exception:
            pass
        if self._own_root:
            # We own this root, quit our mainloop so _build() can return
            try:
                self._root.quit()
            except Exception:
                pass


# ── Public entry points ───────────────────────────────────────────────────────

def cancel_screenshot() -> bool:
    """Close the active screenshot overlay (if any). Must be called from the main thread.
    Returns True if an overlay was found and closed."""
    ov = _pending_overlay[0]
    if ov is not None:
        try:
            ov._close()
        except Exception:
            pass
        return True
    # Belt-and-suspenders: release the active flag even if _pending_overlay is None
    with _overlay_lock:
        if _overlay_active[0]:
            _overlay_active[0] = False
            return True
    return False


def _create_overlay(root, on_done, shot, dim_img,
                    on_extract_text=None, on_translate=None,
                    on_translate_google=None,
                    on_translate_offline_ar=None,
                    on_scan=None) -> None:
    """Called on the main thread once the background grab completes."""
    if not _overlay_active[0]:
        return   # was cancelled while the grab was in flight
    import logging as _logging
    _scr_log = _logging.getLogger(__name__)
    _scr_log.info('[PIPELINE] _create_overlay on main thread; '
                  'constructing ScreenshotOverlay…')
    try:
        ov = ScreenshotOverlay(root, on_done=on_done,
                               on_extract_text=on_extract_text,
                               on_translate=on_translate,
                               on_translate_google=on_translate_google,
                               on_translate_offline_ar=on_translate_offline_ar,
                               on_scan=on_scan,
                               _preloaded_shot=shot, _preloaded_dim=dim_img)
        _pending_overlay[0] = ov
        _scr_log.info('[PIPELINE] ScreenshotOverlay constructed OK; '
                      'overlay should now be visible')
    except Exception as e:
        # Log WHY the overlay failed — the watchdog otherwise silently
        # resets the singleton flag 2-3s later, hiding the root cause.
        # Real symptoms tied to this: PrtSc "stops working" after some
        # window goes wonky, then comes back when the watchdog clears
        # the flag. Without this log we can't tell which.
        import logging, traceback
        logging.getLogger(__name__).warning(
            f'Screenshot overlay construction failed: {e}\n'
            f'{traceback.format_exc()}'
        )
        with _overlay_lock:
            _overlay_active[0] = False


def take_screenshot(root=None, on_done=None,
                    on_extract_text=None, on_translate=None,
                    on_translate_google=None,
                    on_translate_offline_ar=None,
                    on_scan=None) -> None:
    """Grab the screen in a background thread, then build the overlay on the main thread.

    ImageGrab.grab(all_screens=True) on a large or multi-monitor desktop can
    take 500 ms – 2 s.  Running it on the main thread blocks the tkinter event
    loop entirely, making Esc/Del unresponsive and (in extreme cases) freezing
    the whole system because both WH_KEYBOARD_LL hooks stop pumping messages.
    The fix: grab + dim in a daemon thread, then hand the images to the main
    thread via root.after() so _build() never stalls the event loop.
    """
    # Claim the singleton slot BEFORE spawning the thread so rapid PrtSc
    # presses don't start multiple concurrent grabs. Before refusing,
    # self-heal: if the flag is set but no real overlay window exists
    # (previous overlay crashed, was force-closed, raced through cleanup,
    # etc.), clear the flag and proceed. Without this, a single stale
    # True silently dead-locks Print Screen forever.
    import logging as _logging
    _scr_log = _logging.getLogger(__name__)
    _scr_log.info('[PIPELINE] take_screenshot() entered')
    with _overlay_lock:
        if _overlay_active[0]:
            # Grace window: an in-flight grab that hasn't yet built
            # the overlay still has _pending_overlay==None. Without
            # this we'd misread that as "stale flag" and proceed to
            # start a SECOND concurrent grab on top of the first.
            import time as _time
            held_for = _time.monotonic() - _overlay_claim_ts[0]
            if held_for < _OVERLAY_GRACE_SECS:
                _scr_log.info(f'[PIPELINE] rejected — singleton held '
                              f'{held_for*1000:.0f}ms, grab still in flight')
                return
            ov = _pending_overlay[0]
            still_alive = False
            try:
                if ov is not None:
                    # An overlay is "real" iff its toplevel still exists
                    # and is mapped. Check `_win` first — that's where
                    # ScreenshotOverlay puts its visible Toplevel. The
                    # other names (`_overlay`, `_root`) were vestigial
                    # and pointed at the always-withdrawn main app root,
                    # which made this check always say "not alive" and
                    # let duplicate overlays slip through.
                    tk_root = (getattr(ov, '_win',     None)
                               or getattr(ov, '_overlay', None)
                               or getattr(ov, '_root',    None))
                    still_alive = (tk_root is not None
                                   and tk_root.winfo_exists()
                                   and tk_root.winfo_ismapped())
            except Exception:
                still_alive = False
            if still_alive:
                _scr_log.info('[PIPELINE] rejected — overlay already '
                              'live (this PrtSc ignored)')
                return   # genuine in-flight overlay, do not start a 2nd
            # Stale flag, clear and continue.
            _overlay_active[0] = False
            _pending_overlay[0] = None
            _scr_log.info('[PIPELINE] cleared stale singleton flag from '
                          'previous run; proceeding')
        import time as _time
        _overlay_active[0] = True
        _overlay_claim_ts[0] = _time.monotonic()
        _scr_log.info('[PIPELINE] singleton claimed; starting grab thread')

    if root is not None:
        def _grab():
            import time as _time
            _t0 = _time.perf_counter()
            try:
                _scr_log.info('[PIPELINE] grab thread: ImageGrab.grab(all_screens=True) starting')
                shot = ImageGrab.grab(all_screens=True)
                _scr_log.info(f'[PIPELINE] grab done in '
                              f'{(_time.perf_counter() - _t0) * 1000:.0f}ms '
                              f'({shot.size[0]}×{shot.size[1]})')
                # ImageEnhance.Brightness is ~10× faster than image.point()
                # for large captures (uses PIL's C-level implementation).
                from PIL import ImageEnhance
                dim_img = ImageEnhance.Brightness(shot).enhance(_DIM_FACTOR)
                _scr_log.info('[PIPELINE] dim pass done; scheduling overlay on main thread')
            except Exception as e:
                # Log WHY the grab failed — same reasoning as the
                # overlay-construction except above. Common culprits:
                # DWM busy during HDR mode switch, locked desktop,
                # remote-desktop reconnect mid-grab, PIL OOM on huge
                # multi-mon captures.
                import logging, traceback
                logging.getLogger(__name__).warning(
                    f'Screenshot grab failed: {e}\n{traceback.format_exc()}'
                )
                with _overlay_lock:
                    _overlay_active[0] = False
                return
            root.after(0, lambda: _create_overlay(
                root, on_done, shot, dim_img,
                on_extract_text=on_extract_text, on_translate=on_translate,
                on_translate_google=on_translate_google,
                on_translate_offline_ar=on_translate_offline_ar,
                on_scan=on_scan))

        threading.Thread(target=_grab, daemon=True, name='screenshot-grab').start()
    else:
        # Legacy fallback: no root supplied, run everything in a thread
        # (creates its own Tk root inside ScreenshotOverlay).
        threading.Thread(
            target=lambda: ScreenshotOverlay(
                None, on_done=on_done,
                on_extract_text=on_extract_text, on_translate=on_translate,
                on_translate_google=on_translate_google),
            daemon=True, name='screenshot-overlay').start()
