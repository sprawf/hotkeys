"""Notebooks — high-level orchestration.

The four operations the UI cares about:
  1. add_source(nb_id, input_ref)   → parse, chunk, embed, index, save
  2. ask(nb_id, question, history)  → retrieve top-K chunks, prompt LLM, cite
  3. generate_artifact(nb_id, kind) → run a prompt template over all sources
  4. remove_source(nb_id, src_id)   → delete from disk + vector index

Everything below this layer is implementation detail (which embedding
model, sqlite layout, MarkItDown internals, etc.). The UI never imports
from storage / embed / ingest directly — it goes through here.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from . import storage, ingest, embed, llm   # noqa: F401

logger = logging.getLogger(__name__)


# ── Add / remove sources ─────────────────────────────────────────────────────

def add_source(nb_id: str, input_ref: str | Path, *,
               name_hint: str = '',
               progress_cb: Callable[[str], None] | None = None) -> dict:
    """End-to-end ingestion. Returns the saved Source dict (without
    embeddings, which live in vectors.sqlite).

    Dedupe by content hash — if the same text was already ingested into
    this notebook, skip the embedding pass and just return the existing
    source meta. Saves the entire embedding cost on accidental re-adds
    or refreshes.
    """
    source = ingest.ingest(input_ref, name_hint=name_hint,
                           progress_cb=progress_cb)
    # Compute a stable content hash AFTER ingestion (parsed Markdown text)
    # so re-importing the same PDF or URL deduplicates even if the file
    # path / URL string is slightly different.
    import hashlib
    source['content_hash'] = hashlib.sha256(
        (source.get('text') or '').encode('utf-8')
    ).hexdigest()

    # Look for an existing source with the same hash in this notebook.
    for existing in storage.list_sources(nb_id):
        existing_full = storage.get_source(nb_id, existing['id'])
        if existing_full and existing_full.get('content_hash') == source['content_hash']:
            if progress_cb:
                progress_cb(f'Already in this doc set as "{existing_full["name"]}" — skipping')
            return {
                'id':           existing_full['id'],
                'name':         existing_full['name'],
                'origin':       existing_full.get('origin', ''),
                'kind':         existing_full.get('kind', 'text'),
                'created_at':   existing_full['created_at'],
                'chunk_count':  len(existing_full.get('chunks', [])),
                'char_count':   len(existing_full.get('text', '')),
            }

    # Embed the chunks
    if source['chunks']:
        chunk_texts = [c['text'] for c in source['chunks']]
        if progress_cb:
            progress_cb(f'Embedding {len(chunk_texts)} chunks…')
        embeddings = embed.encode_documents(chunk_texts)
        with storage.vector_index(nb_id) as idx:
            idx.upsert(source['id'], source['chunks'], embeddings)

    # Save the source (without embeddings — they're in the vector index)
    storage.save_source(nb_id, source)
    return {
        'id':           source['id'],
        'name':         source['name'],
        'origin':       source['origin'],
        'kind':         source['kind'],
        'created_at':   source['created_at'],
        'chunk_count':  len(source['chunks']),
        'char_count':   len(source['text']),
    }


def remove_source(nb_id: str, src_id: str) -> None:
    storage.delete_source(nb_id, src_id)


# ── Q&A with citations ───────────────────────────────────────────────────────

# Retrieval is intentionally generous; the LLM can ignore irrelevant chunks
# but can't synthesise an answer from chunks it never saw.
# In-process answer cache (per Python session). Keyed by
# (notebook_id, question_normalized, sorted_selected_sources, persona).
# Saves token cost when users ask the same question twice — common when
# experimenting with personas or re-running a query after editing a
# source. Bounded LRU to keep memory in check.
_ANSWER_CACHE: dict[tuple, dict] = {}
_ANSWER_CACHE_MAX = 64


_TOP_K = 8
# Hard cap on context characters fed to the LLM, leaves room for the
# question + system prompt + response without hitting context window limits
# on small providers (Qwen 1.5B is 32k tokens ≈ 100k chars, but we leave a
# huge margin).
_CONTEXT_CHAR_BUDGET = 20_000


# Patterns that signal the user wants an EXHAUSTIVE enumeration / count.
# When matched, we switch the retrieval strategy from semantic-top-K
# (which is great for "explain this" but bad for "list everything") to
# lexical search + a larger top-K so we don't miss long-tail occurrences.
_EXHAUSTIVE_RE = re.compile(
    # Two-stage match to keep this strict:
    # (a) an "enumerate" verb (how many / count / list / each / every / all)
    # (b) PAIRED WITH an "enumeration noun" — mentions, occurrences, instances,
    #     appearances, references, verses, times.
    # This rejects "How many years did Noah preach?" (years is not an
    # enumeration noun) while still catching "How many mentions of X?" and
    # "List every verse mentioning Y."
    r'\b(how many|count|number of|all|every|list|each|enumerate|show)\b'
    r'.{0,60}'
    r'\b(mention|mentions|mentioned|occurrence|occurrences|instance|'
    r'instances|appearance|appearances|appear|appears|appeared|appearing|'
    r'reference|references|verse|verses|time|times|usage|usages|'
    r'row|rows|entry|entries|entrie|record|records|location|locations|'
    r'place|places|line|lines)\b',
    re.IGNORECASE,
)
# Words to NOT use as lexical search terms (stop words).
_STOPWORDS = frozenset(
    'the a an of in on at to for and or but is are was were be been being '
    'this that these those it its their there here what where when why how '
    'which who whom whose can could may might would should will shall do '
    'does did has have had not no yes if then than as so just all any some '
    'each every other another such same different more most less few many '
    'much very also too only own about with from up down out off over under '
    'mention mentions mentioned occurrence occurrences appear appears appearance '
    'verse verses chapter chapters word words count counts'.split()
)


def _detect_exhaustive_intent(question: str) -> tuple[bool, list[str]]:
    """Return (is_exhaustive, lexical_terms).

    `is_exhaustive` is True when the question shape clearly asks for a full
    enumeration / count. `lexical_terms` is the list of content words we
    should fold into the lexical retrieval.
    """
    is_exhaustive = bool(_EXHAUSTIVE_RE.search(question))
    # Cap depends on intent: exhaustive queries frequently list many
    # keyword categories (e.g. "oaths / swearing / vow / By Allah / By my
    # Lord / La uqsimu / abstention / breaking / pledge") and a 12-term
    # cap eats them all with the quoted phrases alone. 30 gives enough
    # room for the non-quoted content words too. Normal queries stay at
    # 12 to avoid seed noise.
    cap = 30 if is_exhaustive else 12
    seed = _content_words(question, cap=cap)
    quoted = re.findall(r'["\']([^"\']{2,40})["\']', question)
    if quoted:
        quoted_terms: list[str] = []
        for qphrase in quoted:
            for w in re.findall(r"[A-Za-z']{2,30}", qphrase):
                if w.lower() not in _STOPWORDS:
                    quoted_terms.append(w)
        # Deduplicate while preserving order: quoted first, then seed.
        seen: set[str] = set()
        merged: list[str] = []
        for t in quoted_terms + seed:
            if t.lower() in seen:
                continue
            seen.add(t.lower())
            merged.append(t)
        return is_exhaustive, merged[:cap]
    return is_exhaustive, seed


def _content_words(question: str, cap: int = 12) -> list[str]:
    """Extract content (non-stopword) tokens from a question. Used to seed
    lexical retrieval when no quoted phrase is present.

    Grammar covers four shapes:
      • Plain 2+ letter words  ("dog", "klb", "ant", "qsm")
      • Buckwalter-flavoured tokens with punctuation ("{ll~ah", "n~aAs",
        ">anaAmila")
      • Verse-location codes like "114:6", "27:18", or standalone digits
        like "2", "108", "286". Kept so the retriever can find literal
        Quran-corpus rows keyed on (S:A:W:P).
      • Location-anchored rewrites: "Surah 108" / "chapter 2" is
        equivalent to "every corpus row starting with (108:" / "(2:".
        We synthesise "(108:" as a lex term so the retriever hits the
        surah's own rows instead of arbitrary chunks that happen to
        contain "108" somewhere in a different context.

    The original impl was `[A-Za-z']{3,30}` which silently dropped every
    digit and every Buckwalter punctuation char. Structural questions on
    the Quran corpus therefore retrieved nothing — the lexical seed was
    empty of the very tokens the corpus actually contains.
    """
    # Location-anchored rewrites: pull "Surah N" / "chapter N" / "verse
    # S:V" and synthesise the corpus-native location prefixes.
    # More-specific anchors first: they score higher because they match
    # fewer (or exactly one) chunks. Order matters for the [:12] cap.
    anchors: list[str] = []
    q_lower = question.lower()

    # Detect "first (token|word|entry) of Surah N" and inject the exact
    # verse-1 prefix. Without this, "(2:" matches 15% of the corpus and
    # the specific (2:1:1:1) chunk gets buried under other (2:X:Y:Z) hits.
    is_first_of = bool(re.search(
        r'\b(?:first|opening|beginning|initial)\b.{0,40}'
        r'\b(?:surah|sura|chapter)\s+\d',
        q_lower))
    is_last_of = bool(re.search(
        r'\b(?:last|final|closing|ending)\b.{0,40}'
        r'\b(?:surah|sura|chapter|verse)\s+\d',
        q_lower))

    for m in re.finditer(r'\b(?:surah|sura|chapter)\s+(\d{1,3})\b',
                          question, flags=re.IGNORECASE):
        s = m.group(1)
        if is_first_of:
            anchors.append(f'({s}:1:1:1)')  # exact opening token
            anchors.append(f'({s}:1:')       # opening verse anchor
        # General surah anchor (least specific, always included).
        anchors.append(f'({s}:')

    for m in re.finditer(r'\bverse\s+(\d{1,3}):(\d{1,3})\b',
                          question, flags=re.IGNORECASE):
        s, v = m.group(1), m.group(2)
        if is_first_of:
            anchors.append(f'({s}:{v}:1:1)')
        anchors.append(f'({s}:{v}:')

    # Verse-location codes like "114:6" or "27:18:6" — kept verbatim.
    locs = re.findall(r'\d{1,3}:\d{1,3}(?:\:\d{1,3})?', question)

    # Plain 2+ char alpha / Buckwalter tokens. Hyphens split into
    # separate tokens: "INL-tagged" should retrieve "INL" (which IS a
    # corpus tag) as its own term. Same for compound phrases like
    # "Al-Kawthar" -> "Al" + "Kawthar".
    words = re.findall(r"[A-Za-z'{}~`]{2,30}", question)
    filtered = [w for w in words if w.lower() not in _STOPWORDS]

    # Standalone integers 1-4 digits (surah / verse / word numbers).
    # Allow single-digit so "Surah 2" is retrievable — the earlier
    # 2-digit minimum lost every reference to surahs 1-9.
    nums = re.findall(r'\b\d{1,4}\b', question)

    seen: set[str] = set()
    out: list[str] = []
    # Anchors first (highest signal), then locations, then words, then bare
    # numbers. Order matters — earlier terms get retained if the [:12] cap
    # is hit.
    for t in anchors + locs + filtered + nums:
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    return out[:cap]


_REFUSAL_PATTERNS = re.compile(
    r'(not\s+(?:mentioned|found|present|available|discussed|covered|included)'
    r'\s+in\s+(?:the\s+)?(?:provided\s+)?sources?'
    r'|(?:the\s+)?sources?\s+(?:do\s+not|don\'?t|does\s+not|doesn\'?t)\s+'
    r'(?:contain|mention|discuss|cover|include|have|address|specify)'
    r'|no\s+information\s+(?:about|regarding|on|is\s+provided)'
    r'|(?:i\s+)?(?:can(?:not|\'?t)\s+find|couldn\'?t\s+find)'
    r'|is\s+not\s+(?:in|part\s+of)\s+(?:the\s+)?(?:provided\s+)?sources?)',
    re.IGNORECASE,
)


def _postprocess_answer(answer: str, *,
                        valid_citation_ids: set[int],
                        is_exhaustive: bool) -> str:
    """Clean up known LLM output quirks:

    1. Citation index drift: LLMs sometimes emit [0] (when we 1-index)
       or out-of-range [N]. We strip / clamp these so the UI's citation
       chips always map to a real source chunk.

    2. Exhaustive total/bullet mismatch: when the model says
       "Total occurrences: 5" but lists 6 bullets, we override the
       total with the actual bullet count.

    3. Refusal citation spam: when the answer is a "not in sources"
       refusal, the LLM habitually trails the sentence with every
       chunk id it inspected (e.g. "not mentioned [1] [2] [3] … [18]").
       Those chips are pure visual noise for a non-answer, so we drop
       every citation marker from short refusal responses.

    The transformations are deliberately conservative — anything we
    can't safely fix we leave alone rather than risk corrupting the
    answer text.
    """
    # ── CJK-bracket normalisation ──────────────────────────────────────────
    # Cerebras' gpt-oss-120b non-deterministically emits fullwidth CJK
    # brackets 【N】 instead of ASCII [N] around citations. The rest of
    # this function (and the UI citation-chip renderer) matches only
    # ASCII, so without this the chips silently don't render and
    # `used_chunk_ids` comes back empty. Same trick works for the
    # (rarer) fullwidth 〔N〕 and 「N」.
    answer = re.sub(r'[【〔「]\s*(\d+)\s*[】〕」]', r'[\1]', answer)

    # ── Refusal citation-chip suppression ──────────────────────────────────
    # Applies BEFORE the valid_citation_ids guard so refusals without
    # citations aren't a special case. Threshold on length keeps this
    # from misfiring on real answers that happen to include a caveat
    # sentence: a legitimate partial-refusal ("the sources mention X
    # but do not discuss Y") is > 400 chars in practice.
    plain = re.sub(r'\[\d+\]', '', answer).strip()
    if _REFUSAL_PATTERNS.search(plain) and len(plain) < 400:
        answer = re.sub(r'\s*\[\d+\]', '', answer)
        # Collapse runs of whitespace introduced by the stripping.
        answer = re.sub(r'[ \t]{2,}', ' ', answer).strip()
        return answer

    if not valid_citation_ids:
        return answer

    max_id = max(valid_citation_ids)
    min_id = min(valid_citation_ids)

    # ── Citation cleanup ───────────────────────────────────────────────────
    def _fix_cite(m: re.Match) -> str:
        try:
            n = int(m.group(1))
        except Exception:
            return m.group(0)
        # [0] is the most common drift — replace with [1] when 1 is valid.
        if n == 0 and 1 in valid_citation_ids:
            return '[1]'
        # Clamp out-of-range citations to nearest valid ID. We prefer
        # silently fixing over removing because removing breaks the
        # markdown sentence structure.
        if n in valid_citation_ids:
            return m.group(0)
        if n > max_id:
            return f'[{max_id}]'
        if n < min_id:
            return f'[{min_id}]'
        return m.group(0)
    answer = re.sub(r'\[(\d+)\]', _fix_cite, answer)

    # Collapse consecutive duplicate citation markers: "[13] [13]" or
    # "[7][7][7]" → "[13]" / "[7]". Cerebras' reasoning models occasionally
    # emit the same chunk id back-to-back when they double-cite for
    # emphasis; a single chip reads cleaner.
    answer = re.sub(r'(\[(\d+)\])(?:\s*\[\2\])+', r'\1', answer)

    # ── Exhaustive total/bullet count reconciliation ────────────────────
    if is_exhaustive:
        # Count bullet items in the answer. The exhaustive prompt template
        # asks the LLM to use "- " bullets under an "## Each occurrence"
        # heading. Be lenient: count any line that starts with "- " or "* "
        # followed by content.
        bullet_count = sum(1 for line in answer.split('\n')
                           if re.match(r'^\s*[-*]\s+\S', line))
        # If we found bullets AND the model stated a different total,
        # rewrite the total to match what the model actually emitted.
        if bullet_count > 0:
            def _fix_total(m: re.Match) -> str:
                stated = int(m.group(2))
                if stated != bullet_count:
                    logger.info(f'Reconciled exhaustive total: '
                                f'{stated} -> {bullet_count}')
                    return f'{m.group(1)}{bullet_count}{m.group(3)}'
                return m.group(0)
            # Match patterns like "Total occurrences: 5" or "**Total: 5**"
            answer = re.sub(
                r'(\bTotal\s*(?:occurrences|mentions|count)?\s*:?\s*\**\s*)(\d+)(\**)',
                _fix_total, answer, flags=re.IGNORECASE,
            )

    return answer


def generate_source_guide(nb_id: str, source_id: str, *,
                          force: bool = False) -> str:
    """Per-source NotebookLM-style "Source guide" — a short, dense overview
    of a single source. Cached on the Source dict after first generation so
    repeated opens are instant. `force=True` re-generates.

    Returns the guide as Markdown, or '' on failure."""
    src = storage.get_source(nb_id, source_id)
    if src is None:
        return ''
    if not force:
        cached = src.get('guide')
        if cached:
            return cached
    snippet = (src.get('text') or '')[:20_000]
    if not snippet:
        return ''
    system = (
        'You produce a NotebookLM-style "Source guide" — a short '
        'introductory overview of a single research source. Style: '
        'one or two compact paragraphs, dense with bolded key terms '
        '(**like this**), no preamble, no "This source...", '
        'just open with the topic itself. Max ~150 words.'
    )
    prompt = (
        f'Generate a Source guide for the following document:\n\n'
        f'## {src.get("name", "Source")}\n\n{snippet}\n\n'
        'Source guide:'
    )
    try:
        guide = llm.ask(prompt, system=system).strip()
    except Exception as e:
        logger.warning(f'generate_source_guide failed: {e}')
        return ''
    src['guide'] = guide
    try:
        storage.save_source(nb_id, src)
    except Exception:
        pass
    return guide


def ask(nb_id: str, question: str, *,
        chat_history: list[dict] | None = None,
        selected_source_ids: list[str] | None = None,
        progress_cb: Callable[[str], None] | None = None,
        stream_cb: Callable[[str], None] | None = None) -> dict:
    """Answer `question` grounded in the notebook's sources.

    Returns:
      {
        'answer':    str,            # markdown text with [source N] markers
        'citations': [{ source_id, source_name, chunk_idx, text, score }],
        'used_chunk_ids': [N, ...]   # indices into citations actually cited
      }
    """
    _log = lambda m: (progress_cb(m) if progress_cb else logger.info(m))

    nb = storage.get_notebook(nb_id)
    if nb is None:
        raise RuntimeError(f'Doc set {nb_id} not found')

    # ── Empty-query guard ────────────────────────────────────────────────
    # Whitespace-only / empty prompts must not reach the LLM. Left alone
    # the model happily invents a "general overview" answer, wastes a
    # round-trip and pollutes the answer cache with a bogus entry keyed
    # on the empty string.
    if not (question or '').strip():
        return {
            'answer':         'Type a question first, then press Enter.',
            'citations':      [],
            'used_chunk_ids': [],
        }

    # ── Answer cache lookup ───────────────────────────────────────────────
    # Cache key includes: notebook, normalised question, selected sources,
    # and persona — change any of these and the cached answer is wrong.
    q_norm = ' '.join(question.lower().split())
    sel_key = tuple(sorted(selected_source_ids or []))
    cache_key = (nb_id, q_norm, sel_key, (nb.get('persona') or '')[:200])
    if cache_key in _ANSWER_CACHE:
        _log('Cached answer hit (no LLM call).')
        return _ANSWER_CACHE[cache_key]

    sources_list = storage.list_sources(nb_id)
    if not sources_list:
        return {
            'answer':         'No sources in this doc set yet. Add some with '
                              'the + button on the left, then ask again.',
            'citations':      [],
            'used_chunk_ids': [],
        }

    # If the caller scoped this query to specific sources, build a quick
    # lookup so we can drop hits that fall outside the scope. None means
    # "use everything" (legacy callers + the default state).
    selected_set = (set(selected_source_ids)
                    if selected_source_ids is not None else None)
    if selected_set is not None and not selected_set:
        return {
            'answer':         'No sources selected. Tick at least one source '
                              'on the left to include it in the chat.',
            'citations':      [],
            'used_chunk_ids': [],
        }

    # ── Retrieve relevant chunks ─────────────────────────────────────────────
    # Choose between three retrieval modes based on question shape:
    #   • EXHAUSTIVE  ("how many mentions of X") → lexical only, top_k large
    #   • HYBRID       (every other question)     → semantic top-K plus a
    #                                                lexical pass to backfill
    #                                                any chunks the semantic
    #                                                top-K missed but that
    #                                                literally mention the key
    #                                                content words
    is_exhaustive, lex_terms = _detect_exhaustive_intent(question)
    _log('Retrieving relevant passages…')

    with storage.vector_index(nb_id) as idx:
        if is_exhaustive and lex_terms:
            _log(f'Exhaustive intent detected; lexical search for: '
                 f'{", ".join(lex_terms[:5])}')
            # Pull a generous top-K — we want every occurrence, not just
            # the most "relevant". 200 should fit a 50-mention term
            # comfortably while staying inside any LLM's context budget.
            raw_hits = idx.lexical_search(lex_terms, top_k=500)
            # Semantic fallback: if the corpus doesn't literally contain
            # the question's content words (e.g. Buckwalter-transliterated
            # Quran morphology, where "verse" or "fatiha" don't appear as
            # strings), the lexical search returns 0 hits and the user
            # sees "no relevant passages found" for a question the corpus
            # actually answers structurally. Fall back to the standard
            # semantic path so meaning survives absence of the exact word.
            if not raw_hits:
                _log('Lexical returned 0 hits — falling back to semantic.')
                q_vec = embed.encode_query(question)
                raw_hits = idx.search(q_vec, top_k=_TOP_K * 3)
        else:
            q_vec = embed.encode_query(question)
            over_fetch = _TOP_K * 3 if selected_set is not None else _TOP_K * 2
            semantic_hits = idx.search(q_vec, top_k=over_fetch)
            # Hybrid retrieval: run a lexical pass in parallel and
            # INTERLEAVE the two lists so both survive the chunk_cap
            # trim. Old behaviour appended lex hits past the semantic
            # tail, then chunk_cap truncated them off — result: literal-
            # keyword questions (e.g. "segments of verse 114:6" on a
            # tabular corpus where the semantic embedding is useless)
            # got zero of the lex hits into the final context.
            if lex_terms:
                lex_hits = idx.lexical_search(lex_terms, top_k=_TOP_K * 2)
                # Round-robin merge: sem, lex, sem, lex, ... skipping
                # duplicates by (source_id, chunk_idx). Guarantees the
                # top lex match is in position 2 no matter how many
                # semantic hits precede it.
                seen: set[tuple[str, int]] = set()
                merged: list[dict] = []
                sem_it = iter(semantic_hits)
                lex_it = iter(lex_hits)
                while True:
                    took = False
                    for it in (sem_it, lex_it):
                        for h in it:
                            key = (h['source_id'], h['chunk_idx'])
                            if key in seen:
                                continue
                            seen.add(key)
                            merged.append(h)
                            took = True
                            break
                    if not took:
                        break
                raw_hits = merged
            else:
                raw_hits = semantic_hits

    if selected_set is not None:
        hits = [h for h in raw_hits if h['source_id'] in selected_set]
    else:
        hits = raw_hits
    # Cap how many chunks we feed to the LLM: more for exhaustive queries,
    # tight for normal chat (model writes shorter answers when given less).
    chunk_cap = 200 if is_exhaustive else _TOP_K
    hits = hits[:chunk_cap]

    # Context expansion — for non-exhaustive questions, pull the immediate
    # neighbours of each retrieved chunk. Often the cited fact is in chunk
    # N but the surrounding sentence (definition, equation, dialogue tag)
    # sits in N-1 or N+1. This is one of the biggest quality wins per
    # token of extra context. Skip for exhaustive queries to keep the
    # context budget free for more occurrences.
    if not is_exhaustive and hits:
        with storage.vector_index(nb_id) as idx:
            seen = {(h['source_id'], h['chunk_idx']) for h in hits}
            expansions = []
            for h in hits[:_TOP_K]:   # only expand the top chunks
                for nb_chunk in idx.fetch_neighbors(h['source_id'],
                                                     int(h['chunk_idx']),
                                                     window=1):
                    key = (nb_chunk['source_id'], nb_chunk['chunk_idx'])
                    if key in seen:
                        continue
                    # Inherit a slightly lower score so we sort right
                    # without confusing the ranking.
                    nb_chunk['score'] = h.get('score', 0.0) * 0.5
                    expansions.append(nb_chunk)
                    seen.add(key)
            hits.extend(expansions)

    # Map source_id → name for citation rendering. Cache the look-up so we
    # don't re-read sources/ for every hit.
    source_names = {s['id']: s['name'] for s in sources_list}

    # ── Build the context string for the LLM ─────────────────────────────────
    # We label each chunk [1], [2], ... — these are the citation IDs the
    # LLM is told to reference in its answer. We keep a parallel list of
    # citations so the UI can resolve [N] → source name + passage on click.
    citations = []
    context_lines = []
    total_chars = 0
    # Exhaustive queries pack extra chunks so we can enumerate every
    # occurrence, but the budget has to survive the smallest provider
    # in the fallback chain — Groq llama-3.3-70b's HTTP endpoint 413s
    # around ~40k characters of body (well below the model's actual
    # context window; a request-body cap, not a token cap). We land at
    # 30k so header + user prompt + system prompt still fit under 40k.
    # For a corpus like the Quran morphology (~900 char/chunk) that's
    # still ~30-35 chunks — plenty to enumerate a term appearing 5-25
    # times across the file.
    # Cerebras gpt-oss-120b is primary and has huge context; 50k fits
    # ~55 chunks of ~900 chars each. On a Groq fallback this will 413
    # (Groq caps around 40k body) but Cerebras stays up 99% of the time.
    # Higher would need per-provider dispatch.
    char_budget = 50_000 if is_exhaustive else _CONTEXT_CHAR_BUDGET
    for n, hit in enumerate(hits, start=1):
        chunk_text = hit['text']
        # Stop adding chunks once we'd exceed the context budget — keeps
        # the prompt size predictable on the providers with smaller limits.
        if total_chars + len(chunk_text) > char_budget:
            break
        src_name = source_names.get(hit['source_id'], 'Unknown source')
        context_lines.append(
            f'[{n}] Source: {src_name}  (chunk {hit["chunk_idx"]})\n'
            f'{chunk_text}\n'
        )
        citations.append({
            'id':          n,
            'source_id':   hit['source_id'],
            'source_name': src_name,
            'chunk_idx':   hit['chunk_idx'],
            'text':        chunk_text,
            'score':       hit['score'],
        })
        total_chars += len(chunk_text)

    if not citations:
        return {
            'answer':         "No relevant passages found in the sources. "
                              "Try rephrasing your question.",
            'citations':      [],
            'used_chunk_ids': [],
        }

    # ── Prompt the LLM ───────────────────────────────────────────────────────
    system = nb.get('persona') or storage._DEFAULT_PERSONA
    history_block = _format_history(chat_history or [])
    # For exhaustive (count / list-all) questions the LLM gets a different
    # set of instructions that forces it to enumerate every match instead
    # of paraphrasing or extrapolating.
    #
    # Persona precedence: when the notebook has a non-default persona, we
    # prepend a flagged section that explicitly outranks the default
    # formatting prescriptions. Without this, the persona ("explain to a
    # 10-year-old") gets overridden by the hard "use **bold**, use ##"
    # rules below.
    has_custom_persona = bool(nb.get('persona')
                              and nb['persona'] != storage._DEFAULT_PERSONA)
    persona_override_block = (
        '## Persona — TAKES PRECEDENCE over the default formatting rules\n'
        f'{nb.get("persona", "")}\n\n'
        'Honor the persona above when it conflicts with the default '
        'formatting rules: keep the citation [N] markers (they are '
        'load-bearing for the UI), but follow the persona for tone, '
        'vocabulary, length, and structure.\n\n'
        if has_custom_persona else ''
    )
    user_prompt = (
        f'{history_block}'
        + persona_override_block
        + f'## Sources\n\n'
        + '\n---\n'.join(context_lines)
        + '\n\n'
        + f'## Question\n{question}\n\n'
        + (
            # Exhaustive (count / list-all) prompt is the same regardless
            # of persona — the answer has to be exhaustive even in a
            # casual voice.
            '## You are answering an EXHAUSTIVE / COUNTING question.\n'
            'Hard rules for this kind of question:\n'
            '1. Scan EVERY source passage above. Do not stop early.\n'
            '2. Match LITERALLY on whatever the user named — a word, a '
            'root code, a tag, a location pattern. If the user says '
            '"ROOT:klb" or "root klb", every row containing the exact '
            'string "klb" as a token counts, regardless of surrounding '
            'lemma or translation. Do not filter by semantic meaning; '
            'do not pre-decide that some matches are "the same thing."\n'
            '3. Report the EXACT count of distinct occurrences.\n'
            '4. Quote each occurrence VERBATIM from the source with '
            'its location code. Do NOT paraphrase, translate, or '
            'reformat the row. Copy the text as-is between quotes.\n'
            '5. **COUNT YOUR BULLETS**: before writing the final total, '
            'literally count the bullet items in your "Each occurrence" '
            'list. The number in your total MUST equal that bullet count. '
            'If you list 6 bullets, the total is 6 — not 5, not 7.\n'
            '6. If the term appears 0 times, say so plainly and STOP. '
            'Do not fabricate example rows.\n'
            '7. **NEVER invent locations or content.** If a specific '
            'verse number, chunk id, or row is not literally present in '
            'the retrieved passages, do NOT claim it exists. Extrapolating '
            'a pattern ("verses 1 through 32 must all be there") is '
            'strictly forbidden — count only what is physically shown.\n'
            '8. When the user asks about a location code like "S:V:W:P" '
            '(surah:verse:word:position), those are FOUR different axes. '
            'Do not confuse the "word" or "position" axis with the verse '
            'axis when counting verses.\n\n'
            '9. **Ultra-compact output.** Do NOT write a description, '
            'summary, or per-item explanation. Do NOT restate the '
            'question. One line per occurrence in the exact format '
            'shown below. This makes room for hundreds of matches '
            'inside the token budget; verbose per-item text truncates '
            'the answer.\n'
            '10. **Scan every chunk in the source above.** Some retrieved '
            'chunks contain many matches; do not stop at the first '
            'match in each chunk. If you see 10 matching rows in one '
            'chunk, emit 10 bullets from that chunk.\n\n'
            'Output format (exactly, nothing else):\n'
            '**Total occurrences: <N>**\n\n'
            '## Each occurrence\n'
            '- (S:V) "<exact verbatim quote>" [N]\n\n'
            'Where (S:V) is the surah:verse reference from the source, '
            'the quote is the exact English gloss verbatim from the '
            'row, and [N] is the chunk id it came from. One line per '
            'row. No section headers between surahs. No prose.\n\n'
            if is_exhaustive else
            # Custom persona → ONLY the citation rule is mandatory.
            # Everything else (style, length, bold, structure) is left to
            # the persona. The model needs explicit permission to drop
            # bold/headings or it defaults to its NotebookLM-style habits.
            '## How to write the answer\n'
            'Follow the **Persona** at the top of this prompt strictly — '
            'tone, vocabulary, structure, and length are all up to the '
            'persona. Do NOT default to bolded terms or markdown headings '
            'or bulleted lists unless the persona asks for them.\n\n'
            '## Hard rules (always apply, regardless of persona)\n'
            '- Use ONLY the sources above; never invent facts.\n'
            '- Put [N] citation markers right after each factual claim, '
            'inline, e.g. "Plants use sunlight [1]." This is the only '
            'formatting that is mandatory — the persona cannot override it.\n'
            '- If multiple sources back the same claim, stack: "[1] [3]".\n'
            '- Do NOT begin with "Based on the sources..." or "Here is...".\n'
            '- If the sources do not contain the answer, say so plainly.\n'
            if has_custom_persona else
            # Default (no persona) → full NotebookLM-style formatting.
            '## How to write the answer\n'
        )
        + 'Open with a 1-2 sentence definition or framing of the topic, '
          'with **bold** on the key term and the most important nouns, and '
          'an inline [N] citation right after each substantive claim.\n\n'
        + 'If the question has multiple parts, or the topic naturally breaks '
          'into stages / steps / categories, follow with a **## Section '
          'Heading** (use H2 with ##) and then a bulleted list. EACH bullet '
          'starts with a **bold lead-in phrase:** followed by 1-2 sentences '
          'of explanation, with [N] markers after the cited facts. Example:\n\n'
        + '  - **Training a Reward Model:** A separate model is trained to '
          'predict which response humans would prefer [1]. The preference is '
          'typically based on whether the answer is **truthful, helpful, and '
          'harmless** [1].\n\n'
        + 'If the answer is short or simple (one fact, one definition) skip '
          'the heading and bullets — just give the 1-2 sentence answer with '
          'inline citations.\n\n'
        + '## Hard rules\n'
        + '- Use ONLY the sources above; never invent facts.\n'
        + '- Put [N] markers right after the claim they support, not at the '
          'end of paragraphs. Example: "...glucose and oxygen [1]."\n'
        + '- If multiple sources back the same claim, stack them: "...neural '
          'networks [1] [3]."\n'
        + '- DO NOT begin with "Based on the sources..." / "The sources say..." '
          '/ "Here is...". Open with the topic itself.\n'
        + '- If the sources do not contain the answer, say so plainly in ONE '
          'sentence.\n'
        + '- Bold proper nouns and important technical terms aggressively — '
          'this is encyclopedia style.\n'
    )

    _log('Asking the model…')
    if stream_cb is not None:
        # Streaming path: feed chunks to the UI as they arrive, accumulate
        # the full answer for post-processing. If streaming fails inside
        # llm.stream() it transparently falls back to one-shot (yielding
        # the complete answer as a single chunk).
        parts: list[str] = []
        for delta in llm.stream(user_prompt, system=system):
            if not delta:
                continue
            parts.append(delta)
            try:
                stream_cb(delta)
            except Exception as cb_err:
                logger.warning(f'stream_cb raised: {cb_err}')
        answer = ''.join(parts)
    else:
        answer = llm.ask(user_prompt, system=system)

    # ── Post-process answer for known LLM quirks ──────────────────────────
    answer = _postprocess_answer(
        answer,
        valid_citation_ids={c['id'] for c in citations},
        is_exhaustive=is_exhaustive,
    )

    # Figure out which citations the LLM actually referenced — used by the
    # UI to dim or hide citations that weren't part of the final answer.
    used = sorted({int(m.group(1)) for m in re.finditer(r'\[(\d+)\]', answer)})

    result = {
        'answer':         answer,
        'citations':      citations,
        'used_chunk_ids': used,
    }
    # Persist to LRU answer cache. Evict oldest if we're over the cap so
    # the cache never grows unbounded in a long-running session.
    if len(_ANSWER_CACHE) >= _ANSWER_CACHE_MAX:
        oldest = next(iter(_ANSWER_CACHE))
        del _ANSWER_CACHE[oldest]
    _ANSWER_CACHE[cache_key] = result
    return result


def _format_history(history: list[dict]) -> str:
    """Render prior chat turns as a compact transcript prefix. Keeps the
    most recent ~6 turns so context size stays bounded."""
    if not history:
        return ''
    recent = history[-6:]
    lines = ['## Prior conversation\n']
    for msg in recent:
        role = msg.get('role', 'user').upper()
        content = (msg.get('content') or '').strip()
        if content:
            lines.append(f'{role}: {content}\n')
    lines.append('\n')
    return ''.join(lines)


# ── Studio: generated artifacts ──────────────────────────────────────────────

# ── First-source bootstrap helpers ───────────────────────────────────────────

def suggest_notebook_title(source: dict) -> str:
    """Ask the LLM to suggest a title for the notebook based on the first
    source's content. Returns a short title (up to ~60 chars). Caller is
    responsible for persisting via storage.save_meta.

    Token-saving shortcut: if the source's own name is already meaningful
    (e.g. a Wikipedia URL has a clear title like "Large_language_model"
    or a PDF filename like "Quran_English.pdf"), we use that directly
    and skip the LLM call. Only "Untitled", "Document", numeric filenames,
    and other low-signal names trigger an LLM titling call.
    """
    raw_name = (source.get('name') or '').strip()
    cleaned = _humanise_source_name(raw_name)
    if _is_meaningful_name(cleaned):
        logger.info(f'suggest_notebook_title: reusing source name "{cleaned}" '
                    '(no LLM call)')
        return cleaned[:80]

    snippet = (source.get('text') or '')[:4000]
    if not snippet:
        return raw_name[:60] or 'Untitled doc set'
    system = (
        'You generate a short, descriptive title for a research notebook '
        'based on its source material. Output ONLY the title — no quotes, '
        'no "Notebook:" prefix, no explanation. Max 60 characters.'
    )
    prompt = (
        f'Generate a title for a notebook whose first source is:\n\n'
        f'## {source.get("name", "Source")}\n\n{snippet}\n\n'
        'Title (max 60 characters):'
    )
    try:
        title = llm.ask(prompt, system=system).strip()
        title = title.strip('"').strip("'").strip()
        if title.lower().startswith('title:'):
            title = title.split(':', 1)[1].strip()
        return (title[:80] or raw_name or 'Untitled doc set')[:80]
    except Exception as e:
        logger.warning(f'suggest_notebook_title failed: {e}')
        return raw_name[:60] or 'Untitled doc set'


def _humanise_source_name(name: str) -> str:
    """Turn a file/URL name into a presentable title:
       "Large_language_model"            → "Large language model"
       "annual-report-2024.pdf"           → "annual-report-2024"
       "en.wikipedia.org/wiki/Foo_bar"    → "Foo bar"
    """
    if not name:
        return ''
    # Strip URL path → keep last segment
    if '/' in name and ('http' in name or '.' in name.split('/', 1)[0]):
        seg = name.rstrip('/').rsplit('/', 1)[-1]
        if seg:
            name = seg
    # Strip extension
    if '.' in name:
        stem, ext = name.rsplit('.', 1)
        # Only strip well-known doc extensions
        if ext.lower() in ('pdf', 'docx', 'doc', 'pptx', 'ppt', 'xlsx', 'xls',
                            'csv', 'txt', 'md', 'html', 'htm', 'xml', 'json',
                            'epub', 'rtf', 'mp3', 'wav', 'm4a'):
            name = stem
    # Replace separators with spaces
    name = re.sub(r'[_\-]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def _is_meaningful_name(cleaned_name: str) -> bool:
    """Return True when the cleaned name is good enough to use as a
    notebook title directly (no LLM call needed). A name is meaningful
    when it has ≥2 real words and isn't a generic placeholder."""
    if not cleaned_name or len(cleaned_name) < 4:
        return False
    low = cleaned_name.lower()
    generic = {
        'untitled', 'document', 'source', 'file', 'pasted', 'noname',
        'no name', 'untitled notebook', 'new document', 'scan', 'image',
        'screenshot',
    }
    if low in generic:
        return False
    # Reject pure-numeric / pure-symbol names
    if not re.search(r'[A-Za-z]{3,}', cleaned_name):
        return False
    word_count = len(re.findall(r'\b[A-Za-z]{2,}\b', cleaned_name))
    return word_count >= 2


