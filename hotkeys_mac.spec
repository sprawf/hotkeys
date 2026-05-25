# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Hotkeys macOS .app build.
# Run via GitHub Actions (build_mac.yml) — not meant to be run locally on Windows.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ── Data files to bundle ──────────────────────────────────────────────────────
datas = [
    ('_bundled_keys.py',    '.'),          # API keys baked in at CI build time
    ('prompts.json',        '.'),          # Default prompt library
    ('assets',              'assets'),     # silero_vad.onnx + any future assets
    ('models/base',         'models/base'),# Whisper base model (~150 MB)
]
datas += collect_data_files('customtkinter')    # CTk themes & images
datas += collect_data_files('faster_whisper')   # VAD assets from the package

# ── Hidden imports ────────────────────────────────────────────────────────────
hiddenimports = [
    '_bundled_keys',
    # pystray macOS backend
    'pystray._darwin',
    'pystray.backend.darwin',
    # PIL / tkinter bridge
    'PIL._tkinter_finder',
    # Audio stack
    'sounddevice',
    'soundfile',
    'noisereduce',
    'scipy.signal',
    'scipy.signal.windows',
    # ONNX runtime (Silero VAD)
    'onnxruntime',
    'onnxruntime.backend',
    # keyboard / mouse global hooks
    'keyboard',
    'mouse',
    # clipboard
    'pyperclip',
    'psutil',
    # screen / GIF recording
    'av',
    'mss',
    'pyspellchecker',
]
hiddenimports += collect_submodules('groq')
hiddenimports += collect_submodules('cerebras_cloud_sdk')
hiddenimports += collect_submodules('httpx')

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim unneeded heavy packages to keep bundle smaller
    excludes=['torch', 'tensorflow', 'matplotlib', 'jupyter', 'notebook',
              'IPython', 'pandas', 'sklearn', 'cv2'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Hotkeys',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # UPX can break native dylibs on Mac
    console=False,
    codesign_identity=None,
    entitlements_file=None,
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

app = BUNDLE(
    coll,
    name='Hotkeys.app',
    icon='icon.icns',
    bundle_identifier='com.sprawf.hotkeys',
    info_plist={
        'CFBundleName':                   'Hotkeys',
        'CFBundleDisplayName':            'Hotkeys',
        'CFBundleShortVersionString':     '3.0.0',
        'CFBundleVersion':                '3.0.0',
        'NSHighResolutionCapable':        True,
        'NSMicrophoneUsageDescription':   'Hotkeys uses the microphone for voice-to-text transcription.',
        'NSAppleEventsUsageDescription':  'Hotkeys uses Apple Events to type text into other apps.',
        'NSScreenCaptureUsageDescription': 'Hotkeys uses screen capture to record video and GIFs.',
        # LSUIElement = True hides the dock icon (menu-bar / tray app)
        'LSUIElement':                    True,
        # Note: screen recording entitlement (com.apple.security.screen-recording)
        # must also be set in the entitlements .plist passed to codesign.
    },
)
