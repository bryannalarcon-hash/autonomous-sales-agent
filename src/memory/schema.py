# Typed models for the unified memory store (plan U2): Episode (ordered turns + outcome + version
# tags + channel), Lead (per-caller memory keyed by a HASHED phone number — raw phone is NEVER
# stored), VersionLineage (champion/challenger parent links + KPI), ExperimentRecord (a persisted
# champion-vs-challenger run — dashboard P6 lab / P7 approvals), plus EscalationLog and the
# belief-snapshot/turn sub-models. Fields are the union of the U2 spec and every field the
# dashboard data contract (docs/design/dashboard-ia-spec.md, pages P1–P9) says it reads, so the
# dashboard never needs a schema retrofit. Stdlib dataclasses only (matches src/config/settings.py).
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Channels an episode can originate from. sim/text are headless self-play + text console;
# voice is the LiveKit boundary. All three normalize to ONE schema (plan R26).
CHANNELS = ("sim", "text", "voice")

_PHONE_NONDIGIT = re.compile(r"\D+")


def phone_hash(raw: str) -> str:
    """Hash a raw phone number to a stable sha256 hex key (plan R42).

    Digits are normalized first (punctuation/spacing stripped) so "+1 (555) 010-0001"
    and "15550100001" map to the same lead. The returned hex is the ONLY phone-derived
    value that ever touches the store; the raw string is never persisted here.
    """
    if raw is None:
        raise ValueError("phone_hash requires a non-None phone string")
    normalized = _PHONE_NONDIGIT.sub("", str(raw))
    if not normalized:
        raise ValueError("phone_hash requires at least one digit in the phone string")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BeliefSnapshot:
    """The agent's factored belief state at one turn (docs/belief-state-schema.md).

    Three layers, all carried verbatim so Call Review can replay the trajectory (P2) and
    the Live monitor can show prioritized signals (P1): discovery slots (value+confidence),
    latent drivers (trust / need_intensity / price_sensitivity / urgency / purchase_intent /
    bail_risk), and deterministically-derived trend/velocity factors.
    """

    # Layer A — discovery slots: {slot_name: {"value": ..., "confidence": 0..1}}
    slots: dict[str, Any] = field(default_factory=dict)
    # Layer B — latent drivers: {driver_name: level 0..1}. purchase_intent is measured, bail_risk
    # is the walk-away signal the live monitor prioritizes.
    drivers: dict[str, float] = field(default_factory=dict)
    # Derived velocity/trend factors computed from the logged trajectory (Markov-preserving).
    trends: dict[str, float] = field(default_factory=dict)
    # Dialogue-control meta the live monitor + gates read: stage, active_objection, last_user_act,
    # decision_confidence, open_question, turn_count, escalation_imminent.
    stage: Optional[str] = None
    active_objection: Optional[str] = None
    decision_confidence: Optional[float] = None
    escalation_imminent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BeliefSnapshot":
        data = dict(data or {})
        return cls(
            slots=data.get("slots", {}) or {},
            drivers=data.get("drivers", {}) or {},
            trends=data.get("trends", {}) or {},
            stage=data.get("stage"),
            active_objection=data.get("active_objection"),
            decision_confidence=data.get("decision_confidence"),
            escalation_imminent=bool(data.get("escalation_imminent", False)),
        )


@dataclass
class Turn:
    """One ordered exchange turn in an episode — the Per-turn record the dashboard reads (P1/P2).

    Carries the transcript text, the agent's decision (next system act) + its one-line rationale
    (decision trace), the belief snapshot at that point, latency for the talk-listen / latency
    panels, and a turn id so logs join transcript+decision+belief by turn (R26).
    """

    turn_id: int
    speaker: str  # "agent" | "prospect" | "system"
    text: str
    # The agent's decision this turn (system dialogue act / pivot / escalate); None on user turns.
    decision: Optional[str] = None
    # One-line rationale per the decision (feeds the decision log / Call Review trace).
    rationale: Optional[str] = None
    belief: Optional[BeliefSnapshot] = None
    # Per-turn latency in ms (P1 latency panel / KPI talk-listen + duration). None for sim turns.
    latency_ms: Optional[int] = None
    # Optional KB version actually queried this turn (U5 grounding pins it per-turn).
    kb_version: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["belief"] = self.belief.to_dict() if self.belief is not None else None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Turn":
        data = dict(data or {})
        belief = data.get("belief")
        return cls(
            turn_id=int(data["turn_id"]),
            speaker=str(data["speaker"]),
            text=str(data.get("text", "")),
            decision=data.get("decision"),
            rationale=data.get("rationale"),
            belief=BeliefSnapshot.from_dict(belief) if belief else None,
            latency_ms=data.get("latency_ms"),
            kb_version=data.get("kb_version"),
        )


