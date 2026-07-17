import re
import threading
import logging
import contextlib
from abc import ABC, abstractmethod

try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore

logger = logging.getLogger(__name__)

# ── OS certificate store injection ───────────────────────────────────────────
# Makes Python trust AV/corporate SSL certs (they live in the Windows cert
# store, not in certifi's bundle).  Must run before any SSL connection opens.
# Handles: AVG, Kaspersky, Bitdefender, ESET, Sophos, Norton, McAfee, corporate CAs.
try:
    import truststore
    truststore.inject_into_ssl()
    logger.debug('SSL: injected OS certificate store via truststore')
except Exception as _ts_err:
    logger.debug(f'truststore unavailable ({_ts_err}), using certifi bundle')

# ── Provider metadata ────────────────────────────────────────────────────────

PROVIDER_KEYS    = ['local', 'groq', 'cerebras', 'openai', 'anthropic', 'gemini', 'custom']
PROVIDER_LABELS  = {
    'local':     'Qwen 2.5 1.5B  (Local · Free · GPU accelerated)',
    'groq':      'Groq  (Free tier · 70B · sub-1s · falls back to Cerebras)',
    'cerebras':  'Cerebras  (Free tier · ultra-fast · falls back to Groq)',
    'openai':    'OpenAI  (GPT-4o · paid · bring your own key)',
    'anthropic': 'Anthropic Claude  (Claude 3.5 · paid · bring your own key)',
    'gemini':    'Google Gemini  (free tier available · bring your own key)',
    'custom':    'Custom  (any OpenAI-compatible endpoint)',
}
GROQ_MODELS      = ['openai/gpt-oss-120b', 'qwen/qwen3.6-27b',
                    'llama-3.1-8b-instant']
CEREBRAS_MODELS  = ['gpt-oss-120b', 'gemma-4-31b']
# Cerebras Developer-tier model roster as of 2026-07 (verified via
# `GET /v1/models` with our key):
#   • gpt-oss-120b        — primary, fast + reliable
#   • gemma-4-31b         — fallback
#   • zai-glm-4.7         — DEPRECATED 2026-08-17 (Cerebras notice)
# Retired earlier: llama3.1-8b, llama3.1-70b, llama-3.3-70b — all 404
# now. Original comment kept below for context. llama-3.3-70b is
# their current high-quality default and matches Groq's flagship in
# capability, so the fallback chain is even.
OPENAI_MODELS    = ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo', 'o1', 'o1-mini']
ANTHROPIC_MODELS = ['claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022',
                    'claude-3-opus-20240229', 'claude-3-haiku-20240307']
GEMINI_MODELS    = ['gemini-2.0-flash', 'gemini-1.5-pro', 'gemini-1.5-flash', 'gemini-1.0-pro']

# ── Bundled API keys ─────────────────────────────────────────────────────────
# Loaded from _bundled_keys.py (gitignored, baked into installer builds).
# Falls back to empty strings in open-source / dev builds.
try:
    from _bundled_keys import CEREBRAS as _CB_KEY, GROQ as _GQ_KEY
    try:
        from _bundled_keys import CEREBRAS_2 as _CB_KEY_2, GROQ_2 as _GQ_KEY_2
    except ImportError:
        _CB_KEY_2 = _GQ_KEY_2 = ''
    _BUNDLED = {
        'groq':       _GQ_KEY,
        'groq_2':     _GQ_KEY_2,
        'cerebras':   _CB_KEY,
        'cerebras_2': _CB_KEY_2,
    }
except ImportError as _bk_exc:
    # LOUD — missing bundled keys is a critical dist bug.
    import logging as _logging
    _logging.getLogger(__name__).warning(
        f'BUNDLED KEYS MISSING: {_bk_exc}. All cloud features (refine, ask, '
        f'vision, transcribe) will require user to add their own keys in '
        f'Settings. This is almost always a dist packaging bug — '
        f'_bundled_keys.py should be beside the .exe.'
    )
    _BUNDLED: dict = {'groq': '', 'groq_2': '', 'cerebras': '', 'cerebras_2': ''}

_SSL_ERRS = (
    # SSL / TLS layer (all AV vendors)
    'SSL', 'CERTIFICATE', 'certificate', 'TLS', 'tls',
    'VERIFY', 'verify', 'handshake', 'WRONG_VERSION',
    # httpx / httpcore connection wrappers
    'ConnectError', 'Connection error', 'RemoteDisconnected',
    'ConnectionReset', 'ConnectionRefused',
    # AVG kernel driver named-pipe interception
    'avgMon', 'Permission denied', '[Errno 13]',
    # Windows-specific socket / WinError codes
    'WinError 10054',   # connection reset by peer
    'WinError 10061',   # connection refused
    'WinError 995',     # I/O operation aborted (IOCP)
)

