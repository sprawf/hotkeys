"""
MacroSavePrompt — small floating dialog shown after a macro recording stops.
Appears near the mouse cursor and lets the user name, assign a hotkey, and
save or discard the recording.
"""
import tkinter as tk

import customtkinter as ctk

from library import HotkeyCapture
from theme import (
    BG, SURFACE, SURF2, SURF3, BORDER2,
    ACCENT, ACCENTL, TEXT_P, TEXT_S,
    FONT_FAMILY, RADIUS_SM, PAD, PAD_SM,
)


class MacroSavePrompt(ctk.CTkToplevel):
    """
    Small floating dialog: "Save this macro?"

    result = {'name': str, 'hotkey': str} if saved, None if discarded.
    """

    def __init__(self, root, default_name: str, default_hotkey: str,
                 on_hotkey_suspend=None, on_hotkey_resume=None) -> None:
        super().__init__(root)
        self.title('Save Macro')
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.grab_set()
        self.result = None

        self._on_hotkey_suspend = on_hotkey_suspend
        self._on_hotkey_resume  = on_hotkey_resume
        self._current_hotkey    = default_hotkey

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr, text='Save macro?',
                     font=(FONT_FAMILY, 14, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', padx=PAD, pady=PAD_SM)

        # ── Body ──────────────────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color='transparent')
        body.pack(fill='x', padx=PAD, pady=(PAD_SM, 0))

        # Name entry
        ctk.CTkLabel(body, text='Name', font=(FONT_FAMILY, 12),
                     text_color=TEXT_S).pack(anchor='w')
        self._name_var = tk.StringVar(value=default_name)
        self._name_entry = ctk.CTkEntry(
            body, textvariable=self._name_var, width=280,
            fg_color=SURF2, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 13),
            corner_radius=RADIUS_SM,
        )
        self._name_entry.pack(fill='x', pady=(4, PAD_SM))

        # Hotkey row
        hk_row = ctk.CTkFrame(body, fg_color='transparent')
        hk_row.pack(fill='x', pady=(0, PAD_SM))

        ctk.CTkLabel(hk_row, text='Hotkey:', font=(FONT_FAMILY, 12),
                     text_color=TEXT_S).pack(side='left', padx=(0, PAD_SM))

        self._hk_badge = ctk.CTkLabel(
            hk_row, text='', width=110, anchor='w',
            fg_color=SURF2, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 12), text_color=ACCENTL,
        )
        self._hk_badge.pack(side='left', ipady=3, ipadx=6)
        self._refresh_hk_badge()

        ctk.CTkButton(
            hk_row, text='Assign…', width=72,
            fg_color=SURF2, hover_color=SURF3,
            text_color=TEXT_P, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 12),
            command=self._assign_hk,
        ).pack(side='left', padx=(PAD_SM, 4))

        ctk.CTkButton(
            hk_row, text='✕', width=30,
            fg_color=SURF2, hover_color=SURF3,
            text_color=TEXT_P, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 12),
            command=self._clear_hk,
        ).pack(side='left')

        # ── Footer ────────────────────────────────────────────────────────────
        foot = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x', pady=(PAD_SM, 0))

        ctk.CTkButton(
            foot, text='Save', width=88,
            fg_color=ACCENT, hover_color=ACCENTL,
            text_color=TEXT_P, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 13),
            command=self._save,
        ).pack(side='right', padx=PAD, pady=PAD_SM)

        ctk.CTkButton(
            foot, text='Discard', width=80,
            fg_color=SURF2, hover_color=SURF3,
            text_color=TEXT_P, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 13),
            command=self._discard,
        ).pack(side='right', pady=PAD_SM)

        # ── Key bindings ──────────────────────────────────────────────────────
        self.bind('<Return>', lambda e: self._save())
        self.bind('<Escape>', lambda e: self._discard())

        # ── Position near mouse cursor ────────────────────────────────────────
        self._position_near_cursor()

        def _raise(e=None):
            self.lift()
            self.focus_force()
            self._name_entry.focus_set()
            self._name_entry.select_range(0, 'end')
            self.unbind('<Map>')
        self.bind('<Map>', _raise)

    # ── Hotkey helpers ────────────────────────────────────────────────────────

    def _refresh_hk_badge(self) -> None:
        if self._current_hotkey:
            self._hk_badge.configure(
                text=f'  ⌨  {self._current_hotkey.upper()}  ',
                text_color=ACCENTL,
            )
        else:
            self._hk_badge.configure(
                text='  None  ',
                text_color=TEXT_S,
            )

    def _assign_hk(self) -> None:
        if self._on_hotkey_suspend:
            self._on_hotkey_suspend()
        dlg = HotkeyCapture(self, current_hotkey=self._current_hotkey)
        self.wait_window(dlg)
        if self._on_hotkey_resume:
            self._on_hotkey_resume()
        if dlg.result is None:
            return   # cancelled
        self._current_hotkey = dlg.result
        self._refresh_hk_badge()

    def _clear_hk(self) -> None:
        self._current_hotkey = ''
        self._refresh_hk_badge()

    # ── Save / Discard ────────────────────────────────────────────────────────

    def _save(self) -> None:
        name = self._name_var.get().strip()
        if not name:
            name = 'Untitled Macro'
        self.result = {'name': name, 'hotkey': self._current_hotkey}
        self.destroy()

    def _discard(self) -> None:
        self.result = None
        self.destroy()

    # ── Geometry ─────────────────────────────────────────────────────────────

    def _position_near_cursor(self) -> None:
        """Place dialog near the current mouse cursor, keeping it on screen."""
        self.update_idletasks()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()
        mx = self.winfo_pointerx() + 24
        my = self.winfo_pointery() + 24
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        # Clamp to screen
        x = min(mx, sw - w - 8)
        y = min(my, sh - h - 48)
        x = max(x, 8)
        y = max(y, 8)
        self.geometry(f'+{x}+{y}')
