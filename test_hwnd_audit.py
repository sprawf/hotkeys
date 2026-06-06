"""Regression test that polices the GA_ROOT / winfo_id() footgun.

Background
----------
Tk's winfo_id() returns the inner widget HWND, not the OS top-level
window. For an overrideredirect(True) Toplevel, the inner HWND is a
CHILD of the real top-level — Win32 calls applied to the child no-op
or hit the wrong window. This bug has hit us at least four times
(Quick Notes maximize, Quick Notes rounded corners, AskPill lift,
audio-editor hint overlay lift). The fix everywhere now routes through
`win_helpers.top_level_hwnd(widget)`.

This test enforces TWO rules to keep us out of the bug forever:

1. The helper function itself works: it returns SOME hwnd (mocked in
   the test) and handles being called with a fresh Tk widget.

2. AST scan: no source file may call a Win32 API (SetWindowPos,
   DwmSetWindowAttribute, SetWindowDisplayAffinity, MoveWindow,
   GetWindowLong, SetWindowLong, GetWindowLongPtr, SetWindowLongPtr)
   with `widget.winfo_id()` as the hwnd argument. The replacement is
   `top_level_hwnd(widget)`. Caught at the AST level so reviewers
   don't have to remember the rule.
"""
from __future__ import annotations

import ast
import sys
import traceback
from pathlib import Path

HOTKEYS = Path(__file__).parent
sys.path.insert(0, str(HOTKEYS))


def _ok(label: str) -> None:
    print(f'  PASS  {label}', flush=True)


def _fail(label: str, why: str) -> None:
    print(f'  FAIL  {label}: {why}', flush=True)
    raise AssertionError(f'{label}: {why}')


# Win32 functions that accept HWND as their first arg. If any of these
# is called with `widget.winfo_id()` as that first arg, it's almost
# certainly wrong (or at minimum fragile).
HWND_FIRST_ARG_APIS = {
    'SetWindowPos',
    'DwmSetWindowAttribute',
    'DwmGetWindowAttribute',
    'SetWindowDisplayAffinity',
    'MoveWindow',
    'GetWindowLong', 'SetWindowLong',
    'GetWindowLongA', 'SetWindowLongA',
    'GetWindowLongW', 'SetWindowLongW',
    'GetWindowLongPtr', 'SetWindowLongPtr',
    'GetWindowLongPtrA', 'SetWindowLongPtrA',
    'GetWindowLongPtrW', 'SetWindowLongPtrW',
    'GetWindowRect',
    'GetClientRect',
    'SetForegroundWindow',
    'IsIconic',
    'ShowWindow',
}

# Source files we audit. Skip generated / vendored / test / packaged
# tree subdirectories.
SOURCES = [
    'library.py', 'quicknotes.py', 'overlay.py', 'explain_pill.py',
    'audio_editor.py', 'screenshot.py', 'screen_recorder.py',
    'gif_recorder.py', 'settings.py', 'transcribe_ui.py',
    'history_ui.py', 'main.py', 'dialogs.py',
    'win_helpers.py',
]


# ── 1. Helper sanity ─────────────────────────────────────────────────────────

def test_top_level_hwnd_helper() -> None:
    print('--- top_level_hwnd helper ---', flush=True)
    import tkinter as tk
    from win_helpers import top_level_hwnd

    root = tk.Tk()
    root.withdraw()
    try:
        h = top_level_hwnd(root)
        if not isinstance(h, int) or h == 0:
            _fail('returns hwnd for root',
                  f'expected non-zero int, got {h!r}')
        _ok(f'returns hwnd for root: {h:#x}')

        frame = tk.Frame(root)
        frame.pack()
        root.update_idletasks()
        h2 = top_level_hwnd(frame)
        if not isinstance(h2, int) or h2 == 0:
            _fail('returns hwnd for child frame', f'got {h2!r}')
        _ok(f'returns hwnd for child frame: {h2:#x}')

        # Calling with a destroyed widget must not raise.
        broken = tk.Frame(root)
        broken.destroy()
        h3 = top_level_hwnd(broken)
        if not isinstance(h3, int):
            _fail('no exception on destroyed widget', f'got {h3!r}')
        _ok('no exception on destroyed widget (returned safely)')
    finally:
        root.destroy()


# ── 2. AST scan: no `<api>(widget.winfo_id(), …)` calls remain ───────────────

def test_no_winfo_id_in_hwnd_apis() -> None:
    print('--- AST scan: winfo_id() in HWND-first-arg APIs ---', flush=True)
    violations = []
    for fname in SOURCES:
        p = HOTKEYS / fname
        if not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
        except SyntaxError as e:
            _fail(f'{fname} parses', f'{e}')
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Pull the called function's name (last attribute or bare
            # name) so we match both `user32.SetWindowPos(...)` and
            # `SetWindowPos(...)`.
            fn = node.func
            if isinstance(fn, ast.Attribute):
                fn_name = fn.attr
            elif isinstance(fn, ast.Name):
                fn_name = fn.id
            else:
                continue
            if fn_name not in HWND_FIRST_ARG_APIS:
                continue
            if not node.args:
                continue
            first = node.args[0]
            # Does the first arg call `widget.winfo_id()` directly?
            if (isinstance(first, ast.Call)
                    and isinstance(first.func, ast.Attribute)
                    and first.func.attr == 'winfo_id'):
                violations.append((
                    fname, node.lineno, fn_name,
                    ast.unparse(first)
                    if hasattr(ast, 'unparse') else 'widget.winfo_id()',
                ))
    if violations:
        report = '\n'.join(
            f'  {f}:{ln}  {api}({snippet}, …)' for (f, ln, api, snippet) in violations)
        _fail('no raw winfo_id() in HWND APIs',
              f'\n{report}\n\nReplace `widget.winfo_id()` with '
              '`top_level_hwnd(widget)` from win_helpers.')
    _ok(f'all {len(SOURCES)} files scanned; no raw winfo_id() passed '
        'to HWND-first-arg Win32 APIs')


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print('===== HWND audit regression test =====', flush=True)
    tests = [
        ('helper_sanity', test_top_level_hwnd_helper),
        ('ast_scan',      test_no_winfo_id_in_hwnd_apis),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except AssertionError:
            failed.append(name)
        except Exception as e:
            print(f'  EXCEPTION  {name}: {e}\n{traceback.format_exc()}',
                  flush=True)
            failed.append(name)
    print('===== Result =====', flush=True)
    if failed:
        print(f'  {len(failed)} FAILED: {failed}', flush=True)
        return 1
    print(f'  All {len(tests)} tests PASSED', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