# ── Session-level SSL flag ────────────────────────────────────────────────────
_ssl_ok = True   # flipped to False once AV SSL interception is detected

def ssl_verify() -> bool:
    return _ssl_ok

def _mark_ssl_broken() -> None:
    global _ssl_ok
    if _ssl_ok:
        _ssl_ok = False
        logger.warning(
            'Antivirus SSL inspection detected, switching to verify=False. '
            'To fix permanently: add api.groq.com / api.cerebras.ai to your '
            'antivirus HTTPS scanning exclusions.'
        )


# Errors that indicate the user is OFFLINE rather than the API itself
# being broken, DNS, connection refused, network unreachable, plus the
# Windows-specific socket error codes.  Used by friendly_error_message().
_OFFLINE_HINTS = (
    'getaddrinfo failed',
    'Name or service not known',
    'nodename nor servname',
    'Temporary failure in name resolution',
    'No address associated with hostname',
    'Network is unreachable',
    'Could not resolve host',
    'Could not connect',
    'Connection refused',
    'Connection reset',
    'Connection aborted',
    'Connection timed out',
    'Failed to establish a new connection',
    '[Errno 11001]', '[Errno 11003]', '[Errno 11004]',
    '[Errno -2]', '[Errno -3]',
    'WinError 10050',   # network is down
    'WinError 10051',   # network unreachable
    'WinError 11001',   # host not found
    'WinError 11004',   # host not found, no DNS server response
    # httpx wrappers
    'ConnectError',
    'ReadTimeout',
    'ConnectTimeout',
)


def is_offline_error(exc: BaseException | str) -> bool:
    """True when the exception text suggests the user is OFFLINE, DNS
    failure, host unreachable, connection refused, or a network-level
    timeout. False for API-side errors (401, 429, 5xx, malformed JSON,
    etc.) which are signs of an upstream issue, not a missing connection.
    """
    msg = exc if isinstance(exc, str) else str(exc)
    msg_l = msg.lower()
    for h in _OFFLINE_HINTS:
        if h.lower() in msg_l:
            return True
    return False


def friendly_error_message(exc: BaseException | str, *, feature: str,
                           active_provider: str = '') -> str:
    """Translate a raw exception into a user-friendly one-liner. Picks the
    right framing depending on whether it looks like the user is offline
    or the API itself is misbehaving.

    Args:
        exc:             the exception (or its str()) to translate.
        feature:         short name for the action that failed
                         ("Refine", "Ask", "Chain", "OCR", "Explain").
        active_provider: 'local' / 'groq' / 'cerebras' / ..., when
                         present and the user is offline, the message
                         distinguishes "switch to Local" from "you're
                         already local but vision needs online".
    """
    msg = exc if isinstance(exc, str) else str(exc)

    if is_offline_error(msg):
        if active_provider == 'local' and feature in ('OCR', 'Explain'):
            # User is already on local but the FEATURE itself needs a
            # vision-capable model that only ships in cloud providers.
            return (f'{feature} needs an online provider, '
                    f'switch to Groq/Cerebras in Settings')
        return f'You appear to be offline, {feature} needs an internet connection'

    msg_l = msg.lower()
    if '429' in msg or 'rate' in msg_l or 'quota' in msg_l:
        return 'Daily limit reached, try again later or add your own API key in Settings'
    if 'api key' in msg_l or 'api_key' in msg_l or 'unauthorized' in msg_l or '401' in msg:
        return 'Invalid API key, check Settings'
    # Generic fallback, keep it short.
    return msg[:80]


def local_provider_available() -> bool:
    """True only when llama_cpp is importable (not excluded from dist build)."""
    try:
        import llama_cpp  # noqa: F401
        return True
    except ImportError:
        return False


