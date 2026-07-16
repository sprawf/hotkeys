import logging
import threading
import time
import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

SAMPLE_RATE    = 16000
BLOCKSIZE      = 512   # matches Silero VAD chunk size
_INTERIM_EVERY = SAMPLE_RATE * 4       # emit interim every 4 s of new audio
_MAX_RECORD_S  = 300                   # hard cap: 5 minutes of recording


# ── Physical-mic heuristics ───────────────────────────────────────────────────
#
# Substrings (case-insensitive) that mark a device as VIRTUAL or NOT-A-MIC.
# Devices matching any of these are deprioritised when auto-detecting.
_VIRTUAL_HINTS = (
    'droidcam', 'voicemod', 'vb-audio', 'vb-cable', 'cable input', 'cable output',
    'voicemeeter', 'obs virtual', 'obs-camera', 'streamlabs',
    'stereo mix', 'what u hear', 'wave out mix',
    'line in', 'line input',
    'midi', 'wave',
    'sound mapper', 'primary sound', 'mapper',
    'spatial sound', 'azure', 'spotify',
)

# Substrings that suggest a real physical mic. Used to boost scoring.
_PHYSICAL_HINTS = (
    'microphone', 'mic ', 'mic-', 'headset', 'webcam',
    'realtek', 'usb audio', 'usb mic',
    'array', 'condenser',
)


def is_virtual_mic(name: str) -> bool:
    """Heuristic: True if the device name looks like a virtual sound source
    (DroidCam, OBS, Stereo Mix, etc.) rather than a real microphone."""
    low = (name or '').lower()
    return any(t in low for t in _VIRTUAL_HINTS)


def _physical_mic_candidates(exclude: set | None = None) -> list:
    """Return ALL physical-mic candidates as a list of (index, name) tuples,
    sorted best-first. Each one is worth trying in turn, Windows often
    exposes the same hardware mic via 3 host APIs (MME, DirectSound,
    WASAPI), and one of them usually works even when the others reject
    our sample rate. The runtime walks the whole list before giving up.
    """
    exclude = set(exclude or set())
    candidates = []
    try:
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            hostapis = []
        for i, d in enumerate(sd.query_devices()):
            if i in exclude:
                continue
            if d.get('max_input_channels', 0) <= 0:
                continue
            name = d.get('name', '') or ''
            low  = name.lower()
            score = 0
            if any(t in low for t in _PHYSICAL_HINTS):
                score += 10
            if any(t in low for t in _VIRTUAL_HINTS):
                score -= 100
            # Host-API preference: WASAPI accepts any sample rate via shared
            # mode and is the most reliable; MME is least permissive.
            hostapi_idx = d.get('hostapi', -1)
            hostapi_name = ''
            if 0 <= hostapi_idx < len(hostapis):
                hostapi_name = (hostapis[hostapi_idx].get('name') or '').lower()
            if 'wasapi' in hostapi_name:
                score += 2
            if 'directsound' in hostapi_name:
                score += 1
            if 'mme' in hostapi_name:
                score -= 1
            if score > 0:        # only real-looking candidates
                candidates.append((score, -i, i, name))
    except Exception as e:
        logger.debug(f'_physical_mic_candidates enumeration failed: {e}')
        return []
    candidates.sort(reverse=True)
    return [(idx, name) for (_score, _neg_i, idx, name) in candidates]


