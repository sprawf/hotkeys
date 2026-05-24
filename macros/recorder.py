"""
Macro Recorder & Replayer
Records mouse + keyboard events with precise timestamps, replays them exactly.

Completely standalone — no dependency on main app.
"""

import json
import threading
import time
from pathlib import Path

from pynput import mouse as _mouse, keyboard as _keyboard
from pynput.mouse import Button
from pynput.keyboard import Key

# ── Constants ─────────────────────────────────────────────────────────────────

# Minimum mouse movement delta to record (filters micro-jitter)
_MIN_MOUSE_DELTA = 5

# Keys that are NEVER recorded — they are reserved for stopping
_STOP_KEYS = {Key.esc, Key.delete}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _key_to_str(key) -> str:
    """Serialise a pynput key to a JSON-safe string."""
    try:
        if key.char is not None:
            return key.char
    except AttributeError:
        pass
    return str(key)   # e.g. "Key.ctrl_l"


def _str_to_key(s: str):
    """Deserialise a key string back to a pynput key."""
    if len(s) == 1:
        return s
    # Strip "Key." prefix if present, then look up in pynput
    name = s.replace('Key.', '')
    try:
        return getattr(Key, name)
    except AttributeError:
        return s


def _release_all(kc, mc) -> None:
    """Release every modifier key and mouse button — called on stop/done."""
    for key in (
        Key.ctrl, Key.ctrl_l, Key.ctrl_r,
        Key.alt,  Key.alt_l,  Key.alt_r,
        Key.shift, Key.shift_l, Key.shift_r,
        Key.cmd,  Key.cmd_l,  Key.cmd_r,
    ):
        try:
            kc.release(key)
        except Exception:
            pass
    for btn in (Button.left, Button.right, Button.middle):
        try:
            mc.release(btn)
        except Exception:
            pass


# ── Recorder ──────────────────────────────────────────────────────────────────

