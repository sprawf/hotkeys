"""
Quick Notes, Shift+F7
Simplenote-inspired: two-column, flat, minimal.
Left , searchable list of saved notes
Right, editor (text / checklist / voice)
"""
import logging
import sys
import threading
import time
import tkinter as tk
import uuid
from datetime import datetime, timedelta
from typing import Callable

import customtkinter as ctk
import numpy as np

from theme import (
    FONT_FAMILY,
    OK, WARN, ERR,
)
from storage import load_notes, save_notes, save_notes_coalesced
from dialogs import PopupMenu, confirm, alert

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
_MAX_REC_S  = 180          # 3-min hard cap on voice recording
_MAX_RENDERED = 250        # max note rows rendered in list (virtual perf cap)

_W, _H  = 1216, 796        # window size, matches Claude desktop app
_LIST_W = 300              # left panel fixed width

# Chat-kind notes (Shift+F4 follow-ups) use the existing
# provider.refine() single-call API; the conversation is packed into
# the user text with "[YOU]" / "[ASSISTANT]" labels. The system prompt
# below is intentionally hard-handed: weaker phrasing made the model
# autocomplete the transcript and hallucinate dozens of fake turns of
# both sides. Marker tokens use [YOU] / [ASSISTANT] (square-bracket
# style) rather than "User:" / "Assistant:" so the input looks less
# like a transcript completion prompt to the underlying LLM.
_CHAT_SYSTEM_PROMPT = (
    'CRITICAL OUTPUT RULES — read carefully:\n'
    '\n'
    'The user message contains a multi-turn conversation transcript '
    'where each turn is prefixed by [YOU] or [ASSISTANT]. The LAST line '
    'prefixed [YOU] is the user\'s CURRENT question. Every earlier '
    'turn is just CONTEXT.\n'
    '\n'
    '1. Output ONLY your reply to the final [YOU] line.\n'
    '2. NEVER prefix your reply with "Assistant:", "[ASSISTANT]", '
    'or any role label. The reply text starts directly.\n'
    '3. NEVER invent additional turns. Do not write "User:" or "[YOU]" '
    'or pretend the user asked anything else.\n'
    '4. NEVER continue the transcript past your single reply.\n'
    '5. Answer concisely — two or three plain-text sentences. No '
    'markdown, no bullet points, no caveats, no conversational filler.\n'
    '\n'
    'If you produce a transcript continuation, role labels, or '
    'multiple turns, you have failed the task.'
)


# Patterns the model still sometimes leaks into the start of its reply
# despite the rules above. We strip them defensively so the user never
# sees "Assistant: ..." prefixes.
_LEADING_LABEL_RE = None  # lazily compiled below

def _clean_chat_reply(text: str) -> str:
    """Strip any leading role label and cut off hallucinated transcript
    continuation. Keeps only the model's reply to the LAST user turn.

    Defensive layer on top of _CHAT_SYSTEM_PROMPT — even with a strong
    prompt, smaller / cheaper LLMs sometimes leak a leading
    "Assistant: " or invent extra "User: ..." turns. We chop both.
    """
    import re
    global _LEADING_LABEL_RE
    if _LEADING_LABEL_RE is None:
        _LEADING_LABEL_RE = re.compile(
            r'^\s*(\[?(assistant|ai|model|bot)\]?\s*[:\-]?\s*)',
            re.IGNORECASE)
    out = text or ''
    # 1. Strip leading role label.
    out = _LEADING_LABEL_RE.sub('', out, count=1)
    # 2. If the model hallucinated a "User:" / "[YOU]" continuation,
    #    cut everything from that marker onward — that's our cue.
    cut_markers = ('\n[YOU]', '\nUser:', '\n[USER]', '\nuser:',
                   '\nYou:', '\nQ:', '\n>>> ', '\nUSER:')
    earliest = len(out)
    for m in cut_markers:
        idx = out.find(m)
        if idx != -1 and idx < earliest:
            earliest = idx
    out = out[:earliest].rstrip()
    return out

# ── Palette, left panel follows app theme; editor stays dark/neutral ─────────
_WIN    = '#0e0e0e'        # outermost bg  (app BG)
_LIST   = '#141414'        # left panel bg (app SURFACE)
_EDIT   = '#1a1a1a'        # right editor bg
_DIV    = '#2a2a2a'        # dividers      (app BORDER)
_HOVER  = '#1e1e1e'        # row hover     (app SURF2)
_SEL_BG = '#20163a'        # selected row  (accent-tinted dark)
_SRCH   = '#1e1e1e'        # search bar bg (app SURF2)

# App accent colours (match theme.py)
_ACCENT  = '#7c3aed'       # primary purple
_ACCENTL = '#9f67fa'       # light purple (pill text)
_SURF2   = '#1e1e1e'       # pill bg
_SURF3   = '#282828'       # pressed/active bg

_SEL_EM = _ACCENT          # star/pin accent

# Text
_T1     = '#f0f0f0'        # primary text  (app TEXT_P)
_T2     = '#909090'        # secondary     (app TEXT_S)
_T3     = '#606060'        # dim / timestamps (app TEXT_D)

# Status
_ERR    = '#ef4444'
_OK_C   = '#22c55e'
_WARN_C = '#f59e0b'
_BLUE   = '#60a5fa'

# Active font family, switched to monospace in light mode
_ACTIVE_FF = FONT_FAMILY   # updated by _set_theme

# ── Palette presets (for light/dark theme switching) ─────────────────────────
_DARK_PALETTE = dict(
    _WIN='#0e0e0e', _LIST='#141414', _EDIT='#1a1a1a',
    _DIV='#2a2a2a', _HOVER='#1e1e1e', _SEL_BG='#20163a',
    _SRCH='#1e1e1e', _SURF2='#1e1e1e', _SURF3='#282828',
    _T1='#f0f0f0', _T2='#909090', _T3='#606060',
    _ACTIVE_FF=FONT_FAMILY,
)
_LIGHT_PALETTE = dict(
    _WIN='#ffffff', _LIST='#f7f7f7', _EDIT='#ffffff',
    _DIV='#e0e0e0', _HOVER='#f0f0f0', _SEL_BG='#ede9fe',
    _SRCH='#ebebeb', _SURF2='#ebebeb', _SURF3='#e5e5e5',
    _T1='#111111', _T2='#555555', _T3='#999999',
    _ACTIVE_FF='Consolas',
)

# ── Color label palette (chip_hex, border_hex, name) ─────────────────────────
NOTE_COLORS = [
    (None,      None,      'None'),
    ('#a78bfa', '#7c3aed', 'Purple'),
    ('#60a5fa', '#2563eb', 'Blue'),
    ('#4ade80', '#16a34a', 'Green'),
    ('#f87171', '#dc2626', 'Red'),
    ('#fbbf24', '#d97706', 'Amber'),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_paste(text: str) -> str:
    """Strip heavy formatting from pasted text, plain body text only.

    Handles clipboard content from Word, Google Docs, web pages, etc.:
    • Normalise line endings  (\r\n / \r  → \n)
    • Line/paragraph separators (U+2028/2029 → \n)
    • Remove invisible/zero-width characters
    • Non-breaking & narrow spaces → regular space
    • Soft hyphen → remove
    • Straighten smart / curly quotes
    • Collapse runs of 3+ blank lines to a single blank line
    """
    import re

    # Line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace(' ', '\n').replace(' ', '\n')

    # Invisible / zero-width characters
    for ch in ('​', '‌', '‍', '⁠', '﻿', '­'):
        text = text.replace(ch, '')

    # Non-breaking and other whitespace-like spaces → regular space
    for ch in (' ', ' ', ' ', ' ', ' ',
               ' ', ' ', '　', ' ', ' ',
               ' ', ' ', ' ', ' ', ' '):
        text = text.replace(ch, ' ')

    # Smart / curly quotes → straight
    text = text.replace('‘', "'").replace('’', "'")   # ' '
    text = text.replace('“', '"').replace('”', '"')   # " "
    text = text.replace('‚', ',').replace('„', '"')   # ‚ „
    text = text.replace('‹', '<').replace('›', '>')   # ‹ ›
    text = text.replace('«', '"').replace('»', '"')   # « »

    # Collapse 3+ consecutive blank lines → one blank line
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text


def _rel_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        d  = datetime.now() - dt
        if d < timedelta(minutes=1): return 'now'
        if d < timedelta(hours=1):   return f'{int(d.total_seconds()//60)}m'
        if d < timedelta(days=1):    return f'{int(d.total_seconds()//3600)}h'
        if d < timedelta(days=7):    return dt.strftime('%a')
        return dt.strftime('%b %d')
    except Exception:
        return ''


def _word_count(text: str) -> str:
    n = len(text.split())
    return f'{n}w' if n else ''


def _note_kind(note: dict) -> str:
    """'chat' for chat-kind notes (Shift+F4 Ask follow-ups), else 'text'.
    Legacy notes without the `kind` field default to 'text', so existing
    data continues to render and save through the original code path."""
    return 'chat' if note.get('kind') == 'chat' else 'text'


def _chat_title_from_messages(messages: list, fallback: str = 'New chat') -> str:
    """First non-empty user message becomes the auto-title for a chat
    note. Truncated to keep list rows tidy."""
    for m in messages or []:
        if m.get('role') == 'user':
            c = (m.get('content') or '').strip()
            if c:
                return c[:80]
    return fallback


def _note_title(note: dict) -> str:
    # Chat-kind: prefer the explicit (editable) title, fall back to the
    # first user message. We do NOT mix in the text-note fallbacks
    # because a chat note has its own fields.
    if _note_kind(note) == 'chat':
        t = (note.get('title') or '').strip()
        if t:
            return t[:48]
        derived = _chat_title_from_messages(note.get('messages', []),
                                            fallback='')
        return derived[:48] if derived else '(new chat)'
    # Text notes: prefer text first line, then checklist, then voice
    raw = note.get('text', '').strip()
    if raw:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if lines:
            return lines[0][:48]
    items = note.get('items', [])
    if items and items[0].get('text'):
        return items[0]['text'][:48]
    voice = note.get('voice', '').strip()
    if voice:
        return voice[:48]
    return '(new note)'


def _note_preview(note: dict) -> str:
    # Chat-kind: last AI line (so the list shows what the chat is about).
    if _note_kind(note) == 'chat':
        msgs = note.get('messages', [])
        for m in reversed(msgs):
            if m.get('role') == 'assistant':
                c = (m.get('content') or '').strip()
                if c:
                    return c.splitlines()[0][:58]
        n = sum(1 for m in msgs if m.get('content'))
        return f'{n} message(s)' if n else 'New chat'
    # Text notes: text body → checklist count → voice snippet
    raw = note.get('text', '').strip()
    if raw:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        return lines[1][:58] if len(lines) > 1 else ''
    items = note.get('items', [])
    if items:
        done = sum(1 for it in items if it.get('checked'))
        return f'{done}/{len(items)} done'
    voice = note.get('voice', '').strip()
    if voice:
        lines = voice.splitlines()
        return lines[0][:58] if lines else ''
    return ''


def _split_text(text: str) -> tuple[str, str]:
    """Split stored text into (title_line, body_rest)."""
    if not text:
        return '', ''
    idx = text.find('\n')
    if idx == -1:
        return text.strip(), ''
    return text[:idx].strip(), text[idx + 1:]


def _attach_tooltip(widget, text: str, delay_ms: int = 500) -> None:
    _job = [None]
    _tip = [None]

    def _show():
        _job[0] = None
        if _tip[0] is not None or not widget.winfo_exists():
            return
        x = widget.winfo_rootx() + widget.winfo_width() // 2
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        win = tk.Toplevel(widget)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg='#1a1a1a')
        tk.Label(win, text=text, bg='#1a1a1a', fg='#b0b0b0',
                 font=(FONT_FAMILY, 9), padx=6, pady=2).pack()
        win.update_idletasks()
        sw = widget.winfo_screenwidth()
        w  = win.winfo_width()
        if x + w > sw - 8:
            x = sw - w - 8
        win.geometry(f'+{x}+{y}')
        _tip[0] = win

    def _hide():
        if _job[0] is not None:
            try: widget.after_cancel(_job[0])
            except Exception: pass
            _job[0] = None
        if _tip[0] is not None:
            try: _tip[0].destroy()
            except Exception: pass
            _tip[0] = None

    def _on_enter(e):
        _hide()
        _job[0] = widget.after(delay_ms, _show)

    widget.bind('<Enter>',         _on_enter,         add='+')
    widget.bind('<Leave>',         lambda e: _hide(), add='+')
    widget.bind('<ButtonPress-1>', lambda e: _hide(), add='+')


# ── Checklist editor ──────────────────────────────────────────────────────────

class _ChecklistEditor(ctk.CTkFrame):

    def __init__(self, parent, items=None, **kw):
        kw.setdefault('fg_color', 'transparent')
        super().__init__(parent, **kw)
        self._rows: list[tuple] = []
        self._items = list(items) if items else [{'text': '', 'checked': False}]
        self._render()

    def get_items(self) -> list[dict]:
        for i, (_, vc, vt, _) in enumerate(self._rows):
            if i < len(self._items):
                self._items[i]['text']    = vt.get()
                self._items[i]['checked'] = bool(vc.get())
        return [it for it in self._items if it['text'].strip()]

    def focus_last(self) -> None:
        if self._rows:
            self._rows[-1][3].focus_set()

    def _render(self) -> None:
        for w in self.winfo_children():
            w.destroy()
        self._rows = []
        for i, item in enumerate(self._items):
            self._make_row(i, item)
        self._make_add_btn()

    def _make_row(self, idx: int, item: dict) -> None:
        row = ctk.CTkFrame(self, fg_color='transparent')
        row.pack(fill='x', pady=1)
        row.columnconfigure(1, weight=1)

        vc = tk.BooleanVar(value=bool(item.get('checked', False)))
        cb = ctk.CTkCheckBox(
            row, text='', variable=vc,
            checkbox_width=15, checkbox_height=15, corner_radius=7,
            fg_color=_BLUE, hover_color='#5a8adc', border_color='#404040',
            command=lambda i=idx, v=vc: self._on_check(i, v.get()),
        )
        cb.grid(row=0, column=0, padx=(2, 8))

        vt = tk.StringVar(value=item.get('text', ''))
        entry = ctk.CTkEntry(
            row, textvariable=vt,
            font=(FONT_FAMILY, 13), fg_color='transparent',
            border_width=0,
            text_color=_T3 if item.get('checked') else _T1,
        )
        entry.grid(row=0, column=1, sticky='ew', padx=(0, 4))
        entry.bind('<Return>',    lambda e, i=idx: self._enter(i))
        entry.bind('<BackSpace>', lambda e, i=idx: self._backspace(i))
        self._rows.append((row, vc, vt, entry))

    def _make_add_btn(self) -> None:
        ctk.CTkButton(
            self, text='＋  Add item', width=90, height=22,
            fg_color='transparent', hover_color=_HOVER,
            text_color=_T3, font=(FONT_FAMILY, 11), corner_radius=3,
            command=self._add_item,
        ).pack(anchor='w', pady=(4, 0))

    def _flush(self) -> None:
        for i, (_, vc, vt, _) in enumerate(self._rows):
            if i < len(self._items):
                self._items[i]['text']    = vt.get()
                self._items[i]['checked'] = bool(vc.get())

    def _on_check(self, idx: int, checked: bool) -> None:
        if idx < len(self._items): self._items[idx]['checked'] = checked
        if idx < len(self._rows):
            self._rows[idx][3].configure(text_color=_T3 if checked else _T1)

    def _enter(self, idx: int) -> None:
        self._flush()
        self._items.insert(idx + 1, {'text': '', 'checked': False})
        self._render()
        if idx + 1 < len(self._rows):
            self._rows[idx + 1][3].focus_set()

    def _backspace(self, idx: int) -> None:
        _, _, vt, _ = self._rows[idx]
        if vt.get() == '' and len(self._items) > 1:
            self._flush()
            self._items.pop(idx)
            self._render()
            prev = max(0, idx - 1)
            if prev < len(self._rows):
                self._rows[prev][3].focus_set()

    def _add_item(self) -> None:
        self._flush()
        self._items.append({'text': '', 'checked': False})
        self._render()
        self.focus_last()


# ── Main window ───────────────────────────────────────────────────────────────

