import os
import sys
import copy
import json
import shutil
import logging

logger = logging.getLogger(__name__)

APP_NAME = 'Hotkeys'
VERSION  = '1.0.0'


# ── Path helpers ──────────────────────────────────────────────────────────────

def appdata_dir() -> str:
    """Return the directory used for all user data (config, prompts, logs, history).

    Frozen (dist) build  — stores data in a `data` folder next to Hotkeys.exe
                           so the install is fully self-contained and portable.
    Source (dev) build   — stores data in the OS roaming AppData folder so the
                           developer's working copy is isolated from dist builds.
    """
    if getattr(sys, 'frozen', False):
        if sys.platform == 'darwin':
            # Mac .app: store data in ~/Library/Application Support/Hotkeys
            path = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', APP_NAME)
        else:
            # Windows portable zip: data folder next to the exe
            exe_dir = os.path.dirname(sys.executable)
            path = os.path.join(exe_dir, 'data')
    elif sys.platform == 'win32':
        path = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), APP_NAME)
    elif sys.platform == 'darwin':
        path = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', APP_NAME)
    else:
        path = os.path.join(os.environ.get('XDG_CONFIG_HOME',
                            os.path.join(os.path.expanduser('~'), '.config')), APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def resource_path(filename: str) -> str:
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS  # type: ignore
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


def config_path() -> str:
    return os.path.join(appdata_dir(), 'config.json')


def prompts_path() -> str:
    return os.path.join(appdata_dir(), 'prompts.json')


def log_path() -> str:
    return os.path.join(appdata_dir(), 'app.log')


def history_path() -> str:
    return os.path.join(appdata_dir(), 'history.json')


def models_dir() -> str:
    """Return path to the whisper model folder (bundled or project-local)."""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'models')  # type: ignore
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')


def assets_dir() -> str:
    """Return path to the assets folder (bundled or project-local)."""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'assets')  # type: ignore
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')


