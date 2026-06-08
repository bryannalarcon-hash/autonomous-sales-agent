# Dockerfile — API service image for Railway (FastAPI brain + local BGE embedder + KB).
# Python 3.10-slim base; torch CPU wheel installed first (avoids the CUDA bloat sentence-transformers
# would otherwise pull); the project installs with its [rag] extra (sentence-transformers); the BGE
# model is baked at build time so runtime is fully offline (HF_HUB_OFFLINE=1). entrypoint.sh applies
# idempotent migrations + a one-time KB ingest, then runs uvicorn on Railway's $PORT. The Next.js
# dashboard is a SEPARATE service (web/, nixpacks) — not built here. Includes livekit-api ONLY (for
# the /api/livekit/token mint path); the full LiveKit agents/plugins voice stack lives in the separate
# voice-worker service (Dockerfile.worker), not here.
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/app/.hf \
    PYTHONPATH=/app

WORKDIR /app

# 1) torch CPU first, PINNED to the locally-verified version. Use the `+cpu` local tag so torch comes
#    ONLY from the pytorch CPU index, but add PyPI as an extra index so torch's own deps (typing-
#    extensions, sympy, …) resolve from PyPI — the CPU-index-only mode chokes on the pytorch index's
#    `typing_extensions` wheel name normalization. Upgrade pip first (older pip is stricter on that).
RUN pip install --upgrade pip && \
    pip install "torch==2.12.0+cpu" \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url https://pypi.org/simple

# 2) project (base deps) + the ML stack PINNED to the locally-verified set so the version skew can't
#    recur. torch is already satisfied (2.12.0), so these don't re-pull it.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . "sentence-transformers==5.5.1" "transformers==5.9.0" "livekit-api==1.1.0"

# 3) bake the embedder model into the HF cache (HF_HOME) so runtime is offline + instant. The global
#    HF_HUB_OFFLINE=1 (for runtime) would block THIS download, so override the offline flags to 0 for
#    just this build step; runtime still loads from the baked /app/.hf cache.
RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 \
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

# 4) runtime assets: migrations, the startup scripts, the config (champion_v0.yaml).
COPY migrations ./migrations
COPY scripts ./scripts
COPY docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

EXPOSE 8000
CMD ["./entrypoint.sh"]