def provider_available(key: str) -> bool:
    """True if the SDK for *key* is importable in this build.

    Dist excludes heavy optional SDKs (openai, anthropic, google-genai)
    to keep the zip small; the Settings dropdown should hide options
    that would raise "pip install X" on use — users can't pip in a
    frozen exe. groq + cerebras are always bundled.
    """
    if key == 'local':
        return local_provider_available()
    if key in ('groq', 'cerebras', 'custom'):
        return True
    _pkg = {'openai': 'openai', 'anthropic': 'anthropic', 'gemini': 'google.genai'}.get(key)
    if _pkg is None:
        return True
    try:
        __import__(_pkg)
        return True
    except Exception:
        return False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def _robust_post(url: str, payload: dict, headers: dict,
                 timeout: float = 30.0) -> dict:
    """POST JSON with three-level antivirus fallback.

    Level 1, httpx verify=True  : normal SSL.
                                   truststore (injected at import) makes Python
                                   trust AV/corporate CAs from the OS cert store,
                                   so AV SSL-MITM (AVG, Kaspersky, Bitdefender,
                                   ESET, Sophos, Norton, McAfee) is transparent.
    Level 2, httpx verify=False : any remaining SSL issue (edge-case AV configs,
                                   self-signed certs, truststore unavailable).
    Level 3, curl.exe           : AV blocks Python's socket layer entirely via a
                                   kernel driver (e.g. AVG avgMonFltProxy).
                                   curl.exe is a native Windows binary that uses
                                   Schannel, the same SSL stack Windows itself
                                   uses, so AV proxies it cleanly.

    Returns parsed JSON dict.  Raises RuntimeError with user-actionable message.
    """
    import json as _json
    import subprocess as _sub
    import os as _os

    # ── Fast reachability probe ─────────────────────────────────────────────
    # When offline (or the host is blocked at the network layer), httpx's
    # connect can hang well past the timeout — same class of bug as the
    # cloud-Whisper hang. A 0.5 s non-blocking TCP probe to the target
    # host:port catches "offline" in well under a second instead of waiting
    # 30 s × 2 verify levels × possible curl fallback before the chain
    # finally raises and falls back to Qwen.
    try:
        from urllib.parse import urlparse as _urlparse
        import socket as _sock
        _p = _urlparse(url)
        _host = _p.hostname
        _port = _p.port or (443 if _p.scheme == 'https' else 80)
        if _host:
            _s = _sock.create_connection((_host, _port), timeout=0.5)
            try: _s.close()
            except Exception: pass
    except Exception as _probe_err:
        # Skip the expensive httpx/curl fallback chain and surface the
        # offline-ness immediately so the caller can fall back to Qwen.
        raise RuntimeError(f'Cloud unreachable: {_probe_err}') from _probe_err

    last_exc: Exception | None = None

    # ── Levels 1 & 2: httpx ──────────────────────────────────────────────────
    if _httpx is not None:
        verify_levels = [False] if not _ssl_ok else [True, False]
        for verify in verify_levels:
            try:
                with _httpx.Client(timeout=timeout, verify=verify) as c:
                    r = c.post(url, json=payload, headers=headers)
                if r.status_code == 401:
                    raise RuntimeError('Invalid API key.')
                if r.status_code == 429:
                    raise RuntimeError('Rate limit reached, wait a moment and try again.')
                if r.status_code >= 400:
                    raise RuntimeError(f'API error {r.status_code}: {r.text[:120]}')
                if not verify and _ssl_ok:
                    _mark_ssl_broken()
                logger.debug(f'_robust_post: httpx verify={verify} succeeded')
                return r.json()
            except RuntimeError:
                raise                # API / auth errors, do not retry
            except Exception as e:
                last_exc = e
                es = str(e)
                if any(k in es for k in _SSL_ERRS):
                    logger.warning(
                        f'httpx (verify={verify}) blocked by AV/SSL '
                        f'({type(e).__name__}: {es[:80]}), trying next level'
                    )
                    continue
                if verify:
                    continue         # non-SSL error on verify=True: still try False
                break                # verify=False also failed, fall to curl

    # ── Level 3: curl.exe (Windows system binary, uses Schannel) ─────────────
    # Prefer C:\Windows\System32\curl.exe, guaranteed on Windows 10 1803+.
    # Fall back to PATH lookup in case the user has a different curl.
    system_curl = r'C:\Windows\System32\curl.exe'
    curl = system_curl if _os.path.isfile(system_curl) else None
    if curl is None:
        import shutil as _shutil
        curl = _shutil.which('curl')

    if curl:
        logger.warning(f'httpx blocked by AV, falling back to {curl}')
        try:
            args = [curl, '-s', '-S', '--max-time', str(int(timeout)),
                    '-k',           # skip cert verify (Schannel handles trust)
                    '--ssl-no-revoke',  # skip CRL check (may be blocked by AV too)
                    '-X', 'POST']
            for k, v in headers.items():
                args += ['-H', f'{k}: {v}']
            args += ['-d', _json.dumps(payload), url]
            r2 = _sub.run(args, capture_output=True, text=True,
                          timeout=timeout + 5)
            if r2.returncode != 0:
                raise RuntimeError(r2.stderr.strip()[:120])
            data = _json.loads(r2.stdout)
            if 'error' in data:
                err = data['error']
                msg = err.get('message', str(err))
                raise RuntimeError(f'API error: {msg[:120]}')
            logger.info('curl.exe fallback succeeded')
            return data
        except RuntimeError:
            raise
        except Exception as curl_exc:
            last_exc = curl_exc

    raise RuntimeError(
        f'Network blocked by antivirus (all methods failed).\n'
        f'Fix: open AVG → Settings → Shields → Web Shield → '
        f'HTTPS Scanning → add exceptions: api.groq.com, api.cerebras.ai\n'
        f'Last error: {str(last_exc)[:120]}'
    )


