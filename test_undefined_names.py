"""Static guard: catch undefined-name bugs before users hit them.

The bug we keep getting bitten by: a function or variable name is
*referenced* in code but never *defined* in scope. Python doesn't
complain at import time; the crash only fires when that specific code
path runs. Examples already paid for:
  - audio_editor.py called get_launcher() that didn't exist  ->  Shift+F10 silently failed
  - quicknotes / library / sticky_note used `exc` in lambdas
    after the except clause had already deleted it  ->  every OCR
    error path crashed with NameError instead of showing the real cause

pyflakes catches both classes statically. This test wraps it so any
new occurrence breaks the test suite before merging.

Run via:  E:\\Hotkeys\\venv\\Scripts\\python.exe E:\\Hotkeys\\test_undefined_names.py
Also runs naturally under pytest if/when that's wired up.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Files we want kept undefined-name-clean. Excludes the venv, the
# scratch helpers (anything starting with _), and the ask_docs models
# directory (third-party Sentence Transformers weights, not our code).
_INCLUDE_GLOBS = ['*.py', 'core/*.py', 'macros/*.py']
_EXCLUDE_PREFIX = '_'
_EXCLUDE_PATH_PARTS = {'venv', 'models', '__pycache__'}


def _gather_files() -> list[Path]:
    files: list[Path] = []
    for pat in _INCLUDE_GLOBS:
        for p in ROOT.glob(pat):
            if p.name.startswith(_EXCLUDE_PREFIX):
                continue
            if any(part in _EXCLUDE_PATH_PARTS for part in p.parts):
                continue
            files.append(p)
    return sorted(files)


def _run_pyflakes(files: list[Path]) -> list[str]:
    """Run pyflakes on `files` and return every output line. Pyflakes
    exits 1 when it finds any issues; we ignore the exit code and parse
    stdout (issues are written to stdout, not stderr)."""
    if not files:
        return []
    proc = subprocess.run(
        [sys.executable, '-m', 'pyflakes', *[str(f) for f in files]],
        capture_output=True, text=True, encoding='utf-8',
    )
    out = (proc.stdout + proc.stderr).splitlines()
    return [line for line in out if line.strip()]


def test_no_undefined_names() -> None:
    """The bug class we care about most. A NameError at runtime is the
    user-facing crash we keep fixing; this catches all current and
    future instances statically."""
    files = _gather_files()
    print(f'Scanning {len(files)} files for undefined names...')

    issues = _run_pyflakes(files)
    undefined = [line for line in issues if 'undefined name' in line]

    if undefined:
        print()
        print(f'FAIL: {len(undefined)} undefined-name issue(s):')
        for line in undefined:
            print(f'  {line}')
        print()
        print('Each of these will crash the app the moment the code path runs.')
        print('Fix by defining the name, importing the symbol, or — for '
              '`exc`/`e` inside a lambda after an except clause — binding it '
              'via a default arg: `lambda m=str(exc): handler(m)`.')
        raise AssertionError(f'{len(undefined)} undefined names')

    print('PASS: 0 undefined names across all monitored files.')


def test_modules_are_importable() -> None:
    """Smoke-test: every module we care about can be imported without
    raising. Catches top-level NameErrors, broken decorators, missing
    dependencies, etc. Skips modules whose import has known side effects
    (start a window, register hotkeys, etc.)."""
    sys.path.insert(0, str(ROOT))
    SIDE_EFFECT_FREE = [
        'core.typer',
        'mini_notepad',
        'audio_editor',
        'vision',
        'engine',
        'storage',
        'overlay',
        'spellcheck',
        'theme',
        'win_geometry',
        'win_helpers',
        'brand_icon',
        'dialogs',
        'hotkey_validator',
        'kbhook',
        'history_ui',
    ]
    failed: list[tuple[str, str]] = []
    for mod_name in SIDE_EFFECT_FREE:
        try:
            __import__(mod_name)
        except Exception as exc:
            failed.append((mod_name, f'{type(exc).__name__}: {exc}'))
    if failed:
        print()
        print(f'FAIL: {len(failed)} module(s) cannot be imported:')
        for mod, err in failed:
            print(f'  {mod}: {err}')
        raise AssertionError(f'{len(failed)} modules cannot be imported')
    print(f'PASS: {len(SIDE_EFFECT_FREE)} modules import cleanly.')


def main() -> None:
    try: sys.stdout.reconfigure(encoding='utf-8')
    except Exception: pass
    print('=' * 60)
    print('Static analysis guard')
    print('=' * 60)
    test_no_undefined_names()
    print()
    test_modules_are_importable()
    print()
    print('=' * 60)
    print('ALL STATIC CHECKS PASSED')
    print('=' * 60)


if __name__ == '__main__':
    main()
