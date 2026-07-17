"""Post-build dist verifier.

Called automatically by build_dist.py after PyInstaller finishes. Exits
non-zero if any critical asset is missing or malformed. Zero exit =
"this dist is safe to zip and ship".

The point: catch missing/broken bundled assets AT BUILD TIME so no
release ever ships without them. Historical bug: _bundled_keys.py was
silently missing from every dist since v3.1 because it was only in
PyInstaller's hiddenimports (which does NOT copy files). Users got
local-only fallback for cloud STT/refine/vision. This script exists
so that never happens again — for _bundled_keys or any other asset.

Each check is a (label, predicate, why-it-matters) tuple. Add new
checks as new assets get bundled.
"""
from __future__ import annotations
import sys
from pathlib import Path

DIST     = Path(__file__).resolve().parent / 'dist' / 'Hotkeys'
INTERNAL = DIST / '_internal'


def file_exists(rel: str) -> tuple[bool, str]:
    p = DIST / rel
    if p.exists():
        return True, f'{p} ({p.stat().st_size} bytes)'
    p2 = INTERNAL / rel
    if p2.exists():
        return True, f'{p2} ({p2.stat().st_size} bytes)'
    return False, f'MISSING: neither {DIST / rel} nor {INTERNAL / rel} exists'


def bundled_keys_valid() -> tuple[bool, str]:
    """_bundled_keys.py must exist AND contain non-empty GROQ + CEREBRAS
    keys with the right prefix. Empty strings would silently break cloud
    features same as missing file."""
    for parent in (DIST, INTERNAL):
        p = parent / '_bundled_keys.py'
        if not p.exists():
            continue
        text = p.read_text(encoding='utf-8', errors='ignore')
        checks = {
            'GROQ':     "gsk_",
            'GROQ_2':   "gsk_",
            'CEREBRAS': "csk-",
        }
        missing = []
        for name, prefix in checks.items():
            # Very loose parse: look for `NAME = '<prefix>...'`
            marker = f"{name}"
            if marker not in text:
                missing.append(f'{name} not defined')
                continue
            # Locate the assignment line and check prefix presence
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith(name + ' ') or stripped.startswith(name + '='):
                    if prefix not in stripped:
                        missing.append(f'{name} does not contain expected prefix {prefix!r}')
                    break
        if missing:
            return False, f'_bundled_keys.py present at {p} but INVALID: ' + '; '.join(missing)
        return True, f'_bundled_keys.py at {p} ({p.stat().st_size} bytes, GROQ+GROQ_2+CEREBRAS all valid)'
    return False, (
        f'_bundled_keys.py MISSING from BOTH {DIST} and {INTERNAL}. '
        f'Cloud STT/refine/vision will silently fall back to local-only. '
        f'This is the exact bug that hid for months in v3.1 through v3.2.6.'
    )


def whisper_model_size_ok(rel: str, min_mb: int) -> tuple[bool, str]:
    """Whisper model files should be within expected size range. A
    truncated download (e.g. GitHub Actions timeout) leaves a small
    corrupt file that lets PyInstaller succeed but breaks at runtime."""
    for parent in (DIST, INTERNAL):
        p = parent / rel
        if p.exists():
            mb = p.stat().st_size / (1024 * 1024)
            if mb < min_mb:
                return False, f'{p} is {mb:.1f}MB, expected >= {min_mb}MB (truncated?)'
            return True, f'{p} = {mb:.0f}MB'
    return False, f'MISSING: {rel}'


CHECKS = [
    # (label, callable returning (ok, detail))
    ('main executable',      lambda: file_exists('Hotkeys.exe')),
    ('bundled API keys',     bundled_keys_valid),
    ('diarize worker',       lambda: file_exists('diarize/diarize.exe')),
    ('whiteboard index',     lambda: file_exists('whiteboard_assets/dist/index.html')),
    ('audio editor',         lambda: file_exists('audio_editor_assets/tenacity/tenacity.exe')),
    ('webview2 runtime',     lambda: file_exists('webview2_runtime/msedgewebview2.exe')),
    ('whisper base model',   lambda: whisper_model_size_ok('models/base/model.bin', 100)),
    ('whisper small model',  lambda: whisper_model_size_ok('models/small/model.bin', 300)),
    # app_icon.ico is generated at runtime from brand_icon.py into
    # APPDATA — not bundled as .ico in dist. The module itself gets
    # frozen into PyInstaller's PYZ archive (not a .py/.pyc file on
    # disk), so verify it's importable from within the frozen exe by
    # checking for the exe's presence (Hotkeys.exe embeds brand_icon
    # via hiddenimports). We can't cheaply check module presence
    # inside PYZ from outside — trust PyInstaller here.
    ('prompts library',      lambda: file_exists('prompts.json')),
    ('assets folder',        lambda: file_exists('assets/silero_vad.onnx')),
    ('macros package',       lambda: file_exists('macros/__init__.py')),
    ('tessdata english',     lambda: file_exists('tessdata/eng.traineddata')),
    ('tessdata arabic',      lambda: file_exists('tessdata/ara.traineddata')),
    ('tessdata osd',         lambda: file_exists('tessdata/osd.traineddata')),
    # Ask Docs (Shift+F11 NotebookLM-style Q&A)
    ('ask_docs subpackage',
        lambda: file_exists('ask_docs/ui.py')),
    ('ask_docs MiniLM model',
        lambda: whisper_model_size_ok('ask_docs/models/all-MiniLM-L6-v2/model.onnx', 80)),
    ('ask_docs MiniLM tokenizer',
        lambda: file_exists('ask_docs/models/all-MiniLM-L6-v2/tokenizer.json')),
]


def main() -> int:
    if not DIST.exists():
        print(f'!! Dist not found at {DIST}')
        print(f'   Run build_dist.py first.')
        return 2

    print('=' * 60)
    print('Dist verification')
    print('=' * 60)
    fails = []
    for label, check in CHECKS:
        try:
            ok, detail = check()
        except Exception as e:
            ok, detail = False, f'check raised: {e!r}'
        mark = '  [OK]' if ok else '  [!!]'
        print(f'{mark} {label:22s} {detail}')
        if not ok:
            fails.append((label, detail))

    print('=' * 60)
    if fails:
        print(f'FAILED: {len(fails)} critical asset(s) missing or broken:')
        for label, detail in fails:
            print(f'  - {label}: {detail}')
        print()
        print('DO NOT ship this dist. Fix the missing assets and rebuild.')
        return 1
    print(f'PASSED: all {len(CHECKS)} critical assets present and valid.')
    print('Safe to zip + release.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
