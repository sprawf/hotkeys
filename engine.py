import re
import threading
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# ── Provider metadata ────────────────────────────────────────────────────────

PROVIDER_KEYS   = ['local', 'groq', 'cerebras']
PROVIDER_LABELS = {
    'local':    'Qwen 2.5 1.5B  (Local · Free · GPU accelerated)',
    'groq':     'Groq  (Free tier · 70B · sub-1s · falls back to Cerebras)',
    'cerebras': 'Cerebras  (Free tier · ultra-fast · falls back to Groq)',
}
GROQ_MODELS     = ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant',
                   'meta-llama/llama-4-scout-17b-16e-instruct', 'openai/gpt-oss-120b']
CEREBRAS_MODELS = ['llama3.1-8b', 'gpt-oss-120b']

# ── Bundled API keys ─────────────────────────────────────────────────────────
# Loaded from _bundled_keys.py (gitignored, baked into installer builds).
# Falls back to empty strings in open-source / dev builds.
try:
    from _bundled_keys import CEREBRAS as _CB_KEY, GROQ as _GQ_KEY
    _BUNDLED = {'cerebras': _CB_KEY, 'groq': _GQ_KEY}
except ImportError:
    _BUNDLED: dict = {'cerebras': '', 'groq': ''}

_SSL_ERRS = ('SSL', 'CERTIFICATE', 'ConnectError', 'Connection error', 'certificate')


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def _ssl_retry(call_fn):
    """Call call_fn(verify=True); on SSL/connection error retry with verify=False."""
    try:
        return call_fn(verify=True)
    except Exception as e:
        if any(k in str(e) for k in _SSL_ERRS):
            logger.warning('SSL/connection error — retrying without verification (antivirus detected)')
            return call_fn(verify=False)
        raise


def _resolve_key(config: dict, provider: str) -> str:
    """Return user-configured key, falling back to bundled key."""
    return config.get('providers', {}).get(provider, {}).get('api_key', '') or _BUNDLED.get(provider, '')


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
                    logger.warning('SSL error — retrying without verification.')
                    self._ssl_bypass()
                    return _dl()
                raise

    @staticmethod
    def _ssl_bypass() -> None:
        import ssl, urllib3, requests
        ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore
        urllib3.disable_warnings()
        _orig = requests.Session.send
        def _no_ssl(self_r, req, **kw):
            kw['verify'] = False
            return _orig(self_r, req, **kw)
        requests.Session.send = _no_ssl  # type: ignore

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
                'Use Groq or Cerebras instead — both are free and much faster.'
            )
        self._loading = True
        logger.info('Loading local GGUF model…')
        model_path = self._find_model()
        from llama_cpp import Llama
        self._llm     = Llama(model_path=model_path, n_gpu_layers=-1, n_ctx=2048, verbose=False)
        self._ready   = True
        self._loading = False
        logger.info('Local model ready.')

    def refine(self, text: str, system_prompt: str) -> str:
        with self._lock:
            out = self._llm.create_chat_completion(  # type: ignore
                messages=[{'role': 'system', 'content': system_prompt},
                          {'role': 'user',   'content': text}],
                max_tokens=300, temperature=0.0,
            )
            return _clean(out['choices'][0]['message']['content'].strip())


# ── Cloud providers ──────────────────────────────────────────────────────────

class GroqProvider(Provider):
    def __init__(self, api_key: str, model: str = 'llama-3.1-8b-instant') -> None:
        self.api_key = api_key
        self.model   = model

    @property
    def name(self)  -> str:  return f'Groq ({self.model})'
    @property
    def ready(self) -> bool: return bool(self.api_key)

    def refine(self, text: str, system_prompt: str) -> str:
        import httpx
        from groq import Groq
        def _call(verify: bool = True) -> str:
            kw = {} if verify else {'http_client': httpx.Client(verify=False)}
            resp = Groq(api_key=self.api_key, **kw).chat.completions.create(
                model=self.model,
                messages=[{'role': 'system', 'content': system_prompt},
                          {'role': 'user',   'content': text}],
                max_tokens=1024,
            )
            return _clean(resp.choices[0].message.content)
        return _ssl_retry(_call)


class CerebrasProvider(Provider):
    def __init__(self, api_key: str, model: str = 'llama3.1-8b') -> None:
        self.api_key = api_key
        self.model   = model

    @property
    def name(self)  -> str:  return f'Cerebras ({self.model})'
    @property
    def ready(self) -> bool: return bool(self.api_key)

    def refine(self, text: str, system_prompt: str) -> str:
        import httpx
        from cerebras.cloud.sdk import Cerebras
        def _call(verify: bool = True) -> str:
            kw = {} if verify else {'http_client': httpx.Client(verify=False)}
            resp = Cerebras(api_key=self.api_key, **kw).chat.completions.create(
                model=self.model,
                messages=[{'role': 'system', 'content': system_prompt},
                          {'role': 'user',   'content': text}],
                max_tokens=1024,
            )
            return _clean(resp.choices[0].message.content)
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
    def load(self)  -> None: pass

    def refine(self, text: str, system_prompt: str) -> str:
        try:
            return self._primary.refine(text, system_prompt)
        except Exception as e:
            logger.warning(f'{self._primary.name} failed ({type(e).__name__}: {e!s:.80}) — falling back')
            if isinstance(self._fallback, LocalProvider):
                # Never block the inference thread loading a GGUF model
                if not self._fallback.ready:
                    raise RuntimeError('Local fallback model not loaded yet — try again shortly.')
            elif not self._fallback.ready:
                self._fallback.load()
            return self._fallback.refine(text, system_prompt)


# ── Factory ──────────────────────────────────────────────────────────────────

def build_provider(config: dict) -> Provider:
    active = config.get('active_provider', 'cerebras')

    cerebras_key = _resolve_key(config, 'cerebras')
    groq_key     = _resolve_key(config, 'groq')
    groq_model   = config.get('providers', {}).get('groq',     {}).get('model', GROQ_MODELS[0])
    cb_model     = config.get('providers', {}).get('cerebras', {}).get('model', CEREBRAS_MODELS[0])

    cerebras = CerebrasProvider(api_key=cerebras_key, model=cb_model)
    groq     = GroqProvider(api_key=groq_key,         model=groq_model)

    if active == 'cerebras':
        if cerebras.ready and groq.ready:
            return FallbackProvider(cerebras, groq)
        return cerebras if cerebras.ready else LocalProvider()

    if active == 'groq':
        if groq.ready and cerebras.ready:
            return FallbackProvider(groq, cerebras)
        return groq if groq.ready else LocalProvider()

    return LocalProvider()
