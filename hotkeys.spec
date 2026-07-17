# hotkeys.spec — PyInstaller build spec for Hotkeys
# Run:  E:\Hotkeys\venv\Scripts\pyinstaller.exe hotkeys.spec

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, collect_all

ROOT = Path(r'E:\Hotkeys')

# ── Generate the brand icon up-front ─────────────────────────────────────────
# The spec is self-contained: it imports brand_icon.py and writes the .ico
# into the build tree before PyInstaller starts packaging. No manual prep
# step required ("run main.py first" etc.) — clone the repo and `pyinstaller
# hotkeys.spec` Just Works™.
sys.path.insert(0, str(ROOT))
from brand_icon import save_ico  # noqa: E402
_BRAND_ICO_FOR_EXE = ROOT / 'build_icon.ico'
save_ico(str(_BRAND_ICO_FOR_EXE))
print(f'[hotkeys.spec] brand icon written to {_BRAND_ICO_FOR_EXE}')

# ── Data files ────────────────────────────────────────────────────────────────

datas = []

# faster_whisper ships its own silero_vad_v6.onnx inside faster_whisper/assets/
# — required at transcription time, NOT the same as our assets/silero_vad.onnx
datas += collect_data_files('faster_whisper', include_py_files=False)

# UI theme/image data for customtkinter
datas += collect_data_files('customtkinter', include_py_files=False)

# pyspellchecker — ships en/de/es/fr/... .json.gz dictionaries under
# spellchecker/resources/. Without this PyInstaller bundles the .py but
# NOT the dictionaries, and the background _get_checker() thread raises
# ValueError("The provided dictionary language (en) does not exist!") at
# import time of spellcheck.py — silently killing the frozen exe between
# hotkey registration and tray creation.
datas += collect_data_files('spellchecker', include_py_files=False)

# tkinterdnd2 — ships native libtkdnd*.dll under tkinterdnd2/tkdnd/win-x64/.
# Used by the Library and Transcribe UIs for drag-and-drop. Bundled
# transitively in past builds by analyser-walk luck; making it explicit
# so a spec edit can never silently drop drag-drop support.
datas += collect_data_files('tkinterdnd2', include_py_files=False)

# ctranslate2 — ships model-format DLLs and CUDA kernels as data
datas += collect_data_files('ctranslate2', include_py_files=False)

# sounddevice PortAudio data
datas += collect_data_files('_sounddevice_data', include_py_files=False)

# onnxruntime providers + data
datas += collect_data_files('onnxruntime', include_py_files=False)

# av (PyAV) — required by faster_whisper.audio, screen recorder, and GIF encoder
# collect_all picks up pyd files, av.libs FFmpeg DLLs, and data in one shot
_av_datas, _av_bins, _av_hidden = collect_all('av')

# ── Shift+F9 Transcribe pipeline ─────────────────────────────────────────────
# DIAGNOSTIC: temporarily NOT bundling torch + pyannote.audio + torchaudio +
# soundfile. Hypothesis: their bundled MKL/OpenMP/BLAS DLLs conflict with
# ctranslate2/onnxruntime/numpy/av, producing the deterministic heap
# corruption (STATUS_STACK_BUFFER_OVERRUN 0xc0000409 at PyInstaller bootloader
# offset 0x1c325). v3.0 didn't bundle these and worked. Restoring them with
# proper isolation is a follow-up — for now, prove the hypothesis.
# Shift+F9 transcribe will gracefully fall back to no-diarization mode at
# runtime (no speaker labels in transcripts, but the rest works).
# _torch_datas,   _torch_bins,   _torch_hidden   = collect_all('torch')
# _taudio_datas,  _taudio_bins,  _taudio_hidden  = collect_all('torchaudio')
# _pyanno_datas,  _pyanno_bins,  _pyanno_hidden  = collect_all('pyannote.audio')
# _sf_datas,      _sf_bins,      _sf_hidden      = collect_all('soundfile')
_ytdl_datas,    _ytdl_bins,    _ytdl_hidden    = collect_all('yt_dlp')
# imageio-ffmpeg ships a portable ffmpeg binary (~83 MB) under
# imageio_ffmpeg/binaries/ — required so yt-dlp can merge separate
# video+audio streams (every YouTube format above 720p is split). Without
# this the F9 downloader silently caps quality at 720p single-stream.
_ioff_datas,    _ioff_bins,    _ioff_hidden    = collect_all('imageio_ffmpeg')

