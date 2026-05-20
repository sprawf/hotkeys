"""Transcription History viewer — shows past whisper results with copy & search."""
import datetime
import threading
from typing import Callable

import customtkinter as ctk
import pyperclip

from dialogs import confirm
from storage import save_history
from theme import (
    BG, SURFACE, SURF2, SURF3, BORDER2,
    ACCENT, ACCENTL,
    TEXT_P, TEXT_S,
    FONT_FAMILY, PAD, PAD_SM, RADIUS, RADIUS_SM,
    ERR,
)


def _fmt_ts_safe(iso: str) -> str:
    """Cross-platform timestamp formatter (handles Windows lack of %-d)."""
    try:
        dt = datetime.datetime.fromisoformat(iso)
        day = str(dt.day)          # no leading zero
        mon = dt.strftime('%b')
        yr  = dt.strftime('%Y')
        hm  = dt.strftime('%H:%M')
        return f'{day} {mon} {yr}  {hm}'
    except Exception:
        return iso


class HistoryWindow:
    def __init__(self, root, on_history_cleared: Callable | None = None) -> None:
        self.root               = root
        self.on_history_cleared = on_history_cleared
        self._history: list     = []
        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.win = ctk.CTkToplevel(self.root)
        self.win.title('Transcription History — Hotkeys')
        self.win.configure(fg_color=BG)
        self.win.minsize(540, 400)
        self.win.withdraw()
        self.win.protocol('WM_DELETE_WINDOW', self.hide)

        self._build_header()
        self._build_search()
        self._build_list()
        self._center()

    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self.win, fg_color=SURFACE, corner_radius=0, height=60)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)

        left = ctk.CTkFrame(hdr, fg_color='transparent')
        left.pack(side='left', fill='y', padx=PAD)
        ctk.CTkLabel(left, text='Transcription History',
                     font=(FONT_FAMILY, 15, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', pady=(10, 0))
        self._count_lbl = ctk.CTkLabel(
            left, text='0 entries',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
        )
        self._count_lbl.pack(anchor='w')

        right = ctk.CTkFrame(hdr, fg_color='transparent')
        right.pack(side='right', fill='y', padx=PAD)
        ctk.CTkButton(
            right, text='Clear All', width=88, height=32,
            fg_color=ERR, hover_color='#c0392b',
            text_color=TEXT_P, font=(FONT_FAMILY, 11),
            corner_radius=RADIUS_SM,
            command=self._clear_all,
        ).pack(anchor='e', pady=14)

    def _build_search(self) -> None:
        bar = ctk.CTkFrame(self.win, fg_color=SURF2, corner_radius=0, height=44)
        bar.pack(fill='x')
        bar.pack_propagate(False)

        self._search_var = ctk.StringVar()
        self._search_var.trace_add('write', lambda *_: self._render_entries())
        ctk.CTkEntry(
            bar, textvariable=self._search_var,
            placeholder_text='Search transcriptions…',
            width=340, height=28,
            fg_color=SURFACE, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 12),
            corner_radius=RADIUS_SM,
        ).pack(side='left', padx=PAD, pady=8)

    def _build_list(self) -> None:
        self._scroll = ctk.CTkScrollableFrame(
            self.win, fg_color=BG,
            scrollbar_button_color=SURF2,
            scrollbar_button_hover_color=SURF3,
        )
        self._scroll.pack(fill='both', expand=True, padx=PAD, pady=PAD)
        self._scroll.columnconfigure(0, weight=1)

    # ── Render ────────────────────────────────────────────────────────────────

    def _render_entries(self) -> None:
        # Clear existing cards
        for w in self._scroll.winfo_children():
            w.destroy()

        query = self._search_var.get().strip().lower()

        # Newest first
        entries = list(reversed(self._history))
        if query:
            entries = [e for e in entries if query in e.get('text', '').lower()]

        n = len(self._history)
        self._count_lbl.configure(
            text=f'{n} {"entry" if n == 1 else "entries"}'
        )

        if not entries:
            msg = 'No transcriptions yet.' if not self._history else 'No entries match your search.'
            ctk.CTkLabel(
                self._scroll, text=msg,
                font=(FONT_FAMILY, 13), text_color=TEXT_S,
            ).grid(row=0, column=0, pady=40)
            return

        for row_i, entry in enumerate(entries):
            self._make_card(row_i, entry)

    def _make_card(self, row_i: int, entry: dict) -> None:
        card = ctk.CTkFrame(
            self._scroll, fg_color=SURFACE,
            corner_radius=RADIUS, border_width=1, border_color=BORDER2,
        )
        card.grid(row=row_i, column=0, sticky='ew', padx=2, pady=4)
        card.columnconfigure(0, weight=1)

        # ── Top row: timestamp · lang · duration ──────────────────────────────
        top = ctk.CTkFrame(card, fg_color='transparent')
        top.grid(row=0, column=0, columnspan=2, sticky='ew', padx=PAD_SM, pady=(PAD_SM, 2))
        top.columnconfigure(1, weight=1)

        ts_text = _fmt_ts_safe(entry.get('ts', ''))
        ctk.CTkLabel(
            top, text=ts_text,
            font=(FONT_FAMILY, 11, 'bold'), text_color=ACCENT,
        ).grid(row=0, column=0, sticky='w')

        lang = (entry.get('language') or '').upper() or '?'
        dur  = entry.get('duration', 0.0)
        meta = f'{lang}  ·  {dur:.1f}s'
        ctk.CTkLabel(
            top, text=meta,
            font=(FONT_FAMILY, 10), text_color=TEXT_S,
        ).grid(row=0, column=1, sticky='w', padx=(PAD_SM, 0))

        # ── Copy button ───────────────────────────────────────────────────────
        text_val = entry.get('text', '')
        ctk.CTkButton(
            top, text='Copy', width=54, height=24,
            fg_color=SURF2, hover_color=SURF3,
            text_color=TEXT_S, font=(FONT_FAMILY, 10),
            corner_radius=RADIUS_SM,
            command=lambda t=text_val: self._copy_entry(t),
        ).grid(row=0, column=2, sticky='e', padx=(PAD_SM, 0))

        # ── Transcription text ────────────────────────────────────────────────
        ctk.CTkLabel(
            card, text=text_val,
            font=(FONT_FAMILY, 12), text_color=TEXT_P,
            anchor='nw', justify='left', wraplength=460,
        ).grid(row=1, column=0, columnspan=2, sticky='ew',
               padx=PAD_SM, pady=(2, PAD_SM))

    def _copy_entry(self, text: str) -> None:
        try:
            pyperclip.copy(text)
        except Exception:
            pass

    def _clear_all(self) -> None:
        if not self._history:
            return
        if confirm(
            self.win,
            'Clear History',
            'Delete all transcription history? This cannot be undone.',
            action_label='Clear All',
            action_color=ERR,
            action_hover='#c0392b',
        ):
            self._history = []
            save_history([])
            if self.on_history_cleared:
                self.on_history_cleared()
            self._render_entries()

    # ── Public API ────────────────────────────────────────────────────────────

    def show(self, history: list) -> None:
        """Refresh entries from the given history list and show the window."""
        self._history = list(history)
        self._search_var.set('')
        self._render_entries()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def hide(self) -> None:
        self.win.withdraw()

    def _center(self) -> None:
        self.win.update_idletasks()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        w, h   = min(640, sw - 80), min(640, sh - 80)
        self.win.geometry(f'{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}')
