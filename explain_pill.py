"""Floating answer pill, Shift+F4 fires the selected text as a question.

Uses the same visual language as OverlayWindow: true pill shape via
Canvas + Windows transparentcolor, left accent bar, dark palette.

Loading state  → single-line pill  "⏳  Asking…"
Answer state   → expanded pill     question header + answer text + Copy/✕
Status state   → single-line pill  "ℹ  No text found in image" (auto-close)
Error state    → single-line pill  "✕  <message>"  (auto-close)
"""
import logging
import threading
import tkinter as tk
import tkinter.font as tkfont

from core.sounds import play_start
from overlay import _draw_pill
from theme import ACCENT, WARN, ERR, OK, FONT_FAMILY, SURFACE, BORDER, TEXT_P, TEXT_S

logger = logging.getLogger(__name__)

# ── Visual constants (match overlay.py) ───────────────────────────────────────
_PILL_BG  = SURFACE    # '#141414', dark surface for pill background
_BORDER   = BORDER     # '#2a2a2a', subtle separator border
_TEXT_CLR = TEXT_P     # '#f0f0f0', primary text
_MUTED    = TEXT_S     # '#909090', muted/secondary text
_GREEN    = OK         # '#22c55e', success green
_TRANSP   = '#010101'  # Windows transparentcolor (near-black = transparent)
_RADIUS   = 20
_PAD_X    = 22           # horizontal pad inside pill
_PAD_Y    = 12           # vertical pad
_BAR_W    = 4            # left accent bar width
_FONT     = (FONT_FAMILY, 11)
_FONT_S   = (FONT_FAMILY, 9)
_MARGIN   = 16           # keep this far from screen edges
_AUTO_DISMISS_MS = 30_000
_WIDTH    = 400          # answer pill width

_SYSTEM_PROMPT = (
    'Answer in two sentences: the first explains the core reason or fact, '
    'the second gives a brief real-world consequence or example. '
    'Plain text only, no markdown, no bullet points, no caveats, no filler. '
    'Never exceed two sentences.'
)


