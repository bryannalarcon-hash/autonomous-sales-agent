# KB package (plan U5) — the authored, versioned knowledge base + grounded RAG.
# Re-exports the embedding seam (EmbeddingModel protocol + FakeEmbedder / SentenceTransformer-
# Embedder, both 384-dim) eagerly (no heavy deps — sentence-transformers is lazy-imported inside),
# and LAZILY forwards the retriever API (ingest / retrieve / grounded / Chunk) so importing the
# package for embeddings or content listing never drags in asyncpg until the DB-backed retriever
# is actually used. Retrieval is intent-gated and called BEFORE generation — NOT an LLM tool.
# No LiveKit imports here.
from __future__ import annotations

from typing import Any

from src.kb.embeddings import (
    DEFAULT_DIM,
    DEFAULT_MODEL,
    EmbeddingModel,
    FakeEmbedder,
    SentenceTransformerEmbedder,
)

# Names served lazily from src.kb.retriever (kept out of import-time so embeddings-only / content-
# listing consumers don't require asyncpg). Resolved on first attribute access (PEP 562).
_RETRIEVER_EXPORTS = (
    "Chunk",
    "GroundednessReport",
    "ingest",
    "retrieve",
    "grounded",
    "list_objection_slugs",
    "objection_slugs_with_content",
    "CONTENT_DIR",
)


def __getattr__(name: str) -> Any:
    """Lazily forward retriever-API names to src.kb.retriever on first access (PEP 562)."""
    if name in _RETRIEVER_EXPORTS:
        from src.kb import retriever

        return getattr(retriever, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # embeddings
    "DEFAULT_DIM",
    "DEFAULT_MODEL",
    "EmbeddingModel",
    "FakeEmbedder",
    "SentenceTransformerEmbedder",
    # retriever (lazy)
    "Chunk",
    "GroundednessReport",
    "ingest",
    "retrieve",
    "grounded",
    "list_objection_slugs",
    "objection_slugs_with_content",
    "CONTENT_DIR",
]
