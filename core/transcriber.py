"""
faster-whisper transcription pipeline.
Adapted from KaiWhisper — replaces config.py imports with storage module.
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
                logger.warning(f'Requested model {model_name!r} not found — falling back to base')
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
        """Synchronous transcription using the base preview model — for Quick Notes.
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
        self._queue.put(audio)

    def cancel(self):
        self._cancelled.set()
        for q in (self._queue, self._preview_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def _worker(self):
        while True:
            audio = self._queue.get()
            if audio is None:
                break
            if self._cancelled.is_set():
                continue
            ready = self._model_ready.wait(timeout=60)
            if not ready or self._model is None:
                logger.error('Whisper model not ready after 60s — reporting error.')
                self._on_status('error')
                continue
            self._on_status('transcribing')
            try:
                text, language, duration = self._transcribe(audio)
                if not self._cancelled.is_set():
                    self._on_result(text, language, duration)
            except Exception:
                if not self._cancelled.is_set():
                    self._on_status('error')
                self._dump_traceback()
            finally:
                if not self._cancelled.is_set():
                    self._on_status('idle')

    def _transcribe(self, audio: np.ndarray):
        t_start = time.perf_counter()

        if self._cfg.audio.noise_reduction:
            t0 = time.perf_counter()
            audio = _denoise(audio)
            t_denoise = time.perf_counter() - t0
        else:
            t_denoise = 0.0

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
                beam_size=1,                     # greedy — fastest; quality unchanged for speech
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
