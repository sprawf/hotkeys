"""
E:\\Hotkeys -- Regression Test Suite
Run with:  E:\\Hotkeys\\venv\\Scripts\\python.exe E:\\Hotkeys\\test_regression.py
"""
import sys
import os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import json
import copy
import time
import tempfile
import threading
import traceback
import tkinter as tk

# -- Ensure project root is on path -------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# -- Test harness --------------------------------------------------------------

PASS = 0
FAIL = 0
WARN = 0
_results = []


def ok(name):
    global PASS
    PASS += 1
    _results.append(('PASS', name))
    print(f'  \033[32mOK\033[0m  {name}')


def fail(name, reason=''):
    global FAIL
    FAIL += 1
    _results.append(('FAIL', name, reason))
    print(f'  \033[31mFAIL\033[0m  {name}')
    if reason:
        print(f'       → {reason}')


def warn(name, reason=''):
    global WARN
    WARN += 1
    _results.append(('WARN', name, reason))
    print(f'  \033[33mWARN\033[0m  {name}')
    if reason:
        print(f'       → {reason}')


def section(title):
    print(f'\n\033[1m{title}\033[0m')
    print('-' * 60)


def run(name, fn):
    try:
        fn()
        ok(name)
    except AssertionError as e:
        fail(name, str(e))
    except Exception as e:
        fail(name, f'{type(e).__name__}: {e}')


# ===============================================================================
# 1. IMPORTS
# ===============================================================================

section('1. Imports')

def _import(mod):
    run(f'import {mod}', lambda: __import__(mod))

for _m in ['storage', 'theme', 'engine', 'overlay', 'library', 'settings',
           'core.audio', 'core.vad', 'core.sounds', 'core.typer', 'core.transcriber']:
    _import(_m)


# ===============================================================================
# 2. STORAGE -- path helpers
# ===============================================================================

section('2. Storage -- path helpers')

import storage

def t_appdata_dir():
    p = storage.appdata_dir()
    assert os.path.isdir(p), f'Not a dir: {p}'
    assert 'Hotkeys' in p, f'Wrong appdata dir: {p}'

def t_resource_path():
    p = storage.resource_path('prompts.json')
    assert p.endswith('prompts.json'), f'Wrong suffix: {p}'

def t_config_path():
    p = storage.config_path()
    assert p.endswith('config.json'), f'Wrong suffix: {p}'
    assert 'Hotkeys' in p

def t_log_path():
    p = storage.log_path()
    assert p.endswith('app.log')

def t_history_path():
    p = storage.history_path()
    assert p.endswith('history.json')

def t_models_dir():
    p = storage.models_dir()
    assert 'models' in p.lower()

def t_assets_dir():
    p = storage.assets_dir()
    assert 'assets' in p.lower()

run('appdata_dir() -- correct path and exists',    t_appdata_dir)
run('resource_path() -- correct suffix',           t_resource_path)
run('config_path() -- correct suffix and dir',     t_config_path)
run('log_path() -- correct suffix',                t_log_path)
run('history_path() -- correct suffix',            t_history_path)
run('models_dir() -- contains "models"',           t_models_dir)
run('assets_dir() -- contains "assets"',           t_assets_dir)


# ===============================================================================
# 3. STORAGE -- config round-trip
# ===============================================================================

section('3. Storage -- config round-trip')

def t_default_config_keys():
    cfg = storage.DEFAULT_CONFIG
    for key in ('version', 'active_provider', 'autostart', 'hotkeys', 'providers', 'whisper'):
        assert key in cfg, f'Missing key: {key}'

def t_default_hotkeys():
    hk = storage.DEFAULT_CONFIG['hotkeys']
    assert 'refine'  in hk
    assert 'library' in hk
    assert 'whisper' in hk

def t_default_providers():
    p = storage.DEFAULT_CONFIG['providers']
    for k in ('local', 'groq', 'cerebras'):
        assert k in p, f'Missing provider: {k}'

def t_default_whisper_sections():
    w = storage.DEFAULT_CONFIG['whisper']
    for k in ('model', 'audio', 'vad', 'transcription', 'output', 'modes'):
        assert k in w, f'Missing whisper section: {k}'

def t_whisper_model_keys():
    m = storage.DEFAULT_CONFIG['whisper']['model']
    for k in ('gpu_model', 'cpu_model', 'device', 'compute_type'):
        assert k in m

def t_save_and_load_config():
    # Write to a temp file, patch config_path, reload
    original = copy.deepcopy(storage.DEFAULT_CONFIG)
    original['active_provider'] = 'groq'
    original['hotkeys']['refine'] = 'ctrl+shift+r'

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(original, f, indent=2)
        tmp_path = f.name

    try:
        # Manually load and merge
        with open(tmp_path, encoding='utf-8') as f:
            saved = json.load(f)
        merged = {**storage.DEFAULT_CONFIG, **saved}
        merged['hotkeys']   = {**storage.DEFAULT_CONFIG['hotkeys'],   **saved.get('hotkeys',   {})}
        merged['providers'] = {**storage.DEFAULT_CONFIG['providers'], **saved.get('providers', {})}
        merged['whisper']   = storage._deep_merge(storage.DEFAULT_CONFIG['whisper'], saved.get('whisper', {}))
        assert merged['active_provider'] == 'groq'
        assert merged['hotkeys']['refine'] == 'ctrl+shift+r'
        assert merged['hotkeys']['library'] == storage.DEFAULT_CONFIG['hotkeys']['library']  # preserved default
    finally:
        os.unlink(tmp_path)

