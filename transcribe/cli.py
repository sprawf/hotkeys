"""Standalone CLI for the transcription pipeline, for proving it works
end-to-end before wiring into the app.

Usage examples:

    # Local file, base model, no diarization, TXT + SRT output
    python -m transcribe.cli E:/clip.mp3 --model base --out E:/out

    # With diarization
    python -m transcribe.cli E:/clip.mp3 --diarize --out E:/out

    # YouTube URL
    python -m transcribe.cli "https://youtu.be/dQw4w9WgXcQ" --out E:/out

    # All 6 formats
    python -m transcribe.cli E:/clip.mp3 --formats txt,srt,vtt,csv,docx,pdf --out E:/out

    # With AI summary (uses the same Groq/Cerebras provider as the app)
    python -m transcribe.cli E:/clip.mp3 --summary --out E:/out

Phase progress is printed to stderr so stdout stays clean for piping the
JSON result.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


def _set_console_utf8() -> None:
    """Make Windows consoles emit UTF-8 so progress lines with arrows / emoji
    don't blow up on cp1252."""
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


def _summarize(job) -> str:
    """Send the joined transcript through engine.py's configured provider
    (Groq / Cerebras / local) for a TL;DR summary. Best-effort, returns
    '' on any failure so the rest of the pipeline still ships."""
    try:
        # Lazy import so a --help with no deps still works.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from engine import build_provider                # type: ignore
        from storage import load_config                  # type: ignore
        full_text = '\n'.join(s['text'] for s in job.segments)
        if not full_text.strip():
            return ''
        provider = build_provider(load_config())
        provider.load()
        system = (
            'You are a concise meeting-summary assistant. Output 3-6 bullet '
            'points capturing the key topics, decisions, and action items '
            'from the transcript. Skip filler. Return plain text only.'
        )
        return (provider.refine(full_text, system) or '').strip()
    except Exception as e:
        print(f'  summary skipped: {type(e).__name__}: {e}', file=sys.stderr)
        return ''


def main(argv: list[str] | None = None) -> int:
    _set_console_utf8()
    ap = argparse.ArgumentParser(description='TurboScribe-style transcription pipeline.')
    ap.add_argument('source', help='Path to audio/video file or YouTube URL')
    ap.add_argument('--model', default='base',
                    help='Whisper model, base | small | large-v3-turbo (default: base)')
    ap.add_argument('--diarize', action='store_true',
                    help='Run pyannote.audio speaker diarization (slow on CPU)')
    ap.add_argument('--language', default=None,
                    help='Force language code (e.g. en, es), default: auto-detect')
    ap.add_argument('--min-speakers', type=int, default=None)
    ap.add_argument('--max-speakers', type=int, default=None)
    ap.add_argument('--summary', action='store_true',
                    help='Generate an AI summary via the configured provider')
    ap.add_argument('--formats', default='txt,srt',
                    help='Comma-separated export formats from txt,srt,vtt,csv,docx,pdf')
    ap.add_argument('--out', default='.', help='Output directory (default: cwd)')
    ap.add_argument('--json', action='store_true',
                    help='Print the full TranscriptJob as JSON on stdout')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='[%(asctime)s] %(name)s %(levelname)s: %(message)s',
        datefmt='%H:%M:%S',
        stream=sys.stderr,
    )

    # Lazy import so a --help with no deps still works.
    from transcribe import (
        ingest_url, is_youtube_url, transcribe_file, export, SUPPORTED_FORMATS,
    )

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    fmts = [f.strip().lower() for f in args.formats.split(',') if f.strip()]
    bad  = [f for f in fmts if f not in SUPPORTED_FORMATS]
    if bad:
        print(f'unsupported formats: {bad} (valid: {SUPPORTED_FORMATS})', file=sys.stderr)
        return 2

    # ── 1. Resolve source (file vs YouTube) ──────────────────────────────────
    source = args.source.strip().strip('"').strip("'")
    if is_youtube_url(source):
        print(f'YouTube URL detected, downloading audio…', file=sys.stderr)
        def _yt_prog(p: float):
            print(f'  download: {p*100:5.1f}%', end='\r', file=sys.stderr)
        try:
            local_path = ingest_url(source, out_dir / '_yt_cache',
                                    on_progress=_yt_prog,
                                    on_log=lambda m: print(f'  {m}', file=sys.stderr))
        except Exception as e:
            print(f'\nYouTube download failed: {type(e).__name__}: {e}', file=sys.stderr)
            return 3
        print('', file=sys.stderr)
    else:
        local_path = Path(source).expanduser().resolve()
        if not local_path.exists():
            print(f'file not found: {local_path}', file=sys.stderr)
            return 4

    print(f'Source: {local_path}', file=sys.stderr)

    # ── 2. Transcribe ────────────────────────────────────────────────────────
    last_phase = ['']
    def _prog(phase: str, pct: float):
        if phase != last_phase[0]:
            print('', file=sys.stderr)
            last_phase[0] = phase
        print(f'  {phase:>10s}: {pct*100:5.1f}%', end='\r', file=sys.stderr)

    t0 = time.time()
    try:
        job = transcribe_file(
            local_path,
            model=args.model,
            diarize=args.diarize,
            language=args.language,
            min_speakers=args.min_speakers,
            max_speakers=args.max_speakers,
            on_progress=_prog,
            on_log=lambda m: print(f'  {m}', file=sys.stderr),
        )
    except Exception as e:
        print(f'\ntranscription failed: {type(e).__name__}: {e}', file=sys.stderr)
        if args.verbose:
            import traceback; traceback.print_exc(file=sys.stderr)
        return 5

    elapsed = time.time() - t0
    rtf = elapsed / max(job.duration, 0.1)
    print(f'\n\nDone in {elapsed:.1f}s ({rtf:.2f}× realtime, {len(job.segments)} segments, '
          f'{len({s["speaker"] for s in job.segments if s["speaker"]})} speakers).',
          file=sys.stderr)

    # ── 3. Summary (optional) ────────────────────────────────────────────────
    if args.summary:
        print('Generating AI summary…', file=sys.stderr)
        job.summary = _summarize(job)
        if job.summary:
            print(f'  summary: {len(job.summary)} chars', file=sys.stderr)
            print('--- summary ---', file=sys.stderr)
            print(job.summary, file=sys.stderr)
            print('---------------', file=sys.stderr)

    # ── 4. Export ────────────────────────────────────────────────────────────
    stem = Path(job.source).stem
    written: list[Path] = []
    for fmt in fmts:
        try:
            p = export(job, fmt, out_dir / f'{stem}.{fmt}')
            written.append(p)
            print(f'  wrote {fmt:>4s}: {p}', file=sys.stderr)
        except Exception as e:
            print(f'  {fmt} export failed: {type(e).__name__}: {e}', file=sys.stderr)

    # ── 5. Optional JSON dump ────────────────────────────────────────────────
    if args.json:
        print(json.dumps(job.to_dict(), ensure_ascii=False, indent=2))

    print('', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