# Legacy wrapper kept for _OpenAICompatProvider (uses SDK, not _robust_post)
def _ssl_retry(call_fn):
    """SSL retry for SDK-based providers (OpenAI/Anthropic/Gemini/Custom)."""
    if not _ssl_ok:
        return call_fn(verify=False)
    try:
        return call_fn(verify=True)
    except Exception as e:
        if any(k in str(e) for k in _SSL_ERRS):
            _mark_ssl_broken()
            return call_fn(verify=False)
        raise


def _resolve_key(config: dict, provider: str) -> str:
    """Return best single key for a provider (kept for SDK-based providers)."""
    return config.get('providers', {}).get(provider, {}).get('api_key', '') or _BUNDLED.get(provider, '')


def _resolve_keys(config: dict, provider: str) -> list[str]:
    """Return deduplicated ordered list of API keys for a provider.

    Priority: user config key → user config secondary → bundled primary → bundled secondary.
    Empty strings and duplicates are removed.
    """
    pcfg = config.get('providers', {}).get(provider, {})
    candidates = [
        pcfg.get('api_key',   ''),
        pcfg.get('api_key_2', ''),
        _BUNDLED.get(provider,        ''),
        _BUNDLED.get(f'{provider}_2', ''),
    ]
    seen: set[str] = set()
    keys: list[str] = []
    for k in candidates:
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


# ── Abstract base ────────────────────────────────────────────────────────────

class Provider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    def load(self) -> None: pass

    @property
    def ready(self) -> bool: return True

    @abstractmethod
    def refine(self, text: str, system_prompt: str) -> str: ...


# ── Local GGUF provider ──────────────────────────────────────────────────────

