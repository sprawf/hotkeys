"""Live spell-check overlay for tk.Text and ctk.CTkTextbox widgets.

Usage
-----
    import spellcheck
    spellcheck.attach(some_text_widget)

Works with both plain tk.Text and ctk.CTkTextbox (auto-unwraps .textbox).
Requires:  pip install pyspellchecker
"""
import re
import threading
import tkinter as tk

from theme import SURF2, TEXT_P, ACCENT

# Word pattern — handles contractions (don't, it's) and plain words
_WORD_RE = re.compile(r"[a-zA-Z]+(?:'[a-zA-Z]+)*")

# Session-wide ignore list — shared across all widgets intentionally so that
# "Ignore all" in the sticky note also suppresses the word in the library editor.
_session_ignore: set[str] = set()

# Lazy singleton — SpellChecker takes ~300 ms to load; background-loaded so
# the UI never blocks on import.
_checker      = None
_checker_lock = threading.Lock()


def _get_checker():
    global _checker
    if _checker is not None:
        return _checker
    with _checker_lock:
        if _checker is None:
            from spellchecker import SpellChecker
            _checker = SpellChecker()
    return _checker


def _preload() -> None:
    """Kick off dictionary load in the background at import time."""
    threading.Thread(target=_get_checker, daemon=True).start()

try:
    _preload()
except Exception:
    pass


# ── Core helper ───────────────────────────────────────────────────────────────

class _SpellCheck:
    """Attaches live spell-check behaviour to a tk.Text widget."""

    def __init__(self, widget: tk.Text) -> None:
        self._w     = widget
        self._after = None

        # Tk 8.7+ supports underlinecolor (red underline, black text).
        # Tk 8.6 (standard Windows) doesn't — fall back to plain underline.
        try:
            self._w.tag_config('misspelled', underline=True, underlinecolor='#dc2626')
        except tk.TclError:
            self._w.tag_config('misspelled', underline=True)

        self._w.bind('<KeyRelease>', self._schedule, add='+')
        self._w.bind('<Button-3>',   self._on_rclick, add='+')
        self._w.after(600, self._check_all)  # check text loaded from saved prompt

    # ── Debounced check ───────────────────────────────────────────────────────

    def _schedule(self, _event=None) -> None:
        if self._after:
            self._w.after_cancel(self._after)
        self._after = self._w.after(500, self._check_all)

    def _check_all(self) -> None:
        try:
            checker = _get_checker()
        except Exception:
            return  # pyspellchecker not installed — silently skip

        content = self._w.get('1.0', 'end-1c')
        self._w.tag_remove('misspelled', '1.0', 'end')

        for m in _WORD_RE.finditer(content):
            word = m.group()
            low  = word.lower()
            if low in _session_ignore:
                continue
            if len(word) < 3:       # skip very short words
                continue
            if word.isupper():      # skip ABBREVIATIONS
                continue
            if checker.unknown([low]):
                self._w.tag_add('misspelled',
                                f'1.0 + {m.start()} chars',
                                f'1.0 + {m.end()} chars')

    # ── Right-click menu ──────────────────────────────────────────────────────

    def _on_rclick(self, event) -> None:
        idx    = self._w.index(f'@{event.x},{event.y}')
        ranges = self._w.tag_ranges('misspelled')

        ws = we = None
        for i in range(0, len(ranges), 2):
            s, e = str(ranges[i]), str(ranges[i + 1])
            if self._w.compare(s, '<=', idx) and self._w.compare(idx, '<=', e):
                ws, we = s, e
                break

        if ws is None:
            return  # not on a misspelled word — don't block normal menu

        word = self._w.get(ws, we)
        try:
            suggestions = sorted(_get_checker().candidates(word) or [])[:6]
        except Exception:
            suggestions = []

        menu = tk.Menu(self._w, tearoff=0,
                       bg=SURF2, fg=TEXT_P,
                       activebackground=ACCENT, activeforeground='#ffffff',
                       relief='flat', bd=0, font=('Segoe UI', 10))

        if suggestions:
            for s in suggestions:
                menu.add_command(
                    label=f'  {s}  ',
                    command=lambda r=s, a=ws, b=we: self._replace(a, b, r),
                )
            menu.add_separator()

        menu.add_command(label='  Ignore all  ',       command=lambda w=word: self._ignore(w))
        menu.add_command(label='  Add to dictionary  ', command=lambda w=word: self._add(w))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _replace(self, start: str, end: str, replacement: str) -> None:
        self._w.delete(start, end)
        self._w.insert(start, replacement)
        self._check_all()

    def _ignore(self, word: str) -> None:
        _session_ignore.add(word.lower())
        self._check_all()

    def _add(self, word: str) -> None:
        try:
            _get_checker().word_frequency.load_words([word.lower()])
        except Exception:
            pass
        self._check_all()


# ── Public API ────────────────────────────────────────────────────────────────

def attach(widget) -> None:
    """Attach live spell-check to *widget*.

    Accepts a plain tk.Text or a ctk.CTkTextbox (auto-unwraps .textbox).
    Does nothing if pyspellchecker is not installed.
    """
    try:
        _get_checker()
    except Exception:
        return  # silently skip if not installed

    inner = getattr(widget, 'textbox', widget)  # unwrap CTkTextbox → tk.Text
    _SpellCheck(inner)
