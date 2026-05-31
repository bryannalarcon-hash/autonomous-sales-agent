# Grounded RAG over the authored KB (plan U5, KTD4 pgvector). Three responsibilities:
#   ingest(embedder, kb_version)  — chunk src/kb/content/*.json, embed, UPSERT into kb_chunk
#                                   tagged with kb_version (idempotent; re-ingest replaces).
#   retrieve(query, ...)          — async cosine top-k vector search SCOPED to one kb_version
#                                   (a turn pins which KB it ran against — plan R12); returns
#                                   cited Chunks (source + text) for the NLG grounding block.
#   grounded(answer, chunks)      — deterministic groundedness check: does the answer's content
#                                   stay supported by the retrieved chunks? Backs the U9 guardrail.
# Retrieval is intent-gated and called BEFORE generation by the adapter — it is a plain function,
# NOT an LLM tool-call. Reuses src.memory.store's asyncpg pool. NO LiveKit imports.
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Sequence

from src.kb.embeddings import EmbeddingModel
from src.memory import store

# Where the authored KB lives. Every *.json under here (except _meta) is ingestible content.
CONTENT_DIR = Path(__file__).parent / "content"

# Words ignored when checking groundedness — function words carry no factual claim, so requiring
# them to appear in the KB would make every answer "ungrounded". Kept small + deterministic.
_STOPWORDS = frozenset(
    """
    a an the and or but if then else of to in on at for with without from by as is are was were be
    been being do does did this that these those it its their our your my his her you we they i me us
    them he she him so not no yes can could would should will shall may might must have has had into
    about over under up down out off than too very just also more most some any each per about your
    over only about what when where which who whom how why here there now today get got make made take
    """.split()
)

# Numeric / money tokens are the riskiest invented facts (prices, guarantees, windows), so they are
# checked strictly: any number-bearing token in the answer must be backed by the chunks.
_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-']+")


@dataclass
class Chunk:
    """One retrieved KB snippet with its citation (plan U5).

    `source` is the citation pointer (file#section_id) the agent cites and groundedness checks
    against; `text` is the snippet; `score` is cosine SIMILARITY (1 - distance), higher = closer.
    `kb_version` records which KB version this chunk came from (the pinned scope).
    """

    id: str
    kb_version: str
    source: str
    text: str
    score: float = 0.0

    def __str__(self) -> str:
        # Rendered into the NLG grounding block; lead with the citation so the source is explicit.
        return f"[{self.source}] {self.text}"


# ---------------------------------------------------------------------------
# Chunking the authored content
# ---------------------------------------------------------------------------
@dataclass
class _RawChunk:
    chunk_id: str
    source: str
    text: str


