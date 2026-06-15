"""File-mode transcription engine (distinct from core/transcriber.py, which
handles live mic dictation).

Pipeline:
  1. Decode source to mono 16 kHz WAV (ffmpeg via faster_whisper.audio)
  2. faster-whisper transcribe, full-file, VAD-aware, word timestamps
  3. (optional) pyannote.audio speaker diarization
  4. Stitch: assign a speaker label to each Whisper segment by majority-overlap
  5. Return TranscriptJob (JSON-serializable)

Progress callback signature:  cb(phase: str, pct: float)
  phase ∈ {'decode', 'transcribe', 'diarize', 'merge'}
  pct   ∈ 0.0 – 1.0

Long-running work runs in the caller's thread, the UI should spawn a
worker thread and route on_progress via root.after() to avoid Tk threading
hazards.
"""
from __future__ import annotations

import json
import logging
import os
import time
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

# Force PyInstaller's static analyzer to bundle huggingface_hub. The actual
# use is inside `_ensure_model_downloaded` (lazy import) which the analyzer
# misses, especially after we excluded pyannote+torch. Without this anchor
# import here at module level, on-demand large-v3 / large-v3-turbo downloads
# fail at runtime with ModuleNotFoundError: huggingface_hub. We don't use
# the import directly here — it just keeps the module present in the bundle.
try:
    import huggingface_hub as _hf_anchor  # noqa: F401
except Exception:
    _hf_anchor = None

# PROJECT.md "feature isolation rule": this module must not mutate any
# process-global state at import time (env vars, OMP threads, warning
# filters, signal handlers). Anything that previously lived here moved
# into `_prepare_pyannote_environment()` and is called only when the user
# actually triggers a diarization job, so opening or even importing
# transcribe.engine for an unrelated reason (e.g. the F9 UI's metadata
# preview) has zero effect on other tabs, dictation, refining, etc.

logger = logging.getLogger(__name__)

# ── Public dataclasses ────────────────────────────────────────────────────────

@dataclass
class TranscriptSegment:
    """One Whisper segment, optionally tagged with a speaker label."""
    start:   float           # seconds from file start
    end:     float
    text:    str
    speaker: str = ''        # 'Speaker 1', 'Speaker 2', ..., empty if no diarization
    words:   list = field(default_factory=list)  # [{'start','end','word','prob'}, ...]


@dataclass
class TranscriptJob:
    """Result of one transcription run. Suitable for JSON persistence."""
    id:          str
    source:      str                            # original filename or URL
    duration:    float                          # seconds
    language:    str                            # detected (e.g. 'en')
    model:       str                            # whisper model id used
    diarized:    bool
    segments:    list                           # list[TranscriptSegment] as dicts
    summary:     str = ''                       # AI-generated, optional
    diarize_note: str = ''                      # why diarization was skipped (if it was)
    created_at:  float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'TranscriptJob':
        return cls(**d)


# ── Helpers ───────────────────────────────────────────────────────────────────

# Hard caps for subtitle chunks. Tuned against real material: 6 s / 55 chars
# keeps a line readable at normal reading speed (~200 ms per word) on a
# 1080p video. Devanagari / CJK glyphs are wider, so the char limit is on
# the conservative side; English with the same chunker stays comfortable.
_MAX_SUBTITLE_SECS = 6.0
_MAX_SUBTITLE_CHARS = 55
_SUBTITLE_PUNCT = set('।.!?,;:')


def _resplit_for_subtitles(segments: list) -> list:
    """Walk word timestamps and rebuild subtitle-sized chunks.

    Whisper occasionally returns a single 30+ s segment for a continuous
    monologue. This helper splits any such segment at natural punctuation
    breaks if available, or at hard duration / character caps otherwise.
    Segments that are already short are emitted unchanged.

    Also caps any chunk whose end timestamp is anomalously far past its
    start (Whisper's word_timestamps end is occasionally the original
    segment boundary, not the actual word end). The cap is
    `_MAX_SUBTITLE_SECS + 1.0` to leave a small comfort margin.
    """
    out = []
    for seg in segments:
        # Already short enough to use as-is.
        dur = seg.end - seg.start
        if dur <= _MAX_SUBTITLE_SECS and len(seg.text) <= _MAX_SUBTITLE_CHARS:
            out.append(seg)
            continue

        words = seg.words or []
        if not words:
            # No word timestamps to split with: cap duration and emit.
            seg.end = min(seg.end, seg.start + _MAX_SUBTITLE_SECS + 1.0)
            out.append(seg)
            continue

        cur_start = None
        cur_end = None
        cur_text = ''
        cur_words = []

        def _flush():
            if not cur_text.strip(): return
            from copy import copy
            new = copy(seg)
            new.start = cur_start
            new.end = min(cur_end, cur_start + _MAX_SUBTITLE_SECS + 1.0)
            new.text = cur_text.strip()
            new.words = list(cur_words)
            out.append(new)

        for w in words:
            w_start = float(w['start'])
            w_end   = float(w['end'])
            w_word  = w['word']
            if cur_start is None:
                cur_start = w_start
            candidate = (cur_text + w_word).strip()
            would_exceed = (w_end - cur_start > _MAX_SUBTITLE_SECS
                            or len(candidate) > _MAX_SUBTITLE_CHARS)
            last_char = (cur_text.strip()[-1] if cur_text.strip() else '')
            has_natural_break = (last_char in _SUBTITLE_PUNCT
                                 and len(cur_text.strip()) > 15)
            if cur_text and (would_exceed or has_natural_break):
                _flush()
                cur_start = w_start
                cur_text = w_word
                cur_words = [w]
            else:
                cur_text += w_word
                cur_words.append(w)
            cur_end = w_end
        _flush()
    return out