def t_deep_merge():
    base = {'a': 1, 'b': {'c': 2, 'd': 3}}
    over = {'b': {'c': 99}, 'e': 5}
    result = storage._deep_merge(base, over)
    assert result['a'] == 1
    assert result['b']['c'] == 99
    assert result['b']['d'] == 3   # preserved
    assert result['e'] == 5
    assert base['b']['c'] == 2     # original untouched

def t_load_config_returns_dict():
    cfg = storage.load_config()
    assert isinstance(cfg, dict)
    assert 'whisper' in cfg
    assert 'hotkeys' in cfg

run('DEFAULT_CONFIG has all top-level keys',      t_default_config_keys)
run('DEFAULT_CONFIG hotkeys has all 3 actions',   t_default_hotkeys)
run('DEFAULT_CONFIG has 3 providers',             t_default_providers)
run('DEFAULT_CONFIG whisper has all 6 sections',  t_default_whisper_sections)
run('DEFAULT_CONFIG whisper.model has 4 keys',    t_whisper_model_keys)
run('save/load round-trip preserves values',      t_save_and_load_config)
run('_deep_merge -- nested merge, preserves base', t_deep_merge)
run('load_config() returns dict with whisper',    t_load_config_returns_dict)


# ===============================================================================
# 4. STORAGE -- WhisperCfg adapter (_Namespace)
# ===============================================================================

section('4. Storage -- WhisperCfg adapter')

def t_namespace_flat():
    ns = storage._Namespace({'a': 1, 'b': 'hello'})
    assert ns.a == 1
    assert ns.b == 'hello'

def t_namespace_nested():
    ns = storage._Namespace({'model': {'device': 'cpu', 'compute_type': 'int8'}})
    assert ns.model.device == 'cpu'
    assert ns.model.compute_type == 'int8'

def t_make_whisper_cfg_default():
    cfg = storage.load_config()
    wcfg = storage.make_whisper_cfg(cfg)
    assert wcfg.model.device == 'auto'
    assert wcfg.model.cpu_model == 'small'
    assert wcfg.audio.noise_reduction is True
    assert wcfg.vad.safety_silence_s == 60
    assert wcfg.transcription.beam_size == 2
    assert wcfg.output.type_text is True
    assert wcfg.modes.active_mode == 'Default'

def t_make_whisper_cfg_modes_definitions():
    cfg = storage.load_config()
    wcfg = storage.make_whisper_cfg(cfg)
    default_mode = getattr(wcfg.modes.definitions, 'Default', None)
    assert default_mode is not None, 'Default mode not found in namespace'
    assert default_mode.initial_prompt == ''
    assert default_mode.post_rules == []

def t_make_whisper_cfg_email_mode():
    cfg = storage.load_config()
    wcfg = storage.make_whisper_cfg(cfg)
    email_mode = getattr(wcfg.modes.definitions, 'Email', None)
    assert email_mode is not None
    assert 'capitalize_sentences' in email_mode.post_rules

run('_Namespace -- flat dict attribute access',                t_namespace_flat)
run('_Namespace -- nested dict attribute access',              t_namespace_nested)
run('make_whisper_cfg() -- default values correct',            t_make_whisper_cfg_default)
run('make_whisper_cfg() -- modes.definitions namespace works', t_make_whisper_cfg_modes_definitions)
run('make_whisper_cfg() -- Email mode post_rules present',     t_make_whisper_cfg_email_mode)


# ===============================================================================
# 5. STORAGE -- prompts
# ===============================================================================

section('5. Storage -- prompts')

def t_load_prompts_returns_list():
    prompts = storage.load_prompts()
    assert isinstance(prompts, list)
    assert len(prompts) > 0, 'No prompts loaded'

def t_prompts_have_required_keys():
    prompts = storage.load_prompts()
    for p in prompts:
        assert 'title'  in p, f'Missing title in prompt: {p}'
        assert 'prompt' in p, f'Missing prompt text in: {p}'
        assert 'color'  in p, f'Missing color in: {p}'

def t_prompts_colors_are_valid_hex():
    prompts = storage.load_prompts()
    for p in prompts:
        c = p['color']
        assert c.startswith('#') and len(c) == 7, f'Invalid color: {c}'

def t_prompts_upgrade_guard():
    # Simulate: user file has fewer prompts than bundled
    # The upgrade guard should NOT remove user prompts, only add missing
    prompts = storage.load_prompts()
    assert len(prompts) >= 15, f'Expected ≥15 prompts, got {len(prompts)}'

def t_save_and_reload_prompts():
    test_prompts = [
        {'title': 'Test', 'prompt': 'Test prompt text', 'color': '#FFF9C4'},
    ]
    # Save to a temp path and load back
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(test_prompts, f)
        tmp = f.name
    try:
        with open(tmp, encoding='utf-8') as f:
            loaded = json.load(f)
        assert loaded[0]['title'] == 'Test'
    finally:
        os.unlink(tmp)

