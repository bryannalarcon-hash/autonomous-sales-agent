# CB-33/CB-73 test-DB isolation (project test fixtures). Two jobs, both keeping the shared dev Postgres
# (the one the operator dashboard reads) free of pytest pollution:
#   1. tag every live in_progress upsert a test makes with cohort "test" (via LIVE_PERSIST_COHORT) so
#      operate._is_active excludes it — a pytest run can NEVER flash a phantom "active" call on the
#      monitor (the CB-33 false alarm). This is set session-wide, autouse, before any test imports.
#   2. an autouse session teardown that snapshots the episode ids + version_lineage state BEFORE the run,
#      then after the run:
#        a) DELETEs ALL new non-live episodes the run created (CB-33 + CB-73): cohort='test', ep-esc-*,
#           coh-{uuid}/goldset-{uuid} per-test tags, held_out/training/etc. Any new row with cohort!='live'
#           is a test artifact (the app is not running during the suite). cohort='live' is never touched.
#        b) DELETEs test-created version_lineage rows (rows whose `version` was NOT present before the
#           run — v0-{uuid}/v1-{uuid} from test_version_lineage_and_champion + challenger rows, CB-73)
#        c) RESTOREs the is_champion flag for any pre-existing version_lineage row whose flag was
#           changed by the test run (_seed_champion+promote cycle demotes champion_v0; this restores it).
# EVENT LOOP: teardown uses asyncio.run() in a fresh loop. _force_fresh_pool() nulls the store's
# module-level pool (may be bound to a closed test loop) so get_pool() reconnects cleanly.
# HARD RULES: only new non-live rows deleted; is_champion only restored to pre-run state (never fabricated).
# DB-FREE SAFE: every DB touch is guarded on DATABASE_URL + asyncpg and swallows errors, so a DB-free
# unit run is completely unaffected. Collaborators: src.memory.store, src.api.persistence (LIVE_PERSIST_COHORT).
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

    await _force_fresh_pool()  # clear any orphaned pool bound to a prior event loop
    pool = await store.get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT episode_id FROM episode")
        return {r["episode_id"] for r in rows}
    finally:
        await _force_fresh_pool()


async def _lineage_snapshot() -> dict[str, bool]:
    """Snapshot the version_lineage table as {version: is_champion} so teardown can restore it.
    Capturing ALL pre-existing versions lets us both identify new rows (CB-73: delete them) and
    restore the is_champion flag for rows that were present before the run but had their flag changed
    (e.g. champion_v0 demoted by _seed_champion+promote in test_loop.py)."""
    from src.memory import store

    await _force_fresh_pool()
    pool = await store.get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT version, is_champion FROM version_lineage")
        return {r["version"]: bool(r["is_champion"]) for r in rows}
    finally:
        await _force_fresh_pool()


async def _force_fresh_pool():
    """Ensure the store's module-level pool is replaced with a fresh one for THIS event loop.

    When asyncio.run() creates a new event loop for teardown, the store may hold a pool bound to
    a now-closed prior event loop. Calling close() on that pool raises 'Event loop is closed'.
    We instead NULL out _POOL directly (skipping close on the orphaned pool) so get_pool() creates
    a fresh connection in the current loop. This is safe: the prior loop's pool connections were
    already torn down when pytest-asyncio closed that loop; we're just clearing the stale reference."""
    from src.memory import store as _store
    _store._POOL = None  # type: ignore[attr-defined]


async def _delete_test_artifacts(preexisting: set[str]) -> int:
    """DELETE episode rows the run CREATED that are unmistakable test artifacts, leaving every row that
    existed before the run untouched.

    Two tiers of deletion — both keyed on the preexisting snapshot so NO pre-run row is ever touched:
    1. Marker-based (original CB-33): rows that are NOT preexisting AND have an identifiable test-only
       marker: cohort='test' (LIVE_PERSIST_COHORT tag), episode_id LIKE 'ep-esc-%' (escalation parent),
       cohort LIKE 'coh-%' or 'goldset-%' (test_store.py per-test uuid cohorts, never in production).
    2. New-episode sweep (CB-73 addition): any episode whose id was NOT in the preexisting snapshot AND
       whose cohort is NOT 'live'. This safely catches held_out/training/other cohort episodes that
       test_store.py, test_selfplay.py, and test_persistence.py write without a test-only cohort tag.
       We exclude cohort='live' because a genuine voice/web call running concurrently writes cohort='live'
       and we must never delete a real in-flight call. No application writes non-live episodes during a
       test suite run (the app isn't running), so any new non-live episode IS a test artifact.

    escalation_log rows FK to episode ON DELETE CASCADE, so deleting the parent episode cleans the queue
    too. Returns the count deleted. Swallows nothing here — the caller guards/swallows."""
    from src.memory import store

    await _force_fresh_pool()
    pool = await store.get_pool()
    try:
        async with pool.acquire() as conn:
            # Fetch ALL current episode ids (with cohort) so we can apply both tiers.
            all_rows = await conn.fetch("SELECT episode_id, cohort FROM episode")
            new_artifacts: list[str] = []
            for r in all_rows:
                eid = r["episode_id"]
                cohort = r["cohort"] or ""
                if eid in preexisting:
                    continue  # NEVER delete a pre-run row
                # Tier 1: explicit test-only markers.
                marker_match = (
                    cohort == _TEST_COHORT
                    or eid.startswith("ep-esc-")
                    or cohort.startswith("coh-")
                    or cohort.startswith("goldset-")
                )
                # Tier 2: any new non-live episode (app not running during tests).
                non_live_new = cohort != "live"
                if marker_match or non_live_new:
                    new_artifacts.append(eid)

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
        await _force_fresh_pool()