def _safe_progress(cb: Callable | None, phase: str, pct: float) -> None:
    if cb is None:
        return
    try:
        cb(phase, max(0.0, min(1.0, float(pct))))
    except Exception as e:
        logger.warning(f'progress cb raised: {e}')


def _resolve_model_path(model: str) -> str:
    """Resolve a model spec to either a local dir (bundled in dist/ models/)
    or a HuggingFace id for the on-demand cache.

    Bundled options: 'base', 'small'.  Anything else (e.g. 'large-v3-turbo')
    falls through to faster-whisper's HF auto-download, which only runs if
    the user picked a non-bundled tier and is online.
    """
    try:
        # Prefer the same lookup logic main.py uses for live dictation.
        from storage import models_dir
        candidate = Path(models_dir()) / model
        if (candidate / 'model.bin').exists():
            return str(candidate)
    except Exception:
        pass
    return model


# Friendly model ID → CTranslate2-on-HuggingFace repo. faster-whisper accepts
# bare strings like 'large-v3-turbo' too, but Systran is the canonical one
# for the CT2-converted weights so we pin to it for reproducibility.
_HF_REPOS = {
    # large-v3-turbo: Systran never released a CT2 conversion. The community
    # Mobius Labs build is the canonical one and the same repo faster-whisper
    # silently fetches when given the bare 'large-v3-turbo' string.
    'large-v3-turbo': 'mobiuslabsgmbh/faster-whisper-large-v3-turbo',
    'large-v3':       'Systran/faster-whisper-large-v3',
}


def _ensure_model_downloaded(model: str,
                             on_progress: Callable[[str, float], None] | None,
                             log: Callable[[str], None]) -> str:
    """If `model.bin` is missing, fetch the full snapshot from HuggingFace
    with progress reported through the existing 'download_model' phase.

    Returns the resolved local path. Falls back to the bare model id (which
    triggers faster-whisper's silent built-in fetch) if HF download fails,
    that way the user still gets transcription, just without progress.
    """
    try:
        from storage import models_dir
    except Exception:
        return model
    target_dir = Path(models_dir()) / model
    if (target_dir / 'model.bin').exists():
        return str(target_dir)

    repo_id = _HF_REPOS.get(model, model)
    log(f'Downloading model {model} from {repo_id}…')
    _safe_progress(on_progress, 'download_model', 0.0)
    try:
        from huggingface_hub import snapshot_download
        # tqdm posts updates to stderr; we hijack it by passing a callback
        # via the `tqdm_class` kwarg so each block-write tick lifts the bar.
        import threading, time as _time
        done = {'flag': False, 'last_pct': 0.0}

        def _heartbeat():
            # Coarse pseudo-progress: HF snapshot_download doesn't expose a
            # clean byte counter to a callback, so we sample directory size
            # against the expected total. Not exact, but smooth and honest
            #, better than a frozen 0 % for 3 minutes.
            expected = {
                'large-v3-turbo': 809 * 1024 * 1024,
                'large-v3':       1500 * 1024 * 1024,
            }.get(model, 800 * 1024 * 1024)
            while not done['flag']:
                try:
                    size = 0
                    if target_dir.exists():
                        for p in target_dir.rglob('*'):
                            try:
                                if p.is_file(): size += p.stat().st_size
                            except Exception: pass
                    pct = min(0.98, size / expected)
                    if pct > done['last_pct']:
                        done['last_pct'] = pct
                        _safe_progress(on_progress, 'download_model', pct)
                except Exception:
                    pass
                _time.sleep(0.4)

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()
        try:
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(target_dir),
                local_dir_use_symlinks=False,
            )
        finally:
            done['flag'] = True
            hb.join(timeout=1.0)
        _safe_progress(on_progress, 'download_model', 1.0)
        log(f'Model {model} download complete.')
        return str(target_dir)
    except Exception as e:
        log(f'Model download via HF failed ({e}); falling back to '
            f'faster-whisper auto-fetch.')
        return model


# ── Whisper model cache ──────────────────────────────────────────────────────
#
# A user transcribing several clips in a row hits the same model each time;
# reloading WhisperModel from disk costs ~1 s (CPU) and 200-1500 MB of fresh
# allocations every job. Cache one instance per (path, device, compute_type)
# triple so back-to-back jobs are warm.
#
# NOT shared with core/transcriber.py, that owns its own model for live
# dictation. Sharing them is a future optimisation; the cost today is one
# duplicate model in RAM only when the user actually uses both features
# within the same session.