class AskPill:
    """Pill-shaped floating window that shows an AI answer near the cursor.

    Parameters
    ----------
    root     : app root Tk window (used for pointer coords)
    question : selected text sent as the question
    provider : object with .refine(text, system_prompt) → str
    static   : if given, show this as a status message without any API call
               (e.g. 'No text found in image')
    """

    def __init__(self, root: tk.Tk, question: str, provider,
                 static: str = None, on_close=None,
                 prepared_answer: str = None) -> None:
        """
        prepared_answer : if set, skip the LLM call and render this string as
                          the answer directly. Used by features like the
                          screenshot "Translate to English" flow where the
                          OCR + translate has already happened upstream.
        """
        self._root      = root
        self._question  = (question or '').strip()
        self._provider  = provider
        self._answer    = ''
        self._auto_id   = None
        self._canvas    = None
        self._copy_id   = None
        self._on_close  = on_close   # called once when the pill closes

        self._win = tk.Toplevel(root)
        self._win.overrideredirect(True)
        # The user explicitly asked for the pill to be on TOP, not
        # behind other windows. Earlier comment here argued against
        # -topmost (afraid it would float over YouTube / Chrome on
        # later app switches), but the alternative — relying on
        # one-shot SetWindowPos lift after creation — kept losing
        # the z-order race to whatever foreground app repainted just
        # after. Setting -topmost wins decisively, and the pill auto-
        # dismisses on Esc / close / 30s timeout, so the "float over
        # YouTube" concern is bounded.
        self._win.attributes('-topmost', True)

        # Windows transparentcolor trick, same as OverlayWindow
        try:
            self._win.configure(bg=_TRANSP)
            self._win.attributes('-transparentcolor', _TRANSP)
            self._use_pill = True
        except Exception:
            self._win.configure(bg=_PILL_BG)
            self._use_pill = False

        self._win.bind('<Escape>', lambda e: self._close())
        self._auto_id  = self._win.after(_AUTO_DISMISS_MS, self._close)
        self._grabbed  = False
        # NOTE: no global keyboard hook here, main.py's _hk_escape handles
        # closing pills so their escape doesn't get nuked by unhook_all().

        if static is not None:
            # Status-only pill, no API call, auto-closes after 5 s
            self._render_single(f'ℹ  {static}', WARN)
            self._win.after(5_000, self._close)
        elif prepared_answer is not None:
            # Pre-computed answer (e.g. screenshot translation result).
            # Render straight into the multi-line answer pill so the look
            # and behaviour match a regular Ask Claude reply: same fonts,
            # same auto-dismiss, click-to-copy, Escape to close.
            self._answer = (prepared_answer or '').strip()
            self._render_answer(self._answer)
        else:
            # Loading pill then fetch
            self._render_single('⏳  Asking…', ACCENT)
            threading.Thread(target=self._fetch, daemon=True).start()

    # ── Single-line pill (loading / status / error) ───────────────────────────

    def _render_single(self, text: str, bar_color: str) -> None:
        """Render (or re-render) as a compact single-line pill.

        Identical to OverlayWindow._build() so it matches all other pills.
        """
        fnt = tkfont.Font(family=FONT_FAMILY, size=11)
        tw  = fnt.measure(text)
        th  = fnt.metrics('linespace')
        w   = tw + _PAD_X * 2 + _BAR_W + 8
        h   = th + _PAD_Y * 2

        self._swap_canvas(w, h)
        c = self._canvas

        if self._use_pill:
            _draw_pill(c, 1, 1, w - 1, h - 1, _RADIUS,
                       fill=_PILL_BG, outline=_BORDER)

        # Left bar
        c.create_rectangle(
            _RADIUS // 2, h // 4,
            _RADIUS // 2 + _BAR_W, h * 3 // 4,
            fill=bar_color, outline='')

        # Text, centred on bar+padding the same way overlay.py does it
        c.create_text(
            _RADIUS // 2 + _BAR_W + 10 + tw // 2, h // 2,
            text=text, fill=_TEXT_CLR, font=_FONT, anchor='center')

        self._place(w, h)

    # ── Multi-line answer pill ────────────────────────────────────────────────

    def _render_answer(self, text: str) -> None:
        """Render the expanded answer pill."""
        pad_l  = _RADIUS // 2 + _BAR_W + 10   # text left edge
        pad_r  = _PAD_X
        w      = _WIDTH
        text_w = w - pad_l - pad_r

        q = self._question.replace('\n', ' ')
        if len(q) > 120:
            q = q[:117] + '…'

        # ── Pass 1: measure heights on a hidden canvas ────────────────────────
        m = tk.Canvas(self._win, width=w, height=4000,
                      bg='black', highlightthickness=0)
        # (not packed, we only use it for text measurement)

        y = _PAD_Y

        sep_y = None
        ans_y = y

        if q:
            q_id = m.create_text(pad_l, y, text=q, width=text_w,
                                 anchor='nw', font=_FONT_S)
            m.update_idletasks()
            bb = m.bbox(q_id)
            q_bottom = bb[3] if bb else y + 14
            sep_y = q_bottom + 5
            ans_y = sep_y + 7

        ans_id = m.create_text(pad_l, ans_y, text=text, width=text_w,
                               anchor='nw', font=_FONT)
        m.update_idletasks()
        bb = m.bbox(ans_id)
        ans_bottom = bb[3] if bb else ans_y + 20

        footer_y = ans_bottom + 10
        fnt_s    = tkfont.Font(family=FONT_FAMILY, size=9)
        h        = footer_y + fnt_s.metrics('linespace') + _PAD_Y
        m.destroy()

        # ── Pass 2: draw on the real canvas at the correct size ───────────────
        self._swap_canvas(w, h)
        c = self._canvas

        # Pill background (drawn first so all text is above it)
        if self._use_pill:
            _draw_pill(c, 1, 1, w - 1, h - 1, _RADIUS,
                       fill=_PILL_BG, outline=_BORDER)

        # Left accent bar (full-height, inset from rounded corners)
        c.create_rectangle(
            _RADIUS // 2, _RADIUS // 2,
            _RADIUS // 2 + _BAR_W, h - _RADIUS // 2,
            fill=ACCENT, outline='')

        # Question header
        if q:
            c.create_text(pad_l, _PAD_Y, text=q, width=text_w,
                          anchor='nw', font=_FONT_S, fill=_MUTED)
            c.create_line(pad_l, sep_y, w - pad_r, sep_y, fill=_BORDER)

        # Answer body
        c.create_text(pad_l, ans_y, text=text, width=text_w,
                      anchor='nw', font=_FONT, fill=_TEXT_CLR)

        # Footer, Copy (left) and ✕ (right)
        self._copy_id = c.create_text(
            pad_l, footer_y, text='⎘  Copy',
            anchor='nw', font=_FONT_S, fill=_MUTED)
        close_id = c.create_text(
            w - pad_r, footer_y, text='×',
            anchor='ne', font=_FONT_S, fill=_MUTED)

        # Hover highlights for Copy and ×
        for item in (self._copy_id, close_id):
            c.tag_bind(item, '<Enter>', lambda e, i=item: c.itemconfig(i, fill=_TEXT_CLR))
            c.tag_bind(item, '<Leave>', lambda e, i=item: c.itemconfig(i, fill=_MUTED))

        # Copy, copies text; return 'break' prevents propagation to window close
        def _on_copy(e):
            self._copy()
            return 'break'
        c.tag_bind(self._copy_id, '<ButtonPress-1>', _on_copy)

        # × and everything else on the canvas, close
        c.tag_bind(close_id, '<ButtonPress-1>', lambda e: self._close())
        c.tag_bind('all', '<ButtonPress-1>', lambda e: self._close())
        c.bind('<ButtonPress-1>', lambda e: self._close())

        # Window-level: catches clicks outside the canvas once grab_set() is active
        self._win.bind('<ButtonPress-1>', lambda e: self._close())

        self._place(w, h)

    # ── Canvas management ─────────────────────────────────────────────────────

    def _swap_canvas(self, w: int, h: int) -> None:
        """Destroy old canvas and create a fresh one at the given size."""
        if self._canvas:
            try:
                self._canvas.destroy()
            except Exception:
                pass
        bg = _TRANSP if self._use_pill else _PILL_BG
        c  = tk.Canvas(self._win, width=w, height=h,
                       bg=bg, highlightthickness=0)
        c.pack()
        self._canvas = c
        self._win.geometry(f'{w}x{h}')

    # ── Positioning ───────────────────────────────────────────────────────────

    def _place(self, w: int, h: int) -> None:
        """Position the pill anchored to the foreground app's window
        (falling back to the mouse if our own UI is in front). This
        keeps the pill over Notepad / browser / whatever app the user
        triggered the hotkey from, even if their mouse happened to be
        parked over the Library window. See overlay.pill_anchor_xy()
        for the full rule and rationale."""
        from overlay import pill_anchor_xy
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()
        mx, my = pill_anchor_xy(self._root)

        x = mx
        y = my
        if x + w + _MARGIN > sw:
            x = mx - w - 10
        if y + h + _MARGIN > sh:
            y = my - h - 10
        x = max(_MARGIN, min(x, sw - w - _MARGIN))
        y = max(_MARGIN, y)
        self._win.geometry(f'{w}x{h}+{x}+{y}')
        # Force the pill onto the screen and to the top of z-order.
        #
        # Why all three:
        #   • deiconify(): when the main root has never been mapped
        #     (Hotkeys boots with root.withdraw()), Tk's WM subsystem
        #     on Windows isn't primed until a Toplevel is explicitly
        #     deiconified. Without this, the FIRST pill after launch
        #     gets created in the WM but never actually shown — the
        #     user sees nothing despite the geometry being correct.
        #     Once Library opens once, the WM is primed and subsequent
        #     pills work. deiconify() forces priming on the pill itself
        #     so it doesn't need the Library to have been opened first.
        #   • update_idletasks(): flushes the deiconify + geometry to
        #     the OS in the same paint frame.
        #   • lift(): wins the z-order race against whatever app the
        #     user just asked from (e.g. Notepad may repaint on top
        #     of a freshly-created Toplevel without this).
        try:
            self._win.deiconify()
            self._win.update_idletasks()
            self._win.lift()
        except Exception:
            pass
        # Lift above whatever app the user just asked from, without
        # stealing focus. HWND_TOP=0, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE.
        # MUST target the OS top-level HWND — self._win is
        # overrideredirect, so winfo_id() returns the inner child HWND
        # which is the wrong window for SetWindowPos. See
        # win_helpers.top_level_hwnd().
        try:
            import sys as _sys
            if _sys.platform == 'win32':
                import ctypes as _ct
                from win_helpers import top_level_hwnd
                _ct.windll.user32.SetWindowPos(
                    top_level_hwnd(self._win), 0, 0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010)
        except Exception:
            pass

    # ── API fetch ─────────────────────────────────────────────────────────────

    def _fetch(self) -> None:
        try:
            answer = self._provider.refine(self._question, _SYSTEM_PROMPT)
            self._win.after(0, lambda: self._on_answer(answer))
        except Exception as exc:
            logger.warning('AskPill: %s', exc)
            from engine import friendly_error_message
            # active_provider isn't reachable from here without main.py
            # context, but Ask uses self._provider directly, if it's the
            # local Qwen we'd still see network errors only when the
            # provider INTERNALLY calls a remote, which Local never does.
            msg = friendly_error_message(exc, feature='Ask')
            self._win.after(0, lambda m=msg: self._on_error(m))

    def _on_answer(self, text: str) -> None:
        try:
            if not self._win.winfo_exists():
                return
        except Exception:
            return
        self._answer = text
        try:
            play_start()
        except Exception:
            pass
        self._render_answer(text)
        # grab_set routes all in-app mouse events here, any click dismisses
        try:
            self._win.grab_set()
            self._grabbed = True
        except Exception:
            pass

    def _on_error(self, msg: str) -> None:
        try:
            if not self._win.winfo_exists():
                return
        except Exception:
            return
        short = msg.split('\n')[0][:60]
        self._render_single(f'×  {short}', ERR)
        self._win.after(4_000, self._close)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _copy(self) -> None:
        if not self._answer:
            return
        try:
            import pyperclip
            pyperclip.copy(self._answer)
            if self._canvas and self._copy_id:
                self._canvas.itemconfig(
                    self._copy_id, text='✓  Copied', fill=_GREEN)
                cid = self._copy_id
                self._win.after(1_500, lambda: (
                    self._canvas.itemconfig(cid, text='⎘  Copy', fill=_MUTED)
                    if self._canvas else None
                ))
        except Exception:
            pass

    def _close(self) -> None:
        try:
            if self._auto_id:
                self._win.after_cancel(self._auto_id)
                self._auto_id = None
        except Exception:
            pass
        try:
            if self._grabbed:
                self._win.grab_release()
                self._grabbed = False
        except Exception:
            pass
        try:
            cb = self._on_close
            self._on_close = None
            if cb is not None:
                cb()
        except Exception:
            pass
        try:
            self._win.destroy()
        except Exception:
            pass