class LocalProvider(Provider):
    MODEL_REPO = 'Qwen/Qwen2.5-1.5B-Instruct-GGUF'
    MODEL_FILE = 'qwen2.5-1.5b-instruct-q4_k_m.gguf'

    def __init__(self) -> None:
        self._ready   = False
        self._loading = False
        self._llm     = None
        self._lock    = threading.Lock()

    @property
    def name(self)  -> str:  return 'Qwen 2.5 1.5B (Local)'
    @property
    def ready(self) -> bool: return self._ready

    # Known local copies of the model (checked before HF cache / download)
    LOCAL_SEARCH_PATHS = [
        r'E:\PromptRefiner\dist\PromptRefiner\_internal\models\qwen2.5-1.5b-instruct-q4_k_m.gguf',
        r'E:\Hotkeys\models\qwen2.5-1.5b-instruct-q4_k_m.gguf',
    ]

    def _find_model(self) -> str:
        """Search order: bundled → known local paths → HF local cache → HF download."""
        import sys, os

        if hasattr(sys, '_MEIPASS'):
            bundled = os.path.join(sys._MEIPASS, 'models', self.MODEL_FILE)
            if os.path.isfile(bundled):
                logger.info(f'Using bundled model: {bundled}')
                return bundled

        for path in self.LOCAL_SEARCH_PATHS:
            if os.path.isfile(path):
                logger.info(f'Using local model: {path}')
                return path

        def _dl(local_only: bool = False):
            from huggingface_hub import hf_hub_download
            return hf_hub_download(repo_id=self.MODEL_REPO, filename=self.MODEL_FILE,
                                   local_files_only=local_only)
        try:
            return _dl(local_only=True)
        except Exception:
            try:
                return _dl()
            except Exception as e:
                err = str(e)
                if 'SSL' in err or 'certificate' in err.lower():
                    logger.warning('SSL error, retrying without verification (scoped).')
                    with self._ssl_bypass_scope():
                        return _dl()
                raise

    @staticmethod
    @contextlib.contextmanager
    def _ssl_bypass_scope():
        """Temporarily disable SSL verification for `requests` calls
        within this `with` block. Restores the original Session.send
        on exit so the rest of the app stays cert-verified.

        Why scoped instead of permanent: the previous implementation
        replaced `requests.Session.send` at the class level, which
        silently disabled TLS verification for every HTTPS call in the
        process (Groq, Cerebras, ask_docs LLM, future plugins) for the
        rest of the session. A real MITM (public Wi-Fi, hostile proxy)
        would have gone undetected. Now the bypass is contained to
        exactly the huggingface download that needed it."""
        import ssl, urllib3, requests
        urllib3.disable_warnings()
        _orig_send = requests.Session.send
        _orig_ssl_ctx = getattr(ssl, '_create_default_https_context', None)
        def _no_ssl(self_r, req, **kw):
            kw['verify'] = False
            return _orig_send(self_r, req, **kw)
        requests.Session.send = _no_ssl  # type: ignore
        ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore
        try:
            yield
        finally:
            try:
                requests.Session.send = _orig_send  # type: ignore
            except Exception:
                pass
            try:
                if _orig_ssl_ctx is not None:
                    ssl._create_default_https_context = _orig_ssl_ctx  # type: ignore
            except Exception:
                pass

    def load(self) -> None:
        if self._ready or self._loading:
            return
        import sys, os
        if hasattr(sys, '_MEIPASS'):
            _dll_dir = os.path.join(sys._MEIPASS, 'llama_cpp', 'lib')
            if os.path.isdir(_dll_dir) and hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(_dll_dir)
                logger.info(f'Registered DLL directory: {_dll_dir}')
            if hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(sys._MEIPASS)
            if sys._MEIPASS not in sys.path:
                sys.path.insert(0, sys._MEIPASS)
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            raise RuntimeError(
                'Local AI is not included in this build.\n'
                'Use Groq or Cerebras instead, both are free and much faster.'
            )
        self._loading = True
        logger.info('Loading local GGUF model…')
        try:
            model_path = self._find_model()
            from llama_cpp import Llama
            self._llm   = Llama(model_path=model_path, n_gpu_layers=-1, n_ctx=2048, verbose=False)
            self._ready = True
            logger.info('Local model ready.')
        except Exception:
            self._loading = False   # allow retry after a failed load
            raise
        else:
            self._loading = False

    def refine(self, text: str, system_prompt: str) -> str:
        if self._llm is None:
            # Without this guard `.create_chat_completion(...)` raises
            # AttributeError on a None target. When this provider is the
            # primary in a chain, the fallback wrapper interprets that as
            # generic failure and silently routes to a cloud provider —
            # the user picked "Local" for privacy, so leaking the prompt
            # is a real harm. Surface a clean error instead.
            if not self._loading:
                # Trigger a load so the next attempt can succeed.
                try: self.load()
                except Exception: pass
            raise RuntimeError(
                'Local model not loaded yet. Please wait a moment, '
                'or pick a different provider in Settings.')
        with self._lock:
            out = self._llm.create_chat_completion(  # type: ignore
                messages=[{'role': 'system', 'content': system_prompt},
                          {'role': 'user',   'content': text}],
                max_tokens=300, temperature=0.0,
            )
            return _clean(out['choices'][0]['message']['content'].strip())


# ── Cloud providers ──────────────────────────────────────────────────────────

_RATE_LIMIT_SIGNALS = ('rate limit', 'rate_limit', '429', 'quota', 'daily limit',
                       'monthly limit', 'tokens per', 'requests per')

def _is_rate_limit(err: Exception) -> bool:
    m = str(err).lower()
    return any(s in m for s in _RATE_LIMIT_SIGNALS)