_WHISPER_CACHE: dict[tuple, object] = {}
_WHISPER_CACHE_LOCK = __import__('threading').Lock()

def _get_or_load_whisper(model_path: str, device: str, compute_type: str,
                         log: Callable[[str], None]):
    from faster_whisper import WhisperModel
    key = (model_path, device, compute_type)
    with _WHISPER_CACHE_LOCK:
        wm = _WHISPER_CACHE.get(key)
        if wm is not None:
            log(f'Reusing cached whisper model: {model_path}')
            return wm
        log(f'Loading whisper model: {model_path}')
        # cpu_threads=0 → CTranslate2 picks a sensible default that respects
        # the torch.set_num_threads cap set at module import.
        wm = WhisperModel(model_path, device=device, compute_type=compute_type)
        _WHISPER_CACHE[key] = wm
        return wm


# Approximate on-disk size per model id, used by the UI so it can warn
# the user before triggering a multi-hundred-megabyte download. These are
# the sizes faster-whisper's model.bin reports; +/- a few MB depending
# on the specific CT2 build.
# Measured on the actual Systran / Mobius-Labs CT2 builds, not the
# numbers the HF model card quotes. Used by the UI's "needs ~X MB
# download" badge and confirm dialog, so accuracy matters: a user who
# was told 1500 MB and saw 3000 MB pull would lose trust.
_MODEL_SIZES_MB = {
    'base':            145,
    'small':           483,
    'large-v3-turbo':  809,
    'large-v3':       3000,
}


def resolve_planned_model(model: str,
                          *, translate: bool = False,
                          music_mode: bool = False) -> dict:
    """Return what `transcribe_file` would actually load given (model,
    translate, music_mode), plus whether the resulting model is already
    on disk and its approximate size in MB.

    The UI calls this before kicking off a job so it can show a one-time
    download confirm to the user instead of silently pulling 1.5 GB.

    Keys in the returned dict:
        effective_model : str           model id the engine will load
        switched_for    : str           why we switched, if at all
                                        ('music_mode' / 'translate_fallback' / '')
        on_disk         : bool          True if model.bin is already local
        size_mb         : int           approximate on-disk weight
        fallback_model  : str | None    the bundled model that would be
                                        used if the user declines the
                                        download (None when no fallback
                                        is meaningful)
    """
    requested = model
    switched_for = ''

    if music_mode and model in ('base', 'small'):
        model = 'large-v3-turbo'
        switched_for = 'music_mode'

    if translate and model == 'large-v3-turbo':
        available = _whisper_models_available()
        model = 'large-v3' if 'large-v3' in available else 'small'
        switched_for = 'translate_fallback'

    available = _whisper_models_available()
    on_disk = model in available

    # If the planned model is too big and the user might want to back out,
    # what would they fall back to? For translate, that is `small` (always
    # bundled, knows how to translate). For non-translate, that is the
    # user's originally-requested model when smaller, else `small`.
    fallback = None
    if translate:
        fallback = 'small'
    elif requested != model and requested in available:
        fallback = requested
    elif model != 'small':
        fallback = 'small'

    return {
        'effective_model': model,
        'switched_for':    switched_for,
        'on_disk':         on_disk,
        'size_mb':         _MODEL_SIZES_MB.get(model, 0),
        'fallback_model':  fallback if fallback != model else None,
    }


def _whisper_models_available() -> list[str]:
    """List of model ids the user can pick without going online."""
    out = ['base', 'small']
    try:
        from storage import models_dir
        for name in ('large-v3-turbo', 'large-v3'):
            if (Path(models_dir()) / name / 'model.bin').exists():
                out.append(name)
    except Exception:
        pass
    return out


# ── Main entry point ──────────────────────────────────────────────────────────

