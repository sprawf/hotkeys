"""Floating sticky-note window for a single prompt, editable, auto-saves on close."""
import logging
import threading
import tkinter as tk
from typing import Callable

logger = logging.getLogger(__name__)

import spellcheck
from dialogs import alert, confirm, Tooltip, PopupMenu
from theme import (
    FONT_FAMILY, CARD_TEXT, CARD_TEXT_S,
    ACCENT, OK, WARN, _darken,
)


class PromptStickyNote:
    """Small floating window showing one prompt, title and text both editable.

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
                 on_close: Callable[[], None] | None = None,
                 vision_extractor: Callable | None = None) -> None:
        self._prompt            = dict(prompt)
        self._on_save           = on_save
        self._on_close          = on_close
        self._vision_extractor  = vision_extractor
        self._ocr_pending       = False
        self._color    = prompt.get('color', '#FFF9C4')
        # Resize state, initialised here so _resize_move is safe even if a
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

        # Inner container inset 2 px, the 2 px gap shows as an ACCENT border
        self._inner = tk.Frame(self.win, bg=self._color)
        self._inner.pack(fill='both', expand=True, padx=2, pady=2)

        self._ocr_staged_img = None   # image waiting for Enter confirmation

        self._build()
        self._place()
        self.win.bind('<Escape>', self._on_escape)
        self.win.bind('<Return>',  self._on_return_key, add='+')
        self._text.bind('<Return>', self._on_return_key, add='+')
        self._text.focus_set()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self._inner, bg=self._dark, height=36)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)

        # Drag grip, explicit handle so Entry/Button don't block dragging
        grip = tk.Label(hdr, text='⠿', bg=self._dark,
                        fg=_darken(self._color, 0.55),
                        font=(FONT_FAMILY, 13), cursor='fleur', padx=4)
        grip.pack(side='left', padx=(4, 0))
        grip.bind('<ButtonPress-1>', self._drag_start)
        grip.bind('<B1-Motion>',     self._drag_move)

        # Hotkey badge, also draggable
        hk = self._prompt.get('hotkey', '')
        if hk:
            badge = tk.Label(hdr, text=f'  ⌨ {hk.upper()}  ', bg=self._darkest,
                             fg=CARD_TEXT, font=(FONT_FAMILY, 10, 'bold'),
                             relief='flat', cursor='fleur')
            badge.pack(side='left', pady=7)
            badge.bind('<ButtonPress-1>', self._drag_start)
            badge.bind('<B1-Motion>',     self._drag_move)

        # Close button, pinned right
        tk.Button(hdr, text='✕', bg=self._dark, fg=CARD_TEXT,
                  activebackground=self._darkest, activeforeground=CARD_TEXT,
                  relief='flat', font=(FONT_FAMILY, 11), width=2,
                  bd=0, cursor='arrow',
                  command=self.close).pack(side='right', padx=4)

        # OCR button, sits just left of close button
        self._ocr_hdr_btn = tk.Button(
            hdr, text='📷', bg=self._dark, fg=CARD_TEXT,
            activebackground=self._darkest, activeforeground=CARD_TEXT,
            relief='flat', font=(FONT_FAMILY, 11), width=2,
            bd=0, cursor='arrow',
            command=self._ocr_start,
        )
        self._ocr_hdr_btn.pack(side='right')
        Tooltip(self._ocr_hdr_btn,
                'Copy an image to clipboard, then click to extract its text.\n'
                'You can also press Ctrl+V in the note.')

        # Title entry (editable)
        self._title_var = tk.StringVar(value=self._prompt.get('title', ''))
        tk.Entry(hdr, textvariable=self._title_var, bg=self._dark, fg=CARD_TEXT,
                 insertbackground=CARD_TEXT,
                 relief='flat', font=(FONT_FAMILY, 12, 'bold'),
                 bd=0, highlightthickness=0).pack(
            side='left', fill='x', expand=True, padx=(4, 0), pady=4)

        # ── Separator ──────────────────────────────────────────────────────────
        tk.Frame(self._inner, bg=self._darkest, height=1).pack(fill='x')

        # ── Prompt text area, fills all remaining space ──────────────────────
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

        # Ctrl+V: intercept at window level so it fires regardless of which
        # widget inside the note has focus.  add='+' keeps default paste intact.
        self.win.bind('<Control-v>', self._on_ctrl_v, add='+')
        # Right-click: standard Cut/Copy/Paste + image-aware smart paste
        self._text.bind('<Button-3>', self._show_text_context_menu, add='+')

        # ── OCR status strip, overlaid via place(), pinned to inner bottom ────
        self._ocr_status_frame = tk.Frame(self._inner, bg=self._darkest)
        self._ocr_status_lbl = tk.Label(
            self._ocr_status_frame, text='', bg=self._darkest, fg='#ffffff',
            font=(FONT_FAMILY, 10), anchor='w', padx=10,
        )
        self._ocr_status_lbl.pack(side='left', fill='x', expand=True)
        self._ocr_dismiss_btn = tk.Button(
            self._ocr_status_frame, text='✕', bg=self._darkest, fg='#ffffff',
            activebackground=self._color, activeforeground=CARD_TEXT,
            relief='flat', font=(FONT_FAMILY, 9), bd=0, cursor='arrow',
            padx=6, pady=0, command=self._ocr_hide_status,
        )
        # dismiss button packed on demand (errors only)
        self._ocr_status_visible = False

        # ── OCR image preview, overlaid via place(), sits above status strip ───
        self._ocr_preview_frame   = tk.Frame(self._inner, bg=self._color)
        self._ocr_thumb_ref       = None   # keep PhotoImage alive (GC guard)
        self._ocr_preview_lbl     = tk.Label(self._ocr_preview_frame,
                                             bg=self._color, anchor='w')
        self._ocr_preview_lbl.pack(side='left', padx=10)
        self._ocr_preview_visible = False

        # ── Resize grip, floated over bottom-right corner, no strip needed ───
        grip_rsz = tk.Label(self._inner, text='◢',
                            bg=self._color, fg=_darken(self._color, 0.45),
                            font=(FONT_FAMILY, 11), cursor='size_nw_se')
        grip_rsz.place(relx=1.0, rely=1.0, anchor='se')
        grip_rsz.bind('<ButtonPress-1>', self._resize_start)
        grip_rsz.bind('<B1-Motion>',     self._resize_move)

    # ── Context menu ──────────────────────────────────────────────────────────

    def _show_text_context_menu(self, event) -> None:
        """Right-click context menu on the text area."""
        w       = event.widget
        has_sel = bool(w.tag_ranges('sel'))
        def _smart_paste():
            """Paste text normally, or stage image for OCR confirmation."""
            from vision import get_clipboard_image
            logger.info('sticky_note: right-click Paste, checking clipboard')
            img, err = get_clipboard_image()
            logger.info('sticky_note: clipboard → img=%s err=%s', img is not None, err)
            if err:
                self._ocr_show_status(f'⚠  {err}', WARN, dismissable=True)
                return
            if img is not None:
                self._ocr_stage(img)
            else:
                w.event_generate('<<Paste>>')

        (PopupMenu(self.win)
            .add('Cut',    lambda: w.event_generate('<<Cut>>'),  enabled=has_sel)
            .add('Copy',   lambda: w.event_generate('<<Copy>>'), enabled=has_sel)
            .add('Paste',  _smart_paste)
            .show(event.x_root, event.y_root)
        )

    # ── OCR ───────────────────────────────────────────────────────────────────

    def _ocr_stage(self, img) -> None:
        """Show thumbnail and wait for Enter before running OCR."""
        self._ocr_staged_img = img
        self._ocr_show_preview(img)
        self._ocr_show_status('↵ Enter to extract · Esc to cancel', CARD_TEXT, dismissable=True)
        logger.info('sticky_note: staged, preview_visible=%s status_visible=%s',
                    self._ocr_preview_visible, self._ocr_status_visible)

    # Status strip and preview use place() so they overlay at the bottom of
    # self._inner without fighting the text widget's expand=True packing.
    _STATUS_H  = 26   # px, height of the status strip
    _PREVIEW_H = 72   # px, max height of the thumbnail preview strip

    def _ocr_show_preview(self, img) -> None:
        """Show a small thumbnail of the image being sent for extraction."""
        try:
            from PIL import Image, ImageTk
            w, h  = img.size
            max_h = self._PREVIEW_H - 8   # leave a little padding
            if h > max_h:
                scale = max_h / h
                img   = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            self._ocr_thumb_ref = ImageTk.PhotoImage(img)
            self._ocr_preview_lbl.configure(image=self._ocr_thumb_ref)
            if not self._ocr_preview_visible:
                # Overlay: pin to bottom of inner frame, above the status strip
                status_offset = self._STATUS_H if self._ocr_status_visible else 0
                self._ocr_preview_frame.place(
                    x=0, rely=1.0, relwidth=1.0,
                    height=self._PREVIEW_H, anchor='sw',
                    y=-status_offset,
                )
                self._ocr_preview_frame.lift()
                self._ocr_preview_visible = True
        except Exception:
            pass

    def _ocr_hide_preview(self) -> None:
        try:
            if self._ocr_preview_visible:
                self._ocr_preview_frame.place_forget()
                self._ocr_preview_visible = False
        except Exception:
            pass

    def _ocr_show_status(self, text: str, color: str,
                         dismissable: bool = False) -> None:
        """Show or update the status strip pinned to the bottom of the note."""
        try:
            self._ocr_status_lbl.configure(text=text, fg=color)
            if dismissable:
                self._ocr_dismiss_btn.pack(side='right', padx=(0, 4))
            else:
                self._ocr_dismiss_btn.pack_forget()
            if not self._ocr_status_visible:
                # Overlay: pin to very bottom of inner frame
                self._ocr_status_frame.place(
                    x=0, rely=1.0, relwidth=1.0,
                    height=self._STATUS_H, anchor='sw',
                )
                self._ocr_status_frame.lift()   # ensure it renders above the text widget
                self._ocr_status_visible = True
                # If preview is already showing, nudge it up by STATUS_H
                if self._ocr_preview_visible:
                    self._ocr_preview_frame.place_configure(y=-self._STATUS_H)
        except Exception as e:
            logger.error('sticky_note: _ocr_show_status failed: %s', e, exc_info=True)

    def _ocr_hide_status(self) -> None:
        self._ocr_staged_img = None   # cancel any pending confirmation
        self._ocr_hide_preview()
        try:
            self._ocr_status_frame.place_forget()
            self._ocr_status_visible = False
        except Exception:
            pass

    def _on_ctrl_v(self, event) -> None:
        """Intercept Ctrl+V: stage image for confirmation, or fall through to text paste."""
        from vision import get_clipboard_image
        logger.info('sticky_note: Ctrl+V, checking clipboard')
        img, err = get_clipboard_image()
        logger.info('sticky_note: clipboard → img=%s err=%s', img is not None, err)
        if err:
            self._ocr_show_status(f'⚠  {err}', WARN, dismissable=True)
            return 'break'
        if img is None:
            return None   # no image, let default Ctrl+V paste text
        self._ocr_stage(img)
        return 'break'

    def _on_return_key(self, event) -> str | None:
        """Enter: confirm staged image → run OCR. Otherwise insert newline normally."""
        if self._ocr_staged_img is not None:
            img = self._ocr_staged_img
            self._ocr_staged_img = None
            self._ocr_hide_preview()
            self._ocr_hide_status()
            self._ocr_start(img=img)
            return 'break'
        return None   # normal Enter → newline in text widget

    def _on_escape(self, event) -> None:
        """Esc: cancel staged image if one is waiting, otherwise close the note."""
        if self._ocr_staged_img is not None:
            self._ocr_staged_img = None
            self._ocr_hide_preview()
            self._ocr_hide_status()
            return
        self.close()

    def _ocr_start(self, img=None) -> None:
        if self._ocr_pending:
            return
        if self._vision_extractor is None:
            alert(self.win, 'OCR needs a vision provider',
                  'Reading text from images needs an AI provider that can '
                  '"see". Open Settings → AI providers and add an OpenAI, '
                  'Anthropic, Gemini, or Groq key.')
            return

        if img is None:
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
            if err:
                self._ocr_show_status(f'⚠  {err}', WARN, dismissable=True)
                return
            if img is None:
                alert(self.win, 'No image found',
                      'Copy an image to the clipboard,\nthen click 📷 or press Ctrl+V.')
                return

        self._ocr_pending = True
        # Show thumbnail + dim button + status strip
        self._ocr_show_preview(img)
        try:
            self._ocr_hdr_btn.configure(state='disabled',
                                        bg=self._darkest, fg=_darken(CARD_TEXT, 0.5))
        except Exception:
            pass
        self._ocr_show_status('⏳ Extracting…', CARD_TEXT, dismissable=False)

        _img       = img
        _extractor = self._vision_extractor

        def _worker():
            try:
                text = _extractor(_img)
                self.win.after(0, lambda: self._ocr_done(text))
            except Exception as exc:
                self.win.after(0, lambda: self._ocr_error(str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    # ── OCR quality detection ─────────────────────────────────────────────────

    _OCR_REFUSAL_PATTERNS = (
        'no text', 'no readable', 'no visible', 'cannot extract',
        'unable to extract', 'does not contain', 'no text found',
        'there is no text', 'i cannot', "i can't", 'no legible',
        'no written', 'no words', 'this image does not', 'image contains no',
    )
    _OCR_SHORT_THRESHOLD = 15   # chars, below this we warn about sparse extraction

    @classmethod
    def _ocr_quality_issue(cls, text: str) -> str | None:
        """Return an amber warning string if the OCR result looks low-quality, else None."""
        stripped = text.strip()
        lower    = stripped.lower()
        n        = len(stripped)

        # Empty result
        if n == 0:
            return 'No text found'

        # Model returned a refusal/no-text response (short replies only,
        # a real document could legitimately contain these words in passing)
        if n < 200:
            for pat in cls._OCR_REFUSAL_PATTERNS:
                if pat in lower:
                    return 'No text detected'

        # Very short result that isn't a refusal, probably partial
        if n < cls._OCR_SHORT_THRESHOLD:
            return f'Only {n} char{"s" if n != 1 else ""} extracted'

        return None

    def _ocr_done(self, text: str) -> None:
        self._ocr_pending = False
        self._ocr_hide_preview()
        # Restore button
        try:
            self._ocr_hdr_btn.configure(state='normal',
                                        bg=self._dark, fg=CARD_TEXT)
        except Exception:
            pass
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return

        from vision import LONG_TEXT_WARN
        if len(text) > LONG_TEXT_WARN:
            if not confirm(self.win, 'Long text extracted',
                           f'Extracted {len(text)} characters.\nInsert into note?',
                           action_label='Insert'):
                self._ocr_hide_status()
                return

        # Quality check first, refusals ("No text detected", "No text found") are
        # not inserted at all; only genuinely short results get inserted with a warning.
        issue = self._ocr_quality_issue(text)
        if issue in ('No text found', 'No text detected'):
            self._ocr_show_status(f'⚠ {issue}', WARN, dismissable=True)
            return

        try:
            pos = self._text.index('insert')
        except Exception:
            pos = 'end'
        self._text.insert(pos, text)

        if issue:
            self._ocr_show_status(f'⚠ {issue}', WARN, dismissable=True)
        else:
            self._ocr_show_status('✓', OK, dismissable=False)
            self.win.after(1200, self._ocr_hide_status)

    def _ocr_error(self, message: str) -> None:
        self._ocr_pending = False
        self._ocr_hide_preview()
        try:
            self._ocr_hdr_btn.configure(state='normal',
                                        bg=self._dark, fg=CARD_TEXT)
        except Exception:
            pass
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        # Map raw error to a friendly one-liner + optional detail for dialog
        m = message.lower()
        if 'api key' in m or ('invalid' in m and 'key' in m):
            friendly = 'No API key'
            detail   = None
        elif 'rate limit' in m or '429' in m or 'quota' in m:
            friendly = 'Rate limit, try again'
            detail   = None
        elif 'network' in m or 'connect' in m or 'timeout' in m:
            friendly = 'Network error'
            detail   = message
        elif 'no image' in m or 'clipboard' in m:
            friendly = 'No image in clipboard'
            detail   = None
        elif 'access denied' in m or 'antivirus' in m or 'blocked' in m:
            friendly = 'Clipboard blocked'
            detail   = message
        else:
            friendly = message.split('\n')[0][:40]
            detail   = message if len(message) > 40 else None

        self._ocr_show_status(f'✕ {friendly}', '#fca5a5', dismissable=True)

        # For unexpected errors (network, AV blocking, unknown) also pop a dialog
        # so the full reason is visible even if the status strip is easy to miss
        if detail:
            self.win.after(0, lambda: alert(
                self.win,
                'Image extraction failed',
                f'{friendly}\n\nDetail:\n{detail[:300]}',
            ))

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
        # Flash 'Applied ✓' before closing, gives the user clear confirmation
        # that this prompt is now the active one.
        self._flash_applied()

    def _flash_applied(self) -> None:
        """Show a brief green 'Applied ✓' badge for 550 ms, then destroy."""
        try:
            self.win.configure(bg=OK)           # swap border from purple → green
            overlay = tk.Frame(self._inner, bg=OK)
            overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            tk.Label(
                overlay, text='✓   Applied',
                bg=OK, fg=CARD_TEXT,
                font=(FONT_FAMILY, 18, 'bold'),
            ).place(relx=0.5, rely=0.5, anchor='center')
        except Exception:
            pass
        self.win.after(550, self.destroy)

    def destroy(self) -> None:
        if getattr(self, '_destroyed', False):
            return
        self._destroyed = True
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass
        try:
            self.win.destroy()
        except Exception:
            pass
