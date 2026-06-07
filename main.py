"""
Hotkeys, unified text refinement + speech-to-text app.
Merges PromptRefiner (Groq / Cerebras / local Qwen) with KaiWhisper (faster-whisper).
One tray icon, both features, keyboard-library hotkeys.
"""
import os
import sys

# ── EARLY EXIT for --whiteboard subprocess ────────────────────────────────────
# When Shift+F8 spawns `Hotkeys.exe --whiteboard`, that subprocess just needs
# to run whiteboard.main() and exit. It does NOT need engine/library/
# transcribe/customtkinter/faster_whisper loaded. Loading them all in the
# subprocess wastes ~10s + ~500MB RAM, AND causes the PARENT process to
# crash with ACCESS_VIOLATION (no event log entry) for reasons we did not
# fully isolate — likely a shared handle / heap interaction across the
# Hotkeys.exe boundary while the subprocess's heavy imports initialize.
# Short-circuit BEFORE any other import so subprocess is small and isolated.
if __name__ == '__main__' and '--whiteboard' in sys.argv:
    try:
        from whiteboard import main as _wb_main
        _wb_main()
    except BaseException:
        import traceback as _wb_tb
        try:
            import ctypes as _wb_ct
            _wb_ct.windll.user32.MessageBoxW(
                0, 'Whiteboard crashed:\n\n' + _wb_tb.format_exc()[:1500],
                'Hotkeys, Whiteboard', 0x10)
        except Exception:
            _wb_tb.print_exc()
    sys.exit(0)

import sys as _sys_for_fh
import faulthandler as _fh
import time as _time_for_fh

# ── NATIVE CRASH HANDLER (must be FIRST runnable code) ────────────────────────
# Python's faulthandler catches segfaults / heap-corruption fastfails / stack
# overflows the moment they happen and dumps the Python + C call stack of
# every thread to a file BEFORE the process dies. Without this, a native
# crash like STATUS_STACK_BUFFER_OVERRUN (0xc0000409) just kills the process
# with no trace beyond the Windows event log offset. This single hook is the
# difference between "crashed somewhere" and "crashed on line X of file Y".
try:
    _crash_dir = (
        os.path.join(os.path.dirname(_sys_for_fh.executable), 'data')
        if getattr(_sys_for_fh, 'frozen', False)
        else os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'Hotkeys')
    )
    os.makedirs(_crash_dir, exist_ok=True)
    _crash_path = os.path.join(_crash_dir, 'crash.log')
    # Open append-binary so multiple launches accumulate; faulthandler needs
    # a real file descriptor, not a buffered stream.
    _crash_fh = open(_crash_path, 'a', buffering=1)
    _crash_fh.write(f'\n=== process start {_time_for_fh.strftime("%Y-%m-%d %H:%M:%S")} '
                    f'pid={os.getpid()} frozen={getattr(_sys_for_fh, "frozen", False)} ===\n')
    _crash_fh.flush()
    _fh.enable(file=_crash_fh, all_threads=True)
    # NOTE: dump_traceback_later(repeat=True) was tried as a diagnostic but
    # it walks every thread's stack from a background timer thread, which
    # races with Tk widget creation in C code and causes 0xc0000005 access
    # violations inside python312.dll. Keep faulthandler.enable for crash
    # trapping only; rely on app.log + Win32 SEH filter for forensics.
except Exception as _e:
    # Crash handler setup itself failed; not fatal. Just continue.
    pass

# Also install a Windows-level Structured Exception Handler that logs the
# Windows exception record (code + address + thread) the instant a native
# fault triggers. faulthandler catches Python-aware signals; SEH catches
# the lower-level ones (heap-corruption fastfail, access-violation, etc).
try:
    if _sys_for_fh.platform == 'win32':
        import ctypes as _ct_seh
        import ctypes.wintypes as _wt_seh
        _LPVOID = _ct_seh.c_void_p
        _LPTOP_LEVEL_EXCEPTION_FILTER = _ct_seh.WINFUNCTYPE(_ct_seh.c_long, _LPVOID)

        def _seh_filter(ep):
            try:
                # EXCEPTION_POINTERS → EXCEPTION_RECORD
                # First 4 bytes of EXCEPTION_RECORD: ExceptionCode
                code = _ct_seh.cast(ep, _ct_seh.POINTER(_ct_seh.c_uint))[0] if ep else 0
                _crash_fh.write(f'\n*** WIN32 SEH: ExceptionCode=0x{code:08x} '
                                f'thread={os.getpid()} ts={_time_for_fh.strftime("%H:%M:%S")} ***\n')
                _crash_fh.flush()
            except Exception:
                pass
            return 0   # EXCEPTION_CONTINUE_SEARCH — let faulthandler / OS handle next
        _SEH_REF = _LPTOP_LEVEL_EXCEPTION_FILTER(_seh_filter)
        _ct_seh.windll.kernel32.SetUnhandledExceptionFilter(_SEH_REF)
except Exception:
    pass

# ── Threading-runtime guards (MUST run before any heavy import) ───────────────
# When PyInstaller bundles torch + ctranslate2 + onnxruntime + numpy + av into
# one process, each ships its OWN copy of OpenMP / MKL threading runtimes.
# Windows loads the first one it sees; the others' C extensions remain bound
# to their build-time OpenMP, creating an ABI mismatch that corrupts the heap
# and crashes the process with STATUS_STACK_BUFFER_OVERRUN (0xc0000409) at
# random points after startup. KMP_DUPLICATE_LIB_OK tells Intel OpenMP to
# tolerate the duplicate; the per-library NUM_THREADS=1 forces single-thread
# mode so the runtimes cannot fight over the thread pool. In source mode
# (pythonw main.py) this never happens because the runtimes load lazily from
# venv/site-packages with a different process layout, so these guards are
# strictly defensive and harmless in dev.
os.environ.setdefault('KMP_DUPLICATE_LIB_OK',  'TRUE')
os.environ.setdefault('OMP_NUM_THREADS',       '1')
os.environ.setdefault('MKL_NUM_THREADS',       '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS',  '1')
os.environ.setdefault('NUMEXPR_NUM_THREADS',   '1')

import sys
import time
import queue
import socket
import ctypes
import logging
import logging.handlers
import threading
import tkinter as tk
import datetime

import customtkinter as ctk
import keyboard
import kbhook  # Bulletproof replacement for keyboard.add_hotkey — see kbhook.py
import mouse
import pyperclip
import pystray
from pathlib import Path
from PIL import Image, ImageDraw

from storage  import (
    load_config, save_config, load_prompts, save_prompts,
    appdata_dir, log_path, models_dir, assets_dir,
    save_history, load_history, make_whisper_cfg, _HISTORY_MAX_ENTRIES,
    load_chains, save_chains,
)
from engine      import build_provider, LocalProvider, Provider, local_provider_available
from overlay     import OverlayWindow
from library     import LibraryWindow
from sticky_note import PromptStickyNote
from settings    import SettingsWindow
from history_ui  import HistoryWindow
from core.audio       import AudioCapture
from core.vad         import SileroVAD
from core.transcriber import Transcriber
from core.typer       import copy_to_clipboard, paste_from_clipboard, copy_selection, undo_last
from core.sounds      import play_start, play_stop
from screenshot       import (take_screenshot, start_prtsc_listener,
                              start_prtsc_keylogger)
from macros.recorder      import MacroRecorder
from macros.library       import MacroLibrary
from macros.save_prompt   import MacroSavePrompt
from screen_recorder      import Recorder as ScreenRecorder, show_save_dialog
from gif_recorder         import GifRecorder, GifSetupDialog, show_gif_save_dialog, add_to_gif_index
from explain_pill         import AskPill
from quicknotes           import QuickNotesWindow
# Legacy Tk whiteboard removed, Shift+F8 now uses whiteboard.py
# (offline @whiteboard/whiteboard inside Edge WebView2). The old WhiteboardWindow,
# perfect_freehand.py, and rough.py have been deleted.

ctk.set_appearance_mode('dark')
ctk.set_default_color_theme('dark-blue')

# ── Logging ───────────────────────────────────────────────────────────────────

os.makedirs(appdata_dir(), exist_ok=True)
_log_handler = logging.handlers.RotatingFileHandler(
    log_path(), maxBytes=1_000_000, backupCount=3, encoding='utf-8',
)
_log_handler.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-8s  %(name)s: %(message)s'))
logging.getLogger().addHandler(_log_handler)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger('main')

VERSION = '1.0.0'

# Sentinel passed to _do_ask when image OCR finds no text
_ASK_NO_TEXT = '\x00IMAGE_NO_TEXT'
_DOWNLOAD_NO_URL = '\x00DOWNLOAD_NO_URL'

# Phrases the vision model returns when an image has no readable text
_NO_TEXT_PHRASES = (
    'there is no text',
    'no text found',
    'no text detected',
    'no text visible',
    'no text in this image',
    'image contains no text',
    'does not contain text',
    'i cannot find any text',
    'there are no words',
    'no readable text',
    'cannot extract text',
    'no text to extract',
)


def _ocr_is_no_text(text: str) -> bool:
    """Return True if the OCR result indicates no text was found in the image."""
    if not text or not text.strip():
        return True
    t = text.lower()
    return any(phrase in t for phrase in _NO_TEXT_PHRASES)


# ── App ───────────────────────────────────────────────────────────────────────

# ── Splash screen ────────────────────────────────────────────────────────────

