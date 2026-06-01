#!/usr/bin/env python3
# scripts/seed_demo.py — idempotent demo-DATA curation for the operator dashboard. The demo DB is the
# organic output of self-play + live calls, so the escalation queue was incoherent: rows cited a
# trigger moment ("Can I talk to a real person?") that never appeared in the linked transcript. This
# makes the queue COHERENT end-to-end: (1) PURGES all escalations, (2) for each completed `escalated`
# episode, APPENDS a real (prospect-trigger → Alex-escalates) turn pair to the transcript (idempotent
# via the _SEED_MARKER kb_version) so the call actually CONTAINS the cited line, then logs an
# escalation whose reason ↔ moment ↔ turn_id ↔ transcript all agree, with a reason-tuned belief at the
# trigger turn + mixed lifecycle; and (3) backfills duration_ms on episodes lacking one. Safe to re-run
# (strips its own prior augmentation before re-appending).
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


# Reason-appropriate driver readings for the belief snapshot AT the escalation (full six-driver set
# the schema expects), so the drawer's "belief at escalation" bars are coherent with the reason.
_TRIGGER_DRIVERS: dict[str, dict[str, float]] = {
    "human_request": {"trust": 0.45, "urgency": 0.50, "bail_risk": 0.62, "need_intensity": 0.55, "purchase_intent": 0.35, "price_sensitivity": 0.40},
    "pricing_concession": {"trust": 0.52, "urgency": 0.48, "bail_risk": 0.46, "need_intensity": 0.60, "purchase_intent": 0.58, "price_sensitivity": 0.86},
    "compliance": {"trust": 0.40, "urgency": 0.45, "bail_risk": 0.50, "need_intensity": 0.50, "purchase_intent": 0.42, "price_sensitivity": 0.45},
    "concession_pressure": {"trust": 0.50, "urgency": 0.60, "bail_risk": 0.58, "need_intensity": 0.60, "purchase_intent": 0.62, "price_sensitivity": 0.80},
}
# What Alex SAYS when escalating (the agent turn right after the trigger) — so the transcript shows
# the hand-off, making the call coherent end-to-end.
_HANDOFF: dict[str, str] = {
    "human_request": "Absolutely — let me bring in one of our human advisors who can take it from here. I'll have someone reach out to you shortly.",
    "pricing_concession": "That's a fair ask on price — let me loop in a specialist who can look at custom options. I'll have them follow up with you.",
    "compliance": "Great question, and I want to get it exactly right — let me connect you with the teammate who handles that directly.",
    "concession_pressure": "I hear you on getting the best deal — let me bring in a specialist who can review what's possible and follow up.",
}
_ACTIVE_OBJECTION: dict[str, Optional[str]] = {
    "pricing_concession": "price", "concession_pressure": "price", "compliance": None, "human_request": None,
}
# Sentinel stamped on the turns this script appends (a real schema field, so no extra keys) — lets a
# re-run STRIP its prior augmentation before re-appending, keeping the transcript idempotent.
_SEED_MARKER = "seed-escalation"


def _escalation_belief(reason: str) -> dict[str, Any]:
    """A full belief snapshot at the escalation moment — drivers tuned to the reason, escalation armed."""
    return {
        "slots": {},
        "stage": "escalation",
        "trends": {},
        "drivers": _TRIGGER_DRIVERS[reason],
        "active_objection": _ACTIVE_OBJECTION.get(reason),
        "decision_confidence": 0.32,
        "escalation_imminent": True,
    }


def _augmented_turns(base_turns: list, *, moment_text: str, reason: str) -> tuple[list, int]:
    """Return (new turns, trigger_turn_id): strip any prior seeded turns, then append a coherent
    (prospect TRIGGER → agent ESCALATION) pair so the transcript actually CONTAINS the trigger line
    the escalation cites. The trigger turn carries the escalation belief (the drawer reads it)."""
    kept = [t for t in base_turns if isinstance(t, dict) and t.get("kb_version") != _SEED_MARKER]
    next_id = (max((int(t.get("turn_id", 0)) for t in kept), default=-1)) + 1
    belief = _escalation_belief(reason)
    trigger = {
        "turn_id": next_id, "speaker": "prospect", "text": moment_text,
        "decision": None, "rationale": None, "belief": belief,
        "latency_ms": None, "kb_version": _SEED_MARKER,
    }
    handoff = {
        "turn_id": next_id + 1, "speaker": "agent", "text": _HANDOFF[reason],
        "decision": "escalate", "rationale": f"Deferred to a human specialist ({reason}).",
        "belief": belief, "latency_ms": 1100, "kb_version": _SEED_MARKER,
    }
    return kept + [trigger, handoff], next_id


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ABORT: DATABASE_URL not set")
        return 2
    c = await asyncpg.connect(url)
    try:
        # 1. Purge ALL escalations and rebuild a coherent set — the prior rows (incl. legacy
        #    "originals") cited trigger moments that never appeared in the linked transcript.
        purged = await c.execute("DELETE FROM escalation_log")
        print(f"purged escalations: {purged}")

        # 2. Rebuild: for each escalated episode, APPEND the trigger turn + Alex's escalation to the
        #    transcript (so the call actually contains the cited line), then log a coherent escalation
        #    pointing at that trigger turn. Reason/moment/transcript/turn_id are all consistent.
        eps = await c.fetch(
            "SELECT episode_id, turns FROM episode WHERE outcome = 'escalated' "
            "ORDER BY created_at DESC LIMIT $1",
            _N_ESCALATIONS,
        )
        now = datetime.now(timezone.utc)
        made = 0
        for i, ep in enumerate(eps):
            reason = _REASONS[i % len(_REASONS)]
            bank = _MOMENTS[reason]
            moment_text = bank[(i // len(_REASONS)) % len(bank)]  # the raw spoken line
            new_turns, trigger_turn_id = _augmented_turns(
                _turns(ep["turns"]), moment_text=moment_text, reason=reason
            )
            # Write the augmented transcript so the call CONTAINS the trigger line + the escalation.
            await c.execute(
                "UPDATE episode SET turns = $1::jsonb WHERE episode_id = $2",
                json.dumps(new_turns), ep["episode_id"],
            )
            lifecycle = _LIFECYCLES[i % len(_LIFECYCLES)]
            esc_id = f"esc-{ep['episode_id'].split('-')[-1][:8]:>08}"[:12]
            created = now - timedelta(hours=i * 3)  # spread over recent history, newest first
            await c.execute(
                "INSERT INTO escalation_log (escalation_id, episode_id, reason, moment, turn_id, "
                "lifecycle, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (escalation_id) DO UPDATE SET reason=EXCLUDED.reason, "
                "moment=EXCLUDED.moment, turn_id=EXCLUDED.turn_id, lifecycle=EXCLUDED.lifecycle",
                esc_id, ep["episode_id"], reason, f"“{moment_text}”", trigger_turn_id,
                lifecycle, created,
            )
            made += 1
        print(f"rebuilt escalations: {made} (transcript contains the cited trigger; reason↔moment↔turn coherent)")

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
