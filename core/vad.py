import numpy as np
import onnxruntime as ort
from pathlib import Path

CHUNK_SAMPLES = 512   # Silero VAD v4 requires exactly 512 samples at 16kHz
SAMPLE_RATE = 16000
MS_PER_CHUNK = int(CHUNK_SAMPLES / SAMPLE_RATE * 1000)  # 32ms


class SileroVAD:
    """
    Silero VAD v4 via ONNX. Used as a safety auto-stop:
    if the user forgets to press the whisper hotkey again,
    recording stops after `safety_silence_s` seconds of silence.
    """

    def __init__(self, onnx_path: Path, speech_threshold: float = 0.5,
                 safety_silence_s: float = 60.0):
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(onnx_path),
            sess_options=opts,
            providers=['CPUExecutionProvider'],
        )
        self._threshold = speech_threshold
        self._silence_chunks_limit = int(safety_silence_s * 1000 / MS_PER_CHUNK)
        self._reset_state()
        self._on_safety_stop = None

    def set_safety_stop_callback(self, cb):
        self._on_safety_stop = cb

    def _reset_state(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._silence_count = 0
        self._speech_detected = False

    def reset(self):
        self._reset_state()

    def process_chunk(self, chunk: np.ndarray):
        """Feed a 512-sample 16kHz float32 chunk. Call only while recording."""
        if len(chunk) != CHUNK_SAMPLES:
            return

        x = chunk[np.newaxis, :].astype(np.float32)
        sr = np.array(SAMPLE_RATE, dtype=np.int64)

        try:
            out, state_n = self._session.run(
                None,
                {'input': x, 'state': self._state, 'sr': sr},
            )
            self._state = state_n
            prob = float(out[0][0])
        except Exception:
            return

        if prob >= self._threshold:
            self._speech_detected = True
            self._silence_count = 0
        else:
            if self._speech_detected:
                self._silence_count += 1
                if self._silence_count >= self._silence_chunks_limit:
                    if self._on_safety_stop:
                        self._on_safety_stop()
                    self._reset_state()
