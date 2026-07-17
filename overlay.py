"""
Floating status pill, appears near the cursor during refinement or recording.
True pill shape via Canvas + Windows transparentcolor trick.
Slot 0 = text-refine pill  (Y offset 0)
Slot 1 = whisper pill      (Y offset +50 px, stacks below refine pill)
"""
import time
import tkinter as tk
from tkinter.font import Font as TkFont

from theme import ACCENT, OK, WARN, ERR, INFO, FONT_FAMILY, FONT_MONO, SURFACE, BORDER, TEXT_P

# Pill styling
_PILL_BG    = SURFACE      # '#141414', dark surface for pill background
_BORDER_CLR = BORDER       # '#2a2a2a', subtle separator border
_TEXT_CLR   = TEXT_P       # '#f0f0f0', primary text
_TRANSP     = '#010101'    # Windows transparentcolor (near-black = transparent)
_RADIUS     = 20
_PAD_X      = 22
_PAD_Y      = 12
_FONT       = (FONT_FAMILY, 11)
_FONT_TIMER = (FONT_MONO, 11)

_SLOT_OFFSET = 50   # px between stacked pills


# Module-level "cursor at the moment the last hotkey fired". Hotkey
# handlers should call latch_hotkey_cursor() AS THEIR FIRST LINE so the
# pill that eventually appears (potentially seconds later, after a
# 0.5 s shift-release wait + 0.5-2.5 s clipboard polling + LLM round-
# trip) reflects where the user was looking when they pressed the
# hotkey — NOT wherever their mouse has drifted in the meantime.
_hotkey_cursor_xy: tuple[int, int] | None = None
_hotkey_cursor_ts: float = 0.0


def latch_hotkey_cursor() -> None:
    """Snapshot the current mouse-cursor position to be used by the next
    pill_anchor_xy() call. Cheap (a Win32 GetCursorPos via ctypes); safe
    to call from any thread. Hotkey callbacks should call this on entry
    so the eventual pill anchors to the spot the user was looking at,
    not to wherever their mouse ended up after the capture + LLM round-
    trip."""
    global _hotkey_cursor_xy, _hotkey_cursor_ts
    try:
        import ctypes, time as _time
        # POINT.x, POINT.y are LONG (4 bytes) on x86-64 Windows.
        class _POINT(ctypes.Structure):
            _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]
        pt = _POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        _hotkey_cursor_xy = (int(pt.x), int(pt.y))
        _hotkey_cursor_ts = _time.time()
    except Exception:
        # Fall through silently — pill_anchor_xy will use the live
        # winfo_pointerxy fallback.
        pass


def pill_anchor_xy(root: tk.Tk) -> tuple[int, int]:
    """Return (x, y) screen coords for a status pill — at the mouse
    cursor + small offset.

    Prefers the position captured by latch_hotkey_cursor() at the moment
    the user pressed the hotkey, so the pill lands where they were
    looking even if they've moved the mouse during the LLM round-trip.
    Falls back to the live cursor (Tk's winfo_pointerxy) when no fresh
    latch exists (e.g., for pills triggered by mouse-click UI rather
    than a hotkey)."""
    try:
        import logging as _logging, time as _time
        # 8 seconds is roughly the worst-case round-trip for a
        # cloud-LLM Refine on a slow connection. Beyond that, the
        # latched position is likely stale (user has truly moved on),
        # so revert to the live cursor.
        if (_hotkey_cursor_xy is not None
                and _time.time() - _hotkey_cursor_ts < 8.0):
            mx, my = _hotkey_cursor_xy[0] + 20, _hotkey_cursor_xy[1] + 20
            _logging.getLogger('overlay').info(
                f'[PILL] hotkey-latched cursor ({mx},{my})')
            return (mx, my)
        mx = root.winfo_pointerx() + 20
        my = root.winfo_pointery() + 20
        _logging.getLogger('overlay').info(
            f'[PILL] live cursor ({mx},{my})')
        return (mx, my)
    except Exception:
        return (200, 200)


def _draw_pill(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
               r: int, fill: str, outline: str) -> None:
    pts = [
        x1 + r, y1,   x2 - r, y1,
        x2,     y1,   x2,     y1 + r,
        x2,     y2 - r, x2,   y2,
        x2 - r, y2,   x1 + r, y2,
        x1,     y2,   x1,     y2 - r,
        x1,     y1 + r, x1,   y1,
    ]
    canvas.create_polygon(pts, smooth=True, fill=fill, outline=outline, width=1)


