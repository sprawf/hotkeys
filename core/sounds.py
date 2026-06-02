import os
import sys
import tempfile
import threading
import numpy as np

# Notification sounds bypass sounddevice/PortAudio on Windows. Reason:
# we call play_stop() immediately after sd.InputStream.close() finishes,
# but PortAudio's device handle release is async at the C layer — the
# back-to-back open/close races and crashes the process with a Win32 SEH
# access violation (see crash.log entries from Jun 2026). Switching to
# winsound.PlaySound (Win32 PlaySound API) uses an entirely separate
# audio path that doesn't share state with PortAudio, so the input
# device can still be tearing down while the sound plays. No more crash.
import wave as _wave

SAMPLE_RATE = 44100

# Cache of generated WAV files, key=sound name, value=path on disk.
_WAV_CACHE: dict[str, str] = {}
_WAV_LOCK = threading.Lock()


def _vibration(freq: float, duration: float, amplitude: float = 0.15) -> np.ndarray:
    """Soft buzz/vibration feel, low sine with rapid tremolo and smooth fade-in/out."""
    t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
    # Tremolo: amplitude modulated at ~28 Hz gives a tactile vibration feel
    tremolo = 0.5 + 0.5 * np.sin(2 * np.pi * 28 * t)
    # Smooth fade-in and fade-out envelope
    fade = int(0.03 * SAMPLE_RATE)
    envelope = np.ones(len(t), dtype=np.float32)
    envelope[:fade]  = np.linspace(0, 1, fade)
    envelope[-fade:] = np.linspace(1, 0, fade)
    wave = amplitude * envelope * tremolo * np.sin(2 * np.pi * freq * t)
    return wave.astype(np.float32)


def _wave_to_wav(wave: np.ndarray) -> bytes:
    """Encode a float32 wave (-1.0…1.0) as 16-bit PCM WAV bytes."""
    int16 = np.clip(wave, -1.0, 1.0)
    int16 = (int16 * 32767.0).astype(np.int16)
    import io
    buf = io.BytesIO()
    with _wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(int16.tobytes())
    return buf.getvalue()


def _cache_wav(name: str, wave: np.ndarray) -> str:
    """Write the wave to a per-app temp file once and return the path.
    winsound.PlaySound needs a filename, not a buffer."""
    with _WAV_LOCK:
        cached = _WAV_CACHE.get(name)
        if cached and os.path.exists(cached):
            return cached
        tmp_dir = os.path.join(tempfile.gettempdir(), 'hotkeys_sounds')
        os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, f'{name}.wav')
        try:
            with open(path, 'wb') as f:
                f.write(_wave_to_wav(wave))
            _WAV_CACHE[name] = path
            return path
        except Exception:
            return ''


def _play_async(wave: np.ndarray, name: str = 'sfx'):
    """Play a generated waveform via Win32 PlaySound (asynchronous)
    on Windows, fall back to sounddevice elsewhere. The fallback path
    is the one with the historical PortAudio crash, only macOS/Linux
    hit it and they don't share Windows' device-handle race."""
    if sys.platform == 'win32':
        path = _cache_wav(name, wave)
        if not path:
            return
        try:
            import winsound
            # SND_ASYNC: non-blocking. SND_NODEFAULT: silence if the file
            # is missing instead of playing the OS default ding. SND_FILENAME
            # tells Win32 the arg is a file path.
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except Exception:
            pass
        return
    # Non-Windows fallback (kept for parity, mac/linux dev environments).
    def _run():
        try:
            import sounddevice as sd
            sd.play(wave, samplerate=SAMPLE_RATE, blocking=True)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def play_start():
    """Soft double-pulse vibration, signals recording has started."""
    pulse = _vibration(120.0, 0.10, amplitude=0.28)
    silence = np.zeros(int(SAMPLE_RATE * 0.06), dtype=np.float32)
    _play_async(np.concatenate([pulse, silence, pulse]), name='start')


def play_stop():
    """Single longer soft vibration, signals recording has ended."""
    pulse = _vibration(90.0, 0.18, amplitude=0.24)
    _play_async(pulse, name='stop')


def play_flip(reverse: bool = False) -> None:
    """Crisp paper page-flip whoosh.

    forward (default), high → mid frequency sweep, feels like turning a page ahead.
    reverse          , mid → high frequency sweep, feels like going back.
    """
    duration = 0.11
    n = int(SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n, endpoint=False)

    # Frequency chirp: linear sweep from f_start → f_end
    f_start, f_end = (1800.0, 600.0) if not reverse else (600.0, 1800.0)
    freq = np.linspace(f_start, f_end, n)
    phase = np.cumsum(2 * np.pi * freq / SAMPLE_RATE)
    wave = np.sin(phase).astype(np.float32)

    # Envelope: sharp attack, exponential decay (feels like a quick flick)
    attack = int(0.008 * SAMPLE_RATE)
    envelope = np.exp(-t * 22)
    envelope[:attack] *= np.linspace(0, 1, attack)
    wave *= (envelope * 0.32).astype(np.float32)

    # Thin high-frequency rustle layered on top (paper texture)
    rustle_freq = np.linspace(3400.0, 1200.0, n) if not reverse else np.linspace(1200.0, 3400.0, n)
    rustle_phase = np.cumsum(2 * np.pi * rustle_freq / SAMPLE_RATE)
    rustle = (np.sin(rustle_phase) * envelope * 0.13).astype(np.float32)

    _play_async(wave + rustle, name=('flip_rev' if reverse else 'flip_fwd'))
