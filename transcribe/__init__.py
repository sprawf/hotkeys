"""TurboScribe-style transcription package.

A complete pipeline that turns an audio/video file (or YouTube URL) into a
diarized, multi-format transcript with optional AI summary, all built on
top of the dependencies the Hotkeys app already ships:

  • faster-whisper  → ASR (already used for live dictation)
  • pyannote.audio  → speaker diarization (added: ~280 MB to dist incl. CPU torch)
  • yt-dlp          → YouTube ingest (pure-Python, ~5 MB)
  • fpdf2 / python-docx → PDF / DOCX export
  • Groq / Cerebras → AI summary (already used for refine / ask)

Public API:

    from transcribe import transcribe_file, export, ingest_url

    job = transcribe_file('/path/to/audio.mp3',
                           model='large-v3-turbo',
                           diarize=True,
                           on_progress=lambda phase, pct: ...)
    export(job, fmt='srt', out_path='/path/to/out.srt')

`job` is a TranscriptJob dataclass, JSON-serializable, suitable for the
history list, with `.segments` (start/end/text/speaker) and `.summary`.
"""
from .engine import TranscriptJob, TranscriptSegment, transcribe_file
from .exporters import export, SUPPORTED_FORMATS
from .youtube import ingest_url, is_youtube_url

__all__ = [
    'TranscriptJob',
    'TranscriptSegment',
    'transcribe_file',
    'export',
    'SUPPORTED_FORMATS',
    'ingest_url',
    'is_youtube_url',
]
