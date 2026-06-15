"""Media operations library, the backend for the F9 "Media Tools" panel.

Every operation is a stand-alone function with a consistent signature:

    op(*inputs, out_path, **options,
       on_progress: Callable[[float], None] | None = None,
       on_log:      Callable[[str], None]   | None = None,
       should_cancel: Callable[[], bool]    | None = None) -> Path | dict

Functions return the output Path when they produce a file, or a dict of
extracted data (metadata, language probabilities, speech segments).

Heavy lifting goes through:
  • ffmpeg  via imageio-ffmpeg's bundled ffmpeg.exe, already in dist
  • yt-dlp  for URL ingest / metadata / subtitles / thumbnails / playlists
  • faster-whisper for translate + language detect
  • noisereduce + numpy for offline noise reduction
  • soundfile + onnxruntime (Silero VAD) for speech segments

Each function is callable from a CLI test, the F9 worker thread, or any
other context, no Tk / UI dependencies.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


# ── ffmpeg resolution + thin wrapper ─────────────────────────────────────────

def _ffmpeg_path() -> str:
    """Same resolution order as yt-dlp's ffmpeg_location, bundled binary
    first, then PATH. Raises if neither found."""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    import shutil
    p = shutil.which('ffmpeg')
    if p:
        return p
    raise RuntimeError(
        'ffmpeg not found, bundled imageio_ffmpeg should provide it. '
        'Reinstall the app or `pip install imageio-ffmpeg`.'
    )


def _run_ffmpeg(args: list[str],
                duration_hint: float = 0.0,
                on_progress: Callable[[float], None] | None = None,
                on_log:      Callable[[str], None]   | None = None,
                should_cancel: Callable[[], bool]    | None = None) -> None:
    """Run ffmpeg with `args` prepended after the binary. Streams stderr to
    parse `time=HH:MM:SS.xx` progress lines and forward as 0.0-1.0 to the
    callback. `duration_hint` is the source duration in seconds, when 0
    we can't compute a percentage so the callback gets a pulsing fraction.

    Captures stderr (where ffmpeg writes progress) line-by-line; stdout
    typically empty unless the user asked for pipe output. Long-running
    ffmpeg calls (a 2-hour transcode) won't block forever if we kill the
    process on cancel.
    """
    log = lambda m: (on_log and on_log(m)) or logger.debug(m)
    cmd = [_ffmpeg_path(), '-hide_banner', '-y', '-nostdin', '-loglevel', 'info',
           *args]
    # Log the full command at INFO level so post-mortem on a failed
    # ffmpeg run can see exactly what was invoked, without needing
    # DEBUG-level logging enabled.
    logger.info('ffmpeg cmd: ' + ' '.join(cmd[1:]))
    log('ffmpeg: ' + ' '.join(cmd[1:]))

    # CREATE_NO_WINDOW so the dist exe doesn't flash a console window for
    # every ffmpeg invocation.
    creationflags = 0
    if sys.platform == 'win32':
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        creationflags=creationflags,
    )

    time_re = re.compile(r'time=(\d+):(\d+):(\d+\.\d+)')
    pulse = 0.0
    # Keep the last N stderr lines so the error path can actually report
    # what ffmpeg said. Previously stderr was drained line-by-line for
    # progress parsing, then read() at error-time returned '' because
    # the pipe was already empty, leaving us with a totally opaque
    # "ffmpeg exited 1234567:" with no message.
    from collections import deque
    stderr_tail = deque(maxlen=60)
    try:
        while True:
            line = proc.stderr.readline() if proc.stderr else ''
            if not line:
                if proc.poll() is not None:
                    break
                continue
            line = line.rstrip()
            stderr_tail.append(line)
            if should_cancel and should_cancel():
                proc.kill()
                raise RuntimeError('cancelled')
            m = time_re.search(line)
            if m and on_progress:
                h, mn, sec = m.groups()
                cur = int(h) * 3600 + int(mn) * 60 + float(sec)
                if duration_hint > 0:
                    on_progress(min(1.0, cur / duration_hint))
                else:
                    pulse = min(0.95, pulse + 0.02)
                    on_progress(pulse)
    finally:
        # ffmpeg can hang indefinitely on a frozen mux / unreachable protocol
        # input; without a timeout the worker thread blocks forever and the
        # whole transcribe pipeline stalls. 30s after stderr close is far
        # past any normal exit; if it's still alive then, kill it.
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try: proc.kill()
            except Exception: pass
            try: proc.wait(timeout=5)
            except Exception: pass
    if proc.returncode != 0:
        tail_text = '\n'.join(stderr_tail)[-1200:].strip()
        # Try to surface the most actionable line. ffmpeg's last error
        # line is usually the most useful; common patterns include
        # "Conversion failed!", "Error opening input", "No such file",
        # "Invalid data found", etc.
        err_hint = ''
        for ln in reversed(stderr_tail):
            low = ln.lower()
            if any(k in low for k in (
                'error', 'failed', 'invalid', 'no such', 'unable',
                'protocol not found', 'codec not currently supported',
            )):
                err_hint = ln
                break
        # Log the full tail at WARNING so the app log keeps a record.
        logger.warning(
            f'ffmpeg failed (exit {proc.returncode} / 0x{proc.returncode & 0xffffffff:08x}). '
            f'Stderr tail:\n{tail_text}'
        )
        msg = f'ffmpeg exited {proc.returncode}'
        if err_hint:
            msg += f': {err_hint}'
        elif tail_text:
            msg += f': {tail_text[-300:]}'
        raise RuntimeError(msg)
    if on_progress:
        on_progress(1.0)


def _probe_duration(path: str | Path) -> float:
    """Fast duration probe via PyAV, same helper engine.py uses, mirrored
    here so tools.py doesn't import the engine module."""
    try:
        import av
        with av.open(str(path)) as c:
            if c.duration:
                return float(c.duration) / 1_000_000.0
            s = c.streams[0]
            if s.duration and s.time_base:
                return float(s.duration * s.time_base)
    except Exception:
        pass
    return 0.0


