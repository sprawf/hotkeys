"""
Floating status pill — appears near the cursor during refinement or recording.
True pill shape via Canvas + Windows transparentcolor trick.
Slot 0 = text-refine pill  (Y offset 0)
Slot 1 = whisper pill      (Y offset +50 px — stacks below refine pill)
"""
import time
import tkinter as tk
from tkinter.font import Font as TkFont

from theme import ACCENT, OK, WARN, ERR, INFO, FONT_FAMILY, FONT_MONO

# Pill styling
_PILL_BG    = '#141414'
_BORDER_CLR = '#2a2a2a'
_TEXT_CLR   = '#f0f0f0'
_TRANSP     = '#010101'    # Windows transparentcolor (near-black = transparent)
_RADIUS     = 20
_PAD_X      = 22
_PAD_Y      = 12
_FONT       = (FONT_FAMILY, 11)
_FONT_TIMER = (FONT_MONO, 11)

_SLOT_OFFSET = 50   # px between stacked pills


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
        self._fnt      = TkFont(family=FONT_FAMILY, size=11)
        self._fnt_mono = TkFont(family='Consolas', size=11)

    # ── Refine pill states ────────────────────────────────────────────────────

    def show(self) -> None:
        """Refining — animated elapsed-time pill."""
        self._close()
        self._t0   = time.time()
        self._tick = True
        self._build('⚡  Refining...  0.0s', _TEXT_CLR, ACCENT)
        self._update()

    def show_loading_model(self) -> None:
        self._close()
        self._build('⏳  Local model loading — try again in ~10s', _TEXT_CLR, WARN)
        if self._win:
            self._win.after(4000, self._close)

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
        """Recording in progress — animated elapsed-time pill."""
        self._close()
        self._t0   = time.time()
        self._tick = True
        self._build('🎙  Recording...  0.0s', _TEXT_CLR, INFO)
        self._update_recording()

    def show_transcribing(self) -> None:
        """Audio captured, running Whisper."""
        self._tick = False
        if self._win:
            self._set_text('⏳  Transcribing...')
            self._set_bar(ACCENT)
        else:
            self._build('⏳  Transcribing...', _TEXT_CLR, ACCENT)
        # Safety: auto-dismiss after 30s in case transcriber never reports back
        if self._win:
            self._win.after(30_000, self._close)

    def show_whisper_done(self, elapsed: float) -> None:
        self._tick = False
        if self._win is None:
            return
        self._set_text(f'✓  Typed  {elapsed:.1f}s')
        self._set_bar(OK)
        if self._win:
            self._win.after(900, self._close)

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
        self._build('⏳  Whisper loading — try again shortly', _TEXT_CLR, WARN)
        if self._win:
            self._win.after(4000, self._close)

    def show_whisper_cancelled(self) -> None:
        self._tick = False
        if self._win:
            self._set_text('—  No speech detected')
            self._set_bar(WARN)
            self._win.after(1600, self._close)
        else:
            self._build('—  No speech detected', _TEXT_CLR, WARN)
            if self._win:
                self._win.after(1600, self._close)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build(self, text: str, fg: str, bar_color: str) -> None:
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

        cx = self.root.winfo_pointerx() + 24
        cy = self.root.winfo_pointery() + 24 + self._slot * _SLOT_OFFSET
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f'{w}x{h}+{min(cx, sw - w - 12)}+{min(cy, sh - h - 12)}')

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

    def _close(self) -> None:
        self._tick = False
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
        self._win     = None
        self._canvas  = None
        self._main_id = None
        self._bar_id  = None