class GroqProvider(Provider):
    _URL = 'https://api.groq.com/openai/v1/chat/completions'

    def __init__(self, api_keys: list[str], model: str = GROQ_MODELS[0]) -> None:
        self.api_keys = api_keys
        self.model    = model

    @property
    def name(self)  -> str:  return f'Groq ({self.model})'
    @property
    def ready(self) -> bool: return bool(self.api_keys)

    def refine(self, text: str, system_prompt: str) -> str:
        payload = {
            'model': self.model,
            'messages': [{'role': 'system', 'content': system_prompt},
                         {'role': 'user',   'content': text}],
            'max_tokens': 1024,
        }
        last_err: Exception | None = None
        for key in self.api_keys:
            try:
                headers = {'Authorization': f'Bearer {key}',
                           'Content-Type': 'application/json'}
                data = _robust_post(self._URL, payload, headers)
                return _clean(data['choices'][0]['message']['content'])
            except RuntimeError as e:
                if _is_rate_limit(e):
                    logger.warning(f'Groq key …{key[-6:]} rate-limited, rotating to next key')
                    last_err = e
                    continue
                raise   # auth errors, server errors, don't rotate
        raise last_err or RuntimeError('All Groq keys exhausted')


class CerebrasProvider(Provider):
    _URL = 'https://api.cerebras.ai/v1/chat/completions'

    def __init__(self, api_keys: list[str], model: str = CEREBRAS_MODELS[0]) -> None:
        self.api_keys = api_keys
        self.model    = model

    @property
    def name(self)  -> str:  return f'Cerebras ({self.model})'
    @property
    def ready(self) -> bool: return bool(self.api_keys)

    def refine(self, text: str, system_prompt: str) -> str:
        payload = {
            'model': self.model,
            'messages': [{'role': 'system', 'content': system_prompt},
                         {'role': 'user',   'content': text}],
            'max_tokens': 1024,
        }
        last_err: Exception | None = None
        for key in self.api_keys:
            try:
                headers = {'Authorization': f'Bearer {key}',
                           'Content-Type': 'application/json'}
                data = _robust_post(self._URL, payload, headers)
                return _clean(data['choices'][0]['message']['content'])
            except RuntimeError as e:
                if _is_rate_limit(e):
                    logger.warning(f'Cerebras key …{key[-6:]} rate-limited, rotating to next key')
                    last_err = e
                    continue
                raise
        raise last_err or RuntimeError('All Cerebras keys exhausted')