class MacroRecorder:
    """
    Records mouse + keyboard activity with timestamps and replays it.

    Usage
    -----
    rec = MacroRecorder()

    rec.start_recording()
    ... user does stuff ...
    rec.stop_recording()

    rec.start_playback(on_done=lambda: print('done'))
    # or:
    rec.force_stop()   # abort playback mid-run

    rec.save('my_macro.json')
    rec.load('my_macro.json')
    """

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._recording  = False
        self._playing    = False
        self._stop_event = threading.Event()
        self._start_time: float = 0.0
        self._lock       = threading.Lock()

        self._mouse_listener:    _mouse.Listener    | None = None
        self._keyboard_listener: _keyboard.Listener | None = None

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def duration(self) -> float:
        return self._events[-1]['t'] if self._events else 0.0

    # ── Recording ─────────────────────────────────────────────────────────────

    def start_recording(self) -> None:
        """Begin capturing mouse + keyboard events."""
        if self._recording or self._playing:
            return
        self._events    = []
        self._recording = True
        self._start_time = time.perf_counter()

        self._mouse_listener = _mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self._keyboard_listener = _keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self._mouse_listener.start()
        self._keyboard_listener.start()

    def stop_recording(self) -> None:
        """Stop capturing. Events are preserved for playback/save."""
        self._recording = False
        if self._mouse_listener:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None
        if self._keyboard_listener:
            try:
                self._keyboard_listener.stop()
            except Exception:
                pass
            self._keyboard_listener = None
        self._trim_stop_hotkey()

    # F1 is the macro record/play hotkey key — never record it so replaying
    # the macro never re-triggers the macro control hotkey.
    # Shift keys are stripped only from the trailing window (the stop-press).
    _F1_KEY       = 'Key.f1'
    _SHIFT_KEYS   = {'Key.shift', 'Key.shift_l', 'Key.shift_r'}

    def _trim_stop_hotkey(self, window_s: float = 0.4) -> None:
        """Strip macro-control key events so replay never re-triggers the hotkey.

        * Key.f1  — removed from the ENTIRE recording (Shift+F1 is the macro
          hotkey; replaying it mid-macro would cause chaos).
        * Shift keys — removed only from the trailing ``window_s`` seconds
          (the actual stop-press), so Shift held during normal typing is kept.
        """
        if not self._events:
            return
        end_t  = self._events[-1]['t']
        cutoff = end_t - window_s
        self._events = [
            e for e in self._events
            if not (
                e['type'] in ('key_press', 'key_release')
                and (
                    e.get('key') == self._F1_KEY                              # ALL F1 events
                    or (e.get('key') in self._SHIFT_KEYS and e['t'] >= cutoff)  # trailing shift only
                )
            )
        ]

    def _ts(self) -> float:
        return time.perf_counter() - self._start_time

    def _on_move(self, x: int, y: int) -> None:
        if not self._recording:
            return
        with self._lock:
            # Only record if moved meaningfully from the last recorded position
            last = next((e for e in reversed(self._events)
                         if e['type'] == 'mouse_move'), None)
            if last and abs(x - last['x']) < _MIN_MOUSE_DELTA \
                    and abs(y - last['y']) < _MIN_MOUSE_DELTA:
                return
            self._events.append({'type': 'mouse_move', 'x': x, 'y': y, 't': self._ts()})

    def _on_click(self, x: int, y: int, button: Button, pressed: bool) -> None:
        if not self._recording:
            return
        with self._lock:
            self._events.append({
                'type':    'mouse_click',
                'x':       x,
                'y':       y,
                'button':  button.name,
                'pressed': pressed,
                't':       self._ts(),
            })

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if not self._recording:
            return
        with self._lock:
            self._events.append({
                'type': 'mouse_scroll',
                'x': x, 'y': y,
                'dx': dx, 'dy': dy,
                't': self._ts(),
            })

    def _on_key_press(self, key) -> None:
        if not self._recording:
            return
        if key in _STOP_KEYS:
            return           # never record stop keys
        with self._lock:
            self._events.append({'type': 'key_press', 'key': _key_to_str(key), 't': self._ts()})

    def _on_key_release(self, key) -> None:
        if not self._recording:
            return
        if key in _STOP_KEYS:
            return
        with self._lock:
            self._events.append({'type': 'key_release', 'key': _key_to_str(key), 't': self._ts()})

    # ── Playback ──────────────────────────────────────────────────────────────

    def start_playback(self, on_done=None, on_stop=None) -> None:
        """
        Replay the recording in a background thread.

        on_done  — called when playback completes naturally.
        on_stop  — called if playback was force-stopped before completion.
        Both callbacks are optional and called from the playback thread.
        """
        if self._playing or self._recording:
            return
        if not self._events:
            return
        self._playing = True
        self._stop_event.clear()

        t = threading.Thread(
            target=self._playback_worker,
            args=(on_done, on_stop),
            daemon=True,
        )
        t.start()

    def _playback_worker(self, on_done, on_stop) -> None:
        mc = _mouse.Controller()
        kc = _keyboard.Controller()
        start = time.perf_counter()
        stopped_early = False

        try:
            for event in self._events:
                if self._stop_event.is_set():
                    stopped_early = True
                    break

                # Absolute-timestamp wait — self-correcting, no drift
                wait = (start + event['t']) - time.perf_counter()
                if wait > 0:
                    if self._stop_event.wait(timeout=wait):
                        stopped_early = True
                        break

                if self._stop_event.is_set():
                    stopped_early = True
                    break

                self._replay_event(event, mc, kc)

        finally:
            self._playing = False
            _release_all(kc, mc)
            if stopped_early:
                if on_stop:
                    try:
                        on_stop()
                    except Exception:
                        pass
            else:
                if on_done:
                    try:
                        on_done()
                    except Exception:
                        pass

    def _replay_event(self, event: dict, mc, kc) -> None:
        t = event['type']
        try:
            if t == 'mouse_move':
                mc.position = (event['x'], event['y'])

            elif t == 'mouse_click':
                btn_name = event['button']
                btn = (Button.left   if btn_name == 'left'   else
                       Button.right  if btn_name == 'right'  else
                       Button.middle)
                mc.position = (event['x'], event['y'])
                if event['pressed']:
                    mc.press(btn)
                else:
                    mc.release(btn)

            elif t == 'mouse_scroll':
                mc.scroll(event['dx'], event['dy'])

            elif t == 'key_press':
                kc.press(_str_to_key(event['key']))

            elif t == 'key_release':
                kc.release(_str_to_key(event['key']))

        except Exception:
            pass   # single bad event never crashes the whole replay

    # ── Force stop ────────────────────────────────────────────────────────────

    def force_stop(self) -> None:
        """
        Immediately abort recording or playback.
        Safe to call from any thread, including the Esc/Del hotkey handler.
        """
        # Signal playback loop to exit mid-wait
        self._stop_event.set()

        # Stop any active listeners
        self._recording = False
        if self._mouse_listener:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None
        if self._keyboard_listener:
            try:
                self._keyboard_listener.stop()
            except Exception:
                pass
            self._keyboard_listener = None

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        """Save the current recording to a JSON file."""
        data = {
            'version':     1,
            'event_count': len(self._events),
            'duration':    round(self.duration, 4),
            'events':      self._events,
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)

    def load(self, path: str | Path) -> None:
        """Load a recording from a JSON file."""
        data = json.loads(Path(path).read_text())
        self._events = data['events']

    def clear(self) -> None:
        """Discard the current recording."""
        self._events = []
