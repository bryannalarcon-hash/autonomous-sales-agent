# Human-readable label translation for the operator dashboard (CLAUDE.md "internal indices stay out
# of observable output" rule). The backend stores compact internal tokens — ladder tier ints (0..4),
# latent-driver enum slugs (trust / bail_risk / price_sensitivity ...), stage / system-act slugs,
# outcome + escalation-reason keys — but NONE of those raw tokens may render in operator-facing text.
# Every page (P1 Live, Call Review, P3 Calls, P4 KPI, P5 Escalations) goes through this map so the
# API ships display strings, never bare indices. Pure stdlib; no I/O. Used by src.api.operate.
from __future__ import annotations

from typing import Optional

# Commitment-ladder tier (Episode.ladder_tier int) -> the human label the operator reads. The int is
# the internal weight grading.kpi_score sums; the operator sees the tier's MEANING, never the number
# alone. Order/strength: 0 none < 1 callback < 2 consult/booked < 3 trial < 4 enrollment.
LADDER_TIER_LABEL: dict[int, str] = {
    0: "No commitment",
    1: "Callback booked",
    2: "Consultation booked",
    3: "Trial booked",
    4: "Same-call enrollment",
}

# Terminal outcome key -> display label. Keys are the Episode.outcome strings the loop/sim emit.
OUTCOME_LABEL: dict[str, str] = {
    "enrolled": "Enrolled",
    "trial_booked": "Trial booked",
    "consult_booked": "Consultation booked",
    "callback_booked": "Callback booked",
    "booked": "Booked",
    "interested": "Interested",
    "released": "Released",
    "abandoned": "Abandoned",  # CB-09: caller hung up mid-call with ~no conversation (0 turns)
    "no_interest": "No interest",
    "walked": "Walked away",
    "disqualified": "Disqualified",
    "escalated": "Escalated",
    "in_progress": "In progress",
}

# Latent-driver enum slug -> display label. These are the belief-state signals the Live monitor (P1)
# prioritizes; the operator must never see the raw slug "bail_risk".
DRIVER_LABEL: dict[str, str] = {
    "trust": "Trust",
    "bail_risk": "Walk-away risk",
    "need_intensity": "Need intensity",
    "price_sensitivity": "Price sensitivity",
    "urgency": "Urgency",
    "purchase_intent": "Purchase intent",
    "rapport": "Rapport",
    "skepticism": "Skepticism",
    "concession_pressure": "Concession pressure",
}

# Dialogue-stage slug -> display label (Discovery / Objection handling / Closing / Wrap-up).
STAGE_LABEL: dict[str, str] = {
    "discovery": "Discovery",
    "objection": "Objection handling",
    "objection_handling": "Objection handling",
    "closing": "Closing",
    "close": "Closing",
    "wrap": "Wrap-up",
    "wrap_up": "Wrap-up",
    "opening": "Opening",
}

# System dialogue-act slug (the agent's decision per turn) -> display label for the decision trace.
ACT_LABEL: dict[str, str] = {
    "greeting": "Open · greeting",
    "ask": "Ask · discovery",
    "answer_via_kb": "Answer from knowledge base",
    "pitch": "Pitch value",
    "handle_objection": "Handle objection",
    "reframe": "Reframe",
    "reframe_cost": "Reframe cost",
    "de_risk": "De-risk",
    "build_trust": "Build trust",
    "pivot": "Pivot",
    "trial_close": "Trial close",
    "attempt_close": "Attempt close",
    "confirm_known": "Confirm known fact",
    "escalate": "Escalate to human",
    "disqualify": "Disqualify",
}

# Escalation-reason key -> display label (P5 queue). Stored keys may be slugs or already-friendly.
ESCALATION_REASON_LABEL: dict[str, str] = {
    "pricing_concession": "Pricing concession",
    "concession": "Pricing concession",
    "human_requested": "Human requested",
    "human_request": "Human requested",
    "compliance": "Compliance",
    "false_promise": "False-promise risk",
    "abuse": "Abusive caller",
}

# Escalation lifecycle key -> display label (P5 segmented control).
LIFECYCLE_LABEL: dict[str, str] = {
    "unreviewed": "Unreviewed",
    "reviewed": "Reviewed",
    "resolved": "Resolved",
    "dismissed": "Dismissed",
}

# Experiment lifecycle state -> display label (P6 lab chips / P7 queue). `blocked` is the human-gate
# state (the loop's "pending_approval"); the operator sees the meaning, never the raw slug.
EXPERIMENT_STATE_LABEL: dict[str, str] = {
    "draft": "Draft",
    "running": "Running",
    "passed": "Result ready",
    "blocked": "Guardrail blocked",
    "promoted": "Promoted",
    "rejected": "Rejected",
    "paused": "Paused",
}

# Mutation-surface dimension slug (declared_diff label) -> operator-facing name. The raw namespaced
# slug ("playbooks.discovery_sequence", "thresholds.pushiness_cap", "persona") is an INTERNAL index
# and must never render in the lab/approvals text — translate it here.
DIMENSION_LABEL: dict[str, str] = {
    "persona": "Persona & tone",
    "playbooks.discovery_sequence": "Discovery sequencing",
    "playbooks.rebuttals": "Objection rebuttals",
    "thresholds.pushiness_cap": "Pushiness cap",
    "thresholds.pushiness_pressure_count_cap": "Pushiness pressure count",
    "thresholds.discovery_slots_required": "Discovery depth",
    "thresholds.low_confidence_level": "Low-confidence threshold",
    "thresholds.trust_gate_open_price": "Trust gate for pricing",
    "thresholds.max_concession_band": "Pricing concession band",
    "thresholds.escalate_low_confidence_turns": "Escalation patience",
    "kb": "Knowledge base",
}


def _titleize(slug: str) -> str:
    """Fallback: turn an unknown slug ("foo_bar") into a readable label ("Foo bar")."""
    return slug.replace("_", " ").replace("-", " ").strip().capitalize()


def ladder_tier_label(tier: Optional[int]) -> str:
    if tier is None:
        return "—"
    return LADDER_TIER_LABEL.get(int(tier), f"Tier {int(tier)}")


def outcome_label(outcome: Optional[str]) -> str:
    if not outcome:
        return "In progress"
    return OUTCOME_LABEL.get(outcome, _titleize(outcome))


def driver_label(slug: str) -> str:
    return DRIVER_LABEL.get(slug, _titleize(slug))


def stage_label(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    return STAGE_LABEL.get(slug, _titleize(slug))


def act_label(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    return ACT_LABEL.get(slug, _titleize(slug))


def escalation_reason_label(slug: Optional[str]) -> str:
    if not slug:
        return "Other"
    return ESCALATION_REASON_LABEL.get(slug, _titleize(slug))


def lifecycle_label(slug: Optional[str]) -> str:
    if not slug:
        return "Unreviewed"
    return LIFECYCLE_LABEL.get(slug, _titleize(slug))


def experiment_state_label(slug: Optional[str]) -> str:
    if not slug:
        return "Running"
    return EXPERIMENT_STATE_LABEL.get(slug, _titleize(slug))


def dimension_label(slug: Optional[str]) -> str:
    """Translate a mutation-surface dimension slug to its operator-facing name. A namespaced
    threshold/playbook slug falls back to a readable title of its trailing key (never the raw slug)."""
    if not slug:
        return "—"
    if slug in DIMENSION_LABEL:
        return DIMENSION_LABEL[slug]
    # Namespaced fallback: show the trailing key titleized, dropping the internal "prompts."/etc.
    tail = slug.split(".", 1)[1] if "." in slug else slug
    return _titleize(tail)
