# The working belief-state object the DST mutates each turn (plan U3; docs/belief-state-schema.md).
# Three layers: Layer A discovery slots {name: {value, confidence}}, Layer B six latent drivers
# (trust / need_intensity / price_sensitivity / urgency / purchase_intent / bail_risk) as levels in
# [0,1], plus deterministically-DERIVED trend/velocity factors, and Layer C dialogue-control meta
# (stage, active_objection, last_user_act, decision_confidence, open_question, turn_count,
# escalation_imminent), plus a working-only `meta` dict for out-of-band / cross-call context the
# bridge (src/memory/hydrate.py) attaches at call start (assigned_voice, persona_read,
# prior_objections, returning_caller). Converts to/from src.memory.schema.BeliefSnapshot so logged
# trajectories persist consistently — the snapshot is the stored projection; this object carries the
# extra working-only state (last_user_act, open_question, turn_count, meta) the snapshot does not
# persist. NO LiveKit imports; `meta` is a plain dict and adds NO DB dependency (core stays DB-free).
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.memory.schema import BeliefSnapshot

# The six latent drivers, in canonical order (docs/belief-state-schema.md Layer B). purchase_intent
# is MEASURED directly (not a composite of the others); urgency is provisional (may fold into
# need_intensity under validation, U11) but is tracked here so the schema is complete.
DRIVERS: tuple[str, ...] = (
    "trust",
    "need_intensity",
    "price_sensitivity",
    "urgency",
    "purchase_intent",
    "bail_risk",
)

# Neutral cold-start prior for every driver (logged as a guess pending sim calibration — see the
# thresholds note in config/versions/champion_v0.yaml and the schema's validation plan).
_NEUTRAL_PRIOR = 0.5

# EFSM stage skeleton (Layer C): greeting -> discovery -> objection -> pitch -> close -> escalate.
GREETING_STAGE = "greeting"


def _clamp01(x: float) -> float:
    """Clamp a driver level to [0, 1] so deltas can never push a level out of range."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


@dataclass
class BeliefState:
    """The mutable per-turn belief state fed to the policy as a state feature vector.

    The DST (src/core/dst.py) is the only thing that should mutate this between turns; the policy
    reads it. `drivers` always holds exactly the six DRIVERS; `trends` holds `<driver>_velocity`
    factors derived from the logged trajectory (never LLM-emitted). Use to_snapshot() to log a
    turn and from_snapshot() to rehydrate a stored trajectory.
    """

    # Layer A — discovery slots: {slot_name: {"value": Any, "confidence": float in [0,1]}}.
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Layer B — latent driver levels in [0, 1].
    drivers: dict[str, float] = field(default_factory=dict)
    # Derived velocity/trend factors (Markov-preserving; computed from the trajectory, not the LLM).
    trends: dict[str, float] = field(default_factory=dict)

    # Layer C — dialogue-control meta.
    stage: str = GREETING_STAGE
    active_objection: Optional[str] = None
    last_user_act: Optional[str] = None
    decision_confidence: Optional[float] = None
    open_question: Optional[str] = None
    turn_count: int = 0
    escalation_imminent: bool = False

    # Cross-call / out-of-band context that is NOT a belief layer and is NOT persisted in the stored
    # snapshot: hydrated per-lead memory the bridge (src/memory/hydrate.py) attaches at call start
    # (e.g. assigned_voice, persona_read, prior_objections, returning_caller) so the opener/voice
    # adapter can use it without polluting Layer-A slots or setting active_objection. Pure dict —
    # adds no DB dependency, keeping this module DB-free; intentionally dropped by to_snapshot().
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def fresh(cls) -> "BeliefState":
        """A new belief state at the start of a call: empty slots, neutral drivers, greeting stage."""
        return cls(
            slots={},
            drivers={d: _NEUTRAL_PRIOR for d in DRIVERS},
            trends={f"{d}_velocity": 0.0 for d in DRIVERS},
            stage=GREETING_STAGE,
            turn_count=0,
            meta={},
        )

    # --- slot helpers ---------------------------------------------------------------------------

    def set_slot(self, name: str, value: Any, confidence: float) -> None:
        """Set/overwrite a slot's value + confidence (callers use merge logic in the DST)."""
        self.slots[name] = {"value": value, "confidence": _clamp01(float(confidence))}

    def slot_value(self, name: str) -> Any:
        slot = self.slots.get(name)
        return slot.get("value") if slot else None

    def slot_confidence(self, name: str) -> float:
        slot = self.slots.get(name)
        return float(slot.get("confidence", 0.0)) if slot else 0.0

    # --- driver helpers -------------------------------------------------------------------------

    def apply_driver_delta(self, name: str, delta: float) -> None:
        """Add a clamped delta to a known driver level; unknown driver names are ignored.

        Ignoring unknown keys (rather than raising) lets a hallucinated key in the LLM JSON pass
        through harmlessly — the DST relies on this to keep the six-driver set intact.
        """
        if name not in self.drivers:
            return
        self.drivers[name] = _clamp01(self.drivers[name] + float(delta))

    # --- snapshot interop (persist trajectories consistently with src/memory.schema) ------------

    def to_snapshot(self) -> BeliefSnapshot:
        """Project to the stored BeliefSnapshot (the logged, persisted shape).

        Slots are normalized to {value, confidence}; drivers/trends copy across; the meta fields the
        snapshot persists (stage, active_objection, decision_confidence, escalation_imminent) are
        carried. last_user_act / open_question / turn_count are working-only and not persisted —
        the snapshot is intentionally a lossy projection, matching src/memory/schema.BeliefSnapshot.
        """
        return BeliefSnapshot(
            slots={k: dict(v) for k, v in self.slots.items()},
            drivers=dict(self.drivers),
            trends=dict(self.trends),
            stage=self.stage,
            active_objection=self.active_objection,
            decision_confidence=self.decision_confidence,
            escalation_imminent=self.escalation_imminent,
        )

    @classmethod
    def from_snapshot(cls, snap: BeliefSnapshot) -> "BeliefState":
        """Rehydrate a working state from a stored snapshot.

        Missing drivers are backfilled to the neutral prior so the six-driver invariant holds even
        for older/partial snapshots; missing velocity trends default to 0.0. Working-only meta the
        snapshot never stored (last_user_act/open_question/turn_count) resets to its default.
        """
        drivers = {d: _NEUTRAL_PRIOR for d in DRIVERS}
        for k, v in (snap.drivers or {}).items():
            if k in drivers:
                drivers[k] = float(v)
        trends = {f"{d}_velocity": 0.0 for d in DRIVERS}
        trends.update({k: float(v) for k, v in (snap.trends or {}).items()})
        return cls(
            slots={k: dict(v) for k, v in (snap.slots or {}).items()},
            drivers=drivers,
            trends=trends,
            stage=snap.stage or GREETING_STAGE,
            active_objection=snap.active_objection,
            decision_confidence=snap.decision_confidence,
            escalation_imminent=bool(snap.escalation_imminent),
        )

    def copy(self) -> "BeliefState":
        """Deep-enough copy for an immutable-style DST update (slots/drivers/trends/meta duplicated)."""
        return BeliefState(
            slots={k: dict(v) for k, v in self.slots.items()},
            drivers=dict(self.drivers),
            trends=dict(self.trends),
            stage=self.stage,
            active_objection=self.active_objection,
            last_user_act=self.last_user_act,
            decision_confidence=self.decision_confidence,
            open_question=self.open_question,
            turn_count=self.turn_count,
            escalation_imminent=self.escalation_imminent,
            meta=dict(self.meta),
        )
