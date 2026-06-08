# Dockerfile — API service image for Railway (FastAPI brain + local BGE embedder + KB).
# Python 3.10-slim base; torch CPU wheel installed first (avoids the CUDA bloat sentence-transformers
# would otherwise pull); the project installs with its [rag] extra (sentence-transformers); the BGE
# model is baked at build time so runtime is fully offline (HF_HUB_OFFLINE=1). entrypoint.sh applies
# idempotent migrations + a one-time KB ingest, then runs uvicorn on Railway's $PORT. The Next.js
# dashboard is a SEPARATE service (web/, nixpacks) — not built here. NO LiveKit/voice deps.
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

# 1) torch CPU first, PINNED to the version that works locally (unpinned/CPU-index torch resolved
#    older than transformers expected → "module 'torch' has no attribute 'float8_e8m0fnu'").
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.12.0

# 2) project (base deps) + the ML stack PINNED to the locally-verified set so the version skew can't
#    recur. torch is already satisfied (2.12.0), so these don't re-pull it.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . "sentence-transformers==5.5.1" "transformers==5.9.0"

# 3) bake the embedder model into the image so the first live turn / KB ingest is offline + instant.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

# 4) runtime assets: migrations, the startup scripts, the config (champion_v0.yaml).
COPY migrations ./migrations
COPY scripts ./scripts
COPY docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

EXPOSE 8000
CMD ["./entrypoint.sh"]
