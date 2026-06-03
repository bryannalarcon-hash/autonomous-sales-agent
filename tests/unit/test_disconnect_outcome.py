# Unit tests for CB-09 + CB-22: a voice call that ends via participant-disconnect must ALWAYS persist
# a TERMINAL outcome, never in_progress (otherwise a hung-up call vanishes from the Calls list, which
# filters out in_progress). Targets the PURE mapping fn src.voice.worker._terminal_outcome_on_disconnect
# (importable WITHOUT livekit — the worker module is import-guarded, so this whole file runs in .venv
# with no livekit installed) plus the _agent_acts "happened at ANY point" scan it relies on.
#
# CB-09 cases: a 0-turn hang-up -> "abandoned"; a multi-turn no-close hang-up -> "released"; a genuine
# CLOSE (committed_tier / attempt_close) -> None (keep the positive outcome derive_outcome produced).
# CB-22 cases (the fall-through fix): the agent escalated/disqualified at SOME point but the LAST turn
# was non-terminal — the mapping itself must land "escalated"/"disqualified" rather than returning None
# and letting derive_outcome (which reads only the last turn) leave the call in_progress. Plus a guard
# that EVERY emitted slug renders via src.api.labels (no bare slug leaks to the operator).
from __future__ import annotations

from src.voice.worker import _agent_acts, _last_committed_act, _terminal_outcome_on_disconnect


class _T:
    """Minimal stand-in for schema.Turn — the scans read only `.speaker` and `.decision`."""

    def __init__(self, speaker: str, decision: str | None = None) -> None:
        self.speaker = speaker
        self.decision = decision


# =============================== CB-09 (original disconnect mapping) ============================


def test_zero_turn_hangup_is_abandoned() -> None:
    # The CB-09 evidence: caller hung up ~12s into the agent's opening, 0 committed turns, no terminal
    # act. A disconnect with ~no conversation is "abandoned", not in_progress.
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


def test_genuine_close_keeps_its_positive_outcome() -> None:
    # A genuine close fired (attempt_close at a tier): the disconnect mapping must NOT override it —
    # return None so the existing derive_outcome result (e.g. enrolled / *_booked) stands. A real
    # booking is never downgraded to released/abandoned by a later disconnect.
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=8, escalated=False, committed_tier="enrollment", last_act="attempt_close"
        )
        is None
    )


def test_close_act_without_explicit_tier_still_keeps_outcome() -> None:
    # Even if the close-tier wasn't threaded out-of-band, an attempt_close last act is a genuine close
    # -> None (derive_outcome maps the booking). Never downgrade a close.
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=6, escalated=False, committed_tier=None, last_act="attempt_close"
        )
        is None
    )


# =============================== CB-22 (escalated-then-continued fall-through) ===================


def test_escalated_then_continued_lands_escalated_not_in_progress() -> None:
    # THE CB-22 REGRESSION (ep-eba52675…, 18 turns): the agent escalated at turn 13 then KEPT TALKING,
    # so the LAST committed turn was a non-terminal handle_objection. The old mapping returned None
    # (deferring to derive_outcome, which reads only the last turn -> in_progress), so the call lingered
    # in_progress and vanished from Calls. The fix: an escalation at ANY point settles "escalated".
    turns = [
        _T("prospect"),
        _T("agent", "answer_via_kb"),
        _T("prospect"),
        _T("agent", "escalate"),  # turn 13 in the real call
        _T("prospect"),
        _T("agent", "handle_objection"),  # kept talking after escalating
    ]
    acts = _agent_acts(turns)
    assert "escalate" in acts
    outcome = _terminal_outcome_on_disconnect(
        turn_count=len(turns),
        escalated=("escalate" in acts),
        committed_tier=None,
        last_act=_last_committed_act(turns),  # handle_objection (non-terminal)
    )
    assert outcome == "escalated"
    assert outcome != "in_progress"


def test_escalation_buffered_flag_alone_lands_escalated() -> None:
    # The escalation may be surfaced via the buffered-escalation flag instead of a scanned act; either
    # source must settle "escalated" (never in_progress) when no genuine close fired.
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=3, escalated=True, committed_tier=None, last_act="ask"
        )
        == "escalated"
    )


def test_disqualified_then_continued_lands_disqualified() -> None:
    # Symmetric to CB-22: the agent disqualified the lead at some point but the last turn was
    # non-terminal — the mapping must land "disqualified", not in_progress.
    turns = [
        _T("prospect"),
        _T("agent", "disqualify"),
        _T("prospect"),
        _T("agent", "ask"),
    ]
    acts = _agent_acts(turns)
    outcome = _terminal_outcome_on_disconnect(
        turn_count=len(turns),
        escalated=False,
        committed_tier=None,
        last_act=_last_committed_act(turns),
        disqualified=("disqualify" in acts),
    )
    assert outcome == "disqualified"


def test_close_anywhere_outranks_a_later_escalation_flag() -> None:
    # A genuine close fired AND an escalation flag is set: the close wins (None) so the positive booking
    # outcome derive_outcome produced is preserved. (A real close is never downgraded.)
    assert (
        _terminal_outcome_on_disconnect(
            turn_count=10, escalated=True, committed_tier="consultation", last_act="ask"
        )
        is None
    )


def test_disconnect_never_returns_in_progress() -> None:
    # The whole point of CB-22: a disconnect ALWAYS settles terminal. Across the non-close space, the
    # mapping never yields in_progress (and only yields None for a genuine close, handled above).
    for tc, esc, dq, last in [
        (0, False, False, None),
        (1, False, False, "ask"),
        (5, True, False, "handle_objection"),
        (5, False, True, "ask"),
        (12, True, False, "answer_via_kb"),
    ]:
        out = _terminal_outcome_on_disconnect(
            turn_count=tc, escalated=esc, committed_tier=None, last_act=last, disqualified=dq
        )
        assert out is not None and out != "in_progress", (tc, esc, dq, last, out)


# =============================== _agent_acts scan (happened-at-any-point) ========================


def test_agent_acts_collects_every_agent_decision() -> None:
    turns = [
        _T("prospect"),
        _T("agent", "ask"),
        _T("prospect"),
        _T("agent", "escalate"),
        _T("agent", "handle_objection"),
    ]
    assert _agent_acts(turns) == {"ask", "escalate", "handle_objection"}


def test_agent_acts_ignores_user_turns_and_blank_decisions() -> None:
    turns = [_T("prospect"), _T("agent", None), _T("system", "noise")]
    assert _agent_acts(turns) == set()


def test_agent_acts_empty_for_no_turns() -> None:
    assert _agent_acts([]) == set()


# =============================== no-bare-slug guard ==============================================


def test_chosen_outcomes_have_human_labels() -> None:
    # CLAUDE.md "internal indices stay out of observable output": every terminal slug this mapping can
    # emit must render as a real label, never a bare slug, on the operator dashboard.
    from src.api.labels import OUTCOME_LABEL, outcome_label

    for slug in ("abandoned", "released", "escalated", "disqualified"):
        assert slug in OUTCOME_LABEL, f"{slug} has no human label in labels.OUTCOME_LABEL"
        # outcome_label must not fall through to the titleized-slug path for these.
        assert outcome_label(slug) == OUTCOME_LABEL[slug]
