"""Prompt Library — sticky-note grid with full CRUD."""
import math
import threading
import tkinter as tk
from typing import Callable

import customtkinter as ctk
import keyboard

import spellcheck
from dialogs import alert, confirm, center_over_parent
from theme import (
    BG, SURFACE, SURF2, SURF3, BORDER, BORDER2,
    ACCENT, ACCENTL, TEXT_P, TEXT_S, TEXT_D,
    CARD_COLORS, CARD_TEXT, CARD_TEXT_S,
    FONT_FAMILY, FONT_SM_BOLD,
    PAD, PAD_SM, PAD_LG, RADIUS, RADIUS_SM,
    _darken,
)

CARD_W   = 300
CARD_PAD = 16


# ── Helper ────────────────────────────────────────────────────────────────────

def _btn(parent, text, command, width=None, fg_color=SURF2,
         hover=SURF3, text_color=TEXT_P, corner=RADIUS_SM, **kw):
    kw.update(text=text, command=command, fg_color=fg_color, hover_color=hover,
              text_color=text_color, corner_radius=corner, font=(FONT_FAMILY, 13))
    if width:
        kw['width'] = width
    return ctk.CTkButton(parent, **kw)


# ── Edit / New Prompt Dialog ──────────────────────────────────────────────────

class EditDialog(ctk.CTkToplevel):
    def __init__(self, parent, prompt: dict | None = None,
                 on_hotkey_suspend: Callable | None = None,
                 on_hotkey_resume:  Callable | None = None,
                 reserved_hotkeys:  set | None = None) -> None:
        super().__init__(parent)
        is_new = prompt is None
        self.title('New Prompt' if is_new else 'Edit Prompt')
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.transient(parent)   # stay above library window
        self.grab_set()          # block interaction with library while open
        self.result: dict | None = None
        self._on_hotkey_suspend = on_hotkey_suspend
        self._on_hotkey_resume  = on_hotkey_resume
        self._reserved_hotkeys  = reserved_hotkeys or set()
        self._hotkey            = (prompt or {}).get('hotkey', '')

        data = prompt or {'title': '', 'prompt': '', 'color': CARD_COLORS[0]}

        hdr = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr, text='New Prompt' if is_new else 'Edit Prompt',
                     font=(FONT_FAMILY, 16, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', padx=PAD, pady=PAD_SM)

        body = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        body.pack(fill='both', expand=True, padx=PAD, pady=PAD)

        ctk.CTkLabel(body, text='Title', font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w')
        self._title_var = tk.StringVar(value=data.get('title', ''))
        ctk.CTkEntry(body, textvariable=self._title_var, width=420,
                     fg_color=SURFACE, border_color=BORDER2, border_width=1,
                     text_color=TEXT_P, font=(FONT_FAMILY, 13),
                     corner_radius=RADIUS_SM).pack(fill='x', pady=(4, PAD))

        ctk.CTkLabel(body, text='Card colour', font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w')
        cf = ctk.CTkFrame(body, fg_color='transparent')
        cf.pack(anchor='w', pady=(4, PAD))

        self._color_var  = tk.StringVar(value=data.get('color', CARD_COLORS[0]))
        self._color_btns: dict[str, ctk.CTkButton] = {}
        for c in CARD_COLORS:
            btn = ctk.CTkButton(
                cf, text='', width=28, height=28, corner_radius=6,
                fg_color=c, hover_color=c, border_width=2,
                border_color=ACCENT if c == self._color_var.get() else BG,
                command=lambda col=c: self._pick(col),
            )
            btn.pack(side='left', padx=2)
            self._color_btns[c] = btn

        # ── Hotkey row ────────────────────────────────────────────────────────
        ctk.CTkLabel(body, text='Hotkey', font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w')
        hk_row = ctk.CTkFrame(body, fg_color='transparent')
        hk_row.pack(fill='x', pady=(4, PAD))

        self._hk_badge = ctk.CTkLabel(
            hk_row, text='', width=180, anchor='w',
            fg_color=SURF2, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 12), text_color=TEXT_D,
        )
        self._hk_badge.pack(side='left', ipady=4, ipadx=8)
        self._refresh_hk()

        _btn(hk_row, '⌨  Assign…', self._assign_hk, width=110).pack(side='left', padx=(8, 4))
        _btn(hk_row, '✕',          self._clear_hk,  width=36).pack(side='left')

        # ── Prompt textbox ────────────────────────────────────────────────────
        ctk.CTkLabel(body, text='Prompt', font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w')
        self._text = ctk.CTkTextbox(
            body, width=420, height=160, wrap='word',
            fg_color=SURFACE, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 13), corner_radius=RADIUS_SM,
        )
        self._text.insert('1.0', data.get('prompt', ''))
        self._text.pack(fill='x', pady=(4, 0))
        spellcheck.attach(self._text)

        foot = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')
        _btn(foot, 'Save',   self._save,   width=100, fg_color=ACCENT, hover=ACCENTL).pack(side='right', padx=PAD, pady=PAD_SM)
        _btn(foot, 'Cancel', self.destroy, width=80).pack(side='right', pady=PAD_SM)

        self._center(parent)

    # ── Hotkey helpers ────────────────────────────────────────────────────────

    def _refresh_hk(self) -> None:
        if self._hotkey:
            self._hk_badge.configure(
                text=f'  ⌨  {self._hotkey.upper()}  ',
                text_color=ACCENTL, fg_color=SURF2,
            )
        else:
            self._hk_badge.configure(text='  —  None assigned', text_color=TEXT_D, fg_color=SURFACE)

    def _assign_hk(self) -> None:
        if self._on_hotkey_suspend:
            self._on_hotkey_suspend()
        dlg = HotkeyCapture(self, current_hotkey=self._hotkey)
        self.wait_window(dlg)
        if self._on_hotkey_resume:
            self._on_hotkey_resume()
        if dlg.result is None:
            return  # cancelled
        new_hk = dlg.result
        if new_hk and new_hk.strip().lower() in self._reserved_hotkeys:
            alert(self, 'Hotkey reserved',
                  f'"{new_hk.upper()}" is a system shortcut.\n'
                  'Change system hotkeys in Settings first.')
            return
        self._hotkey = new_hk
        self._refresh_hk()

    def _clear_hk(self) -> None:
        self._hotkey = ''
        self._refresh_hk()

    # ── Color picker ─────────────────────────────────────────────────────────

    def _pick(self, color: str) -> None:
        self._color_var.set(color)
        for c, btn in self._color_btns.items():
            btn.configure(border_color=ACCENT if c == color else BG)

    def _save(self) -> None:
        title  = self._title_var.get().strip()
        prompt = self._text.get('1.0', 'end-1c').strip()
        if not title or not prompt:
            alert(self, 'Required', 'Title and Prompt cannot be empty.')
            return
        self.result = {'title': title, 'prompt': prompt, 'color': self._color_var.get(),
                       'hotkey': self._hotkey}   # '' = cleared; str = assigned
        self.destroy()

    def _center(self, parent) -> None:
        center_over_parent(self, parent)
        # Use <Map> so lift/focus fire exactly when the window appears — no
        # arbitrary delay that can race on slow machines or waste time on fast ones.
        def _raise(e=None):
            self.lift()
            self.focus_force()
            self.unbind('<Map>')
        self.bind('<Map>', _raise)


# ── Hotkey Capture Dialog ─────────────────────────────────────────────────────

class HotkeyCapture(ctk.CTkToplevel):
    """Listens for the next key combination and returns it as a string.

    result after wait_window():
        None  — user cancelled
        ''    — user chose to clear the hotkey
        str   — e.g. 'f12' or 'alt+shift+1'

    Design notes:
    • keyboard.read_hotkey() runs in a daemon thread — never on the UI thread.
    • A 350 ms startup delay prevents the right-click / Enter that opened this
      dialog from being captured as the hotkey.
    • Live preview: the dialog updates the display label as modifier keys are
      held so the user has instant feedback before the combo is committed.
    """

    # Modifier keysyms we should NOT treat as the final key
    _MODS = frozenset({
        'shift', 'ctrl', 'alt', 'control', 'win', 'super', 'caps_lock',
        'shift_l', 'shift_r', 'control_l', 'control_r',
        'alt_l', 'alt_r', 'super_l', 'super_r',
    })
    # State-bit → modifier label (tkinter bitmask).
    # 0x20000 is the Windows Alt bit; 0x0008 is the X11/some-platforms Alt bit.
    # Both map to 'alt' and dedup is handled in _get_mods via the `added` set.
    _STATE_MODS = [(0x0004, 'ctrl'), (0x0001, 'shift'), (0x20000, 'alt'), (0x0008, 'alt')]

    def __init__(self, parent, current_hotkey: str = '') -> None:
        super().__init__(parent)
        self.title('')
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.grab_set()
        self.result: str | None = None
        self._done = False

        # ── Header — matches SettingsWindow / ThemedDialog pattern ────────────
        hdr = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr, text='⌨   Assign Hotkey',
                     font=(FONT_FAMILY, 14, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', padx=PAD, pady=PAD_SM)

        # ── Separator ─────────────────────────────────────────────────────────
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')

        # ── Body ──────────────────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color='transparent', corner_radius=0)
        body.pack(fill='both', expand=True, padx=PAD_LG, pady=PAD)

        ctk.CTkLabel(body, text='Press a key combination…',
                     font=(FONT_FAMILY, 13), text_color=TEXT_S).pack()

        if current_hotkey:
            ctk.CTkLabel(body, text=f'Current:  {current_hotkey.upper()}',
                         font=(FONT_FAMILY, 11), text_color=TEXT_D).pack(pady=(4, 0))

        # Live preview chip — styled as a keyboard shortcut badge
        chip = ctk.CTkFrame(body, fg_color=SURF2, corner_radius=RADIUS_SM,
                             border_width=1, border_color=BORDER2)
        chip.pack(pady=(PAD, 0))
        self._preview_var = tk.StringVar(value='—')
        ctk.CTkLabel(chip, textvariable=self._preview_var,
                     font=(FONT_FAMILY, 15, 'bold'), text_color=ACCENTL,
                     width=210).pack(padx=PAD_LG, pady=PAD_SM)

        # ── Separator ─────────────────────────────────────────────────────────
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')

        # ── Footer — matches ThemedDialog pattern ─────────────────────────────
        foot = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')
        _btn(foot, 'Cancel', self._cancel, width=88).pack(side='right', padx=PAD, pady=PAD_SM)
        _btn(foot, 'Clear',  self._clear,  width=88).pack(side='right', pady=PAD_SM)

        self._center(parent)
        # Small delay: ignore whatever key/mouse event opened this dialog
        self.after(350, self._start_listen)

    # ── Capture logic ─────────────────────────────────────────────────────────

    def _start_listen(self) -> None:
        """Start both the tkinter binding (for live preview) and the keyboard
        thread (for accurate cross-platform hotkey string capture)."""
        self.focus_force()
        # tkinter binding for live preview only — shows modifier combos as held
        self.bind('<KeyPress>',   self._on_key_preview)
        self.bind('<KeyRelease>', self._on_key_release)
        # Accurate capture in a daemon thread via the keyboard library.
        # The thread is a daemon so it never blocks app exit.  If the user
        # clicks Cancel/Clear before pressing a key, _done=True prevents the
        # thread's eventual callback from acting; the TclError on the already-
        # destroyed widget is silently caught inside _listen_thread.
        threading.Thread(target=self._listen_thread, daemon=True).start()

    def _on_key_preview(self, event) -> None:
        """Update the display label in real time as keys are pressed."""
        if self._done:
            return
        sym = event.keysym.lower()
        if sym in self._MODS:
            # Just modifiers held — show them
            mods = self._get_mods(event.state)
            self._preview_var.set('+'.join(mods) + '+…' if mods else '—')
        else:
            mods  = self._get_mods(event.state)
            parts = mods + [event.keysym.upper() if len(event.keysym) == 1 else event.keysym]
            self._preview_var.set('+'.join(parts))

    def _on_key_release(self, event) -> None:
        sym = event.keysym.lower()
        if sym in self._MODS and not self._done:
            self._preview_var.set('—')

    @staticmethod
    def _get_mods(state: int) -> list[str]:
        seen:  list[str] = []
        added: set[str]  = set()
        for bit, name in HotkeyCapture._STATE_MODS:
            if (state & bit) and name not in added:
                seen.append(name)
                added.add(name)
        return seen

    def _listen_thread(self) -> None:
        """Block until a full hotkey combo is released, then commit."""
        try:
            hk = keyboard.read_hotkey(suppress=True)
            if not self._done:
                self._done  = True
                self.result = hk
                try:
                    self.after(0, self.destroy)
                except Exception:
                    pass
        except Exception:
            pass

    # ── Button handlers ───────────────────────────────────────────────────────

    def _clear(self) -> None:
        self._done  = True
        self.result = ''       # empty string → caller removes the hotkey
        self.destroy()

    def _cancel(self) -> None:
        self._done  = True
        self.result = None     # None → caller makes no change
        self.destroy()

    # ── Geometry ──────────────────────────────────────────────────────────────

    def _center(self, parent) -> None:
        center_over_parent(self, parent)
        def _raise(e=None):
            self.lift()
            self.focus_force()
            self.unbind('<Map>')
        self.bind('<Map>', _raise)


# ── Folder Input Dialog ───────────────────────────────────────────────────────

class FolderInputDialog(ctk.CTkToplevel):
    """Dialog for creating or renaming a folder.

    result: dict | None — {'name': str, 'color': str} on save, None on cancel.
    On rename, color defaults to the current folder color passed as current_color.
    """

    def __init__(self, parent, mode: str = 'create',
                 current: str = '', current_color: str = '') -> None:
        super().__init__(parent)
        is_create = (mode == 'create')
        self.title('New Folder' if is_create else 'Rename Folder')
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.result: dict | None = None

        # Header
        hdr = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr,
                     text='📁  New Folder' if is_create else '📁  Rename Folder',
                     font=(FONT_FAMILY, 14, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', padx=PAD, pady=PAD_SM)

        # Separator
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')

        # Body
        body = ctk.CTkFrame(self, fg_color='transparent', corner_radius=0)
        body.pack(fill='both', expand=True, padx=PAD, pady=PAD)

        ctk.CTkLabel(body, text='Folder name', font=FONT_SM_BOLD,
                     text_color=TEXT_S).pack(anchor='w')
        self._name_var = tk.StringVar(value=current)
        self._entry = ctk.CTkEntry(
            body, textvariable=self._name_var, width=320,
            fg_color=SURFACE, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 13),
            corner_radius=RADIUS_SM,
        )
        self._entry.pack(fill='x', pady=(4, PAD))

        # Colour picker — same swatches as card editor
        ctk.CTkLabel(body, text='Folder colour', font=FONT_SM_BOLD,
                     text_color=TEXT_S).pack(anchor='w')
        cf = ctk.CTkFrame(body, fg_color='transparent')
        cf.pack(anchor='w', pady=(4, 0))
        self._color_var  = tk.StringVar(value=current_color or CARD_COLORS[0])
        self._color_btns: dict[str, ctk.CTkButton] = {}
        for c in CARD_COLORS:
            btn = ctk.CTkButton(
                cf, text='', width=28, height=28, corner_radius=6,
                fg_color=c, hover_color=c, border_width=2,
                border_color=ACCENT if c == self._color_var.get() else BG,
                command=lambda col=c: self._pick(col),
            )
            btn.pack(side='left', padx=2)
            self._color_btns[c] = btn

        # Separator
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')

        # Footer
        foot = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')
        _btn(foot, 'Save', self._save, width=88,
             fg_color=ACCENT, hover=ACCENTL).pack(side='right', padx=PAD, pady=PAD_SM)
        _btn(foot, 'Cancel', self.destroy, width=80).pack(side='right', pady=PAD_SM)

        self.bind('<Escape>', lambda e: self.destroy())
        self.bind('<Return>', lambda e: self._save())

        center_over_parent(self, parent)
        def _raise(e=None):
            self.lift()
            self.focus_force()
            self._entry.focus_set()
            self.unbind('<Map>')
        self.bind('<Map>', _raise)

    def _pick(self, color: str) -> None:
        self._color_var.set(color)
        for c, btn in self._color_btns.items():
            btn.configure(border_color=ACCENT if c == color else BG)

    def _save(self) -> None:
        name = self._name_var.get().strip()
        if name:
            self.result = {'name': name, 'color': self._color_var.get()}
        self.destroy()


# ── Library Window ────────────────────────────────────────────────────────────

class LibraryWindow:
    def __init__(self, root, prompts: list, on_select: Callable, on_save: Callable,
                 hotkey_cfg: dict | None = None,
                 on_hotkey_suspend: Callable | None = None,
                 on_hotkey_resume:  Callable | None = None,
                 folders: list | None = None,
                 folder_colors: dict | None = None,
                 on_folders_changed: Callable | None = None) -> None:
        self.root               = root
        self.prompts            = list(prompts)
        self.on_select          = on_select
        self.on_save            = on_save
        self.hotkey_cfg         = hotkey_cfg or {}
        self._on_hotkey_suspend = on_hotkey_suspend
        self._on_hotkey_resume  = on_hotkey_resume
        self._folders: list[str]       = list(folders or [])
        self._folder_colors: dict[str, str] = dict(folder_colors or {})
        self._on_folders_changed = on_folders_changed
        self.active_idx  = 0
        self._cards: list[ctk.CTkFrame] = []
        self._current_cols = 2
        self._collapsed_folders: set[str] = set()
        self._folder_headers: dict[str, ctk.CTkFrame] = {}
        self._drag_folder_tgt: str | None = None
        self._build()

    def _build(self) -> None:
        self.win = ctk.CTkToplevel(self.root)
        self.win.title('Prompt Library — Hotkeys')
        self.win.configure(fg_color=BG)
        self.win.minsize(680, 460)
        self.win.withdraw()
        self.win.protocol('WM_DELETE_WINDOW', self.hide)
        self._resize_job      = None
        self._drag_src        = None   # card index being dragged
        self._drag_over       = None   # card index currently hovered
        self._drag_folder_tgt = None   # folder name being hovered ('' = root)
        self._drag_x0         = 0
        self._drag_y0         = 0
        self._drag_off_x      = 14     # ghost offset from cursor
        self._drag_off_y      = -16
        self._dragging        = False
        self._ghost: 'tk.Toplevel | None' = None   # semi-transparent drag image
        self._sep_widget: 'ctk.CTkFrame | None' = None  # separator between root and folders
        # Search state: maps displayed card position → original prompts index
        self._search_var    = tk.StringVar()
        self._filtered_idxs: list[int] = []   # original indices of currently shown cards
        self._search_var.trace_add('write', lambda *_: self._render_cards())
        self._build_header()
        self._build_grid()
        self._render_cards()
        self._center()
        self.win.bind('<Configure>', self._on_resize)

    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self.win, fg_color=SURFACE, corner_radius=0, height=72)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)

        left = ctk.CTkFrame(hdr, fg_color='transparent')
        left.pack(side='left', fill='y', padx=PAD)
        ctk.CTkLabel(left, text='Prompt Library', font=(FONT_FAMILY, 15, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', pady=(12, 0))
        self._active_lbl = ctk.CTkLabel(
            left,
            text=f'Active: {self.prompts[0]["title"] if self.prompts else "—"}',
            font=(FONT_FAMILY, 12), text_color=TEXT_S,
        )
        self._active_lbl.pack(anchor='w')

        right = ctk.CTkFrame(hdr, fg_color='transparent')
        right.pack(side='right', fill='y', padx=PAD)
        _btn(right, '＋ Add', self._add, width=88,
             fg_color=ACCENT, hover=ACCENTL).pack(anchor='e', pady=20)

        hint = ctk.CTkFrame(self.win, fg_color=SURF2, height=36, corner_radius=0)
        hint.pack(fill='x')
        hint.pack_propagate(False)
        refine_hk = self.hotkey_cfg.get('refine', 'alt+shift+w').upper()
        ctk.CTkLabel(
            hint,
            text=f'Click to activate  ·  Double-click to edit  ·  Right-click for menu  ·  {refine_hk} to refine',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
        ).pack(side='left', padx=PAD)

        search_bar = ctk.CTkFrame(self.win, fg_color=BG, corner_radius=0, height=44)
        search_bar.pack(fill='x')
        search_bar.pack_propagate(False)
        ctk.CTkEntry(
            search_bar, textvariable=self._search_var,
            placeholder_text='Search prompts…',
            height=28,
            fg_color=SURF2, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 12),
            corner_radius=RADIUS_SM,
        ).pack(fill='x', padx=PAD, pady=8)

    def _build_grid(self) -> None:
        self._scroll = ctk.CTkScrollableFrame(
            self.win, fg_color=BG,
            scrollbar_button_color=SURF2,
            scrollbar_button_hover_color=SURF3,
        )
        self._scroll.pack(fill='both', expand=True, padx=PAD, pady=PAD)
        # CTkScrollableFrame has several internal layers; bind them all so
        # right-clicking anywhere in the empty grid area shows the menu.
        for widget in (self._scroll,):
            widget.bind('<Button-3>', self._on_bg_right_click)
        for attr in ('_parent_frame', '_parent_canvas', '_canvas'):
            try:
                getattr(self._scroll, attr).bind('<Button-3>', self._on_bg_right_click)
            except Exception:
                pass

    def _cols(self) -> int:
        try:
            w = self._scroll.winfo_width()
        except Exception:
            return self._current_cols
        if w < 10:
            return self._current_cols
        return max(2, w // (CARD_W + CARD_PAD))

    def _on_resize(self, event=None) -> None:
        # Debounce: cancel any pending re-render and reschedule.
        # This prevents re-rendering on every pixel during a drag/maximize.
        if hasattr(self, '_resize_job') and self._resize_job:
            try:
                self.win.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.win.after(60, self._do_resize)

    def _do_resize(self) -> None:
        self._resize_job = None
        try:
            new_cols = self._cols()
        except Exception:
            return
        if new_cols != self._current_cols:
            self._current_cols = new_cols
            self._render_cards()

    def _render_cards(self) -> None:
        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        # Build filtered index list
        query = self._search_var.get().strip().lower()
        if query:
            self._filtered_idxs = [
                i for i, p in enumerate(self.prompts)
                if query in p.get('title', '').lower()
                or query in p.get('prompt', '').lower()
            ]
        else:
            self._filtered_idxs = list(range(len(self.prompts)))

        cols = self._current_cols
        for c in range(cols):
            self._scroll.columnconfigure(c, weight=1)

        if not self._filtered_idxs:
            ctk.CTkLabel(
                self._scroll,
                text='No prompts match your search.',
                font=(FONT_FAMILY, 13), text_color=TEXT_S,
            ).grid(row=0, column=0, columnspan=cols, pady=40)
            return

        # Partition filtered prompts into groups: one per folder + root group
        # all_folder_idxs maps folder → all (not just filtered) orig indices in it
        all_folder_counts: dict[str, int] = {}
        for fname in self._folders:
            all_folder_counts[fname] = sum(
                1 for p in self.prompts if p.get('folder', '') == fname
            )

        folder_groups: dict[str, list[int]] = {f: [] for f in self._folders}
        root_group: list[int] = []
        for orig_i in self._filtered_idxs:
            p = self.prompts[orig_i]
            folder = p.get('folder', '')
            if folder and folder in folder_groups:
                folder_groups[folder].append(orig_i)
            else:
                root_group.append(orig_i)

        current_row = 0

        # ── Root cards (no folder) — displayed first ──────────────────────────
        for card_pos_in_group, orig_i in enumerate(root_group):
            card_pos = len(self._cards)
            p = self.prompts[orig_i]
            col = card_pos_in_group % cols
            row = current_row + card_pos_in_group // cols
            card = self._make_card(card_pos, orig_i, p)
            card.grid(row=row, column=col, padx=8, pady=8, sticky='nsew')
            self._cards.append(card)
        if root_group:
            current_row += math.ceil(len(root_group) / cols)

        # ── Separator between root and folder sections ─────────────────────────
        has_folders = any(self._folders)
        self._sep_widget = None
        if has_folders:
            sep = ctk.CTkFrame(self._scroll, fg_color=BORDER, height=2, corner_radius=0)
            sep.grid(row=current_row, column=0, columnspan=cols,
                     sticky='ew', padx=8, pady=(4, 0))
            self._sep_widget = sep
            current_row += 1

        # ── Folder groups — displayed below root as card-sized folder widgets ─
        for fname in self._folders:
            group = folder_groups[fname]
            visible_count = len(group)
            all_count     = all_folder_counts.get(fname, 0)

            # Folder card occupies a single card slot (col 0 of its own row)
            fcard = self._make_folder_card(fname, visible_count, all_count)
            fcard.grid(row=current_row, column=0, padx=8, pady=(8, 2), sticky='nsew')
            self._folder_headers[fname] = fcard
            current_row += 1

            if fname not in self._collapsed_folders:
                for card_pos_in_group, orig_i in enumerate(group):
                    card_pos = len(self._cards)
                    p   = self.prompts[orig_i]
                    col = card_pos_in_group % cols
                    row = current_row + card_pos_in_group // cols
                    card = self._make_card(card_pos, orig_i, p)
                    card.grid(row=row, column=col, padx=8, pady=8, sticky='nsew')
                    self._cards.append(card)
                if group:
                    current_row += math.ceil(len(group) / cols)

        # Rebuild _filtered_idxs to match the card order (root first, then folders)
        ordered_idxs = list(root_group)
        for fname in self._folders:
            if fname not in self._collapsed_folders:
                ordered_idxs.extend(folder_groups[fname])
        self._filtered_idxs = ordered_idxs

        # Highlight based on active_idx in filtered list (if present)
        try:
            active_card_pos = self._filtered_idxs.index(self.active_idx)
        except ValueError:
            active_card_pos = -1
        self._highlight(active_card_pos)

        # Re-bind right-click on scroll canvas — card widgets added above can
        # steal events from the canvas in the empty space between/after them.
        for attr in ('_parent_canvas', '_canvas'):
            try:
                getattr(self._scroll, attr).bind('<Button-3>', self._on_bg_right_click)
            except Exception:
                pass

    def _make_folder_card(self, name: str, visible_count: int, all_count: int) -> ctk.CTkFrame:
        """Card-sized folder widget with a manila-folder tab at the top-left."""
        color    = self._folder_colors.get(name, CARD_COLORS[0])
        tab_color = _darken(color, 0.78)   # the folder tab is slightly darker
        text_fg  = CARD_TEXT
        is_col   = name in self._collapsed_folders
        arrow    = '▶' if is_col else '▼'

        # ── Outer card (same corner radius as prompt cards) ───────────────────
        outer = ctk.CTkFrame(self._scroll, fg_color=color,
                              corner_radius=RADIUS, border_width=2, border_color=BG)

        # ── Folder tab row — mimics the raised tab on a manila folder ─────────
        tab_row = ctk.CTkFrame(outer, fg_color='transparent', height=32)
        tab_row.pack(fill='x')
        tab_row.pack_propagate(False)

        tab = ctk.CTkFrame(tab_row, fg_color=tab_color, corner_radius=RADIUS_SM,
                            width=96, height=26)
        tab.place(x=12, y=4)
        tab.pack_propagate(False)

        ctk.CTkLabel(tab, text=f'{arrow}   {all_count}',
                     font=(FONT_FAMILY, 10, 'bold'),
                     text_color=text_fg).pack(expand=True)

        # ── Thin line between tab and body (the "folder spine") ───────────────
        ctk.CTkFrame(outer, fg_color=tab_color, height=1,
                     corner_radius=0).pack(fill='x')

        # ── Body ──────────────────────────────────────────────────────────────
        body = ctk.CTkFrame(outer, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=12, pady=(10, 12))

        name_lbl = ctk.CTkLabel(body, text=name,
                                 font=(FONT_FAMILY, 14, 'bold'),
                                 text_color=text_fg, anchor='w',
                                 wraplength=CARD_W - 48, justify='left')
        name_lbl.pack(anchor='w', fill='x')

        count_text = (f'{visible_count} of {all_count} shown'
                      if visible_count != all_count else
                      f'{all_count} prompt{"s" if all_count != 1 else ""}')
        ctk.CTkLabel(body, text=count_text, font=(FONT_FAMILY, 11),
                     text_color=_darken(text_fg, 0.65),
                     anchor='w').pack(anchor='w', pady=(4, 0))

        # ── ⋯ button (top-right corner) ───────────────────────────────────────
        menu_btn = _btn(outer, '⋯',
                        lambda n=name: self._show_folder_menu(n, outer),
                        width=28, fg_color='transparent', hover=tab_color,
                        text_color=text_fg, corner=RADIUS_SM)
        menu_btn.configure(font=(FONT_FAMILY, 14))
        menu_btn.place(relx=1.0, x=-8, y=36, anchor='ne')

        # ── Bindings — toggle on click, menu on right-click ───────────────────
        def _toggle(e=None, n=name):
            if n in self._collapsed_folders:
                self._collapsed_folders.discard(n)
            else:
                self._collapsed_folders.add(n)
            self._render_cards()

        def _rclick(e=None, n=name, h=outer):
            self._show_folder_menu(n, h)
            return 'break'   # stop event bubbling to canvas background binding

        for w in (outer, tab_row, tab, body, name_lbl):
            w.bind('<Button-1>', _toggle)
            w.bind('<Button-3>', _rclick)

        return outer

    def _show_folder_menu(self, name: str, header: ctk.CTkFrame) -> None:
        menu = tk.Menu(self.win, tearoff=0, bg=SURFACE, fg=TEXT_P,
                       activebackground=ACCENT, activeforeground='#fff',
                       font=(FONT_FAMILY, 12))
        menu.add_command(label='✏  Rename…', command=lambda: self._rename_folder(name))
        menu.add_separator()
        menu.add_command(label='✕  Delete folder', command=lambda: self._delete_folder(name))
        try:
            x = header.winfo_rootx()
            y = header.winfo_rooty() + header.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _make_card(self, card_pos: int, orig_i: int, prompt: dict) -> ctk.CTkFrame:
        """
        card_pos  — position in the filtered/displayed card list (used for drag-and-drop)
        orig_i    — index into self.prompts (used for all data operations)
        """
        color = prompt.get('color', CARD_COLORS[orig_i % len(CARD_COLORS)])
        outer = ctk.CTkFrame(self._scroll, fg_color=color, corner_radius=RADIUS,
                             border_width=2, border_color=BG)

        title_lbl = ctk.CTkLabel(outer, text=prompt['title'],
                                 font=(FONT_FAMILY, 14, 'bold'), text_color=CARD_TEXT,
                                 anchor='w', wraplength=CARD_W - 36, justify='left')
        title_lbl.pack(anchor='w', fill='x', padx=12, pady=(12, 4))

        # Hotkey badge (only shown when a hotkey is assigned to this prompt)
        hk_str   = prompt.get('hotkey', '').strip()
        hk_badge = None
        if hk_str:
            hk_badge = ctk.CTkLabel(
                outer,
                text=f'  ⌨  {hk_str.upper()}  ',   # leading/trailing spaces act as padding
                font=(FONT_FAMILY, 10), text_color=CARD_TEXT,
                fg_color=_darken(color, 0.78), corner_radius=RADIUS_SM,
            )
            hk_badge.pack(anchor='w', padx=12, pady=(0, 5))

        ctk.CTkFrame(outer, fg_color=CARD_TEXT_S, height=1, corner_radius=0).pack(fill='x', padx=12, pady=(0, 6))

        preview = (prompt['prompt'][:300] + '…') if len(prompt['prompt']) > 300 else prompt['prompt']
        preview_lbl = ctk.CTkLabel(outer, text=preview, font=(FONT_FAMILY, 11),
                                   text_color=CARD_TEXT_S, anchor='nw',
                                   wraplength=CARD_W - 24, justify='left')

        def _update_wrap(e, tl=title_lbl, pl=preview_lbl, o=outer):
            w = o.winfo_width()
            if w > 10:
                tl.configure(wraplength=w - 36)
                pl.configure(wraplength=w - 24)
        outer.bind('<Configure>', _update_wrap)
        preview_lbl.pack(fill='both', expand=True, padx=12, pady=(0, 12))

        # ✕ button — placed AFTER pack() children so it renders on top
        del_btn = ctk.CTkButton(
            outer, text='✕', width=26, height=26,
            fg_color=_darken(color, 0.72), hover_color=_darken(color, 0.58),
            text_color=CARD_TEXT, font=(FONT_FAMILY, 13, 'bold'), corner_radius=13,
            command=lambda i=orig_i: self._delete(i),
        )
        del_btn.place(relx=1.0, rely=0.0, anchor='ne', x=-6, y=6)
        del_btn.lift()                          # float above packed widgets
        del_btn.bind('<Button-1>', lambda e: 'break')

        # Right-click context menu
        def _show_menu(e, i=orig_i):
            menu = tk.Menu(self.win, tearoff=0,
                           bg=SURFACE, fg=TEXT_P, activebackground=ACCENT,
                           activeforeground='#ffffff', bd=0,
                           font=(FONT_FAMILY, 12))
            menu.add_command(label='✔  Select (Apply)',  command=lambda: self._select(i))
            menu.add_command(label='✏  Edit',            command=lambda: self._edit(i))
            menu.add_command(label='⌨  Assign hotkey…', command=lambda: self._assign_hotkey(i))
            menu.add_separator()
            menu.add_command(label='✕  Delete',          command=lambda: self._delete(i))
            if self._folders:
                menu.add_separator()
                # "Remove from folder" shortcut when card is already in one
                current_folder = self.prompts[i].get('folder', '')
                if current_folder:
                    menu.add_command(
                        label=f'↑  Remove from "{current_folder}"',
                        command=lambda ii=i: self._move_to_folder(ii, ''),
                    )
                # Move to folder submenu
                other_folders = [f for f in self._folders if f != current_folder]
                if other_folders:
                    folder_menu = tk.Menu(menu, tearoff=0, bg=SURFACE, fg=TEXT_P,
                                          activebackground=ACCENT, activeforeground='#fff',
                                          font=(FONT_FAMILY, 12))
                    for fname in other_folders:
                        folder_menu.add_command(
                            label=fname,
                            command=lambda ii=i, f=fname: self._move_to_folder(ii, f),
                        )
                    menu.add_cascade(label='📁  Move to folder', menu=folder_menu)
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()
            return 'break'   # stop event bubbling to canvas background binding

        # Drag only works when not filtering (search is clear) to keep index math simple
        _bindable = [outer, title_lbl, preview_lbl]
        if hk_badge is not None:
            _bindable.append(hk_badge)
        for w in _bindable:
            w.bind('<ButtonPress-1>',   lambda e, pos=card_pos: self._drag_start(e, pos))
            w.bind('<B1-Motion>',       self._drag_motion)
            w.bind('<ButtonRelease-1>', lambda e, pos=card_pos, i=orig_i: self._drag_end(e, pos, i))
            w.bind('<Double-Button-1>', lambda e, i=orig_i: (self._edit(i), 'break')[1])
            w.bind('<Button-3>',        _show_menu)

        return outer

    def _highlight(self, card_pos: int) -> None:
        """Highlight card at card_pos (display position), -1 to clear all."""
        for i, card in enumerate(self._cards):
            if i == card_pos:
                card.configure(border_color=ACCENTL, border_width=3)
            else:
                card.configure(border_color=BG, border_width=2)

    def _select(self, orig_i: int) -> None:
        """Select prompt by its original index in self.prompts."""
        self.active_idx = orig_i
        try:
            card_pos = self._filtered_idxs.index(orig_i)
        except ValueError:
            card_pos = -1
        self._highlight(card_pos)
        self.on_select(self.prompts[orig_i])
        self._active_lbl.configure(text=f'Active: {self.prompts[orig_i]["title"]}')

    def _edit(self, orig_i: int) -> None:
        reserved = {v.strip().lower() for v in self.hotkey_cfg.values() if v}
        dlg = EditDialog(self.win, self.prompts[orig_i],
                         on_hotkey_suspend=self._on_hotkey_suspend,
                         on_hotkey_resume=self._on_hotkey_resume,
                         reserved_hotkeys=reserved)
        self.win.wait_window(dlg)
        if dlg.result:
            updated = dict(self.prompts[orig_i])
            updated.update(dlg.result)
            # Hotkey: '' means the user cleared it — remove the key entirely
            if not updated.get('hotkey'):
                updated.pop('hotkey', None)
            self.prompts[orig_i] = updated
            self.on_save(self.prompts)
            self._render_cards()
            self._select(orig_i)

    def _add(self) -> None:
        reserved = {v.strip().lower() for v in self.hotkey_cfg.values() if v}
        dlg = EditDialog(self.win,
                         on_hotkey_suspend=self._on_hotkey_suspend,
                         on_hotkey_resume=self._on_hotkey_resume,
                         reserved_hotkeys=reserved)
        self.win.wait_window(dlg)
        if dlg.result:
            new_prompt = dict(dlg.result)
            if not new_prompt.get('hotkey'):
                new_prompt.pop('hotkey', None)
            self.prompts.append(new_prompt)
            self.on_save(self.prompts)
            self._render_cards()
            new_orig_i = len(self.prompts) - 1
            self._select(new_orig_i)
            # Scroll the grid so the newly added card is visible
            try:
                new_card_pos = self._filtered_idxs.index(new_orig_i)
                self._scroll_to_card(new_card_pos)
            except ValueError:
                pass

    def _scroll_to_card(self, card_pos: int) -> None:
        """Scroll the grid so the card at card_pos is visible (runs after layout)."""
        if card_pos < 0 or card_pos >= len(self._cards):
            return
        card = self._cards[card_pos]

        def _do() -> None:
            try:
                canvas = self._scroll._parent_canvas
                canvas.update_idletasks()
                bbox = canvas.bbox('all')
                if not bbox:
                    return
                total_h  = bbox[3]
                canvas_h = canvas.winfo_height()
                if total_h <= canvas_h:
                    return   # everything fits — nothing to scroll
                # Card y relative to scrollable content frame
                card_y = card.winfo_rooty() - self._scroll._parent_frame.winfo_rooty()
                card_h = card.winfo_height()
                # Centre the card vertically in the viewport if possible
                target_top = card_y - max(0, (canvas_h - card_h) // 2)
                fraction   = max(0.0, min(1.0, target_top / total_h))
                canvas.yview_moveto(fraction)
            except Exception:
                pass

        # Defer so tkinter finishes laying out the new cards first
        self.win.after(80, _do)

    def _assign_hotkey(self, orig_i: int) -> None:
        """Open HotkeyCapture dialog; save result into prompt['hotkey']."""
        current = self.prompts[orig_i].get('hotkey', '')
        # Suspend all hotkeys so existing bindings don't fire while capturing
        if self._on_hotkey_suspend:
            self._on_hotkey_suspend()
        dlg = HotkeyCapture(self.win, current_hotkey=current)
        self.win.wait_window(dlg)
        # Always resume — if on_save is also called below it will re-register
        # again, but the pending-flag loop handles that gracefully
        if self._on_hotkey_resume:
            self._on_hotkey_resume()
        if dlg.result is None:
            return   # cancelled — no change

        new_hk = dlg.result  # '' = clear, str = assign

        # Guard: reject hotkeys reserved for app-wide actions (refine / library / whisper).
        # These are read from hotkey_cfg so they respect whatever the user set in Settings.
        if new_hk:
            _LABEL = {
                'refine':  'the Refine shortcut',
                'library': 'the Prompt Library shortcut',
                'whisper': 'the Whisper / Speech-to-text shortcut',
            }
            _reserved_hk = new_hk.strip().lower()
            for _action, _hk in self.hotkey_cfg.items():
                if _hk and _hk.strip().lower() == _reserved_hk:
                    alert(
                        self.win, 'Hotkey reserved',
                        f'"{new_hk.upper()}" is {_LABEL.get(_action, "a system shortcut")}.\n\n'
                        f'To use it here, reassign the system shortcuts in Settings first.',
                    )
                    return

        # Check if this hotkey is already used by a different prompt
        if new_hk:
            conflict_i = next(
                (i for i, p in enumerate(self.prompts)
                 if i != orig_i and p.get('hotkey', '').strip().lower() == new_hk.strip().lower()),
                None,
            )
            if conflict_i is not None:
                conflict_title = self.prompts[conflict_i].get('title', f'Prompt {conflict_i + 1}')
                this_title     = self.prompts[orig_i].get('title', f'Prompt {orig_i + 1}')
                ok = confirm(
                    self.win,
                    'Hotkey already in use',
                    f'"{new_hk.upper()}" is already assigned to "{conflict_title}".\n\n'
                    f'Reassign it to "{this_title}"?',
                    action_label='Reassign',
                )
                if not ok:
                    return
                # Remove from the old prompt
                old = dict(self.prompts[conflict_i])
                old.pop('hotkey', None)
                self.prompts[conflict_i] = old

        updated = dict(self.prompts[orig_i])
        if new_hk == '':
            updated.pop('hotkey', None)   # clear
        else:
            updated['hotkey'] = new_hk
        self.prompts[orig_i] = updated
        self.on_save(self.prompts)
        self._render_cards()
        self._select(orig_i)   # keep active highlight + header label in sync

    def _delete(self, orig_i: int) -> None:
        if len(self.prompts) <= 1:
            alert(self.win, 'Cannot delete', 'You need at least one prompt.')
            return
        if confirm(self.win, 'Delete prompt',
                   f'Delete "{self.prompts[orig_i]["title"]}"?',
                   action_label='Delete',
                   action_color='#b03030', action_hover='#d04040'):
            self.prompts.pop(orig_i)
            if orig_i < self.active_idx:
                self.active_idx -= 1
            self.active_idx = min(self.active_idx, len(self.prompts) - 1)
            self.on_save(self.prompts)
            self._render_cards()
            self._select(self.active_idx)

    # ── Drag-and-drop reorder ─────────────────────────────────────────────────

    def _drag_start(self, event, card_pos: int) -> None:
        # Disable drag when search filter is active
        if self._search_var.get().strip():
            self._drag_src = None
            return
        self._drag_src  = card_pos
        self._drag_over = None
        self._drag_x0   = event.x_root
        self._drag_y0   = event.y_root
        self._dragging  = False

    def _drag_motion(self, event) -> None:
        if self._drag_src is None:
            return
        if not self._dragging:
            # Low threshold — feels immediately responsive like Windows
            if abs(event.x_root - self._drag_x0) > 3 or abs(event.y_root - self._drag_y0) > 3:
                self._dragging = True
                self._start_ghost(event.x_root, event.y_root)
                try:
                    self.win.configure(cursor='fleur')
                except Exception:
                    pass
        if self._dragging:
            # Move ghost with cursor
            if self._ghost:
                try:
                    self._ghost.geometry(
                        f'+{event.x_root + self._drag_off_x}'
                        f'+{event.y_root + self._drag_off_y}'
                    )
                except Exception:
                    pass

            over = self._card_at(event.x_root, event.y_root)
            fhdr = self._folder_header_at(event.x_root, event.y_root)
            changed = False
            if fhdr is not None:
                if self._drag_over is not None or self._drag_folder_tgt != fhdr:
                    self._drag_over = None
                    self._drag_folder_tgt = fhdr
                    changed = True
                    try:
                        self.win.configure(cursor='hand2')
                    except Exception:
                        pass
            elif over is not None:
                if self._drag_folder_tgt is not None or self._drag_over != over:
                    self._drag_folder_tgt = None
                    self._drag_over = over
                    changed = True
                    try:
                        self.win.configure(cursor='fleur')
                    except Exception:
                        pass
            else:
                if self._drag_over is not None or self._drag_folder_tgt is not None:
                    self._drag_over = None
                    self._drag_folder_tgt = None
                    changed = True
                    try:
                        self.win.configure(cursor='fleur')
                    except Exception:
                        pass
            if changed:
                self._highlight_drag()

    def _start_ghost(self, x_root: int, y_root: int) -> None:
        """Create the semi-transparent card ghost that follows the cursor."""
        if self._drag_src is None:
            return
        try:
            orig_i = self._filtered_idxs[self._drag_src]
            p      = self.prompts[orig_i]
        except (IndexError, KeyError):
            return
        color = p.get('color', CARD_COLORS[0])
        title = p.get('title', '…')
        dark  = _darken(color, 0.80)

        g = tk.Toplevel(self.win)
        g.overrideredirect(True)
        g.attributes('-topmost', True)
        g.attributes('-alpha', 0.80)
        g.configure(bg=ACCENT)          # thin ACCENT border peeks around inner frame

        inner = tk.Frame(g, bg=color)
        inner.pack(padx=2, pady=2)

        # Top colour strip (mimics card header area)
        tk.Frame(inner, bg=dark, height=6).pack(fill='x')
        tk.Label(
            inner, text=title, bg=color, fg=CARD_TEXT,
            font=(FONT_FAMILY, 12, 'bold'),
            padx=12, pady=8, anchor='w', justify='left',
            wraplength=200,
        ).pack(fill='x')

        g.geometry(f'+{x_root + self._drag_off_x}+{y_root + self._drag_off_y}')
        self._ghost = g

    def _destroy_ghost(self) -> None:
        if self._ghost is not None:
            try:
                self._ghost.destroy()
            except Exception:
                pass
            self._ghost = None

    def _dropped_in_root_zone(self, y_root: int) -> bool:
        """True if the drop y-coordinate is above the folder separator line."""
        try:
            if self._sep_widget:
                return y_root < self._sep_widget.winfo_rooty()
        except Exception:
            pass
        # No separator (no folders) → everything is root zone
        return True

    def _drag_end(self, event, card_pos: int, orig_i: int) -> None:
        dragging       = self._dragging
        src_pos        = self._drag_src
        over_pos       = self._drag_over
        folder_tgt     = self._drag_folder_tgt
        # Reset state
        self._drag_src        = None
        self._drag_over       = None
        self._drag_folder_tgt = None
        self._dragging        = False
        self._destroy_ghost()
        try:
            self.win.configure(cursor='')
        except Exception:
            pass

        if dragging and src_pos is not None and folder_tgt is not None:
            # ── Drop onto folder card → move into that folder ──────────────────
            try:
                src_orig = self._filtered_idxs[src_pos]
            except IndexError:
                return
            updated  = dict(self.prompts[src_orig])
            if folder_tgt:
                updated['folder'] = folder_tgt
            else:
                updated.pop('folder', None)
            self.prompts[src_orig] = updated
            self.on_save(self.prompts)
            self._render_cards()

        elif dragging and src_pos is not None and over_pos is not None and src_pos != over_pos:
            # ── Drop onto another card → reorder (plain swap, folders unchanged) ─
            try:
                src_orig  = self._filtered_idxs[src_pos]
                over_orig = self._filtered_idxs[over_pos]
            except IndexError:
                return
            self.prompts[src_orig], self.prompts[over_orig] = (
                self.prompts[over_orig], self.prompts[src_orig]
            )
            if self.active_idx == src_orig:
                self.active_idx = over_orig
            elif self.active_idx == over_orig:
                self.active_idx = src_orig
            self.on_save(self.prompts)
            self._render_cards()

        elif dragging and src_pos is not None:
            # ── Dropped in empty space — check if in the root zone ────────────
            try:
                src_orig = self._filtered_idxs[src_pos]
            except IndexError:
                return
            if self.prompts[src_orig].get('folder') and self._dropped_in_root_zone(event.y_root):
                updated = dict(self.prompts[src_orig])
                updated.pop('folder', None)
                self.prompts[src_orig] = updated
                self.on_save(self.prompts)
                self._render_cards()
            else:
                try:
                    active_card_pos = self._filtered_idxs.index(self.active_idx)
                except ValueError:
                    active_card_pos = -1
                self._highlight(active_card_pos)

        elif not dragging and src_pos is not None:
            # Plain click — select by original index
            self._select(orig_i)
        else:
            try:
                active_card_pos = self._filtered_idxs.index(self.active_idx)
            except ValueError:
                active_card_pos = -1
            self._highlight(active_card_pos)

    def _card_at(self, x_root: int, y_root: int) -> 'int | None':
        """Return the index of the card whose bounding box contains (x_root, y_root)."""
        for i, card in enumerate(self._cards):
            try:
                cx = card.winfo_rootx()
                cy = card.winfo_rooty()
                if cx <= x_root <= cx + card.winfo_width() and \
                   cy <= y_root <= cy + card.winfo_height():
                    return i
            except Exception:
                pass
        return None

    def _folder_header_at(self, x_root: int, y_root: int) -> 'str | None':
        """Return folder name whose header bounding box contains (x_root, y_root)."""
        for fname, header in self._folder_headers.items():
            try:
                hx = header.winfo_rootx()
                hy = header.winfo_rooty()
                if hx <= x_root <= hx + header.winfo_width() and \
                   hy <= y_root <= hy + header.winfo_height():
                    return fname
            except Exception:
                pass
        return None

    def _highlight_drag(self) -> None:
        """Visual feedback while a drag is in progress."""
        try:
            active_card_pos = self._filtered_idxs.index(self.active_idx)
        except ValueError:
            active_card_pos = -1
        for i, card in enumerate(self._cards):
            if i == self._drag_src:
                card.configure(border_color='#555555', border_width=2)   # dimmed — being lifted
            elif i == self._drag_over:
                card.configure(border_color=ACCENT, border_width=3)       # target slot
            elif i == active_card_pos:
                card.configure(border_color=ACCENTL, border_width=3)
            else:
                card.configure(border_color=BG, border_width=2)
        # Highlight folder card — strong ACCENT glow when it's the drop target
        for fname, fcard in self._folder_headers.items():
            try:
                if fname == self._drag_folder_tgt:
                    fcard.configure(border_color=ACCENT, border_width=3)
                else:
                    fcard.configure(border_color=BG, border_width=2)
            except Exception:
                pass

    # ── Folder management ─────────────────────────────────────────────────────

    def _move_to_folder(self, orig_i: int, folder: str) -> None:
        updated = dict(self.prompts[orig_i])
        if folder:
            updated['folder'] = folder
        else:
            updated.pop('folder', None)
        self.prompts[orig_i] = updated
        self.on_save(self.prompts)
        self._render_cards()

    def _create_folder(self) -> None:
        dlg = FolderInputDialog(self.win, mode='create')
        self.win.wait_window(dlg)
        if dlg.result:
            name  = dlg.result['name']
            color = dlg.result['color']
            if name not in self._folders:
                self._folders.append(name)
                self._folder_colors[name] = color
                if self._on_folders_changed:
                    self._on_folders_changed(self._folders, self._folder_colors)
                self._render_cards()

    def _rename_folder(self, old_name: str) -> None:
        current_color = self._folder_colors.get(old_name, CARD_COLORS[0])
        dlg = FolderInputDialog(self.win, mode='rename',
                                current=old_name, current_color=current_color)
        self.win.wait_window(dlg)
        if dlg.result:
            new_name  = dlg.result['name']
            new_color = dlg.result['color']
            if new_name == old_name and new_color == current_color:
                return
            # Update prompts that belong to this folder
            for i, p in enumerate(self.prompts):
                if p.get('folder', '') == old_name:
                    updated = dict(p)
                    updated['folder'] = new_name
                    self.prompts[i] = updated
            # Update folder list and colors
            idx = self._folders.index(old_name) if old_name in self._folders else -1
            if idx >= 0:
                self._folders[idx] = new_name
            self._folder_colors.pop(old_name, None)
            self._folder_colors[new_name] = new_color
            # Update collapsed state
            if old_name in self._collapsed_folders:
                self._collapsed_folders.discard(old_name)
                self._collapsed_folders.add(new_name)
            if self._on_folders_changed:
                self._on_folders_changed(self._folders, self._folder_colors)
            self.on_save(self.prompts)
            self._render_cards()

    def _delete_folder(self, name: str) -> None:
        prompts_in = [p for p in self.prompts if p.get('folder', '') == name]
        count      = len(prompts_in)

        # If the folder is empty, just confirm and remove
        if count == 0:
            if not confirm(self.win, 'Delete folder',
                           f'Delete empty folder "{name}"?',
                           action_label='Delete',
                           action_color='#b03030', action_hover='#d04040'):
                return
            self._do_remove_folder(name, delete_prompts=False)
            return

        # Non-empty — three-button dialog
        choice: list[str] = []   # populated by button callbacks

        dlg = ctk.CTkToplevel(self.win)
        dlg.title('Delete folder')
        dlg.configure(fg_color=BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        # Header
        hdr = ctk.CTkFrame(dlg, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr, text='Delete folder',
                     font=(FONT_FAMILY, 14, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', padx=PAD, pady=PAD_SM)

        ctk.CTkFrame(dlg, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')

        # Body
        noun = 'prompt' if count == 1 else 'prompts'
        ctk.CTkLabel(dlg,
                     text=f'"{name}" contains {count} {noun}.\nWhat would you like to do?',
                     font=(FONT_FAMILY, 13), text_color=TEXT_S,
                     wraplength=340, justify='left').pack(padx=PAD, pady=PAD)

        ctk.CTkFrame(dlg, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')

        # Footer
        foot = ctk.CTkFrame(dlg, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')

        def _pick(c):
            choice.append(c)
            dlg.destroy()

        ctk.CTkButton(
            foot, text='Delete with prompts',
            fg_color='#b03030', hover_color='#d04040',
            text_color='#ffffff', font=(FONT_FAMILY, 13),
            corner_radius=RADIUS_SM, width=160,
            command=lambda: _pick('all'),
        ).pack(side='right', padx=PAD, pady=PAD_SM)

        ctk.CTkButton(
            foot, text='Just delete folder',
            fg_color=SURF2, hover_color=SURF3,
            text_color=TEXT_P, font=(FONT_FAMILY, 13),
            corner_radius=RADIUS_SM, width=140,
            command=lambda: _pick('folder_only'),
        ).pack(side='right', pady=PAD_SM)

        ctk.CTkButton(
            foot, text='Cancel',
            fg_color='transparent', hover_color=SURF2,
            text_color=TEXT_S, font=(FONT_FAMILY, 13),
            corner_radius=RADIUS_SM, width=80,
            command=dlg.destroy,
        ).pack(side='right', pady=PAD_SM)

        dlg.bind('<Escape>', lambda e: dlg.destroy())
        center_over_parent(dlg, self.win)
        self.win.wait_window(dlg)

        if not choice:
            return   # cancelled

        self._do_remove_folder(name, delete_prompts=(choice[0] == 'all'))

    def _do_remove_folder(self, name: str, delete_prompts: bool) -> None:
        if delete_prompts:
            remaining = [p for p in self.prompts if p.get('folder', '') != name]
            if not remaining:
                # Deleting this folder would remove every prompt — block it.
                alert(self.win, 'Cannot delete',
                      f'All prompts are inside "{name}".\n'
                      'Move some out first, or use "Just delete folder".')
                return
            self.prompts = remaining
            # Keep active_idx in bounds
            self.active_idx = min(self.active_idx, max(0, len(self.prompts) - 1))
        else:
            # Move cards to root (clear folder field)
            for i, p in enumerate(self.prompts):
                if p.get('folder', '') == name:
                    updated = dict(p)
                    updated.pop('folder', None)
                    self.prompts[i] = updated
        if name in self._folders:
            self._folders.remove(name)
        self._folder_colors.pop(name, None)
        self._collapsed_folders.discard(name)
        if self._on_folders_changed:
            self._on_folders_changed(self._folders, self._folder_colors)
        self.on_save(self.prompts)
        self._render_cards()

    def _on_bg_right_click(self, event) -> None:
        menu = tk.Menu(self.win, tearoff=0, bg=SURFACE, fg=TEXT_P,
                       activebackground=ACCENT, activeforeground='#fff',
                       font=(FONT_FAMILY, 12))
        menu.add_command(label='✚  Create prompt',  command=self._add)
        menu.add_separator()
        menu.add_command(label='📁  Create folder', command=self._create_folder)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def show(self) -> None:
        self._render_cards()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def hide(self) -> None:
        self.win.withdraw()

    def _center(self) -> None:
        self.win.update_idletasks()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        w, h   = min(740, sw - 80), min(580, sh - 80)
        self.win.geometry(f'{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}')
