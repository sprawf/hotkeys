"""Notebooks — two-column UI styled to match the NotebookLM reference.

  ┌───────────────────────────────────────────────────────────────────────┐
  │ 📒  Notebook name                          [+ Create]  ⚙              │
  ├──────────────────────────────────┬────────────────────────────────────┤
  │ Sources                       ⬜  │ Chat                            ⋮ │
  │                                  │                                    │
  │ [ + Add sources           ]      │   👋                               │
  │ ┌──────────────────────────┐     │   Let's start your notebook…       │
  │ │ Search the web …  🔎     │     │                                    │
  │ │ [Web ▾] [Fast Research]  │     │   This is your blank canvas…       │
  │ └──────────────────────────┘     │                                    │
  │                                  │   What would you like…?            │
  │           📄                     │   [Start a project]  [Learn…]      │
  │  Saved sources will appear here  │   [Create a podcast…] [Other]      │
  │  Click Add sources above…        │                                    │
  │                                  │ ┌────────────────────────────────┐ │
  │                                  │ │Ask a question…   N sources  →  │ │
  │                                  │ └────────────────────────────────┘ │
  └──────────────────────────────────┴────────────────────────────────────┘

Light theme. Empty-state-first design: the chat panel walks the user
through what to do before they have any sources or chats.
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog

import customtkinter as ctk

from . import engine
from . import md_render
from . import storage

logger = logging.getLogger(__name__)

# ── Light-theme palette (close to NotebookLM's neutral white-on-grey) ────────
BG       = '#f4f4f7'      # window background (slight blue-grey)
SURFACE  = '#ffffff'      # panel background
SURF2    = '#f6f6f9'      # subtle card / hover
SURF3    = '#ebebef'      # raised card (e.g. "Search the web")
BORDER   = '#dadce0'
BORDER2  = '#e8eaed'
TEXT_P   = '#1f1f1f'
TEXT_S   = '#5f6368'
TEXT_T   = '#80868b'
ACCENT   = '#1a73e8'      # link-blue, NotebookLM uses this for accents
ACCENTL  = '#1967d2'
DARK_PILL = '#202124'     # dark "Create notebook" CTA pill background
DARK_PILLH = '#3c4043'
USER_BUBBLE = '#e8f0fe'   # soft blue for user messages
ASSISTANT_BUBBLE = '#ffffff'   # plain white for assistant, leans on border
ERR      = '#d3573d'

FONT_FAMILY = 'Google Sans'
FONT_BODY   = 'Roboto'    # falls back to Segoe UI / system if absent
FONT_MONO   = 'Cascadia Mono'

PAD     = 16
PAD_SM  = 10
RADIUS  = 14    # rounded panels
RADIUS_SM = 22  # rounded pill buttons


class AskDocsWindow(ctk.CTkToplevel):

    def __init__(self, root: tk.Tk | None = None, *, on_close=None):
        if root is None:
            root = ctk.CTk()
            root.withdraw()
            self._owns_root = True
        else:
            self._owns_root = False
        super().__init__(root)

        self._on_close = on_close
        self._root_ref = root

        self.title('Ask Docs')
        self.configure(fg_color=BG)
        self._center_on_screen(1216, 796)
        self.minsize(960, 620)

        # State
        self._current_nb_id: str | None = None
        self._current_chat: dict | None = None
        self._sources_cache: list[dict] = []
        self._is_thinking = False
        # When the user clicks INTO a source, the left panel transforms
        # from the source-list view to a single-source preview. None =
        # list view, str = id of the source being previewed.
        self._viewing_source_id: str | None = None
        # When non-None, the next source-preview render scrolls its body
        # text to this citation's passage. Set by _show_citation().
        self._pending_citation_jump: dict | None = None
        # Per-checkbox bookkeeping (tk.BooleanVar refs by source id) so
        # we can read the live state without walking the widget tree.
        self._source_check_vars: dict[str, tk.BooleanVar] = {}

        self._install_text_context_menu()
        self._build()
        self._refresh_notebook_picker()
        nbs = storage.list_notebooks()
        if nbs:
            self._open_notebook(nbs[0]['id'])
        else:
            # Match NotebookLM's first-launch behaviour: auto-create
            # "Untitled notebook" instead of asking the user.
            nb = storage.create_notebook('Untitled doc set')
            self._refresh_notebook_picker()
            self._open_notebook(nb['id'])

        self.protocol('WM_DELETE_WINDOW', self._handle_close)

    # ── Global right-click context menu for text inputs ─────────────────────

    def _install_text_context_menu(self) -> None:
        """Wire Cut / Copy / Paste / Select-all onto EVERY tk.Entry and
        tk.Text in the app via class-level bindings, so every input box
        (including ones inside dialogs built later) gets the menu without
        per-widget setup. CTkEntry wraps a tk.Entry, CTkTextbox wraps a
        tk.Text — both are covered."""
        self._text_ctx_menu = tk.Menu(self, tearoff=0)
        # The widget that triggered the popup; the commands operate on it.
        self._text_ctx_target: tk.Widget | None = None

        def _do(action: str) -> None:
            w = self._text_ctx_target
            if w is None:
                return
            try:
                if action == 'cut':
                    w.event_generate('<<Cut>>')
                elif action == 'copy':
                    w.event_generate('<<Copy>>')
                elif action == 'paste':
                    w.event_generate('<<Paste>>')
                elif action == 'select_all':
                    if isinstance(w, tk.Entry):
                        w.select_range(0, 'end')
                        w.icursor('end')
                    else:
                        # tk.Text
                        w.tag_add('sel', '1.0', 'end-1c')
                        w.mark_set('insert', 'end-1c')
                        w.see('insert')
            except Exception as e:
                logger.debug(f'context menu {action} failed: {e}')

        m = self._text_ctx_menu
        m.add_command(label='Cut',        accelerator='Ctrl+X',
                      command=lambda: _do('cut'))
        m.add_command(label='Copy',       accelerator='Ctrl+C',
                      command=lambda: _do('copy'))
        m.add_command(label='Paste',      accelerator='Ctrl+V',
                      command=lambda: _do('paste'))
        m.add_separator()
        m.add_command(label='Select all', accelerator='Ctrl+A',
                      command=lambda: _do('select_all'))

        def _popup(event) -> str:
            w = event.widget
            self._text_ctx_target = w
            # Disable Cut / Paste on read-only widgets so the user isn't
            # offered actions that silently no-op.
            try:
                state = str(w.cget('state'))
            except Exception:
                state = 'normal'
            editable = state == 'normal'
            self._text_ctx_menu.entryconfigure('Cut',
                state='normal' if editable else 'disabled')
            self._text_ctx_menu.entryconfigure('Paste',
                state='normal' if editable else 'disabled')
            # Gray Copy when nothing is selected.
            has_sel = False
            try:
                if isinstance(w, tk.Entry):
                    has_sel = w.selection_present()
                else:
                    has_sel = bool(w.tag_ranges('sel'))
            except Exception:
                pass
            self._text_ctx_menu.entryconfigure('Copy',
                state='normal' if has_sel else 'disabled')
            try:
                self._text_ctx_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._text_ctx_menu.grab_release()
            return 'break'

        # Bind at the Tk class level so EVERY current and future Entry /
        # Text widget gets it — no per-widget bookkeeping needed.
        self.bind_class('Entry', '<Button-3>', _popup)
        self.bind_class('Text',  '<Button-3>', _popup)
        # Ctrl+A select-all also missing by default on Tk Entry/Text on
        # Windows — add it here so users get the conventional shortcut
        # everywhere too.
        def _select_all_evt(event) -> str:
            self._text_ctx_target = event.widget
            _do('select_all')
            return 'break'
        self.bind_class('Entry', '<Control-a>', _select_all_evt)
        self.bind_class('Entry', '<Control-A>', _select_all_evt)
        self.bind_class('Text',  '<Control-a>', _select_all_evt)
        self.bind_class('Text',  '<Control-A>', _select_all_evt)

    # ── Top bar ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # The whole window background is the off-white BG. Panels float
        # on top with white SURFACE and rounded corners.
        topbar = ctk.CTkFrame(self, fg_color=BG, corner_radius=0, height=60)
        topbar.pack(fill='x', side='top')
        topbar.pack_propagate(False)

        # ── Left side: logo + editable notebook title ────────────────────────
        left_grp = ctk.CTkFrame(topbar, fg_color='transparent')
        left_grp.pack(side='left', padx=PAD, pady=10)

        # Logo (round chip with an icon). Simple coloured circle with emoji.
        logo = ctk.CTkLabel(
            left_grp, text='📒', width=40, height=40,
            font=(FONT_FAMILY, 18), text_color=TEXT_P,
            fg_color=SURFACE, corner_radius=20,
        )
        logo.pack(side='left', padx=(0, 14))

        # Inline-editable notebook title. Renders as a Label until clicked,
        # then swaps to an Entry. Saves on blur or Enter, cancels on Esc.
        self._title_lbl = ctk.CTkLabel(
            left_grp, text='Untitled doc set',
            font=(FONT_FAMILY, 20, 'bold'), text_color=TEXT_P,
            anchor='w', cursor='hand2',
        )
        self._title_lbl.pack(side='left')
        self._title_lbl.bind('<Button-1>', lambda e: self._begin_title_edit())
        self._title_entry: ctk.CTkEntry | None = None

        # ── Right side: New / Rename(via title click) / Delete / picker ──────
        right_grp = ctk.CTkFrame(topbar, fg_color='transparent')
        right_grp.pack(side='right', padx=PAD, pady=10)

        # Notebook switcher dropdown — kept compact, sits just left of the CTA
        self._nb_picker = ctk.CTkOptionMenu(
            right_grp, values=['Loading…'], width=200,
            fg_color=SURFACE, button_color=SURFACE, button_hover_color=SURF2,
            text_color=TEXT_P, font=(FONT_BODY, 12),
            corner_radius=RADIUS_SM, dropdown_fg_color=SURFACE,
            dropdown_text_color=TEXT_P, dropdown_hover_color=SURF2,
            command=self._on_picker_change,
        )
        self._nb_picker.pack(side='left', padx=(0, PAD_SM))

        ctk.CTkButton(
            right_grp, text='+ Create doc set', height=40,
            fg_color=DARK_PILL, hover_color=DARK_PILLH, text_color='#ffffff',
            font=(FONT_FAMILY, 12, 'bold'), corner_radius=RADIUS_SM,
            command=lambda: self._new_notebook_dialog(),
        ).pack(side='left', padx=(0, PAD_SM))

        ctk.CTkButton(
            right_grp, text='Delete', width=80, height=40,
            fg_color=SURFACE, hover_color='#fce8e6', text_color=ERR,
            font=(FONT_BODY, 12), corner_radius=RADIUS_SM,
            border_width=1, border_color=BORDER2,
            command=self._delete_notebook_dialog,
        ).pack(side='left')

        # ── Two-column body ──────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=0, minsize=340)   # Sources
        body.columnconfigure(1, weight=1)                # Chat
        body.rowconfigure(0, weight=1)

        self._sources_col = self._build_sources_column(body)
        self._sources_col.grid(row=0, column=0, sticky='nsew',
                               padx=(PAD, PAD_SM), pady=(0, PAD))

        self._chat_col = self._build_chat_column(body)
        self._chat_col.grid(row=0, column=1, sticky='nsew',
                            padx=(PAD_SM, PAD), pady=(0, PAD))

        # Status pill stays at the bottom — only visible while we're working
        self._status_bar = ctk.CTkLabel(
            self, text='', text_color=TEXT_T,
            font=(FONT_BODY, 10), height=14, fg_color=BG,
        )
        self._status_bar.pack(fill='x', side='bottom')

    # ── Sources column ───────────────────────────────────────────────────────

    def _build_sources_column(self, parent) -> ctk.CTkFrame:
        col = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=RADIUS,
                           border_width=1, border_color=BORDER2)
        # Header row: "Sources" + decorative sidebar toggle icon
        hdr = ctk.CTkFrame(col, fg_color='transparent', height=44)
        hdr.pack(fill='x', padx=PAD, pady=(PAD_SM, 0))
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text='Sources',
                     font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_P,
                     ).pack(side='left', pady=10)
        # (No decorative right-side icon — the original ◧ was a
        # NotebookLM sidebar-collapse toggle that we don't have.)

        # Big rounded "Add sources" button
        ctk.CTkButton(
            col, text='+  Add sources', height=42,
            fg_color=SURFACE, hover_color=SURF2, text_color=TEXT_P,
            font=(FONT_BODY, 12, 'bold'), corner_radius=RADIUS_SM,
            border_width=1, border_color=BORDER,
            command=self._show_add_source_menu,
        ).pack(fill='x', padx=PAD, pady=(8, PAD_SM))

        # (Web-search card intentionally omitted — Notebooks is offline-first
        # and ingests via the + Add sources flow instead.)

        # Scrollable list of saved sources
        self._sources_list = ctk.CTkScrollableFrame(
            col, fg_color='transparent', scrollbar_button_color=BORDER2,
        )
        self._sources_list.pack(fill='both', expand=True,
                                padx=PAD_SM, pady=(0, PAD))

        # Empty state placeholder lives inside _sources_list; we add/remove
        # it in _refresh_sources().
        return col

    # ── Chat column ──────────────────────────────────────────────────────────

    def _build_chat_column(self, parent) -> ctk.CTkFrame:
        col = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=RADIUS,
                           border_width=1, border_color=BORDER2)
        # Header
        hdr = ctk.CTkFrame(col, fg_color='transparent', height=44)
        hdr.pack(fill='x', padx=PAD, pady=(PAD_SM, 0))
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text='Chat',
                     font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_P,
                     ).pack(side='left', pady=10)
        ctk.CTkButton(
            hdr, text='⋮', width=28, height=28,
            fg_color='transparent', hover_color=SURF2, text_color=TEXT_T,
            font=(FONT_BODY, 14, 'bold'), corner_radius=14,
            command=self._show_chat_menu,
        ).pack(side='right', pady=8)

        # Transcript scroll area + empty-state container live here
        self._chat_scroll = ctk.CTkScrollableFrame(
            col, fg_color='transparent', scrollbar_button_color=BORDER2,
        )
        self._chat_scroll.pack(fill='both', expand=True,
                               padx=PAD, pady=(0, 0))

        # Input pill at the bottom
        input_wrap = ctk.CTkFrame(col, fg_color='transparent')
        input_wrap.pack(fill='x', padx=PAD, pady=(PAD_SM, PAD))
        self._input_bar = ctk.CTkFrame(
            input_wrap, fg_color=SURFACE,
            corner_radius=RADIUS, border_width=1, border_color=BORDER,
        )
        self._input_bar.pack(fill='x')
        self._input = ctk.CTkTextbox(
            self._input_bar, height=44, fg_color=SURFACE, text_color=TEXT_P,
            font=(FONT_BODY, 12),
            border_width=0, wrap='word', corner_radius=RADIUS,
        )
        self._input.pack(side='left', fill='both', expand=True,
                         padx=(PAD, 0), pady=PAD_SM)
        # Source count + send button on the right edge of the input
        right_box = ctk.CTkFrame(self._input_bar, fg_color='transparent')
        right_box.pack(side='right', padx=PAD_SM, pady=PAD_SM)
        self._source_count_lbl = ctk.CTkLabel(
            right_box, text='0 sources',
            font=(FONT_BODY, 10), text_color=TEXT_T,
        )
        self._source_count_lbl.pack(side='left', padx=(0, 8))
        self._send_btn = ctk.CTkButton(
            right_box, text='→', width=36, height=36,
            fg_color=TEXT_P, hover_color=DARK_PILLH, text_color='#ffffff',
            font=(FONT_BODY, 16, 'bold'), corner_radius=18,
            command=self._send,
        )
        self._send_btn.pack(side='left')

        # Ctrl+Enter to send
        self._input.bind('<Control-Return>', lambda e: (self._send(), 'break')[1])
        # Auto-grow placeholder rendering — CTk doesn't natively support a
        # placeholder on textbox; we fake it with grey "Ask a question…"
        # that clears on first keypress.
        self._input.insert('1.0', _PLACEHOLDER)
        self._input.configure(text_color=TEXT_T)
        self._input.bind('<FocusIn>', self._on_input_focus_in)
        self._input.bind('<FocusOut>', self._on_input_focus_out)
        return col

    # ── Notebook switching ───────────────────────────────────────────────────

    def _refresh_notebook_picker(self) -> None:
        nbs = storage.list_notebooks()
        if not nbs:
            self._nb_picker.configure(values=['(no doc sets)'])
            self._nb_picker.set('(no doc sets)')
            return
        names = [nb['name'] for nb in nbs]
        seen, labels = {}, []
        for n in names:
            seen[n] = seen.get(n, 0) + 1
            labels.append(n if seen[n] == 1 else f'{n} ({seen[n]})')
        self._nb_labels_to_ids = {labels[i]: nbs[i]['id'] for i in range(len(nbs))}
        self._nb_picker.configure(values=labels)

    def _on_picker_change(self, label: str) -> None:
        nb_id = getattr(self, '_nb_labels_to_ids', {}).get(label)
        if nb_id:
            self._open_notebook(nb_id)

    def _open_notebook(self, nb_id: str) -> None:
        nb = storage.get_notebook(nb_id)
        if nb is None: return
        self._current_nb_id = nb_id
        self._title_lbl.configure(text=nb['name'])
        for label, nid in getattr(self, '_nb_labels_to_ids', {}).items():
            if nid == nb_id:
                self._nb_picker.set(label)
                break
        self._current_chat = self._load_or_create_chat(nb_id)
        self._refresh_sources()
        self._render_chat()

    # ── Inline title editing ─────────────────────────────────────────────────

    def _begin_title_edit(self) -> None:
        """Swap the title label for an entry. Click anywhere else, Enter,
        or Esc to finish."""
        if not self._current_nb_id:
            return
        if self._title_entry is not None:
            return  # already editing
        current = self._title_lbl.cget('text')
        self._title_lbl.pack_forget()
        self._title_entry = ctk.CTkEntry(
            self._title_lbl.master, width=320, height=36,
            fg_color=SURF2, text_color=TEXT_P,
            font=(FONT_FAMILY, 20, 'bold'), border_color=BORDER,
            border_width=1, corner_radius=8,
        )
        self._title_entry.insert(0, current)
        self._title_entry.pack(side='left')
        self._title_entry.focus_set()
        self._title_entry.select_range(0, 'end')
        self._title_entry.bind('<Return>',   lambda e: self._commit_title_edit(True))
        self._title_entry.bind('<KP_Enter>', lambda e: self._commit_title_edit(True))
        self._title_entry.bind('<Escape>',   lambda e: self._commit_title_edit(False))
        self._title_entry.bind('<FocusOut>', lambda e: self._commit_title_edit(True))

    def _commit_title_edit(self, save: bool) -> None:
        if self._title_entry is None:
            return
        new_name = self._title_entry.get().strip() if save else None
        self._title_entry.destroy()
        self._title_entry = None
        if save and new_name and self._current_nb_id:
            nb = storage.get_notebook(self._current_nb_id)
            if nb and new_name != nb['name']:
                nb['name'] = new_name
                storage.save_meta(self._current_nb_id, nb)
                self._title_lbl.configure(text=new_name)
                self._refresh_notebook_picker()
                # Restore selection in the picker
                for label, nid in getattr(self, '_nb_labels_to_ids', {}).items():
                    if nid == self._current_nb_id:
                        self._nb_picker.set(label)
                        break
        self._title_lbl.pack(side='left')

    # ── New / delete ─────────────────────────────────────────────────────────

    def _new_notebook_dialog(self) -> None:
        nb = storage.create_notebook('Untitled doc set')
        self._refresh_notebook_picker()
        self._open_notebook(nb['id'])
        # Drop the user straight into title editing — easier than a modal.
        self.after(150, self._begin_title_edit)

    def _delete_notebook_dialog(self) -> None:
        if not self._current_nb_id: return
        nb = storage.get_notebook(self._current_nb_id)
        if nb is None: return
        from tkinter import messagebox
        if not messagebox.askyesno(
            'Delete doc set?',
            f'Permanently delete "{nb["name"]}" and all its sources and '
            'chats? This cannot be undone.',
            parent=self,
        ):
            return
        storage.delete_notebook(self._current_nb_id)
        self._current_nb_id = None
        self._refresh_notebook_picker()
        nbs = storage.list_notebooks()
        if nbs:
            self._open_notebook(nbs[0]['id'])
        else:
            new = storage.create_notebook('Untitled doc set')
            self._refresh_notebook_picker()
            self._open_notebook(new['id'])

    # ── Sources ──────────────────────────────────────────────────────────────

    def _refresh_sources(self) -> None:
        for w in self._sources_list.winfo_children():
            w.destroy()
        self._source_check_vars.clear()
        if not self._current_nb_id:
            return

        # In source-preview mode, the list view is replaced by a single-
        # source detail panel. Render that and bail.
        if self._viewing_source_id is not None:
            self._render_source_preview(self._viewing_source_id)
            return

        self._sources_cache = storage.list_sources(self._current_nb_id)
        n = len(self._sources_cache)
        # Count text only reflects SELECTED sources (matches NotebookLM:
        # the "1 source" / "5 sources" pill next to send shows the active
        # scope, not the total).
        self._update_source_count()
        if not self._sources_cache:
            self._render_sources_empty_state()
            return

        # "Select all" master checkbox (only when ≥ 2 sources — NotebookLM
        # hides this for a single-source notebook).
        nb = storage.get_notebook(self._current_nb_id) or {}
        selected_ids = set(nb.get('selected_source_ids',
                                  [s['id'] for s in self._sources_cache]))
        all_selected = all(s['id'] in selected_ids for s in self._sources_cache)
        if len(self._sources_cache) >= 2:
            header_row = ctk.CTkFrame(self._sources_list, fg_color='transparent')
            header_row.pack(fill='x', pady=(0, 4))
            self._select_all_var = tk.BooleanVar(value=all_selected)
            ctk.CTkLabel(
                header_row, text='Select all',
                font=(FONT_BODY, 11), text_color=TEXT_S, anchor='w',
            ).pack(side='left', padx=(4, 0))
            ctk.CTkCheckBox(
                header_row, text='', width=24,
                variable=self._select_all_var,
                fg_color=ACCENT, hover_color=ACCENTL,
                border_color=BORDER, checkmark_color='#ffffff',
                command=self._toggle_select_all,
            ).pack(side='right', padx=4)

        for s in self._sources_cache:
            checked = s['id'] in selected_ids
            self._render_source_card(s, checked=checked)

    def _render_sources_empty_state(self) -> None:
        container = ctk.CTkFrame(self._sources_list, fg_color='transparent')
        container.pack(expand=True, fill='both', pady=(40, 0))
        ctk.CTkLabel(
            container, text='📄',
            font=(FONT_BODY, 28), text_color=TEXT_T,
        ).pack(pady=(0, 8))
        ctk.CTkLabel(
            container, text='Saved sources will appear here',
            font=(FONT_BODY, 11, 'bold'), text_color=TEXT_S,
        ).pack()
        ctk.CTkLabel(
            container,
            text=('Click Add sources above to add PDFs, websites, text, '
                  'videos, or audio files.'),
            font=(FONT_BODY, 10), text_color=TEXT_T,
            wraplength=260, justify='center',
        ).pack(pady=(4, 0))

    def _render_source_card(self, s: dict, *, checked: bool = True) -> None:
        """One row in the sources list: icon + name + checkbox. Clicking
        the name opens the source preview; clicking the checkbox toggles
        whether this source is in scope for the chat."""
        kind_icon = {
            'pdf':         '📄', 'doc':       '📝', 'slides':   '📊',
            'spreadsheet': '📈', 'web':       '🌐', 'book':     '📚',
            'text':        '📃', 'audio':     '🎵', 'image':    '🖼️',
            'archive':     '📦', 'url':       '🔗',
        }.get(s.get('kind', 'text'), '📄')

        card = ctk.CTkFrame(
            self._sources_list, fg_color=SURFACE,
            corner_radius=RADIUS, border_width=1, border_color=BORDER2,
        )
        card.pack(fill='x', pady=(0, 6))

        # Body: clickable area (icon + name) on the left, checkbox on the right
        row = ctk.CTkFrame(card, fg_color='transparent')
        row.pack(fill='x', padx=PAD_SM, pady=PAD_SM)

        # Left side: icon + truncated name. Clicking opens the source preview.
        click_zone = ctk.CTkFrame(row, fg_color='transparent', cursor='hand2')
        click_zone.pack(side='left', fill='x', expand=True)
        # Tk doesn't propagate clicks from child widgets to the parent frame,
        # so we bind on each non-button widget separately.
        click_zone.bind('<Button-1>',
                        lambda _e, sid=s['id']: self._open_source_preview(sid))

        icon_lbl = ctk.CTkLabel(
            click_zone, text=kind_icon, font=(FONT_BODY, 14),
            text_color=TEXT_S, width=24, cursor='hand2',
        )
        icon_lbl.pack(side='left', padx=(0, 6))
        icon_lbl.bind('<Button-1>',
                      lambda _e, sid=s['id']: self._open_source_preview(sid))

        text_box = ctk.CTkFrame(click_zone, fg_color='transparent', cursor='hand2')
        text_box.pack(side='left', fill='x', expand=True)
        text_box.bind('<Button-1>',
                      lambda _e, sid=s['id']: self._open_source_preview(sid))

        name_lbl = ctk.CTkLabel(
            text_box, text=s['name'],
            font=(FONT_BODY, 11, 'bold'), text_color=TEXT_P,
            anchor='w', justify='left', cursor='hand2',
        )
        name_lbl.pack(fill='x')
        name_lbl.bind('<Button-1>',
                      lambda _e, sid=s['id']: self._open_source_preview(sid))

        meta_lbl = ctk.CTkLabel(
            text_box,
            text=(f'{s.get("chunk_count", 0)} chunk'
                  f'{"s" if s.get("chunk_count", 0) != 1 else ""}'),
            font=(FONT_BODY, 9), text_color=TEXT_T,
            anchor='w', cursor='hand2',
        )
        meta_lbl.pack(fill='x')
        meta_lbl.bind('<Button-1>',
                      lambda _e, sid=s['id']: self._open_source_preview(sid))

        # Right side: checkbox
        var = tk.BooleanVar(value=checked)
        self._source_check_vars[s['id']] = var
        ctk.CTkCheckBox(
            row, text='', width=24,
            variable=var,
            fg_color=ACCENT, hover_color=ACCENTL,
            border_color=BORDER, checkmark_color='#ffffff',
            command=lambda sid=s['id']: self._on_source_check_toggled(sid),
        ).pack(side='right', padx=(8, 0))

    # ── Source selection ────────────────────────────────────────────────────

    def _on_source_check_toggled(self, source_id: str) -> None:
        """Persist the new selection state to notebook meta + update the
        send-button source count."""
        if not self._current_nb_id:
            return
        nb = storage.get_notebook(self._current_nb_id)
        if nb is None:
            return
        selected = [
            sid for sid, var in self._source_check_vars.items()
            if var.get()
        ]
        nb['selected_source_ids'] = selected
        storage.save_meta(self._current_nb_id, nb)
        # Sync the master "Select all" checkbox
        try:
            all_sel = len(selected) == len(self._sources_cache)
            if hasattr(self, '_select_all_var'):
                self._select_all_var.set(all_sel)
        except Exception:
            pass
        self._update_source_count()

    def _toggle_select_all(self) -> None:
        """The 'Select all' master checkbox click."""
        if not self._current_nb_id:
            return
        new_state = self._select_all_var.get()
        for var in self._source_check_vars.values():
            var.set(new_state)
        nb = storage.get_notebook(self._current_nb_id)
        if nb is not None:
            nb['selected_source_ids'] = (
                [s['id'] for s in self._sources_cache] if new_state else []
            )
            storage.save_meta(self._current_nb_id, nb)
        self._update_source_count()

    def _update_source_count(self) -> None:
        """Refresh the 'N sources' label next to the send button. Reflects
        the SELECTED count, not the total — matches NotebookLM's pill."""
        if not self._current_nb_id:
            self._source_count_lbl.configure(text='0 sources')
            return
        nb = storage.get_notebook(self._current_nb_id) or {}
        sources = self._sources_cache or storage.list_sources(self._current_nb_id)
        if 'selected_source_ids' in nb:
            n = len([s for s in sources
                     if s['id'] in set(nb['selected_source_ids'])])
        else:
            n = len(sources)
        self._source_count_lbl.configure(
            text=f'{n} source{"s" if n != 1 else ""}')

    def _get_selected_source_ids(self) -> list[str]:
        """Returns the currently active (checked) source IDs, falling back
        to ALL sources if no explicit selection is stored."""
        if not self._current_nb_id:
            return []
        nb = storage.get_notebook(self._current_nb_id) or {}
        sources = self._sources_cache or storage.list_sources(self._current_nb_id)
        if 'selected_source_ids' in nb:
            return list(nb['selected_source_ids'])
        return [s['id'] for s in sources]

    # ── Source preview (left panel takeover) ────────────────────────────────

    def _open_source_preview(self, source_id: str) -> None:
        """Click a source in the list → swap the list view for a single-
        source content view. Matches NotebookLM's behaviour exactly."""
        self._viewing_source_id = source_id
        self._refresh_sources()

    def _close_source_preview(self) -> None:
        self._viewing_source_id = None
        self._refresh_sources()

    def _render_source_preview(self, source_id: str) -> None:
        """Single-source detail panel: name + close + Source guide +
        full source text rendered with markdown."""
        src = storage.get_source(self._current_nb_id, source_id)
        if src is None:
            # Source was deleted while we were viewing it; bail to list.
            self._close_source_preview()
            return

        # Header row: back arrow + truncated name + external-link button
        hdr = ctk.CTkFrame(self._sources_list, fg_color='transparent')
        hdr.pack(fill='x', pady=(0, 6))
        ctk.CTkButton(
            hdr, text='←', width=28, height=28,
            fg_color='transparent', hover_color=SURF2, text_color=TEXT_S,
            font=(FONT_BODY, 14), corner_radius=14,
            command=self._close_source_preview,
        ).pack(side='left')
        ctk.CTkLabel(
            hdr, text=src.get('name', 'Source'),
            font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_P,
            anchor='w', justify='left',
        ).pack(side='left', padx=(4, 0), fill='x', expand=True)
        # External-link button: opens the origin in a browser, when the
        # source is a URL or a file path.
        origin = src.get('origin', '')
        if origin and (origin.startswith('http://') or origin.startswith('https://')
                       or '\\' in origin or '/' in origin):
            ctk.CTkButton(
                hdr, text='↗', width=28, height=28,
                fg_color='transparent', hover_color=SURF2, text_color=TEXT_S,
                font=(FONT_BODY, 12), corner_radius=14,
                command=lambda: self._open_external(origin),
            ).pack(side='right')

        # Source guide card. Loads lazily — first visit pops a "Generating…"
        # state, subsequent visits hit the cached text.
        guide_card = ctk.CTkFrame(self._sources_list, fg_color=SURF3,
                                  corner_radius=RADIUS)
        guide_card.pack(fill='x', pady=(0, 6))
        ctk.CTkLabel(
            guide_card, text='✨  Source guide',
            font=(FONT_BODY, 11, 'bold'), text_color=TEXT_P,
            anchor='w',
        ).pack(fill='x', padx=PAD_SM, pady=(PAD_SM, 4))

        guide_text = src.get('guide') or ''
        guide_body = tk.Text(
            guide_card, wrap='word', bg=SURF3, fg=TEXT_P,
            font=(FONT_BODY, 11), bd=0, highlightthickness=0,
            padx=0, pady=0, cursor='arrow', height=4,
        )
        guide_body.pack(fill='x', padx=PAD_SM, pady=(0, PAD_SM))
        md_render.install_tags(
            guide_body,
            body_font=(FONT_BODY, 11), mono_font=(FONT_MONO, 10),
            fg=TEXT_P, accent=ACCENT, accent_bg=USER_BUBBLE,
            code_bg=SURFACE, heading_font_family=FONT_FAMILY,
        )
        if guide_text:
            md_render.render(guide_body, guide_text)
            guide_body.config(height=min(20, md_render.estimate_line_count(guide_body) + 1))
        else:
            guide_body.insert('1.0', 'Generating…')
            guide_body.config(state='disabled')
            # Kick off generation off the UI thread.
            self._generate_guide_in_background(source_id, guide_body)

        # Full source text (read-only).
        body_card = ctk.CTkFrame(self._sources_list, fg_color='transparent')
        body_card.pack(fill='both', expand=True)
        body_text = tk.Text(
            body_card, wrap='word', bg=SURFACE, fg=TEXT_P,
            font=(FONT_BODY, 11), bd=0, highlightthickness=0,
            padx=0, pady=0, cursor='arrow',
        )
        body_text.pack(fill='both', expand=True)
        md_render.install_tags(
            body_text,
            body_font=(FONT_BODY, 11), mono_font=(FONT_MONO, 10),
            fg=TEXT_P, accent=ACCENT, accent_bg=USER_BUBBLE,
            code_bg=SURF2, heading_font_family=FONT_FAMILY,
        )
        md_render.render(body_text, src.get('text', ''))
        body_text.config(state='disabled')

        # If we landed on this preview via a citation click, scroll the
        # body text to the cited passage and flash a highlight on it so
        # the user can see exactly where the claim came from.
        jump = getattr(self, '_pending_citation_jump', None)
        if jump and jump.get('source_id') == source_id:
            self._pending_citation_jump = None
            # Defer until Tk has laid out the widget — without this, the
            # see() call below ends up at the top of the widget because
            # the body_text reports height=0 mid-pack.
            self.after(60, lambda: self._scroll_text_to_passage(
                body_text, jump.get('text', '')))

    def _scroll_text_to_passage(self, widget: tk.Text, passage: str) -> None:
        """Find `passage` inside the read-only body text, scroll it into
        view, and flash a yellow highlight on it for 2s. Falls back to a
        prefix search if the chunk's first 60 chars aren't found verbatim
        (markdown rendering can drop the source's `#` heading prefixes)."""
        if not passage or not widget.winfo_exists():
            return
        # Try a verbatim match first; if that misses (markdown render may
        # have stripped leading "#" / bullet markers), fall back to a
        # progressively shorter prefix of the chunk.
        widget.config(state='normal')
        for needle_len in (80, 50, 30):
            needle = passage.strip()[:needle_len].strip()
            if not needle:
                continue
            try:
                idx = widget.search(needle, '1.0', stopindex='end',
                                    nocase=True)
            except Exception:
                idx = ''
            if idx:
                end_idx = f'{idx} + {len(needle)} chars'
                widget.tag_configure('cite_hl', background='#fff3a8')
                widget.tag_remove('cite_hl', '1.0', 'end')
                widget.tag_add('cite_hl', idx, end_idx)
                widget.see(idx)
                # Fade the highlight after 2.4s so it doesn't linger.
                self.after(2400, lambda: (widget.winfo_exists()
                           and widget.tag_remove('cite_hl', '1.0', 'end')))
                break
        widget.config(state='disabled')

    def _generate_guide_in_background(self, source_id: str,
                                       target_widget: tk.Text) -> None:
        nb_id = self._current_nb_id
        def _worker():
            try:
                guide = engine.generate_source_guide(nb_id, source_id)
            except Exception as e:
                logger.warning(f'guide generation failed: {e}')
                guide = ''
            def _done():
                # Only render if we're still on the same source preview
                if (self._viewing_source_id == source_id
                        and target_widget.winfo_exists()):
                    target_widget.config(state='normal')
                    target_widget.delete('1.0', 'end')
                    if guide:
                        md_render.render(target_widget, guide)
                        target_widget.config(
                            height=min(20,
                                       md_render.estimate_line_count(target_widget) + 1))
                    else:
                        target_widget.insert(
                            '1.0',
                            '(Could not generate a guide — try again later.)')
                    target_widget.config(state='disabled')
            self.after(0, _done)
        threading.Thread(target=_worker, daemon=True,
                         name='notebooks-guide').start()

    def _open_external(self, origin: str) -> None:
        """Open the source's origin (URL or file path) in the system default
        application."""
        try:
            import os as _os, webbrowser
            if origin.startswith('http://') or origin.startswith('https://'):
                webbrowser.open(origin)
            elif _os.path.exists(origin):
                _os.startfile(origin)
        except Exception as e:
            logger.warning(f'open external failed: {e}')

    # Add-source flow
    def _show_add_source_menu(self) -> None:
        """Open the Add Sources modal — NotebookLM-style. Big drop zone
        in the middle, three pill buttons at the bottom."""
        if not self._current_nb_id:
            return
        dlg = _AddSourcesDialog(
            self,
            on_files=self._ingest_in_background,
            on_url=self._add_url_dialog,
            on_text=self._add_text_dialog,
        )
        # Modal is non-blocking — its callbacks fire after the user picks
        # an option; we don't wait_window here so the dialog can close
        # cleanly before the ingestion thread spawns.

    def _add_file_dialog(self) -> None:
        """File-picker entry point — used by the Upload files button inside
        _AddSourcesDialog. Not called directly anywhere else now that the
        modal exists, but kept as a stable callable."""
        if not self._current_nb_id: return
        paths = filedialog.askopenfilenames(
            parent=self, title='Add source files',
            filetypes=[
                ('All supported', '*.pdf *.docx *.pptx *.xlsx *.csv *.txt '
                                  '*.md *.html *.htm *.xml *.json *.epub '
                                  '*.mp3 *.wav *.m4a *.jpg *.jpeg *.png'),
                ('All files', '*.*'),
            ],
        )
        if not paths: return
        self._ingest_in_background(list(paths))

    def _add_url_dialog(self) -> None:
        if not self._current_nb_id: return
        dlg = _UrlPasteDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            # _UrlPasteDialog returns a LIST already (split by whitespace).
            self._ingest_in_background(dlg.result)

    def _add_text_dialog(self) -> None:
        if not self._current_nb_id: return
        dlg = _PasteDialog(
            self, title='Add pasted text',
            hint='Paste any text — notes, an email, a snippet, an article '
                 'body. It will be stored as a source you can chat with.',
            placeholder='',
        )
        self.wait_window(dlg)
        if dlg.result:
            self._ingest_in_background([dlg.result])

    def _ingest_in_background(self, items: list[str]) -> None:
        """Add 1..N sources. When this is the user's FIRST source in a
        new notebook, kick off the NotebookLM-style bootstrap: derive a
        notebook title from the source and post an auto-summary as the
        opening assistant message. Both run on the worker thread off the
        critical UI path."""
        nb_id = self._current_nb_id
        was_empty_notebook = not bool(storage.list_sources(nb_id))
        chat_was_empty = not bool(
            self._current_chat and self._current_chat.get('messages'))

        self._set_status(f'Adding {len(items)} source(s)…')
        def _worker():
            failures = []
            first_full_source = None
            for item in items:
                try:
                    src_meta = engine.add_source(
                        nb_id, item,
                        progress_cb=lambda m: self.after(0, self._set_status, m),
                    )
                    # Capture the first successful add so we can use it for
                    # the notebook-rename + opening-summary if this notebook
                    # was empty before this batch.
                    if first_full_source is None and src_meta is not None:
                        first_full_source = storage.get_source(nb_id, src_meta['id'])
                except Exception as e:
                    logger.exception(f'Add source failed: {item[:60]}')
                    failures.append((item, str(e)))

            # ── First-source bootstrap ────────────────────────────────────
            new_title = None
            opener_msg = None
            if was_empty_notebook and first_full_source is not None:
                # Auto-rename the notebook based on the source
                try:
                    self.after(0, self._set_status, 'Naming doc set…')
                    new_title = engine.suggest_notebook_title(first_full_source)
                except Exception as e:
                    logger.warning(f'suggest_notebook_title failed: {e}')

                # Auto-summary as first assistant message
                if chat_was_empty:
                    try:
                        self.after(0, self._set_status, 'Generating overview…')
                        opener_msg = engine.generate_first_source_summary(
                            nb_id, first_full_source,
                        )
                    except Exception as e:
                        logger.warning(f'generate_first_source_summary failed: {e}')

            def _done():
                self._refresh_sources()
                # Apply auto-rename
                if new_title:
                    nb = storage.get_notebook(nb_id)
                    if nb is not None and nb.get('name') in (
                        'Untitled doc set', '', None):
                        nb['name'] = new_title
                        storage.save_meta(nb_id, nb)
                        if nb_id == self._current_nb_id:
                            self._title_lbl.configure(text=new_title)
                            self._refresh_notebook_picker()
                            for label, nid in getattr(self, '_nb_labels_to_ids', {}).items():
                                if nid == nb_id:
                                    self._nb_picker.set(label)
                                    break
                # Append the auto-summary as the first message
                if opener_msg and nb_id == self._current_nb_id:
                    self._current_chat['messages'].append(opener_msg)
                    if len(self._current_chat['messages']) == 1:
                        self._current_chat['title'] = 'Source overview'
                    storage.save_chat(nb_id, self._current_chat)
                    self._render_chat()
                    self._scroll_chat_to_bottom()
                # Status pill
                msg = (f'Added {len(items) - len(failures)} of {len(items)}'
                       if failures else f'Added {len(items)} source(s)')
                self._set_status(msg)
                self.after(3000, lambda: self._set_status(''))
            self.after(0, _done)
        threading.Thread(target=_worker, daemon=True,
                         name='notebooks-ingest').start()

    def _remove_source(self, source_id: str) -> None:
        if not self._current_nb_id: return
        engine.remove_source(self._current_nb_id, source_id)
        self._refresh_sources()

    # ── Chat menu (⋮) ────────────────────────────────────────────────────────

    def _show_chat_menu(self) -> None:
        menu = tk.Menu(self, tearoff=0,
                       bg=SURFACE, fg=TEXT_P,
                       activebackground=ACCENT, activeforeground='#ffffff',
                       borderwidth=1, relief='solid', font=(FONT_BODY, 10))
        menu.add_command(label='  Customize doc set',
                         command=self._open_customize_dialog)
        menu.add_command(label='  Clear chat', command=self._clear_chat)
        try:
            self.update_idletasks()
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _open_customize_dialog(self) -> None:
        """3-dot menu → Customize doc set. Opens a modal where the user
        can edit the persona (system prompt) that shapes every answer in
        this notebook."""
        if not self._current_nb_id:
            return
        nb = storage.get_notebook(self._current_nb_id)
        if nb is None:
            return
        dlg = _CustomizeDialog(self, persona=nb.get('persona', ''))
        self.wait_window(dlg)
        if dlg.result is not None:
            nb['persona'] = dlg.result
            storage.save_meta(self._current_nb_id, nb)
            self._set_status('Saved customization')
            self.after(2500, lambda: self._set_status(''))

    # ── Chat session ─────────────────────────────────────────────────────────

    def _load_or_create_chat(self, nb_id: str) -> dict:
        chats = storage.list_chats(nb_id)
        if chats:
            return storage.get_chat(nb_id, chats[0]['id']) or self._new_chat(nb_id)
        return self._new_chat(nb_id)

    def _new_chat(self, nb_id: str) -> dict:
        import uuid
        from datetime import datetime
        chat = {
            'id':         str(uuid.uuid4()),
            'title':      'New chat',
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'messages':   [],
        }
        storage.save_chat(nb_id, chat)
        return chat

    def _clear_chat(self) -> None:
        if not self._current_nb_id: return
        from tkinter import messagebox
        if not messagebox.askyesno(
            'Clear chat?', 'Discard the current conversation? '
            'Sources stay intact.', parent=self,
        ): return
        self._current_chat = self._new_chat(self._current_nb_id)
        self._render_chat()

    def _render_chat(self) -> None:
        for w in self._chat_scroll.winfo_children():
            w.destroy()
        msgs = self._current_chat.get('messages', []) if self._current_chat else []
        if not msgs:
            self._render_chat_empty_state()
            return
        for m in msgs:
            self._render_message(m)

    def _render_chat_empty_state(self) -> None:
        """Big welcome screen matching the NotebookLM reference: wave
        emoji, headline, descriptive paragraph, prompt, four
        clickable suggestion chips."""
        wrap = ctk.CTkFrame(self._chat_scroll, fg_color='transparent')
        wrap.pack(fill='x', padx=PAD, pady=(60, 0), anchor='w')

        ctk.CTkLabel(
            wrap, text='👋', font=(FONT_BODY, 36),
        ).pack(anchor='w', pady=(0, 8))

        ctk.CTkLabel(
            wrap, text="Let's start your doc set...",
            font=(FONT_FAMILY, 22, 'bold'), text_color=TEXT_P,
            anchor='w',
        ).pack(anchor='w', pady=(0, 12))

        ctk.CTkLabel(
            wrap,
            text=('Add your sources on the left, then ask anything about '
                  'them here. Answers cite the sources they pull from.'),
            font=(FONT_BODY, 12), text_color=TEXT_S,
            anchor='w', justify='left', wraplength=640,
        ).pack(anchor='w', pady=(0, 16))

    def _render_message(self, msg: dict) -> None:
        role = msg.get('role', 'user')
        content = msg.get('content', '')
        if role == 'user':
            self._render_user_message(content)
        else:
            self._render_assistant_message(msg)

    def _render_user_message(self, content: str) -> None:
        row = ctk.CTkFrame(self._chat_scroll, fg_color='transparent')
        row.pack(fill='x', pady=(PAD_SM, 0))
        bubble = ctk.CTkFrame(row, fg_color=USER_BUBBLE, corner_radius=RADIUS)
        bubble.pack(side='right', anchor='e', padx=(60, 0))
        ctk.CTkLabel(
            bubble, text=content,
            font=(FONT_BODY, 12), text_color=TEXT_P,
            wraplength=560, justify='left',
        ).pack(padx=PAD, pady=PAD_SM)

    def _render_assistant_message(self, msg: dict) -> None:
        """Assistant messages match NotebookLM's flat-text layout:
        no bubble, no border — small avatar on the left, markdown-rendered
        text running across the panel with inline citation chips.
        Below the text: a row of action icons (copy / save-as-note)."""
        row = ctk.CTkFrame(self._chat_scroll, fg_color='transparent')
        row.pack(fill='x', pady=(PAD, 0))

        # Avatar (top-aligned so it sits next to the first line of text)
        ctk.CTkLabel(
            row, text='✨', font=(FONT_BODY, 18),
            text_color=ACCENT, width=32,
        ).pack(side='left', anchor='n', padx=(0, 8))

        # Right-side column holds the text + actions
        text_col = ctk.CTkFrame(row, fg_color='transparent')
        text_col.pack(side='left', fill='both', expand=True)

        # The markdown text widget. Wrap='word' is essential for long
        # assistant answers to fit the panel width.
        tw = tk.Text(
            text_col, wrap='word', bg=SURFACE, fg=TEXT_P,
            font=(FONT_BODY, 12), bd=0, highlightthickness=0,
            padx=0, pady=0, cursor='arrow',
            spacing1=0, spacing2=2, spacing3=2,
        )
        tw.pack(fill='x', expand=True)

        md_render.install_tags(
            tw,
            body_font=(FONT_BODY, 12),
            mono_font=(FONT_MONO, 11),
            fg=TEXT_P, accent=ACCENT, accent_bg=USER_BUBBLE,
            code_bg=SURF2, heading_font_family=FONT_FAMILY,
        )
        citations = msg.get('citations', [])
        md_render.render(
            tw, msg.get('content', ''),
            citations=citations,
            on_citation_click=self._show_citation,
            accent=ACCENT, accent_bg=USER_BUBBLE,
        )

        # Auto-size the text widget to its content. We give wrap a chance
        # to compute by flushing layout first, then asking for the
        # display-line count.
        text_col.update_idletasks()
        lines = md_render.estimate_line_count(tw)
        # Word-wrap can grow the visual line count beyond the source-line
        # count; add a small fudge for safety, and cap to a sane maximum
        # to avoid one runaway answer eating the whole panel.
        tw.config(height=min(40, lines + 2))

        # Action row: copy + save-to-Hotkeys-Notes (matches NotebookLM's
        # pin/copy icons under each answer).
        action_row = ctk.CTkFrame(text_col, fg_color='transparent')
        action_row.pack(fill='x', pady=(4, 0))
        ctk.CTkButton(
            action_row, text='📋', width=28, height=28,
            fg_color='transparent', hover_color=SURF2, text_color=TEXT_T,
            font=(FONT_BODY, 12), corner_radius=14,
            command=lambda c=msg.get('content', ''): self._copy_to_clipboard(c),
        ).pack(side='left')
        ctk.CTkButton(
            action_row, text='📌', width=28, height=28,
            fg_color='transparent', hover_color=SURF2, text_color=TEXT_T,
            font=(FONT_BODY, 12), corner_radius=14,
            command=lambda c=msg.get('content', ''): self._save_response_as_note(c),
        ).pack(side='left')

        # Follow-up suggestion chips (rendered if the message has them).
        followups = msg.get('followups', [])
        if followups:
            fu_wrap = ctk.CTkFrame(text_col, fg_color='transparent')
            fu_wrap.pack(fill='x', pady=(8, 0))
            for q in followups:
                ctk.CTkButton(
                    fu_wrap, text=q, height=30,
                    fg_color=SURFACE, hover_color=SURF2, text_color=TEXT_P,
                    font=(FONT_BODY, 11), corner_radius=RADIUS_SM,
                    border_width=1, border_color=BORDER,
                    command=lambda qq=q: self._send_question(qq),
                ).pack(anchor='w', pady=(0, 4))

    def _build_streaming_bubble(self) -> tk.Text:
        """Render a temporary assistant bubble with an empty Text widget that
        gets appended to as tokens arrive. Returns the Text widget. The
        bubble is replaced by the full _render_assistant_message() when
        the stream finishes (which re-renders citations + actions +
        followups properly)."""
        row = ctk.CTkFrame(self._chat_scroll, fg_color='transparent')
        row.pack(fill='x', pady=(PAD, 0))
        self._stream_row = row
        ctk.CTkLabel(
            row, text='✨', font=(FONT_BODY, 18),
            text_color=ACCENT, width=32,
        ).pack(side='left', anchor='n', padx=(0, 8))
        text_col = ctk.CTkFrame(row, fg_color='transparent')
        text_col.pack(side='left', fill='both', expand=True)
        tw = tk.Text(
            text_col, wrap='word', bg=SURFACE, fg=TEXT_P,
            font=(FONT_BODY, 12), bd=0, highlightthickness=0,
            padx=0, pady=0, cursor='arrow',
            spacing1=0, spacing2=2, spacing3=2, height=1,
        )
        tw.pack(fill='x', expand=True)
        return tw

    def _append_stream_chunk(self, chunk: str) -> None:
        w = getattr(self, '_stream_text_widget', None)
        if w is None or not chunk:
            return
        try:
            w.configure(state='normal')
            w.insert('end', chunk)
            # Resize as content grows; cap at 40 lines.
            try:
                last = w.index('end-1c')
                lines = int(last.split('.')[0])
                w.config(height=min(40, max(1, lines + 1)))
            except Exception:
                pass
            self._scroll_chat_to_bottom()
        except Exception as e:
            logger.debug(f'stream chunk failed: {e}')

    def _clear_stream_bubble(self) -> None:
        try:
            row = getattr(self, '_stream_row', None)
            if row is not None:
                row.destroy()
        except Exception:
            pass
        self._stream_row = None
        self._stream_text_widget = None

    # ── Per-message actions ─────────────────────────────────────────────────

    def _copy_to_clipboard(self, text: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._set_status('Copied to clipboard')
            self.after(2000, lambda: self._set_status(''))
        except Exception:
            pass

    def _save_response_as_note(self, text: str) -> None:
        """Save the answer to Hotkeys' Quick Notes so it surfaces in Shift+F7
        immediately. Falls back to clipboard if Hotkeys' storage is missing."""
        try:
            import sys
            from pathlib import Path
            hk_root = Path(__file__).resolve().parent.parent
            if str(hk_root) not in sys.path:
                sys.path.insert(0, str(hk_root))
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location('hk_storage', str(hk_root / 'storage.py'))
            hk_storage = _ilu.module_from_spec(spec); spec.loader.exec_module(hk_storage)
            import uuid as _uuid
            from datetime import datetime as _dt
            notes = hk_storage.load_notes()
            notes.append({
                'id': str(_uuid.uuid4()),
                'text': text,
                'items': [{'text': '', 'checked': False}],
                'voice': '', 'color': None, 'pinned': False,
                'created_at': _dt.now().isoformat(timespec='seconds'),
            })
            hk_storage.save_notes(notes)
            self._set_status('Saved to Hotkeys Notes')
            self.after(2500, lambda: self._set_status(''))
        except Exception as e:
            logger.warning(f'Save-to-Hotkeys-Notes failed: {e}')
            self._copy_to_clipboard(text)

    def _send_question(self, question: str) -> None:
        """Programmatically send a question — used by suggestion chips."""
        self._clear_placeholder()
        self._input.delete('1.0', 'end')
        self._input.insert('1.0', question)
        self._input.configure(text_color=TEXT_P)
        self._send()

    def _show_citation(self, citation: dict) -> None:
        """Citation chip click → open the cited source in the left panel
        and scroll to the passage. Matches NotebookLM's in-context jump
        rather than popping a separate modal."""
        source_id = citation.get('source_id')
        if not source_id:
            return
        # Stash the passage so _render_source_preview can scroll to it
        # once the body text widget is laid out.
        self._pending_citation_jump = {
            'source_id': source_id,
            'text':      citation.get('text', ''),
        }
        self._open_source_preview(source_id)

    # ── Send flow ────────────────────────────────────────────────────────────

    def _send(self) -> None:
        if not self._current_nb_id or self._is_thinking:
            return
        question = self._input.get('1.0', 'end').strip()
        if not question or question == _PLACEHOLDER:
            return
        self._input.delete('1.0', 'end')
        # Re-apply the placeholder
        self._input.insert('1.0', _PLACEHOLDER)
        self._input.configure(text_color=TEXT_T)

        self._current_chat['messages'].append(
            {'role': 'user', 'content': question})
        if len(self._current_chat['messages']) == 1:
            self._current_chat['title'] = question[:60]
        storage.save_chat(self._current_nb_id, self._current_chat)
        self._render_chat()
        self._scroll_chat_to_bottom()

        self._is_thinking = True
        self._send_btn.configure(state='disabled', text='…')
        self._set_status('Thinking…')

        # Build the streaming bubble immediately so the user sees the
        # answer materialise as tokens arrive (NotebookLM-style UX).
        self._stream_text_widget = self._build_streaming_bubble()
        self._scroll_chat_to_bottom()

        nb_id = self._current_nb_id
        history = self._current_chat['messages'][:-1]
        selected_ids = self._get_selected_source_ids()

        def _worker():
            try:
                result = engine.ask(
                    nb_id, question, chat_history=history,
                    selected_source_ids=selected_ids,
                    progress_cb=lambda m: self.after(0, self._set_status, m),
                    stream_cb=lambda d: self.after(
                        0, self._append_stream_chunk, d),
                )
            except Exception as e:
                logger.exception('ask failed')
                result = {'answer': f'Error: {e}',
                          'citations': [], 'used_chunk_ids': []}
            # While we're already on a worker thread, also generate 3
            # follow-up suggestions so the user can keep exploring with
            # one click. Failure here is non-fatal; an empty list just
            # means no chips render under the answer.
            followups: list[str] = []
            try:
                followups = engine.suggest_followups_after(
                    result.get('answer', ''),
                    context_hint=question,
                )
            except Exception:
                pass

            def _done():
                # Drop the temporary streaming bubble; the full re-render
                # below produces the proper bubble with post-processed
                # text, citation chips, action row, and follow-ups.
                self._clear_stream_bubble()
                self._current_chat['messages'].append({
                    'role':           'assistant',
                    'content':        result['answer'],
                    'citations':      result['citations'],
                    'used_chunk_ids': result['used_chunk_ids'],
                    'followups':      followups,
                })
                storage.save_chat(nb_id, self._current_chat)
                self._render_chat()
                self._scroll_chat_to_bottom()
                self._is_thinking = False
                self._send_btn.configure(state='normal', text='→')
                self._set_status('')
            self.after(0, _done)
        threading.Thread(target=_worker, daemon=True,
                         name='notebooks-ask').start()

    def _scroll_chat_to_bottom(self) -> None:
        try:
            self.update_idletasks()
            self._chat_scroll._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    # ── Input placeholder helpers ────────────────────────────────────────────

    def _on_input_focus_in(self, _event=None) -> None:
        text = self._input.get('1.0', 'end').strip()
        if text == _PLACEHOLDER:
            self._clear_placeholder()

    def _on_input_focus_out(self, _event=None) -> None:
        text = self._input.get('1.0', 'end').strip()
        if not text:
            self._input.delete('1.0', 'end')
            self._input.insert('1.0', _PLACEHOLDER)
            self._input.configure(text_color=TEXT_T)

    def _clear_placeholder(self) -> None:
        self._input.delete('1.0', 'end')
        self._input.configure(text_color=TEXT_P)

    # ── Status bar ───────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        try: self._status_bar.configure(text=msg)
        except Exception: pass

    # ── Window lifecycle ─────────────────────────────────────────────────────

    def _center_on_screen(self, w: int, h: int) -> None:
        try:
            # CTkToplevel reports stale screen dimensions until idletasks
            # has flushed — without this, winfo_screenwidth() can return
            # the FRAME's width (~200 px) instead of the actual monitor
            # width, parking the window in the top-left corner.
            self.update_idletasks()
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            # Defend against the buggy case: if sw is implausibly small,
            # fall back to a reasonable 1920x1080 assumption.
            if sw < 800 or sh < 600:
                sw, sh = 1920, 1080
            x = max(0, (sw - w) // 2)
            y = max(20, (sh - h) // 2 - 20)
            self.geometry(f'{w}x{h}+{x}+{y}')
        except Exception:
            self.geometry(f'{w}x{h}')

    def _handle_close(self) -> None:
        if self._on_close:
            try: self._on_close()
            except Exception: pass
        self.destroy()
        if self._owns_root:
            try: self._root_ref.quit()
            except Exception: pass


_PLACEHOLDER = 'Ask anything about your sources'


def _center_dialog_over_parent(dlg, parent, w: int, h: int) -> None:
    """Center `dlg` on its parent window, falling back to screen-center
    if the parent's geometry isn't usable. Call AFTER setting geometry/
    widgets but BEFORE deiconify/grab so the window is positioned by the
    time it becomes visible (otherwise CTkToplevel paints once at 0,0
    and then jumps, which the user sees as a flash in the corner)."""
    try:
        dlg.update_idletasks()
        # Parent geometry — use the main app window position + size.
        try:
            px = int(parent.winfo_rootx())
            py = int(parent.winfo_rooty())
            pw = int(parent.winfo_width())
            ph = int(parent.winfo_height())
        except Exception:
            px = py = 0; pw = ph = 0
        if pw < 200 or ph < 200:
            # Parent not realised yet; center on the screen instead.
            sw = dlg.winfo_screenwidth()
            sh = dlg.winfo_screenheight()
            if sw < 800 or sh < 600:
                sw, sh = 1920, 1080
            x = max(0, (sw - w) // 2)
            y = max(20, (sh - h) // 2 - 20)
        else:
            x = max(0, px + (pw - w) // 2)
            y = max(20, py + (ph - h) // 2 - 20)
        dlg.geometry(f'{w}x{h}+{x}+{y}')
    except Exception:
        dlg.geometry(f'{w}x{h}')


# ── Paste-text dialog ────────────────────────────────────────────────────────

class _AddSourcesDialog(ctk.CTkToplevel):
    """The big 'Add sources' modal — wraps file picker / URL paste / text
    paste behind one cohesive entry point, with a drop zone in the middle
    that accepts drag-and-drop file drops (when tkinterdnd2 is available).

    Layout matches the NotebookLM reference: title, large drop area,
    three pill buttons at the bottom (Upload files, Websites, Copied text).
    """

    def __init__(self, parent, *, on_files, on_url, on_text):
        super().__init__(parent)
        self.title('Add sources')
        self.configure(fg_color=SURFACE)
        _center_dialog_over_parent(self, parent, 680, 440)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._on_files = on_files
        self._on_url   = on_url
        self._on_text  = on_text
        self._parent   = parent

        # Header: title on the left, close X on the right
        head = ctk.CTkFrame(self, fg_color='transparent')
        head.pack(fill='x', padx=PAD * 2, pady=(PAD * 2, PAD_SM))
        ctk.CTkLabel(
            head, text='Add sources',
            font=(FONT_FAMILY, 22, 'bold'), text_color=TEXT_P,
        ).pack(side='left')
        ctk.CTkButton(
            head, text='×', width=32, height=32,
            fg_color='transparent', hover_color=SURF2, text_color=TEXT_T,
            font=(FONT_BODY, 20), corner_radius=16,
            command=self.destroy,
        ).pack(side='right')

        # Drop zone — big rounded card with dashed-style border feel
        drop = ctk.CTkFrame(
            self, fg_color=SURF2, corner_radius=RADIUS,
            border_width=1, border_color=BORDER,
        )
        drop.pack(fill='both', expand=True, padx=PAD * 2, pady=(0, PAD * 2))

        # Vertical centering of the drop-zone contents
        inner = ctk.CTkFrame(drop, fg_color='transparent')
        inner.place(relx=0.5, rely=0.5, anchor='center')

        ctk.CTkLabel(
            inner, text='or drop your files',
            font=(FONT_FAMILY, 18, 'bold'), text_color=TEXT_P,
        ).pack(pady=(0, 4))
        # Show the formats users actually have on disk most often, plus
        # a small clickable "see all" toggle that expands the full list.
        # NotebookLM's "and more" is opaque — being explicit makes it
        # obvious whether a particular file will work BEFORE the user
        # tries to drag it in.
        ctk.CTkLabel(
            inner,
            text='PDF · DOCX · PPTX · XLSX · TXT · MD · HTML · CSV · '
                 'JSON · EPUB · MP3 · WAV · M4A · PNG · JPG',
            font=(FONT_BODY, 11), text_color=TEXT_T,
            wraplength=500, justify='center',
        ).pack(pady=(0, 6))
        ctk.CTkLabel(
            inner,
            text='URLs (Wikipedia, blogs, docs, YouTube transcripts) '
                 'via Websites · Plain text via Copied text',
            font=(FONT_BODY, 10), text_color=TEXT_T,
            wraplength=500, justify='center',
        ).pack(pady=(0, 24))

        btn_row = ctk.CTkFrame(inner, fg_color='transparent')
        btn_row.pack()
        ctk.CTkButton(
            btn_row, text='↑  Upload files', height=40,
            fg_color=SURFACE, hover_color=SURF2, text_color=TEXT_P,
            font=(FONT_BODY, 12), corner_radius=RADIUS_SM,
            border_width=1, border_color=BORDER,
            command=self._do_upload,
        ).pack(side='left', padx=(0, 8))
        ctk.CTkButton(
            btn_row, text='🔗  Websites', height=40,
            fg_color=SURFACE, hover_color=SURF2, text_color=TEXT_P,
            font=(FONT_BODY, 12), corner_radius=RADIUS_SM,
            border_width=1, border_color=BORDER,
            command=self._do_url,
        ).pack(side='left', padx=(0, 8))
        ctk.CTkButton(
            btn_row, text='📋  Copied text', height=40,
            fg_color=SURFACE, hover_color=SURF2, text_color=TEXT_P,
            font=(FONT_BODY, 12), corner_radius=RADIUS_SM,
            border_width=1, border_color=BORDER,
            command=self._do_text,
        ).pack(side='left')

        # Register drop zone as a DnD target. tkinterdnd2 needs the root
        # to be a TkinterDnD.Tk (or imported with patch). CTk's root works
        # if tkinterdnd2 was imported before any tk windows were realized.
        try:
            from tkinterdnd2 import DND_FILES
            drop.drop_target_register(DND_FILES)
            drop.dnd_bind('<<Drop>>', self._handle_drop)
            inner.drop_target_register(DND_FILES)
            inner.dnd_bind('<<Drop>>', self._handle_drop)
        except Exception as e:
            logger.debug(f'DnD unavailable in Add Sources modal: {e}')

        self.bind('<Escape>', lambda e: self.destroy())

    # ── Button handlers ──────────────────────────────────────────────────────

    def _do_upload(self) -> None:
        # Filter list matches the formats we advertise in the modal subtitle.
        # Each group is a separate filetype entry so power users can narrow
        # the dropdown when they have many files in a folder.
        paths = filedialog.askopenfilenames(
            parent=self, title='Add source files',
            filetypes=[
                ('All supported', '*.pdf *.docx *.pptx *.xlsx *.csv *.tsv '
                                  '*.txt *.md *.html *.htm *.xml *.json '
                                  '*.yaml *.yml *.epub *.rtf '
                                  '*.mp3 *.wav *.m4a *.flac *.ogg '
                                  '*.jpg *.jpeg *.png *.bmp *.gif *.tiff '
                                  '*.webp *.zip'),
                ('Documents',     '*.pdf *.docx *.doc *.rtf *.txt *.md *.epub'),
                ('Spreadsheets',  '*.xlsx *.xls *.csv *.tsv'),
                ('Slides',        '*.pptx *.ppt'),
                ('Web pages',     '*.html *.htm *.xml *.json *.yaml *.yml'),
                ('Audio',         '*.mp3 *.wav *.m4a *.flac *.ogg'),
                ('Images',        '*.jpg *.jpeg *.png *.bmp *.gif *.tiff *.webp'),
                ('Archives',      '*.zip'),
                ('All files',     '*.*'),
            ],
        )
        if not paths:
            return
        self.destroy()
        self._on_files(list(paths))

    def _do_url(self) -> None:
        self.destroy()
        # Defer so this dialog has time to fully unmap before the next
        # one tries to grab the pointer.
        self._parent.after(50, self._on_url)

    def _do_text(self) -> None:
        self.destroy()
        self._parent.after(50, self._on_text)

    # ── Drag-and-drop ────────────────────────────────────────────────────────

    def _handle_drop(self, event) -> None:
        """Files dragged onto the drop zone. event.data is a space-separated
        string of paths, with paths-containing-spaces wrapped in braces:
            '{C:/Users/me/My Doc.pdf} C:/notes.txt {D:/another file.docx}'
        """
        raw = event.data or ''
        paths = self._parse_dnd_paths(raw)
        if not paths:
            return
        self.destroy()
        self._on_files(paths)

    @staticmethod
    def _parse_dnd_paths(raw: str) -> list[str]:
        out: list[str] = []
        i = 0
        n = len(raw)
        while i < n:
            if raw[i] == '{':
                # Brace-quoted path, scan to closing brace
                end = raw.find('}', i + 1)
                if end == -1:
                    break
                out.append(raw[i + 1:end])
                i = end + 1
            elif raw[i].isspace():
                i += 1
            else:
                # Unquoted path, scan to next whitespace
                end = i
                while end < n and not raw[end].isspace():
                    end += 1
                out.append(raw[i:end])
                i = end
        return [p for p in out if p]


class _PasteDialog(ctk.CTkToplevel):
    """Modal for pasting a URL OR raw text — same widget, two presets.
    Returns the entered string in self.result, or None on cancel.

    `title`       — window title + bold header text in the dialog body
    `hint`        — sub-line shown under the title
    `placeholder` — greyed-out placeholder inside the textbox
    """

    def __init__(self, parent, *, title: str = 'Paste source',
                 hint: str = '', placeholder: str = ''):
        super().__init__(parent)
        self.title(title)
        self.configure(fg_color=SURFACE)
        _center_dialog_over_parent(self, parent, 640, 420)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self._placeholder = placeholder

        ctk.CTkLabel(
            self, text=title,
            font=(FONT_FAMILY, 18, 'bold'), text_color=TEXT_P, anchor='w',
        ).pack(fill='x', padx=PAD * 2, pady=(PAD * 2, 4))
        if hint:
            ctk.CTkLabel(
                self, text=hint,
                font=(FONT_BODY, 11), text_color=TEXT_T,
                anchor='w', justify='left', wraplength=560,
            ).pack(fill='x', padx=PAD * 2, pady=(0, PAD))

        self._tb = ctk.CTkTextbox(
            self, fg_color=SURF2, text_color=TEXT_P,
            font=(FONT_BODY, 12), wrap='word', corner_radius=RADIUS,
            border_width=1, border_color=BORDER,
        )
        self._tb.pack(fill='both', expand=True, padx=PAD * 2, pady=(0, PAD_SM))
        if placeholder:
            self._tb.insert('1.0', placeholder)
            self._tb.configure(text_color=TEXT_T)
            self._tb.bind('<FocusIn>',  self._on_focus_in)
            self._tb.bind('<FocusOut>', self._on_focus_out)
        self._tb.focus_set()

        btn_row = ctk.CTkFrame(self, fg_color='transparent')
        btn_row.pack(fill='x', padx=PAD * 2, pady=(0, PAD * 2))
        ctk.CTkButton(
            btn_row, text='Cancel', width=100, height=36,
            fg_color=SURFACE, hover_color=SURF2, text_color=TEXT_P,
            border_width=1, border_color=BORDER,
            font=(FONT_BODY, 12), corner_radius=RADIUS_SM,
            command=self._cancel,
        ).pack(side='right')
        ctk.CTkButton(
            btn_row, text='Add', width=100, height=36,
            fg_color=DARK_PILL, hover_color=DARK_PILLH, text_color='#ffffff',
            font=(FONT_FAMILY, 12, 'bold'), corner_radius=RADIUS_SM,
            command=self._ok,
        ).pack(side='right', padx=(0, PAD_SM))
        self.bind('<Escape>', lambda e: self._cancel())

    def _on_focus_in(self, _e=None):
        if self._tb.get('1.0', 'end').strip() == self._placeholder:
            self._tb.delete('1.0', 'end')
            self._tb.configure(text_color=TEXT_P)

    def _on_focus_out(self, _e=None):
        if not self._tb.get('1.0', 'end').strip():
            self._tb.insert('1.0', self._placeholder)
            self._tb.configure(text_color=TEXT_T)

    def _ok(self):
        text = self._tb.get('1.0', 'end').strip()
        if text == self._placeholder:
            text = ''
        self.result = text or None
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class _UrlPasteDialog(ctk.CTkToplevel):
    """The Websites flow — multi-URL paste with the same constraint
    bullets NotebookLM shows. Returns self.result as a list of URLs
    (split by whitespace/newline) or None on cancel."""

    _PLACEHOLDER = 'Paste any links'

    def __init__(self, parent):
        super().__init__(parent)
        self.title('Website and YouTube URLs')
        self.configure(fg_color=SURFACE)
        _center_dialog_over_parent(self, parent, 660, 540)
        self.transient(parent)
        self.grab_set()
        self.result: list[str] | None = None

        # Header with back arrow + title + close
        head = ctk.CTkFrame(self, fg_color='transparent')
        head.pack(fill='x', padx=PAD * 2, pady=(PAD * 2, 4))
        ctk.CTkButton(
            head, text='←', width=28, height=28,
            fg_color='transparent', hover_color=SURF2, text_color=TEXT_T,
            font=(FONT_BODY, 14), corner_radius=14,
            command=self._cancel,
        ).pack(side='left')
        ctk.CTkLabel(
            head, text='Website and YouTube URLs',
            font=(FONT_FAMILY, 18, 'bold'), text_color=TEXT_P,
        ).pack(side='left', padx=(8, 0))
        ctk.CTkButton(
            head, text='×', width=32, height=32,
            fg_color='transparent', hover_color=SURF2, text_color=TEXT_T,
            font=(FONT_BODY, 20), corner_radius=16,
            command=self._cancel,
        ).pack(side='right')

        ctk.CTkLabel(
            self,
            text=('Paste in Website and YouTube URLs below to upload as a '
                  'source in your doc set.'),
            font=(FONT_BODY, 11), text_color=TEXT_T,
            anchor='w', justify='left', wraplength=560,
        ).pack(fill='x', padx=PAD * 2, pady=(0, PAD))

        # Big multi-line textbox
        self._tb = ctk.CTkTextbox(
            self, fg_color=SURF2, text_color=TEXT_P,
            font=(FONT_BODY, 12), wrap='word', corner_radius=RADIUS,
            border_width=1, border_color=BORDER, height=160,
        )
        self._tb.pack(fill='x', padx=PAD * 2, pady=(0, PAD))
        self._tb.insert('1.0', self._PLACEHOLDER)
        self._tb.configure(text_color=TEXT_T)
        self._tb.bind('<FocusIn>',  self._on_focus_in)
        self._tb.bind('<FocusOut>', self._on_focus_out)
        self._tb.focus_set()

        # Constraint bullets — match NotebookLM's exact copy where they apply
        bullets = ctk.CTkFrame(self, fg_color='transparent')
        bullets.pack(fill='x', padx=PAD * 2, pady=(0, PAD))
        for line in [
            '• To add multiple URLs, separate with a space or new line.',
            '• Only the visible text on the website will be imported at this time.',
            '• Paid articles are not supported.',
            '• Only the text transcript in YouTube will be imported at this time.',
            '• Only public YouTube videos are supported.',
        ]:
            ctk.CTkLabel(
                bullets, text=line,
                font=(FONT_BODY, 10), text_color=TEXT_T,
                anchor='w', justify='left',
            ).pack(fill='x', pady=1)

        # Buttons row
        btn_row = ctk.CTkFrame(self, fg_color='transparent')
        btn_row.pack(fill='x', padx=PAD * 2, pady=(0, PAD * 2))
        self._insert_btn = ctk.CTkButton(
            btn_row, text='Insert', width=110, height=36,
            fg_color=DARK_PILL, hover_color=DARK_PILLH, text_color='#ffffff',
            font=(FONT_FAMILY, 12, 'bold'), corner_radius=RADIUS_SM,
            command=self._ok,
        )
        self._insert_btn.pack(side='right')

        self.bind('<Escape>', lambda e: self._cancel())

    def _on_focus_in(self, _e=None):
        if self._tb.get('1.0', 'end').strip() == self._PLACEHOLDER:
            self._tb.delete('1.0', 'end')
            self._tb.configure(text_color=TEXT_P)

    def _on_focus_out(self, _e=None):
        if not self._tb.get('1.0', 'end').strip():
            self._tb.insert('1.0', self._PLACEHOLDER)
            self._tb.configure(text_color=TEXT_T)

    def _ok(self):
        raw = self._tb.get('1.0', 'end').strip()
        if not raw or raw == self._PLACEHOLDER:
            self.result = None
            self.destroy()
            return
        # Split on any whitespace (spaces, newlines, tabs) and keep only
        # URL-shaped tokens.
        urls = [
            tok for tok in raw.split()
            if tok.startswith('http://') or tok.startswith('https://')
        ]
        self.result = urls if urls else None
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class _CustomizeDialog(ctk.CTkToplevel):
    """3-dot menu → Customize doc set. Edits the system-prompt persona
    that shapes every answer. Returns the new persona text in self.result
    (empty string is valid = wipe persona), or None on cancel."""

    def __init__(self, parent, *, persona: str = ''):
        super().__init__(parent)
        self.title('Customize doc set')
        self.configure(fg_color=SURFACE)
        _center_dialog_over_parent(self, parent, 600, 460)
        self.transient(parent)
        self.grab_set()
        self.result: str | None = None

        ctk.CTkLabel(
            self, text='Customize doc set',
            font=(FONT_FAMILY, 18, 'bold'), text_color=TEXT_P, anchor='w',
        ).pack(fill='x', padx=PAD * 2, pady=(PAD * 2, 4))
        ctk.CTkLabel(
            self,
            text=('Tell the assistant how to behave in this doc set. '
                  'Mention the audience, the tone, the format you want '
                  'answers in. This applies to every answer in this doc set.'),
            font=(FONT_BODY, 11), text_color=TEXT_T,
            anchor='w', justify='left', wraplength=520,
        ).pack(fill='x', padx=PAD * 2, pady=(0, PAD))

        self._tb = ctk.CTkTextbox(
            self, fg_color=SURF2, text_color=TEXT_P,
            font=(FONT_BODY, 12), wrap='word', corner_radius=RADIUS,
            border_width=1, border_color=BORDER,
        )
        self._tb.pack(fill='both', expand=True, padx=PAD * 2, pady=(0, PAD))
        if persona:
            self._tb.insert('1.0', persona)
        self._tb.focus_set()

        btn_row = ctk.CTkFrame(self, fg_color='transparent')
        btn_row.pack(fill='x', padx=PAD * 2, pady=(0, PAD * 2))
        ctk.CTkButton(
            btn_row, text='Cancel', width=100, height=36,
            fg_color=SURFACE, hover_color=SURF2, text_color=TEXT_P,
            border_width=1, border_color=BORDER,
            font=(FONT_BODY, 12), corner_radius=RADIUS_SM,
            command=self._cancel,
        ).pack(side='right')
        ctk.CTkButton(
            btn_row, text='Save', width=100, height=36,
            fg_color=DARK_PILL, hover_color=DARK_PILLH, text_color='#ffffff',
            font=(FONT_FAMILY, 12, 'bold'), corner_radius=RADIUS_SM,
            command=self._ok,
        ).pack(side='right', padx=(0, PAD_SM))

        self.bind('<Escape>', lambda e: self._cancel())

    def _ok(self):
        self.result = self._tb.get('1.0', 'end').strip()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()