# ── Unified config schema ─────────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    'version':         VERSION,
    'active_provider': 'cerebras',   # fastest out of the box
    'autostart':       True,
    'push_to_talk':    False,
    'hotkeys': {
        'refine':       'alt+shift+w',
        'library':      'alt+shift+e',
        'whisper':      'ctrl+enter',
        'undo_refine':  'alt+shift+z',
    },
    'providers': {
        'local':    {'model_id': 'Qwen/Qwen2.5-1.5B-Instruct-GGUF'},
        'groq':     {'api_key': '', 'model': 'llama-3.3-70b-versatile'},
        'cerebras': {'api_key': '', 'model': 'llama3.1-8b'},
    },
    'whisper': {
        'model': {
            'gpu_model':    'large-v3-turbo',
            'cpu_model':    'small',
            'device':       'auto',
            'compute_type': 'auto',
        },
        'audio': {
            'input_device_index': None,
            'noise_reduction':    True,
        },
        'vad': {
            'safety_silence_s': 60,
            'speech_threshold': 0.5,
        },
        'transcription': {
            'language':                   None,
            'beam_size':                  2,
            'temperature':                0.0,
            'condition_on_previous_text': False,
            'custom_vocabulary':          '',
            'initial_prompt':             '',
        },
        'output': {
            'type_text':          True,
            'copy_to_clipboard':  True,
            'add_trailing_space': True,
        },
        'modes': {
            'active_mode': 'Default',
            'definitions': {
                'Default': {'initial_prompt': '', 'post_rules': []},
                'Email': {
                    'initial_prompt': 'Professional email. Capitalize sentences. Use proper punctuation.',
                    'post_rules': ['capitalize_sentences', 'fix_punctuation'],
                },
                'Code': {
                    'initial_prompt': 'Programming code and identifiers. Preserve case exactly.',
                    'post_rules': [],
                },
                'Notes': {
                    'initial_prompt': 'Casual notes.',
                    'post_rules': [],
                },
            },
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def load_config() -> dict:
    path = config_path()
    try:
        with open(path, encoding='utf-8-sig') as f:   # utf-8-sig strips BOM if present
            cfg = json.load(f)
        merged = {**DEFAULT_CONFIG, **cfg}
        merged['providers'] = {**DEFAULT_CONFIG['providers'], **cfg.get('providers', {})}
        merged['hotkeys']   = {**DEFAULT_CONFIG['hotkeys'],   **cfg.get('hotkeys',   {})}
        merged['whisper']   = _deep_merge(DEFAULT_CONFIG['whisper'], cfg.get('whisper', {}))
        merged.get('providers', {}).pop('gemini', None)
        return merged
    except FileNotFoundError:
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)
    except Exception as e:
        logger.error(f'Config load error: {e} — using defaults')
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    try:
        with open(config_path(), 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f'Config save error: {e}')


# ── WhisperCfg adapter ────────────────────────────────────────────────────────

class _Namespace:
    """Recursively wrap a dict for attribute-style access (used by core/ modules)."""
    def __init__(self, d: dict) -> None:
        for k, v in d.items():
            setattr(self, k, _Namespace(v) if isinstance(v, dict) else v)


def make_whisper_cfg(config: dict) -> _Namespace:
    """Wrap config['whisper'] as a _Namespace for core/ module compatibility."""
    return _Namespace(config.get('whisper', DEFAULT_CONFIG['whisper']))


# ── Prompts ───────────────────────────────────────────────────────────────────

_FALLBACK_COLORS = [
    '#FFF9C4', '#DCEDC8', '#BBDEFB', '#F8BBD0',
    '#FFE0B2', '#E1BEE7', '#D7CCC8', '#B2DFDB',
]


def load_prompts() -> list:
    user_path = prompts_path()
    bundled   = resource_path('prompts.json')

    if not os.path.exists(user_path):
        # Fresh install — copy the bundled default set to AppData
        try:
            shutil.copy2(bundled, user_path)
            logger.info('Default prompts copied to AppData.')
        except Exception as e:
            logger.error(f'Failed to copy default prompts: {e}')
            return []

    try:
        with open(user_path, encoding='utf-8-sig') as f:
            prompts = json.load(f)
        # Migrate: add color field if missing
        changed = False
        for i, p in enumerate(prompts):
            if 'color' not in p:
                p['color'] = _FALLBACK_COLORS[i % len(_FALLBACK_COLORS)]
                changed = True
        if changed:
            save_prompts(prompts)
            logger.info('Migrated prompts: added missing color fields.')
        return prompts
    except Exception as e:
        logger.error(f'Prompts load error: {e}')
        return []


def save_prompts(prompts: list) -> None:
    data = json.dumps(prompts, indent=2, ensure_ascii=False)
    # Always write the user's copy (exe-adjacent data\ for dist, AppData for source)
    paths = [prompts_path()]
    if not getattr(sys, 'frozen', False):
        # Dev: also update the source prompts.json so the next dist build is current
        paths.append(resource_path('prompts.json'))
    for path in paths:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(data)
        except Exception as e:
            logger.error(f'Prompts save error ({path}): {e}')


# ── History ───────────────────────────────────────────────────────────────────

_HISTORY_MAX_ENTRIES = 200
_HISTORY_MAX_AGE_DAYS = 30


def _prune_history(entries: list) -> list:
    """Remove entries older than _HISTORY_MAX_AGE_DAYS and enforce count cap."""
    import datetime
    cutoff = datetime.datetime.now() - datetime.timedelta(days=_HISTORY_MAX_AGE_DAYS)
    pruned = []
    for e in entries:
        ts = e.get('ts', '')
        try:
            if datetime.datetime.fromisoformat(ts) >= cutoff:
                pruned.append(e)
        except Exception:
            pruned.append(e)   # keep entries with unparseable timestamps
    return pruned[-_HISTORY_MAX_ENTRIES:]


def load_history() -> list:
    try:
        with open(history_path(), encoding='utf-8') as f:
            entries = json.load(f)
        pruned = _prune_history(entries)
        if len(pruned) != len(entries):
            # Persist the pruned version immediately so the file stays tidy
            save_history(pruned)
            logger.info(f'History pruned: {len(entries) - len(pruned)} old entries removed.')
        return pruned
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error(f'History load error: {e}')
        return []


def save_history(entries: list) -> None:
    try:
        pruned = _prune_history(entries)
        with open(history_path(), 'w', encoding='utf-8') as f:
            json.dump(pruned, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f'History save error: {e}')


# ── Autostart ─────────────────────────────────────────────────────────────────

def set_autostart(enabled: bool) -> None:
    if sys.platform == 'darwin':
        _set_autostart_mac(enabled)
    elif sys.platform == 'win32':
        _set_autostart_win(enabled)
    # Linux: no-op (systemd units are out of scope)


def _set_autostart_win(enabled: bool) -> None:
    import winreg
    exe = sys.executable if getattr(sys, 'frozen', False) else None
    if not exe:
        return  # Don't set autostart when running from source
    key_path = r'Software\Microsoft\Windows\CurrentVersion\Run'
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        logger.error(f'Autostart error: {e}')


def _set_autostart_mac(enabled: bool) -> None:
    import plistlib
    from pathlib import Path
    launch_agents = Path.home() / 'Library' / 'LaunchAgents'
    plist_path    = launch_agents / f'com.{APP_NAME.lower()}.app.plist'
    exe = sys.executable if getattr(sys, 'frozen', False) else None
    if not exe:
        return  # Don't set autostart when running from source
    try:
        if enabled:
            launch_agents.mkdir(parents=True, exist_ok=True)
            plist = {
                'Label':           f'com.{APP_NAME.lower()}.app',
                'ProgramArguments': [exe],
                'RunAtLoad':       True,
                'KeepAlive':       False,
            }
            with open(plist_path, 'wb') as f:
                plistlib.dump(plist, f)
        else:
            plist_path.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f'Autostart (Mac) error: {e}')
