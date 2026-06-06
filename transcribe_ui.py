"""F9 Media Tools panel. Mounts inside the Library window's Shift+F9 tab.

Exposes one class:

    TranscribePanel(parent_frame, hotkey_cfg, provider_factory=None,
                    notify_fn=None)

The panel owns a single workflow: pick a source → choose an operation →
configure options → run. Operations cover transcription, translation,
language detection, subtitle fetching, audio/video downloads, format
conversion, noise reduction, loudness normalization, speed adjustment,
trimming, frame extraction, file joining, speech-segment detection,
playlist downloads, and metadata/thumbnail fetch.

Each operation routes through the same worker-thread + cancel +
queue-polling infrastructure that powered the earlier transcribe-only
panel: see _do_run, _worker_dispatch, _poll.

Backend implementations live in:
  • transcribe.engine   : Whisper transcription + pyannote diarization
  • transcribe.tools    : ffmpeg / yt-dlp / VAD / noise reduction
  • transcribe.youtube  : yt-dlp URL ingest + downloader
  • transcribe.exporters: TXT/SRT/VTT/CSV/DOCX/PDF
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Callable

import customtkinter as ctk

from dialogs import alert, confirm
from storage import (
    appdata_dir, load_config, load_transcripts, save_transcript,
    delete_transcript,
)
from theme import (
    BG, SURFACE, SURF2, SURF3, BORDER,
    ACCENT, ACCENTL, TEXT_P, TEXT_S, TEXT_D,
    OK, WARN, ERR,
    FONT_FAMILY, PAD, PAD_SM, PAD_LG, RADIUS, RADIUS_SM,
)
from transcribe import (
    TranscriptJob, transcribe_file, export, SUPPORTED_FORMATS,
    ingest_url, is_youtube_url,
)
from transcribe.youtube import DOWNLOAD_FORMATS, download_url
from transcribe import tools

# Plain-English hover descriptions for every export format. Each tells the
# user (a) what KIND of file they'll get, (b) what they'd USE it for, and
# (c) which apps open it, no jargon, no acronym left unexplained.
EXPORT_TOOLTIPS = {
    'txt':  'Plain text. Just the words, no timestamps.',
    'srt':  'Subtitles for video editors (CapCut, Premiere, DaVinci, YouTube).',
    'vtt':  'Subtitles for websites / HTML5 video players.',
    'lrc':  'Synced lyrics for karaoke / lyric videos. Reads in VLC, '
            'Spotify, Aegisub.',
    'csv':  'Spreadsheet: one row per segment. Opens in Excel / Sheets.',
    'docx': 'Word document with speaker headings and timestamps.',
    'pdf':  'Print-ready transcript.',
}

logger = logging.getLogger(__name__)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _btn(parent, text, command, width=None, fg_color=SURF2,
         hover=SURF3, text_color=TEXT_P, **kw):
    """Match library.py's button styling so this panel feels native."""
    kw.update(text=text, command=command, fg_color=fg_color,
              hover_color=hover, text_color=text_color,
              corner_radius=RADIUS_SM, font=(FONT_FAMILY, 13))
    if width is not None:
        kw['width'] = width
    return ctk.CTkButton(parent, **kw)


def _fmt_duration(secs: float) -> str:
    secs = max(0.0, float(secs))
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f'{h:d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def _fmt_rel(ts: float) -> str:
    d = time.time() - ts
    if d < 60:    return 'just now'
    if d < 3600:  return f'{int(d//60)}m ago'
    if d < 86400: return f'{int(d//3600)}h ago'
    if d < 7*86400: return f'{int(d//86400)}d ago'
    return time.strftime('%b %d %Y', time.localtime(ts))


def _default_downloads_dir() -> Path:
    """Where downloads/output files land by default, Windows Downloads."""
    home = os.environ.get('USERPROFILE') or str(Path.home())
    return Path(home) / 'Downloads'


def _sweep_yt_cache(max_age_seconds: int = 24*3600,
                    max_total_bytes: int = 500 * 1024 * 1024) -> None:
    """Bound the YT-dlp cache so it can't grow unboundedly.

    Called once when the Transcribe panel is constructed. Two-tier policy:
      1. Delete any file older than `max_age_seconds` (24 h default).
      2. If the cache is still over `max_total_bytes` (500 MB default),
         delete oldest-first until it's under.
    Both bounds err generous so a user mid-job never loses their working
    file, anything younger than 24 h survives the age sweep, and the
    size cap only activates if there's a lot of dead weight on top.
    Best-effort: errors are logged and swallowed.
    """
    try:
        cache = _yt_cache_dir()
        if not cache.exists():
            return
        files = []
        now = time.time()
        for p in cache.iterdir():
            try:
                if not p.is_file(): continue
                st = p.stat()
                files.append((p, st.st_size, st.st_mtime))
            except Exception:
                continue
        # Tier 1: age
        removed_age = 0
        survivors = []
        for p, size, mtime in files:
            if (now - mtime) > max_age_seconds:
                try: p.unlink(); removed_age += 1
                except Exception: survivors.append((p, size, mtime))
            else:
                survivors.append((p, size, mtime))
        # Tier 2: size cap
        total = sum(s for _, s, _ in survivors)
        removed_size = 0
        if total > max_total_bytes:
            survivors.sort(key=lambda t: t[2])   # oldest first
            for p, size, _ in survivors:
                if total <= max_total_bytes: break
                try: p.unlink(); total -= size; removed_size += 1
                except Exception: pass
        if removed_age or removed_size:
            logger.info(
                f'YT cache sweep: removed {removed_age} aged + '
                f'{removed_size} oversize files from {cache}')
    except Exception as e:
        logger.warning(f'YT cache sweep failed: {e}')


def _yt_cache_dir() -> Path:
    """Transient YT-dlp cache. Dist: <exe>/data/transcripts_cache.
    Dev: <repo>/.transcripts_cache (NEVER C:)."""
    import sys as _sys
    if getattr(_sys, 'frozen', False):
        return Path(appdata_dir()) / 'transcripts_cache'
    return Path(__file__).resolve().parent / '.transcripts_cache'


# ── Whisper model discovery ──────────────────────────────────────────────────

_MODEL_LABELS = {
    # Layperson framing: speed and quality, not raw model size. Sizes are
    # surfaced as tooltips so power users can still see them.
    'base':            'Fast (good for clear speech)',
    'small':           'Balanced (recommended for most)',
    'large-v3-turbo':  'Accurate (slower, handles accents better)',
    'large-v3':        'Most accurate (slowest, handles noise + accents)',
}

_MODEL_TOOLTIPS = {
    'base':            '~74 MB on disk · ~2-3× realtime on CPU',
    'small':           '~244 MB on disk · ~1× realtime on CPU',
    'large-v3-turbo':  '~809 MB on disk · ~3-5 min per 3-min song on CPU',
    'large-v3':        '~1.5 GB on disk · ~6-10 min per 3-min song on CPU',
}

# Short tier names used in the tooltip so it doesn't repeat the
# parenthetical that the dropdown labels already show.
_MODEL_TIER = {
    'base':           'Fast',
    'small':          'Balanced',
    'large-v3-turbo': 'Accurate',
    'large-v3':       'Most accurate',
}

def _discover_model_choices(for_translate: bool = False) -> dict:
    """Return {friendly_label: model_id} for the Model dropdown.

    We always include the full ladder (base, small, large-v3-turbo,
    large-v3) so the user can pick any of them; ones that are not yet on
    disk get an "(~XYZ MB download)" suffix so picking is informed.

    When for_translate=True, large-v3-turbo is omitted: that model is a
    decoder-pruned finetune that silently ignores task='translate' and
    returns source-language text. Hiding it from the dropdown prevents
    the surprise.
    """
    try:
        from transcribe.engine import (_whisper_models_available,
                                       _MODEL_SIZES_MB)
        on_disk = set(_whisper_models_available())
    except Exception:
        on_disk = {'base'}
        _MODEL_SIZES_MB = {}
    all_ids = ['base', 'small', 'large-v3-turbo', 'large-v3']
    if for_translate:
        all_ids = [m for m in all_ids if m != 'large-v3-turbo']
    out = {}
    for mid in all_ids:
        base = _MODEL_LABELS.get(mid, mid)
        if mid in on_disk:
            label = base
        else:
            size = _MODEL_SIZES_MB.get(mid, 0)
            label = f'{base}  ·  needs ~{size} MB download' if size \
                    else f'{base}  ·  needs download'
        out[label] = mid
    return out


# ── Language list ────────────────────────────────────────────────────────────

_LANGUAGES = [
    ('Auto-detect', None), ('English', 'en'), ('Spanish', 'es'),
    ('French', 'fr'), ('German', 'de'), ('Italian', 'it'),
    ('Portuguese', 'pt'), ('Dutch', 'nl'), ('Russian', 'ru'),
    ('Polish', 'pl'), ('Japanese', 'ja'), ('Chinese', 'zh'),
    ('Korean', 'ko'), ('Arabic', 'ar'), ('Hindi', 'hi'),
]


# ── Operation catalog ─────────────────────────────────────────────────────────
#
# Each entry describes one user-visible operation. The catalog is the single
# source of truth: the UI builds the picker from it, and the worker
# dispatches to `runner` with the option dict. Keep this ordered, the UI
# preserves the order within each category.

OPERATIONS = [
    # ── Get text from audio/video ────────────────────────────────────────────
    {'key':'transcribe', 'cat':'Get text',
     'label':'📝  Transcribe',
     'desc':'Turn speech into text with timestamps and speaker labels.',
     'needs':'audio_or_url',
     'options':['model','language','music_mode','diarize','summary','audio_dir']},

    {'key':'translate', 'cat':'Get text',
     'label':'🌐  Translate to English',
     'desc':'Listen to speech in any language and write it down in English.',
     'needs':'audio_or_url',
     'options':['model','audio_dir']},

    {'key':'detect_lang', 'cat':'Get text',
     'label':'🔤  What language is this?',
     'desc':"Tell me what language is being spoken (doesn't write it down).",
     'needs':'audio_or_url',
     'options':['model']},

    {'key':'get_subs', 'cat':'Get text',
     'label':'💬  Download subtitles',
     'desc':"If the video has captions, save them directly. Faster and more accurate than transcribing.",
     'needs':'url',
     'options':['sub_langs','out_dir']},

    {'key':'batch_subtitle', 'cat':'Get text',
     'label':'🎞  Subtitle a folder of videos',
     'desc':('Auto-generates an .srt next to every video in a folder, '
             'tick "Translate to English" if the audio is not English.'),
     'needs':'folder',
     'options':['model','language','translate_to_en','music_mode']},

    # ── Get audio ────────────────────────────────────────────────────────────
    {'key':'dl_audio', 'cat':'Get audio',
     'label':'🎵  Get audio from a link',
     'desc':'Save just the audio (MP3 etc.) from a YouTube / Vimeo / SoundCloud link.',
     'needs':'url',
     'options':['audio_format','out_dir']},

    {'key':'extract_audio', 'cat':'Get audio',
     'label':'🎬  Get audio from a file',
     'desc':'Pull the audio out of a video file you already have on your computer.',
     'needs':'file',
     'options':['audio_format','out_dir']},

    {'key':'convert_audio', 'cat':'Get audio',
     'label':'🔁  Convert audio to a different format',
     'desc':'Turn any audio file into MP3, M4A, WAV, FLAC, or Opus.',
     'needs':'file',
     'options':['audio_format','out_dir']},

    {'key':'denoise', 'cat':'Get audio',
     'label':'🧹  Clean up a noisy recording',
     'desc':'Reduce background hum, hiss, fan noise, and similar steady-state noise.',
     'needs':'file',
     'options':['out_dir']},

    {'key':'normalize', 'cat':'Get audio',
     'label':'📶  Even out the volume',
     'desc':'Make quiet parts louder and loud parts quieter so nothing blasts you.',
     'needs':'file',
     'options':['out_dir']},

    {'key':'change_speed', 'cat':'Get audio',
     'label':'⏩  Speed up or slow down',
     'desc':'Change playback rate without making voices sound chipmunky.',
     'needs':'file',
     'options':['speed','out_dir']},

    # ── Get video ────────────────────────────────────────────────────────────
    {'key':'dl_video', 'cat':'Get video',
     'label':'🎬  Download video',
     'desc':'Save the video (with audio) from a URL.\n'
            'Tip: select / copy any URL and press Ctrl+Alt+D from any app.',
     'needs':'url',
     'options':['video_format','out_dir']},

    {'key':'metadata', 'cat':'Get video',
     'label':'ℹ️  Just show me the details',
     'desc':"Show the video's title, channel, duration, and description without downloading anything.",
     'needs':'url',
     'options':[]},

    {'key':'thumbnail', 'cat':'Get video',
     'label':'🖼  Save the thumbnail image',
     'desc':'Download just the cover image of the video.',
     'needs':'url',
     'options':['out_dir']},

    {'key':'playlist', 'cat':'Get video',
     'label':'📚  Download a whole playlist',
     'desc':'Save every video (or audio) in a YouTube playlist into a folder.',
     'needs':'url',
     'options':['video_format','out_dir']},

    # ── Edit ─────────────────────────────────────────────────────────────────
    {'key':'trim', 'cat':'Edit',
     'label':'✂️  Cut out a section',
     'desc':'Keep just the part between two timestamps and save it as a new file.',
     'needs':'file',
     'options':['start_time','end_time','out_dir']},

    {'key':'extract_frame', 'cat':'Edit',
     'label':'📸  Save a still image from video',
     'desc':'Pick a moment in the video and save that frame as a PNG.',
     'needs':'file',
     'options':['frame_time','out_dir']},

    {'key':'find_speech', 'cat':'Edit',
     'label':'🔍  Find when people are talking',
     'desc':('Lists every chunk of audio that contains speech (with '
             'start/end times) so you can skip silence when editing.'),
     'needs':'file',
     'options':[]},

    {'key':'concat', 'cat':'Edit',
     'label':'➕  Join several audio or video files',
     'desc':('Glues multiple audio OR video files end-to-end into one '
             '(MP3 / WAV / M4A / MP4 / MKV / MOV, not images or PDFs).'),
     'needs':'multi_file',
     'options':['out_dir']},

    {'key':'embed_subs', 'cat':'Edit',
     'label':'💬  Add subtitles to a video',
     'desc':('Mux a video and an .srt into one file, soft by default, '
             'tick "Burn into the picture" to bake them in permanently.'),
     'needs':'file',
     'options':['subtitle_file','burn_subs','out_dir']},
]