def generate_first_source_summary(nb_id: str, source: dict) -> dict:
    """Produce a NotebookLM-style auto-summary as the FIRST assistant
    message of a notebook, right after the user adds their first source.
    Returns a message dict ready to be appended to the chat:
        {role, content, citations, used_chunk_ids, followups}
    """
    sources_list = storage.list_sources(nb_id)
    if not sources_list:
        return {
            'role': 'assistant',
            'content': '*No sources to summarise yet.*',
            'citations': [], 'used_chunk_ids': [], 'followups': [],
        }
    # Build a citation list keyed [1] for the just-added source.
    citations = [{
        'id':          1,
        'source_id':   source['id'],
        'source_name': source.get('name', 'Source'),
        'chunk_idx':   0,
        'text':        (source.get('text') or '')[:1200],
        'score':       1.0,
    }]
    snippet = (source.get('text') or '')[:30_000]
    system = (
        'You produce a NotebookLM-style overview of a research source — the '
        'kind of opening summary that appears as the first assistant message '
        'when a user adds their first source to a new notebook. Style: '
        'confident, factual, dense with bolded key terms; one or two short '
        'paragraphs of body text. Use **bold** for proper nouns and important '
        'concepts. Cite the source inline with [1] markers placed right '
        'after the claims they support. Do NOT begin with "Here is a '
        'summary..." or "This document...". Just open with the topic.'
    )
    prompt = (
        f'## Source: {source.get("name", "")}\n\n{snippet}\n\n'
        '## Task\nWrite the opening summary now.'
    )
    try:
        answer = llm.ask(prompt, system=system).strip()
    except Exception as e:
        logger.warning(f'generate_first_source_summary failed: {e}')
        answer = (f"Added **{source.get('name', 'source')}**. Ask anything "
                  'about it.')
    followups = suggest_followups_after(answer, source.get('name', ''))
    return {
        'role': 'assistant',
        'content': answer,
        'citations': citations,
        'used_chunk_ids': [1],
        'followups': followups,
    }