# Map of file extension to (ffmpeg encoder name, default bitrate).
# Used so audio-modifying ops (normalize, denoise, change_speed) can
# write output in the same format as the input instead of bloating
# everything to uncompressed WAV.
_AUDIO_OUT_DEFAULTS = {
    '.mp3':  ('libmp3lame', '192k'),
    '.m4a':  ('aac',        '192k'),
    '.aac':  ('aac',        '192k'),
    '.mp4':  ('aac',        '192k'),
    '.mkv':  ('aac',        '192k'),
    '.webm': ('libopus',    '128k'),
    '.ogg':  ('libvorbis',  '128k'),
    '.opus': ('libopus',    '128k'),
    '.flac': ('flac',       ''),
    '.wav':  ('pcm_s16le',  ''),
    '.wma':  ('aac',        '192k'),   # no native WMA encoder in most builds
}


# Encoders the bundled ffmpeg knows about. Lazily populated by querying
# `ffmpeg -encoders` once. We use this to validate every -c:a name we
# pass — otherwise an unsupported codec produces ffmpeg's cryptic
# "Encoder not found" error and the user is stuck.
_AVAILABLE_ENCODERS: set[str] | None = None


def _available_encoders() -> set[str]:
    """Set of audio encoder names the current ffmpeg binary supports.
    Lazy + cached: only spawns ffmpeg once per session."""
    global _AVAILABLE_ENCODERS
    if _AVAILABLE_ENCODERS is not None:
        return _AVAILABLE_ENCODERS
    encoders: set[str] = set()
    try:
        out = subprocess.check_output(
            [_ffmpeg_path(), '-hide_banner', '-encoders'],
            stderr=subprocess.STDOUT, text=True, timeout=10,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == 'win32' else 0),
        )
        for ln in out.splitlines():
            # Encoder lines start with ' V', ' A', ' S' (with flags like
            # FS....). The encoder name is the 2nd column.
            s = ln.lstrip()
            if not s or not s[0].isalpha():
                continue
            parts = s.split(None, 2)
            if len(parts) >= 2 and 'A' in parts[0]:
                encoders.add(parts[1].lower())
    except Exception as e:
        logger.warning(f'Could not list ffmpeg encoders: {e}')
    _AVAILABLE_ENCODERS = encoders
    logger.info(f'ffmpeg audio encoders available: {len(encoders)}')
    return encoders


_AUDIO_ONLY_EXTS = {'.mp3', '.m4a', '.aac', '.wav', '.flac',
                    '.ogg', '.opus', '.wma'}


def _reencode_codec_args(in_path: str | Path,
                         out_path: str | Path) -> list[str]:
    """Build `-c:*` args for a reencode pass that respects the output
    container. Audio-only outputs get JUST an audio encoder (no video
    codec to confuse ffmpeg about); video outputs get libx264 + a safe
    audio encoder. Used by trim_media and concat_media, which used to
    hardcode `-c:v libx264 -c:a aac` regardless of output extension —
    which broke when the user asked for an .mp3 output (AAC stream in
    MP3 container is invalid)."""
    out_ext = Path(out_path).suffix.lower()
    if out_ext in _AUDIO_ONLY_EXTS:
        # Pick the right audio encoder for this container.
        return _matched_output_args(in_path, out_path)
    # Video container — keep H.264 video + safe audio encoder.
    args = ['-c:v', 'libx264']
    aac, _ = _resolve_safe_encoder('aac', '.m4a')
    if aac:
        args += ['-c:a', aac, '-b:a', '192k']
    return args


