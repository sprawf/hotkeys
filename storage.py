import os
import sys
import copy
import json
import shutil
import logging
import threading

logger = logging.getLogger(__name__)

APP_NAME = 'Hotkeys'
VERSION  = '1.0.0'


# ── Atomic JSON write helper ────────────────────────────────────────────────
#
# Every JSON save in this module used to do `open('w')` then `json.dump`
# into the real file. A crash, power loss, or AV interference mid-write
# truncated the file. The loader then fell back to defaults; the next
# save made that loss permanent. ~7 call sites also fired saves on
# unsynchronised daemon threads from quicknotes/library, so two rapid
# actions could interleave fragments of two different JSON documents
# into the same file. Both classes of failure end with the user's
# notes/config/bookmarks silently wiped.
#
# write_json_atomic() ALWAYS writes to <path>.tmp, fsyncs, then
# os.replace()s into the final name. os.replace is atomic on every
# platform we ship to (NTFS + APFS + Linux ext4). A per-path lock
# serialises concurrent writers so we never half-merge two saves.

_path_write_locks: dict[str, threading.Lock] = {}
_path_write_locks_guard = threading.Lock()


def _quarantine_corrupt(path: str) -> None:
    """Rename a JSON file we failed to parse so the next save won't blow
    it away. The user (or a future recovery routine) can salvage the
    original from the .corrupt-<ts> sidecar. Best-effort: any failure
    here just gets logged so the caller can still return defaults."""
    try:
        if not os.path.exists(path):
            return
        import time as _t
        ts = _t.strftime('%Y%m%d-%H%M%S')
        dest = f'{path}.corrupt-{ts}'
        # If another corruption happened in the same second, append a
        # counter so we never overwrite an earlier quarantine file.
        n = 1
        while os.path.exists(dest):
            dest = f'{path}.corrupt-{ts}-{n}'
            n += 1
        os.rename(path, dest)
        logger.warning(f'Quarantined corrupt JSON: {path} -> {dest}')
    except Exception as e:
        logger.warning(f'Quarantine failed for {path}: {e}')


def _lock_for(path: str) -> threading.Lock:
    """Per-path lock (lazy-init). Same path → same lock object across
    every call, so even daemon threads from different modules serialise."""
    key = os.path.normcase(os.path.abspath(path))
    with _path_write_locks_guard:
        lock = _path_write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_write_locks[key] = lock
        return lock


# ── Coalesced save (drop intermediate writes during rapid edits) ─────────────
#
# Rapid typing in Quick Notes used to spawn one daemon save thread per
# keystroke (each debounced ~300ms). The per-path lock above serialises
# them, but threads pile up: thread N finishes, N+1 wakes, etc. On
# shutdown all queued daemons get killed mid-write. With the coalescer,
# only one writer is ever in flight, and it always picks up the LATEST
# data — intermediate snapshots are silently dropped, which is what the
# caller wants anyway (the final state is what should land on disk).

_coalesce_pending: dict[str, object] = {}      # path → latest data
_coalesce_workers: dict[str, threading.Thread] = {}  # path → live worker
_coalesce_guard = threading.Lock()


def save_notes_coalesced(notes: list) -> None:
    """Replace any unwritten pending save for notes.json with `notes`,
    spawn one worker if none is running. Safe to call from any thread."""
    _coalesced_save(notes_path(), notes, lambda d: save_notes(d))


def save_history_coalesced(entries: list) -> None:
    _coalesced_save(history_path(), entries, lambda d: save_history(d))


def save_chains_coalesced(chains: list) -> None:
    _coalesced_save(chains_path(), chains, lambda d: save_chains(d))


def save_config_coalesced(config: dict) -> None:
    _coalesced_save(config_path(), config, lambda d: save_config(d))


def save_bookmarks_coalesced(bms: list) -> None:
    _coalesced_save(bookmarks_path(), bms, lambda d: save_bookmarks(d))


def _coalesced_save(path: str, data, writer) -> None:
    key = os.path.normcase(os.path.abspath(path))
    with _coalesce_guard:
        _coalesce_pending[key] = data
        existing = _coalesce_workers.get(key)
        if existing is not None and existing.is_alive():
            return   # worker will pick up the new data on its next loop
        def _drain():
            while True:
                with _coalesce_guard:
                    pending = _coalesce_pending.pop(key, _SENTINEL)
                    if pending is _SENTINEL:
                        _coalesce_workers.pop(key, None)
                        return
                try:
                    writer(pending)
                except Exception as e:
                    logger.warning(f'Coalesced save failed for {path}: {e}')
        t = threading.Thread(target=_drain, daemon=True,
                             name=f'save-{os.path.basename(path)}')
        _coalesce_workers[key] = t
        t.start()


