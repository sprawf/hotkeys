"""Image-to-text extraction via Groq vision API."""
import base64
import io
import logging

logger = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = 'meta-llama/llama-4-scout-17b-16e-instruct'
LONG_TEXT_WARN       = 2000   # chars — prompt user if result is very long
_MAX_PX              = 1280   # max image dimension sent to API
_TIMEOUT             = 15.0   # seconds per request

_EXTRACT_PROMPT = (
    'Extract all text from this image exactly as it appears. '
    'Do not translate, summarize, reformat, or add any commentary. '
    'Return only the extracted text and nothing else. '
    'Preserve the original language and formatting as closely as possible.'
)


# ── Clipboard helper ──────────────────────────────────────────────────────────

def get_clipboard_image():
    """Read an image from the clipboard.

    Returns
    -------
    (PIL.Image, None)
        Image found and decoded successfully.
    (None, None)
        Clipboard contains no image data — expected, let normal paste proceed.
    (None, str)
        Something went wrong trying to read the clipboard — show the string
        to the user so they know what to fix.

    Tries every known Windows clipboard image format in order:
      1. PIL ImageGrab.grabclipboard()  — CF_DIB / CF_BITMAP (classic PrtSc)
      2. win32clipboard PNG format       — Win11 Snipping Tool (Win+Shift+S)
      3. win32clipboard CF_DIB           — our built-in PrtSc screenshot tool
      4. win32clipboard CF_BITMAP        — older apps
      5. File list on clipboard          — image file copied from Explorer
    """
    import io
    import struct
    from PIL import Image

    file_list   = None   # populated if grabclipboard returns a path list
    last_syserr = None   # most recent system-level error string (if any)

    # ── Method 1: PIL ImageGrab ───────────────────────────────────────────────
    try:
        from PIL import ImageGrab
        data = ImageGrab.grabclipboard()
        if data is not None:
            if hasattr(data, 'tobytes'):        # PIL Image directly
                return data.convert('RGB'), None
            if isinstance(data, list):
                file_list = data               # defer to Method 5
    except Exception as e:
        logger.debug(f'ImageGrab.grabclipboard: {e}')
        last_syserr = str(e)

    # ── Methods 2-4: win32clipboard ───────────────────────────────────────────
    try:
        import win32clipboard
        import win32con

        try:
            win32clipboard.OpenClipboard()
        except Exception as e:
            # Can't open clipboard at all — likely blocked by AV or another app
            last_syserr = str(e)
            err_lower   = last_syserr.lower()
            if 'access' in err_lower or '5' in last_syserr:
                return None, ('Clipboard access denied — antivirus (e.g. AVG Clipboard Shield) '
                              'may be blocking it. Try disabling clipboard protection temporarily.')
            return None, f'Could not open clipboard: {last_syserr}'

        try:
            # Method 2: PNG (Win11 Snipping Tool)
            png_fmt = win32clipboard.RegisterClipboardFormat('PNG')
            if win32clipboard.IsClipboardFormatAvailable(png_fmt):
                try:
                    raw = win32clipboard.GetClipboardData(png_fmt)
                    return Image.open(io.BytesIO(raw)).convert('RGB'), None
                except Exception as e:
                    logger.debug(f'PNG clipboard decode: {e}')
                    last_syserr = str(e)

            # Method 3: CF_DIB (our built-in PrtSc tool + most Windows apps)
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB):
                try:
                    raw = win32clipboard.GetClipboardData(win32con.CF_DIB)
                    # Read actual header size so we handle V4/V5 headers correctly
                    hdr_size  = struct.unpack_from('<I', raw, 0)[0]
                    # Count colour-table entries for indexed bitmaps
                    bit_count = struct.unpack_from('<H', raw, 14)[0]
                    clr_used  = struct.unpack_from('<I', raw, 32)[0] if len(raw) > 36 else 0
                    if bit_count <= 8:
                        clr_entries = clr_used if clr_used else (1 << bit_count)
                    else:
                        clr_entries = clr_used  # 0 for 24/32-bit
                    px_offset = 14 + hdr_size + clr_entries * 4
                    fsize     = 14 + len(raw)
                    bmp_hdr   = struct.pack('<2sIHHI', b'BM', fsize, 0, 0, px_offset)
                    return Image.open(io.BytesIO(bmp_hdr + raw)).convert('RGB'), None
                except Exception as e:
                    logger.debug(f'CF_DIB decode: {e}')
                    last_syserr = str(e)

            # Method 4: CF_BITMAP (older apps — convert via PIL)
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_BITMAP):
                try:
                    # PIL's grabclipboard handles CF_BITMAP; if we're here it failed,
                    # so log and skip rather than retry.
                    logger.debug('CF_BITMAP present but grabclipboard already failed')
                except Exception:
                    pass

        finally:
            win32clipboard.CloseClipboard()

    except ImportError:
        logger.debug('win32clipboard not available')
    except Exception as e:
        logger.debug(f'win32clipboard: {e}')
        last_syserr = str(e)

    # ── Method 5: file list (image file copied from Explorer) ────────────────
    if file_list:
        loaded_any = False
        for path in file_list:
            try:
                return Image.open(path).convert('RGB'), None
            except Exception:
                loaded_any = True   # path existed but wasn't a readable image
        if loaded_any:
            return None, 'Clipboard contains a file but it could not be opened as an image.'

    # ── Nothing found ─────────────────────────────────────────────────────────
    if last_syserr:
        # Clipboard was accessible but decoding failed — surface the reason
        return None, f'Image found on clipboard but could not be decoded: {last_syserr}'
    return None, None   # genuinely no image — expected, let normal paste proceed


# ── Image helpers ─────────────────────────────────────────────────────────────

def _resize(img):
    """Downsample if either dimension exceeds _MAX_PX, preserving aspect ratio."""
    from PIL import Image
    w, h = img.size
    if w <= _MAX_PX and h <= _MAX_PX:
        return img
    scale = _MAX_PX / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _to_base64(img) -> str:
    """Encode a PIL Image as a JPEG base64 string."""
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return base64.b64encode(buf.getvalue()).decode()


# ── Main extraction function ──────────────────────────────────────────────────

def extract_text(img, api_key: str, model: str = DEFAULT_VISION_MODEL) -> str:
    """Extract text from a PIL Image using the Groq vision API.

    Parameters
    ----------
    img      : PIL Image
    api_key  : Groq API key (user-configured or bundled)
    model    : vision-capable model name

    Returns
    -------
    Extracted text as a string.

    Raises
    ------
    RuntimeError with a user-friendly message on any failure.
    """
    import httpx

    if not api_key:
        raise RuntimeError(
            'No Groq API key configured.\n'
            'Add your key in Settings → Providers → Groq.'
        )

    img = _resize(img)
    b64 = _to_base64(img)

    payload = {
        'model': model,
        'messages': [{
            'role':    'user',
            'content': [
                {'type': 'text',      'text': _EXTRACT_PROMPT},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
            ],
        }],
        'max_tokens':  4096,
        'temperature': 0.0,
    }
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type':  'application/json',
    }
    url = 'https://api.groq.com/openai/v1/chat/completions'

    from engine import _robust_post

    try:
        resp_data = _robust_post(url, payload, headers, timeout=_TIMEOUT)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f'Network error: {exc}') from exc

    try:
        text = resp_data['choices'][0]['message']['content'].strip()
    except Exception as exc:
        raise RuntimeError(f'Unexpected API response: {exc}') from exc

    logger.info(f'vision: extracted {len(text)} chars from image via {model}')
    return text
