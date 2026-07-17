"""Ask Docs — LLM provider abstraction.

Uses Hotkeys' existing engine.py to reuse the Cerebras → Groq → Qwen
fallback chain. Single source of truth for provider config, API keys,
retry logic, and offline reachability — when Hotkeys' engine improves,
Ask Docs inherits it for free.

Historical note: earlier revisions loaded Hotkeys' `engine.py` and
`storage.py` via `importlib.spec_from_file_location` under aliases
(`hk_storage`, `hk_engine`) to avoid a flat-import collision when this
package was standalone. Now that ask_docs is a proper subpackage
inside Hotkeys, our own modules live at `ask_docs.storage` /
`ask_docs.engine` and Hotkeys' live at bare `storage` / `engine`, so we
can just import the parent-app modules by name.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ── Lazy provider singleton ──────────────────────────────────────────────────

_provider = None


def _get_provider():
    """Build the provider chain once, on first use. Reads config from
    Hotkeys' standard storage so a user's API keys and provider choice
    transfer across both apps."""
    global _provider
    if _provider is not None:
        return _provider
    try:
        import storage as hk_storage
        import engine  as hk_engine
        cfg = hk_storage.load_config()

        # Notebooks bypasses Cerebras and builds a Groq-first chain
        # directly. Reasons:
        #   • Cerebras-tier accounts on the bundled keys 404 on every
        #     supported model name in practice (8b retired; 70b not
        #     provisioned for many accounts). The wasted call adds
        #     ~500ms per chat turn.
        #   • Notebooks does many small LLM calls per ingest (titling,
        #     summary, source guide, follow-ups). 500ms × 4 = 2s of
        #     wasted latency PER source added.
        #   • Hotkeys text refine still keeps the full Cerebras-first
        #     chain — we only override here for the Notebooks app.
        groq_keys = hk_engine._resolve_keys(cfg, 'groq')
        groq_model = (cfg.get('providers', {}).get('groq', {})
                      .get('model', hk_engine.GROQ_MODELS[0]))
        groq = hk_engine.GroqProvider(api_keys=groq_keys, model=groq_model)
        local = (hk_engine.LocalProvider()
                 if hk_engine.local_provider_available() else None)
        if local:
            _provider = hk_engine.FallbackProvider(groq, local)
        else:
            _provider = groq
        logger.info(f'Ask Docs LLM: Groq-first chain ready ({_provider.name})')
    except Exception as e:
        logger.exception(f'Ask Docs LLM: provider build failed: {e}')
        raise RuntimeError(f'Could not initialise AI provider: {e}') from e
    return _provider


# ── Public API ───────────────────────────────────────────────────────────────

def ask(prompt: str, system: str = '') -> str:
    """Single-shot Q&A. Returns the model's response as a string.

    Hotkeys' engine.refine(text, system_prompt) takes the user content
    as `text` and the system instructions as `system_prompt`. We adapt
    our (prompt, system) signature onto that.

    Retry-with-backoff for transient rate-limit errors: Groq's free tier
    bursts to a few RPM then 429s. The user-facing Notebooks app does
    many quick calls (ingest → auto-title + auto-summary + follow-ups,
    then a chat turn), which can blow that budget. A short backoff
    avoids surfacing the rate-limit error to the user — if we genuinely
    can't get through after the retries, the error propagates and the
    UI shows it.
    """
    import time as _t
    provider = _get_provider()
    last_err: Exception | None = None
    for attempt, delay in enumerate([0, 8, 20, 35]):
        if delay:
            logger.info(f'LLM: rate-limited, retrying in {delay}s '
                        f'(attempt {attempt + 1})')
            _t.sleep(delay)
        try:
            return provider.refine(prompt, system).strip()
        except RuntimeError as e:
            msg = str(e).lower()
            if 'rate limit' in msg or '429' in msg or 'too many requests' in msg:
                last_err = e
                continue
            raise
    if last_err is not None:
        raise last_err
    return ''


def stream(prompt: str, system: str = ''):
    """Streaming variant of ask().

    Yields incremental text chunks (str) as the model emits them. The
    full text is whatever you get by `''.join()`-ing the chunks. Only
    Groq is supported for streaming (it's the primary path); the local
    Qwen fallback isn't wired here because it goes through llama-cpp's
    completion API which would need a separate generator path.

    Falls back to one-shot ask() if the streaming HTTP call fails for
    any non-rate-limit reason (auth, server, malformed) so the user
    still gets an answer, just not progressively.
    """
    import json
    import time as _t
    import requests

    # Make sure the provider chain is built (resolves keys + model).
    provider = _get_provider()
    # Walk the chain to find the GroqProvider so we can re-use its keys + model.
    # Hotkeys' FallbackProvider stores primary/secondary as _primary/_fallback;
    # follow _primary down until we reach a non-fallback provider.
    cand = provider
    while hasattr(cand, '_primary'):
        cand = cand._primary
    if cand.__class__.__name__ != 'GroqProvider':
        # Non-Groq primary: just fall through to one-shot.
        yield ask(prompt, system)
        return

    url = cand._URL
    payload = {
        'model': cand.model,
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user',   'content': prompt}],
        'max_tokens': 1024,
        'stream': True,
    }
    last_err: Exception | None = None
    # Mirror the ask() retry-with-backoff for 429s.
    for attempt, delay in enumerate([0, 8, 20, 35]):
        if delay:
            logger.info(f'LLM stream: rate-limited, retrying in {delay}s')
            _t.sleep(delay)
        for key in cand.api_keys:
            try:
                headers = {'Authorization': f'Bearer {key}',
                           'Content-Type': 'application/json',
                           'Accept': 'text/event-stream',
                           # Disable gzip so iter_lines doesn't have to
                           # buffer the entire compressed response before
                           # decoding — without this, "stream=True" still
                           # delivers everything as one chunk after a
                           # 1-2s wait.
                           'Accept-Encoding': 'identity'}
                with requests.post(url, json=payload, headers=headers,
                                   stream=True, timeout=(10, 120)) as r:
                    if r.status_code == 429:
                        last_err = RuntimeError('rate limit')
                        break  # rotate to backoff loop
                    if r.status_code != 200:
                        raise RuntimeError(
                            f'Groq stream HTTP {r.status_code}: '
                            f'{r.text[:200]}')
                    any_chunk = False
                    # chunk_size=1 forces byte-level reads so iter_lines
                    # yields SSE events as they arrive instead of waiting
                    # for the default 512-byte buffer to fill — at Groq
                    # 70B's ~280 tok/s the whole response fits in 512B and
                    # streaming collapses into one chunk without this.
                    for line in r.iter_lines(chunk_size=1,
                                              decode_unicode=True):
                        if not line or not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            return
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        try:
                            delta = (obj['choices'][0]
                                     .get('delta', {})
                                     .get('content', ''))
                        except Exception:
                            delta = ''
                        if delta:
                            any_chunk = True
                            yield delta
                    if any_chunk:
                        return
                    # Empty stream — treat as failure, fall through.
                    last_err = RuntimeError('empty stream')
            except RuntimeError as e:
                msg = str(e).lower()
                if 'rate limit' in msg or '429' in msg:
                    last_err = e
                    continue  # rotate keys, then break to outer backoff
                # Non-rate-limit error — hard fail, fall back to one-shot.
                logger.warning(f'Stream failed ({e}); falling back to one-shot')
                yield ask(prompt, system)
                return
        else:
            # Inner for completed without break → all keys rotated, all
            # rate-limited. Outer loop applies backoff.
            continue
        # If we hit `break` (rate-limited and broke out), wait + retry.
    # All retries exhausted — final fallback to one-shot ask().
    logger.warning(f'Stream exhausted retries ({last_err}); one-shot fallback')
    yield ask(prompt, system)


def reset_provider() -> None:
    """Called from Notebooks when the user toggles which provider is
    active (matches Hotkeys' tray menu behaviour). Forces a rebuild on
    next ask()."""
    global _provider
    _provider = None