class SplashScreen:
    """Startup pill, bolt logo + spinning ring + progress bar.

    Tracks N weighted steps. Each step contributes its weight to the overall
    progress when marked done; partial progress within a step can be reported
    by passing 0.0-1.0 to mark_done. The progress bar fills smoothly across
    the bottom edge of the pill, with the current step name + percentage
    shown as the subtitle, so the user always knows what's happening.

    Steps registered up front (in order):
      • whisper    , load the faster-whisper model from disk
      • whisper_jit, JIT-warm CTranslate2's CPU kernels
      • cloud      , open TLS connection to Groq for cloud transcription
      • provider   , pre-warm the LLM provider (Groq/Cerebras/local)

    Callers use:
      mark_done('whisper')        # default 1.0, fully done
      mark_done('whisper', 0.5)   # 50% through this step (partial UI update)
      mark_error('whisper')       # red border + 'Error' text
    """

    _TRANSP  = '#010101'   # Windows transparent color
    _RADIUS  = 22
    _H       = 64          # taller to fit progress bar + 2 text rows
    _W       = 380         # slightly wider for longer status strings
    _ICO     = 32
    _ICON_CX = 30
    _PB_H    = 4           # progress bar thickness
    _PB_PAD  = 14          # left/right inset of progress bar inside pill

    # (key, label, weight), weights ≈ relative time on a typical machine
    _STEPS = (
        ('whisper',     'Loading Whisper model',     30),
        ('whisper_jit', 'Warming up speech engine',  35),
        ('cloud',       'Connecting to cloud',       10),
        ('provider',    'Connecting AI provider',    25),
    )

    # Short rotating tips for the JIT-warmup pause. Kept ≤ ~38 chars so
    # they fit inside the pill at 10 pt without truncation.
    _TIPS = (
        'Ctrl+Enter → dictate anywhere',
        'Alt+Shift+W → rewrite selection',
        'Shift+F4 → explain selection',
        'Shift+F1 → record a macro',
        'Shift+F8 → offline whiteboard',
        'Shift+F9 → transcribe any file',
    )

    def __init__(self, root: ctk.CTk, provider_label: str) -> None:
        from theme import SURFACE, ACCENT, OK, ERR, TEXT_P, TEXT_S, FONT_FAMILY
        self._root           = root
        self._closed         = False
        # progress[key] in 0.0-1.0; weight[key] is registered at init.
        self._progress: dict = {k: 0.0 for k, _, _ in self._STEPS}
        self._weights:  dict = {k: w   for k, _, w in self._STEPS}
        self._labels:   dict = {k: l   for k, l, _ in self._STEPS}
        self._has_error      = False
        self._provider_label = provider_label
        self._font_family    = FONT_FAMILY
        self._c_surface      = SURFACE
        self._c_accent       = ACCENT
        self._c_ok           = OK
        self._c_err          = ERR
        self._c_text         = TEXT_P
        self._c_sub          = TEXT_S
        self._c_pb_fg        = ACCENT
        self._c_pb_bg        = '#2a2a32'
        self._text_id        = None
        self._sub_id         = None
        self._icon_img       = None
        self._pb_bg_id       = None
        self._pb_fg_id       = None
        self._displayed_pct  = 0.0
        self._tip_tick       = 0

        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.attributes('-transparentcolor', self._TRANSP)
        win.configure(bg=self._TRANSP)
        win.resizable(False, False)
        self._win = win

        self._canvas = tk.Canvas(
            win, width=self._W, height=self._H,
            bg=self._TRANSP, highlightthickness=0,
        )
        self._canvas.pack()

        self._build_pill(self._c_accent)
        self._build_icon()
        self._build_text('Starting up…', self._c_sub)
        self._build_progress_bar()

        win.update_idletasks()
        sw = win.winfo_screenwidth()
        win.geometry(f'{self._W}x{self._H}+{sw - self._W - 20}+20')

        self._animate()

    # ── Build helpers (called once; ring/text updated in place) ───────────────

    def _build_pill(self, accent: str) -> None:
        c = self._canvas
        W, H, R = self._W, self._H, self._RADIUS
        pts = [
            R, 0,   W-R, 0,
            W, 0,   W,   R,
            W, H-R, W,   H,
            W-R, H, R,   H,
            0,   H, 0,   H-R,
            0,   R, 0,   0,
        ]
        c.create_polygon(pts, smooth=True,
                         fill=self._c_surface, outline=accent, width=1,
                         tags='pill')

    def _build_icon(self) -> None:
        """Render the bolt logo (same as tray icon) as a PhotoImage on the canvas."""
        from PIL import Image as _Img, ImageDraw as _IDraw, ImageFilter as _IF, ImageTk
        S = 4
        B = self._ICO * S

        def _hex(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

        def _grad_mask(mask, c1, c2):
            r1,g1,b1 = _hex(c1); r2,g2,b2 = _hex(c2)
            grad = _Img.new('RGBA', (B, B))
            dg   = _IDraw.Draw(grad)
            for y in range(B):
                t = y / (B - 1)
                dg.line([(0,y),(B,y)], fill=(
                    int(r1+(r2-r1)*t), int(g1+(g2-g1)*t), int(b1+(b2-b1)*t), 255))
            out = _Img.new('RGBA', (B, B), (0,0,0,0))
            out.paste(grad, mask=mask.split()[0])
            return out

        base = _Img.new('RGBA', (B, B), (0,0,0,0))
        d    = _IDraw.Draw(base)
        d.rounded_rectangle([0,0,B-1,B-1], radius=13*S//2, fill='#7c3aed')
        d.rounded_rectangle([3*S//2,3*S//2,B-1-3*S//2,B-1-3*S//2], radius=11*S//2, fill='#080f1a')

        BOLT = [(x*S//2, y*S//2) for x,y in [(42,4),(10,34),(28,34),(22,60),(52,26),(36,26)]]
        bolt_mask = _Img.new('RGBA', (B, B), (0,0,0,0))
        _IDraw.Draw(bolt_mask).polygon(BOLT, fill='white')

        glow = _grad_mask(bolt_mask.filter(_IF.GaussianBlur(6)), '#7dd3fc', '#1e40af')
        base = _Img.alpha_composite(base, glow)
        base = _Img.alpha_composite(base, _grad_mask(bolt_mask, '#bae6fd', '#0f2a6e'))
        base = base.resize((self._ICO, self._ICO), _Img.LANCZOS)

        self._icon_img = ImageTk.PhotoImage(base)
        cx = self._icon_cx()
        cy = self._H // 2
        self._canvas.create_image(cx, cy, image=self._icon_img, anchor='center', tags='icon')

    def _build_text(self, sub: str, sub_color: str) -> None:
        tx = self._ICON_CX + self._ICO // 2 + 14
        # Bias text up slightly to leave space for the progress bar.
        cy = (self._H - self._PB_H - 6) // 2 + 2
        self._text_id = self._canvas.create_text(
            tx, cy - 8,
            text='Hotkeys', anchor='w',
            font=(self._font_family, 13, 'bold'),
            fill=self._c_text,
        )
        self._sub_id = self._canvas.create_text(
            tx, cy + 8,
            text=sub, anchor='w',
            font=(self._font_family, 10),
            fill=sub_color,
        )

    def _build_progress_bar(self) -> None:
        """Draw the empty progress track inside the bottom edge of the pill."""
        c = self._canvas
        y1 = self._H - self._PB_H - 10
        y2 = y1 + self._PB_H
        x1 = self._PB_PAD
        x2 = self._W - self._PB_PAD
        # Background track
        self._pb_bg_id = c.create_rectangle(
            x1, y1, x2, y2,
            fill=self._c_pb_bg, outline='', tags='pb_bg',
        )
        # Foreground fill, starts as a zero-width rect at the left edge.
        self._pb_fg_id = c.create_rectangle(
            x1, y1, x1, y2,
            fill=self._c_pb_fg, outline='', tags='pb_fg',
        )

    def _icon_cx(self) -> int:
        return self._ICON_CX

    # ── Public API ────────────────────────────────────────────────────────────

    def mark_done(self, step: str, fraction: float = 1.0) -> None:
        """Mark `step` as `fraction`-done (0.0-1.0). Callers can call this
        multiple times for partial updates (e.g. mark_done('whisper', 0.4)
        then mark_done('whisper', 1.0)). Idempotent."""
        if self._closed or step not in self._progress:
            return
        self._progress[step] = max(self._progress[step], min(1.0, max(0.0, float(fraction))))
        self._update_visual()
        # All done? Flash green + close.
        if self._is_complete():
            try:
                self._canvas.itemconfigure('pill', outline=self._c_ok)
                self._canvas.itemconfigure(self._pb_fg_id, fill=self._c_ok)
                self._canvas.itemconfigure(self._sub_id,
                                           text='Ready  ✓', fill=self._c_ok)
                self._canvas.itemconfigure(self._text_id, text='Hotkeys',
                                           fill=self._c_text)
            except Exception:
                pass
            try: play_start()
            except Exception: pass
            self._root.after(1400, self._close)

    def mark_error(self, step: str) -> None:
        if self._closed:
            return
        self._has_error = True
        try:
            self._canvas.itemconfigure('pill', outline=self._c_err)
            self._canvas.itemconfigure(self._pb_fg_id, fill=self._c_err)
            self._canvas.itemconfigure(self._sub_id,
                                       text='Error (check log)', fill=self._c_err)
        except Exception:
            pass
        self._root.after(2800, self._close)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _is_complete(self) -> bool:
        return all(v >= 1.0 for v in self._progress.values())

    def _overall_pct(self) -> float:
        total_w = sum(self._weights.values()) or 1
        done_w  = sum(self._weights[k] * self._progress[k]
                      for k in self._progress)
        return min(1.0, done_w / total_w)

    def _current_step_label(self) -> str:
        """The first step that isn't yet finished, what the user is waiting
        on right now."""
        for k, _, _ in self._STEPS:
            if self._progress[k] < 1.0:
                if k == 'provider':
                    return self._provider_label
                return self._labels[k]
        return 'Ready'

    def _update_visual(self) -> None:
        if self._has_error or self._closed:
            return
        target_pct = self._overall_pct()
        # Smoothly interpolate the bar fill, the animate loop pushes
        # _displayed_pct toward target_pct each tick. We just store the
        # target here.
        self._target_pct = target_pct
        try:
            label = self._current_step_label()
            # Show percentage so users see progress is real, not faked.
            pct = int(target_pct * 100)
            self._canvas.itemconfigure(self._sub_id,
                                       text=f'{label}…  {pct}%')
        except Exception:
            pass

    def _animate(self) -> None:
        if self._closed:
            return
        # Smoothly fill toward target. Without this the bar jumps in chunks
        # as steps complete; with it the bar always feels alive.
        target = getattr(self, '_target_pct', self._overall_pct())
        if abs(self._displayed_pct - target) > 0.001:
            self._displayed_pct += (target - self._displayed_pct) * 0.18
            try:
                x1 = self._PB_PAD
                x2 = self._W - self._PB_PAD
                w  = (x2 - x1) * self._displayed_pct
                y1 = self._H - self._PB_H - 10
                y2 = y1 + self._PB_H
                self._canvas.coords(self._pb_fg_id, x1, y1, x1 + w, y2)
            except Exception:
                pass

        # Cycle through tips every 4 s on the longer steps (whisper_jit
        # dominates startup time, gives the user something to read).
        self._tip_tick += 1
        if not self._has_error and self._tip_tick % 32 == 0:
            try:
                # Only show the tip if the current step has been "in
                # progress" for a while and percentage hasn't budged much.
                if 0.05 < self._displayed_pct < 0.95:
                    tip_idx = (self._tip_tick // 32) % len(self._TIPS)
                    label = self._current_step_label()
                    pct   = int(self._overall_pct() * 100)
                    if self._tip_tick // 32 % 2 == 0:
                        text = f'{label}…  {pct}%'
                    else:
                        text = self._TIPS[tip_idx]
                    self._canvas.itemconfigure(self._sub_id, text=text)
            except Exception:
                pass

        self._root.after(120, self._animate)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._win.destroy()
        except Exception:
            pass


# ── App ───────────────────────────────────────────────────────────────────────

class App:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()

        # ── Refine state ──────────────────────────────────────────────────────
        self._refine_t0:         float = 0.0
        self._refine_in_progress: bool = False
        self._refine_gen:          int = 0   # incremented per request; stale callbacks check this

        # ── Whisper state ─────────────────────────────────────────────────────
        self._whisper_recording = False
        self._whisper_t0: float = 0.0
        # Watchdog: pill stuck on "Transcribing…" recovery. Set when
        # transcription begins, cancelled on result/error, fires after
        # 30 s if nothing arrived. See _transcribe_watchdog_fire().
        self._transcribe_watchdog_id = None
        self._whisper_ready     = False
        self._history: list     = load_history()

        # ── Sticky note (per-prompt hotkey popup) ────────────────────────────
        self._sticky: 'PromptStickyNote | None' = None
        self._sticky_idx: int | None = None   # which prompt index is currently shown

        # ── Undo last refinement ─────────────────────────────────────────────
        self._undo_available: bool  = False
        self._undo_t:         float = 0.0   # timestamp of last completed refinement

        # ── Hotkey re-registration guard ─────────────────────────────────────
        self._hk_reg_lock    = threading.Lock()
        self._hk_reg_pending = False   # set True when a save arrives mid-flight

        # ── Config & prompts ─────────────────────────────────────────────────
        self.config  = load_config()
        self.prompts = load_prompts()

        # ── Restore-defaults state ────────────────────────────────────────────
        # Cache the bundled defaults NOW before any edit can overwrite prompts.json
        # in dev mode (save_prompts writes to the source file in non-frozen builds).
        import json as _json
        try:
            from storage import resource_path as _rp
            with open(_rp('prompts.json'), encoding='utf-8') as _f:
                self._bundled_defaults: list = _json.load(_f)
        except Exception:
            self._bundled_defaults = []
        # Start enabled, only grey out immediately after the user clicks
        # "Restore Default Prompts", and re-enable as soon as any edit is saved.
        # Checking against bundled defaults at startup was too aggressive: it
        # permanently disabled the button whenever prompts happened to match
        # defaults (e.g. fresh install), even if the user never used Restore.
        self._at_default_prompts: bool = False
        self.folders: list[str]       = self.config.get('folders', [])
        self.folder_colors: dict[str, str] = self.config.get('folder_colors', {})
        self.active_prompt: dict = self.prompts[0] if self.prompts else {
            'title': 'Refine', 'prompt': 'Improve the following text and return only the result.'
        }

        # ── Root window (hidden) ─────────────────────────────────────────────
        self.root = ctk.CTk()
        self.root.withdraw()
        self.root.title('Hotkeys')
        self.root.protocol('WM_DELETE_WINDOW', self._quit)
        # Load TkDND extension into the Tk interpreter so any widget can
        # opt into drag-drop later via drop_target_register(DND_FILES).
        # We only require it once at startup, not per widget. If the
        # extension isn't available, drag-drop just won't work but the
        # rest of the app continues normally.
        try:
            from tkinterdnd2 import TkinterDnD
            TkinterDnD._require(self.root)
        except Exception as _e:
            logger.warning(f'TkinterDnD init skipped: {_e}')

        # ── Text-refine provider ─────────────────────────────────────────────
        self.provider: Provider = build_provider(self.config)

        # ── Splash screen ─────────────────────────────────────────────────────
        if isinstance(self.provider, LocalProvider):
            _prov_label = 'Loading local Qwen model'
        elif self.provider.ready:
            _active = self.config.get('active_provider', 'cerebras').title()
            _prov_label = f'Connecting to {_active}'
        else:
            _prov_label = 'AI provider (add API key in Settings)'
        self._splash = SplashScreen(self.root, _prov_label)
        self.root.update()   # render splash before rest of __init__ runs

        # ── UI windows ───────────────────────────────────────────────────────
        self.refine_overlay    = OverlayWindow(self.root, slot=0)
        self.whisper_overlay   = OverlayWindow(self.root, slot=1)
        self.macro_overlay     = OverlayWindow(self.root, slot=2)
        self.recorder_overlay  = OverlayWindow(self.root, slot=3)
        self.gif_overlay       = OverlayWindow(self.root, slot=4)
        self.chain_overlay     = OverlayWindow(self.root, slot=5)

        # ── Chains ────────────────────────────────────────────────────────────
        self.chains: list = load_chains()
        self._chain_saved_hks: list = []

        # ── Quick Notes ───────────────────────────────────────────────────────
        self._notes_win: QuickNotesWindow | None = None

        # ── Whiteboard ────────────────────────────────────────────────────────
        # Tracked subprocess pid for the offline-Whiteboard whiteboard
        # (window itself is identified by exact title, see _do_open_whiteboard).
        self._wb_proc_pid: int | None = None

        # ── AskPill tracking ──────────────────────────────────────────────────
        # Keeps weak refs to open pills so _hk_escape can close them and
        # _do_ask can replace a stale pill instead of stacking indefinitely.
        self._ask_pills: list = []

        # ── Screen recorder ───────────────────────────────────────────────────
        self._screen_recorder: ScreenRecorder | None = None
        self._recorder_state  = 'idle'   # 'idle' | 'recording' | 'stopping'
        self._recorder_t0     = 0.0

        # ── GIF recorder ──────────────────────────────────────────────────────
        self._gif_recorder: GifRecorder | None = None
        self._gif_state       = 'idle'   # 'idle' | 'recording' | 'encoding'
        self._gif_t0          = 0.0
        self._gif_setup_dlg   = None   # open GifSetupDialog, if any

        # ── Macro recorder + library ─────────────────────────────────────────
        self._macro          = MacroRecorder()
        self._macro_state    = 'idle'   # 'idle' | 'recording' | 'ready' | 'playing'
        self._macro_stop_hks: list = []
        self._macro_library  = MacroLibrary(Path(appdata_dir()) / 'macros')
        self._macro_saved_hks: list = []   # registered playback hotkeys for saved macros
        self.library  = LibraryWindow(self.root, self.prompts,
                                      on_select=self._on_prompt_selected,
                                      on_save=self._on_prompts_saved,
                                      hotkey_cfg=self._hotkey_cfg(),
                                      on_hotkey_suspend=self._suspend_hotkeys,
                                      on_hotkey_resume=self._resume_hotkeys,
                                      folders=self.folders,
                                      folder_colors=self.folder_colors,
                                      on_folders_changed=self._on_folders_changed,
                                      vision_extractor=self._vision_extractor,
                                      macro_library=self._macro_library,
                                      on_macro_play=self._on_library_macro_play,
                                      on_macro_hotkeys_changed=self._register_macro_saved_hotkeys,
                                      on_feature_hotkey_changed=self._on_feature_hotkey_changed,
                                      on_chains_changed=self._on_chains_changed_cb)
        # Wire the library's recorder tab toggle button → main.py handler
        self.library._on_recorder_toggle = lambda: self._q.put(('recorder:toggle', None))
        # Wire the library's macros tab right-click record → same queue event as Shift+F1
        self.library._on_macro_toggle    = lambda: self._q.put(('macro:hotkey',    None))
        # Wire the macros tab reset button → abort everything and return to idle
        self.library._on_macro_reset     = lambda: self._q.put(('macro:reset',     None))
        # Wire the library's GIF tab toggle button → main.py handler
        self.library._on_gif_toggle      = lambda: self._q.put(('gif:toggle',      None))
        # Wire the library's Ask tab text input → ask handler
        self.library._on_ask             = lambda text: self._q.put(('ask', text))
        # Wire the library's Notes tab "New Note" button → open QuickNotesWindow
        self.library._on_new_note        = self._do_open_notes
        # Wire the library's Whiteboard tab "Open" button → open Whiteboard
        self.library._on_open_whiteboard = self._do_open_whiteboard
        # Wire the library's Audio editor tab "Open" button → toggle Tenacity
        self.library._on_open_audio_editor = self._do_open_audio_editor
        self.settings = SettingsWindow(self.root, self.config,
                                       on_save=self._on_settings_saved,
                                       on_restore=lambda: self._q.put(('restore_all_defaults', None)))
        self.history_win = HistoryWindow(self.root,
                                         on_history_cleared=self._on_history_cleared)

        # ── Whisper pipeline ─────────────────────────────────────────────────
        wcfg = make_whisper_cfg(self.config)
        vad_onnx = Path(assets_dir()) / 'silero_vad.onnx'

        self._vad = SileroVAD(
            vad_onnx,
            speech_threshold=wcfg.vad.speech_threshold,
            safety_silence_s=wcfg.vad.safety_silence_s,
        )
        self._vad.set_safety_stop_callback(self._on_vad_safety_stop)

        self._audio = AudioCapture(
            on_chunk=self._on_audio_chunk,
            on_utterance_ready=self._on_utterance_ready,
            cfg=wcfg,
        )

        self._transcriber = Transcriber(
            cfg=wcfg,
            on_result=self._on_transcription_result,
            on_status=self._on_transcriber_status,
            models_dir=models_dir(),
            log_file=log_path(),
        )

        # ── Event dispatch ────────────────────────────────────────────────────
        self._dispatch = {
            'refine':           self._do_refine,
            'undo_refine':      self._do_undo_refine,
            'library':          lambda _: self.library.show(),
            'settings':         lambda _: self.settings.show(),
            'history':          lambda _: self.history_win.show(self._history),
            'refine:done':      self._on_refine_done,
            'refine:error':     self.refine_overlay.show_error,
            'model_ready':      self._on_model_ready,
            'model_error':      self._on_model_error,
            'prewarm:done':     lambda _: self._splash.mark_done('provider'),
            'refine:timeout':   self._on_refine_timeout,
            'refine:unlock':    self._on_refine_unlock,
            'switch_provider':  self._switch_provider,
            'whisper:start':    lambda _: self._whisper_start_recording(),
            'whisper:stop':     lambda _: self._whisper_stop_recording(),
            'whisper:cancel':   lambda _: self._whisper_cancel_recording(),
            'restore_all_defaults': lambda _: self._do_restore_all_defaults(),
            'reload_hotkeys':   lambda _: self._reload_hotkeys_manual(),
            'prompt_hotkey':    self._on_prompt_hotkey,
            'whisper:status':   self._on_transcriber_status_event,
            'whisper:result':   self._on_whisper_result,
            'whisper:error':    self._on_whisper_error,
            'macro:hotkey':       self._on_macro_hotkey,
            'macro:stop':         self._on_macro_emergency_stop,
            'macro:cap':          lambda _: self._on_macro_cap(),
            'macro:reset':        lambda _: self._macro_reset(),
            'macro:play_saved':   self._on_library_macro_play,
            'screenshot:cancel':  lambda _: self._do_cancel_screenshot(),
            'recorder:toggle':    lambda _: self._on_recorder_toggle(),
            'recorder:cap':       lambda _: self._on_recorder_cap(),
            'recorder:size':      lambda b: None,   # handled by _recorder_tick poll
            'gif:toggle':         lambda _: self._on_gif_toggle(),
            'gif:cap':            lambda _: self._on_gif_cap(),
            'gif:done':           self._on_gif_done,
            'gif:error':          self._on_gif_error,
            'ask':                self._do_ask,
            'ask:close_all':      lambda _: self._close_all_ask_pills(),
            'web':                lambda _: self._do_web(),
            'chain':              self._do_chain,
            'chain_named':        self._do_chain_named,
            'notes':              lambda _: self._do_open_notes(),
            'whiteboard':         lambda _: self._do_open_whiteboard(),
            # Shift+F9, open the Library on the Transcribe tab (file/URL
            # → diarized transcript + AI summary + multi-format export).
            'transcribe':         lambda _: self._do_open_transcribe(),
            # Shift+F10, bundled audio editor (Tenacity, relabeled).
            'audio_editor':       lambda _: self._do_open_audio_editor(),
            # Ctrl+Alt+D, downloads URL from selection/clipboard via yt-dlp.
            'download_url':       lambda url: self._do_download_url(url),
        }

        self._register_hotkeys()
        logger.info('DEBUG: _register_hotkeys returned')
        self._register_macro_saved_hotkeys()
        logger.info('DEBUG: _register_macro_saved_hotkeys returned')
        self._register_chain_hotkeys()
        logger.info('DEBUG: _register_chain_hotkeys returned')
        self.root.after(2000, self._hotkey_watchdog)
        logger.info('DEBUG: hotkey_watchdog scheduled')
        start_prtsc_listener(self._hk_screenshot)
        logger.info('DEBUG: start_prtsc_listener returned')
        # Independent diagnostic — polls VK_SNAPSHOT directly so we still
        # see the keypress in the log even when our WH_KEYBOARD_LL hook
        # gets suppressed (UIPI, anti-cheat, shell hijack, hook-timeout
        # uninstall). When the keylogger fires but [HOOK] doesn't, the
        # diagnosis is unambiguous: the OS saw the key, our hook didn't.
        start_prtsc_keylogger()
        self._start_tray()
        logger.info('DEBUG: _start_tray returned')
        self._start_ipc()
        logger.info('DEBUG: _start_ipc returned')

        self.root.after(2000, self._check_data_dir_writable)


        if isinstance(self.provider, LocalProvider):
            threading.Thread(target=self._load_model, daemon=True).start()

        threading.Thread(target=self._prewarm, daemon=True).start()
        # NOTE: we used to call `self._audio.start()` here to pre-warm the
        # input stream so the first Ctrl+Enter had zero latency. That pre-
        # warm probes every WASAPI device on the system, and on machines
        # where every device rejects our format (16 kHz / mono / float32)
        # PortAudio's C library corrupts its internal heap after ~5 failed
        # opens and crashes the whole process with STATUS_STACK_BUFFER_OVERRUN
        # (0xc0000409). start_recording() already lazy-opens the stream on
        # demand, so removing the pre-warm only adds ~100 ms to the first
        # voice-typing press but makes the app survive mic-less machines.
        threading.Thread(target=self._watch_singleton_socket, daemon=True).start()

        self.root.after(30, self._poll)
        logger.info(f'Hotkeys v{VERSION} started.')

    # ── Hotkeys ───────────────────────────────────────────────────────────────

    def _hotkey_cfg(self) -> dict:
        return self.config.get('hotkeys', {
            'refine':  'alt+shift+w',
            'library': 'alt+shift+e',
            'whisper': 'ctrl+enter',
        })

    def _suspend_hotkeys(self) -> None:
        """Unhook all keyboard and mouse bindings, called while HotkeyCapture dialog
        is open so nothing fires during capture."""
        try:
            kbhook.unhook_all()
        except Exception:
            pass
        try:
            keyboard.unhook_all()   # clears PTT on_press/on_release if any
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass

    def _resume_hotkeys(self) -> None:
        """Re-register hotkeys after HotkeyCapture closes."""
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()

    def _register_hotkeys(self) -> None:
        """Register all global hotkeys, forcefully resetting the keyboard listener first."""
        # ── Step 1: full teardown ─────────────────────────────────────────────
        try:
            kbhook.unhook_all()
        except Exception:
            pass
        try:
            keyboard.unhook_all()   # PTT on_press_key handlers (if any)
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass

        # Force-stop the keyboard listener thread so it gets a clean slate on
        # the next add_hotkey call.  After multiple hard-kill / restart cycles
        # the listener thread can become a ghost, new hooks are added but
        # never actually fire.
        try:
            if hasattr(keyboard, '_listener') and keyboard._listener is not None:
                keyboard._listener.stop()
                keyboard._listener = None
        except Exception:
            pass

        # ── Step 2: register with one automatic retry ─────────────────────────
        hk  = self._hotkey_cfg()
        ptt = self.config.get('push_to_talk', False)

        def _do_register():
            # suppress=False: the library observes keypresses but never consumes
            # them.  suppress=True was causing the library's internal modifier-state
            # machine to lock up permanently after each suppressed hotkey, Alt and
            # Shift would appear "stuck" to the library even after the user released
            # them, silently blocking all subsequent hotkeys with no recovery path
            # (even unhook_all + re-registration couldn't fix a broken listener).
            # Use kbhook (our bulletproof LL hook) for all permanent
            # hotkey registration. keyboard.add_hotkey()'s shared
            # callback can silently die under load. See kbhook.py.
            kbhook.add_hotkey(hk.get('refine',       'alt+shift+w'), self._hk_refine)
            kbhook.add_hotkey(hk.get('library',      'alt+shift+e'), self._hk_library)
            kbhook.add_hotkey(hk.get('undo_refine',  'alt+shift+z'), self._hk_undo_refine)
            kbhook.add_hotkey(hk.get('macro_record', 'shift+f1'),
                              lambda: self._q.put(('macro:hotkey', None)))
            kbhook.add_hotkey(hk.get('recorder',     'shift+f2'),
                              lambda: self._q.put(('recorder:toggle', None)))
            kbhook.add_hotkey(hk.get('gif_record',   'shift+f3'),
                              lambda: self._q.put(('gif:toggle',      None)))
            kbhook.add_hotkey(hk.get('ask',          'shift+f4'),
                              self._hk_ask)
            kbhook.add_hotkey(hk.get('web',          'shift+f5'),
                              lambda: self._q.put(('web', None)))
            kbhook.add_hotkey(hk.get('chain',        'shift+f6'),
                              self._hk_chain)
            kbhook.add_hotkey(hk.get('notes',        'shift+f7'),
                              lambda: self._q.put(('notes',      None)))
            kbhook.add_hotkey(hk.get('whiteboard',   'shift+f8'),
                              lambda: self._q.put(('whiteboard', None)))
            kbhook.add_hotkey(hk.get('transcribe',   'shift+f9'),
                              lambda: self._q.put(('transcribe', None)))
            # Shift+F10, bundled audio editor (Tenacity).
            kbhook.add_hotkey(hk.get('audio_editor', 'shift+f10'),
                              lambda: self._q.put(('audio_editor', None)))
            kbhook.add_hotkey(hk.get('download_url', 'ctrl+alt+d'),
                              self._hk_download_url)

            if ptt:
                # Push-to-talk reads the full whisper hotkey (e.g. ctrl+enter)
                # and only starts recording while ALL modifiers are held AND
                # the trigger key is pressed. Releasing EITHER part stops the
                # recording. Without this, bare Enter would trigger recording
                # every time the user pressed it in a chat app — useless.
                whisper_hk = hk.get('whisper', 'ctrl+enter').lower()
                parts = [p.strip() for p in whisper_hk.split('+') if p.strip()]
                trigger_key = parts[-1] if parts else 'enter'
                required_mods = parts[:-1]   # e.g. ['ctrl'] for ctrl+enter

                def _mods_held() -> bool:
                    try:
                        return all(keyboard.is_pressed(m) for m in required_mods)
                    except Exception:
                        return False

                def _on_press(_evt):
                    # Only fire if every required modifier is currently held.
                    if not required_mods or _mods_held():
                        self._q.put(('whisper:start', None))

                def _on_release(_evt):
                    self._q.put(('whisper:stop', None))

                keyboard.on_press_key(trigger_key, _on_press, suppress=False)
                keyboard.on_release_key(trigger_key, _on_release, suppress=False)

                # Releasing the modifier (e.g. letting Ctrl up while still
                # holding Enter) also stops the recording — so the user can
                # cancel by simply lifting Ctrl without releasing Enter.
                for _mod in required_mods:
                    keyboard.on_release_key(
                        _mod,
                        lambda _e: self._q.put(('whisper:stop', None)),
                        suppress=False,
                    )

                logger.info(f'PTT mode: hold {whisper_hk!r} '
                            f'(trigger={trigger_key!r}, mods={required_mods})')
            else:
                # suppress=False — the original behaviour. We tried suppress=True
                # to stop WhatsApp / Discord seeing the bare Enter and inserting
                # a newline, but it interfered with games and other apps that
                # legitimately use Ctrl+Enter. The newline cost in chat apps is
                # acceptable; the game compatibility is not.
                kbhook.add_hotkey(hk.get('whisper', 'ctrl+enter'), self._hk_whisper)

            kbhook.add_hotkey('escape', self._hk_escape)

            # Per-prompt hotkeys (assigned via right-click → Assign hotkey…)
            for _idx, _p in enumerate(self.prompts):
                _hk = _p.get('hotkey', '').strip()
                if not _hk:
                    continue
                def _make_ph_handler(idx=_idx):
                    def _handler():
                        self._q.put(('prompt_hotkey', idx))
                    return _handler
                try:
                    kbhook.add_hotkey(_hk, _make_ph_handler())
                    logger.info(f'Per-prompt hotkey: {_hk!r} → [{_idx}] {_p["title"]!r}')
                except Exception as _e:
                    logger.warning(f'Per-prompt hotkey {_hk!r} failed: {_e}')

        try:
            _do_register()
            logger.info(f'Hotkeys registered: {hk}  PTT={ptt}')
        except Exception as e:
            logger.warning(f'Hotkey registration failed ({e}), retrying in 0.5 s')
            time.sleep(0.5)
            try:
                kbhook.unhook_all()
                keyboard.unhook_all()
                mouse.unhook_all()
                _do_register()
                logger.info(f'Hotkeys registered (retry ok): {hk}')
            except Exception as e2:
                logger.error(f'Hotkey registration failed after retry: {e2}')

    @property
    def _vision_extractor(self):
        """Return a callable (img) → str that extracts text from a PIL Image.

        Uses the Groq vision API with the personal bundled key.  The callable is
        safe to pass to LibraryWindow / PromptStickyNote and call from threads.
        """
        from vision import extract_text, DEFAULT_VISION_MODEL
        from engine import _resolve_keys
        import logging as _log
        config = self.config

        def _extract(img):
            model = config.get('providers', {}).get('groq', {}).get(
                'vision_model', DEFAULT_VISION_MODEL)
            keys = _resolve_keys(config, 'groq')
            last_err = None
            for key in keys:
                try:
                    return extract_text(img, key, model)
                except RuntimeError as e:
                    msg = str(e)
                    if 'rate limit' in msg.lower() or '429' in msg or 'quota' in msg.lower():
                        _log.getLogger(__name__).warning(
                            f'Vision: Groq key …{key[-6:]} rate-limited, trying next key')
                        last_err = e
                        continue
                    raise
            raise last_err or RuntimeError('All Groq vision keys exhausted')

        return _extract

    def _reregister_after_action(self) -> None:
        """Re-register hotkeys after a paste action.

        keyboard.send() and injected Ctrl+V/C events flow through the
        keyboard library's own WH_KEYBOARD_LL hook.  With suppress=True
        active on all hotkeys, the library's internal modifier-key state
        can get stuck (it thinks Alt/Shift are still held), silently
        preventing subsequent hotkeys from firing.  Re-registering clears
        the hook, flushes all state, and reinstalls fresh hooks.
        """
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()

    def _hotkey_watchdog(self) -> None:
        """Periodic safety net. Two jobs:

        1. **Listener health**: if the keyboard library's listener thread
           has died, re-register everything so hotkeys keep working.
        2. **State reconciliation**: walk every state-machine flag that
           gates a hotkey ('recording', 'in progress', etc.) and check
           it against the actual underlying object. If the flag says
           "busy" but the corresponding worker / window / thread is gone,
           the flag is stale, so reset it. Without this a single missed
           cleanup poisons that hotkey until the user clicks Reload
           Hotkeys.

        Runs every 2 s. All work is wrapped in try/except so one bad
        reconciliation never breaks the loop.
        """
        # 1. Listener health
        try:
            listener = getattr(keyboard, '_listener', None)
            if listener is not None:
                t = getattr(listener, 'thread', None)
                if t is not None and not t.is_alive():
                    logger.warning('Hotkey listener thread dead, auto re-registering.')
                    threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
        except Exception:
            pass

        # 2. State reconciliation: any flag set to "busy" but with no
        #    real underlying object is stale and gets cleared silently.
        self._reconcile_stuck_states()

        self.root.after(2000, self._hotkey_watchdog)


    def _reconcile_stuck_states(self) -> None:
        """Reset any state flag whose ground-truth object is gone.

        Each block follows the same shape: read the current flag, check
        whether the object that should accompany it is still alive, and
        if not, reset the flag plus any matching UI hint. A reset here
        is silent for the user but lets the next hotkey press succeed.
        """
        # ── Screenshot overlay singleton ──────────────────────────────
        try:
            from screenshot import (_overlay_lock, _overlay_active,
                                    _pending_overlay, _overlay_claim_ts,
                                    _OVERLAY_GRACE_SECS)
            import time as _time
            with _overlay_lock:
                # Grace window: don't reset a flag that was JUST set.
                # The grab+dim runs on a worker thread for ~50-200 ms
                # before the overlay Toplevel is constructed on the
                # main thread. During that window _pending_overlay
                # is legitimately None — without this check the
                # watchdog would race ahead and reset the singleton,
                # letting a second PrtSc start a duplicate grab.
                held_for = (_time.monotonic() - _overlay_claim_ts[0]
                            if _overlay_active[0] else 0.0)
                if _overlay_active[0] and held_for >= _OVERLAY_GRACE_SECS:
                    ov = _pending_overlay[0]
                    alive = False
                    try:
                        if ov is not None:
                            # ScreenshotOverlay puts its visible Toplevel
                            # in `_win`. Earlier the watchdog read
                            # `_overlay` / `_root` first — but `_root` is
                            # the (legitimately withdrawn) main app root,
                            # so winfo_ismapped() returned False on every
                            # successful screenshot and the watchdog
                            # reset the singleton flag every time. Prefer
                            # `_win` (the overlay's actual Toplevel).
                            tk_root = (getattr(ov, '_win',     None)
                                       or getattr(ov, '_overlay', None)
                                       or getattr(ov, '_root',    None))
                            alive = (tk_root is not None
                                     and tk_root.winfo_exists()
                                     and tk_root.winfo_ismapped())
                    except Exception:
                        alive = False
                    if not alive:
                        # Distinguish the three ways we get here so we can
                        # actually diagnose "PrtSc randomly didn't work":
                        #   1. ov is None        → overlay construction
                        #      never returned (look for "Screenshot
                        #      overlay construction failed" upstream).
                        #   2. ov exists but no tk_root → overlay built
                        #      then destroyed mid-construction.
                        #   3. tk_root exists but not mapped → built but
                        #      Tk never put it on screen (DWM hung,
                        #      foreground app stole focus before map).
                        reason = ('no overlay obj' if ov is None
                                  else ('no tk root' if tk_root is None
                                        else 'tk root not mapped'))
                        _overlay_active[0] = False
                        _pending_overlay[0] = None
                        logger.info(f'Watchdog: reset stuck screenshot flag '
                                    f'(reason: {reason}).')
        except Exception:
            pass

        # ── Whisper recording ─────────────────────────────────────────
        # AudioCapture uses `_recording` (underscore-prefixed); we also
        # check the recording started recently because the audio thread
        # spins up async after start_recording() and may take a tick to
        # flip the flag. Without that grace window the watchdog races
        # the start path and kills the recording within the first 2 s.
        try:
            if self._whisper_recording:
                audio_active = False
                try:
                    audio_active = bool(self._audio
                                        and getattr(self._audio, '_recording', False))
                except Exception:
                    audio_active = False
                started_at = getattr(self, '_whisper_t0', 0.0)
                fresh = (time.time() - started_at) < 5.0 if started_at else True
                if not audio_active and not fresh:
                    self._whisper_recording = False
                    try: self.whisper_overlay.hide()
                    except Exception: pass
                    logger.info('Watchdog: reset stuck whisper recording flag.')
        except Exception:
            pass

        # Grace window: a flag flipped within the last `fresh_secs` seconds
        # is left alone, no matter what the underlying object reports. This
        # prevents the watchdog from racing the start path (set-flag then
        # spin-up-async-worker) and killing a hotkey the user just pressed.
        # The audit said 5 s gives every start path plenty of room.
        def _fresh(start_t: float, fresh_secs: float = 5.0) -> bool:
            return bool(start_t) and (time.time() - start_t) < fresh_secs

        # ── Screen recorder ───────────────────────────────────────────
        try:
            if (self._recorder_state == 'recording'
                    and self._screen_recorder is None
                    and not _fresh(getattr(self, '_recorder_t0', 0.0))):
                self._recorder_state = 'idle'
                try: self._update_library_recorder_state()
                except Exception: pass
                logger.info('Watchdog: reset stuck screen recorder flag.')
        except Exception:
            pass

        # ── GIF recorder ──────────────────────────────────────────────
        try:
            if (self._gif_state in ('recording', 'encoding')
                    and self._gif_recorder is None
                    and not _fresh(getattr(self, '_gif_t0', 0.0))):
                self._gif_state = 'idle'
                try: self._update_library_gif_state()
                except Exception: pass
                logger.info('Watchdog: reset stuck GIF recorder flag.')
        except Exception:
            pass

        # ── Macro recorder ────────────────────────────────────────────
        try:
            if self._macro_state in ('recording', 'playing'):
                rec = getattr(self, '_macro', None)
                alive = bool(rec and (rec.is_recording or rec.is_playing))
                if not alive and not _fresh(getattr(self, '_macro_t0', 0.0)):
                    try: self._set_macro_state('idle')
                    except Exception:
                        self._macro_state = 'idle'
                    logger.info('Watchdog: reset stuck macro state.')
        except Exception:
            pass

        # ── Refine in-flight ──────────────────────────────────────────
        # If the refine_overlay is hidden and the request gen has not
        # advanced in >60 s, the in-flight flag is almost certainly
        # orphaned from a thread that crashed before unlocking.
        try:
            if self._refine_in_progress:
                last_change = getattr(self, '_refine_gen_t', 0.0)
                if last_change and (time.time() - last_change) > 60.0:
                    self._refine_in_progress = False
                    self._refine_gen += 1
                    logger.info('Watchdog: reset orphaned refine in-flight flag.')
        except Exception:
            pass

    def _show_mic_error(self, err: str = '') -> None:
        """Dialog shown when the input stream can't be opened. We try to
        infer the actual cause from the error string so the suggested
        fix matches the failure mode, permissions look very different
        from sample-rate mismatches, "device gone" looks different again.
        """
        import tkinter.messagebox as _mb
        low = (err or '').lower()
        # Permission-style errors (Windows blocks the app, etc.)
        if any(t in low for t in ('access denied', 'permission', 'unauthorized',
                                  'not allowed', '-9985', '-9988')):
            body = (
                'Hotkeys could not access your microphone.\n\n'
                'To fix this:\n'
                '  Windows 11/10 → Settings → Privacy & Security\n'
                '  → Microphone → allow desktop apps to use the mic.\n\n'
                'Then press the hotkey again.'
            )
        # Sample-rate / format issues, by the time we get here, every
        # fallback (system default, native rate, etc.) has been exhausted
        # in core/audio.py. Telling the user "pick the built-in mic" is
        # wrong because that was already tried and also failed.
        elif any(t in low for t in ('sample rate', '-9997', 'invalid sample',
                                    'format', 'paformatisunsupported')):
            body = (
                "Hotkeys couldn't open any working microphone.\n\n"
                'The selected mic AND the system default both refused the '
                'audio format Hotkeys needs.\n\n'
                'Try:\n'
                '  • Plugging a different mic in, or\n'
                '  • Restarting the app you were using your virtual mic '
                'with (DroidCam, Voicemod, etc.).'
            )
        # "Device unavailable", chosen device disappeared AND system
        # default also unreachable (both were attempted by the audio
        # engine before we got here).
        elif any(t in low for t in ('device unavailable', 'no default input',
                                    '-9996', 'device not')):
            body = (
                'The microphone you selected is gone, and the system '
                "default mic also couldn't be opened.\n\n"
                'Plug a mic in, then open Settings to confirm which one '
                'to use.'
            )
        else:
            body = (
                "Hotkeys couldn't open your microphone.\n\n"
                'Check that a mic is plugged in and not in use by another '
                'app, then try again.'
            )
        _mb.showerror('Microphone unavailable', body, parent=self.root)

    def _check_data_dir_writable(self) -> None:
        from storage import appdata_dir
        appdata_dir()   # triggers the write test and sets _permission_warning
        warn = getattr(appdata_dir, '_permission_warning', None)
        if warn:
            import tkinter.messagebox as _mb
            _mb.showwarning('Storage warning', warn, parent=self.root)

    def _show_first_run_tip(self) -> None:
        if self.config.get('first_run_done'):
            return
        self.config['first_run_done'] = True
        threading.Thread(target=save_config, args=(self.config,), daemon=True).start()
        try:
            hk = self._hotkey_cfg()
            refine_hk = hk.get('refine', 'alt+shift+w').upper()
            lib_hk    = hk.get('library', 'alt+shift+e').upper()
            self._tray.notify(
                f'Select text → press {refine_hk} to refine with AI\n'
                f'Press {lib_hk} to open the Library.',
                'Hotkeys is running ⚡',
            )
        except Exception:
            pass

    def _reload_hotkeys_manual(self) -> None:
        """Full reset from tray menu, cancels anything stuck, re-registers hotkeys."""
        logger.info('Manual reload requested from tray.')

        # ── 1. Reload config from disk so hotkeys reflect the latest saved values ──
        try:
            fresh = load_config()
            self.config.update(fresh)
            logger.info('Config reloaded from disk for hotkey reset.')
        except Exception as e:
            logger.warning(f'Config reload failed during manual reset: {e}')

        # ── 2. Close any open GIF setup dialog that may be stuck ──────────────────
        dlg = getattr(self, '_gif_setup_dlg', None)
        if dlg is not None:
            try:
                dlg.win.grab_release()
                dlg.win.destroy()
            except Exception:
                pass
            self._gif_setup_dlg = None

        # ── 3. Close any floating AskPill windows that grabbed the pointer ────────
        # Build a set of permanent app windows to skip, destroying these would
        # corrupt self.library / self.settings with no recovery path.
        _permanent = set()
        for _attr in ('library', 'settings'):
            try:
                _w = getattr(self, _attr, None)
                if _w is not None:
                    _permanent.add(getattr(_w, 'win', None))
            except Exception:
                pass
        for widget in self.root.winfo_children():
            try:
                if widget in _permanent:
                    continue
                if widget.winfo_class() == 'Toplevel':
                    widget.grab_release()
                    widget.destroy()
            except Exception:
                pass

        # ── 4. Cancel any stuck whisper recording ─────────────────────────────────
        try:
            if self._whisper_recording:
                self._whisper_cancel_recording()
        except Exception:
            pass

        # ── 4b. Abort macro recording / playback ──────────────────────────────────
        try:
            if self._macro_state in ('recording', 'playing'):
                self._macro.force_stop()
                self._macro.clear()
                self._macro_unregister_stop_keys()
                self._set_macro_state('idle')
                logger.info('Reload: macro recording/playback aborted')
        except Exception:
            pass

        # ── 4c. Stop active screen recording ──────────────────────────────────────
        try:
            if self._recorder_state == 'recording' and self._screen_recorder is not None:
                rec = self._screen_recorder
                self._screen_recorder = None
                self._recorder_state  = 'idle'
                self._update_library_recorder_state()
                threading.Thread(target=rec.stop, daemon=True).start()
                logger.info('Reload: screen recording force-stopped')
        except Exception:
            pass

        # ── 4d. Stop active GIF recording ─────────────────────────────────────────
        try:
            if self._gif_state in ('recording', 'encoding') and self._gif_recorder is not None:
                rec = self._gif_recorder
                self._gif_recorder = None
                self._gif_state    = 'idle'
                self._update_library_gif_state()
                threading.Thread(target=rec.force_stop, daemon=True).start()
                logger.info('Reload: GIF recording force-stopped')
        except Exception:
            pass

        # ── 4e. Close Quick Notes (save pending content first) ────────────────────
        try:
            if self._notes_win is not None:
                win = self._notes_win
                self._notes_win = None
                try:
                    win._save_and_close()
                except Exception:
                    try:
                        win.destroy()
                    except Exception:
                        pass
                logger.info('Reload: Quick Notes window closed')
        except Exception:
            pass

        # ── 4f. Close Whiteboard (save first) ────────────────────────────────────
        try:
            if self._wb_win is not None:
                win = self._wb_win
                self._wb_win = None
                try:
                    win._save_and_close()
                except Exception:
                    try:
                        win.destroy()
                    except Exception:
                        pass
                logger.info('Reload: Whiteboard window closed')
        except Exception:
            pass

        # ── 4f-2. Close Whiteboard SUBPROCESS (pywebview) ────────────────────────
        # The whiteboard runs in its own pywebview process, separate from the
        # main app. If a previous launch is stuck (frozen UI, debounced save
        # hanging), a plain destroy() of the in-process window above misses it.
        # Find any window whose title matches and post WM_CLOSE so its
        # auto-save flushes before exit.
        if sys.platform == 'win32':
            try:
                import win32gui, win32con
                def _wb_cb(h, _):
                    if win32gui.GetWindowText(h) == 'Whiteboard (Shift+F8)':
                        win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
                win32gui.EnumWindows(_wb_cb, None)
            except Exception as e:
                logger.warning(f'Reload: whiteboard subprocess close failed: {e}')

        # ── 4g. Close any floating AskPills ──────────────────────────────────────
        try:
            self._close_all_ask_pills()
        except Exception:
            pass

        # ── 4h. Cancel any in-flight Refine request ──────────────────────────────
        # Bumping the generation counter makes every pending callback a no-op
        # (the engine threads check it before touching UI state). This frees
        # the user from a hung "Thinking…" pill if the network call is stuck.
        try:
            if self._refine_in_progress:
                self._refine_gen += 1
                self._refine_in_progress = False
                logger.info('Reload: in-flight Refine cancelled')
        except Exception:
            pass

        # ── 4i. Cancel any active F9 Transcribe job ──────────────────────────────
        # The Transcribe panel exposes a threading.Event the worker checks at
        # every step (download / convert / diarize / transcribe / export).
        # Setting it terminates the job cleanly without leaving temp files.
        try:
            panel = getattr(self.library, '_transcribe_panel', None)
            if panel is not None:
                ce = getattr(panel, '_cancel', None)
                if ce is not None:
                    ce.set()
                    logger.info('Reload: F9 transcribe job cancel requested')
        except Exception:
            pass

        # ── 4j. Close the prompt sticky note if it's floating around ─────────────
        try:
            if self._sticky is not None:
                try:
                    self._sticky.destroy()
                except Exception:
                    pass
                self._sticky = None
        except Exception:
            pass

        # ── 4k. Force-release the screenshot overlay singleton flag ──────────────
        # Print Screen claims `screenshot._overlay_active[0] = True` before
        # grabbing the desktop. If the previous overlay crashed, was killed
        # by the user via Esc-on-the-grab-thread, or unwound without
        # touching the flag for any reason, every subsequent PrtSc press
        # silently no-ops on the singleton check and the user thinks the
        # feature is broken. Reload Hotkeys is the user's panic-recovery
        # path, so it should reset this flag too.
        try:
            from screenshot import (_overlay_lock, _overlay_active,
                                    _pending_overlay)
            ov = None
            with _overlay_lock:
                ov = _pending_overlay[0]
                _pending_overlay[0] = None
                _overlay_active[0]  = False
            if ov is not None:
                try: ov.cancel()
                except Exception: pass
            logger.info('Reload: screenshot overlay flag reset.')
        except Exception as e:
            logger.warning(f'Reload: screenshot flag reset failed: {e}')

        # ── 5. Hide all overlays ──────────────────────────────────────────────────
        for ov in (self.refine_overlay, self.whisper_overlay,
                   self.macro_overlay, self.recorder_overlay, self.gif_overlay,
                   self.chain_overlay):
            try:
                ov.hide()
            except Exception:
                pass

        # ── 6. Re-register all hotkeys (global + per-prompt + saved macros) ───────
        self._register_hotkeys()
        self._register_macro_saved_hotkeys()
        self._register_chain_hotkeys()

        # ── 7. Refresh dependent UI so visible labels match new bindings ──────────
        # The Library renders hotkey labels next to each prompt / chain / tab;
        # if the user remapped anything since the last open, those labels are
        # stale until we explicitly refresh. The tray menu is rebuilt so its
        # right-aligned shortcut hints also reflect the latest config.
        try:
            self.library.refresh_hotkeys(self._hotkey_cfg())
        except Exception:
            pass
        try:
            self._update_tray()
        except Exception:
            pass

        self._notify('Hotkeys reset ⚡', 'All hotkeys reloaded and ready.')

    def _schedule_rereg(self, delay_ms: int = 80) -> None:
        """Schedule a hotkey re-registration *delay_ms* after a hotkey fires.

        Called from every hotkey handler (keyboard hook thread) so the
        keyboard library always gets a clean state after each press.
        The 80 ms default gives the OS time to see all key-up events before
        we unhook; _register_hotkeys_bg is a no-op if already in-flight.
        """
        self.root.after(
            delay_ms,
            lambda: threading.Thread(
                target=self._register_hotkeys_bg, daemon=True,
            ).start(),
        )

    def _hk_refine(self) -> None:
        logger.info('Refine hotkey fired.')
        threading.Thread(target=self._capture_and_queue, daemon=True).start()

    def _capture_and_queue(self) -> None:
        # Wait until Alt and Shift are physically released before injecting
        # Ctrl+C.  If they're still held, the target app sees Ctrl+Shift+Alt+C
        # instead of plain Ctrl+C and silently ignores it (→ "select text first").
        # GetAsyncKeyState is used directly so this works even while the
        # keyboard hook is briefly suspended during re-registration.
        if sys.platform == 'win32':
            _u32 = ctypes.windll.user32
            _deadline = time.time() + 0.5
            while time.time() < _deadline:
                if not (_u32.GetAsyncKeyState(0x10) & 0x8000 or   # VK_SHIFT
                        _u32.GetAsyncKeyState(0x12) & 0x8000):    # VK_MENU (Alt)
                    break
                time.sleep(0.015)
        else:
            # macOS: use keyboard.is_pressed, brief wait for modifier release
            try:
                import keyboard as _kb
                _deadline = time.time() + 0.5
                while time.time() < _deadline:
                    if not (_kb.is_pressed('shift') or _kb.is_pressed('alt')):
                        break
                    time.sleep(0.015)
            except Exception:
                pass
        time.sleep(0.04)                       # brief settle after release
        try:
            prev = pyperclip.paste()
        except Exception:
            prev = ''
        try:
            pyperclip.copy('')
        except Exception:
            pass
        copy_selection()
        captured = ''
        for _ in range(25):                   # up to 0.75 s total
            time.sleep(0.03)                  # poll every 30 ms (was 50 ms)
            try:
                current = pyperclip.paste()
            except Exception:
                continue
            if current and current.strip():
                captured = current
                break
        if not captured:
            try:
                pyperclip.copy(prev)
            except Exception:
                pass
        logger.info(f'Captured text ({len(captured)} chars): {captured[:80]!r}')
        self._q.put(('refine', captured))

    def _hk_ask(self) -> None:
        """Shift+F4, capture selected text and show answer pill."""
        threading.Thread(target=self._capture_and_queue_ask, daemon=True).start()

    def _capture_and_queue_ask(self) -> None:
        """Capture question for Shift+F4.

        Priority (REORDERED 2026-05-31 — selected text wins over stale clipboard):
          0. Screenshot overlay with an active drag selection → crop + OCR.
          1. **Selected text** (fresh Ctrl+C copy) → use as the question.
          2. Image in clipboard → OCR it (only if no selection captured).

        The old order had clipboard-image OCR before selection, which meant
        a stale screenshot lurking in the user's clipboard (e.g. from a
        browser tab-strip copy hours ago) would override a freshly-selected
        question like "Why is the sky blue?". Fresh user intent always wins.
        """
        # ── Priority 0: screenshot overlay with active selection ─────────────
        # Check BEFORE the Shift-release wait so the overlay closes immediately.
        from screenshot import _overlay_active, _pending_overlay
        if _overlay_active[0]:
            ov = _pending_overlay[0]
            if ov is not None:
                img = ov.capture_for_ask()
                if img is not None:
                    logger.info('Ask: capturing from screenshot overlay selection')
                    self.root.after(0, ov._close)   # close overlay on main thread
                    try:
                        extractor = self._vision_extractor
                        captured  = extractor(img).strip()
                        logger.info(
                            f'Ask: overlay OCR gave ({len(captured)} chars): {captured[:80]!r}')
                        if _ocr_is_no_text(captured):
                            self._q.put(('ask', _ASK_NO_TEXT))
                        else:
                            self._q.put(('ask', captured))
                        return
                    except Exception as exc:
                        logger.warning(f'Ask: overlay OCR failed ({exc}), falling back')

        # Wait for Shift to release before doing anything with the clipboard
        if sys.platform == 'win32':
            _u32 = ctypes.windll.user32
            _deadline = time.time() + 0.5
            while time.time() < _deadline:
                if not _u32.GetAsyncKeyState(0x10) & 0x8000:   # VK_SHIFT
                    break
                time.sleep(0.015)
        time.sleep(0.04)

        # ── Priority 1: selected text (CHECK FIRST) ─────────────────────────
        # Save whatever's in clipboard (could be a stale image), clear, send
        # Ctrl+C, watch for fresh text to land. If text arrives → that's the
        # user's selection and wins over any stale clipboard image.
        try:
            prev = pyperclip.paste()
        except Exception:
            prev = ''
        try:
            pyperclip.copy('')
        except Exception:
            pass
        copy_selection()
        captured = ''
        for _ in range(25):
            time.sleep(0.03)
            try:
                current = pyperclip.paste()
            except Exception:
                continue
            if current and current.strip():
                captured = current
                break

        if captured:
            # Restore the previous clipboard contents so we don't leak the
            # selection into whatever the user had copied before pressing
            # Shift+F4. (Pyperclip text-only — image clipboard is restored
            # by the OS since Ctrl+C only overwrites the text format.)
            logger.info(f'Ask: selected-text captured ({len(captured)} chars): {captured[:80]!r}')
            try:
                pyperclip.copy(prev)
            except Exception:
                pass
            self._q.put(('ask', captured))
            return

        # No selection captured. Restore prev so the image (if any) survives.
        try:
            pyperclip.copy(prev)
        except Exception:
            pass

        # ── Priority 2: image in clipboard → OCR (fallback only) ────────────
        try:
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
            if img is not None:
                logger.info('Ask: no selection, falling back to clipboard image OCR')
                try:
                    extractor = self._vision_extractor
                    captured  = extractor(img).strip()
                    logger.info(f'Ask: OCR gave ({len(captured)} chars): {captured[:80]!r}')
                    if _ocr_is_no_text(captured):
                        self._q.put(('ask', _ASK_NO_TEXT))
                    else:
                        self._q.put(('ask', captured))
                    return
                except Exception as exc:
                    logger.warning(f'Ask: clipboard OCR failed ({exc}); no question available')
            elif err:
                logger.warning(f'Ask: clipboard image error: {err}')
        except Exception as exc:
            logger.warning(f'Ask: clipboard check failed: {exc}')

        # Nothing usable found.
        logger.info('Ask: nothing to ask about (no selection, no clipboard image)')
        self._q.put(('ask', _ASK_NO_TEXT))

    def _close_all_ask_pills(self) -> None:
        """Close every tracked AskPill. Safe to call from main thread."""
        for pill in list(self._ask_pills):
            try:
                pill._close()
            except Exception:
                pass
        self._ask_pills.clear()

    def _do_ask(self, text: str) -> None:
        """Main-thread handler, open the answer pill."""
        # Close any existing pill before opening a new one, prevents stacking.
        self._close_all_ask_pills()

        def _on_pill_close(pill_ref):
            try:
                self._ask_pills.remove(pill_ref)
            except ValueError:
                pass

        if not text or not text.strip():
            # Match Refine (Alt+Shift+W) behavior: show the simple "No text
            # selected" info pill instead of opening a chat-like AskPill.
            # Less surface, matches user expectation that empty-input hotkeys
            # behave consistently across features.
            self.refine_overlay.show_no_selection()
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if text == _ASK_NO_TEXT:
            # Image had no readable text → tiny toast, same shape as
            # show_no_selection but with the more accurate message.
            try:
                self.refine_overlay.show_error('No text found in image')
            except Exception:
                # Fallback to AskPill if the overlay can't render the error.
                pill = AskPill(self.root, '', self.provider,
                               static='No text found in image')
                pill._on_close = lambda p=pill: _on_pill_close(p)
                self._ask_pills.append(pill)
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if not self.provider.ready:
            self.refine_overlay.show_error('API key required, open Settings')
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        pill = AskPill(self.root, text.strip(), self.provider)
        pill._on_close = lambda p=pill: _on_pill_close(p)
        self._ask_pills.append(pill)
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()

    def _do_web(self) -> None:
        """Open the active bookmark in the default browser."""
        import webbrowser
        from storage import get_active_bookmark
        bm = get_active_bookmark()
        if not bm:
            self.refine_overlay.show_error('No bookmark set, open Web tab to add one')
            return
        url = bm['url']
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    # ── Quick Notes ───────────────────────────────────────────────────────────

    def _do_open_notes(self) -> None:
        """Shift+F7, toggle / restore the Quick Notes overlay."""
        if self._notes_win is not None:
            try:
                if self._notes_win.winfo_exists():
                    # If minimized (withdrawn), restore it
                    if not self._notes_win.winfo_viewable():
                        self._notes_win.deiconify()
                        self._notes_win.lift()
                        return
                    # If visible, close+save it
                    self._notes_win._save_and_close()
                    return
            except Exception:
                pass
            self._notes_win = None

        def _on_close():
            self._notes_win = None
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            # Refresh Notes tab in library if it's currently visible.
            # IMPORTANT: must go through _invalidate_tab('notes') instead
            # of calling _render_notes_tab() directly. The direct render
            # writes into self._scroll, which AT REST is the OUTER
            # CTkScrollableFrame whose children are EVERY tab's container
            # frame. Destroying its children wipes out all tab containers,
            # leaving subsequent tab clicks visually stuck (grid_remove
            # of the stale container refs fails with "bad window path
            # name", and the new container.grid() lands nowhere).
            # _invalidate_tab routes the render through the per-tab
            # container instead, mirroring update_recorder_state.
            try:
                if (self.library and
                        getattr(self.library, '_active_tab', None) == 'notes' and
                        self.library.win.winfo_viewable()):
                    self.root.after(
                        0, lambda: self.library._invalidate_tab('notes'))
            except Exception:
                pass

        def _mic_busy() -> bool:
            """Returns True when any other feature is actively using the microphone."""
            return bool(self._whisper_recording)

        def _on_geometry_change(geo: str) -> None:
            self.config['notes_geometry'] = geo
            threading.Thread(target=save_config, args=(self.config,), daemon=True).start()

        def _on_theme_change(theme: str) -> None:
            self.config['notes_theme'] = theme
            threading.Thread(target=save_config, args=(self.config,), daemon=True).start()

        self._notes_win = QuickNotesWindow(
            self.root,
            transcribe_fn=self._transcriber.transcribe_for_notes,
            on_close=_on_close,
            mic_busy_fn=_mic_busy,
            vision_extractor=self._vision_extractor,
            provider=self.provider,
            initial_geometry=self.config.get('notes_geometry', ''),
            on_geometry_change=_on_geometry_change,
            initial_theme=self.config.get('notes_theme', 'light'),
            on_theme_change=_on_theme_change,
        )

    # ── Transcribe (Shift+F9) ────────────────────────────────────────────────

    def _do_open_transcribe(self) -> None:
        """Open the Library window directly on the Transcribe tab. The tab
        owns the entire transcription pipeline (file/URL → diarized
        transcript + AI summary + multi-format export); we just route the
        user there."""
        try:
            self.library.show()
            self.library._switch_tab('transcribe')
        except Exception as e:
            logger.warning(f'Open Transcribe failed: {e}')
            self._notify('Transcribe', f'Could not open: {e}')

    # ── Whiteboard ────────────────────────────────────────────────────────────

    def _do_open_whiteboard(self) -> None:
        """Shift+F8, toggle the offline-Whiteboard whiteboard subprocess.

        Runs whiteboard.py (pywebview + bundled @whiteboard/whiteboard)
        as its own process, pywebview's edgechromium backend needs to own the
        main thread, so it can't co-host inside this Tk app.

        Toggle semantics:
          • no process    → spawn
          • foreground    → minimize
          • minimized     → restore + foreground
          • background    → foreground
          • dead          → respawn
        """
        import subprocess
        from pathlib import Path as _Path

        # When frozen, sys.executable IS the app exe, re-launch self with a
        # sentinel arg that main() catches early and routes into whiteboard
        # mode (see _maybe_run_whiteboard_mode below). When running from
        # source, just invoke the .py file with python.
        frozen = getattr(sys, 'frozen', False)
        if frozen:
            spawn_cmd = [sys.executable, '--whiteboard']
            spawn_cwd = str(_Path(sys.executable).parent)
        else:
            script = _Path(__file__).resolve().parent / 'whiteboard.py'
            if not script.exists():
                logger.error(f'whiteboard.py missing at {script}')
                return
            spawn_cmd = [sys.executable, str(script)]
            spawn_cwd = str(script.parent)

        # Find the existing window by title, single_instance.py re-execs
        # pythonw so the window owner is a grandchild we don't directly track.
        if sys.platform == 'win32':
            try:
                import win32gui, win32con
                found = []
                # Exact prefix uniquely identifies our pywebview window,
                # avoids matching unrelated Chrome tabs etc.
                WB_TITLE = 'Whiteboard (Shift+F8)'
                def _cb(h, _):
                    if not win32gui.IsWindow(h): return
                    if win32gui.GetWindowText(h) == WB_TITLE:
                        found.append(h)
                win32gui.EnumWindows(_cb, None)
                if found:
                    h = found[0]
                    if win32gui.IsIconic(h):
                        win32gui.ShowWindow(h, win32con.SW_RESTORE)
                        self._force_foreground(h)
                    elif win32gui.GetForegroundWindow() == h:
                        win32gui.ShowWindow(h, win32con.SW_MINIMIZE)
                    else:
                        self._force_foreground(h)
                    return
            except Exception as e:
                logger.warning(f'whiteboard toggle failed, will respawn: {e}')

        # In dev mode, swap python.exe → pythonw.exe to suppress the console
        # flash. Frozen exe is already windowed.
        if not frozen:
            py_lc = spawn_cmd[0].lower()
            if py_lc.endswith('python.exe'):
                pyw = spawn_cmd[0][:-10] + 'pythonw.exe'
                if _Path(pyw).exists():
                    spawn_cmd[0] = pyw
        # Use PowerShell Start-Process as a launcher intermediary. Direct
        # subprocess.Popen with every isolation flag we tried (DETACHED,
        # NEW_PROCESS_GROUP, BREAKAWAY_FROM_JOB, DEVNULL stdio) STILL
        # killed the parent process whenever the child loaded pywebview
        # / Edge WebView2. PowerShell's Start-Process creates the child
        # through a PowerShell intermediary which then exits — the final
        # whiteboard process is orphaned to System and has zero linkage
        # to us. This is the only launch method we found that survives.
        if sys.platform == 'win32':
            arg_str = ' '.join(f'"{a}"' for a in spawn_cmd[1:])
            ps_script = (
                f"Start-Process -FilePath '{spawn_cmd[0]}' "
                + (f"-ArgumentList {arg_str} " if arg_str else '')
                + f"-WindowStyle Hidden -WorkingDirectory '{spawn_cwd}'"
            )
            proc = subprocess.Popen(
                ['powershell', '-NoProfile', '-WindowStyle', 'Hidden',
                 '-Command', ps_script],
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # PowerShell exits within ~1s; we don't track its pid.
            self._wb_proc = proc
            self._wb_proc_pid = None  # actual whiteboard pid is unknown; we find it by window title
        else:
            proc = subprocess.Popen(
                spawn_cmd, cwd=spawn_cwd, close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wb_proc = proc
            self._wb_proc_pid = proc.pid
        logger.info(f'Launched whiteboard via {"powershell intermediary" if sys.platform == "win32" else "direct"} ({"frozen" if frozen else "source"})')

    # ── URL downloader (Ctrl+Alt+D) ─────────────────────────────────────────

    def _hk_download_url(self) -> None:
        """Ctrl+Alt+D, capture URL from selection or clipboard, queue download."""
        threading.Thread(target=self._capture_and_queue_download, daemon=True).start()

    def _capture_and_queue_download(self) -> None:
        """Capture URL the same way Refine / Ask capture text (selection
        first, then clipboard), then validate it's a URL. If no URL is found
        the user gets a clear 'no URL' message — no download attempted."""
        # Wait for modifier release
        if sys.platform == 'win32':
            _u32 = ctypes.windll.user32
            _deadline = time.time() + 0.5
            while time.time() < _deadline:
                if not (_u32.GetAsyncKeyState(0x10) & 0x8000 or  # Shift
                        _u32.GetAsyncKeyState(0x11) & 0x8000 or  # Ctrl
                        _u32.GetAsyncKeyState(0x12) & 0x8000):   # Alt
                    break
                time.sleep(0.015)
        time.sleep(0.04)

        # Selection first
        try:
            prev = pyperclip.paste()
        except Exception:
            prev = ''
        try:
            pyperclip.copy('')
        except Exception:
            pass
        copy_selection()
        captured = ''
        for _ in range(25):
            time.sleep(0.03)
            try:
                cur = pyperclip.paste()
            except Exception:
                continue
            if cur and cur.strip():
                captured = cur
                break

        # Restore clipboard. We extract URLs from BOTH the selection and
        # the previous clipboard text in case the user copied a URL
        # earlier rather than selecting it just now.
        candidates = []
        if captured.strip():
            candidates.append(captured.strip())
        if prev and prev.strip() and prev.strip() != captured.strip():
            candidates.append(prev.strip())
        try:
            pyperclip.copy(prev)
        except Exception:
            pass

        import re as _re
        url_re = _re.compile(r'https?://[^\s<>"\'\)]+', _re.IGNORECASE)
        url = None
        for src in candidates:
            m = url_re.search(src)
            if m:
                url = m.group(0).rstrip('.,;:!?)]}')
                break

        if not url:
            logger.info('Download URL: no URL found in selection or clipboard')
            self._q.put(('download_url', _DOWNLOAD_NO_URL))
            return

        logger.info(f'Download URL queued: {url[:80]}')
        self._q.put(('download_url', url))

    def _do_download_url(self, url) -> None:
        """Main-thread handler: kicks off the yt-dlp download on a worker
        thread so we never block the UI. Pill progresses 0→1.0 then
        flips to 'Saved' / 'Failed' when the worker reports back."""
        if url is _DOWNLOAD_NO_URL:
            # Reuse the refine overlay for the simple toast
            try:
                self.refine_overlay.show_error('No URL in selection / clipboard')
            except Exception:
                pass
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if not isinstance(url, str):
            return

        # Dedupe in-flight downloads — spamming Ctrl+Alt+D on the same
        # selected URL would otherwise spawn N parallel yt-dlp workers,
        # each writing to the same outtmpl. With nooverwrites=True the
        # extras get `(1)`, `(2)` suffixes, so you'd end up with multiple
        # copies of the same video.
        if not hasattr(self, '_downloads_in_flight'):
            self._downloads_in_flight = set()
            self._downloads_lock = threading.Lock()
        with self._downloads_lock:
            if url in self._downloads_in_flight:
                try:
                    self.refine_overlay.show_error('Already downloading this URL')
                except Exception:
                    pass
                threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
                return
            self._downloads_in_flight.add(url)

        # Start the visual pill before kicking off the download so the user
        # sees feedback immediately even on slow disks.
        try:
            self.refine_overlay.show_download_starting()
        except Exception:
            pass

        threading.Thread(
            target=self._download_url_worker,
            args=(url,),
            daemon=True,
            name='url-download',
        ).start()
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()

    def _download_url_worker(self, url: str) -> None:
        """Background download via yt-dlp into ~/Downloads."""
        try:
            from pathlib import Path as _P
            import re as _re, time as _time
            dest_dir = _P.home() / 'Downloads'
            dest_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f'Download URL: {url[:80]} → {dest_dir}')

            # Sweep orphaned per-stream fragments from a previous run that
            # was killed mid-merge — files like "Title [id].f137.mp4" and
            # ".f140.m4a" sitting next to the merged file (or alone).
            try:
                cutoff = _time.time() - 24 * 3600
                frag_re = _re.compile(r'\.f\d+\.(mp4|m4a|webm|opus|aac)$', _re.I)
                for f in dest_dir.iterdir():
                    if f.is_file() and frag_re.search(f.name) and f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                        logger.info(f'Swept stale fragment: {f.name}')
            except Exception as sweep_exc:
                logger.debug(f'Fragment sweep skipped: {sweep_exc}')

            # Best video+audio (MP4) — same preset Transcribe's Shift+F9
            # uses for "download original media" mode.
            fmt = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

            def _progress(p: float) -> None:
                self.root.after(0, self.refine_overlay.show_download_progress, p)

            def _phase(label: str) -> None:
                if label == 'merging':
                    self.root.after(0, self.refine_overlay.show_download_merging)

            from transcribe.youtube import download_url as _dl
            out_path = _dl(url, dest_dir, fmt,
                           on_progress=_progress, on_log=None, on_phase=_phase)

            name = out_path.name if hasattr(out_path, 'name') else str(out_path).rsplit('\\', 1)[-1]
            self.root.after(0, self.refine_overlay.show_download_done, name)
            logger.info(f'Download URL: complete → {out_path}')
        except Exception as exc:
            msg = str(exc)
            if len(msg) > 80:
                msg = msg[:77] + '…'
            logger.warning(f'Download URL failed: {exc}')
            self.root.after(0, self.refine_overlay.show_error,
                            f'Download failed: {msg}')
        finally:
            try:
                with self._downloads_lock:
                    self._downloads_in_flight.discard(url)
            except Exception:
                pass

    def _do_open_audio_editor(self) -> None:
        """Shift+F10, toggle the bundled audio editor (Tenacity portable).

        Launched as a sibling process. The launcher relabels the upstream
        window title to "Audio Editor" so the front UI is brand-clean.
        Toggle semantics mirror the whiteboard, see audio_editor.py.
        Pass our Tk root so the launcher can pop a "drop file here"
        hint overlay over the editor on first launch.
        """
        try:
            import audio_editor
            audio_editor.toggle(tk_root=self.root)
        except Exception as e:
            logger.error(f'audio editor toggle failed: {e}')

    @staticmethod
    def _is_whiteboard_foreground() -> bool:
        """True when the offline-Whiteboard whiteboard owns the foreground.

        Used to gate global app hotkeys: while the user is drawing /
        text-editing in the whiteboard, the app's hotkeys (refine, whisper,
        per-prompt F-keys, macros, etc.) should silently no-op so they
        don't double-fire alongside Whiteboard's own shortcuts. Shift+F8
        itself is NOT gated, toggling the window must always work.
        """
        if sys.platform != 'win32':
            return False
        try:
            import win32gui
            h = win32gui.GetForegroundWindow()
            return win32gui.GetWindowText(h) == 'Whiteboard (Shift+F8)'
        except Exception:
            return False

    @staticmethod
    def _force_foreground(hwnd) -> None:
        """SetForegroundWindow with the AttachThreadInput workaround, Windows
        ignores plain SetForegroundWindow from a non-foreground process unless
        we briefly attach to the current foreground thread's input queue.

        Critically, we ONLY call ShowWindow(SW_RESTORE) if the target window
        is currently minimised. Calling SW_RESTORE on a maximised window
        un-maximises it (Windows treats SW_RESTORE as "previous non-iconic
        state"), which is exactly the "title bar double-click resize" effect
        the user sees after every Ctrl+Enter on a maximised editor.
        """
        try:
            import ctypes
            u, k = ctypes.windll.user32, ctypes.windll.kernel32
            fg = u.GetForegroundWindow()
            fg_t = u.GetWindowThreadProcessId(fg, None) if fg else 0
            cur = k.GetCurrentThreadId()
            attached = False
            if fg_t and fg_t != cur:
                attached = bool(u.AttachThreadInput(cur, fg_t, True))
            u.BringWindowToTop(hwnd)
            # Only restore if the window is actually minimised (iconic);
            # otherwise leave its size alone. This preserves maximised
            # state and prevents the "window shrinks after every paste" bug.
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)  # SW_RESTORE
            u.SetForegroundWindow(hwnd)
            if attached:
                u.AttachThreadInput(cur, fg_t, False)
        except Exception as e:
            logger.warning(f'_force_foreground: {e}')

    # ── Chain hotkey ─────────────────────────────────────────────────────────

    def _hk_chain(self) -> None:
        """Shift+F6 (or per-chain hotkey), capture text and run active chain."""
        logger.info('Chain hotkey fired.')
        threading.Thread(target=self._capture_and_queue_chain, daemon=True).start()

    def _capture_and_queue_chain(self) -> None:
        """Same modifier-release wait + Ctrl+C logic as _capture_and_queue."""
        if sys.platform == 'win32':
            _u32 = ctypes.windll.user32
            _deadline = time.time() + 0.5
            while time.time() < _deadline:
                if not (_u32.GetAsyncKeyState(0x10) & 0x8000 or   # VK_SHIFT
                        _u32.GetAsyncKeyState(0x12) & 0x8000):    # VK_MENU (Alt)
                    break
                time.sleep(0.015)
        else:
            try:
                import keyboard as _kb
                _deadline = time.time() + 0.5
                while time.time() < _deadline:
                    if not (_kb.is_pressed('shift') or _kb.is_pressed('alt')):
                        break
                    time.sleep(0.015)
            except Exception:
                pass
        time.sleep(0.04)
        try:
            prev = pyperclip.paste()
        except Exception:
            prev = ''
        try:
            pyperclip.copy('')
        except Exception:
            pass
        copy_selection()
        captured = ''
        for _ in range(25):
            time.sleep(0.03)
            try:
                current = pyperclip.paste()
            except Exception:
                continue
            if current and current.strip():
                captured = current
                break
        if not captured:
            try:
                pyperclip.copy(prev)
            except Exception:
                pass
        logger.info(f'Chain: captured text ({len(captured)} chars): {captured[:80]!r}')
        self._q.put(('chain', captured))

    def _do_chain(self, text: str) -> None:
        """Main-thread handler, find active chain and start runner thread."""
        if self._refine_in_progress:
            self.chain_overlay.show_error('Refine in progress, wait for it to finish')
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if self._whisper_recording:
            self.chain_overlay.show_error('Recording in progress, stop first')
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        self.chains = load_chains()
        active_chain = next((c for c in self.chains if c.get('active')), None)
        if not self.chains or active_chain is None:
            self.chain_overlay.show_error('No active chain, open Chains tab to set one')
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if not text or not text.strip():
            self.chain_overlay.show_no_selection()
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if not self.provider.ready:
            self.chain_overlay.show_error('API key required, open Settings')
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        threading.Thread(
            target=self._run_chain,
            args=(active_chain, text.strip()),
            daemon=True,
        ).start()

    def _run_chain(self, chain: dict, text: str) -> None:
        """Background thread, execute all steps sequentially."""
        steps = chain.get('steps', [])
        if not steps:
            self.root.after(0, lambda: self.chain_overlay.show_error('Chain has no steps'))
            return
        current_text = text
        try:
            for i, step in enumerate(steps):
                lbl = step.get('label', f'Step {i + 1}')
                self.root.after(
                    0,
                    lambda i=i, lbl=lbl: self.chain_overlay.show_chain_step(
                        i + 1, len(steps), lbl
                    ),
                )
                result = self.provider.refine(current_text, step['prompt'])
                if not result or not result.strip():
                    raise RuntimeError(f'Step {i + 1} ({lbl}) returned empty response')
                current_text = result.strip()
            # All steps done, paste result
            name = chain.get('name', 'Chain')
            self.root.after(0, lambda: self.chain_overlay.show_chain_done(name))
            pyperclip.copy(current_text)
            self.root.after(40, paste_from_clipboard)
            self.root.after(150, self._reregister_after_action)
            logger.info(f'Chain "{name}" complete, {len(steps)} steps')
        except Exception as ex:
            logger.error(f'Chain error: {ex}')
            from engine import friendly_error_message
            err_msg = friendly_error_message(
                ex, feature='Chain',
                active_provider=self.config.get('active_provider', ''))
            self.root.after(0, lambda e=err_msg: self.chain_overlay.show_error(e))
            self.root.after(0, self._reregister_after_action)

    def _do_chain_named(self, chain: dict) -> None:
        """Main-thread handler for per-chain hotkeys, captures text then runs that chain."""
        # Reuse the same capture flow but run a specific chain
        threading.Thread(
            target=self._capture_and_queue_chain_named,
            args=(chain,),
            daemon=True,
        ).start()

    def _capture_and_queue_chain_named(self, chain: dict) -> None:
        """Same as _capture_and_queue_chain but dispatches to run a specific chain."""
        if sys.platform == 'win32':
            _u32 = ctypes.windll.user32
            _deadline = time.time() + 0.5
            while time.time() < _deadline:
                if not (_u32.GetAsyncKeyState(0x10) & 0x8000 or
                        _u32.GetAsyncKeyState(0x12) & 0x8000):
                    break
                time.sleep(0.015)
        else:
            try:
                import keyboard as _kb
                _deadline = time.time() + 0.5
                while time.time() < _deadline:
                    if not (_kb.is_pressed('shift') or _kb.is_pressed('alt')):
                        break
                    time.sleep(0.015)
            except Exception:
                pass
        time.sleep(0.04)
        try:
            prev = pyperclip.paste()
        except Exception:
            prev = ''
        try:
            pyperclip.copy('')
        except Exception:
            pass
        copy_selection()
        captured = ''
        for _ in range(25):
            time.sleep(0.03)
            try:
                current = pyperclip.paste()
            except Exception:
                continue
            if current and current.strip():
                captured = current
                break
        if not captured:
            try:
                pyperclip.copy(prev)
            except Exception:
                pass
        if not captured or not captured.strip():
            self.root.after(0, lambda: self.chain_overlay.show_no_selection())
            self.root.after(0, lambda: threading.Thread(
                target=self._register_hotkeys_bg, daemon=True).start())
            return
        if not self.provider.ready:
            self.root.after(0, lambda: self.chain_overlay.show_error(
                'API key required, open Settings'))
            self.root.after(0, lambda: threading.Thread(
                target=self._register_hotkeys_bg, daemon=True).start())
            return
        threading.Thread(
            target=self._run_chain,
            args=(chain, captured.strip()),
            daemon=True,
        ).start()

    def _on_chains_changed_cb(self) -> None:
        """Called by LibraryWindow when a chain is added/edited/deleted.
        Reloads chain data and re-registers per-chain hotkeys."""
        self.chains = load_chains()
        self._register_chain_hotkeys()

    def _register_chain_hotkeys(self) -> None:
        """Re-register per-chain playback hotkeys (chains with a hotkey field set)."""
        for hk in self._chain_saved_hks:
            try:
                kbhook.remove_hotkey(hk)
            except Exception:
                pass
        self._chain_saved_hks = []
        self.chains = load_chains()
        for chain in self.chains:
            hk = chain.get('hotkey', '').strip()
            if not hk:
                continue
            cname = chain.get('name', 'Chain')
            try:
                handle = kbhook.add_hotkey(
                    hk,
                    lambda c=chain: self._q.put(('chain_named', c)),
                )
                self._chain_saved_hks.append(handle)
                logger.info(f'Chain hotkey registered: {hk!r} -> "{cname}"')
            except Exception as e:
                logger.warning(f'Could not register chain hotkey {hk!r}: {e}')

    def _hk_undo_refine(self) -> None:
        self._q.put(('undo_refine', None))

    def _hk_library(self) -> None:
        self._q.put(('library', None))

    def _hk_whisper(self) -> None:
        if not self._whisper_recording:
            self._q.put(('whisper:start', None))
        else:
            self._q.put(('whisper:stop', None))

    def _hk_screenshot(self) -> None:
        self.root.after(0, lambda: take_screenshot(
            self.root,
            on_extract_text=self._screenshot_extract_text,
            on_translate=self._screenshot_translate,
        ))

    def _screenshot_extract_text(self, img) -> None:
        """Context-menu "Extract text" from screenshot. OCR the image,
        copy result to clipboard, show in result popup."""
        threading.Thread(
            target=self._screenshot_ocr_worker,
            args=(img, False),
            daemon=True, name='screenshot-ocr',
        ).start()

    def _screenshot_translate(self, img) -> None:
        """Context-menu "Translate to English" from screenshot. OCR the image,
        translate the extracted text via the configured LLM provider, copy
        result to clipboard, show in result popup."""
        threading.Thread(
            target=self._screenshot_ocr_worker,
            args=(img, True),
            daemon=True, name='screenshot-translate',
        ).start()

    def _screenshot_ocr_worker(self, img, translate: bool) -> None:
        """Background worker: OCR image -> (optionally) translate -> show popup.
        Runs off the Tk thread because the Groq vision API and provider.refine
        are both network-bound."""
        title = 'Translate to English' if translate else 'Extract text'
        kind = 'translate' if translate else 'extract'

        # ── Dedupe rapid identical clicks ────────────────────────────────────
        # Hash the (resized) pixel bytes so spam-clicking Translate on the same
        # selection doesn't spawn N parallel vision API calls. Each request
        # costs real money + risks rate limits, so an in-flight guard pays for
        # itself the first time the user double-clicks the menu.
        if not hasattr(self, '_screenshot_in_flight'):
            self._screenshot_in_flight: set = set()
            self._screenshot_lock = threading.Lock()
        try:
            import hashlib as _hl
            thumb = img.resize((64, 64)) if hasattr(img, 'resize') else img
            key = _hl.sha1(thumb.tobytes()).hexdigest() + f':{int(translate)}'
        except Exception:
            key = None
        if key is not None:
            with self._screenshot_lock:
                if key in self._screenshot_in_flight:
                    self.root.after(0, self.refine_overlay.show_error,
                                    'Already processing this selection')
                    return
                self._screenshot_in_flight.add(key)

        # ── Downscale oversized selections ───────────────────────────────────
        # Full-screen 4K captures push the base64 payload past Groq's vision
        # context budget. 4 MP (≈ 2000×2000) keeps text crisp enough for OCR
        # while shaving the encoded request from ~10 MB down to ~1 MB.
        try:
            from PIL import Image as _Im  # noqa: F401
            w, h = img.size
            mp = (w * h) / 1_000_000
            if mp > 4.0:
                scale = (4.0 / mp) ** 0.5
                new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
                img = img.resize((new_w, new_h), resample=getattr(__import__('PIL').Image, 'LANCZOS', 1))
                logger.info(f'Screenshot downscaled {w}x{h} → {new_w}x{new_h} '
                            f'(was {mp:.1f} MP, now ≤4 MP) for vision API')
        except Exception:
            pass

        try:
            # In-flight pill so the user knows something is happening.
            self.root.after(0, lambda: self.refine_overlay.show_screenshot_working(kind))
        except Exception:
            pass
        try:
            extract = self._vision_extractor
            text = (extract(img) or '').strip()
        except Exception as exc:
            msg = str(exc).lower()
            # Friendlier copy for the two failure modes users actually hit:
            # rate limits and offline. Generic exceptions fall through to the
            # raw message so weird upstream errors are still surfaced.
            if '429' in msg or 'rate limit' in msg or 'quota' in msg:
                friendly = 'OCR rate-limited, try again in ~1 min'
            elif any(k in msg for k in ('connection', 'timeout', 'unreachable',
                                         'name resolution', 'getaddrinfo')):
                friendly = 'OCR offline — check your internet'
            else:
                friendly = f'OCR failed: {str(exc)[:60]}'
            logger.warning(f'Screenshot OCR failed: {exc}')
            self.root.after(0, self.refine_overlay.show_error, friendly)
            if key is not None:
                with self._screenshot_lock:
                    self._screenshot_in_flight.discard(key)
            return
        # Vision models love to fill empty-image cases with a description
        # like "There is no text in the image." Treat the canonical sentinel
        # plus a handful of common phrasings as "no text found" so we hit
        # the proper pill instead of trying to translate a description.
        _NO_TEXT_PHRASES = (
            'no_text_found',
            'no text found',
            'there is no text',
            'there are no text',
            'no readable text',
            'no visible text',
            'image contains no text',
            'image does not contain',
            'this image contains no',
            'the image is blank',
        )
        text_l = text.lower().strip(' .!?"\'')
        if not text or text_l in _NO_TEXT_PHRASES or any(p in text_l for p in _NO_TEXT_PHRASES):
            self.root.after(0, self.refine_overlay.show_error,
                            'No text found in selection')
            if key is not None:
                with self._screenshot_lock:
                    self._screenshot_in_flight.discard(key)
            return

        # ── Skip translate when OCR returns code-like text ───────────────────
        # Translating JavaScript / Python / shell into "English" is meaningless
        # and tends to mangle identifiers. Detect heavy brace + symbol density
        # and force the extract-only path in that case. The user still gets
        # the OCR'd code on the clipboard and in the result, just not a
        # nonsensical "translation".
        looks_like_code = False
        if translate:
            import re as _re
            # Code indicators: braces, semicolons, arrow/equals, common
            # keywords. We require BOTH high symbol density AND a code
            # keyword to avoid false positives on natural text that happens
            # to contain a stray symbol.
            symbol_chars = sum(1 for c in text if c in '{};()<>=[]')
            symbol_ratio = symbol_chars / max(1, len(text))
            kw_re = _re.compile(
                r'\b(function|def|class|return|import|const|let|var|if|else|'
                r'elif|while|for|public|private|static|void|int|string|bool|'
                r'console\.log|print|System\.out|null|None|undefined|true|false)\b'
            )
            has_kw = bool(kw_re.search(text))
            if symbol_ratio > 0.08 and has_kw:
                looks_like_code = True
                logger.info('Screenshot translate: source looks like code '
                            f'(symbol_ratio={symbol_ratio:.2f}), skipping translate')

        if translate and not looks_like_code:
            try:
                # Use the active LLM provider to translate. The prompt is
                # written to handle the already-English case cleanly: most
                # OCR'd captures will be non-English, but a user may
                # absent-mindedly translate an English signpost, and we
                # don't want the model to paraphrase or "improve" it.
                prompt = (
                    'You are a translator. The user will give you some text. '
                    'If the text is in English, output it exactly as-is, '
                    'unchanged, character-for-character. If the text is in any '
                    'other language, translate it to English literally. '
                    'Output ONLY the result. No commentary, no quotes, no '
                    '"Translation:" prefix, no explanation.'
                )
                translated = self.provider.refine(text, prompt)
                translated = (translated or '').strip()
                # If the model returned nothing useful, fall back to the OCR
                # text so the user at least sees the recognised characters.
                text = translated or text
            except Exception as exc:
                msg = str(exc).lower()
                if '429' in msg or 'rate limit' in msg or 'quota' in msg:
                    friendly = 'Translate rate-limited, try again in ~1 min'
                elif any(k in msg for k in ('connection', 'timeout', 'unreachable',
                                             'name resolution', 'getaddrinfo')):
                    friendly = 'Translate offline — check your internet'
                else:
                    friendly = f'Translate failed: {str(exc)[:60]}'
                logger.warning(f'Screenshot translate failed: {exc}')
                self.root.after(0, self.refine_overlay.show_error, friendly)
                if key is not None:
                    with self._screenshot_lock:
                        self._screenshot_in_flight.discard(key)
                return
        if translate and looks_like_code:
            # Rewrite the title so the popup header makes sense — the user
            # asked to translate but we deliberately skipped it.
            title = 'Extract text (code detected, not translated)'

        # Copy to clipboard regardless of how we surface the result.
        try:
            import pyperclip as _pc
            _pc.copy(text)
        except Exception:
            pass
        # Dismiss the "in flight" pill before opening the result UI so the
        # two pills never overlap on screen.
        try:
            self.root.after(0, self.refine_overlay.close)
        except Exception:
            pass
        # Tweet-length threshold (280, single line) → reuse the Ask Claude
        # (Shift+F4) AskPill so styling and dwell match exactly. Anything
        # longer routes to the scrollable popup window which handles
        # paragraph-sized content better than a floating pill.
        _PILL_MAX_CHARS = 280
        is_short = (len(text) <= _PILL_MAX_CHARS) and ('\n' not in text.strip())
        if is_short:
            self.root.after(0, lambda: self._show_screenshot_pill(title, text))
        else:
            self.root.after(0, lambda: self._show_screenshot_result(title, text))
        if key is not None:
            with self._screenshot_lock:
                self._screenshot_in_flight.discard(key)

    def _show_screenshot_pill(self, title: str, text: str) -> None:
        """Render the short-form screenshot translation/extract result as an
        AskPill, identical look-and-feel to a Shift+F4 Ask Claude answer.

        Reuses the existing pill stack lifecycle (self._ask_pills) so the
        user can dismiss multiple stacked pills the same way."""
        def _on_close(pill_ref):
            try:
                self._ask_pills.remove(pill_ref)
            except ValueError:
                pass
        try:
            pill = AskPill(
                self.root,
                question=title,
                provider=self.provider,
                prepared_answer=text,
            )
            pill._on_close = lambda p=pill: _on_close(p)
            self._ask_pills.append(pill)
        except Exception as exc:
            logger.exception(f'Screenshot pill failed: {exc}')

    def _show_screenshot_result(self, title: str, text: str) -> None:
        """Floating result window with the extracted/translated text. Scrollable,
        selectable, copy + close buttons. Already copied to clipboard."""
        try:
            from theme import SURFACE, BORDER, TEXT_P, TEXT_S, ACCENT, ACCENTL, FONT_FAMILY, FONT_MONO
            from win_geometry import center_on_work_area
            win = ctk.CTkToplevel(self.root)
            win.title(f'Hotkeys — {title}')
            win.configure(fg_color=SURFACE)
            # Match the Quick Notes / Whiteboard default size + centering
            # so every popup feels like the same app instead of random
            # corner toasts. center_on_work_area clamps the requested
            # size to the current monitor's work area so it always fits.
            _W, _H = 1216, 796
            x, y, W, H = center_on_work_area(_W, _H)
            win.geometry(f'{W}x{H}+{x}+{y}')
            win.attributes('-topmost', True)
            try: win.after(2000, lambda: win.attributes('-topmost', False))
            except Exception: pass

            header = ctk.CTkLabel(
                win, text=title,
                font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_P,
                anchor='w',
            )
            header.pack(fill='x', padx=14, pady=(12, 4))
            sub = ctk.CTkLabel(
                win, text='Already copied to clipboard — paste anywhere.',
                font=(FONT_FAMILY, 10), text_color=TEXT_S, anchor='w',
            )
            sub.pack(fill='x', padx=14, pady=(0, 8))

            txt = ctk.CTkTextbox(
                win, fg_color='#0e0e0e', text_color=TEXT_P,
                font=(FONT_MONO, 11), border_color=BORDER, border_width=1,
                wrap='word',
            )
            txt.pack(fill='both', expand=True, padx=14, pady=(0, 8))
            txt.insert('1.0', text)

            btns = ctk.CTkFrame(win, fg_color='transparent')
            btns.pack(fill='x', padx=14, pady=(0, 14))
            def _copy_again():
                try:
                    import pyperclip as _pc
                    _pc.copy(txt.get('1.0', 'end').rstrip('\n'))
                except Exception: pass
            def _save_note():
                try:
                    from storage import load_notes, save_notes
                    import uuid as _uuid
                    from datetime import datetime as _dt
                    notes = load_notes()
                    notes.append({
                        'id': str(_uuid.uuid4()),
                        'text': f'[{title}]\n\n{text}',
                        'items': [{'text': '', 'checked': False}],
                        'voice': '', 'color': None, 'pinned': False,
                        'created_at': _dt.now().isoformat(timespec='seconds'),
                    })
                    save_notes(notes)
                except Exception as exc:
                    logger.warning(f'Screenshot result save-to-notes failed: {exc}')
                    try:
                        save_btn.configure(text='✗ Save failed')
                    except Exception: pass
                    return
                # Confirm so the user sees the action took effect. Button
                # flips to "✓ Saved" briefly, then resets so they can save
                # again if they want.
                try:
                    save_btn.configure(text='✓ Saved to Notes', state='disabled')
                    def _reset_btn():
                        try:
                            save_btn.configure(text='Save to Notes', state='normal')
                        except Exception: pass
                    win.after(1800, _reset_btn)
                except Exception: pass
                # Also nudge an open Notes window to refresh its list so
                # the new entry shows immediately without reopening.
                try:
                    nw = getattr(self, '_notes_win', None)
                    if nw is not None and nw.winfo_exists():
                        nw.after(0, nw._refresh_list)
                        try: nw._invalidate_notes_cache()
                        except Exception: pass
                except Exception: pass
            copy_btn = ctk.CTkButton(btns, text='Copy', width=80, fg_color=ACCENT,
                                     hover_color=ACCENTL, command=_copy_again)
            copy_btn.pack(side='left')
            save_btn = ctk.CTkButton(btns, text='Save to Notes', width=140,
                                     fg_color='#2a2a2a', hover_color='#3a3a3a',
                                     command=_save_note)
            save_btn.pack(side='left', padx=(8, 0))
            ctk.CTkButton(btns, text='Close', width=80, fg_color='#2a2a2a',
                          hover_color='#3a3a3a', command=win.destroy).pack(side='right')

            # Same brief "✓ Copied" affordance for the Copy button so both
            # actions feel symmetric — without it Copy also looked like a
            # no-op even though it was working.
            def _copy_with_feedback():
                _copy_again()
                try:
                    copy_btn.configure(text='✓ Copied', state='disabled')
                    def _reset():
                        try: copy_btn.configure(text='Copy', state='normal')
                        except Exception: pass
                    win.after(1500, _reset)
                except Exception: pass
            copy_btn.configure(command=_copy_with_feedback)
        except Exception as exc:
            logger.exception(f'Screenshot result popup failed: {exc}')

    def _do_cancel_screenshot(self) -> None:
        """Cancel the active screenshot overlay. Called on main thread via _poll."""
        from screenshot import cancel_screenshot
        cancel_screenshot()

    def _hk_escape(self) -> None:
        # Screenshot overlay has top priority, Esc must always dismiss it,
        # even if the grab is still in flight (main thread not yet blocked).
        from screenshot import _overlay_active
        if _overlay_active[0]:
            self._q.put(('screenshot:cancel', None))
            return
        # Macro takes priority, stop recording/playback first.
        if self._macro_state in ('recording', 'playing'):
            self._q.put(('macro:stop', None))
            return
        # GIF recording, Esc aborts capture.
        if self._gif_state == 'recording':
            self._q.put(('gif:toggle', None))   # stop → encode → save dialog
            return
        if self._whisper_recording:
            self._q.put(('whisper:cancel', None))
            return
        # Nothing active, close any floating AskPills.
        # (Pills no longer register their own global escape hook because
        # keyboard.unhook_all() inside _register_hotkeys would nuke them.)
        if self._ask_pills:
            self._q.put(('ask:close_all', None))

    # ── Per-prompt hotkey handler ─────────────────────────────────────────────

    def _on_prompt_hotkey(self, idx: int) -> None:
        """Called on main thread when a per-prompt hotkey fires.

        Activates the prompt and opens (or replaces) the floating sticky note.
        """
        if idx >= len(self.prompts):
            return
        prompt = self.prompts[idx]

        # 1. Activate via library._select, updates active_idx, highlight, header
        #    label, and fires on_select (which sets self.active_prompt) all at once.
        try:
            self.library._select(idx)
        except Exception:
            self._on_prompt_selected(prompt)   # fallback if library isn't built yet

        # Guard: if the tracked sticky window no longer exists (destroyed externally,
        # or mid-close flash), clear the stale reference so we don't get stuck.
        if self._sticky is not None:
            try:
                alive = self._sticky.win.winfo_exists()
            except Exception:
                alive = False
            if not alive:
                self._sticky     = None
                self._sticky_idx = None

        # 2. If the SAME prompt's note is already open, apply & close it (toggle).
        #    Pressing F1 → F1 is the quick "confirm and continue" flow.
        if self._sticky is not None and self._sticky_idx == idx:
            try:
                self._sticky.close()
            except Exception:
                self._sticky.destroy()
            return

        # 2b. Different prompt's note is open, replace it silently.
        if self._sticky is not None:
            try:
                self._sticky.destroy()
            except Exception:
                pass
            self._sticky = None
            self._sticky_idx = None

        # 3. Save callback: write changes back to prompts list + disk
        def _on_note_save(updated: dict) -> None:
            # Guard: prompt may have been deleted while the note was open
            if idx >= len(self.prompts):
                logger.warning(f'Sticky note save: prompt[{idx}] no longer exists, discarding')
                return
            updated['hotkey'] = self.prompts[idx].get('hotkey', '')
            self.prompts[idx] = updated
            self.active_prompt = updated
            # File I/O off the main thread
            threading.Thread(
                target=save_prompts, args=(list(self.prompts),), daemon=True,
            ).start()
            # Always sync the library's prompt list so it's current next open
            try:
                self.library.prompts = self.prompts
                if self.library.win.winfo_ismapped():
                    self.library._render_cards()
            except Exception:
                pass
            logger.info(f'Sticky note saved changes to prompt[{idx}] {updated["title"]!r}')

        # 4. on_close: clear self._sticky / _sticky_idx so future hotkey presses
        #    don't try to destroy an already-gone window.
        def _on_note_close() -> None:
            self._sticky     = None
            self._sticky_idx = None

        # 5. Open sticky note
        self._sticky_idx = idx
        self._sticky = PromptStickyNote(
            self.root, prompt, on_save=_on_note_save, on_close=_on_note_close,
            vision_extractor=self._vision_extractor,
        )
        logger.info(f'Prompt hotkey fired → [{idx}] {prompt["title"]!r}')

    # ── History callbacks ─────────────────────────────────────────────────────

    def _on_history_cleared(self) -> None:
        self._history = []

    # ── Refine callbacks ──────────────────────────────────────────────────────

    def _on_prompt_selected(self, prompt: dict) -> None:
        self.active_prompt = prompt

    def _on_prompts_saved(self, prompts: list) -> None:
        self.prompts = prompts
        self._at_default_prompts = False   # any edit re-enables Restore Default Prompts
        self._update_tray()
        # Save to disk in background, no need to block the UI thread for file I/O
        threading.Thread(target=save_prompts, args=(prompts,), daemon=True).start()
        if prompts and self.active_prompt not in prompts:
            self.active_prompt = prompts[0]
        # Re-register hotkeys in background: _register_hotkeys() has a 150 ms
        # sleep inside it (OS hook flush), running it here would freeze the UI.
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()

    def _on_folders_changed(self, folders: list, folder_colors: dict | None = None) -> None:
        self.folders = folders
        self.config['folders'] = folders
        if folder_colors is not None:
            self.folder_colors = folder_colors
            self.config['folder_colors'] = folder_colors
        threading.Thread(target=save_config, args=(self.config,), daemon=True).start()

    def _register_hotkeys_bg(self) -> None:
        """Thread-safe wrapper, guarantees the latest prompt list is always applied.

        Uses a pending flag so rapid saves (e.g. drag-reorder + edit in quick
        succession) never silently lose a registration: if the lock is busy the
        flag is set, and the in-flight run loops once more after finishing.
        """
        if not self._hk_reg_lock.acquire(blocking=False):
            self._hk_reg_pending = True   # in-flight run will re-register after
            return
        try:
            while True:
                self._hk_reg_pending = False
                self._register_hotkeys()
                if not self._hk_reg_pending:
                    break   # nothing changed while we were registering
            # Always re-register saved-macro and chain hotkeys after _register_hotkeys()
            # because unhook_all() inside it wipes them out.
            self._register_macro_saved_hotkeys()
            self._register_chain_hotkeys()
        finally:
            self._hk_reg_lock.release()
        # Push the new hotkey config to the LibraryWindow so its cached
        # header label / hint bar / tab tooltips update immediately. Tk
        # widgets must be touched from the main thread, so marshal via
        # root.after(), safe whether we were called from a worker or the
        # main thread.
        try:
            if hasattr(self, 'library') and self.library is not None:
                self.root.after(
                    0,
                    lambda: self.library.refresh_hotkeys(self._hotkey_cfg()),
                )
        except Exception as e:
            logger.warning(f'library hotkey-label refresh failed: {e}')

    def _on_feature_hotkey_changed(self, cfg_key: str, combo: str) -> None:
        """Called when user right-click-rebinds a feature hotkey from a library tab."""
        if 'hotkeys' not in self.config:
            self.config['hotkeys'] = {}
        self.config['hotkeys'][cfg_key] = combo
        threading.Thread(target=save_config, args=(self.config,), daemon=True).start()
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
        logger.info(f'Feature hotkey rebound: {cfg_key!r} → {combo!r}')

    def _on_settings_saved(self, new_config: dict) -> None:
        if self._whisper_recording:
            self._whisper_cancel_recording()
        self.config   = new_config
        save_config(new_config)
        self.provider = build_provider(new_config)
        if isinstance(self.provider, LocalProvider):
            threading.Thread(target=self._load_model, daemon=True).start()
        # Rebuild whisper pipeline with new config
        self._rebuild_whisper_pipeline(new_config)
        # Re-register hotkeys off the main thread, _register_hotkeys() has a
        # 150 ms sleep inside it (OS hook flush) that would freeze the UI here.
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
        self._update_tray()

    def _rebuild_whisper_pipeline(self, config: dict) -> None:
        """Recreate audio + transcriber with updated config (called after settings save)."""
        try:
            self._audio.stop()
        except Exception:
            pass
        wcfg = make_whisper_cfg(config)
        # Update VAD thresholds
        self._vad._threshold = wcfg.vad.speech_threshold
        self._vad._silence_chunks_limit = int(
            wcfg.vad.safety_silence_s * 1000 / 32
        )
        # Rebuild audio capture with new device setting
        self._audio = AudioCapture(
            on_chunk=self._on_audio_chunk,
            on_utterance_ready=self._on_utterance_ready,
            cfg=wcfg,
        )
        threading.Thread(target=lambda: self._audio.start(), daemon=True).start()
        # Transcriber model is already loaded; create new one only if model changed
        # (For simplicity, recreate, user is in settings anyway so latency is ok)
        self._transcriber.shutdown()
        self._transcriber = Transcriber(
            cfg=wcfg,
            on_result=self._on_transcription_result,
            on_status=self._on_transcriber_status,
            models_dir=models_dir(),
            log_file=log_path(),
        )

    # ── Prewarm ───────────────────────────────────────────────────────────────

    def _prewarm(self) -> None:
        if isinstance(self.provider, LocalProvider):
            return   # splash provider step handled by model_ready
        if not self.provider.ready:
            self._q.put(('prewarm:done', None))   # no API key, mark done immediately
            return
        try:
            self.provider.refine('Hello', 'Reply with one word: OK')
            logger.info('Connection pre-warmed.')
        except Exception as e:
            logger.info(f'Pre-warm skipped: {e!s:.60}')
        self._q.put(('prewarm:done', None))

    # ── Model loading (local Qwen) ────────────────────────────────────────────

    def _load_model(self) -> None:
        try:
            self.provider.load()
            self._q.put(('model_ready', None))
        except Exception as e:
            logger.error(f'Model load failed: {e}')
            self._q.put(('model_error', str(e)))

    # ── Event poll loop ───────────────────────────────────────────────────────

    # Empty gate by design, every app feature works seamlessly inside
    # the whiteboard:
    #
    # • Result-paste features (Whisper, OCR, Macros, Recorder, GIF, Web,
    #   Notes) end by writing to the clipboard + Ctrl+V; Whiteboard
    #   natively handles Ctrl+V → text/image element.
    #
    # • Text-capture features (Refine, Ask, Chain, Library, per-prompt
    #   hotkeys) start with Ctrl+C. Whiteboard's Ctrl+C is "smart": a
    #   selected text element copies its TEXT CONTENT, not its JSON. So
    #   "select text element → F1 (Refine prompt)" gives a useful flow:
    #   the refined text comes back as a new text element via Ctrl+V.
    #
    # • The only known minor friction is Ctrl+Enter, which is Whisper
    #   start AND Whiteboard's commit-text-edit. When the user is
    #   editing text, both fire, Whiteboard commits, Whisper starts
    #   recording. Esc cancels the accidental recording; we accept the
    #   trade-off for keeping Whisper available everywhere.
    #
    # Structure kept (instead of removed) so future regressions can be
    # gated narrowly without rewiring the dispatch loop.
    _WHITEBOARD_GATED_EVENTS: frozenset[str] = frozenset()

    def _is_event_gated(self, event: str) -> bool:
        """True when this event should be silently swallowed because the
        whiteboard owns focus. Currently always False, see the comment
        on _WHITEBOARD_GATED_EVENTS for the design rationale."""
        return event in self._WHITEBOARD_GATED_EVENTS

    def _poll(self) -> None:
        # Reschedule FIRST so a handler that calls wait_window() (which creates
        # a nested Tk event loop) doesn't prevent the next poll from running.
        # Without this, any modal dialog opened from a handler would stop all
        # queue processing, including tray "Reload hotkeys", until it closed.
        self.root.after(30, self._poll)
        try:
            wb_fg = self._is_whiteboard_foreground()
            while True:
                event, data = self._q.get_nowait()
                if wb_fg and self._is_event_gated(event):
                    continue  # Whiteboard owns focus, let Whiteboard handle it
                handler = self._dispatch.get(event)
                if handler:
                    try:
                        handler(data)
                    except Exception:
                        logger.exception(f'_poll: unhandled exception in handler for {event!r}')
        except queue.Empty:
            pass

    # ── Refine actions ────────────────────────────────────────────────────────

    def _do_refine(self, text: str) -> None:
        if self._whisper_recording:
            return   # don't clobber the clipboard mid-recording
        if self._refine_in_progress:
            return   # already running, ignore rapid double-press
        if not text or not text.strip():
            self.refine_overlay.show_no_selection()
            # Re-register so the keyboard library resets after the suppressed
            # hotkey, without this, the library's stuck modifier state blocks
            # all subsequent hotkeys until the next successful paste re-reg.
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if isinstance(self.provider, LocalProvider) and not self.provider.ready:
            self.refine_overlay.show_loading_model()
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if not self.provider.ready:
            self.refine_overlay.show_error('API key required, open Settings')
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return

        self._undo_available = False   # new refinement invalidates any prior undo
        self._refine_in_progress = True
        self._refine_gen_t = time.time()   # stamp for watchdog timeout check
        self._refine_gen += 1
        gen      = self._refine_gen
        self._refine_t0 = time.time()
        self.refine_overlay.show()
        prompt   = self.active_prompt
        provider = self.provider

        def infer() -> None:
            # 30-second hard timeout, fires refine:timeout on the main thread
            timer = threading.Timer(
                30.0, lambda: self._q.put(('refine:timeout', gen))
            )
            timer.start()
            try:
                result = provider.refine(text, prompt['prompt'])
                timer.cancel()
                if gen != self._refine_gen:
                    return   # timeout already fired and reset gen
                if not result or not result.strip():
                    self._q.put(('refine:error', 'Empty response from AI'))
                else:
                    self._q.put(('refine:done', result))
            except Exception as e:
                timer.cancel()
                logger.error(f'Inference error: {e}')
                if gen == self._refine_gen:
                    from engine import friendly_error_message
                    msg = friendly_error_message(
                        e, feature='Refine',
                        active_provider=self.config.get('active_provider', ''))
                    self._q.put(('refine:error', msg))
            finally:
                self._q.put(('refine:unlock', gen))

        threading.Thread(target=infer, daemon=True).start()

    def _on_refine_done(self, result: str) -> None:
        elapsed = time.time() - self._refine_t0
        self.refine_overlay.show_done(elapsed)
        pyperclip.copy(result)
        # Use direct Win32 SendInput (same path as whisper), avoids routing
        # through the keyboard library, which can leave its key-state machine
        # stale (stuck modifier keys) and break subsequent hotkeys.
        self.root.after(40, paste_from_clipboard)
        # Re-register hotkeys after the paste lands, resets any library state
        # corruption that injected Ctrl+V events may have caused.
        self.root.after(150, self._reregister_after_action)
        self._undo_available = True
        self._undo_t         = time.time()
        logger.info(f'Refinement complete in {elapsed:.2f}s')

    def _do_undo_refine(self, _) -> None:
        """Undo the last AI refinement by sending Ctrl+Z to the active window."""
        if not self._undo_available:
            return
        if time.time() - self._undo_t > 30.0:   # 30-second undo window
            self._undo_available = False
            return
        self._undo_available = False
        # Ctrl+Z in the focused app undoes our Ctrl+V paste, restoring the
        # original selected text.  We delay 40 ms so the hotkey release clears
        # before the synthetic key arrives.  Uses Win32 SendInput directly,
        # not keyboard.send(), to avoid corrupting the library's modifier state.
        self.root.after(40, undo_last)
        logger.info('Undo last refinement')

    def _prompts_are_default(self, prompts: list | None = None) -> bool:
        """Return True if the given prompts match the cached bundled defaults.

        Uses self._bundled_defaults (loaded once at startup) so that dev-mode
        saves, which overwrite prompts.json, don't corrupt the comparison.
        """
        defaults = getattr(self, '_bundled_defaults', [])
        if not defaults:
            return False
        current = prompts if prompts is not None else (
            self.library.prompts if getattr(self, 'library', None) else []
        )
        if not current or len(current) != len(defaults):
            return False
        return all(
            c.get('title') == d.get('title') and c.get('prompt') == d.get('prompt')
            for c, d in zip(current, defaults)
        )

    def _do_restore_all_defaults(self) -> None:
        """Restore prompts, hotkeys, bookmarks, chains, and window sizes to
        factory defaults. Re-registers all dependent hooks (keyboard,
        per-chain hotkeys, per-prompt hotkeys) so the live app state is
        consistent, the user should not need to restart for any reset to
        take effect."""
        from dialogs import confirm
        from storage import (DEFAULT_CONFIG, _DEFAULT_BOOKMARKS, save_bookmarks,
                             resource_path, DEFAULT_CHAINS, save_chains)
        from win_geometry import center_on_work_area
        import copy, json

        # ── Race guard: Settings window open ─────────────────────────────────
        # If Settings is open, any unsaved field plus a later Save would
        # overwrite our just-reset config and silently undo the reset. Ask
        # the user to close Settings first rather than corrupting state.
        try:
            _sw = getattr(self.settings, 'win', None)
            if _sw is not None and _sw.winfo_exists() and _sw.winfo_viewable():
                confirm(
                    self.root,
                    'Close Settings first',
                    'The Settings window is open. Close it before resetting '
                    'so any unsaved changes don\'t overwrite the reset.',
                    action_label='OK',
                )
                try:
                    _sw.lift()
                    _sw.focus_force()
                except Exception:
                    pass
                return
        except Exception:
            pass

        if not confirm(self.root,
                       'Reset everything?',
                       'This puts the whole app back to brand-new state:\n\n'
                       'Will be reset:\n'
                       '  • Your AI templates  →  back to the defaults\n'
                       '  • Template folders + colours  →  cleared\n'
                       '  • Multi-step workflows  →  back to the defaults\n'
                       '  • Keyboard shortcuts  →  back to the defaults\n'
                       '  • Favourite websites  →  back to the defaults\n'
                       '  • Quick Notes window  →  default size, blank notes\n'
                       '  • Whiteboard window  →  default size, position, blank canvas\n'
                       '  • Voice typing  →  cloud on, fast local model, auto noise cleanup,\n'
                       '       auto microphone (your hardware mic stays selected)\n'
                       '  • Transcripts history  →  cleared\n'
                       '  • Action history  →  cleared (past Refine/Ask/Chain outputs)\n'
                       '  • AI helper choice  →  fastest free option\n'
                       '  • AI helper models  →  defaults  (your API keys are kept)\n'
                       '  • Welcome flow  →  shown again on next launch\n'
                       '  • Launch on startup  →  on\n'
                       '  • Push-to-talk  →  off\n'
                       '  • Activity log  →  wiped (a fresh log starts now)\n\n'
                       "Won't be touched:\n"
                       '  • Your saved API keys\n'
                       '  • Your recorded macros\n'
                       '  • Your screen recordings and GIFs\n\n'
                       'This can\'t be undone.'):
            return

        # ── Prompts ───────────────────────────────────────────────────────────
        # Prefer in-memory cache; fall back to reading the bundled file directly
        defaults = getattr(self, '_bundled_defaults', [])
        if not defaults:
            try:
                with open(resource_path('prompts.json'), encoding='utf-8') as f:
                    defaults = json.load(f)
                logger.info('Restore: loaded bundled prompts from disk.')
            except Exception as e:
                logger.error(f'Restore: could not load bundled prompts: {e}')
        if defaults:
            self.prompts             = list(defaults)
            self.active_prompt       = self.prompts[0]
            self._at_default_prompts = True
            self.library.prompts     = list(defaults)
            self.library._render_cards()
            self.library._select(0)
            threading.Thread(target=save_prompts, args=(self.prompts,), daemon=True).start()
            logger.info(f'Restore: {len(defaults)} prompts written.')

        # ── Hotkeys ───────────────────────────────────────────────────────────
        self.config['hotkeys'] = dict(DEFAULT_CONFIG['hotkeys'])
        threading.Thread(target=save_config, args=(self.config,), daemon=True).start()
        try:
            self.library.hotkey_cfg = self.config['hotkeys']
        except Exception:
            pass
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
        logger.info('Restore: hotkeys reset.')

        # ── Bookmarks ─────────────────────────────────────────────────────────
        bm_defaults = copy.deepcopy(_DEFAULT_BOOKMARKS)
        threading.Thread(target=lambda: save_bookmarks(bm_defaults), daemon=True).start()
        try:
            # Route through _invalidate_tab — NEVER call _render_web_tab
            # directly. The render method assumes self._scroll has been
            # swapped to the Web tab's container; calling it directly
            # tripped the new tab-render guard (rerouted safely + warning
            # logged). Also: unconditional invalidation refreshes Web's
            # cached content even when it's not the active tab — without
            # this the user sees stale bookmarks on next Web tab visit.
            self.library._invalidate_tab('web')
        except Exception:
            pass
        logger.info('Restore: bookmarks reset.')

        # ── Chains ─────────────────────────────────────────────────────────────
        # Chains live in chains.json and have factory defaults in DEFAULT_CHAINS.
        # Per-chain hotkeys are derived from each chain's 'hotkey' field, so
        # we must re-register them after resetting.
        try:
            self.chains = copy.deepcopy(DEFAULT_CHAINS)
            threading.Thread(target=save_chains, args=(self.chains,),
                             daemon=True).start()
            self._register_chain_hotkeys()
            try:
                self.library.chains = list(self.chains)
                # Route through _invalidate_tab — same reasoning as the
                # bookmarks reset above. Unconditional invalidation so
                # Chains tab refreshes even when not currently active.
                self.library._invalidate_tab('chains')
            except Exception:
                pass
            logger.info(f'Restore: {len(self.chains)} chains written + hotkeys re-registered.')
        except Exception as e:
            logger.error(f'Restore: chains reset failed: {e}')

        # ── Quick Notes window: geometry + theme + recentering ───────────────
        # Use the shared work-area helper so the title bar is never under the
        # system menu and the bottom edge is never behind the taskbar.
        self.config['notes_geometry'] = ''
        self.config['notes_theme']    = DEFAULT_CONFIG['notes_theme']
        threading.Thread(target=save_config, args=(self.config,), daemon=True).start()
        if self._notes_win is not None:
            try:
                from quicknotes import _W, _H
                x, y, w, h = center_on_work_area(_W, _H)
                self._notes_win.geometry(f'{w}x{h}+{x}+{y}')
                self._notes_win._set_theme(DEFAULT_CONFIG['notes_theme'])
            except Exception as e:
                logger.warning(f'Restore: Notes re-center failed: {e}')
        logger.info('Restore: Notes geometry + theme reset.')

        # ── Whiteboard: subprocess-aware reset ────────────────────────────────
        # The whiteboard runs in its own pywebview process. Order matters:
        # close the live whiteboard FIRST so its debounced auto-save can't
        # race ahead and overwrite our reset. Then wipe the scene file.
        # Next Shift+F8 reopens default-centered.
        if sys.platform == 'win32':
            try:
                import win32gui, win32con, win32process
                closed_pids = set()
                def _cb(h, _):
                    if win32gui.GetWindowText(h) == 'Whiteboard (Shift+F8)':
                        win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
                        try:
                            _, pid = win32process.GetWindowThreadProcessId(h)
                            closed_pids.add(pid)
                        except Exception:
                            pass
                win32gui.EnumWindows(_cb, None)
                # Wait for the subprocess(es) to actually exit so their final
                # save can't land after our reset write.
                if closed_pids:
                    import psutil
                    deadline = time.time() + 3.0
                    while time.time() < deadline:
                        alive = [pid for pid in closed_pids if psutil.pid_exists(pid)]
                        if not alive: break
                        time.sleep(0.15)
                    logger.info(f'Restore: closed {len(closed_pids)} whiteboard subprocess(es).')
            except Exception as e:
                logger.warning(f'Restore: whiteboard close failed: {e}')

        try:
            from storage import whiteboard_path
            wb_json = whiteboard_path()
            try:
                scene = json.load(open(wb_json, encoding='utf-8'))
            except Exception:
                scene = {}
            scene.setdefault('type', 'excalidraw')
            scene.setdefault('version', 2)
            scene.setdefault('source', 'restore-defaults')
            scene.setdefault('elements', [])
            scene.setdefault('files', {})
            app_state = scene.get('appState') or {}
            app_state['theme']               = 'light'
            app_state['viewBackgroundColor'] = '#ffffff'
            # Drop any persisted zoom/scroll so the next open is back at 1:1 origin
            for k in ('zoom', 'scrollX', 'scrollY'):
                app_state.pop(k, None)
            scene['appState'] = app_state
            tmp = wb_json + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(scene, f)
            import os as _os; _os.replace(tmp, wb_json)
        except Exception as e:
            logger.warning(f'Restore: whiteboard.json theme reset failed: {e}')

        logger.info('Restore: Whiteboard scene theme + size reset.')

        # ── Transcripts: clear stored JSONs + YouTube cache ──────────────────
        # Restore is the user's "factory reset", wipe past transcripts so a
        # fresh start has no history. The downloaded YouTube cache is also
        # purged from BOTH the AppData path (older builds + dist mode) and
        # the repo-local .transcripts_cache used in dev (no-C-drive rule).
        # Each delete is best-effort: a file currently being read by an
        # active transcribe worker will raise PermissionError on Windows
        # and we skip it rather than fight the lock.
        try:
            from storage import transcripts_dir
            import shutil
            _repo = os.path.dirname(os.path.abspath(__file__))
            candidates = [
                transcripts_dir(),
                os.path.join(appdata_dir(), 'transcripts_cache'),
                os.path.join(_repo, '.transcripts_cache'),
            ]
            skipped = 0
            for sub in candidates:
                if os.path.isdir(sub):
                    for name in os.listdir(sub):
                        p = os.path.join(sub, name)
                        try:
                            if os.path.isfile(p): os.remove(p)
                            elif os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
                        except Exception:
                            # File in use by a live worker, leave it; it
                            # will be cleaned up when the worker finishes.
                            skipped += 1
            if skipped:
                logger.info(f'Restore: transcripts cleared ({skipped} skipped, in use).')
            else:
                logger.info('Restore: transcripts cleared.')
        except Exception as e:
            logger.warning(f'Restore: transcripts wipe failed: {e}')

        # ── App-level settings ────────────────────────────────────────────────
        for key in ('active_provider', 'autostart', 'push_to_talk'):
            self.config[key] = DEFAULT_CONFIG[key]
        logger.info('Restore: active_provider / autostart / push_to_talk reset.')

        # ── Library folders + colors ──────────────────────────────────────────
        # User-created folder groupings drift from default unless reset here.
        # Clearing both the in-memory copy AND the on-disk config keeps the
        # Library sidebar consistent with "factory state".
        try:
            self.folders        = list(DEFAULT_CONFIG.get('folders', []))
            self.folder_colors  = dict(DEFAULT_CONFIG.get('folder_colors', {}))
            self.config['folders']       = list(self.folders)
            self.config['folder_colors'] = dict(self.folder_colors)
            try:
                self.library.folders       = list(self.folders)
                self.library.folder_colors = dict(self.folder_colors)
                self.library._render_cards()
            except Exception:
                pass
            logger.info('Restore: folders + folder_colors reset.')
        except Exception as e:
            logger.warning(f'Restore: folders reset failed: {e}')

        # ── First-run flag, clear so the welcome / onboarding flow can run
        # again on the next launch, matching genuine "fresh install" behaviour.
        self.config['first_run_done'] = False

        # ── Quick Notes content, wipe the JSON so the next open is empty.
        # We do this AFTER closing the notes window above (notes_geometry block)
        # so an in-flight save can't race ahead of the wipe.
        try:
            from storage import notes_path
            np = notes_path()
            try:
                with open(np, 'w', encoding='utf-8') as f:
                    json.dump([], f)
                logger.info('Restore: notes.json cleared.')
            except FileNotFoundError:
                pass   # never written yet, nothing to clear
            except Exception as e:
                logger.warning(f'Restore: notes.json clear failed: {e}')
        except Exception:
            pass

        # ── Action history, clear past Refine/Ask/Chain outcomes.
        try:
            self._history = []
            from storage import save_history
            threading.Thread(target=save_history, args=(self._history,),
                             daemon=True).start()
            logger.info('Restore: history cleared.')
        except Exception as e:
            logger.warning(f'Restore: history clear failed: {e}')

        # ── Transient cross-call state added by later features ───────────────
        # These dicts/sets accumulate during normal use (dedupe guards, pill
        # rate-limiters, last-known-error caches). They aren't persisted to
        # disk so Reset doesn't strictly need to touch them, but clearing
        # them gives the user a TRUE clean slate — e.g. the next cloud-fail
        # after Reset will surface the explanatory pill instead of being
        # suppressed by a stale 10-minute cooldown carried over from before
        # the reset.
        try:
            if hasattr(self, '_cloud_notice_seen'):
                self._cloud_notice_seen.clear()
            if hasattr(self, '_screenshot_in_flight'):
                self._screenshot_in_flight.clear()
            if hasattr(self, '_downloads_in_flight'):
                self._downloads_in_flight.clear()
            tr = getattr(self, '_transcriber', None)
            if tr is not None:
                tr._cloud_last_error = None
                tr._CLOUD_RECENT_OK = True
        except Exception as e:
            logger.warning(f'Restore: transient state clear failed: {e}')

        # ── Prompt sticky note, close if open. Position state will respawn
        # centered on next prompt-hotkey fire.
        try:
            if self._sticky is not None:
                try:
                    self._sticky.destroy()
                except Exception:
                    pass
                self._sticky = None
        except Exception:
            pass

        # ── Live overlays + ask pills, hide so the UI is back to baseline ────
        try:
            for ov in (self.refine_overlay, self.whisper_overlay,
                       self.macro_overlay, self.recorder_overlay,
                       self.gif_overlay, self.chain_overlay):
                try:
                    ov.hide()
                except Exception:
                    pass
            self._close_all_ask_pills()
        except Exception:
            pass

        # ── app.log, truncate so the user really gets a clean slate.
        # We rotate handlers off the file first so the active RotatingFileHandler
        # isn't holding a write lock when we truncate (Windows would otherwise
        # raise PermissionError). The handler reopens lazily on the next log
        # call, so no logger setup is needed after.
        try:
            lp = log_path()
            for h in list(logger.handlers):
                if hasattr(h, 'baseFilename') and os.path.abspath(h.baseFilename) == os.path.abspath(lp):
                    try:
                        h.close()
                    except Exception:
                        pass
            try:
                with open(lp, 'w', encoding='utf-8'):
                    pass   # truncate
                # Also remove any rotated siblings (.1 .2 .3) so disk state matches fresh install
                for i in range(1, 6):
                    side = lp + f'.{i}'
                    try:
                        if os.path.exists(side):
                            os.remove(side)
                    except Exception:
                        pass
                logger.info('Restore: app.log truncated.')
            except Exception as e:
                logger.warning(f'Restore: app.log truncate failed: {e}')
        except Exception:
            pass

        # ── Provider models (preserve API keys) ───────────────────────────────
        for pkey, pdefaults in DEFAULT_CONFIG['providers'].items():
            if pkey not in self.config.get('providers', {}):
                continue
            for field, val in pdefaults.items():
                if field == 'api_key':
                    continue   # never wipe API keys
                self.config['providers'][pkey][field] = val
        threading.Thread(target=save_config, args=(self.config,), daemon=True).start()
        logger.info('Restore: provider models reset (API keys preserved).')

        # ── Whisper config, VAD threshold, noise reduction, model, etc. ──────
        # Without this, anything the user (or a previous reactive support
        # fix) tweaked under whisper.vad.* / whisper.audio.* / whisper.model.*
        # would survive a "Restore All Defaults", which the user wouldn't
        # expect.  Audio device override is preserved if explicitly set
        # (it's a hardware choice, not a preference).
        try:
            from copy import deepcopy
            wcfg = deepcopy(DEFAULT_CONFIG['whisper'])
            # Preserve a non-default audio.input_device_index, that's the
            # user's mic selection, not a preference to reset.
            user_dev = (self.config.get('whisper') or {}).get(
                'audio', {}).get('input_device_index')
            if user_dev is not None:
                wcfg.setdefault('audio', {})['input_device_index'] = user_dev
            self.config['whisper'] = wcfg
            threading.Thread(target=save_config, args=(self.config,),
                             daemon=True).start()
            logger.info('Restore: whisper config reset (mic device preserved).')
        except Exception as e:
            logger.warning(f'Restore: whisper config reset failed: {e}')

        # ── Brand icon, regenerate if missing so the dist looks identical
        # to first launch (the .ico itself is deterministic, but the user
        # may have deleted it manually). _save_brand_ico is idempotent.
        try:
            self._save_brand_ico()
        except Exception:
            pass

        self._update_tray()
        self._notify('Everything reset ✓',
                     'Templates, workflows, shortcuts, bookmarks, windows, '
                     'voice typing, and AI helpers all back to defaults.')
        logger.info('All defaults restored successfully.')

    def _on_refine_timeout(self, gen: int) -> None:
        if gen != self._refine_gen:
            return   # already handled by normal completion
        self._refine_in_progress = False
        self._refine_gen += 1   # invalidate so any late result is discarded
        self.refine_overlay.show_error('Request timed out, try again')
        logger.warning('Refine request timed out after 30s')

    def _on_refine_unlock(self, gen: int) -> None:
        """Called after every infer() thread regardless of outcome."""
        if gen == self._refine_gen:
            self._refine_in_progress = False

    def _on_model_ready(self, _) -> None:
        logger.info('Local model ready.')
        self._splash.mark_done('provider')
        self._update_tray()
        hk = self._hotkey_cfg().get('refine', 'alt+shift+w').upper()
        self._notify('Hotkeys is ready ⚡', f'Select any text and press {hk} to refine it.')

    def _on_model_error(self, msg: str) -> None:
        self._splash.mark_error('provider')
        self._notify('Model failed to load', msg[:120])

    # ── Whisper actions ───────────────────────────────────────────────────────

    def _whisper_start_recording(self) -> None:
        if self._whisper_recording:
            return   # already recording, ignore key-repeat in PTT mode
        if not self._whisper_ready:
            self.whisper_overlay.show_whisper_loading()
            return
        # Capture the window the user was typing into RIGHT NOW, before
        # anything else can steal focus (a notification toast, the user
        # accidentally Alt-Tabbing, the recording pill briefly painting,
        # etc.). We'll re-foreground this HWND right before the paste so
        # the transcribed text lands where the user expected.
        try:
            import win32gui as _wg
            self._whisper_target_hwnd = _wg.GetForegroundWindow()
        except Exception:
            self._whisper_target_hwnd = None
        self._whisper_recording = True
        self._whisper_t0 = time.time()
        self._vad.reset()
        try:
            self._audio.start_recording()
        except Exception as e:
            self._whisper_recording = False
            logger.error(f'Microphone error: {e}')
            # Forward the actual error so the dialog can show the right
            # fix instead of the generic "permissions" copy.
            self._show_mic_error(str(e))
            return
        play_start()
        self.whisper_overlay.show_recording()
        self._update_tray()
        logger.info('Whisper recording started.')

    def _whisper_stop_recording(self) -> None:
        if not self._whisper_recording:
            return
        self._whisper_recording = False
        play_stop()
        self._audio.stop_recording()
        self.whisper_overlay.show_transcribing()
        self._update_tray()
        logger.info('Whisper recording stopped, transcribing.')
        # Watchdog: if neither result nor error arrives within the
        # budget, force-clear the pill + state and surface a clear
        # error to the user. Without this, ANY silent failure in the
        # audio→transcriber chain leaves the "Transcribing…" pill
        # frozen until the user reloads. Real transcriptions complete
        # well under 30 s; we cancel any prior watchdog if a new
        # recording starts inside the window.
        try:
            if self._transcribe_watchdog_id is not None:
                self.root.after_cancel(self._transcribe_watchdog_id)
        except Exception:
            pass
        self._transcribe_watchdog_id = self.root.after(
            30_000, self._transcribe_watchdog_fire)

    def _transcribe_watchdog_fire(self) -> None:
        """30 s expired waiting for whisper:result / whisper:error.
        Clear the stuck UI + state and let the next press succeed.
        (The overlay's own 30 s auto-dismiss handles the visible pill;
        this watchdog handles the BEHIND-THE-SCENES STATE — resetting
        the recording flag, cancelling in-flight transcriber work, so
        the next Ctrl+Enter doesn't run into the stale state.)"""
        self._transcribe_watchdog_id = None
        logger.warning(
            'Transcribe watchdog: 30 s elapsed since recording stopped '
            'with no result/error. Forcing whisper state reset.'
        )
        try:
            self.whisper_overlay.show_whisper_error('Transcription stuck — try again')
        except Exception:
            pass
        # Belt + braces: cancel any in-flight audio + transcriber state
        # so the next Ctrl+Enter starts cleanly.
        try:
            self._audio.cancel_recording()
        except Exception:
            pass
        try:
            self._transcriber.cancel()
        except Exception:
            pass
        self._whisper_recording = False
        try:
            self._update_tray()
        except Exception:
            pass

    def _whisper_cancel_recording(self) -> None:
        if not self._whisper_recording:
            return
        self._whisper_recording = False
        self._audio.cancel_recording()
        self.whisper_overlay.show_whisper_cancelled()
        self._update_tray()
        logger.info('Whisper recording cancelled.')

    def _on_vad_safety_stop(self) -> None:
        """Called from audio thread when silence limit exceeded."""
        self._q.put(('whisper:stop', None))

    def _on_audio_chunk(self, chunk) -> None:
        if self._whisper_recording:
            self._vad.process_chunk(chunk)

    def _on_utterance_ready(self, audio) -> None:
        self._transcriber.submit(audio)

    def _on_transcriber_status(self, status: str) -> None:
        """Called from transcriber thread, post to main queue."""
        self._q.put(('whisper:status', status))

    def _on_transcriber_status_event(self, status: str) -> None:
        """Handle transcriber status on main thread."""
        if status == 'loading':
            self._whisper_ready = False
        elif status == 'ready':
            self._whisper_ready = True
            self._splash.mark_done('whisper')
            pass  # no notification, Whisper ready is silent
        elif status == 'jit_done':
            # CTranslate2 CPU kernels are compiled, first real Ctrl+Enter
            # will skip the ~500-800 ms JIT cost.
            self._splash.mark_done('whisper_jit')
        elif status == 'cloud_warm':
            # TLS handshake to api.groq.com is established, cloud
            # transcription's first call only pays inference time.
            self._splash.mark_done('cloud')
        elif status == 'error':
            self._whisper_ready = True  # allow retry
            self._splash.mark_error('whisper')
            self._q.put(('whisper:error', 'Transcription failed'))

    def _on_transcription_result(self, text: str, language: str, duration_s: float) -> None:
        """Called from transcriber thread, post to main queue."""
        self._q.put(('whisper:result', (text, language, duration_s)))

    def _on_whisper_result(self, payload) -> None:
        # Result arrived → cancel the transcribe watchdog.
        try:
            if self._transcribe_watchdog_id is not None:
                self.root.after_cancel(self._transcribe_watchdog_id)
                self._transcribe_watchdog_id = None
        except Exception:
            pass
        text, language, duration_s = payload
        elapsed = time.time() - self._whisper_t0

        # If the cloud Whisper path failed and we fell back to local model,
        # surface that to the user — once per 10 minutes per unique message,
        # not every dictation. The fallback "just works" so the user gets
        # the pasted transcript either way; they only need to be told once
        # what's going on, not nagged on every Ctrl+Enter.
        try:
            tr = getattr(self, '_transcriber', None)
            err = getattr(tr, '_cloud_last_error', None)
            if err:
                tr._cloud_last_error = None   # always clear regardless
                if not hasattr(self, '_cloud_notice_seen'):
                    self._cloud_notice_seen: dict[str, float] = {}
                last_t = self._cloud_notice_seen.get(err, 0.0)
                if (time.time() - last_t) > 600:   # 10-minute cooldown
                    self._cloud_notice_seen[err] = time.time()
                    self.refine_overlay.show_cloud_fallback_notice(err)
        except Exception:
            pass

        # Sentinel from the transcriber meaning the recorded audio was
        # silent, no point pasting and no point letting Whisper hallucinate
        # "Thank you." Surface a clear, actionable message.
        if text == '__NO_AUDIO__':
            self.whisper_overlay.show_whisper_cancelled()
            logger.info('Whisper: no speech detected.')
            # Pill in the corner is enough; only escalate to a tray
            # notification if the recording was non-trivially long (so the
            # user definitely tried to dictate and deserves to know we
            # heard nothing) AND the mic looks dead, not just quiet.
            try:
                if duration_s and duration_s > 4.0:
                    self._notify(
                        'No speech detected',
                        "We did not hear anything in that recording. "
                        "If your mic is plugged in and unmuted, try again "
                        "and speak slightly closer to it.",
                    )
            except Exception:
                pass
            return

        if not text:
            self.whisper_overlay.show_whisper_cancelled()
            logger.info('Whisper: no speech detected.')
            return

        # Log the actual transcript so we can diagnose mishears.
        # ("Memo, …" is sometimes transcribed as plain "…" by Whisper base
        # because the soft "M" gets clipped by VAD.) Truncate to 200 chars
        # so we don't fill the log with full dictations on every call.
        _preview = text[:200] + ('…' if len(text) > 200 else '')
        logger.info(f'Whisper transcript: {_preview!r}')

        # ── Voice-command short-circuit: single-word app commands ────────────
        # If the dictation is exactly one of our known command words
        # (e.g. "library"), open that feature instead of typing the word.
        # Checked BEFORE memo so single-word "library" doesn't fall through
        # to the memo logic. Single-word only — multi-word phrases still
        # paste normally so users can dictate "library books" etc.
        if self._maybe_run_voice_command(text):
            return

        # ── Voice-command short-circuit: "memo" at start or end ──────────────
        # Detect BEFORE pasting. If triggered, save to Quick Notes and skip
        # the normal paste path entirely — the user doesn't want the trigger
        # word typed into whatever app has focus.
        _note_body = self._extract_voice_note_body(text)
        if _note_body is not None:
            self._save_voice_note(_note_body)
            self.whisper_overlay.show_whisper_saved_to_notes()
            # Still push the FULL transcript (with trigger phrase) into history,
            # so the user can audit what they said.
            self._history.append({
                'text':     text,
                'language': language,
                'duration': round(duration_s, 2),
                'ts':       datetime.datetime.now().isoformat(timespec='seconds'),
                'source':   'voice-to-notes',
            })
            if len(self._history) > _HISTORY_MAX_ENTRIES:
                self._history = self._history[-_HISTORY_MAX_ENTRIES:]
            _snap = list(self._history)
            threading.Thread(target=save_history, args=(_snap,), daemon=True).start()
            logger.info(f'voice-to-notes: saved ({len(_note_body)} chars), '
                        f'transcript was {len(text)} chars')
            return

        out_cfg = self.config.get('whisper', {}).get('output', {})
        out  = text + (' ' if out_cfg.get('add_trailing_space', True) else '')

        copy_to_clipboard(out)
        if out_cfg.get('type_text', True):
            # Restore focus to the window the user was typing into when
            # they pressed Ctrl+Enter. Without this, a fraction of users
            # see "Typed ✓" but no text appears because focus shifted
            # during recording (notification toast, accidental click,
            # the source window losing keyboard focus to the recording
            # pill on some setups, etc.). The Win32 SetForegroundWindow
            # workaround in _force_foreground bypasses Windows' anti-
            # focus-stealing rules using AttachThreadInput.
            hwnd = getattr(self, '_whisper_target_hwnd', None)
            if hwnd:
                try:
                    self._force_foreground(hwnd)
                except Exception as e:
                    logger.warning(f'Could not restore focus before paste: {e}')
            # The user's stop press (Ctrl+Enter) passes through to the
            # focused window because our hotkey is suppress=False (the
            # keyboard library's modifier-state machine locks up on
            # suppressed hotkeys). That means the focused text editor
            # received an Enter and the cursor is now one line below
            # where the user wanted their text. Send a single Backspace
            # to undo the newline before the paste, restoring the
            # original cursor position. Wrapped in macro-suspend so the
            # synthetic Backspace does not pollute an active recording.
            whisper_hk = (self.config.get('hotkeys') or {}).get('whisper', '')
            if 'enter' in whisper_hk.lower():
                def _undo_stray_enter():
                    try:
                        from macros.recorder import suspend_capture
                        import ctypes
                        from ctypes import wintypes
                        VK_BACK = 0x08
                        KEYEVENTF_KEYUP = 0x0002
                        INPUT_KEYBOARD = 1
                        class KEYBDINPUT(ctypes.Structure):
                            _fields_ = [('wVk', wintypes.WORD),
                                        ('wScan', wintypes.WORD),
                                        ('dwFlags', wintypes.DWORD),
                                        ('time', wintypes.DWORD),
                                        ('dwExtraInfo', ctypes.c_ulonglong)]
                        class _U(ctypes.Union):
                            _fields_ = [('ki', KEYBDINPUT),
                                        ('_pad', ctypes.c_byte * 32)]
                        class INPUT(ctypes.Structure):
                            _anonymous_ = ('u',)
                            _fields_ = [('type', wintypes.DWORD), ('u', _U)]
                        with suspend_capture():
                            dn = INPUT(type=INPUT_KEYBOARD)
                            dn.ki = KEYBDINPUT(VK_BACK, 0, 0, 0, 0)
                            up = INPUT(type=INPUT_KEYBOARD)
                            up.ki = KEYBDINPUT(VK_BACK, 0, KEYEVENTF_KEYUP, 0, 0)
                            arr = (INPUT * 2)(dn, up)
                            ctypes.windll.user32.SendInput(
                                2, arr, ctypes.sizeof(INPUT))
                    except Exception as e:
                        logger.warning(f'undo-stray-enter failed: {e}')
                self.root.after(80, _undo_stray_enter)
            # Slight extra delay so the foreground swap has time to land
            # before SendInput fires Ctrl+V.
            self.root.after(160, paste_from_clipboard)
        # Re-register after paste for the same reason as refine, injected
        # Ctrl+V can leave the keyboard library's state stale.
        self.root.after(150, self._reregister_after_action)

        self.whisper_overlay.show_whisper_done(elapsed)

        # Save to history off the main thread so it never delays the paste
        self._history.append({
            'text':     text,
            'language': language,
            'duration': round(duration_s, 2),
            'ts':       datetime.datetime.now().isoformat(timespec='seconds'),
        })
        if len(self._history) > _HISTORY_MAX_ENTRIES:
            self._history = self._history[-_HISTORY_MAX_ENTRIES:]
        _snap = list(self._history)
        threading.Thread(target=save_history, args=(_snap,), daemon=True).start()
        logger.info(f'Whisper complete: {len(text)} chars in {elapsed:.2f}s')

    # ── Single-word voice commands ────────────────────────────────────────────
    # Maps a (cleaned, lowercased) single-word dictation to a tuple of
    # (event-queue command, user-facing pill label). Only matched when the
    # transcript contains exactly that one word (ignoring punctuation), so
    # the user can still dictate longer sentences containing these words.
    _VOICE_COMMANDS = {
        'library':    ('library',      'Library opened'),
        'whiteboard': ('whiteboard',   'Whiteboard opened'),
        'audio':      ('audio_editor', 'Audio editor opened'),
    }

    def _maybe_run_voice_command(self, text: str) -> bool:
        """If *text* is a recognized single-word command, dispatch it and
        return True (caller skips paste). Otherwise return False."""
        import re as _re
        # Strip surrounding punctuation Whisper adds, then lowercase.
        cleaned = _re.sub(r'[^\w]', '', text.strip(), flags=_re.UNICODE).lower()
        if not cleaned:
            return False
        entry = self._VOICE_COMMANDS.get(cleaned)
        if entry is None:
            return False
        cmd, label = entry
        try:
            self._q.put((cmd, None))
        except Exception as exc:
            logger.warning(f'voice-command {cmd!r} queue failed: {exc}')
            return False
        # Visual feedback
        try:
            self.whisper_overlay.show_whisper_command_fired(label)
        except Exception:
            pass
        # Also save to history so it's discoverable
        self._history.append({
            'text':     text,
            'language': 'voice-command',
            'duration': 0,
            'ts':       datetime.datetime.now().isoformat(timespec='seconds'),
            'source':   f'voice-command:{cmd}',
        })
        if len(self._history) > _HISTORY_MAX_ENTRIES:
            self._history = self._history[-_HISTORY_MAX_ENTRIES:]
        _snap = list(self._history)
        threading.Thread(target=save_history, args=(_snap,), daemon=True).start()
        logger.info(f'voice-command fired: {cmd!r}')
        return True

    @staticmethod
    def _extract_voice_note_body(text: str) -> str | None:
        """Detect the "save to Quick Notes" voice command in *text*.

        Trigger word: literally "memo" — case-insensitive, word-bounded
        so "memorial" / "memos" / mid-sentence usages never trigger.
        Position: must be the FIRST or LAST word (with optional leading
        / trailing punctuation that Whisper likes to insert).

        Returns the body with the trigger word stripped, or None if no
        trigger. Returns '' if the user said only "memo" alone.
        """
        import re as _re
        # First word is exactly "memo" (with optional surrounding punctuation).
        PREFIX_RE = _re.compile(
            r'^[\s,;:!\.\-]*memo\b[\s,;:!\.\-]*',
            _re.IGNORECASE,
        )
        # Last word is exactly "memo" (with optional surrounding punctuation).
        SUFFIX_RE = _re.compile(
            r'[\s,;:!\.\-]*\bmemo[\s,;:!\.\-]*[\s\.\!\?]*$',
            _re.IGNORECASE,
        )

        body = text
        triggered = False
        m = PREFIX_RE.match(body)
        if m:
            body = body[m.end():]
            triggered = True
        m = SUFFIX_RE.search(body)
        if m:
            body = body[:m.start()]
            triggered = True

        if not triggered:
            return None

        body = body.strip().strip(' .,;:!?-').strip()
        if not body:
            return ''
        return body

    def _save_voice_note(self, body: str) -> None:
        """Persist *body* as a fresh Quick Notes entry. Triggers a live
        refresh of the Quick Notes window if it's open, and pings a tray
        notification so the user knows the save happened even if their
        cursor is far from the whisper overlay pill."""
        if not body:
            logger.info('voice-to-notes: empty body, skipping save')
            return
        try:
            from storage import load_notes, save_notes
            import uuid
            notes = load_notes()
            notes.append({
                'id':         str(uuid.uuid4()),
                'text':       body,
                'items':      [{'text': '', 'checked': False}],
                'voice':      '',
                'color':      None,
                'pinned':     False,
                'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
                'source':     'voice',
            })
            save_notes(notes)
            # Live-refresh open Quick Notes window
            _win = getattr(self, '_notes_win', None)
            if _win is not None:
                try:
                    _win._invalidate_notes_cache()
                    self.root.after(0, _win._refresh_list)
                except Exception:
                    pass
            # NOTE: deliberately no Windows toast notification here. The
            # near-cursor "📝 Saved to Notes" pill (shown by the caller) is
            # the user-facing confirmation. The Windows toast comes through
            # as "Python ▸ Saved to Quick Notes" which looks unbranded and
            # interrupts the user's window; the near-cursor pill is enough
            # and matches the rest of the app's notification language.
        except Exception as exc:
            logger.warning(f'voice-to-notes persist failed: {exc}')

    def _on_whisper_error(self, msg: str) -> None:
        # Error arrived → cancel the transcribe watchdog.
        try:
            if self._transcribe_watchdog_id is not None:
                self.root.after_cancel(self._transcribe_watchdog_id)
                self._transcribe_watchdog_id = None
        except Exception:
            pass
        self.whisper_overlay.show_whisper_error(msg)
        logger.error(f'Whisper error: {msg}')

    # ── Macro record & replay ─────────────────────────────────────────────────

    def _on_macro_hotkey(self, _=None) -> None:
        """Shift+F1, cycles: idle→recording, recording→ready, ready→playing."""
        state = self._macro_state
        if state == 'idle':
            self._macro_start_recording()
        elif state == 'recording':
            self._macro_stop_recording()
        elif state == 'ready':
            self._macro_start_playback()
        elif state == 'playing':
            self._on_macro_emergency_stop()

    def _set_macro_state(self, state: str) -> None:
        """Set macro state on both main.py and the library window (for right-click menu labels)."""
        self._macro_state = state
        self.library._macro_state = state
        self.library._sync_hint_bar()

    def _macro_reset(self) -> None:
        """Abort any active recording/playback and return to idle, called from Library reset button."""
        self._macro.force_stop()
        self._macro.clear()
        self._macro_unregister_stop_keys()
        self._set_macro_state('idle')
        self.macro_overlay._close()
        self.library.refresh_macros()
        logger.info('Macro session discarded, reset to idle')

    def _macro_start_recording(self) -> None:
        self._macro_t0 = time.time()   # stamp for watchdog grace window
        self._set_macro_state('recording')
        self._macro.start_recording(
            on_cap_reached=lambda: self._q.put(('macro:cap', None))
        )
        self._macro_register_stop_keys()
        self.macro_overlay.show_macro_recording()
        logger.info('Macro recording started')

    def _macro_stop_recording(self) -> None:
        self._macro.stop_recording()
        n = self._macro.event_count
        self._set_macro_state('ready' if n > 0 else 'idle')
        self._macro_unregister_stop_keys()
        if n > 0:
            self.macro_overlay.show_macro_ready(n)
            logger.info(f'Macro recording stopped, {n} events, {self._macro.duration:.2f}s')
        else:
            self.macro_overlay._close()
            logger.info('Macro recording stopped, no events captured')

    def _on_macro_cap(self) -> None:
        """5 000-event hard cap reached, auto-stop recording and notify user."""
        from macros.recorder import _MAX_EVENTS
        logger.warning(f'Macro recording capped at {_MAX_EVENTS} events, auto-stopped')
        self._macro.stop_recording()
        n = self._macro.event_count
        self._set_macro_state('ready' if n > 0 else 'idle')
        self._macro_unregister_stop_keys()
        self.macro_overlay.show_macro_ready(n)
        self._notify(
            'Macro recording capped ⚠',
            f'Reached the {_MAX_EVENTS:,}-event limit, recording stopped automatically.',
        )

    def _macro_start_playback(self) -> None:
        if not self._macro.event_count:
            return
        self._macro_t0 = time.time()   # stamp for watchdog grace window
        self._set_macro_state('playing')
        self._macro_register_stop_keys()
        self.macro_overlay.show_macro_playing()
        self._macro.start_playback(
            on_done=lambda: self.root.after(0, self._macro_play_done),
            on_stop=lambda: self.root.after(0, self._macro_play_stopped),
        )
        logger.info('Macro playback started')

    def _macro_play_done(self) -> None:
        self._set_macro_state('ready')
        self._macro_unregister_stop_keys()
        self.macro_overlay.show_macro_done()
        logger.info('Macro playback complete')
        # Show save prompt after a short delay (let pill appear first)
        self.root.after(900, self._macro_show_save_prompt)

    def _macro_show_save_prompt(self) -> None:
        """Show 'Save this macro?' dialog near cursor."""
        if self._macro_state != 'ready' or not self._macro.event_count:
            return
        default_name = self._macro_library.next_default_name()
        default_hk   = self._macro_library.next_available_hotkey()
        dlg = MacroSavePrompt(
            self.root,
            default_name=default_name,
            default_hotkey=default_hk,
            on_hotkey_suspend=self._suspend_hotkeys,
            on_hotkey_resume=self._resume_hotkeys,
        )
        self.root.wait_window(dlg)
        # Reset to idle regardless of save/discard so Shift+F1 starts fresh.
        self._set_macro_state('idle')
        if dlg.result:
            name = dlg.result['name'].strip() or default_name
            hk   = dlg.result['hotkey']
            meta = self._macro_library.save(self._macro, name, hk)
            logger.info(f'Macro saved: "{name}" ({meta["event_count"]} events) hotkey={hk!r}')
            self._register_macro_saved_hotkeys()
            self.library.refresh_macros()
            # Confirmation pill, replaces the "done" pill
            self.macro_overlay.show_macro_saved(name, hk)
        # Clear after save (or discard), not before, otherwise save gets empty events
        self._macro.clear()

    def _on_library_macro_play(self, meta: dict) -> None:
        """Play a saved macro triggered from the Library UI."""
        if self._macro_state in ('recording', 'playing'):
            return
        rec = self._macro_library.load_recorder(meta['id'])
        # Replace the live recorder temporarily for playback
        self._macro = rec
        self._macro_t0 = time.time()   # stamp for watchdog grace window
        self._set_macro_state('playing')
        self._macro_register_stop_keys()
        self.macro_overlay.show_macro_playing()
        self._macro.start_playback(
            on_done=lambda: self.root.after(0, self._macro_saved_play_done),
            on_stop=lambda: self.root.after(0, self._macro_play_stopped),
        )
        logger.info(f'Macro playback (saved): "{meta["name"]}"')

    def _macro_saved_play_done(self) -> None:
        """Playback of a saved macro finished, don't offer save again."""
        self._set_macro_state('idle')
        self._macro_unregister_stop_keys()
        self.macro_overlay.show_macro_done()
        logger.info('Saved macro playback complete')

    def _register_macro_saved_hotkeys(self) -> None:
        """Re-register all saved-macro playback hotkeys."""
        for hk in self._macro_saved_hks:
            try:
                kbhook.remove_hotkey(hk)
            except Exception:
                pass
        self._macro_saved_hks = []
        for meta in self._macro_library.macros:
            hk = meta.get('hotkey', '').strip()
            if not hk:
                continue
            mid  = meta['id']
            name = meta['name']
            try:
                handle = kbhook.add_hotkey(
                    hk,
                    lambda m=meta: self._q.put(('macro:play_saved', m)),
                )
                self._macro_saved_hks.append(handle)
                logger.info(f'Macro hotkey registered: {hk!r} -> "{name}"')
            except Exception as e:
                logger.warning(f'Could not register macro hotkey {hk!r}: {e}')

    def _macro_play_stopped(self) -> None:
        # Guard: _on_macro_emergency_stop already ran if state is no longer 'playing'
        if self._macro_state != 'playing':
            return
        self._set_macro_state('ready')
        self._macro_unregister_stop_keys()
        self.macro_overlay.show_macro_stopped()
        logger.info('Macro playback force-stopped')

    def _on_macro_emergency_stop(self, _=None) -> None:
        """Esc or Del, abort recording or playback immediately."""
        state = self._macro_state
        if state not in ('recording', 'playing'):
            return
        self._macro.force_stop()
        if state == 'recording':
            n = self._macro.event_count
            self._set_macro_state('ready' if n > 0 else 'idle')
            self._macro_unregister_stop_keys()
            if n > 0:
                self.root.after(0, lambda: self.macro_overlay.show_macro_ready(n))
            else:
                self.root.after(0, self.macro_overlay._close)
            logger.info(f'Macro recording aborted by stop key, {n} events kept')
        else:   # playing
            self._set_macro_state('ready')
            self._macro_unregister_stop_keys()
            self.root.after(0, self.macro_overlay.show_macro_stopped)
            logger.info('Macro playback aborted by stop key')

    def _macro_register_stop_keys(self) -> None:
        # Esc is handled by the permanent _hk_escape (which checks macro state),
        # so we only add Delete here to avoid a double-Esc handler.
        self._macro_stop_hks = [
            kbhook.add_hotkey('delete', lambda: self._q.put(('macro:stop', None))),
        ]

    def _macro_unregister_stop_keys(self) -> None:
        for hk in self._macro_stop_hks:
            try:
                kbhook.remove_hotkey(hk)
            except Exception:
                pass
        self._macro_stop_hks = []

    # ── Screen recorder ───────────────────────────────────────────────────────

    def _on_recorder_toggle(self) -> None:
        """Shift+F2 or Library tab button, starts or stops screen recording."""
        if self._recorder_state == 'idle':
            self._recorder_start()
        elif self._recorder_state == 'recording':
            self._recorder_stop()

    def _recorder_start(self) -> None:
        """Start recording immediately (full screen, no mic, 30 fps), no setup dialog."""
        self._screen_recorder = ScreenRecorder(
            hwnd=0,
            mon=None,
            mic=False,
            mic_device=None,
            fps=30,
            on_size_update=lambda b: self._q.put(('recorder:size', b)),
            on_cap_reached=lambda: self._q.put(('recorder:cap', None)),
        )
        try:
            self._screen_recorder.start()
        except Exception as exc:
            logger.error(f'Screen recorder failed to start: {exc}')
            from dialogs import alert
            # Sanitize raw exception text, drop the Python class prefix
            # and trim long stack-trace-like content.
            _txt = str(exc).strip() or 'Unknown recorder failure.'
            alert(self.root, 'Screen recorder failed',
                  f'{_txt[:240]}\n\nClose any window blocking screen capture '
                  'and try again.')
            self._screen_recorder = None
            return

        self._recorder_state = 'recording'
        self._recorder_t0    = time.time()
        self.recorder_overlay.show_recorder_recording()
        self._update_library_recorder_state()
        self._recorder_tick()
        logger.info('Screen recording started')

    def _recorder_stop(self) -> None:
        """Stop recording and show save dialog."""
        if self._screen_recorder is None:
            return
        self._recorder_state = 'stopping'
        self.recorder_overlay.show_recorder_stopping()
        self._update_library_recorder_state()

        rec = self._screen_recorder

        def _finish():
            rec.stop()
            self.root.after(0, lambda: self._recorder_finish(rec))

        threading.Thread(target=_finish, daemon=True, name='rec-stop').start()

    def _recorder_finish(self, rec: ScreenRecorder) -> None:
        """Called on main thread after encoding is complete."""
        self._screen_recorder = None
        self._recorder_state  = 'idle'
        self.recorder_overlay._close()
        self._update_library_recorder_state()
        logger.info(f'Screen recording stopped, {rec.bytes_written/1024**2:.1f} MB')

        if rec.error:
            from dialogs import alert
            alert(self.root, 'Recorder error', rec.error)
            return
        if not rec.output_path or not os.path.exists(rec.output_path):
            return
        if os.path.getsize(rec.output_path) == 0:
            from dialogs import alert
            try:
                os.unlink(rec.output_path)
            except Exception:
                pass
            alert(self.root, 'Recording failed',
                  'The output file is empty, the encoder produced no data.\n\n'
                  'This can happen if the recording was stopped too quickly\n'
                  'or if the screen capture failed to initialise.')
            return

        dur     = int(rec.elapsed())
        size_mb = os.path.getsize(rec.output_path) / (1024 ** 2)
        try:
            parent = self.library.win if self.library.win.winfo_ismapped() else self.root
        except Exception:
            parent = self.root
        dest = show_save_dialog(parent, rec.output_path, dur, size_mb)
        if dest:
            logger.info(f'Recording saved: {dest}')
            # Track path in index so it shows in the list regardless of save location
            try:
                from screen_recorder import add_to_recordings_index
                add_to_recordings_index(dest)
            except Exception:
                pass
            # Refresh the library recorder tab list
            if hasattr(self, 'library'):
                self.library.update_recorder_state('idle')

    def _on_recorder_cap(self) -> None:
        """1 GB cap hit, auto-stop."""
        logger.info('Screen recording: 1 GB cap reached, stopping')
        from dialogs import alert
        self._recorder_stop()
        self.root.after(500, lambda: alert(
            self.root, '1 GB limit reached',
            'The recording reached the 1 GB size cap\nand has been stopped automatically.'))

    def _recorder_tick(self) -> None:
        """Called every 500ms while recording to push live state to the library tab."""
        if self._recorder_state != 'recording' or self._screen_recorder is None:
            return
        elapsed = time.time() - self._recorder_t0
        size_mb = self._screen_recorder.bytes_written / (1024 ** 2)
        self._update_library_recorder_state(elapsed=elapsed, size_mb=size_mb)
        self.root.after(500, self._recorder_tick)

    def _update_library_recorder_state(self, elapsed: float = 0.0, size_mb: float = 0.0) -> None:
        try:
            self.library.update_recorder_state(self._recorder_state, elapsed, size_mb)
        except Exception:
            pass

    # ── GIF recorder ─────────────────────────────────────────────────────────

    def _on_gif_toggle(self) -> None:
        """Shift+F3 / button press, start or stop GIF recording."""
        if self._gif_setup_dlg is not None:
            return  # setup dialog already open, ignore duplicate presses
        if self._gif_state == 'idle':
            self._gif_start()
        elif self._gif_state == 'recording':
            self._gif_stop()
        # 'encoding', ignore, let it finish

    def _gif_start(self) -> None:
        """Show setup dialog, then begin capturing."""
        self._gif_setup_dlg = True   # sentinel, set before Toplevel creation
        try:
            mapped = self.library.win.winfo_ismapped()
            parent = self.library.win if mapped else self.root
        except Exception:
            mapped = False
            parent = self.root
        try:
            dlg = GifSetupDialog(parent)
        except Exception as exc:
            logger.exception(f'GIF setup dialog creation failed: {exc}')
            self._gif_setup_dlg = None
            return
        self._gif_setup_dlg = dlg
        parent.wait_window(dlg.win)
        self._gif_setup_dlg = None
        if dlg.result is None:
            return   # user cancelled

        cfg = dlg.result
        logger.info(f'GIF setup: {cfg}')
        try:
            self._gif_recorder = GifRecorder(
                hwnd=cfg['hwnd'],
                mon=cfg.get('mon'),
                fps=cfg['fps'],
                max_width=cfg['max_width'],
                max_duration_s=cfg['max_duration_s'],
            )
            self._gif_recorder.start(
                on_done=lambda path, dur: self._q.put(('gif:done', (path, dur))),
                on_error=lambda msg: self._q.put(('gif:error', msg)),
                on_cap_reached=lambda: self._q.put(('gif:cap', None)),
            )
        except Exception as exc:
            logger.error(f'GIF recorder failed to start: {exc}')
            from dialogs import alert
            _txt = str(exc).strip() or 'Unknown GIF recorder failure.'
            alert(self.root, 'GIF recorder failed',
                  f'{_txt[:240]}\n\nClose any window blocking screen capture '
                  'and try again.')
            self._gif_recorder = None
            return

        self._gif_state = 'recording'
        self._gif_t0    = time.time()
        self.gif_overlay.show_gif_recording()
        self._update_library_gif_state()
        self._gif_tick()
        logger.info('GIF recording started')

    def _gif_stop(self) -> None:
        """Signal capture to stop; encoding happens in background."""
        if self._gif_recorder is None:
            return
        self._gif_state = 'encoding'
        self.gif_overlay.show_gif_encoding()
        self._update_library_gif_state()
        self._gif_recorder.stop()

    def _on_gif_done(self, data) -> None:
        """Called on main thread when encoding finishes successfully."""
        tmp_path, dur = data
        self._gif_recorder = None
        self._gif_state    = 'idle'
        self.gif_overlay._close()
        self._update_library_gif_state()
        elapsed = int(dur)
        logger.info(f'GIF recording complete, {elapsed}s, {tmp_path}')

        if not tmp_path or not os.path.exists(tmp_path):
            return

        try:
            parent = self.library.win if self.library.win.winfo_ismapped() else self.root
        except Exception:
            parent = self.root

        dest = show_gif_save_dialog(parent, tmp_path, dur)
        if dest:
            logger.info(f'GIF saved: {dest}')
            # Track path in index so it shows in the list regardless of save location
            try:
                add_to_gif_index(dest)
            except Exception:
                pass
            # Refresh library GIF tab
            try:
                self.library.update_gif_state('idle')
            except Exception:
                pass

    def _on_gif_error(self, msg: str) -> None:
        self._gif_recorder = None
        self._gif_state    = 'idle'
        self.gif_overlay._close()
        self._update_library_gif_state()
        logger.error(f'GIF recorder error: {msg}')
        from dialogs import alert
        alert(self.root, 'GIF error', msg)

    def _on_gif_cap(self) -> None:
        """Max duration cap reached, auto-stop."""
        if self._gif_recorder is None:
            return
        logger.info('GIF recording: max duration reached, stopping')
        dur_s = int(self._gif_recorder.max_duration_s)
        self.gif_overlay.show_gif_capped(dur_s)
        self._gif_state = 'encoding'
        self._update_library_gif_state()
        self._gif_recorder.stop()

    def _gif_tick(self) -> None:
        """Push live elapsed/frame count to the library tab every 500ms."""
        if self._gif_state != 'recording' or self._gif_recorder is None:
            return
        elapsed = time.time() - self._gif_t0
        frames  = self._gif_recorder.frame_count
        self._update_library_gif_state(elapsed=elapsed, frames=frames)
        self.root.after(500, self._gif_tick)

    def _update_library_gif_state(self, elapsed: float = 0.0, frames: int = 0) -> None:
        try:
            self.library.update_gif_state(self._gif_state, elapsed, frames)
        except Exception:
            pass

    # ── System tray ───────────────────────────────────────────────────────────

    def _make_icon(self) -> Image.Image:
        # Render at 8× then downsample to 64×64 for clean anti-aliased edges.
        S = 8
        B = 64 * S   # 512 px working canvas

        def _hex(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

        def _grad_mask(mask, c1, c2):
            """Apply a top→bottom gradient through a white-on-black mask."""
            r1,g1,b1 = _hex(c1); r2,g2,b2 = _hex(c2)
            grad = Image.new('RGBA', (B, B))
            dg   = ImageDraw.Draw(grad)
            for y in range(B):
                t = y / (B - 1)
                dg.line([(0,y),(B,y)], fill=(
                    int(r1+(r2-r1)*t), int(g1+(g2-g1)*t), int(b1+(b2-b1)*t), 255))
            out = Image.new('RGBA', (B, B), (0,0,0,0))
            out.paste(grad, mask=mask.split()[0])
            return out

        # ── Background: purple border + dark fill ─────────────────────────────
        base = Image.new('RGBA', (B, B), (0,0,0,0))
        d    = ImageDraw.Draw(base)
        d.rounded_rectangle([0, 0, B-1, B-1], radius=13*S, fill='#7c3aed')   # ACCENT border
        d.rounded_rectangle([3*S, 3*S, B-1-3*S, B-1-3*S], radius=11*S, fill='#080f1a')

        # ── Lightning bolt polygon ────────────────────────────────────────────
        BOLT = [(x*S, y*S) for x,y in [(42,4),(10,34),(28,34),(22,60),(52,26),(36,26)]]

        bolt_mask = Image.new('RGBA', (B, B), (0,0,0,0))
        ImageDraw.Draw(bolt_mask).polygon(BOLT, fill='white')

        # Glow layer
        from PIL import ImageFilter as _IF
        glow = _grad_mask(bolt_mask.filter(_IF.GaussianBlur(12)), '#7dd3fc', '#1e40af')
        base = Image.alpha_composite(base, glow)

        # Sharp bolt, sky blue top → deep navy bottom
        base = Image.alpha_composite(base, _grad_mask(bolt_mask, '#bae6fd', '#0f2a6e'))

        # Downsample to final 64×64
        return base.resize((64, 64), Image.LANCZOS)

    # ── IPC socket (localhost:58765) ──────────────────────────────────────────

    def _start_ipc(self) -> None:
        threading.Thread(target=self._ipc_loop, daemon=True, name='ipc').start()

    def _ipc_loop(self) -> None:
        import socket as _sock
        _VALID = {'library', 'notes', 'refine', 'ask', 'recorder',
                  'gif_record', 'macro_record', 'web', 'chain', 'whiteboard',
                  'transcribe', 'audio_editor',
                  # Destructive actions still go through the confirm dialog
                  # before they touch state, safe to expose for scripting.
                  'restore_all_defaults', 'reload_hotkeys'}
        # 'whisper' is a toggle that already handles start/stop based on
        # state, route it through _hk_whisper instead of a queued event.
        _DIRECT_CALL = {'whisper': lambda: self._hk_whisper()}
        try:
            with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as srv:
                srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
                srv.bind(('127.0.0.1', 58765))
                srv.listen(5)
                while True:
                    try:
                        conn, _ = srv.accept()
                        with conn:
                            cmd = conn.recv(64).decode().strip()
                            logger.info(f'IPC: received {cmd!r}')
                            if cmd in _VALID:
                                self._q.put((cmd, None))
                                logger.info(f'IPC: queued {cmd!r}')
                            elif cmd in _DIRECT_CALL:
                                self.root.after(0, _DIRECT_CALL[cmd])
                                logger.info(f'IPC: dispatched {cmd!r}')
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f'IPC listener failed: {e}')

    def _save_brand_ico(self) -> str:
        """Materialize the brand .ico in %APPDATA% if missing. Same icon
        the PyInstaller build embeds as the .exe resource (see
        brand_icon.save_ico in the spec)."""
        from pathlib import Path
        ico_path = Path(appdata_dir()) / 'app_icon.ico'
        try:
            if not ico_path.exists():
                from brand_icon import save_ico
                save_ico(str(ico_path))
            return str(ico_path)
        except Exception as e:
            logger.warning(f'_save_brand_ico failed: {e}')
            return ''

    def _start_tray(self) -> None:
        # Make our brand icon available to every Tk Toplevel we'll spawn,
        # Notes, Library, Settings, History, etc. all inherit `default`.
        try:
            ico = self._save_brand_ico()
            if ico:
                self.root.iconbitmap(default=ico)
                logger.info(f'Brand icon installed: {ico}')
        except Exception as e:
            logger.warning(f'iconbitmap default failed: {e}')

        # Build the pystray Icon defensively — any of _make_icon /
        # _tooltip / _make_menu can raise in frozen mode if a resource
        # is missing, and we want the rest of the app to keep working
        # rather than the whole process exiting silently.
        try:
            logger.info('DEBUG: about to _make_icon')
            _icon_img = self._make_icon()
            logger.info('DEBUG: _make_icon done')
            _tip = self._tooltip()
            logger.info('DEBUG: _tooltip done')
            _menu = self._make_menu()
            logger.info('DEBUG: _make_menu done')
            self._tray = pystray.Icon('Hotkeys', _icon_img, _tip, _menu)
            logger.info('DEBUG: pystray.Icon constructed')
            t = threading.Thread(target=self._run_tray, daemon=True)
            t.start()
            logger.info('Tray started.')
        except Exception as e:
            logger.exception(f'Tray init failed: {e}')
            self._tray = None

    def _run_tray(self) -> None:
        """Run the pystray event loop. On macOS, AppKit must be invoked carefully
        from a background thread, log any failure clearly instead of crashing silently."""
        try:
            self._tray.run()
        except Exception as e:
            logger.error(f'Tray crashed: {e}')
            # On macOS pystray may fail if AppKit isn't available on this thread.
            # The app continues working (hotkeys, transcription), only the tray icon is lost.
            if sys.platform == 'darwin':
                logger.error(
                    'macOS tray error, this usually means pystray could not access AppKit. '
                    'The app will keep running but the menu bar icon will be missing. '
                    'Check that pyobjc-framework-Cocoa is installed: pip install pyobjc-framework-Cocoa'
                )

    def _make_menu(self) -> pystray.Menu:
        """Build the right-click tray menu.

        Two-section layout designed for scannability:
          • TOP, what you can DO (the user-facing features)
          • MIDDLE, what you can RECORD
          • BOTTOM, settings / history / reset / quit
        Status line under the header shows online/cloud state at a glance.
        Every action lists its hotkey on the right so users learn the
        bindings without opening Settings.
        """
        def prov_item(key: str, label: str) -> pystray.MenuItem:
            return pystray.MenuItem(
                label,
                lambda: self._q.put(('switch_provider', key)),
                checked=lambda item, k=key: self.config.get('active_provider') == k,
                radio=True,
            )

        hk = self._hotkey_cfg()
        recording = bool(self._whisper_recording)

        lib_hk        = hk.get('library',      'alt+shift+e').upper()
        notes_hk      = hk.get('notes',         'shift+f7').upper()
        whisper_hk    = hk.get('whisper',       'ctrl+enter').upper()
        refine_hk     = hk.get('refine',        'alt+shift+w').upper()
        recorder_hk   = hk.get('recorder',      'shift+f2').upper()
        gif_hk        = hk.get('gif_record',    'shift+f3').upper()
        macro_hk      = hk.get('macro_record',  'shift+f1').upper()
        ask_hk        = hk.get('ask',           'shift+f4').upper()
        web_hk        = hk.get('web',           'shift+f5').upper()
        chain_hk      = hk.get('chain',         'shift+f6').upper()
        whiteboard_hk = hk.get('whiteboard',    'shift+f8').upper()
        transcribe_hk = hk.get('transcribe',    'shift+f9').upper()
        audio_edit_hk = hk.get('audio_editor',  'shift+f10').upper()

        # Status line, at-a-glance "what's happening" + the value prop.
        # Designed so a layperson seeing this menu for the first time
        # understands the app in 3 seconds: "press a key, AI does the rest"
        # is the entire elevator pitch on one line. When recording, swap
        # to feedback so the user knows the mic is hot.
        if recording:
            status_line = '🔴  Recording your voice…'
        else:
            status_line = 'Press a key — your AI does the rest'

        # ── WHAT YOU CAN DO ──────────────────────────────────────────────────
        # Every label here is an OUTCOME verb ("Speak to type") not an
        # internal feature name ("Start dictation"). A first-time user
        # reading these should understand what each one does without ever
        # opening Settings or a tutorial.
        do_items = [
            pystray.MenuItem(
                f'{"🛑  Stop recording" if recording else "🎙  Speak to type"}'
                f'           {whisper_hk}',
                lambda: self._q.put(('whisper:start', None) if not recording
                                    else ('whisper:stop', None)),
            ),
            pystray.MenuItem(
                f'✨  Transform my text             {refine_hk}',
                self._hk_refine,
            ),
            pystray.MenuItem(
                f'💬  Explain or answer something   {ask_hk}',
                lambda: self._q.put(('ask', '')),
            ),
            pystray.MenuItem(
                f'📝  Jot a quick note              {notes_hk}',
                lambda: self._q.put(('notes', None)),
            ),
            pystray.MenuItem(
                f'📚  Open Library                  {lib_hk}',
                lambda: self._q.put(('library', None)),
            ),
            pystray.MenuItem(
                f'🎬  Turn audio or video into text {transcribe_hk}',
                lambda: self._q.put(('transcribe', None)),
            ),
            pystray.MenuItem(
                f'🎨  Sketch on a whiteboard        {whiteboard_hk}',
                lambda: self._q.put(('whiteboard', None)),
            ),
            pystray.MenuItem(
                f'🎵  Open audio editor             {audio_edit_hk}',
                lambda: self._q.put(('audio_editor', None)),
            ),
            pystray.MenuItem(
                f'🌐  Jump to a favourite site      {web_hk}',
                lambda: self._q.put(('web', None)),
            ),
            pystray.MenuItem(
                f'🔗  Run a multi-step workflow     {chain_hk}',
                lambda: self._q.put(('chain', None)),
            ),
        ]

        # ── RECORD ──────────────────────────────────────────────────────────
        record_items = [
            pystray.MenuItem(
                f'📸  Take a screenshot             PRINTSCREEN',
                lambda: self._hk_screenshot(),
            ),
            pystray.MenuItem(
                f'⏺  Record my screen              {recorder_hk}',
                lambda: self._q.put(('recorder:toggle', None)),
            ),
            pystray.MenuItem(
                f'🎞  Capture a GIF                 {gif_hk}',
                lambda: self._q.put(('gif:toggle', None)),
            ),
            pystray.MenuItem(
                f'⚡  Record a clicks/keys macro    {macro_hk}',
                lambda: self._q.put(('macro:hotkey', None)),
            ),
        ]

        # ── APP ─────────────────────────────────────────────────────────────
        app_items = [
            pystray.MenuItem('🤖  AI Brain', pystray.Menu(
                *([prov_item('local', 'Qwen 2.5 1.5B  (Local · Free)')]
                  if local_provider_available() else []),
                # Cerebras serves Llama on dedicated inference hardware,
                # it's measurably the fastest for Refine / Ask / Chain.
                # Groq is still fast but second; it's also the only one
                # that exposes a Whisper endpoint, so dictation + F9
                # transcribe always route through Groq regardless of which
                # one is picked here.
                prov_item('cerebras', 'Cerebras  (fastest)'),
                prov_item('groq',     'Groq  (also fast)'),
            )),
            pystray.MenuItem('🎤  Dictation mode', pystray.Menu(
                pystray.MenuItem(
                    'Push-to-talk',
                    self._toggle_ptt,
                    checked=lambda item: self.config.get('push_to_talk', False),
                ),
            )),
            pystray.MenuItem('🕒  History',
                             lambda: self._q.put(('history', None))),
            pystray.MenuItem('⚙  Settings…',
                             lambda: self._q.put(('settings', None))),
        ]

        # ── RESET / RELOAD ──────────────────────────────────────────────────
        # Layperson-friendly labels, "Stop everything" is the panic button,
        # findable when something feels stuck. "Reset everything" is the
        # factory-reset, intentionally further down the menu.
        reset_items = [
            pystray.MenuItem('🛑  Stop everything & reload hotkeys',
                             lambda: self._q.put(('reload_hotkeys', None))),
            pystray.MenuItem('↺  Reset everything…',
                             lambda: self._q.put(('restore_all_defaults', None))),
        ]

        return pystray.Menu(
            # Header with name and live status
            pystray.MenuItem(f'Hotkeys  v{VERSION}', None, enabled=False),
            pystray.MenuItem(status_line, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('— What you can do —', None, enabled=False),
            *do_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('— Capture what you see —', None, enabled=False),
            *record_items,
            pystray.Menu.SEPARATOR,
            *app_items,
            pystray.Menu.SEPARATOR,
            *reset_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit Hotkeys', self._quit),
        )

    def _switch_provider(self, key: str) -> None:
        if self.config.get('active_provider') == key:
            return   # already on this provider, nothing to do
        self.config['active_provider'] = key
        save_config(self.config)
        self.provider = build_provider(self.config)
        if isinstance(self.provider, LocalProvider) and not self.provider.ready:
            threading.Thread(target=self._load_model, daemon=True).start()
        self._update_tray()
        logger.info(f'Switched to provider: {key}')

    def _toggle_ptt(self) -> None:
        self.config['push_to_talk'] = not self.config.get('push_to_talk', False)
        save_config(self.config)
        self._register_hotkeys()
        self._update_tray()
        state = 'ON' if self.config['push_to_talk'] else 'OFF'
        logger.info(f'Push-to-talk toggled: {state}')

    def _tooltip(self) -> str:
        active  = self.config.get('active_provider', 'cerebras')
        r_state = 'Ready' if self.provider.ready else 'Loading…'
        w_state = '🔴 Recording' if self._whisper_recording else 'Idle'
        return f'Hotkeys  ·  {active.title()}  ·  {r_state}  ·  Whisper: {w_state}'

    def _update_tray(self) -> None:
        try:
            self._tray.title = self._tooltip()
            self._tray.menu  = self._make_menu()
        except Exception:
            pass

    def _notify(self, title: str, msg: str) -> None:
        try:
            self._tray.notify(msg, title)
        except Exception:
            pass

    def _watch_singleton_socket(self) -> None:
        """Background thread: waits for a new instance to signal QUIT.

        The TCP connection itself is proof a new instance is running, we
        do not do a secondary PID check, because in dist builds the process
        name / cmdline heuristic is unreliable during the brief startup window.
        """
        if not _singleton_sock:
            return
        while True:
            try:
                conn, _ = _singleton_sock.accept()
                try:
                    conn.recv(16)
                finally:
                    conn.close()
                logger.info('New instance launched, shutting down gracefully.')
                self.root.after(0, self._quit)
                return
            except Exception:
                return   # socket closed during normal _quit()

    def _quit(self) -> None:
        logger.info('Shutting down.')

        # Schedule a hard kill in case any cleanup step hangs
        def _force_exit():
            logger.warning('Forced exit after timeout.')
            os._exit(0)
        _killer = threading.Timer(5.0, _force_exit)
        _killer.daemon = True
        _killer.start()

        # Singleton socket — SO_LINGER (1, 0) + shutdown() forces an
        # immediate TCP RST instead of FIN, so port 58765 is released
        # right away with no TIME_WAIT window. Without this, the next
        # launch can occasionally see "port already in use" for up to
        # 2 minutes on Windows even though we're already gone.
        try:
            if _singleton_sock:
                try:
                    import struct as _struct
                    _singleton_sock.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER,
                        _struct.pack('ii', 1, 0))
                except Exception:
                    pass
                try:
                    _singleton_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                _singleton_sock.close()
        except Exception:
            pass
        try:
            kbhook.stop()
        except Exception:
            pass
        keyboard.unhook_all()
        try:
            self._audio.stop()
        except Exception:
            pass
        try:
            self._transcriber.shutdown()
        except Exception:
            pass
        # Audio editor (Tenacity) might still be a live child process.
        # Without this it survives our quit as a phantom — user closes
        # the Hotkeys tray and finds an "Audio Editor" window still
        # open, with no obvious way to relate it to Hotkeys.
        try:
            import audio_editor as _ae
            _ae.get_launcher().shutdown()
        except Exception:
            pass
        # Belt-and-suspenders tray icon removal. pystray.stop() usually
        # fires Shell_NotifyIcon(NIM_DELETE) but if it doesn't, the
        # icon stays in the tray as a ghost until you hover over it.
        # We explicitly remove via Win32 immediately after pystray.stop.
        try:
            self._tray.visible = False
            self._tray.stop()
            time.sleep(0.6)   # let pystray finish its own NIM_DELETE first
            try:
                _sweep_ghost_tray_icons()
            except Exception:
                pass
        except Exception:
            pass
        _killer.cancel()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        sys.exit(0)

    def run(self) -> None:
        self.root.mainloop()


# ── Single-instance guard ─────────────────────────────────────────────────────

# ── Single-instance guard ─────────────────────────────────────────────────────
_singleton_sock: socket.socket | None = None
_SINGLETON_PORT = 47_294   # localhost IPC port


def _find_other_hotkeys_pids() -> list[int]:
    """Return PIDs of other TOP-LEVEL Hotkeys instances only.

    Works for both frozen dist builds (Hotkeys.exe / Hotkeys) and source
    runs (python / pythonw / python3 … main.py).

    Excludes our entire lineage (descendants AND ancestors) so we never
    accidentally kill the venv launcher (our parent) which would collapse
    its Windows Job Object and kill us too.
    """
    try:
        import psutil
    except ImportError:
        return []

    my_pid    = os.getpid()
    is_frozen = getattr(sys, 'frozen', False)   # True when bundled by PyInstaller

    # Build the set of PIDs we must never touch: us, our children, our parents.
    safe: set[int] = {my_pid}
    try:
        for c in psutil.Process(my_pid).children(recursive=True):
            safe.add(c.pid)
    except Exception:
        pass
    try:
        p = psutil.Process(my_pid)
        while True:
            p = p.parent()
            if p is None:
                break
            safe.add(p.pid)
    except Exception:
        pass

    # Exe names to match (both platforms, case-insensitive)
    FROZEN_NAMES  = {'hotkeys.exe', 'hotkeys'}
    SOURCE_NAMES  = {'pythonw.exe', 'python.exe', 'python3', 'python',
                     'python3.11', 'python3.12', 'hotkeys.exe', 'hotkeys'}

    candidates: dict[int, int] = {}   # pid → parent_pid
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'ppid']):
        try:
            if proc.pid in safe:
                continue
            name = (proc.info['name'] or '').lower()

            if is_frozen:
                # Dist build: just match by executable name, no cmdline needed
                if name in FROZEN_NAMES:
                    candidates[proc.pid] = proc.info.get('ppid') or 0
            else:
                # Source run: python interpreter running main.py inside Hotkeys dir
                if name not in SOURCE_NAMES:
                    continue
                cmdline = ' '.join(proc.info['cmdline'] or []).lower()
                if 'main.py' in cmdline and 'hotkeys' in cmdline:
                    candidates[proc.pid] = proc.info.get('ppid') or 0
        except Exception:
            pass

    # Keep only roots (parent is not itself a candidate) to avoid double-counting
    return [pid for pid, ppid in candidates.items() if ppid not in candidates]


def _sweep_ghost_tray_icons() -> None:
    """Simulate mouse movement across the Windows notification-area toolbars.

    When Windows receives WM_MOUSEMOVE over a tray slot whose owner process is
    dead, it removes that icon automatically, no user hover needed.
    Covers both the visible tray and the overflow (hidden icons) area.
    """
    if sys.platform != 'win32':
        return
    try:
        import struct
        u32 = ctypes.windll.user32
        WM_MOUSEMOVE = 0x0200

        def _child(parent: int, cls: str) -> int:
            return u32.FindWindowExW(parent, None, cls, None)

        def _sweep(toolbar: int) -> None:
            if not toolbar:
                return
            buf = ctypes.create_string_buffer(16)
            u32.GetClientRect(toolbar, buf)
            _, _, w, h = struct.unpack('iiii', buf.raw)
            mid_y = (h // 2) & 0xFFFF
            for x in range(0, max(w, 1), 4):
                u32.SendMessageW(toolbar, WM_MOUSEMOVE, 0, (x & 0xFFFF) | (mid_y << 16))

        # Primary notification area
        tray    = u32.FindWindowW('Shell_TrayWnd', None)
        notify  = _child(tray,   'TrayNotifyWnd')
        pager   = _child(notify, 'SysPager')
        _sweep(_child(pager, 'ToolbarWindow32'))

        # Overflow (hidden icons) area
        overflow = u32.FindWindowW('NotifyIconOverflowWindow', None)
        _sweep(_child(overflow, 'ToolbarWindow32'))
    except Exception:
        pass


def _ensure_single_instance(_depth: int = 0) -> None:
    """Guarantee exactly one running copy.

    On Windows a named mutex serialises concurrent launches.
    On macOS/Linux the socket-based approach is used directly.
    """
    global _singleton_sock
    import psutil

    # ── Windows: use a named mutex to serialise concurrent launches ───────────
    if sys.platform == 'win32':
        kernel32   = ctypes.windll.kernel32
        MUTEX_NAME = 'Hotkeys_StartupLock_v3'

        mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        err   = kernel32.GetLastError()

        if err == 183:      # ERROR_ALREADY_EXISTS, another launch is starting
            kernel32.CloseHandle(mutex)
            if _depth >= 3:
                sys.exit(1)
            time.sleep(4.0)
            if _find_other_hotkeys_pids():
                sys.exit(0)
            _ensure_single_instance(_depth + 1)
            return

    # ── All platforms: graceful quit + hard-kill + socket bind ───────────────

    try:
        # 1. Graceful quit via socket
        c = socket.create_connection(('127.0.0.1', _SINGLETON_PORT), timeout=1)
        c.sendall(b'QUIT')
        c.close()
        time.sleep(2.5)
    except Exception:
        pass

    # 2. Hard-kill anything still alive
    for pid in _find_other_hotkeys_pids():
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            proc.kill()
        except Exception:
            pass

    # 3. Actively sweep the notification area to evict ghost icons from dead
    #    processes (no hovering required), then give the OS a moment to settle.
    _sweep_ghost_tray_icons()
    time.sleep(0.8)

    # 4. Bind socket as graceful-quit channel for the NEXT launch
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', _SINGLETON_PORT))
        s.listen(5)
        _singleton_sock = s
    except Exception:
        pass

    if sys.platform == 'win32':
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


# ── macOS accessibility permission ────────────────────────────────────────────

def _mac_ensure_accessibility() -> None:
    """macOS only: block startup until Accessibility permission is granted.

    Global hotkeys (keyboard library) require the Accessibility entitlement.
    If not yet granted, open System Settings to the right pane and show a
    clear CTk dialog that waits until the user has toggled the switch.
    Silently returns on Windows/Linux or if already trusted.
    """
    if sys.platform != 'darwin':
        return

    try:
        from ctypes import cdll
        _libax = cdll.LoadLibrary(
            '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices'
        )
        _is_trusted = lambda: bool(_libax.AXIsProcessTrusted())
    except Exception:
        return   # Can't check, proceed; keyboard will fail naturally if needed

    if _is_trusted():
        return

    import subprocess as _sp

    # Open the Accessibility pane in System Settings automatically
    _sp.Popen(['open',
               'x-apple.systempreferences:'
               'com.apple.preference.security?Privacy_Accessibility'])

    # Build a blocking CTk dialog, auto-closes the moment permission is granted
    _setup_root = ctk.CTk()
    _setup_root.withdraw()

    _win = ctk.CTkToplevel(_setup_root)
    _win.title('Hotkeys, One-time Setup')
    _win.resizable(False, False)
    _win.attributes('-topmost', True)
    _win.geometry('460x340')
    _win.protocol('WM_DELETE_WINDOW', lambda: None)   # prevent accidental close

    ctk.CTkLabel(_win, text='⚡  Almost ready!',
                 font=ctk.CTkFont(size=22, weight='bold')).pack(pady=(30, 8))

    ctk.CTkLabel(_win,
                 text=(
                     'Hotkeys needs one permission to work.\n'
                     'System Settings has opened for you, just:\n'
                 ),
                 font=ctk.CTkFont(size=14)).pack()

    ctk.CTkLabel(_win,
                 text='1.  Find Hotkeys in the list\n2.  Flip the switch  ON',
                 font=ctk.CTkFont(size=16, weight='bold'),
                 justify='left').pack(pady=8)

    ctk.CTkLabel(_win,
                 text="That's it. Hotkeys will start automatically.",
                 font=ctk.CTkFont(size=13),
                 text_color='#94a3b8').pack()

    # Animated waiting indicator
    _dots = ['', '.', '..', '...']
    _dot_idx = [0]
    _wait_lbl = ctk.CTkLabel(_win, text='Waiting for permission...',
                             font=ctk.CTkFont(size=12), text_color='#7c3aed')
    _wait_lbl.pack(pady=(16, 0))

    def _poll():
        if _is_trusted():
            _setup_root.quit()
            return
        _dot_idx[0] = (_dot_idx[0] + 1) % len(_dots)
        _wait_lbl.configure(text=f'Waiting for permission{_dots[_dot_idx[0]]}')
        _setup_root.after(500, _poll)

    _setup_root.after(500, _poll)
    _setup_root.mainloop()
    try:
        _setup_root.destroy()
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

def _set_app_user_model_id():
    """Tell Windows to treat us as a distinct app, not pythonw.exe,
    so the taskbar / Alt+Tab / jump list use OUR icon and grouping
    instead of falling back to the generic Python logo.

    Must run BEFORE any window is created. AUMID is process-wide;
    the whiteboard subprocess sets the same string so its window
    groups under the same app entry."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'Hotkeys.App.1')
    except Exception:
        pass


if __name__ == '__main__':
    _set_app_user_model_id()
    # NOTE: the --whiteboard short-circuit moved to the TOP of this file so
    # the subprocess does not waste time importing heavy modules. That fork
    # exits before this code ever runs in subprocess mode.

    # pystray uses multiprocessing on Windows and spawns a child process with
    # the exact same command line (pythonw.exe main.py).  That child re-imports
    # __main__, so __name__ == '__main__' is True inside it too.  We MUST skip
    # _ensure_single_instance() and App() in that child or it will kill us.
    # multiprocessing.current_process().name is 'MainProcess' only in the real
    # user-launched process; spawned workers get names like 'Process-1'.
    import multiprocessing as _mp
    if _mp.current_process().name == 'MainProcess':
        _mac_ensure_accessibility()   # no-op on Windows; blocks until permission granted on Mac
        _ensure_single_instance()
        app = App()
        import signal
        signal.signal(signal.SIGTERM, lambda *_: app._quit())
        signal.signal(signal.SIGINT,  lambda *_: app._quit())
        app.run()
    # else: we are pystray's multiprocessing worker, do nothing here;
    # multiprocessing's spawn handler will call the real target function.
