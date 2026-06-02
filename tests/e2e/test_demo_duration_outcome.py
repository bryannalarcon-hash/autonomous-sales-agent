# Regression tests for CB-08/CB-09 persistence + Calls-visibility behavior (pure, no DB):
#   - operate._is_completed: a terminal "abandoned" hang-up surfaces in the Calls list EVEN with 0
#     turns (the user's hung-up call), while in_progress / other 0-turn rows stay excluded.
#   - persistence.episode_from_session: records metrics["duration_ms"] + preserves created_at when the
#     stable start is passed; outcome_override forces a terminal outcome ONLY when no terminal act fired
#     (so a mid-call hang-up / a demo that closed earlier lands terminal, never overriding a real close).
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.api.operate import _is_completed
from src.api.persistence import episode_from_session
from src.config.settings import load_config
from src.core.belief_state import BeliefState
from src.memory.schema import Episode, Turn

_CFG = load_config("champion_v0")


def _ep(outcome, n_turns):
    turns = [Turn(turn_id=i, speaker="agent" if i % 2 else "prospect", text="x") for i in range(n_turns)]
    return Episode(episode_id="ep-x", turns=turns, outcome=outcome)


# --- operate._is_completed (CB-09 Calls visibility) ------------------------------------------------

def test_zero_turn_abandoned_call_is_completed():
    """A hung-up call (terminal 'abandoned', 0 committed turns) MUST surface in the Calls list."""
    assert _is_completed(_ep("abandoned", 0)) is True


def test_zero_turn_in_progress_is_not_completed():
    """An active/never-finalized 0-turn shell stays excluded."""
    assert _is_completed(_ep("in_progress", 0)) is False
    assert _is_completed(_ep(None, 0)) is False
    assert _is_completed(_ep("", 0)) is False


def test_zero_turn_nonabandoned_terminal_still_excluded():
    """The 0-turn guard still applies to every terminal outcome EXCEPT 'abandoned' (only the hang-up
    case legitimately has 0 turns; a 0-turn consult_booked would be a data artifact, keep it hidden)."""
    assert _is_completed(_ep("consult_booked", 0)) is False


def test_multiturn_terminal_is_completed():
    assert _is_completed(_ep("consult_booked", 4)) is True
    assert _is_completed(_ep("released", 6)) is True


# --- persistence.episode_from_session (CB-08 duration + CB-09 override) -----------------------------

def _session(*, last_agent_act=None):
    state = MagicMock()
    turns = [Turn(turn_id=0, speaker="prospect", text="hi")]
    if last_agent_act is not None:
        turns.append(Turn(turn_id=1, speaker="agent", text="reply", decision=last_agent_act))
    state.turns = turns
    state.belief = BeliefState.fresh()
    session = MagicMock()
    session.state = state
    session.recorded = True
    return session


def test_duration_ms_recorded_when_created_at_passed():
    start = datetime.now(timezone.utc) - timedelta(seconds=42)
    ep = episode_from_session(_session(), config=_CFG, channel="text", created_at=start)
    assert ep.created_at == start
    assert ep.metrics["duration_ms"] >= 41_000  # ~42 s, clamped ≥0


def test_no_duration_ms_without_created_at():
    ep = episode_from_session(_session(), config=_CFG, channel="text")
    assert "duration_ms" not in ep.metrics


def test_outcome_override_applies_only_when_no_terminal_act():
    # No terminal act -> derive_outcome yields in_progress -> the override takes effect.
    ep = episode_from_session(_session(last_agent_act="ask"), config=_CFG, channel="text",
                              outcome_override="consult_booked")
    assert ep.outcome == "consult_booked"


def test_outcome_override_ignored_when_agent_actually_closed():
    # The agent's last act IS a real close -> derive_outcome wins, the override is ignored.
    ep = episode_from_session(_session(last_agent_act="attempt_close"), config=_CFG, channel="text",
                              last_tier="enrollment", outcome_override="abandoned")
    assert ep.outcome == "enrolled"