_SENTINEL = object()


def wait_for_writes(timeout: float = 3.0) -> bool:
    """Block until every known per-path lock is briefly acquirable AND
    every active coalesced-save worker drains, then release. After this
    returns True, no save thread is mid-fsync and no pending data is
    in flight — the caller can safely tear down (or arm a force-killer)
    without losing a partial write. Returns False if it times out (caller
    should still proceed: better partial loss than indefinite hang)."""
    import time as _t
    deadline = _t.time() + timeout
    # First: drain coalesced workers. Their loop exits once
    # _coalesce_pending is empty, at which point the worker thread ends.
    with _coalesce_guard:
        workers = list(_coalesce_workers.values())
    for w in workers:
        remaining = deadline - _t.time()
        if remaining <= 0:
            return False
        w.join(timeout=remaining)
        if w.is_alive():
            return False
    # Second: acquire each lock briefly to guarantee any direct-write
    # save_* thread (non-coalesced legacy callers) has finished.
    with _path_write_locks_guard:
        locks = list(_path_write_locks.values())
    held: list[threading.Lock] = []
    try:
        for lk in locks:
            remaining = deadline - _t.time()
            if remaining <= 0 or not lk.acquire(timeout=max(0.05, remaining)):
                return False
            held.append(lk)
        return True
    finally:
        for lk in held:
            try:
                lk.release()
            except Exception:
                pass


def write_json_atomic(path: str, data, *, indent: int = 2,
                      ensure_ascii: bool = False,
                      fsync: bool = True) -> None:
    """Write `data` to `path` atomically: write to .tmp, fsync, replace.
    Synchronised across threads on `path`. Raises on serious I/O error
    (callers should log but most catch broadly). The pattern is what
    main.py's whiteboard restore already used; this just centralises it."""
    tmp = path + '.tmp'
    lock = _lock_for(path)
    with lock:
        try:
            # Ensure target dir exists (rare but possible on fresh installs).
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
                f.flush()
                if fsync:
                    try: os.fsync(f.fileno())
                    except (OSError, AttributeError):
                        # fsync may fail on certain network mounts; we still
                        # got the write, just lose the durability guarantee.
                        pass
            os.replace(tmp, path)
        except Exception:
            # Clean up the orphan tmp so future loads don't pick it up.
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            raise


# ── Path helpers ──────────────────────────────────────────────────────────────

def appdata_dir() -> str:
    """Return the directory used for all user data (config, prompts, logs, history).

    Frozen (dist) build , stores data in a `data` folder next to Hotkeys.exe
                           so the install is fully self-contained and portable.
    Source (dev) build  , stores data in the OS roaming AppData folder so the
                           developer's working copy is isolated from dist builds.
    """
    # Memoise the resolved path. The disk probe used to run on EVERY
    # call, and notes_path() is called many times per click — multiple
    # threads racing on the same `.write_test` would have one thread's
    # cleanup-remove fail (file already deleted by another thread),
    # send the loser to the FALLBACK TEMP dir, and read stale notes
    # there. The path never changes during a session, so cache once.
    cached = getattr(appdata_dir, '_resolved_path', None)
    if cached is not None:
        return cached
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
    try:
        os.makedirs(path, exist_ok=True)
        # Verify we can actually write there. Use a PID-specific name
        # so parallel callers don't clobber each other's probe file
        # (the old single shared name caused the disk-race that sent
        # readers to the fallback dir mid-click).
        _test = os.path.join(path, f'.write_test_{os.getpid()}')
        with open(_test, 'w') as _f:
            _f.write('ok')
        # The cleanup remove is best-effort. The write SUCCEEDED, so
        # we know the path works; if AV / another thread / a permissions
        # quirk makes the remove fail, do NOT misinterpret that as
        # "directory is not writable".
        try: os.remove(_test)
        except Exception: pass
    except Exception as e:
        logger.error(f'Data folder not writable ({path}): {e}')
        # Fall back to a writable temp location so the app can still run
        import tempfile
        fallback = os.path.join(tempfile.gettempdir(), APP_NAME)
        os.makedirs(fallback, exist_ok=True)
        logger.warning(f'Using fallback data dir: {fallback}')
        # Store the warning so main.py can surface it to the user once at startup
        appdata_dir._permission_warning = (
            f'Hotkeys cannot write to its data folder:\n{path}\n\n'
            f'Move the Hotkeys folder out of Program Files or any read-only location.\n\n'
            f'Using temporary storage for now, your settings will not be saved.'
        )
        appdata_dir._resolved_path = fallback
        return fallback
    appdata_dir._permission_warning = None
    appdata_dir._resolved_path = path
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


