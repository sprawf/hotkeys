"""Notebooks — universal source ingestion.

Hands the input off to Microsoft's MarkItDown, which handles ~18 file types
(PDF, DOCX, PPTX, XLSX, HTML, EPUB, CSV, JSON, XML, audio, images, YouTube
URLs, even ZIP archives) and returns clean Markdown ready for embedding.

Outputs a Source dict with id, name, origin, kind, text, chunks. The chunks
are 512-character windows with 64-char overlap — small enough that retrieval
fetches a tight context, big enough that one chunk usually contains a
self-contained idea.
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


# ── SSL truststore injection ────────────────────────────────────────────────
# MarkItDown uses requests for URL fetching, which by default uses certifi's
# CA bundle. On machines with AV/corporate HTTPS scanning (AVG, Kaspersky,
# Bitdefender, etc), the MITM cert chains back to a local trust store that
# certifi doesn't know about, so every fetch fails with
# `CERTIFICATE_VERIFY_FAILED`. truststore patches Python's SSL to use the
# OS cert store instead, which DOES contain the AV root. Same fix Hotkeys
# uses in engine.py — see comments there for the longer story.
try:
    import truststore
    truststore.inject_into_ssl()
    logger.info('truststore: OS cert store injected for URL fetches')
except Exception as _ts_err:
    logger.debug(f'truststore unavailable, sticking with certifi: {_ts_err}')


# ── Chunking config ──────────────────────────────────────────────────────────
# Numbers chosen to balance:
#   • Retrieval quality (smaller = tighter relevance, larger = more context)
#   • Embedding cost (each chunk = one forward pass on the embedding model)
#   • LLM context budget (we pack ~8-12 chunks into a prompt)
_CHUNK_SIZE      = 1024   # characters per chunk (~250 tokens for English)
_CHUNK_OVERLAP   = 128    # characters that bleed between adjacent chunks
_MAX_CHUNK_BYTES = 8000   # hard ceiling, defensive

# Filetypes we know MarkItDown handles. Anything not here we still TRY
# but the user gets a clear "couldn't parse" if it fails.
SUPPORTED_EXTENSIONS = {
    '.pdf', '.docx', '.pptx', '.xlsx', '.xls', '.csv', '.tsv',
    '.html', '.htm', '.xml', '.json', '.yaml', '.yml',
    '.epub', '.md', '.txt', '.rtf',
    '.mp3', '.wav', '.m4a', '.flac', '.ogg',
    '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp',
    '.zip',
}


# ── Public entry point ───────────────────────────────────────────────────────

def ingest(input_ref: str | Path, *,
           name_hint: str = '',
           progress_cb: Callable[[str], None] | None = None) -> dict:
    """Ingest a file path, URL, or raw text into a Source dict.

    input_ref:
      • Path or str path → read from disk via MarkItDown
      • URL starting with http(s):// → fetch + parse via MarkItDown
      • Anything else as a str → treat as raw text input

    Returns: {id, name, origin, kind, text, chunks, created_at}
    Raises: RuntimeError on parse failure (caller renders a friendly pill).
    """
    _log = lambda msg: (progress_cb(msg) if progress_cb else logger.info(msg))
    src_id = str(uuid.uuid4())
    now = datetime.now().isoformat(timespec='seconds')

    # ── Decide what kind of input this is ────────────────────────────────────
    ref_str = str(input_ref)
    is_url = ref_str.startswith('http://') or ref_str.startswith('https://')
    is_path = (not is_url) and Path(ref_str).exists()
    is_raw_text = (not is_url) and (not is_path)

    if is_raw_text:
        # Raw text shortcut: skip MarkItDown entirely.
        text = ref_str
        name = name_hint or _first_line_as_name(text)
        kind = 'text'
        origin = '(pasted)'
    elif is_path and Path(ref_str).suffix.lower() in (
            '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.webp'):
        # Standalone images: MarkItDown doesn't OCR images by default
        # (needs tesseract or an LLM hook). We already have Groq vision
        # OCR plumbed for the scanned-PDF fallback — reuse it here so
        # users can drop a screenshot / receipt / whiteboard photo /
        # chart and have it ingested cleanly.
        p = Path(ref_str)
        text = _ocr_single_image_via_vision(p, log=_log)
        if not text:
            raise RuntimeError(
                f'No text could be extracted from "{p.name}". The image may '
                'be unreadable, or Groq vision OCR is unavailable.'
            )
        name = name_hint or p.name
        kind = 'image'
        origin = str(p.resolve())
    elif is_path and Path(ref_str).suffix.lower() in (
            '.mp3', '.wav', '.m4a', '.flac', '.ogg', '.opus'):
        # Audio files: route through Hotkeys' faster-whisper pipeline
        # which gives us substantially better transcription quality than
        # MarkItDown's default SpeechRecognition backend (which round-
        # tripped "Aurora Photonics" into "Bolero Photonics" in testing).
        p = Path(ref_str)
        text = _transcribe_audio_via_whisper(p, log=_log)
        if not text:
            raise RuntimeError(
                f'No speech could be transcribed from "{p.name}".'
            )
        name = name_hint or p.name
        kind = 'audio'
        origin = str(p.resolve())
    elif is_path and Path(ref_str).suffix.lower() == '.pdf':
        # Three-tier PDF strategy:
        #   1. pymupdf direct text extraction — fast, works for ~95% of PDFs
        #   2. Groq vision OCR per page — handles scanned / image-only PDFs
        #   3. MarkItDown — final fallback for weird edge cases
        p = Path(ref_str)
        text = ''
        try:
            import fitz  # PyMuPDF
            _log(f'Parsing PDF (pymupdf): {p.name}…')
            doc = fitz.open(str(p))
            try:
                parts = []
                images_only = True
                for page in doc:
                    page_text = page.get_text('text')
                    parts.append(page_text)
                    if page_text.strip():
                        images_only = False
                text = '\n\n'.join(parts).strip()
                # If pymupdf got nothing, this is a scanned / image-only
                # PDF. Render each page as a PNG and run Groq vision OCR.
                if not text and images_only:
                    _log(f'Image-only PDF detected; OCR-ing '
                         f'{doc.page_count} pages via Groq vision…')
                    text = _ocr_pdf_via_vision(doc, log=_log)
            finally:
                doc.close()
        except Exception as e:
            logger.warning(f'pymupdf failed ({e}); falling back to MarkItDown')
        if text:
            name = name_hint or p.name
            kind = 'pdf'
            origin = str(p.resolve())
        else:
            # Last-resort fallback to MarkItDown.
            from markitdown import MarkItDown
            import requests as _rq
            sess = _rq.Session()
            sess.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0)'})
            md = MarkItDown(requests_session=sess)
            try:
                result = md.convert(str(p))
                text = (result.text_content or '').strip()
            except Exception as e:
                raise RuntimeError(f'Could not parse "{p.name}": {e}') from e
            if not text:
                raise RuntimeError(
                    f'No text extracted from "{p.name}". The PDF may be '
                    'encrypted or contain unreadable images.'
                )
            name = name_hint or p.name
            kind = 'pdf'
            origin = str(p.resolve())
    else:
        from markitdown import MarkItDown
        # Many sites — Wikipedia, Reddit, news outlets, Cloudflare-fronted
        # pages — return 403 to the default `python-requests/X` UA. Give
        # MarkItDown a session that looks like a real browser.
        import requests as _rq
        sess = _rq.Session()
        sess.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/127.0.0.0 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
                      'image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        md = MarkItDown(requests_session=sess)
        _log(f'Parsing {ref_str[:80]}…')
        try:
            result = md.convert(ref_str)
            text = (result.text_content or '').strip()
        except Exception as e:
            raise RuntimeError(f'Could not parse "{ref_str[:60]}": {e}') from e
        if not text:
            raise RuntimeError(
                f'No text extracted from "{ref_str[:60]}". The file may be '
                'image-only, encrypted, or in an unsupported format.'
            )
        if is_url:
            name = name_hint or _name_from_url(ref_str)
            kind = 'url'
            origin = ref_str
        else:
            p = Path(ref_str)
            name = name_hint or p.name
            kind = _kind_from_extension(p.suffix)
            origin = str(p.resolve())

    # ── Normalise + chunk ────────────────────────────────────────────────────
    text = _normalise_whitespace(text)
    chunks = _chunk_text(text)
    _log(f'Ingested {len(text)} chars -> {len(chunks)} chunks')

    return {
        'id':         src_id,
        'name':       name[:200],
        'origin':     origin,
        'kind':       kind,
        'text':       text,
        'chunks':     chunks,
        'created_at': now,
    }


# ── Chunking ─────────────────────────────────────────────────────────────────

def _chunk_text(text: str) -> list[dict]:
    """Sliding-window character chunks with overlap, plus a soft preference
    for breaking at paragraph or sentence boundaries to keep chunks
    semantically clean.

    Returns: [{idx, text, start_char, end_char}, ...]
    """
    if not text:
        return []
    chunks = []
    pos = 0
    idx = 0
    n = len(text)
    while pos < n:
        end = min(pos + _CHUNK_SIZE, n)
        # Try to extend slightly to land on a paragraph/sentence boundary
        # — but never more than 20% past the target, otherwise we drift
        # too far and chunks become uneven.
        if end < n:
            scan_max = min(end + _CHUNK_SIZE // 5, n)
            window = text[end:scan_max]
            # Prefer paragraph break, then sentence end, then any whitespace.
            for needle in ('\n\n', '. ', '! ', '? ', '\n', ' '):
                m = window.find(needle)
                if m != -1:
                    end = end + m + len(needle)
                    break
        chunk_text = text[pos:end].strip()
        if chunk_text:
            chunks.append({
                'idx':        idx,
                'text':       chunk_text[:_MAX_CHUNK_BYTES],
                'start_char': pos,
                'end_char':   end,
            })
            idx += 1
        if end >= n:
            break
        # Slide window forward with overlap.
        pos = max(pos + 1, end - _CHUNK_OVERLAP)
    return chunks


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalise_whitespace(text: str) -> str:
    # Collapse Windows + Mac line endings to \n, squash 3+ blank lines to 2.
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Strip per-line trailing spaces.
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    return text.strip()


def _first_line_as_name(text: str) -> str:
    first = text.split('\n', 1)[0].strip()
    return first[:80] if first else 'Pasted text'


def _name_from_url(url: str) -> str:
    # Strip protocol and query string, keep host + path tail.
    s = re.sub(r'^https?://', '', url)
    s = s.split('?', 1)[0].split('#', 1)[0]
    return s[:120]


def _ocr_single_image_via_vision(path: Path, *, log) -> str:
    """OCR a standalone image (PNG/JPG/etc) via Groq vision. Mirrors
    _ocr_pdf_via_vision but for one image instead of a page sequence."""
    from PIL import Image
    try:
        import vision  as hk_vision
        import engine  as hk_engine
        import storage as hk_storage
    except Exception as e:
        logger.warning(f'Image OCR fallback unavailable: {e}')
        return ''
    try:
        cfg = hk_storage.load_config()
        keys = hk_engine._resolve_keys(cfg, 'groq')
    except Exception as e:
        logger.warning(f'Image OCR: no Groq keys: {e}')
        return ''
    if not keys:
        logger.warning('Image OCR: no Groq API keys configured')
        return ''
    try:
        # `with` so the file handle releases as soon as the Groq call
        # returns. hk_vision.extract_text reads the image into memory
        # (base64-encodes pixel data) so we don't need the file handle
        # alive after the call.
        with Image.open(str(path)) as img:
            log(f'OCR image via Groq vision: {path.name}')
            # Rate-limit rotation across bundled keys.
            for key in keys:
                try:
                    return hk_vision.extract_text(img, key,
                                                  hk_vision.DEFAULT_VISION_MODEL).strip()
                except Exception as e:
                    em = str(e).lower()
                    if 'rate limit' in em or '429' in em:
                        continue
                    raise
    except Exception as e:
        logger.warning(f'Image OCR failed: {e}')
    return ''


def _transcribe_audio_via_whisper(path: Path, *, log) -> str:
    """Transcribe an audio file via Hotkeys' faster-whisper pipeline.
    The bundled `small` model gives much higher accuracy than MarkItDown's
    default SpeechRecognition backend (which mis-heard our TTS test as
    'Bolero Photonics ... Doctor Alina ... Element 7' instead of
    'Aurora Photonics ... Dr. Elena Voss ... Lumin 7')."""
    try:
        # Hotkeys ships faster-whisper + the small model under
        # E:\Hotkeys\models\small (per the existing dist layout). In
        # a frozen build these land at <_MEIPASS>/models/<size> via
        # PyInstaller's data collection; walk up from ask_docs/ingest.py
        # to the app root (parent of ask_docs) which equals _MEIPASS.
        log(f'Transcribing audio via Whisper: {path.name}')
        from faster_whisper import WhisperModel
        models_dir = Path(__file__).resolve().parent.parent / 'models'
        # Prefer 'small' (better accuracy than 'base', still CPU-friendly).
        for model_name in ('small', 'base'):
            model_path = models_dir / model_name
            if model_path.exists():
                wm = WhisperModel(str(model_path), device='cpu',
                                  compute_type='int8')
                segments, _info = wm.transcribe(str(path), language=None,
                                                 vad_filter=True, beam_size=1)
                text = ' '.join(s.text.strip() for s in segments).strip()
                if text:
                    return text
                break
        logger.warning('Whisper transcription returned no text')
    except Exception as e:
        logger.warning(f'Whisper transcription failed: {e}')
    return ''


def _ocr_pdf_via_vision(doc, *, log) -> str:
    """Run Groq vision OCR over every page of a scanned PDF. Renders each
    page at ~2× scale (good text quality without ballooning request size),
    sends to the same Groq vision endpoint Hotkeys uses for the screenshot
    translate feature, and concatenates the per-page text.

    Falls back to empty string on any error — caller then tries MarkItDown.
    """
    import io
    from PIL import Image

    try:
        import vision  as hk_vision
        import engine  as hk_engine
        import storage as hk_storage
    except Exception as e:
        logger.warning(f'OCR fallback unavailable (vision import failed): {e}')
        return ''

    try:
        cfg = hk_storage.load_config()
        keys = hk_engine._resolve_keys(cfg, 'groq')
    except Exception as e:
        logger.warning(f'OCR fallback unavailable (no Groq keys): {e}')
        return ''
    if not keys:
        logger.warning('OCR fallback unavailable: no Groq API keys configured')
        return ''

    parts = []
    n_pages = doc.page_count
    for i, page in enumerate(doc):
        log(f'OCR page {i+1}/{n_pages}…')
        try:
            # Render at 2× DPI for OCR-friendly resolution. Higher scales
            # blow up request size with diminishing accuracy returns.
            pix = page.get_pixmap(matrix=__import__('fitz').Matrix(2, 2))
            # `with` so each page's PNG buffer is released as soon as the
            # API call returns rather than waiting for the loop body's GC.
            with Image.open(io.BytesIO(pix.tobytes('png'))) as img:
                page_text = ''
                for key in keys:
                    try:
                        page_text = hk_vision.extract_text(img, key,
                                                           hk_vision.DEFAULT_VISION_MODEL)
                        break
                    except Exception as e:
                        em = str(e).lower()
                        if 'rate limit' in em or '429' in em:
                            continue
                        raise
            if page_text:
                parts.append(page_text)
        except Exception as e:
            logger.warning(f'OCR page {i+1} failed: {e}')
            # Continue with remaining pages rather than abort the whole
            # ingest — partial coverage is better than nothing for a
            # 100-page scan with one broken page.
    return '\n\n'.join(parts).strip()


def _kind_from_extension(ext: str) -> str:
    ext = ext.lower()
    if ext in ('.pdf',):
        return 'pdf'
    if ext in ('.docx', '.doc', '.rtf'):
        return 'doc'
    if ext in ('.pptx', '.ppt'):
        return 'slides'
    if ext in ('.xlsx', '.xls', '.csv', '.tsv'):
        return 'spreadsheet'
    if ext in ('.html', '.htm', '.xml', '.json', '.yaml', '.yml'):
        return 'web'
    if ext in ('.epub',):
        return 'book'
    if ext in ('.md', '.txt'):
        return 'text'
    if ext in ('.mp3', '.wav', '.m4a', '.flac', '.ogg'):
        return 'audio'
    if ext in ('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp'):
        return 'image'
    if ext in ('.zip',):
        return 'archive'
    return 'file'