def _resolve_safe_encoder(preferred: str, out_ext: str) -> tuple[str, str]:
    """Given a desired encoder + output extension, return (encoder,
    bitrate) that the current ffmpeg can actually USE. Falls through:
      1. preferred encoder, if available
      2. extension's canonical default, if available
      3. universal safe fallback: aac (in mp4-family) or pcm_s16le (wav)
    The bitrate string is empty for lossless codecs."""
    available = _available_encoders()
    # 1. Try the preferred name
    if preferred and preferred.lower() in available:
        return (preferred, '')
    # 2. Try the extension default
    fallback = _AUDIO_OUT_DEFAULTS.get(out_ext.lower())
    if fallback:
        enc, br = fallback
        if enc.lower() in available:
            return (enc, br)
    # 3. Universal safe fallback. aac is built into ffmpeg without
    # needing libfdk/libfaac; pcm_s16le is always present. Pick by ext.
    if out_ext.lower() == '.wav':
        return ('pcm_s16le', '')
    if 'aac' in available:
        return ('aac', '192k')
    if 'libmp3lame' in available:
        return ('libmp3lame', '192k')
    # Total failure mode — let ffmpeg auto-pick something or fail
    # with a clearer "no encoder for this container" message.
    return ('', '')


def _input_audio_codec(path: str | Path) -> tuple[str, str]:
    """Inspect the input and return (ffmpeg-encoder-name, bitrate-str)
    suitable for re-encoding to the same format with the same fidelity.

    Returns ('', '') if the codec can't be detected; the caller should
    then fall back to whatever ffmpeg picks from the output extension.
    """
    try:
        import av
        with av.open(str(path)) as c:
            audio = next((s for s in c.streams if s.type == 'audio'), None)
            if audio is None: return ('', '')
            codec_name = (audio.codec_context.name or '').lower()
            # Map decoder name to encoder name (often identical, but mp3
            # decodes as 'mp3' and encodes as 'libmp3lame'; opus / vorbis
            # need 'libopus' / 'libvorbis' on encode).
            decoder_to_encoder = {
                'mp3':    'libmp3lame',
                'opus':   'libopus',
                'vorbis': 'libvorbis',
            }
            encoder = decoder_to_encoder.get(codec_name, codec_name)
            bitrate = ''
            try:
                br = audio.bit_rate or c.bit_rate or 0
                if br > 0:
                    bitrate = f'{int(br // 1000)}k'
            except Exception:
                pass
            return (encoder, bitrate)
    except Exception:
        return ('', '')


def _matched_output_args(in_path: str | Path,
                         out_path: str | Path) -> list[str]:
    """Build ffmpeg `-c:a` / `-b:a` flags so the output matches the input
    when the extensions agree, and falls back to a sensible default for
    the output extension when they differ.

    Saves the user from getting a 150 MB WAV when they normalize a 4 MB
    MP3, which is the kind of "what just happened" surprise that erodes
    trust in the tool.
    """
    in_ext  = Path(in_path).suffix.lower()
    out_ext = Path(out_path).suffix.lower()
    # Same container: copy input codec + bitrate so size / quality match.
    preferred = ''
    preferred_br = ''
    if in_ext == out_ext:
        preferred, preferred_br = _input_audio_codec(in_path)
    # Always resolve through the safe encoder picker so we never ask
    # ffmpeg for an encoder it doesn't have ("Encoder not found").
    codec, default_br = _resolve_safe_encoder(preferred, out_ext)
    if not codec:
        return []
    # Prefer the input's detected bitrate (preserves "feel" of the
    # original) if the picked codec is lossy; lossless ignores bitrate.
    bitrate = preferred_br or default_br
    if codec in ('flac', 'pcm_s16le', 'pcm_s24le', 'pcm_s32le'):
        bitrate = ''
    args = ['-c:a', codec]
    if bitrate:
        args += ['-b:a', bitrate]
    return args


# ── Audio extraction / conversion ────────────────────────────────────────────

