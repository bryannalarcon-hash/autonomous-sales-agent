# KB corpus ingest entrypoint (plan U5) — makes the authored corpus REAL in pgvector so retrieval
# is grounded instead of empty. ingest_corpus() loads every src/kb/content/*.json (one chunk per
# program / price-tier / policy / competitor / objection section, title + body), embeds each through
# a REAL SentenceTransformerEmbedder by default (FakeEmbedder injectable for tests / offline), and
# UPSERTs into kb_chunk via retriever.ingest (idempotent per kb_version — re-ingest replaces the
# version's rows, never duplicates). It applies migrations/002_kb.sql first so the table+HNSW index
# exist on a clean DB. Run it directly: `PYTHONPATH=. python3 -m src.kb.ingest [kb_version]`. The
# chunking itself lives in retriever._load_content_chunks (single source of truth) — this module is
# the runnable wrapper that picks the embedder, ensures the schema, and reports the row count. The
# voice worker + demo API retrieve against exactly what this writes. NO LiveKit imports.
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

from src.kb import retriever
from src.kb.embeddings import EmbeddingModel, SentenceTransformerEmbedder
from src.memory import store

# The KB schema migration (kb_chunk vector(384) + HNSW). Applied (idempotently) before ingest so a
# fresh database has the table; CREATE EXTENSION/TABLE/INDEX ... IF NOT EXISTS make re-runs safe.
_KB_SQL = Path(__file__).resolve().parents[2] / "migrations" / "002_kb.sql"

DEFAULT_KB_VERSION = "kb_v0"


async def _apply_kb_migration() -> None:
    """Apply migrations/002_kb.sql so kb_chunk + its indexes exist before ingest (idempotent)."""
    sql = _KB_SQL.read_text(encoding="utf-8")
    pool = await store.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def ingest_corpus(
    *,
    kb_version: str = DEFAULT_KB_VERSION,
    embedder: Optional[EmbeddingModel] = None,
) -> int:
    """Ingest the authored KB corpus into kb_chunk for `kb_version`, returning the chunk count.

    Loads every src/kb/content/*.json, flattens it to one text chunk per section (title + body — done
    in retriever._load_content_chunks), embeds with `embedder` (defaults to the REAL
    SentenceTransformerEmbedder / BAAI/bge-small-en-v1.5 @ 384-dim; tests pass a FakeEmbedder so the
    path is offline + deterministic), and UPSERTs via retriever.ingest. Idempotent per kb_version:
    re-ingesting the same version overwrites its rows rather than duplicating. Applies the KB
    migration first so a fresh DB has the table + HNSW index. Returns the number of chunks written.
    """
    emb = embedder or SentenceTransformerEmbedder()
    await _apply_kb_migration()
    return await retriever.ingest(emb, kb_version)


async def _main() -> None:
    """`python3 -m src.kb.ingest [kb_version]` — ingest with the real embedder, print the count.

    Reports the live embedder (so the operator sees whether the real BGE model or the deterministic
    fake produced the vectors) and the resulting kb_chunk row count for the version.
    """
    kb_version = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KB_VERSION
    embedder = SentenceTransformerEmbedder()
    n = await ingest_corpus(kb_version=kb_version, embedder=embedder)
    # Verify by counting the persisted rows for this version (proves the write reached pgvector).
    pool = await store.get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM kb_chunk WHERE kb_version = $1", kb_version
        )
    print(f"ingested {n} chunks into kb_chunk (kb_version={kb_version!r}); row count now {count}")
    print(f"embedder: {type(embedder).__name__} (dim={embedder.dim})")
    await store.close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
