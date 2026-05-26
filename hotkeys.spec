# hotkeys.spec — PyInstaller build spec for Hotkeys
# Run:  E:\Hotkeys\venv\Scripts\pyinstaller.exe hotkeys.spec

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, collect_all

ROOT = Path(r'E:\Hotkeys')

# ── Data files ────────────────────────────────────────────────────────────────

datas = []

# faster_whisper ships its own silero_vad_v6.onnx inside faster_whisper/assets/
# — required at transcription time, NOT the same as our assets/silero_vad.onnx
datas += collect_data_files('faster_whisper', include_py_files=False)

# UI theme/image data for customtkinter
datas += collect_data_files('customtkinter', include_py_files=False)

# ctranslate2 — ships model-format DLLs and CUDA kernels as data
datas += collect_data_files('ctranslate2', include_py_files=False)

# sounddevice PortAudio data
datas += collect_data_files('_sounddevice_data', include_py_files=False)

# onnxruntime providers + data
datas += collect_data_files('onnxruntime', include_py_files=False)

# av (PyAV) — required by faster_whisper.audio, screen recorder, and GIF encoder
# collect_all picks up pyd files, av.libs FFmpeg DLLs, and data in one shot
_av_datas, _av_bins, _av_hidden = collect_all('av')

# pynput — macro recorder uses pynput for mouse/keyboard capture & replay
# collect_all is needed; PyInstaller misses the Windows backend otherwise
_pynput_datas, _pynput_bins, _pynput_hidden = collect_all('pynput')
datas += _av_datas
datas += _pynput_datas

# Whisper models (base=141 MB, small=464 MB; large-v3-turbo excluded — no model.bin)
datas += [(str(ROOT / 'models' / 'base'),  'models/base')]
datas += [(str(ROOT / 'models' / 'small'), 'models/small')]

# Silero VAD ONNX model
datas += [(str(ROOT / 'assets'), 'assets')]

# Prompt library
datas += [(str(ROOT / 'prompts.json'), '.')]

# ── Binaries (native shared libs) ─────────────────────────────────────────────

binaries = []
binaries += collect_dynamic_libs('ctranslate2')
binaries += collect_dynamic_libs('onnxruntime')
binaries += _av_bins       # av.libs FFmpeg DLLs + av .pyd extensions
binaries += _pynput_bins   # pynput Windows backend

# ── Hidden imports ────────────────────────────────────────────────────────────

hiddenimports = [
    # UI
    'customtkinter',
    'darkdetect',
    'PIL._tkinter_finder',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageFont',
    'PIL.ImageTk',

    # Whisper / transcription
    'ctranslate2',
    'faster_whisper',
    'faster_whisper.transcribe',
    'faster_whisper.audio',
    'faster_whisper.feature_extractor',
    'faster_whisper.tokenizer',
    'faster_whisper.vad',
    'onnxruntime',
    'onnxruntime.capi._pybind_state',

    # Audio
    'sounddevice',
    'numpy',

    # Noise reduction
    'noisereduce',
    'scipy',
    'scipy.signal',
    'scipy.signal.windows',
    'scipy.fft',
    'scipy._lib.messagestream',

    # System tray
    'pystray',
    'pystray._win32',

    # Win32
    'win32api',
    'win32con',
    'win32clipboard',
    'win32gui',
    'win32process',
    'pywintypes',

    # faster_whisper runtime deps (imported at module level even with bundled models)
    'huggingface_hub',
    'huggingface_hub.utils',
    'fsspec',
    'fsspec.implementations.local',

    # AI providers
    'groq',
    'cerebras',
    'cerebras.cloud',
    'cerebras.cloud.sdk',
    'httpx',
    'certifi',
    'truststore',

    # Hotkeys / clipboard
    'keyboard',
    'mouse',
    'pyperclip',

    # pynput — macro recorder (Shift+F1)
    'pynput',
    'pynput.keyboard',
    'pynput.keyboard._win32',
    'pynput.mouse',
    'pynput.mouse._win32',

    # Win32 UI — screen capture used by screen recorder + GIF recorder
    'win32ui',

    # Utilities
    'psutil',
    'psutil._pswindows',
    'spellchecker',

    # App core modules
    'storage',
    'engine',
    'overlay',
    'library',
    'settings',
    'history_ui',
    'dialogs',
    'theme',
    'spellcheck',
    'sticky_note',
    '_bundled_keys',
    'core',
    'core.audio',
    'core.vad',
    'core.transcriber',
    'core.typer',
    'core.sounds',

    # New feature modules
    'screenshot',
    'vision',
    'explain_pill',
    'screen_recorder',
    'gif_recorder',
    'macros',
    'macros.recorder',
    'macros.library',
    'macros.save_prompt',

    # Quick Notes
    'quicknotes',
]

# Collect all submodules of heavy packages so nothing gets missed
hiddenimports += collect_submodules('faster_whisper')
hiddenimports += collect_submodules('huggingface_hub')
hiddenimports += _av_hidden   # av submodules from collect_all
hiddenimports += collect_submodules('ctranslate2')
hiddenimports += [m for m in collect_submodules('onnxruntime') if 'quantization' not in m and 'onnx' not in m]
hiddenimports += collect_submodules('groq')
hiddenimports += collect_submodules('cerebras')
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('scipy.signal')
hiddenimports += _pynput_hidden   # pynput submodules from collect_all

# ── Excludes (heavy packages NOT used by this app) ────────────────────────────

excludes = [
    'llama_cpp',
    'matplotlib',
    'contourpy',
    'cycler',
    'kiwisolver',
    'pyparsing',
    'fontTools',
    # NOTE: huggingface_hub and fsspec must NOT be excluded —
    # faster_whisper/utils.py imports huggingface_hub at module level,
    # even though we bundle the models and never download them at runtime.
    'unittest',
    'test',
    'tkinter.test',
    'xmlrpc',
    'email.mime',
    'http.server',
    'urllib.robotparser',
]

# ── Analysis ──────────────────────────────────────────────────────────────────

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Hotkeys',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX can corrupt ctranslate2/onnxruntime DLLs — skip
    console=False,       # no terminal window (tray app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # No .ico file present — icon drawn at runtime via PIL
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Hotkeys',
)