# Token-conservation toggle. False = no auto-follow-up suggestions
# (saves ~3k tokens per chat turn × N turns per session). Users still
# get follow-up suggestions if they type questions naturally; the auto-
# chip generation just isn't burning tokens every turn.
ENABLE_AUTO_FOLLOWUPS = False


def suggest_followups_after(answer: str, context_hint: str = '') -> list[str]:
    """Generate 3 short follow-up questions a user might ask after seeing
    `answer`. Returned as a list of plain question strings (no markers,
    no numbering). Empty list on failure — caller renders 0 chips.

    Disabled by default via ENABLE_AUTO_FOLLOWUPS=False — Balanced mode
    skips this to conserve free-tier LLM tokens."""
    if not ENABLE_AUTO_FOLLOWUPS:
        return []
    system = (
        'You generate 3 short follow-up questions a curious user might ask '
        'after reading the assistant message provided. Output EXACTLY 3 '
        'questions, one per line, no numbering, no bullets, no commentary. '
        'Questions should be specific and answerable from the source '
        'material — not vague. Max 90 characters each.'
    )
    body = answer[:3000]
    if context_hint:
        body = f'Context source: {context_hint}\n\n' + body
    try:
        out = llm.ask(body + '\n\n3 follow-up questions:', system=system).strip()
    except Exception as e:
        logger.debug(f'suggest_followups failed: {e}')
        return []
    lines = []
    for raw in out.split('\n'):
        s = raw.strip().lstrip('-*•').lstrip('0123456789.) ').strip()
        if s and len(s) > 10 and s.endswith('?'):
            lines.append(s[:120])
        if len(lines) >= 3:
            break
    return lines


