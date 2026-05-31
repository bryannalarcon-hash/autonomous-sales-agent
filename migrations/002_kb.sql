-- 002_kb.sql — knowledge-base vector store for grounded RAG (plan U5, KTD4 pgvector).
-- Creates kb_chunk: one row per embedded KB snippet, tagged with kb_version so retrieval is
-- SCOPED to the version a turn pinned (plan R12 kb-pinning). embedding is vector(384) to match
-- the default embedder (BAAI/bge-small-en-v1.5 / FakeEmbedder, both 384-dim). source carries the
-- citation pointer (file#section) the retriever returns for groundedness.
-- Idempotent: safe to re-run (CREATE EXTENSION/TABLE/INDEX ... IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- kb_chunk — embedded KB snippets, versioned. The retriever vector-searches
-- WITHIN one kb_version (a turn pins which KB it ran against). id is the stable
-- chunk key (source + ordinal) so re-ingesting the same content is an upsert.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kb_chunk (
    id          TEXT PRIMARY KEY,
    kb_version  TEXT         NOT NULL,
    source      TEXT         NOT NULL,
    text        TEXT         NOT NULL,
    embedding   vector(384)  NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Scope filter: every retrieval is WHERE kb_version = $pinned, so this index backs the hot path.
CREATE INDEX IF NOT EXISTS ix_kb_chunk_version ON kb_chunk (kb_version);

-- Approximate-NN index on the embedding for fast top-k cosine search. HNSW with
-- vector_cosine_ops matches the retriever's `<=>` cosine-distance query. HNSW is chosen over
-- ivfflat because it gives high recall on a SMALL dataset out of the box: ivfflat with the
-- default single-list probe (ivfflat.probes=1) examines only ~1/lists of the vectors and misses
-- candidates when the KB is only a few dozen chunks, whereas HNSW does not partition into lists.
CREATE INDEX IF NOT EXISTS ix_kb_chunk_embedding
    ON kb_chunk USING hnsw (embedding vector_cosine_ops);
