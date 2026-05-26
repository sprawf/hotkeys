"""
MacroLibrary — persists recorded macros to disk.

Storage: C:\\Users\\User\\AppData\\Roaming\\Hotkeys\\macros\\
Each macro is a single JSON file: {id}.json
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

from macros.recorder import MacroRecorder

# Hard cap on how many macros can be saved — keeps the library manageable and
# prevents the macros/ folder from growing without bound.
_MAX_SAVED_MACROS = 50


class MacroLibrary:
    """Manages saved macros stored as JSON files on disk."""

    def __init__(self, folder: Path) -> None:
        self._folder = Path(folder)
        self._folder.mkdir(parents=True, exist_ok=True)
        self._macros: list[dict] = []   # metadata only — no 'events' key
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def macros(self) -> list[dict]:
        """Returns list of metadata dicts (no 'events' key — kept on disk only)."""
        return list(self._macros)

    def next_default_name(self) -> str:
        """Returns 'Macro 1', 'Macro 2', etc. — first unused name."""
        existing = {m['name'] for m in self._macros}
        n = 1
        while True:
            candidate = f'Macro {n}'
            if candidate not in existing:
                return candidate
            n += 1

    def next_available_hotkey(self) -> str:
        """Returns first unused ctrl+f# from ctrl+f1..ctrl+f12, or '' if all taken."""
        used = {m.get('hotkey', '').strip().lower() for m in self._macros}
        for i in range(1, 13):
            hk = f'ctrl+f{i}'
            if hk not in used:
                return hk
        return ''

    def save(self, recorder: MacroRecorder, name: str, hotkey: str) -> dict:
        """Save a recording to disk. Returns the metadata dict.

        If the library is already at _MAX_SAVED_MACROS, the oldest macro
        (by saved_at) is deleted first to make room.
        """
        if len(self._macros) >= _MAX_SAVED_MACROS:
            oldest = min(self._macros, key=lambda m: m.get('saved_at', ''))
            self.delete(oldest['id'])

        mid      = uuid.uuid4().hex[:8]
        saved_at = datetime.now().replace(microsecond=0).isoformat()
        data = {
            'version':     1,
            'id':          mid,
            'name':        name,
            'hotkey':      hotkey,
            'event_count': recorder.event_count,
            'duration':    round(recorder.duration, 4),
            'saved_at':    saved_at,
            'events':      recorder._events,
        }
        self._write(mid, data)
        meta = {k: v for k, v in data.items() if k != 'events'}
        # Insert sorted by saved_at
        self._macros.append(meta)
        self._macros.sort(key=lambda m: m['saved_at'])
        return meta

    def delete(self, mid: str) -> None:
        """Delete a macro from disk and memory."""
        path = self._folder / f'{mid}.json'
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        self._macros = [m for m in self._macros if m['id'] != mid]

    def rename(self, mid: str, name: str) -> None:
        """Update name in memory and on disk."""
        for m in self._macros:
            if m['id'] == mid:
                m['name'] = name
                break
        self._update_file(mid, {'name': name})

    def assign_hotkey(self, mid: str, hotkey: str) -> None:
        """Assign hotkey. Clears it from any other macro that had it first."""
        hk_norm = hotkey.strip().lower()
        # Clear hotkey from any other macro that currently holds it
        if hk_norm:
            for m in self._macros:
                if m['id'] != mid and m.get('hotkey', '').strip().lower() == hk_norm:
                    m['hotkey'] = ''
                    self._update_file(m['id'], {'hotkey': ''})
        # Assign to target macro
        for m in self._macros:
            if m['id'] == mid:
                m['hotkey'] = hotkey
                break
        self._update_file(mid, {'hotkey': hotkey})

    def load_recorder(self, mid: str) -> MacroRecorder:
        """Load events from disk into a fresh MacroRecorder."""
        path = self._folder / f'{mid}.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        rec = MacroRecorder()
        rec._events = data['events']
        return rec

    # ── Internal ─────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Glob all *.json files, parse each, skip broken files, sort by saved_at."""
        self._macros = []
        for path in self._folder.glob('*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if 'id' not in data:
                    continue
                meta = {k: v for k, v in data.items() if k != 'events'}
                # Ensure expected fields exist
                meta.setdefault('hotkey', '')
                meta.setdefault('name', path.stem)
                meta.setdefault('event_count', 0)
                meta.setdefault('duration', 0.0)
                meta.setdefault('saved_at', '')
                self._macros.append(meta)
            except Exception:
                pass   # skip broken files silently
        self._macros.sort(key=lambda m: m.get('saved_at', ''))

    def _write(self, mid: str, data: dict) -> None:
        """Atomically write the full macro JSON file."""
        path = self._folder / f'{mid}.json'
        tmp  = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
        tmp.replace(path)

    def _update_file(self, mid: str, updates: dict) -> None:
        """Patch an existing JSON file with the given updates dict."""
        path = self._folder / f'{mid}.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return
        data.update(updates)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
        tmp.replace(path)