def extract_audio(in_path: str | Path,
                  out_path: str | Path,
                  *, codec: str = 'mp3',
                  bitrate: str = '192k',
                  on_progress=None, on_log=None, should_cancel=None) -> Path:
    """Pull the audio stream out of a video (or re-encode an audio file).
    `codec` is the ffmpeg encoder name (mp3, aac, opus, flac, libvorbis,
    pcm_s16le, etc.).  `bitrate` only matters for lossy codecs.
    """
    in_p, out_p = Path(in_path), Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    # Validate the requested codec against the bundled ffmpeg's actual
    # encoder list; fall back to a safe alternative if missing.
    if codec.lower() not in _available_encoders():
        safe_codec, safe_br = _resolve_safe_encoder(codec, out_p.suffix)
        if safe_codec:
            logger.warning(
                f'extract_audio: requested codec {codec!r} not available '
                f'in bundled ffmpeg; falling back to {safe_codec!r}.'
            )
            codec = safe_codec
            bitrate = safe_br or bitrate
    args = ['-i', str(in_p), '-vn',
            '-c:a', codec]
    if codec not in ('flac', 'pcm_s16le', 'pcm_s24le'):
        args += ['-b:a', bitrate]
    args.append(str(out_p))
    _run_ffmpeg(args, _probe_duration(in_p),
                on_progress=on_progress, on_log=on_log,
                should_cancel=should_cancel)
    return out_p


# User-friendly aliases for the audio operation dropdown
AUDIO_FORMATS = {
    # Use ENCODER names (not decoder names) so extract_audio doesn't
    # have to fall back via _resolve_safe_encoder. MP3 decodes as
    # 'mp3' but encodes as 'libmp3lame'; same name-mismatch for opus.
    'MP3 (most compatible)':    ('libmp3lame', '192k'),
    'M4A / AAC (smaller)':      ('aac',        '192k'),
    'Opus (best quality/size)': ('libopus',    '128k'),
    'FLAC (lossless)':          ('flac',       ''),
    'WAV (uncompressed)':       ('pcm_s16le',  ''),
}


def convert_audio(in_path, out_path, fmt_label: str, **kw) -> Path:
    """Convert any audio/video to the picked AUDIO_FORMATS preset."""
    codec, bitrate = AUDIO_FORMATS.get(fmt_label, ('mp3', '192k'))
    return extract_audio(in_path, out_path, codec=codec, bitrate=bitrate, **kw)


# ── Loudness normalization (EBU R128) ────────────────────────────────────────

def normalize_loudness(in_path, out_path,
                       *, target_lufs: float = -16.0,
                       on_progress=None, on_log=None,
                       should_cancel=None) -> Path:
    """One-pass EBU R128 normalization. Two-pass would be more accurate
    but doubles the runtime; for a user-facing 'make it louder' button
    the single-pass loudnorm is close enough.

    Output codec + bitrate are chosen to match the input format so the
    user doesn't get a 150 MB WAV from a 4 MB MP3.
    """
    in_p, out_p = Path(in_path), Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    filt = f'loudnorm=I={target_lufs}:TP=-1.5:LRA=11'
    args = ['-i', str(in_p), '-af', filt]
    args += _matched_output_args(in_p, out_p)
    args.append(str(out_p))
    _run_ffmpeg(args, _probe_duration(in_p),
                on_progress=on_progress, on_log=on_log,
                should_cancel=should_cancel)
    return out_p


# ── Speed / pitch shift ──────────────────────────────────────────────────────

def change_speed(in_path, out_path,
                 *, factor: float = 1.5,
                 preserve_pitch: bool = True,
                 on_progress=None, on_log=None,
                 should_cancel=None) -> Path:
    """Re-time playback by `factor` (1.0 = no change, 2.0 = twice as fast,
    0.5 = half speed). `preserve_pitch=True` uses ffmpeg's `atempo` filter
    which keeps the original pitch, what users expect when they "speed up
    a podcast." atempo is clamped to 0.5-2.0 per filter, so for extreme
    changes we chain multiple atempo passes.
    """
    in_p, out_p = Path(in_path), Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    if preserve_pitch:
        # Decompose factor into chain of atempo calls each within 0.5-2.0
        chain = []
        rem = factor
        while rem > 2.0:
            chain.append('atempo=2.0'); rem /= 2.0
        while rem < 0.5:
            chain.append('atempo=0.5'); rem /= 0.5
        chain.append(f'atempo={rem:.4f}')
        filt = ','.join(chain)
    else:
        # rubberband-free pitch shift: change sample rate then setpts
        filt = f'asetrate=48000*{factor:.4f},aresample=48000'
    args = ['-i', str(in_p), '-af', filt]
    args += _matched_output_args(in_p, out_p)
    args.append(str(out_p))
    _run_ffmpeg(args, _probe_duration(in_p) / max(factor, 0.01),
                on_progress=on_progress, on_log=on_log,
                should_cancel=should_cancel)
    return out_p


# ── Noise reduction (offline) ────────────────────────────────────────────────

