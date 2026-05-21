"""
Mac smoke test — runs headless on GitHub Actions macOS runner.

Tests everything that doesn't need a display or real keyboard:
  - All critical imports (the class of bug that broke the Windows dist)
  - Whisper model load (the actual transcriber pipeline)
  - Audio device enumeration
  - Config / storage helpers
  - AI provider import chain
  - Clipboard helpers (import only — no display needed)

Exit 0 = pass, Exit 1 = fail (CI marks the job red).
"""
import sys
import os
import time
import logging
import traceback

# ── Logging to file + stdout ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('smoke_test.log', encoding='utf-8'),
    ]
)
log = logging.getLogger('smoke')

PASS = []
FAIL = []


def check(name: str, fn):
    try:
        result = fn()
        msg = f'  ✓  {name}'
        if result:
            msg += f'  ({result})'
        log.info(msg)
        PASS.append(name)
    except Exception as e:
        log.error(f'  ✗  {name}')
        log.error(traceback.format_exc())
        FAIL.append(name)


# ── 1. Core imports ───────────────────────────────────────────────────────────
log.info('\n── Imports ──────────────────────────────────────────────────')

check('faster_whisper',       lambda: __import__('faster_whisper'))
check('faster_whisper.audio', lambda: __import__('faster_whisper.audio'))
check('faster_whisper.transcribe', lambda: __import__('faster_whisper.transcribe'))
check('faster_whisper.utils', lambda: __import__('faster_whisper.utils'))
check('ctranslate2',          lambda: __import__('ctranslate2'))
check('tokenizers',           lambda: __import__('tokenizers'))
check('huggingface_hub',      lambda: __import__('huggingface_hub'))
check('av',                   lambda: __import__('av'))
check('sounddevice',          lambda: __import__('sounddevice'))
check('numpy',                lambda: __import__('numpy'))
check('onnxruntime',          lambda: __import__('onnxruntime'))
check('noisereduce',          lambda: __import__('noisereduce'))
check('scipy',                lambda: __import__('scipy'))
check('psutil',               lambda: __import__('psutil'))
check('pyperclip',            lambda: __import__('pyperclip'))
check('PIL',                  lambda: __import__('PIL'))
check('groq',                 lambda: __import__('groq'))
check('cerebras.cloud.sdk',   lambda: __import__('cerebras.cloud.sdk'))
check('pystray',              lambda: __import__('pystray'))

# ── 2. App module imports ─────────────────────────────────────────────────────
log.info('\n── App modules ──────────────────────────────────────────────')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

check('storage',       lambda: __import__('storage'))
check('engine',        lambda: __import__('engine'))
check('core.audio',    lambda: __import__('core.audio'))
check('core.vad',      lambda: __import__('core.vad'))
check('core.transcriber', lambda: __import__('core.transcriber'))
check('core.typer',    lambda: __import__('core.typer'))
check('core.sounds',   lambda: __import__('core.sounds'))

# ── 3. Storage helpers ────────────────────────────────────────────────────────
log.info('\n── Storage ──────────────────────────────────────────────────')

def _test_appdata():
    from storage import appdata_dir, load_config, make_whisper_cfg
    d = appdata_dir()
    assert os.path.isdir(d), f'appdata_dir not created: {d}'
    cfg = load_config()
    assert isinstance(cfg, dict)
    wcfg = make_whisper_cfg(cfg)
    assert hasattr(wcfg, 'model')
    return d

check('appdata_dir + config', _test_appdata)

# ── 4. Audio device enumeration ───────────────────────────────────────────────
log.info('\n── Audio ────────────────────────────────────────────────────')

def _test_audio_devices():
    import sounddevice as sd
    devices = sd.query_devices()
    return f'{len(devices)} device(s) found'

check('sounddevice.query_devices', _test_audio_devices)

# ── 5. Whisper model load (the critical one) ──────────────────────────────────
log.info('\n── Whisper model load ───────────────────────────────────────')

def _test_whisper():
    from faster_whisper import WhisperModel
    model_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'models', 'base')
    assert os.path.isdir(model_path), f'Model not found: {model_path}'
    assert os.path.isfile(os.path.join(model_path, 'model.bin')), 'model.bin missing'
    t0 = time.perf_counter()
    model = WhisperModel(model_path, device='cpu', compute_type='int8',
                         num_workers=1, cpu_threads=2)
    elapsed = time.perf_counter() - t0
    # Quick sanity transcription on silence
    import numpy as np
    silence = np.zeros(16000, dtype=np.float32)   # 1 second of silence
    segments, info = model.transcribe(silence, language='en', beam_size=1)
    list(segments)   # consume the generator
    return f'loaded in {elapsed:.1f}s, lang_prob={info.language_probability:.2f}'

check('WhisperModel load + transcribe', _test_whisper)

# ── 6. VAD (ONNX) ─────────────────────────────────────────────────────────────
log.info('\n── VAD ──────────────────────────────────────────────────────')

def _test_vad():
    from pathlib import Path
    from core.vad import SileroVAD
    onnx = Path(__file__).parent.parent / 'assets' / 'silero_vad.onnx'
    assert onnx.exists(), f'silero_vad.onnx not found: {onnx}'
    vad = SileroVAD(onnx)
    import numpy as np
    chunk = np.zeros(512, dtype=np.float32)
    vad.process_chunk(chunk)
    return 'ok'

check('SileroVAD', _test_vad)

# ── 7b. faster_whisper internal VAD model ─────────────────────────────────────

def _test_fw_vad():
    """faster_whisper/assets/silero_vad_v6.onnx must exist — used internally
    during transcribe() regardless of whether we pass vad_filter=True."""
    import faster_whisper
    fw_assets = os.path.join(os.path.dirname(faster_whisper.__file__), 'assets')
    vad_path = os.path.join(fw_assets, 'silero_vad_v6.onnx')
    assert os.path.isfile(vad_path), f'faster_whisper internal VAD missing: {vad_path}'
    size_kb = os.path.getsize(vad_path) // 1024
    return f'{size_kb} KB'

check('faster_whisper internal silero_vad_v6.onnx', _test_fw_vad)

# ── 7. Platform-specific paths ────────────────────────────────────────────────
log.info('\n── Platform checks ──────────────────────────────────────────')

def _test_platform():
    from storage import appdata_dir
    d = appdata_dir()
    if sys.platform == 'darwin':
        expected = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Hotkeys')
        assert d == expected, f'Wrong Mac path: {d!r} != {expected!r}'
    return f'platform={sys.platform}  path={d}'

check('appdata platform path', _test_platform)

# ── Summary ───────────────────────────────────────────────────────────────────
log.info('\n' + '─' * 60)
log.info(f'PASSED: {len(PASS)}   FAILED: {len(FAIL)}')
if FAIL:
    log.error('FAILED CHECKS:')
    for name in FAIL:
        log.error(f'  ✗ {name}')
    sys.exit(1)
else:
    log.info('All checks passed ✓')
    sys.exit(0)
