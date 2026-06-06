"""Regression test for the tab-render safety guard.

Background
----------
Every Library tab render method (_render_macros / _render_notes_tab /
_render_whiteboard_tab / etc.) destroys all children of self._scroll
at its very first line. That's safe WHEN self._scroll has been swapped
to the per-tab container by _show_active_tab / _invalidate_tab /
_prewarm_tab — but is CATASTROPHIC when called with self._scroll still
pointing at the outer CTkScrollableFrame, because the outer scroll's
direct children are ALL tab containers. One unsafe call wipes every
tab in one shot.

That's exactly what main.py did in its Quick-Notes-close handler
(library._render_notes_tab() directly), and that's the bug the user
saw as "tab strip is stuck, click any tab, content stays on Notes."

The guard
---------
Each render method now starts with:

    if self._render_tab_guard(tab_key):
        return

The guard detects when self._scroll IS the outer scroll (i.e., no swap
in effect) and reroutes through _invalidate_tab(tab_key). This makes
ANY direct call to a render method safe, regardless of caller context.

This test
---------
1. Verifies every render method has the guard (static AST check).
2. Verifies that a direct call to a render method (simulating the
   main.py bug) does NOT destroy other tab containers.

Run: venv/Scripts/python.exe test_tab_guard.py
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


# Methods we expect to find — used ONLY for the "no method was deleted
# by accident" sanity check. The guard itself is enforced on AUTO-
# DISCOVERED methods (every `def _render_*_tab` / `_render_macro_cards`
# in the source), so new tabs are checked without anyone having to
# touch this file. The expected-key mapping just tells the test which
# tabs use a literal key vs the parameterized `_render_slot_tab(key)`.
EXPECTED_LITERAL_KEYS = {
    '_render_macro_cards':       'macros',
    '_render_recorder_tab':      'recorder',
    '_render_gif_tab':           'gif',
    '_render_ask_tab':           'ask',
    '_render_chains_tab':        'chains',
    '_render_notes_tab':         'notes',
    '_render_transcribe_tab':    'transcribe',
    '_render_whiteboard_tab':    'whiteboard',
    '_render_audio_editor_tab':  'audio_editor',
    '_render_web_tab':           'web',
}
# Methods that take their guard key from a parameter, so we don't pin
# the literal value.
PARAMETERIZED_METHODS = {'_render_slot_tab'}


def _discover_render_methods(tree: ast.Module) -> list[ast.FunctionDef]:
    """Return every method whose name matches the per-tab render
    convention. By doing this from the source tree (not a hardcoded
    list), a new tab added to library.py is automatically subject to
    the guard check — the dev cannot skip it by forgetting to update
    this file."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            n = node.name
            if (n.endswith('_tab') and n.startswith('_render_')) \
                    or n == '_render_macro_cards':
                # _render_cards_impl is the dispatcher, not a per-tab
                # render — skip it.
                if n == '_render_cards_impl':
                    continue
                out.append(node)
    return out


# ── 1. Static check: every render method starts with the guard ───────────────