def reduce_noise(in_path, out_path,
                 *, prop_decrease: float = 0.85,
                 on_progress=None, on_log=None,
                 should_cancel=None) -> Path:
    """Apply spectral-gating noise reduction. `noisereduce` is already
    bundled (used by live dictation).

    Output format follows the output extension and matches input fidelity
    when the extensions agree, so a 4 MB MP3 in produces a similarly-sized
    MP3 out, not a 50 MB WAV.

    Loads the full waveform into RAM, fine for clips up to ~3 hours
    (mono 16 kHz float32 ≈ 700 MB for 3 h). For longer files we still try
    but a numpy MemoryError is plausible on low-end PCs.
    """
    import soundfile as sf
    import noisereduce as nr
    from faster_whisper.audio import decode_audio
    import tempfile, os as _os

    in_p, out_p = Path(in_path), Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    (on_log or logger.info)(f'noisereduce: loading {in_p}')
    if on_progress: on_progress(0.05)
    data = decode_audio(str(in_p), sampling_rate=16000)
    if should_cancel and should_cancel():
        raise RuntimeError('cancelled')
    if on_progress: on_progress(0.20)
    (on_log or logger.info)('noisereduce: running spectral gate…')
    cleaned = nr.reduce_noise(y=data, sr=16000,
                              prop_decrease=prop_decrease,
                              stationary=False)
    if should_cancel and should_cancel():
        raise RuntimeError('cancelled')
    if on_progress: on_progress(0.65)

    # Stage 1: write cleaned audio to a temp WAV (soundfile only knows
    # uncompressed / FLAC / OGG, not MP3 / AAC, so we hand off to ffmpeg).
    tmp_wav = Path(tempfile.mkstemp(suffix='.wav')[1])
    try:
        sf.write(str(tmp_wav), cleaned, 16000, subtype='PCM_16')
        if on_progress: on_progress(0.80)

        # Stage 2: re-encode to the user-chosen container with codec /
        # bitrate that match what the input had. If the output is .wav,
        # _matched_output_args picks pcm_s16le and the second pass is
        # essentially a copy.
        args = ['-i', str(tmp_wav)]
        args += _matched_output_args(in_p, out_p)
        args.append(str(out_p))
        _run_ffmpeg(args, max(_probe_duration(in_p), 1.0),
                    on_progress=lambda p: (
                        on_progress(0.80 + p * 0.20) if on_progress else None),
                    on_log=on_log,
                    should_cancel=should_cancel)
    finally:
        try: _os.remove(tmp_wav)
        except Exception: pass

    if on_progress: on_progress(1.0)
    return out_p


# ── Trim a section ───────────────────────────────────────────────────────────