run('load_prompts() returns non-empty list',          t_load_prompts_returns_list)
run('all prompts have title/prompt/color keys',       t_prompts_have_required_keys)
run('all prompt colors are valid #RRGGBB hex',        t_prompts_colors_are_valid_hex)
run('upgrade guard -- ≥15 prompts present',            t_prompts_upgrade_guard)
run('save/load prompts round-trip',                   t_save_and_reload_prompts)


# ===============================================================================
# 6. STORAGE -- history
# ===============================================================================

section('6. Storage -- history')

def t_load_history_returns_list():
    h = storage.load_history()
    assert isinstance(h, list)

def t_save_history_truncates():
    entries = [{'text': f'entry {i}'} for i in range(300)]
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        tmp = f.name
    try:
        # Patch history_path temporarily
        orig = storage.history_path
        storage.history_path = lambda: tmp
        storage.save_history(entries, max_entries=200)
        storage.history_path = orig
        with open(tmp, encoding='utf-8') as f:
            saved = json.load(f)
        assert len(saved) == 200, f'Expected 200, got {len(saved)}'
        assert saved[0]['text'] == 'entry 100'  # first 100 dropped
    finally:
        os.unlink(tmp)
        storage.history_path = orig

run('load_history() returns list (empty ok)',     t_load_history_returns_list)
run('save_history() caps at max_entries',         t_save_history_truncates)


# ===============================================================================
# 7. THEME
# ===============================================================================

section('7. Theme')

import theme

def t_palette_colors():
    for name in ('BG', 'SURFACE', 'SURF2', 'SURF3', 'ACCENT', 'ACCENTL',
                 'TEXT_P', 'TEXT_S', 'OK', 'WARN', 'ERR', 'INFO'):
        val = getattr(theme, name)
        assert val.startswith('#') and len(val) in (4, 7), f'{name}={val} is not a hex color'

def t_card_colors():
    assert len(theme.CARD_COLORS) == 8
    for c in theme.CARD_COLORS:
        assert c.startswith('#') and len(c) == 7

def t_font_tuples():
    for name in ('FONT_XS', 'FONT_SM', 'FONT_MD', 'FONT_LG', 'FONT_XL',
                 'FONT_SM_BOLD', 'FONT_MD_BOLD'):
        val = getattr(theme, name)
        assert isinstance(val, tuple) and len(val) >= 2, f'{name} is not a font tuple'

def t_geometry_constants():
    assert theme.RADIUS    > 0
    assert theme.RADIUS_SM > 0
    assert theme.PAD       > 0

run('all palette colors are valid hex',        t_palette_colors)
run('CARD_COLORS has 8 valid hex entries',     t_card_colors)
run('font constants are tuples of ≥2 items',   t_font_tuples)
run('geometry constants are positive ints',    t_geometry_constants)


# ===============================================================================
# 8. ENGINE -- providers
# ===============================================================================

section('8. Engine -- providers')

import engine

def t_provider_keys():
    assert set(engine.PROVIDER_KEYS) == {'local', 'groq', 'cerebras'}

def t_provider_labels_complete():
    for k in engine.PROVIDER_KEYS:
        assert k in engine.PROVIDER_LABELS

def t_groq_models_list():
    assert len(engine.GROQ_MODELS) >= 1
    assert all(isinstance(m, str) for m in engine.GROQ_MODELS)

def t_cerebras_models_list():
    assert len(engine.CEREBRAS_MODELS) >= 1

def t_build_provider_no_keys():
    # No API keys → should return LocalProvider
    cfg = copy.deepcopy(storage.DEFAULT_CONFIG)
    cfg['active_provider'] = 'cerebras'
    cfg['providers']['cerebras']['api_key'] = ''
    cfg['providers']['groq']['api_key'] = ''
    p = engine.build_provider(cfg)
    assert isinstance(p, engine.LocalProvider), f'Expected LocalProvider, got {type(p).__name__}'

def t_build_provider_with_key():
    cfg = copy.deepcopy(storage.DEFAULT_CONFIG)
    cfg['active_provider'] = 'cerebras'
    cfg['providers']['cerebras']['api_key'] = 'fake-key-123'
    cfg['providers']['groq']['api_key'] = ''
    p = engine.build_provider(cfg)
    assert isinstance(p, engine.CerebrasProvider), f'Expected CerebrasProvider, got {type(p).__name__}'
    assert p.ready is True

def t_build_provider_both_keys_fallback():
    cfg = copy.deepcopy(storage.DEFAULT_CONFIG)
    cfg['active_provider'] = 'cerebras'
    cfg['providers']['cerebras']['api_key'] = 'key-a'
    cfg['providers']['groq']['api_key'] = 'key-b'
    p = engine.build_provider(cfg)
    assert isinstance(p, engine.FallbackProvider), f'Expected FallbackProvider, got {type(p).__name__}'

def t_build_provider_groq():
    cfg = copy.deepcopy(storage.DEFAULT_CONFIG)
    cfg['active_provider'] = 'groq'
    cfg['providers']['groq']['api_key'] = 'key-g'
    cfg['providers']['cerebras']['api_key'] = ''
    p = engine.build_provider(cfg)
    assert isinstance(p, engine.GroqProvider)
    assert p.ready is True

def t_build_provider_local_explicit():
    cfg = copy.deepcopy(storage.DEFAULT_CONFIG)
    cfg['active_provider'] = 'local'
    p = engine.build_provider(cfg)
    assert isinstance(p, engine.LocalProvider)
    assert p.ready is False   # not loaded yet

