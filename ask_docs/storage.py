"""Notebooks — persistence layer.

Each notebook is a folder under <data_dir>/notebooks/<id>/ containing:
  meta.json           — name, persona prompt, timestamps
  sources/<sid>.json  — one file per source (raw text + chunks + embeddings)
  chats/<cid>.json    — chat sessions (messages, citations)
  artifacts/<a>.md    — generated artifacts (Summary/FAQ/StudyGuide/Timeline)
  vectors.sqlite      — sqlite vector index across all sources in this notebook

Layout is folder-per-notebook so users can copy / share / back up individual
notebooks without exporting the whole app state. The vectors.sqlite lives at
notebook level so retrieval queries don't have to load every notebook's
embeddings into memory.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── App data directory ───────────────────────────────────────────────────────

def data_dir() -> Path:
    """Top-level data folder. Hotkeys-compatible — when embedded into the
    Hotkeys app later, this will point at the same Roaming\\Hotkeys\\ask_docs
    folder, so a user upgrading from standalone keeps their data.

    Migration: pre-rename data lived under Roaming\\Hotkeys\\notebooks. If
    that directory exists and the new one doesn't, rename it so existing
    doc sets remain accessible after the product rename.
    """
    base = Path(os.environ.get('APPDATA', Path.home())) / 'Hotkeys'
    new_root = base / 'ask_docs'
    old_root = base / 'notebooks'
    if old_root.exists() and not new_root.exists():
        try:
            old_root.rename(new_root)
            logger.info(f'Migrated data dir: {old_root} -> {new_root}')
        except Exception as e:
            logger.warning(f'data_dir migration failed: {e}; '
                           f'falling back to old path')
            old_root.mkdir(parents=True, exist_ok=True)
            return old_root
    new_root.mkdir(parents=True, exist_ok=True)
    return new_root


# ── Notebook CRUD ────────────────────────────────────────────────────────────

def list_notebooks() -> list[dict]:
    """Return every notebook's meta.json, newest first."""
    notebooks = []
    root = data_dir()
    for nb_dir in root.iterdir():
        if not nb_dir.is_dir():
            continue
        meta_p = nb_dir / 'meta.json'
        if not meta_p.exists():
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding='utf-8'))
            meta['_dir'] = str(nb_dir)
            notebooks.append(meta)
        except Exception as e:
            logger.warning(f'list_notebooks: failed to read {meta_p}: {e}')
    notebooks.sort(key=lambda n: n.get('updated_at', ''), reverse=True)
    return notebooks


def create_notebook(name: str, persona: str = '') -> dict:
    """Create a new notebook folder with empty subfolders + meta."""
    nb_id = str(uuid.uuid4())
    nb_dir = data_dir() / nb_id
    (nb_dir / 'sources').mkdir(parents=True, exist_ok=True)
    (nb_dir / 'chats').mkdir(parents=True, exist_ok=True)
    (nb_dir / 'artifacts').mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec='seconds')
    meta = {
        'id':         nb_id,
        'name':       name or 'Untitled doc set',
        'persona':    persona or _DEFAULT_PERSONA,
        'created_at': now,
        'updated_at': now,
    }
    save_meta(nb_id, meta)
    logger.info(f'Doc set created: {meta["name"]!r} ({nb_id})')
    return meta


def get_notebook(nb_id: str) -> dict | None:
    p = data_dir() / nb_id / 'meta.json'
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def save_meta(nb_id: str, meta: dict) -> None:
    meta['updated_at'] = datetime.now().isoformat(timespec='seconds')
    p = data_dir() / nb_id / 'meta.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')


def delete_notebook(nb_id: str) -> None:
    """Hard delete — wipes the notebook folder and everything inside."""
    nb_dir = data_dir() / nb_id
    if nb_dir.exists():
        shutil.rmtree(nb_dir, ignore_errors=True)
        logger.info(f'Doc set deleted: {nb_id}')


# ── Source CRUD ──────────────────────────────────────────────────────────────

def list_sources(nb_id: str) -> list[dict]:
    src_dir = data_dir() / nb_id / 'sources'
    if not src_dir.exists():
        return []
    out = []
    for p in src_dir.glob('*.json'):
        try:
            s = json.loads(p.read_text(encoding='utf-8'))
            # Don't ship the entire text / embeddings to the UI list view —
            # the panel only needs name + summary stats.
            out.append({
                'id':           s.get('id'),
                'name':         s.get('name', 'Untitled source'),
                'origin':       s.get('origin', ''),
                'kind':         s.get('kind', 'text'),
                'created_at':   s.get('created_at', ''),
                'chunk_count':  len(s.get('chunks', [])),
                'char_count':   len(s.get('text', '')),
            })
        except Exception as e:
            logger.warning(f'list_sources: failed to read {p}: {e}')
    out.sort(key=lambda s: s.get('created_at', ''))
    return out