def bookmarks_path() -> str:
    return os.path.join(appdata_dir(), 'bookmarks.json')


def notes_path() -> str:
    return os.path.join(appdata_dir(), 'notes.json')


def whiteboard_path() -> str:
    return os.path.join(appdata_dir(), 'whiteboard.json')


def load_notes() -> list:
    p = notes_path()
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        logger.warning('Failed to load notes (quarantining corrupt file)')
        _quarantine_corrupt(p)
        return []


_MAX_NOTES = 500   # hard cap, oldest unpinned trimmed first, then oldest pinned


def save_notes(notes: list) -> None:
    if len(notes) > _MAX_NOTES:
        # Keep all pinned notes first, then fill with the most-recent unpinned
        pinned   = [n for n in notes if n.get('pinned')]
        unpinned = [n for n in notes if not n.get('pinned')]
        # Trim unpinned from the oldest end (front of list, which is oldest)
        keep_unpinned = _MAX_NOTES - len(pinned)
        if keep_unpinned < 0:
            keep_unpinned = 0
        notes = pinned + unpinned[-keep_unpinned:] if keep_unpinned else pinned
        logger.info(f'Notes trimmed to {_MAX_NOTES} (cap reached)')
    try:
        write_json_atomic(notes_path(), notes)
    except Exception:
        logger.exception('Failed to save notes')


def transcripts_dir() -> str:
    """Folder where TranscriptJob JSON dumps live (one file per job).
    Lazy-created on first access."""
    d = os.path.join(appdata_dir(), 'transcripts')
    os.makedirs(d, exist_ok=True)
    return d


_MAX_TRANSCRIPTS = 200   # hard cap, oldest deleted first when exceeded


def load_transcripts() -> list:
    """Return all saved TranscriptJob dicts, newest first.  Each file is one
    job; corrupt files are skipped silently so a single bad write can't
    poison the entire list."""
    d = transcripts_dir()
    out: list = []
    try:
        for name in os.listdir(d):
            if not name.endswith('.json'):
                continue
            try:
                with open(os.path.join(d, name), encoding='utf-8') as f:
                    out.append(json.load(f))
            except Exception:
                continue
    except FileNotFoundError:
        return []
    out.sort(key=lambda j: j.get('created_at', 0), reverse=True)
    return out


def save_transcript(job: dict) -> None:
    """Write one TranscriptJob dict to <id>.json. Caller passes the
    JSON-serializable dict (TranscriptJob.to_dict()).  Trims oldest if over
    the cap so the folder doesn't grow unbounded."""
    d = transcripts_dir()
    jid = job.get('id', '')
    if not jid:
        return
    try:
        path = os.path.join(d, f'{jid}.json')
        write_json_atomic(path, job)
    except Exception:
        logger.exception('Failed to save transcript')
        return
    # Trim oldest when over cap
    try:
        files = sorted(
            (f for f in os.listdir(d) if f.endswith('.json')),
            key=lambda f: os.path.getmtime(os.path.join(d, f)),
        )
        for old in files[:-_MAX_TRANSCRIPTS]:
            try: os.remove(os.path.join(d, old))
            except Exception: pass
    except Exception:
        pass