class OverlayWindow:
    def __init__(self, root: tk.Tk, slot: int = 0) -> None:
        self.root      = root
        self._slot     = slot      # 0 = refine, 1 = whisper
        self._win      = None
        self._canvas   = None
        self._main_id  = None
        self._bar_id   = None
        self._tick     = False
        self._t0       = 0.0
        self._fnt             = TkFont(family=FONT_FAMILY, size=11)
        self._fnt_mono        = TkFont(family=FONT_MONO, size=11)
        self._safety_timer_id = None   # after() ID for the 30 s transcribe safety close
        self._pulse_job       = None   # after() ID for chain step pulse animation
        # Optional click-to-cancel callback. Set by main.py so if the
        # global hotkey hook dies mid-recording, the user can still
        # dismiss the pill by clicking it — no keyboard interaction
        # required. Same escape hatch principle as the tray menu.
        self._click_cancel = None

    # ── Refine pill states ────────────────────────────────────────────────────

    def show(self) -> None:
        """Refining, animated elapsed-time pill."""
        self._close()
        self._t0   = time.time()
        self._tick = True
        self._build('⚡  Refining...  0.0s', _TEXT_CLR, ACCENT)
        self._update()

    def show_loading_model(self) -> None:
        self._close()
        self._build('⏳  Local model loading, try again in ~10s', _TEXT_CLR, WARN)
        if self._win:
            self._win.after(4000, self._close)

    def show_cloud_fallback_notice(self, msg: str) -> None:
        """Non-alarming info pill: cloud path failed, but the local
        fallback is now working. Yellow accent bar (WARN) rather than
        red ✗ (ERR) so users don't think the whole operation failed.
        Stays visible for 6s — short enough not to clutter, long enough
        to actually read. The follow-up transcribing pill paints over
        this one when local Whisper kicks in."""
        self._tick = False
        # Trim to fit nicely in the single-line pill.
        short = (msg[:80] + '…') if len(msg) > 80 else msg
        body = f'🌐  {short}'
        if self._win:
            self._set_text(body)
            self._set_bar(WARN)
        else:
            self._build(body, _TEXT_CLR, WARN)
        if self._win:
            self._win.after(6000, self._close)

    def show_screenshot_working(self, kind: str = 'translate') -> None:
        """Brief 'in progress' pill while the screenshot OCR + translate
        round-trip is in flight. Kept up indefinitely until the caller
        closes it (via close() or by showing a different pill)."""
        self._close()
        label = 'Translating…' if kind == 'translate' else 'Reading text…'
        self._build(f'⏳  {label}', _TEXT_CLR, INFO)

    def close(self) -> None:
        """Public close so worker threads can dismiss a long-running pill
        once their work completes (e.g. before showing a follow-up window)."""
        try: self._close()
        except Exception: pass

    def show_no_selection(self) -> None:
        self._close()
        self._build('✦  Select text first', _TEXT_CLR, WARN)
        if self._win:
            self._win.after(1500, self._close)

    def show_done(self, elapsed: float) -> None:
        self._tick = False
        if self._win is None:
            return
        self._set_text(f'✓  Done  {elapsed:.1f}s')
        self._set_bar(OK)
        if self._win:
            self._win.after(750, self._close)

    # ── URL downloader pills (Ctrl+Alt+D) ────────────────────────────────────

    def show_download_capturing(self) -> None:
        """Immediate pill shown the instant Ctrl+Alt+D fires, before the
        clipboard capture completes. Bridges the ~500ms silence between
        the hotkey press and the download-starting pill."""
        self._close()
        self._build('🔍  Reading URL…', _TEXT_CLR, ACCENT)

    def show_download_starting(self) -> None:
        """Initial pill — shown the moment the URL is captured, before
        yt-dlp's first progress hook fires (which can take a few seconds
        while it resolves the format manifest)."""
        self._close()
        self._build('📥  Downloading…  0%', _TEXT_CLR, ACCENT)

    def show_download_asking(self) -> None:
        """Pill shown while the playlist-confirmation dialog is on screen,
        so the user knows the download is waiting on their input rather
        than stalled."""
        self._close()
        self._build('❓  Playlist detected — choose in dialog', _TEXT_CLR, ACCENT)

    def show_download_progress(self, frac: float) -> None:
        """Update the percentage in the in-flight download pill. Safe to
        call from a worker thread via Tk's after()."""
        pct = max(0, min(100, int(frac * 100)))
        if self._win is None:
            self._build(f'📥  Downloading…  {pct}%', _TEXT_CLR, ACCENT)
        else:
            self._set_text(f'📥  Downloading…  {pct}%')
            self._set_bar(ACCENT)

    def show_download_merging(self) -> None:
        """Streams arrived, ffmpeg is now muxing video + audio. Without
        a distinct pill, users think the app hung at 100% and kill it
        (leaving the .fNNN fragments behind)."""
        if self._win is None:
            self._build('🔀  Merging video + audio…', _TEXT_CLR, ACCENT)
        else:
            self._set_text('🔀  Merging video + audio…')
            self._set_bar(ACCENT)

    def show_download_done(self, filename: str) -> None:
        """Replace the progress pill with a 'Saved <name>' confirmation,
        auto-dismiss after a few seconds."""
        short = filename if len(filename) <= 40 else filename[:37] + '…'
        if self._win is None:
            self._build(f'✓  Saved  {short}', _TEXT_CLR, OK)
        else:
            self._set_text(f'✓  Saved  {short}')
            self._set_bar(OK)
        if self._win:
            self._win.after(3500, self._close)

    def show_translation_pill(self, text: str, kind: str = 'translate') -> None:
        """Short-form pill for screenshot Extract-text / Translate results.
        Displays up to ~100 chars near the cursor; auto-dismisses after a
        readable interval. The full text is already on the clipboard so
        truncation doesn't lose information.

        For longer results, the caller should fall back to the popup window
        instead — a pill that wraps to multiple lines stops looking like
        a pill.
        """
        self._tick = False
        icon = '🌐' if kind == 'translate' else '🔤'
        short = (text[:280] + '…') if len(text) > 280 else text
        # Replace internal newlines with " · " so the single-line pill stays
        # readable even if the source had line breaks.
        short = short.replace('\r', '').replace('\n', '  ·  ')
        body = f'{icon}  {short}'
        if self._win:
            self._set_text(body)
            self._set_bar(OK)
        else:
            self._build(body, _TEXT_CLR, OK)
        # Longer dwell than other pills — the user is reading text, not
        # just glancing at a status. ~280 chars at ~25 chars/sec ≈ 11 s
        # read time, with a small buffer.
        if self._win:
            self._win.after(13000, self._close)

    # ── Whiteboard launch pill (Shift+F8) ────────────────────────────────────

    def show_whiteboard_launching(self, elapsed: float,
                                  est_max: float = 45.0) -> None:
        """In-progress pill while the whiteboard subprocess + WebView2
        cold-init is running. Percentage is an estimate based on the
        typical 45s startup window; capped at 95% so it doesn't claim
        complete before the window actually shows."""
        self._tick = False
        pct = max(0, min(95, int(elapsed / est_max * 100)))
        body = f'🖌  Launching whiteboard…  {pct}%  {elapsed:.1f}s'
        if self._win:
            self._set_text(body)
            self._set_bar(ACCENT)
        else:
            self._build(body, _TEXT_CLR, ACCENT)

    def show_whiteboard_ready(self, elapsed: float) -> None:
        """Final pill once the whiteboard window is up. Auto-dismisses."""
        self._tick = False
        body = f'✓  Whiteboard ready  {elapsed:.1f}s'
        if self._win:
            self._set_text(body)
            self._set_bar(OK)
        else:
            self._build(body, _TEXT_CLR, OK)
        if self._win:
            self._win.after(1800, self._close)

    def show_error(self, msg: str) -> None:
        self._tick = False
        short = (msg[:48] + '…') if len(msg) > 48 else msg
        if self._win:
            self._set_text(f'✗  {short}')
            self._set_bar(ERR)
            self._win.after(2400, self._close)
        else:
            self._build(f'✗  {short}', _TEXT_CLR, ERR)
            if self._win:
                self._win.after(2400, self._close)

    # ── Whisper pill states ───────────────────────────────────────────────────

    def show_recording(self) -> None:
        """Recording in progress, animated elapsed-time pill.

        Two escape hatches from the pill in case the global hotkey hook
        dies mid-recording (which would leave Ctrl+Enter / Esc unable
        to stop the recording — user would otherwise have to force-kill
        the app from taskbar):
          1. Click on the pill itself → cancels the recording.
          2. Safety timer: pill force-closes after 6 minutes regardless
             of any hotkey activity, so it can never stick around forever.
        """
        self._close()
        self._t0   = time.time()
        self._tick = True
        self._build('🎙  Recording... (click to cancel)  0.0s',
                    _TEXT_CLR, INFO, clickable=True)
        self._update_recording()
        # Safety close after 10 minutes. audio.py already caps active
        # recording at 5 min via _MAX_RECORD_S, and the normal flow is
        # Recording (up to 5m) → auto-stop → Transcribing (up to 30s) →
        # Done → closes. show_transcribing() cancels this timer when it
        # takes over. 10-min buffer covers: max-recording + slow-network
        # transcription + any misc lag, without cutting off a legitimate
        # long dictation. Only fires if the pill is STUCK in "Recording"
        # state (hotkey hook dead + audio-auto-stop notification lost).
        self._cancel_safety_timer()
        if self._win:
            self._safety_timer_id = self.root.after(
                10 * 60 * 1000, self._safety_force_close)

    def _safety_force_close(self):
        """Fires 6 minutes after show_recording. Triggers cancel so
        state resets cleanly, then closes the pill."""
        try:
            import logging
            logging.getLogger('overlay').warning(
                'Whisper pill stuck for 6 minutes — force-closing via '
                'safety timer. Hotkey hook may have died.')
        except Exception:
            pass
        if self._click_cancel:
            try: self._click_cancel()
            except Exception: pass
        self._close()

    def show_transcribing(self) -> None:
        """Audio captured, running Whisper."""
        self._tick = False
        if self._win:
            self._set_text('⏳  Transcribing...')
            self._set_bar(ACCENT)
        else:
            self._build('⏳  Transcribing...', _TEXT_CLR, ACCENT)
        # Safety: auto-dismiss after 30 s in case transcriber never reports back.
        # Cancel any previous timer first, if show_transcribing is called again
        # before the old 30 s fires, we must not let the stale callback destroy
        # the new window.
        self._cancel_safety_timer()
        if self._win:
            self._safety_timer_id = self.root.after(30_000, self._close)

    def show_whisper_done(self, elapsed: float) -> None:
        self._tick = False
        if self._win is None:
            return
        self._set_text(f'✓  Typed  {elapsed:.1f}s')
        self._set_bar(OK)
        if self._win:
            self._win.after(900, self._close)

    def show_whisper_command_fired(self, label: str) -> None:
        """Shown when a dictation matched a recognized voice command (like
        "library") and was dispatched as an action instead of being typed.
        Label is the user-facing action name, e.g. "Library opened"."""
        self._tick = False
        if self._win is None:
            self._build(f'⚡  {label}', _TEXT_CLR, OK)
        else:
            self._set_text(f'⚡  {label}')
            self._set_bar(OK)
        if self._win:
            self._win.after(1200, self._close)

    def show_whisper_saved_to_notes(self) -> None:
        """Shown instead of show_whisper_done when the user's dictation
        triggered the "save to notes" voice command. Distinct icon + text
        so the user immediately knows the dictation went to Quick Notes
        instead of pasting normally."""
        self._tick = False
        if self._win is None:
            self._build('📝  Saved to Notes', _TEXT_CLR, OK)
        else:
            self._set_text('📝  Saved to Notes')
            self._set_bar(OK)
        if self._win:
            self._win.after(1500, self._close)

    def show_whisper_error(self, msg: str) -> None:
        self._tick = False
        short = (msg[:48] + '…') if len(msg) > 48 else msg
        if self._win:
            self._set_text(f'✗  {short}')
            self._set_bar(ERR)
            self._win.after(2400, self._close)
        else:
            self._build(f'✗  {short}', _TEXT_CLR, ERR)
            if self._win:
                self._win.after(2400, self._close)

    def show_whisper_loading(self) -> None:
        self._close()
        self._build('⏳  Whisper loading, try again shortly', _TEXT_CLR, WARN)
        if self._win:
            self._win.after(4000, self._close)

    def show_whisper_cancelled(self) -> None:
        self._tick = False
        text = '🔇  No speech detected'
        if self._win:
            self._set_text(text)
            self._set_bar(WARN)
            self._win.after(1800, self._close)
        else:
            self._build(text, _TEXT_CLR, WARN)
            if self._win:
                self._win.after(1800, self._close)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build(self, text: str, fg: str, bar_color: str,
               clickable: bool = False) -> None:
        tw = self._fnt.measure(text)
        th = self._fnt.metrics('linespace')
        bar_w = 4
        w = tw + _PAD_X * 2 + bar_w + 8
        h = th + _PAD_Y * 2

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.attributes('-alpha', 0.96)
        win.configure(bg=_TRANSP)

        try:
            win.attributes('-transparentcolor', _TRANSP)
            use_pill = True
        except Exception:
            use_pill = False
            win.configure(bg=_PILL_BG)

        canvas = tk.Canvas(win, width=w, height=h,
                           bg=_TRANSP if use_pill else _PILL_BG,
                           highlightthickness=0)
        canvas.pack()

        if use_pill:
            _draw_pill(canvas, 1, 1, w - 1, h - 1, _RADIUS,
                       fill=_PILL_BG, outline=_BORDER_CLR)

        bar_id = canvas.create_rectangle(
            _RADIUS // 2, h // 4, _RADIUS // 2 + bar_w, h * 3 // 4,
            fill=bar_color, outline='',
        )

        text_id = canvas.create_text(
            _RADIUS // 2 + bar_w + 10 + tw // 2, h // 2,
            text=text, fill=fg,
            font=_FONT, anchor='center',
        )

        # Anchor to the foreground app's window rect, not the mouse —
        # so a pill triggered by a hotkey-in-Notepad appears over
        # Notepad even when the mouse happens to be over our Library
        # window. Falls back to mouse-relative when our own UI is in
        # the foreground (see pill_anchor_xy() for the full rule).
        cx, cy = pill_anchor_xy(self.root)
        cy += self._slot * _SLOT_OFFSET
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f'{w}x{h}+{min(cx, sw - w - 12)}+{min(cy, sh - h - 12)}')
        # Prime + lift in one shot. Same fix as AskPill: when Hotkeys
        # boots with root.withdraw() and the user fires a hotkey
        # before opening Library / Notes / any other window, Tk's
        # window manager on Windows isn't primed and the Toplevel
        # never appears even though geometry is correct. Explicit
        # deiconify + update_idletasks + lift forces it visible in
        # the same paint frame.
        try:
            win.deiconify()
            win.update_idletasks()
            win.lift()
        except Exception:
            pass

        # Hide this window from screen-capture APIs (BitBlt, DWM, OBS, etc.)
        # so it never appears in recordings, while remaining visible to the user.
        # WDA_EXCLUDEFROMCAPTURE = 0x11, available on Windows 10 2004+.
        # winfo_id() returns the inner Tk child-frame HWND; Windows ignores the
        # affinity flag on child windows.  GetAncestor(GA_ROOT=2) walks up to
        # the real Win32 top-level so the flag is applied to the correct window.
        try:
            import ctypes
            win.update_idletasks()   # ensure the HWND exists before querying
            _u32 = ctypes.windll.user32
            # Set argtypes/restype so ctypes uses the correct 64-bit HWND type on
            # 64-bit Windows (HANDLE is a pointer-sized integer, without this,
            # ctypes defaults to c_int which silently truncates the upper 32 bits).
            _u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            _u32.GetAncestor.restype  = ctypes.c_void_p
            _u32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            _u32.SetWindowDisplayAffinity.restype  = ctypes.c_bool
            GA_ROOT = 2
            hwnd = _u32.GetAncestor(win.winfo_id(), GA_ROOT) or win.winfo_id()
            _u32.SetWindowDisplayAffinity(hwnd, 0x00000011)
        except Exception:
            pass

        # Wire click-to-cancel if the caller asked for it. Both left-
        # and right-click trigger cancel — user reflex on a stuck
        # popup is often right-click, don't want to make them figure
        # out which button.
        if clickable and self._click_cancel:
            def _on_click(_evt, cb=self._click_cancel):
                try: cb()
                except Exception: pass
                try: win.destroy()
                except Exception: pass
            canvas.bind('<Button-1>', _on_click)
            canvas.bind('<Button-3>', _on_click)
            canvas.configure(cursor='hand2')

        self._win     = win
        self._canvas  = canvas
        self._main_id = text_id
        self._bar_id  = bar_id
        self._pill_w  = w
        self._pill_h  = h

    def _set_text(self, text: str) -> None:
        if not (self._canvas and self._main_id and self._win):
            return
        self._canvas.itemconfig(self._main_id, text=text)
        # Resize pill if new text is wider than the original
        bar_w  = 4
        tw     = self._fnt.measure(text)
        new_w  = max(self._pill_w, tw + _PAD_X * 2 + bar_w + 8)
        if new_w != self._pill_w:
            self._pill_w = new_w
            self._win.geometry(f'{new_w}x{self._pill_h}')
            self._canvas.config(width=new_w)
            # Redraw pill background at new width
            self._canvas.delete('bg')
            self._canvas.create_rectangle(
                0, 0, new_w, self._pill_h, fill=_PILL_BG, outline='', tags='bg')
            self._canvas.tag_lower('bg')
        new_x = _RADIUS // 2 + bar_w + 10 + tw // 2
        self._canvas.coords(self._main_id, new_x, self._pill_h // 2)

    def _set_bar(self, color: str) -> None:
        if self._canvas and self._bar_id:
            self._canvas.itemconfig(self._bar_id, fill=color)

    def _update(self) -> None:
        """Animate refine elapsed timer."""
        if not self._tick or self._win is None:
            return
        elapsed = time.time() - self._t0
        self._set_text(f'⚡  Refining...  {elapsed:.1f}s')
        self._win.after(80, self._update)

    def _update_recording(self) -> None:
        """Animate recording elapsed timer."""
        if not self._tick or self._win is None:
            return
        elapsed = time.time() - self._t0
        self._set_text(f'🎙  Recording...  {elapsed:.1f}s')
        self._win.after(80, self._update_recording)

    # ── Macro pill states ─────────────────────────────────────────────────────

    def show_macro_recording(self) -> None:
        """Macro recording, animated elapsed-time pill."""
        self._close()
        self._t0   = time.time()
        self._tick = True
        self._build('⏺  Recording macro...  0.0s', _TEXT_CLR, ERR)
        self._update_macro_recording()

    def show_macro_playing(self) -> None:
        """Macro playback, animated elapsed-time pill."""
        self._close()
        self._t0   = time.time()
        self._tick = True
        self._build('▶  Playing macro...  0.0s', _TEXT_CLR, OK)
        self._update_macro_playing()

    def show_macro_ready(self, n_events: int) -> None:
        """Recording stopped, show count, then auto-close after 3 s."""
        self._tick = False
        self._close()
        self._build(f'⏹  {n_events} events, Shift+F1 to play', _TEXT_CLR, ACCENT)
        if self._win:
            self._win.after(3000, self._close)

    def show_macro_done(self) -> None:
        self._tick = False
        if self._win:
            self._set_text('✓  Macro done, Shift+F1 to replay')
            self._set_bar(OK)
            self._win.after(2000, self._close)
        else:
            self._build('✓  Macro done, Shift+F1 to replay', _TEXT_CLR, OK)
            if self._win:
                self._win.after(2000, self._close)

    def show_macro_stopped(self) -> None:
        self._tick = False
        if self._win:
            self._set_text('⬜  Stopped, Shift+F1 to replay')
            self._set_bar(WARN)
            self._win.after(1500, self._close)
        else:
            self._build('⬜  Stopped, Shift+F1 to replay', _TEXT_CLR, WARN)
            if self._win:
                self._win.after(1500, self._close)

    def show_macro_saved(self, name: str, hotkey: str) -> None:
        """Brief confirmation pill after a macro is saved."""
        self._tick = False
        self._close()
        hk_part = f'  ·  {hotkey.upper()} to play' if hotkey else '  ·  assign a hotkey in Library'
        text = f'✓  "{name}" saved{hk_part}'
        self._build(text, _TEXT_CLR, OK)
        if self._win:
            self._win.after(3500, self._close)

    def _update_macro_recording(self) -> None:
        if not self._tick or self._win is None:
            return
        elapsed = time.time() - self._t0
        self._set_text(f'⏺  Recording macro...  {elapsed:.1f}s')
        self._win.after(80, self._update_macro_recording)

    def _update_macro_playing(self) -> None:
        if not self._tick or self._win is None:
            return
        elapsed = time.time() - self._t0
        self._set_text(f'▶  Playing macro...  {elapsed:.1f}s')
        self._win.after(80, self._update_macro_playing)

    # ── Recorder pill states ──────────────────────────────────────────────────

    def show_recorder_recording(self) -> None:
        """Screen recording active, animated elapsed-time pill (red)."""
        self._close()
        self._t0   = time.time()
        self._tick = True
        self._build('⏺  Recording…  0s', _TEXT_CLR, ERR)
        self._update_recorder_recording()

    def show_recorder_stopping(self) -> None:
        """Encoding / finalizing, brief amber pill."""
        self._tick = False
        self._close()
        self._build('⏳  Saving recording…', _TEXT_CLR, WARN)

    def _update_recorder_recording(self) -> None:
        if not self._tick or self._win is None:
            return
        elapsed = time.time() - self._t0
        m, s = divmod(int(elapsed), 60)
        self._set_text(f'⏺  Recording…  {m:02d}:{s:02d}  ·  Shift+F2 to stop')
        self._win.after(1000, self._update_recorder_recording)

    # ── GIF pill states ───────────────────────────────────────────────────────

    def show_gif_recording(self) -> None:
        """GIF capture active, animated frame-count pill (purple/accent)."""
        self._close()
        self._t0   = time.time()
        self._tick = True
        self._build('🎞  GIF  0s  ·  Shift+F3 to stop', _TEXT_CLR, ACCENT)
        self._update_gif_recording()

    def _update_gif_recording(self) -> None:
        if not self._tick or self._win is None:
            return
        elapsed = time.time() - self._t0
        self._set_text(f'🎞  GIF  {int(elapsed)}s  ·  Shift+F3 to stop')
        self._win.after(1000, self._update_gif_recording)

    def show_gif_encoding(self) -> None:
        """GIF encoding in progress, brief amber pill."""
        self._tick = False
        self._close()
        self._build('⏳  Saving GIF…', _TEXT_CLR, WARN)

    def show_gif_capped(self, dur_s: int) -> None:
        """Duration cap hit, show notification, then auto-close."""
        self._tick = False
        self._close()
        self._build(f'⏹  Max duration reached ({dur_s}s)', _TEXT_CLR, WARN)
        if self._win:
            self._win.after(2500, self._close)

    # ── Chain pill states ─────────────────────────────────────────────────────

    def show_chain_step(self, step_num: int, total: int, label: str) -> None:
        """Animated pulsing pill while a chain step is running."""
        # Cancel any outstanding pulse timer before starting a new one (UI-1)
        if self._pulse_job is not None:
            try:
                if self._win:
                    self._win.after_cancel(self._pulse_job)
            except Exception:
                pass
            self._pulse_job = None
        self._tick = False
        text = f'⛓  Step {step_num}/{total}, {label}…'
        if self._win:
            self._set_text(text)
            self._set_bar(ACCENT)
        else:
            self._close()
            self._build(text, _TEXT_CLR, ACCENT)
        self._tick = True
        self._pulse_phase = 0
        self._update_chain_pulse()

    def show_chain_done(self, name: str) -> None:
        """Brief success pill after all chain steps finish."""
        self._tick = False   # UI-2: stop pulse before transitioning to done state
        if self._pulse_job is not None:
            try:
                if self._win:
                    self._win.after_cancel(self._pulse_job)
            except Exception:
                pass
            self._pulse_job = None
        text = f'✓  Chain done, {name}'
        if self._win:
            self._set_text(text)
            self._set_bar(OK)
            self._win.after(3000, self._close)
        else:
            self._build(text, _TEXT_CLR, OK)
            if self._win:
                self._win.after(3000, self._close)

    def hide(self) -> None:
        """Public alias for _close, lets external callers hide the overlay."""
        self._close()

    def _update_chain_pulse(self) -> None:
        """Alternate bar colour between ACCENT and ACCENTL to indicate activity."""
        if not self._tick or self._win is None:
            self._pulse_job = None
            return
        self._pulse_phase = getattr(self, '_pulse_phase', 0) + 1
        clr = ACCENT if self._pulse_phase % 2 == 0 else '#9f67fa'
        self._set_bar(clr)
        self._pulse_job = self._win.after(500, self._update_chain_pulse)

    def _cancel_safety_timer(self) -> None:
        if self._safety_timer_id is not None:
            try:
                self.root.after_cancel(self._safety_timer_id)
            except Exception:
                pass
            self._safety_timer_id = None

    def _close(self) -> None:
        self._tick = False
        self._cancel_safety_timer()
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
        self._win     = None
        self._canvas  = None
        self._main_id = None
        self._bar_id  = None