def get_source(nb_id: str, source_id: str) -> dict | None:
    p = data_dir() / nb_id / 'sources' / f'{source_id}.json'
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def save_source(nb_id: str, source: dict) -> None:
    """source must have keys: id, name, origin, kind, text, chunks (list of
    {idx, text, start_char, end_char}), and optionally embeddings."""
    src_dir = data_dir() / nb_id / 'sources'
    src_dir.mkdir(parents=True, exist_ok=True)
    p = src_dir / f'{source["id"]}.json'
    p.write_text(json.dumps(source, indent=2, ensure_ascii=False), encoding='utf-8')
    # Update notebook updated_at so the list view sorts naturally.
    meta = get_notebook(nb_id)
    if meta is not None:
        save_meta(nb_id, meta)


def delete_source(nb_id: str, source_id: str) -> None:
    p = data_dir() / nb_id / 'sources' / f'{source_id}.json'
    if p.exists():
        p.unlink()
    # Also remove from the vector index so retrieval doesn't return ghosts.
    try:
        with vector_index(nb_id) as idx:
            idx.delete_source(source_id)
    except Exception as e:
        logger.warning(f'delete_source: vector index cleanup failed: {e}')


# ── Chat sessions ────────────────────────────────────────────────────────────

def list_chats(nb_id: str) -> list[dict]:
    chat_dir = data_dir() / nb_id / 'chats'
    if not chat_dir.exists():
        return []
    out = []
    for p in chat_dir.glob('*.json'):
        try:
            c = json.loads(p.read_text(encoding='utf-8'))
            out.append({
                'id':            c.get('id'),
                'title':         c.get('title', 'New chat'),
                'created_at':    c.get('created_at', ''),
                'message_count': len(c.get('messages', [])),
            })
        except Exception:
            pass
    out.sort(key=lambda c: c.get('created_at', ''), reverse=True)
    return out


def get_chat(nb_id: str, chat_id: str) -> dict | None:
    p = data_dir() / nb_id / 'chats' / f'{chat_id}.json'
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def save_chat(nb_id: str, chat: dict) -> None:
    chat_dir = data_dir() / nb_id / 'chats'
    chat_dir.mkdir(parents=True, exist_ok=True)
    p = chat_dir / f'{chat["id"]}.json'
    p.write_text(json.dumps(chat, indent=2, ensure_ascii=False), encoding='utf-8')


def delete_chat(nb_id: str, chat_id: str) -> None:
    p = data_dir() / nb_id / 'chats' / f'{chat_id}.json'
    if p.exists():
        p.unlink()


# ── Artifacts (generated outputs in Studio panel) ────────────────────────────

def save_artifact(nb_id: str, kind: str, markdown: str) -> None:
    """kind ∈ {summary, faq, study_guide, timeline}. One file per kind so
    re-generating overwrites cleanly."""
    art_dir = data_dir() / nb_id / 'artifacts'
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / f'{kind}.md').write_text(markdown, encoding='utf-8')


def get_artifact(nb_id: str, kind: str) -> str | None:
    p = data_dir() / nb_id / 'artifacts' / f'{kind}.md'
    if not p.exists():
        return None
    return p.read_text(encoding='utf-8')


# ── Vector index (per-notebook sqlite) ───────────────────────────────────────