def _load_content_chunks() -> List[_RawChunk]:
    """Read every content/*.json and flatten its `sections` into one chunk per section.

    Each section already carries a stable `id` and a self-contained `text`; we use that as the
    chunk granularity (one fact-bearing paragraph per chunk) so a retrieved chunk is directly
    citable. `source` is "<file_stem>#<section_id>"; `chunk_id` namespaces it under the file.
    """
    chunks: List[_RawChunk] = []
    for path in sorted(CONTENT_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        stem = path.stem
        for section in data.get("sections", []) or []:
            sid = str(section.get("id") or "")
            title = str(section.get("title") or "")
            body = str(section.get("text") or "")
            if not sid or not body:
                continue
            # Prepend the title so the embedding and the cited text both carry the topic label.
            text = f"{title}. {body}" if title else body
            chunks.append(
                _RawChunk(chunk_id=f"{stem}:{sid}", source=f"{stem}#{sid}", text=text)
            )
    return chunks


def list_objection_slugs() -> List[str]:
    """The declared nine-objection taxonomy from objections.json (used by the coverage test)."""
    path = CONTENT_DIR / "objections.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("objection_taxonomy", []) or [])


def objection_slugs_with_content() -> List[str]:
    """Objection slugs that actually have a rebuttal SECTION present in objections.json.

    The coverage test asserts this equals the declared taxonomy — i.e. all nine objections have
    grounded rebuttal content, none merely declared.
    """
    path = CONTENT_DIR / "objections.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    slugs: List[str] = []
    for section in data.get("sections", []) or []:
        slug = section.get("objection")
        if slug and str(section.get("text") or "").strip():
            slugs.append(str(slug))
    return slugs


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
async def ingest(embedder: EmbeddingModel, kb_version: str) -> int:
    """Chunk content/, embed, and UPSERT into kb_chunk tagged with kb_version. Returns count.

    Idempotent per (kb_version): the chunk id is namespaced by kb_version so re-ingesting the same
    version overwrites its rows rather than duplicating them, and different versions coexist so
    retrieval can be scoped (plan R12 kb-pinning). Embedding happens through the injected
    `embedder` (FakeEmbedder in tests, SentenceTransformerEmbedder in prod), so ingest is offline
    and deterministic under the fake.
    """
    raw = _load_content_chunks()
    if not raw:
        return 0
    vectors = embedder.embed([c.text for c in raw])
    if len(vectors) != len(raw):
        raise RuntimeError("embedder returned a different number of vectors than chunks")

    pool = await store.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for c, vec in zip(raw, vectors):
                row_id = f"{kb_version}:{c.chunk_id}"
                await conn.execute(
                    """
                    INSERT INTO kb_chunk (id, kb_version, source, text, embedding)
                    VALUES ($1, $2, $3, $4, $5::vector)
                    ON CONFLICT (id) DO UPDATE SET
                        kb_version = EXCLUDED.kb_version,
                        source = EXCLUDED.source,
                        text = EXCLUDED.text,
                        embedding = EXCLUDED.embedding
                    """,
                    row_id,
                    kb_version,
                    c.source,
                    c.text,
                    _to_vector_literal(vec),
                )
    return len(raw)


# ---------------------------------------------------------------------------
# Retrieve (scoped to kb_version)
# ---------------------------------------------------------------------------
async def retrieve(
    query: str,
    *,
    kb_version: str,
    embedder: EmbeddingModel,
    k: int = 4,
) -> List[Chunk]:
    """Cosine top-k vector search SCOPED to one kb_version (plan U5, R12).

    The query is embedded through the same `embedder` used at ingest, then matched with pgvector's
    cosine distance operator (`<=>`) against ONLY the rows tagged with the pinned kb_version, so a
    different version returns nothing. Returns cited Chunks ordered closest-first with a cosine
    similarity score. This is called BEFORE generation by the adapter (intent-gated) — it is a
    plain function, not an LLM tool.
    """
    if k <= 0:
        return []
    qvec = embedder.embed([query])[0]
    pool = await store.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, kb_version, source, text,
                   1 - (embedding <=> $1::vector) AS score
            FROM kb_chunk
            WHERE kb_version = $2
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            _to_vector_literal(qvec),
            kb_version,
            k,
        )
    return [
        Chunk(
            id=r["id"],
            kb_version=r["kb_version"],
            source=r["source"],
            text=r["text"],
            score=float(r["score"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Groundedness check (deterministic)
# ---------------------------------------------------------------------------
@dataclass
class GroundednessReport:
    """The result of grounded(): is the answer supported, and which tokens were unsupported.

    `grounded` is the boolean the guardrail reads; `unsupported_numbers` and `unsupported_terms`
    name the offending tokens (an invented price, a claim absent from the KB) so the U9 grading /
    decision log can explain WHY an answer failed. Empty lists when fully supported.
    """

    grounded: bool
    unsupported_numbers: List[str]
    unsupported_terms: List[str]

    def __bool__(self) -> bool:
        return self.grounded


def _support_text(chunks: Sequence[Any]) -> str:
    """Concatenate the supporting text from retrieved chunks (accepts Chunk or plain str)."""
    parts: List[str] = []
    for c in chunks:
        if isinstance(c, Chunk):
            parts.append(c.text)
        else:
            parts.append(str(c))
    return " ".join(parts).lower()


def _normalize_number(tok: str) -> str:
    """Strip $/%/commas so '$199' and '199' compare equal against KB support text."""
    return tok.replace("$", "").replace(",", "").replace("%", "")


def _stem(word: str) -> str:
    """Crude deterministic stem: lowercase + drop a common inflectional suffix.

    Not a real stemmer — just enough that tier/tiered/tiers, bundle/bundling, month/monthly all
    collapse to a shared prefix so groundedness matches inflected surface forms in the KB without
    a dependency. Keeps a 4-char floor so short words are left intact.
    """
    w = word.lower()
    for suffix in ("ing", "ned", "ged", "ly", "es", "ed", "s", "y", "e"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def _term_supported(term: str, support_stems: frozenset[str]) -> bool:
    """A term is supported if its stem matches (or shares a >=4-char prefix with) a support stem."""
    s = _stem(term)
    if s in support_stems:
        return True
    # Prefix overlap both directions handles residual inflection the crude stemmer misses.
    return any(
        (len(s) >= 4 and ss.startswith(s)) or (len(ss) >= 4 and s.startswith(ss))
        for ss in support_stems
    )


def grounded(
    answer: str,
    chunks: Sequence[Any],
    *,
    min_term_support: float = 0.6,
) -> GroundednessReport:
    """Deterministic groundedness check: is the answer supported by the retrieved chunks?

    Two-part check (plan U5 / R8 — the agent states no fact absent from the KB):
      1. NUMBERS are strict — every number/money/percent token in the answer (prices, windows,
         guarantees) must appear in the support text. An invented figure ($299, 30 days) fails.
      2. CONTENT WORDS must be MOSTLY supported — at least `min_term_support` of the non-stopword
         terms in the answer must have a (stem-matched) counterpart in the support text. Stemming
         lets inflected surface forms (tier/tiered/tiers, month/monthly) match, so ordinary NLG
         phrasing passes while a wholly off-KB claim (an invented feature) still fails.

    With no chunks, any answer containing a content word is treated as ungrounded. Returns a
    GroundednessReport (truthy when grounded) naming the unsupported tokens for the decision log.
    """
    support = _support_text(chunks)

    # 1. Numbers must be backed exactly.
    unsupported_numbers: List[str] = []
    support_numbers = {
        _normalize_number(m) for m in _NUMBER_RE.findall(support)
    }
    for tok in _NUMBER_RE.findall(answer):
        if _normalize_number(tok) not in support_numbers:
            unsupported_numbers.append(tok)

    # 2. Content words must be mostly present (stem-matched against the support vocabulary).
    support_stems = frozenset(_stem(w) for w in _WORD_RE.findall(support))
    terms = [
        w.lower()
        for w in _WORD_RE.findall(answer)
        if w.lower() not in _STOPWORDS
    ]
    unsupported_terms = [t for t in terms if not _term_supported(t, support_stems)]
    if terms:
        supported_ratio = 1.0 - (len(unsupported_terms) / len(terms))
    else:
        supported_ratio = 1.0  # an answer with no content words makes no factual claim

    is_grounded = (
        not unsupported_numbers and supported_ratio >= min_term_support
    )
    return GroundednessReport(
        grounded=is_grounded,
        unsupported_numbers=unsupported_numbers,
        # Only surface the unsupported terms when the answer actually failed the ratio bar, so a
        # passing answer reports a clean (empty) term list even with incidental connective words.
        unsupported_terms=(
            unsupported_terms if supported_ratio < min_term_support else []
        ),
    )


# ---------------------------------------------------------------------------
# pgvector literal helper
# ---------------------------------------------------------------------------
def _to_vector_literal(vec: Sequence[float]) -> str:
    """Render a vector as the pgvector text literal '[a,b,c]'.

    Sent as a string and cast in SQL with `::vector`, so the KB layer needs no binary codec
    registered on the store's shared pool (the memory schema has no vector column). pgvector
    parses this text representation for both the stored column value and the `<=>` operands.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