# Quick lookup helpers
OP_BY_KEY     = {op['key']: op for op in OPERATIONS}
OP_CATEGORIES = ('Get text', 'Get audio', 'Get video', 'Edit')


# ── Panel ────────────────────────────────────────────────────────────────────

class TranscribePanel:
    """Mounts the Media Tools UI inside a frame supplied by the host
    (library.py's _scroll content frame). One worker thread at a time;
    second Run press while busy shows an "Already busy" notification."""

    def __init__(self, parent: tk.Widget, *,
                 hotkey_cfg: dict,
                 provider_factory: Callable | None = None,
                 notify_fn: Callable[[str, str], None] | None = None) -> None:
        self.parent      = parent
        self.hotkey_cfg  = hotkey_cfg
        self._provider_factory = provider_factory
        self._notify     = notify_fn or (lambda *a, **k: None)

        # Bound the transient YT-dlp cache before this session adds to it.
        # Runs off the UI thread because a cold sweep over a huge cache
        # could otherwise stall the Library opening for a noticeable beat.
        threading.Thread(target=_sweep_yt_cache, daemon=True).start()

        # Worker / queue / cancel
        self._worker:    threading.Thread | None = None
        self._cancel     = threading.Event()
        self._msg_q:     queue.Queue = queue.Queue()
        self._poll_id:   str | None = None
        self._job_t0:    float = 0.0
        self._destroyed  = False

        # Currently-active job state
        self._current_op_key: str = ''
        self._current_job:    TranscriptJob | None = None
        self._yt_local_path:    Path | None = None
        # Where the kept download ended up after a successful job, surfaced
        # in the result panel so the user can find their file.
        self._kept_download_path: Path | None = None
        # Per-job destination chosen by the user via "Save downloaded audio to".
        # Stashed when the job starts; consumed by _done / _cancelled / _errored.
        self._current_audio_dir:  Path | None = None
        self._extra_files:    list[Path] = []   # for multi-file concat input

        # Tk vars, created lazily inside _render()
        self.src_var:        tk.StringVar  | None = None
        self.op_var:         tk.StringVar  | None = None
        self.model_var:      tk.StringVar  | None = None
        self.lang_var:       tk.StringVar  | None = None
        self.diarize_var:    tk.BooleanVar | None = None
        self.summary_var:    tk.BooleanVar | None = None
        self.audio_fmt_var:  tk.StringVar  | None = None
        self.video_fmt_var:  tk.StringVar  | None = None
        self.sub_langs_var:  tk.StringVar  | None = None
        self.out_dir_var:    tk.StringVar  | None = None
        self.speed_var:      tk.StringVar  | None = None
        self.start_t_var:    tk.StringVar  | None = None
        self.end_t_var:      tk.StringVar  | None = None
        self.frame_t_var:    tk.StringVar  | None = None

        # Container references that survive across rebuilds
        self.options_inner: ctk.CTkFrame | None = None

        self._render()

    # ── Top-level layout ─────────────────────────────────────────────────────

    def _render(self) -> None:
        # Wipe ONLY this panel's previous container — NOT every child of
        # self.parent. Earlier this destroyed self.parent.winfo_children(),
        # which is the exact "destroy too many siblings" trap that bit
        # us in library.py's tab renders. If parent ever holds anything
        # besides this panel, those siblings would be silently wiped.
        # Always render into a panel-owned container instead.
        prev = getattr(self, 'container', None)
        if prev is not None:
            try: prev.destroy()
            except Exception: pass
        self.container = ctk.CTkFrame(self.parent, fg_color='transparent')
        self.container.grid(row=0, column=0, sticky='nsew', padx=PAD, pady=PAD)
        self.container.columnconfigure(0, weight=1)
        try:
            self.parent.columnconfigure(0, weight=1)
        except Exception:
            pass

        self._render_source_card()       # row 0
        self._render_operation_picker()  # row 1
        self._render_options_card()      # row 2, contextual
        self._render_action_row()        # row 3
        self._render_progress_card()     # row 4, hidden until run
        self._render_result_card()       # row 5, hidden until finished
        self._render_history_card()      # row 6
        self._wire_wheel_forwarding()

    # ── 1. Source ────────────────────────────────────────────────────────────

    def _render_source_card(self) -> None:
        card = ctk.CTkFrame(self.container, fg_color=SURFACE,
                            corner_radius=RADIUS_SM)
        card.grid(row=0, column=0, sticky='ew', pady=(0, PAD_SM))
        card.columnconfigure(1, weight=1)

        hk = self.hotkey_cfg.get('transcribe', 'shift+f9').upper()
        ctk.CTkLabel(card, text='🎬  Media Tools',
                     font=(FONT_FAMILY, 16, 'bold'), text_color=TEXT_P,
                     ).grid(row=0, column=0, columnspan=3, sticky='w',
                            padx=PAD, pady=(PAD, 2))
        ctk.CTkLabel(card,
                     text=(f'{hk} to open  ·  Paste a URL or pick a file, '
                           f'then choose what to do with it.'),
                     font=(FONT_FAMILY, 12), text_color=TEXT_S,
                     ).grid(row=1, column=0, columnspan=3, sticky='w',
                            padx=PAD, pady=(0, PAD_SM))

        ctk.CTkLabel(card, text='Source:',
                     font=(FONT_FAMILY, 13), text_color=TEXT_P,
                     ).grid(row=2, column=0, sticky='w', padx=(PAD, 6),
                            pady=(0, PAD))
        self.src_var = tk.StringVar(value='')
        self.src_entry = ctk.CTkEntry(
            card, textvariable=self.src_var,
            font=(FONT_FAMILY, 13), height=34,
        )
        self.src_entry.grid(row=2, column=1, sticky='ew', padx=(0, 6),
                            pady=(0, PAD))
        # CTk 5.2.2's placeholder_text breaks when textvariable is set
        # (it compares the StringVar object to "" instead of calling
        # .get()). Use our own Label overlay that doesn't touch the
        # StringVar at all, so the placeholder is purely visual and
        # never leaks into the value.
        self._install_placeholder(
            self.src_entry, self.src_var,
            'Paste a URL  •  Pick a file  •  Or drop an audio/video file anywhere on this card',
        )
        # Register the entire source card AND the entry as a drag-drop
        # target so the user can drop any audio/video file from Explorer
        # (or from another app) and have its path land in the Source
        # field. Done lazily because tkinterdnd2 may not be available
        # in every dev environment.
        self._register_source_drop_target(card)
        self._register_source_drop_target(self.src_entry)
        try:
            from dialogs import Tooltip
            Tooltip(self.src_entry,
                    'What we accept here:\n'
                    '  • Any audio file (MP3, WAV, M4A, FLAC, OGG, Opus…)\n'
                    '  • Any video file (MP4, MKV, MOV, AVI, WebM…)\n'
                    '  • A link from YouTube, SoundCloud, Vimeo, Twitch,\n'
                    '    Bandcamp, Twitter/X, TikTok, or ~1700 other sites\n'
                    '\n'
                    'NOT for: images, PDFs, Word documents.')
        except Exception:
            pass
        # Right-click → context menu (Cut / Copy / Paste / Select All).
        # CTkEntry wraps a real tk.Entry as `._entry`; we bind to that so the
        # built-in clipboard verbs work without us reimplementing them.
        try:
            _inner = self.src_entry._entry
            _inner.bind('<Button-3>', self._show_src_context_menu, add='+')
        except Exception:
            pass
        _btn(card, '📁  Browse', self._do_browse, width=110,
             ).grid(row=2, column=2, sticky='e', padx=(0, PAD), pady=(0, PAD))

    # ── 2. Operation picker ──────────────────────────────────────────────────

    def _render_operation_picker(self) -> None:
        card = ctk.CTkFrame(self.container, fg_color=SURFACE,
                            corner_radius=RADIUS_SM)
        card.grid(row=1, column=0, sticky='ew', pady=(0, PAD_SM))
        for c in range(len(OP_CATEGORIES)):
            card.columnconfigure(c, weight=1, uniform='ops')

        ctk.CTkLabel(card, text='What do you want to do?',
                     font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_P,
                     ).grid(row=0, column=0, columnspan=len(OP_CATEGORIES),
                            sticky='w', padx=PAD, pady=(PAD, PAD_SM))

        self.op_var = tk.StringVar(value='transcribe')
        # One column per category, each holds a header + a stack of radios
        for ci, cat in enumerate(OP_CATEGORIES):
            col = ctk.CTkFrame(card, fg_color='transparent')
            col.grid(row=1, column=ci, sticky='nsew', padx=PAD_SM, pady=(0, PAD))

            ctk.CTkLabel(col, text=cat,
                         font=(FONT_FAMILY, 12, 'bold'),
                         text_color=TEXT_S,
                         ).pack(anchor='w', pady=(0, 4))

            for op in OPERATIONS:
                if op['cat'] != cat:
                    continue
                rb = ctk.CTkRadioButton(
                    col, text=op['label'],
                    variable=self.op_var, value=op['key'],
                    fg_color=ACCENT, hover_color=ACCENTL,
                    text_color=TEXT_P, font=(FONT_FAMILY, 12),
                    command=self._on_operation_change,
                )
                rb.pack(anchor='w', pady=1)
                # Hover tooltip, shows the full op description so users
                # can scan all 18 operations without clicking each one to
                # discover what it does.
                try:
                    from dialogs import Tooltip
                    Tooltip(rb, op.get('desc', ''))
                except Exception:
                    pass

        # Description line, updated on operation change. Bumped from
        # size 11 italic TEXT_S to size 13 non-italic TEXT_P for legibility,
        # the old style read as washed-out small print on the dark surface.
        self.op_desc_lbl = ctk.CTkLabel(
            card, text='',
            font=(FONT_FAMILY, 13), text_color=TEXT_P,
            wraplength=1000, justify='left', anchor='w',
        )
        self.op_desc_lbl.grid(row=2, column=0, columnspan=len(OP_CATEGORIES),
                              sticky='ew', padx=PAD, pady=(0, PAD))
        # Initial description
        self._on_operation_change()

    def _on_operation_change(self) -> None:
        """Called when the user clicks a different radio, updates the
        description line, rebuilds the contextual options card, and labels
        the Run button so users see what they're about to do."""
        op = OP_BY_KEY.get(self.op_var.get())
        if not op:
            return
        if hasattr(self, 'op_desc_lbl'):
            self.op_desc_lbl.configure(text=op['desc'])
        if self.options_inner is not None:
            self._build_options(op)
        if hasattr(self, 'start_btn'):
            # Strip the leading emoji + spaces from the catalog label so the
            # button reads "▶  Transcribe" instead of "▶  📝  Transcribe".
            import re as _re
            clean = _re.sub(r'^[^\w]+', '', op['label']).strip()
            try: self.start_btn.configure(text=f'▶  {clean}')
            except Exception: pass

    # ── 3. Contextual options ────────────────────────────────────────────────

    def _render_options_card(self) -> None:
        card = ctk.CTkFrame(self.container, fg_color=SURFACE,
                            corner_radius=RADIUS_SM)
        card.grid(row=2, column=0, sticky='ew', pady=(0, PAD_SM))
        card.columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text='Options',
                     font=(FONT_FAMILY, 13, 'bold'), text_color=TEXT_P,
                     ).grid(row=0, column=0, sticky='w',
                            padx=PAD, pady=(PAD, PAD_SM))

        # Inner frame that gets cleared + rebuilt whenever the operation
        # changes. Lets each op show only the options it actually uses.
        self.options_inner = ctk.CTkFrame(card, fg_color='transparent')
        self.options_inner.grid(row=1, column=0, sticky='ew',
                                padx=PAD, pady=(0, PAD))
        self.options_inner.columnconfigure(1, weight=1)

        # Initialise vars with defaults
        # Speed=1.0 (no change) so a stray click doesn't silently re-time the
        # audio. Summary=off because it invokes an LLM API which costs money /
        # latency / needs a configured provider.
        self.model_var      = tk.StringVar(value=list(_discover_model_choices().keys())[0])
        self.lang_var       = tk.StringVar(value=_LANGUAGES[0][0])
        self.diarize_var    = tk.BooleanVar(value=False)
        self.summary_var    = tk.BooleanVar(value=False)
        self.music_mode_var = tk.BooleanVar(value=False)
        # New: batch translate flag for "Subtitle a folder of videos".
        self.translate_to_en_var = tk.BooleanVar(value=False)
        # New: external subtitle file path + burn flag for "Add subtitles to video".
        self.subtitle_file_var   = tk.StringVar(value='')
        self.burn_subs_var       = tk.BooleanVar(value=False)
        self.audio_fmt_var  = tk.StringVar(value=list(tools.AUDIO_FORMATS.keys())[0])
        self.video_fmt_var  = tk.StringVar(value=DOWNLOAD_FORMATS[0][0])
        self.sub_langs_var  = tk.StringVar(value=_LANGUAGES[1][0])  # English
        self.out_dir_var    = tk.StringVar(value=str(_default_downloads_dir()))
        self.speed_var      = tk.StringVar(value='1.0')
        self.start_t_var    = tk.StringVar(value='00:00')
        self.end_t_var      = tk.StringVar(value='')
        self.frame_t_var    = tk.StringVar(value='00:00')

        op = OP_BY_KEY.get(self.op_var.get())
        if op:
            self._build_options(op)

    def _build_options(self, op: dict) -> None:
        """Clear options_inner and rebuild the rows the operation declares.
        Each row is added with the appropriate widget(s)."""
        for w in self.options_inner.winfo_children():
            w.destroy()
        wanted = set(op['options'])
        row = 0
        if not wanted:
            ctk.CTkLabel(self.options_inner,
                         text='No options. Just click Run.',
                         font=(FONT_FAMILY, 12, 'italic'), text_color=TEXT_S,
                         ).grid(row=0, column=0, columnspan=3, sticky='w')
            return

        if 'model' in wanted:
            # Translate-to-English silently misbehaves on large-v3-turbo
            # (decoder-pruned finetune returns source-language text), so
            # hide it from the dropdown when the user picked Translate.
            is_translate = op.get('key') == 'translate'
            choices = _discover_model_choices(for_translate=is_translate)
            # Keep model_var pointing at something valid. If the user had
            # large-v3-turbo selected and then switched to Translate, snap
            # to the first remaining choice so the dropdown shows a real
            # entry instead of a phantom label.
            if self.model_var.get() not in choices:
                self.model_var.set(next(iter(choices.keys())))
            tip_lines = []
            for friendly, mid in choices.items():
                t = _MODEL_TOOLTIPS.get(mid)
                if t:
                    tier = _MODEL_TIER.get(mid, friendly)
                    tip_lines.append(f'{tier}  →  {t}')
            if is_translate:
                tip_lines.append('')
                tip_lines.append('Note: "Accurate" (large-v3-turbo) is hidden '
                                 'here because it cannot translate, only '
                                 'transcribe in the source language.')
            self._opt_dropdown(row, 'Model:',
                               list(choices.keys()),
                               self.model_var, width=260,
                               tooltip='\n'.join(tip_lines)); row += 1
        if 'language' in wanted:
            self._opt_dropdown(row, 'Language:',
                               [lbl for lbl, _ in _LANGUAGES],
                               self.lang_var, width=180,
                               tooltip='Setting this explicitly is a touch '
                                       'more accurate than Auto-detect '
                                       'when you already know the language.'
                               ); row += 1
        if 'translate_to_en' in wanted:
            self._opt_check(row,
                            '🌐  Translate to English '
                            '(use when audio is not English)',
                            self.translate_to_en_var); row += 1
        if 'music_mode' in wanted:
            self._opt_check(row,
                            '🎵  Music mode (for songs: keeps sung vocals, '
                            'avoids treating singing as silence)',
                            self.music_mode_var); row += 1
        if 'subtitle_file' in wanted:
            self._opt_file_picker(
                row, 'Subtitle file (.srt):', self.subtitle_file_var,
                title='Pick the subtitle file',
                filetypes=[('Subtitle files', '*.srt *.vtt'),
                           ('All files', '*.*')],
            ); row += 1
        if 'burn_subs' in wanted:
            self._opt_check(row,
                            '🔥  Burn into the picture '
                            '(slower, irreversible; for platforms that strip '
                            'subtitle tracks)',
                            self.burn_subs_var); row += 1
        if 'diarize' in wanted:
            self._opt_check(row, 'Label each speaker (Speaker 1, Speaker 2… slower)',
                            self.diarize_var); row += 1
        if 'summary' in wanted:
            self._opt_check(row,
                            'AI summary at the top (3-6 bullet points of key topics)',
                            self.summary_var); row += 1
        if 'audio_format' in wanted:
            self._opt_dropdown(
                row, 'Audio format:',
                list(tools.AUDIO_FORMATS.keys()),
                self.audio_fmt_var, width=300,
                tooltip=('MP3: plays in every app, ~3 MB per minute.\n'
                         'M4A / AAC: smaller than MP3, plays in Apple apps.\n'
                         'Opus: best fidelity per byte, some older apps skip it.\n'
                         'FLAC: identical to source quality, files much bigger.\n'
                         'WAV: identical to source, biggest files, plays anywhere.')
            ); row += 1
        if 'video_format' in wanted:
            self._opt_dropdown(
                row, 'Video format:',
                [lbl for lbl, _, _ in DOWNLOAD_FORMATS],
                self.video_fmt_var, width=320,
                tooltip=('Best: highest resolution the source has.\n'
                         '1080p / 720p / 480p: cap the resolution to save space.\n'
                         'Audio only: skip the picture entirely.')
            ); row += 1
        if 'sub_langs' in wanted:
            # Dropdown of the same 15 friendly language names used for
            # transcription, only ones that are actually likely to have
            # YouTube captions. Stored as the display label; we map to the
            # ISO code in _collect_options.
            self._opt_dropdown(row, 'Subtitle language:',
                               [lbl for lbl, c in _LANGUAGES if c],
                               self.sub_langs_var, width=180); row += 1
        if 'speed' in wanted:
            self._opt_entry(row, 'Speed factor (1.0 = normal, 2.0 = 2× fast, 0.5 = half):',
                            self.speed_var, placeholder='1.5'); row += 1
        if 'start_time' in wanted:
            self._opt_entry(row, 'Start time (HH:MM:SS or seconds):',
                            self.start_t_var, placeholder='00:00'); row += 1
        if 'end_time' in wanted:
            self._opt_entry(row, 'End time (blank = to the end):',
                            self.end_t_var, placeholder=''); row += 1
        if 'frame_time' in wanted:
            self._opt_entry(row, 'Frame timestamp (HH:MM:SS):',
                            self.frame_t_var, placeholder='00:00'); row += 1
        if 'out_dir' in wanted:
            self._opt_dir_picker(row, 'Save to:', self.out_dir_var); row += 1
        if 'audio_dir' in wanted:
            # For URL-input transcribe ops, this is where the downloaded
            # audio is kept. We reuse out_dir_var so the user's last folder
            # choice is remembered across ops. Labelled distinctly so it
            # doesn't read as "save the transcript here", the transcript
            # has its own per-format file picker on Export.
            self._opt_dir_picker(
                row,
                'Save downloaded audio to:',
                self.out_dir_var,
            ); row += 1
        # New widgets just got created, re-bind wheel forwarding so the
        # outer canvas keeps scrolling when the cursor is over them.
        try:
            self._wire_wheel_forwarding()
        except Exception:
            pass

    def _install_placeholder(self, entry, var: tk.StringVar, text: str) -> None:
        """Render *text* as a dimmed overlay label inside *entry* while
        the StringVar is empty AND the entry is not focused.

        Worked around CTk 5.2.2's broken placeholder_text mechanism,
        which fails to activate when textvariable is set (the library
        compares the StringVar OBJECT to the empty string instead of
        calling .get(), so the condition is never true).

        The overlay never touches *var*, so the placeholder cannot
        accidentally end up as a value submitted to the backend.
        """
        try:
            inner = entry._entry   # underlying tk.Entry
        except Exception:
            return
        try:
            bg = inner.cget('background')
        except Exception:
            bg = SURF2
        lbl = tk.Label(
            inner, text=text, bg=bg, fg=TEXT_S,
            font=(FONT_FAMILY, 13), anchor='w', borderwidth=0, padx=2,
        )

        def _refresh(*_):
            try:
                has_text   = bool(var.get())
                has_focus  = (inner.focus_displayof() is inner)
                if not has_text and not has_focus:
                    lbl.place(in_=inner, x=4, y=0, relwidth=1, relheight=1)
                else:
                    lbl.place_forget()
            except Exception:
                pass

        # Clicking the placeholder should drop the user into the entry,
        # otherwise the label sits on top and steals focus events.
        lbl.bind('<Button-1>', lambda e: inner.focus_set())
        inner.bind('<FocusIn>',  _refresh, add='+')
        inner.bind('<FocusOut>', _refresh, add='+')
        var.trace_add('write', _refresh)
        # Defer the first paint so the entry is fully laid out before we
        # place the overlay on top of it.
        try:
            entry.after(50, _refresh)
        except Exception:
            _refresh()

    def _register_source_drop_target(self, widget) -> None:
        """Make *widget* accept dropped audio/video files. The dropped
        path lands in self.src_var so the user can immediately click
        Run. Quietly no-ops if tkinterdnd2 isn't available."""
        try:
            from tkinterdnd2 import DND_FILES
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind('<<Drop>>', self._on_source_drop)
        except Exception:
            pass

    def _on_source_drop(self, event) -> None:
        """Handle a file drop on the Source card.

        TkDND delivers a Tcl list of paths in event.data. Paths with
        spaces are wrapped in {braces}. We accept the first path,
        strip any wrapping, and set src_var. Folders are accepted too
        because the batch-subtitle operation expects a folder, and
        users may not realise the same source field handles both.
        """
        raw = (event.data or '').strip()
        if not raw:
            return
        # Tcl list splitting: paths with spaces come back like
        # "{C:/a b/c.mp4}", multiple paths like
        # "{C:/a b/c.mp4} {D:/d.mkv}". We use Tk's own splitlist for
        # correctness on every Windows path quirk.
        try:
            parts = self.parent.tk.splitlist(raw)
        except Exception:
            parts = [raw]
        if not parts:
            return
        path = parts[0]
        # Strip stray { } if splitlist didn't catch them.
        if path.startswith('{') and path.endswith('}'):
            path = path[1:-1]
        self.src_var.set(path)
        # Move focus into the entry so the user sees the path landed
        # there, and so subsequent Ctrl+A / typing works naturally.
        try:
            self.src_entry.focus_set()
            inner = getattr(self.src_entry, '_entry', None)
            if inner is not None:
                inner.icursor('end')
        except Exception:
            pass

    def _show_src_context_menu(self, event) -> None:
        """Cut/Copy/Paste/Select-All on the Source entry, the layperson
        expectation for any text field. CTkEntry doesn't ship one."""
        try:
            from dialogs import PopupMenu
        except Exception:
            return
        entry = self.src_entry._entry
        try:
            has_sel = bool(entry.selection_present())
        except Exception:
            has_sel = False
        try:
            import tkinter as _tk
            clip = self.parent.clipboard_get() if True else ''
            has_clip = bool(clip)
        except Exception:
            has_clip = False

        def _cut():
            try: entry.event_generate('<<Cut>>')
            except Exception: pass
        def _copy():
            try: entry.event_generate('<<Copy>>')
            except Exception: pass
        def _paste():
            try: entry.event_generate('<<Paste>>')
            except Exception: pass
        def _select_all():
            try:
                entry.select_range(0, 'end'); entry.icursor('end')
            except Exception: pass
        def _clear():
            try:
                self.src_var.set('')
            except Exception: pass

        m = PopupMenu(self.parent)
        m.add('Cut',        _cut,        enabled=has_sel)
        m.add('Copy',       _copy,       enabled=has_sel)
        m.add('Paste',      _paste,      enabled=has_clip)
        m.add('Select all', _select_all)
        m.separator()
        m.add('Clear',      _clear,      enabled=bool(self.src_var.get()))
        m.show(event.x_root, event.y_root)

    def _opt_dropdown(self, row, label, values, var, width=200, tooltip: str = ''):
        lbl = ctk.CTkLabel(self.options_inner, text=label,
                           font=(FONT_FAMILY, 12), text_color=TEXT_P)
        lbl.grid(row=row, column=0, sticky='w', padx=(0, 6), pady=3)
        menu = ctk.CTkOptionMenu(
            self.options_inner, values=values, variable=var, width=width,
            fg_color=SURF2, button_color=SURF3, button_hover_color=ACCENT,
            text_color=TEXT_P, dropdown_fg_color=SURFACE,
            dropdown_text_color=TEXT_P, dropdown_hover_color=SURF3,
            font=(FONT_FAMILY, 12),
        )
        menu.grid(row=row, column=1, sticky='w', pady=3)
        if tooltip:
            try:
                from dialogs import Tooltip
                Tooltip(lbl, tooltip); Tooltip(menu, tooltip)
            except Exception:
                pass

    def _opt_check(self, row, label, var):
        ctk.CTkCheckBox(
            self.options_inner, text=label, variable=var,
            fg_color=ACCENT, hover_color=ACCENTL,
            text_color=TEXT_P, font=(FONT_FAMILY, 12),
        ).grid(row=row, column=0, columnspan=2, sticky='w', pady=3)

    def _opt_entry(self, row, label, var, placeholder=''):
        ctk.CTkLabel(self.options_inner, text=label,
                     font=(FONT_FAMILY, 12), text_color=TEXT_P,
                     ).grid(row=row, column=0, sticky='w', padx=(0, 6), pady=3)
        ctk.CTkEntry(
            self.options_inner, textvariable=var, width=220,
            placeholder_text=placeholder, font=(FONT_FAMILY, 12), height=28,
        ).grid(row=row, column=1, sticky='w', pady=3)

    def _opt_dir_picker(self, row, label, var):
        ctk.CTkLabel(self.options_inner, text=label,
                     font=(FONT_FAMILY, 12), text_color=TEXT_P,
                     ).grid(row=row, column=0, sticky='w', padx=(0, 6), pady=3)
        ent = ctk.CTkEntry(
            self.options_inner, textvariable=var,
            font=(FONT_FAMILY, 12), height=28,
        )
        ent.grid(row=row, column=1, sticky='ew', pady=3)
        _btn(self.options_inner, '📁', lambda: self._pick_dir(var),
             width=40,
             ).grid(row=row, column=2, padx=(4, 0), pady=3)

    def _opt_file_picker(self, row, label, var, *,
                         title: str = 'Pick a file',
                         filetypes: list | None = None):
        """Option row with an inline file picker, used by the 'embed subtitles'
        op for the secondary .srt input. Mirrors _opt_dir_picker for layout."""
        ctk.CTkLabel(self.options_inner, text=label,
                     font=(FONT_FAMILY, 12), text_color=TEXT_P,
                     ).grid(row=row, column=0, sticky='w', padx=(0, 6), pady=3)
        ent = ctk.CTkEntry(
            self.options_inner, textvariable=var,
            font=(FONT_FAMILY, 12), height=28,
        )
        ent.grid(row=row, column=1, sticky='ew', pady=3)
        def _pick():
            p = filedialog.askopenfilename(
                parent=self.parent.winfo_toplevel(),
                title=title,
                filetypes=filetypes or [('All files', '*.*')],
            )
            if p: var.set(p)
        _btn(self.options_inner, '📁', _pick, width=40,
             ).grid(row=row, column=2, padx=(4, 0), pady=3)

    # ── 4. Action row ────────────────────────────────────────────────────────

    def _render_action_row(self) -> None:
        row = ctk.CTkFrame(self.container, fg_color='transparent')
        row.grid(row=3, column=0, sticky='ew', pady=(0, PAD_SM))
        row.columnconfigure(2, weight=1)
        self.start_btn = _btn(
            row, '▶  Run', self._do_run,
            fg_color=ACCENT, hover=ACCENTL, text_color='#fff', width=240,
        )
        self.start_btn.grid(row=0, column=0, sticky='w', padx=(0, PAD_SM))
        # Cancel sits right next to Run so a running job can be aborted
        # from the action row, no scrolling needed to reach a progress
        # card further down. Hidden in idle state via grid_remove().
        self.cancel_btn = _btn(
            row, '✕  Cancel', self._do_cancel,
            fg_color=SURF2, hover=ERR, text_color=TEXT_P, width=110,
        )
        self.cancel_btn.grid(row=0, column=1, sticky='w', padx=(0, PAD_SM))
        self.cancel_btn.grid_remove()
        # Inline status label next to the buttons; used by _show_status
        # for non-blocking success / cancel messages.
        self.status_lbl = ctk.CTkLabel(
            row, text='', font=(FONT_FAMILY, 12), text_color=TEXT_S,
            anchor='w',
        )
        self.status_lbl.grid(row=0, column=2, sticky='w', padx=(PAD_SM, 0))
        self._status_after_id = None

    def _show_status(self, text: str, hold_ms: int = 5000) -> None:
        """Set a brief status message next to Run / Cancel. Auto-clears
        after `hold_ms`. Non-blocking, no modal."""
        try:
            self.status_lbl.configure(text=text)
            if self._status_after_id:
                try: self.parent.after_cancel(self._status_after_id)
                except Exception: pass
            self._status_after_id = self.parent.after(
                hold_ms, lambda: self.status_lbl.configure(text=''))
        except Exception:
            pass

    # ── 5. Progress card ─────────────────────────────────────────────────────

    def _render_progress_card(self) -> None:
        self.progress_card = ctk.CTkFrame(
            self.container, fg_color=SURFACE, corner_radius=RADIUS_SM)
        self.progress_card.columnconfigure(0, weight=1)
        self.progress_title = ctk.CTkLabel(
            self.progress_card, text='Starting…',
            font=(FONT_FAMILY, 13, 'bold'), text_color=TEXT_P,
        )
        self.progress_title.grid(row=0, column=0, sticky='w',
                                 padx=PAD, pady=(PAD, 4))
        self.progress_bar = ctk.CTkProgressBar(
            self.progress_card, progress_color=ACCENT, fg_color=SURF3,
            corner_radius=4, height=10,
        )
        self.progress_bar.set(0.0)
        self.progress_bar.grid(row=1, column=0, sticky='ew', padx=PAD, pady=2)
        self.progress_meta = ctk.CTkLabel(
            self.progress_card, text='',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
        )
        self.progress_meta.grid(row=2, column=0, sticky='w',
                                padx=PAD, pady=(2, 4))
        # Cancel button moved to the action row (next to Run) so it stays
        # visible above the fold while a long job runs.

    # ── 6. Result card ───────────────────────────────────────────────────────

    def _render_result_card(self) -> None:
        self.result_card = ctk.CTkFrame(
            self.container, fg_color=SURFACE, corner_radius=RADIUS_SM)
        self.result_card.columnconfigure(0, weight=1)
        # Header row holds the source label + a "Run another" affordance.
        # Without the affordance the auto-scroll-to-result UX traps the
        # user: they see their result, then have to scroll back up by
        # hand to enter a new Source. The button puts them one click
        # from a fresh run.
        hdr_row = ctk.CTkFrame(self.result_card, fg_color='transparent')
        hdr_row.grid(row=0, column=0, sticky='ew', padx=PAD, pady=(PAD, 2))
        hdr_row.columnconfigure(0, weight=1)
        self.result_header = ctk.CTkLabel(
            hdr_row, text='',
            font=(FONT_FAMILY, 14, 'bold'), text_color=TEXT_P,
            anchor='w',
        )
        self.result_header.grid(row=0, column=0, sticky='w')
        _btn(
            hdr_row, '↑  Run another',
            self._scroll_to_source_for_new_run,
            fg_color=SURF2, hover=ACCENTL, text_color=TEXT_P, width=140,
        ).grid(row=0, column=1, sticky='e', padx=(PAD_SM, 0))
        self.result_meta = ctk.CTkLabel(
            self.result_card, text='',
            font=(FONT_FAMILY, 11), text_color=TEXT_S,
        )
        self.result_meta.grid(row=1, column=0, sticky='w',
                              padx=PAD, pady=(0, PAD_SM))
        self.summary_box = ctk.CTkFrame(
            self.result_card, fg_color=SURF2, corner_radius=RADIUS_SM)
        self.summary_box.columnconfigure(0, weight=1)
        self.transcript_frame = ctk.CTkFrame(
            self.result_card, fg_color=BG, corner_radius=RADIUS_SM, height=260)
        self.transcript_frame.grid(row=3, column=0, sticky='nsew',
                                   padx=PAD, pady=PAD_SM)
        self.transcript_frame.grid_propagate(False)
        self.transcript_frame.columnconfigure(0, weight=1)
        self.transcript_frame.rowconfigure(0, weight=1)
        self.result_card.rowconfigure(3, weight=1)
        self.transcript_text = ctk.CTkTextbox(
            self.transcript_frame, fg_color=BG, text_color=TEXT_P,
            font=(FONT_FAMILY, 12), wrap='word',
            corner_radius=0, border_width=0,
        )
        self.transcript_text.grid(row=0, column=0, sticky='nsew',
                                  padx=8, pady=8)
        # Stop the mouse-wheel from bubbling out to the Library's outer
        # scrollable canvas when the cursor is inside the transcript box.
        # Without this, scrolling the lyrics also drags the whole tab page,
        # which is jarring. We bind on the inner tk.Text (CTkTextbox wraps
        # one as `._textbox`) so the inner widget consumes the wheel and
        # we return 'break' to halt propagation. Mirrored for the macOS
        # <Button-4>/<Button-5> events even though we run Windows, keeps
        # the helper portable if anyone ever runs from WSL/X11.
        def _absorb_wheel(_e):
            return 'break'
        try:
            _inner = self.transcript_text._textbox
            _inner.bind('<MouseWheel>',       _absorb_wheel, add='+')
            _inner.bind('<Shift-MouseWheel>', _absorb_wheel, add='+')
            _inner.bind('<Button-4>',         _absorb_wheel, add='+')
            _inner.bind('<Button-5>',         _absorb_wheel, add='+')
        except Exception:
            pass
        # Export bar, only meaningful for transcribe/translate results
        self.export_bar = ctk.CTkFrame(self.result_card, fg_color='transparent')
        self.export_bar.grid(row=4, column=0, sticky='ew', padx=PAD, pady=(0, PAD))
        ctk.CTkLabel(self.export_bar, text='Export:',
                     font=(FONT_FAMILY, 12), text_color=TEXT_S,
                     ).pack(side='left', padx=(0, PAD_SM))
        for fmt in SUPPORTED_FORMATS:
            btn = _btn(self.export_bar, fmt.upper(),
                       lambda f=fmt: self._do_export(f), width=58,
                       fg_color=SURF2, hover=ACCENTL)
            btn.pack(side='left', padx=2)
            # Hover tooltip, explains the format in plain English so the
            # user doesn't have to guess what "LRC" or "VTT" means.
            tip = EXPORT_TOOLTIPS.get(fmt)
            if tip:
                try:
                    from dialogs import Tooltip
                    Tooltip(btn, tip)
                except Exception:
                    pass
        _btn(self.export_bar, '🗑  Delete', self._do_delete_current, width=110,
             fg_color=SURF2, hover=ERR, text_color=TEXT_D,
             ).pack(side='right')

    # ── 7. History card ──────────────────────────────────────────────────────

    def _render_history_card(self) -> None:
        self.history_card = ctk.CTkFrame(
            self.container, fg_color=SURFACE, corner_radius=RADIUS_SM)
        self.history_card.grid(row=6, column=0, sticky='ew', pady=(PAD, 0))
        self.history_card.columnconfigure(0, weight=1)
        ctk.CTkLabel(self.history_card, text='📚  Recent transcripts',
                     font=(FONT_FAMILY, 13, 'bold'), text_color=TEXT_P,
                     ).grid(row=0, column=0, sticky='w',
                            padx=PAD, pady=(PAD, PAD_SM))
        self.history_list = ctk.CTkFrame(
            self.history_card, fg_color='transparent')
        self.history_list.grid(row=1, column=0, sticky='ew',
                               padx=PAD, pady=(0, PAD))
        self.history_list.columnconfigure(0, weight=1)
        self._refresh_history()

    # ── Source handling ──────────────────────────────────────────────────────

    def _do_browse(self) -> None:
        op = OP_BY_KEY.get(self.op_var.get())
        if op and op['needs'] == 'folder':
            # Batch ops need a folder picker, not a file picker.
            path = filedialog.askdirectory(
                parent=self.parent.winfo_toplevel(),
                title='Pick a folder of videos',
            )
            if path:
                self.src_var.set(path)
                self._extra_files = []
            return
        multi = op and op['needs'] == 'multi_file'
        if multi:
            # Multi-file picker for concat
            paths = filedialog.askopenfilenames(
                parent=self.parent.winfo_toplevel(),
                title='Pick files to join (in order)',
                filetypes=[('Media files', '*.*')],
            )
            if paths:
                self.src_var.set(paths[0])
                self._extra_files = [Path(p) for p in paths[1:]]
                if self._extra_files:
                    self._notify('Files selected',
                                 f'{len(paths)} files queued for joining.')
        else:
            path = filedialog.askopenfilename(
                parent=self.parent.winfo_toplevel(),
                title='Choose audio or video file',
                filetypes=[
                    ('Audio', '*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.opus '
                              '*.wma *.aiff *.au *.amr *.ape *.tta *.wv '
                              '*.dsf *.spx *.ac3 *.dts'),
                    ('Video (audio is extracted)',
                     '*.mp4 *.mov *.mkv *.webm *.avi *.flv *.wmv *.ts *.vob '
                     '*.3gp *.mxf *.asf *.mlv'),
                    ('All files', '*.*'),
                ],
            )
            if path:
                self.src_var.set(path)
                self._extra_files = []

    def _pick_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(
            parent=self.parent.winfo_toplevel(),
            title='Choose output folder',
            initialdir=var.get() or str(_default_downloads_dir()),
        )
        if path:
            var.set(path)

    # ── Run dispatcher ───────────────────────────────────────────────────────

    def _do_run(self) -> None:
        """The single action button. Validates input for the chosen op,
        then spins up a worker thread that calls the right backend."""
        if self._worker and self._worker.is_alive():
            self._notify('Already busy',
                         'One job is already running. Wait for it to finish.')
            return
        op = OP_BY_KEY.get(self.op_var.get())
        if not op:
            return
        src = (self.src_var.get() or '').strip().strip('"').strip("'")

        # Validate source per op.needs
        is_url = bool(src) and (src.startswith(('http://', 'https://')) or
                                is_youtube_url(src))
        if op['needs'] == 'url' and not is_url:
            self._notify('URL required',
                         'This operation needs a URL. Paste one in Source.')
            return
        if op['needs'] in ('file', 'audio_or_url') and not src:
            self._notify('Source required',
                         'Paste a URL or pick a file in Source first.')
            return
        if op['needs'] in ('file', 'multi_file') and not is_url:
            if not Path(src).exists():
                self._notify('File not found', f'No such file:\n{src}')
                return
        if op['needs'] == 'folder':
            if not src:
                self._notify('Folder required',
                             'Pick a folder of videos in Source first.')
                return
            if not Path(src).is_dir():
                self._notify('Folder not found',
                             f'No such folder:\n{src}')
                return

        # Build option payload from the catalog's declared options
        opts: dict = self._collect_options(op)

        # Post-collection validation, refuse to start a job with values
        # that would silently produce surprising output.
        err = self._validate_options(op, opts, src)
        if err:
            self._notify('Check your settings', err)
            return

        # One-time model-download confirm: if the selected operation will
        # need a Whisper model that is not bundled and we have not pulled
        # it yet, ask the user before the worker silently kicks off a
        # multi-hundred-megabyte download. Plug-and-play after that: once
        # the file is on disk no prompt fires again.
        if not self._confirm_model_download_if_needed(op, opts):
            return

        # Wire UI to running state
        self._current_op_key = op['key']
        self.start_btn.configure(state='disabled', text='⏳  Running…')
        self.progress_card.grid(row=4, column=0, sticky='ew', pady=(0, PAD_SM))
        self.progress_bar.set(0.0)
        self.progress_title.configure(text=f'{op["label"]}  · starting…')
        self.progress_meta.configure(text='')
        self.cancel_btn.configure(state='normal', text='✕  Cancel')
        self.cancel_btn.grid()   # show next to Run

        self._cancel.clear()
        self._job_t0 = time.time()
        # Stash where the user wants the downloaded source kept, so the
        # _done / _cancelled / _errored handlers can route the file there
        # without having to look up opts again on the UI thread.
        self._current_audio_dir = opts.get('audio_dir') or opts.get('out_dir')
        self._worker = threading.Thread(
            target=self._worker_dispatch,
            args=(op, src, opts),
            daemon=True,
        )
        self._worker.start()
        self._start_poll()

    # Threshold above which we ask the user before triggering an auto-
    # download. base + small are bundled with the installer; anything
    # above ~500 MB is a real wait on most home connections, so we surface
    # it as an explicit "Download / use smaller / cancel" choice.
    _AUTO_DOWNLOAD_PROMPT_MB = 500

    def _confirm_model_download_if_needed(self, op: dict, opts: dict) -> bool:
        """Pre-flight model availability check. Returns True if the job
        should continue (either nothing to download, or the user clicked
        "Download & continue"). Returns False if the user cancelled.

        When the user picks "Use smaller model instead", `opts` is
        mutated in place so the worker gets a model that is already on
        disk."""
        # Only the speech-bearing ops route through Whisper; everything
        # else (download video, extract frame, etc.) is irrelevant here.
        if op['key'] not in (
            'transcribe', 'translate', 'detect_lang',
            'batch_subtitle',
        ):
            return True

        try:
            from transcribe.engine import resolve_planned_model
        except Exception:
            return True

        # What the engine will actually load given (model, translate,
        # music_mode) after its internal music-promote and translate
        # auto-fallback logic.
        translate = (op['key'] == 'translate'
                     or bool(opts.get('translate_to_en', False)))
        plan = resolve_planned_model(
            opts.get('model', 'small'),
            translate=translate,
            music_mode=bool(opts.get('music_mode', False)),
        )

        if plan['on_disk']:
            return True
        if plan['size_mb'] < self._AUTO_DOWNLOAD_PROMPT_MB:
            return True   # tiny model, just pull silently

        # Build a layperson-friendly explanation.
        from transcribe_ui import _MODEL_LABELS as _LBL
        friendly = _LBL.get(plan['effective_model'], plan['effective_model'])
        size_mb = plan['size_mb']
        why = ''
        if plan['switched_for'] == 'music_mode':
            why = ('Music mode is on, which needs the larger AI engine '
                   'to handle sung vocals.\n\n')
        elif plan['switched_for'] == 'translate_fallback':
            why = ('Translate to English needs an engine that is good at '
                   'switching languages.\n\n')

        fallback_friendly = ''
        if plan['fallback_model']:
            fallback_friendly = _LBL.get(plan['fallback_model'],
                                         plan['fallback_model'])

        primary_label = f'⬇  Download & continue (~{size_mb} MB)'
        alt_label     = (f'Use "{fallback_friendly}" instead'
                         if fallback_friendly else '')

        body = (
            f'{why}'
            f'This is a one-time download (~{size_mb} MB). After it '
            f'finishes, every job runs straight away with no waiting.'
        )
        if fallback_friendly:
            body += (f'\n\nIf you would rather not wait, we can run this '
                     f'job right now with "{fallback_friendly}", which is '
                     f'already on your machine.')

        try:
            from dialogs import confirm3
            choice = confirm3(
                self.parent.winfo_toplevel(),
                title='Engine download needed',
                message=body,
                primary_label=primary_label,
                alt_label=alt_label or 'Cancel',
            )
        except Exception:
            return True

        if choice == 'primary':
            return True
        if choice == 'alt' and plan['fallback_model']:
            # Force the worker to use the bundled model. Important: also
            # disable Music mode here when the user dropped to a bundled
            # tier, otherwise the engine will promote them right back up
            # to large-v3-turbo and trigger another download prompt.
            opts['model'] = plan['fallback_model']
            opts['music_mode'] = False
            return True
        return False

    def _validate_options(self, op: dict, opts: dict, src: str) -> str:
        """Return an empty string if the options are sane for this op, or
        a layperson-readable error message if not. Surfaces silent-failure
        cases (blank time fields, audio-only inputs to video ops, etc.)."""
        key = op['key']
        if key == 'trim':
            start = opts.get('start_time', 0.0)
            end   = opts.get('end_time')
            if end is None:
                return 'Type an end time (or leave it blank ONLY if you want to keep everything from the start point onward).'
            if end <= start:
                return ('The end time must be later than the start time.\n\n'
                        f'You entered start = {self.start_t_var.get()} '
                        f'and end = {self.end_t_var.get()}.')
        if key == 'extract_frame':
            # Quick MIME-ish check, video extensions ffmpeg accepts.
            ext = Path(src).suffix.lower().lstrip('.')
            video_exts = {'mp4','mov','mkv','webm','avi','flv','wmv','ts',
                          'vob','3gp','mxf','asf','mlv','m4v'}
            if ext and ext not in video_exts:
                return ('"Save a still image from video" needs a video file.\n\n'
                        f'You picked a {ext.upper()} file. That has no '
                        'video to grab a frame from.')
        if key == 'change_speed':
            try:
                factor = float(self.speed_var.get())
            except ValueError:
                return (f'"{self.speed_var.get()}" is not a number.\n\n'
                        'Type the speed factor as a decimal. For example '
                        '1.5 (50% faster) or 0.5 (half speed).')
            if factor <= 0:
                return 'Speed factor must be greater than 0.'
            if factor > 4 or factor < 0.25:
                return (f'Speed factor {factor} is outside the safe range.\n\n'
                        'Use values between 0.25 and 4. Anything else will '
                        'sound garbled.')
        return ''

    def _collect_options(self, op: dict) -> dict:
        """Read Tk vars into a plain dict based on op['options']."""
        out: dict = {}
        wanted = set(op['options'])
        if 'model' in wanted:
            # Look up in the same list the dropdown was built from so a
            # Translate-mode label (sans large-v3-turbo) still resolves.
            choices = _discover_model_choices(for_translate=(op.get('key') == 'translate'))
            out['model'] = choices.get(self.model_var.get()) \
                           or _discover_model_choices().get(self.model_var.get(), 'base')
        if 'language' in wanted:
            out['language'] = next(
                (c for lbl, c in _LANGUAGES if lbl == self.lang_var.get()),
                None,
            )
        if 'diarize' in wanted: out['diarize'] = bool(self.diarize_var.get())
        if 'summary' in wanted: out['summary'] = bool(self.summary_var.get())
        if 'music_mode' in wanted: out['music_mode'] = bool(self.music_mode_var.get())
        if 'translate_to_en' in wanted:
            out['translate_to_en'] = bool(self.translate_to_en_var.get())
        if 'subtitle_file' in wanted:
            out['subtitle_file'] = self.subtitle_file_var.get().strip()
        if 'burn_subs' in wanted:
            out['burn_subs'] = bool(self.burn_subs_var.get())
        if 'audio_format' in wanted:
            out['audio_format'] = self.audio_fmt_var.get()
        if 'video_format' in wanted:
            lbl = self.video_fmt_var.get()
            fmt = next((f for l, f, _ in DOWNLOAD_FORMATS if l == lbl),
                       'bestaudio/best')
            out['video_format'] = fmt
        if 'sub_langs' in wanted:
            # Map the chosen friendly label back to its ISO code.
            picked = self.sub_langs_var.get()
            code = next((c for lbl, c in _LANGUAGES if lbl == picked and c), 'en')
            out['sub_langs'] = [code]
        if 'out_dir' in wanted:
            out['out_dir'] = Path(self.out_dir_var.get()).expanduser()
        if 'audio_dir' in wanted:
            out['audio_dir'] = Path(self.out_dir_var.get()).expanduser()
        if 'speed' in wanted:
            try:
                out['speed'] = float(self.speed_var.get() or '1.0')
            except ValueError:
                out['speed'] = 1.0
        if 'start_time' in wanted:
            out['start_time'] = _parse_time(self.start_t_var.get())
        if 'end_time' in wanted:
            raw = (self.end_t_var.get() or '').strip()
            out['end_time'] = _parse_time(raw) if raw else None
        if 'frame_time' in wanted:
            out['frame_time'] = _parse_time(self.frame_t_var.get())
        return out

    def _do_cancel(self) -> None:
        self._cancel.set()
        self.cancel_btn.configure(state='disabled', text='Cancelling…')
        self.progress_meta.configure(
            text='Cancellation requested. Current stage will finish.')

    # ── Worker dispatch ──────────────────────────────────────────────────────

    def _worker_dispatch(self, op: dict, src: str, opts: dict) -> None:
        """Long-running work runs here in a daemon thread. We never touch
        Tk from inside, all UI updates go through the message queue."""
        try:
            key = op['key']

            # If this op needs a local audio file but the user gave a URL,
            # ingest the audio first (yt-dlp), then re-use the local copy.
            # yt-dlp natively supports ~1700 sites (SoundCloud, Bandcamp,
            # Vimeo, Twitch, etc.), so we accept any http(s) URL here, not
            # just YouTube, the upload-progress phase still labels it as
            # "Fetching audio…" so the UI reads correctly for any source.
            local_path: Path | None = None
            _is_http_url = bool(src) and src.startswith(('http://', 'https://'))
            if op['needs'] == 'audio_or_url' and (_is_http_url or is_youtube_url(src)):
                self._msg_q.put(('phase', 'download', 0.0, 'Fetching audio…'))
                cache = _yt_cache_dir()
                cache.mkdir(parents=True, exist_ok=True)
                self._yt_local_path = ingest_url(
                    src, cache,
                    on_progress=lambda p: self._msg_q.put(('phase', 'download', p, None)),
                    on_log=lambda m: None,
                )
                local_path = self._yt_local_path

            # Dispatch
            if key in ('transcribe', 'translate'):
                result = self._run_transcribe_like(op, src, opts, local_path)
            elif key == 'batch_subtitle':
                result = self._run_batch_subtitle(src, opts)
            elif key == 'embed_subs':
                result = self._run_embed_subs(src, opts)
            elif key == 'detect_lang':
                result = self._run_detect_lang(src, opts, local_path)
            elif key == 'get_subs':
                result = self._run_get_subs(src, opts)
            elif key == 'dl_audio':
                result = self._run_download(src, opts, audio_only=True)
            elif key == 'extract_audio' or key == 'convert_audio':
                result = self._run_extract_or_convert(src, opts)
            elif key == 'denoise':
                result = self._run_denoise(src, opts)
            elif key == 'normalize':
                result = self._run_normalize(src, opts)
            elif key == 'change_speed':
                result = self._run_change_speed(src, opts)
            elif key == 'dl_video':
                result = self._run_download(src, opts, audio_only=False)
            elif key == 'metadata':
                result = self._run_metadata(src)
            elif key == 'thumbnail':
                result = self._run_thumbnail(src, opts)
            elif key == 'playlist':
                result = self._run_playlist(src, opts)
            elif key == 'trim':
                result = self._run_trim(src, opts)
            elif key == 'extract_frame':
                result = self._run_extract_frame(src, opts)
            elif key == 'find_speech':
                result = self._run_find_speech(src)
            elif key == 'concat':
                result = self._run_concat(src, opts)
            else:
                raise RuntimeError(f'unknown operation: {key}')

            if self._destroyed:
                # For transcripts, persist before exit; for others the
                # work already wrote a file to disk. Preserve the download
                # so the user keeps their bytes even if the window vanished.
                if isinstance(result, dict) and 'segments' in result:
                    try: save_transcript(result)
                    except Exception: pass
                self._cleanup_yt_cache_thread_safe(preserve=True, dest_dir=self._current_audio_dir)
                return
            # If the user clicked Cancel while the engine was running, the
            # engine still returns a partial result. Route to the cancelled
            # path so we don't preserve the audio or save a half-finished
            # transcript: the user explicitly asked to abort.
            if self._cancel.is_set():
                self._msg_q.put(('cancelled', None, op, None))
                return
            self._msg_q.put(('done', result, op, None))

        except Exception as e:
            logger.exception(f'{op["key"]} worker crashed')
            if self._destroyed:
                # On error during teardown, discard the partial/orphan file.
                self._cleanup_yt_cache_thread_safe(preserve=False)
                return
            self._msg_q.put(('error', str(e), type(e).__name__, op))

    # ── Per-operation runners ────────────────────────────────────────────────

    def _run_transcribe_like(self, op: dict, src: str, opts: dict,
                             local_path: Path | None) -> dict:
        """Both 'transcribe' and 'translate' route here, task differs."""
        def _prog(phase: str, pct: float):
            self._msg_q.put(('phase', phase, pct, None))
        translate = (op['key'] == 'translate')
        # For URL source: local_path was set by the dispatcher above.
        # For local source: just use src.
        path = local_path if local_path else Path(src)
        job = transcribe_file(
            path,
            model=opts.get('model', 'base'),
            diarize=bool(opts.get('diarize', False)),
            translate=translate,
            language=opts.get('language'),
            music_mode=bool(opts.get('music_mode', False)),
            on_progress=_prog,
            on_log=lambda m: None,
            should_cancel=self._cancel.is_set,
        )
        job.source = src
        # AI summary if requested
        if opts.get('summary'):
            self._msg_q.put(('phase', 'summary', 0.5, 'Generating AI summary…'))
            try:
                job.summary = self._run_summary(job)
            except Exception as e:
                logger.warning(f'Summary failed: {e}')
        return job.to_dict()

    def _run_summary(self, job: TranscriptJob) -> str:
        try:
            if self._provider_factory:
                provider = self._provider_factory()
            else:
                from engine import build_provider
                provider = build_provider(load_config())
            provider.load()
            text = '\n'.join(s['text'] for s in job.segments)
            if not text.strip():
                return ''
            system = (
                'You are a concise meeting-summary assistant. Output 3-6 '
                'bullet points capturing key topics, decisions, action items. '
                'Skip filler. Return plain text only.'
            )
            return (provider.refine(text, system) or '').strip()
        except Exception as e:
            logger.warning(f'Summary provider failed: {e}')
            return ''

    def _run_detect_lang(self, src: str, opts: dict,
                         local_path: Path | None) -> dict:
        path = local_path if local_path else Path(src)
        result = tools.detect_language(
            path, model=opts.get('model', 'base'),
            on_progress=lambda p: self._msg_q.put(('phase', 'detect', p, None)),
            should_cancel=self._cancel.is_set,
        )
        # Wrap so the dispatch in _finish_job can route to a render path.
        return {'kind': 'lang', 'data': result}

    def _run_get_subs(self, src: str, opts: dict) -> dict:
        paths = tools.get_subtitles(
            src, opts['out_dir'],
            langs=opts.get('sub_langs') or ['en'],
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'files', 'paths': [str(p) for p in paths],
                'message': f'Saved {len(paths)} subtitle file(s)'}

    def _run_download(self, src: str, opts: dict, *,
                      audio_only: bool) -> dict:
        fmt = opts.get('video_format') if not audio_only else 'bestaudio/best'
        if audio_only and opts.get('audio_format'):
            # We let yt-dlp grab bestaudio then convert via tools.convert_audio
            audio_lbl = opts['audio_format']
        else:
            audio_lbl = None
        out_dir = opts['out_dir']
        out_dir.mkdir(parents=True, exist_ok=True)
        path = download_url(
            src, out_dir, fmt,
            on_progress=lambda p: self._msg_q.put(('phase', 'download', p, None)),
            on_log=lambda m: None,
        )
        # Optional audio-format conversion
        if audio_only and audio_lbl and audio_lbl != list(tools.AUDIO_FORMATS.keys())[0]:
            try:
                self._msg_q.put(('phase', 'convert', 0.5, 'Converting…'))
                codec, _ = tools.AUDIO_FORMATS.get(audio_lbl, ('mp3', '192k'))
                ext = {'mp3':'mp3','aac':'m4a','libopus':'opus','flac':'flac',
                       'pcm_s16le':'wav'}.get(codec, codec)
                new_path = path.with_suffix(f'.{ext}')
                tools.convert_audio(path, new_path, audio_lbl,
                                    should_cancel=self._cancel.is_set)
                path.unlink(missing_ok=True)
                path = new_path
            except Exception as e:
                logger.warning(f'Post-download convert failed: {e}')
        return {'kind': 'file', 'path': str(path),
                'message': f'Saved {path.name}'}

    def _run_extract_or_convert(self, src: str, opts: dict) -> dict:
        in_p = Path(src)
        codec, _ = tools.AUDIO_FORMATS.get(opts['audio_format'], ('mp3', '192k'))
        ext = {'mp3':'mp3','aac':'m4a','libopus':'opus','flac':'flac',
               'pcm_s16le':'wav'}.get(codec, codec)
        out = opts['out_dir'] / (in_p.stem + '.' + ext)
        out = _unique_path(out)
        tools.convert_audio(
            in_p, out, opts['audio_format'],
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'file', 'path': str(out), 'message': f'Saved {out.name}'}

    def _run_denoise(self, src: str, opts: dict) -> dict:
        in_p = Path(src)
        # Keep the user's original container so they get an MP3 back when
        # they fed in an MP3, not a 10x-larger WAV. tools.reduce_noise
        # handles the re-encode internally.
        out = _unique_path(opts['out_dir'] / (in_p.stem + '_denoised' + in_p.suffix))
        tools.reduce_noise(
            in_p, out,
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'file', 'path': str(out), 'message': f'Saved {out.name}'}

    def _run_normalize(self, src: str, opts: dict) -> dict:
        in_p = Path(src)
        out = _unique_path(opts['out_dir'] / (in_p.stem + '_normalized' + in_p.suffix))
        tools.normalize_loudness(
            in_p, out,
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'file', 'path': str(out), 'message': f'Saved {out.name}'}

    def _run_change_speed(self, src: str, opts: dict) -> dict:
        in_p = Path(src)
        factor = float(opts.get('speed', 1.5))
        out = _unique_path(opts['out_dir'] / (in_p.stem + f'_x{factor}'.replace('.', 'p') + in_p.suffix))
        tools.change_speed(
            in_p, out, factor=factor,
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'file', 'path': str(out), 'message': f'Saved {out.name}'}

    def _run_metadata(self, src: str) -> dict:
        info = tools.get_metadata(src, should_cancel=self._cancel.is_set)
        return {'kind': 'info', 'data': info}

    def _run_thumbnail(self, src: str, opts: dict) -> dict:
        p = tools.get_thumbnail(src, opts['out_dir'],
                                should_cancel=self._cancel.is_set)
        return {'kind': 'file', 'path': str(p), 'message': f'Saved {p.name}'}

    def _run_playlist(self, src: str, opts: dict) -> dict:
        paths = tools.download_playlist(
            src, opts['out_dir'], opts.get('video_format', 'bestaudio/best'),
            on_progress=lambda p: self._msg_q.put(('phase', 'playlist', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'files', 'paths': [str(p) for p in paths],
                'message': f'Downloaded {len(paths)} item(s)'}

    def _run_trim(self, src: str, opts: dict) -> dict:
        in_p = Path(src)
        start = float(opts.get('start_time', 0.0))
        end   = opts.get('end_time')
        out = _unique_path(opts['out_dir'] / (in_p.stem + '_trimmed' + in_p.suffix))
        tools.trim_media(
            in_p, out, start=start, end=end, reencode=True,
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'file', 'path': str(out), 'message': f'Saved {out.name}'}

    def _run_extract_frame(self, src: str, opts: dict) -> dict:
        in_p = Path(src)
        at = float(opts.get('frame_time', 0.0))
        out = _unique_path(opts['out_dir'] / (in_p.stem + f'_frame_{int(at)}.png'))
        tools.extract_frame(
            in_p, out, at=at,
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'file', 'path': str(out), 'message': f'Saved {out.name}'}

    def _run_find_speech(self, src: str) -> dict:
        result = tools.find_speech_segments(
            Path(src),
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'vad', 'data': result}

    def _run_concat(self, src: str, opts: dict) -> dict:
        inputs = [Path(src)] + list(self._extra_files)
        ext = inputs[0].suffix or '.mp4'
        out = _unique_path(opts['out_dir'] / ('joined' + ext))
        tools.concat_media(
            inputs, out, reencode=True,
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'file', 'path': str(out),
                'message': f'Joined {len(inputs)} files → {out.name}'}

    # File extensions that batch subtitling treats as video. We use a
    # generous set so the user does not have to know which container their
    # rip happens to be in (mkv / mp4 / avi / etc.). Audio files are also
    # accepted because Whisper does not actually need a video stream; the
    # output .srt sits next to the file regardless.
    _BATCH_EXTS = {
        '.mkv', '.mp4', '.mov', '.avi', '.webm', '.flv', '.wmv',
        '.ts', '.m4v', '.mpg', '.mpeg', '.3gp', '.vob',
        '.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.opus',
    }

    def _run_batch_subtitle(self, src: str, opts: dict) -> dict:
        """Walk a folder of videos and write one .srt next to each.

        Cancel-safe per file (Whisper sees the cancel flag at every segment
        boundary). On per-file error we log and continue so one bad file
        does not abort the whole batch.
        """
        from transcribe.exporters import export as _export
        folder = Path(src)
        files = sorted(p for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in self._BATCH_EXTS)
        if not files:
            return {'kind': 'file',
                    'path': str(folder),
                    'message': (f'No video / audio files in {folder.name}. '
                                f'Looked for: {", ".join(sorted(self._BATCH_EXTS))}')}

        translate = bool(opts.get('translate_to_en', False))
        model = opts.get('model', 'small')
        language = opts.get('language')
        music_mode = bool(opts.get('music_mode', False))
        total = len(files)

        written = []
        skipped = []
        for idx, f in enumerate(files):
            if self._cancel.is_set():
                break
            # Skip files that already have a .srt next to them so re-runs
            # are idempotent. The user can delete the .srt if they want a
            # fresh transcription.
            srt_target = f.with_suffix('.srt')
            if srt_target.exists():
                skipped.append(f.name)
                continue

            self._msg_q.put(('phase', 'transcribe',
                             idx / max(total, 1),
                             f'Subtitle {idx+1}/{total}: {f.name[:60]}'))
            try:
                job = transcribe_file(
                    f,
                    model=model,
                    diarize=False,
                    translate=translate,
                    language=language,
                    music_mode=music_mode,
                    on_progress=lambda phase, p: None,
                    on_log=lambda m: None,
                    should_cancel=self._cancel.is_set,
                )
                if not job.segments:
                    skipped.append(f'{f.name} (no speech)')
                    continue
                # Hand off to the standard SRT exporter so the format
                # exactly matches what the manual Export → SRT writes.
                _export(job, 'srt', srt_target)
                written.append(srt_target.name)
            except Exception as e:
                logger.exception(f'batch_subtitle: failed on {f.name}')
                skipped.append(f'{f.name} (error: {type(e).__name__})')

        self._msg_q.put(('phase', 'transcribe', 1.0, None))
        cancelled = self._cancel.is_set()
        verb = 'Subtitled' if not cancelled else 'Partially subtitled before cancel'
        msg_parts = [f'{verb} {len(written)} of {total} files in {folder.name}.']
        if skipped:
            msg_parts.append(f'Skipped: {", ".join(skipped[:5])}'
                             + (f' (+{len(skipped)-5} more)' if len(skipped) > 5 else ''))
        return {'kind': 'file',
                'path': str(folder),
                'message': ' '.join(msg_parts)}

    def _run_embed_subs(self, src: str, opts: dict) -> dict:
        """Mux (or burn) an external .srt into a video file."""
        in_p = Path(src)
        sub_p_str = (opts.get('subtitle_file') or '').strip()
        if not sub_p_str:
            raise RuntimeError('No subtitle file picked. Use the "Subtitle '
                               'file" picker below the operation list.')
        sub_p = Path(sub_p_str)
        if not sub_p.exists():
            raise RuntimeError(f'Subtitle file not found:\n{sub_p}')
        burn = bool(opts.get('burn_subs', False))
        # MKV is the safe default for soft-mux because it carries SRT
        # natively. For MP4 we'd need mov_text, which we let ffmpeg
        # convert into. For burn we always produce MP4 since the subs
        # are now baked into the picture stream.
        # Derive a language hint from the SRT filename so two runs with
        # different SRTs produce distinguishable output names (a soft-mux
        # of `alien.en.srt` lands as `alien_subs_en.mkv`, and a soft-mux
        # of `alien.orig.srt` lands as `alien_subs_orig.mkv`). Without
        # this hint the user got `alien_subs.mkv` and `alien_subs (1).mkv`
        # which lose the language signal entirely.
        sub_stem = sub_p.stem  # 'alien.en' from 'alien.en.srt'
        hint = sub_stem.rsplit('.', 1)[-1] if '.' in sub_stem else ''
        # Strip hint if it looks like a generic name component (e.g. user
        # named their file just "subs.srt"); only keep when it reads as a
        # language tag.
        if hint in (in_p.stem.lower(), '', sub_stem.lower()):
            hint = ''
        if burn:
            out_ext = '.mp4'
            suffix  = '_subs_burned'
        else:
            out_ext = in_p.suffix.lower() if in_p.suffix.lower() in ('.mkv', '.mp4') else '.mkv'
            suffix  = '_subs'
        if hint:
            suffix += f'_{hint}'
        out = _unique_path(opts['out_dir'] / (in_p.stem + suffix + out_ext))
        tools.embed_subtitles(
            in_p, sub_p, out, burn=burn,
            on_progress=lambda p: self._msg_q.put(('phase', 'process', p, None)),
            should_cancel=self._cancel.is_set,
        )
        return {'kind': 'file', 'path': str(out),
                'message': f'Saved {out.name}'}

    # ── Poll loop (worker → UI) ──────────────────────────────────────────────

    def _start_poll(self) -> None:
        self._poll_id = self.parent.after(150, self._poll)

    def _poll(self) -> None:
        if self._destroyed:
            return
        try:
            while True:
                kind, a, b, c = self._msg_q.get_nowait()
                if kind == 'phase':
                    self._update_phase(a, b, c)
                elif kind == 'done':
                    self._finish_job(a, b)
                    return
                elif kind == 'cancelled':
                    self._cancelled()
                    return
                elif kind == 'error':
                    self._errored(a, b, c)
                    return
        except queue.Empty:
            pass

        # Keep the elapsed-time line ticking even when no phase events
        # arrive (model load can sit silent for 30+s on large-v3, and
        # there's also a gap between VAD finishing and the first Whisper
        # segment). Without this, the UI looks frozen.
        try:
            if getattr(self, '_job_t0', 0) and self._worker and self._worker.is_alive():
                elapsed = time.time() - self._job_t0
                since_phase = time.time() - getattr(self, '_last_phase_t', self._job_t0)
                spinner = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
                tick = spinner[int(time.time() * 8) % len(spinner)]
                if since_phase > 3.0:
                    meta = f'Elapsed: {_fmt_duration(elapsed)}  ·  {tick} still working…'
                else:
                    meta = f'Elapsed: {_fmt_duration(elapsed)}'
                self.progress_meta.configure(text=meta)
        except Exception:
            pass
        if self._worker and self._worker.is_alive() and not self._destroyed:
            self._poll_id = self.parent.after(150, self._poll)
        else:
            self._poll_id = None

    def _update_phase(self, phase: str, pct: float, label: str | None) -> None:
        names = {
            'download':       '⬇  Downloading',
            'download_model': '🧠 Downloading AI model (one-time)',
            'load_model':     '🧠 Loading model into memory…',
            'analyze':        '🎚️ Analyzing audio…',
            'transcribe':     '📝 Transcribing',
            'diarize':        '🎤 Identifying speakers',
            'summary':        '✨ AI summary',
            'merge':          '🪡 Finalising',
            'detect':         '🔤 Detecting language',
            'process':        '⚙️  Processing',
            'convert':        '🔁 Converting',
            'playlist':       '📚 Downloading playlist',
        }
        self.progress_title.configure(text=label or names.get(phase, phase))
        self.progress_bar.set(max(0.0, min(1.0, float(pct))))
        elapsed = time.time() - self._job_t0
        self.progress_meta.configure(text=f'Elapsed: {_fmt_duration(elapsed)}')
        self._last_phase_t = time.time()

    # ── Termination handlers ─────────────────────────────────────────────────

    def _finish_job(self, payload, op: dict) -> None:
        self._reset_action_buttons()
        self.progress_card.grid_forget()

        # Transcribe / translate → render full transcript view + persist
        if isinstance(payload, dict) and 'segments' in payload:
            job = TranscriptJob.from_dict(payload)
            self._current_job = job
            try: save_transcript(payload)
            except Exception as e: logger.warning(f'save_transcript: {e}')
            # Move the kept download into Downloads BEFORE rendering so the
            # result meta can show its final path. Otherwise the render
            # reads a stale None and the user never sees where their file
            # went.
            self._cleanup_yt_cache(preserve=True, dest_dir=self._current_audio_dir)
            self._render_result_for(job)
            self._refresh_history()
            return

        # Other ops return {'kind': 'file'|'files'|'info'|'vad', ...}
        kind = (payload or {}).get('kind')
        if kind == 'file':
            path = Path(payload['path'])
            # Open the folder so the user sees their file. No modal blocks
            # the panel; the in-place opened folder is the feedback.
            try:
                if os.name == 'nt': os.startfile(str(path.parent))
            except Exception: pass
            self._show_status(f'✓ Saved {path.name} to {path.parent}')
        elif kind == 'files':
            self._show_status(f'✓ {payload.get("message", "Saved.")}')
        elif kind == 'info':
            self._render_info_for(payload['data'])
        elif kind == 'vad':
            self._render_vad_for(payload['data'])
        elif kind == 'lang':
            self._render_lang_for(payload['data'])
        # Non-transcribe ops typically wrote their own file already; the
        # input is still worth keeping if it came from a URL.
        self._cleanup_yt_cache(preserve=True)

    def _cancelled(self) -> None:
        self._reset_action_buttons()
        self.progress_card.grid_forget()
        # Cancelled mid-job, file is orphan / partial, drop it.
        self._cleanup_yt_cache(preserve=False)
        # Non-blocking inline note; the user just clicked Cancel, blocking
        # them with a modal "Operation stopped" they have to dismiss
        # is condescending and obstructs the panel.
        self._show_status('✕ Cancelled')

    def _errored(self, msg: str, exc_type: str, op: dict | None) -> None:
        self._reset_action_buttons()
        self.progress_card.grid_forget()
        # Errored mid-job, file may be incomplete, drop it.
        self._cleanup_yt_cache(preserve=False)
        body = (msg or '').strip() or exc_type
        op_lbl = (op or {}).get('label', 'Operation')
        # Translate the most common technical errors into plain English so a
        # non-technical user gets a useful message instead of a stack-trace
        # snippet. Falls through to the raw text for anything we don't
        # recognise, that's still better than nothing.
        friendly = _humanize_error(exc_type, body)
        self._notify(f'{op_lbl} failed', friendly[:300])

    def _reset_action_buttons(self) -> None:
        try:
            op = OP_BY_KEY.get(self.op_var.get() if self.op_var else '')
            if op:
                import re as _re
                clean = _re.sub(r'^[^\w]+', '', op['label']).strip()
                self.start_btn.configure(state='normal', text=f'▶  {clean}')
            else:
                self.start_btn.configure(state='normal', text='▶  Run')
        except Exception: pass
        try:
            self.cancel_btn.grid_remove()
        except Exception: pass

    def _cleanup_yt_cache(self, preserve: bool = False,
                          dest_dir: Path | None = None) -> None:
        """Finalise the downloaded audio after a job.

        * preserve=True (default for successful jobs)  →  move the file out
          of the transient cache into `dest_dir` (falls back to the user's
          Downloads folder if not provided) so they keep the audio they
          just fetched. Downloading is a real feature on its own,
          deleting it after every transcribe would silently throw away
          the user's bytes.
        * preserve=False (cancel / error)  →  unlink, since a half-used
          or orphan file isn't useful and would grow the cache unbounded.
        """
        p = self._yt_local_path
        self._yt_local_path = None
        if not p or not p.exists():
            return
        if not preserve:
            try: p.unlink()
            except Exception as e: logger.warning(f'YT cache cleanup: {e}')
            return
        try:
            target_dir = dest_dir or _default_downloads_dir()
            target_dir.mkdir(parents=True, exist_ok=True)
            dest = target_dir / p.name
            n = 1
            while dest.exists():
                dest = target_dir / f'{p.stem} ({n}){p.suffix}'
                n += 1
            import shutil
            shutil.move(str(p), str(dest))
            self._kept_download_path = dest
            logger.info(f'Kept downloaded audio: {dest}')
        except Exception as e:
            logger.warning(f'YT cache preserve failed: {e}')
            self._kept_download_path = p

    def _cleanup_yt_cache_thread_safe(self, preserve: bool = False,
                                      dest_dir: Path | None = None) -> None:
        # Worker-thread variant, same semantics, just must not touch Tk.
        p = self._yt_local_path
        if not p or not p.exists():
            self._yt_local_path = None
            return
        if not preserve:
            try: p.unlink()
            except Exception: pass
            self._yt_local_path = None
            return
        try:
            target_dir = dest_dir or _default_downloads_dir()
            target_dir.mkdir(parents=True, exist_ok=True)
            dest = target_dir / p.name
            n = 1
            while dest.exists():
                dest = target_dir / f'{p.stem} ({n}){p.suffix}'
                n += 1
            import shutil
            shutil.move(str(p), str(dest))
            self._kept_download_path = dest
        except Exception:
            self._kept_download_path = p
        self._yt_local_path = None

    # ── Result rendering ─────────────────────────────────────────────────────

    def _scroll_to_source_for_new_run(self) -> None:
        """Snap the panel back to the top, focus the Source field, and
        select its contents so the user can paste a new URL or type a
        new path immediately. Called by the "Run another" button on the
        result card."""
        try:
            w = self.parent
            while w is not None and not hasattr(w, '_parent_canvas'):
                w = getattr(w, 'master', None)
            if w is not None:
                w._parent_canvas.yview_moveto(0.0)
        except Exception:
            pass
        try:
            # CTkEntry wraps a real tk.Entry as `._entry`; the selection
            # helpers live there, not on the wrapper.
            inner = getattr(self.src_entry, '_entry', None) or self.src_entry
            inner.focus_set()
            try:
                inner.select_range(0, 'end')
                inner.icursor('end')
            except Exception:
                pass
        except Exception:
            pass

    def _wire_wheel_forwarding(self) -> None:
        """Recursively bind <MouseWheel> on every descendant of self.container
        so the mouse wheel scrolls the Library's outer CTkScrollableFrame
        even when the cursor is over a nested widget (radio button,
        dropdown, options card, etc.).

        Tk routes wheel events to the topmost widget under the cursor.
        CTk's CTkScrollableFrame only auto-handles wheel events on its
        direct children, so anything nested deeper (which is every
        widget in this panel) swallows the wheel and never reaches the
        canvas. The forwarder finds the parent canvas once, then re-
        delivers each wheel event to it via yview_scroll.

        We skip the transcript text widget, it has its own absorber
        that returns 'break' so scrolling lyrics does not yank the
        whole tab around.
        """
        # Locate the outer canvas once.
        canvas = None
        w = self.parent
        while w is not None:
            if hasattr(w, '_parent_canvas'):
                canvas = w._parent_canvas
                break
            w = getattr(w, 'master', None)
        if canvas is None:
            return

        def _forward(event):
            try:
                # Tk on Windows: event.delta is ±120 per notch. CTk's canvas
                # uses a 1px scroll-increment, so yview_scroll('units') is
                # painfully slow. Instead move by a fraction of the visible
                # viewport per notch — matches Windows' standard 3-line
                # scroll feel without depending on the canvas's font metrics.
                notches = -1 * event.delta / 120  # +/-1 per wheel click
                top, bottom = canvas.yview()
                view_frac = max(0.05, bottom - top)
                # 15% of the visible viewport per notch ≈ Windows default.
                canvas.yview_moveto(max(0.0, min(1.0 - view_frac,
                                                  top + notches * view_frac * 0.15)))
            except Exception:
                pass
            return 'break'

        # Widgets to skip so they keep owning their own wheel events.
        skip_widgets = {getattr(self, 'transcript_text', None)}

        def _bind_recursive(widget):
            if widget in skip_widgets:
                return
            try:
                widget.bind('<MouseWheel>',       _forward, add='+')
                widget.bind('<Shift-MouseWheel>', _forward, add='+')
                widget.bind('<Button-4>',         _forward, add='+')
                widget.bind('<Button-5>',         _forward, add='+')
            except Exception:
                pass
            try:
                for child in widget.winfo_children():
                    _bind_recursive(child)
            except Exception:
                pass

        _bind_recursive(self.container)

    def _scroll_result_into_view(self) -> None:
        """Drag the Library's outer canvas so the result card is on screen
        when a job finishes. Best-effort: walks up to the CTkScrollableFrame
        ancestor and sets its canvas yview to the top of the result card.
        Falls back silently if the layout has changed."""
        try:
            w = self.parent
            while w is not None and not hasattr(w, '_parent_canvas'):
                w = getattr(w, 'master', None)
            if w is None: return
            canvas = w._parent_canvas
            self.result_card.update_idletasks()
            # Top of result card in canvas coordinates
            top_y = self.result_card.winfo_y()
            scroll_max = canvas.bbox('all')
            if not scroll_max: return
            total_h = scroll_max[3] - scroll_max[1]
            if total_h <= 0: return
            canvas.yview_moveto(max(0, top_y / total_h - 0.02))
        except Exception:
            pass

    def _render_result_for(self, job: TranscriptJob) -> None:
        self.result_card.grid(row=5, column=0, sticky='nsew', pady=(0, PAD_SM))
        # After the result card is laid out, scroll it into view. Without
        # this the user lands back at the top of the panel after a long
        # job and has to scroll down to find their transcript, which is
        # confusing on first use.
        self.parent.after(50, lambda: self._scroll_result_into_view())
        src_label = Path(job.source).name if not job.source.startswith('http') else job.source
        self.result_header.configure(text=src_label)
        speakers = {s.get('speaker') for s in job.segments if s.get('speaker')}
        meta = (f'{_fmt_duration(job.duration)}  ·  '
                f'{len(job.segments)} segments  ·  '
                f'language {job.language or "?"}  ·  '
                f'model {job.model}')
        if speakers:
            meta += f'  ·  {len(speakers)} speakers'
        elif getattr(job, 'diarize_note', ''):
            meta += f'  ·  diarization skipped: {job.diarize_note[:80]}'
        # Show where the downloaded source was kept (URL inputs only).
        kept = self._kept_download_path
        if kept and kept.exists():
            meta += f'\n💾  Audio saved: {kept}'
        self.result_meta.configure(text=meta)

        # Summary
        for w in self.summary_box.winfo_children(): w.destroy()
        if job.summary:
            self.summary_box.grid(row=2, column=0, sticky='ew',
                                  padx=PAD, pady=(0, PAD_SM))
            ctk.CTkLabel(self.summary_box, text='Summary',
                         font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_P,
                         ).grid(row=0, column=0, sticky='w',
                                padx=PAD, pady=(PAD_SM, 2))
            ctk.CTkLabel(self.summary_box, text=job.summary,
                         font=(FONT_FAMILY, 12), text_color=TEXT_P,
                         wraplength=860, justify='left',
                         ).grid(row=1, column=0, sticky='w',
                                padx=PAD, pady=(0, PAD_SM))
        else:
            self.summary_box.grid_forget()

        # Transcript body
        self.transcript_text.configure(state='normal')
        self.transcript_text.delete('1.0', 'end')
        if not job.segments:
            # Zero-result case: instead of leaving the user with a blank
            # box and a cryptic "0 segments", tell them WHY and what to
            # try next. The most common cause is VAD removing audio that
            # is mostly music or silence.
            self.transcript_text.insert(
                'end',
                "No speech was found in this audio.\n\n"
                "Common reasons:\n"
                "  • It is a song or instrumental piece — tick Music mode "
                "and try again.\n"
                "  • The audio is silent or very quiet — try Even out the "
                "volume first.\n"
                "  • It is a non-speech track like sound effects or noise.\n"
                "  • The speech is in a language Whisper cannot detect — "
                "pick the language manually.\n"
            )
        else:
            cur_speaker = None
            for s in job.segments:
                sp = s.get('speaker') or ''
                ts = _fmt_duration(s['start'])
                if sp and sp != cur_speaker:
                    self.transcript_text.insert('end', f'\n{sp}\n')
                    cur_speaker = sp
                self.transcript_text.insert('end', f'[{ts}]  {s["text"]}\n')
        self.transcript_text.configure(state='disabled')

        # Export bar: hidden when there is nothing to export.
        try:
            if job.segments:
                self.export_bar.grid()
            else:
                self.export_bar.grid_remove()
        except Exception: pass

    def _render_info_for(self, info: dict) -> None:
        self.result_card.grid(row=5, column=0, sticky='nsew', pady=(0, PAD_SM))
        title = info.get('title') or 'Untitled'
        self.result_header.configure(text=title)
        meta_bits = []
        if info.get('channel'): meta_bits.append(info['channel'])
        if info.get('duration'): meta_bits.append(_fmt_duration(info['duration']))
        if info.get('view_count'):
            meta_bits.append(f'{info["view_count"]:,} views')
        if info.get('upload_date'):
            d = info['upload_date']
            meta_bits.append(f'{d[:4]}-{d[4:6]}-{d[6:8]}')
        self.result_meta.configure(text='  ·  '.join(meta_bits))

        self.summary_box.grid_forget()
        self.transcript_text.configure(state='normal')
        self.transcript_text.delete('1.0', 'end')
        if info.get('description'):
            self.transcript_text.insert('end', info['description'])
        avail = (info.get('subtitles') or []) + (info.get('auto_subtitles') or [])
        if avail:
            self.transcript_text.insert(
                'end',
                f'\n\n── Available subtitles ──\n{", ".join(sorted(set(avail))[:30])}',
            )
        self.transcript_text.configure(state='disabled')
        try: self.export_bar.grid_remove()
        except Exception: pass

    def _render_lang_for(self, data: dict) -> None:
        """Pretty-print the language-detection result. Top language by
        probability + the next 4 runners-up so the user sees the model
        wasn't guessing."""
        self.result_card.grid(row=5, column=0, sticky='nsew', pady=(0, PAD_SM))
        # Map ISO codes back to friendly names where we have them
        iso_to_name = {c: lbl for lbl, c in _LANGUAGES if c}
        primary = data.get('language', '?')
        primary_name = iso_to_name.get(primary, primary)
        prob = data.get('probability', 0.0)
        self.result_header.configure(text=f'{primary_name}')
        self.result_meta.configure(
            text=f'{prob*100:.1f}% confident  ·  ISO code: {primary}'
        )
        self.summary_box.grid_forget()
        self.transcript_text.configure(state='normal')
        self.transcript_text.delete('1.0', 'end')
        # Sorted top 5
        items = sorted((data.get('all') or {}).items(),
                       key=lambda kv: kv[1], reverse=True)[:5]
        self.transcript_text.insert('end', 'Top candidates:\n\n')
        for code, p in items:
            name = iso_to_name.get(code, code)
            self.transcript_text.insert('end',
                                        f'  {name:14s} ({code})  {p*100:5.1f}%\n')
        self.transcript_text.configure(state='disabled')
        try: self.export_bar.grid_remove()
        except Exception: pass

    def _render_vad_for(self, data: dict) -> None:
        self.result_card.grid(row=5, column=0, sticky='nsew', pady=(0, PAD_SM))
        self.result_header.configure(text='Speech segments')
        self.result_meta.configure(
            text=(f'{_fmt_duration(data["duration"])} total  ·  '
                  f'{_fmt_duration(data["speech_s"])} speech  ·  '
                  f'{_fmt_duration(data["silence_s"])} silence  ·  '
                  f'{len(data["segments"])} segments')
        )
        self.summary_box.grid_forget()
        self.transcript_text.configure(state='normal')
        self.transcript_text.delete('1.0', 'end')
        for i, seg in enumerate(data['segments'], 1):
            self.transcript_text.insert(
                'end',
                f'{i:3d}.  {_fmt_duration(seg["start"])}  →  '
                f'{_fmt_duration(seg["end"])}  '
                f'({seg["end"]-seg["start"]:.1f}s)\n',
            )
        self.transcript_text.configure(state='disabled')
        try: self.export_bar.grid_remove()
        except Exception: pass

    # ── Export / delete ──────────────────────────────────────────────────────

    def _do_export(self, fmt: str) -> None:
        if not self._current_job: return
        stem = Path(self._current_job.source).stem or 'transcript'
        # When source is a URL (e.g. https://youtube.com/watch?v=ID),
        # Path(url).stem returns 'watch?v=ID' — the '?' is an illegal
        # Windows filename char and the Save dialog silently rejects it
        # on click. Strip < > : " / \ | ? * and any control chars.
        import re as _re
        stem = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem).strip(' ._') or 'transcript'
        path = filedialog.asksaveasfilename(
            parent=self.parent.winfo_toplevel(),
            title=f'Export {fmt.upper()}',
            defaultextension=f'.{fmt}',
            initialfile=f'{stem}.{fmt}',
            filetypes=[(f'{fmt.upper()} file', f'*.{fmt}'), ('All files', '*.*')],
        )
        if not path: return
        try:
            export(self._current_job, fmt, path)
            self._notify('Exported', f'{Path(path).name}')
            try:
                if os.name == 'nt': os.startfile(os.path.dirname(path))
            except Exception: pass
        except Exception as e:
            # Route through the shared humanizer so users see a sentence,
            # not a raw Python exception class name + repr.
            self._notify('Export failed',
                         _humanize_error(type(e).__name__, str(e)))

    def _do_delete_current(self) -> None:
        if not self._current_job: return
        if not confirm(self.parent.winfo_toplevel(), 'Delete transcript',
                       'Remove this transcript from history?'):
            return
        delete_transcript(self._current_job.id)
        self._current_job = None
        self.result_card.grid_forget()
        self._refresh_history()

    # ── History list ─────────────────────────────────────────────────────────

    def _refresh_history(self) -> None:
        for w in self.history_list.winfo_children(): w.destroy()
        items = load_transcripts()
        if not items:
            ctk.CTkLabel(self.history_list,
                         text='No transcripts yet. Your first one will appear here.',
                         font=(FONT_FAMILY, 12), text_color=TEXT_S,
                         ).grid(row=0, column=0, sticky='w', pady=(0, PAD_SM))
            return
        for i, j in enumerate(items[:30]):
            self._render_history_row(self.history_list, i, j)

    def _render_history_row(self, parent, row: int, j: dict) -> None:
        bar = ctk.CTkFrame(parent, fg_color=SURF2, corner_radius=RADIUS_SM)
        bar.grid(row=row, column=0, sticky='ew', pady=2)
        bar.columnconfigure(0, weight=1)
        src = j.get('source', '?')
        label = Path(src).name if not src.startswith('http') else src
        meta = (f'{_fmt_duration(j.get("duration", 0))}  ·  '
                f'{j.get("language", "?")}  ·  '
                f'{j.get("model", "?")}  ·  '
                f'{_fmt_rel(j.get("created_at", time.time()))}')
        ctk.CTkLabel(bar, text=label[:80],
                     font=(FONT_FAMILY, 12, 'bold'), text_color=TEXT_P,
                     anchor='w',
                     ).grid(row=0, column=0, sticky='ew', padx=PAD, pady=(6, 0))
        ctk.CTkLabel(bar, text=meta,
                     font=(FONT_FAMILY, 11), text_color=TEXT_S, anchor='w',
                     ).grid(row=1, column=0, sticky='ew', padx=PAD, pady=(0, 6))
        _btn(bar, 'Open', lambda jj=j: self._open_history(jj), width=70,
             ).grid(row=0, column=1, rowspan=2, padx=4, pady=4)
        _btn(bar, '✕', lambda jj=j: self._delete_history(jj), width=32,
             fg_color='transparent', hover=ERR, text_color=TEXT_D,
             ).grid(row=0, column=2, rowspan=2, padx=(0, 4), pady=4)

    def _open_history(self, j: dict) -> None:
        try:
            job = TranscriptJob.from_dict(j)
            self._current_job = job
            self._render_result_for(job)
        except Exception as e:
            self._notify('Could not open',
                         _humanize_error(type(e).__name__, str(e)))

    def _delete_history(self, j: dict) -> None:
        jid = j.get('id', '')
        if not jid: return
        if not confirm(self.parent.winfo_toplevel(), 'Delete transcript',
                       f'Remove "{Path(j.get("source","?")).name}"?'):
            return
        delete_transcript(jid)
        if self._current_job and self._current_job.id == jid:
            self._current_job = None
            self.result_card.grid_forget()
        self._refresh_history()

    # ── Public teardown ──────────────────────────────────────────────────────

    def destroy(self) -> None:
        self._destroyed = True
        if self._poll_id:
            try: self.parent.after_cancel(self._poll_id)
            except Exception: pass
            self._poll_id = None
        if self._worker and self._worker.is_alive():
            self._cancel.set()
            logger.info('TranscribePanel destroyed mid-job, worker will '
                        'cancel at next checkpoint')
        else:
            self._cleanup_yt_cache()


# ── Module helpers ────────────────────────────────────────────────────────────

def _parse_time(s: str) -> float:
    """Convert a forgiving range of time strings to seconds (float).

    Accepts:
      'HH:MM:SS', 'MM:SS', 'SS'
      '12.5', '90', '90s'
      '1m30s', '1h2m3s', '2h30m'

    Returns 0.0 on parse failure, callers should validate before using.
    """
    import re as _re
    s = (s or '').strip().lower()
    if not s:
        return 0.0
    # Colon-separated (most common)
    if ':' in s:
        parts = s.split(':')
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        except ValueError:
            return 0.0
    # Suffix form: 90s / 1m30s / 1h2m3s / 2h30m
    suffix = _re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?', s)
    if suffix and any(suffix.groups()):
        h, m, sec = suffix.groups()
        return ((int(h) if h else 0) * 3600
                + (int(m) if m else 0) * 60
                + (float(sec) if sec else 0.0))
    # Plain number = seconds
    try:
        return float(s)
    except ValueError:
        return 0.0


def _humanize_error(exc_type: str, body: str) -> str:
    """Map noisy Python / ffmpeg / yt-dlp errors to a sentence a layperson
    can act on. Always preserves the original at the end so power users
    can still see what really happened."""
    low = body.lower()
    if exc_type == 'FileNotFoundError' or 'no such file' in low:
        return ("We couldn't find that file. Did the path get typed wrong, "
                "or was the file moved?")
    if 'sign in to confirm' in low or 'age' in low and 'restrict' in low:
        return ('YouTube wants you to sign in to view this video '
                '(age-restricted or members-only). The app cannot do this.')
    if 'private video' in low or 'unavailable' in low:
        return ('The video is private, removed, or not available in your '
                'region.')
    if 'http error 429' in low or 'too many requests' in low:
        return ('YouTube is rate-limiting your IP. Wait a few minutes '
                'before trying again.')
    if 'no audio' in low or 'no such stream' in low:
        return ('This file has no audio stream. Try a different file.')
    if 'ffmpeg exited' in low:
        return ('ffmpeg could not process this file. It may be corrupted, '
                'protected, or in an unsupported codec.\n\nDetails: '
                + body[:160])
    if 'live stream' in low:
        return ('This is a live stream. They have to be recorded as they '
                'happen and cannot be downloaded after the fact.')
    if 'cannot find' in low and ('ffmpeg' in low or 'codec' in low):
        return ('Missing ffmpeg or codec support. The bundled ffmpeg should '
                'have handled this. Please report the file format.')
    if exc_type == 'PermissionError':
        return ("The output folder isn't writable, or a file in it is open "
                'in another program. Pick a different folder or close it.')
    if exc_type == 'MemoryError':
        return ('The file is too large to process in memory on this machine. '
                'Try a shorter clip or split the file first.')
    # Default: short prefix + raw
    return f'{exc_type}: {body[:200]}'


def _unique_path(p: Path) -> Path:
    """Return `p` if it doesn't exist, else `p` with ` (1)`, ` (2)`… until
    unused. Prevents silent clobber when the user runs the same op twice."""
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    parent = p.parent
    for i in range(1, 1000):
        candidate = parent / f'{stem} ({i}){suffix}'
        if not candidate.exists():
            return candidate
    return p
