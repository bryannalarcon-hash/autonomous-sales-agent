#!/usr/bin/env python3
# scripts/seed_demo.py — idempotent demo-DATA curation for the operator dashboard. The demo DB is the
# organic output of self-play + live calls, so a few surfaces showed junk: 121 escalations created by
# in-progress live-call debugging (raw gate-rationale reason, NO trigger moment, pointing at 0-turn
# calls), and episodes missing a duration. This script (1) PURGES those junk escalations, (2)
# REGENERATES realistic escalations on real COMPLETED+escalated episodes — varied reasons + a real
# quoted trigger moment pulled from the episode transcript + mixed lifecycle, and (3) backfills a
# plausible duration_ms (proportional to turn count) on episodes that lack one. Safe to re-run.
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# Clean reason keys that map to a human label in src/api/labels.py (no raw gate rationale strings).
_REASONS = ["human_request", "pricing_concession", "compliance", "concession_pressure"]
# Mostly unreviewed (so the queue has work) with some reviewed/resolved for lifecycle variety.
_LIFECYCLES = ["unreviewed", "unreviewed", "unreviewed", "unreviewed", "reviewed", "resolved"]
_N_ESCALATIONS = 48  # a believable review-queue size referencing real escalated calls


def _turns(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


def _as_turn_id(raw) -> int:
    """escalation_log.turn_id is an INTEGER column; coerce a turn's id to int (0 on anything odd)."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _trigger_moment(turns: list) -> tuple[str, int]:
    """Pick a real prospect line from the transcript as the escalation trigger moment + its turn id."""
    for t in reversed(turns):
        if isinstance(t, dict) and t.get("speaker") in ("prospect", "user") and (t.get("text") or "").strip():
            return f"“{str(t['text']).strip()[:160]}”", _as_turn_id(t.get("turn_id"))
    # Fall back to any non-empty line.
    for t in reversed(turns):
        if isinstance(t, dict) and (t.get("text") or "").strip():
            return f"“{str(t['text']).strip()[:160]}”", _as_turn_id(t.get("turn_id"))
    return "Prospect asked to speak with a human advisor.", 0


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ABORT: DATABASE_URL not set")
        return 2
    c = await asyncpg.connect(url)
    try:
        # 1. Purge the junk escalations (raw gate-rationale reason OR no trigger moment).
        purged = await c.execute(
            "DELETE FROM escalation_log WHERE reason LIKE '%deferring to a specialist%' "
            "OR coalesce(moment,'') = ''"
        )
        print(f"purged junk escalations: {purged}")

        # 2. Regenerate realistic escalations on real completed+escalated episodes.
        eps = await c.fetch(
            "SELECT episode_id, turns FROM episode WHERE outcome = 'escalated' "
            "ORDER BY created_at DESC LIMIT $1",
            _N_ESCALATIONS,
        )
        now = datetime.now(timezone.utc)
        made = 0
        for i, ep in enumerate(eps):
            moment, turn_id = _trigger_moment(_turns(ep["turns"]))
            reason = _REASONS[i % len(_REASONS)]
            lifecycle = _LIFECYCLES[i % len(_LIFECYCLES)]
            esc_id = f"esc-{ep['episode_id'].split('-')[-1][:8]:>08}"[:12]
            created = now - timedelta(hours=i * 3)  # spread over recent history, newest first
            await c.execute(
                "INSERT INTO escalation_log (escalation_id, episode_id, reason, moment, turn_id, "
                "lifecycle, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (escalation_id) DO UPDATE SET reason=EXCLUDED.reason, "
                "moment=EXCLUDED.moment, turn_id=EXCLUDED.turn_id, lifecycle=EXCLUDED.lifecycle",
                esc_id, ep["episode_id"], reason, moment, turn_id, lifecycle, created,
            )
            made += 1
        print(f"regenerated escalations: {made} (reasons varied, real trigger moments)")

        # 3. Backfill a plausible duration_ms (proportional to turns) where missing.
        backfilled = await c.execute(
            "UPDATE episode SET metrics = coalesce(metrics, '{}'::jsonb) || "
            "jsonb_build_object('duration_ms', "
            "  greatest(20000, jsonb_array_length(coalesce(turns,'[]'::jsonb)) * 24000)) "
            "WHERE (metrics->>'duration_ms') IS NULL "
            "AND jsonb_array_length(coalesce(turns,'[]'::jsonb)) > 0"
        )
        print(f"backfilled durations: {backfilled}")

        # Report the resulting escalation lifecycle distribution.
        dist = await c.fetch("SELECT lifecycle, count(*) n FROM escalation_log GROUP BY lifecycle ORDER BY n DESC")
        print("escalation lifecycle:", {r["lifecycle"]: r["n"] for r in dist})
    finally:
        await c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
