# Embedding models for the KB RAG layer (plan U5). Defines the EmbeddingModel protocol
# (embed(texts) -> list[vector] + dim) and two implementations: SentenceTransformerEmbedder
# (real, model from EMBEDDING_MODEL env, default BAAI/bge-small-en-v1.5 @ 384-dim; sentence-
# transformers is LAZY-IMPORTED so importing this module never requires the package) and
# FakeEmbedder (DETERMINISTIC unit-norm vectors from text hashes — no model download, no network,
# for tests). Both produce 384-dim vectors by default so they share migrations/002_kb.sql's
# vector(384). No DB and no LiveKit imports here.
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import List, Protocol, Sequence, runtime_checkable

# Default embedding model + its dimensionality. bge-small-en-v1.5 emits 384-dim vectors, which is
# what migrations/002_kb.sql's vector(384) column and FakeEmbedder are both sized to.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384

Vector = List[float]


@runtime_checkable
class EmbeddingModel(Protocol):
    """The seam the retriever depends on: embed a batch of texts to fixed-dim vectors.

    Implementations must return one vector per input text, each of length `dim`. The retriever
    embeds KB chunks at ingest and the query at retrieval through this same interface, so the
    real model and the test fake are drop-in interchangeable.
    """

    @property
    def dim(self) -> int:  # pragma: no cover - structural
        ...

    def embed(self, texts: Sequence[str]) -> List[Vector]:  # pragma: no cover - structural
        ...


def _normalize(vec: List[float]) -> List[float]:
    """Scale a vector to unit L2 norm so cosine distance is a clean dot-product comparison."""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    inv = 1.0 / norm
    return [v * inv for v in vec]


_TOKEN_RE = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Deterministic, dependency-free embedder for tests (plan U5 — no download, no network).

    A feature-hashing (hashing-vectorizer) embedding: text is lowercased + tokenized, each token
    is hashed to a lane index (and a sign) and accumulated, then the vector is L2-normalized. This
    gives two properties the tests need: (1) DETERMINISM — identical text always yields the
    identical vector, with no model download or network; and (2) a weak LEXICAL signal — texts that
    SHARE tokens land closer in cosine space, so "a price query retrieves the pricing chunk" is a
    meaningful, reproducible assertion rather than coincidence. It is NOT a semantic model
    (synonyms are not related); it is a deterministic lexical stand-in for sentence-transformers.
    """

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        if dim <= 0:
            raise ValueError("FakeEmbedder dim must be positive")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _token_lane(self, token: str) -> tuple[int, float]:
        """Hash a token to a (lane_index, sign) pair — stable across runs (md5 digest -> ints)."""
        digest = hashlib.md5(token.encode("utf-8")).digest()
        lane = int.from_bytes(digest[:4], "big") % self._dim
        sign = 1.0 if digest[4] & 1 else -1.0
        return lane, sign

    def _vector(self, text: str) -> Vector:
        vec = [0.0] * self._dim
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            # No tokens -> derive a stable nonzero vector from the raw text so empty/punctuation
            # inputs still embed deterministically rather than collapsing to all-zeros.
            tokens = [hashlib.sha256(text.encode("utf-8")).hexdigest()]
        for tok in tokens:
            lane, sign = self._token_lane(tok)
            vec[lane] += sign
        return _normalize(vec)

    def embed(self, texts: Sequence[str]) -> List[Vector]:
        return [self._vector(t) for t in texts]


class SentenceTransformerEmbedder:
    """Real embedder backed by sentence-transformers (model from EMBEDDING_MODEL env).

    The heavy `sentence_transformers` import and model load happen LAZILY on first embed() call,
    so importing this module (and the whole src.kb package) costs nothing and never requires the
    package — only actually embedding does. `dim` is read from the loaded model so it always
    matches the served vectors. Use this in production / when ingesting the real KB.
    """

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL)
        self._model = None  # loaded lazily
        self._dim = DEFAULT_DIM  # provisional until the model is loaded

    def _ensure_model(self) -> None:
        if self._model is None:
            # Lazy import: only needed when actually embedding, so the module import is free.
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._model = SentenceTransformer(self._model_name)
            self._dim = int(self._model.get_sentence_embedding_dimension())

    @property
    def dim(self) -> int:
        self._ensure_model()
        return self._dim

    def embed(self, texts: Sequence[str]) -> List[Vector]:
        self._ensure_model()
        assert self._model is not None
        # normalize_embeddings=True so cosine distance behaves; tolist() -> plain Python floats
        # for the asyncpg/pgvector codec.
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True
        )
        return [list(map(float, row)) for row in vectors]
