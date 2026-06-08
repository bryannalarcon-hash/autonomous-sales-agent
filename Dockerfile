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

# 1) torch CPU first (the heavy transitive dep) — pin to the CPU index so no CUDA wheels are pulled.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

# 2) project deps + the rag extra (sentence-transformers). Copy only what the install needs first
#    so this layer caches across source-only changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[rag]"

# 3) bake the embedder model into the image so the first live turn / KB ingest is offline + instant.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

# 4) runtime assets: migrations, the startup scripts, the config (champion_v0.yaml).
COPY migrations ./migrations
COPY scripts ./scripts
COPY docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

EXPOSE 8000
CMD ["./entrypoint.sh"]