@dataclass
class Episode:
    """One conversation (real or sim) normalized to a single shape (plan R26).

    Holds the ordered turns (each with decision + rationale + belief snapshot), the outcome and
    commitment-ladder tier, the qualified/disqualified verdict + reason, and the version/kb_version/
    channel tags so every page can attribute behavior to the exact config that ran. The extra
    cohort/persona/metric fields below exist because the dashboard data contract (P3 filters,
    P4 KPIs) reads them — omitting them would force a later retrofit.
    """

    episode_id: str
    # Ordered turns — the belief trajectory the round-trip test must preserve.
    turns: list[Turn] = field(default_factory=list)
    # Terminal outcome label (e.g. "enrolled", "trial_booked", "consult_booked", "released",
    # "walked", "escalated"). P2/P3/P4 read this.
    outcome: Optional[str] = None
    # Weighted commitment-ladder tier (e.g. 0 none .. N enrollment). Distinct from same-call
    # enrollment rate, which P4 derives from outcome. Plain int tier so KPI views can aggregate.
    ladder_tier: int = 0
    qualified: bool = False
    disqualifier_reason: Optional[str] = None
    # Version attribution (stamped from AgentConfig.stamp()).
    version: str = ""
    kb_version: str = ""
    # Origin channel ∈ CHANNELS — real (voice/text) and sim share this one schema.
    channel: str = "sim"

    # --- Dashboard data-contract extensions (P1/P3/P4/P5) ---
    # Lead key (phone-hash) when the episode is tied to a known caller; None for anonymous sim runs.
    lead_phone_hash: Optional[str] = None
    # Persona/archetype label for per-archetype conversion (P4) and sim cohorting.
    persona: Optional[str] = None
    # Free-form cohort tag for P3 filtering / P4 grouping (e.g. "held_out", "training", "live").
    cohort: Optional[str] = None
    # Whether this episode produced an escalation (P3 escalated? filter, P5 link).
    escalated: bool = False
    # KPI-feeder rollups computed by the producing unit and stored for fast P4 aggregation
    # (talk-listen ratio, turn_count, duration_ms, time_to_pivot, abandon_turn, per-stage dwell,
    # objection_recovered, etc.). Kept as free-form JSON so KPI metrics can grow without migration.
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"channel must be one of {CHANNELS}, got {self.channel!r}")

    @property
    def belief_trajectory(self) -> list[Optional[BeliefSnapshot]]:
        """The ordered belief snapshots — the trajectory the round-trip test asserts is preserved."""
        return [t.belief for t in self.turns]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["turns"] = [t.to_dict() for t in self.turns]
        d["created_at"] = self.created_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        data = dict(data or {})
        created = data.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return cls(
            episode_id=str(data["episode_id"]),
            turns=[Turn.from_dict(t) for t in data.get("turns", []) or []],
            outcome=data.get("outcome"),
            ladder_tier=int(data.get("ladder_tier", 0) or 0),
            qualified=bool(data.get("qualified", False)),
            disqualifier_reason=data.get("disqualifier_reason"),
            version=str(data.get("version", "") or ""),
            kb_version=str(data.get("kb_version", "") or ""),
            channel=str(data.get("channel", "sim") or "sim"),
            lead_phone_hash=data.get("lead_phone_hash"),
            persona=data.get("persona"),
            cohort=data.get("cohort"),
            escalated=bool(data.get("escalated", False)),
            metrics=data.get("metrics", {}) or {},
            created_at=created or _utcnow(),
        )