_ARTIFACT_PROMPTS = {
    'summary': (
        'You are summarising a collection of research sources. Read all the '
        'sources below and produce a clear, well-structured Markdown summary '
        'covering the main themes, key findings, and important details. '
        'Use headings (##), bullet points, and bold for emphasis. Aim for '
        '300-700 words depending on the depth of the material.'
    ),
    'faq': (
        'You are generating an FAQ from a collection of research sources. '
        'Identify 6-10 of the most important questions someone would ask '
        'about this material, then answer each one using only the sources. '
        'Format as Markdown with a "## Q: ..." line followed by the answer '
        'in plain paragraphs.'
    ),
    'study_guide': (
        'You are creating a study guide from a collection of research '
        'sources. Produce a Markdown document with: (1) a short overview, '
        '(2) "Key Concepts" as a bulleted glossary, (3) "Main Takeaways" as '
        'a numbered list, (4) "Suggested Review Questions" — open-ended '
        'questions a student should be able to answer after reading. '
        'Use only the provided sources.'
    ),
    'timeline': (
        'You are extracting a chronological timeline from a collection of '
        'research sources. List every dated event you find, in order from '
        'earliest to most recent. Format as a Markdown bulleted list, one '
        'event per line: "- **<date>** — <description>". If the sources '
        'have no dates, say so clearly and explain what the material does '
        'cover instead.'
    ),
}


