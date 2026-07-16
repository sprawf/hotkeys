"""YouTube (and any yt-dlp-supported site) audio ingest.

`ingest_url(url, dest_dir)` downloads the best audio-only stream as an
.m4a/.webm and returns the local path, which is then fed into
`transcribe_file()` exactly like a user-supplied file.

Pure-Python (yt-dlp is a small wheel). Requires internet at call time,
graceful failure if offline.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Plug truststore into SSL once at import so yt-dlp's urllib calls trust the
# Windows root cert store instead of the empty venv bundle. The rest of the
# Hotkeys app already does the same trick for Groq/Cerebras (see
# hotkeys.spec hiddenimports). Falling back to no-op keeps non-Windows
# platforms unaffected.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass


def _find_ffmpeg() -> str | None:
    """Resolve a usable ffmpeg binary path. yt-dlp needs ffmpeg to merge
    separate video+audio streams (1080p+ on YouTube is always split).

    Search order:
      1. imageio-ffmpeg's bundled binary, works in both dev and dist
         (bundled into the PyInstaller spec under
         imageio_ffmpeg/binaries/)
      2. ffmpeg on PATH (system install), covers users who already have it
      3. None, yt-dlp falls back to single-stream formats only (max 720p
         on YouTube), no crash
    """
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    try:
        import shutil
        p = shutil.which('ffmpeg')
        if p:
            return p
    except Exception:
        pass
    return None


FFMPEG_PATH = _find_ffmpeg()
if FFMPEG_PATH:
    logger.info(f'ffmpeg available at: {FFMPEG_PATH}')

# Match youtube.com / youtu.be / m.youtube.com / music.youtube.com plus
# direct video IDs and playlist links.  yt-dlp handles hundreds of sites,
# we keep the URL detector narrow here so the UI can show the right
# "fetching from YouTube…" label; other yt-dlp-supported URLs still work
# via the generic file path.
_YT_PAT = re.compile(
    r'^(https?://)?(www\.|m\.|music\.)?'
    r'(youtube\.com/(watch\?v=|shorts/|embed/|live/|playlist\?list=)'
    r'|youtu\.be/)',
    re.IGNORECASE,
)


def is_youtube_url(s: str) -> bool:
    return bool(_YT_PAT.match(s.strip()))


# ── Format presets for user-facing download menu ─────────────────────────────
#
# Every entry is a tuple of:
#   ("Display label", "yt-dlp format string", "expected container hint")
#
# We intentionally avoid format strings that require ffmpeg post-processing
# (e.g. converting to MP3) so the dist stays plug-and-play, yt-dlp can't
# find a system ffmpeg in a frozen exe. Users who want MP3 specifically can
# pick "Best audio" and convert via Audacity / VLC, or we can add a
# PyAV-based post-conversion later.

DOWNLOAD_FORMATS = [
    ('🎬  Best video + audio (MP4)',
     'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',  'mp4'),
    ('🎬  1080p video + audio (MP4)',
     'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]', 'mp4'),
    ('🎬  720p video + audio (MP4)',
     'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]',   'mp4'),
    ('🎬  480p video + audio (MP4)',
     'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]',   'mp4'),
    ('🎵  Audio only (M4A, smallest)',
     'bestaudio[ext=m4a]/bestaudio', 'm4a'),
    ('🎵  Audio only (best quality, any container)',
     'bestaudio/best', None),
]


def ingest_url(
    url: str,
    dest_dir: str | Path,
    *,
    on_progress: Callable[[float], None] | None = None,
    on_log:      Callable[[str], None]  | None = None,
) -> Path:
    """Download the best audio-only stream of `url` into `dest_dir`. Returns
    the resolved Path of the downloaded file.

    Used by the transcription pipeline, always grabs audio-only to keep
    the download minimal.  See `download_url()` for the user-facing
    downloader which exposes video + format options.

    Progress callback receives 0.0-1.0; phases are smoothed across the yt-dlp
    download lifecycle so the UI sees a single continuous bar.
    """
    return _download(url, dest_dir, 'bestaudio/best',
                     on_progress=on_progress, on_log=on_log)


def download_url(
    url: str,
    dest_dir: str | Path,
    fmt: str = 'bestaudio/best',
    *,
    on_progress: Callable[[float], None] | None = None,
    on_log:      Callable[[str], None]  | None = None,
    on_phase:    Callable[[str], None]  | None = None,
    allow_playlist: bool = False,
) -> Path:
    """User-facing downloader: saves `url` to `dest_dir` using the yt-dlp
    format string `fmt` (see DOWNLOAD_FORMATS for the curated menu). Returns
    the final on-disk Path.

    Unlike ingest_url (which always uses bestaudio for transcription), this
    honors the caller's format choice so video + various qualities can be
    delivered.
    """
    return _download(url, dest_dir, fmt,
                     on_progress=on_progress, on_log=on_log,
                     on_phase=on_phase, allow_playlist=allow_playlist)


def _download(url: str, dest_dir, fmt: str, *,
              on_progress, on_log, on_phase=None,
              allow_playlist: bool = False) -> Path:
    """on_phase(label) is called with short status strings like 'merging',
    'transcoding', or 'done' so callers can update UI text separately
    from the progress bar."""
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError('yt-dlp not installed') from e

    log = lambda m: (on_log and on_log(m)) or logger.info(m)
    dest = Path(dest_dir).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    # Smooth two-stream (bestvideo+bestaudio) into a single 0→1 bar by
    # weighting each unique stream as an equal slice. Without this the
    # pill bounces: video 0→100%, audio 0→100% — looks broken.
    state = {'streams': {}, 'order': []}

    def _hook(d):
        try:
            status = d.get('status')
            fname = d.get('filename') or d.get('info_dict', {}).get('filepath') or 'stream'
            if fname not in state['streams']:
                state['order'].append(fname)
            if status == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                done  = d.get('downloaded_bytes') or 0
                state['streams'][fname] = (done / total) if total > 0 else 0.0
            elif status == 'finished':
                state['streams'][fname] = 1.0
            if on_progress and state['order']:
                # Average across all streams seen so far. While first stream
                # downloads, denominator is 1 → real percent. When second
                # stream starts, denominator becomes 2 → first sits at 50%
                # contribution and the pill keeps climbing past it.
                vals = [state['streams'].get(f, 0.0) for f in state['order']]
                # Assume max 2 streams (video+audio) so denominator stabilizes
                denom = max(2, len(state['order']))
                on_progress(sum(vals) / denom)
        except Exception:
            pass

    def _pp_hook(d):
        try:
            status = d.get('status')
            pp = (d.get('postprocessor') or '').lower()
            if status == 'started' and ('merger' in pp or 'ffmpeg' in pp):
                if on_phase: on_phase('merging')
                if on_progress: on_progress(1.0)
        except Exception:
            pass

    # Bridge yt-dlp's internal logger into ours so failures show the
    # real extractor/HTTP trace in app.log without needing quiet=False
    # (which would flood the console). Every yt-dlp log line is prefixed
    # with 'yt-dlp:' so it's greppable.
    class _YtdlpLogger:
        def debug(self, msg):
            # yt-dlp routes both real debug AND normal info through debug()
            # (an [debug] prefix distinguishes them). We downgrade real
            # debug lines and keep info at INFO.
            if msg.startswith('[debug] '):
                logger.debug(f'yt-dlp: {msg[8:]}')
            else:
                logger.info(f'yt-dlp: {msg}')
        def info(self, msg):     logger.info(f'yt-dlp: {msg}')
        def warning(self, msg):  logger.warning(f'yt-dlp: {msg}')
        def error(self, msg):    logger.error(f'yt-dlp: {msg}')

    opts = {
        'format':           fmt,
        'outtmpl':          str(dest / '%(title).80s [%(id)s].%(ext)s'),
        'noplaylist':       not allow_playlist,
        'quiet':            True,
        'no_warnings':      True,
        'logger':           _YtdlpLogger(),
        'progress_hooks':   [_hook],
        'postprocessor_hooks': [_pp_hook],
        'extract_flat':     False,
        'allow_unplayable_formats': False,
        # Force yt-dlp to delete the per-stream source files after a
        # successful merge — defaults to False already in yt-dlp but
        # being explicit so the .fNNN.mp4 / .fNNN.m4a fragments don't
        # linger if a previous defaults change ever flips this.
        'keepvideo':        False,
        # Sanitize titles for Windows (strips < > : " | ? * \ /) so a
        # video named   AC/DC: "Live!?"   doesn't crash the rename step.
        'windowsfilenames': True,
        # Never silently clobber a previously-downloaded copy of the same
        # video, if the user re-runs the download, yt-dlp appends ` (1)`.
        'nooverwrites':     True,
    }
    # Hand yt-dlp the bundled ffmpeg so it can merge split video+audio
    # streams (1080p+ on YouTube is always split). Without this, yt-dlp
    # falls back to single-stream formats only (max 720p on YT). The
    # DOWNLOAD_FORMATS fallback chain (`/best[ext=mp4]/best`) keeps the
    # pipeline working even if ffmpeg is missing, just at lower quality.
    if FFMPEG_PATH:
        opts['ffmpeg_location'] = FFMPEG_PATH

    log(f'Fetching {url}  [format={fmt}]')
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # yt-dlp returns the resolved filename in 'requested_downloads'
        # (newer) or via prepare_filename (older).
        downloaded = (
            (info.get('requested_downloads') or [{}])[0].get('filepath')
            or ydl.prepare_filename(info)
        )

    path = Path(downloaded)
    if not path.exists():
        raise RuntimeError(f'yt-dlp claimed success but file missing: {path}')
    log(f'Downloaded {path.name} ({path.stat().st_size / 1024 / 1024:.1f} MB)')
    return path
