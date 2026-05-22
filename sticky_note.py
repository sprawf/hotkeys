"""Floating sticky-note window for a single prompt — editable, auto-saves on close."""
import tkinter as tk
from typing import Callable

import spellcheck
from theme import (
    FONT_FAMILY, CARD_TEXT, CARD_TEXT_S,
    ACCENT, _darken,
)


class PromptStickyNote:
    """Small floating window showing one prompt — title and text both editable.

    When closed (✕ button or Escape), saves any edits back via on_save().
    Draggable via the header bar.

    Parameters
    ----------
    on_save  : called with the updated prompt dict if title or text changed.
    on_close : called (no args) when the window is destroyed, so the caller
               can clear its reference.  Safe to omit.
    """

    def __init__(self, root, prompt: dict,
                 on_save:  Callable[[dict], None],
                 on_close: Callable[[], None] | None = None) -> None:
        self._prompt   = dict(prompt)
        self._on_save  = on_save
        self._on_close = on_close
        self._color    = prompt.get('color', '#FFF9C4')
        # Resize state — initialised here so _resize_move is safe even if a
        # spurious B1-Motion arrives before the first ButtonPress-1 event.
        self._rsz_x = self._rsz_y = self._rsz_w = self._rsz_h = 0
        self._dark     = _darken(self._color, 0.82)
        self._darkest  = _darken(self._color, 0.68)

        self.win = tk.Toplevel(root)
        self.win.title('')
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        # Thin accent-colored outer frame: set win bg to ACCENT and inset all
        # content by 2 px so a purple border peeks around the edge.
        self.win.configure(bg=ACCENT)

        # Inner container inset 2 px — the 2 px gap shows as an ACCENT border
        self._inner = tk.Frame(self.win, bg=self._color)
        self._inner.pack(fill='both', expand=True, padx=2, pady=2)

        self._build()
        self._place()
        self.win.bind('<Escape>', lambda e: self.close())

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self._inner, bg=self._dark, height=36)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)

        # Drag grip — explicit handle so Entry/Button don't block dragging
        grip = tk.Label(hdr, text='⠿', bg=self._dark,
                        fg=_darken(self._color, 0.55),
                        font=(FONT_FAMILY, 13), cursor='fleur', padx=4)
        grip.pack(side='left', padx=(4, 0))
        grip.bind('<ButtonPress-1>', self._drag_start)
        grip.bind('<B1-Motion>',     self._drag_move)

        # Hotkey badge — also draggable
        hk = self._prompt.get('hotkey', '')
        if hk:
            badge = tk.Label(hdr, text=f'  ⌨ {hk.upper()}  ', bg=self._darkest,
                             fg=CARD_TEXT, font=(FONT_FAMILY, 10, 'bold'),
                             relief='flat', cursor='fleur')
            badge.pack(side='left', pady=7)
            badge.bind('<ButtonPress-1>', self._drag_start)
            badge.bind('<B1-Motion>',     self._drag_move)

        # Close button — pack right before title so it stays pinned right
        tk.Button(hdr, text='✕', bg=self._dark, fg=CARD_TEXT,
                  activebackground=self._darkest, activeforeground=CARD_TEXT,
                  relief='flat', font=(FONT_FAMILY, 11), width=2,
                  bd=0, cursor='arrow',
                  command=self.close).pack(side='right', padx=4)

        # Title entry (editable)
        self._title_var = tk.StringVar(value=self._prompt.get('title', ''))
        tk.Entry(hdr, textvariable=self._title_var, bg=self._dark, fg=CARD_TEXT,
                 insertbackground=CARD_TEXT,
                 relief='flat', font=(FONT_FAMILY, 12, 'bold'),
                 bd=0, highlightthickness=0).pack(
            side='left', fill='x', expand=True, padx=(4, 0), pady=4)

        # ── Separator ──────────────────────────────────────────────────────────
        tk.Frame(self._inner, bg=self._darkest, height=1).pack(fill='x')

        # ── Prompt text area — fills all remaining space ──────────────────────
        self._text = tk.Text(
            self._inner, wrap='word',
            bg=self._color, fg=CARD_TEXT,
            insertbackground=CARD_TEXT,
            relief='flat', font=(FONT_FAMILY, 12),
            bd=0, highlightthickness=0,
            padx=10, pady=8,
            undo=True,
        )
        self._text.insert('1.0', self._prompt.get('prompt', ''))
        self._text.pack(fill='both', expand=True)
        spellcheck.attach(self._text)

        # ── Resize grip — floated over bottom-right corner, no strip needed ───
        grip_rsz = tk.Label(self._inner, text='◢',
                            bg=self._color, fg=_darken(self._color, 0.45),
                            font=(FONT_FAMILY, 11), cursor='size_nw_se')
        grip_rsz.place(relx=1.0, rely=1.0, anchor='se')
        grip_rsz.bind('<ButtonPress-1>', self._resize_start)
        grip_rsz.bind('<B1-Motion>',     self._resize_move)

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, event) -> None:
        self._drag_x = event.x_root - self.win.winfo_x()
        self._drag_y = event.y_root - self.win.winfo_y()

    def _drag_move(self, event) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.win.geometry(f'+{x}+{y}')

    # ── Resize ────────────────────────────────────────────────────────────────

    def _resize_start(self, event) -> None:
        self._rsz_x = event.x_root
        self._rsz_y = event.y_root
        self._rsz_w = self.win.winfo_width()
        self._rsz_h = self.win.winfo_height()

    def _resize_move(self, event) -> None:
        new_w = max(280, self._rsz_w + (event.x_root - self._rsz_x))
        new_h = max(160, self._rsz_h + (event.y_root - self._rsz_y))
        self.win.geometry(f'{new_w}x{new_h}+{self.win.winfo_x()}+{self.win.winfo_y()}')

    # ── Placement ─────────────────────────────────────────────────────────────

    def _place(self) -> None:
        """Bottom-right corner, above the taskbar."""
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w, h = 360, 280
        self.win.minsize(280, 160)
        self.win.geometry(f'{w}x{h}+{sw - w - 20}+{sh - h - 60}')
        self.win.update_idletasks()  # apply geometry without re-entering the event loop

    # ── Save & close ──────────────────────────────────────────────────────────

    def close(self) -> None:
        title  = self._title_var.get().strip()
        # Fall back to the original title rather than silently discarding edits
        # when the user clears the title field.
        if not title:
            title = self._prompt.get('title', '')
        prompt = self._text.get('1.0', 'end-1c').strip()
        if title and prompt:
            updated = dict(self._prompt)
            updated['title']  = title
            updated['prompt'] = prompt
            if updated['title'] != self._prompt.get('title') or \
               updated['prompt'] != self._prompt.get('prompt'):
                self._on_save(updated)
        # Flash 'Applied ✓' before closing — gives the user clear confirmation
        # that this prompt is now the active one.
        self._flash_applied()

    def _flash_applied(self) -> None:
        """Show a brief green 'Applied ✓' badge for 550 ms, then destroy."""
        _OK = '#22c55e'
        try:
            self.win.configure(bg=_OK)          # swap border from purple → green
            overlay = tk.Frame(self._inner, bg=_OK)
            overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            tk.Label(
                overlay, text='✓   Applied',
                bg=_OK, fg='#ffffff',
                font=(FONT_FAMILY, 18, 'bold'),
            ).place(relx=0.5, rely=0.5, anchor='center')
        except Exception:
            pass
        self.win.after(550, self.destroy)

    def destroy(self) -> None:
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass
        try:
            self.win.destroy()
        except Exception:
            pass