async def _restore_lineage(pre_snapshot: dict[str, bool]) -> None:
    """CB-73: restore the version_lineage table to its pre-run state.

    Two operations in one transaction:
    1. DELETE every version_lineage row whose version key did NOT exist before the run — these are
       test-created rows (v0-{uuid8}/v1-{uuid8} from test_version_lineage_and_champion; the
       champion_v0__... challenger rows from loop promotion tests). Only new rows are deleted; no
       pre-existing row is ever touched by this DELETE.
    2. UPDATE the is_champion flag for any pre-existing row whose flag was mutated during the run.
       Specifically: test_loop.py's _seed_champion + promote() demotes champion_v0 to is_champion=False;
       we restore it (and any other pre-existing row) to its captured pre-run flag. We do NOT blindly
       set a single champion — we restore each row to exactly what it was, which is the only safe thing
       to do without knowing which row SHOULD be champion (the orchestrator handles that separately).
    Fully guarded — swallows nothing; the caller wraps in try/except."""
    from src.memory import store

    await _force_fresh_pool()
    pool = await store.get_pool()
    try:
        async with pool.acquire() as conn:
            # Step 1: find and delete rows that are new (not in pre_snapshot).
            current_rows = await conn.fetch("SELECT version FROM version_lineage")
            current_versions = {r["version"] for r in current_rows}
            new_versions = [v for v in current_versions if v not in pre_snapshot]
            if new_versions:
                await conn.execute(
                    "DELETE FROM version_lineage WHERE version = ANY($1::text[])", new_versions
                )

            # Step 2: restore is_champion for pre-existing rows whose flag changed.
            # Fetch the current state of pre-existing rows to find ones that differ.
            after_rows = await conn.fetch(
                "SELECT version, is_champion FROM version_lineage WHERE version = ANY($1::text[])",
                list(pre_snapshot.keys()),
            )
            to_fix = [
                (v, pre_snapshot[v])
                for r in after_rows
                for v in [r["version"]]
                if bool(r["is_champion"]) != pre_snapshot[v]
            ]
            # CRITICAL: the partial unique index allows only ONE is_champion=TRUE at a time.
            # Apply demotions (flag=False) first, then promotions (flag=True), so we never
            # transiently have two champion rows which would violate the constraint.
            for version, flag in sorted(to_fix, key=lambda x: x[1]):  # False before True
                await conn.execute(
                    "UPDATE version_lineage SET is_champion = $1 WHERE version = $2",
                    flag,
                    version,
                )
    finally:
        await _force_fresh_pool()


@pytest.fixture(scope="session", autouse=True)
def _cb33_cb73_cleanup_test_db_artifacts():
    """Session teardown: snapshot the episode ids + version_lineage state BEFORE the run, then after:
    (a) DELETE test-artifact episode rows (CB-33: cohort='test' + ep-esc-* shells);
    (b) DELETE test-created version_lineage rows + restore is_champion flags (CB-73).
    No-op when no DB is available (DB-free unit runs). Fully guarded — failures are printed + swallowed."""
    import asyncio
    import sys

    preexisting_episodes: set[str] = set()
    lineage_before: dict[str, bool] = {}
    if _db_available():
        try:
            preexisting_episodes = asyncio.run(_episode_ids())
            print(f"\n[CB-73 conftest] Pre-snapshot: {len(preexisting_episodes)} episodes", file=sys.stderr)
        except Exception as exc:  # pragma: no cover - DB hiccup must not block the run
            print(f"\n[CB-73 conftest] _episode_ids FAILED: {exc}", file=sys.stderr)
            preexisting_episodes = set()
        try:
            lineage_before = asyncio.run(_lineage_snapshot())
            print(f"[CB-73 conftest] Pre-snapshot: {len(lineage_before)} lineage rows", file=sys.stderr)
        except Exception as exc:  # pragma: no cover - DB hiccup must not block the run
            print(f"[CB-73 conftest] _lineage_snapshot FAILED: {exc}", file=sys.stderr)
            lineage_before = {}

    yield

    if _db_available():
        try:
            n = asyncio.run(_delete_test_artifacts(preexisting_episodes))
            print(f"\n[CB-73 conftest] Teardown: deleted {n} test episode artifacts", file=sys.stderr)
        except Exception as exc:  # pragma: no cover - teardown cleanup is best-effort
            print(f"\n[CB-73 conftest] _delete_test_artifacts FAILED: {exc}", file=sys.stderr)
        if lineage_before:
            try:
                asyncio.run(_restore_lineage(lineage_before))
                print(f"[CB-73 conftest] Teardown: lineage restore complete", file=sys.stderr)
            except Exception as exc:  # pragma: no cover - teardown cleanup is best-effort
                print(f"[CB-73 conftest] _restore_lineage FAILED: {exc}", file=sys.stderr)
