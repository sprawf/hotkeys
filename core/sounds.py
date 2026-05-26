import threading
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 44100


def _vibration(freq: float, duration: float, amplitude: float = 0.15) -> np.ndarray:
    """Soft buzz/vibration feel — low sine with rapid tremolo and smooth fade-in/out."""
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


def _play_async(wave: np.ndarray):
    def _run():
        try:
            sd.play(wave, samplerate=SAMPLE_RATE, blocking=True)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def play_start():
    """Soft double-pulse vibration — signals recording has started."""
    pulse = _vibration(120.0, 0.10, amplitude=0.28)
    silence = np.zeros(int(SAMPLE_RATE * 0.06), dtype=np.float32)
    _play_async(np.concatenate([pulse, silence, pulse]))


def play_stop():
    """Single longer soft vibration — signals recording has ended."""
    pulse = _vibration(90.0, 0.18, amplitude=0.24)
    _play_async(pulse)


def play_flip(reverse: bool = False) -> None:
    """Crisp paper page-flip whoosh.

    forward (default) — high → mid frequency sweep, feels like turning a page ahead.
    reverse           — mid → high frequency sweep, feels like going back.
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

    _play_async(wave + rustle)
