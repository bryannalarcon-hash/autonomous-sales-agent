# Unit tests for CB-09: a voice call that ends via participant-disconnect WITHOUT reaching a terminal
# act must persist a TERMINAL outcome, never in_progress (otherwise a hung-up call vanishes from the
# Calls list, which filters out in_progress). Targets the PURE mapping fn
# src.voice.worker._terminal_outcome_on_disconnect (importable WITHOUT livekit — the worker module is
# import-guarded, so this whole file runs in .venv with no livekit installed). Cases: a 0-turn hang-up
# -> "abandoned"; a multi-turn no-close hang-up -> "released"; an already-terminal call (close /
# escalate / disqualify) -> None (keep the outcome derive_outcome already produced); and a guard that
# the chosen non-in_progress slugs render via src.api.labels (no bare slug leaks to the operator).
from __future__ import annotations

from src.voice.worker import _terminal_outcome_on_disconnect


def test_zero_turn_hangup_is_abandoned() -> None:
    # The CB-09 evidence episode: caller hung up ~12s into the agent's opening, 0 committed turns,
    # no terminal act. A disconnect with ~no conversation is "abandoned", not in_progress.
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=0, escalated=False, committed_tier=None, last_act=None
        )
        == "abandoned"
    )


def test_substantive_no_close_hangup_is_released() -> None:
    # A real conversation happened (committed turns) but the caller hung up before any close act ->
    # "released" (the same canonical terminal the sim uses for "ran out of runway, no commitment").
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=5, escalated=False, committed_tier=None, last_act="ask"
        )
        == "released"
    )


def test_single_committed_turn_is_released_not_abandoned() -> None:
    # Boundary: even one committed exchange means a real turn occurred -> substantive -> "released".
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=1, escalated=False, committed_tier=None, last_act="answer_via_kb"
        )
        == "released"
    )


def test_already_closed_call_keeps_its_outcome() -> None:
    # A terminal close act already fired (attempt_close at a tier): the disconnect mapping must NOT
    # override it — return None so the existing derive_outcome result (e.g. enrolled/booked) stands.
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=8, escalated=False, committed_tier="enrollment", last_act="attempt_close"
        )
        is None
    )


def test_escalated_call_keeps_its_outcome() -> None:
    # An escalation is a terminal state (-> "escalated"); never downgrade it to abandoned/released.
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=3, escalated=True, committed_tier=None, last_act="escalate"
        )
        is None
    )


def test_disqualified_call_keeps_its_outcome() -> None:
    # A disqualify is terminal (-> "disqualified"); the disconnect mapping must leave it alone.
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=4, escalated=False, committed_tier=None, last_act="disqualify"
        )
        is None
    )


def test_chosen_outcomes_have_human_labels() -> None:
    # CLAUDE.md "internal indices stay out of observable output": every terminal slug this mapping can
    # emit must render as a real label, never a bare slug, on the operator dashboard.
    from src.api.labels import OUTCOME_LABEL, outcome_label

    for slug in ("abandoned", "released"):
        assert slug in OUTCOME_LABEL, f"{slug} has no human label in labels.OUTCOME_LABEL"
        # outcome_label must not fall through to the titleized-slug path for these.
        assert outcome_label(slug) == OUTCOME_LABEL[slug]