def transcribe_file(
    source_path: str | os.PathLike,
    *,
    model: str = 'small',
    diarize: bool = True,
    language: str | None = None,
    translate: bool = False,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    device: str = 'auto',
    compute_type: str = 'auto',
    music_mode: bool = False,
    on_progress: Callable[[str, float], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> TranscriptJob:
    """Run the full TurboScribe-style pipeline on `source_path`.

    Parameters
    ----------
    model        whisper tier, 'base'/'small' are bundled, 'large-v3-turbo' downloads.
    diarize      if True, run pyannote.audio over the same file and tag segments.
    language     ISO-639-1 code or None for auto-detect.
    min/max_speakers  optional hints to pyannote.
    device       'auto' | 'cuda' | 'cpu' for whisper inference.

    Returns
    -------
    TranscriptJob.  Raises on fatal errors; partial failures (e.g. diarization
    times out) degrade gracefully, the transcript still returns, just without
    speaker labels.
    """
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f'{src} does not exist')

    job_id = f'tr_{int(time.time()*1000)}_{src.stem[:20]}'
    log = lambda m: (on_log and on_log(m)) or logger.info(m)

    # ── 1. Transcribe ─────────────────────────────────────────────────────────
    _safe_progress(on_progress, 'transcribe', 0.0)
    # Music mode promotes to large-v3-turbo for dramatically better lyrics
    # accuracy on sung vocals. `base` and `small` hallucinate repeat-loops
    # ("Kévi, dis-pudet si l'on s'est sorti tu, Kévi, dis-pudet…") and miss
    # multi-syllable words; turbo's larger context handles them. If turbo's
    # weights aren't on disk yet we download them once (~800 MB) with the
    # 'download_model' phase showing a real progress bar, the user sees
    # exactly what's happening instead of a frozen UI.
    # If the user explicitly picked a larger tier we respect it.
    if music_mode and model in ('base', 'small'):
        log(f'Music mode: upgrading model {model} → large-v3-turbo')
        model = 'large-v3-turbo'

    # `large-v3-turbo` is a decoder-pruned finetune that does NOT support
    # the translate task: passing task='translate' to it silently returns
    # source-language text. This trap caught us testing alien.mkv (Hindi
    # in, Hindi out instead of English). Auto-fallback to the best model
    # that does translate: `large-v3` if the user has it on disk, else
    # `small` (always bundled).
    if translate and model == 'large-v3-turbo':
        try:
            available = _whisper_models_available()
        except Exception:
            available = ['base', 'small']
        target = 'large-v3' if 'large-v3' in available else 'small'
        log(f'Translate to English requires a translation-capable model; '
            f'auto-switching from large-v3-turbo to {target}.')
        model = target

    # If model.bin isn't on disk, fetch the snapshot with progress (one-time).
    # Falls back to the bare model id if HF is unreachable, which triggers
    # faster-whisper's built-in silent auto-fetch as a last resort.
    model_path = _ensure_model_downloaded(model, on_progress, log)

    # auto-resolve device + compute_type the same way core/transcriber does
    if device == 'auto':
        try:
            import ctranslate2
            device = 'cuda' if ctranslate2.get_cuda_device_count() > 0 else 'cpu'
        except Exception:
            device = 'cpu'
    if compute_type == 'auto':
        compute_type = 'float16' if device == 'cuda' else 'int8'

    # Loading the model into memory takes ~30s for large-v3 on CPU and
    # produces no progress events of its own, the UI watchdog will pulse
    # but a labelled phase tells the user what's actually happening.
    _safe_progress(on_progress, 'load_model', 0.0)
    wm = _get_or_load_whisper(model_path, device, compute_type, log)
    _safe_progress(on_progress, 'load_model', 1.0)
    log(f'Transcribing {src.name}…')
    # Marks the gap between model-ready and first-segment-emitted (VAD
    # runs here, plus Whisper's first decode pass). Without this the UI
    # bar would sit at 0% under the 'transcribe' label for 5-30s.
    _safe_progress(on_progress, 'analyze', 0.0)

    # Music mode tunes Whisper for sung vocals over instrumental: aggressive
    # VAD treats singing as non-speech and strips it (3:28 song → 11 s kept
    # in our SoundCloud test). We disable the VAD filter and loosen the
    # no-speech threshold so quieter sung passages aren't dropped.
    # `condition_on_previous_text=False` prevents Whisper from latching onto
    # repeated chorus lines and looping forever once it hits one.
    if music_mode:
        log('Music mode ON, VAD disabled, sung-vocal tuning')
        segments_iter, info = wm.transcribe(
            str(src),
            language=language,
            task='translate' if translate else 'transcribe',
            word_timestamps=True,
            vad_filter=False,
            no_speech_threshold=0.20,
            condition_on_previous_text=False,
            beam_size=5,
            # Temperature fallback chain, each higher value is tried when
            # the previous one produces a segment Whisper deems untrusted
            # (low log-prob or high compression ratio). Wider chain helps
            # the model break out of repetition loops on sung passages.
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            # The repetition guard: if a segment's gzip ratio exceeds this,
            # Whisper rejects it and retries with the next temperature.
            # Default is 2.4, at 1.8 we catch the "Kévi, dis-pudet si l'on
            # s'est sorti tu" repeat-storm and force a retry. Tighter than
            # default but loose enough that real choruses still pass.
            compression_ratio_threshold=1.8,
            # Reject segments where Whisper's own confidence is very low.
            # Default is -1.0; -0.7 is stricter without dropping decent
            # sung passages that just have lower confidence than speech.
            log_prob_threshold=-0.7,
        )
    else:
        # Tighter VAD silence (150 ms vs the default 500 ms) splits long
        # monologue segments into subtitle-sized chunks. With 500 ms a
        # 90 s stand-up clip in Hindi came back as a single 38 s segment;
        # at 150 ms it splits at natural pauses. The post-process loop
        # below caps any remaining oversized chunks at MAX_SUBTITLE_SECS.
        segments_iter, info = wm.transcribe(
            str(src),
            language=language,
            task='translate' if translate else 'transcribe',
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={'min_silence_duration_ms': 150},
            beam_size=5,
        )

    # Stream segments so we can update progress as we go.
    duration = float(getattr(info, 'duration', 0.0))
    raw_segments: list[TranscriptSegment] = []
    # Heuristic fallback when faster-whisper can't pre-compute duration
    # (e.g. some streamed inputs), pulse progress on segment count so the
    # bar moves visibly instead of sitting at 0 for the whole job.
    _assumed_segments = max(50, int(duration / 5)) if duration > 0 else 50
    for i, s in enumerate(segments_iter):
        # Cancellation checkpoint inside the long-running loop. Whisper
        # itself can't be interrupted mid-segment (single C call), but
        # bailing between segments at least caps the wait when a user
        # cancels a multi-hour job partway through.
        if should_cancel is not None and should_cancel():
            log('Cancellation requested mid-transcribe, aborting after '
                f'{len(raw_segments)} segments.')
            break
        words = []
        if getattr(s, 'words', None):
            for w in s.words:
                words.append({
                    'start': float(w.start),
                    'end':   float(w.end),
                    'word':  w.word,
                    'prob':  float(w.probability),
                })
        raw_segments.append(TranscriptSegment(
            start=float(s.start),
            end=float(s.end),
            text=s.text.strip(),
            words=words,
        ))
        if duration > 0:
            _safe_progress(on_progress, 'transcribe', s.end / duration)
        else:
            # Asymptote toward 0.95, never claim done until we exit the loop
            _safe_progress(on_progress, 'transcribe',
                           min(0.95, i / float(_assumed_segments)))

    _safe_progress(on_progress, 'transcribe', 1.0)
    log(f'Transcribed {len(raw_segments)} segments, {duration:.1f}s audio')

    # ── 1b. Post-split long segments into subtitle-sized chunks ──────────────
    # Whisper sometimes returns a single 30-40 s segment for a continuous
    # monologue (especially in Hindi / Japanese transcription with turbo).
    # That is unreadable as a subtitle line. We walk the word timestamps
    # we already requested and split at natural punctuation breaks or hard
    # caps (max 6 s per chunk, max ~55 characters). Skipped in Music mode
    # because lyric exports want the original Whisper segmentation.
    if not music_mode and raw_segments:
        raw_segments = _resplit_for_subtitles(raw_segments)
        log(f'Resplit for subtitles: {len(raw_segments)} chunks.')

    # ── 2. Diarize (optional) ─────────────────────────────────────────────────
    diarization_ok = False
    diarization_skip_reason = ''
    if diarize:
        try:
            _safe_progress(on_progress, 'diarize', 0.0)
            log('Running speaker diarization…')
            speaker_turns = _run_diarization(
                str(src),
                min_speakers=min_speakers,
                max_speakers=max_speakers,
                on_progress=lambda p: _safe_progress(on_progress, 'diarize', p),
            )
            _assign_speakers(raw_segments, speaker_turns)
            n_speakers = len({s.speaker for s in raw_segments if s.speaker})
            if n_speakers == 0:
                # Pipeline ran but found no clear speaker turns (very short
                # audio, all-silence, one continuous monologue with low SNR).
                # Mark this distinctly from "diarization not requested" so
                # the UI can tell the user the analysis ran but came up empty.
                diarization_ok = False
                diarization_skip_reason = (
                    'no distinct speakers detected, try a longer clip '
                    'or audio with clearer speaker turns'
                )
            else:
                diarization_ok = True
            _safe_progress(on_progress, 'diarize', 1.0)
            log(f'Diarization tagged {n_speakers} speakers')
        except _AudioTooLongForDiarization as e:
            # Expected, non-error fall-through for long files.
            diarization_skip_reason = str(e)
            log(diarization_skip_reason)
            _safe_progress(on_progress, 'diarize', 1.0)
        except Exception as e:
            diarization_skip_reason = f'{type(e).__name__}: {e}'
            log(f'Diarization failed ({diarization_skip_reason}), continuing without speaker labels')
            _safe_progress(on_progress, 'diarize', 1.0)

    # ── 3. Build job ──────────────────────────────────────────────────────────
    _safe_progress(on_progress, 'merge', 1.0)
    return TranscriptJob(
        id=job_id,
        source=str(src),
        duration=duration,
        language=getattr(info, 'language', '') or '',
        model=model,
        diarized=diarization_ok,
        segments=[asdict(s) for s in raw_segments],
        diarize_note=diarization_skip_reason,
    )


# ── Diarization (pyannote.audio) ──────────────────────────────────────────────

_PYANNOTE_PIPELINE = None  # lazy singleton


def _prepare_pyannote_environment() -> None:
    """Set up the env vars + warning filters pyannote needs RIGHT BEFORE
    importing it. Scoped to this call so live dictation / refining /
    other tabs never inherit these settings just because pyannote.audio
    happens to be importable in this process.

    Idempotent, safe to call many times. setdefault keeps user-supplied
    env vars (e.g. an explicit HF_HOME) untouched.
    """
    # Disable pyannote's OpenTelemetry phone-home. Must be set BEFORE
    # `import pyannote.audio`, its telemetry module captures the env
    # var at its own import time. We can't undo this once pyannote is
    # loaded; that's pyannote's design.
    os.environ.setdefault('PYANNOTE_METRICS_ENABLED', 'false')
    os.environ.setdefault('PYANNOTE_OTEL_ENABLED',    'false')

    # Keep on-demand HF downloads inside the SAME data folder the rest
    # of the app uses (storage.appdata_dir()). In frozen mode this is
    # <exe_dir>/data, with a %TEMP%/Hotkeys/ fallback when the install
    # location is read-only. Only matters if a user opts into a
    # non-bundled tier in a future build; bundled `assets/diarization/`
    # is already loaded directly via `_resolve_diarization_model_dir()`.
    try:
        from pathlib import Path as _P
        try:
            from storage import appdata_dir as _appdata_dir
            cache = _P(_appdata_dir()) / '.hf_cache'
        except Exception:
            import sys as _sys
            root = (
                _P(_sys.executable).resolve().parent
                if getattr(_sys, 'frozen', False)
                else _P(__file__).resolve().parents[1]
            )
            cache = root / '.hf_cache'
        cache.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('HF_HOME',               str(cache))
        os.environ.setdefault('HUGGINGFACE_HUB_CACHE', str(cache / 'hub'))
        os.environ.setdefault('TRANSFORMERS_CACHE',    str(cache / 'transformers'))
    except Exception:
        pass

# Diarization needs the full waveform in RAM (pyannote clusters embeddings
# across the whole file). Cap that explicitly so a 6-hour podcast doesn't
# silently OOM the user. At 16 kHz float32 mono:
#   90 min  ≈ 520 MB  ← safe for any modern PC
#   180 min ≈ 1.0 GB  ← OK on 8+ GB
#   360 min ≈ 2.1 GB  ← risky on low-end
#
# Anything over the cap falls back to transcription-only with a clear log
# line. The caller (UI) sees `job.diarized == False` and can surface that
# to the user.
DIARIZE_MAX_SECONDS = 90 * 60  # 90 min


class _AudioTooLongForDiarization(RuntimeError):
    """Raised when the file exceeds DIARIZE_MAX_SECONDS, engine catches
    this and falls back to transcription-only."""


def _probe_duration(audio_path: str) -> float:
    """Fast duration probe via PyAV, works on every format faster-whisper
    accepts (WAV/MP3/MP4/M4A/MOV/WebM/OGG/FLAC/...) because PyAV wraps
    ffmpeg, which is already bundled for the rest of the app.

    Falls back to soundfile.info() for the WAV/FLAC fast path, and finally
    to 0.0 if both fail, callers treat 0.0 as "unknown" and skip duration
    pre-flight checks.
    """
    try:
        import av
        with av.open(audio_path) as container:
            if container.duration:
                return float(container.duration) / 1_000_000.0  # μs → s
            # Fallback for streams without container-level duration
            stream = container.streams.audio[0]
            if stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
    except Exception:
        pass
    try:
        import soundfile as sf
        info = sf.info(audio_path)
        return float(info.frames) / float(info.samplerate or 1)
    except Exception:
        return 0.0


def _load_audio_for_pyannote(audio_path: str) -> dict:
    """Load `audio_path` as a mono 16 kHz torch tensor, bypasses torchaudio's
    backend dispatch (which can fail with libtorchcodec DLL errors on
    Windows) AND soundfile's narrow format support (WAV/FLAC/OGG only).

    Uses faster-whisper's own decode_audio() which goes through PyAV/ffmpeg
   , the exact same code path Whisper uses to transcribe, so anything that
    works for transcription also works for diarization. This means MP3,
    MP4, M4A, MOV, WebM, AAC, OPUS, etc. all decode correctly.

    Returns the dict pyannote accepts directly so the pipeline never
    touches file I/O itself.

    Raises _AudioTooLongForDiarization if the file would exceed our memory
    safety cap when loaded as one contiguous tensor.
    """
    import numpy as np
    import torch
    from faster_whisper.audio import decode_audio

    dur = _probe_duration(audio_path)
    if dur > DIARIZE_MAX_SECONDS:
        raise _AudioTooLongForDiarization(
            f'audio is {dur/60:.1f} min, diarization caps at '
            f'{DIARIZE_MAX_SECONDS/60:.0f} min to keep RAM safe; '
            f'transcription will continue without speaker labels'
        )

    # decode_audio returns a 1-D float32 numpy array at the requested
    # sample_rate (default 16 kHz), mono mixdown handled internally via
    # ffmpeg.  Works on every codec ffmpeg supports.
    data = decode_audio(audio_path, sampling_rate=16000)
    if not isinstance(data, np.ndarray):
        data = np.asarray(data, dtype=np.float32)
    elif data.dtype != np.float32:
        data = data.astype(np.float32)
    # Pyannote expects (channels, samples) so unsqueeze the channel dim.
    waveform = torch.from_numpy(np.ascontiguousarray(data)).unsqueeze(0)
    return {'waveform': waveform, 'sample_rate': 16000}


def _run_diarization_subprocess(audio_path: str, *,
                                min_speakers=None, max_speakers=None,
                                on_progress=None) -> list[tuple]:
    """Out-of-process diarization for FROZEN dist builds.

    Why: torch + pyannote bundled with ctranslate2 / onnxruntime / numpy / av
    in the same process produces STATUS_STACK_BUFFER_OVERRUN (0xc0000409)
    heap corruption from duplicate MKL / OpenMP runtimes. Running pyannote
    in its OWN exe (diarize.exe, next to Hotkeys.exe) keeps the main process
    clean. Parent decodes the audio with faster-whisper's PyAV path, hands
    the waveform to the worker via numpy.save, worker writes a JSON list of
    speaker turns back. See diarize_worker.py + hotkeys_diarize.spec.
    """
    import sys as _sys
    import subprocess as _sp
    import tempfile
    import shutil as _sh
    from pathlib import Path as _P

    diar_exe = _P(_sys.executable).resolve().parent / 'diarize' / 'diarize.exe'
    if not diar_exe.exists():
        raise RuntimeError(
            'speaker labels unavailable in this build (diarize worker missing); '
            'transcript saved without speaker labels'
        )

    # Decode audio in-process using the SAME path the main transcribe uses.
    audio_in = _load_audio_for_pyannote(audio_path)
    waveform = audio_in['waveform'].squeeze(0).cpu().numpy()
    sample_rate = int(audio_in['sample_rate'])

    if on_progress:
        on_progress(0.1)

    # Resolve bundled model dir relative to the worker exe's own _internal
    # (PyInstaller puts data files there). Hand the worker an absolute path.
    model_dir = (_P(_sys.executable).resolve().parent / 'diarize' / '_internal'
                 / 'assets' / 'diarization')
    if not model_dir.is_dir():
        # Fall back to the main exe's bundled assets/diarization (the worker
        # spec re-bundles the same files, but if that copy is missing we can
        # share the main exe's copy).
        from storage import assets_dir
        model_dir = _P(assets_dir()) / 'diarization'

    work_dir = _P(tempfile.mkdtemp(prefix='hotkeys_diarize_'))
    try:
        import numpy as _np
        _np.save(work_dir / 'input.npy', waveform.astype(_np.float32))
        cfg = {
            'sample_rate':  sample_rate,
            'min_speakers': min_speakers,
            'max_speakers': max_speakers,
            'model_dir':    str(model_dir),
        }
        (work_dir / 'input.json').write_text(json.dumps(cfg), encoding='utf-8')

        # Spawn worker. Detach fully so any worker-side issues stay isolated
        # from the main process — that was the whole reason for splitting.
        creation = 0
        if _sys.platform == 'win32':
            creation = (
                _sp.CREATE_NO_WINDOW
                | _sp.DETACHED_PROCESS
                | _sp.CREATE_NEW_PROCESS_GROUP
                | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
            )
        proc = _sp.Popen(
            [str(diar_exe), str(work_dir)],
            creationflags=creation,
            close_fds=True,
            stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )

        # Poll for output.json. Cap at 15 minutes for a 90-min audio clip.
        timeout_s = 15 * 60
        import time as _time
        t0 = _time.time()
        out_json = work_dir / 'output.json'
        while True:
            if out_json.exists():
                break
            if proc.poll() is not None and not out_json.exists():
                raise RuntimeError(
                    f'diarize worker exited (code={proc.returncode}) '
                    'before writing output.json'
                )
            if _time.time() - t0 > timeout_s:
                # terminate() sends a soft signal that ONNX-bound workers
                # often ignore. Escalate to kill() if it doesn't exit
                # in 3s, then await the exit so we don't orphan.
                try: proc.terminate()
                except Exception: pass
                try: proc.wait(timeout=3)
                except _sp.TimeoutExpired:
                    try: proc.kill()
                    except Exception: pass
                    try: proc.wait(timeout=3)
                    except Exception: pass
                except Exception: pass
                raise RuntimeError(
                    f'diarize worker did not respond within {timeout_s}s; '
                    'transcript saved without speaker labels'
                )
            if on_progress:
                # Rough progress estimate: nothing to measure from outside,
                # tick from 0.1 → 0.85 linearly with elapsed time so the UI
                # bar stays alive.
                on_progress(min(0.85, 0.1 + (_time.time() - t0) / timeout_s * 0.75))
            _time.sleep(0.5)

        response = json.loads(out_json.read_text(encoding='utf-8'))
        if not response.get('ok'):
            raise RuntimeError(
                'diarization failed: ' + str(response.get('error', 'unknown'))
            )
        if on_progress:
            on_progress(0.95)
        return [tuple(t) for t in response.get('turns', [])]
    finally:
        try:
            _sh.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


def _run_diarization(audio_path: str, *, min_speakers=None, max_speakers=None,
                     on_progress: Callable[[float], None] | None = None) -> list[tuple]:
    """Run pyannote.audio speaker-diarization on the file. Returns a list of
    (start, end, label) tuples sorted by start time.

    In FROZEN dist mode this delegates to diarize.exe (out-of-process worker)
    so torch + pyannote runtime DLLs don't conflict with the main exe's
    ctranslate2 / onnxruntime / numpy bundle. In dev mode (running from
    source) we import pyannote directly, since the dev venv loads libraries
    lazily from site-packages without the bundling conflict.
    """
    import sys as _sys
    if getattr(_sys, 'frozen', False):
        return _run_diarization_subprocess(
            audio_path,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            on_progress=on_progress,
        )
    # ── DEV MODE: in-process pyannote (the original implementation) ─────────
    global _PYANNOTE_PIPELINE
    if _PYANNOTE_PIPELINE is None:
        # All the process-global setup happens HERE, right before pyannote
        # is imported, never at module load. Other features never inherit
        # these settings.
        _prepare_pyannote_environment()
        # Silence pyannote/torchaudio's torchcodec warning for the
        # duration of THIS pipeline import only. Returning to the previous
        # warning state preserves any filters the user/other code wants.
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message='.*torchcodec.*')
            warnings.filterwarnings('ignore', message='.*libtorchcodec.*')
            from pyannote.audio import Pipeline
        # Bundled model path resolution. At dev time the pipeline is loaded
        # from HF cache (one-time download); at dist time we embed the model
        # under assets/diarization and load offline.
        bundled = _resolve_diarization_model_dir()
        if bundled is not None:
            logger.info(f'Loading pyannote pipeline from bundled dir: {bundled}')
            _PYANNOTE_PIPELINE = Pipeline.from_pretrained(str(bundled))
        else:
            # No bundled model, only proceed if we have an explicit token.
            # Falling back to `token=True` (HF's "use cached login") raises
            # a cryptic GatedRepoError when nothing is cached, which the
            # UI then surfaces as a useless one-liner. Refuse early with a
            # clear message instead.
            tok = os.environ.get('HF_TOKEN')
            if not tok:
                # User-visible: no jargon about HF, tokens, or bundling.
                # The dist always ships with the bundled model, so this only
                # fires for devs running from a stripped source tree.
                raise RuntimeError(
                    'speaker detection unavailable in this build, '
                    'transcript saved without speaker labels'
                )
            logger.info('Loading pyannote pipeline from HF cache (community-1)')
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', message='.*torchcodec.*')
                warnings.filterwarnings('ignore', message='.*libtorchcodec.*')
                _PYANNOTE_PIPELINE = Pipeline.from_pretrained(
                    'pyannote/speaker-diarization-community-1',
                    token=tok,
                )

    if on_progress:
        on_progress(0.1)

    # Hand pyannote a pre-loaded tensor so we never trip over torchaudio's
    # torchcodec/ffmpeg backend resolution, that path is broken on stock
    # Windows venvs and would block plug-and-play dist.
    audio_in = _load_audio_for_pyannote(audio_path)

    kw = {}
    if min_speakers is not None: kw['min_speakers'] = min_speakers
    if max_speakers is not None: kw['max_speakers'] = max_speakers
    result = _PYANNOTE_PIPELINE(audio_in, **kw)

    if on_progress:
        on_progress(0.9)

    # community-1 returns a DiarizeOutput object exposing two Annotations:
    # `.speaker_diarization` (may overlap) and `.exclusive_speaker_diarization`
    # (strictly disjoint). For overlap-to-text assignment we prefer the
    # exclusive variant when available, every Whisper segment gets at most
    # one speaker, which matches the UI's speaker-chip-per-turn rendering.
    # Older pyannote 2.x returns an Annotation directly; handle both.
    annotation = None
    for attr in ('exclusive_speaker_diarization', 'speaker_diarization'):
        cand = getattr(result, attr, None)
        if cand is not None and hasattr(cand, 'itertracks'):
            annotation = cand
            break
    if annotation is None and hasattr(result, 'itertracks'):
        annotation = result

    if annotation is None:
        # pyannote produced no diarization (all-silence audio, or a result
        # variant we don't know about). Return empty turns so the caller
        # renders the transcript without speaker labels — that's the right
        # degraded-mode behaviour, not an error.
        logger.info(
            'pyannote produced no diarization data for this audio; '
            'transcript will have no speaker labels'
        )
        return []

    turns: list[tuple] = []
    for turn, _, label in annotation.itertracks(yield_label=True):
        turns.append((float(turn.start), float(turn.end), str(label)))
    turns.sort(key=lambda t: t[0])
    return turns