class QuickNotesWindow(ctk.CTkToplevel):

    def __init__(self, root: tk.Tk,
                 transcribe_fn: Callable | None = None,
                 on_close: Callable | None = None,
                 mic_busy_fn: Callable | None = None,
                 vision_extractor: Callable | None = None,
                 provider=None,
                 initial_geometry: str = '',
                 on_geometry_change: Callable | None = None,
                 initial_theme: str = 'light',
                 on_theme_change: Callable | None = None) -> None:
        """
        mic_busy_fn     , optional callable() -> bool; returns True when the main
                           Whisper recorder is active. Quick Notes won't record while it's True.
        vision_extractor, optional callable(img) -> str; Groq vision API extractor.
        provider        , optional LLM provider with .refine(text, prompt) -> str.
        """
        super().__init__(root)
        self._transcribe_fn    = transcribe_fn
        self._provider         = provider
        self._on_close            = on_close
        self._mic_busy_fn         = mic_busy_fn
        self._vision_extractor    = vision_extractor
        self._on_geometry_change  = on_geometry_change
        self._on_theme_change     = on_theme_change
        self._initial_geometry    = initial_geometry

        # Editor state
        self._mode    = 'text'
        self._color   = None
        self._pinned  = False
        self._text_content     = ''
        self._checklist_items  = [{'text': '', 'checked': False}]
        self._voice_transcript = ''
        self._editing_nid: str | None = None   # nid of note open in editor, or None for new
        self._prev_note_nid: str | None = None # nid of note open before current (for swipe-back)

        # ── Chat-kind notes (Shift+F4 follow-ups) ─────────────────────────────
        # Chat state is fully isolated from text-note state above:
        # nothing in the text-note code path touches these, and the
        # chat code path never touches the text-note fields. Lets us
        # keep the rest of Quick Notes' editor untouched.
        self._chat_messages: list = []        # [{role,content}, ...]
        self._chat_title:    str  = ''        # editable, derived from msg[0] if blank
        self._chat_inflight_gen: int = 0      # bump → drop stale completions
        # Reuse the same LLM provider the rest of Hotkeys uses; falls
        # back to None when launched without one (chat send will then
        # render an inline error instead of crashing).
        self._chat_provider = provider
        self._chat_pending:  bool = False     # "Working..." placeholder visible
        # Debounced title save+refresh: cancel-and-reschedule pattern
        # so the left list updates ~200ms after the user stops typing,
        # not on every keystroke (which would hammer save_notes).
        self._chat_title_save_after_id = None
        # User-editable rendered transcript. Stored as the note's
        # `text` field on disk so the user's edits persist; absent on
        # newly created chats (then we render from messages instead).
        self._chat_text_override: str = ''
        self._chat_text_save_after_id = None
        self._chat_text_render_in_progress = False
        # Widget refs; cleared by _show_panel each rebuild.
        self._chat_transcript = None
        self._chat_input      = None
        self._chat_send_btn   = None
        self._chat_stop_btn   = None
        self._chat_title_var  = None
        self._chat_bar_frame  = None

        # OCR state
        self._ocr_pending         = False
        self._ocr_staged_img      = None
        self._ocr_staged_target   = None
        self._ocr_status_visible  = False
        self._ocr_preview_visible = False
        self._ocr_thumb_ref       = None   # PhotoImage GC guard

        # Swipe gesture state
        self._swipe_start_x        = 0
        self._swipe_start_y        = 0
        self._swipe_active         = False
        self._swipe_btn1_down      = False   # track L button ourselves (state flags unreliable)
        self._swipe_btn3_down      = False   # track R button ourselves
        self._swipe_just_completed = False   # suppress spurious right-click after swipe

        # Recording state
        self._rec_state  = 'idle'
        self._rec_stream = None
        self._rec_frames: list = []
        self._rec_start  = 0.0
        self._pulse_job  = None

        # Gesture undo stack
        self._gesture_undo_stack: list = []

        # Window maximize state
        self._maximized   = False
        self._restore_geo: str | None = None

        # Drag
        self._drag_ox = self._drag_oy = 0


        # Search debounce
        self._search_job = None

        # Notes cache (avoids re-reading JSON on every keystroke)
        self._notes_cache: list = []
        self._notes_dirty = True

        # Theme, apply saved palette to module globals before _build()
        self._theme = initial_theme if initial_theme in ('light', 'dark') else 'light'
        _palette = _LIGHT_PALETTE if self._theme == 'light' else _DARK_PALETTE
        _mod = sys.modules[__name__]
        for _k, _v in _palette.items():
            setattr(_mod, _k, _v)

        # Voice decide bar (inline recording confirmation)
        self._voice_decide_bar = None
        self._pending_audio    = None

        # Drag-and-drop state
        self._note_drag_nid: str | None = None   # note row being dragged to editor
        self._drop_indicator     = None    # floating drop-target overlay

        # Search placeholder tracking
        self._srch_placeholder = True
        self._selected_row_widgets: list = []  # widgets of currently selected note row
        self._trash_expanded = False

        # ── Window chrome ─────────────────────────────────────────────────────
        # overrideredirect(True) gives us a borderless frame for the custom
        # title bar, but on Windows it also hides the window from Alt+Tab
        # and the taskbar by default, we restore both via WS_EX_APPWINDOW
        # after the window is realised (see _enable_alt_tab below).
        # We do NOT use -topmost: clicking another app must be able to put
        # Notes in the background (Whiteboard behaves the same way).
        self.withdraw()
        self.overrideredirect(True)
        self.title('Notes')   # taskbar hover-preview + Alt+Tab caption
        self.configure(fg_color=_WIN)
        # Window icon + taskbar promotion handled in _enable_alt_tab()
        # after deiconify(). Setting icon here (iconphoto/iconbitmap)
        # silently no-ops on overrideredirect windows on Win32, and in
        # one tried-and-failed iteration also interfered with the
        # subsequent WS_EX_APPWINDOW transition.

        self._build()
        self._bind_keys()

        # Centering and clamping use the Windows WORK AREA (screen minus
        # taskbar), same helper the Whiteboard uses. The previous code
        # used winfo_screenwidth/height which is the full monitor, so the
        # bottom edge could end up behind the taskbar and the right edge
        # could slide off-screen on multi-monitor setups.
        from win_geometry import center_on_work_area, get_work_area
        wa_x, wa_y, wa_w, wa_h = get_work_area()

        self.update_idletasks()
        if self._initial_geometry:
            # Restore saved geometry, but clamp BOTH size and position to
            # the WORK area. A saved width/height from an older version of
            # this code (or a different monitor) could exceed the current
            # work area; shrink it so the title bar and bottom edge always
            # stay visible.
            try:
                # Parse the full WxH+X+Y from the saved string directly.
                # We can't trust winfo_width()/winfo_x() here — the window
                # is still withdrawn, so Tk reports its internal default
                # frame size (~200x200) instead of the just-applied geo.
                # That round-trip was clamping the real saved size down
                # to ~200x200, which minsize(600,400) then bumped to the
                # smallest legal window.
                import re as _re
                _full = _re.match(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)',
                                  self._initial_geometry)
                if _full:
                    _sw, _sh = int(_full.group(1)), int(_full.group(2))
                    _sx, _sy = int(_full.group(3)), int(_full.group(4))
                    # Reject suspiciously tiny saves so we don't restore
                    # a useless minsize-shaped window forever.
                    if _sw < int(_W * 0.8) or _sh < int(_H * 0.8):
                        logger.info(
                            f'Quick Notes: ignoring tiny saved geometry '
                            f'({_sw}x{_sh}), falling back to default '
                            f'({_W}x{_H}) — likely an accidental resize.'
                        )
                        self._initial_geometry = ''
                    else:
                        # Clamp to the current monitor's work area using
                        # the parsed values, not winfo_*.
                        _sw = min(_sw, wa_w)
                        _sh = min(_sh, wa_h)
                        _sx = max(wa_x, min(_sx, wa_x + wa_w - _sw))
                        _sy = max(wa_y, min(_sy, wa_y + wa_h - _sh))
                        self.geometry(f'{_sw}x{_sh}+{_sx}+{_sy}')
                else:
                    # Malformed save string — wipe so the default path runs.
                    self._initial_geometry = ''
            except Exception:
                self._initial_geometry = ''
        if not self._initial_geometry:
            # First-launch default: identical centering to the Whiteboard
            # so users see the same layout for both Shift+F7 and Shift+F8.
            # center_on_work_area shrinks the requested size if needed so
            # the whole window always fits inside the work area, even on
            # short monitors.
            x, y, W, H = center_on_work_area(_W, _H)
            self.geometry(f'{W}x{H}+{x}+{y}')
        self.minsize(600, 400)

        # Windows 11 rounded corners. Must target the OS top-level HWND;
        # self.winfo_id() returns the inner child HWND for an
        # overrideredirect window, and DwmSetWindowAttribute on the
        # child silently no-ops (this is why the rounded corners never
        # appeared). See win_helpers.top_level_hwnd().
        try:
            import ctypes as _ct
            from win_helpers import top_level_hwnd
            val = _ct.c_int(2)
            _ct.windll.dwmapi.DwmSetWindowAttribute(
                top_level_hwnd(self), 33, _ct.byref(val), _ct.sizeof(val))
        except Exception:
            pass

        self.deiconify()
        self.lift()
        self._enable_native_resize()
        # Promote the overrideredirect window into Alt+Tab + taskbar so
        # the user can switch back after clicking another app. Has to
        # happen after deiconify(), the HWND only exists once mapped.
        self.after(60, self._enable_alt_tab)
        self.after(80, self._focus_content)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Purple accent border (matches Shift+F8 Whiteboard's frame).
        # The window is borderless (overrideredirect=True), so the border is
        # drawn by an outer tk.Frame whose background is the accent color
        # and which pads its inner content by 3px on every edge.
        border = tk.Frame(self, bg=_ACCENT, highlightthickness=0)
        border.pack(fill='both', expand=True)
        outer = tk.Frame(border, bg=_WIN, highlightthickness=0)
        outer.pack(fill='both', expand=True, padx=3, pady=3)
        self._outer = outer  # resize handles are placed inside this frame

        # Left panel (note list)
        left = tk.Frame(outer, bg=_LIST, width=_LIST_W)
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        self._build_left(left)

        # Vertical divider
        tk.Frame(outer, bg=_DIV, width=1).pack(side='left', fill='y')

        # Right panel (editor)
        right = tk.Frame(outer, bg=_EDIT)
        right.pack(side='left', fill='both', expand=True)
        self._build_right(right)

    # ── Left panel ────────────────────────────────────────────────────────────

    def _build_left(self, parent) -> None:
        # Header
        hdr = tk.Frame(parent, bg=_LIST, height=44)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        hdr.bind('<ButtonPress-1>', self._drag_start)
        hdr.bind('<B1-Motion>',     self._drag_move)
        hdr.bind('<Double-Button-1>', lambda e: self._toggle_maximize())

        # Header label, app-style bold section title
        lbl = tk.Label(hdr, text='Notes', bg=_LIST, fg=_T1,
                       font=(FONT_FAMILY, 13, 'bold'))
        lbl.pack(side='left', padx=(14, 0), pady=12)
        # Tk does NOT propagate <Double-Button-1> from a child Label to the
        # parent Frame, so double-clicking right on the 'Notes' text would
        # silently no-op. Re-bind drag + double-click on the label itself
        # so the entire title bar feels like one consistent draggable /
        # maximizable region (matches the native Win11 caption-bar UX).
        lbl.bind('<ButtonPress-1>', self._drag_start)
        lbl.bind('<B1-Motion>',     self._drag_move)
        lbl.bind('<Double-Button-1>', lambda e: self._toggle_maximize())

        # New-note button, large, accent-filled, impossible to miss
        nb = ctk.CTkButton(
            hdr, text='+', width=32, height=32,
            fg_color=_ACCENT, hover_color='#6d28d9',
            text_color='#ffffff', font=(FONT_FAMILY, 15, 'bold'),
            corner_radius=8, cursor='hand2',
            anchor='center',
            command=self._new_note,
        )
        nb.pack(side='right', padx=(0, 10), pady=6)
        _attach_tooltip(nb, 'New note  (saves current)')

        # New-chat button, same size, sits just to the left of New-note.
        # Creates a chat-kind note (Shift+F4 Ask-style follow-up thread).
        cb = ctk.CTkButton(
            hdr, text='💬', width=32, height=32,
            fg_color=_SURF2, hover_color=_SURF3,
            text_color=_T1, font=(FONT_FAMILY, 14),
            corner_radius=8, cursor='hand2',
            anchor='center',
            command=self._new_chat,
        )
        cb.pack(side='right', padx=(0, 4), pady=6)
        _attach_tooltip(cb, 'New chat  (Ask-style follow-up thread)')

        # Separator
        tk.Frame(parent, bg=_DIV, height=1).pack(fill='x')

        # Search bar, CTkEntry for rounded pill look
        sf = tk.Frame(parent, bg=_LIST, pady=8, padx=10)
        sf.pack(fill='x')

        self._srch_entry = ctk.CTkEntry(
            sf, height=28, corner_radius=6,
            fg_color=_SRCH, border_color='#2a2a2a', border_width=1,
            text_color=_T3, placeholder_text='Search notes…',
            placeholder_text_color=_T3,
            font=(FONT_FAMILY, 11),
        )
        self._srch_entry.pack(fill='x')
        self._srch_entry.bind('<FocusIn>',    self._srch_in)
        self._srch_entry.bind('<FocusOut>',   self._srch_out)
        self._srch_entry.bind('<KeyRelease>', self._on_search_key)

        # Separator
        tk.Frame(parent, bg=_DIV, height=1).pack(fill='x')

        # ── Small paste zone, just below search bar ────────────────────────
        pz = tk.Frame(parent, bg=_LIST, height=26)
        pz.pack(fill='x')
        pz.pack_propagate(False)
        pz_lbl = tk.Label(pz, text='  + Paste as note',
                          bg=_LIST, fg=_T3, font=(FONT_FAMILY, 9),
                          anchor='w', cursor='hand2')
        pz_lbl.pack(fill='both', expand=True)
        # Click on the label directly pastes; hover gives feedback
        pz_lbl.bind('<Button-1>',  self._paste_to_list)
        pz_lbl.bind('<Enter>',     lambda e: (pz_lbl.configure(fg=_T1), pz.configure(bg=_SURF3), pz_lbl.configure(bg=_SURF3)))
        pz_lbl.bind('<Leave>',     lambda e: (pz_lbl.configure(fg=_T3), pz.configure(bg=_LIST), pz_lbl.configure(bg=_LIST)))
        for w in (pz, pz_lbl):
            w.bind('<Button-3>',  self._on_list_bg_rclick)
            w.bind('<Control-v>', self._paste_to_list)
            w.bind('<Control-V>', self._paste_to_list)
        tk.Frame(parent, bg=_DIV, height=1).pack(fill='x')

        # ── Trash toggle, bottom-pinned (36px, matches right-panel bottom bar) ─
        self._trash_section = tk.Frame(parent, bg=_LIST, height=36)
        self._trash_section.pack(side='bottom', fill='x')
        self._trash_section.pack_propagate(False)
        # Separator sits above the trash section, packed side='bottom' after the
        # section so it appears just above it (same level as right-panel separator)
        tk.Frame(parent, bg=_DIV, height=1).pack(side='bottom', fill='x')
        self._trash_btn: tk.Label | None = None

        # ── Scrollable note list (fills space above trash) ───────────────────
        lf = tk.Frame(parent, bg=_LIST)
        lf.pack(fill='both', expand=True)

        self._list_canvas = tk.Canvas(lf, bg=_LIST, highlightthickness=0, bd=0)
        self._list_canvas.pack(side='left', fill='both', expand=True)
        # Focus canvas on click so it can receive Ctrl+V
        self._list_canvas.bind('<Button-1>',
            lambda e: self._list_canvas.focus_set(), add='+')
        self._list_canvas.bind('<Control-v>', self._paste_to_list)
        self._list_canvas.bind('<Control-V>', self._paste_to_list)
        self._list_canvas.bind('<Button-3>',  self._on_list_bg_rclick)

        self._list_inner = tk.Frame(self._list_canvas, bg=_LIST)
        self._list_win_id = self._list_canvas.create_window(
            (0, 0), window=self._list_inner, anchor='nw')

        self._list_inner.bind('<Configure>',
            lambda e: self._list_canvas.configure(
                scrollregion=self._list_canvas.bbox('all')))
        self._list_inner.bind('<Button-3>', self._on_list_bg_rclick)
        def _on_canvas_resize(e):
            self._list_canvas.itemconfigure(self._list_win_id, width=e.width)
            # Stretch inner frame to at least the canvas height so
            # the empty area below notes is always right-clickable.
            inner_h = self._list_inner.winfo_reqheight()
            if inner_h < e.height:
                self._list_canvas.itemconfigure(
                    self._list_win_id, height=e.height)
        self._list_canvas.bind('<Configure>', _on_canvas_resize)
        # Scroll routing: on Windows <MouseWheel> goes to the keyboard-focused widget,
        # not the widget under the cursor. Intercept at the window level and forward
        # to the canvas when the cursor is inside the list panel.
        def _on_window_scroll(e):
            try:
                cx = self._list_canvas.winfo_rootx()
                cy = self._list_canvas.winfo_rooty()
                cw = self._list_canvas.winfo_width()
                ch = self._list_canvas.winfo_height()
                if cx <= e.x_root <= cx + cw and cy <= e.y_root <= cy + ch:
                    self._list_canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
            except Exception:
                pass

        # Bind at the toplevel, fires for every scroll anywhere in the window
        self.bind('<MouseWheel>', _on_window_scroll, add='+')

        # Dummy enter/leave stores (no-ops now, kept so _make_note_row doesn't break)
        self._list_enter_fn = lambda e: None
        self._list_leave_fn = lambda e: None

        self._refresh_list()

    def _srch_in(self, _=None) -> None:
        # CTkEntry handles its own placeholder, just update text color on focus
        try: self._srch_entry.configure(text_color=_T1)
        except Exception: pass

    def _srch_out(self, _=None) -> None:
        try:
            val = self._srch_entry.get().strip()
            self._srch_entry.configure(text_color=_T1 if val else _T3)
        except Exception: pass

    def _get_search(self) -> str:
        try:
            val = self._srch_entry.get().strip()
            return val.lower() if val else ''
        except Exception:
            return ''

    def _invalidate_notes_cache(self) -> None:
        """Mark notes cache stale so next _refresh_list re-reads from disk."""
        self._notes_dirty = True

    def _rebuild_trash_section(self, trashed_notes: list) -> None:
        """Rebuild the bottom-pinned trash toggle (always visible, 36px)."""
        for w in self._trash_section.winfo_children():
            w.destroy()
        arrow = '▾' if self._trash_expanded else '▸'
        if trashed_notes:
            label = f'  {arrow} Trash  ({len(trashed_notes)})'
            fg = _T2
            cursor = 'hand2'
        else:
            label = f'  {arrow} Trash'
            fg = _T3
            cursor = 'arrow'
        btn = tk.Label(
            self._trash_section,
            text=label, bg=_LIST, fg=fg,
            font=(FONT_FAMILY, 10), cursor=cursor,
        )
        btn.pack(fill='both', expand=True)
        if trashed_notes:
            _toggle = lambda e: self._toggle_trash_section()
            btn.bind('<Button-1>', _toggle)
            self._trash_section.bind('<Button-1>', _toggle)
            btn.bind('<Enter>', lambda e: btn.configure(fg=_T1))
            btn.bind('<Leave>', lambda e: btn.configure(fg=_T2))

    def _toggle_trash_section(self) -> None:
        self._trash_expanded = not self._trash_expanded
        self._refresh_list()

    # ── Drag and drop ─────────────────────────────────────────────────────────

    def _on_note_row_drag(self, event, nid: str) -> None:
        """Note row dragged rightward into editor → show open indicator."""
        try:
            abs_x = event.widget.winfo_rootx() + event.x
        except Exception:
            return
        win_x = abs_x - self.winfo_rootx()
        if win_x > _LIST_W + 30:
            if self._note_drag_nid != nid:
                self._note_drag_nid = nid
                self._show_note_drag_indicator(nid)
        else:
            if self._note_drag_nid is not None:
                self._note_drag_nid = None
                self._hide_note_drag_indicator()

    def _on_window_mouse_release(self, event) -> None:
        """Complete note-row drag to editor on mouse release."""
        if self._note_drag_nid is not None:
            nid = self._note_drag_nid
            self._note_drag_nid = None
            self._hide_note_drag_indicator()
            rel_x = event.x_root - self.winfo_rootx()
            if rel_x > _LIST_W + 10:
                self._open_note(nid)

    def _show_note_drag_indicator(self, nid: str) -> None:
        """Show a pill in the editor area indicating the note will be opened."""
        try:
            if self._drop_indicator is None or not self._drop_indicator.winfo_exists():
                self._drop_indicator = tk.Label(
                    self._content_host,
                    text='↗ Open note',
                    bg=_ACCENT, fg='#ffffff',
                    font=(FONT_FAMILY, 11, 'bold'),
                    padx=12, pady=6,
                )
            self._drop_indicator.place(relx=0.5, rely=0.5, anchor='center')
            self._drop_indicator.lift()
        except Exception:
            pass

    def _hide_note_drag_indicator(self) -> None:
        try:
            if self._drop_indicator and self._drop_indicator.winfo_exists():
                self._drop_indicator.place_forget()
        except Exception:
            pass

    def _paste_to_list(self, event=None) -> str:
        """Ctrl+V on the list panel.

        Two modes:
          1. Clipboard has IMAGE → create empty note, open it, stage image
             for OCR in the editor. The same _ocr_stage flow the editor's
             own Ctrl+V uses, so user sees the image preview + Enter prompt.
          2. Clipboard has TEXT → create note with that text (legacy behaviour).
        Returns 'break' so the event doesn't propagate.
        """
        # ── Mode 1: clipboard image → new note + OCR staging ─────────────────
        try:
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
            if err:
                logger.warning(f'Quick Notes: clipboard image read error: {err}')
            elif img is not None:
                # Create a fresh empty note and open it, then stage the image.
                note = {
                    'id':         str(uuid.uuid4()),
                    'text':       '',
                    'items':      [{'text': '', 'checked': False}],
                    'voice':      '',
                    'color':      None,
                    'pinned':     False,
                    'created_at': datetime.now().isoformat(timespec='seconds'),
                }
                notes = load_notes()
                notes.append(note)
                save_notes(notes)
                self._invalidate_notes_cache()
                self._refresh_list()
                # Open the new note for editing so _ocr_stage's preview/status
                # lands in a visible context.
                try:
                    self._open_note(note['id'])
                except Exception as exc:
                    logger.warning(f'Quick Notes: open new note for OCR failed: {exc}')
                # Stage the image for OCR on the next idle tick — the editor's
                # widgets need to be mapped first.
                try:
                    self.after_idle(lambda: self._ocr_stage(img))
                except Exception as exc:
                    logger.warning(f'Quick Notes: schedule OCR stage failed: {exc}')
                logger.info('Quick note created from clipboard image (OCR staged)')
                return 'break'
        except Exception as exc:
            logger.warning(f'Quick Notes: clipboard image check failed: {exc}')

        # ── Mode 2: clipboard text → new note ───────────────────────────────
        try:
            raw = self.clipboard_get()
        except tk.TclError:
            return 'break'
        text = _normalize_paste(raw).strip()
        if not text:
            return 'break'
        note = {
            'id':         str(uuid.uuid4()),
            'text':       text,
            'items':      [{'text': '', 'checked': False}],
            'voice':      '',
            'color':      None,
            'pinned':     False,
            'created_at': datetime.now().isoformat(timespec='seconds'),
        }
        notes = load_notes()
        notes.append(note)
        save_notes(notes)
        self._invalidate_notes_cache()
        self._refresh_list()
        logger.info('Quick note created from clipboard paste (%d chars)', len(text))
        return 'break'

    def _refresh_list(self) -> None:
        self._search_job = None
        for w in self._list_inner.winfo_children():
            w.destroy()

        # Load / refresh cache
        if self._notes_dirty:
            self._notes_cache = load_notes()
            self._notes_dirty = False

        # Auto-purge trash older than 30 days
        _cutoff = datetime.now() - timedelta(days=30)
        _purged = [n for n in self._notes_cache
                   if not (n.get('trashed') and
                           datetime.fromisoformat(n.get('trashed_at', '1970-01-01')) < _cutoff)]
        if len(_purged) < len(self._notes_cache):
            save_notes(_purged)
            self._notes_cache = _purged
        all_notes = self._notes_cache

        active_notes  = [n for n in all_notes if not n.get('trashed')]
        trashed_notes = [n for n in all_notes if n.get('trashed')]

        q = self._get_search()
        if q:
            active_notes = [n for n in active_notes
                            if q in n.get('text', '').lower()
                            or q in n.get('voice', '').lower()
                            or any(q in it.get('text', '').lower()
                                   for it in n.get('items', []))
                            or q in _note_title(n).lower()]

        # Pinned first, then newest
        pinned   = [n for n in reversed(active_notes) if n.get('pinned')]
        unpinned = [n for n in reversed(active_notes) if not n.get('pinned')]
        notes    = pinned + unpinned

        # Auto-collapse trash view if it's now empty
        if self._trash_expanded and not trashed_notes:
            self._trash_expanded = False

        if self._trash_expanded:
            # Trash view: show ONLY trashed notes, no active notes
            if not trashed_notes:
                tk.Label(self._list_inner, text='Trash is empty',
                         bg=_LIST, fg=_T3, font=(FONT_FAMILY, 11),
                         ).pack(padx=14, pady=20, anchor='w')
            else:
                for note in reversed(trashed_notes):
                    self._make_note_row(note)
        else:
            # Normal view: active notes only
            if not notes:
                if q:
                    tk.Label(self._list_inner, text='No results',
                             bg=_LIST, fg=_T3, font=(FONT_FAMILY, 11),
                             ).pack(padx=14, pady=20, anchor='w')
                else:
                    # Empty state, show brief usage hints
                    hint_frame = tk.Frame(self._list_inner, bg=_LIST)
                    hint_frame.pack(fill='x', padx=14, pady=(20, 4))
                    tk.Label(hint_frame, text='No notes yet',
                             bg=_LIST, fg=_T2, font=(FONT_FAMILY, 12, 'bold'),
                             anchor='w').pack(fill='x')
                    hints = [
                        'Click + New or start typing →',
                        'Ctrl+V here to paste as note',
                        'Ctrl+V an image → OCR into a note',
                        'Right-click for more options',
                    ]
                    for h in hints:
                        tk.Label(hint_frame, text=h,
                                 bg=_LIST, fg=_T3, font=(FONT_FAMILY, 10),
                                 anchor='w').pack(fill='x', pady=(4, 0))
                    # Voice-memo discovery tip — only shown in the empty
                    # state, where a new user is most likely to look for
                    # ways to add their first note.
                    tip = tk.Label(
                        hint_frame,
                        text='💡  While dictating (Ctrl+Enter), say "memo"\n'
                             '   at the start or end → saves as a note',
                        bg=_LIST, fg=_T2, font=(FONT_FAMILY, 10, 'italic'),
                        justify='left', anchor='w',
                    )
                    tip.pack(fill='x', pady=(12, 0))
            else:
                total = len(notes)
                for note in notes[:_MAX_RENDERED]:
                    self._make_note_row(note)
                if total > _MAX_RENDERED:
                    tk.Label(
                        self._list_inner,
                        text=f'Showing {_MAX_RENDERED} of {total} — search to filter',
                        bg=_LIST, fg=_T3, font=(FONT_FAMILY, 9),
                    ).pack(padx=14, pady=(4, 0), anchor='w')

        # Spacer, always fills remaining height so right-click / Ctrl+V
        # has a generous target area even when the note list is short.
        # Small spacer so the last note isn't flush with the paste zone
        spacer = tk.Frame(self._list_inner, bg=_LIST, height=8)
        spacer.pack(fill='x')

        # Trash toggle pinned to bottom (outside scroll area)
        self._rebuild_trash_section(trashed_notes)

        # Always reset scroll to top so deleting notes doesn't leave a gap
        self._list_canvas.yview_moveto(0.0)

    def _make_note_row(self, note: dict) -> None:
        nid    = note.get('id', '')
        title  = _note_title(note)
        # Tag chat-kind rows visually so the user can tell at a glance.
        # Bare '💬 ' prefix is cheaper than another widget and matches
        # the rest of the list's title-only rendering.
        if _note_kind(note) == 'chat':
            title = '💬  ' + title
        prev   = _note_preview(note)
        ts     = _rel_time(note.get('created_at', ''))
        ntype  = note.get('type', 'text')
        color  = note.get('color')   # border_hex for coloured notes
        pinned = note.get('pinned', False)

        is_selected = (nid == self._editing_nid)
        base_bg  = _SEL_BG if is_selected else _LIST

        # ── Outer row, fixed height so next row snaps to the same Y position ──
        row = tk.Frame(self._list_inner, bg=base_bg, cursor='hand2', height=62)
        row.pack(fill='x')
        row.pack_propagate(False)  # lock height regardless of content

        # ── Left accent bar, always purple; custom color overrides ──────────
        bar_color = color if color else _ACCENT
        tk.Frame(row, bg=bar_color, width=3).pack(side='left', fill='y')

        # ── Content area ─────────────────────────────────────────────────────
        content = tk.Frame(row, bg=base_bg, padx=10, pady=7)
        content.pack(side='left', fill='both', expand=True)

        # Top line: [★] title
        top = tk.Frame(content, bg=base_bg)
        top.pack(fill='x')

        pin_lbl = None
        if pinned:
            pin_lbl = tk.Label(top, text='★', bg=base_bg, fg=_ACCENT,
                               font=(FONT_FAMILY, 9))
            pin_lbl.pack(side='left', padx=(0, 3))

        is_trashed  = note.get('trashed', False)
        is_untitled = (title == '(new note)')
        title_fg = _T3 if (is_trashed or is_untitled) else _T1

        # Action button, pack side='right' BEFORE the expanding title label
        # so Tkinter reserves its space first and it's never crowded out.
        action_fn = (
            (lambda i=nid: self._del_note_permanent(i)) if is_trashed
            else (lambda i=nid: self._trash_note(i))
        )
        trash_cvs = tk.Canvas(top, width=16, height=18, bg=base_bg,
                              highlightthickness=0, cursor='hand2')
        trash_cvs.pack(side='right', padx=(2, 1))

        title_lbl = tk.Label(top, text=title, bg=base_bg, fg=title_fg,
                             font=(_ACTIVE_FF, 13, 'bold'), anchor='w')
        title_lbl.pack(side='left', fill='x', expand=True, padx=(0, 6))

        _trash_fg = ['#3a3a3a']  # mutable cell so closures below can update it

        def _draw_trash(fg: str | None = None, bg: str | None = None) -> None:
            if fg is not None:
                _trash_fg[0] = fg
            if bg is not None:
                trash_cvs.configure(bg=bg)
            trash_cvs.delete('all')
            c = _trash_fg[0]
            # Handle (short bar at top centre)
            trash_cvs.create_line(5.5, 1.5, 10.5, 1.5, fill=c, width=1.5, capstyle='round')
            # Lid (full-width bar)
            trash_cvs.create_line(1, 5, 15, 5, fill=c, width=1.5, capstyle='round')
            # Body rectangle
            trash_cvs.create_rectangle(2, 6.5, 14, 17, outline=c, width=1.5)
            # Three vertical stripes inside body
            for lx in (5.0, 8.0, 11.0):
                trash_cvs.create_line(lx, 9, lx, 15, fill=c, width=1.2)

        _draw_trash()
        trash_cvs.bind('<Button-1>', lambda e, fn=action_fn: fn())

        # ── Bottom row: preview text  +  info pills ───────────────────────
        bot = tk.Frame(content, bg=base_bg)
        bot.pack(fill='x', pady=(3, 0))

        prev_lbl = None
        if prev:
            prev_lbl = tk.Label(bot, text=prev, bg=base_bg, fg=_T2,
                                font=(_ACTIVE_FF, 11), anchor='w')
            prev_lbl.pack(side='left', fill='x', expand=True)

        # ── Hover / selection state ───────────────────────────────────────────
        # IMPORTANT: include prev_lbl. Without it, the preview-text
        # Label silently eats clicks (Tk doesn't bubble events to
        # parents). For chat-kind rows the preview is the last AI reply
        # — a multi-line block that fills most of the row's clickable
        # area. The user then has to hunt for the small title to land
        # a click; symptom was "needs 2-3 clicks to open chat note".
        tk_widgets = [row, content, top, bot, title_lbl]
        if prev_lbl: tk_widgets.append(prev_lbl)
        if pin_lbl:  tk_widgets.append(pin_lbl)
        if prev_lbl: tk_widgets.append(prev_lbl)
        _active = [False]

        hover_fg = _ERR  # 🗑 always red on hover (trash or permanent delete)

        def _set_bg(bg):
            for w in tk_widgets:
                try: w.configure(bg=bg)
                except Exception: pass
            _draw_trash(bg=bg)

        def _enter(e):
            if _active[0]: return
            _active[0] = True
            if not is_selected:
                _set_bg(_HOVER)
                _draw_trash(fg='#5a5a5a')

        def _leave(e):
            try:
                rx, ry = row.winfo_rootx(), row.winfo_rooty()
                rw, rh = row.winfo_width(), row.winfo_height()
                if rx <= row.winfo_pointerx() <= rx+rw and ry <= row.winfo_pointery() <= ry+rh:
                    return
            except Exception: pass
            _active[0] = False
            if not is_selected:
                _set_bg(_LIST)
                _draw_trash(fg='#3a3a3a')

        for w in tk_widgets + [trash_cvs]:
            w.bind('<Enter>', _enter, add='+')
            w.bind('<Leave>', _leave, add='+')

        trash_cvs.bind('<Enter>', lambda e, hfg=hover_fg: _draw_trash(fg=hfg) if _active[0] else _draw_trash(fg='#5a5a5a'), add='+')
        trash_cvs.bind('<Leave>', lambda e: _draw_trash(fg=_T3) if _active[0] else _draw_trash(fg='#3a3a3a'), add='+')

        # ── Click to open / right-click menu ─────────────────────────────────
        def _click(e, i=nid):
            self._open_note(i)

        def _rclick(e, i=nid):
            self._row_context_menu(e, i)

        for w in tk_widgets:
            w.bind('<Button-1>', _click,                  add='+')
            w.bind('<Button-3>', _rclick,                 add='+')
            w.bind('<Enter>',    self._list_enter_fn,     add='+')
            w.bind('<Leave>',    self._list_leave_fn,     add='+')
            w.bind('<B1-Motion>', lambda e, i=nid: self._on_note_row_drag(e, i), add='+')

        # Thin separator
        tk.Frame(self._list_inner, bg=_DIV, height=1).pack(fill='x')

    def _del_note(self, nid: str) -> None:
        self._trash_note(nid)

    def _trash_note(self, nid: str) -> None:
        # Work directly on the cache so refresh is instant (no race with bg save)
        if self._notes_dirty:
            self._notes_cache = load_notes()
        notes = list(self._notes_cache)
        for n in notes:
            if n.get('id') == nid:
                n['trashed']    = True
                n['trashed_at'] = datetime.now().isoformat(timespec='seconds')
                break
        self._notes_cache = notes
        self._notes_dirty = False
        save_notes_coalesced(notes)
        if self._editing_nid == nid:
            self._editing_nid = None
            self._reset_editor()
        else:
            self._refresh_list()

    def _restore_note(self, nid: str) -> None:
        if self._notes_dirty:
            self._notes_cache = load_notes()
        notes = list(self._notes_cache)
        for n in notes:
            if n.get('id') == nid:
                n.pop('trashed', None)
                n.pop('trashed_at', None)
                break
        self._notes_cache = notes
        self._notes_dirty = False
        save_notes_coalesced(notes)
        self._refresh_list()

    def _del_note_permanent(self, nid: str) -> None:
        if self._notes_dirty:
            self._notes_cache = load_notes()
        notes = [n for n in self._notes_cache if n.get('id') != nid]
        self._notes_cache = notes
        self._notes_dirty = False
        save_notes_coalesced(notes)
        if self._editing_nid == nid:
            self._editing_nid = None
            self._reset_editor()
        else:
            self._refresh_list()

    def _open_note(self, nid: str) -> None:
        """Load an existing note into the editor for viewing / editing."""
        if self._editing_nid == nid:
            return  # already open
        # Save any unsaved edits in the CURRENT editor before switching.
        # Previously this only handled the new-draft case; editing an
        # existing note then clicking another row silently dropped the
        # edits. Branches on what's currently open.
        if self._editing_nid is None:
            # Brand-new unsaved draft.
            data = self._get_note_data()
            if data:
                self._save_current_as_new()
        elif self._is_chat_mode():
            # Chat note: persist title + messages.
            self._save_current_chat()
        else:
            # Existing text-kind note: persist text/items/voice/pinned.
            data = self._get_note_data()
            if data:
                try:
                    notes = load_notes()
                    for n in notes:
                        if n.get('id') == self._editing_nid:
                            n['text']   = data['text']
                            n['items']  = data['items']
                            n['voice']  = data['voice']
                            n['pinned'] = self._pinned
                            break
                    self._invalidate_notes_cache()
                    save_notes_coalesced(notes)
                except Exception:
                    logger.exception('save-on-navigate failed for text note')
        self._prev_note_nid = self._editing_nid   # remember for swipe-back
        note = next((n for n in load_notes() if n.get('id') == nid), None)
        if note is None:
            return
        self._editing_nid = nid
        self._pinned      = bool(note.get('pinned', False))
        self._color       = None

        # ── Chat-kind note: skip text-note load path entirely ────────────────
        # This is a fully separate code path from the unified text editor;
        # zero out the text-note fields so a later save can never write
        # chat content into text fields by accident.
        if _note_kind(note) == 'chat':
            self._chat_messages = [dict(m) for m in note.get('messages', [])]
            self._chat_title = (note.get('title') or '').strip()
            # Restore the user-edited rendered transcript if one exists,
            # so opening a chat the user edited preserves their edits.
            self._chat_text_override = note.get('text', '') or ''
            self._chat_inflight_gen += 1   # cancel anything in flight
            self._chat_pending = False
            self._text_content     = ''
            self._checklist_items  = [{'text': '', 'checked': False}]
            self._voice_transcript = ''
            try: self._pin_btn.configure(fg='#d4aa00' if self._pinned else _T3)
            except Exception: pass
            self._show_panel()
            self._refresh_list()
            return

        # ── Unified load with backwards compat ───────────────────────────────
        ntype = note.get('type', 'text')   # legacy field
        if ntype == 'checklist':
            # Old format: items in 'items', text is serialized, ignore serialized text
            self._text_content = ''
            items = note.get('items')
            if items:
                self._checklist_items = [dict(it) for it in items]
            else:
                lines  = note.get('text', '').splitlines()
                parsed = []
                for l in lines:
                    if l.startswith('☑ '):
                        parsed.append({'text': l[2:], 'checked': True})
                    elif l.startswith('☐ '):
                        parsed.append({'text': l[2:], 'checked': False})
                    else:
                        parsed.append({'text': l, 'checked': False})
                self._checklist_items = parsed or [{'text': '', 'checked': False}]
            self._voice_transcript = note.get('voice', '')
        elif ntype == 'voice':
            # Old format: transcript stored in 'text' field
            self._text_content     = ''
            self._checklist_items  = [{'text': '', 'checked': False}]
            self._voice_transcript = note.get('voice', '') or note.get('text', '')
        else:
            # Unified / plain text note
            self._text_content    = note.get('text', '')
            items = note.get('items')
            self._checklist_items = (
                [dict(it) for it in items] if items
                else [{'text': '', 'checked': False}]
            )
            self._voice_transcript = note.get('voice', '')

        # Update pin button
        try: self._pin_btn.configure(fg='#d4aa00' if self._pinned else _T3)
        except Exception: pass
        self._show_panel()
        self._refresh_list()   # re-render rows to highlight selected
        self.after(30, self._focus_content)

    def _save_current_as_new(self) -> None:
        """Persist the current editor draft as a brand-new note (no nid).
        Chat-kind notes have their own persistence path; this method
        is a no-op when a chat note is the current editor."""
        if self._is_chat_mode():
            return
        data = self._get_note_data()
        if not data:
            return
        note = {
            'id':         str(uuid.uuid4()),
            'text':       data['text'],
            'items':      data['items'],
            'voice':      data['voice'],
            'color':      None,
            'pinned':     self._pinned,
            'created_at': datetime.now().isoformat(timespec='seconds'),
        }
        notes = load_notes()
        notes.append(note)
        self._invalidate_notes_cache()
        save_notes(notes)

    def _reset_editor(self) -> None:
        """Clear editor back to a blank new-note state."""
        self._editing_nid      = None
        self._text_content     = ''
        self._checklist_items  = [{'text': '', 'checked': False}]
        self._voice_transcript = ''
        self._color            = None
        self._pinned           = False
        try: self._pin_btn.configure(fg=_T3)
        except Exception: pass
        self._show_panel()
        self._refresh_list()
        self.after(30, self._focus_content)

    def _popup(self) -> 'PopupMenu':
        """Create a PopupMenu pre-coloured for the current light/dark theme."""
        return PopupMenu(self, colors={
            'bg':     _SURF2,
            'border': _DIV,
            'text':   _T1,
            'dim':    _T3,
            'sep':    _DIV,
        })

    def _new_note(self) -> None:
        """Save current note (new draft or update existing), then open a fresh editor."""
        self._prev_note_nid = self._editing_nid   # remember for swipe-back
        # Chat-kind current editor saves itself on every reply; just
        # close out the chat without going through the text-note save
        # flow (which would clobber chat data with empty text fields).
        if self._is_chat_mode():
            self._save_current_chat()
            self._reset_editor()
            return
        data = self._get_note_data()
        if data:
            if self._editing_nid:
                # Update the existing note in place
                notes = load_notes()
                for n in notes:
                    if n.get('id') == self._editing_nid:
                        n['text']   = data['text']
                        n['items']  = data['items']
                        n['voice']  = data['voice']
                        n['pinned'] = self._pinned
                        break
                self._invalidate_notes_cache()
                save_notes_coalesced(notes)
                logger.info('Quick note updated via New')
            else:
                note = {
                    'id':         str(uuid.uuid4()),
                    'text':       data['text'],
                    'items':      data['items'],
                    'voice':      data['voice'],
                    'color':      None,
                    'pinned':     self._pinned,
                    'created_at': datetime.now().isoformat(timespec='seconds'),
                }
                notes = load_notes()
                notes.append(note)
                self._invalidate_notes_cache()
                save_notes_coalesced(notes)
                logger.info('Quick note saved via New')

        self._reset_editor()

    # ── Right-click context menu (note rows) ──────────────────────────────────

    def _on_list_bg_rclick(self, event) -> None:
        """Right-click on empty list background, quick actions."""
        try:
            has_clip = bool(self.clipboard_get().strip())
        except tk.TclError:
            has_clip = False
        all_notes = load_notes()
        active_count  = sum(1 for n in all_notes if not n.get('trashed'))
        trashed_count = sum(1 for n in all_notes if n.get('trashed'))
        m = (self._popup()
            .add('New note',        self._new_note)
            .add('Paste as note',   self._paste_to_list, enabled=has_clip))
        if active_count > 0 or trashed_count > 0:
            m.separator()
        if active_count > 0:
            m.add(f'Move all to Trash  ({active_count})',
                  self._trash_all_notes)
        if trashed_count > 0:
            m.add(f'Empty Trash  ({trashed_count})',
                  self._empty_trash)
        m.show(event.x_root, event.y_root)

    def _trash_all_notes(self) -> None:
        """Move every non-trashed note to Trash. Confirmation required so
        the user can't nuke their notes with a misclick. Pinned notes are
        moved too — pinning was a presentation choice, not a permanence
        guarantee, and excluding them would confuse users who pinned
        everything."""
        notes = load_notes()
        active = [n for n in notes if not n.get('trashed')]
        if not active:
            return
        if not confirm(
            self,
            'Move all notes to Trash?',
            f'This will move {len(active)} note(s) to Trash. '
            'You can restore them from the Trash section at the bottom.',
            action_label='Move all to Trash',
        ):
            return
        from datetime import datetime as _dt
        now = _dt.now().isoformat(timespec='seconds')
        for n in notes:
            if not n.get('trashed'):
                n['trashed'] = True
                n['trashed_at'] = now
        save_notes(notes)
        self._invalidate_notes_cache()
        # If the editor is open on a now-trashed note, close it cleanly.
        try:
            if self._editing_nid:
                self._editing_nid = None
                self._show_panel()
        except Exception: pass
        self._refresh_list()
        logger.info(f'Quick notes: moved {len(active)} notes to Trash.')

    def _empty_trash(self) -> None:
        """Permanently delete every trashed note. Strong confirmation —
        this is not recoverable."""
        notes = load_notes()
        trashed = [n for n in notes if n.get('trashed')]
        if not trashed:
            return
        if not confirm(
            self,
            'Empty Trash?',
            f'Permanently delete {len(trashed)} note(s)? '
            'This cannot be undone.',
            action_label='Empty Trash',
        ):
            return
        kept = [n for n in notes if not n.get('trashed')]
        save_notes(kept)
        self._invalidate_notes_cache()
        self._refresh_list()
        logger.info(f'Quick notes: emptied Trash ({len(trashed)} notes purged).')

    def _row_context_menu(self, event, nid: str) -> None:
        note = next((n for n in load_notes() if n.get('id') == nid), None)
        if note is None:
            return
        pinned   = note.get('pinned', False)
        trashed  = note.get('trashed', False)
        m = self._popup()
        if trashed:
            m.add('Restore', lambda: self._restore_note(nid))
            m.separator()
            m.add('Delete permanently', lambda: self._del_note_permanent(nid))
        else:
            m.add('Open', lambda: self._open_note(nid), enabled=(self._editing_nid != nid))
            m.separator()
            m.add('Unpin' if pinned else 'Pin to top', lambda: self._toggle_note_pin(nid))
            m.separator()
            m.add('Move to Trash', lambda: self._trash_note(nid))
        m.show(event.x_root, event.y_root)

    def _toggle_note_pin(self, nid: str) -> None:
        notes = load_notes()
        for n in notes:
            if n.get('id') == nid:
                n['pinned'] = not n.get('pinned', False)
                break
        self._invalidate_notes_cache()
        save_notes_coalesced(notes)
        # If this note is open in the editor, update the in-memory pin state too
        if self._editing_nid == nid:
            self._pinned = not self._pinned
            try: self._pin_btn.configure(fg='#d4aa00' if self._pinned else _T3)
            except Exception: pass
        self._refresh_list()

    # ── OCR ───────────────────────────────────────────────────────────────────

    _STATUS_H  = 26
    _PREVIEW_H = 72

    def _ocr_stage(self, img, target=None) -> None:
        self._ocr_staged_img    = img
        self._ocr_staged_target = target
        self._ocr_show_preview(img)
        self._ocr_show_status('↵ Extract · Esc Cancel', _T2, dismissable=True)

    def _chat_bar_offset(self) -> int:
        """Pixel height of the chat input bar (Ask follow-up… + Send) so
        OCR overlays can sit ABOVE it in chat mode instead of covering
        it. Returns 0 for regular text notes."""
        bar = getattr(self, '_chat_bar_frame', None)
        if bar is None:
            return 0
        try:
            if not bar.winfo_exists():
                return 0
            self.update_idletasks()
            return bar.winfo_height() or 0
        except Exception:
            return 0

    def _ocr_show_preview(self, img) -> None:
        try:
            from PIL import Image, ImageTk
            w, h  = img.size
            max_h = self._PREVIEW_H - 8
            if h > max_h:
                scale = max_h / h
                img   = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            self._ocr_thumb_ref = ImageTk.PhotoImage(img)
            self._ocr_thumb_lbl.configure(image=self._ocr_thumb_ref)
            if not self._ocr_preview_visible:
                # Preview sits at the very bottom of the content host but
                # above the chat input bar in chat mode (offset by bar
                # height). Status no longer overlaps since it lives in
                # the bottom toolbar.
                bar = self._chat_bar_offset()
                self._ocr_preview_frame.place(
                    x=0, rely=1.0, relwidth=1.0,
                    height=self._PREVIEW_H, anchor='sw', y=-bar,
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

    def _ocr_show_status(self, text: str, color: str, dismissable: bool = False) -> None:
        try:
            self._ocr_status_lbl.configure(text=text, fg=color)
            if dismissable:
                self._ocr_dismiss_btn.pack(side='right', padx=(0, 4))
            else:
                self._ocr_dismiss_btn.pack_forget()
            if not self._ocr_status_visible:
                self._ocr_status_frame.pack(fill='both', expand=True)
                self._ocr_status_visible = True
        except Exception as exc:
            logger.error('quicknotes: _ocr_show_status: %s', exc)

    def _ocr_hide_status(self) -> None:
        self._ocr_staged_img = None
        self._ocr_hide_preview()
        try:
            self._ocr_status_frame.pack_forget()
            self._ocr_status_visible = False
        except Exception:
            pass

    def _on_ctrl_v(self, event) -> str | None:
        from vision import get_clipboard_image
        img, err = get_clipboard_image()
        if err:
            self._ocr_show_status(f'⚠  {err}', _WARN_C, dismissable=True)
            return 'break'
        if img is not None:
            self._ocr_stage(img)
            return 'break'
        # No image, paste as normalized plain text
        try:
            raw = self.clipboard_get()
        except tk.TclError:
            return 'break'
        text = _normalize_paste(raw)
        tb = self._tb._textbox
        # Clear placeholder if active (ph text lives inside the textbox)
        if self._ph:
            self._tb.delete('1.0', 'end')
            self._tb.configure(text_color=_T1)
            self._ph = False
        else:
            try:
                tb.delete('sel.first', 'sel.last')
            except tk.TclError:
                pass
        tb.insert('insert', text)
        return 'break'

    def _on_return_key(self, event) -> str | None:
        if self._ocr_staged_img is not None:
            img = self._ocr_staged_img
            self._ocr_staged_img = None
            self._ocr_hide_preview()
            self._ocr_hide_status()
            self._ocr_start(img=img)
            return 'break'
        return None

    def _on_title_right_click(self, event) -> None:
        w = event.widget
        has_sel = bool(w.selection_present() if hasattr(w, 'selection_present') else False)
        (self._popup()
            .add('Cut',   lambda: w.event_generate('<<Cut>>'),   enabled=has_sel)
            .add('Copy',  lambda: w.event_generate('<<Copy>>'),  enabled=has_sel)
            .add('Paste', lambda: w.event_generate('<<Paste>>'))
            .show(event.x_root, event.y_root))

    def _on_editor_right_click(self, event) -> None:
        # Suppress menu if L is still held, OR if a swipe just completed
        if self._swipe_btn1_down or self._swipe_just_completed:
            self._swipe_just_completed = False
            return
        import webbrowser, urllib.parse, spellcheck as _sc
        w = event.widget
        has_sel = bool(w.tag_ranges('sel'))
        has_text = bool(self._tb_text())
        can_proofread = bool(self._provider and getattr(self._provider, 'ready', False) and has_text)
        proofread_label = 'Proofread selection' if has_sel else 'Proofread'

        # Grab selected text for Google search label
        sel_text = ''
        if has_sel:
            try:
                sel_text = w.get('sel.first', 'sel.last').strip()
            except tk.TclError:
                pass
        if sel_text:
            label = f'Search Google for "{sel_text[:28]}…"' if len(sel_text) > 28 else f'Search Google for "{sel_text}"'
        else:
            label = 'Search Google'

        def _search_google():
            try:
                q = w.get('sel.first', 'sel.last').strip()
            except tk.TclError:
                q = ''
            if q:
                webbrowser.open(f'https://www.google.com/search?q={urllib.parse.quote(q)}')

        def _smart_paste():
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
            if err:
                self._ocr_show_status(f'⚠  {err}', _WARN_C, dismissable=True)
                return
            if img is not None:
                self._ocr_stage(img)
            else:
                w.event_generate('<<Paste>>')

        pm = self._popup()

        # ── Spell suggestions (injected at top when cursor is on a misspelled word) ──
        spell = _sc.get_info(w, event.x, event.y)
        if spell:
            word, ws, we, suggestions = spell
            for s in suggestions:
                pm.add(s, lambda r=s, a=ws, b=we: _sc.apply_suggestion(w, a, b, r))
            pm.separator()
            pm.add('Ignore all',        lambda wrd=word: _sc.ignore_word(w, wrd))
            pm.add('Add to dictionary', lambda wrd=word: _sc.add_word(w, wrd))
            pm.separator()

        (pm
            .add('Cut',               lambda: w.event_generate('<<Cut>>'),   enabled=has_sel)
            .add('Copy',              lambda: w.event_generate('<<Copy>>'),  enabled=has_sel)
            .add('Paste',             lambda: w.event_generate('<<Paste>>'))
            .separator()
            .add(label,               _search_google,                         enabled=has_sel)
            .separator()
            .add(proofread_label,     self._proofread,                        enabled=can_proofread)
            .separator()
            .add('Paste & Extract Image', _smart_paste)
            .show(event.x_root, event.y_root))

    def _proofread(self) -> None:
        """Proofread selected text (or full note if no selection) via LLM."""
        if not self._provider or not getattr(self._provider, 'ready', False):
            return
        tb = self._tb._textbox
        # Decide scope: selection if active, else full note
        try:
            sel_start = tb.index('sel.first')
            sel_end   = tb.index('sel.last')
            text      = tb.get(sel_start, sel_end)
            selection = True
        except tk.TclError:
            text      = self._tb_text()
            sel_start = sel_end = None
            selection = False
        if not text.strip():
            return
        self._set_status('Proofreading…', _BLUE)

        def _run():
            try:
                result = self._provider.refine(
                    text,
                    'Proofread the following text. Fix spelling, grammar, and '
                    'punctuation errors. Preserve the original meaning, tone, '
                    'line breaks, and formatting. Return ONLY the corrected text '
                    ', no explanations, no commentary.',
                )
            except Exception as exc:
                msg = f'⚠ Proofread failed: {exc}'
                self.after(0, lambda m=msg: self._set_status(m, _ERR))
                return

            def _apply():
                try:
                    if selection:
                        tb.delete(sel_start, sel_end)
                        tb.insert(sel_start, result.strip())
                    else:
                        tb.delete('1.0', 'end')
                        tb.insert('1.0', result.strip())
                    self._ph = False
                    self._tb.configure(text_color=_T1)
                    self._set_status('✓ Proofread done', _OK_C)
                    self.after(2000, lambda: self._set_status(''))
                except Exception:
                    pass
            self.after(0, _apply)

        threading.Thread(target=_run, daemon=True).start()

    def _ocr_start(self, img=None) -> None:
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
            if err:
                self._ocr_show_status(f'⚠  {err}', _WARN_C, dismissable=True)
                return
            if img is None:
                alert(self, 'No image found',
                      'Copy an image to the clipboard, then click 📷 or press Ctrl+V.')
                return
        self._ocr_pending = True
        self._ocr_show_preview(img)
        try: self._ocr_btn.configure(fg=_T3)
        except Exception: pass
        self._ocr_show_status('⏳ Extracting…', _T2, dismissable=False)
        _extractor = self._vision_extractor
        def _worker():
            try:
                text = _extractor(img)
                self.after(0, lambda: self._ocr_done(text))
            except Exception as exc:
                # Friendly message: offline → "OCR needs an internet
                # connection"; rate-limited → "Daily limit reached";
                # otherwise → original exception (short form). Avoids
                # surfacing raw tracebacks like "ConnectError(...)".
                try:
                    from engine import friendly_error_message
                    msg = friendly_error_message(exc, feature='OCR')
                except Exception:
                    msg = str(exc)
                self.after(0, lambda m=msg: self._ocr_error(m))
        threading.Thread(target=_worker, daemon=True).start()

    _OCR_REFUSAL_PATTERNS = (
        'no text', 'no readable', 'no visible', 'cannot extract',
        'unable to extract', 'does not contain', 'no text found',
        'there is no text', 'i cannot', "i can't", 'no legible',
        'no written', 'no words', 'this image does not', 'image contains no',
    )
    _OCR_SHORT_THRESHOLD = 15

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
        if n < cls._OCR_SHORT_THRESHOLD:
            return f'Only {n} char{"s" if n != 1 else ""} extracted'
        return None

    def _ocr_done(self, text: str) -> None:
        self._ocr_pending = False
        self._ocr_hide_preview()
        try: self._ocr_btn.configure(fg=_T3)
        except Exception: pass
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        from vision import LONG_TEXT_WARN
        if len(text) > LONG_TEXT_WARN:
            if not confirm(self, 'Long text extracted',
                           f'Extracted {len(text)} characters. Insert into note?',
                           action_label='Insert'):
                self._ocr_hide_status()
                return
        issue = self._ocr_quality_issue(text)
        if issue in ('No text found', 'No text detected'):
            self._ocr_show_status(f'⚠ {issue}', _WARN_C, dismissable=True)
            return
        # Route the extracted text to whichever input is active. Chat-
        # kind notes have a single-line CTkEntry (self._chat_input) for
        # follow-up messages; regular text notes have the editor textbox
        # (self._tb). The OCR staging UI is shared between the two
        # modes, but the destination differs.
        # In chat-kind notes the editor textbox isn't visible — the
        # right pane is the chat transcript + a CTkEntry input. Route
        # the extracted text into that input instead. _is_chat_note()
        # consults the persisted note kind; _chat_input is the entry
        # widget (None when not in chat mode).
        target = self._ocr_staged_target
        self._ocr_staged_target = None
        # Validate target still exists.
        try:
            if target is not None and not target.winfo_exists():
                target = None
        except Exception:
            target = None
        is_chat = False
        try: is_chat = self._is_chat_mode()
        except Exception: pass
        if target is None and is_chat:
            target = getattr(self, '_chat_input', None)
        if target is not None:
            try: pos = target.index('insert')
            except Exception: pos = 'end'
            try:
                target.insert(pos, text)
                target.focus_set()
                # If transcript was the target, persist the edit so the
                # chat note saves to disk.
                if (self._chat_transcript is not None
                        and target is self._chat_transcript._textbox):
                    self._chat_text_override = target.get('1.0', 'end-1c')
                    self._chat_text_flush()
            except Exception:
                logger.exception('quicknotes: _ocr_done insert failed')
        elif self._tb is not None:
            # Regular text-note flow.
            if self._ph:
                self._tb.delete('1.0', 'end')
                self._tb.configure(text_color=_T1)
                self._ph = False
            try:
                pos = self._tb._textbox.index('insert')
            except Exception:
                pos = 'end'
            self._tb.insert(pos, text)
            try: self._update_wcount()
            except Exception: pass
        # Text was inserted: show ✓ Extracted and auto-hide so the strip
        # doesn't sit over the input bar. The "Only N chars" warning was
        # noisy and never auto-dismissed; trust the user to see the text.
        self._ocr_show_status('✓ Extracted', _OK_C, dismissable=False)
        self.after(1400, self._ocr_hide_status)

    def _ocr_error(self, message: str) -> None:
        self._ocr_pending = False
        self._ocr_hide_preview()
        try: self._ocr_btn.configure(fg=_T3)
        except Exception: pass
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        m = message.lower()
        if 'api key' in m or ('invalid' in m and 'key' in m):
            friendly = 'No API key'
        elif 'connect' in m or 'network' in m or 'timeout' in m:
            friendly = 'Offline'
        else:
            friendly = message[:40]
        self._ocr_show_status(f'⚠ {friendly}', _ERR, dismissable=True)

    # ── Swipe gestures (book-page flip) ───────────────────────────────────────

    _SWIPE_MIN_X = 90    # minimum horizontal pixels for a swipe
    _SWIPE_MAX_Y = 45    # maximum vertical pixels (keeps it horizontal)

    def _swipe_press(self, event) -> None:
        # Track button state ourselves, Tkinter's event.state bitmask is
        # populated inconsistently on Windows for the *second* button press.
        if event.num == 1:
            self._swipe_btn1_down = True
            if self._swipe_btn3_down:          # R was already held → both down
                self._swipe_start_x = event.x_root
                self._swipe_start_y = event.y_root
                self._swipe_active  = True
        elif event.num == 3:
            self._swipe_btn3_down = True
            if self._swipe_btn1_down:          # L was already held → both down
                self._swipe_start_x = event.x_root
                self._swipe_start_y = event.y_root
                self._swipe_active  = True

    def _swipe_release(self, event) -> None:
        if event.num == 1:
            self._swipe_btn1_down = False
        elif event.num == 3:
            self._swipe_btn3_down = False

        if not self._swipe_active:
            return
        # Only evaluate the swipe when the FIRST button to come up fires
        # (the second release will arrive with _swipe_active already False)
        self._swipe_active = False
        dx = event.x_root - self._swipe_start_x
        dy = event.y_root - self._swipe_start_y
        if abs(dx) < self._SWIPE_MIN_X or abs(dy) > self._SWIPE_MAX_Y:
            return
        if abs(dy) > abs(dx) * 0.55:
            return
        self._swipe_just_completed = True   # suppress next right-click menu
        if dx < 0:
            self._swipe_forward()

    def _swipe_forward(self) -> None:
        """Right-to-left swipe: save current note, open a fresh blank note."""
        self._push_undo_state()
        try:
            from core.sounds import play_start
            play_start()
        except Exception:
            pass
        self._flip_animate(direction='left', callback=self._new_note)

    def _swipe_back(self) -> None:
        """Left-to-right swipe: open the previously viewed note."""
        if self._prev_note_nid is None:
            return   # nowhere to go back
        self._push_undo_state()
        try:
            from core.sounds import play_flip
            play_flip(reverse=True)
        except Exception:
            pass
        prev = self._prev_note_nid
        def _go():
            self._open_note(prev)
        self._flip_animate(direction='right', callback=_go)

    def _flip_animate(self, direction: str, callback) -> None:
        """Brief page-flip visual: overlay slides off in `direction`, then callback."""
        try:
            w = self._content_host.winfo_width()
            h = self._content_host.winfo_height()
        except Exception:
            callback()
            return

        overlay = tk.Frame(self._content_host, bg=_WIN)
        overlay.place(x=0, y=0, width=w, height=h)
        overlay.lift()

        steps   = 7
        delay   = 18   # ms per step
        sign    = -1 if direction == 'left' else 1
        step_px = w // steps

        def _step(i):
            if not self._content_host.winfo_exists():
                return
            x = sign * step_px * i
            try:
                overlay.place_configure(x=x)
            except Exception:
                return
            if i >= steps:
                try: overlay.destroy()
                except Exception: pass
                callback()
            else:
                self.after(delay, lambda: _step(i + 1))

        _step(1)

    # ── Right panel ───────────────────────────────────────────────────────────

    def _build_right(self, parent) -> None:
        self._right_panel = parent   # needed for OCR overlay placement
        # Top bar: drag handle + pin + close
        tb = tk.Frame(parent, bg=_EDIT, height=40)
        tb.pack(fill='x')
        tb.pack_propagate(False)
        tb.bind('<ButtonPress-1>', self._drag_start)
        tb.bind('<B1-Motion>',     self._drag_move)
        tb.bind('<Double-Button-1>', lambda e: self._toggle_maximize())

        # Right controls: pin + minimize + close
        ctrl = tk.Frame(tb, bg=_EDIT)
        ctrl.pack(side='right', padx=(0, 12), pady=8)
        # Empty pixels between buttons (padding) still belong to the
        # title-bar drag/maximize region — child Buttons absorb their
        # own clicks, but bare ctrl pixels should match the bare tb
        # pixels next to them. Without this, the area between the pin
        # icon and the close icon feels "dead."
        ctrl.bind('<ButtonPress-1>', self._drag_start)
        ctrl.bind('<B1-Motion>',     self._drag_move)
        ctrl.bind('<Double-Button-1>', lambda e: self._toggle_maximize())

        # Pin / star, gold when pinned, dim when not
        self._pin_btn = tk.Label(ctrl, text='★', bg=_EDIT, fg=_T3,
                                  font=(FONT_FAMILY, 14), cursor='hand2')
        self._pin_btn.pack(side='left', padx=(0, 10))
        self._pin_btn.bind('<Button-1>', lambda e: self._toggle_pin())
        self._pin_btn.bind('<Enter>', lambda e: self._pin_btn.configure(fg='#d4aa00'))
        self._pin_btn.bind('<Leave>', lambda e: self._pin_btn.configure(
            fg='#d4aa00' if self._pinned else _T3))
        _attach_tooltip(self._pin_btn, 'Pin to top of list')

        # Minimize, hide without saving
        mn = tk.Label(ctrl, text='–', bg=_EDIT, fg=_T3,
                       font=(FONT_FAMILY, 16), cursor='hand2')
        mn.pack(side='left', padx=(0, 10))
        mn.bind('<Button-1>', lambda e: self.withdraw())
        mn.bind('<Enter>', lambda e: mn.configure(fg=_T1))
        mn.bind('<Leave>', lambda e: mn.configure(fg=_T3))
        _attach_tooltip(mn, 'Minimize  (Shift+F7 to reopen)')

        # Maximize / restore
        self._max_btn = tk.Label(ctrl, text='□', bg=_EDIT, fg=_T3,
                                  font=(FONT_FAMILY, 13), cursor='hand2')
        self._max_btn.pack(side='left', padx=(0, 10))
        self._max_btn.bind('<Button-1>', lambda e: self._toggle_maximize())
        self._max_btn.bind('<Enter>', lambda e: self._max_btn.configure(fg=_T1))
        self._max_btn.bind('<Leave>', lambda e: self._max_btn.configure(fg=_T3))
        _attach_tooltip(self._max_btn, 'Maximize / restore window')

        # Close, save and close
        cl = tk.Label(ctrl, text='×', bg=_EDIT, fg=_T3,
                       font=(FONT_FAMILY, 20), cursor='hand2')
        cl.pack(side='left')
        cl.bind('<Button-1>', lambda e: self._save_and_close())
        cl.bind('<Enter>', lambda e: cl.configure(fg=_ERR))
        cl.bind('<Leave>', lambda e: cl.configure(fg=_T3))
        _attach_tooltip(cl, 'Save & close  (Esc)')

        # Theme toggle, ☀ light / 🌙 dark
        _theme_icon = '☀' if self._theme == 'light' else '🌙'
        self._theme_btn = tk.Label(ctrl, text=_theme_icon, bg=_EDIT, fg=_T3,
                                    font=(FONT_FAMILY, 13), cursor='hand2')
        self._theme_btn.pack(side='left', padx=(14, 0))
        self._theme_btn.bind('<Button-1>', lambda e: self._toggle_theme())
        self._theme_btn.bind('<Enter>', lambda e: self._theme_btn.configure(fg=_T1))
        self._theme_btn.bind('<Leave>', lambda e: self._theme_btn.configure(fg=_T3))
        _attach_tooltip(self._theme_btn, 'Toggle light / dark mode')

        # Separator
        tk.Frame(parent, bg=_DIV, height=1).pack(fill='x')

        # Gesture hint, persistent dim hint bar (survives panel rebuilds)
        _hint = tk.Frame(parent, bg=_EDIT, height=18)
        _hint.pack(fill='x')
        _hint.pack_propagate(False)
        tk.Label(_hint,
                 text='Hold L+R mouse buttons and swipe left to save',
                 bg=_EDIT, fg='#808080',
                 font=(FONT_FAMILY, 9),
                 ).pack(side='right', padx=(0, 18))

        # Content host (fills remaining height above bottom bar)
        self._content_host = tk.Frame(parent, bg=_EDIT)
        self._content_host.pack(fill='both', expand=True)

        self._show_panel()
        self._build_ocr_overlays()

        # Bottom bar
        tk.Frame(parent, bg=_DIV, height=1).pack(fill='x')
        self._build_bottom_bar(parent)

    def _build_bottom_bar(self, parent) -> None:
        bar = tk.Frame(parent, bg=_EDIT, height=36)
        bar.pack(fill='x')
        bar.pack_propagate(False)

        self._chip_canvases = []

        # Left side: inline content tools (Task + Voice)
        lf = tk.Frame(bar, bg=_EDIT)
        lf.pack(side='left', padx=(10, 0), pady=6)

        # ☑ Task, insert checkbox line at cursor
        task_btn = ctk.CTkButton(
            lf, text='☑  Task', width=64, height=24,
            fg_color='transparent', hover_color=_SURF3,
            text_color=_T3, font=(FONT_FAMILY, 11), corner_radius=4,
            command=self._insert_task,
        )
        task_btn.pack(side='left', padx=(0, 4))
        _attach_tooltip(task_btn, 'Insert checklist item at cursor')

        # 🎙 Voice, inline recording button
        self._toolbar_mic = ctk.CTkButton(
            lf, text='🎙  Voice', width=68, height=24,
            fg_color='transparent', hover_color=_SURF3,
            text_color=_T3, font=(FONT_FAMILY, 11), corner_radius=4,
            command=self._toggle_rec,
            state='normal' if self._transcribe_fn else 'disabled',
        )
        self._toolbar_mic.pack(side='left', padx=(0, 4))
        _attach_tooltip(self._toolbar_mic, 'Record voice → transcribe into note')

        # 📷 OCR, capture screen region and insert text
        ocr_btn = ctk.CTkButton(
            lf, text='📷  OCR', width=64, height=24,
            fg_color='transparent', hover_color=_SURF3,
            text_color=_T3, font=(FONT_FAMILY, 11), corner_radius=4,
            command=self._ocr_start,
        )
        ocr_btn.pack(side='left', padx=(0, 4))
        _attach_tooltip(ocr_btn, 'Capture screen region → extract text into note')
        self._ocr_btn = ocr_btn

        # Right side: word count
        rf = tk.Frame(bar, bg=_EDIT)
        rf.pack(side='right', padx=(0, 14), pady=7)

        # Word count
        self._wcount = tk.Label(rf, text='', bg=_EDIT, fg=_T3, font=(FONT_FAMILY, 10))
        self._wcount.pack(side='left')

        # OCR status host: persistent middle slot the OCR status strip
        # packs into when active. Sits between the toolbar buttons and
        # the word count so it doesn't overlap the chat input bar.
        self._ocr_status_host = tk.Frame(bar, bg=_EDIT)
        self._ocr_status_host.pack(side='left', fill='both', expand=True,
                                   padx=(20, 14), pady=4)

        # Status (short messages from _set_status: "Proofreading…", etc.)
        self._status = tk.Label(bar, text='', bg=_EDIT, fg=_T2, font=(FONT_FAMILY, 10))
        self._status.pack(side='left', padx=(14, 0))

    def _show_panel(self) -> None:
        for w in self._content_host.winfo_children():
            w.destroy()
        # Chat-kind widget refs were children of _content_host; the
        # destroy above invalidated them. Null them so any in-flight
        # update_idle work doesn't talk to a dead Tk widget.
        self._chat_transcript = None
        self._chat_input      = None
        self._chat_send_btn   = None
        self._chat_stop_btn   = None
        self._chat_title_var  = None
        self._chat_bar_frame  = None
        # Dispatch on whether we're editing a chat-kind note.
        if self._is_chat_mode():
            self._panel_chat()
        else:
            self._panel_unified()
        # _content_host's children were all destroyed including the OCR
        # overlay widgets (status strip + preview thumbnail). Without
        # rebuilding them, self._ocr_status_lbl points at a dead widget
        # and any subsequent paste / OCR call fails silently with
        # "invalid command name". Rebuild every time the panel rebuilds.
        self._build_ocr_overlays()

    def _is_chat_mode(self) -> bool:
        """True iff the editor is currently showing a chat-kind note.
        Detected by looking up the open note's `kind` in the on-disk
        list; falls back to False on any error so text behavior is
        the safe default."""
        if not self._editing_nid:
            return False
        try:
            note = next((n for n in load_notes()
                         if n.get('id') == self._editing_nid), None)
            return note is not None and _note_kind(note) == 'chat'
        except Exception:
            return False

    def _build_ocr_overlays(self) -> None:
        """(Re)construct the OCR status banner + preview thumbnail as
        children of self._content_host. Called once from the editor's
        initial build and again on every _show_panel() rebuild so the
        widget refs stay live across navigation."""
        # Visibility state always resets — the place_forget'd state of
        # any prior widgets died with them.
        self._ocr_status_visible  = False
        self._ocr_preview_visible = False

        # Status strip lives inline in the bottom toolbar (persistent),
        # NOT over the content host where it would cover the chat input.
        # Rebuilt every panel switch only because the dismiss/auto-hide
        # logic resets visibility state above.
        host = getattr(self, '_ocr_status_host', None) or self._content_host
        self._ocr_status_frame = tk.Frame(host, bg=_EDIT)
        self._ocr_status_lbl = tk.Label(
            self._ocr_status_frame, text='', bg=_EDIT, fg=_T2,
            font=(FONT_FAMILY, 10), anchor='w', padx=8,
        )
        self._ocr_status_lbl.pack(side='left', fill='x', expand=True)
        self._ocr_dismiss_btn = tk.Button(
            self._ocr_status_frame, text='✕', bg=_EDIT, fg=_T3,
            activebackground=_HOVER, activeforeground=_T1,
            relief='flat', font=(FONT_FAMILY, 9), bd=0, cursor='hand2',
            command=self._ocr_hide_status,
        )
        self._ocr_preview_frame = tk.Frame(self._content_host, bg='#1e1e1e')
        self._ocr_thumb_lbl = tk.Label(self._ocr_preview_frame, bg='#1e1e1e', anchor='w')
        self._ocr_thumb_lbl.pack(side='left', padx=10)

    # ── Unified panel ─────────────────────────────────────────────────────────

    def _panel_unified(self) -> None:
        """Single-textbox editor, first line is auto-styled as title (bold, larger).
        No separate title entry. Matches SimpleNote UX: one continuous flow."""
        _PH_BODY  = 'Start writing…'
        _PH_COLOR = '#5a5a5a'

        # Build full content: stored text + inline checklist items + voice
        parts: list[str] = []
        if self._text_content:
            parts.append(self._text_content)
        for it in self._checklist_items:
            if it.get('text', '').strip():
                prefix = '✓ ' if it.get('checked') else '□ '
                parts.append(prefix + it['text'])
        if self._voice_transcript:
            parts.append(self._voice_transcript)
        full_content = '\n'.join(parts)

        # ── Outer container ───────────────────────────────────────────────────
        outer = tk.Frame(self._content_host, bg=_EDIT)
        outer.pack(fill='both', expand=True)

        # ── Single textbox, body font; title_line tag overrides line 1 ──────
        self._tb = ctk.CTkTextbox(
            outer,
            font=(_ACTIVE_FF, 17), fg_color=_EDIT,
            text_color=_T1, border_width=0,
            scrollbar_button_color='#333',
            scrollbar_button_hover_color='#444',
            wrap='word', corner_radius=0,
        )
        self._tb.pack(fill='both', expand=True, padx=18, pady=(14, 4))
        self._tb._textbox.configure(undo=True, maxundo=100)

        # Title tag: line 1 rendered larger and bold
        self._tb._textbox.tag_configure('title_line', font=(_ACTIVE_FF, 20, 'bold'))
        # Task done tag
        self._tb._textbox.tag_configure('task_done', foreground=_T3)

        self._ph = not bool(full_content)
        self._ph_text  = _PH_BODY
        self._ph_color = _PH_COLOR
        if full_content:
            self._tb.insert('1.0', full_content)
            self._tb.configure(text_color=_T1)
        else:
            self._tb.insert('1.0', _PH_BODY)
            self._tb.configure(text_color=_PH_COLOR)

        # Apply tags after widget is rendered
        self.after(10, self._apply_all_task_tags)
        self.after(10, self._apply_title_tag)

        self._tb._textbox.bind('<Key>',         self._ph_key)
        self._tb._textbox.bind('<FocusOut>',    self._ph_out)
        self._tb._textbox.bind('<KeyRelease>',  self._on_key)
        self._tb._textbox.bind('<Control-v>',   self._on_ctrl_v)
        self._tb._textbox.bind('<Control-V>',   self._on_ctrl_v)
        self._tb._textbox.bind('<Return>',      self._on_return_key)
        self._tb._textbox.bind('<Button-3>',    self._on_editor_right_click)
        outer.bind('<Button-3>', self._on_editor_right_click)
        self._tb._textbox.bind('<Control-z>',   self._text_ctrl_z)
        self._tb._textbox.bind('<Control-Z>',   self._text_ctrl_z)
        self._tb._textbox.bind('<ButtonPress-1>',   self._swipe_press,   add='+')
        self._tb._textbox.bind('<ButtonRelease-1>', self._swipe_release, add='+')
        self._tb._textbox.bind('<ButtonPress-3>',   self._swipe_press,   add='+')
        self._tb._textbox.bind('<ButtonRelease-3>', self._swipe_release, add='+')
        # Click on □/✓ prefix to toggle checkbox
        self._tb._textbox.bind('<Button-1>', self._on_body_click, add='+')

        pass  # no spell-check in notes, keep editor clean

    # ── Chat-kind note panel ─────────────────────────────────────────────────

    def _panel_chat(self) -> None:
        """Chat panel for chat-kind notes. Layout:

            ┌──────────────────────────────────────┐
            │ <editable title>                     │
            │ ──────────────────────────────────── │
            │                                      │
            │ You: ...                             │
            │ AI:  ...                             │
            │ ...                                  │   ← scrollable transcript
            │                                      │
            ├──────────────────────────────────────┤
            │ [ Ask follow-up...      ] [Send/Stop]│
            └──────────────────────────────────────┘

        Transcript is a read-only CTkTextbox so the user can select
        and copy any text. Send button becomes Stop while a reply is
        in flight; clicking Stop bumps _chat_inflight_gen so the
        worker's result is dropped on arrival."""
        outer = tk.Frame(self._content_host, bg=_EDIT)
        outer.pack(fill='both', expand=True)

        # ── Single textbox (title line + transcript), matches normal note ──
        # No separate title widget — line 1 is the title, rest is the body,
        # exactly like a regular text note. The title gets the same
        # `title_line` tag treatment (20 bold) the normal editor uses.
        self._chat_title_var = tk.StringVar(
            value=(self._chat_title
                   or _chat_title_from_messages(self._chat_messages)))
        self._chat_transcript = ctk.CTkTextbox(
            outer, font=(_ACTIVE_FF, 17), fg_color=_EDIT,
            text_color=_T1, border_width=0,
            scrollbar_button_color='#333',
            scrollbar_button_hover_color='#444',
            wrap='word', corner_radius=0,
        )
        self._chat_transcript.pack(fill='both', expand=True,
                                    padx=18, pady=(14, 4))
        tb = self._chat_transcript._textbox
        tb.configure(undo=True, maxundo=100)
        tb.tag_configure('title_line', font=(_ACTIVE_FF, 20, 'bold'))
        tb.tag_configure('placeholder', foreground=_T3,
                         font=(_ACTIVE_FF, 17, 'italic'))
        # Standard editor bindings: same right-click menu, smart paste,
        # undo, swipe-save. These mirror the regular text-note editor
        # bindings in _panel_unified so the chat note feels identical
        # to a normal note from the user's perspective.
        tb.bind('<KeyRelease>',        self._on_chat_transcript_keyrelease)
        tb.bind('<Button-3>',          self._on_chat_transcript_rclick)
        tb.bind('<Control-v>',         self._on_chat_transcript_ctrl_v)
        tb.bind('<Control-V>',         self._on_chat_transcript_ctrl_v)
        tb.bind('<Return>',            self._on_chat_transcript_return)
        tb.bind('<Escape>',
            lambda e: (self._ocr_hide_status(), 'break')
                      if self._ocr_staged_img is not None else None)
        tb.bind('<Control-z>',         self._chat_transcript_ctrl_z)
        tb.bind('<Control-Z>',         self._chat_transcript_ctrl_z)
        tb.bind('<ButtonPress-1>',     self._swipe_press,   add='+')
        tb.bind('<ButtonRelease-1>',   self._swipe_release, add='+')
        tb.bind('<ButtonPress-3>',     self._swipe_press,   add='+')
        tb.bind('<ButtonRelease-3>',   self._swipe_release, add='+')

        # ── Bottom input bar ─────────────────────────────────────────────
        bar = tk.Frame(outer, bg=_EDIT)
        bar.pack(fill='x', padx=18, pady=(0, 14))
        # Track the bar so the OCR overlays can sit above it instead of
        # over it when staging an image in a chat note.
        self._chat_bar_frame = bar

        # Multi-line entry would be nice but a single-line CTkEntry is
        # consistent with the rest of the app's input affordances. Enter
        # sends; nothing else.
        self._chat_input = ctk.CTkEntry(
            bar, placeholder_text='Ask follow-up…',
            placeholder_text_color=_T3,
            font=(_ACTIVE_FF, 13), fg_color=_SURF2,
            border_color=_DIV, border_width=1, text_color=_T1,
            height=34,
        )
        self._chat_input.pack(side='left', fill='x', expand=True)
        # Enter: if an image is staged, run OCR (inserts text into THIS
        # input; user then edits + presses Enter again to send). If
        # nothing is staged, send the typed text directly. Matches the
        # regular-notes paste-image → preview → Enter-to-extract flow
        # the user is used to.
        self._chat_input.bind('<Return>', self._on_chat_return)
        # Esc cancels a staged image without sending anything.
        self._chat_input.bind('<Escape>',
            lambda e: (self._ocr_hide_status(), 'break'))
        # Ctrl+V: if clipboard holds an image, stage it for OCR; else
        # let the default <<Paste>> handler insert text as normal.
        # Both the CTk wrapper and the inner Entry get the binding so a
        # paste fires no matter which pixel the keystroke routes to.
        self._chat_input.bind('<Control-v>', self._on_chat_ctrl_v)
        self._chat_input.bind('<Control-V>', self._on_chat_ctrl_v)
        try:
            _inner_v = getattr(self._chat_input, '_entry', None)
            if _inner_v is not None:
                _inner_v.bind('<Control-v>', self._on_chat_ctrl_v)
                _inner_v.bind('<Control-V>', self._on_chat_ctrl_v)
                _inner_v.bind('<Return>',    self._on_chat_return)
                _inner_v.bind('<Escape>',
                    lambda e: (self._ocr_hide_status(), 'break'))
        except Exception: pass
        # Standard Cut / Copy / Paste / Select All right-click menu.
        # Bind on both the CTk wrapper AND the underlying tk.Entry so
        # the menu fires regardless of which pixel the right-click lands
        # on (CTk's frame catches some clicks, the inner Entry catches
        # others).
        try:
            self._chat_input.bind('<Button-3>', self._on_chat_input_rclick)
            inner = getattr(self._chat_input, '_entry', None)
            if inner is not None:
                inner.bind('<Button-3>', self._on_chat_input_rclick)
        except Exception:
            pass

        # Send / Stop button (same slot, label flips during stream).
        self._chat_send_btn = ctk.CTkButton(
            bar, text='Send', width=72, height=34,
            fg_color=_ACCENT, hover_color='#6d28d9',
            text_color='#ffffff', font=(FONT_FAMILY, 12, 'bold'),
            corner_radius=8, command=self._on_chat_send,
        )
        self._chat_send_btn.pack(side='right', padx=(8, 0))

        # Render the existing transcript so a reopen shows past turns.
        self._render_chat_transcript()
        self._chat_input.focus_set()

    def _on_chat_input_rclick(self, event) -> None:
        """Right-click menu for the chat input box: Cut / Copy / Paste /
        Select All. Items dim when not applicable (no selection = no
        Copy/Cut; empty clipboard = no Paste). Matches the editor's
        existing context-menu styling so the rest of Quick Notes looks
        consistent."""
        if self._chat_input is None:
            return
        entry = getattr(self._chat_input, '_entry', None) or self._chat_input
        # Has a selection in the entry?
        try:
            has_sel = entry.selection_present()
        except Exception:
            has_sel = False
        # Anything in the system clipboard? Use Tk's own clipboard_get
        # so we don't depend on pyperclip in this code path.
        try:
            cb = entry.clipboard_get()
            has_clipboard = bool(cb)
        except Exception:
            has_clipboard = False
        try:
            has_text = bool(entry.get())
        except Exception:
            has_text = False

        def _cut():
            try: entry.event_generate('<<Cut>>')
            except Exception: pass

        def _copy():
            try: entry.event_generate('<<Copy>>')
            except Exception: pass

        def _paste():
            # Smart paste: if clipboard holds an image, stage it for
            # OCR (matches Ctrl+V); otherwise paste clipboard text.
            try:
                from vision import get_clipboard_image
                img, err = get_clipboard_image()
                if err:
                    self._ocr_show_status(f'⚠  {err}', _WARN_C, dismissable=True)
                    return
                if img is not None:
                    self._ocr_stage(img, target=self._chat_input)
                    return
            except Exception:
                pass
            try: entry.event_generate('<<Paste>>')
            except Exception: pass

        def _select_all():
            try:
                entry.selection_range(0, 'end')
                entry.icursor('end')
            except Exception:
                pass

        m = self._popup()
        m.add('Cut',         _cut,        enabled=has_sel)
        m.add('Copy',        _copy,       enabled=has_sel)
        m.add('Paste',       _paste,      enabled=has_clipboard)
        m.separator()
        m.add('Select All',  _select_all, enabled=has_text)
        m.show(event.x_root, event.y_root)
        return 'break'

    def _on_chat_transcript_rclick(self, event) -> str | None:
        """Right-click menu for the editable chat transcript. Mirrors
        the regular text-note editor's menu so the chat behaves like a
        normal note: Cut / Copy / Paste / Search Google / Proofread /
        Paste & Extract Image."""
        # Suppress menu if L is still held, OR if a swipe just completed
        if self._swipe_btn1_down or self._swipe_just_completed:
            self._swipe_just_completed = False
            return 'break'
        import webbrowser, urllib.parse
        w = event.widget
        try:
            has_sel = bool(w.tag_ranges('sel'))
        except Exception:
            has_sel = False
        try:
            has_text = bool(w.get('1.0', 'end-1c').strip())
        except Exception:
            has_text = False
        can_proofread = bool(
            self._chat_provider
            and getattr(self._chat_provider, 'ready', False)
            and has_text)
        proofread_label = 'Proofread selection' if has_sel else 'Proofread'

        sel_text = ''
        if has_sel:
            try:
                sel_text = w.get('sel.first', 'sel.last').strip()
            except tk.TclError:
                pass
        if sel_text:
            search_label = (f'Search Google for "{sel_text[:28]}…"'
                            if len(sel_text) > 28
                            else f'Search Google for "{sel_text}"')
        else:
            search_label = 'Search Google'

        def _search_google():
            try:
                q = w.get('sel.first', 'sel.last').strip()
            except tk.TclError:
                q = ''
            if q:
                webbrowser.open(
                    f'https://www.google.com/search?q={urllib.parse.quote(q)}')

        def _smart_paste():
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
            if err:
                self._ocr_show_status(f'⚠  {err}', _WARN_C, dismissable=True)
                return
            if img is not None:
                # Stage image with preview (matches regular-note flow).
                self._ocr_stage(img, target=w)
            else:
                w.event_generate('<<Paste>>')

        import spellcheck as _sc
        pm = self._popup()

        # Spell suggestions: injected at top when cursor sits on a misspelled word.
        spell = _sc.get_info(w, event.x, event.y)
        if spell:
            word, ws, we, suggestions = spell
            for s in suggestions:
                pm.add(s, lambda r=s, a=ws, b=we: _sc.apply_suggestion(w, a, b, r))
            pm.separator()
            pm.add('Ignore all',        lambda wrd=word: _sc.ignore_word(w, wrd))
            pm.add('Add to dictionary', lambda wrd=word: _sc.add_word(w, wrd))
            pm.separator()

        (pm
            .add('Cut',   lambda: w.event_generate('<<Cut>>'),   enabled=has_sel)
            .add('Copy',  lambda: w.event_generate('<<Copy>>'),  enabled=has_sel)
            .add('Paste', lambda: w.event_generate('<<Paste>>'))
            .separator()
            .add(search_label,  _search_google, enabled=has_sel)
            .separator()
            .add(proofread_label, self._chat_proofread,
                 enabled=can_proofread)
            .separator()
            .add('Paste & Extract Image', _smart_paste)
            .show(event.x_root, event.y_root))
        return 'break'

    def _on_chat_ctrl_v(self, event) -> str | None:
        """Smart paste in the chat transcript: image → OCR text;
        anything else → standard text paste."""
        try:
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
            if img is not None:
                self._chat_ocr_extract(img)
                return 'break'
        except Exception:
            pass
        return None   # let Tk do the default text paste

    def _chat_ocr_extract(self, img) -> None:
        """OCR an image into the chat transcript at the cursor."""
        extractor = self._vision_extractor
        if extractor is None:
            self._ocr_show_status(
                '⚠ No vision provider', _WARN_C, dismissable=True)
            return
        self._ocr_show_status('⏳ Extracting…', _T2)
        def _run():
            try:
                text = (extractor(img) or '').strip()
            except Exception as exc:
                self.after(0, lambda: self._ocr_show_status(
                    f'⚠ OCR failed', _ERR, dismissable=True))
                return
            def _apply():
                tb = self._chat_transcript
                if tb is None or not tb.winfo_exists():
                    return
                inner = tb._textbox
                try:
                    inner.insert('insert', text)
                except Exception:
                    pass
                self._chat_text_override = inner.get('1.0', 'end-1c')
                self._chat_text_flush()
                self._ocr_hide_status()
            self.after(0, _apply)
        threading.Thread(target=_run, daemon=True,
                         name='chat-ocr').start()

    def _chat_proofread(self) -> None:
        """Proofread selection (or full transcript) via LLM. Mirrors
        the regular editor's _proofread but targets the chat textbox."""
        if not self._chat_provider or not getattr(
                self._chat_provider, 'ready', False):
            return
        if self._chat_transcript is None:
            return
        tb = self._chat_transcript._textbox
        try:
            sel_start = tb.index('sel.first')
            sel_end   = tb.index('sel.last')
            text      = tb.get(sel_start, sel_end)
            selection = True
        except tk.TclError:
            text      = tb.get('1.0', 'end-1c')
            sel_start = sel_end = None
            selection = False
        if not text.strip():
            return
        self._set_status('Proofreading…', _BLUE)
        def _run():
            try:
                result = self._chat_provider.refine(
                    text,
                    'Proofread the following text. Fix spelling, grammar, and '
                    'punctuation errors. Preserve the original meaning, tone, '
                    'line breaks, and formatting. Return ONLY the corrected text '
                    ', no explanations, no commentary.',
                )
            except Exception as exc:
                msg = f'⚠ Proofread failed: {exc}'
                self.after(0, lambda m=msg: self._set_status(m, _ERR))
                return
            def _apply():
                try:
                    if selection:
                        tb.delete(sel_start, sel_end)
                        tb.insert(sel_start, result.strip())
                    else:
                        tb.delete('1.0', 'end')
                        tb.insert('1.0', result.strip())
                    self._chat_text_override = tb.get('1.0', 'end-1c')
                    self._chat_text_flush()
                    self._set_status('✓ Proofread done', _OK_C)
                    self.after(2000, lambda: self._set_status(''))
                except Exception as exc:
                    self._set_status(f'⚠ Apply failed: {exc}', _ERR)
            self.after(0, _apply)
        threading.Thread(target=_run, daemon=True,
                         name='chat-proofread').start()

    def _render_chat_transcript(self) -> None:
        """Repaint the transcript widget. If a user-edited `text` was
        stored on the note (`self._chat_text_override`), use that as
        the source of truth so the user's edits aren't blown away on
        every redraw. Otherwise, build the initial rendering from the
        messages list with You/AI role labels."""
        if self._chat_transcript is None:
            return
        try:
            if not self._chat_transcript.winfo_exists():
                return
        except Exception:
            return
        tb = self._chat_transcript._textbox
        # Suppress the KeyRelease save handler while we're programmatically
        # rewriting the textbox; otherwise the save would race against
        # the very write that's repopulating it.
        self._chat_text_render_in_progress = True
        try:
            tb.delete('1.0', 'end')
            title = (self._chat_title_var.get() or '').strip()
            override = (self._chat_text_override or '').strip()
            if override:
                body = override
            else:
                parts = []
                for m in self._chat_messages:
                    content = (m.get('content') or '').strip()
                    if content:
                        parts.append(content)
                body = ('\n\n'.join(parts) + '\n\n') if parts else ''
            tb.insert('1.0', f'{title}\n{body}')
            tb.tag_remove('title_line', '1.0', 'end')
            tb.tag_add('title_line', '1.0', '1.end')
            if self._chat_pending:
                tb.insert('end', '…\n\n', 'placeholder')
            tb.see('end')
        finally:
            self._chat_text_render_in_progress = False

    def _on_chat_transcript_keyrelease(self, event) -> None:
        """User edited the title+transcript widget. Line 1 is title, rest is body.
        Both saved on a debounce; title also propagates to the left-panel list."""
        if getattr(self, '_chat_text_render_in_progress', False):
            return
        if self._chat_transcript is None:
            return
        try:
            tb = self._chat_transcript._textbox
            title = tb.get('1.0', '1.end').strip()
            body  = tb.get('2.0', 'end-1c').lstrip('\n')
            tb.tag_remove('title_line', '1.0', 'end')
            tb.tag_add('title_line', '1.0', '1.end')
        except Exception:
            return
        self._chat_text_override = body
        try: self._update_wcount()
        except Exception: pass
        if title != (self._chat_title or '').strip():
            self._chat_title = title
            try:
                self._chat_title_var.set(title)
            except Exception:
                pass
            self._on_chat_title_change()
        try:
            if getattr(self, '_chat_text_save_after_id', None) is not None:
                self.after_cancel(self._chat_text_save_after_id)
        except Exception:
            pass
        self._chat_text_save_after_id = self.after(
            300, self._chat_text_flush)

    def _chat_text_flush(self) -> None:
        """Debounced sync save of the chat's user-edited transcript."""
        self._chat_text_save_after_id = None
        nid = self._editing_nid
        if not nid:
            return
        text = self._chat_text_override or ''
        try:
            notes = load_notes()
            for n in notes:
                if n.get('id') == nid:
                    n['kind'] = 'chat'
                    n['text'] = text   # user-edited rendered transcript
                    break
            self._invalidate_notes_cache()
            save_notes(notes)
        except Exception:
            logger.exception('chat text flush: save failed')

    def _on_chat_title_change(self) -> None:
        """User typed in the title bar — propagate to in-memory state
        and schedule a debounced disk save + list refresh so the left
        panel reflects the new title shortly after they stop typing.
        Without the debounce we'd hammer save_notes() on every key."""
        try:
            self._chat_title = (self._chat_title_var.get() or '').strip()
        except Exception:
            return
        # Cancel pending save+refresh, schedule a new one 200ms out.
        try:
            if self._chat_title_save_after_id is not None:
                self.after_cancel(self._chat_title_save_after_id)
        except Exception:
            pass
        self._chat_title_save_after_id = self.after(
            200, self._chat_title_flush)

    def _chat_title_flush(self) -> None:
        """Debounced SYNC save + left-list refresh. We can't reuse the
        async _save_current_chat() here because the list reads from
        disk and would race the background save thread; the user
        would see the right-panel title update instantly but the left
        list would lag a frame. The save is a small JSON write, fast
        enough to do inline."""
        self._chat_title_save_after_id = None
        nid = self._editing_nid
        if not nid:
            return
        messages = list(self._chat_messages)
        title = (self._chat_title or
                 _chat_title_from_messages(messages, fallback=''))
        pinned = bool(self._pinned)
        try:
            notes = load_notes()
            for n in notes:
                if n.get('id') == nid:
                    n['kind']     = 'chat'
                    n['messages'] = messages
                    n['title']    = title
                    n['pinned']   = pinned
                    break
            self._invalidate_notes_cache()
            save_notes(notes)
        except Exception:
            logger.exception('chat title flush: save failed')
            return
        try: self._refresh_list()
        except Exception: pass

    def _chat_transcript_ctrl_z(self, _event=None) -> str:
        """Ctrl+Z on the chat transcript: try widget undo, else fall through
        so the window-level gesture undo handler can fire (mirrors
        _text_ctrl_z for regular notes)."""
        try:
            self._chat_transcript._textbox.edit_undo()
            return 'break'
        except Exception:
            return None

    def _active_body_textbox(self):
        """Return whichever body textbox is currently live: the regular
        editor textbox (text-note mode) or the chat transcript textbox
        (chat-note mode). Used by Task / Voice / OCR toolbar buttons so
        they work uniformly in both modes."""
        if self._tb is not None:
            try:
                if self._tb._textbox.winfo_exists():
                    return self._tb._textbox
            except Exception: pass
        if self._chat_transcript is not None:
            try:
                if self._chat_transcript._textbox.winfo_exists():
                    return self._chat_transcript._textbox
            except Exception: pass
        return None

    def _on_chat_transcript_ctrl_v(self, _event=None):
        """Ctrl+V on the chat transcript textbox: if clipboard holds an
        image, stage it with the transcript as the target (OCR text gets
        inserted at the transcript cursor). Else None → default text paste."""
        try:
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
        except Exception:
            return None
        if err:
            self._ocr_show_status(f'⚠  {err}', _WARN_C, dismissable=True)
            return 'break'
        if img is not None:
            tgt = self._chat_transcript._textbox if self._chat_transcript else None
            self._ocr_stage(img, target=tgt)
            return 'break'
        return None

    def _on_chat_transcript_return(self, _event=None):
        """Enter on the chat transcript widget. If a clipboard image is
        staged (after Ctrl+V), run OCR (text routed by _ocr_done to the
        chat input). Otherwise return None so Tk inserts a newline."""
        if self._ocr_staged_img is not None:
            img = self._ocr_staged_img
            self._ocr_staged_img = None
            self._ocr_hide_preview()
            self._ocr_hide_status()
            self._ocr_start(img=img)
            return 'break'
        return None

    def _on_chat_return(self, _event=None) -> str:
        """Enter handler for the chat input. Stages-first semantics:
        if a clipboard image has been pasted and is sitting in the
        preview pane, run OCR (result appears in this input — user
        edits / confirms / presses Enter again to send). Otherwise the
        typed text is sent as a normal chat message.

        Returns 'break' so the default Tk Entry binding doesn't also
        try to handle the keystroke (which would beep or duplicate).
        """
        if self._ocr_staged_img is not None:
            img = self._ocr_staged_img
            self._ocr_staged_img = None
            self._ocr_hide_preview()
            self._ocr_hide_status()
            self._ocr_start(img=img)
            return 'break'
        self._on_chat_send()
        return 'break'

    def _on_chat_ctrl_v(self, _event=None) -> str | None:
        """Ctrl+V on the chat input. If clipboard has an image, stage
        it for OCR (same preview UX as regular notes). Else return None
        so the default <<Paste>> binding inserts the clipboard text."""
        if self._chat_input is None:
            return None
        try:
            from vision import get_clipboard_image
            img, err = get_clipboard_image()
        except Exception:
            return None
        if err:
            self._ocr_show_status(f'⚠  {err}', _WARN_C, dismissable=True)
            return 'break'
        if img is not None:
            self._ocr_stage(img, target=self._chat_input)
            return 'break'
        # No image — let default text-paste happen.
        return None

    def _on_chat_send(self) -> None:
        """User pressed Enter / clicked Send. Append the user message,
        re-render, kick off a worker that calls provider.refine() with
        the full conversation, then renders the assistant reply on
        completion.

        Captures the target chat note id at send time so a reply that
        lands AFTER the user has navigated to another note still saves
        to the correct chat, instead of stamping `kind='chat'` and the
        chat history onto whatever note happens to be open."""
        if self._chat_input is None:
            return
        try:
            text = self._chat_input.get().strip()
        except Exception:
            return
        if not text:
            return                       # ignore empty / whitespace
        if self._chat_pending:
            return                       # one stream at a time
        provider = self._chat_provider
        if provider is None:
            # main.py wires this on every note open. If it's missing,
            # something's badly broken; show an inline error.
            self._append_chat_error(
                'AI provider not connected. Reopen Quick Notes via Shift+F7.')
            return
        # Append user message + clear the input
        self._chat_messages.append({'role': 'user', 'content': text})
        # If the user has been editing the transcript, append the new
        # turn to their edited text so their edits aren't blown away
        # by the re-render. If the override is empty, the render falls
        # back to building from messages and picks up the new turn
        # automatically.
        if self._chat_text_override:
            extra = self._chat_text_override.rstrip()
            if extra:
                extra += '\n\n'
            self._chat_text_override = f'{extra}{text}\n\n'
            self._chat_text_flush()
        try:
            self._chat_input.delete(0, 'end')
        except Exception:
            pass
        # Set pending state & switch button to Stop
        self._chat_pending = True
        self._chat_inflight_gen += 1
        my_gen = self._chat_inflight_gen
        my_target_nid = self._editing_nid    # bind target at send time
        self._flip_send_to_stop()
        self._render_chat_transcript()
        self._save_current_chat()   # persist the user message immediately

        # Build the conversation blob for the existing single-call refine().
        # Engine has no chat-messages API; we serialise the turns into
        # one user-message body with role tags and rely on the system
        # prompt below to instruct the model how to read it.
        convo_blob = self._serialise_chat_for_refine()

        def _worker():
            try:
                answer = provider.refine(convo_blob, _CHAT_SYSTEM_PROMPT)
            except Exception as exc:
                self.after(0, lambda e=exc:
                            self._on_chat_error(my_gen, my_target_nid, e))
                return
            self.after(0, lambda a=answer:
                        self._on_chat_reply(my_gen, my_target_nid, a))

        threading.Thread(target=_worker, daemon=True,
                          name='chat-worker').start()

    def _on_chat_stop(self) -> None:
        """Cancel the in-flight LLM call. Python threads can't actually
        be killed, so we bump the gen counter; the worker will check
        on return and drop its result."""
        self._chat_inflight_gen += 1
        self._chat_pending = False
        self._flip_stop_to_send()
        # Drop the placeholder. The user's question stays in the
        # transcript (they typed it; not our place to delete).
        self._render_chat_transcript()

    def _on_chat_reply(self, gen: int, target_nid: str, answer: str) -> None:
        """Worker callback: append assistant reply to the chat note that
        ISSUED the request, even if the user has since navigated away.

        Two cases:
          (a) Still editing the same chat note → update in-memory state
              and re-render the transcript.
          (b) Navigated away → persist the reply to disk against
              target_nid only; never touch UI or _editing_nid. Prevents
              the reply from corrupting whatever note is now open.

        The gen check still handles "Stop pressed before reply arrived"."""
        # Defensive cleanup: strip leading "Assistant:" labels and
        # chop hallucinated "User:" / "[YOU]" continuations. The
        # system prompt forbids both but smaller models still leak.
        cleaned = _clean_chat_reply(answer or '').strip()

        same_target = (self._editing_nid == target_nid
                       and self._is_chat_mode())
        if same_target:
            # Still on the chat that asked. Only drop on explicit Stop.
            if gen != self._chat_inflight_gen or not self._chat_pending:
                return
            self._chat_pending = False
            self._chat_messages.append({'role': 'assistant',
                                        'content': cleaned})
            # Mirror the append into the user-edited override so the
            # render keeps showing both the user's edits AND the new
            # AI turn. If no override yet (user hasn't edited), the
            # render rebuilds from messages naturally.
            if self._chat_text_override:
                extra = self._chat_text_override.rstrip()
                if extra:
                    extra += '\n\n'
                self._chat_text_override = f'{extra}{cleaned}\n\n'
                self._chat_text_flush()
            self._flip_stop_to_send()
            self._render_chat_transcript()
            self._save_current_chat()
        else:
            # User navigated away. Append the reply to the target chat
            # on disk WITHOUT touching the current editor's state.
            self._save_reply_to_chat_nid_async(target_nid, cleaned)
        # Refresh list either way so the preview/title update.
        try: self._refresh_list()
        except Exception: pass

    def _on_chat_error(self, gen: int, target_nid: str,
                       exc: Exception) -> None:
        from engine import friendly_error_message
        msg = friendly_error_message(exc, feature='Chat')
        short = msg.split('\n')[0][:160]
        same_target = (self._editing_nid == target_nid
                       and self._is_chat_mode())
        if same_target:
            if gen != self._chat_inflight_gen:
                return
            self._chat_pending = False
            self._flip_stop_to_send()
            self._append_chat_error(short)
        else:
            # Save the error message to the target chat's history so
            # the user sees it when they reopen the chat.
            self._save_reply_to_chat_nid_async(
                target_nid, f'[error] {short}')

    def _save_reply_to_chat_nid_async(self, nid: str,
                                       assistant_content: str) -> None:
        """Append a single assistant message to a chat note's stored
        messages, by nid, without touching in-memory editor state.
        Used by _on_chat_reply when the user navigated away while a
        reply was in flight. The chat note's history stays consistent
        even though the editor is showing something else."""
        def _persist():
            try:
                notes = load_notes()
                changed = False
                for n in notes:
                    if n.get('id') == nid and _note_kind(n) == 'chat':
                        msgs = list(n.get('messages', []))
                        msgs.append({'role': 'assistant',
                                     'content': assistant_content})
                        n['messages'] = msgs
                        changed = True
                        break
                if changed:
                    save_notes(notes)
            except Exception:
                logger.exception('save_reply_to_chat_nid: failed')
        self._invalidate_notes_cache()
        threading.Thread(target=_persist, daemon=True,
                          name='chat-reply-save').start()

    def _append_chat_error(self, text: str) -> None:
        """Show an inline error as the next assistant turn. Persisted
        like any other message so the user can see what went wrong
        when they reopen the chat."""
        self._chat_messages.append({'role': 'assistant',
                                    'content': f'[error] {text}'})
        self._render_chat_transcript()
        self._save_current_chat()

    def _serialise_chat_for_refine(self) -> str:
        """Pack the conversation into the single text input the
        existing provider.refine() API takes. Uses [YOU] / [ASSISTANT]
        markers (not "User: " / "Assistant: ") because the latter look
        too much like a transcript-completion prompt and the underlying
        LLM hallucinates a fake multi-turn conversation. The system
        prompt also explicitly tells the model to reply only to the
        final [YOU] line."""
        lines = []
        for m in self._chat_messages:
            role = m.get('role', 'user')
            content = (m.get('content') or '').strip()
            if not content:
                continue
            label = '[YOU]' if role == 'user' else '[ASSISTANT]'
            lines.append(f'{label}\n{content}')
        return '\n\n'.join(lines)

    def _flip_send_to_stop(self) -> None:
        if self._chat_send_btn is None:
            return
        try:
            self._chat_send_btn.configure(
                text='Stop', fg_color='#7a1f1f',
                hover_color='#a32626', command=self._on_chat_stop)
        except Exception:
            pass

    def _flip_stop_to_send(self) -> None:
        if self._chat_send_btn is None:
            return
        try:
            self._chat_send_btn.configure(
                text='Send', fg_color=_ACCENT,
                hover_color='#6d28d9', command=self._on_chat_send)
        except Exception:
            pass

    def _save_current_chat(self) -> None:
        """Persist the chat note's messages + title back to disk.
        Always async (load_notes/save_notes both touch disk; we don't
        want a UI hitch on every keystroke / Enter press)."""
        nid = self._editing_nid
        if not nid:
            return
        messages = list(self._chat_messages)
        title = (self._chat_title or
                 _chat_title_from_messages(messages, fallback=''))
        pinned = bool(self._pinned)

        def _persist():
            try:
                notes = load_notes()
                found = False
                for n in notes:
                    if n.get('id') == nid:
                        n['kind']     = 'chat'
                        n['messages'] = messages
                        n['title']    = title
                        n['pinned']   = pinned
                        # Don't touch text/items/voice fields here:
                        # a chat note shouldn't have them set anyway,
                        # and silently overwriting could confuse a
                        # future text-note migration.
                        found = True
                        break
                if not found:
                    return
                save_notes(notes)
            except Exception:
                logger.exception('Failed to save chat note')

        self._invalidate_notes_cache()
        threading.Thread(target=_persist, daemon=True,
                          name='chat-save').start()

    def cancel_chat_streams(self) -> None:
        """Public method called from main.py's Stop-everything handler.
        Bumps the gen counter so any in-flight provider.refine() result
        is dropped on arrival, and resets the UI button. Per the tray-
        coverage rule, every state-holding feature must be reset by
        the panic button."""
        self._chat_inflight_gen += 1
        self._chat_pending = False
        self._flip_stop_to_send()
        try: self._render_chat_transcript()
        except Exception: pass

    def set_chat_provider(self, provider) -> None:
        """main.py wires the LLM provider so the chat panel can call it.
        Lives separately from the text-note path so we don't have to
        touch the existing editor."""
        self._chat_provider = provider

    def open_chat_note_with_messages(self, title: str,
                                      messages: list) -> str:
        """Create a brand-new chat-kind note pre-populated with
        `messages` (typically the (question, answer) pair from
        AskPill's Follow-up button), persist it, and select it in the
        editor. Returns the new note id. Idempotent: a second
        identical call just creates another chat."""
        nid = str(uuid.uuid4())
        note = {
            'id':         nid,
            'kind':       'chat',
            'title':      (title or '').strip()[:80],
            'messages':   [dict(m) for m in messages],
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'pinned':     False,
        }
        notes = load_notes()
        notes.append(note)
        self._invalidate_notes_cache()
        # Sync save so the subsequent _open_note() reads it back.
        save_notes(notes)
        self._open_note(nid)
        return nid

    def _new_chat(self) -> None:
        """User clicked the chat-bubble button in the left header.
        Saves the current draft (if any), creates an empty chat note,
        opens it ready for the first question."""
        self._prev_note_nid = self._editing_nid
        # Stash any unsaved text-note draft (no-op for chat current)
        if self._editing_nid is None and not self._is_chat_mode():
            data = self._get_note_data()
            if data:
                self._save_current_as_new()
        self.open_chat_note_with_messages('', [])

    def _apply_title_tag(self) -> None:
        """Re-apply the 'title_line' font tag to line 1 (skipped when placeholder active)."""
        try:
            w = self._tb._textbox
            w.tag_remove('title_line', '1.0', 'end')
            if not self._ph:
                w.tag_add('title_line', '1.0', '1.end')
        except Exception:
            pass

    def _ph_key(self, event=None) -> None:
        """Clear placeholder on the first printable keystroke (not on focus)."""
        if self._ph and event is not None:
            ch = getattr(event, 'char', '')
            if ch and (ch.isprintable() or ch in ('\r', '\n')):
                self._tb.delete('1.0', 'end')
                self._tb.configure(text_color=_T1)
                self._ph = False

    def _ph_out(self, _=None) -> None:
        if not self._ph and not self._tb_text():
            self._tb.delete('1.0', 'end')
            self._tb.insert('1.0', self._ph_text)
            self._tb.configure(text_color=self._ph_color)
            self._ph = True

    def _tb_text(self) -> str:
        try:
            return '' if self._ph else self._tb.get('1.0', 'end-1c').strip()
        except Exception:
            return ''

    def _voice_tb_text(self) -> str:
        # Voice is now inline in _tb; voice field unused in new notes
        return ''

    # ── Color chips ───────────────────────────────────────────────────────────

    def _redraw_chips(self) -> None:
        for c, (chip_clr, _, _) in zip(self._chip_canvases, NOTE_COLORS):
            c.delete('all')
            selected = (self._color == chip_clr)
            if chip_clr is None:
                c.create_oval(2, 2, 10, 10, outline='#555', fill='', width=1)
            else:
                c.create_oval(2, 2, 10, 10, fill=chip_clr, outline='', width=0)
            if selected:
                c.create_oval(1, 1, 11, 11, outline=_T1, fill='', width=1.5)

    def _set_color(self, chip_clr) -> None:
        self._color = chip_clr
        self._redraw_chips()

    # ── Pin ───────────────────────────────────────────────────────────────────

    def _toggle_pin(self) -> None:
        self._pinned = not self._pinned
        self._pin_btn.configure(fg='#d4aa00' if self._pinned else _T3)

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle_rec(self) -> None:
        {'idle': self._start_rec, 'recording': self._stop_rec}.get(
            self._rec_state, lambda: None)()

    def _start_rec(self) -> None:
        if self._mic_busy_fn and self._mic_busy_fn():
            self._set_status('Mic busy, stop other recording first', _WARN_C)
            self.after(2500, lambda: self._set_status(''))
            return
        try:
            import sounddevice as sd
        except ImportError:
            self._set_status('sounddevice not available', _ERR)
            return
        self._rec_frames = []
        self._rec_start  = time.time()
        try:
            self._rec_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype='float32',
                callback=lambda d, f, t, s: self._rec_frames.append(d[:, 0].copy()),
            )
            self._rec_stream.start()
        except Exception as e:
            self._set_status(f'Mic error: {e}', _ERR)
            return
        self._rec_state = 'recording'
        self._update_mic_ui('recording')
        self._tick_rec()

    def _tick_rec(self) -> None:
        if self._rec_state != 'recording':
            return
        s = int(time.time() - self._rec_start)
        if s >= _MAX_REC_S:
            self._set_status(f'Max {_MAX_REC_S}s reached, stopping', _WARN_C)
            try: self._voice_hint.configure(text=f'Max {_MAX_REC_S}s reached')
            except Exception: pass
            self.after(400, self._stop_rec)
            return
        remaining = _MAX_REC_S - s
        self._set_status(f'● {s}s  ({remaining}s left)', _ERR)
        try: self._voice_hint.configure(text=f'● Recording  {s}s')
        except Exception: pass
        self._pulse_job = self.after(500, self._tick_rec)

    def _stop_rec(self) -> None:
        if self._pulse_job:
            try: self.after_cancel(self._pulse_job)
            except Exception: pass
            self._pulse_job = None
        if self._rec_stream:
            try:
                self._rec_stream.stop()
                self._rec_stream.close()
            except Exception: pass
            self._rec_stream = None
        audio = (np.concatenate(self._rec_frames)
                 if self._rec_frames else np.zeros(0, dtype='float32'))
        self._rec_state  = 'idle'
        self._pending_audio = audio
        self._update_mic_ui('idle')
        self._show_voice_decide()

    def _update_mic_ui(self, state: str) -> None:
        cfgs = {
            'idle':         dict(text='🎙  Voice', fg_color='transparent', text_color=_T3),
            'recording':    dict(text='⏹  Stop',   fg_color=_ERR,          text_color='#ffffff'),
            'transcribing': dict(text='⏳  …',      fg_color='transparent', text_color=_BLUE),
        }
        try:
            self._toolbar_mic.configure(**cfgs.get(state, cfgs['idle']))
        except Exception:
            pass

    def _do_transcribe(self, audio: np.ndarray) -> None:
        try:
            text = self._transcribe_fn(audio) if self._transcribe_fn else ''
        except Exception as e:
            logger.warning(f'Notes transcription: {e}')
            text = ''
        self.after(0, lambda t=text: self._on_transcribed(t))

    def _on_transcribed(self, text: str) -> None:
        self._rec_state = 'idle'
        self._update_mic_ui('idle')
        if not text.strip():
            self._set_status('Nothing heard', _WARN_C)
            self.after(2500, lambda: self._set_status(''))
            return
        # Insert transcript inline at cursor position. Routes to whichever
        # body widget is live: regular editor or chat transcript.
        try:
            in_text_mode = self._tb is not None
            if in_text_mode and self._ph:
                self._tb.delete('1.0', 'end')
                self._tb.configure(text_color=_T1)
                self._ph = False
            tb = self._active_body_textbox()
            if tb is not None:
                idx      = tb.index('insert')
                line_num = idx.split('.')[0]
                line_txt = tb.get(f'{line_num}.0', f'{line_num}.end').strip()
                prefix   = '' if not line_txt else '\n'
                tb.insert('insert', prefix + text)
                tb.see('insert')
                if (not in_text_mode and self._chat_transcript is not None):
                    self._chat_text_override = tb.get('1.0', 'end-1c')
                    self._chat_text_flush()
        except Exception:
            logger.exception('quicknotes: _on_transcribed insert failed')
        self._update_wcount()
        self._set_status('✓ Transcribed', _OK_C)
        self.after(2500, lambda: self._set_status(''))

    # ── Word count / status ───────────────────────────────────────────────────

    def _on_key(self, _=None) -> None:
        self._update_wcount()
        self._apply_title_tag()

    def _update_wcount(self) -> None:
        try:
            if self._tb is not None:
                txt = self._tb_text()
            elif self._chat_transcript is not None:
                txt = self._chat_transcript._textbox.get('1.0', 'end-1c')
            else:
                txt = ''
            self._wcount.configure(text=_word_count(txt))
        except Exception:
            pass

    def _set_status(self, text: str, color: str = _T3) -> None:
        try:
            self._status.configure(text=text, fg=color)
        except Exception:
            pass

    # ── Focus ─────────────────────────────────────────────────────────────────

    def _focus_content(self) -> None:
        try:
            self._tb._textbox.focus_set()
        except Exception:
            pass

    # ── Flush ─────────────────────────────────────────────────────────────────

    def _flush_current(self) -> None:
        # Full content in one textbox, first line is the title when stored
        raw = self._tb_text()
        text_lines:  list[str]  = []
        item_lines:  list[dict] = []
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith('□ ') or s.startswith('☐ '):
                item_lines.append({'text': s[2:], 'checked': False})
            elif s.startswith('✓ ') or s.startswith('☑ '):
                item_lines.append({'text': s[2:], 'checked': True})
            else:
                text_lines.append(line)

        self._text_content    = '\n'.join(text_lines).strip()
        self._checklist_items = item_lines or [{'text': '', 'checked': False}]
        # Voice transcript is embedded inline; keep field empty for new notes
        self._voice_transcript = ''

    # ── Save / close ──────────────────────────────────────────────────────────

    def _get_note_data(self) -> dict | None:
        """Return unified note data with all three content fields.
        Returns None only if the note is completely empty."""
        self._flush_current()
        text  = self._text_content
        items = [it for it in self._checklist_items if it.get('text', '').strip()]
        voice = self._voice_transcript
        if not text and not items and not voice:
            return None
        return {'text': text, 'items': items, 'voice': voice}

    def _save_and_close(self, _=None) -> None:
        if self._rec_state == 'recording':
            self._stop_rec()
            self.after(320, self._save_and_close)
            return
        # Chat-kind: persisted on every turn already; just close window.
        if self._is_chat_mode():
            self._save_current_chat()
            self.withdraw()
            return
        data = self._get_note_data()
        if data:
            if self._editing_nid:
                # Update existing note
                notes = load_notes()
                for n in notes:
                    if n.get('id') == self._editing_nid:
                        n['text']   = data['text']
                        n['items']  = data['items']
                        n['voice']  = data['voice']
                        n['pinned'] = self._pinned
                        break
                self._invalidate_notes_cache()
                save_notes(notes)
                logger.info('Quick note updated')
            else:
                border_hex = None
                for chip_clr, bdr_clr, _ in NOTE_COLORS:
                    if self._color == chip_clr:
                        border_hex = bdr_clr
                        break
                note = {
                    'id':         str(uuid.uuid4()),
                    'text':       data['text'],
                    'items':      data['items'],
                    'voice':      data['voice'],
                    'color':      border_hex,
                    'pinned':     self._pinned,
                    'created_at': datetime.now().isoformat(timespec='seconds'),
                }
                notes = load_notes()
                notes.append(note)
                self._invalidate_notes_cache()
                save_notes(notes)
                logger.info('Quick note saved')

        # Persist geometry so next open restores size/position.
        # Matching guard to the load-side 80% check: refuse to PERSIST a
        # geometry smaller than 80% of the default. Without this, an
        # accidental corner-drag below the default size would write a
        # tiny geometry to config; the load guard catches it on next
        # open and falls back to default, but the same tiny value is
        # rewritten on close, so the cycle never breaks until config
        # is hand-edited. Now: only "real" sizes get saved.
        if self._on_geometry_change:
            try:
                self.update_idletasks()
                geo = self.geometry()   # e.g. "1216x796+120+80"
                import re as _re
                _m = _re.match(r'(\d+)x(\d+)', geo)
                if _m:
                    _sw, _sh = int(_m.group(1)), int(_m.group(2))
                    if _sw < int(_W * 0.8) or _sh < int(_H * 0.8):
                        logger.info(
                            f'Quick Notes: not persisting tiny geometry '
                            f'({_sw}x{_sh}); keeping previous saved size.'
                        )
                    else:
                        self._on_geometry_change(geo)
                else:
                    self._on_geometry_change(geo)
            except Exception:
                pass
        if self._on_close:
            try: self._on_close()
            except Exception: pass
        try:
            self.destroy()
        except Exception: pass

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, e) -> None:
        self._drag_ox = e.x_root - self.winfo_x()
        self._drag_oy = e.y_root - self.winfo_y()

    def _drag_move(self, e) -> None:
        self.geometry(f'+{e.x_root - self._drag_ox}+{e.y_root - self._drag_oy}')

    # ── Keys ──────────────────────────────────────────────────────────────────

    def _bind_keys(self) -> None:
        self.bind('<Escape>',    self._on_escape)
        self.bind('<Control-s>', self._save_and_close)
        self.bind('<Control-S>', self._save_and_close)
        self.bind('<Control-m>', lambda _: self._toggle_rec())
        self.bind('<Control-M>', lambda _: self._toggle_rec())

        # Window-level Ctrl+V → paste-as-note. Originally bound only to
        # the list canvas + paste-zone label, but that meant the user
        # had to click those widgets first to give them keyboard focus.
        # Clicking the search bar or any other widget broke Ctrl+V.
        # Now firing regardless of focus, with a guard that lets the
        # in-editor Ctrl+V keep working (text/image paste into the note
        # textbox itself is handled by _smart_paste / event_generate).
        def _window_ctrl_v(event):
            w = event.widget
            try:
                cls = w.winfo_class() if w else ''
            except Exception:
                cls = ''
            # If focus is inside a text-input widget (the note editor,
            # search bar, etc.), let the native paste handler run so the
            # user can paste text/images into the field as usual.
            if cls in ('Text', 'Entry', 'TEntry', 'CTkEntry', 'CTkTextbox'):
                return None
            # Otherwise treat the keypress as "paste as note" and route
            # through the same handler the paste-zone label uses.
            return self._paste_to_list()
        self.bind('<Control-v>', _window_ctrl_v, add='+')
        self.bind('<Control-V>', _window_ctrl_v, add='+')
        # Gesture undo at window level
        self.bind('<Control-z>', self._gesture_ctrl_z)
        self.bind('<Control-Z>', self._gesture_ctrl_z)
        # Window-level Ctrl+V → paste as note when focus is in left panel.
        # Unbind before add='+' to prevent handler accumulation on theme switch.
        self.unbind('<Control-v>')
        self.unbind('<Control-V>')
        self.bind('<Control-v>', self._on_window_ctrl_v, add='+')
        self.bind('<Control-V>', self._on_window_ctrl_v, add='+')
        # Drag-and-drop release detection.
        # Unbind before add='+' to prevent handler accumulation on theme switch.
        self.unbind('<ButtonRelease-1>')
        self.bind('<ButtonRelease-1>', self._on_window_mouse_release, add='+')

    def _on_window_ctrl_v(self, event) -> str | None:
        """Window-level Ctrl+V: paste as note unless focus is in the editor or search bar."""
        try:
            fw = self.focus_get()
            # Let the text editor handle its own Ctrl+V
            if hasattr(self, '_tb') and self._tb is not None:
                try:
                    if fw is self._tb._textbox:
                        return None
                except Exception:
                    pass
            # Let the chat transcript handle its own Ctrl+V
            if hasattr(self, '_chat_transcript') and self._chat_transcript is not None:
                try:
                    if fw is self._chat_transcript._textbox:
                        return None
                except Exception:
                    pass
            # Let the chat input (follow-up bar) handle its own Ctrl+V
            if hasattr(self, '_chat_input') and self._chat_input is not None:
                try:
                    if fw is self._chat_input._entry:
                        return None
                except Exception:
                    pass
            # Let the search bar handle its own Ctrl+V
            if hasattr(self, '_srch_entry'):
                try:
                    if fw is self._srch_entry._entry:
                        return None
                except Exception:
                    pass
        except Exception:
            pass
        return self._paste_to_list(event)

    def _on_escape(self, _=None) -> None:
        """Esc: cancel staged OCR if pending, otherwise save & close."""
        if self._ocr_staged_img is not None:
            self._ocr_staged_img = None
            self._ocr_hide_preview()
            self._ocr_hide_status()
            return
        self._save_and_close()

    # ── Maximize / restore ────────────────────────────────────────────────────

    def _toggle_maximize(self) -> None:
        logger.info(f'[NOTES] _toggle_maximize called (maximized was {self._maximized})')

        # Why we don't use self.geometry() — Tk's geometry() ends up
        # calling MoveWindow on the CHILD HWND returned by winfo_id().
        # For an overrideredirect borderless window, the visible window
        # is the ROOT ancestor; resizing the child has no visible
        # effect. The native-resize handles (_enable_native_resize)
        # already work around this by walking GA_ROOT and calling
        # SetWindowPos directly. Mirror that approach here so the
        # maximize / restore actions actually move the visible window.
        try:
            import ctypes, ctypes.wintypes as _wt
            u32 = ctypes.windll.user32
            u32.GetAncestor.restype  = ctypes.c_void_p
            u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            u32.GetWindowRect.restype  = ctypes.c_bool
            u32.GetWindowRect.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(_wt.RECT)]
            u32.SetWindowPos.restype  = ctypes.c_bool
            u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int,
                                         ctypes.c_uint]
            child_hwnd = self.winfo_id()
            hwnd = u32.GetAncestor(child_hwnd, 2) or child_hwnd  # GA_ROOT
            SWP_NOZORDER   = 0x0004
            SWP_NOACTIVATE = 0x0010
        except Exception as e:
            logger.warning(f'[NOTES] could not resolve root HWND: {e}; '
                           'falling back to Tk geometry()')
            hwnd = None

        # Always read both CURRENT rect and TARGET work-area rect up
        # front. The toggle decision depends on comparing them — if
        # the window is already at (or very close to) the work-area
        # size, "maximize" would be a no-op, so we flip to "restore"
        # behaviour instead. This is what makes the button feel like
        # a real maximize toggle even when the window opens at a
        # previously-saved fullscreen geometry.
        try:
            if hwnd:
                cur = _wt.RECT()
                u32.GetWindowRect(hwnd, ctypes.byref(cur))
                cur_w, cur_h = cur.right - cur.left, cur.bottom - cur.top
                cur_x, cur_y = cur.left, cur.top
            else:
                cur_w, cur_h = self.winfo_width(), self.winfo_height()
                cur_x, cur_y = self.winfo_x(), self.winfo_y()
        except Exception:
            cur_w, cur_h = self.winfo_width(), self.winfo_height()
            cur_x, cur_y = self.winfo_x(), self.winfo_y()

        try:
            r = _wt.RECT()
            ctypes.windll.user32.SystemParametersInfoW(
                48, 0, ctypes.byref(r), 0)
            wa_w, wa_h, wa_x, wa_y = (r.right - r.left, r.bottom - r.top,
                                       r.left, r.top)
        except Exception:
            wa_w = self.winfo_screenwidth()
            wa_h = self.winfo_screenheight()
            wa_x, wa_y = 0, 0

        # Detect "the window is already at work-area size" within a
        # ±16 px tolerance (drop shadow / invisible border quirks).
        # If so, treat the user's click as a restore request even when
        # self._maximized was False (e.g., first session click on a
        # window whose saved geometry IS the work area).
        near_max = (abs(cur_w - wa_w) <= 16 and abs(cur_h - wa_h) <= 16
                    and abs(cur_x - wa_x) <= 16 and abs(cur_y - wa_y) <= 16)
        treat_as_maximized = self._maximized or near_max

        if treat_as_maximized:
            # ── Restore path ──────────────────────────────────────────
            # Pick a target geometry. Priority:
            #   1. _restore_geo if it exists AND is meaningfully smaller
            #      than the work area (else we'd restore to fullscreen
            #      and the user would see no change again).
            #   2. A sensible default — 70% of work area, centred — so
            #      the click visibly does something the very first time
            #      the user toggles, even if no prior smaller geometry
            #      was ever recorded.
            target = None
            if self._restore_geo:
                try:
                    wh, _, rest = self._restore_geo.partition('+')
                    rx, _, ry = rest.partition('+')
                    rw_s, _, rh_s = wh.partition('x')
                    rw, rh = int(rw_s), int(rh_s)
                    if rw < wa_w - 32 and rh < wa_h - 32:
                        target = (rw, rh, int(rx), int(ry))
                except Exception:
                    pass
            if target is None:
                # Default-smaller target: 70% of work area, centred.
                dw = max(640, int(wa_w * 0.7))
                dh = max(480, int(wa_h * 0.7))
                dx = wa_x + (wa_w - dw) // 2
                dy = wa_y + (wa_h - dh) // 2
                target = (dw, dh, dx, dy)
                logger.info(f'[NOTES] no usable _restore_geo '
                            f'({self._restore_geo!r}); using 70%-default '
                            f'{dw}x{dh}+{dx}+{dy}')
            w, h, x, y = target
            if hwnd:
                u32.SetWindowPos(hwnd, 0, x, y, w, h,
                                 SWP_NOZORDER | SWP_NOACTIVATE)
            else:
                self.geometry(f'{w}x{h}+{x}+{y}')
            self._maximized = False
            try: self._max_btn.configure(text='□')
            except Exception: pass
            logger.info(f'[NOTES] restored to {w}x{h}+{x}+{y} '
                        f'(was {cur_w}x{cur_h}+{cur_x}+{cur_y}, '
                        f'near_max={near_max})')
        else:
            # ── Maximize path ─────────────────────────────────────────
            # Save the current rect as the restore target before resizing.
            self._restore_geo = f'{cur_w}x{cur_h}+{cur_x}+{cur_y}'
            if hwnd:
                u32.SetWindowPos(hwnd, 0, wa_x, wa_y, wa_w, wa_h,
                                 SWP_NOZORDER | SWP_NOACTIVATE)
            else:
                self.geometry(f'{wa_w}x{wa_h}+{wa_x}+{wa_y}')
            self._maximized = True
            try: self._max_btn.configure(text='⊡')
            except Exception: pass
            logger.info(f'[NOTES] maximized to {wa_w}x{wa_h}+{wa_x}+{wa_y} '
                        f'(restore={self._restore_geo})')

    # ── Alt+Tab / taskbar visibility for an overrideredirect window ───────────

    def _enable_alt_tab(self) -> None:
        """Force the borderless window into the Alt+Tab switcher + taskbar.

        Tk's overrideredirect(True) defaults to WS_EX_TOOLWINDOW which
        hides the window from Alt+Tab/taskbar; we swap for WS_EX_APPWINDOW
        so the user can click another app to send Notes to the background
        and Alt+Tab back, same as the Shift+F8 Whiteboard.

        The taskbar icon itself is dictated by pythonw.exe in dev/source
        mode (Windows falls back to the executable icon when no per-
        window icon resource is registered). The dist build embeds the
        brand .ico as the .exe's own icon resource, which is the only
        reliable way to override this in source mode without unstable
        Win32 / Tk interactions. We attempted the WM_SETICON path and
        rejected it because it interfered with Tk's window-state machine.
        """
        if sys.platform != 'win32':
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            # restype = c_void_p so HWND / LONG_PTR results aren't truncated
            # to 32-bit on 64-bit Windows. Otherwise a HWND with bit 31 set
            # would compare unequal to its true value and GetWindowLongPtrW
            # would return a corrupted style.
            u.GetParent.restype          = ctypes.c_void_p
            u.GetParent.argtypes         = (ctypes.c_void_p,)
            u.GetWindowLongPtrW.restype  = ctypes.c_ssize_t
            u.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
            u.SetWindowLongPtrW.restype  = ctypes.c_ssize_t
            u.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
            u.SetWindowPos.argtypes      = (ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_uint)
            hwnd = ctypes.c_void_p(self.winfo_id())
            for _ in range(8):
                parent = u.GetParent(hwnd)
                if not parent:
                    break
                hwnd = ctypes.c_void_p(parent)
            GWL_EXSTYLE      = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW  = 0x00040000
            style = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            new   = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            if new == style:
                return
            u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new)
            # Force the Explorer-side taskbar registration to pick up the
            # new ex-style WITHOUT touching window visibility. SWP_FRAME-
            # CHANGED is the safe path; an SW_HIDE / SW_SHOWNOACTIVATE
            # toggle hides the window for reasons specific to CTk's
            # internal state machine.
            SWP_NOMOVE       = 0x0002
            SWP_NOSIZE       = 0x0001
            SWP_NOZORDER     = 0x0004
            SWP_NOACTIVATE   = 0x0010
            SWP_FRAMECHANGED = 0x0020
            u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                           SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                           SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception as e:
            logger.warning(f'_enable_alt_tab failed: {e}')

    # ── All-sides resize ──────────────────────────────────────────────────────

    def _enable_native_resize(self) -> None:
        """Smooth borderless resize, no WS_THICKFRAME, no distortion.

        Eight transparent tk.Frame handles are placed on _outer (above all
        content) for each resize edge/corner.  They carry Tkinter cursors and
        bind press/drag/release.  On motion, SetWindowPos is called directly;
        WM_SIZE flows through so Tkinter re-layouts live, no DWM stretching.
        CORN=12 keeps corner handles clear of the 'Notes' label text (≈x15,y13).
        """
        try:
            import ctypes, ctypes.wintypes as wt

            SWP_NOZORDER   = 0x0004
            SWP_NOACTIVATE = 0x0010
            _MIN_W, _MIN_H = 600, 400

            user32 = ctypes.windll.user32
            user32.GetWindowRect.restype  = ctypes.c_bool
            user32.GetWindowRect.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(wt.RECT)]
            user32.GetAncestor.restype    = ctypes.c_void_p
            user32.GetAncestor.argtypes   = [ctypes.c_void_p, ctypes.c_uint]
            user32.SetWindowPos.restype   = ctypes.c_bool
            user32.SetWindowPos.argtypes  = [ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int,
                                             ctypes.c_uint]

            child_hwnd = self.winfo_id()
            hwnd = user32.GetAncestor(child_hwnd, 2)  # GA_ROOT
            if not hwnd:
                hwnd = child_hwnd

            # No WndProc subclassing needed: each SetWindowPos from a
            # <B1-Motion> callback generates exactly one WM_SIZE, no flood,
            # no suppression required.  Letting WM_SIZE through means Tkinter
            # re-layouts live during drag, preventing DWM bitmap stretching.

            # ── Drag state ────────────────────────────────────────────────────
            _d = {}   # sx, sy, origL, origT, origR, origB, ht

            # HT codes matching the 8 edge/corner names
            _HT = {'nw': 13, 'n': 12, 'ne': 14,
                   'w':  10,            'e':  11,
                   'sw': 16, 's': 15,  'se': 17}

            # Tkinter cursor names for each edge (Windows-valid names only)
            _CUR = {
                'nw': 'size_nw_se',        'se': 'size_nw_se',
                'ne': 'size_ne_sw',        'sw': 'size_ne_sw',
                'n':  'sb_v_double_arrow', 's':  'sb_v_double_arrow',
                'w':  'sb_h_double_arrow', 'e':  'sb_h_double_arrow',
            }

            def _press(event, ht):
                r = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                _d.update(sx=event.x_root, sy=event.y_root, ht=ht,
                          origL=r.left, origT=r.top,
                          origR=r.right, origB=r.bottom)

            def _motion(event):
                if not _d:
                    return
                dx = event.x_root - _d['sx']
                dy = event.y_root - _d['sy']
                L, T, R, B = _d['origL'], _d['origT'], _d['origR'], _d['origB']
                ht = _d['ht']
                if ht in (10, 13, 16): L += dx
                if ht in (11, 14, 17): R += dx
                if ht in (12, 13, 14): T += dy
                if ht in (15, 16, 17): B += dy
                if R - L < _MIN_W:
                    if ht in (10, 13, 16): L = R - _MIN_W
                    else:                  R = L + _MIN_W
                if B - T < _MIN_H:
                    if ht in (12, 13, 14): T = B - _MIN_H
                    else:                  B = T + _MIN_H
                user32.SetWindowPos(hwnd, 0, L, T, R - L, B - T,
                                    SWP_NOZORDER | SWP_NOACTIVATE)

            def _release(event):
                _d.clear()

            # ── Transparent edge/corner handle frames ─────────────────────────
            # CORN=12: NW corner reaches x=1-12, y=1-12.  The "Notes" label
            # text starts at ≈x=15, y=13 within _outer, just outside the
            # corner zone, so no handle frame ever covers visible text.
            EDGE = 8    # resize grab width in px
            CORN = 12   # corner square size in px
            HL   = 1    # _outer highlightthickness, offset handles inward

            # Compute placements as functions of outer-frame size
            def _placements(w, h):
                return {
                    'nw': (HL,        HL,        CORN,           CORN),
                    'ne': (w-CORN-HL, HL,        CORN,           CORN),
                    'sw': (HL,        h-CORN-HL, CORN,           CORN),
                    'se': (w-CORN-HL, h-CORN-HL, CORN,           CORN),
                    'n':  (CORN,      HL,        w-2*CORN,       EDGE),
                    's':  (CORN,      h-EDGE-HL, w-2*CORN,       EDGE),
                    'w':  (HL,        CORN,      EDGE,           h-2*CORN),
                    'e':  (w-EDGE-HL, CORN,      EDGE,           h-2*CORN),
                }

            # 4 accent border strips, placed via place() so they're always
            # fully visible regardless of CTkToplevel's internal frame layout.
            # Lifted before handles so handles sit on top (no overlap anyway
            # since handles start at HL=1 inset and strips are at edge 0).
            _border_strips = [tk.Frame(self._outer, bg=_ACCENT) for _ in range(4)]

            _handle_frames = []
            # Edges first, then corners, corners lifted last so they win at
            # overlap zones and always show the diagonal cursor
            for edge_name in ('n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se'):
                ht = _HT[edge_name]
                frm = tk.Frame(self._outer, bg=_WIN,
                               cursor=_CUR[edge_name])
                frm.bind('<ButtonPress-1>',   lambda e, h=ht: _press(e, h))
                frm.bind('<B1-Motion>',       _motion)
                frm.bind('<ButtonRelease-1>', _release)
                _handle_frames.append((frm, edge_name))

            def _place_handles():
                try:
                    w = self._outer.winfo_width()
                    h = self._outer.winfo_height()
                    if w < 20 or h < 20:
                        self.after(80, _place_handles)
                        return
                    # Border strips: top, bottom, left, right (1px each)
                    _border_strips[0].place(x=0,   y=0,   width=w, height=1)
                    _border_strips[1].place(x=0,   y=h-1, width=w, height=1)
                    _border_strips[2].place(x=0,   y=0,   width=1, height=h)
                    _border_strips[3].place(x=w-1, y=0,   width=1, height=h)
                    for bf in _border_strips:
                        bf.lift()
                    # Resize handles on top of border strips
                    pl = _placements(w, h)
                    for frm, edge_name in _handle_frames:
                        x, y, fw, fh = pl[edge_name]
                        frm.place(x=x, y=y, width=fw, height=fh)
                        frm.lift()
                except Exception:
                    pass

            # Reposition handles whenever the outer frame changes size
            self._outer.bind('<Configure>', lambda e: _place_handles(), add='+')
            self.after(120, _place_handles)
            self._resize_handles = _handle_frames   # prevent GC
            self._border_strips  = _border_strips   # prevent GC

            logger.info(f'Resize hook installed (handle-frames): hwnd={hwnd:#010x}')
        except Exception as e:
            logger.warning(f'Resize hook failed: {e}')

    # ── Gesture undo ──────────────────────────────────────────────────────────

    def _push_undo_state(self) -> None:
        """Save full editor state to the gesture undo stack before a swipe."""
        self._flush_current()
        self._gesture_undo_stack.append({
            'editing_nid':     self._editing_nid,
            'prev_note_nid':   self._prev_note_nid,
            'text_content':    self._text_content,
            'checklist_items': [dict(it) for it in self._checklist_items],
            'voice_transcript': self._voice_transcript,
            'pinned':          self._pinned,
        })
        if len(self._gesture_undo_stack) > 20:
            self._gesture_undo_stack.pop(0)

    def _restore_gesture_state(self, state: dict) -> None:
        """Restore editor state from a gesture undo snapshot."""
        self._editing_nid      = state['editing_nid']
        self._prev_note_nid    = state['prev_note_nid']
        self._text_content     = state['text_content']
        self._checklist_items  = state['checklist_items']
        self._voice_transcript = state['voice_transcript']
        self._pinned           = state['pinned']
        try: self._pin_btn.configure(fg='#d4aa00' if self._pinned else _T3)
        except Exception: pass
        self._show_panel()
        self._refresh_list()
        self._set_status('↩ Gesture undone', _ACCENTL)
        self.after(1500, lambda: self._set_status(''))
        self.after(30, self._focus_content)

    def _text_ctrl_z(self, event=None) -> str:
        """Ctrl+Z handler bound directly to the body textbox.
        Tries text undo; if stack is empty lets propagation continue to window
        level so gesture undo can fire."""
        try:
            self._tb._textbox.edit_undo()
            return 'break'   # undo succeeded, stop propagation
        except Exception:
            pass             # stack empty, fall through to window handler
        return None          # allow window-level binding to fire

    def _gesture_ctrl_z(self, event=None) -> str:
        """Window-level Ctrl+Z: pops the gesture undo stack if available.
        Fires when the textbox doesn't intercept (non-text modes, or text
        undo stack exhausted)."""
        if self._gesture_undo_stack:
            state = self._gesture_undo_stack.pop()
            self._restore_gesture_state(state)
        return 'break'

    # ── Inline task insertion ─────────────────────────────────────────────────

    def _insert_task(self) -> None:
        """Insert a □ checklist line at the current cursor position. Works
        in both regular-note (self._tb) and chat-note (self._chat_transcript)
        modes via _active_body_textbox()."""
        try:
            in_text_mode = self._tb is not None
            if in_text_mode and self._ph:
                self._tb.delete('1.0', 'end')
                self._tb.configure(text_color=_T1)
                self._ph = False
            tb = self._active_body_textbox()
            if tb is None:
                return
            idx      = tb.index('insert')
            line_num = idx.split('.')[0]
            line_end = f'{line_num}.end'
            line_txt = tb.get(f'{line_num}.0', line_end).strip()
            if line_txt:
                tb.mark_set('insert', line_end)
                tb.insert('insert', '\n□ ')
            else:
                tb.insert(f'{line_num}.0', '□ ')
            tb.see('insert')
            tb.focus_set()
            # Persist in chat mode so the inserted task survives navigation.
            if (not in_text_mode and self._chat_transcript is not None):
                self._chat_text_override = tb.get('1.0', 'end-1c')
                self._chat_text_flush()
        except Exception:
            logger.exception('quicknotes: _insert_task failed')

    def _on_body_click(self, event) -> str | None:
        """Toggle □ ↔ ✓ when clicking within the first 2 chars of a task line."""
        try:
            tb       = self._tb._textbox
            idx      = tb.index(f'@{event.x},{event.y}')
            line_num = int(idx.split('.')[0])
            col      = int(idx.split('.')[1])
            if col <= 2:
                ls  = f'{line_num}.0'
                txt = tb.get(ls, f'{line_num}.end')
                if txt.startswith('□ '):
                    tb.delete(ls, f'{ls}+2c')
                    tb.insert(ls, '✓ ')
                    self._apply_task_tag(line_num, done=True)
                    return 'break'
                elif txt.startswith('✓ '):
                    tb.delete(ls, f'{ls}+2c')
                    tb.insert(ls, '□ ')
                    self._apply_task_tag(line_num, done=False)
                    return 'break'
        except Exception:
            pass
        return None

    def _apply_task_tag(self, line_num: int, done: bool) -> None:
        """Dim a completed task line, un-dim an active one."""
        try:
            tb = self._tb._textbox
            ls = f'{line_num}.0'
            le = f'{line_num}.end'
            tb.tag_remove('task_done', ls, le)
            if done:
                tb.tag_add('task_done', ls, le)
        except Exception:
            pass

    def _apply_all_task_tags(self) -> None:
        """Apply visual styling to all □/✓ lines (called after content load)."""
        try:
            tb          = self._tb._textbox
            total_lines = int(tb.index('end-1c').split('.')[0])
            for i in range(1, total_lines + 1):
                txt = tb.get(f'{i}.0', f'{i}.end')
                if txt.startswith('✓ ') or txt.startswith('☑ '):
                    self._apply_task_tag(i, done=True)
        except Exception:
            pass

    # ── Voice decide bar (record → transcribe / discard) ──────────────────────

    def _show_voice_decide(self) -> None:
        """Floating confirm bar after recording: Transcribe | Discard."""
        self._hide_voice_decide()
        bar = tk.Frame(self._content_host, bg=_SURF2, height=38)
        bar.place(x=0, rely=1.0, relwidth=1.0, height=38, anchor='sw')
        bar.lift()
        bar.pack_propagate(False)
        # Top border line for visual separation
        tk.Frame(bar, bg=_DIV, height=1).pack(side='top', fill='x')
        self._voice_decide_bar = bar

        tk.Label(bar, text='Voice ready:', bg=_SURF2, fg=_T2,
                 font=(FONT_FAMILY, 10)).pack(side='left', padx=(10, 6))

        ctk.CTkButton(
            bar, text='📝 Transcribe & insert', width=150, height=26,
            fg_color=_ACCENT, hover_color='#6d28d9',
            text_color='#ffffff', font=(FONT_FAMILY, 10), corner_radius=4,
            command=self._decide_transcribe,
        ).pack(side='left', padx=(0, 6))

        ctk.CTkButton(
            bar, text='✕ Discard', width=70, height=26,
            fg_color='transparent', hover_color=_HOVER,
            text_color=_T2, font=(FONT_FAMILY, 10), corner_radius=4,
            command=self._decide_discard,
        ).pack(side='left')

    def _hide_voice_decide(self) -> None:
        bar = self._voice_decide_bar
        if bar is not None:
            try: bar.destroy()
            except Exception: pass
        self._voice_decide_bar = None

    def _decide_transcribe(self) -> None:
        audio = self._pending_audio
        self._pending_audio = None
        self._hide_voice_decide()
        if audio is None or len(audio) == 0:
            return
        self._rec_state = 'transcribing'
        self._update_mic_ui('transcribing')
        self._set_status('Transcribing…', _BLUE)
        threading.Thread(target=self._do_transcribe, args=(audio,), daemon=True).start()

    def _decide_discard(self) -> None:
        self._pending_audio = None
        self._hide_voice_decide()
        self._set_status('Recording discarded', _T3)
        self.after(1500, lambda: self._set_status(''))

    # ── Search debounce ───────────────────────────────────────────────────────

    def _on_search_key(self, e=None) -> None:
        """Debounce search: wait 150 ms after last keystroke before re-rendering."""
        if self._search_job is not None:
            try: self.after_cancel(self._search_job)
            except Exception: pass
        self._search_job = self.after(150, self._refresh_list)

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _toggle_theme(self) -> None:
        self._set_theme('light' if self._theme == 'dark' else 'dark')

    def _set_theme(self, theme: str) -> None:
        """Switch between dark and light palettes and rebuild the window."""
        self._flush_current()
        self._theme = theme
        if self._on_theme_change:
            try: self._on_theme_change(theme)
            except Exception: pass
        palette = _LIGHT_PALETTE if theme == 'light' else _DARK_PALETTE
        _mod = sys.modules[__name__]
        for k, v in palette.items():
            setattr(_mod, k, v)
        # Rebuild all widgets with new palette
        for w in list(self.winfo_children()):
            try: w.destroy()
            except Exception: pass
        self.configure(fg_color=_WIN)
        self._build()
        self._bind_keys()
        if self._editing_nid:
            self.after(30, self._show_panel)
        self.after(60, self._focus_content)