def t_local_provider_ready_false():
    p = engine.LocalProvider()
    assert p.ready is False
    assert p.name == 'Qwen 2.5 1.5B (Local)'

def t_groq_provider_no_key_not_ready():
    p = engine.GroqProvider(api_key='')
    assert p.ready is False

def t_cerebras_provider_with_key_ready():
    p = engine.CerebrasProvider(api_key='test-key')
    assert p.ready is True
    assert 'Cerebras' in p.name

def t_fallback_provider_delegates_ready():
    primary  = engine.CerebrasProvider(api_key='key1')
    fallback = engine.GroqProvider(api_key='key2')
    fp = engine.FallbackProvider(primary, fallback)
    assert fp.ready is True
    assert fp.name == primary.name

def t_bundled_keys_import():
    # _bundled_keys.py should exist and return strings
    assert isinstance(engine._BUNDLED, dict)
    assert 'cerebras' in engine._BUNDLED
    assert 'groq' in engine._BUNDLED

run('PROVIDER_KEYS has exactly 3 entries',                    t_provider_keys)
run('PROVIDER_LABELS covers all 3 providers',                 t_provider_labels_complete)
run('GROQ_MODELS is a non-empty list of strings',             t_groq_models_list)
run('CEREBRAS_MODELS is a non-empty list',                    t_cerebras_models_list)
run('build_provider -- no keys → LocalProvider',               t_build_provider_no_keys)
run('build_provider -- cerebras key → CerebrasProvider.ready', t_build_provider_with_key)
run('build_provider -- both keys → FallbackProvider',          t_build_provider_both_keys_fallback)
run('build_provider -- groq key → GroqProvider',               t_build_provider_groq)
run('build_provider -- local explicit → LocalProvider',        t_build_provider_local_explicit)
run('LocalProvider.ready is False before load()',             t_local_provider_ready_false)
run('GroqProvider(key="") .ready is False',                   t_groq_provider_no_key_not_ready)
run('CerebrasProvider(key) .ready is True',                   t_cerebras_provider_with_key_ready)
run('FallbackProvider.ready mirrors primary',                 t_fallback_provider_delegates_ready)
run('_bundled_keys imported and is dict',                     t_bundled_keys_import)


# ===============================================================================
# 9. OVERLAY -- window construction and state methods
# ===============================================================================

section('9. Overlay -- pill window')

import overlay as ov

_root = tk.Tk()
_root.withdraw()

def t_overlay_slot0():
    o = ov.OverlayWindow(_root, slot=0)
    assert o._slot == 0
    assert o._win is None

def t_overlay_slot1():
    o = ov.OverlayWindow(_root, slot=1)
    assert o._slot == 1

def t_overlay_show_and_close():
    o = ov.OverlayWindow(_root, slot=0)
    o.show()
    _root.update()
    assert o._win is not None, 'Pill window not created'
    assert o._canvas is not None
    assert o._tick is True
    o._close()
    assert o._win is None

def t_overlay_show_done():
    o = ov.OverlayWindow(_root, slot=0)
    o.show()
    _root.update()
    o.show_done(1.23)
    _root.update()
    # After 750ms auto-close -- we won't wait, just check state set correctly
    assert o._tick is False

def t_overlay_show_error():
    o = ov.OverlayWindow(_root, slot=0)
    o.show_error('Something went wrong')
    _root.update()
    assert o._win is not None
    o._close()

def t_overlay_no_selection():
    o = ov.OverlayWindow(_root, slot=0)
    o.show_no_selection()
    _root.update()
    assert o._win is not None
    o._close()

def t_overlay_loading_model():
    o = ov.OverlayWindow(_root, slot=0)
    o.show_loading_model()
    _root.update()
    assert o._win is not None
    o._close()

def t_overlay_whisper_recording():
    o = ov.OverlayWindow(_root, slot=1)
    o.show_recording()
    _root.update()
    assert o._win is not None
    assert o._tick is True
    o._close()

def t_overlay_whisper_transcribing():
    o = ov.OverlayWindow(_root, slot=1)
    o.show_recording()
    _root.update()
    o.show_transcribing()
    _root.update()
    assert o._win is not None   # pill still open, text changed
    assert o._tick is False
    o._close()

def t_overlay_whisper_done():
    o = ov.OverlayWindow(_root, slot=1)
    o.show_recording()
    _root.update()
    o.show_whisper_done(2.5)
    _root.update()
    assert o._tick is False

def t_overlay_whisper_error():
    o = ov.OverlayWindow(_root, slot=1)
    o.show_whisper_error('Transcription failed')
    _root.update()
    assert o._win is not None
    o._close()

def t_overlay_whisper_cancelled():
    o = ov.OverlayWindow(_root, slot=1)
    o.show_recording()
    _root.update()
    o.show_whisper_cancelled()
    _root.update()
    assert o._win is None   # cancelled closes immediately

def t_overlay_slot_y_offset():
    # slot=1 should position 50px lower than slot=0
    o0 = ov.OverlayWindow(_root, slot=0)
    o1 = ov.OverlayWindow(_root, slot=1)
    assert o1._slot * ov._SLOT_OFFSET == 50
    assert o0._slot * ov._SLOT_OFFSET == 0

