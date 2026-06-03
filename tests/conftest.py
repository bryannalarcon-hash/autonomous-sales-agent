# CB-33 test-DB isolation (project test fixtures). Two jobs, both keeping the shared dev Postgres
# (the one the operator dashboard reads) free of pytest pollution:
#   1. tag every live in_progress upsert a test makes with cohort "test" (via LIVE_PERSIST_COHORT) so
#      operate._is_active excludes it — a pytest run can NEVER flash a phantom "active" call on the
#      monitor (the CB-33 false alarm). This is set session-wide, autouse, before any test imports.
#   2. an autouse session teardown that snapshots the episode ids present BEFORE the run and DELETEs
#      only unmistakable test-artifact rows the run CREATED (cohort='test' rows + ep-esc-* shells) so
#      nothing accumulates across runs (the ~16-shells-from-3-runs growth observed in CB-33). It does
#      NOT delete on a bare in_progress match, so a real live call overlapping a test run is never lost.
# DB-FREE SAFE: every DB touch is guarded on DATABASE_URL + asyncpg and swallows errors, so a DB-free
# unit run (no Postgres, no asyncpg) is completely unaffected. Collaborators: src.memory.store,
# src.api.persistence (LIVE_PERSIST_COHORT), src.api.operate (_TEST_LIVE_COHORTS).
from __future__ import annotations

import os

import pytest

# The cohort tag CB-33 keys on. Kept in sync with src.api.operate._TEST_LIVE_COHORTS / the
# LIVE_PERSIST_COHORT default the DB-gated tests rely on.
_TEST_COHORT = "test"


@pytest.fixture(scope="session", autouse=True)
def _cb33_tag_live_upserts_as_test_cohort():
    """Set LIVE_PERSIST_COHORT=test for the whole session so any REAL persist_call_live a test drives
    (the live_rag default-path / demo-call e2e tests) writes its in_progress row under the "test"
    cohort — which operate._is_active excludes from the Live monitor. Restored on teardown so the env
    is left clean. Autouse + session-scoped so it is in place before the first test runs."""
    prior = os.environ.get("LIVE_PERSIST_COHORT")
    os.environ["LIVE_PERSIST_COHORT"] = _TEST_COHORT
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("LIVE_PERSIST_COHORT", None)
        else:
            os.environ["LIVE_PERSIST_COHORT"] = prior


def _db_available() -> bool:
    """True only when a real Postgres is reachable for cleanup: DATABASE_URL set AND asyncpg present.
    A DB-free unit run (no driver / no URL) returns False so the cleanup fixture is a pure no-op."""
    if not os.environ.get("DATABASE_URL"):
        return False
    try:
        import asyncpg  # noqa: F401
    except Exception:
        return False
    return True


async def _episode_ids() -> set[str]:
    """All episode ids currently in the DB (bounded high so a run's new rows are always captured)."""
    from src.memory import store

    await store.close_pool()  # bind a pool to THIS loop (pytest-asyncio loop hygiene)
    pool = await store.get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT episode_id FROM episode")
        return {r["episode_id"] for r in rows}
    finally:
        await store.close_pool()


async def _delete_test_artifacts(preexisting: set[str]) -> int:
    """DELETE episode rows the run CREATED that are unmistakable test artifacts, leaving every row that
    existed before the run untouched. A row is a deletable artifact when it is NOT pre-existing AND is
    one of: a test-cohort row (cohort='test' — every live in_progress shell a test drives is tagged
    this via LIVE_PERSIST_COHORT), or an ep-esc-* shell (the escalation-FK parent the DB-gated escalation
    test creates; real episodes never use that id prefix). We deliberately do NOT delete on a bare
    `outcome='in_progress'` match: a GENUINE human/voice call mid-flight on the shared dev DB is
    in_progress with cohort='live', and started-during-the-run, so a bare in_progress arm would DELETE a
    real live call (a data-loss footgun the cohort tag already makes unnecessary). escalation_log rows
    FK to episode ON DELETE CASCADE, so deleting the parent episode cleans the queue too. Returns the
    count deleted. Swallows nothing here — the caller guards/swallows so a teardown hiccup never fails
    the suite."""
    from src.memory import store

    await store.close_pool()
    pool = await store.get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT episode_id FROM episode
                WHERE cohort = $1
                   OR episode_id LIKE 'ep-esc-%'
                """,
                _TEST_COHORT,
            )
            new_artifacts = [r["episode_id"] for r in rows if r["episode_id"] not in preexisting]
            if not new_artifacts:
                return 0
            await conn.execute(
                "DELETE FROM escalation_log WHERE episode_id = ANY($1::text[])", new_artifacts
            )
            await conn.execute(
                "DELETE FROM episode WHERE episode_id = ANY($1::text[])", new_artifacts
            )
        return len(new_artifacts)
    finally:
        await store.close_pool()


@pytest.fixture(scope="session", autouse=True)
def _cb33_cleanup_test_db_artifacts():
    """Session teardown: snapshot the episode ids present BEFORE the run, then after the run DELETE any
    test-artifact rows the run created (see _delete_test_artifacts). No-op when no DB is available
    (DB-free unit runs). Fully guarded — a cleanup failure logs and is swallowed, never fails tests."""
    import asyncio

    preexisting: set[str] = set()
    if _db_available():
        try:
            preexisting = asyncio.run(_episode_ids())
        except Exception:  # pragma: no cover - DB hiccup must not block the run
            preexisting = set()

    yield

    if _db_available():
        try:
            asyncio.run(_delete_test_artifacts(preexisting))
        except Exception:  # pragma: no cover - teardown cleanup is best-effort
            pass