def _resolve_diarization_model_dir() -> Path | None:
    """Path to a bundled pyannote pipeline config, or None to fall back to HF."""
    try:
        from storage import assets_dir
        d = Path(assets_dir()) / 'diarization'
        if (d / 'config.yaml').exists():
            return d
    except Exception:
        pass
    return None


def _assign_speakers(segments: list[TranscriptSegment], turns: list[tuple]) -> None:
    """For each whisper segment, pick the diarization speaker with the largest
    time overlap. Re-label as 'Speaker 1', 'Speaker 2', ... in first-seen order
    so downstream UI gets stable, human-readable names."""
    if not turns:
        return

    # Build a mapping from pyannote's raw label ('SPEAKER_00') → 'Speaker N'
    relabel: dict[str, str] = {}
    def _humanize(raw: str) -> str:
        if raw not in relabel:
            relabel[raw] = f'Speaker {len(relabel) + 1}'
        return relabel[raw]

    for seg in segments:
        best_label, best_overlap = '', 0.0
        for t_start, t_end, raw_label in turns:
            if t_end < seg.start:
                continue
            if t_start > seg.end:
                break
            overlap = max(0.0, min(seg.end, t_end) - max(seg.start, t_start))
            if overlap > best_overlap:
                best_overlap, best_label = overlap, raw_label
        if best_label:
            seg.speaker = _humanize(best_label)
