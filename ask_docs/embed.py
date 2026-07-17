"""Notebooks — embedding model wrapper.

Wraps **all-MiniLM-L6-v2** (ONNX, ~88 MB float32) for CPU-only inference via
onnxruntime. Used to convert text chunks into dense vectors for RAG retrieval.

We switched from `nomic-embed-text-v1.5` (137M params, 130 MB) to MiniLM-L6
(22M params, 88 MB) because the larger model was too slow on CPU — 3 chunks/
second meant 5+ minute ingests for a Quran-sized PDF. MiniLM gives ~6× the
throughput at a small retrieval-quality cost (MTEB ~58 vs ~62), which for
our chunked-passage RAG use case is essentially invisible.

The model is loaded lazily on first use; subsequent calls reuse the session.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_DIR = Path(__file__).parent / 'models' / 'all-MiniLM-L6-v2'
# MiniLM was trained at 256 tokens; our 1024-char chunks fit comfortably.
_MAX_SEQ_LEN = 256
_EMBED_DIM   = 384


# ── Singleton session, lazily loaded ─────────────────────────────────────────

_session = None        # onnxruntime.InferenceSession
_tokenizer = None      # tokenizers.Tokenizer
_load_lock = threading.Lock()


def _ensure_loaded():
    """Load the ONNX model + tokenizer on first use. Subsequent calls no-op."""
    global _session, _tokenizer
    if _session is not None and _tokenizer is not None:
        return
    with _load_lock:
        if _session is not None and _tokenizer is not None:
            return
        logger.info('Loading all-MiniLM-L6-v2 ONNX model…')
        import onnxruntime as ort
        from tokenizers import Tokenizer
        model_path = _MODEL_DIR / 'model.onnx'
        tok_path   = _MODEL_DIR / 'tokenizer.json'
        if not model_path.exists():
            raise RuntimeError(
                f'Embedding model missing: {model_path}. '
                'Bundled model not found in expected location.'
            )
        # ORT_ENABLE_BASIC trims graph optimisation time; the network is
        # small enough that aggressive opts barely help inference.
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        _session = ort.InferenceSession(
            str(model_path), sess_options=opts,
            providers=['CPUExecutionProvider'],
        )
        _tokenizer = Tokenizer.from_file(str(tok_path))
        _tokenizer.enable_padding(pad_id=0, pad_token='[PAD]', length=_MAX_SEQ_LEN)
        _tokenizer.enable_truncation(max_length=_MAX_SEQ_LEN)
        logger.info(f'Embedding model ready (dim={_EMBED_DIM}, max_seq={_MAX_SEQ_LEN})')


# ── Public API ───────────────────────────────────────────────────────────────

def encode_documents(texts: list[str], batch_size: int = 64) -> np.ndarray:
    """Encode passages for the index. Returns (N, 384) float32 L2-normalised.

    MiniLM-L6 has no separate document/query prefix (unlike nomic) and its
    tiny size lets us comfortably batch 64 on CPU without RAM pressure.
    """
    return _encode(texts, batch_size)


def encode_query(text: str) -> np.ndarray:
    """Encode a single user question. Returns (384,) float32 L2-normalised."""
    return _encode([text], batch_size=1)[0]


def prewarm() -> None:
    """Load the model now so the first user-visible ingest doesn't pay the
    1-2s model-load cost. Called from the UI on startup in a background
    thread. Idempotent."""
    try:
        _ensure_loaded()
    except Exception as e:
        logger.warning(f'embed prewarm failed: {e}')


# ── Internals ────────────────────────────────────────────────────────────────

def _encode(texts: list[str], batch_size: int) -> np.ndarray:
    if not texts:
        return np.zeros((0, _EMBED_DIM), dtype=np.float32)
    _ensure_loaded()
    out = np.zeros((len(texts), _EMBED_DIM), dtype=np.float32)
    for batch_start in range(0, len(texts), batch_size):
        batch = texts[batch_start:batch_start + batch_size]
        encodings = _tokenizer.encode_batch(batch)
        input_ids      = np.array([e.ids for e in encodings],            dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)
        outputs = _session.run(None, {
            'input_ids':      input_ids,
            'attention_mask': attention_mask,
            'token_type_ids': token_type_ids,
        })
        # Mean-pool the last hidden state over the valid (non-pad) tokens.
        hidden = outputs[0]
        mask = attention_mask[..., None].astype(np.float32)
        summed = (hidden * mask).sum(axis=1)
        counts = mask.sum(axis=1).clip(min=1e-9)
        pooled = summed / counts
        # L2 normalise so cosine == dot product at retrieval time.
        norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
        out[batch_start:batch_start + len(batch)] = pooled / norms
    return out