def generate_artifact(nb_id: str, kind: str, *,
                      progress_cb: Callable[[str], None] | None = None) -> str:
    """Run an artifact generator over ALL of the notebook's source text.
    Returns Markdown. Persists under artifacts/<kind>.md so the UI can
    re-display without re-running the LLM call."""
    if kind not in _ARTIFACT_PROMPTS:
        raise ValueError(f'Unknown artifact kind: {kind}')
    _log = lambda m: (progress_cb(m) if progress_cb else logger.info(m))

    sources_list = storage.list_sources(nb_id)
    if not sources_list:
        return '*No sources in this doc set yet.*'

    # Concatenate all source text up to a generous budget. Artifacts work
    # best when the model sees the whole picture, so we use a bigger
    # context budget than the chat path.
    _CONTEXT_BUDGET = 60_000
    parts = []
    total = 0
    for s_meta in sources_list:
        src = storage.get_source(nb_id, s_meta['id'])
        if src is None:
            continue
        chunk = src['text']
        if total + len(chunk) > _CONTEXT_BUDGET:
            chunk = chunk[: max(0, _CONTEXT_BUDGET - total)]
        if not chunk:
            break
        parts.append(f'## Source: {src["name"]}\n\n{chunk}\n')
        total += len(chunk)
        if total >= _CONTEXT_BUDGET:
            break

    body = '\n---\n'.join(parts)
    system = _ARTIFACT_PROMPTS[kind]
    prompt = (
        f'## Sources\n\n{body}\n\n'
        '## Task\nProduce the requested document based on the sources above.'
    )

    _log(f'Generating {kind}…')
    out = llm.ask(prompt, system=system)
    storage.save_artifact(nb_id, kind, out)
    return out