class VectorIndex:
    """Tiny sqlite-backed cosine retrieval. Embeddings stored as float32 bytes.

    We chose sqlite over a dedicated vector DB because:
      • It's stdlib — zero extra bundle weight
      • Per-notebook isolation means the largest single index stays small
        (~tens of thousands of chunks max in practice)
      • Pure-Python cosine over numpy is fast enough at this scale (sub-50 ms
        for top-K on 10 000 chunks)
    For really huge collections later (>100 k chunks) we can swap in FAISS or
    similar without touching the rest of the codebase, since this class is
    the only retrieval surface."""

    def __init__(self, nb_id: str):
        self._nb_id = nb_id
        self._path = data_dir() / nb_id / 'vectors.sqlite'
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute('''
            CREATE TABLE IF NOT EXISTS chunks (
                source_id TEXT NOT NULL,
                chunk_idx INTEGER NOT NULL,
                text      TEXT NOT NULL,
                embedding BLOB NOT NULL,
                PRIMARY KEY (source_id, chunk_idx)
            )
        ''')
        self._conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_source ON chunks(source_id)'
        )
        self._conn.commit()

    def upsert(self, source_id: str, chunks: list[dict],
               embeddings) -> None:
        """chunks: [{idx, text, ...}, ...], embeddings: np.ndarray shape (N, D)."""
        import numpy as np
        if len(chunks) != len(embeddings):
            raise ValueError('chunks and embeddings length mismatch')
        rows = []
        for chunk, vec in zip(chunks, embeddings):
            rows.append((
                source_id, int(chunk['idx']), chunk['text'],
                np.asarray(vec, dtype=np.float32).tobytes(),
            ))
        self._conn.executemany(
            'INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?)', rows)
        self._conn.commit()

    def delete_source(self, source_id: str) -> None:
        self._conn.execute('DELETE FROM chunks WHERE source_id = ?', (source_id,))
        self._conn.commit()

    def search(self, query_embedding, top_k: int = 8) -> list[dict]:
        """Return top-K matching chunks by cosine similarity. Each result:
        {source_id, chunk_idx, text, score}."""
        import numpy as np
        rows = self._conn.execute(
            'SELECT source_id, chunk_idx, text, embedding FROM chunks').fetchall()
        if not rows:
            return []
        # Stack all embeddings into a single (N, D) matrix for vectorised
        # cosine. For tens of thousands of rows this is far faster than
        # row-by-row dot products.
        # Decode embeddings per-row and DROP malformed ones so a single
        # bad blob can't shift index-alignment between `rows` and the
        # similarity scores (this was causing IndexError on a notebook
        # where one chunk's blob length didn't match the embedding dim
        # — argsort then returned indices past the end of `rows`).
        D = len(query_embedding)
        kept_rows: list = []
        vecs: list = []
        for r in rows:
            blob = r[3]
            if not blob:
                continue
            try:
                v = np.frombuffer(blob, dtype=np.float32)
            except Exception:
                continue
            if v.size != D:
                continue
            kept_rows.append(r)
            vecs.append(v)
        if not kept_rows:
            logger.warning('VectorIndex.search: no usable embeddings '
                           f'(had {len(rows)} rows, embedding dim={D})')
            return []
        rows = kept_rows
        M = np.stack(vecs)
        q = np.asarray(query_embedding, dtype=np.float32)
        # Cosine = dot product / (|a| * |b|), normalise both sides.
        Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
        qn = q / (np.linalg.norm(q) + 1e-9)
        scores = Mn @ qn
        top_idx = np.argsort(-scores)[:top_k]
        return [
            {
                'source_id': rows[int(i)][0],
                'chunk_idx': rows[int(i)][1],
                'text':      rows[int(i)][2],
                'score':     float(scores[int(i)]),
            }
            for i in top_idx
        ]

    def fetch_neighbors(self, source_id: str, chunk_idx: int,
                        window: int = 1) -> list[dict]:
        """Return the chunks immediately adjacent to `(source_id, chunk_idx)`,
        within ±window. Useful for context expansion — a retrieved chunk
        often references something defined in the previous or next chunk
        (e.g. an equation introduced in chunk N, used in chunk N+1).
        """
        if window < 1:
            return []
        rows = self._conn.execute(
            'SELECT source_id, chunk_idx, text FROM chunks '
            'WHERE source_id = ? AND chunk_idx BETWEEN ? AND ? '
            'AND chunk_idx != ? ORDER BY chunk_idx',
            (source_id, chunk_idx - window, chunk_idx + window, chunk_idx),
        ).fetchall()
        return [
            {'source_id': r[0], 'chunk_idx': r[1], 'text': r[2], 'score': 0.0}
            for r in rows
        ]

    def lexical_search(self, terms: list[str], top_k: int = 50) -> list[dict]:
        """Pure keyword search: return every chunk that contains any of the
        whole-word `terms` (case-insensitive). Useful for exhaustive counting
        questions ("how many mentions of dog?") where semantic retrieval
        would miss long-tail occurrences after top-K cutoff.

        Score = number of matches in that chunk (so a chunk with 3 hits
        ranks above one with 1). Chunks with zero matches are filtered out.
        """
        import re
        if not terms:
            return []
        # Build a single regex with word boundaries for each term.
        pats = [re.compile(r'\b' + re.escape(t) + r'\b', re.IGNORECASE)
                for t in terms]
        rows = self._conn.execute(
            'SELECT source_id, chunk_idx, text FROM chunks').fetchall()
        hits = []
        for src_id, idx, text in rows:
            score = sum(len(p.findall(text)) for p in pats)
            if score > 0:
                hits.append({
                    'source_id': src_id,
                    'chunk_idx': idx,
                    'text':      text,
                    'score':     float(score),
                })
        hits.sort(key=lambda h: -h['score'])
        return hits[:top_k]

    def close(self) -> None:
        try: self._conn.close()
        except Exception: pass

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def vector_index(nb_id: str) -> VectorIndex:
    return VectorIndex(nb_id)


# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULT_PERSONA = (
    'You are a careful research assistant. Answer the user\'s question using '
    'ONLY the information in the provided sources. If the answer is not in '
    'the sources, say so clearly. Cite the sources you use with [source N] '
    'markers tied to the chunk IDs.'
)
