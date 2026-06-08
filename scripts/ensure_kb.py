"""ensure_kb.py — ingest the KB corpus into pgvector ONCE on a fresh deploy, then skip on later boots.
Checks kb_chunk for the given kb_version; if rows already exist it no-ops (ingest is expensive — it
embeds 175 chunks through the local BGE model). Otherwise runs ingest_corpus (real embedder, baked
into the image / offline). Used by docker/entrypoint.sh after migrations, before uvicorn starts.
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from src.kb.ingest import ingest_corpus, DEFAULT_KB_VERSION


async def main() -> None:
    kb_version = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KB_VERSION
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ensure_kb: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    # Count first (table may not exist yet on a truly fresh DB — treat that as "needs ingest").
    existing = 0
    conn = await asyncpg.connect(url)
    try:
        existing = await conn.fetchval(
            "SELECT count(*) FROM kb_chunk WHERE kb_version = $1", kb_version
        )
    except asyncpg.UndefinedTableError:
        existing = 0
    finally:
        await conn.close()
    if existing and int(existing) > 0:
        print(f"ensure_kb: kb_version={kb_version!r} already has {existing} chunks — skipping ingest")
        return
    print(f"ensure_kb: ingesting kb_version={kb_version!r} (fresh DB) ...", flush=True)
    n = await ingest_corpus(kb_version=kb_version)  # real BGE embedder (baked, offline)
    print(f"ensure_kb: ingested {n} chunks into kb_chunk (kb_version={kb_version!r})")


if __name__ == "__main__":
    asyncio.run(main())
