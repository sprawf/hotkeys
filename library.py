"""Library, the central hub window. Houses every tab the app exposes:
prompts, macros, screen recorder, GIF recorder, explain, web bookmarks,
chains, notes, whiteboard, transcribe, audio editor, and reserved Shift+F11..F12 slots.
The bare name "Library" is intentional now that this is more than a
prompt manager."""
import logging
import math
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from typing import Callable

_log = logging.getLogger('library')

import customtkinter as ctk
import keyboard

import spellcheck
from dialogs import alert, confirm, center_over_parent, Tooltip, PopupMenu
from macros.library import MacroLibrary
from storage import appdata_dir
from theme import (
    BG, SURFACE, SURF2, SURF3, BORDER, BORDER2,
    ACCENT, ACCENTL, TEXT_P, TEXT_S, TEXT_D,
    OK, WARN, ERR,
    CARD_COLORS, CARD_TEXT, CARD_TEXT_S,
    FONT_FAMILY, FONT_SM_BOLD,
    PAD, PAD_SM, PAD_LG, RADIUS, RADIUS_SM,
    _darken,
)

CARD_W   = 300
CARD_H   = 175   # fixed card height, all cards uniform regardless of text length
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
                 reserved_hotkeys:  set | None = None,
                 vision_extractor:  Callable | None = None) -> None:
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
        self._vision_extractor  = vision_extractor
        self._ocr_pending       = False
        self._ocr_staged_img    = None

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
            font=(FONT_FAMILY, 12), text_color=TEXT_S,
            cursor='hand2',
        )
        self._hk_badge.pack(side='left', ipady=4, ipadx=8)
        self._hk_badge.bind('<Button-1>', lambda e: self._assign_hk())
        self._refresh_hk()

        _btn(hk_row, '⌨  Assign…', self._assign_hk, width=110).pack(side='left', padx=(8, 4))
        _btn(hk_row, '✕',          self._clear_hk,  width=36).pack(side='left')

        # ── Prompt textbox ────────────────────────────────────────────────────
        ctk.CTkLabel(body, text='Prompt', font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w')
        self._text = ctk.CTkTextbox(
            body, width=420, height=160, wrap='word',
            fg_color=SURFACE, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 13), corner_radius=RADIUS_SM,
            undo=True,
        )
        self._text.insert('1.0', data.get('prompt', ''))
        self._text.pack(fill='x', pady=(4, 0))
        spellcheck.attach(self._text)

        # ── OCR row (📷 Paste Image button + status + char count) ───────────────
        ocr_row = ctk.CTkFrame(body, fg_color='transparent')
        ocr_row.pack(fill='x', pady=(6, 0))

        self._ocr_btn = _btn(
            ocr_row, '📷  Paste Image', self._ocr_start,
            width=140, fg_color=SURF2, hover=SURF3,
        )
        self._ocr_btn.pack(side='left')
        Tooltip(self._ocr_btn,
                'Copy an image to clipboard, then click to extract its text.\n'
                'You can also press Ctrl+V in the text box above.')

        # Thumbnail, always packed right after the button, just empty when idle
        self._ocr_thumb_lbl = ctk.CTkLabel(ocr_row, text='', fg_color='transparent')
        self._ocr_thumb_lbl.pack(side='left', padx=(6, 0))
        self._ocr_thumb_ref = None   # keep CTkImage alive

        # Char count, pinned to the right edge
        self._char_lbl = ctk.CTkLabel(
            ocr_row, text='', font=(FONT_FAMILY, 11), text_color=TEXT_S,
        )
        self._char_lbl.pack(side='right')
        self._update_char_count()

        # Inline status message, centre of the row, changes during OCR
        self._ocr_status_lbl = ctk.CTkLabel(
            ocr_row, text='', font=(FONT_FAMILY, 11), text_color=TEXT_S,
        )
        self._ocr_status_lbl.pack(side='left', padx=(10, 0))

        # Update char count whenever text changes
        try:
            inner = self._text._textbox
        except AttributeError:
            inner = getattr(self._text, 'textbox', self._text)
        inner.bind('<KeyRelease>', lambda e: self._update_char_count(), add='+')

        # Ctrl+V: stage image or fall through to text paste
        inner.bind('<Control-v>', self._on_ctrl_v, add='+')
        # Enter: confirm staged image → run OCR, else normal newline
        inner.bind('<Return>', self._on_return_key, add='+')
        self.bind('<Return>', self._on_return_key, add='+')
        # Esc: cancel staged image first, then allow normal close
        self.bind('<Escape>', self._on_escape)
        # Right-click: Cut / Copy / smart Paste
        inner.bind('<Button-3>', self._show_text_context_menu, add='+')

        # Description is auto-generated from prompt text on save, no manual field needed

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
            self._hk_badge.configure(text='— None assigned', text_color=TEXT_S, fg_color=SURFACE)

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
            alert(self, 'Hotkey already in use',
                  f'"{new_hk.upper()}" is already assigned to a built-in '
                  'Hotkeys action. Pick another combo, or reassign the '
                  'built-in one in Settings → Hotkeys.')
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
        # Auto-generate description from the first line of prompt text
        first_line = prompt.strip().split('\n')[0]
        auto_desc  = first_line[:80] + ('…' if len(first_line) > 80 else '')
        self.result = {'title': title, 'prompt': prompt, 'color': self._color_var.get(),
                       'hotkey': self._hotkey,   # '' = cleared; str = assigned
                       'description': auto_desc}
        self.destroy()

    # ── Char count ────────────────────────────────────────────────────────────

    def _update_char_count(self) -> None:
        try:
            n = len(self._text.get('1.0', 'end-1c'))
            self._char_lbl.configure(text=f'{n} chars')
        except Exception:
            pass

    def _show_text_context_menu(self, event) -> None:
        """Right-click context menu on the prompt text box."""
        w       = event.widget
        has_sel = bool(w.tag_ranges('sel'))

        def _smart_paste():
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
            if err:
                self._ocr_set_status(f'⚠  {err}', WARN)
                return
            if img is not None:
                self._ocr_stage(img)
            else:
                w.event_generate('<<Paste>>')

        pm = PopupMenu(self)

        # ── Spell suggestions at top when cursor is on a misspelled word ──
        spell = spellcheck.get_info(w, event.x, event.y)
        if spell:
            word, ws, we, suggestions = spell
            for s in suggestions:
                pm.add(s, lambda r=s, a=ws, b=we: spellcheck.apply_suggestion(w, a, b, r))
            pm.separator()
            pm.add('Ignore all',        lambda wrd=word: spellcheck.ignore_word(w, wrd))
            pm.add('Add to dictionary', lambda wrd=word: spellcheck.add_word(w, wrd))
            pm.separator()

        (pm
            .add('Cut',   lambda: w.event_generate('<<Cut>>'),  enabled=has_sel)
            .add('Copy',  lambda: w.event_generate('<<Copy>>'), enabled=has_sel)
            .add('Paste', _smart_paste)
            .show(event.x_root, event.y_root)
        )

    _OCR_REFUSAL_PATTERNS = (
        'no text', 'no readable', 'no visible', 'cannot extract',
        'unable to extract', 'does not contain', 'no text found',
        'there is no text', 'i cannot', "i can't", 'no legible',
        'no written', 'no words', 'this image does not', 'image contains no',
    )

    @classmethod
    def _ocr_quality_issue(cls, text: str) -> str | None:
        stripped = text.strip()
        lower    = stripped.lower()
        n        = len(stripped)
        if n == 0:
            return 'No text found'
        if n < 200:
            for pat in cls._OCR_REFUSAL_PATTERNS:
                if pat in lower:
                    return 'No text detected'
        if n < 15:
            return f'Only {n} char{"s" if n != 1 else ""} extracted'
        return None

    def _ocr_set_status(self, text: str, color: str) -> None:
        try:
            self._ocr_status_lbl.configure(text=text, text_color=color)
        except Exception:
            pass

    def _ocr_clear_status(self) -> None:
        self._ocr_set_status('', TEXT_D)

    # ── OCR ───────────────────────────────────────────────────────────────────

    def _on_ctrl_v(self, event) -> None:
        """Intercept Ctrl+V: stage image for confirmation, or fall through to text paste."""
        from vision import get_clipboard_image
        img, err = get_clipboard_image()
        if err:
            self._ocr_set_status(f'⚠  {err}', WARN)
            return 'break'
        if img is None:
            return None   # no image, fall through to normal text paste
        self._ocr_stage(img)
        return 'break'

    def _on_return_key(self, event) -> str | None:
        """Enter: confirm staged image → run OCR. Otherwise insert newline normally."""
        if self._ocr_staged_img is not None:
            img = self._ocr_staged_img
            self._ocr_staged_img = None
            self._ocr_hide_preview()
            self._ocr_clear_status()
            self._ocr_start(img=img)
            return 'break'
        return None

    def _on_escape(self, event=None) -> None:
        """Esc: cancel staged image if waiting, otherwise do nothing (Cancel btn closes)."""
        if self._ocr_staged_img is not None:
            self._ocr_staged_img = None
            self._ocr_hide_preview()
            self._ocr_clear_status()

    def _ocr_stage(self, img) -> None:
        """Show thumbnail and wait for Enter before running OCR."""
        self._ocr_staged_img = img
        self._ocr_show_preview(img)
        self._ocr_set_status('↵ Enter to extract · Esc to cancel', TEXT_S)

    def _ocr_show_preview(self, img) -> None:
        """Show a small thumbnail of the staged image next to the Paste Image button."""
        try:
            from PIL import Image
            from customtkinter import CTkImage
            w, h  = img.size
            max_h = 48
            if h > max_h:
                scale = max_h / h
                img   = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            self._ocr_thumb_ref = CTkImage(light_image=img, dark_image=img,
                                           size=(img.width, img.height))
            self._ocr_thumb_lbl.configure(image=self._ocr_thumb_ref)
        except Exception:
            pass

    def _ocr_hide_preview(self) -> None:
        try:
            self._ocr_thumb_lbl.configure(image=None)
            self._ocr_thumb_ref = None
        except Exception:
            pass

    def _ocr_start(self, img=None) -> None:
        """Kick off OCR in a background thread.  img may be pre-supplied or read from clipboard."""
        if self._ocr_pending:
            return
        if self._vision_extractor is None:
            alert(self, 'OCR needs a vision provider',
                  'Reading text from images needs an AI provider that can '
                  '"see". Open Settings → AI providers and add an OpenAI, '
                  'Anthropic, Gemini, or Groq key.')
            return

        if img is None:
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
            if img is None:
                alert(self, 'No image found',
                      err or 'Copy an image to the clipboard first,\nthen click Paste Image.')
                return

        self._ocr_pending = True
        # Visual: button turns accent purple + spinner text; status label appears
        self._ocr_btn.configure(
            text='⏳  Extracting…',
            fg_color=ACCENT, hover_color=ACCENTL,
            state='disabled',
        )
        self._ocr_set_status('⏳ Extracting…', ACCENTL)

        _img       = img
        _extractor = self._vision_extractor

        def _worker():
            try:
                text = _extractor(_img)
                self.after(0, lambda: self._ocr_done(text))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._ocr_error(m))

        threading.Thread(target=_worker, daemon=True).start()

    def _ocr_done(self, text: str) -> None:
        """Called on the UI thread when OCR completes successfully."""
        self._ocr_pending = False
        self._ocr_hide_preview()
        # Restore button to idle state
        self._ocr_btn.configure(
            text='📷  Paste Image',
            fg_color=SURF2, hover_color=SURF3,
            state='normal',
        )
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        from vision import LONG_TEXT_WARN
        if len(text) > LONG_TEXT_WARN:
            if not confirm(self, 'Long text extracted',
                           f'Extracted {len(text)} characters.\nInsert into prompt?',
                           action_label='Insert'):
                self._ocr_clear_status()
                return

        # Refusals are not inserted, just show the warning
        issue = self._ocr_quality_issue(text)
        if issue in ('No text found', 'No text detected'):
            self._ocr_set_status(f'⚠ {issue}', WARN)
            self.after(4000, self._ocr_clear_status)
            return

        # Insert at cursor position (or end)
        try:
            pos = self._text.index('insert')
        except Exception:
            pos = 'end'
        self._text.insert(pos, text)
        self._update_char_count()

        if issue:
            self._ocr_set_status(f'⚠ {issue}', WARN)
            self.after(4000, self._ocr_clear_status)
        else:
            self._ocr_set_status('✓', OK)
            self.after(1200, self._ocr_clear_status)

    def _ocr_error(self, message: str) -> None:
        """Called on the UI thread when OCR fails."""
        _log.warning('OCR failed: %s', message)
        self._ocr_pending = False
        self._ocr_hide_preview()
        self._ocr_btn.configure(
            text='📷  Paste Image',
            fg_color=SURF2, hover_color=SURF3,
            state='normal',
        )
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        # Show error inline, no blocking dialog
        short = message.split('\n')[0][:40]
        self._ocr_set_status(f'✕ {short}', ERR)
        self.after(4000, self._ocr_clear_status)

    def _center(self, parent) -> None:
        # CTkToplevel continues async geometry configuration after <Map> fires,
        # so we wait a short tick before centering to let it finish.
        def _do_center():
            center_over_parent(self, parent)
            self.lift()
            self.focus_force()
        def _on_map(e=None):
            self.unbind('<Map>')
            self.after(200, _do_center)
        self.bind('<Map>', _on_map)


# ── Hotkey Capture Dialog ─────────────────────────────────────────────────────

class HotkeyCapture(ctk.CTkToplevel):
    """Listens for the next key combination and returns it as a string.

    result after wait_window():
        None , user cancelled
        ''   , user chose to clear the hotkey
        str  , e.g. 'f12' or 'alt+shift+1'

    Design notes:
    • keyboard.read_hotkey() runs in a daemon thread, never on the UI thread.
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
        # Lock guards _done + result. Without it the listener thread
        # could write `result = <captured hotkey>` after Cancel/Clear
        # set `result = ''` on the main thread, returning the wrong
        # value to the parent. Tiny race window in practice but the
        # observed value was non-deterministic.
        import threading as _th
        self._commit_lock = _th.Lock()

        # ── Header, matches SettingsWindow / ThemedDialog pattern ────────────
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
                         font=(FONT_FAMILY, 11), text_color=TEXT_S).pack(pady=(4, 0))

        # Live preview chip, styled as a keyboard shortcut badge
        chip = ctk.CTkFrame(body, fg_color=SURF2, corner_radius=RADIUS_SM,
                             border_width=1, border_color=BORDER2)
        chip.pack(pady=(PAD, 0))
        self._preview_var = tk.StringVar(value=',')
        ctk.CTkLabel(chip, textvariable=self._preview_var,
                     font=(FONT_FAMILY, 15, 'bold'), text_color=ACCENTL,
                     width=210).pack(padx=PAD_LG, pady=PAD_SM)

        # ── Separator ─────────────────────────────────────────────────────────
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')

        # ── Footer, matches ThemedDialog pattern ─────────────────────────────
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
        # tkinter binding for live preview only, shows modifier combos as held
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
            # Just modifiers held, show them
            mods = self._get_mods(event.state)
            self._preview_var.set('+'.join(mods) + '+…' if mods else ',')
        else:
            mods  = self._get_mods(event.state)
            parts = mods + [event.keysym.upper() if len(event.keysym) == 1 else event.keysym]
            self._preview_var.set('+'.join(parts))

    def _on_key_release(self, event) -> None:
        sym = event.keysym.lower()
        if sym in self._MODS and not self._done:
            self._preview_var.set(',')

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
            hk = keyboard.read_hotkey(suppress=False)
            # Under lock so a concurrent Cancel/Clear on the main
            # thread (which also writes _done + result) can't interleave
            # and leave us with mixed values. _commit_lock is short-held
            # and only acquired here + in _clear/_cancel — no deadlock.
            with self._commit_lock:
                if self._done:
                    return
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
        with self._commit_lock:
            self._done  = True
            self.result = ''       # empty string → caller removes the hotkey
        self.destroy()

    def _cancel(self) -> None:
        with self._commit_lock:
            self._done  = True
            self.result = None     # None → caller makes no change
        self.destroy()

    # ── Geometry ──────────────────────────────────────────────────────────────

    def _center(self, parent) -> None:
        def _do_center():
            center_over_parent(self, parent)
            self.lift()
            self.focus_force()
        def _on_map(e=None):
            self.unbind('<Map>')
            self.after(200, _do_center)
        self.bind('<Map>', _on_map)


# ── Folder Input Dialog ───────────────────────────────────────────────────────

class FolderInputDialog(ctk.CTkToplevel):
    """Dialog for creating or renaming a folder.

    result: dict | None, {'name': str, 'color': str} on save, None on cancel.
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

        # Colour picker, same swatches as card editor
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

        def _do_center():
            center_over_parent(self, parent)
            self.lift()
            self.focus_force()
            self._entry.focus_set()
        def _on_map(e=None):
            self.unbind('<Map>')
            self.after(200, _do_center)
        self.bind('<Map>', _on_map)

    def _pick(self, color: str) -> None:
        self._color_var.set(color)
        for c, btn in self._color_btns.items():
            btn.configure(border_color=ACCENT if c == color else BG)

    def _save(self) -> None:
        name = self._name_var.get().strip()
        if name:
            self.result = {'name': name, 'color': self._color_var.get()}
        self.destroy()


# ── Chain Edit Dialog ─────────────────────────────────────────────────────────