def _hhmmss(s: float) -> str:
    """Format seconds as HH:MM:SS.mmm for ffmpeg -ss / -to."""
    s = max(0.0, float(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{int(h):02d}:{int(m):02d}:{sec:06.3f}'


def trim_media(in_path, out_path,
               *, start: float = 0.0,
               end: float | None = None,
               reencode: bool = False,
               on_progress=None, on_log=None,
               should_cancel=None) -> Path:
    """Cut [start, end] from a media file. By default uses stream-copy
    (`-c copy`) for near-instant, lossless cuts, but cuts only land on
    keyframes, so the trim may be off by up to a GOP (~2-10 s of video).
    Pass `reencode=True` for frame-accurate cuts (slower).

    `end=None` means "to the end of file".
    """
    in_p, out_p = Path(in_path), Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    args = ['-ss', _hhmmss(start)]
    if end is not None:
        args += ['-to', _hhmmss(end)]
    args += ['-i', str(in_p)]
    if not reencode:
        args += ['-c', 'copy']
    else:
        args += _reencode_codec_args(in_p, out_p)
    args.append(str(out_p))
    dur = (end or _probe_duration(in_p)) - start
    _run_ffmpeg(args, max(dur, 0.0),
                on_progress=on_progress, on_log=on_log,
                should_cancel=should_cancel)
    return out_p


# ── Extract a frame ──────────────────────────────────────────────────────────

def extract_frame(in_path, out_path,
                  *, at: float = 0.0,
                  on_progress=None, on_log=None,
                  should_cancel=None) -> Path:
    """Save one image (PNG/JPG, decided by out_path extension) at the
    given timestamp."""
    in_p, out_p = Path(in_path), Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    args = ['-ss', _hhmmss(at), '-i', str(in_p),
            '-vframes', '1', '-q:v', '2', str(out_p)]
    _run_ffmpeg(args, 0.1,
                on_progress=on_progress, on_log=on_log,
                should_cancel=should_cancel)
    return out_p


# ── Embed subtitles into a video ─────────────────────────────────────────────

def _guess_subtitle_language(srt_path: Path) -> str:
    """Best-effort language tag for an external .srt. Inspects the
    filename for the standard `.<code>.srt` convention used across
    the Whisper / subtitle ecosystem (e.g. `movie.en.srt`,
    `movie.hi.srt`). Returns an ISO 639-2 three-letter code suitable
    for ffmpeg's `language=` metadata flag.

    Falls back to 'und' (undefined) so VLC and the like do not falsely
    label a Hindi track as English."""
    stem = Path(srt_path).stem.lower()  # e.g. 'alien.en' from 'alien.en.srt'
    # Inner code: 'alien.en' -> 'en'; 'alien.orig' -> 'orig'
    inner = stem.rsplit('.', 1)[-1] if '.' in stem else ''
    # ISO 639-1 -> 639-2 lookup for the codes the Whisper UI exposes.
    iso2 = {
        'en':'eng','es':'spa','fr':'fra','de':'deu','it':'ita','pt':'por',
        'nl':'nld','ru':'rus','pl':'pol','ja':'jpn','zh':'zho','ko':'kor',
        'ar':'ara','hi':'hin','tr':'tur','sv':'swe','no':'nor','da':'dan',
        'fi':'fin','el':'ell','he':'heb','th':'tha','vi':'vie','id':'ind',
    }
    return iso2.get(inner, 'und')


def embed_subtitles(in_video, in_subs, out_path,
                    *, burn: bool = False,
                    language: str | None = None,
                    on_progress=None, on_log=None,
                    should_cancel=None) -> Path:
    """Combine a video file with an external .srt subtitle file.

    burn=False (default, soft-mux, fast):
        Add the .srt as a subtitle track inside the output container.
        The video and audio streams are copied with no re-encode, so the
        operation finishes in seconds even for a 2-hour movie. The user's
        player must support showing subtitle tracks (every common player
        does). MKV gets a native SRT track; MP4 needs mov_text, which
        ffmpeg converts to automatically.

    burn=True (hard-burn, slow, irreversible):
        Rasterize the subtitle text onto every video frame. The result
        always shows subtitles, even on players or platforms that strip
        subtitle tracks (some social uploads). This re-encodes the video
        so it takes roughly real-time on CPU; the original cannot be
        recovered from the output.
    """
    in_v, in_s, out_p = Path(in_video), Path(in_subs), Path(out_path)
    if not in_v.exists():
        raise FileNotFoundError(f'video not found: {in_v}')
    if not in_s.exists():
        raise FileNotFoundError(f'subtitle file not found: {in_s}')
    out_p.parent.mkdir(parents=True, exist_ok=True)

    dur = _probe_duration(in_v)

    if burn:
        # ffmpeg subtitles filter needs forward-slashed, escape-quoted path.
        # On Windows the drive colon also needs escaping (e.g. C\:/Users/...).
        sub_arg = str(in_s).replace('\\', '/').replace(':', r'\:')
        filt = f"subtitles='{sub_arg}'"
        args = [
            '-i', str(in_v),
            '-vf', filt,
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '20',
            '-c:a', 'copy',
            str(out_p),
        ]
    else:
        # Soft-mux: pick the right subtitle codec for the output container.
        out_ext = out_p.suffix.lower()
        sub_codec = 'mov_text' if out_ext == '.mp4' else 'srt'
        lang_tag = language or _guess_subtitle_language(in_s)
        args = [
            '-i', str(in_v),
            '-i', str(in_s),
            '-map', '0', '-map', '1',
            '-c', 'copy',
            '-c:s', sub_codec,
            f'-metadata:s:s:0', f'language={lang_tag}',
            '-disposition:s:0', 'default',
            str(out_p),
        ]
    _run_ffmpeg(args, max(dur, 1.0),
                on_progress=on_progress, on_log=on_log,
                should_cancel=should_cancel)
    return out_p


# ── Concat multiple files ────────────────────────────────────────────────────

def concat_media(in_paths: list, out_path,
                 *, reencode: bool = False,
                 on_progress=None, on_log=None,
                 should_cancel=None) -> Path:
    """Join multiple media files into one. With `reencode=False`, all
    inputs MUST share the same codec/sample rate/dimensions (fast, lossless,
    but fails on mismatched inputs). With `reencode=True`, normalizes to
    libx264/aac and works on anything.
    """
    if not in_paths:
        raise ValueError('concat_media needs at least one input')
    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    # Build the concat manifest in a temp file, ffmpeg's concat demuxer
    # reads `file '...'` lines.
    with tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', delete=False, encoding='utf-8') as mf:
        for p in in_paths:
            safe = str(Path(p).resolve()).replace("'", "'\\''")
            mf.write(f"file '{safe}'\n")
        manifest = mf.name
    try:
        args = ['-f', 'concat', '-safe', '0', '-i', manifest]
        if not reencode:
            args += ['-c', 'copy']
        else:
            # Use the first input as the representative audio source
            # for codec detection (concat assumes inputs share codecs).
            args += _reencode_codec_args(in_paths[0], out_p)
        args.append(str(out_p))
        dur = sum(_probe_duration(p) for p in in_paths)
        _run_ffmpeg(args, dur,
                    on_progress=on_progress, on_log=on_log,
                    should_cancel=should_cancel)
    finally:
        try: os.remove(manifest)
        except Exception: pass
    return out_p


# ── Voice Activity Detection (Silero ONNX) ───────────────────────────────────

def find_speech_segments(in_path,
                         *, threshold: float = 0.5,
                         min_speech_ms: int = 250,
                         min_silence_ms: int = 250,
                         on_progress=None, on_log=None,
                         should_cancel=None) -> dict:
    """Run Silero VAD over `in_path` and return list of speech intervals.

    Returns:
        {
          'duration':  float (total file duration, seconds),
          'segments':  list of {'start': float, 'end': float} (seconds),
          'speech_s':  float (total speech time),
          'silence_s': float (total non-speech time),
        }
    """
    import numpy as np
    from faster_whisper.audio import decode_audio
    # Silero VAD ONNX ships in faster_whisper itself; reuse the loader
    # rather than wrangling our own copy.
    from faster_whisper.vad import get_speech_timestamps, VadOptions

    if on_progress: on_progress(0.1)
    (on_log or logger.info)(f'VAD: decoding {in_path}')
    audio = decode_audio(str(in_path), sampling_rate=16000)
    if should_cancel and should_cancel():
        raise RuntimeError('cancelled')
    if on_progress: on_progress(0.4)

    # faster-whisper exposes the VAD as get_speech_timestamps(audio,
    # vad_options=VadOptions(...)).  The dataclass field set is stable
    # across recent versions; we pass kwargs that exist on all of them.
    opts = VadOptions(
        threshold=threshold,
        min_speech_duration_ms=min_speech_ms,
        min_silence_duration_ms=min_silence_ms,
    )
    stamps = get_speech_timestamps(audio, vad_options=opts, sampling_rate=16000)

    segments = [
        {'start': float(s['start']) / 16000,
         'end':   float(s['end'])   / 16000}
        for s in stamps
    ]
    duration = len(audio) / 16000
    speech = sum(s['end'] - s['start'] for s in segments)
    if on_progress: on_progress(1.0)
    return {
        'duration':  duration,
        'segments':  segments,
        'speech_s':  speech,
        'silence_s': max(0.0, duration - speech),
    }


# ── Language detection ───────────────────────────────────────────────────────

def detect_language(in_path,
                    *, model: str = 'base',
                    on_progress=None, on_log=None,
                    should_cancel=None) -> dict:
    """Probe the audio's language without doing a full transcription.
    Returns {'language': 'en', 'probability': 0.98, 'all': {...}}."""
    from transcribe.engine import _resolve_model_path, _get_or_load_whisper
    if on_progress: on_progress(0.1)
    log = on_log or logger.info
    log(f'language detection on {in_path}')

    # faster-whisper's `detect_language` returns (language, probability,
    # full distribution). We reuse the cached WhisperModel from
    # transcribe.engine so this op shares RAM with any subsequent
    # transcribe of the same model.
    wm = _get_or_load_whisper(_resolve_model_path(model), 'cpu', 'int8', log)
    if should_cancel and should_cancel():
        raise RuntimeError('cancelled')
    if on_progress: on_progress(0.4)

    # faster-whisper exposes a private `feature_extractor`, use the
    # public path: a 30-s transcribe + immediate stop.
    from faster_whisper.audio import decode_audio
    audio = decode_audio(str(in_path), sampling_rate=16000)
    # Detect from the first ~30 s only (Whisper's window).
    sample = audio[: 30 * 16000]
    lang, prob, all_langs = wm.detect_language(sample)
    # `all_langs` is a list of (lang_code, probability) tuples sorted
    # high-to-low, flatten into a dict for easy lookup by callers.
    all_dict: dict[str, float] = {}
    try:
        for entry in (all_langs or []):
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                all_dict[str(entry[0])] = float(entry[1])
            elif isinstance(entry, dict):
                for k, v in entry.items():
                    all_dict[str(k)] = float(v)
    except Exception:
        pass
    if on_progress: on_progress(1.0)
    return {
        'language':    lang,
        'probability': float(prob),
        'all':         all_dict,
    }


# ── yt-dlp helpers (metadata / subtitles / thumbnail / playlist) ─────────────

def get_metadata(url: str,
                 *, on_log=None, should_cancel=None) -> dict:
    """Fetch info without downloading the video. Returns the most useful
    subset of yt-dlp's `info_dict`, title, channel, duration, view count,
    upload date, description, thumbnail URL, available formats summary.
    """
    import yt_dlp
    if should_cancel and should_cancel():
        raise RuntimeError('cancelled')
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
            'skip_download': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    pick = lambda k: info.get(k)
    return {
        'title':           pick('title'),
        'channel':         pick('channel') or pick('uploader'),
        'duration':        pick('duration'),
        'view_count':      pick('view_count'),
        'upload_date':     pick('upload_date'),
        'description':     pick('description') or '',
        'thumbnail':       pick('thumbnail'),
        'webpage_url':     pick('webpage_url'),
        'id':              pick('id'),
        'extractor':       pick('extractor'),  # 'youtube', 'vimeo', etc.
        'subtitles':       sorted((pick('subtitles') or {}).keys()),
        'auto_subtitles':  sorted((pick('automatic_captions') or {}).keys()),
    }


def get_subtitles(url: str,
                  out_dir: str | Path,
                  *, langs: list = None,
                  auto: bool = True,
                  on_log=None, should_cancel=None) -> list:
    """Save the URL's subtitle tracks (creator-uploaded if available, else
    YouTube auto-generated when `auto=True`) as .srt files in `out_dir`.

    `langs` is a list of ISO codes (`['en', 'es']`) or None for English.
    Returns a list of saved file Paths.
    """
    import yt_dlp
    out_d = Path(out_dir).expanduser().resolve()
    out_d.mkdir(parents=True, exist_ok=True)
    opts = {
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'skip_download':  True,
        'writesubtitles': True,
        'writeautomaticsub': auto,
        'subtitleslangs':  langs or ['en'],
        'subtitlesformat': 'srt',
        'outtmpl':         str(out_d / '%(title).80s [%(id)s].%(ext)s'),
        'windowsfilenames': True,
    }
    if should_cancel and should_cancel():
        raise RuntimeError('cancelled')
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    # Collect .srt files matching the video id
    found = list(out_d.glob('*.srt')) + list(out_d.glob('*.vtt'))
    return [p for p in found if p.stat().st_size > 0]


def get_thumbnail(url: str,
                  out_dir: str | Path,
                  *, on_log=None, should_cancel=None) -> Path:
    """Download the best available thumbnail for `url` into `out_dir`.
    Returns the resolved Path of the saved image."""
    import yt_dlp
    out_d = Path(out_dir).expanduser().resolve()
    out_d.mkdir(parents=True, exist_ok=True)
    opts = {
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'skip_download':     True,
        'writethumbnail':    True,
        'outtmpl':           str(out_d / '%(title).80s [%(id)s].%(ext)s'),
        'windowsfilenames':  True,
    }
    if should_cancel and should_cancel():
        raise RuntimeError('cancelled')
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    vid_id = (info or {}).get('id', '')
    # Thumbnail is saved alongside the (skipped) media file as <stem>.jpg/png
    for ext in ('jpg', 'webp', 'png'):
        for p in out_d.glob(f'*{vid_id}*.{ext}'):
            if p.stat().st_size > 0:
                return p
    raise RuntimeError('thumbnail downloaded but no file found on disk')


# ── Playlist downloader ──────────────────────────────────────────────────────

def download_playlist(url: str,
                      out_dir: str | Path,
                      fmt: str = 'bestaudio/best',
                      *, on_progress=None, on_log=None,
                      should_cancel=None) -> list:
    """Download an entire playlist. Returns the list of saved Paths.
    Progress is per-playlist (0.0 → 1.0 across all items)."""
    import yt_dlp
    out_d = Path(out_dir).expanduser().resolve()
    out_d.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    counter = {'i': 0, 'n': 0}

    def _hook(d):
        if d.get('status') == 'finished':
            counter['i'] += 1
            fp = d.get('filename') or d.get('info_dict', {}).get('_filename')
            if fp:
                saved.append(Path(fp))
            if on_progress and counter['n']:
                on_progress(counter['i'] / counter['n'])
        if should_cancel and should_cancel():
            raise RuntimeError('cancelled')

    opts = {
        'format':           fmt,
        'outtmpl':          str(out_d / '%(playlist_index)03d - %(title).70s [%(id)s].%(ext)s'),
        'noplaylist':       False,
        'quiet':            True,
        'no_warnings':      True,
        'progress_hooks':   [_hook],
        'windowsfilenames': True,
        'nooverwrites':     True,
    }
    # Inject ffmpeg location for merge if available (same logic as youtube.py)
    try:
        from transcribe.youtube import FFMPEG_PATH
        if FFMPEG_PATH:
            opts['ffmpeg_location'] = FFMPEG_PATH
    except Exception:
        pass

    with yt_dlp.YoutubeDL(opts) as ydl:
        # Probe playlist size first for accurate progress
        info = ydl.extract_info(url, download=False)
        if info and info.get('_type') == 'playlist':
            counter['n'] = len(info.get('entries') or [])
        ydl.download([url])

    return saved