def t_overlay_double_close_safe():
    o = ov.OverlayWindow(_root, slot=0)
    o._close()  # close when never opened
    o._close()  # close again -- should not raise

def t_overlay_error_msg_truncated():
    o = ov.OverlayWindow(_root, slot=0)
    long_msg = 'x' * 100
    o.show_error(long_msg)
    _root.update()
    # Canvas text should have '…' and be ≤ 48+2 chars shown
    text = _root.nametowidget(o._canvas).itemcget(o._main_id, 'text')
    assert '…' in text or len(long_msg) <= 48
    o._close()

run('OverlayWindow slot=0 initialises',                      t_overlay_slot0)
run('OverlayWindow slot=1 initialises',                      t_overlay_slot1)
run('show() creates pill, _close() destroys it',             t_overlay_show_and_close)
run('show_done() stops timer tick',                          t_overlay_show_done)
run('show_error() builds pill',                              t_overlay_show_error)
run('show_no_selection() builds pill',                       t_overlay_no_selection)
run('show_loading_model() builds pill',                      t_overlay_loading_model)
run('show_recording() starts whisper pill + tick',           t_overlay_whisper_recording)
run('show_transcribing() updates pill, stops tick',          t_overlay_whisper_transcribing)
run('show_whisper_done() stops tick',                        t_overlay_whisper_done)
run('show_whisper_error() builds error pill',                t_overlay_whisper_error)
run('show_whisper_cancelled() closes pill immediately',      t_overlay_whisper_cancelled)
run('slot Y offset: slot*50px',                              t_overlay_slot_y_offset)
run('double _close() is safe (no exception)',                t_overlay_double_close_safe)
run('long error message is truncated with …',               t_overlay_error_msg_truncated)


# ===============================================================================
# 10. CORE -- transcriber helpers (no model load)
# ===============================================================================

section('10. Core -- transcriber helpers')

from core import transcriber as tr
import numpy as np

def t_denoise_passthrough_short():
    audio = np.random.randn(100).astype(np.float32)
    out = tr._denoise(audio, enabled=True)
    # Too short to denoise -- should return unchanged
    assert len(out) == len(audio)

def t_denoise_disabled():
    audio = np.random.randn(32000).astype(np.float32)
    out = tr._denoise(audio, enabled=False)
    np.testing.assert_array_equal(out, audio)

def t_split_audio_short():
    # < 28s → single chunk
    audio = np.zeros(int(tr._SR * 10), dtype=np.float32)
    chunks = tr._split_audio(audio)
    assert len(chunks) == 1
    assert len(chunks[0]) == len(audio)

def t_split_audio_long():
    # 60s → multiple chunks with overlap
    audio = np.zeros(int(tr._SR * 60), dtype=np.float32)
    chunks = tr._split_audio(audio)
    assert len(chunks) > 1

def t_stitch_texts_empty():
    assert tr._stitch_texts([]) == ''

def t_stitch_texts_single():
    assert tr._stitch_texts(['  hello world  ']) == 'hello world'

def t_stitch_texts_overlap():
    # "hello world" + "world how are you" → "hello world how are you"
    result = tr._stitch_texts(['hello world', 'world how are you'])
    assert result == 'hello world how are you', f'Got: {result!r}'

def t_stitch_texts_no_overlap():
    result = tr._stitch_texts(['hello world', 'goodbye moon'])
    assert 'hello world' in result
    assert 'goodbye moon' in result

def t_build_prompt_empty():
    class FakeCfg:
        class transcription:
            custom_vocabulary = ''
    p = tr._build_prompt(FakeCfg(), '')
    assert p == ''

def t_build_prompt_with_mode():
    class FakeCfg:
        class transcription:
            custom_vocabulary = 'Python\nDjango'
    p = tr._build_prompt(FakeCfg(), 'Professional language.')
    assert 'Professional language.' in p
    assert 'Python' in p
    assert 'Django' in p

def t_apply_post_rules_capitalize():
    # Create a minimal Transcriber to call _apply_post_rules
    cfg = storage.make_whisper_cfg(storage.load_config())
    t = tr.Transcriber.__new__(tr.Transcriber)
    result = t._apply_post_rules('hello world. how are you', ['capitalize_sentences'])
    assert result[0].isupper(), f'First char not capitalized: {result!r}'

def t_apply_post_rules_fix_punctuation():
    cfg = storage.make_whisper_cfg(storage.load_config())
    t = tr.Transcriber.__new__(tr.Transcriber)
    result = t._apply_post_rules('hello world', ['fix_punctuation'])
    assert result.endswith('.'), f'No period added: {result!r}'

def t_apply_post_rules_already_punctuated():
    t = tr.Transcriber.__new__(tr.Transcriber)
    result = t._apply_post_rules('hello world!', ['fix_punctuation'])
    assert result.endswith('!'), f'Punctuation changed: {result!r}'

