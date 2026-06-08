#!/bin/sh
# entrypoint.sh — API container boot: apply idempotent migrations, ensure the KB is ingested once,
# then launch uvicorn on Railway's $PORT. All steps are safe to re-run on every deploy/restart.
set -e
echo "[entrypoint] applying migrations..."
python scripts/run_migrations.py
echo "[entrypoint] ensuring KB ingested (kb_v0)..."
python scripts/ensure_kb.py kb_v0
echo "[entrypoint] starting API on port ${PORT:-8000}..."
exec python -m uvicorn src.api.server:app --host 0.0.0.0 --port "${PORT:-8000}"
