# Live RAG wiring (plan U5 / R43) — the shared, self-contained retrieve hook that turns the
# DB-free VoiceSession seam (src.voice.session.RetrieveHook) into a real pgvector lookup. Both the
# text demo API (src.api.server.create_app) AND the LiveKit voice worker import build_live_retrieve_hook
# so the two channels run the SAME grounded retrieval against the SAME kb_chunk store (plan R26 single
# shape). The hook closes over an embedder + kb_version + k and, per turn, calls retriever.retrieve(...)
# and returns the cited Chunks (which str() to "[source] text") straight into respond()'s grounding
# block. Async (the store is asyncpg-backed); the session awaits it. NO LiveKit imports here.
from __future__ import annotations

from typing import List

from src.kb import retriever
from src.kb.embeddings import EmbeddingModel
from src.voice.session import RetrieveHook

# Default KB version the live agent retrieves against (matches config/versions/*.yaml kb_version and
# the ingested corpus). Callers pin a different version per-turn by passing kb_version into the hook.
DEFAULT_KB_VERSION = "kb_v0"
DEFAULT_K = 4


def build_live_retrieve_hook(
    embedder: EmbeddingModel,
    *,
    kb_version: str = DEFAULT_KB_VERSION,
    k: int = DEFAULT_K,
) -> RetrieveHook:
    """Build the live retrieve hook the VoiceSession threads into respond() (U5 grounded RAG).

    Returns an async callable with the RetrieveHook shape `(query, *, kb_version=..., k=...)` ->
    a list of cited Chunks. The closure binds the `embedder` (real SentenceTransformerEmbedder in
    prod, FakeEmbedder in tests) and the default `kb_version`/`k`; a caller (the session, which pins
    the running kb_version per turn) may override them per call. Each invocation runs
    retriever.retrieve against the shared pgvector store and returns the Chunks ordered closest-first
    — they str() to "[source] text", so respond()'s NLG grounding block cites them and
    retriever.grounded() can check the answer against real chunk text.

    Self-contained + importable: the LiveKit voice worker imports this same factory so text and voice
    ground identically. The default kb_version is captured here so an `embedder`-only call still works.
    """
    default_version = kb_version
    default_k = k

    async def _hook(
        query: str,
        *,
        kb_version: str = default_version,
        k: int = default_k,
    ) -> List[retriever.Chunk]:
        # The session passes the turn-pinned kb_version (it can be "" before a config stamp); fall
        # back to the bound default so an unstamped session still retrieves real grounding.
        version = kb_version or default_version
        return await retriever.retrieve(query, kb_version=version, embedder=embedder, k=k)

    return _hook
