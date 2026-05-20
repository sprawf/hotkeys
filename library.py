"""Prompt Library — sticky-note grid with full CRUD."""
import tkinter as tk
from typing import Callable

import customtkinter as ctk

from dialogs import alert, confirm
from theme import (
    BG, SURFACE, SURF2, SURF3, BORDER2,
    ACCENT, ACCENTL, TEXT_P, TEXT_S,
    CARD_COLORS, CARD_TEXT, CARD_TEXT_S,
    FONT_FAMILY, FONT_SM_BOLD,
    PAD, PAD_SM, RADIUS, RADIUS_SM,
)

CARD_W   = 300
CARD_PAD = 16


def _darken(hex_color: str, factor: float = 0.72) -> str:
    """Return a darkened version of a hex card color for the ✕ button."""
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f'#{int(r*factor):02x}{int(g*factor):02x}{int(b*factor):02x}'


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
    def __init__(self, parent, prompt: dict | None = None) -> None:
        super().__init__(parent)
        is_new = prompt is None
        self.title('New Prompt' if is_new else 'Edit Prompt')
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.grab_set()
        self.result: dict | None = None

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

        ctk.CTkLabel(body, text='Prompt', font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w')
        self._text = ctk.CTkTextbox(
            body, width=420, height=180, wrap='word',
            fg_color=SURFACE, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 13), corner_radius=RADIUS_SM,
        )
        self._text.insert('1.0', data.get('prompt', ''))
        self._text.pack(fill='x', pady=(4, 0))

        foot = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')
        _btn(foot, 'Save',   self._save,   width=100, fg_color=ACCENT, hover=ACCENTL).pack(side='right', padx=PAD, pady=PAD_SM)
        _btn(foot, 'Cancel', self.destroy, width=80).pack(side='right', pady=PAD_SM)

        self._center(parent)

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
        self.result = {'title': title, 'prompt': prompt, 'color': self._color_var.get()}
        self.destroy()

    def _center(self, parent) -> None:
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(),  parent.winfo_height()
            w,  h  = self.winfo_reqwidth(), self.winfo_reqheight()
            self.geometry(f'+{px + (pw - w) // 2}+{py + (ph - h) // 2}')
        except Exception:
            pass


# ── Library Window ────────────────────────────────────────────────────────────

class LibraryWindow:
    def __init__(self, root, prompts: list, on_select: Callable, on_save: Callable,
                 hotkey_cfg: dict | None = None) -> None:
        self.root        = root
        self.prompts     = list(prompts)
        self.on_select   = on_select
        self.on_save     = on_save
        self.hotkey_cfg  = hotkey_cfg or {}
        self.active_idx  = 0
        self._cards: list[ctk.CTkFrame] = []
        self._current_cols = 2
        self._build()

    def _build(self) -> None:
        self.win = ctk.CTkToplevel(self.root)
        self.win.title('Prompt Library — Hotkeys')
        self.win.configure(fg_color=BG)
        self.win.minsize(680, 460)
        self.win.withdraw()
        self.win.protocol('WM_DELETE_WINDOW', self.hide)
        self._resize_job  = None
        self._drag_src    = None   # card index being dragged
        self._drag_over   = None   # card index currently hovered
        self._drag_x0     = 0
        self._drag_y0     = 0
        self._dragging    = False
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

        for card_pos, orig_i in enumerate(self._filtered_idxs):
            p = self.prompts[orig_i]
            row, col = divmod(card_pos, cols)
            card = self._make_card(card_pos, orig_i, p)
            card.grid(row=row, column=col, padx=8, pady=8, sticky='nsew')
            self._cards.append(card)

        # Highlight based on active_idx in filtered list (if present)
        try:
            active_card_pos = self._filtered_idxs.index(self.active_idx)
        except ValueError:
            active_card_pos = -1
        self._highlight(active_card_pos)

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
            menu.add_separator()
            menu.add_command(label='✕  Delete',          command=lambda: self._delete(i))
            menu.tk_popup(e.x_root, e.y_root)

        # Drag only works when not filtering (search is clear) to keep index math simple
        for w in (outer, title_lbl, preview_lbl):
            w.bind('<ButtonPress-1>',   lambda e, pos=card_pos: self._drag_start(e, pos))
            w.bind('<B1-Motion>',       self._drag_motion)
            w.bind('<ButtonRelease-1>', lambda e, pos=card_pos, i=orig_i: self._drag_end(e, pos, i))
            w.bind('<Double-Button-1>', lambda e, i=orig_i: (self._edit(i), 'break')[1])
            w.bind('<Button-3>',        _show_menu)

        return outer

    def _highlight(self, card_pos: int) -> None:
        """Highlight card at card_pos (display position), -1 to clear all."""
        for i, card in enumerate(self._cards):
            card.configure(border_color=ACCENT if i == card_pos else BG)

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
        dlg = EditDialog(self.win, self.prompts[orig_i])
        self.win.wait_window(dlg)
        if dlg.result:
            self.prompts[orig_i] = dlg.result
            self.on_save(self.prompts)
            self._render_cards()
            self._select(orig_i)

    def _add(self) -> None:
        dlg = EditDialog(self.win)
        self.win.wait_window(dlg)
        if dlg.result:
            self.prompts.append(dlg.result)
            self.on_save(self.prompts)
            self._render_cards()
            self._select(len(self.prompts) - 1)

    def _delete(self, orig_i: int) -> None:
        if len(self.prompts) <= 1:
            alert(self.win, 'Cannot delete', 'You need at least one prompt.')
            return
        if confirm(self.win, 'Delete prompt',
                   f'Delete "{self.prompts[orig_i]["title"]}"?',
                   action_label='Delete',
                   action_color='#b03030', action_hover='#d04040'):
            self.prompts.pop(orig_i)
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
            if abs(event.x_root - self._drag_x0) > 6 or abs(event.y_root - self._drag_y0) > 6:
                self._dragging = True
                try:
                    self.win.configure(cursor='fleur')
                except Exception:
                    pass
        if self._dragging:
            over = self._card_at(event.x_root, event.y_root)
            if over != self._drag_over:
                self._drag_over = over
                self._highlight_drag()

    def _drag_end(self, event, card_pos: int, orig_i: int) -> None:
        dragging = self._dragging
        src_pos  = self._drag_src
        over_pos = self._drag_over
        # Reset state
        self._drag_src  = None
        self._drag_over = None
        self._dragging  = False
        try:
            self.win.configure(cursor='')
        except Exception:
            pass

        if dragging and src_pos is not None and over_pos is not None and src_pos != over_pos:
            # Map card positions to original prompt indices (no filter active during drag)
            src_orig  = self._filtered_idxs[src_pos]
            over_orig = self._filtered_idxs[over_pos]
            # Swap prompts
            self.prompts[src_orig], self.prompts[over_orig] = (
                self.prompts[over_orig], self.prompts[src_orig]
            )
            # Keep active_idx tracking correct after swap
            if self.active_idx == src_orig:
                self.active_idx = over_orig
            elif self.active_idx == over_orig:
                self.active_idx = src_orig
            self.on_save(self.prompts)
            self._render_cards()
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
                card.configure(border_color=ACCENT, border_width=2)
            else:
                card.configure(border_color=BG, border_width=2)

    def show(self) -> None:
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
