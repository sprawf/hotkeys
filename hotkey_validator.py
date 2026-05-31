"""Hotkey conflict validator, centralised checks for every place the user
can assign or rebind a keyboard shortcut.

Why centralised: hotkeys come in from four UI surfaces (Settings, per-prompt
assignment in Library, per-chain assignment, saved-macro assignment) plus
loaded-from-disk paths. They all need the same checks. If any one path
skips a check, users hit "key does nothing" or "two actions fire" bugs
that are hard to trace.

Checks performed:
    1. Syntax, does the `keyboard` library understand the string at all?
    2. Self-conflict, is the same hotkey already bound to a DIFFERENT
       action in the live config (other Settings entries, prompts, chains,
       saved macros)?
    3. OS-reserved, Windows pre-empts these at the kernel level and
       `keyboard` can never see them (Ctrl+Alt+Del, Win+L, Win+D, …).
    4. Whiteboard shortcut clash, informational. The app's hotkey
       is automatically silenced when the whiteboard owns focus (see
       _WHITEBOARD_GATED_EVENTS in main.py), so a clash is not broken,
       just worth surfacing so the user knows what to expect.
    5. Common app risk (Alt+F4, Ctrl+W, browser-zoom keys), warning,
       not error.

Returns severity-tagged result objects so callers can pick UI treatment:
    OK    → no diagnostic
    WARN  → show a non-blocking note next to the field
    ERROR → block save / refuse to apply
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable


# ── Severity levels ─────────────────────────────────────────────────────────

OK, WARN, ERROR = 'ok', 'warn', 'error'


@dataclass
class Diagnostic:
    severity: str   # OK | WARN | ERROR
    message: str
    action: str = ''   # the action being validated (refine, library, …)

    @property
    def is_blocking(self) -> bool:
        return self.severity == ERROR


# ── Hotkey string normalisation ─────────────────────────────────────────────

# `keyboard` accepts modifier aliases, fold them all to a single canonical
# form so "alt + shift + w", "ALT+SHIFT+W", "Alt-Shift-W" all compare equal.
_MOD_ALIASES = {
    'control': 'ctrl', 'ctl': 'ctrl', 'ctrl_l': 'ctrl', 'ctrl_r': 'ctrl',
    'lctrl':   'ctrl', 'rctrl': 'ctrl',
    'option':  'alt',  'menu':  'alt',  'alt_l': 'alt',   'alt_r':  'alt',
    'lalt':    'alt',  'ralt':  'alt',
    'cmd':     'win',  'meta':  'win',  'super': 'win',   'lwin':   'win',
    'rwin':    'win',  'windows': 'win',
    'shift_l': 'shift','shift_r':'shift','lshift':'shift','rshift': 'shift',
    'return':  'enter','esc':   'escape',
    'pgup':    'page up','pgdn':'page down',
    'plus':    '+', 'minus': '-', 'equals': '=',
}
_MOD_ORDER = ('ctrl', 'alt', 'shift', 'win')


def normalize_hotkey(s: str) -> str:
    """Return a canonical comparable form: lower-case, modifiers sorted in
    a fixed order, alias-folded. Empty string in → empty out."""
    s = (s or '').strip().lower()
    if not s:
        return ''
    # Replace common separators with '+' so we can split uniformly
    for sep in (' + ', ' +', '+ ', ' - ', '-', ' '):
        s = s.replace(sep, '+')
    parts = [p for p in s.split('+') if p]
    parts = [_MOD_ALIASES.get(p, p) for p in parts]
    # Split modifiers vs the final key
    mods = sorted({p for p in parts if p in _MOD_ORDER},
                  key=_MOD_ORDER.index)
    keys = [p for p in parts if p not in _MOD_ORDER]
    return '+'.join(mods + keys)


# ── Reference sets ──────────────────────────────────────────────────────────

# Hotkeys Windows owns and never delivers to user-space hooks. Picking any
# of these as a hotkey means the action silently never fires.
_WINDOWS_RESERVED = {
    normalize_hotkey(s) for s in [
        'ctrl+alt+delete',
        'win+l',          # lock workstation
        'win+d',          # show desktop
        'win+e',          # explorer
        'win+r',          # run dialog
        'win+i',          # settings
        'win+tab',        # task view
        'alt+tab',        # switcher (sometimes capturable, usually flaky)
        'win+a',          # action center
        'win+x',          # power user menu
        'win+m',          # minimize all
        'win+shift+s',    # snipping tool
        'win+v',          # clipboard history
        'win+space',      # input language
        'win+period',     # emoji
        'ctrl+shift+esc', # task manager
        'ctrl+alt+esc',
        'alt+escape',
    ]
}

# Keys that work but are risky, chosen by the user themselves at their
# own risk, but worth warning about.
_RISKY = {
    normalize_hotkey('alt+f4'):  'Alt+F4 closes the active window',
    normalize_hotkey('ctrl+w'):  'Ctrl+W closes a tab/document in most apps',
    normalize_hotkey('ctrl+s'):  'Ctrl+S triggers Save in most apps',
    normalize_hotkey('ctrl+p'):  'Ctrl+P opens Print in most apps',
    normalize_hotkey('ctrl+f'):  'Ctrl+F opens Find in most apps (including whiteboard)',
    normalize_hotkey('ctrl+t'):  'Ctrl+T opens a new tab in browsers',
    normalize_hotkey('ctrl+n'):  'Ctrl+N opens a new window in most apps',
    normalize_hotkey('f1'):      'F1 opens Help in most apps',
}

# Whiteboard's built-in single-key tool selectors and editor chords.
#
# RUNTIME BEHAVIOR (important): at draw time the whiteboard's JS reads the
# host app's hotkey list via `Api.get_reserved_keys()` and blocks itself
# from handling any key the host has claimed. So app hotkeys ALWAYS win,
# even inside the whiteboard window, the bundled-shortcut entries here
# are informational only.
#
# Keys that ARE main app hotkeys (ctrl+enter for dictation, alt+shift+w
# for refine, etc.) are intentionally NOT listed here, they're not
# "whiteboard shortcuts" from the user's perspective; they belong to the
# main app and the warning popup was misleading.
_BUNDLED_SHORTCUTS = {
    normalize_hotkey(k) for k in [
        # Tool letters
        'h', 'v', 'r', 'd', 'o', 'a', 'l', 'p', 't', 'e',
        'f', 'k', 'i', 'q',
        # Tool digits
        '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
        # Editor chords (whiteboard-local editing only, none clash with
        # the main app's binding set)
        'ctrl+a', 'ctrl+x', 'ctrl+c', 'ctrl+v', 'ctrl+shift+v',
        'ctrl+d', 'ctrl+g', 'ctrl+shift+g', 'ctrl+shift+l',
        'ctrl+=', 'ctrl+-', 'ctrl+0',
        'ctrl+y',                          # redo (we use alt+shift+z for undo-refine, not ctrl+z/y)
        'ctrl+f', 'ctrl+/', 'ctrl+shift+e',
        'shift+alt+d',
        # NOTE: 'ctrl+enter' removed, it's the main app's dictation hotkey
        # and the whiteboard's runtime defers to the host via
        # get_reserved_keys(). Listing it here was misleading users.
        'delete', 'tab', '?', 'shift+/',
        # NOTE: 'escape' removed too, every modal close behaviour is
        # consistent app-wide; the whiteboard's escape is also fine.
    ]
}


# ── Syntax check via the keyboard library ───────────────────────────────────

def _parses(s: str) -> bool:
    """True if the `keyboard` library can parse this string into scan codes.
    Importing keyboard at module top would pull a heavy dependency into
    every test that touches this module, defer it."""
    try:
        import keyboard
        keyboard.parse_hotkey(s)
        return True
    except Exception:
        return False


# ── Main validator ──────────────────────────────────────────────────────────

def validate_hotkey(
    hotkey: str,
    action: str,
    *,
    other_assignments: dict[str, str] | None = None,
) -> Diagnostic:
    """Validate a single proposed (action, hotkey) pair.

    Args:
        hotkey: the raw string the user typed / captured (e.g. "Alt+Shift+W").
        action: a stable identifier for what this hotkey will run
            ("refine", "prompt:Expand", "macro:abc123", "chain:Brainstorm").
            Used only in error messages and to skip self-comparisons.
        other_assignments: a dict {action_id → hotkey_str} of EVERY currently
            assigned hotkey in the app (Settings + per-prompt + per-chain +
            per-macro). Caller is responsible for collecting these.

    Returns the first diagnostic encountered, severity-ordered ERROR > WARN.
    OK only when every check passes.
    """
    raw = (hotkey or '').strip()
    if not raw:
        return Diagnostic(OK, '', action)  # blank = unassigned, fine.

    norm = normalize_hotkey(raw)

    # 1. Syntax
    if not _parses(raw):
        return Diagnostic(ERROR,
            f'"{raw}" isn\'t a valid hotkey. Use modifier+key, e.g. '
            f'Ctrl+Shift+Z, Alt+F4, Shift+F8.', action)

    # 2. OS-reserved, these silently never fire
    if norm in _WINDOWS_RESERVED:
        return Diagnostic(ERROR,
            f'"{raw}" is reserved by Windows and can never be captured by an '
            f'app. Pick something else.', action)

    # 3. Self-conflict with another app assignment
    if other_assignments:
        for other_act, other_hk in other_assignments.items():
            if other_act == action: continue
            if not other_hk: continue
            if normalize_hotkey(other_hk) == norm:
                return Diagnostic(ERROR,
                    f'"{raw}" is already assigned to "{other_act}". '
                    f'Two actions can\'t share a hotkey.', action)

    # 4. Whiteboard clash, INTENTIONALLY NOT WARNED.
    #
    # Earlier versions threw a "may be confusing" popup when a user assigned
    # a hotkey that also appears in the whiteboard's bundled shortcut list.
    # That was misleading: the whiteboard's runtime reads the host app's
    # reserved-hotkey list via Api.get_reserved_keys() and refuses to act
    # on any key the host has claimed, so the app's hotkey actually wins
    # inside the whiteboard window too. No real conflict exists; warning
    # the user about a non-issue caused more confusion than it prevented.
    # _BUNDLED_SHORTCUTS is kept for diagnostic introspection (e.g. tests
    # auditing key coverage) but no longer surfaces a Diagnostic to the UI.

    # 5. Risky pickings
    if norm in _RISKY:
        return Diagnostic(WARN,
            f'"{raw}", {_RISKY[norm]}. The app will still receive it, but '
            f'background apps may also react.', action)

    return Diagnostic(OK, '', action)


def validate_batch(
    proposed: dict[str, str],
    *,
    other_assignments: dict[str, str] | None = None,
) -> list[Diagnostic]:
    """Validate many at once. Used when applying the full Settings page.
    `proposed` and `other_assignments` are both action→hotkey dicts; the
    proposed entries are treated as authoritative (i.e. they replace
    matching entries in `other_assignments` for the self-conflict check).
    """
    if other_assignments is None:
        other_assignments = {}
    # The combined view the user is about to save
    combined = {**other_assignments, **proposed}

    out: list[Diagnostic] = []
    for action, hk in proposed.items():
        diag = validate_hotkey(hk, action, other_assignments={
            a: h for a, h in combined.items() if a != action
        })
        if diag.severity != OK:
            out.append(diag)
    return out


def collect_app_hotkeys(config: dict, prompts: list[dict] | None = None,
                        chains: list[dict] | None = None,
                        macros: list[dict] | None = None) -> dict[str, str]:
    """Build the canonical action→hotkey map across every assignment surface
    in the app. Pass this as `other_assignments` to validate_hotkey() so
    new bindings are checked against EVERYTHING already in flight."""
    out: dict[str, str] = {}
    for action, hk in (config.get('hotkeys') or {}).items():
        if hk: out[action] = hk
    for p in (prompts or []):
        hk = (p.get('hotkey') or '').strip()
        if hk: out[f'prompt:{p.get("title", "?")}'] = hk
    for c in (chains or []):
        hk = (c.get('hotkey') or '').strip()
        if hk: out[f'chain:{c.get("name", "?")}'] = hk
    for m in (macros or []):
        hk = (m.get('hotkey') or '').strip()
        if hk: out[f'macro:{m.get("name", m.get("id", "?"))}'] = hk
    return out