class ChainEditDialog(ctk.CTkToplevel):
    """Create or edit a prompt chain.

    result after wait_window():
        None , cancelled
        dict , {'name', 'color', 'hotkey', 'steps': [{'label', 'prompt'}, ...]}
    """

    def __init__(self, parent, chain: dict | None = None,
                 prompts: list | None = None,
                 on_hotkey_suspend=None,
                 on_hotkey_resume=None) -> None:
        super().__init__(parent)
        is_new = chain is None
        self.title('New Chain' if is_new else 'Edit Chain')
        self.configure(fg_color=BG)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.result: dict | None = None
        self._on_hotkey_suspend = on_hotkey_suspend
        self._on_hotkey_resume  = on_hotkey_resume
        self._prompts = list(prompts or [])

        data = chain or {'name': '', 'color': CARD_COLORS[0], 'hotkey': '', 'steps': []}
        self._hotkey = data.get('hotkey', '')
        # Working copy of steps, list of {'label': str, 'prompt': str}
        self._steps: list[dict] = [dict(s) for s in data.get('steps', [])]

        # ── Header ─────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr, text='New Chain' if is_new else 'Edit Chain',
                     font=(FONT_FAMILY, 16, 'bold'), text_color=TEXT_P,
                     ).pack(anchor='w', padx=PAD, pady=PAD_SM)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')

        # ── Scrollable body ─────────────────────────────────────────────────────
        body_outer = ctk.CTkScrollableFrame(
            self, fg_color=BG,
            scrollbar_button_color=SURF2,
            scrollbar_button_hover_color=SURF3,
        )
        body_outer.pack(fill='both', expand=True, padx=PAD, pady=PAD)

        # Name
        ctk.CTkLabel(body_outer, text='Chain name', font=(FONT_FAMILY, 12, 'bold'),
                     text_color=TEXT_S).pack(anchor='w')
        self._name_var = tk.StringVar(value=data.get('name', ''))
        ctk.CTkEntry(body_outer, textvariable=self._name_var, width=400,
                     fg_color=SURFACE, border_color=BORDER2, border_width=1,
                     text_color=TEXT_P, font=(FONT_FAMILY, 13),
                     corner_radius=RADIUS_SM).pack(fill='x', pady=(4, PAD))

        # Color picker
        ctk.CTkLabel(body_outer, text='Card colour', font=(FONT_FAMILY, 12, 'bold'),
                     text_color=TEXT_S).pack(anchor='w')
        cf = ctk.CTkFrame(body_outer, fg_color='transparent')
        cf.pack(anchor='w', pady=(4, PAD))
        self._color_var  = tk.StringVar(value=data.get('color', CARD_COLORS[0]))
        self._color_btns: dict[str, ctk.CTkButton] = {}
        for c in CARD_COLORS:
            btn = ctk.CTkButton(
                cf, text='', width=28, height=28, corner_radius=6,
                fg_color=c, hover_color=c, border_width=2,
                border_color=ACCENT if c == self._color_var.get() else BG,
                command=lambda col=c: self._pick_color(col),
            )
            btn.pack(side='left', padx=2)
            self._color_btns[c] = btn

        # Hotkey
        ctk.CTkLabel(body_outer, text='Hotkey  (optional)',
                     font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_S).pack(anchor='w')
        hk_row = ctk.CTkFrame(body_outer, fg_color='transparent')
        hk_row.pack(fill='x', pady=(4, PAD))
        self._hk_badge = ctk.CTkLabel(
            hk_row, text='', width=180, anchor='w',
            fg_color=SURF2, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 12), text_color=TEXT_S,
            cursor='hand2',
        )
        self._hk_badge.pack(side='left', ipady=4, ipadx=8)
        self._hk_badge.bind('<Button-1>', lambda e: self._assign_hk())
        self._refresh_hk()
        _btn(hk_row, '⌨  Assign…', self._assign_hk, width=110).pack(side='left', padx=(8, 4))
        _btn(hk_row, '✕', self._clear_hk, width=36).pack(side='left')

        # Steps section
        ctk.CTkLabel(body_outer, text='Steps',
                     font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_S).pack(anchor='w')
        ctk.CTkLabel(body_outer,
                     text="Each step's output becomes the next step's input.",
                     font=(FONT_FAMILY, 11), text_color=TEXT_D).pack(anchor='w', pady=(0, 4))

        self._steps_frame = ctk.CTkFrame(body_outer, fg_color='transparent')
        self._steps_frame.pack(fill='x')
        self._rebuild_steps_ui()

        # Add step buttons
        add_row = ctk.CTkFrame(body_outer, fg_color='transparent')
        add_row.pack(fill='x', pady=(PAD_SM, 0))
        _btn(add_row, '＋ Add step (blank)', self._add_blank_step, width=160).pack(side='left', padx=(0, 8))
        if self._prompts:
            _btn(add_row, '＋ Add from library', self._pick_from_library, width=160).pack(side='left')

        # ── Footer ─────────────────────────────────────────────────────────────
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill='x')
        foot = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')
        _btn(foot, 'Save', self._save, width=100,
             fg_color=ACCENT, hover=ACCENTL).pack(side='right', padx=PAD, pady=PAD_SM)
        _btn(foot, 'Cancel', self.destroy, width=80).pack(side='right', pady=PAD_SM)

        self.bind('<Escape>', lambda e: self.destroy())
        self._center(parent)

    # ── Color picker ──────────────────────────────────────────────────────────

    def _pick_color(self, color: str) -> None:
        self._color_var.set(color)
        for c, btn in self._color_btns.items():
            btn.configure(border_color=ACCENT if c == color else BG)

    # ── Hotkey ────────────────────────────────────────────────────────────────

    def _refresh_hk(self) -> None:
        if self._hotkey:
            self._hk_badge.configure(
                text=f'  ⌨  {self._hotkey.upper()}  ',
                text_color=ACCENTL, fg_color=SURF2,
            )
        else:
            self._hk_badge.configure(text='— None assigned', text_color=TEXT_S, fg_color=SURFACE)

    def _assign_hk(self) -> None:
        if self._on_hotkey_suspend:
            self._on_hotkey_suspend()
        dlg = HotkeyCapture(self, current_hotkey=self._hotkey)
        self.wait_window(dlg)
        if self._on_hotkey_resume:
            self._on_hotkey_resume()
        if dlg.result is None:
            return
        new_hk = dlg.result
        if new_hk:
            try:
                from hotkey_validator import (
                    validate_hotkey, ERROR, WARN)
                others = getattr(self, '_validator_other_hotkeys', None) or {}
                this_action = f'chain:{getattr(self, "_name_var", None).get() if hasattr(self, "_name_var") else "Chain"}'
                others.pop(this_action, None)
                diag = validate_hotkey(new_hk, this_action,
                                       other_assignments=others)
                if diag.severity == ERROR:
                    alert(self, 'Hotkey conflict', diag.message)
                    return
                if diag.severity == WARN:
                    if not confirm(self, 'Conflicts with common shortcuts',
                                   diag.message + '\n\nAssign anyway?'):
                        return
            except Exception:
                pass  # validator unavailable, fall through
        self._hotkey = new_hk
        self._refresh_hk()

    def _clear_hk(self) -> None:
        self._hotkey = ''
        self._refresh_hk()

    # ── Steps UI ──────────────────────────────────────────────────────────────

    def _rebuild_steps_ui(self) -> None:
        """Rebuild the steps list widgets from self._steps."""
        for w in self._steps_frame.winfo_children():
            w.destroy()
        if not self._steps:
            ctk.CTkLabel(
                self._steps_frame,
                text='No steps yet, add one below.',
                font=(FONT_FAMILY, 12), text_color=TEXT_D,
            ).pack(anchor='w', pady=PAD_SM)
            return
        for i, step in enumerate(self._steps):
            self._make_step_row(i, step)

    def _make_step_row(self, i: int, step: dict) -> None:
        row = ctk.CTkFrame(self._steps_frame, fg_color=SURFACE, corner_radius=RADIUS_SM)
        row.pack(fill='x', pady=(0, 4))
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=2)

        # ▲▼ reorder buttons
        nav = ctk.CTkFrame(row, fg_color='transparent')
        nav.grid(row=0, column=0, padx=(PAD_SM, 4), pady=PAD_SM, sticky='w')
        _btn(nav, '▲', lambda ii=i: self._move_step(ii, -1),
             width=28, fg_color=SURF3, hover=SURF2).pack()
        _btn(nav, '▼', lambda ii=i: self._move_step(ii, +1),
             width=28, fg_color=SURF3, hover=SURF2).pack(pady=(2, 0))

        # Label entry
        lv = tk.StringVar(value=step.get('label', ''))
        lbl_e = ctk.CTkEntry(
            row, textvariable=lv, width=110,
            fg_color=BG, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 12),
            corner_radius=RADIUS_SM, placeholder_text='Label',
        )
        lbl_e.grid(row=0, column=1, padx=(0, 4), pady=PAD_SM, sticky='w')

        def _on_label_change(*_, ii=i, var=lv):
            if ii < len(self._steps):
                self._steps[ii]['label'] = var.get()
        lv.trace_add('write', _on_label_change)

        # Prompt preview (truncated), click to edit full prompt
        prompt_text = step.get('prompt', '')
        preview = (prompt_text[:60] + '…') if len(prompt_text) > 60 else (prompt_text or '(click to set prompt)')
        prev_lbl = ctk.CTkLabel(
            row, text=preview, font=(FONT_FAMILY, 11), text_color=TEXT_S,
            anchor='w', justify='left', cursor='hand2',
        )
        prev_lbl.grid(row=0, column=2, padx=(0, 4), pady=PAD_SM, sticky='ew')

        def _edit_prompt(ii=i, pl=prev_lbl):
            self._edit_step_prompt(ii, pl)

        prev_lbl.bind('<Button-1>', lambda e, ii=i, pl=prev_lbl: _edit_prompt(ii, pl))

        # Delete button
        _btn(row, '✕', lambda ii=i: self._del_step(ii),
             width=28, fg_color=SURF3, hover=ERR, text_color=TEXT_S,
             ).grid(row=0, column=3, padx=(0, PAD_SM), pady=PAD_SM)

    def _edit_step_prompt(self, idx: int, preview_lbl: ctk.CTkLabel) -> None:
        """Open a small dialog to edit the full prompt text for a step."""
        if idx >= len(self._steps):
            return
        step = self._steps[idx]
        dlg = ctk.CTkToplevel(self)
        dlg.title(f'Edit step: {step.get("label", f"Step {idx+1}")}')
        dlg.configure(fg_color=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text='Prompt text', font=(FONT_FAMILY, 12, 'bold'),
                     text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(PAD, 2))
        txt = ctk.CTkTextbox(
            dlg, width=480, height=140, wrap='word',
            fg_color=SURFACE, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 13), corner_radius=RADIUS_SM,
        )
        txt.pack(fill='x', padx=PAD, pady=(0, PAD_SM))
        txt.insert('1.0', step.get('prompt', ''))
        spellcheck.attach(txt)

        def _save_prompt():
            new_p = txt.get('1.0', 'end-1c').strip()
            self._steps[idx]['prompt'] = new_p
            preview = (new_p[:60] + '…') if len(new_p) > 60 else (new_p or '(click to set prompt)')
            try:
                preview_lbl.configure(text=preview)
            except Exception:
                pass
            dlg.destroy()

        foot = ctk.CTkFrame(dlg, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')
        _btn(foot, 'Save', _save_prompt, width=80,
             fg_color=ACCENT, hover=ACCENTL).pack(side='right', padx=PAD, pady=PAD_SM)
        _btn(foot, 'Cancel', dlg.destroy, width=72).pack(side='right', pady=PAD_SM)

        dlg.bind('<Escape>', lambda e: dlg.destroy())

        def _on_map(e=None):
            center_over_parent(dlg, self)
            dlg.lift()
            dlg.focus_force()
            dlg.unbind('<Map>')
        dlg.bind('<Map>', _on_map)

    def _move_step(self, idx: int, direction: int) -> None:
        new_idx = idx + direction
        if 0 <= new_idx < len(self._steps):
            self._steps[idx], self._steps[new_idx] = self._steps[new_idx], self._steps[idx]
            self._rebuild_steps_ui()

    def _del_step(self, idx: int) -> None:
        if 0 <= idx < len(self._steps):
            self._steps.pop(idx)
            self._rebuild_steps_ui()

    def _add_blank_step(self) -> None:
        self._steps.append({'label': f'Step {len(self._steps) + 1}', 'prompt': ''})
        self._rebuild_steps_ui()

    def _pick_from_library(self) -> None:
        """Show a small popup listing prompt titles; click to add as a step."""
        if not self._prompts:
            return
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes('-topmost', True)
        popup.configure(bg=BG)
        popup.grab_set()

        card = ctk.CTkFrame(popup, fg_color=SURFACE, corner_radius=RADIUS,
                            border_width=1, border_color=BORDER2)
        card.pack(padx=1, pady=1)

        ctk.CTkLabel(card, text='Add step from library',
                     font=(FONT_FAMILY, 13, 'bold'), text_color=TEXT_P,
                     ).pack(anchor='w', padx=PAD, pady=(PAD, PAD_SM))

        scroll = ctk.CTkScrollableFrame(card, fg_color='transparent', width=300, height=240)
        scroll.pack(padx=PAD_SM, pady=(0, PAD_SM))

        def _add_step(p: dict):
            self._steps.append({
                'label':  p.get('title', 'Step'),
                'prompt': p.get('prompt', ''),
            })
            self._rebuild_steps_ui()
            popup.destroy()

        for p in self._prompts:
            ctk.CTkButton(
                scroll,
                text=p.get('title', ','),
                anchor='w', height=30,
                fg_color='transparent', hover_color=SURF2,
                text_color=TEXT_P, font=(FONT_FAMILY, 12),
                corner_radius=RADIUS_SM,
                command=lambda pp=p: _add_step(pp),
            ).pack(fill='x', pady=1)

        _btn(card, 'Cancel', popup.destroy, width=80).pack(pady=(0, PAD_SM))

        popup.update_idletasks()
        x = self.winfo_rootx() + self.winfo_width() // 2 - popup.winfo_width() // 2
        y = self.winfo_rooty() + self.winfo_height() // 2 - popup.winfo_height() // 2
        popup.geometry(f'+{max(0, x)}+{max(0, y)}')

        def _safe_close_popup():
            try:
                if popup.winfo_exists():
                    popup.destroy()
            except Exception:
                pass

        popup.bind('<Escape>', lambda e: _safe_close_popup())
        popup.bind('<FocusOut>', lambda e: self.after(100, _safe_close_popup))

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self) -> None:
        name = self._name_var.get().strip()
        if not name:
            alert(self, 'Required', 'Chain name cannot be empty.')
            return
        if not self._steps:
            alert(self, 'Required', 'Add at least one step.')
            return
        for i, s in enumerate(self._steps):
            if not s.get('prompt', '').strip():
                alert(self, 'Required', f'Step {i + 1} ({s.get("label", "")!r}) has no prompt text.')
                return
        self.result = {
            'name':   name,
            'color':  self._color_var.get(),
            'hotkey': self._hotkey,
            'active': False,   # caller sets active state
            'steps':  [dict(s) for s in self._steps],
        }
        self.destroy()

    # ── Geometry ──────────────────────────────────────────────────────────────

    def _center(self, parent) -> None:
        def _do_center():
            # Size the dialog to comfortably show all fields, then center on screen
            w, h = 520, 640
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x  = (sw - w) // 2
            y  = max(40, (sh - h) // 2)
            self.geometry(f'{w}x{h}+{x}+{y}')
            self.minsize(480, 560)
            self.lift()
            self.focus_force()
        def _on_map(e=None):
            self.unbind('<Map>')
            self.after(200, _do_center)
        self.bind('<Map>', _on_map)


# ── Library Window ────────────────────────────────────────────────────────────

class LibraryWindow:
    # Maps library tab key → config hotkeys key (for right-click rebind)
    _TAB_HOTKEY_MAP = {
        'recorder':     'recorder',
        'gif':          'gif_record',
        'ask':          'ask',
        'web':          'web',
        'chains':       'chain',
        'notes':        'notes',
        'whiteboard':   'whiteboard',
        'transcribe':   'transcribe',
        'audio_editor': 'audio_editor',
    }

    # No placeholder slots — F11/F12 will be wired when real features arrive.
    _PLACEHOLDER_SLOTS = ()

    def __init__(self, root, prompts: list, on_select: Callable, on_save: Callable,
                 hotkey_cfg: dict | None = None,
                 on_hotkey_suspend: Callable | None = None,
                 on_hotkey_resume:  Callable | None = None,
                 folders: list | None = None,
                 folder_colors: dict | None = None,
                 on_folders_changed: Callable | None = None,
                 vision_extractor: Callable | None = None,
                 macro_library: 'MacroLibrary | None' = None,
                 on_macro_play: Callable | None = None,
                 on_macro_hotkeys_changed: Callable | None = None,
                 on_feature_hotkey_changed: Callable | None = None,
                 on_chains_changed: Callable | None = None) -> None:
        # Boot-time safety check: if any per-tab render method is
        # missing the _render_tab_guard line, log a CRITICAL warning.
        # Belt-and-suspenders pair with test_tab_guard.py. See the
        # docstring on _verify_tab_guards_at_boot for the full story.
        self.__class__._verify_tab_guards_at_boot()
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
        self._vision_extractor   = vision_extractor
        self._macro_library           = macro_library
        self._on_macro_play           = on_macro_play
        self._on_macro_hotkeys_changed = on_macro_hotkeys_changed
        self._on_feature_hotkey_changed = on_feature_hotkey_changed
        self._on_chains_changed = on_chains_changed
        # Recorder tab state (updated by main.py via update_recorder_state)
        self._recorder_state    = 'idle'   # 'idle'|'recording'|'stopping'
        self._recorder_elapsed  = 0.0
        self._recorder_size_mb  = 0.0
        self._on_recorder_toggle: Callable | None = None  # set by main.py
        self._on_macro_toggle:   Callable | None = None  # set by main.py → fires macro:hotkey
        self._on_macro_reset:    Callable | None = None  # set by main.py → abort + clear
        self._macro_state = 'idle'   # 'idle'|'recording'|'ready'|'playing', mirrors main.py
        self._rec_tab_ticker    = None   # after() job id
        # GIF tab state
        self._gif_state   = 'idle'   # 'idle'|'recording'|'encoding'
        self._gif_elapsed = 0.0
        self._gif_frames  = 0
        self._gif_tab_ticker: int | None = None
        self._on_gif_toggle: Callable | None = None  # set by main.py → fires gif:toggle
        self._on_ask: Callable | None = None         # set by main.py → fires ('ask', text)
        self._on_new_note: Callable | None = None    # set by main.py → opens QuickNotesWindow
        self._on_open_whiteboard: Callable | None = None  # set by main.py → opens Whiteboard
        self._on_open_audio_editor: Callable | None = None  # set by main.py → toggles audio_editor
        self._active_tab         = 'prompts'   # 'prompts' | 'macros' | 'recorder' | 'gif' | 'ask'
        self.active_idx  = 0
        self._cards: list[ctk.CTkFrame] = []
        self._macro_cards: list[ctk.CTkFrame] = []
        self._current_cols = 2
        self._resize_cover: ctk.CTkFrame | None = None
        # Stored after each full _render_cards() for fast reflow on resize
        self._reflow_root_cards: list = []
        self._reflow_sep_widget = None
        self._reflow_folders: list = []   # [(fname, header_widget, [card_widgets])]
        # Tab content cache: keep each tab's widgets alive across switches so
        # re-visiting a tab is instant (grid_remove + grid instead of destroy
        # + rebuild). The expensive render only runs on first visit or when
        # explicitly invalidated by a data-mutation path via _render_cards.
        self._tab_containers: dict[str, 'ctk.CTkFrame'] = {}
        self._tab_built:     set[str] = set()
        # Per-tab scroll position memory so switching away and back feels like
        # picking up where you left off, not jumping to the top every time.
        self._tab_scroll_pos: dict[str, float] = {}
        self._collapsed_folders: set[str] = set()
        self._folder_headers: dict[str, ctk.CTkFrame] = {}
        self._drag_folder_tgt: str | None = None
        self._tab_btns: dict[str, ctk.CTkButton] = {}
        self._search_bar_frame: 'ctk.CTkFrame | None' = None
        self._build()

    def _build(self) -> None:
        self.win = ctk.CTkToplevel(self.root)
        self.win.title('Library, Hotkeys')
        self.win.configure(fg_color=BG)
        self.win.minsize(820, 460)
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
        # Pre-warm every other tab into a hidden container so the user's
        # first click on any tab feels instant. Adds ~500ms-1s to startup
        # (the heaviest is Transcribe, which builds ~100 widgets) but the
        # window stays withdrawn so the user doesn't see it. By the time
        # they press Alt+Shift+E for the first time, all tabs are hot.
        _PREWARM_ORDER = (
            'transcribe',       # heaviest, do first while we have momentum
            'macros',
            'chains',
            'ask',
            'web',
            'recorder',
            'gif',
            'notes',
            'whiteboard',
            'audio_editor',
        )
        for _tk in _PREWARM_ORDER:
            try:
                self._prewarm_tab(_tk)
            except Exception as _e:
                _log.warning(f'pre-warm of {_tk} failed: {_e}')
        self._center()
        self.win.bind('<Configure>', self._on_resize)
        self.win.bind('<Map>', self._on_map_restore)
        self.win.bind('<FocusIn>', self._on_focus_in)

    def _build_header(self) -> None:
        # Taller header so we can fit the title + 3-second tagline + active-
        # prompt indicator without crowding. The tagline is the elevator
        # pitch: a layperson opening this window should grok "what's in
        # here" before they finish reading it.
        hdr = ctk.CTkFrame(self.win, fg_color=SURFACE, corner_radius=0, height=88)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)

        left = ctk.CTkFrame(hdr, fg_color='transparent')
        left.pack(side='left', fill='y', padx=PAD)
        ctk.CTkLabel(left, text='Library',
                     font=(FONT_FAMILY, 16, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', pady=(10, 0))
        # 3-second value-prop subtitle. Action verbs + emojis so the eye
        # scans the row in under a second.
        ctk.CTkLabel(
            left,
            text='Your hub for templates, voice typing, recordings, and more',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
        ).pack(anchor='w', pady=(0, 2))
        refine_hk = self.hotkey_cfg.get('refine', 'alt+shift+w').upper()
        self._active_lbl = ctk.CTkLabel(
            left,
            text=f'{refine_hk}  →  {self.prompts[0]["title"] if self.prompts else ","}',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
        )
        self._active_lbl.pack(anchor='w')

        right = ctk.CTkFrame(hdr, fg_color='transparent')
        right.pack(side='right', fill='y', padx=PAD)
        _btn(right, '?', self._show_shortcuts,
             width=32, fg_color=SURF2, hover=SURF3).pack(anchor='e', pady=20, side='left')

        hint = ctk.CTkFrame(self.win, fg_color=SURF2, height=36, corner_radius=0)
        hint.pack(fill='x')
        hint.pack_propagate(False)
        refine_hk = self.hotkey_cfg.get('refine', 'alt+shift+w').upper()
        self._hint_lbl = ctk.CTkLabel(
            hint,
            text=f'Click to activate  ·  Double-click to edit  ·  Right-click for menu  ·  {refine_hk} to refine',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
        )
        self._hint_lbl.pack(side='left', padx=PAD)
        self._hint_prompts_text = (
            f'Click to activate  ·  Double-click to edit  ·  Right-click for menu  ·  {refine_hk} to refine'
        )
        hk = self.hotkey_cfg
        macro_hk_h    = hk.get('macro_record', 'shift+f1').upper()
        recorder_hk_h = hk.get('recorder',     'shift+f2').upper()
        gif_hk_h      = hk.get('gif_record',   'shift+f3').upper()
        ask_hk_h      = hk.get('ask',          'shift+f4').upper()
        web_hk_h      = hk.get('web',          'shift+f5').upper()
        self._hint_macros_text = (
            f'{macro_hk_h} to record  ·  {macro_hk_h} again to stop  ·  {macro_hk_h} once more to play  ·  Esc / Del to abort'
        )
        self._hint_recorder_text = (
            f'{recorder_hk_h} to start / stop recording  ·  1 GB cap auto-stops  ·  Esc to abort'
        )
        self._hint_gif_text = (
            f'{gif_hk_h} to start / stop GIF  ·  Auto-stops at max duration  ·  Esc to abort'
        )
        self._hint_ask_text = (
            f'{ask_hk_h} to explain, select text, copy a screenshot, or type a question below'
        )
        self._hint_web_text = (
            f'{web_hk_h} to open active bookmark  ·  Click any bookmark to open in browser'
        )
        chain_hk_h = hk.get('chain', 'shift+f6').upper()
        self._hint_chains_text = (
            f'{chain_hk_h} to run the active chain on selected text  ·  Click ✓ to set a chain active'
        )
        notes_hk_h = hk.get('notes', 'shift+f7').upper()
        self._hint_notes_text = (
            f'{notes_hk_h} to open Quick Notes  ·  All your saved notes live here'
        )
        wb_hk_h = hk.get('whiteboard', 'shift+f8').upper()
        self._hint_whiteboard_text = (
            f'{wb_hk_h} to open the Whiteboard  ·  Sketch, diagram, brainstorm, offline'
        )
        tr_hk_h = hk.get('transcribe', 'shift+f9').upper()
        self._hint_transcribe_text = (
            f'{tr_hk_h} to open Transcribe  ·  Audio/video → text with speakers & summary'
        )
        # Placeholder slots, same hint shape as the real tabs; the slot
        # name and hotkey are the only varying parts.
        self._hint_slot_text: dict[str, str] = {}
        for _key, _label, _default_hk in self._PLACEHOLDER_SLOTS:
            _h = hk.get(_key, _default_hk).upper()
            self._hint_slot_text[_key] = (
                f'{_h} reserved for {_label}  ·  Feature coming soon, right-click the tab to rebind'
            )

        # ── Tab switcher row ──────────────────────────────────────────────────
        tab_row = ctk.CTkFrame(self.win, fg_color=SURFACE, corner_radius=0, height=38)
        tab_row.pack(fill='x')
        tab_row.pack_propagate(False)

        def _make_tab_btn(text, tab_name, width=88):
            is_active = (self._active_tab == tab_name)
            btn = ctk.CTkButton(
                tab_row, text=text, width=width,
                fg_color=ACCENT if is_active else SURF2,
                hover_color=ACCENTL if is_active else SURF3,
                text_color=TEXT_P,
                corner_radius=RADIUS_SM,
                font=(FONT_FAMILY, 13),
                command=lambda t=tab_name: self._switch_tab(t),
            )
            btn.pack(side='left', padx=(PAD if tab_name == 'prompts' else 4, 0), pady=4)
            self._tab_btns[tab_name] = btn
            if tab_name in self._TAB_HOTKEY_MAP:
                btn.bind('<Button-3>', lambda e, t=tab_name: self._show_rebind_popup(t, e))

        # Tab tooltips follow the principle: never repeat the label, never
        # repeat the same instruction across tabs. Hotkey + use-case only.
        # "Right-click to rebind" is shown once in the library hint bar
        # instead of pasted on every tooltip.
        _make_tab_btn('✦  Prompts', 'prompts')
        Tooltip(self._tab_btns['prompts'],
                f'Pick the template Alt+Shift+W uses to transform selected text')
        _make_tab_btn('⏺  Macros', 'macros')
        Tooltip(self._tab_btns['macros'],
                f'{macro_hk_h} to record / replay a mouse + keyboard sequence')
        _make_tab_btn('🎥  Screen', 'recorder')
        Tooltip(self._tab_btns['recorder'],
                f'{recorder_hk_h} to start / stop a video recording')
        _make_tab_btn('🎞  GIF', 'gif')
        Tooltip(self._tab_btns['gif'],
                f'{gif_hk_h} to start / stop an animated GIF of a screen region')
        _make_tab_btn('✦  Explain', 'ask')
        Tooltip(self._tab_btns['ask'],
                f'{ask_hk_h} to ask the AI about selected text '
                f'(answers it instead of transforming it)')
        _make_tab_btn('🌐  Web', 'web')
        Tooltip(self._tab_btns['web'],
                f'{web_hk_h} to open the active bookmark')
        chain_hk_h2 = hk.get('chain', 'shift+f6').upper()
        _make_tab_btn('⛓  Chains', 'chains')
        Tooltip(self._tab_btns['chains'],
                f'{chain_hk_h2} to run the active multi-step workflow on selected text')
        notes_hk_h2 = hk.get('notes', 'shift+f7').upper()
        _make_tab_btn('📝  Notes', 'notes')
        Tooltip(self._tab_btns['notes'],
                f'{notes_hk_h2} to open Quick Notes anywhere')
        wb_hk_h2 = hk.get('whiteboard', 'shift+f8').upper()
        _make_tab_btn('🎨  Whiteboard', 'whiteboard')
        Tooltip(self._tab_btns['whiteboard'],
                f'{wb_hk_h2} to open it from anywhere')
        tr_hk_h2 = hk.get('transcribe', 'shift+f9').upper()
        _make_tab_btn('🎙  Transcribe', 'transcribe')
        Tooltip(self._tab_btns['transcribe'],
                f'{tr_hk_h2} to open it from anywhere')
        ae_hk_h2 = hk.get('audio_editor', 'shift+f10').upper()
        _make_tab_btn('🎵  Audio editor', 'audio_editor')
        Tooltip(self._tab_btns['audio_editor'],
                f'{ae_hk_h2} to open it from anywhere')
        # ── Reserved Shift+F11..F12 placeholder tabs ────────────────────────
        # Compact buttons, just the function-key label, so the tab row
        # doesn't blow out horizontally. Tooltip still shows the full hint.
        _slot_list = list(self._PLACEHOLDER_SLOTS)
        for _i, (_key, _label, _default_hk) in enumerate(_slot_list):
            _hk = hk.get(_key, _default_hk).upper()
            _fn = _hk.split('+')[-1]  # 'F9' from 'SHIFT+F9'
            _make_tab_btn(f'·  {_fn}', _key, width=56)
            Tooltip(self._tab_btns[_key],
                    f'{_hk} reserved for {_label}')
        # Right margin on the strip: mirror the PAD spacing on the left
        # of the first tab so the row breathes equally on both sides.
        # An invisible spacer frame is simpler than fighting Tk's pack
        # logic to add trailing padx on the last button.
        ctk.CTkFrame(tab_row, fg_color=SURFACE, width=PAD, height=1
                     ).pack(side='left')

        # "Open Folder" button is rendered inside the Macros tab content (_render_macro_cards)

        # ── Search bar + Add button (Prompts tab only) ───────────────────────
        search_bar = ctk.CTkFrame(self.win, fg_color=BG, corner_radius=0, height=44)
        search_bar.pack(fill='x')
        search_bar.pack_propagate(False)
        _btn(search_bar, '＋ Add', self._add, width=88,
             fg_color=ACCENT, hover=ACCENTL).pack(side='right', padx=(0, PAD), pady=8)
        self._search_entry = ctk.CTkEntry(
            search_bar,
            placeholder_text='Search prompts…',
            height=28,
            fg_color=SURF2, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 12),
            corner_radius=RADIUS_SM,
        )
        self._search_entry.pack(fill='x', padx=(PAD, 8), pady=8)
        # Sync entry text → _search_var without using textvariable (which breaks placeholder)
        def _on_search_change(*_):
            self._search_var.set(self._search_entry.get())
        self._search_entry._entry.bind('<KeyRelease>', _on_search_change)
        self._search_entry._entry.bind('<<Paste>>', lambda e: self.win.after(10, _on_search_change))
        self._search_entry._entry.bind('<<Cut>>',   lambda e: self.win.after(10, _on_search_change))
        self._search_bar_frame = search_bar

    def _build_grid(self) -> None:
        # ── Outer scroll + guard reference ──────────────────────────────
        # `self._scroll` gets temporarily swapped to the per-tab container
        # by _show_active_tab / _invalidate_tab / _prewarm_tab whenever a
        # render runs. Save the ORIGINAL outer scroll separately so
        # _render_X_tab methods can detect a "called without swap" situation
        # and route through _invalidate_tab() instead — otherwise
        # `for w in self._scroll.winfo_children(): w.destroy()` (at the top
        # of every render) destroys every tab container in one shot,
        # leaving subsequent tab clicks visually stuck. See the
        # _render_tab_guard() helper below; every tab render calls it.
        self._scroll = ctk.CTkScrollableFrame(
            self.win, fg_color=BG,
            scrollbar_button_color=SURF2,
            scrollbar_button_hover_color=SURF3,
        )
        self._scroll.pack(fill='both', expand=True, padx=PAD, pady=PAD)
        # Permanent reference to the OUTER scroll — never reassigned.
        # `self._scroll` itself gets swapped to per-tab containers during
        # rendering; comparisons against `self._outer_scroll` let
        # _render_tab_guard() detect "called without swap" scenarios.
        self._outer_scroll = self._scroll
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

    # ------------------------------------------------------------------ resize
    def _on_resize(self, event=None) -> None:
        if hasattr(self, '_resize_job') and self._resize_job:
            try:
                self.win.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.win.after(100, self._do_deferred_resize)

    def _do_deferred_resize(self) -> None:
        self._resize_job = None
        # Only the Prompts tab has a column-based grid that needs reflow.
        # All other tabs use weight=1 columns and expand automatically.
        if self._active_tab != 'prompts':
            return
        try:
            if self.win.state() == 'iconic':
                return
        except Exception:
            pass
        try:
            new_cols = self._cols()
        except Exception:
            return
        if new_cols == self._current_cols:
            return  # nothing to do, column count unchanged
        self._current_cols = new_cols
        self._reflow_cards()

    def _on_focus_in(self, event=None) -> None:
        """Re-render file-backed tabs when the window regains focus so that
        files deleted outside the app disappear from the list immediately."""
        if event and event.widget is not self.win:
            return   # ignore focus events bubbling from child widgets
        if self._active_tab == 'recorder':
            self._invalidate_tab('recorder')
        elif self._active_tab == 'gif':
            self._invalidate_tab('gif')

    def _on_map_restore(self, event=None) -> None:
        """Window restored from minimised, reflow to correct column count."""
        if hasattr(self, '_resize_job') and self._resize_job:
            try:
                self.win.after_cancel(self._resize_job)
            except Exception:
                pass
            self._resize_job = None
        self._do_deferred_resize()

    def _disable_dwm_transitions(self) -> None:
        """Kill the DWM zoom-to-taskbar / zoom-to-fullscreen animations.

        Without this, maximize/minimize still show an animated transition
        during which the window is briefly visible at intermediate sizes,
        WM_SETREDRAW alone can't suppress those DWM-composited frames.
        """
        try:
            import ctypes
            from win_helpers import top_level_hwnd
            DWMWA_TRANSITIONS_FORCEDISABLED = 3
            # Use the OS top-level HWND. For CTkToplevel, winfo_id()
            # may return an inner widget HWND in some Tk/CTk versions;
            # always walking GA_ROOT makes this resilient.
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                top_level_hwnd(self.win),
                DWMWA_TRANSITIONS_FORCEDISABLED,
                ctypes.byref(ctypes.c_int(1)),
                ctypes.sizeof(ctypes.c_int))
        except Exception:
            pass

    def _remove_resize_cover(self) -> None:
        if self._resize_cover:
            try:
                self._resize_cover.place_forget()
                self._resize_cover.destroy()
            except Exception:
                pass
            self._resize_cover = None

    def _reflow_cards(self) -> None:
        """Reposition existing card widgets for the current column count.

        Zero widget creation/destruction, just moves widgets to new grid
        cells.  Called on resize; _render_cards() is called when data changes.
        """
        cols = self._current_cols
        # Clear weights on all columns (reset leftover weights from old layout)
        for _c in range(cols + 6):
            try:
                self._scroll.columnconfigure(_c, weight=0)
            except Exception:
                pass

        current_row = 0

        # Root cards
        for i, card in enumerate(self._reflow_root_cards):
            card.grid(row=current_row + i // cols,
                      column=i % cols, padx=8, pady=8, sticky='n')
        if self._reflow_root_cards:
            current_row += math.ceil(len(self._reflow_root_cards) / cols)

        # Separator
        if self._reflow_sep_widget:
            self._reflow_sep_widget.grid(
                row=current_row, column=0, columnspan=cols,
                sticky='ew', padx=8, pady=(4, 0))
            current_row += 1

        # Folder sections
        for fname, fheader, fcards in self._reflow_folders:
            fheader.grid(row=current_row, column=0,
                         padx=8, pady=(8, 2), sticky='n')
            current_row += 1
            if fname not in self._collapsed_folders:
                for i, card in enumerate(fcards):
                    card.grid(row=current_row + i // cols,
                              column=i % cols, padx=8, pady=8, sticky='n')
                if fcards:
                    current_row += math.ceil(len(fcards) / cols)

    # ── Tab-content caching layer ─────────────────────────────────────────────
    # Each tab gets its own CTkFrame inside self._scroll, kept alive across
    # switches. Tab-switch path uses _show_active_tab, which hides others and
    # shows the target (building it once on first visit). Data-mutation paths
    # keep calling _render_cards, which now invalidates and rebuilds only the
    # ACTIVE tab's container, leaving other cached tabs untouched. This makes
    # repeat switches sub-100ms while still refreshing on data changes.

    def _show_active_tab(self) -> None:
        """Tab-switch entry: hide non-active containers, show the active one.
        If first visit, build it. Never rebuilds an already-built tab.

        Wrapped in defensive recovery: if the render for the new tab
        raises, the user must never be stranded with a blank pane.
        We rebuild the previous tab (if any) so at least something is
        visible while the failure gets investigated via the log.
        """
        target = self._active_tab
        _log.info(f'[TAB] _show_active_tab target={target!r}  '
                  f'containers={list(self._tab_containers.keys())}')

        # Hide every cached container that isn't the target tab. Track
        # which ones we hid so we can re-grid them on render failure.
        hidden = []
        for k, c in self._tab_containers.items():
            if k != target:
                try:
                    # Was the container actually mapped before grid_remove?
                    was_mapped = bool(c.winfo_ismapped())
                    c.grid_remove()
                    if was_mapped:
                        _log.info(f'[TAB]   hid {k!r} (was mapped)')
                    hidden.append((k, c))
                except Exception as e:
                    _log.warning(f'[TAB]   grid_remove({k!r}) failed: {e}')

        # Get or create the container for the target tab.
        if target not in self._tab_containers:
            try:
                container = ctk.CTkFrame(self._scroll, fg_color='transparent')
                container.columnconfigure(0, weight=1)
                self._tab_containers[target] = container
            except Exception as exc:
                _log.error(f'Tab switch: failed to create container for {target!r}: {exc}')
                # Re-grid the most recent previously-active tab so the user
                # sees something.
                for k, c in hidden:
                    try: c.grid(row=0, column=0, sticky='nsew')
                    except Exception: pass
                    break
                return

        container = self._tab_containers[target]
        try:
            container.grid(row=0, column=0, sticky='nsew')
            # After grid: log mapped state + children count. If the user
            # reports "stuck on previous tab" the question is whether
            # THIS line actually made the new tab's container visible.
            container.update_idletasks()
            _log.info(f'[TAB]   gridded {target!r}: '
                      f'mapped={container.winfo_ismapped()} '
                      f'kids={len(container.winfo_children())} '
                      f'size={container.winfo_width()}x{container.winfo_height()}')
        except Exception as exc:
            _log.error(f'Tab switch: failed to grid {target!r}: {exc}')

        # Already built? Just show, no work.
        if target in self._tab_built:
            _log.info(f'[TAB]   {target!r} already built; switch complete')
            return
        _log.info(f'[TAB]   {target!r} not yet built; building now')

        # First visit: build the widgets. Swap self._scroll → container so the
        # existing render methods (which use self._scroll as their parent)
        # build into the container without modification.
        real_scroll = self._scroll
        self._scroll = container
        build_ok = False
        try:
            self._render_cards_impl()
            build_ok = True
        except Exception as exc:
            # Loud log so we can find the actual cause from user reports
            # of "tab went blank". The defensive recovery below at least
            # gets them back to a working pane.
            _log.exception(f'Tab switch: _render_cards_impl raised on {target!r}: {exc}')
        finally:
            self._scroll = real_scroll

        if build_ok:
            self._tab_built.add(target)
            _log.info(f'[TAB]   {target!r} build OK; switch complete')
        else:
            # Render failed — invalidate so the next click retries from
            # scratch, and re-grid the most recent prior tab so the user
            # isn't staring at a blank pane.
            _log.warning(f'[TAB]   {target!r} build FAILED; running '
                         f'fallback (re-grid first hidden container)')
            self._tab_built.discard(target)
            try: container.grid_remove()
            except Exception: pass
            for k, c in hidden:
                try:
                    c.grid(row=0, column=0, sticky='nsew')
                    self._active_tab = k
                    _log.warning(f'[TAB]   fallback re-gridded {k!r}; '
                                 f'_active_tab now {k!r}')
                    break
                except Exception:
                    pass

    def _prewarm_tab(self, tab_key: str) -> None:
        """Build *tab_key*'s widget tree into a hidden container so its
        first user-visible visit is instant. No-op if already built.

        Used during LibraryWindow construction to warm every tab off
        the critical path. The user opted into a slower startup in
        exchange for a smooth post-startup experience.
        """
        if tab_key in self._tab_built:
            return
        if tab_key not in self._tab_containers:
            container = ctk.CTkFrame(self._scroll, fg_color='transparent')
            container.columnconfigure(0, weight=1)
            self._tab_containers[tab_key] = container
        container = self._tab_containers[tab_key]
        # Container intentionally NOT gridded, so it stays invisible.

        # Swap state during the build so the existing render impl wires
        # widgets into this hidden container without modification.
        real_active = self._active_tab
        real_scroll = self._scroll
        self._active_tab = tab_key
        self._scroll = container
        try:
            self._render_cards_impl()
        finally:
            self._active_tab = real_active
            self._scroll = real_scroll
        self._tab_built.add(tab_key)

    def _invalidate_tab(self, tab: str) -> None:
        """Mark *tab* as needing a full rebuild on its next visit. If *tab* is
        currently active, rebuild immediately so the user sees fresh data."""
        # Diagnostic: log the caller so we can see WHO invalidates Macros/
        # Recorder/Gif silently while the user is on another tab.
        try:
            import sys as _sys
            f = _sys._getframe(1)
            caller = f'{f.f_code.co_name}:{f.f_lineno}'
        except Exception:
            caller = '?'
        _log.info(f'[TAB] _invalidate_tab({tab!r}) by {caller} '
                  f'(active={self._active_tab!r})')
        self._tab_built.discard(tab)
        if self._active_tab != tab:
            return  # next visit will rebuild
        # Rebuild now into the cached container.
        if tab not in self._tab_containers:
            # No container yet, normal show path will build it.
            self._show_active_tab()
            return
        c = self._tab_containers[tab]
        for w in c.winfo_children():
            try: w.destroy()
            except Exception: pass
        real_scroll = self._scroll
        self._scroll = c
        try:
            self._render_cards_impl()
        finally:
            self._scroll = real_scroll
        self._tab_built.add(tab)

    def _render_cards(self) -> None:
        """Data-mutation entry for the PROMPTS tab. Always invalidates
        'prompts' explicitly; previously this used self._active_tab,
        which meant edit-a-prompt-from-elsewhere left the prompts tab
        cache stale AND destroyed whichever tab (e.g. Transcribe with
        a live worker) the user happened to be looking at."""
        self._invalidate_tab('prompts')

    # ── Tab render safety guard ──────────────────────────────────────────────
    #
    # Every _render_X_tab method (and _render_macro_cards) MUST start with:
    #
    #     if self._render_tab_guard('X'):
    #         return
    #
    # The guard exists because every tab render begins with
    #     for w in self._scroll.winfo_children(): w.destroy()
    # which is correct WHEN self._scroll has been swapped to the per-tab
    # container (the normal path via _show_active_tab / _invalidate_tab /
    # _prewarm_tab does this swap), but CATASTROPHICALLY WRONG when
    # called with self._scroll pointing at the OUTER CTkScrollableFrame —
    # because the outer scroll's direct children are ALL tab containers.
    # One unswapped call wipes every tab container in one shot, leaving
    # subsequent tab clicks visually stuck (grid_remove on the stale refs
    # fails with "bad window path name" and the new grid lands nowhere).
    #
    # The guard detects the unswapped state by comparing self._scroll to
    # self._outer_scroll. If they match → reroute through _invalidate_tab
    # (which sets up the swap correctly), and signal the caller to bail.
    #
    # WHEN ADDING A NEW TAB: drop `if self._render_tab_guard('your_tab_key'):
    # return` as the first line of your render method. Without it, any
    # external caller that calls your method directly (e.g. main.py after
    # a state change) can break every other tab.

    def _render_tab_guard(self, tab_key: str) -> bool:
        """Return True if the caller should bail and let _invalidate_tab
        rerun the render correctly. Returns False when self._scroll is
        already swapped to the per-tab container (the normal path)."""
        if self._scroll is getattr(self, '_outer_scroll', None):
            _log.info(f'[TAB] _render_tab_guard tripped for {tab_key!r}: '
                      'caller used outer scroll, routing through '
                      '_invalidate_tab to protect other tab containers')
            self._invalidate_tab(tab_key)
            return True
        return False

    @classmethod
    def _verify_tab_guards_at_boot(cls) -> None:
        """Boot-time safety net: walk every method on this class whose
        name matches the per-tab render convention and verify it begins
        with `if self._render_tab_guard(...): return`. Logs a CRITICAL
        warning for any method missing the guard.

        This is the belt-and-suspenders pair to test_tab_guard.py — the
        test catches missing guards in CI / pre-commit, this catches
        them at app boot if the test was skipped. Together they make
        it impossible for a new tab to ship with the guard missing
        without somebody yelling at the dev.
        """
        import ast as _ast, inspect as _inspect, textwrap as _tw
        # Pull every method whose name fits the per-tab render pattern.
        candidates = []
        for name in dir(cls):
            if name == '_render_cards_impl':
                continue  # dispatcher, not a per-tab render
            if name == '_render_macro_cards' or (
                    name.startswith('_render_') and name.endswith('_tab')):
                obj = getattr(cls, name, None)
                if callable(obj):
                    candidates.append((name, obj))

        missing = []
        for name, fn in candidates:
            try:
                src = _tw.dedent(_inspect.getsource(fn))
                tree = _ast.parse(src)
                if not tree.body or not isinstance(tree.body[0], _ast.FunctionDef):
                    continue
                body = tree.body[0].body[:]
                # Skip docstring if any
                if (body and isinstance(body[0], _ast.Expr)
                        and isinstance(body[0].value, _ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body = body[1:]
                if not body:
                    missing.append(name); continue
                first = body[0]
                guarded = (
                    isinstance(first, _ast.If)
                    and isinstance(first.test, _ast.Call)
                    and isinstance(first.test.func, _ast.Attribute)
                    and first.test.func.attr == '_render_tab_guard'
                    and len(first.body) == 1
                    and isinstance(first.body[0], _ast.Return)
                )
                if not guarded:
                    missing.append(name)
            except Exception:
                # If we can't inspect the source for some reason, don't
                # break boot — just skip this method. The static test
                # is the authoritative check.
                continue

        if missing:
            _log.critical(
                '[TAB GUARD] %d tab render method(s) are missing the '
                '_render_tab_guard() safety check: %s. Without it, any '
                'external direct call (e.g. from main.py after a state '
                'change) will destroy every tab container. See the '
                'comment above _render_cards_impl in library.py and add '
                '`if self._render_tab_guard(\'<key>\'): return` as the '
                'first statement of each listed method.',
                len(missing), missing)
        else:
            _log.info(f'[TAB GUARD] startup check: all '
                      f'{len(candidates)} per-tab render methods are '
                      'guarded')

    def _render_cards_impl(self) -> None:
        if self._active_tab == 'macros':
            self._render_macro_cards()
            return
        if self._active_tab == 'recorder':
            self._render_recorder_tab()
            return
        if self._active_tab == 'gif':
            self._render_gif_tab()
            return
        if self._active_tab == 'ask':
            self._render_ask_tab()
            return
        if self._active_tab == 'web':
            self._render_web_tab()
            return
        if self._active_tab == 'chains':
            self._render_chains_tab()
            return
        if self._active_tab == 'notes':
            self._render_notes_tab()
            return
        if self._active_tab == 'whiteboard':
            self._render_whiteboard_tab()
            return
        if self._active_tab == 'transcribe':
            self._render_transcribe_tab()
            return
        if self._active_tab == 'audio_editor':
            self._render_audio_editor_tab()
            return
        if self._active_tab.startswith('slot'):
            self._render_slot_tab(self._active_tab)
            return

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
        # Cards are fixed-width (CARD_W); don't give columns weight=1 so they
        # don't stretch.  Reset any extra weights from a previous tab render.
        for _c in range(cols + 1):
            self._scroll.columnconfigure(_c, weight=0)

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

        # ── Root cards (no folder), displayed first ──────────────────────────
        root_cards_widgets: list = []
        for card_pos_in_group, orig_i in enumerate(root_group):
            card_pos = len(self._cards)
            p = self.prompts[orig_i]
            col = card_pos_in_group % cols
            row = current_row + card_pos_in_group // cols
            card = self._make_card(card_pos, orig_i, p)
            card.grid(row=row, column=col, padx=8, pady=8, sticky='n')
            self._cards.append(card)
            root_cards_widgets.append(card)
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

        # ── Folder groups, displayed below root as card-sized folder widgets ─
        reflow_folders: list = []
        for fname in self._folders:
            group = folder_groups[fname]
            visible_count = len(group)
            all_count     = all_folder_counts.get(fname, 0)

            fcard = self._make_folder_card(fname, visible_count, all_count)
            fcard.grid(row=current_row, column=0, padx=8, pady=(8, 2), sticky='n')
            self._folder_headers[fname] = fcard
            current_row += 1

            folder_card_widgets: list = []
            if fname not in self._collapsed_folders:
                for card_pos_in_group, orig_i in enumerate(group):
                    card_pos = len(self._cards)
                    p   = self.prompts[orig_i]
                    col = card_pos_in_group % cols
                    row = current_row + card_pos_in_group // cols
                    card = self._make_card(card_pos, orig_i, p)
                    card.grid(row=row, column=col, padx=8, pady=8, sticky='n')
                    self._cards.append(card)
                    folder_card_widgets.append(card)
                if group:
                    current_row += math.ceil(len(group) / cols)
            reflow_folders.append((fname, fcard, folder_card_widgets))

        # Save layout state so _reflow_cards() can reposition without recreating
        self._reflow_root_cards = root_cards_widgets[:]
        self._reflow_sep_widget = self._sep_widget
        self._reflow_folders    = reflow_folders

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

        # Re-bind right-click on scroll canvas, card widgets added above can
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

        # ── Folder tab row, mimics the raised tab on a manila folder ─────────
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

        # ── Bindings, toggle on click, menu on right-click ───────────────────
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
        card_pos , position in the filtered/displayed card list (used for drag-and-drop)
        orig_i   , index into self.prompts (used for all data operations)
        """
        color = prompt.get('color', CARD_COLORS[orig_i % len(CARD_COLORS)])
        outer = ctk.CTkFrame(self._scroll, fg_color=color, corner_radius=RADIUS,
                             border_width=2, border_color=BG,
                             width=CARD_W, height=CARD_H)
        outer.pack_propagate(False)   # lock height, card never grows with content
        outer.grid_propagate(False)

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

        preview = prompt.get('prompt', '')
        preview_lbl = ctk.CTkLabel(outer, text=preview, font=(FONT_FAMILY, 11),
                                   text_color=CARD_TEXT_S, anchor='nw',
                                   wraplength=CARD_W - 24, justify='left')

        preview_lbl.pack(fill='both', expand=True, padx=12, pady=(0, 12))

        # ✕ button, placed AFTER pack() children so it renders on top
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
        refine_hk = self.hotkey_cfg.get('refine', 'alt+shift+w').upper()
        self._active_lbl.configure(text=f'{refine_hk}  →  {self.prompts[orig_i]["title"]}')

    def _edit(self, orig_i: int) -> None:
        reserved = {v.strip().lower() for v in self.hotkey_cfg.values() if v} | {'ctrl+z'}
        dlg = EditDialog(self.win, self.prompts[orig_i],
                         on_hotkey_suspend=self._on_hotkey_suspend,
                         on_hotkey_resume=self._on_hotkey_resume,
                         reserved_hotkeys=reserved,
                         vision_extractor=self._vision_extractor)
        self.win.wait_window(dlg)
        if dlg.result:
            updated = dict(self.prompts[orig_i])
            updated.update(dlg.result)
            # Hotkey: '' means the user cleared it, remove the key entirely
            if not updated.get('hotkey'):
                updated.pop('hotkey', None)
            self.prompts[orig_i] = updated
            self.on_save(self.prompts)
            self._render_cards()
            self._select(orig_i)

    def _add(self) -> None:
        reserved = {v.strip().lower() for v in self.hotkey_cfg.values() if v} | {'ctrl+z'}
        dlg = EditDialog(self.win,
                         on_hotkey_suspend=self._on_hotkey_suspend,
                         on_hotkey_resume=self._on_hotkey_resume,
                         reserved_hotkeys=reserved,
                         vision_extractor=self._vision_extractor)
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
                    return   # everything fits, nothing to scroll
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
        # Always resume, if on_save is also called below it will re-register
        # again, but the pending-flag loop handles that gracefully
        if self._on_hotkey_resume:
            self._on_hotkey_resume()
        if dlg.result is None:
            return   # cancelled, no change

        new_hk = dlg.result  # '' = clear, str = assign

        # Centralised conflict validator, checks syntax, OS-reserved,
        # collisions across app/prompts/chains/macros, and surfaces
        # whiteboard / risky-key warnings.
        if new_hk:
            from hotkey_validator import (
                validate_hotkey, collect_app_hotkeys, normalize_hotkey,
                ERROR, WARN)
            this_action = f'prompt:{self.prompts[orig_i].get("title", "Prompt")}'
            others = collect_app_hotkeys(
                {'hotkeys': dict(self.hotkey_cfg or {})},
                prompts=self.prompts, chains=getattr(self, 'chains', None),
                macros=getattr(self, '_macros_for_validate', None),
            )
            others.pop(this_action, None)  # don't compare against ourselves
            diag = validate_hotkey(new_hk, this_action, other_assignments=others)
            if diag.severity == ERROR:
                # Special-case prompt↔prompt collisions so we keep the
                # existing "reassign?" UX (move the binding from the other
                # prompt). Other ERROR types (OS-reserved, app/chain/macro
                # collision) just block.
                conflict_i = next(
                    (i for i, p in enumerate(self.prompts)
                     if i != orig_i and
                        normalize_hotkey(p.get('hotkey', '')) ==
                        normalize_hotkey(new_hk)),
                    None,
                )
                if conflict_i is not None:
                    conflict_title = self.prompts[conflict_i].get(
                        'title', f'Prompt {conflict_i + 1}')
                    this_title = self.prompts[orig_i].get(
                        'title', f'Prompt {orig_i + 1}')
                    ok = confirm(
                        self.win, 'Hotkey already in use',
                        f'"{new_hk.upper()}" is already assigned to '
                        f'"{conflict_title}".\n\nReassign it to '
                        f'"{this_title}"?', action_label='Reassign')
                    if not ok:
                        return
                    old = dict(self.prompts[conflict_i])
                    old.pop('hotkey', None)
                    self.prompts[conflict_i] = old
                else:
                    alert(self.win, 'Hotkey conflict', diag.message)
                    return
            elif diag.severity == WARN:
                if not confirm(self.win, 'Conflicts with common shortcuts',
                               diag.message + '\n\nAssign anyway?'):
                    return

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
            # Low threshold, feels immediately responsive like Windows
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
            # ── Dropped in empty space, check if in the root zone ────────────
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
            # Plain click, select by original index
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
                card.configure(border_color=BORDER2, border_width=2)   # dimmed, being lifted
            elif i == self._drag_over:
                card.configure(border_color=ACCENT, border_width=3)       # target slot
            elif i == active_card_pos:
                card.configure(border_color=ACCENTL, border_width=3)
            else:
                card.configure(border_color=BG, border_width=2)
        # Highlight folder card, strong ACCENT glow when it's the drop target
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

        # Non-empty, three-button dialog
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
            fg_color=ERR, hover_color='#d04040',
            text_color=TEXT_P, font=(FONT_FAMILY, 13),
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
                # Deleting this folder would remove every prompt, block it.
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

    # ── Tab switching ─────────────────────────────────────────────────────────

    def _sync_hint_bar(self) -> None:
        """Update the hint bar to reflect the current tab + state."""
        if not hasattr(self, '_hint_lbl'):
            return
        if self._active_tab == 'macros':
            _mhk = self.hotkey_cfg.get('macro_record', 'shift+f1').upper()
            state = self._macro_state
            if state == 'recording':
                text = f'Recording…  {_mhk} to stop  ·  Esc to discard'
            elif state == 'ready':
                text = f'Recording saved  ·  {_mhk} to play back  ·  Esc to discard'
            elif state == 'playing':
                text = 'Playing macro…  Esc to stop'
            else:
                text = f'{_mhk} to start recording a macro sequence'
            self._hint_lbl.configure(text=text)
        elif self._active_tab == 'recorder':
            self._hint_lbl.configure(text=self._hint_recorder_text)
        elif self._active_tab == 'gif':
            self._hint_lbl.configure(text=self._hint_gif_text)
        elif self._active_tab == 'ask':
            self._hint_lbl.configure(text=self._hint_ask_text)
        elif self._active_tab == 'web':
            self._hint_lbl.configure(text=self._hint_web_text)
        elif self._active_tab == 'chains':
            self._hint_lbl.configure(text=self._hint_chains_text)
        elif self._active_tab == 'notes':
            self._hint_lbl.configure(text=self._hint_notes_text)
        elif self._active_tab == 'whiteboard':
            self._hint_lbl.configure(text=self._hint_whiteboard_text)
        elif self._active_tab == 'transcribe':
            self._hint_lbl.configure(text=self._hint_transcribe_text)
        elif self._active_tab in self._hint_slot_text:
            self._hint_lbl.configure(text=self._hint_slot_text[self._active_tab])
        else:
            self._hint_lbl.configure(text=self._hint_prompts_text)

    def _switch_tab(self, tab: str) -> None:
        # Diagnostic: log every tab-switch click so we can correlate user-
        # reported "tab got stuck" reports with exactly what the code did.
        _log.info(f'[TAB] _switch_tab({tab!r}) called  '
                  f'active={self._active_tab!r}  '
                  f'built={sorted(self._tab_built)}')
        # Bail out fast on no-op clicks.
        if tab == self._active_tab and tab in self._tab_built:
            _log.info(f'[TAB] _switch_tab: no-op (same tab, already built)')
            return

        prev = self._active_tab
        self._active_tab = tab

        # ── 1. Tab button colours: only flip the two that changed ─────────────
        # CTk's configure() is expensive (color tag recompute + repaint).
        # Looping over all 12 buttons used to cost ~300-500ms per switch.
        # The only buttons whose visual state actually changes on a switch
        # are the previously-active one (loses accent) and the new one
        # (gains accent). Touching just those two is ~10× faster.
        if prev in self._tab_btns and prev != tab:
            try: self._tab_btns[prev].configure(
                fg_color=SURF2, hover_color=SURF3)
            except Exception: pass
        if tab in self._tab_btns:
            try: self._tab_btns[tab].configure(
                fg_color=ACCENT, hover_color=ACCENTL)
            except Exception: pass

        # ── 2. Search bar: repack only when crossing the prompts↔other line ──
        # Going from macros to notes etc shouldn't touch the search bar at
        # all. pack/forget on every switch was an unnecessary relayout cost.
        if self._search_bar_frame:
            prev_was_prompts = (prev == 'prompts')
            new_is_prompts   = (tab == 'prompts')
            if prev_was_prompts != new_is_prompts:
                if new_is_prompts:
                    self._scroll.pack_forget()
                    self._search_bar_frame.pack(fill='x', padx=0, pady=0)
                    self._scroll.pack(fill='both', expand=True, padx=PAD, pady=PAD)
                    try:
                        current = self._search_var.get()
                        self._search_entry.delete(0, 'end')
                        if current:
                            self._search_entry.insert(0, current)
                    except Exception:
                        pass
                else:
                    self._search_bar_frame.pack_forget()

        # ── 3. Save the scroll position of the tab we're leaving ─────────────
        try:
            self._tab_scroll_pos[prev] = self._scroll._parent_canvas.yview()[0]
        except Exception:
            pass

        # ── 4. Cache-aware swap to the new tab (already fast) ────────────────
        is_first_build = (tab not in self._tab_built)
        self._show_active_tab()

        # ── 5. Hint bar update deferred to next idle cycle ───────────────────
        # The user's eye is on the content area, not the hint bar. Deferring
        # this lets the visual swap paint first, then the hint catches up
        # without blocking. Drops perceived latency on the swap itself.
        try:
            self._scroll.after_idle(self._sync_hint_bar)
        except Exception:
            self._sync_hint_bar()

        # ── 6. Scroll position: fresh build → top, cached → restore ──────────
        # update_idletasks forces a synchronous layout flush, ~50-100ms.
        # We only need it when the content is brand new (canvas scrollregion
        # hasn't seen these widgets yet). For cached tabs the layout is
        # already valid, so we skip the flush entirely.
        try:
            cv = self._scroll._parent_canvas
            if is_first_build:
                self._scroll.update_idletasks()
                cv.yview_moveto(0)
            else:
                saved = self._tab_scroll_pos.get(tab, 0)
                cv.yview_moveto(saved)
        except Exception:
            pass

    # ── Macro card rendering ──────────────────────────────────────────────────

    def _render_macro_cards(self) -> None:
        """Render macro cards into the scroll frame."""
        if self._render_tab_guard('macros'):
            return
        for w in self._scroll.winfo_children():
            w.destroy()
        self._macro_cards.clear()
        self._cards.clear()
        self._folder_headers.clear()

        if self._macro_library is None:
            ctk.CTkLabel(
                self._scroll,
                text='Macro library not available.',
                font=(FONT_FAMILY, 13), text_color=TEXT_S,
            ).grid(row=0, column=0, pady=40)
            return

        macros = self._macro_library.macros
        cols = self._current_cols
        # Reset any extra column weights from a previous tab render before
        # configuring only the columns this tab actually uses.
        for _c in range(cols + 1):
            self._scroll.columnconfigure(_c, weight=0)
        for c in range(cols):
            self._scroll.columnconfigure(c, weight=1)

        # ── Open Folder button ────────────────────────────────────────────────────
        def _open_macros_folder():
            import ctypes as _ct2, threading as _th2
            p = Path(appdata_dir()) / 'macros'
            p.mkdir(parents=True, exist_ok=True)
            _th2.Thread(
                target=lambda: _ct2.windll.shell32.ShellExecuteW(
                    0, 'explore', str(p), None, None, 1),
                daemon=False).start()
        _folder_row = ctk.CTkFrame(self._scroll, fg_color='transparent')
        _folder_row.grid(row=0, column=0, columnspan=cols, padx=4, pady=(4, 0), sticky='e')
        ctk.CTkButton(
            _folder_row, text='📁  Open Folder', width=120, height=26,
            fg_color=SURF2, hover_color=SURF3, text_color=TEXT_S,
            corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
            command=_open_macros_folder,
        ).pack(side='right')

        # Bulk delete — only render when there's at least one macro to delete.
        # The button sits to the LEFT of Open Folder so the destructive action
        # is visually separated from the navigation one.
        if macros and self._macro_library is not None:
            def _delete_all_macros():
                n = len(self._macro_library.macros)
                if n == 0:
                    return
                if not confirm(self.win, 'Delete all macros',
                               f'Delete all {n} saved macros?\n\n'
                               'This cannot be undone.',
                               action_label='Delete all',
                               action_color='#b03030',
                               action_hover='#d04040'):
                    return
                # Snapshot ids so iteration isn't affected by deletes
                ids = [m['id'] for m in list(self._macro_library.macros)]
                for mid in ids:
                    try:
                        self._macro_library.delete(mid)
                    except Exception as e:
                        _log.warning(f'delete_all_macros: {mid}: {e}')
                self._render_cards()
            ctk.CTkButton(
                _folder_row, text='🗑  Delete all', width=110, height=26,
                fg_color=SURF2, hover_color=ERR, text_color=TEXT_S,
                corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                command=_delete_all_macros,
            ).pack(side='right', padx=(0, 6))

        # ── Active-session banner (shown when a recording/playback is in progress) ──
        if self._macro_state != 'idle':
            state_labels = {
                'recording': ('⏺  Recording in progress…', ERR),
                'ready':     ('⏹  Recording stopped, ready to play', ACCENT),
                'playing':   ('▶  Playback in progress…', OK),
            }
            label_text, label_color = state_labels.get(
                self._macro_state, ('…', TEXT_S))

            banner = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
            banner.grid(row=1, column=0, columnspan=cols, padx=8, pady=(4, 4), sticky='ew')

            ctk.CTkLabel(banner, text=label_text,
                         font=(FONT_FAMILY, 12, 'bold'),
                         text_color=label_color).pack(side='left', padx=PAD, pady=PAD_SM)

            def _do_reset():
                if self._on_macro_reset:
                    self._on_macro_reset()
            ctk.CTkButton(
                banner, text='↺  Discard & start fresh',
                fg_color=SURF2, hover_color=SURF3, text_color=TEXT_S,
                corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                width=160, height=26,
                command=_do_reset,
            ).pack(side='right', padx=PAD, pady=PAD_SM)

            # Offset saved macro cards below the folder row + banner row
            grid_row_offset = 2
        else:
            # Offset saved macro cards below the folder row
            grid_row_offset = 1

        if not macros:
            card = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
            card.grid(row=grid_row_offset, column=0, columnspan=cols, padx=32, pady=32, sticky='ew')

            # Icon + title
            ctk.CTkLabel(card, text='⏺', font=(FONT_FAMILY, 28),
                         text_color=ACCENT).pack(pady=(PAD_LG, 2))
            ctk.CTkLabel(card, text='No macros saved yet',
                         font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P).pack()
            ctk.CTkLabel(card,
                         text='Record any sequence of mouse moves, clicks, and key presses, then replay it any time.',
                         font=(FONT_FAMILY, 12), text_color=TEXT_S,
                         wraplength=400, justify='center').pack(pady=(4, PAD))

            # Divider
            ctk.CTkFrame(card, fg_color=SURF2, height=1,
                         corner_radius=0).pack(fill='x', padx=PAD)

            # Step-by-step instructions
            steps_frame = ctk.CTkFrame(card, fg_color='transparent')
            steps_frame.pack(padx=PAD_LG, pady=PAD, anchor='w')

            _mhk2 = self.hotkey_cfg.get('macro_record', 'shift+f1').upper()
            steps = [
                ('1', f'Press  {_mhk2}  to start recording'),
                ('2', 'Do your actions, mouse, clicks, or typing'),
                ('3', f'Press  {_mhk2}  again to stop'),
                ('4', f'Press  {_mhk2}  once more to play it back'),
                ('5', 'Choose  Save  to keep it here with a name & hotkey'),
            ]
            for num, desc in steps:
                row = ctk.CTkFrame(steps_frame, fg_color='transparent')
                row.pack(fill='x', pady=3)
                badge = ctk.CTkFrame(row, fg_color=ACCENT, corner_radius=10,
                                     width=22, height=22)
                badge.pack(side='left', padx=(0, 10))
                badge.pack_propagate(False)
                ctk.CTkLabel(badge, text=num,
                             font=(FONT_FAMILY, 10, 'bold'),
                             text_color='#ffffff').pack(expand=True)
                ctk.CTkLabel(row, text=desc,
                             font=(FONT_FAMILY, 12), text_color=TEXT_P,
                             anchor='w').pack(side='left')

            # Right-click hint
            ctk.CTkFrame(card, fg_color=SURF2, height=1,
                         corner_radius=0).pack(fill='x', padx=PAD)
            ctk.CTkLabel(card,
                         text='Tip: right-click anywhere in this tab to start recording',
                         font=(FONT_FAMILY, 11), text_color=TEXT_S).pack(pady=(PAD_SM, PAD))

            # Propagate right-click through every widget in the card so the
            # context menu fires regardless of where the user clicks.
            def _bind_rc(w):
                w.bind('<Button-3>', self._on_bg_right_click, add=True)
                for child in w.winfo_children():
                    _bind_rc(child)
            card.after(50, lambda: _bind_rc(card))
            return

        for idx, m in enumerate(macros):
            col  = idx % cols
            row  = idx // cols + grid_row_offset
            card = self._make_macro_card(m)
            card.grid(row=row, column=col, padx=8, pady=8, sticky='nsew')
            self._macro_cards.append(card)

    def _make_macro_card(self, m: dict) -> ctk.CTkFrame:
        """Build and return a single macro card widget."""
        mid  = m['id']
        hk   = m.get('hotkey', '').strip()

        # Parse saved_at for display
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(m['saved_at'])
            if dt.year == _dt.now().year:
                date_str = dt.strftime('%b %-d') if hasattr(dt, 'strftime') else dt.strftime('%b %d').lstrip('0')
            else:
                date_str = dt.strftime('%b %d, %Y')
            # strftime day without zero-pad: use %#d on Windows
            try:
                date_str = dt.strftime('%b %#d') if dt.year == _dt.now().year else dt.strftime('%b %#d, %Y')
            except Exception:
                date_str = dt.strftime('%b %d').lstrip('0') if dt.year == _dt.now().year else dt.strftime('%b %d, %Y')
        except Exception:
            date_str = m.get('saved_at', '')[:10]

        subtitle = f'{m["duration"]:.1f}s · {m["event_count"]} events · {date_str}'

        outer = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS,
                             border_width=2, border_color=BORDER)

        # Name label
        name_lbl = ctk.CTkLabel(
            outer, text=m['name'],
            font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_P,
            anchor='w', wraplength=CARD_W - 48, justify='left',
        )
        name_lbl.pack(anchor='w', fill='x', padx=12, pady=(12, 2))

        # Subtitle
        ctk.CTkLabel(
            outer, text=subtitle,
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
            anchor='w',
        ).pack(anchor='w', padx=12, pady=(0, 6))

        # Hotkey badge
        if hk:
            hk_badge = ctk.CTkLabel(
                outer,
                text=f'  ⌨  {hk.upper()}  ',
                font=(FONT_FAMILY, 10), text_color=ACCENTL,
                fg_color=SURF2, corner_radius=RADIUS_SM,
            )
        else:
            hk_badge = ctk.CTkLabel(
                outer,
                text='  ⌨  None  ',
                font=(FONT_FAMILY, 10), text_color=TEXT_S,
                fg_color=SURF2, corner_radius=RADIUS_SM,
            )
        hk_badge.pack(anchor='w', padx=12, pady=(0, 8))

        # Play button (bottom-right area)
        play_btn = ctk.CTkButton(
            outer, text='▶  Play', width=72,
            fg_color=OK, hover_color='#1a9e4a',
            text_color=TEXT_P, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 12),
            command=lambda meta=m: self._play_macro(meta),
        )
        play_btn.pack(anchor='e', padx=12, pady=(0, 10))

        # Delete button (top-right corner, placed after other widgets)
        del_btn = ctk.CTkButton(
            outer, text='✕', width=26, height=26,
            fg_color=SURF3, hover_color=ERR,
            text_color=TEXT_S, font=(FONT_FAMILY, 13, 'bold'), corner_radius=13,
            command=lambda meta=m: self._delete_macro(meta['id'], meta['name']),
        )
        del_btn.place(relx=1.0, rely=0.0, anchor='ne', x=-6, y=6)
        del_btn.lift()
        del_btn.bind('<Button-1>', lambda e: 'break')

        # Right-click context menu
        def _show_menu(e, meta=m):
            menu = tk.Menu(self.win, tearoff=0,
                           bg=SURFACE, fg=TEXT_P, activebackground=ACCENT,
                           activeforeground='#ffffff', bd=0,
                           font=(FONT_FAMILY, 12))
            menu.add_command(label='✏  Rename',
                             command=lambda: self._rename_macro_inline(meta['id'], name_lbl))
            menu.add_command(label='⌨  Assign hotkey…',
                             command=lambda: self._assign_macro_hotkey(meta['id']))
            menu.add_separator()
            def _open_location(mid=meta['id']):
                import os, subprocess, threading
                file_path = str(self._macro_library._folder / f'{mid}.json')
                def _do():
                    try:
                        # /select highlights the specific file; works on most systems.
                        # CREATE_NO_WINDOW + close_fds match PROJECT.md's
                        # subprocess spawn rule (rule #12).
                        flags = (subprocess.CREATE_NO_WINDOW
                                 if sys.platform == 'win32' else 0)
                        subprocess.Popen(
                            ['explorer', f'/select,{file_path}'],
                            creationflags=flags,
                            close_fds=True,
                        )
                    except Exception:
                        # Fallback: just open the containing folder
                        os.startfile(str(self._macro_library._folder))
                threading.Thread(target=_do, daemon=True).start()
            menu.add_command(label='📂  Open file location', command=_open_location)
            menu.add_separator()
            menu.add_command(label='✕  Delete',
                             command=lambda: self._delete_macro(meta['id'], meta['name']))
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()
            return 'break'

        # Click on name = inline rename
        def _name_click(e, meta=m, nl=name_lbl):
            self._rename_macro_inline(meta['id'], nl)

        name_lbl.bind('<Button-1>', _name_click)

        for w in (outer, hk_badge):
            w.bind('<Button-3>', _show_menu)

        return outer

    def _play_macro(self, meta: dict) -> None:
        """Load and play a macro by its metadata dict."""
        if self._on_macro_play:
            self._on_macro_play(meta)

    def _rename_macro_inline(self, mid: str, name_lbl: ctk.CTkLabel) -> None:
        """Replace name label with an inline entry for rename."""
        old_name = name_lbl.cget('text')
        entry_var = tk.StringVar(value=old_name)

        entry = ctk.CTkEntry(
            name_lbl.master, textvariable=entry_var, width=200,
            fg_color=SURF2, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 13, 'bold'),
            corner_radius=RADIUS_SM,
        )
        # Hide name label and show entry in its place
        name_lbl.pack_forget()
        entry.pack(anchor='w', fill='x', padx=12, pady=(12, 2))
        entry.focus_set()
        entry.select_range(0, 'end')

        committed: list[bool] = [False]

        def _commit(e=None):
            if committed[0]:
                return
            committed[0] = True
            new_name = entry_var.get().strip()
            if not new_name:
                new_name = old_name
            if self._macro_library:
                self._macro_library.rename(mid, new_name)
            entry.destroy()
            self._render_cards()

        entry.bind('<Return>',    _commit)
        entry.bind('<FocusOut>',  _commit)
        entry.bind('<Escape>',    lambda e: _commit())

    def _assign_macro_hotkey(self, mid: str) -> None:
        """Open HotkeyCapture; assign result to macro."""
        # Find current hotkey
        current = ''
        if self._macro_library:
            for m in self._macro_library.macros:
                if m['id'] == mid:
                    current = m.get('hotkey', '')
                    break

        if self._on_hotkey_suspend:
            self._on_hotkey_suspend()
        dlg = HotkeyCapture(self.win, current_hotkey=current)
        self.win.wait_window(dlg)
        if self._on_hotkey_resume:
            self._on_hotkey_resume()

        if dlg.result is None:
            return   # cancelled

        new_hk = dlg.result
        if new_hk:
            try:
                from hotkey_validator import (
                    validate_hotkey, collect_app_hotkeys, ERROR, WARN)
                macros_list = (self._macro_library.macros
                               if self._macro_library else None)
                others = collect_app_hotkeys(
                    {'hotkeys': dict(self.hotkey_cfg or {})},
                    prompts=self.prompts,
                    chains=getattr(self, 'chains', None),
                    macros=macros_list,
                )
                this_name = next((m.get('name', mid)
                                  for m in (macros_list or [])
                                  if m['id'] == mid), mid)
                this_action = f'macro:{this_name}'
                others.pop(this_action, None)
                diag = validate_hotkey(new_hk, this_action,
                                       other_assignments=others)
                if diag.severity == ERROR:
                    alert(self.win, 'Hotkey conflict', diag.message)
                    return
                if diag.severity == WARN:
                    if not confirm(self.win, 'Conflicts with common shortcuts',
                                   diag.message + '\n\nAssign anyway?'):
                        return
            except Exception:
                pass

        if self._macro_library:
            self._macro_library.assign_hotkey(mid, new_hk)
        if self._on_macro_hotkeys_changed:
            self._on_macro_hotkeys_changed()
        self._render_cards()

    def _delete_macro(self, mid: str, name: str) -> None:
        """Confirm then delete macro."""
        if confirm(self.win, 'Delete macro',
                   f'Delete "{name}"?',
                   action_label='Delete',
                   action_color='#b03030', action_hover='#d04040'):
            if self._macro_library:
                self._macro_library.delete(mid)
            self._render_cards()

    def refresh_macros(self) -> None:
        """Public method, invalidates macros tab cache so the next visit
        re-renders with current data. Uses _invalidate_tab (which writes to
        the per-tab container) instead of calling _render_macro_cards
        directly (which would write to raw self._scroll, missing the cache)."""
        self._invalidate_tab('macros')

    # ── Recorder tab ─────────────────────────────────────────────────────────

    def _render_recorder_tab(self) -> None:
        """Render the Recorder tab contents inside self._scroll."""
        if self._render_tab_guard('recorder'):
            return
        # Cancel any existing live ticker from a previous render
        if self._rec_tab_ticker is not None:
            try:
                self.win.after_cancel(self._rec_tab_ticker)
            except Exception:
                pass
            self._rec_tab_ticker = None

        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        state = self._recorder_state

        # ── Top control card ──────────────────────────────────────────────────
        # Reset any extra column weights left by a previous Prompts/Macros render
        # (e.g. if the window was wide enough for 3+ columns those stay weight=1
        # unless explicitly cleared, causing the 2-column recorder content to
        # only occupy 2/N of the available width).
        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        card = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
        card.grid(row=0, column=0, columnspan=2, sticky='ew', padx=8, pady=8)
        self._scroll.columnconfigure(0, weight=1)
        self._scroll.columnconfigure(1, weight=1)

        if state == 'idle':
            ctk.CTkLabel(card, text='🎥  Screen Recording',
                         font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P).pack(pady=(PAD, 4))
            ctk.CTkLabel(
                card,
                text=f'Press  {self.hotkey_cfg.get("recorder", "shift+f2").upper()}  or click Start Recording to capture your screen.\n'
                     'H.264 · AAC audio optional · 1 GB auto-stop',
                font=(FONT_FAMILY, 12), text_color=TEXT_S,
            ).pack(pady=(0, PAD))

            def _start():
                if self._on_recorder_toggle:
                    self._on_recorder_toggle()
            ctk.CTkButton(
                card, text='🎥  Start Recording',
                fg_color=ACCENT, hover_color=ACCENTL,
                text_color=TEXT_P, font=(FONT_FAMILY, 13, 'bold'),
                width=180, height=36, corner_radius=RADIUS_SM,
                command=_start,
            ).pack(pady=(0, PAD))

        elif state == 'recording':
            # Live timer label, updated by ticker
            ctk.CTkLabel(card, text='⏺  Recording…',
                         font=(FONT_FAMILY, 13, 'bold'), text_color=ERR).pack(pady=(PAD, 2))
            self._rec_time_lbl = ctk.CTkLabel(
                card, text='00:00',
                font=(FONT_FAMILY, 28, 'bold'), text_color=TEXT_P)
            self._rec_time_lbl.pack()
            self._rec_size_lbl = ctk.CTkLabel(
                card,
                text=f'{self._recorder_size_mb:.1f} MB  /  1,024 MB cap',
                font=(FONT_FAMILY, 11), text_color=TEXT_S)
            self._rec_size_lbl.pack(pady=(0, PAD))

            def _stop():
                if self._on_recorder_toggle:
                    self._on_recorder_toggle()
            ctk.CTkButton(
                card, text='⏹  Stop Recording',
                fg_color=ERR, hover_color='#d04040',
                text_color=TEXT_P, font=(FONT_FAMILY, 13, 'bold'),
                width=180, height=36, corner_radius=RADIUS_SM,
                command=_stop,
            ).pack(pady=(0, PAD))

            # Start live ticker
            self._rec_tab_ticker_fn()

        elif state == 'stopping':
            ctk.CTkLabel(card, text='⏳  Finishing encoding…',
                         font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_S).pack(pady=PAD*2)

        # ── Recent recordings list ────────────────────────────────────────────
        from screen_recorder import list_recordings
        from storage import appdata_dir
        rec_dir = str(Path(appdata_dir()) / 'recordings')
        recordings = list_recordings(rec_dir)

        if recordings:
            sep = ctk.CTkFrame(self._scroll, fg_color=BORDER, height=1, corner_radius=0)
            sep.grid(row=1, column=0, columnspan=2, sticky='ew', padx=8, pady=(4, 0))

            hdr = ctk.CTkFrame(self._scroll, fg_color='transparent')
            hdr.grid(row=2, column=0, columnspan=2, sticky='ew', padx=8)
            ctk.CTkLabel(hdr, text='Recent recordings',
                         font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_S).pack(side='left', pady=4)

            def _open_folder():
                import ctypes, threading
                p = Path(rec_dir)
                p.mkdir(parents=True, exist_ok=True)
                threading.Thread(
                    target=lambda: ctypes.windll.shell32.ShellExecuteW(
                        0, 'explore', str(p), None, None, 1),
                    daemon=False).start()
            ctk.CTkButton(
                hdr, text='📁  Open Folder', width=110, height=22,
                fg_color=SURF2, hover_color=SURF3, text_color=TEXT_S,
                corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                command=_open_folder,
            ).pack(side='right', pady=4)

            # Bulk delete for recordings — iterates the on-disk list, removes
            # each file, prunes the recordings index, then invalidates the
            # tab cache so the rebuild reflects the cleanup.
            def _delete_all_recordings():
                from screen_recorder import (list_recordings as _list_rec,
                                              remove_from_recordings_index
                                              as _rm_rec_idx)
                items = _list_rec(rec_dir)
                if not items:
                    return
                if not confirm(self.win, 'Delete all recordings',
                               f'Delete all {len(items)} recordings from '
                               f'disk?\n\nThis cannot be undone.',
                               action_label='Delete all',
                               action_color='#b03030',
                               action_hover='#d04040'):
                    return
                for r in items:
                    try:
                        os.unlink(r['path'])
                    except Exception as e:
                        _log.warning(f'delete_all_recordings: {r["path"]}: {e}')
                    try:
                        _rm_rec_idx(r['path'])
                    except Exception:
                        pass
                self._invalidate_tab('recorder')
            ctk.CTkButton(
                hdr, text='🗑  Delete all', width=100, height=22,
                fg_color=SURF2, hover_color=ERR, text_color=TEXT_S,
                corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                command=_delete_all_recordings,
            ).pack(side='right', pady=4, padx=(0, 6))

            import datetime as _dt
            for row_i, rec in enumerate(recordings[:10]):
                rcard = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
                rcard.grid(row=3 + row_i, column=0, columnspan=2,
                           sticky='ew', padx=8, pady=4)
                rcard.columnconfigure(0, weight=1)

                # Info
                mtime = _dt.datetime.fromtimestamp(rec['mtime']).strftime('%Y-%m-%d %H:%M')
                ctk.CTkLabel(
                    rcard, text=rec['name'],
                    font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_P,
                    anchor='w',
                ).grid(row=0, column=0, sticky='w', padx=PAD, pady=(PAD_SM, 0))
                ctk.CTkLabel(
                    rcard,
                    text=f'{rec["size_mb"]:.1f} MB  ·  {mtime}',
                    font=(FONT_FAMILY, 11), text_color=TEXT_S,
                    anchor='w',
                ).grid(row=1, column=0, sticky='w', padx=PAD, pady=(0, PAD_SM))

                # Buttons
                btn_row = ctk.CTkFrame(rcard, fg_color='transparent')
                btn_row.grid(row=0, column=1, rowspan=2, padx=PAD, pady=PAD_SM, sticky='e')

                def _play(path=rec['path']):
                    import ctypes, threading
                    threading.Thread(
                        target=lambda: ctypes.windll.shell32.ShellExecuteW(
                            0, 'open', os.path.normpath(path), None, None, 1),
                        daemon=False).start()

                def _del(path=rec['path']):
                    if not confirm(self.win, 'Delete recording',
                                   f'Delete {Path(path).name} from disk?',
                                   action_label='Delete',
                                   action_color='#b03030', action_hover='#d04040'):
                        return
                    try:
                        os.unlink(path)
                    except Exception as _e:
                        alert(self.win, 'Delete failed',
                              f'Could not delete the file:\n{path}\n\n{_e}')
                        return
                    # Prune from the recordings index so it doesn't reappear
                    try:
                        from screen_recorder import remove_from_recordings_index
                        remove_from_recordings_index(path)
                    except Exception:
                        pass
                    # Use _invalidate_tab so the per-tab container gets the
                    # update, not raw self._scroll.
                    self._invalidate_tab('recorder')

                ctk.CTkButton(
                    btn_row, text='▶  Play', width=70, height=26,
                    fg_color=SURF2, hover_color=SURF3, text_color=TEXT_P,
                    corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                    command=_play,
                ).pack(side='left', padx=(0, 4))
                ctk.CTkButton(
                    btn_row, text='✕', width=30, height=26,
                    fg_color=SURF2, hover_color=ERR, text_color=TEXT_S,
                    corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                    command=_del,
                ).pack(side='left')

        else:
            # No recordings exist, show hint regardless of current recorder state
            no_rec = ctk.CTkFrame(self._scroll, fg_color='transparent')
            no_rec.grid(row=1, column=0, columnspan=2, pady=8)
            ctk.CTkLabel(
                no_rec,
                text='No recordings saved yet.',
                font=(FONT_FAMILY, 12), text_color=TEXT_D,
            ).pack()

    def _rec_tab_ticker_fn(self) -> None:
        """Update the live timer in the Recorder tab while recording."""
        if self._active_tab != 'recorder' or self._recorder_state != 'recording':
            self._rec_tab_ticker = None
            return
        try:
            elapsed = self._recorder_elapsed
            m, s = divmod(int(elapsed), 60)
            self._rec_time_lbl.configure(text=f'{m:02d}:{s:02d}')
            self._rec_size_lbl.configure(
                text=f'{self._recorder_size_mb:.1f} MB  /  1,024 MB cap')
        except Exception:
            pass
        self._rec_tab_ticker = self.win.after(500, self._rec_tab_ticker_fn)

    def update_recorder_state(self, state: str,
                               elapsed: float = 0.0,
                               size_mb: float = 0.0) -> None:
        """Called by main.py when recording state changes or ticks.

        IMPORTANT: must go through `_invalidate_tab('recorder')` instead of
        calling `_render_recorder_tab()` directly. The direct render writes
        into `self._scroll`, but per-tab content actually lives in
        `_tab_containers['recorder']`. Direct render = widgets land in the
        wrong place + tab cache stays stale, so a recording saved while
        another tab is active never shows up on the recorder tab.
        """
        self._recorder_state   = state
        self._recorder_elapsed = elapsed
        self._recorder_size_mb = size_mb
        if state in ('idle', 'stopping'):
            # State changed — recorder tab cache is stale, invalidate so
            # next visit (or current view) rebuilds with the new state.
            self._invalidate_tab('recorder')
        elif state == 'recording':
            if self._active_tab == 'recorder':
                if self._rec_tab_ticker is None:
                    self._invalidate_tab('recorder')
                else:
                    try:
                        m, s = divmod(int(elapsed), 60)
                        self._rec_time_lbl.configure(text=f'{m:02d}:{s:02d}')
                        self._rec_size_lbl.configure(
                            text=f'{size_mb:.1f} MB  /  1,024 MB cap')
                    except Exception:
                        pass
            else:
                # Recording started while user is on a different tab.
                # Invalidate so the recorder tab rebuilds with the live state
                # when they switch to it.
                self._invalidate_tab('recorder')

    # ── GIF tab ───────────────────────────────────────────────────────────────

    def update_gif_state(
        self, state: str, elapsed: float = 0.0, frames: int = 0
    ) -> None:
        """Called by main.py whenever GIF recorder state changes.

        Same fix as update_recorder_state: use _invalidate_tab not direct render.
        """
        self._gif_state   = state
        self._gif_elapsed = elapsed
        self._gif_frames  = frames
        if state in ('idle', 'encoding'):
            self._invalidate_tab('gif')
        elif state == 'recording':
            if self._active_tab == 'gif' and self._gif_tab_ticker is not None:
                try:
                    self._gif_time_lbl.configure(
                        text=f'{int(elapsed)}s  ·  {frames} frames')
                except Exception:
                    pass
            else:
                self._invalidate_tab('gif')

    def _render_gif_tab(self) -> None:
        """Render the GIF tab contents inside self._scroll."""
        if self._render_tab_guard('gif'):
            return
        # Cancel any existing ticker
        if self._gif_tab_ticker is not None:
            try:
                self.win.after_cancel(self._gif_tab_ticker)
            except Exception:
                pass
            self._gif_tab_ticker = None

        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        state = self._gif_state

        # Reset column weights
        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        card = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
        card.grid(row=0, column=0, columnspan=2, sticky='ew', padx=8, pady=8)
        self._scroll.columnconfigure(0, weight=1)
        self._scroll.columnconfigure(1, weight=1)

        if state == 'idle':
            ctk.CTkLabel(card, text='🎞  GIF Recorder',
                         font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P).pack(pady=(PAD, 4))
            ctk.CTkLabel(
                card,
                text=f'Capture an animated GIF of your screen.\n'
                     f'Press  {self.hotkey_cfg.get("gif_record", "shift+f3").upper()}  or click Start to begin.',
                font=(FONT_FAMILY, 12), text_color=TEXT_S,
            ).pack(pady=(0, PAD))

            def _start():
                if self._on_gif_toggle:
                    self._on_gif_toggle()
            ctk.CTkButton(
                card, text='🎞  Start GIF',
                fg_color=ACCENT, hover_color='#6d28d9',
                text_color=TEXT_P, font=(FONT_FAMILY, 13, 'bold'),
                width=180, height=36, corner_radius=RADIUS_SM,
                command=_start,
            ).pack(pady=(0, PAD))

        elif state == 'recording':
            ctk.CTkLabel(card, text='🎞  Recording GIF…',
                         font=(FONT_FAMILY, 13, 'bold'), text_color=ACCENT).pack(pady=(PAD, 2))
            self._gif_time_lbl = ctk.CTkLabel(
                card, text=f'{int(self._gif_elapsed)}s  ·  {self._gif_frames} frames',
                font=(FONT_FAMILY, 22, 'bold'), text_color=TEXT_P)
            self._gif_time_lbl.pack()
            ctk.CTkLabel(
                card, text='Esc to abort  ·  auto-stops at max duration',
                font=(FONT_FAMILY, 11), text_color=TEXT_S).pack(pady=(0, PAD))

            def _stop():
                if self._on_gif_toggle:
                    self._on_gif_toggle()
            ctk.CTkButton(
                card, text='⏹  Stop & Save',
                fg_color=ACCENT, hover_color=ACCENTL,
                text_color=TEXT_P, font=(FONT_FAMILY, 13, 'bold'),
                width=180, height=36, corner_radius=RADIUS_SM,
                command=_stop,
            ).pack(pady=(0, PAD))

            # Live ticker, update elapsed/frames every second
            def _tick():
                if self._active_tab != 'gif' or self._gif_state != 'recording':
                    self._gif_tab_ticker = None
                    return
                try:
                    self._gif_time_lbl.configure(
                        text=f'{int(self._gif_elapsed)}s  ·  {self._gif_frames} frames')
                except Exception:
                    pass
                self._gif_tab_ticker = self.win.after(500, _tick)

            self._gif_tab_ticker = self.win.after(500, _tick)

        elif state == 'encoding':
            ctk.CTkLabel(card, text='⏳  Encoding GIF…',
                         font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_S).pack(pady=PAD * 2)

        # ── Recent GIFs ───────────────────────────────────────────────────────
        from storage import appdata_dir as _appdata_dir
        from gif_recorder import list_gifs as _list_gifs, remove_from_gif_index as _rm_gif_idx
        gif_dir = Path(_appdata_dir()) / 'gifs'
        gif_dir.mkdir(parents=True, exist_ok=True)
        gifs = _list_gifs(str(gif_dir))[:10]

        if gifs:
            sep = ctk.CTkFrame(self._scroll, fg_color=BORDER, height=1, corner_radius=0)
            sep.grid(row=1, column=0, columnspan=2, sticky='ew', padx=8, pady=(4, 0))

            hdr = ctk.CTkFrame(self._scroll, fg_color='transparent')
            hdr.grid(row=2, column=0, columnspan=2, sticky='ew', padx=8)
            ctk.CTkLabel(hdr, text='Saved GIFs',
                         font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_S).pack(side='left', pady=4)

            def _open_gif_folder():
                import ctypes as _ct
                threading.Thread(
                    target=lambda: _ct.windll.shell32.ShellExecuteW(
                        0, 'explore', str(gif_dir), None, None, 1),
                    daemon=False).start()
            ctk.CTkButton(
                hdr, text='📁  Open Folder', width=110, height=22,
                fg_color=SURF2, hover_color=SURF3, text_color=TEXT_S,
                corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                command=_open_gif_folder,
            ).pack(side='right', pady=4)

            # Bulk delete for saved GIFs — iterates the list, removes each
            # file, prunes the gif index, then invalidates the tab cache.
            def _delete_all_gifs():
                items = _list_gifs(str(gif_dir))
                if not items:
                    return
                if not confirm(self.win, 'Delete all GIFs',
                               f'Delete all {len(items)} GIFs from disk?'
                               f'\n\nThis cannot be undone.',
                               action_label='Delete all',
                               action_color='#b03030',
                               action_hover='#d04040'):
                    return
                for g in items:
                    p = Path(g['path'])
                    try:
                        p.unlink(missing_ok=True)
                    except Exception as e:
                        _log.warning(f'delete_all_gifs: {p}: {e}')
                    try:
                        _rm_gif_idx(str(p))
                    except Exception:
                        pass
                self._invalidate_tab('gif')
            ctk.CTkButton(
                hdr, text='🗑  Delete all', width=100, height=22,
                fg_color=SURF2, hover_color=ERR, text_color=TEXT_S,
                corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                command=_delete_all_gifs,
            ).pack(side='right', pady=4, padx=(0, 6))

            import datetime as _dt
            for row_i, gif_info in enumerate(gifs):
                gif_path = Path(gif_info['path'])
                row_frame = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
                row_frame.grid(row=row_i + 3, column=0, columnspan=2,
                               sticky='ew', padx=8, pady=2)
                row_frame.columnconfigure(0, weight=1)

                ts = _dt.datetime.fromtimestamp(gif_info['mtime']).strftime('%d %b %Y  %H:%M')

                info_col = ctk.CTkFrame(row_frame, fg_color='transparent')
                info_col.grid(row=0, column=0, sticky='w', padx=PAD, pady=PAD_SM)
                ctk.CTkLabel(info_col, text=gif_path.name,
                             font=(FONT_FAMILY, 13), text_color=TEXT_P).pack(anchor='w')
                ctk.CTkLabel(info_col, text=f'{ts}  ·  {gif_info["size_kb"]:.0f} KB',
                             font=(FONT_FAMILY, 11), text_color=TEXT_S).pack(anchor='w')

                btn_row = ctk.CTkFrame(row_frame, fg_color='transparent')
                btn_row.grid(row=0, column=1, sticky='e', padx=PAD, pady=PAD_SM)

                def _open(_p=gif_path):
                    threading.Thread(
                        target=lambda: os.startfile(str(_p)),
                        daemon=False).start()

                def _del(_p=gif_path, _row=row_frame):
                    from dialogs import confirm as _confirm
                    if _confirm(self.win, 'Delete GIF?',
                                f'Delete "{_p.name}"? This cannot be undone.'):
                        try:
                            _p.unlink(missing_ok=True)
                        except Exception:
                            pass
                        _rm_gif_idx(str(_p))
                        _row.destroy()

                ctk.CTkButton(
                    btn_row, text='▶  Open', width=72, height=26,
                    fg_color=SURF2, hover_color=SURF3, text_color=TEXT_P,
                    corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                    command=_open,
                ).pack(side='left', padx=(0, 4))
                ctk.CTkButton(
                    btn_row, text='✕', width=30, height=26,
                    fg_color=SURF2, hover_color=ERR, text_color=TEXT_S,
                    corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11),
                    command=_del,
                ).pack(side='left')

        else:
            no_gif = ctk.CTkFrame(self._scroll, fg_color='transparent')
            no_gif.grid(row=1, column=0, columnspan=2, pady=8)
            ctk.CTkLabel(
                no_gif, text='No GIFs saved yet.',
                font=(FONT_FAMILY, 12), text_color=TEXT_D,
            ).pack()

    def _render_ask_tab(self) -> None:
        """Render the Ask tab, type a question or use Shift+F4 on selected text."""
        if self._render_tab_guard('ask'):
            return
        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        self._scroll.columnconfigure(0, weight=1)

        # ── Main card ─────────────────────────────────────────────────────────
        card = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
        card.grid(row=0, column=0, sticky='ew', padx=8, pady=8)
        card.columnconfigure(0, weight=1)

        def _blur_textbox(e=None):
            """Clicking anywhere on the card outside the textbox steals focus,
            which triggers FocusOut on the textbox and restores the placeholder."""
            self.win.focus_set()

        card.bind('<ButtonPress-1>', _blur_textbox, add='+')
        # Also bind on the scroll area so clicking below the card works too
        self._scroll.bind('<ButtonPress-1>', _blur_textbox, add='+')

        ctk.CTkLabel(
            card, text='✦  Explain',
            font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P,
        ).grid(row=0, column=0, sticky='w', padx=PAD, pady=(PAD, 2))

        ctk.CTkLabel(
            card,
            text=f'Select text anywhere → {self.hotkey_cfg.get("ask", "shift+f4").upper()} to get an explanation.\n'
                 'Or type any question below.  Answer appears near your cursor.',
            font=(FONT_FAMILY, 12), text_color=TEXT_S, justify='left', anchor='w',
        ).grid(row=1, column=0, sticky='w', padx=PAD, pady=(0, PAD_SM))

        chips_row = ctk.CTkFrame(card, fg_color='transparent')
        chips_row.grid(row=2, column=0, sticky='w', padx=PAD, pady=(0, PAD_SM))

        for chip_text in ('📝 Selected text', '🖼 Clipboard image', '⌨ Type below'):
            ctk.CTkLabel(
                chips_row, text=chip_text,
                font=(FONT_FAMILY, 11), text_color=TEXT_S,
                fg_color=SURF2, corner_radius=RADIUS_SM,
                padx=8, pady=3,
            ).pack(side='left', padx=(0, 6))

        # Text input
        _PLACEHOLDER = 'Why is the sky blue?'
        self._ask_entry = ctk.CTkTextbox(
            card, height=68, corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 13),
            fg_color=SURF2, text_color=TEXT_D,
            border_color=BORDER, border_width=1,
        )
        self._ask_entry.grid(row=3, column=0, sticky='ew', padx=PAD, pady=(0, PAD_SM))
        self._ask_entry.insert('1.0', _PLACEHOLDER)
        self._ask_is_placeholder = True

        def _on_focus_in(e):
            if self._ask_is_placeholder:
                self._ask_entry.delete('1.0', 'end')
                self._ask_entry.configure(text_color=TEXT_P)
                self._ask_is_placeholder = False

        def _on_focus_out(e):
            if not self._ask_entry.get('1.0', 'end').strip():
                self._ask_entry.insert('1.0', _PLACEHOLDER)
                self._ask_entry.configure(text_color=TEXT_D)
                self._ask_is_placeholder = True

        self._ask_entry.bind('<FocusIn>',  _on_focus_in)
        self._ask_entry.bind('<FocusOut>', _on_focus_out)
        # CTkTextbox wraps an inner tk.Text widget, bind there too
        # so FocusOut fires reliably when the user clicks away
        try:
            self._ask_entry._textbox.bind('<FocusIn>',  _on_focus_in, add='+')
            self._ask_entry._textbox.bind('<FocusOut>', _on_focus_out, add='+')
        except Exception:
            pass

        def _fire_ask():
            if self._ask_is_placeholder:
                # Use the placeholder as the actual question
                text = _PLACEHOLDER
                self._ask_entry.delete('1.0', 'end')
                self._ask_entry.configure(text_color=TEXT_P)
                self._ask_is_placeholder = False
            else:
                text = (self._ask_entry.get('1.0', 'end') or '').strip()
            if text and self._on_ask:
                self._on_ask(text)
                self._ask_entry.delete('1.0', 'end')
                # Restore placeholder after firing
                self._ask_entry.insert('1.0', _PLACEHOLDER)
                self._ask_entry.configure(text_color=TEXT_D)
                self._ask_is_placeholder = True

        self._ask_entry.bind('<Return>', lambda e: (_fire_ask(), 'break'))
        self._ask_entry.bind('<Shift-Return>', lambda e: None)   # allow newlines with Shift+Enter

        ctk.CTkButton(
            card, text='✦  Ask',
            fg_color=ACCENT, hover_color=ACCENTL,
            text_color=TEXT_P, font=(FONT_FAMILY, 13, 'bold'),
            width=120, height=34, corner_radius=RADIUS_SM,
            command=_fire_ask,
        ).grid(row=4, column=0, sticky='w', padx=PAD, pady=(0, PAD))

        # ── Tips card ─────────────────────────────────────────────────────────
        tips = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
        tips.grid(row=1, column=0, sticky='ew', padx=8, pady=(0, 8))
        tips.columnconfigure(0, weight=1)

        _ahk = self.hotkey_cfg.get('ask', 'shift+f4').upper()
        ctk.CTkLabel(
            tips, text=f'Ways to use {_ahk}',
            font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_S,
        ).grid(row=0, column=0, sticky='w', padx=PAD, pady=(PAD_SM, 2))

        tip_lines = [
            (f'Select text  →  {_ahk}',       f'Highlight any text on screen then press {_ahk}'),
            (f'Screenshot  →  {_ahk}',        f'Press PrtSc, drag a region, press {_ahk} without copying'),
            (f'Clipboard image  →  {_ahk}',   f'Copy an image to clipboard, then press {_ahk}'),
        ]
        for i, (title, desc) in enumerate(tip_lines):
            row_f = ctk.CTkFrame(tips, fg_color='transparent')
            row_f.grid(row=i + 1, column=0, sticky='ew', padx=PAD,
                       pady=(0, PAD_SM if i < len(tip_lines) - 1 else PAD))
            ctk.CTkLabel(
                row_f, text=f'· {title}',
                font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_P,
            ).pack(anchor='w')
            ctk.CTkLabel(
                row_f, text=f'  {desc}',
                font=(FONT_FAMILY, 11), text_color=TEXT_S,
            ).pack(anchor='w')

    # ── Chains tab ────────────────────────────────────────────────────────────

    def _render_chains_tab(self) -> None:
        """Render the Chains tab, ordered list of chains with active toggle, edit, delete."""
        if self._render_tab_guard('chains'):
            return
        from storage import load_chains, save_chains

        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        self._scroll.columnconfigure(0, weight=1)

        chains = load_chains()
        chain_hk = self.hotkey_cfg.get('chain', 'shift+f6').upper()

        # ── Toolbar with "+ New Chain" ─────────────────────────────────────────
        toolbar = ctk.CTkFrame(self._scroll, fg_color='transparent')
        toolbar.grid(row=0, column=0, sticky='ew', padx=8, pady=(8, 4))
        toolbar.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            toolbar, text='⛓  Prompt Chains',
            font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P,
        ).grid(row=0, column=0, sticky='w')

        def _add_chain():
            dlg = ChainEditDialog(self.win, prompts=self.prompts,
                                  on_hotkey_suspend=self._on_hotkey_suspend,
                                  on_hotkey_resume=self._on_hotkey_resume)
            self.win.wait_window(dlg)
            if dlg.result:
                new_chain = dlg.result
                # Always reload from disk, avoid stale closure (CRITICAL-2)
                _chains = load_chains()
                if not _chains:
                    new_chain['active'] = True
                _chains.append(new_chain)
                threading.Thread(target=lambda: save_chains(_chains), daemon=True).start()
                self._render_chains_tab()
                if self._on_chains_changed:
                    self._on_chains_changed()

        _btn(
            toolbar, '＋ New Chain', _add_chain, width=110,
            fg_color=ACCENT, hover=ACCENTL,
        ).grid(row=0, column=1, sticky='e')

        ctk.CTkLabel(
            toolbar,
            text=f'{chain_hk} runs the active chain on selected text.',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
        ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(2, 0))

        # ── Empty state ────────────────────────────────────────────────────────
        if not chains:
            empty = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
            empty.grid(row=1, column=0, sticky='ew', padx=8, pady=8)
            ctk.CTkLabel(empty, text='⛓', font=(FONT_FAMILY, 28),
                         text_color=ACCENT).pack(pady=(PAD_LG, 2))
            ctk.CTkLabel(empty, text='No chains yet',
                         font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P).pack()
            ctk.CTkLabel(empty,
                         text='A chain runs multiple prompts in sequence,\n'
                              'feeding each result into the next step.\n'
                              'Click  ＋ New Chain  to get started.',
                         font=(FONT_FAMILY, 12), text_color=TEXT_S,
                         justify='center').pack(pady=(4, PAD_LG))
            return

        # ── Chain cards ────────────────────────────────────────────────────────
        for idx, chain in enumerate(chains):
            self._make_chain_card(
                parent=self._scroll, chains=chains, idx=idx,
                chain=chain, grid_row=idx + 1,
                save_fn=lambda cl=chains: threading.Thread(
                    target=lambda: save_chains(cl), daemon=True).start(),
            )

    def _make_chain_card(self, parent, chains: list, idx: int, chain: dict,
                         grid_row: int, save_fn) -> None:
        """Build one chain card row."""
        from storage import load_chains, save_chains as _sc

        is_active = chain.get('active', False)
        color     = chain.get('color', CARD_COLORS[idx % len(CARD_COLORS)])
        steps     = chain.get('steps', [])

        card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=RADIUS_SM,
                            border_width=2,
                            border_color=ACCENTL if is_active else BORDER)
        card.grid(row=grid_row, column=0, sticky='ew', padx=8, pady=4)
        card.columnconfigure(1, weight=1)

        # ── Active tick button ─────────────────────────────────────────────────
        def _set_active(i=idx):
            _chains = load_chains()
            for j, c in enumerate(_chains):
                c['active'] = (j == i)
            threading.Thread(target=lambda: _sc(_chains), daemon=True).start()
            self._render_chains_tab()

        tick_btn = ctk.CTkButton(
            card,
            text='✓' if is_active else '○',
            width=32, height=32,
            fg_color=ACCENT if is_active else 'transparent',
            hover_color=ACCENTL if is_active else SURF2,
            text_color='#ffffff' if is_active else SURF3,
            corner_radius=RADIUS_SM,
            font=(FONT_FAMILY, 15, 'bold') if is_active else (FONT_FAMILY, 15),
            command=_set_active,
        )
        tick_btn.grid(row=0, column=0, rowspan=2, padx=(PAD_SM, 0), pady=PAD_SM, sticky='w')

        # ── Color swatch + name ────────────────────────────────────────────────
        name_row = ctk.CTkFrame(card, fg_color='transparent')
        name_row.grid(row=0, column=1, sticky='w', padx=(PAD_SM, 0), pady=(PAD_SM, 2))

        # Color swatch
        ctk.CTkFrame(name_row, fg_color=color, width=12, height=12,
                     corner_radius=3).pack(side='left', padx=(0, 6))

        name_lbl = ctk.CTkLabel(
            name_row, text=chain.get('name', 'Unnamed Chain'),
            font=(FONT_FAMILY, 13, 'bold'), text_color=TEXT_P, anchor='w',
        )
        name_lbl.pack(side='left')

        # Step count badge
        step_count = len(steps)
        ctk.CTkLabel(
            name_row,
            text=f'  {step_count} step{"s" if step_count != 1 else ""}',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
            fg_color=SURF2, corner_radius=RADIUS_SM,
            padx=6, pady=2,
        ).pack(side='left', padx=(6, 0))

        # ── Step chips row ─────────────────────────────────────────────────────
        chips_row = ctk.CTkFrame(card, fg_color='transparent')
        chips_row.grid(row=1, column=1, sticky='w', padx=(PAD_SM, 0), pady=(0, PAD_SM))

        for si, step in enumerate(steps):
            chip_text = step.get('label', f'Step {si + 1}')
            ctk.CTkLabel(
                chips_row,
                text=chip_text,
                font=(FONT_FAMILY, 11), text_color=TEXT_S,
                fg_color=SURF3, corner_radius=RADIUS_SM,
                padx=6, pady=2,
            ).pack(side='left', padx=(0, 2))
            if si < len(steps) - 1:
                ctk.CTkLabel(
                    chips_row, text='→',
                    font=(FONT_FAMILY, 11), text_color=TEXT_D,
                ).pack(side='left', padx=(0, 2))

        # ── Edit + Delete buttons ──────────────────────────────────────────────
        btn_col = ctk.CTkFrame(card, fg_color='transparent')
        btn_col.grid(row=0, column=2, rowspan=2, padx=(4, PAD_SM), pady=PAD_SM, sticky='e')

        def _edit_chain(i=idx):
            _chains = load_chains()
            if i >= len(_chains):
                return
            dlg = ChainEditDialog(
                self.win,
                chain=_chains[i],
                prompts=self.prompts,
                on_hotkey_suspend=self._on_hotkey_suspend,
                on_hotkey_resume=self._on_hotkey_resume,
            )
            self.win.wait_window(dlg)
            if dlg.result:
                # Preserve active state
                dlg.result['active'] = _chains[i].get('active', False)
                _chains[i] = dlg.result
                threading.Thread(target=lambda: _sc(_chains), daemon=True).start()
                self._render_chains_tab()
                if self._on_chains_changed:
                    self._on_chains_changed()
            else:
                self._render_chains_tab()

        def _del_chain(i=idx):
            _chains = load_chains()
            if i >= len(_chains):
                return
            cname = _chains[i].get('name', 'this chain')
            if not confirm(self.win, 'Delete chain', f'Delete "{cname}"?',
                           action_label='Delete',
                           action_color='#b03030', action_hover='#d04040'):
                return
            _chains.pop(i)
            # Ensure at least one is active
            if _chains and not any(c.get('active') for c in _chains):
                _chains[0]['active'] = True
            threading.Thread(target=lambda: _sc(_chains), daemon=True).start()
            self._render_chains_tab()
            if self._on_chains_changed:
                self._on_chains_changed()

        _btn(btn_col, '✏  Edit', _edit_chain, width=70).pack(pady=(0, 4))
        _btn(btn_col, '✕', _del_chain, width=32,
             fg_color=SURF3, hover=ERR, text_color=TEXT_S).pack()

    # ── Notes tab ─────────────────────────────────────────────────────────────

    def _render_notes_tab(self) -> None:
        """Notes tab is a splash card, the actual note list lives inside the
        Quick Notes window itself (Shift+F7). Mirrors the Whiteboard tab
        pattern so the Library tab strip stays consistent and the user is
        never shown two interfaces for the same content.
        """
        if self._render_tab_guard('notes'):
            return
        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        self._scroll.columnconfigure(0, weight=1)

        notes_hk = self.hotkey_cfg.get('notes', 'shift+f7').upper()
        # Include the secondary hotkey (bare Home by default) in the
        # description so users discover both openers.
        notes_alt = (self.hotkey_cfg.get('notes_alt') or '').strip().upper()
        if notes_alt and notes_alt != notes_hk:
            notes_hk_display = f'{notes_hk} or {notes_alt}'
        else:
            notes_hk_display = notes_hk

        container = ctk.CTkFrame(self._scroll, fg_color='transparent')
        container.grid(row=0, column=0, sticky='ew', padx=PAD, pady=PAD)
        container.columnconfigure(0, weight=1)

        # ── Header card ───────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(container, fg_color=SURFACE, corner_radius=RADIUS_SM)
        hdr.grid(row=0, column=0, sticky='ew')
        hdr.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr, text='📝  Quick Notes',
            font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P,
        ).grid(row=0, column=0, sticky='w', padx=PAD, pady=(PAD, 2))
        ctk.CTkLabel(
            hdr,
            text=(f'Press {notes_hk_display} anywhere to capture a thought. '
                  'Text, checklist or voice, runs fully offline. All '
                  'your saved notes live in the Quick Notes window.'),
            font=(FONT_FAMILY, 13), text_color=TEXT_P, justify='left',
            wraplength=900,
        ).grid(row=1, column=0, sticky='w', padx=PAD, pady=(0, PAD_SM))

        # Voice-memo tip — visible alongside the regular "Press Shift+F7" hint
        # so users discover the hands-free save flow without opening docs.
        ctk.CTkLabel(
            hdr,
            text=('💡  Tip: while dictating with Ctrl+Enter, say "memo" '
                  'at the start or end of your sentence to save it as a '
                  'note instead of typing it into the focused app.'),
            font=(FONT_FAMILY, 12, 'italic'), text_color=TEXT_S, justify='left',
            wraplength=900,
        ).grid(row=2, column=0, sticky='w', padx=PAD, pady=(0, PAD))

        # ── Open button ───────────────────────────────────────────────────────
        def _open():
            if self._on_new_note:
                self._on_new_note()

        _btn(container, f'📝  Open Quick Notes  ({notes_hk})', _open, width=240,
             fg_color=ACCENT, hover=ACCENTL,
             ).grid(row=1, column=0, sticky='w', pady=(PAD, 0))


    def _on_bg_right_click(self, event) -> None:
        menu = tk.Menu(self.win, tearoff=0, bg=SURFACE, fg=TEXT_P,
                       activebackground=ACCENT, activeforeground='#fff',
                       font=(FONT_FAMILY, 12))

        if self._active_tab == 'prompts':
            menu.add_command(label='✚  Create prompt',  command=self._add)
            menu.add_separator()
            menu.add_command(label='📁  Create folder', command=self._create_folder)

        elif self._active_tab == 'macros':
            ms = self._macro_state
            if ms == 'idle':
                label = '⏺  Record macro'
            elif ms == 'recording':
                label = '⏹  Stop recording'
            elif ms == 'ready':
                label = '▶  Play back recording'
            elif ms == 'playing':
                label = '⏹  Stop playback'
            else:
                label = '⏺  Record macro'
            def _do_macro_toggle():
                if self._on_macro_toggle:
                    self._on_macro_toggle()
            menu.add_command(label=label, command=_do_macro_toggle)

        elif self._active_tab == 'recorder':
            state = self._recorder_state
            if state == 'idle':
                def _do_start():
                    if self._on_recorder_toggle:
                        self._on_recorder_toggle()
                menu.add_command(label='🎥  Start Recording', command=_do_start)
            elif state == 'recording':
                def _do_stop():
                    if self._on_recorder_toggle:
                        self._on_recorder_toggle()
                menu.add_command(label='⏹  Stop Recording', command=_do_stop)
            else:
                return  # 'stopping', encoding in progress, nothing useful to show

        elif self._active_tab == 'gif':
            gs = self._gif_state
            if gs == 'idle':
                def _do_gif_start():
                    if self._on_gif_toggle:
                        self._on_gif_toggle()
                menu.add_command(label='🎞  Start GIF', command=_do_gif_start)
            elif gs == 'recording':
                def _do_gif_stop():
                    if self._on_gif_toggle:
                        self._on_gif_toggle()
                menu.add_command(label='⏹  Stop GIF', command=_do_gif_stop)
            else:
                return  # encoding, nothing useful to show

        else:
            return

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ── Web / Bookmarks tab ───────────────────────────────────────────────────

    # ── Transcribe tab (Shift+F9) ─────────────────────────────────────────────

    def _render_transcribe_tab(self) -> None:
        """Mount the TurboScribe-style transcription UI inside the scroll area.
        The full UI lives in transcribe_ui.TranscribePanel, this method is
        just the wiring."""
        if self._render_tab_guard('transcribe'):
            return
        # Tear down any previous panel (e.g. switching back to this tab)
        if getattr(self, '_transcribe_panel', None) is not None:
            try: self._transcribe_panel.destroy()
            except Exception: pass
            self._transcribe_panel = None

        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        self._scroll.columnconfigure(0, weight=1)

        from transcribe_ui import TranscribePanel
        self._transcribe_panel = TranscribePanel(
            self._scroll,
            hotkey_cfg=self.hotkey_cfg,
            provider_factory=None,           # falls back to engine.build_provider(config)
            notify_fn=lambda title, msg: alert(self.win, title, msg),
        )

    # ── Whiteboard tab ────────────────────────────────────────────────────────

    def _render_whiteboard_tab(self) -> None:
        """Render the Whiteboard tab, hotkey hint + an Open button.

        The Whiteboard itself runs in its own pywebview process (the scene
        is auto-saved there), so this tab is just a launcher. We keep the
        layout consistent with the Notes empty-state card.
        """
        if self._render_tab_guard('whiteboard'):
            return
        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        self._scroll.columnconfigure(0, weight=1)

        wb_hk = self.hotkey_cfg.get('whiteboard', 'shift+f8').upper()

        container = ctk.CTkFrame(self._scroll, fg_color='transparent')
        container.grid(row=0, column=0, sticky='ew', padx=PAD, pady=PAD)
        container.columnconfigure(0, weight=1)

        # ── Header card ───────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(container, fg_color=SURFACE, corner_radius=RADIUS_SM)
        hdr.grid(row=0, column=0, sticky='ew')
        hdr.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr, text='🎨  Whiteboard',
            font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P,
        ).grid(row=0, column=0, sticky='w', padx=PAD, pady=(PAD, 2))
        ctk.CTkLabel(
            hdr,
            text=(f'Press {wb_hk} anywhere to open the Whiteboard. '
                  'Sketch diagrams, brainstorm or annotate, runs fully '
                  'offline. Your scene auto-saves; reopening picks up '
                  'where you left off.'),
            font=(FONT_FAMILY, 13), text_color=TEXT_P, justify='left',
            wraplength=900,
        ).grid(row=1, column=0, sticky='w', padx=PAD, pady=(0, PAD_SM))

        # Voice-command discoverability tip — same pattern as the Notes tab.
        ctk.CTkLabel(
            hdr,
            text=('💡  Tip: while dictating with Ctrl+Enter, say "whiteboard" '
                  'to open this from any app, hands-free.'),
            font=(FONT_FAMILY, 12, 'italic'), text_color=TEXT_S, justify='left',
            wraplength=900,
        ).grid(row=2, column=0, sticky='w', padx=PAD, pady=(0, PAD))

        # ── Open button ───────────────────────────────────────────────────────
        def _open():
            if self._on_open_whiteboard:
                self._on_open_whiteboard()

        _btn(container, f'🎨  Open Whiteboard  ({wb_hk})', _open, width=240,
             fg_color=ACCENT, hover=ACCENTL,
             ).grid(row=1, column=0, sticky='w', pady=(PAD, 0))

    def _render_audio_editor_tab(self) -> None:
        """Render the Audio editor tab, hotkey hint + an Open button.

        The editor itself is a bundled portable Tenacity build relabeled
        to "Audio Editor", launched as a sibling process. This tab
        is just a launcher card, same shape as the Whiteboard tab.
        """
        if self._render_tab_guard('audio_editor'):
            return
        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        self._scroll.columnconfigure(0, weight=1)

        ae_hk = self.hotkey_cfg.get('audio_editor', 'shift+f10').upper()

        container = ctk.CTkFrame(self._scroll, fg_color='transparent')
        container.grid(row=0, column=0, sticky='ew', padx=PAD, pady=PAD)
        container.columnconfigure(0, weight=1)

        # ── Header card ───────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(container, fg_color=SURFACE, corner_radius=RADIUS_SM)
        hdr.grid(row=0, column=0, sticky='ew')
        hdr.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr, text='🎵  Audio editor',
            font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P,
        ).grid(row=0, column=0, sticky='w', padx=PAD, pady=(PAD, 2))
        ctk.CTkLabel(
            hdr,
            text=(f'Press {ae_hk} anywhere to open the audio editor. '
                  'Drag in an audio or video file (mp3, wav, flac, ogg, '
                  'mkv, mp4, mov, m4a) to load it. Trim, cut, fade, '
                  'amplify, normalise, change speed or pitch, remove '
                  'noise, then export back out. Runs offline.'),
            font=(FONT_FAMILY, 13), text_color=TEXT_P, justify='left',
            wraplength=900,
        ).grid(row=1, column=0, sticky='w', padx=PAD, pady=(0, PAD_SM))

        # Voice-command discoverability tip.
        ctk.CTkLabel(
            hdr,
            text=('💡  Tip: while dictating with Ctrl+Enter, say "audio" '
                  'to open this from any app, hands-free.'),
            font=(FONT_FAMILY, 12, 'italic'), text_color=TEXT_S, justify='left',
            wraplength=900,
        ).grid(row=2, column=0, sticky='w', padx=PAD, pady=(0, PAD))

        # ── Open button ───────────────────────────────────────────────────────
        def _open():
            if self._on_open_audio_editor:
                self._on_open_audio_editor()

        _btn(container, f'🎵  Open audio editor  ({ae_hk})', _open, width=260,
             fg_color=ACCENT, hover=ACCENTL,
             ).grid(row=1, column=0, sticky='w', pady=(PAD, 0))

    # ── Placeholder slot tabs (Shift+F11..F12) ────────────────────────────────

    def _render_slot_tab(self, key: str) -> None:
        """Render a reserved-slot tab. Same shape as the Whiteboard tab so it
        slots into the existing layout without surprises, a header card
        explaining the slot is reserved, plus the hotkey for reference."""
        if self._render_tab_guard(key):
            return
        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        self._scroll.columnconfigure(0, weight=1)

        # Resolve label + hotkey from the central placeholder table
        label, default_hk = key, ''
        for _k, _lbl, _dhk in self._PLACEHOLDER_SLOTS:
            if _k == key:
                label, default_hk = _lbl, _dhk
                break
        hk_str = self.hotkey_cfg.get(key, default_hk).upper()

        container = ctk.CTkFrame(self._scroll, fg_color='transparent')
        container.grid(row=0, column=0, sticky='ew', padx=PAD, pady=PAD)
        container.columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(container, fg_color=SURFACE, corner_radius=RADIUS_SM)
        hdr.grid(row=0, column=0, sticky='ew')
        hdr.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr, text=f'·  {label}',
            font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P,
        ).grid(row=0, column=0, sticky='w', padx=PAD, pady=(PAD, 2))
        ctk.CTkLabel(
            hdr,
            text=(f'{hk_str} is reserved for a future feature. Right-click '
                  'the tab button above to rebind the hotkey now if you '
                  'want the slot for one of your own shortcuts.'),
            font=(FONT_FAMILY, 13), text_color=TEXT_P, justify='left',
            wraplength=900,
        ).grid(row=1, column=0, sticky='w', padx=PAD, pady=(0, PAD))

    def _render_web_tab(self) -> None:
        """Render the Web bookmarks tab, radio-select active site, Shift+F5 opens it."""
        if self._render_tab_guard('web'):
            return
        from storage import load_bookmarks, save_bookmarks
        import webbrowser, threading

        for w in self._scroll.winfo_children():
            w.destroy()
        self._cards.clear()
        self._folder_headers.clear()

        for _c in range(max(2, self._current_cols) + 1):
            self._scroll.columnconfigure(_c, weight=0)
        self._scroll.columnconfigure(0, weight=1)

        bookmarks = load_bookmarks()

        def _save_and_refresh(bms):
            threading.Thread(target=lambda: save_bookmarks(bms), daemon=True).start()
            self._render_web_tab()

        def _set_active(idx: int) -> None:
            bms = load_bookmarks()
            for i, b in enumerate(bms):
                b['active'] = (i == idx)
            _save_and_refresh(bms)

        def _delete(idx: int) -> None:
            bms = load_bookmarks()
            if 0 <= idx < len(bms):
                name = bms[idx].get('name', 'this bookmark')
                if not confirm(self.win, 'Remove bookmark', f'Remove "{name}"?'):
                    return
                was_active = bms[idx].get('active', False)
                bms.pop(idx)
                if was_active and bms:
                    bms[0]['active'] = True
                _save_and_refresh(bms)

        def _open_url(url: str) -> None:
            u = url if url.startswith(('http://', 'https://')) else 'https://' + url
            threading.Thread(target=lambda: webbrowser.open(u), daemon=True).start()

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
        hdr.grid(row=0, column=0, sticky='ew', padx=8, pady=8)
        hdr.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr, text='🌐  Web Bookmarks',
            font=(FONT_FAMILY, 15, 'bold'), text_color=TEXT_P,
        ).grid(row=0, column=0, sticky='w', padx=PAD, pady=(PAD, 2))
        ctk.CTkLabel(
            hdr,
            text=f'Tick one site — {self.hotkey_cfg.get("web", "shift+f5").upper()} opens it instantly.\nClick the name to open it, Edit to rename or change URL, ✕ to remove.',
            font=(FONT_FAMILY, 12), text_color=TEXT_S, justify='left',
        ).grid(row=1, column=0, sticky='w', padx=PAD, pady=(0, PAD))

        # ── Bookmark rows ─────────────────────────────────────────────────────
        list_frame = ctk.CTkFrame(self._scroll, fg_color='transparent')
        list_frame.grid(row=1, column=0, sticky='ew', padx=8, pady=(0, 4))
        list_frame.columnconfigure(1, weight=1)

        for i, bm in enumerate(bookmarks):
            is_active = bm.get('active', False)
            row_bg    = SURFACE
            row = ctk.CTkFrame(list_frame, fg_color=row_bg, corner_radius=RADIUS_SM)
            row.grid(row=i, column=0, sticky='ew', pady=(0, 4))
            row.columnconfigure(1, weight=1)

            # Radio tick, filled accent bg + checkmark if active, muted empty circle if not
            tick_btn = ctk.CTkButton(
                row,
                text='✓' if is_active else '○',
                width=32, height=32,
                fg_color=ACCENT if is_active else 'transparent',
                hover_color=ACCENTL if is_active else SURF2,
                text_color='#ffffff' if is_active else SURF3,
                corner_radius=RADIUS_SM,
                font=(FONT_FAMILY, 15, 'bold') if is_active else (FONT_FAMILY, 15),
                command=lambda idx=i: _set_active(idx),
            )
            tick_btn.grid(row=0, column=0, padx=(PAD_SM, 0), pady=PAD_SM)

            # Name button, click to open URL
            name_fg = ACCENT if is_active else SURF2
            name_btn = ctk.CTkButton(
                row, text=bm.get('name', 'Untitled'), width=130, height=32,
                fg_color=name_fg, hover_color=ACCENTL,
                text_color=TEXT_P, corner_radius=RADIUS_SM,
                font=(FONT_FAMILY, 13, 'bold'), anchor='w',
                command=lambda u=bm['url']: _open_url(u),
            )
            name_btn.grid(row=0, column=1, padx=(6, 4), pady=PAD_SM, sticky='w')

            # URL label, strip https://, http://, and www. for cleaner display
            _url_display = bm.get('url', '')
            for _pfx in ('https://', 'http://'):
                if _url_display.startswith(_pfx):
                    _url_display = _url_display[len(_pfx):]
                    break
            if _url_display.startswith('www.'):
                _url_display = _url_display[4:]
            ctk.CTkLabel(
                row, text=_url_display, font=(FONT_FAMILY, 11),
                text_color=TEXT_S, anchor='w',
            ).grid(row=0, column=2, padx=(0, 4), sticky='ew')
            row.columnconfigure(2, weight=1)

            # Edit button → inline edit
            edit_btn = ctk.CTkButton(
                row, text='Edit', width=44, height=28,
                fg_color=SURF2, hover_color=SURF3,
                text_color=TEXT_P, corner_radius=RADIUS_SM, font=(FONT_FAMILY, 11))
            edit_btn.grid(row=0, column=3, padx=(0, 4), pady=PAD_SM)

            # Delete button
            ctk.CTkButton(
                row, text='✕', width=28, height=28,
                fg_color=SURF2, hover_color=ERR,
                text_color=TEXT_P, corner_radius=RADIUS_SM, font=(FONT_FAMILY, 12),
                command=lambda idx=i: _delete(idx),
            ).grid(row=0, column=4, padx=(0, PAD_SM), pady=PAD_SM)

            # Edit command, replaces row contents with entry fields
            def _start_edit(idx=i, r=row, bm_data=bm):
                for w in r.winfo_children():
                    w.grid_forget()
                r.columnconfigure(1, weight=1)
                r.columnconfigure(2, weight=1)

                nv = tk.StringVar(value=bm_data.get('name', ''))
                _raw_url = bm_data.get('url', '')
                for _p in ('https://', 'http://'):
                    if _raw_url.startswith(_p):
                        _raw_url = _raw_url[len(_p):]
                        break
                if _raw_url.startswith('www.'):
                    _raw_url = _raw_url[4:]
                uv = tk.StringVar(value=_raw_url)

                n_ent = ctk.CTkEntry(
                    r, textvariable=nv, width=120, height=30,
                    fg_color=SURF2, border_color=BORDER2, border_width=1,
                    text_color=TEXT_P, font=(FONT_FAMILY, 12), corner_radius=RADIUS_SM)
                n_ent.grid(row=0, column=0, columnspan=2, padx=(PAD_SM, 4), pady=PAD_SM, sticky='w')

                u_ent = ctk.CTkEntry(
                    r, textvariable=uv, height=30,
                    fg_color=SURF2, border_color=BORDER2, border_width=1,
                    text_color=TEXT_P, font=(FONT_FAMILY, 12), corner_radius=RADIUS_SM)
                u_ent.grid(row=0, column=2, padx=(0, 4), pady=PAD_SM, sticky='ew')

                def _commit(ev=None):
                    bms = load_bookmarks()
                    if 0 <= idx < len(bms):
                        name = nv.get().strip()
                        url  = uv.get().strip()
                        if url:
                            bms[idx]['name'] = name or url
                            bms[idx]['url']  = url
                            _save_and_refresh(bms)
                        else:
                            self._render_web_tab()

                ctk.CTkButton(
                    r, text='✓', width=28, height=28,
                    fg_color=OK, hover_color=_darken(OK, 0.15),
                    text_color='#fff', corner_radius=RADIUS_SM, font=(FONT_FAMILY, 12),
                    command=_commit,
                ).grid(row=0, column=3, padx=(0, 4), pady=PAD_SM)
                ctk.CTkButton(
                    r, text='✕', width=28, height=28,
                    fg_color=SURF2, hover_color=SURF3,
                    text_color=TEXT_P, corner_radius=RADIUS_SM, font=(FONT_FAMILY, 12),
                    command=lambda: self._render_web_tab(),
                ).grid(row=0, column=4, padx=(0, PAD_SM), pady=PAD_SM)

                u_ent.bind('<Return>', _commit)
                u_ent.bind('<Escape>', lambda e: self._render_web_tab())
                n_ent.focus_set()

            edit_btn.configure(command=_start_edit)

        # ── Active hint ───────────────────────────────────────────────────────
        active_bm   = next((b for b in bookmarks if b.get('active')), bookmarks[0] if bookmarks else None)
        active_name = active_bm.get('name', '') if active_bm else ''
        web_hk      = self.hotkey_cfg.get('web', 'shift+f5').upper()
        hint_frame  = ctk.CTkFrame(self._scroll, fg_color='transparent')
        hint_frame.grid(row=2, column=0, sticky='ew', padx=8, pady=(0, 4))
        ctk.CTkLabel(
            hint_frame,
            text=f'⚡  {web_hk}  →  {active_name}',
            font=(FONT_FAMILY, 12), text_color=TEXT_S,
        ).pack(side='left', padx=PAD_SM)

        # ── Add row ───────────────────────────────────────────────────────────
        add = ctk.CTkFrame(self._scroll, fg_color=SURFACE, corner_radius=RADIUS_SM)
        add.grid(row=3, column=0, sticky='ew', padx=8, pady=(0, 8))
        add.columnconfigure(1, weight=1)

        new_name_var = tk.StringVar()
        new_url_var  = tk.StringVar()

        ctk.CTkLabel(
            add, text='Add new', font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_S,
        ).grid(row=0, column=0, columnspan=3, sticky='w', padx=PAD, pady=(PAD_SM, 4))

        ctk.CTkEntry(
            add, textvariable=new_name_var, placeholder_text='Name',
            height=30, width=120,
            fg_color=SURF2, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 12), corner_radius=RADIUS_SM,
        ).grid(row=1, column=0, padx=(PAD, 4), pady=(0, PAD), sticky='w')

        ctk.CTkEntry(
            add, textvariable=new_url_var, placeholder_text='URL  (e.g. youtube.com)',
            height=30,
            fg_color=SURF2, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 12), corner_radius=RADIUS_SM,
        ).grid(row=1, column=1, padx=(0, 4), pady=(0, PAD), sticky='ew')

        def _add():
            url  = new_url_var.get().strip()
            name = new_name_var.get().strip() or url
            if not url:
                return
            bms = load_bookmarks()
            bms.append({'name': name, 'url': url, 'active': False})
            _save_and_refresh(bms)

        ctk.CTkButton(
            add, text='＋ Add', width=72, height=30,
            fg_color=ACCENT, hover_color=ACCENTL,
            text_color=TEXT_P, corner_radius=RADIUS_SM, font=(FONT_FAMILY, 12, 'bold'),
            command=_add,
        ).grid(row=1, column=2, padx=(0, PAD), pady=(0, PAD))

    def _show_rebind_popup(self, tab_name: str, event) -> None:
        """Small floating popup that captures a new hotkey for a feature tab."""
        import keyboard as _kb

        cfg_key   = self._TAB_HOTKEY_MAP[tab_name]
        current   = self.hotkey_cfg.get(cfg_key, '').upper() or ','
        tab_label = {
            'macros': 'Macros', 'recorder': 'Screen', 'gif': 'GIF',
            'ask': 'Explain', 'chains': 'Chains',
            'web': 'Web', 'notes': 'Notes', 'whiteboard': 'Whiteboard',
        }[tab_name]

        popup = tk.Toplevel(self.win)
        popup.overrideredirect(True)
        popup.attributes('-topmost', True)
        popup.configure(bg=BG)

        card = ctk.CTkFrame(popup, fg_color=SURFACE, corner_radius=RADIUS,
                            border_width=1, border_color=BORDER2)
        card.pack(padx=1, pady=1)

        ctk.CTkLabel(card, text=f'Rebind  {tab_label}',
                     font=(FONT_FAMILY, 13, 'bold'), text_color=TEXT_P
                     ).pack(anchor='w', padx=PAD, pady=(PAD, 2))

        ctk.CTkLabel(card, text=f'Current:  {current}',
                     font=(FONT_FAMILY, 11), text_color=TEXT_S
                     ).pack(anchor='w', padx=PAD, pady=(0, 4))

        hint_var = tk.StringVar(value='Press new hotkey…')
        hint_lbl = ctk.CTkLabel(card, textvariable=hint_var,
                     font=(FONT_FAMILY, 12), text_color=ACCENT)
        hint_lbl.pack(anchor='w', padx=PAD, pady=(0, 4))

        # Confirm row, hidden until a combo is captured
        confirm_frame = ctk.CTkFrame(card, fg_color='transparent')
        confirm_lbl   = ctk.CTkLabel(confirm_frame, text='',
                                     font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_P)
        confirm_lbl.pack(anchor='w', pady=(0, 6))
        btn_row = ctk.CTkFrame(confirm_frame, fg_color='transparent')
        btn_row.pack(anchor='w')
        yes_btn  = ctk.CTkButton(btn_row, text='✓  Set it', width=90,
                                 fg_color=OK, hover_color=_darken(OK, 0.15),
                                 text_color='#fff', corner_radius=RADIUS_SM,
                                 font=(FONT_FAMILY, 12))
        yes_btn.pack(side='left', padx=(0, 6))
        no_btn   = ctk.CTkButton(btn_row, text='↩  Try again', width=100,
                                 fg_color=SURF2, hover_color=SURF3,
                                 text_color=TEXT_P, corner_radius=RADIUS_SM,
                                 font=(FONT_FAMILY, 12))
        no_btn.pack(side='left')

        ctk.CTkLabel(card, text='Esc or click away to cancel',
                     font=(FONT_FAMILY, 10), text_color=TEXT_D,
                     ).pack(anchor='w', padx=PAD, pady=(4, PAD_SM))

        # Position below the tab button
        popup.update_idletasks()
        bx = event.widget.winfo_rootx()
        by = event.widget.winfo_rooty() + event.widget.winfo_height() + 4
        popup.geometry(f'+{bx}+{by}')

        _done    = [False]
        _hook    = [None]
        _pending = [None]   # combo string awaiting confirmation
        _MODS    = {'ctrl', 'left ctrl', 'right ctrl',
                    'alt', 'left alt', 'right alt',
                    'shift', 'left shift', 'right shift',
                    'windows', 'left windows', 'right windows'}

        def _close():
            if _hook[0] is not None:
                try:
                    _kb.unhook(_hook[0])
                except Exception:
                    pass
                _hook[0] = None
            try:
                popup.destroy()
            except Exception:
                pass
            if self._on_hotkey_resume:
                self._on_hotkey_resume()

        def _enter_capture_phase():
            """Show the 'press a key' state."""
            _pending[0] = None
            confirm_frame.pack_forget()
            hint_var.set('Press new hotkey…')
            hint_lbl.configure(text_color=ACCENT)
            popup.update_idletasks()
            popup.focus_force()

        def _enter_confirm_phase(combo: str):
            """Show 'set to X?' with Yes / Try again."""
            _pending[0] = combo
            hint_lbl.configure(text_color=TEXT_D)
            hint_var.set('Captured:')
            confirm_lbl.configure(text=combo.upper())
            confirm_frame.pack(anchor='w', padx=PAD, pady=(0, PAD_SM))
            popup.update_idletasks()

        def _confirm_yes():
            _done[0] = True
            combo = _pending[0]
            if combo:
                self.hotkey_cfg[cfg_key] = combo
                if self._on_feature_hotkey_changed:
                    self.win.after(0, lambda c=combo: self._on_feature_hotkey_changed(cfg_key, c))
            _close()

        def _confirm_no():
            _enter_capture_phase()

        yes_btn.configure(command=_confirm_yes)
        no_btn.configure(command=_confirm_no)

        def _on_key(e):
            if _done[0] or e.event_type != 'down':
                return
            name = e.name.lower()
            if name in ('esc', 'escape'):
                _done[0] = True
                self.win.after(0, _close)
                return
            # If in confirm phase, Enter = yes
            if _pending[0] is not None:
                if name in ('return', 'enter'):
                    _done[0] = True
                    self.win.after(0, _confirm_yes)
                return
            if name in _MODS:
                return   # wait for a non-modifier key
            # Build combo from currently held modifiers + this key
            mods = []
            if _kb.is_pressed('ctrl'):    mods.append('ctrl')
            if _kb.is_pressed('alt'):     mods.append('alt')
            if _kb.is_pressed('shift'):   mods.append('shift')
            if _kb.is_pressed('windows'): mods.append('windows')
            if not mods:
                self.win.after(0, lambda: hint_var.set('Need a modifier (Ctrl/Alt/Shift)'))
                self.win.after(1200, lambda: hint_var.set('Press new hotkey…') if not _pending[0] else None)
                return
            combo = '+'.join(mods + [name])
            self.win.after(0, lambda c=combo: _enter_confirm_phase(c))

        # Dismiss if user clicks anywhere outside the popup
        popup.bind('<FocusOut>', lambda e: self.win.after(150, lambda: _done[0] or _close()))

        if self._on_hotkey_suspend:
            self._on_hotkey_suspend()
        _hook[0] = _kb.hook(_on_key, suppress=False)
        popup.focus_force()

    def _show_shortcuts(self) -> None:
        """Show a modal with all keyboard shortcuts."""
        hk = self.hotkey_cfg
        refine_hk   = hk.get('refine',       'alt+shift+w').upper()
        lib_hk      = hk.get('library',      'alt+shift+e').upper()
        whisper_hk  = hk.get('whisper',      'ctrl+enter').upper()
        undo_hk     = hk.get('undo_refine',  'alt+shift+z').upper()
        ask_hk      = hk.get('ask',          'shift+f4').upper()
        macro_hk    = hk.get('macro_record', 'shift+f1').upper()
        recorder_hk = hk.get('recorder',     'shift+f2').upper()
        gif_hk      = hk.get('gif_record',   'shift+f3').upper()

        lines = [
            ('Refine selected text',       refine_hk),
            ('Undo last refinement',        undo_hk),
            ('Open/close this library',     lib_hk),
            ('Dictate (speech → text)',     whisper_hk),
            ('Explain / ask a question',    ask_hk),
            ('Record/stop/play macro',      macro_hk),
            ('Start/stop screen recording', recorder_hk),
            ('Start/stop GIF capture',      gif_hk),
            ('Refine (scroll gesture)',      'CTRL + SCROLL UP'),
            ('Cancel / close pill',         'ESC'),
        ]

        dlg = ctk.CTkToplevel(self.win)
        dlg.title('Keyboard Shortcuts')
        dlg.configure(fg_color=BG)
        dlg.resizable(False, False)
        dlg.transient(self.win)
        dlg.grab_set()
        dlg.withdraw()

        hdr = ctk.CTkFrame(dlg, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr, text='⌨  Keyboard Shortcuts',
                     font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_P
                     ).pack(anchor='w', padx=PAD, pady=PAD_SM)

        body = ctk.CTkFrame(dlg, fg_color=BG, corner_radius=0)
        body.pack(fill='both', expand=True, padx=PAD, pady=PAD)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)

        for r, (desc, keys) in enumerate(lines):
            ctk.CTkLabel(body, text=desc, font=(FONT_FAMILY, 12),
                         text_color=TEXT_P, anchor='w'
                         ).grid(row=r, column=0, sticky='w', pady=3)
            ctk.CTkLabel(body, text=keys,
                         font=(FONT_FAMILY, 11), text_color=TEXT_S,
                         fg_color=SURF2, corner_radius=RADIUS_SM,
                         padx=8, pady=2,
                         ).grid(row=r, column=1, sticky='e', padx=(16, 0), pady=3)

        foot = ctk.CTkFrame(dlg, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')
        ctk.CTkButton(foot, text='Close', command=dlg.destroy,
                      width=80, fg_color=SURF2, hover_color=SURF3,
                      text_color=TEXT_P, corner_radius=RADIUS_SM,
                      font=(FONT_FAMILY, 13)).pack(side='right', padx=PAD, pady=PAD_SM)
        dlg.bind('<Escape>', lambda e: dlg.destroy())

        def _on_map(e=None):
            from dialogs import center_over_parent
            center_over_parent(dlg, self.win)
            dlg.lift()
            dlg.focus_force()
            dlg.unbind('<Map>')
        dlg.bind('<Map>', _on_map)
        dlg.after(50, dlg.deiconify)

    def show(self) -> None:
        # Perceived-latency optimisation: deiconify the window FIRST so the
        # user sees it appear instantly, then build the active tab's content
        # (if not cached) on the next idle tick. For cached tabs this is
        # a no-op (the widgets are already laid out). For first-ever opens
        # the user sees an empty chrome for ~30ms before content fills in,
        # which feels dramatically faster than waiting for a full render
        # before the window appears.
        is_cached = self._active_tab in self._tab_built

        self.win.deiconify()
        self._snap_into_work_area()
        self.win.lift()
        self.win.focus_force()

        if is_cached:
            # Inline show, no rebuild. Effectively instant.
            try:
                self._show_active_tab()
            except Exception as exc:
                _log.error('show() _show_active_tab error: %s', exc, exc_info=True)
        else:
            # Defer the build so the window paints first.
            self.win.after_idle(self._safe_show_active_tab)

        # Snap again after DWM has finalised the rect, and disable transition
        # animations on subsequent show()s.
        self.win.after(200, self._snap_into_work_area)
        self.win.after(50, self._disable_dwm_transitions)

    def _safe_show_active_tab(self) -> None:
        """Wrapped tab-show used by deferred (after_idle) paths so an
        exception in the renderer doesn't poison the Tk event loop."""
        try:
            self._show_active_tab()
        except Exception as exc:
            _log.error('_safe_show_active_tab error: %s', exc, exc_info=True)

    def show_tab(self, tab: str) -> None:
        """Show the library window open on a specific tab."""
        self._switch_tab(tab)
        self.show()

    def hide(self) -> None:
        self.win.withdraw()

    def refresh_hotkeys(self, hotkey_cfg: dict) -> None:
        """Replace this window's cached hotkey config and refresh every
        visible label that quotes a hotkey string.

        Called by main.py after _register_hotkeys() so a hot reload of the
        bindings (e.g. via the tray 'Reload hotkeys' menu, or an IPC
        reload_hotkeys command) propagates to the UI immediately instead
        of going stale until the user closes + reopens the window.
        """
        self.hotkey_cfg = hotkey_cfg
        hk = hotkey_cfg

        # ── Rebuild every cached hint-text string ─────────────────────────
        refine_hk     = hk.get('refine',       'alt+shift+w').upper()
        macro_hk_h    = hk.get('macro_record', 'shift+f1').upper()
        recorder_hk_h = hk.get('recorder',     'shift+f2').upper()
        gif_hk_h      = hk.get('gif_record',   'shift+f3').upper()
        ask_hk_h      = hk.get('ask',          'shift+f4').upper()
        web_hk_h      = hk.get('web',          'shift+f5').upper()
        chain_hk_h    = hk.get('chain',        'shift+f6').upper()
        notes_hk_h    = hk.get('notes',        'shift+f7').upper()
        wb_hk_h       = hk.get('whiteboard',   'shift+f8').upper()
        tr_hk_h       = hk.get('transcribe',   'shift+f9').upper()

        self._hint_prompts_text = (
            f'Click to activate  ·  Double-click to edit  ·  Right-click for menu  ·  {refine_hk} to refine'
        )
        self._hint_macros_text = (
            f'{macro_hk_h} to record  ·  {macro_hk_h} again to stop  ·  {macro_hk_h} once more to play  ·  Esc / Del to abort'
        )
        self._hint_recorder_text = (
            f'{recorder_hk_h} to start / stop recording  ·  1 GB cap auto-stops  ·  Esc to abort'
        )
        self._hint_gif_text = (
            f'{gif_hk_h} to start / stop GIF  ·  Auto-stops at max duration  ·  Esc to abort'
        )
        self._hint_ask_text = (
            f'{ask_hk_h} to explain, select text, copy a screenshot, or type a question below'
        )
        self._hint_web_text = (
            f'{web_hk_h} to open active bookmark  ·  Click any bookmark to open in browser'
        )
        self._hint_chains_text = (
            f'{chain_hk_h} to run the active chain on selected text  ·  Click ✓ to set a chain active'
        )
        self._hint_notes_text = (
            f'{notes_hk_h} to open Quick Notes  ·  All your saved notes live here'
        )
        self._hint_whiteboard_text = (
            f'{wb_hk_h} to open the Whiteboard  ·  Sketch, diagram, brainstorm, offline'
        )
        self._hint_transcribe_text = (
            f'{tr_hk_h} to open Transcribe  ·  Audio/video → text with speakers & summary'
        )
        if hasattr(self, '_hint_slot_text'):
            for _key, _label, _default_hk in self._PLACEHOLDER_SLOTS:
                _h = hk.get(_key, _default_hk).upper()
                self._hint_slot_text[_key] = (
                    f'{_h} reserved for {_label}  ·  Feature coming soon, right-click the tab to rebind'
                )

        # ── Refresh visible labels ────────────────────────────────────────
        # Header: "ALT+SHIFT+W → <active prompt title>"
        try:
            if hasattr(self, '_active_lbl') and self._active_lbl.winfo_exists():
                idx = max(0, min(self.active_idx, len(self.prompts) - 1))
                title = self.prompts[idx]['title'] if self.prompts else ','
                self._active_lbl.configure(text=f'{refine_hk}  →  {title}')
        except Exception:
            pass

        # Hint bar: re-pick the text for the currently-active tab so the
        # hot reload shows immediately without a tab switch.
        try:
            if hasattr(self, '_sync_hint_bar'):
                self._sync_hint_bar()
        except Exception:
            pass

    # Target window size for the Library, same for every tab so geometry
    # stays consistent whether the user opens Prompts, Macros, Transcribe,
    # etc. Tall enough to show the entire Transcribe panel (Source +
    # Operations + Options + Run + first half of the Result card) without
    # the bottom getting cut off by the taskbar. These are CHROME-EXCLUSIVE
    # (Tk client-area) dimensions; the actual window is ~40 px taller and
    # ~16 px wider due to the OS title bar + borders. _snap_into_work_area
    # uses Win32 GetWindowRect to account for the chrome accurately.
    # Tk client-area target. The OS-level window with title bar + borders
    # is ~32 px taller and ~16 px wider. We aim for a height that, once
    # chrome is added, leaves a ~20 px safety margin between the window
    # bottom and the taskbar.
    _DEFAULT_W = 1280
    _DEFAULT_H = 920
    # Hard safety gap kept between the window's OS-level bottom and the
    # work-area bottom (taskbar top). DPI rounding + DWM extended frame
    # shadow can push the visible edge under the taskbar otherwise.
    _SAFETY_GAP = 20

    def _center(self) -> None:
        from win_geometry import center_on_work_area
        self.win.update_idletasks()
        x, y, w, h = center_on_work_area(self._DEFAULT_W, self._DEFAULT_H)
        self.win.geometry(f'{w}x{h}+{x}+{y}')

    def _snap_into_work_area(self) -> None:
        """If the current window (including OS title bar + borders) crosses
        the work-area edge in any direction, nudge it back inside with a
        safety gap. Uses Win32 GetWindowRect / SetWindowPos because Tk's
        winfo_x/y/width/height are client-area numbers and miss the ~32 px
        chrome that is exactly what pushes the bottom under the taskbar.

        Called on every show() and again 200 ms later so DWM has time to
        finalise the window rect after deiconify."""
        try:
            import sys as _sys
            if _sys.platform != 'win32':
                return
            import ctypes
            from ctypes import wintypes
            from win_geometry import get_work_area
            self.win.update_idletasks()
            hwnd = self.win.winfo_id()
            user32 = ctypes.windll.user32
            # restype = c_void_p so HWND results don't truncate to 32-bit
            # on 64-bit Windows. SetWindowPos argtypes match LPARAM signs.
            user32.GetAncestor.restype    = ctypes.c_void_p
            user32.GetAncestor.argtypes   = (ctypes.c_void_p, ctypes.c_uint)
            user32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.RECT))
            user32.SetWindowPos.argtypes  = (ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, ctypes.c_uint)
            top = user32.GetAncestor(ctypes.c_void_p(hwnd), 2)   # GA_ROOT = 2
            if not top: return
            rect = wintypes.RECT()
            if not user32.GetWindowRect(top, ctypes.byref(rect)):
                return
            wa_x, wa_y, wa_w, wa_h = get_work_area()
            # Reserve the safety gap by shrinking the usable work area.
            usable_w = max(100, wa_w)
            usable_h = max(100, wa_h - self._SAFETY_GAP)
            cur_w = rect.right - rect.left
            cur_h = rect.bottom - rect.top
            new_w = min(cur_w, usable_w)
            new_h = min(cur_h, usable_h)
            # Clamp position so the entire rectangle (after sizing) sits
            # inside the work area minus the safety gap.
            max_x = wa_x + usable_w - new_w
            max_y = wa_y + usable_h - new_h
            new_x = max(wa_x, min(rect.left, max_x))
            new_y = max(wa_y, min(rect.top,  max_y))
            if (new_w, new_h, new_x, new_y) != (cur_w, cur_h, rect.left, rect.top):
                # SWP_NOZORDER | SWP_NOACTIVATE = 0x4 | 0x10 = 0x14
                user32.SetWindowPos(top, 0, new_x, new_y, new_w, new_h, 0x14)
        except Exception:
            pass