@dataclass
class Lead:
    """Per-caller memory keyed by a HASHED phone number (plan R42, R2, R11).

    The raw phone number is NEVER stored in this body or in any episode — `phone_hash` is the only
    phone-derived value persisted. Hydrated at call start so the policy can skip already-known
    slots and reuse the caller's assigned (sticky) TTS voice. `slots` merge across calls.
    """

    phone_hash: str
    slots: dict[str, Any] = field(default_factory=dict)
    prior_objections: list[str] = field(default_factory=list)
    last_outcome: Optional[str] = None
    assigned_voice: Optional[str] = None
    # Latest persona read for the caller (e.g. archetype the agent inferred) — informs the opener.
    persona_read: Optional[str] = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at
        d["updated_at"] = self.updated_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Lead":
        data = dict(data or {})
        created = data.get("created_at")
        updated = data.get("updated_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated)
        return cls(
            phone_hash=str(data["phone_hash"]),
            slots=data.get("slots", {}) or {},
            prior_objections=list(data.get("prior_objections", []) or []),
            last_outcome=data.get("last_outcome"),
            assigned_voice=data.get("assigned_voice"),
            persona_read=data.get("persona_read"),
            created_at=created or _utcnow(),
            updated_at=updated or _utcnow(),
        )


@dataclass
class VersionLineage:
    """One node in the champion/challenger lineage tree (plan R12; dashboard P6/P9).

    Links a version to its parent, snapshots the config reference + kb_version that ran, carries the
    version's KPI record (JSON — the R34 set computed for it), and marks the current champion for
    one-click rollback. `config_ref` is a pointer (git ref / path), not the config body.
    """

    version: str
    parent_version: Optional[str] = None
    config_ref: Optional[str] = None
    kb_version: str = ""
    kpi: dict[str, Any] = field(default_factory=dict)
    is_champion: bool = False
    created_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionLineage":
        data = dict(data or {})
        created = data.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return cls(
            version=str(data["version"]),
            parent_version=data.get("parent_version"),
            config_ref=data.get("config_ref"),
            kb_version=str(data.get("kb_version", "") or ""),
            kpi=data.get("kpi", {}) or {},
            is_champion=bool(data.get("is_champion", False)),
            created_at=created or _utcnow(),
        )


@dataclass
class EscalationLog:
    """A deferred/extreme moment linked to an episode (plan R10; dashboard P5).

    The agent already deferred at runtime; this is the post-hoc review record. `reason` is the
    moment category (concession / human-request / compliance), `moment` is the turn/quote, and
    `lifecycle` tracks unreviewed → reviewed/resolved for the escalation queue.
    """

    escalation_id: str
    episode_id: str
    reason: Optional[str] = None
    moment: Optional[str] = None
    turn_id: Optional[int] = None
    lifecycle: str = "unreviewed"  # unreviewed | reviewed | resolved | dismissed
    created_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EscalationLog":
        data = dict(data or {})
        created = data.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return cls(
            escalation_id=str(data["escalation_id"]),
            episode_id=str(data["episode_id"]),
            reason=data.get("reason"),
            moment=data.get("moment"),
            turn_id=data.get("turn_id"),
            lifecycle=str(data.get("lifecycle", "unreviewed") or "unreviewed"),
            created_at=created or _utcnow(),
        )


# Experiment lifecycle states (dashboard P6 lab chips / P7 queue). `blocked` IS the
# PromotionDecision "pending_approval" — an extreme challenger that passed the bar but needs a human
# (R19). `draft`/`running` are in-flight; `passed` cleared the bar non-extreme; `promoted` shipped
# as champion (auto or post-approval); `rejected`/`paused` are terminal/halted.
EXPERIMENT_STATES = ("draft", "running", "passed", "blocked", "promoted", "rejected", "paused")
# Guardrail verdict for an experiment arm (mirrors the prototype's pass/warn/trip chips).
GUARDRAIL_VERDICTS = ("pass", "warn", "trip")


@dataclass
class ExperimentRecord:
    """A persisted champion-vs-challenger experiment (plan U16; dashboard P6 lab / P7 approvals).

    The loop (src.loop.experiment.run_experiment) returns an in-memory ExperimentResult; this is the
    DURABLE record the dashboard reads. It carries the BEFORE/AFTER comparison (champion_kpi vs
    challenger_kpi, delta + delta_ci + challenger_better — the significant-lift decision), the
    guardrail verdict, the per-arm qualification accuracy (R29), the single declared diff (R17), the
    is_extreme flag (R19), and a lifecycle `state` ∈ EXPERIMENT_STATES. A KB/playbook editor SAVE
    persists a NEW record in `running` state (a DRAFT CHALLENGER) WITHOUT touching version_lineage —
    so the live champion is never mutated by an edit (R20).
    """

    experiment_id: str
    challenger_version: str
    parent_version: Optional[str] = None
    name: str = ""
    dimension: str = ""
    # The minimal one-dimension diff, as the namespaced labels declared_diff() returns (R17).
    declared_diff: list[str] = field(default_factory=list)
    diff_description: str = ""
    population: str = ""
    n: int = 0
    target: int = 0
    kb_version: str = ""
    # --- before/after comparison (grading.ComparisonResult fields) ---
    champion_kpi: float = 0.0
    challenger_kpi: float = 0.0
    delta: float = 0.0
    delta_ci: list[float] = field(default_factory=lambda: [0.0, 0.0])
    challenger_better: bool = False
    # Same-call enrollment delta (challenger − champion), distinct from the ladder delta above.
    enroll_delta: float = 0.0
    # Significance ∈ 0..1 (1 − the alpha at which the delta CI excludes 0; surfaced as a % chip).
    significance: float = 0.0
    # --- guardrail + qualification (R24/R29) ---
    guardrail: str = "pass"  # pass | warn | trip
    guardrail_reason: Optional[str] = None
    champion_qual_acc: float = 1.0
    challenger_qual_acc: float = 1.0
    # --- gate + lifecycle (R19) ---
    is_extreme: bool = False
    state: str = "running"
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.state not in EXPERIMENT_STATES:
            raise ValueError(f"state must be one of {EXPERIMENT_STATES}, got {self.state!r}")
        if self.guardrail not in GUARDRAIL_VERDICTS:
            raise ValueError(
                f"guardrail must be one of {GUARDRAIL_VERDICTS}, got {self.guardrail!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperimentRecord":
        data = dict(data or {})
        created = data.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        ci = data.get("delta_ci") or [0.0, 0.0]
        return cls(
            experiment_id=str(data["experiment_id"]),
            challenger_version=str(data["challenger_version"]),
            parent_version=data.get("parent_version"),
            name=str(data.get("name", "") or ""),
            dimension=str(data.get("dimension", "") or ""),
            declared_diff=list(data.get("declared_diff", []) or []),
            diff_description=str(data.get("diff_description", "") or ""),
            population=str(data.get("population", "") or ""),
            n=int(data.get("n", 0) or 0),
            target=int(data.get("target", 0) or 0),
            kb_version=str(data.get("kb_version", "") or ""),
            champion_kpi=float(data.get("champion_kpi", 0.0) or 0.0),
            challenger_kpi=float(data.get("challenger_kpi", 0.0) or 0.0),
            delta=float(data.get("delta", 0.0) or 0.0),
            delta_ci=[float(ci[0]), float(ci[1])],
            challenger_better=bool(data.get("challenger_better", False)),
            enroll_delta=float(data.get("enroll_delta", 0.0) or 0.0),
            significance=float(data.get("significance", 0.0) or 0.0),
            guardrail=str(data.get("guardrail", "pass") or "pass"),
            guardrail_reason=data.get("guardrail_reason"),
            champion_qual_acc=float(data.get("champion_qual_acc", 1.0) or 1.0),
            challenger_qual_acc=float(data.get("challenger_qual_acc", 1.0) or 1.0),
            is_extreme=bool(data.get("is_extreme", False)),
            state=str(data.get("state", "running") or "running"),
            created_at=created or _utcnow(),
        )