class _OpenAICompatProvider(Provider):
    """Shared base for providers that speak the OpenAI chat-completions API.

    Subclasses implement _client_kwargs(verify) to supply the OpenAI() constructor
    arguments; refine() and SSL-retry logic live here once.
    """

    model: str  # defined by each subclass __init__

    def _client_kwargs(self, verify: bool) -> dict:
        raise NotImplementedError

    def refine(self, text: str, system_prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError('openai package not installed, run: pip install openai')

        def _call(verify: bool = True) -> str:
            kw = self._client_kwargs(verify)
            # The OpenAI SDK defaults to a 600 s timeout and 2 internal
            # retries — blackholed network / dead Ollama host could leave
            # the "Thinking…" pill stuck for tens of minutes. Match the
            # Groq / Cerebras `_robust_post` budget instead.
            kw.setdefault('timeout', 30.0)
            kw.setdefault('max_retries', 0)
            resp = OpenAI(**kw).chat.completions.create(
                model=self.model,
                messages=[{'role': 'system', 'content': system_prompt},
                          {'role': 'user',   'content': text}],
                max_tokens=1024,
            )
            return _clean(resp.choices[0].message.content)

        return _ssl_retry(_call)

    # Module-cached client. Without this, every refine() call constructed
    # a fresh httpx.Client(verify=False) — connection pool + sockets +
    # thread executor — that the provider SDK held but never closed.
    # Per-call leak that grew with usage. Cached singleton matches what
    # the SDKs do internally for the default client.
    _SHARED_NO_VERIFY_CLIENT = None

    @classmethod
    def _no_verify_kw(cls) -> dict:
        """Extra kwargs to disable SSL verification (antivirus workaround).
        Reuses one process-wide httpx.Client; safe because httpx.Client is
        documented thread-safe and idempotent across providers."""
        if _httpx is None:
            return {}
        if cls._SHARED_NO_VERIFY_CLIENT is None:
            cls._SHARED_NO_VERIFY_CLIENT = _httpx.Client(verify=False)
        return {'http_client': cls._SHARED_NO_VERIFY_CLIENT}


class OpenAIProvider(_OpenAICompatProvider):
    def __init__(self, api_key: str, model: str = 'gpt-4o-mini') -> None:
        self.api_key = api_key
        self.model   = model

    @property
    def name(self)  -> str:  return f'OpenAI ({self.model})'
    @property
    def ready(self) -> bool: return bool(self.api_key)

    def _client_kwargs(self, verify: bool) -> dict:
        kw: dict = {'api_key': self.api_key}
        if not verify:
            kw.update(self._no_verify_kw())
        return kw


class GeminiProvider(_OpenAICompatProvider):
    """Google Gemini via its OpenAI-compatible REST endpoint, no extra SDK needed."""
    _BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/'

    def __init__(self, api_key: str, model: str = 'gemini-2.0-flash') -> None:
        self.api_key = api_key
        self.model   = model

    @property
    def name(self)  -> str:  return f'Gemini ({self.model})'
    @property
    def ready(self) -> bool: return bool(self.api_key)

    def _client_kwargs(self, verify: bool) -> dict:
        kw: dict = {'api_key': self.api_key, 'base_url': self._BASE_URL}
        if not verify:
            kw.update(self._no_verify_kw())
        return kw


class CustomProvider(_OpenAICompatProvider):
    """Any OpenAI-compatible endpoint: Ollama, LM Studio, OpenRouter, etc."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key  = api_key
        self.base_url = base_url.rstrip('/')
        self.model    = model

    @property
    def name(self)  -> str:  return f'Custom ({self.model or self.base_url})'
    @property
    def ready(self) -> bool: return bool(self.base_url and self.model)

    def _client_kwargs(self, verify: bool) -> dict:
        kw: dict = {
            'base_url': self.base_url,
            'api_key':  self.api_key or 'none',  # SDK requires a non-empty value
        }
        if not verify:
            kw.update(self._no_verify_kw())
        return kw


class AnthropicProvider(Provider):
    """Anthropic Claude, uses the anthropic SDK (different API shape from OpenAI)."""

    def __init__(self, api_key: str, model: str = 'claude-3-5-haiku-20241022') -> None:
        self.api_key = api_key
        self.model   = model

    @property
    def name(self)  -> str:  return f'Anthropic ({self.model})'
    @property
    def ready(self) -> bool: return bool(self.api_key)

    def refine(self, text: str, system_prompt: str) -> str:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError('anthropic package not installed, run: pip install anthropic')

        # Module-cached client for the same reason as _OpenAICompatProvider:
        # avoid leaking a fresh httpx.Client per call.
        if not hasattr(self.__class__, '_SHARED_NO_VERIFY_CLIENT_ANTHROPIC'):
            self.__class__._SHARED_NO_VERIFY_CLIENT_ANTHROPIC = None

        def _call(verify: bool = True) -> str:
            kw: dict = {'api_key': self.api_key}
            if not verify and _httpx:
                if self.__class__._SHARED_NO_VERIFY_CLIENT_ANTHROPIC is None:
                    self.__class__._SHARED_NO_VERIFY_CLIENT_ANTHROPIC = (
                        _httpx.Client(verify=False))
                kw['http_client'] = (
                    self.__class__._SHARED_NO_VERIFY_CLIENT_ANTHROPIC)
            # Bound the call to 30 s + 0 retries, matching Groq/Cerebras.
            # Without this Anthropic's SDK default (~600 s, 2 retries)
            # could leave the user staring at "Thinking…" for tens of
            # minutes when the network is blackholed.
            kw.setdefault('timeout', 30.0)
            kw.setdefault('max_retries', 0)
            resp = anthropic.Anthropic(**kw).messages.create(
                model=self.model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{'role': 'user', 'content': text}],
            )
            return _clean(resp.content[0].text)

        return _ssl_retry(_call)


# ── Fallback wrapper ─────────────────────────────────────────────────────────

class FallbackProvider(Provider):
    """Primary provider with transparent fallback to any secondary on failure."""

    def __init__(self, primary: Provider, fallback: Provider) -> None:
        self._primary  = primary
        self._fallback = fallback

    @property
    def name(self)  -> str:  return self._primary.name
    @property
    def ready(self) -> bool: return self._primary.ready or self._fallback.ready
    def load(self)  -> None:
        # Walk the chain (primary may itself be a FallbackProvider) and
        # kick off `load()` on any LocalProvider tier in a background
        # thread. Without this the bundled GGUF stays unloaded for the
        # whole session: main.py only triggers _load_model() when the
        # active provider is exactly LocalProvider, so a chained Local
        # tier (active='local' with bundled cloud keys) never loaded,
        # and the first refine() call AttributeError'd → silent fall to
        # Cerebras, breaking the "stays on-device" promise.
        import threading as _threading
        def _walk(p):
            if isinstance(p, FallbackProvider):
                _walk(p._primary)
                _walk(p._fallback)
                return
            if isinstance(p, LocalProvider) and not p.ready and not p._loading:
                _threading.Thread(target=p.load, daemon=True,
                                   name='fallback-local-load').start()
        try:
            _walk(self._primary)
            _walk(self._fallback)
        except Exception:
            pass

    def refine(self, text: str, system_prompt: str) -> str:
        try:
            return self._primary.refine(text, system_prompt)
        except Exception as e:
            logger.warning(f'{self._primary.name} failed ({type(e).__name__}: {e!s:.80}), falling back')
            if isinstance(self._fallback, LocalProvider):
                # Never block the inference thread loading a GGUF model
                if not self._fallback.ready:
                    raise RuntimeError('Local fallback model not loaded yet, try again shortly.')
            elif not self._fallback.ready:
                self._fallback.load()
            try:
                return self._fallback.refine(text, system_prompt)
            except Exception as e2:
                # Both providers failed, give a clean user-facing message
                if '429' in str(e2) or 'rate' in str(e2).lower() or 'quota' in str(e2).lower():
                    raise RuntimeError(
                        'Daily limit reached on both providers. '
                        'Try again later or add your own API key in Settings.'
                    )
                raise


# ── Factory ──────────────────────────────────────────────────────────────────

def _chain(providers: list[Provider]) -> Provider:
    """Nest a list of providers into a FallbackProvider chain (left = highest priority)."""
    if len(providers) == 1:
        return providers[0]
    return FallbackProvider(providers[0], _chain(providers[1:]))


def build_provider(config: dict) -> Provider:
    """Build the active provider with a full fallback chain.

    For cloud providers the chain is always:
        primary[key1, key2] → secondary[key1, key2] → Local Qwen
    Each cloud provider rotates through its keys on rate-limit before handing
    off to the next tier.  Local Qwen is always the last resort.
    """
    active = config.get('active_provider', 'cerebras')
    pcfg   = config.get('providers', {})

    groq_model = pcfg.get('groq',     {}).get('model', GROQ_MODELS[0])
    cb_model   = pcfg.get('cerebras', {}).get('model', CEREBRAS_MODELS[0])

    groq     = GroqProvider(api_keys=_resolve_keys(config, 'groq'),      model=groq_model)
    cerebras = CerebrasProvider(api_keys=_resolve_keys(config, 'cerebras'), model=cb_model)

    # ── Single-provider modes (no bundled fallback chain) ─────────────────────
    if active == 'openai':
        key   = _resolve_key(config, 'openai')
        model = pcfg.get('openai', {}).get('model', OPENAI_MODELS[0])
        return OpenAIProvider(api_key=key, model=model)

    if active == 'anthropic':
        key   = _resolve_key(config, 'anthropic')
        model = pcfg.get('anthropic', {}).get('model', ANTHROPIC_MODELS[0])
        return AnthropicProvider(api_key=key, model=model)

    if active == 'gemini':
        key   = _resolve_key(config, 'gemini')
        model = pcfg.get('gemini', {}).get('model', GEMINI_MODELS[0])
        return GeminiProvider(api_key=key, model=model)

    if active == 'custom':
        cpfg     = pcfg.get('custom', {})
        key      = cpfg.get('api_key', '')
        base_url = cpfg.get('base_url', '')
        model    = cpfg.get('model', '')
        return CustomProvider(api_key=key, base_url=base_url, model=model)

    # ── Cloud + local chain ───────────────────────────────────────────────────
    # Build ordered list: selected provider first, other cloud second, Local last.
    local = LocalProvider() if local_provider_available() else None

    if active == 'groq':
        tiers: list[Provider] = []
        if groq.ready:     tiers.append(groq)
        if cerebras.ready: tiers.append(cerebras)
        if local:          tiers.append(local)
        return _chain(tiers) if tiers else groq   # groq shown as not-ready if nothing available

    if active == 'cerebras':
        tiers = []
        if cerebras.ready: tiers.append(cerebras)
        if groq.ready:     tiers.append(groq)
        if local:          tiers.append(local)
        return _chain(tiers) if tiers else cerebras

    # 'local' selected, local first, cloud as silent backup
    if local:
        tiers = [local]
        if cerebras.ready: tiers.append(cerebras)
        if groq.ready:     tiers.append(groq)
        return _chain(tiers)
    if cerebras.ready: return FallbackProvider(cerebras, groq) if groq.ready else cerebras
    return groq
