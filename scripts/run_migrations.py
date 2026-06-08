"""run_migrations.py — apply every migrations/*.sql in lexical order via asyncpg at API startup.
All migrations are idempotent (CREATE ... IF NOT EXISTS / CREATE EXTENSION IF NOT EXISTS vector), so
re-running on each deploy is safe. Reads DATABASE_URL from the environment (Railway provides it as a
reference variable from the Postgres service). Used by docker/entrypoint.sh before the app boots.
"""
from __future__ import annotations

import asyncio
import glob
import os
import sys

import asyncpg


async def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("run_migrations: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    conn = await asyncpg.connect(url)
    try:
        for path in sorted(glob.glob("migrations/*.sql")):
            with open(path, encoding="utf-8") as fh:
                sql = fh.read()
            # asyncpg's simple-query protocol runs a multi-statement DDL script in one execute().
            await conn.execute(sql)
            print(f"run_migrations: applied {path}", flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
