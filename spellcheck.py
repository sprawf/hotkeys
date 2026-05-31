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

# Word pattern, handles contractions (don't, it's) and plain words
_WORD_RE = re.compile(r"[a-zA-Z]+(?:'[a-zA-Z]+)*")

# Session-wide ignore list, shared across all widgets intentionally so that
# "Ignore all" in the sticky note also suppresses the word in the library editor.
_session_ignore: set[str] = set()

# Lazy singleton, SpellChecker takes ~300 ms to load; background-loaded so
# the UI never blocks on import.
_checker      = None
_checker_lock = threading.Lock()

# Instances keyed by id(inner tk.Text widget), lets the host query spell info
# without a direct reference to the _SpellCheck object.
_instances: 'dict[int, _SpellCheck]' = {}


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
    """Kick off dictionary load in the background at import time.

    Wraps _get_checker in a guard that LOGS any failure (e.g. PyInstaller
    forgot to bundle spellchecker/resources/*.json.gz) instead of dying
    silently on the daemon thread. The frozen-exe crash that killed v3.1
    was caused by a silent failure in this exact code path; the log line
    is the next-build canary.
    """
    def _bg():
        try:
            _get_checker()
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                'Spell-check preload failed (%s: %s); '
                'live spell-check will be disabled but app continues.',
                type(exc).__name__, exc)
    threading.Thread(target=_bg, daemon=True).start()

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
        # Tk 8.6 (standard Windows) doesn't, fall back to plain underline.
        try:
            self._w.tag_config('misspelled', underline=True, underlinecolor='#dc2626')
        except tk.TclError:
            self._w.tag_config('misspelled', underline=True)

        self._w.bind('<KeyRelease>', self._schedule, add='+')
        # NOTE: <Button-3> is intentionally NOT bound here.
        # The host widget's right-click handler calls get_info() to retrieve
        # spell suggestions and injects them into its own unified popup menu.
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
            return  # pyspellchecker not installed, silently skip

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

    # ── Spell info (called by the host's right-click handler) ─────────────────

    def get_info(self, x: int, y: int) -> 'tuple | None':
        """Return (word, ws, we, suggestions) if (x,y) is over a misspelled word, else None."""
        idx    = self._w.index(f'@{x},{y}')
        ranges = self._w.tag_ranges('misspelled')

        ws = we = None
        for i in range(0, len(ranges), 2):
            s, e = str(ranges[i]), str(ranges[i + 1])
            if self._w.compare(s, '<=', idx) and self._w.compare(idx, '<=', e):
                ws, we = s, e
                break

        if ws is None:
            return None

        word = self._w.get(ws, we)
        try:
            suggestions = sorted(_get_checker().candidates(word) or [])[:6]
        except Exception:
            suggestions = []
        return (word, ws, we, suggestions)

    # ── Actions ───────────────────────────────────────────────────────────────

    def apply_suggestion(self, ws: str, we: str, replacement: str) -> None:
        self._w.delete(ws, we)
        self._w.insert(ws, replacement)
        self._check_all()

    def ignore_word(self, word: str) -> None:
        _session_ignore.add(word.lower())
        self._check_all()

    def add_word(self, word: str) -> None:
        try:
            _get_checker().word_frequency.load_words([word.lower()])
        except Exception:
            pass
        self._check_all()


# ── Public API ────────────────────────────────────────────────────────────────

def attach(widget) -> '_SpellCheck | None':
    """Attach live spell-check to *widget*.

    Accepts a plain tk.Text or a ctk.CTkTextbox (auto-unwraps .textbox).
    Does nothing if pyspellchecker is not installed.
    Returns the _SpellCheck instance (or None) so callers can store it.
    """
    try:
        _get_checker()
    except Exception:
        return None  # silently skip if not installed

    inner = getattr(widget, 'textbox', widget)  # unwrap CTkTextbox → tk.Text
    sc = _SpellCheck(inner)
    _instances[id(inner)] = sc
    return sc


def get_info(widget, x: int, y: int) -> 'tuple | None':
    """Return spell info at widget-local position (x, y).

    Returns (word, ws, we, suggestions) if the cursor is over a misspelled
    word, or None otherwise.  Pass this to the host's right-click popup to
    inject spell suggestions at the top of the unified menu.
    """
    inner = getattr(widget, 'textbox', widget)
    sc    = _instances.get(id(inner))
    if sc is None:
        return None
    return sc.get_info(x, y)


def ignore_word(widget, word: str) -> None:
    """Add *word* to the session ignore list and re-check *widget*."""
    inner = getattr(widget, 'textbox', widget)
    sc    = _instances.get(id(inner))
    if sc:
        sc.ignore_word(word)
    else:
        _session_ignore.add(word.lower())


def add_word(widget, word: str) -> None:
    """Add *word* to the spell-checker dictionary and re-check *widget*."""
    inner = getattr(widget, 'textbox', widget)
    sc    = _instances.get(id(inner))
    if sc:
        sc.add_word(word)


def apply_suggestion(widget, ws: str, we: str, replacement: str) -> None:
    """Replace the misspelled span [ws, we) with *replacement* in *widget*."""
    inner = getattr(widget, 'textbox', widget)
    sc    = _instances.get(id(inner))
    if sc:
        sc.apply_suggestion(ws, we, replacement)