def test_every_render_method_has_guard() -> None:
    print('--- Static guard presence (auto-discovered) ---', flush=True)
    src = (HOTKEYS / 'library.py').read_text(encoding='utf-8')
    tree = ast.parse(src)

    discovered = _discover_render_methods(tree)
    if not discovered:
        _fail('discovery',
              'no _render_*_tab methods found — library.py changed?')
    _ok(f'auto-discovered {len(discovered)} render method(s) by '
        f'naming convention')

    # Sanity: every name we historically knew about must still exist.
    # If someone renames or deletes a tab, this fails loud rather than
    # silently passing because no method was found to check.
    expected_names = (set(EXPECTED_LITERAL_KEYS) | PARAMETERIZED_METHODS)
    found_names = {f.name for f in discovered}
    missing = expected_names - found_names
    if missing:
        _fail('historical methods present',
              f'expected methods missing from source: {missing}')
    _ok('all historically-known render methods still present')

    # Each auto-discovered method MUST have the guard as its first
    # statement (after an optional docstring).
    for func in discovered:
        name = func.name
        body = func.body[:]
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        if not body:
            _fail(f'{name} has guard',
                  'method body is empty after docstring')
        first = body[0]
        match = (
            isinstance(first, ast.If)
            and isinstance(first.test, ast.Call)
            and isinstance(first.test.func, ast.Attribute)
            and first.test.func.attr == '_render_tab_guard'
            and len(first.body) == 1
            and isinstance(first.body[0], ast.Return)
        )
        if not match:
            _fail(f'{name} has guard',
                  f'first non-docstring statement is not the guard. '
                  f'New tabs MUST start with `if self._render_tab_guard'
                  f'(\'<key>\'): return` — see the comment above '
                  f'_render_cards_impl in library.py.')
        # Literal-key methods get an exact-match check; parameterized
        # methods (currently just _render_slot_tab) get a softer check
        # that the guard call has at least one arg.
        if name in EXPECTED_LITERAL_KEYS:
            expected_key = EXPECTED_LITERAL_KEYS[name]
            args = first.test.args
            if not (len(args) == 1
                    and isinstance(args[0], ast.Constant)
                    and args[0].value == expected_key):
                _fail(f'{name} guard key',
                      f'expected literal {expected_key!r}, '
                      f'got {ast.dump(args[0])[:80]}')
            _ok(f'{name} -> guard with key={expected_key!r}')
        elif name in PARAMETERIZED_METHODS:
            if not first.test.args:
                _fail(f'{name} guard key',
                      'parameterized guard call has no arguments')
            _ok(f'{name} -> guard with parameterized key')
        else:
            # New tab the test doesn't know about specifically — it
            # passed the guard-presence check, so this is just an
            # informational note that the auto-discovery handled it.
            _ok(f'{name} -> guard present (new tab, auto-discovered)')


# ── 2. Behavioural check: direct call doesn't nuke other tab containers ──────

def test_direct_call_is_safe() -> None:
    print('--- Direct-call safety ---', flush=True)
    # We can't instantiate LibraryWindow standalone (heavy deps + tray +
    # win-only Win32 calls), so the test simulates the structural shape
    # with a minimal stub mirroring the guard contract.
    import tkinter as tk
    import customtkinter as ctk
    ctk.set_appearance_mode('dark')

    root = tk.Tk()
    root.withdraw()
    try:
        # Stub mirrors LibraryWindow's _outer_scroll + _scroll + tab
        # container layout exactly.
        outer_scroll = ctk.CTkScrollableFrame(root)
        outer_scroll.pack(fill='both', expand=True)
        containers = {}
        for key in ('prompts', 'notes', 'whiteboard'):
            c = ctk.CTkFrame(outer_scroll, fg_color='transparent')
            c.grid(row=0, column=0, sticky='nsew')
            containers[key] = c

        class _Stub:
            pass
        stub = _Stub()
        stub._outer_scroll = outer_scroll
        stub._scroll       = outer_scroll  # ← unswapped, the bug scenario
        stub._tab_containers = containers
        stub._tab_built      = set(containers)
        stub._active_tab     = 'notes'
        stub._render_tab_guard_routed_to = []  # spy

        # Copy the real guard implementation behaviour.
        def _render_tab_guard(self, tab_key):
            if self._scroll is self._outer_scroll:
                self._render_tab_guard_routed_to.append(tab_key)
                # In real code this would call _invalidate_tab which
                # sets up the swap. Here we just record it.
                return True
            return False

        def _unsafe_render(self):
            # Simulates an old-style render method without the guard
            # (the broken behaviour): destroy all children of
            # self._scroll. If self._scroll is the outer scroll, this
            # nukes every tab container.
            for w in self._scroll.winfo_children():
                w.destroy()

        def _safe_render(self):
            # Real render shape — guarded FIRST, then the destroy loop.
            if self._render_tab_guard('notes'):
                return
            for w in self._scroll.winfo_children():
                w.destroy()

        _Stub._render_tab_guard = _render_tab_guard

        # First demonstrate the bug DOES happen without the guard.
        before = {k: bool(c.winfo_exists()) for k, c in containers.items()}
        if not all(before.values()):
            _fail('baseline', f'containers not initially valid: {before}')
        _ok('baseline: 3 tab containers alive')

        _unsafe_render(stub)
        after_unsafe = {k: bool(c.winfo_exists()) for k, c in containers.items()}
        if any(after_unsafe.values()):
            _fail('bug reproduction',
                  f'unsafe direct render should have destroyed all '
                  f'containers, but some survived: {after_unsafe}')
        _ok('bug reproduced: unsafe direct render destroyed all containers')

        # Now re-create containers and exercise the GUARDED render.
        containers.clear()
        for key in ('prompts', 'notes', 'whiteboard'):
            c = ctk.CTkFrame(outer_scroll, fg_color='transparent')
            c.grid(row=0, column=0, sticky='nsew')
            containers[key] = c
        stub._tab_containers = containers
        stub._scroll = outer_scroll  # unswapped again
        stub._render_tab_guard_routed_to.clear()

        _safe_render(stub)

        after_safe = {k: bool(c.winfo_exists()) for k, c in containers.items()}
        if not all(after_safe.values()):
            _fail('guard protection',
                  f'guarded render destroyed containers: {after_safe}')
        if stub._render_tab_guard_routed_to != ['notes']:
            _fail('guard routing',
                  f'expected guard to route through "notes", got '
                  f'{stub._render_tab_guard_routed_to}')
        _ok('guarded render kept all 3 containers + routed via guard')

    finally:
        root.destroy()