# pynput — macro recorder uses pynput for mouse/keyboard capture & replay
# collect_all is needed; PyInstaller misses the Windows backend otherwise
_pynput_datas, _pynput_bins, _pynput_hidden = collect_all('pynput')
datas += _av_datas
datas += _pynput_datas
# datas += _torch_datas
# datas += _taudio_datas
# datas += _pyanno_datas
# datas += _sf_datas
datas += _ytdl_datas
datas += _ioff_datas

# Whisper models (base=141 MB, small=464 MB; large-v3-turbo excluded — no model.bin)
datas += [(str(ROOT / 'models' / 'base'),  'models/base')]
datas += [(str(ROOT / 'models' / 'small'), 'models/small')]

# Silero VAD ONNX model + pyannote diarization pipeline (Shift+F9 Transcribe).
# `assets/diarization/` holds the config.yaml + segmentation + embedding +
# PLDA files — pre-downloaded with HF auth at dev time, redistributed under
# CC-BY-4.0. ~33 MB. Resolved at runtime via storage.assets_dir() which
# returns <_MEIPASS>/assets in frozen mode.
datas += [(str(ROOT / 'assets'), 'assets')]

# Prompt library
datas += [(str(ROOT / 'prompts.json'), '.')]

# ── Bundled API keys (Cerebras + Groq) — CRITICAL ────────────────────────────
# _bundled_keys.py provides the free-tier API keys baked into every dist so
# users get instant cloud STT / refine / vision without needing to sign up
# for their own keys. Listing it in hiddenimports alone is NOT enough:
# PyInstaller SILENTLY skips missing hidden imports. Explicit datas entry
# is what actually copies the .py file into the dist. Missing file at
# build time = FATAL (users would get local-only fallback + confused).
_bk = ROOT / '_bundled_keys.py'
if _bk.exists():
    datas += [(str(_bk), '.')]
    print(f'++ _bundled_keys.py bundled ({_bk.stat().st_size} bytes)')
else:
    raise SystemExit(
        f'!! FATAL: {_bk} missing. Every dist without it silently falls '
        f'back to local models for cloud features. Create the file with '
        f'CEREBRAS/GROQ/CEREBRAS_2/GROQ_2 keys before building.'
    )

# ── Whiteboard offline bundle (Shift+F8 whiteboard) ──────────────────────────
# whiteboard.py loads whiteboard_assets/dist/index.html via file://.
# Path resolution at runtime: when frozen, looks under sys._MEIPASS — so the
# tree must land at <_MEIPASS>/whiteboard_assets/dist/...
_wb_dist = ROOT / 'whiteboard_assets' / 'dist'
if _wb_dist.exists():
    for _p in _wb_dist.rglob('*'):
        if _p.is_file():
            _rel = _p.relative_to(ROOT).parent  # e.g. whiteboard_assets/dist/fonts/Cascadia
            datas += [(str(_p), str(_rel))]
else:
    print(f'!! whiteboard_assets/dist missing — build it first: '
          f'cd whiteboard_assets && npm install && node build.mjs')

# ── Audio editor bundle (Shift+F10, Tenacity portable, relabeled) ────────────
# audio_editor.py spawns audio_editor_assets/tenacity/tenacity.exe as a
# sibling process. PyInstaller drops every file under _MEIPASS/ at the same
# relative path. In onedir mode (what we ship) _MEIPASS is <dist>/_internal,
# so the runnable layout becomes
#   _internal/audio_editor_assets/tenacity/tenacity.exe + DLLs + Plug-Ins/ + ...
# which is exactly what the launcher expects.
_ae_dir = ROOT / 'audio_editor_assets' / 'tenacity'
if _ae_dir.exists():
    for _p in _ae_dir.rglob('*'):
        if _p.is_file():
            _rel = _p.relative_to(ROOT).parent
            datas += [(str(_p), str(_rel))]
else:
    print(f'!! audio_editor_assets/tenacity missing — see audio_editor.py'
          f' header for bundling steps')

# ── WebView2 Fixed Version runtime (Shift+F8 whiteboard, dependency-free) ────
# Ship Microsoft's Fixed Version WebView2 Runtime inside the dist so the
# whiteboard opens on PCs that don't have WebView2 Runtime pre-installed
# (common on Windows 10 pre-1903 and clean corporate images). Set as an
# environment variable in whiteboard.py before webview.start(). Without
# this bundle, whiteboard falls back to the system-installed WebView2
# runtime and only works if the user has it.
# Adds ~180 MB to the compressed zip and ~650 MB to the extracted dist.
_wv2_dir = ROOT / 'webview2_runtime'
if _wv2_dir.exists() and (_wv2_dir / 'msedgewebview2.exe').is_file():
    for _p in _wv2_dir.rglob('*'):
        if _p.is_file():
            _rel = _p.relative_to(ROOT).parent
            datas += [(str(_p), str(_rel))]
    print(f'++ webview2_runtime bundled ({sum(f.stat().st_size for f in _wv2_dir.rglob("*") if f.is_file()) / (1024*1024):.0f} MB)')