def delete_transcript(job_id: str) -> None:
    """Remove the saved JSON for a job id; silent no-op if missing."""
    try:
        os.remove(os.path.join(transcripts_dir(), f'{job_id}.json'))
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning(f'Failed to delete transcript {job_id}')


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
    # Portable dist gets shared as a zip — autostart=True writes the
    # extract path into the Run registry key. If the user later moves
    # or deletes the folder, that registry entry points at a missing
    # file and Windows nags every login. Default off; users can flip
    # it in Settings once they decide on a permanent install location.
    'autostart':       False,
    'push_to_talk':    False,
    'notes_geometry':       '',       # saved geometry for Quick Notes window (WxH+X+Y)
    'notes_theme':          'light',  # 'light' or 'dark', persisted across sessions
    'hotkeys': {
        'refine':       'alt+shift+w',
        'library':      'alt+shift+e',
        'whisper':      'ctrl+enter',
        'undo_refine':  'alt+shift+z',
        'macro_record': 'shift+f1',
        'recorder':     'shift+f2',
        'gif_record':   'shift+f3',
        'ask':          'shift+f4',
        'web':          'shift+f5',
        'chain':        'shift+f6',
        'notes':        'shift+f7',
        'whiteboard':   'shift+f8',
        # File/URL transcription pipeline (faster-whisper + pyannote diarization
        # + AI summary + multi-format export). See transcribe/ package.
        'transcribe':   'shift+f9',
        # Bundled audio editor (Tenacity portable, relabeled to "Audio
        # Editor" at the window-title layer). See audio_editor.py.
        'audio_editor': 'shift+f10',
        # URL downloader (YouTube / SoundCloud / Vimeo / Twitter / 1000+
        # sites yt-dlp supports). Select a URL in any app → press hotkey →
        # downloads best-quality video into ~/Downloads.
        'download_url': 'ctrl+alt+d',
    },
    'providers': {
        'local':    {'model_id': 'Qwen/Qwen2.5-1.5B-Instruct-GGUF'},
        'groq':     {'api_key': '', 'model': 'llama-3.3-70b-versatile',
                     'vision_model': 'qwen/qwen3.6-27b'},
        # llama3.1-8b was retired by Cerebras (404s on every call); the
        # current default matches engine.CEREBRAS_MODELS[0].
        'cerebras': {'api_key': '', 'model': 'llama-3.3-70b'},
    },
    'whisper': {
        'model': {
            'gpu_model':    'large-v3-turbo',
            # Default to `base` on CPU: ~2 s for short dictation on a 6-core
            # consumer CPU vs ~6 s with `small`. Quality is good enough for
            # most dictation use cases. Users who want highest local
            # accuracy can flip to `small` in Settings → Audio → CPU model.
            'cpu_model':    'base',
            'device':       'auto',
            'compute_type': 'auto',
        },
        'audio': {
            'input_device_index': None,
            'noise_reduction':    True,
            # Hybrid transcription: when True and online, dictation goes to
            # Groq's hosted Whisper (large-v3-turbo, ~13× faster than local
            # CPU small). If the cloud call fails or times out, the local
            # CPU model is the transparent fallback, the user always gets
            # a result. Set False to force local-only.
            'cloud_enabled':      True,
            # How long to wait for Groq before giving up and using local.
            # Keep tight, 3 s covers 99 % of successful cloud calls; longer
            # waits just delay the eventual local fallback for the unlucky
            # 1 %.
            'cloud_timeout_s':    3.0,
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
        merged['providers'] = _deep_merge(DEFAULT_CONFIG['providers'], cfg.get('providers', {}))
        merged['hotkeys']   = {**DEFAULT_CONFIG['hotkeys'],   **cfg.get('hotkeys',   {})}
        merged['whisper']   = _deep_merge(DEFAULT_CONFIG['whisper'], cfg.get('whisper', {}))
        # ── Migration: retired provider models ────────────────────────────
        # If the saved config still names a Cerebras model that the
        # provider has retired (e.g. llama3.1-8b → 404 on every call),
        # silently upgrade to the current default so the user doesn't
        # have to discover Settings and re-pick. The user's API key
        # still works; only the model id was retired.
        _RETIRED_CEREBRAS = {'llama3.1-8b', 'llama3.1-70b'}
        try:
            _cb = merged['providers'].get('cerebras', {})
            if _cb.get('model') in _RETIRED_CEREBRAS:
                _new = DEFAULT_CONFIG['providers']['cerebras']['model']
                logger.info(
                    f'Config migration: Cerebras model '
                    f'{_cb.get("model")!r} retired, upgrading to {_new!r}.')
                _cb['model'] = _new
                merged['providers']['cerebras'] = _cb
        except Exception:
            pass
        # ── Migration: vision-model rollback ────────────────────────────────
        # An earlier release attempted to move to llama-4-maverick which
        # turns out NOT to be on Groq (only Scout is). Any saved config
        # naming maverick → 404. Snap it back to Scout so OCR works.
        _migrated = False
        try:
            _gq = merged['providers'].get('groq', {})
            _vm = _gq.get('vision_model', '')
            if 'maverick' in _vm:
                _new_vm = DEFAULT_CONFIG['providers']['groq']['vision_model']
                logger.info(
                    f'Config migration: Groq vision_model '
                    f'{_vm!r} → {_new_vm!r} (maverick not on Groq).')
                _gq['vision_model'] = _new_vm
                merged['providers']['groq'] = _gq
                _migrated = True
        except Exception:
            pass
        if _migrated:
            try:
                save_config(merged)
            except Exception:
                pass
        return merged
    except FileNotFoundError:
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)
    except Exception as e:
        logger.error(f'Config load error: {e}, using defaults (quarantining corrupt file)')
        _quarantine_corrupt(path)
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    try:
        write_json_atomic(config_path(), config)
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
        # Fresh install, copy the bundled default set to AppData
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
        logger.error(f'Prompts load error: {e} (quarantining corrupt file)')
        _quarantine_corrupt(user_path)
        return []


def save_prompts(prompts: list) -> None:
    # Write only to the user's AppData copy.
    # The source prompts.json is the shipped defaults and must never be
    # overwritten by user edits, otherwise Restore Default Prompts would
    # restore whatever the user last saved, not the real defaults.
    for path in [prompts_path()]:
        try:
            write_json_atomic(path, prompts)
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
    p = history_path()
    try:
        with open(p, encoding='utf-8') as f:
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
        logger.error(f'History load error: {e} (quarantining corrupt file)')
        _quarantine_corrupt(p)
        return []


def save_history(entries: list) -> None:
    try:
        pruned = _prune_history(entries)
        write_json_atomic(history_path(), pruned)
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


# ── Bookmarks ─────────────────────────────────────────────────────────────────

_DEFAULT_BOOKMARKS = [
    {'name': 'YouTube',  'url': 'https://www.youtube.com',           'active': True},
    {'name': 'Google',   'url': 'https://www.google.com',            'active': False},
    {'name': 'EditPad',  'url': 'https://www.editpad.org',           'active': False},
    {'name': 'X',        'url': 'https://www.x.com',                 'active': False},
    {'name': 'WhatsApp', 'url': 'https://web.whatsapp.com',           'active': False},
    {'name': 'GeoScore', 'url': 'geoscoreapp.pages.dev',             'active': False},
]


def load_bookmarks() -> list:
    bp = bookmarks_path()
    try:
        with open(bp, encoding='utf-8') as f:
            bms = json.load(f)
        # Migrate: ensure every entry has an 'active' field
        changed = False
        for i, b in enumerate(bms):
            if 'active' not in b:
                b['active'] = (i == 0)
                changed = True
        # Ensure exactly one is active
        active_count = sum(1 for b in bms if b.get('active'))
        if active_count == 0 and bms:
            bms[0]['active'] = True
            changed = True
        if changed:
            save_bookmarks(bms)
        return bms
    except FileNotFoundError:
        save_bookmarks(_DEFAULT_BOOKMARKS)
        return copy.deepcopy(_DEFAULT_BOOKMARKS)
    except Exception as e:
        logger.error(f'Bookmarks load error: {e} (quarantining corrupt file)')
        _quarantine_corrupt(bp)
        return copy.deepcopy(_DEFAULT_BOOKMARKS)


def save_bookmarks(bookmarks: list) -> None:
    try:
        write_json_atomic(bookmarks_path(), bookmarks)
    except Exception as e:
        logger.error(f'Bookmarks save error: {e}')


def get_active_bookmark() -> dict | None:
    """Return the currently active bookmark, or None."""
    bms = load_bookmarks()
    for b in bms:
        if b.get('active'):
            return b
    return bms[0] if bms else None


# ── Chains ─────────────────────────────────────────────────────────────────────

def chains_path() -> str:
    return os.path.join(appdata_dir(), 'chains.json')


DEFAULT_CHAINS: list = [
    {
        'name':   'Refine & Translate',
        'color':  '#B2EBF2',
        'active': True,
        'hotkey': '',
        'steps': [
            {
                'label':  'Refine',
                'prompt': 'Fix grammar, spelling, and clarity. Return only the improved text.',
            },
            {
                'label':  'Translate',
                'prompt': 'Translate the text to Spanish. Return only the translated text.',
            },
        ],
    },
    {
        'name':   'Simplify & Tweet',
        'color':  '#DCEDC8',
        'active': False,
        'hotkey': '',
        'steps': [
            {
                'label':  'Simplify',
                'prompt': 'Rewrite this in simple, clear language. Return only the simplified text.',
            },
            {
                'label':  'Tweet',
                'prompt': 'Compress this into a tweet (max 280 chars, no hashtags). Return only the tweet.',
            },
        ],
    },
]


def load_chains() -> list:
    path = chains_path()
    if not os.path.exists(path):
        try:
            write_json_atomic(path, DEFAULT_CHAINS)
            logger.info('Default chains written to AppData.')
        except Exception as e:
            logger.error(f'Chains init error: {e}')
        return copy.deepcopy(DEFAULT_CHAINS)
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f'Chains load error: {e} (quarantining corrupt file)')
        _quarantine_corrupt(path)
        return copy.deepcopy(DEFAULT_CHAINS)


def save_chains(chains: list) -> None:
    try:
        write_json_atomic(chains_path(), chains)
    except Exception as e:
        logger.error(f'Chains save error: {e}')