run('_denoise -- too short → passthrough',                t_denoise_passthrough_short)
run('_denoise -- disabled → exact passthrough',           t_denoise_disabled)
run('_split_audio -- short clip → 1 chunk',               t_split_audio_short)
run('_split_audio -- 60s → multiple chunks',              t_split_audio_long)
run('_stitch_texts -- empty list → ""',                   t_stitch_texts_empty)
run('_stitch_texts -- single → stripped',                 t_stitch_texts_single)
run('_stitch_texts -- overlapping words deduped',         t_stitch_texts_overlap)
run('_stitch_texts -- no overlap → concatenated',         t_stitch_texts_no_overlap)
run('_build_prompt -- empty → ""',                        t_build_prompt_empty)
run('_build_prompt -- mode + vocabulary → merged',        t_build_prompt_with_mode)
run('_apply_post_rules -- capitalize_sentences',          t_apply_post_rules_capitalize)
run('_apply_post_rules -- fix_punctuation adds period',   t_apply_post_rules_fix_punctuation)
run('_apply_post_rules -- existing punct preserved',      t_apply_post_rules_already_punctuated)


# ===============================================================================
# 11. CORE -- VAD
# ===============================================================================

section('11. Core -- Silero VAD')

from pathlib import Path
from core.vad import SileroVAD, CHUNK_SAMPLES, SAMPLE_RATE

_onnx_path = Path(storage.assets_dir()) / 'silero_vad.onnx'

def t_onnx_file_exists():
    assert _onnx_path.exists(), f'silero_vad.onnx not found at {_onnx_path}'

def t_vad_init():
    if not _onnx_path.exists():
        raise AssertionError('onnx missing -- skipped')
    vad = SileroVAD(_onnx_path, speech_threshold=0.5, safety_silence_s=60.0)
    assert vad._threshold == 0.5
    assert vad._on_safety_stop is None

def t_vad_chunk_constants():
    assert CHUNK_SAMPLES == 512
    assert SAMPLE_RATE == 16000

def t_vad_reset():
    if not _onnx_path.exists():
        raise AssertionError('onnx missing -- skipped')
    vad = SileroVAD(_onnx_path)
    vad._speech_detected = True
    vad._silence_count = 99
    vad.reset()
    assert vad._speech_detected is False
    assert vad._silence_count == 0

def t_vad_process_silence():
    if not _onnx_path.exists():
        raise AssertionError('onnx missing -- skipped')
    vad = SileroVAD(_onnx_path, speech_threshold=0.5, safety_silence_s=1.0)
    chunk = np.zeros(512, dtype=np.float32)   # pure silence
    for _ in range(5):
        vad.process_chunk(chunk)
    # No crash; speech_detected still False for silence
    assert vad._speech_detected is False

def t_vad_wrong_chunk_size_ignored():
    if not _onnx_path.exists():
        raise AssertionError('onnx missing -- skipped')
    vad = SileroVAD(_onnx_path)
    bad_chunk = np.zeros(256, dtype=np.float32)
    vad.process_chunk(bad_chunk)   # should silently ignore, not raise

def t_vad_safety_stop_callback():
    if not _onnx_path.exists():
        raise AssertionError('onnx missing -- skipped')
    triggered = []
    vad = SileroVAD(_onnx_path, speech_threshold=0.01, safety_silence_s=0.032)
    vad.set_safety_stop_callback(lambda: triggered.append(1))
    # Simulate speech then silence to trigger auto-stop
    # First mark speech as detected
    vad._speech_detected = True
    vad._silence_chunks_limit = 1   # trigger after 1 silent chunk
    chunk = np.zeros(512, dtype=np.float32)
    vad.process_chunk(chunk)
    assert len(triggered) == 1, 'Safety stop callback not triggered'

run('silero_vad.onnx exists at assets_dir()',          t_onnx_file_exists)
run('SileroVAD init with correct threshold',           t_vad_init)
run('CHUNK_SAMPLES=512, SAMPLE_RATE=16000',            t_vad_chunk_constants)
run('vad.reset() clears state',                        t_vad_reset)
run('vad processes silence chunks without error',      t_vad_process_silence)
run('wrong chunk size silently ignored',               t_vad_wrong_chunk_size_ignored)
run('safety_stop callback fires after silence limit',  t_vad_safety_stop_callback)


# ===============================================================================
# 12. CORE -- AudioCapture (init only, no real mic)
# ===============================================================================

section('12. Core -- AudioCapture (init)')

from core.audio import AudioCapture, SAMPLE_RATE as AUDIO_SR, BLOCKSIZE

cfg_ns = storage.make_whisper_cfg(storage.load_config())

def t_audio_constants():
    assert AUDIO_SR == 16000
    assert BLOCKSIZE == 512

def t_audio_init():
    ac = AudioCapture(
        on_chunk=lambda c: None,
        on_utterance_ready=lambda a: None,
        cfg=cfg_ns,
    )
    assert ac._recording is False
    assert ac._stream is None
    assert ac.db == -60.0

def t_audio_cancel_when_idle():
    ac = AudioCapture(
        on_chunk=lambda c: None,
        on_utterance_ready=lambda a: None,
        cfg=cfg_ns,
    )
    ac.cancel_recording()   # should not raise when not recording
    assert ac._buffer == []

def t_audio_stop_when_idle():
    ac = AudioCapture(
        on_chunk=lambda c: None,
        on_utterance_ready=lambda a: None,
        cfg=cfg_ns,
    )
    ac.stop()   # should not raise when stream is None

run('AudioCapture constants SAMPLE_RATE=16000, BLOCKSIZE=512',    t_audio_constants)
run('AudioCapture init -- stream None, recording False, db=-60',   t_audio_init)
run('cancel_recording() when idle -- no exception',                 t_audio_cancel_when_idle)
run('stop() when stream is None -- no exception',                   t_audio_stop_when_idle)


