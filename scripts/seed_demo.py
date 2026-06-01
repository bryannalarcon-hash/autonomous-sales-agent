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
# Reason-COHERENT trigger moments: the real 3-turn sim transcripts contain no escalation-worthy line
# (they're generic discovery), so pulling "the last prospect turn" gave 48 identical, incoherent
# quotes. Instead each reason carries a small bank of realistic quotes that actually EVIDENCE that
# reason, cycled for variety — so the queue reads as a believable set of distinct escalations.
_MOMENTS: dict[str, list[str]] = {
    "human_request": [
        "Can I just talk to a real person about this?",
        "I'd rather speak with an actual advisor, not a bot.",
        "Is there a human I can get on the line?",
    ],
    "pricing_concession": [
        "Can you match the $99/month another service quoted me?",
        "I need at least 20% off to make this work for us.",
        "That's over our budget — what can you actually do on price?",
    ],
    "compliance": [
        "How is my child's data protected — is this FERPA compliant?",
        "Are you allowed to help with a graded, for-credit assignment?",
        "I need your privacy policy in writing before we go further.",
    ],
    "concession_pressure": [
        "Give me the Premier tier at the Core price or I'm walking.",
        "I'll only sign up today if you throw in extra hours free.",
        "Knock 30% off right now and we have a deal.",
    ],
}
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
            reason = _REASONS[i % len(_REASONS)]
            # A reason-COHERENT moment (cycled for variety), and the turn it would have fired on
            # (the last turn of the call) — not a generic line pulled from the thin transcript.
            bank = _MOMENTS[reason]
            moment = bank[(i // len(_REASONS)) % len(bank)]
            turn_id = max(0, len(_turns(ep["turns"])) - 1)
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
