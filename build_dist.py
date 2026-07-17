"""
build_dist.py, one-shot build script for Hotkeys dist
Usage:  E:\Hotkeys\venv\Scripts\python.exe build_dist.py

Steps:
  1. Run PyInstaller with hotkeys.spec
  2. Copy pywin32_system32 DLLs into dist root  (pywintypes*.dll, pythoncom*.dll)
  3. Copy macros/ package into dist root         (plain .py, PyInstaller can miss subpackages)
  4. Verify critical files are present
  5. Print final dist size

PyInstaller 6.x layout note:
  dist/Hotkeys/
    Hotkeys.exe          ← the launcher
    pywintypes3XX.dll    ← pywin32 DLLs must live here, beside the exe
    pythoncom3XX.dll
    macros/              ← source package copied here for runtime import
    _internal/           ← ALL other bundled files (libs, data, bytecode)
      customtkinter/
      av.libs/
      models/
      assets/
      prompts.json
      ...
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT       = Path(__file__).parent
VENV       = ROOT / 'venv'
PYTHON     = VENV / 'Scripts' / 'python.exe'
PYINST     = VENV / 'Scripts' / 'pyinstaller.exe'
SPEC       = ROOT / 'hotkeys.spec'
DIARSPEC   = ROOT / 'hotkeys_diarize.spec'
DIST_ROOT  = ROOT / 'dist'
DIST       = DIST_ROOT / 'Hotkeys'
INTERNAL   = DIST / '_internal'   # PyInstaller 6.x puts data/libs here
BUILD_DIR  = ROOT / 'build'
SITE       = VENV / 'Lib' / 'site-packages'
DIAR_DIST  = DIST_ROOT / 'diarize'  # temporary, contents merged into DIST/diarize/

def run(cmd, **kw):
    print(f'\n>>> {" ".join(str(c) for c in cmd)}')
    result = subprocess.run(cmd, **kw)
    if result.returncode != 0:
        print(f'FAILED (exit {result.returncode})')
        sys.exit(result.returncode)

# ── 1. PyInstaller ────────────────────────────────────────────────────────────
print('=' * 60)
print('Step 1: PyInstaller')
print('=' * 60)
run([
    str(PYINST), str(SPEC), '--noconfirm',
    '--distpath', str(DIST_ROOT),
    '--workpath', str(BUILD_DIR),
])

# ── 2. pywin32 system DLLs ────────────────────────────────────────────────────
# pywintypes3XX.dll and pythoncom3XX.dll must sit in the dist root alongside
# the exe so win32ui / win32gui / win32clipboard can load them at runtime.
# They must NOT only be in _internal, Windows searches beside the exe first.
print('\n' + '=' * 60)
print('Step 2: Copy pywin32_system32 DLLs')
print('=' * 60)
pw32_dir = SITE / 'pywin32_system32'
if not pw32_dir.exists():
    print(f'WARNING: {pw32_dir} not found, skipping')
else:
    for dll in pw32_dir.glob('*.dll'):
        dest = DIST / dll.name
        shutil.copy2(dll, dest)
        print(f'  copied {dll.name}')

# ── 3. macros/ subpackage ─────────────────────────────────────────────────────
# Copy macros/ to the dist root (beside Hotkeys.exe) so the frozen app can
# import it at runtime from sys.path[0] (the exe's directory).
print('\n' + '=' * 60)
print('Step 3: Copy macros/ package')
print('=' * 60)
macros_src = ROOT / 'macros'
macros_dst = DIST / 'macros'
if macros_dst.exists():
    shutil.rmtree(macros_dst)
shutil.copytree(macros_src, macros_dst)
print(f'  copied macros/ ({len(list(macros_dst.rglob("*.py")))} .py files)')

# ── 3b. Build out-of-process diarization worker ──────────────────────────────
# Separate exe so torch + pyannote load in their own process heap — no MKL /
# OpenMP runtime collisions with the main Hotkeys.exe bundle. The worker lands
# at <DIST>/diarize/diarize.exe alongside its own _internal/.
print('\n' + '=' * 60)
print('Step 3b: PyInstaller — diarization worker')
print('=' * 60)
if not DIARSPEC.exists():
    print(f'  WARNING: {DIARSPEC} missing, skipping diarize worker build')
else:
    run([
        str(PYINST), str(DIARSPEC), '--noconfirm',
        '--distpath', str(DIST),         # land inside the main dist folder
        '--workpath', str(BUILD_DIR / 'diarize'),
    ])
    # PyInstaller created <DIST>/diarize/diarize.exe + _internal next to it.
    diar_exe = DIST / 'diarize' / 'diarize.exe'
    if diar_exe.exists():
        size_mb = sum(
            f.stat().st_size for f in (DIST / 'diarize').rglob('*') if f.is_file()
        ) / 1024 / 1024
        print(f'  [OK] diarize worker built ({size_mb:.0f} MB)')
    else:
        print(f'  [WARN] diarize.exe not produced — speaker labels will be unavailable')


# ── 4. Verify critical files ──────────────────────────────────────────────────
# PyInstaller 6.x places almost everything under _internal/, check both
# the dist root (exe + pywin32 DLLs) and _internal/ (libraries + data).
print('\n' + '=' * 60)
print('Step 4: Verify critical files')
print('=' * 60)

# Items expected in the dist ROOT (beside the exe)
root_checks = [
    DIST / 'Hotkeys.exe',
    *list(DIST.glob('pywintypes*.dll')),
    *list(DIST.glob('pythoncom*.dll')),
    DIST / 'macros',
]

# Items expected inside _internal/
internal_checks = [
    INTERNAL / 'customtkinter',
    INTERNAL / 'av.libs',
    INTERNAL / '_sounddevice_data',
    *list(INTERNAL.glob('ctranslate2*')),
    INTERNAL / 'prompts.json',
    INTERNAL / 'assets',
    INTERNAL / 'models' / 'base',
    INTERNAL / 'models' / 'small',
    INTERNAL / 'pynput',
    # Bundled audio editor (Shift+F10). audio_editor.py spawns the
    # tenacity.exe inside this folder via subprocess.
    INTERNAL / 'audio_editor_assets' / 'tenacity' / 'tenacity.exe',
    # Spell-check dictionary — without this the background _get_checker
    # thread crashes and (before the v3.2 fixes) silently killed the exe.
    INTERNAL / 'spellchecker' / 'resources' / 'en.json.gz',
    # tkinterdnd2 native helper for drag-and-drop (Library + Transcribe).
    INTERNAL / 'tkinterdnd2',
    # Out-of-process diarization worker (Shift+F9 speaker labels).
    DIST / 'diarize' / 'diarize.exe',
]

all_ok = True
seen = set()

for p in root_checks + internal_checks:
    if p in seen:
        continue
    seen.add(p)
    # Show path relative to DIST for readability
    try:
        rel = p.relative_to(DIST)
    except ValueError:
        rel = p
    if p.exists():
        print(f'  [OK] {rel}')
    else:
        print(f'  [MISSING] {rel}')
        all_ok = False

# Verify model.bin files are non-zero (not accidentally excluded)
for model_dir in ['base', 'small']:
    mb = INTERNAL / 'models' / model_dir / 'model.bin'
    if mb.exists():
        size_mb = mb.stat().st_size / 1024 / 1024
        print(f'  [OK] models/{model_dir}/model.bin  ({size_mb:.0f} MB)')
    else:
        print(f'  [MISSING] models/{model_dir}/model.bin')
        all_ok = False

# ── 5. Dist size ──────────────────────────────────────────────────────────────
print('\n' + '=' * 60)
print('Step 5: Dist size')
print('=' * 60)
total = sum(f.stat().st_size for f in DIST.rglob('*') if f.is_file())
print(f'  Total: {total / 1024 / 1024:.0f} MB  ({DIST})')

print('\n' + '=' * 60)
if not all_ok:
    print('BUILD FAILED (step 4) — one or more critical files are MISSING.')
    print('Do NOT ship this dist. Fix the spec, then re-run build_dist.py.')
    sys.exit(1)

# ── 6. Comprehensive dist verification ────────────────────────────────────────
# Second-layer check that catches everything Step 4 doesn't (bundled API
# keys with wrong prefix, whisper models truncated below expected size,
# whiteboard/audio-editor/webview2 runtime missing, etc.). Historical
# root cause of the _bundled_keys.py silent-missing bug was that Step 4
# never explicitly checked it — verify_dist.py enumerates EVERY critical
# asset. If ANY check fails here, the dist is invalid and we abort.
print('\n' + '=' * 60)
print('Step 6: Comprehensive verification (verify_dist.py)')
print('=' * 60)
verify_script = ROOT / 'verify_dist.py'
if verify_script.exists():
    verify_result = subprocess.run(
        [sys.executable, str(verify_script)],
        cwd=str(ROOT),
    )
    if verify_result.returncode != 0:
        print()
        print('=' * 60)
        print('BUILD FAILED (step 6 verification).')
        print('Do NOT zip or release this dist. Fix the issues above and rebuild.')
        print('=' * 60)
        sys.exit(verify_result.returncode)
else:
    print(f'  [WARN] verify_dist.py not found at {verify_script} — skipping.')
    print(f'         Manually verify all critical assets before shipping.')

print('\n' + '=' * 60)
print('BUILD COMPLETE, dist/Hotkeys/ is ready to zip and ship')
print()
print('Distribution notes:')
print('  • Zip the entire dist/Hotkeys/ folder and share it')
print('  • Recipient: extract anywhere, run Hotkeys.exe, no install needed')
print('  • User data (config, logs, prompts) stored in dist/Hotkeys/data/')
print('  * Bundled API keys ship in _bundled_keys.py; user-added keys via tray -> Settings')
print('=' * 60)