# ===============================================================================
# 13. CORE -- sounds (generate waveforms without playing)
# ===============================================================================

section('13. Core -- sounds')

from core.sounds import _bell, SAMPLE_RATE as SND_SR
import numpy as np

def t_bell_waveform_shape():
    wave = _bell(880.0, 0.22)
    expected_len = int(SND_SR * 0.22)
    assert len(wave) == expected_len, f'Expected {expected_len}, got {len(wave)}'

def t_bell_waveform_dtype():
    wave = _bell(880.0, 0.22)
    assert wave.dtype == np.float32

def t_bell_amplitude_bounded():
    wave = _bell(880.0, 0.5, amplitude=0.5)
    assert np.max(np.abs(wave)) <= 1.0, 'Wave exceeds ±1.0'

def t_bell_silence_decay():
    # Exponential decay: end should be near zero
    wave = _bell(440.0, 1.0, amplitude=0.5)
    tail = wave[-100:]
    assert np.max(np.abs(tail)) < 0.01, 'Wave tail not near zero'

run('_bell() produces correct length array',   t_bell_waveform_shape)
run('_bell() dtype is float32',                t_bell_waveform_dtype)
run('_bell() amplitude stays within ±1.0',     t_bell_amplitude_bounded)
run('_bell() decays to near-zero at end',       t_bell_silence_decay)


# ===============================================================================
# 14. CORE -- typer (clipboard logic without real clipboard)
# ===============================================================================

section('14. Core -- typer')

from core.typer import (KEYBDINPUT, INPUT, _make_key_input,
                        VK_CONTROL, VK_V, KEYEVENTF_KEYUP)

def t_keybdinput_struct_size():
    ki = KEYBDINPUT()
    assert ki.wVk   == 0
    assert ki.dwFlags == 0

def t_make_key_input_returns_input():
    inp = _make_key_input(VK_CONTROL, 0, 0)
    assert isinstance(inp, INPUT)
    assert inp.ki.wVk == VK_CONTROL

def t_make_key_input_keyup():
    inp = _make_key_input(VK_V, 0, KEYEVENTF_KEYUP)
    assert inp.ki.dwFlags == KEYEVENTF_KEYUP

def t_vk_constants():
    assert VK_CONTROL == 0x11
    assert VK_V       == 0x56

run('KEYBDINPUT struct initialises to zeros',            t_keybdinput_struct_size)
run('_make_key_input returns INPUT with correct vk',     t_make_key_input_returns_input)
run('_make_key_input with KEYUP flag sets dwFlags',      t_make_key_input_keyup)
run('VK_CONTROL=0x11, VK_V=0x56',                        t_vk_constants)


# ===============================================================================
# 15. MODELS & ASSETS -- files on disk
# ===============================================================================

section('15. Models & assets on disk')

def t_onnx_present():
    p = os.path.join(storage.assets_dir(), 'silero_vad.onnx')
    assert os.path.isfile(p), f'Missing: {p}'
    size = os.path.getsize(p)
    assert size > 1_000_000, f'onnx too small ({size} bytes) -- corrupted?'

def t_model_base_exists():
    p = os.path.join(storage.models_dir(), 'base')
    assert os.path.isdir(p), f'Missing model dir: {p}'

def t_model_small_exists():
    p = os.path.join(storage.models_dir(), 'small')
    assert os.path.isdir(p), f'Missing model dir: {p}'

def t_model_large_v3_turbo_exists():
    p = os.path.join(storage.models_dir(), 'large-v3-turbo')
    assert os.path.isdir(p), f'Missing model dir: {p}'

def t_model_base_has_bin():
    p = os.path.join(storage.models_dir(), 'base', 'model.bin')
    assert os.path.isfile(p), f'model.bin missing in base/'

def t_model_small_has_bin():
    p = os.path.join(storage.models_dir(), 'small', 'model.bin')
    assert os.path.isfile(p), f'model.bin missing in small/'

def t_prompts_json_exists():
    p = storage.resource_path('prompts.json')
    assert os.path.isfile(p), f'prompts.json not found at {p}'

run('silero_vad.onnx present and > 1 MB',                    t_onnx_present)
run('models/base/ directory exists',                         t_model_base_exists)
run('models/small/ directory exists',                        t_model_small_exists)
run('models/large-v3-turbo/ directory exists',               t_model_large_v3_turbo_exists)
run('models/base/model.bin present',                         t_model_base_has_bin)
run('models/small/model.bin present',                        t_model_small_has_bin)
run('prompts.json exists next to main.py',                   t_prompts_json_exists)


# ===============================================================================
# 16. GUI WINDOWS -- build without crash (hidden)
# ===============================================================================

section('16. GUI windows -- build without crash')

import customtkinter as ctk
ctk.set_appearance_mode('dark')
ctk.set_default_color_theme('dark-blue')

_ctk_root = ctk.CTk()
_ctk_root.withdraw()

def t_library_window_builds():
    from library import LibraryWindow
    prompts = storage.load_prompts()
    lib = LibraryWindow(
        _ctk_root, prompts,
        on_select=lambda p: None,
        on_save=lambda p: None,
    )
    _ctk_root.update()
    assert lib.win.winfo_exists()
    lib.win.destroy()

