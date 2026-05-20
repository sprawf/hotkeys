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