else:
    print(f'!! webview2_runtime missing at {_wv2_dir} — dist will fall back to '
          f'system-installed WebView2 (whiteboard breaks on PCs without it). '
          f'Extract Microsoft.WebView2.FixedVersionRuntime.*.x64.cab there.')


# ── Tesseract language packs (Scan-doc: auto-orient + Extract text) ─────────
# main.py's scan-preview code points TESSDATA_PREFIX at <app>/tessdata when
# present, so Tesseract uses our bundled ara/eng/osd traineddata even if
# the user has only English installed system-wide. Without this, Arabic
# OCR + Tesseract OSD auto-orient silently fall back to whatever the
# system tesseract.exe ships (usually English-only).
#   ara.traineddata  ~16 MB  Arabic OCR
#   eng.traineddata  ~22 MB  English OCR
#   osd.traineddata  ~10 MB  Orientation + Script Detection (--psm 0)
# Total ~37 MB — acceptable for a plug-and-play dist that "just works"
# for Arabic + auto-orient without any user-side install.
_tess_dir = ROOT / 'tessdata'
if _tess_dir.exists():
    for _p in _tess_dir.iterdir():
        if _p.is_file() and _p.suffix in ('.traineddata',):
            datas += [(str(_p), 'tessdata')]
else:
    print(f'!! tessdata/ missing — auto-orient + Arabic OCR will fall '
          f'back to system Tesseract language packs.')


# ── Binaries (native shared libs) ─────────────────────────────────────────────

binaries = []
binaries += collect_dynamic_libs('ctranslate2')
binaries += collect_dynamic_libs('onnxruntime')
binaries += _av_bins       # av.libs FFmpeg DLLs + av .pyd extensions
binaries += _pynput_bins   # pynput Windows backend
# DIAGNOSTIC: dropped torch/torchaudio/pyannote/soundfile binaries (see notes above).
# binaries += _torch_bins
# binaries += _taudio_bins
# binaries += _pyanno_bins
# binaries += _sf_bins
binaries += _ytdl_bins     # yt-dlp Cython speedups if present
binaries += _ioff_bins     # imageio-ffmpeg's ffmpeg.exe

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
    'tkinterdnd2',

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
    # RTL text rendering for MiniNotepad — Tk's Text widget doesn't
    # apply Unicode BiDi, so we preprocess Arabic/Hebrew via these two
    # libs before insertion. Missing → Arabic in MiniNotepad renders
    # visually reversed (word-order flipped, characters scrambled).
    'bidi',
    'bidi.algorithm',
    'arabic_reshaper',
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

    # Shift+F9 — file/URL transcription pipeline
    'transcribe',
    'transcribe.engine',
    'transcribe.exporters',
    'transcribe.youtube',
    'transcribe_ui',
    # DIAGNOSTIC: soundfile / pyannote.audio / torch / torchaudio temporarily
    # NOT bundled — see diagnostic block at top of this spec. Transcribe will
    # fall back to no-diarization mode (faster_whisper handles the core
    # transcription on its own).
    # 'soundfile',
    'yt_dlp',
    'fpdf',           # fpdf2 wheel installs as `fpdf`
    'docx',           # python-docx
    # 'pyannote.audio',
    # 'torch',
    # 'torchaudio',
    'imageio_ffmpeg',

    # Quick Notes
    'quicknotes',

    # Whiteboard — offline Whiteboard via pywebview (Shift+F8)
    'whiteboard',
    'win_geometry',     # shared: center-on-work-area for Notes + Whiteboard
    'hotkey_validator', # central conflict checks for every hotkey UI
    'brand_icon',       # render + save brand .ico (used at build + runtime)
    'webview',
    'webview.platforms.edgechromium',
    'clr_loader',
    'pythonnet',
    'bottle',
    'proxy_tools',
    'typing_extensions',
    # Stdlib modules lazy-imported by webview/wsgiref/bottle that PyInstaller's
    # static analyzer misses. Without these, the whiteboard subprocess crashes
    # on `from wsgiref.simple_server import make_server` → ModuleNotFoundError.
    'http',
    'http.server',
    'http.client',
    'wsgiref',
    'wsgiref.simple_server',
    'wsgiref.util',
    'wsgiref.headers',
    'wsgiref.handlers',
    'wsgiref.validate',
    'socketserver',
    'xml',
    'xml.etree',
    'xml.etree.ElementTree',
]