def _pick_best_physical_mic(exclude: set | None = None) -> int | None:
    """Single-best-candidate convenience wrapper for callers that only
    want one device. The runtime in `_open_stream` uses
    `_physical_mic_candidates` directly to try the whole list."""
    cands = _physical_mic_candidates(exclude)
    if not cands:
        return None
    idx, name = cands[0]
    logger.info(f'Auto-detected physical mic: [{idx}] {name!r} (top of {len(cands)})')
    return idx


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
        # Single-worker executor for interim transcribe callbacks. The old
        # code spawned a fresh Thread per chunk; on a long recording that's
        # dozens of short-lived threads. With max_workers=1, a slow interim
        # transcribe coalesces (the next chunk waits in the queue) instead
        # of stacking up parallel work.
        from concurrent.futures import ThreadPoolExecutor
        self._interim_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='audio-interim')
        self._db             = -60.0
        # Resampling state, set when the chosen device rejects 16 kHz and
        # we have to open the stream at its native rate (e.g. DroidCam at
        # 44.1 kHz, some USB mics at 48 kHz). `_device_rate=None` means
        # the device accepts 16 kHz directly and no resampling is needed.
        self._device_rate:  int | None = None
        self._resample_buf: np.ndarray = np.zeros(0, dtype=np.float32)
        # Set by _callback the first time audio actually flows. Used by
        # start_recording to block briefly on cold-start so users don't
        # start speaking into a not-yet-flowing WASAPI stream.
        self._first_chunk_seen: bool = False
        # Surfaced to the UI: when non-empty, the last open-stream error
        # message, lets the "Microphone unavailable" dialog show what
        # actually went wrong (sample rate? permissions? device gone?).
        self.last_error: str = ''

    @property
    def db(self):
        return self._db

    def _open_stream(self):
        """Open the input stream. Self-healing, tries every reasonable
        combination before surfacing an error to the user, so a stale
        config or a wrongly-configured Windows default (e.g. DroidCam
        marked as default but silent) silently degrades to a working
        physical mic.

        Try order:
          1. Saved device      @ 16 kHz                ← ideal
          2. Saved device      @ device's native rate  ← weird-rate mics
          3. System default    @ 16 kHz                ← saved device gone
          4. System default    @ native rate           ← belt + suspenders
          5. Best PHYSICAL mic @ 16 kHz                ← Windows default is bad
          6. Best physical mic @ native rate           ← physical mic non-16k
        Each failed attempt is logged at warning; only when all attempts
        fail do we re-raise so the UI dialog appears.
        """
        saved_device = self._cfg.audio.input_device_index
        self.last_error = ''
        self._device_rate = None
        self.fell_back_to_default = False   # surfaced to UI for an info toast

        attempts = []
        if saved_device is not None:
            # User explicitly picked a device, respect that first, then
            # fall back through default → scan if it fails.
            attempts.append((saved_device, SAMPLE_RATE,
                             'saved @ 16 kHz'))
            attempts.append((saved_device, self._native_rate(saved_device),
                             'saved @ native'))
            attempts.append((None,         SAMPLE_RATE,
                             'default @ 16 kHz'))
            attempts.append((None,         self._native_rate(None),
                             'default @ native'))
            scanned = _pick_best_physical_mic(exclude={saved_device})
            if scanned is not None:
                attempts.append((scanned, SAMPLE_RATE,
                                 f'scan-physical[{scanned}] @ 16 kHz'))
                attempts.append((scanned, self._native_rate(scanned),
                                 f'scan-physical[{scanned}] @ native'))
        else:
            # AUTO-DETECT: walk the full list of physical-mic candidates
            # in score order. Windows often exposes the same Realtek mic
            # via MME + DirectSound + WASAPI as 3 separate device indices,
            # one may reject our sample rate while another accepts it.
            # Trying each in turn means we recover from per-backend
            # weirdness before falling back to whatever Windows says is
            # "default" (which is often a silent virtual mic).
            for idx, name in _physical_mic_candidates():
                attempts.append((idx, SAMPLE_RATE,
                                 f'auto-pick[{idx}] @ 16 kHz'))
                attempts.append((idx, self._native_rate(idx),
                                 f'auto-pick[{idx}] @ native'))
            # Windows default as the final safety net, covers users who
            # explicitly set a virtual mic as their Windows default ON
            # PURPOSE (e.g. they DO use DroidCam and want it).
            attempts.append((None, SAMPLE_RATE,
                             'default @ 16 kHz'))
            attempts.append((None, self._native_rate(None),
                             'default @ native'))

        # CAP attempts at 4. PortAudio's WASAPI host has a known issue:
        # after ~5 failed sd.InputStream opens (e.g. format-unsupported on
        # every device the user has), its internal heap corrupts and the
        # whole process crashes with STATUS_STACK_BUFFER_OVERRUN. Trying
        # the four most likely options is enough for any sane Windows
        # config; if none of them work we tell the user gracefully.
        attempts = attempts[:4]
        last_exc: Exception | None = None
        for device, rate, label in attempts:
            if rate is None:        # query_devices failed, skip this combo
                continue
            try:
                self._stream = sd.InputStream(
                    device=device,
                    samplerate=rate,
                    channels=1,
                    dtype='float32',
                    blocksize=BLOCKSIZE,
                    callback=self._callback,
                )
                self._stream.start()
                self._device_rate  = rate if rate != SAMPLE_RATE else None
                self._resample_buf = np.zeros(0, dtype=np.float32)
                self.fell_back_to_default = (
                    saved_device is not None and device is None
                )
                if self.fell_back_to_default:
                    logger.warning(
                        f"Saved mic (device {saved_device}) couldn't be "
                        f"opened, using system default instead ({label})."
                    )
                elif self._device_rate is not None:
                    logger.info(
                        f'Opened input at {rate} Hz ({label}); '
                        f'will resample to {SAMPLE_RATE} Hz in callback.'
                    )
                else:
                    logger.info(f'Opened input at {SAMPLE_RATE} Hz ({label}).')
                return
            except Exception as e:
                last_exc = e
                logger.warning(f'Audio open attempt failed [{label}]: {e}')
                self._stream = None

        # Everything failed, propagate the last error to the UI.
        msg = str(last_exc) if last_exc else 'unknown audio error'
        logger.error(f'Audio stream error (all fallbacks exhausted): {msg}')
        self.last_error = msg
        raise last_exc if last_exc else RuntimeError('audio init failed')

    def _native_rate(self, device) -> int | None:
        """Best-effort lookup of `device`'s default sample rate. Returns
        None if the query fails (caller will skip that attempt)."""
        try:
            info = sd.query_devices(device, 'input')
            return int(info.get('default_samplerate') or 44100)
        except Exception as e:
            logger.debug(f'query_devices({device}) failed: {e}')
            return None

    def _callback(self, indata, frames, time_info, status):
        # Mark first-chunk-arrived so start_recording can wait for real
        # audio flow before returning. Without this, PortAudio's WASAPI
        # cold-start latency (50-500 ms after .start() returns) means
        # the first few hundred ms of the user's speech is spoken into
        # a not-yet-flowing stream, buffer ends up short/silent, VAD
        # + hallucination guard classify it as "no speech detected."
        # Only relevant on the FIRST press after opening; subsequent
        # recordings hit the fast path (stream already flowing).
        if not self._first_chunk_seen:
            self._first_chunk_seen = True
        try:
            chunk = np.clip(indata[:, 0].copy(), -1.0, 1.0)
            # Resample on the fly when the device couldn't give us 16 kHz
            # directly. We buffer the resampled output so we always emit
            # exact-BLOCKSIZE chunks downstream (Silero VAD requires this).
            if self._device_rate is not None:
                try:
                    from scipy.signal import resample_poly
                    from math import gcd
                    g    = gcd(self._device_rate, SAMPLE_RATE)
                    up   = SAMPLE_RATE      // g
                    down = self._device_rate // g
                    resampled = resample_poly(chunk, up, down).astype(np.float32)
                except Exception as e:
                    logger.error(f'Resample failed: {e}')
                    return
                self._resample_buf = np.concatenate([self._resample_buf, resampled])
                while len(self._resample_buf) >= BLOCKSIZE:
                    chunk = self._resample_buf[:BLOCKSIZE].copy()
                    self._resample_buf = self._resample_buf[BLOCKSIZE:]
                    self._dispatch_chunk(chunk)
                return
            self._dispatch_chunk(chunk)
        except Exception as e:
            logger.error(f'Audio callback error: {e}')

    def _dispatch_chunk(self, chunk: np.ndarray) -> None:
        """Per-chunk hot path, runs at ~31 Hz (16 kHz / 512). Extracted
        so the resampling fast path and the no-resample fast path share
        identical bookkeeping."""
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
            logger.warning('Max recording duration reached, auto-stopping.')
            threading.Thread(target=self.stop_recording, daemon=True).start()
        elif should_interim and buf_snapshot:
            audio_snap = np.concatenate(buf_snapshot)
            # Single-worker pool: if the previous interim is still in
            # flight, this submission queues behind it rather than
            # starting another parallel transcribe.
            try:
                self._interim_pool.submit(self._on_interim, audio_snap)
            except RuntimeError:
                # Pool was shut down (recorder destructor ran while a
                # final chunk was inflight). Drop silently.
                pass

    def start_recording(self):
        """Start capturing to the buffer. Robust against transient PortAudio /
        WASAPI failures.

        Failure modes we handle without surfacing an error to the user:
          - Zombie stream: `.active` reports True but the underlying HRESULT
            is dead (rare sounddevice/PortAudio corner case). We tear down
            and reopen if the callback hasn't produced audio recently.
          - Bluetooth headset HFP switch (~500-2000 ms): the mic goes
            unavailable while Windows renegotiates the audio profile.
          - Handle-release delay after a previous recording ended (~100-500
            ms): WASAPI holds the input handle briefly.
          - Windows exclusive-mode contention: another app just grabbed
            the mic; usually released within a second.

        Only surface a user-facing error after exhausting ~2.6 s of retries.
        """
        with self._lock:
            self._buffer         = []
            self._interim_last_n = 0
            self._recording      = True

        # Fast path: existing stream is already open + streaming AND
        # we've seen audio flow through it. Second-and-later presses go
        # here (~0 ms latency).
        if (self._stream is not None and self._stream.active
                and self._first_chunk_seen):
            return

        # Slow path: stream is None or inactive. Clean up any zombie
        # stream object first so subsequent _open_stream() gets a fresh
        # sd.InputStream instance and doesn't confuse PortAudio's
        # per-device state.
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        # Escalating backoff. Total budget ~2.6 s across 4 attempts.
        # Cap at 4 to stay well under PortAudio's ~5-failed-open heap-
        # corruption threshold documented at the top of _open_stream.
        delays = (0, 0.3, 0.8, 1.5)
        last_exc: Exception | None = None
        for attempt, delay in enumerate(delays):
            if delay > 0:
                logger.info(f'Mic open retry #{attempt} after {delay*1000:.0f}ms')
                time.sleep(delay)
            try:
                self._first_chunk_seen = False
                self._open_stream()
                # Wait up to 500 ms for the first real callback so we don't
                # return before audio is actually flowing. WASAPI cold-start
                # latency is typically 50-200 ms; 500 ms covers the tail.
                # If the callback never fires within the window we proceed
                # anyway (won't block the user forever) — a truly dead
                # stream will show up as silence downstream, same as before.
                t0 = time.perf_counter()
                while not self._first_chunk_seen and (time.perf_counter() - t0) < 0.5:
                    time.sleep(0.01)
                if self._first_chunk_seen:
                    warmup_ms = (time.perf_counter() - t0) * 1000
                    if warmup_ms > 50:  # log only if the wait was meaningful
                        logger.info(f'Mic cold-start warmup: {warmup_ms:.0f}ms')
                else:
                    logger.warning('Mic opened but no audio in 500ms — proceeding anyway')
                return  # success
            except Exception as e:
                last_exc = e
                # Clean up any partial state before the next retry.
                if self._stream is not None:
                    try: self._stream.close()
                    except Exception: pass
                    self._stream = None
        # All retries exhausted — surface the last error.
        with self._lock:
            self._recording = False
        raise last_exc if last_exc else RuntimeError(
            'Failed to open microphone after 4 attempts')

    def stop_recording(self):
        with self._lock:
            self._recording = False
            audio = np.concatenate(self._buffer) if self._buffer else np.zeros(0, dtype=np.float32)
            self._buffer = []
        # ALWAYS hand the audio off, even when the buffer is empty.
        # Without this, an empty buffer (audio device hiccup, sample-rate
        # fallback, no-input race) silently dropped the pipeline and left
        # the "Transcribing…" pill stuck forever. Empty audio is handled
        # cleanly downstream — the transcriber's silence guard returns
        # __NO_AUDIO__ which surfaces a clear "No audio" pill.
        if len(audio) == 0:
            import logging
            logging.getLogger(__name__).warning(
                'stop_recording: audio buffer was empty; downstream will '
                'show "No audio" pill instead of getting stuck'
            )
        try:
            self._on_utterance_ready(audio)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                'stop_recording: on_utterance_ready raised'
            )

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