def t_settings_window_builds():
    from settings import SettingsWindow
    cfg = storage.load_config()
    sw = SettingsWindow(_ctk_root, cfg, on_save=lambda c: None)
    _ctk_root.update()
    assert sw.win.winfo_exists()
    # Check all 3 panels present
    assert 'general'   in sw._panels
    assert 'providers' in sw._panels
    assert 'whisper'   in sw._panels
    # Check all 3 nav buttons
    assert 'general'   in sw._nav_btns
    assert 'providers' in sw._nav_btns
    assert 'whisper'   in sw._nav_btns
    sw.win.destroy()

def t_settings_panel_switching():
    from settings import SettingsWindow
    cfg = storage.load_config()
    sw = SettingsWindow(_ctk_root, cfg, on_save=lambda c: None)
    _ctk_root.update()
    sw._show_panel('providers')
    _ctk_root.update()
    sw._show_panel('whisper')
    _ctk_root.update()
    sw._show_panel('general')
    _ctk_root.update()
    sw.win.destroy()

def t_settings_save_produces_valid_config():
    from settings import SettingsWindow
    saved = {}
    cfg = storage.load_config()
    sw = SettingsWindow(_ctk_root, cfg, on_save=lambda c: saved.update(c))
    _ctk_root.update()
    sw._save()
    _ctk_root.update()
    assert 'active_provider' in saved
    assert 'hotkeys'         in saved
    assert 'whisper'         in saved
    assert 'providers'       in saved
    assert 'whisper' in saved
    assert 'model' in saved['whisper']
    assert 'audio' in saved['whisper']

def t_settings_hotkey_vars_present():
    from settings import SettingsWindow
    cfg = storage.load_config()
    sw = SettingsWindow(_ctk_root, cfg, on_save=lambda c: None)
    _ctk_root.update()
    assert 'refine'  in sw._hotkey_vars
    assert 'library' in sw._hotkey_vars
    assert 'whisper' in sw._hotkey_vars
    sw.win.destroy()

run('LibraryWindow builds and window exists',           t_library_window_builds)
run('SettingsWindow builds with 3 panels + 3 nav btns', t_settings_window_builds)
run('SettingsWindow panel switching -- no crash',         t_settings_panel_switching)
run('SettingsWindow._save() produces valid config',      t_settings_save_produces_valid_config)
run('SettingsWindow has hotkey vars for all 3 actions',  t_settings_hotkey_vars_present)


# ===============================================================================
# 17. INTEGRATION -- config → make_whisper_cfg → Transcriber init
# ===============================================================================

section('17. Integration -- config pipeline')

def t_config_to_transcriber_init():
    cfg  = storage.load_config()
    wcfg = storage.make_whisper_cfg(cfg)
    results = []
    statuses = []

    t = tr.Transcriber(
        cfg=wcfg,
        on_result=lambda text, lang, dur: results.append(text),
        on_status=lambda s: statuses.append(s),
        models_dir=storage.models_dir(),
        log_file=storage.log_path(),
    )
    # Wait up to 30s for loading status
    deadline = time.time() + 30
    while 'loading' not in statuses and time.time() < deadline:
        time.sleep(0.1)
    assert 'loading' in statuses, f'Never got loading status. Got: {statuses}'
    t.shutdown()

def t_config_to_transcriber_cancel():
    cfg  = storage.load_config()
    wcfg = storage.make_whisper_cfg(cfg)
    t = tr.Transcriber(
        cfg=wcfg,
        on_result=lambda *a: None,
        on_status=lambda s: None,
        models_dir=storage.models_dir(),
        log_file='',
    )
    t.cancel()   # should not raise
    t.shutdown()

def t_whisper_cfg_round_trip():
    cfg   = storage.load_config()
    cfg['whisper']['model']['cpu_model'] = 'base'
    cfg['whisper']['audio']['noise_reduction'] = False
    wcfg  = storage.make_whisper_cfg(cfg)
    assert wcfg.model.cpu_model == 'base'
    assert wcfg.audio.noise_reduction is False

def t_provider_build_from_loaded_config():
    cfg = storage.load_config()
    p = engine.build_provider(cfg)
    assert isinstance(p, engine.Provider)

run('Transcriber init fires loading status',                   t_config_to_transcriber_init)
run('Transcriber cancel() when idle -- no exception',           t_config_to_transcriber_cancel)
run('whisper config changes reflected in make_whisper_cfg()',  t_whisper_cfg_round_trip)
run('build_provider(load_config()) returns a Provider',        t_provider_build_from_loaded_config)


# ===============================================================================
# SUMMARY
# ===============================================================================

try:
    _root.destroy()
except Exception:
    pass
try:
    _ctk_root.destroy()
except Exception:
    pass

total = PASS + FAIL + WARN
print(f'\n{"="*60}')
print(f'  Results: {total} tests -- '
      f'\033[32m{PASS} passed\033[0m  '
      f'\033[31m{FAIL} failed\033[0m  '
      f'\033[33m{WARN} warnings\033[0m')

if FAIL:
    print('\nFailed tests:')
    for r in _results:
        if r[0] == 'FAIL':
            print(f'  FAIL: {r[1]}')
            if len(r) > 2:
                print(f'      {r[2]}')

print(f'{"="*60}\n')
sys.exit(0 if FAIL == 0 else 1)
