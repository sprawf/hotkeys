"""
faster-whisper transcription pipeline.
Adapted from KaiWhisper, replaces config.py imports with storage module.
"""
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# ── Audio helpers ─────────────────────────────────────────────────────────────

_SR = 16000
_CHUNK_S     = 28     # Whisper trained on 30s windows; 28s for safety
_OVERLAP_S   = 2
_MIN_CHUNK_S = 0.5



def _denoise(audio: np.ndarray) -> np.ndarray:
    if len(audio) < _SR * 0.5:
        return audio
    try:
        import noisereduce as nr
        return nr.reduce_noise(y=audio, sr=_SR, stationary=True,
                               prop_decrease=0.75).astype(np.float32)
    except Exception:
        return audio


# Noise floor above which an audio buffer is considered "noisy enough that
# denoising is worth its CPU cost." Calibrated against:
#   • Quiet home office, decent mic: floor ≈ 0.001-0.003 (skip denoise)
#   • Coffee shop / noisy fan / cheap mic: floor ≈ 0.01-0.05 (denoise helps)
#   • Live music nearby: floor > 0.05 (denoise essential)
# Picked conservatively so silent rooms never spend CPU on a no-op.
_NOISE_FLOOR_THRESHOLD = 0.008


def _should_denoise(audio: np.ndarray) -> bool:
    """Estimate whether `audio` would benefit from spectral-gating noise
    reduction. Returns True for buffers with a meaningful background-noise
    floor (cafe, office hum, fan noise, traffic) and False for clean
    recordings where running noisereduce is just wasted CPU.

    Robust to "the user started talking immediately" by using a 10th-
    percentile estimate of per-chunk RMS, the quietest 10% of the
    recording is almost always background, never speech.
    """
    if len(audio) < _SR * 0.5:
        return False
    try:
        # Split into 100 ms chunks and compute RMS per chunk.
        chunk_n = max(1, _SR // 10)
        rms_chunks = []
        for i in range(0, len(audio) - chunk_n + 1, chunk_n):
            seg = audio[i:i + chunk_n]
            rms_chunks.append(float(np.sqrt(np.mean(seg ** 2) + 1e-12)))
        if not rms_chunks:
            return False
        # 10th percentile = floor (skips the chatty 90%).
        floor = float(np.percentile(rms_chunks, 10))
        return floor >= _NOISE_FLOOR_THRESHOLD
    except Exception:
        return False


def _split_audio(audio: np.ndarray) -> list:
    if len(audio) / _SR <= _CHUNK_S:
        return [audio]
    chunk_n = int(_CHUNK_S * _SR)
    step_n  = int((_CHUNK_S - _OVERLAP_S) * _SR)
    min_n   = int(_MIN_CHUNK_S * _SR)
    chunks, start = [], 0
    while start < len(audio):
        chunk = audio[start:start + chunk_n]
        if len(chunk) >= min_n:
            chunks.append(chunk)
        start += step_n
    return chunks or [audio]


def _stitch_texts(texts: list) -> str:
    if not texts:
        return ''
    if len(texts) == 1:
        return texts[0].strip()
    result = texts[0].strip()
    for nxt in texts[1:]:
        nxt = nxt.strip()
        if not nxt:
            continue
        rw, nw = result.split(), nxt.split()
        overlap = 0
        for n in range(min(len(rw), len(nw), 20), 0, -1):
            if rw[-n:] == nw[:n]:
                overlap = n
                break
        result = result + ' ' + (' '.join(nw[overlap:]) if overlap else nxt)
    return result.strip()


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(cfg, mode_prompt: str) -> str:
    parts = []
    if mode_prompt:
        parts.append(mode_prompt)
    vocab_raw = getattr(cfg.transcription, 'custom_vocabulary', '').strip()
    if vocab_raw:
        terms = [t.strip() for t in vocab_raw.splitlines() if t.strip()]
        if terms:
            parts.append(', '.join(terms) + '.')
    return ' '.join(parts)


# ── Transcriber ────────────────────────────────────────────────────────────────

class Transcriber:
    def __init__(self, cfg, on_result, on_status, models_dir: str,
                 log_file: str = '', on_preview=None):
        """
        cfg         : _Namespace wrapping config['whisper']
        on_result   : callable(text, language, duration_s)
        on_status   : callable(status_str)
        models_dir  : path to folder containing base/, small/, large-v3-turbo/
        log_file    : path to log file for traceback dumps (optional)
        on_preview  : callable(text) for interim transcription preview
        """
        self._cfg        = cfg
        self._on_result  = on_result
        self._on_status  = on_status
        self._models_dir = Path(models_dir)
        self._log_file   = log_file
        self._on_preview = on_preview

        self._model      = None
        self._model_ready   = threading.Event()
        self._queue         = queue.Queue()
        self._cancelled     = threading.Event()
        # Per-job generation counter. Bumped on every submit() AND
        # cancel(). The worker tags each in-flight job with the gen it
        # observed at start; on completion it re-checks self._gen and
        # drops the result if it has moved. Without this, a job that
        # was cancelled mid-_transcribe_hybrid could deliver as the
        # next job's result (the global event gets cleared by submit()).
        self._gen = 0
        self._gen_lock = threading.Lock()

        self._preview_model      = None
        self._preview_model_lock = threading.Lock()   # guards concurrent use of _preview_model
        self._preview_queue = queue.Queue(maxsize=1)

        threading.Thread(target=self._worker,          daemon=True).start()
        threading.Thread(target=self._preview_worker,  daemon=True).start()
        threading.Thread(target=self._prewarm,         daemon=True).start()
        threading.Thread(target=self._prewarm_preview, daemon=True).start()

    def _resolve_device(self):
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return 'cuda', 'float16'
        except Exception:
            pass
        return 'cpu', 'int8'

    def _prewarm(self):
        self._on_status('loading')
        try:
            from faster_whisper import WhisperModel

            cfg_device  = self._cfg.model.device
            cfg_compute = self._cfg.model.compute_type

            if cfg_device == 'auto' or cfg_compute == 'auto':
                device, compute_type = self._resolve_device()
            else:
                device, compute_type = cfg_device, cfg_compute

            model_name = (getattr(self._cfg.model, 'gpu_model', 'large-v3-turbo')
                          if device == 'cuda'
                          else getattr(self._cfg.model, 'cpu_model', 'small'))
            model_path = self._models_dir / model_name

            # Graceful fallback: if requested model not bundled, use base
            if not model_path.exists():
                logger.warning(f'Requested model {model_name!r} not found, falling back to base')
                model_path = self._models_dir / 'base'
                device, compute_type = 'cpu', 'int8'

            logger.info(f'Loading Whisper model: {model_path.name}  device={device}  compute={compute_type}')
            cpu_threads = os.cpu_count() or 4
            self._model = WhisperModel(
                str(model_path),
                device=device,
                compute_type=compute_type,
                num_workers=2,
                cpu_threads=cpu_threads if device == 'cpu' else 0,
            )
            self._model_ready.set()
            logger.info(f'Whisper model ready ✓  ({model_path.name})')
            self._on_status('ready')
            # JIT-warm CTranslate2's CPU kernels by transcribing 1 second of
            # silence. First real Ctrl+Enter then runs warm, saving the
            # ~400-800 ms of one-time kernel compilation on a cold call.
            # Runs in this same thread, after status='ready', so the user
            # sees the "Hotkeys is ready" notification BEFORE we start
            # consuming CPU on the warmup. Failure is non-fatal, at worst
            # the user pays the warmup cost on their first real call.
            try:
                t0 = time.perf_counter()
                _warmup = np.zeros(int(_SR * 1.0), dtype=np.float32)
                segments, _ = self._model.transcribe(
                    _warmup, beam_size=1, vad_filter=False,
                    condition_on_previous_text=False,
                )
                _ = list(segments)
                logger.info(
                    f'Whisper model JIT-warmed in {(time.perf_counter()-t0)*1000:.0f}ms'
                )
            except Exception as e:
                logger.warning(f'Whisper warmup skipped: {e}')
            self._on_status('jit_done')
            # Pre-warm the cloud TLS connection in a separate background
            # thread so it overlaps with anything else happening at startup.
            # By the time the user presses Ctrl+Enter, the TLS handshake to
            # api.groq.com has already happened, the first real cloud call
            # only pays the audio upload + inference time.
            def _cloud_then_signal():
                self._prewarm_cloud()
                self._on_status('cloud_warm')
            threading.Thread(target=_cloud_then_signal, daemon=True).start()
        except Exception:
            logger.exception('Whisper model failed to load')
            self._on_status('error')
            self._dump_traceback()

    def _prewarm_preview(self):
        try:
            from faster_whisper import WhisperModel
            path = self._models_dir / 'base'
            if not path.exists():
                return
            self._preview_model = WhisperModel(
                str(path), device='cpu', compute_type='int8',
                num_workers=1, cpu_threads=max(2, (os.cpu_count() or 4) // 4),
            )
        except Exception:
            pass

    def _dump_traceback(self):
        if not self._log_file:
            return
        import traceback
        try:
            with open(self._log_file, 'a', encoding='utf-8') as f:
                traceback.print_exc(file=f)
        except Exception:
            pass

    def submit_preview(self, audio: np.ndarray):
        if self._preview_model is None or self._on_preview is None:
            return
        while not self._preview_queue.empty():
            try:
                self._preview_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._preview_queue.put_nowait(audio)
        except queue.Full:
            pass

    def _preview_worker(self):
        while True:
            audio = self._preview_queue.get()
            if audio is None:
                break
            if self._preview_model is None or self._on_preview is None:
                continue
            try:
                lang = self._cfg.transcription.language or None
                with self._preview_model_lock:
                    segments, info = self._preview_model.transcribe(
                        audio,
                        language=lang,
                        beam_size=1,
                        temperature=0.0,
                        condition_on_previous_text=False,
                        vad_filter=True,
                        no_speech_threshold=0.6,
                    )
                text = ''.join(s.text for s in segments).strip()
                if text and not self._cancelled.is_set():
                    self._on_preview(text)
            except Exception as e:
                logger.warning(f'Preview transcription error: {e}')

    def transcribe_for_notes(self, audio) -> str:
        """Synchronous transcription using the base preview model, for Quick Notes.
        Blocks until complete. Safe to call from any thread."""
        if self._preview_model is None:
            # Wait up to 8 s for the model to finish loading
            for _ in range(80):
                import time as _time
                _time.sleep(0.1)
                if self._preview_model is not None:
                    break
            if self._preview_model is None:
                return ''
        try:
            lang = self._cfg.transcription.language or None
            with self._preview_model_lock:
                segments, _ = self._preview_model.transcribe(
                    audio,
                    language=lang,
                    beam_size=1,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    vad_filter=True,
                    no_speech_threshold=0.6,
                )
                return ''.join(s.text for s in segments).strip()
        except Exception as e:
            logger.warning(f'Notes transcription error: {e}')
            return ''

    def submit(self, audio: np.ndarray):
        self._cancelled.clear()
        with self._gen_lock:
            self._gen += 1
            job_gen = self._gen
        # Queue the (gen, audio) pair so the worker can verify on
        # completion that this job is still the "current" one.
        self._queue.put((job_gen, audio))

    def cancel(self):
        self._cancelled.set()
        # Bump gen so any in-flight job, when it finishes, sees the
        # mismatch and drops its result. Without this, finishing job A
        # after cancel and the user re-submitting job B would deliver
        # A's text as if it were B's (the _cancelled flag was cleared
        # by submit(B)).
        with self._gen_lock:
            self._gen += 1
        for q in (self._queue, self._preview_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def _worker(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            # Tolerate the historical shape: an old caller might still
            # put a bare ndarray on the queue. Treat that as gen=0
            # (always stale; will drop).
            if isinstance(item, tuple) and len(item) == 2:
                job_gen, audio = item
            else:
                job_gen, audio = 0, item
            if self._cancelled.is_set() or job_gen != self._gen:
                continue
            ready = self._model_ready.wait(timeout=60)
            if not ready or self._model is None:
                logger.error('Whisper model not ready after 60s, reporting error.')
                self._on_status('error')
                continue
            self._on_status('transcribing')
            try:
                text, language, duration = self._transcribe_hybrid(audio)
                # Recheck after transcription: a cancel that happened
                # WHILE we were in _transcribe_hybrid would set
                # _cancelled, but a subsequent submit() would clear
                # it and then the result would deliver as if it were
                # the new job. The gen check catches this.
                if (not self._cancelled.is_set()
                        and job_gen == self._gen):
                    self._on_result(text, language, duration)
            except Exception:
                if not self._cancelled.is_set() and job_gen == self._gen:
                    self._on_status('error')
                self._dump_traceback()
            finally:
                if not self._cancelled.is_set() and job_gen == self._gen:
                    self._on_status('idle')

    # ── Hybrid cloud-first transcription ──────────────────────────────────────
    #
    # Tries Groq's hosted Whisper (`whisper-large-v3-turbo`, ~450 ms for a 2-s
    # clip on a typical home connection) first. If cloud is disabled in
    # settings, offline, rate-limited, or just slow, falls back transparently
    # to the local CPU pipeline. The caller gets exactly the same return
    # tuple either way, `(text, language, duration_s)`.
    #
    # Feature-isolation note: this method never runs at module import time,
    # never mutates process-global state, and only affects the dictation
    # path. Refine / Ask / Whiteboard / Library are completely untouched.

    # Last-success cache, purely diagnostic, used for logging
    _CLOUD_RECENT_OK: bool = True
    # Friendly message describing the most recent cloud transcription
    # failure. main.py reads + clears this after each transcription so
    # it can show the user a one-shot pill explaining the fallback.
    _cloud_last_error: str | None = None
    # Shared requests.Session keeps the TCP/TLS connection to api.groq.com
    # open between calls. First call still pays the handshake cost; we
    # pre-warm it in a background thread at startup (see _prewarm_cloud).
    _cloud_session = None
    _cloud_session_lock = threading.Lock()

    @classmethod
    def _get_cloud_session(cls):
        with cls._cloud_session_lock:
            if cls._cloud_session is None:
                import requests
                cls._cloud_session = requests.Session()
            return cls._cloud_session

    def _prewarm_cloud(self) -> None:
        """Open and warm the TLS connection to api.groq.com so the first
        real cloud Ctrl+Enter doesn't pay the 400-700 ms handshake cost.
        Runs in a background thread at startup.  Failures are silent,
        worst case the user pays handshake on first call.
        """
        if not self._cloud_enabled():
            return
        try:
            import truststore
            truststore.inject_into_ssl()
        except Exception:
            pass
        try:
            # Tiny request that returns fast, 401 (no auth) is fine, we
            # only care that the TLS connection is established.
            sess = self._get_cloud_session()
            t0 = time.perf_counter()
            sess.get(
                'https://api.groq.com/openai/v1/models',
                timeout=10.0,
            )
            logger.info(
                f'Cloud TLS pre-warmed in {(time.perf_counter()-t0)*1000:.0f}ms'
            )
        except Exception as e:
            logger.info(f'Cloud TLS pre-warm skipped: {e!s:.80}')

    def _cloud_enabled(self) -> bool:
        """Read the user's preference; default True for fast UX."""
        try:
            audio_cfg = self._cfg.audio
            # `_Namespace` falls through to the underlying dict for missing
            # attrs, so the `getattr` default handles both cases.
            return bool(getattr(audio_cfg, 'cloud_enabled', True))
        except Exception:
            return True

    def _cloud_reachable(self, host: str = 'api.groq.com',
                         port: int = 443, timeout: float = 3.0) -> bool:
        """Fast non-blocking TCP probe to detect whether the cloud
        Whisper host is reachable RIGHT NOW. Returns True on successful
        connect, False on any failure (DNS resolution failure, network
        unreachable, refused, timeout).

        Used as a pre-flight before the much slower requests.Session.post
        call — without this, an offline / blocked session can hang the
        write phase indefinitely while the pill shows 'Transcribing…'.
        """
        import socket
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            try: s.close()
            except Exception: pass
            return True
        except Exception:
            return False

    @staticmethod
    def _audio_to_wav_bytes(audio: np.ndarray, sample_rate: int = _SR) -> bytes:
        """Encode a float32 numpy array as 16-bit PCM WAV bytes, the
        smallest format Groq accepts that doesn't require ffmpeg. Whisper
        models normalize to 16 kHz mono internally anyway, so we send the
        exact format the model expects."""
        import io, wave
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype('<i2').tobytes()
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm)
        return buf.getvalue()

    def _transcribe_via_groq(self, audio: np.ndarray, language: str | None,
                             timeout: float) -> tuple:
        """Send `audio` to Groq's Whisper endpoint and return
        `(text, language, duration_s)`. Raises on network/HTTP error so the
        hybrid orchestrator can decide to fall back.

        Rotates through bundled keys on rate-limit (HTTP 429) so a single
        key's daily cap doesn't break the experience.
        """
        # Lazy imports, neither truststore nor requests get pulled in until
        # the very first cloud call. Refine etc. already trigger them, but
        # this avoids polluting cold-start time if cloud is disabled.
        try:
            import truststore
            truststore.inject_into_ssl()
        except Exception:
            pass
        # Reuse the warmed session if pre-warm ran; create on demand otherwise.
        sess = self._get_cloud_session()

        # Bundled keys + any user-configured key, in priority order.
        keys: list[str] = []
        try:
            from _bundled_keys import GROQ, GROQ_2
            keys += [GROQ, GROQ_2]
        except Exception:
            pass
        try:
            extra = (self._cfg.providers.groq.api_key if hasattr(self._cfg, 'providers') else '') or ''
            if extra and extra not in keys:
                keys.insert(0, extra)
        except Exception:
            pass
        if not keys:
            raise RuntimeError('No Groq API key available for cloud transcription')

        wav_bytes = self._audio_to_wav_bytes(audio)
        duration  = len(audio) / float(_SR)

        last_err: Exception | None = None
        for idx, key in enumerate(keys):
            try:
                r = sess.post(
                    'https://api.groq.com/openai/v1/audio/transcriptions',
                    headers={'Authorization': f'Bearer {key}'},
                    files={'file': ('audio.wav', wav_bytes, 'audio/wav')},
                    data={
                        'model':           'whisper-large-v3-turbo',
                        'response_format': 'verbose_json',
                        **({'language': language} if language else {}),
                    },
                    timeout=timeout,
                )
            except Exception as e:
                # requests.Timeout subclasses ConnectionError; treat any
                # network issue as a fail-over candidate, but timeouts
                # specifically should NOT retry (already waited the full
                # budget).
                import requests as _r
                if isinstance(e, _r.exceptions.Timeout):
                    raise
                last_err = e
                continue
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    j = {}
                text = (j.get('text') if isinstance(j, dict) else '') or ''
                lang = (j.get('language') if isinstance(j, dict) else '') or 'unknown'
                return text.strip(), lang, duration
            if r.status_code == 429:
                logger.warning(
                    f'Groq Whisper key #{idx + 1} rate-limited, trying next key.'
                )
                last_err = RuntimeError(f'HTTP 429: {r.text[:120]}')
                continue
            # Any other status → record and try next key (rare, but safer)
            last_err = RuntimeError(f'HTTP {r.status_code}: {r.text[:160]}')
        raise last_err if last_err else RuntimeError('All Groq keys failed')

    # Audio-activity threshold below which we treat the recording as
    # "the mic captured nothing." Calibrated against the noise floor of a
    # working mic with the user not speaking (~0.003 peak, ~0.0008 mean)
    # vs a silent/missing mic (~0.000 peak, ~0.000 mean). The chosen
    # values give a comfortable margin in both directions.
    _SILENCE_PEAK_THRESHOLD: float = 0.01
    _SILENCE_MEAN_THRESHOLD: float = 0.0005

    # Audio-energy ceiling below which we treat a Whisper output as a
    # silence hallucination. Above the pure-silence threshold (mic was
    # capturing) but below the level a real voice produces. If Whisper
    # returned a known hallucination phrase AND the audio is in this
    # quiet-room band, we drop the text and surface "No speech detected".
    _QUIET_ROOM_PEAK_CEILING: float = 0.06
    _QUIET_ROOM_MEAN_CEILING: float = 0.006

    # Phrases Whisper reliably hallucinates from silence / ambient noise.
    # The lowercase + punctuation-stripped form is what we compare against.
    # Sourced from openai/whisper issue trackers; these are the offenders
    # the community has flagged repeatedly. Keep the set small and only
    # add things that have ZERO chance of being a real short utterance,
    # otherwise we'd drop legitimate "Thanks." replies.
    _HALLUCINATION_PHRASES = frozenset({
        'thank you',
        'thanks for watching',
        'thanks for watching!',
        'subscribe',
        'please subscribe',
        'like and subscribe',
        "i'll see you in the next video",
        'see you in the next video',
        'bye',
        'goodbye',
        '.',
        '...',
        'you',
        '♪',
        '[music]',
        '(music)',
    })

    def _looks_like_hallucination(self, text: str, audio: 'np.ndarray | None') -> bool:
        """True when `text` is a phrase Whisper is famous for inventing on
        silence AND the underlying audio is in the quiet-room energy band.

        We require BOTH conditions: a real short reply like "Thanks." in a
        room with someone actually speaking will pass; a Whisper-invented
        "Thank you." over a fan and a keyboard will not.
        """
        if not text or audio is None or len(audio) == 0:
            return False
        normalised = text.strip().lower().rstrip('.!?,;:')
        if normalised not in self._HALLUCINATION_PHRASES:
            return False
        try:
            abs_audio = np.abs(audio)
            peak = float(np.max(abs_audio))
            mean = float(np.mean(abs_audio))
        except Exception:
            return False
        return (peak < self._QUIET_ROOM_PEAK_CEILING
                and mean < self._QUIET_ROOM_MEAN_CEILING)

    def _is_effectively_silent(self, audio: np.ndarray) -> bool:
        """Return True if `audio` is so quiet there's no plausible speech
        in it. Used to short-circuit the transcription path so Whisper
        never gets the chance to hallucinate "Thank you." from silence,
        the user gets a clear "no sound" message instead.

        Works on the raw float32 audio buffer we'd otherwise send to
        Whisper. Cheap (one pass over the array).
        """
        if audio is None or len(audio) == 0:
            return True
        try:
            abs_audio = np.abs(audio)
            peak = float(np.max(abs_audio))
            mean = float(np.mean(abs_audio))
        except Exception:
            return False
        return (peak < self._SILENCE_PEAK_THRESHOLD
                and mean < self._SILENCE_MEAN_THRESHOLD)

    def _transcribe_hybrid(self, audio: np.ndarray):
        """The new entry point: cloud-first with transparent local fallback.
        Returns `(text, language, duration_s)` so the worker code is
        unchanged."""
        # Silence guard, Whisper reliably hallucinates "Thank you." (or
        # "Thanks for watching." on longer clips) when fed silence. If the
        # captured audio is below the noise floor of a working mic, skip
        # transcription entirely and surface a clear message.
        duration = (len(audio) / float(_SR)) if audio is not None else 0.0
        if self._is_effectively_silent(audio):
            logger.warning(
                f'Audio buffer is silent ({duration:.1f}s), skipping '
                f'transcription. Mic may be muted, disconnected, or wrong '
                f'device selected.'
            )
            return (
                '__NO_AUDIO__',                # sentinel for main.py to detect
                'silent',                      # language placeholder
                duration,
            )

        # Auto-noise-reduce BEFORE sending to either path. Applied only if
        # the toggle is ON and the recording's noise floor warrants it.
        # Cloud Whisper (large-v3-turbo) is already robust to background
        # noise but denoising can still nudge accuracy up on truly noisy
        # clips; for quiet rooms it's pure waste, so we skip.
        # `_skip_denoise` flag stops `_transcribe` from re-denoising on
        # cloud→local fallback (it would be a no-op but we shouldn't even
        # check the noise floor twice).
        denoised_here = False
        if self._cfg.audio.noise_reduction and _should_denoise(audio):
            try:
                t0 = time.perf_counter()
                audio = _denoise(audio)
                denoised_here = True
                logger.debug(
                    f'Auto-denoise (hybrid): applied '
                    f'({(time.perf_counter()-t0)*1000:.0f}ms)'
                )
            except Exception as e:
                logger.warning(f'Auto-denoise failed: {e}')

        # Cloud-disabled or audio empty → straight to local
        if not self._cloud_enabled() or audio is None or len(audio) == 0:
            return self._transcribe(audio, _already_denoised=denoised_here)

        # ── Fast reachability probe ──────────────────────────────────────────
        # requests' timeout=3.0 covers connect + read, but the pre-warmed
        # TCP socket in the pool can write into a dead network for minutes
        # before the kernel decides the connection is gone — and that whole
        # time the worker thread is hung, the pill shows "Transcribing…",
        # and the user thinks the app is broken. A 0.5 s non-blocking TCP
        # probe to api.groq.com:443 catches the offline / DNS-fail case
        # before we touch the slow session pool path. If the probe fails,
        # we skip the cloud attempt entirely and go straight to local —
        # the user sees the "Cloud unreachable" pill within a second.
        if not self._cloud_reachable():
            self._cloud_last_error = 'Cloud unreachable — using local model'
            self._CLOUD_RECENT_OK = False
            logger.info('Cloud reachability probe failed (offline / DNS) — '
                        'skipping cloud, going to local.')
            return self._transcribe(audio, _already_denoised=denoised_here)

        # Cloud timeout budget. Was 3s (too aggressive: users on slow /
        # rural / congested WiFi never completed the upload phase for a
        # 2s audio clip before we aborted to local, even when the network
        # would eventually deliver the response). Bumped to 15s so slow
        # uploaders on ~20-50 kbps connections still get the higher-
        # quality Groq result. Local pipeline stays as safety net for
        # anything slower than that.
        cloud_timeout = float(getattr(self._cfg.audio, 'cloud_timeout_s', 15.0))
        lang_hint = self._cfg.transcription.language or None

        t0 = time.perf_counter()
        try:
            text, lang, duration = self._transcribe_via_groq(
                audio, lang_hint, cloud_timeout,
            )
            dt = time.perf_counter() - t0
            self._CLOUD_RECENT_OK = True
            logger.info(
                f'⏱ transcribe(cloud): groq={dt*1000:.0f}ms  '
                f'audio={duration:.1f}s  RTF={dt/max(duration, 0.1):.2f}x'
            )
            # Drop classic Whisper hallucinations on near-silent audio so
            # the user sees "No speech detected" instead of a phantom
            # "Thank you." typed into their document.
            if self._looks_like_hallucination(text, audio):
                logger.info(
                    f'Discarding hallucination "{text[:40]}" on quiet audio.'
                )
                return ('__NO_AUDIO__', 'silent', duration)
            return text, lang, duration
        except Exception as e:
            dt = time.perf_counter() - t0
            self._CLOUD_RECENT_OK = False
            # Surface the cloud failure reason so main.py can show a pill.
            # Friendly mapping for the common cases users actually hit on
            # Windows: PermissionError 13 = AV / firewall blocking the
            # request; ConnectionRefused = network down; Timeout = slow link.
            msg = str(e)
            msg_l = msg.lower()
            # Only set the user-visible message for genuine OFFLINE /
            # UNREACHABLE conditions — those are worth surfacing because
            # the user might want to know their internet's down. For
            # AV-blocked / permission-denied / other "cloud reachable but
            # refusing" cases, the local fallback runs transparently and
            # produces a correct result, so we stay silent.
            exc_name = type(e).__name__.lower()
            if 'name resolution' in msg_l or 'getaddrinfo' in msg_l:
                self._cloud_last_error = 'Cloud unreachable (DNS) — using local model'
            elif 'connection' in msg_l and ('refus' in msg_l or 'reset' in msg_l):
                self._cloud_last_error = 'Cloud refused connection — using local model'
            elif 'timeout' in msg_l or 'timed out' in msg_l:
                # Timeout on slow internet is the most common cause of the
                # silent "why is my dictation worse" complaint. Make the
                # cause explicit so the user knows to check connection.
                self._cloud_last_error = 'Cloud too slow (internet slow?) — used local model'
            elif (
                # Antivirus HTTPS interception. AVG / Avast / Kaspersky /
                # Bitdefender / ESET all MITM outbound TLS by default and
                # present their own cert, which our TLS stack rejects
                # because it isn't in truststore. Signature: SSL /
                # certificate errors, PermissionError 13, "wrong version
                # number", "unknown ca", "self signed certificate".
                'ssl' in msg_l or 'certificate' in msg_l
                or 'winerror 13' in msg_l or 'permission' in exc_name
                or 'wrong version number' in msg_l or 'unknown ca' in msg_l
                or 'self signed' in msg_l or 'self-signed' in msg_l
                or 'cert_' in msg_l
            ):
                self._cloud_last_error = (
                    'Antivirus blocked cloud — add api.groq.com to AV exceptions')
            else:
                # Unknown errors: still surface something so the user has
                # a hint. Truncated exception name is enough for us to
                # diagnose from a screenshot without needing full logs.
                self._cloud_last_error = (
                    f'Cloud failed ({type(e).__name__}) — used local model')
            logger.warning(
                f'Cloud transcription failed in {dt*1000:.0f}ms, falling back '
                f'to local Whisper. ({type(e).__name__}: {e})'
            )
            return self._transcribe(audio)

    def _transcribe(self, audio: np.ndarray, *,
                    _already_denoised: bool = False):
        t_start = time.perf_counter()

        # ── Fast bail on near-empty audio ────────────────────────────────────
        # If the recording is shorter than 250 ms OR has no signal above the
        # noise floor, faster-whisper still pads to 30 s of silence and runs
        # the full encoder + decoder, which costs ~10 s on CPU. Returning
        # the silent sentinel immediately saves the user a long, pointless
        # "transcribing…" wait and matches the cloud path's __NO_AUDIO__
        # short-circuit.
        try:
            duration = len(audio) / float(_SR)
            if duration < 0.25:
                logger.info(f'Local transcribe: audio is {duration*1000:.0f}ms, '
                            'too short — skipping inference.')
                return '__NO_AUDIO__', 'silent', duration
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            if peak < 0.005:   # essentially digital silence
                logger.info(f'Local transcribe: audio peak {peak:.4f} '
                            'below noise floor — skipping inference.')
                return '__NO_AUDIO__', 'silent', duration
        except Exception:
            pass

        # Noise reduction is now auto-adaptive. With the toggle ON, the
        # transcriber samples the recording's noise floor and only spends
        # the CPU on spectral gating when the room is actually noisy.
        # Quiet rooms skip the work entirely, same quality, faster, no
        # risk of over-cleaning subtle speech features.  Toggle OFF still
        # means "never denoise" for users who specifically want it off.
        # `_already_denoised` is set by the hybrid orchestrator when the
        # cloud path falls back to local, we don't re-denoise.
        t_denoise = 0.0
        if not _already_denoised and self._cfg.audio.noise_reduction:
            if _should_denoise(audio):
                t0 = time.perf_counter()
                audio = _denoise(audio)
                t_denoise = time.perf_counter() - t0
                logger.debug(f'Auto-denoise: applied ({t_denoise*1000:.0f}ms)')
            else:
                logger.debug('Auto-denoise: skipped, recording is clean.')

        chunks = _split_audio(audio)

        try:
            mode_obj       = getattr(self._cfg.modes.definitions, self._cfg.modes.active_mode, None)
            initial_prompt = mode_obj.initial_prompt if mode_obj else ''
            post_rules     = mode_obj.post_rules     if mode_obj else []
        except Exception:
            initial_prompt, post_rules = '', []

        override_prompt = getattr(self._cfg.transcription, 'initial_prompt', '') or ''
        prompt = _build_prompt(self._cfg, initial_prompt or override_prompt or '')

        lang      = self._cfg.transcription.language or None
        base_temp = float(self._cfg.transcription.temperature)

        texts, total_duration, detected_lang = [], 0.0, 'unknown'
        t_whisper_total = 0.0
        for chunk in chunks:
            if self._cancelled.is_set():
                break
            tw0 = time.perf_counter()
            segments, info = self._model.transcribe(
                chunk,
                language=lang,
                beam_size=1,                     # greedy, fastest; quality unchanged for speech
                temperature=base_temp,
                condition_on_previous_text=False,
                initial_prompt=prompt or None,
                vad_filter=True,                 # Whisper trims silence before encoder
                no_speech_threshold=0.6,
            )
            chunk_text = ''.join(s.text for s in segments).strip()
            t_whisper_total += time.perf_counter() - tw0
            texts.append(chunk_text)
            total_duration += info.duration
            detected_lang   = info.language

        text = _stitch_texts(texts)
        text = self._apply_post_rules(text, post_rules)

        t_total = time.perf_counter() - t_start
        nr_note = f'denoise={t_denoise:.3f}s  ' if self._cfg.audio.noise_reduction else 'denoise=OFF  '
        logger.info(
            f'⏱ transcribe: {nr_note}'
            f'whisper={t_whisper_total:.3f}s  '
            f'total={t_total:.3f}s  '
            f'audio={total_duration:.1f}s  '
            f'RTF={t_whisper_total/total_duration:.2f}x' if total_duration else ''
        )

        # Same hallucination guard as the cloud path. Whisper is famous
        # for inventing "Thank you." / "Thanks for watching." on near-
        # silent audio; if the recording was quiet AND Whisper handed us
        # one of those canonical phrases, treat as no speech.
        if self._looks_like_hallucination(text, audio):
            logger.info(
                f'Discarding hallucination "{text[:40]}" on quiet audio (local).'
            )
            return ('__NO_AUDIO__', 'silent', total_duration)

        return text, detected_lang, total_duration

    def _apply_post_rules(self, text: str, rules) -> str:
        if not text:
            return text
        for rule in (list(rules) if not isinstance(rules, list) else rules):
            if rule == 'capitalize_sentences':
                text = re.sub(r'(^|(?<=[.!?])\s+)([a-z])',
                              lambda m: m.group(1) + m.group(2).upper(), text)
            elif rule == 'fix_punctuation':
                text = text.strip()
                if text and text[-1] not in '.!?':
                    text += '.'
        return text

    def shutdown(self):
        self._queue.put(None)
        self._preview_queue.put(None)