# ── 3. Boot-time runtime check actually catches a missing guard ──────────────

def test_boot_check_detects_missing_guard() -> None:
    """Even if the test in CI is skipped, LibraryWindow.__init__ calls
    _verify_tab_guards_at_boot which scans every per-tab render method
    and logs a CRITICAL warning if any is unguarded. This test proves
    that detector actually works: we add a synthetic unguarded render
    method to LibraryWindow, run the boot check, and verify the
    warning shows up in the captured log records."""
    print('--- Boot-time detector ---', flush=True)
    import logging

    # Inject a fake unguarded render method on the LibraryWindow class.
    # We don't construct a LibraryWindow instance (its dependencies are
    # too heavy); _verify_tab_guards_at_boot is a classmethod that
    # scans the class itself, so monkey-patching the class is enough.
    from library import LibraryWindow

    def _render_fake_broken_tab(self):
        # NO guard line at the top — exactly the shape a new tab would
        # have if a dev forgot the guard.
        for w in self._scroll.winfo_children():
            w.destroy()

    LibraryWindow._render_fake_broken_tab = _render_fake_broken_tab
    try:
        # Capture log output from the library module's logger.
        records: list[logging.LogRecord] = []
        class _Capture(logging.Handler):
            def emit(self, record): records.append(record)
        cap = _Capture()
        lib_logger = logging.getLogger('library')
        lib_logger.addHandler(cap)
        prior_level = lib_logger.level
        lib_logger.setLevel(logging.DEBUG)
        try:
            LibraryWindow._verify_tab_guards_at_boot()
        finally:
            lib_logger.removeHandler(cap)
            lib_logger.setLevel(prior_level)

        crit = [r for r in records
                if r.levelno >= logging.CRITICAL
                and '_render_fake_broken_tab' in r.getMessage()]
        if not crit:
            _fail('CRITICAL log emitted',
                  'expected a CRITICAL log naming '
                  '_render_fake_broken_tab, got: '
                  f'{[r.getMessage() for r in records]}')
        _ok(f'boot check emitted CRITICAL warning for the unguarded '
            f'method: {crit[0].getMessage()[:90]}...')
    finally:
        # Clean up so other tests / later boots don't keep seeing this.
        delattr(LibraryWindow, '_render_fake_broken_tab')


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print('===== Tab-render guard regression test =====', flush=True)
    tests = [
        ('static_guard_presence',     test_every_render_method_has_guard),
        ('direct_call_safety',        test_direct_call_is_safe),
        ('boot_detector',             test_boot_check_detects_missing_guard),
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