# Collect all submodules of heavy packages so nothing gets missed
hiddenimports += collect_submodules('faster_whisper')
hiddenimports += collect_submodules('huggingface_hub')
# Belt-and-braces: also collect data + submodules via collect_all so the
# package is FORCED into the bundle. PyInstaller's static analyzer has been
# dropping huggingface_hub when nothing at module level imports it (lazy
# import inside _ensure_model_downloaded was being missed). transcribe/
# engine.py also has a top-level anchor import; either should be enough,
# both is safer.
_hf_datas, _hf_bins, _hf_hidden = collect_all('huggingface_hub')
datas += _hf_datas
binaries += _hf_bins
hiddenimports += _hf_hidden
hiddenimports += _av_hidden   # av submodules from collect_all
hiddenimports += collect_submodules('ctranslate2')
hiddenimports += [m for m in collect_submodules('onnxruntime') if 'quantization' not in m and 'onnx' not in m]
hiddenimports += collect_submodules('groq')
hiddenimports += collect_submodules('cerebras')
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('scipy.signal')
hiddenimports += _pynput_hidden   # pynput submodules from collect_all
# DIAGNOSTIC: dropped torch/torchaudio/pyannote/soundfile hidden imports.
# hiddenimports += _torch_hidden
# hiddenimports += _taudio_hidden
# hiddenimports += _pyanno_hidden
# hiddenimports += _sf_hidden
hiddenimports += _ytdl_hidden     # yt_dlp submodules
hiddenimports += _ioff_hidden     # imageio_ffmpeg submodules

# pywebview's edgechromium backend pulls .NET (pythonnet/clr_loader)
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('clr_loader')
_webview_datas, _webview_bins, _webview_hidden = collect_all('webview')
datas += _webview_datas
binaries += _webview_bins
hiddenimports += _webview_hidden
_clr_datas, _clr_bins, _clr_hidden = collect_all('clr_loader')
datas += _clr_datas
binaries += _clr_bins
hiddenimports += _clr_hidden

# NOTE: tried adding `collect_all('pythonnet')` here but it caused a native
# heap corruption at runtime (STATUS_STACK_BUFFER_OVERRUN 0xc0000409 at
# fault offset 0x1c325 in the PyInstaller bootloader). Reverting. pythonnet
# is still bundled transitively via pywebview's edgechromium backend.

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
    # KEEP unittest available — scipy.signal.resample lazily imports it for
    # input validation, and excluding it makes the audio resampler fail with
    # "No module named 'unittest'" on every audio chunk, eventually crashing
    # the audio callback thread with ACCESS_VIOLATION 0xc0000005.
    # 'unittest',     # DO NOT EXCLUDE
    # 'test',         # DO NOT EXCLUDE — used by stdlib test helpers some libs hit
    # 'tkinter.test', # DO NOT EXCLUDE
    'sklearn.datasets.tests',
    'sklearn.tests',
    # DIAGNOSTIC FORCE-EXCLUDE: torch + pyannote + torchaudio + soundfile.
    # PyInstaller was still pulling these in transitively despite my drops
    # earlier in this spec, producing the 0xc0000409 heap corruption crash.
    # Force them out completely. Shift+F9 transcribe-with-diarization falls
    # back gracefully (try/except in transcribe/engine.py:578).
    'torch',
    'torchaudio',
    'pyannote',
    'pyannote.audio',
    'pyannote.core',
    'pyannote.database',
    'pyannote.metrics',
    'pyannote.pipeline',
    'soundfile',
    'lightning',
    'pytorch_lightning',
    'tensorboard',
    # KEEP stdlib modules — they're lazy-imported by libraries at runtime.
    # http.server: wsgiref.simple_server (used by pywebview's bottle web
    #              server) imports it. Excluding crashes whiteboard.
    # 'xmlrpc',
    # 'email.mime',
    # 'http.server',
    # 'urllib.robotparser',
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
    # Brand .ico embedded as the exe's native icon resource. Windows uses
    # this for the taskbar, Alt+Tab, jump list, file properties, shortcut
    # creation — every default-icon fallback path in the shell.
    icon=str(_BRAND_ICO_FOR_EXE),
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
