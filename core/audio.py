import logging
import threading
import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

SAMPLE_RATE    = 16000
BLOCKSIZE      = 512   # matches Silero VAD chunk size
_INTERIM_EVERY = SAMPLE_RATE * 4       # emit interim every 4 s of new audio
_MAX_RECORD_S  = 300                   # hard cap: 5 minutes of recording


class AudioCapture:
    def __init__(self, on_chunk, on_utterance_ready, cfg, on_interim=None):
        self._on_chunk           = on_chunk
        self._on_utterance_ready = on_utterance_ready
        self._on_interim         = on_interim
        self._cfg    = cfg
        self._stream = None
        self._lock   = threading.Lock()
        self._recording      = False
        self._buffer         = []
        self._interim_last_n = 0
        self._db             = -60.0

    @property
    def db(self):
        return self._db

    def _open_stream(self):
        try:
            self._stream = sd.InputStream(
                device=self._cfg.audio.input_device_index,
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype='float32',
                blocksize=BLOCKSIZE,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as e:
            logger.error(f'Audio stream error: {e}')
            self._stream = None
            raise

    def _callback(self, indata, frames, time_info, status):
        try:
            chunk = np.clip(indata[:, 0].copy(), -1.0, 1.0)
            rms = np.sqrt(np.mean(chunk ** 2) + 1e-9)
            self._db = float(20 * np.log10(rms))
            should_interim  = False
            buf_snapshot    = None
            force_stop      = False
            with self._lock:
                if self._recording:
                    self._buffer.append(chunk)
                    total_n = sum(len(b) for b in self._buffer)
                    # Hard cap: auto-stop at 5 minutes to prevent memory exhaustion
                    if total_n >= _MAX_RECORD_S * SAMPLE_RATE:
                        self._recording = False
                        force_stop = True
                    elif self._on_interim:
                        if total_n - self._interim_last_n >= _INTERIM_EVERY:
                            self._interim_last_n = total_n
                            should_interim = True
                            buf_snapshot   = list(self._buffer)
            self._on_chunk(chunk)
            if force_stop:
                logger.warning('Max recording duration reached — auto-stopping.')
                threading.Thread(target=self.stop_recording, daemon=True).start()
            elif should_interim and buf_snapshot:
                audio_snap = np.concatenate(buf_snapshot)
                threading.Thread(
                    target=lambda a=audio_snap: self._on_interim(a),
                    daemon=True,
                ).start()
        except Exception as e:
            logger.error(f'Audio callback error: {e}')

    def start_recording(self):
        with self._lock:
            self._buffer         = []
            self._interim_last_n = 0
            self._recording      = True
        if self._stream is None or not self._stream.active:
            try:
                self._open_stream()
            except Exception:
                with self._lock:
                    self._recording = False
                raise

    def stop_recording(self):
        with self._lock:
            self._recording = False
            audio = np.concatenate(self._buffer) if self._buffer else np.zeros(0, dtype=np.float32)
            self._buffer = []
        if len(audio) > 0:
            self._on_utterance_ready(audio)

    def cancel_recording(self):
        with self._lock:
            self._recording = False
            self._buffer    = []

    def start(self):
        if self._stream is None:
            self._open_stream()

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
