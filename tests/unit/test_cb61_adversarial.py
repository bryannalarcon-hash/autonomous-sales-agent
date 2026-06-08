# CB-61 adversarial verifier tests — false-positive and ordering attacks.
# Authored by the adversarial verifier; tests kept because they expose real issues.
# Zero LLM cost: only deterministic extractors and gate logic are exercised.
# Run with: .venv/bin/python -m pytest tests/unit/test_cb61_adversarial.py -v
from __future__ import annotations

import pytest

from src.config.settings import AgentConfig, Persona
from src.core import gates
from src.core.belief_state import BeliefState
from src.core.dst import _classify_intent, _extract_callback_window, _extract_timeline
from src.core.policy import Decision


# -------------------------------------------------------------------------------------
# Shared helpers
# -------------------------------------------------------------------------------------

def _make_config(**overrides) -> AgentConfig:
    thresholds = {
        "trust_gate_open_price": 0.5,
        "pushiness_cap": 0.7,
        "pushiness_pressure_count_cap": 2,
        "escalate_low_confidence_turns": 2,
        "low_confidence_level": 0.4,
        "max_concession_band": 0.15,
        "discovery_slots_required": 0,
        "min_turns_before_close": 4,
        "discovery_slots_before_close": 1,
        "extreme_bail_deescalate": 0.9,
        "max_slot_asks": 2,
        "budget_refusals_before_callback": 2,
        "callback_price_sensitivity": 0.75,
        "close_ready_trust": 0.6,
        "close_ready_trial_trust": 0.7,
        "close_ready_trial_intent": 0.55,
    }
    thresholds.update(overrides)
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Nerdy", role="learning advisor", style="warm-consultative"),
        prompts={"policy": "Propose the next act as JSON.", "nlg": "Reply warmly."},
        playbooks={"discovery_sequence": ["grade_level", "subject", "goal", "timeline", "budget"]},
        thresholds=thresholds,
    )


# ==============================================================================
# GROUP A — callback_window false positives: deadline day captured as scheduling offer
# ==============================================================================

class TestCallbackWindowDeadlineConfusion:
    """A day name that describes a TEST or EXAM deadline must NOT be captured as a callback_window.
    The _CALLBACK_WINDOW_RE has no mechanism to distinguish 'her test is Thursday' from
    'Thursday after 3pm works for me'; the scheduling-cue guard only fires for bare TIME-of-day
    matches, not for named-day matches, so deadline day names extract at 0.9 — BLOCKING BUG."""

    def test_her_test_is_thursday_must_not_fill_callback_window(self):
        """'her test is on Thursday' is a deadline statement, not a scheduling offer.
        Capturing it as callback_window at 0.9 will make the agent echo 'Thursday — noted, our
        team will call you then' instead of asking for a callback day."""
        result = _extract_callback_window("her test is on Thursday")
        assert result is None, (
            f"BLOCKING: 'her test is on Thursday' must NOT extract as callback_window "
            f"(deadline day, not scheduling offer); got {result!r}"
        )

    def test_exam_thursday_morning_must_not_fill_callback_window(self):
        """'the exam is Thursday morning' is a test time, not a proposed callback window."""
        result = _extract_callback_window("the exam is Thursday morning")
        assert result is None, (
            f"BLOCKING: 'the exam is Thursday morning' must NOT extract as callback_window; "
            f"got {result!r}"
        )

    def test_she_has_a_quiz_friday_morning_must_not_fill_callback_window(self):
        """'she has a quiz on Friday morning' is an exam context, not a scheduling offer."""
        result = _extract_callback_window("she has a quiz on Friday morning")
        assert result is None, (
            f"BLOCKING: quiz-day must NOT extract as callback_window; got {result!r}"
        )

    def test_test_on_monday_must_not_fill_callback_window(self):
        """'there is a test on Monday' is a deadline, not a scheduling window."""
        result = _extract_callback_window("there is a test on Monday")
        assert result is None, (
            f"BLOCKING: deadline Monday must NOT become callback_window; got {result!r}"
        )


# ==============================================================================
# GROUP B — confirm_request masks decision_maker objection (BLOCKING BUG)
# ==============================================================================

class TestConfirmRequestMasksDecisionMakerObjection:
    """BLOCKING: 'let me confirm with my wife first' contains the word 'confirm' and therefore
    hits _CONFIRM_REQUEST_RE *before* the objection scanner fires. This erases the
    decision_maker objection cue. The CB-50 guard (no-authority -> callback) depends on
    active_objection='decision_maker' being set, so when this fires the agent goes straight to
    confirm_known instead of routing to the decision_maker callback path.

    The intent ordering in _classify_intent is:
        human_request → confirm_request → objections → …
    'confirm_request' must NOT be raised when 'confirm' is used as 'confirm with <spouse>'.
    The verb 'confirm with <person>' is a deferral, not a confirmation request directed AT the agent."""

    def test_confirm_with_wife_first_is_decision_maker_not_confirm_request(self):
        """'let me confirm with my wife first' — objection, not a confirm_request.
        The word 'confirm' here means 'check with', not 'please confirm this fact for me'."""
        act, obj, q = _classify_intent("let me confirm with my wife first")
        assert act != "confirm_request", (
            f"BLOCKING: 'let me confirm with my wife first' must NOT classify as confirm_request "
            f"(it is a decision_maker deferral); got act={act!r}, obj={obj!r}"
        )
        assert obj == "decision_maker" or act == "objection", (
            f"BLOCKING: must detect decision_maker objection; got act={act!r}, obj={obj!r}"
        )

    def test_confirm_with_husband_is_decision_maker(self):
        """'I need to confirm with my husband' — decision_maker objection, not confirm_request."""
        act, obj, q = _classify_intent("I need to confirm with my husband before we proceed")
        assert act != "confirm_request", (
            f"BLOCKING: must not be confirm_request; got {act!r}"
        )
        assert obj == "decision_maker" or act == "objection", (
            f"BLOCKING: must detect decision_maker; got act={act!r}, obj={obj!r}"
        )

    def test_double_check_with_partner_is_decision_maker(self):
        """'let me double-check with my partner' — the double-check verb here means consulting
        the partner, not asking the agent to verify a slot."""
        act, obj, q = _classify_intent("let me double-check with my partner")
        assert act != "confirm_request", (
            f"BLOCKING: 'double-check with my partner' must not be confirm_request; got {act!r}"
        )
        assert obj == "decision_maker" or act == "objection", (
            f"BLOCKING: must detect decision_maker; got act={act!r}, obj={obj!r}"
        )

    def test_confirm_with_spouse_does_not_suppress_objection_in_gate_chain(self):
        """End-to-end gate chain: belief pre-loaded with active_objection=decision_maker,
        last_user_act='confirm_request' (as currently misclassified) — must_clear_objection
        must still redirect to handle_objection.

        This test exercises the interaction between the misclassification and the full gate:
        even if confirm_request is set, must_clear_objection (which runs AFTER address_direct_input)
        only guards attempt_close, so a deferrable act converted to confirm_known by the
        confirm_request path bypasses the objection entirely. The objection is never handled."""
        cfg = _make_config()
        b = BeliefState.fresh()
        b.active_objection = "decision_maker"
        b.last_user_act = "confirm_request"  # as currently mislabeled
        b.open_question = "let me confirm with my wife first"

        # Proposed: a deferrable ask — address_direct_input currently converts to confirm_known
        proposed = Decision(act="ask", target_slot="grade_level", rationale="discovery")
        out = gates.address_direct_input(proposed, b, cfg, history=[])
        # With the current bug, out.act == 'confirm_known' even though decision_maker is live.
        # Correct behaviour: when active_objection is set, the result should be handle_objection.
        assert out.act == "handle_objection", (
            f"BLOCKING: with active_objection='decision_maker' and a confirm-with-spouse turn, "
            f"address_direct_input must route to handle_objection, not {out.act!r}. "
            f"The confirm_request branch fires BEFORE the objection check in address_direct_input "
            f"(gates.py line 618 < line 633), suppressing the decision_maker handling."
        )


# ==============================================================================
# GROUP C — hedged callback window must NOT lock at 0.9
# ==============================================================================

class TestHedgedCallbackWindowConfidence:
    """'maybe Thursday could work?' expresses uncertainty. Extracting at 0.9 (lock confidence)
    means skip_known will protect it as if it were a firm commitment, and the agent will echo
    it as confirmed. The coder acknowledged this as a risk but did NOT implement hedge softening
    for callback_window the way it was done for timeline."""

    def test_maybe_thursday_could_work_is_below_lock_confidence(self):
        """'maybe Thursday could work?' — hedged; must be below lock confidence (0.8)."""
        result = _extract_callback_window("maybe Thursday could work?")
        if result is not None:
            _value, conf = result
            assert conf < 0.8, (
                f"NON-BLOCKING: 'maybe Thursday could work?' extracted at conf={conf}; "
                f"a hedged window must be below lock confidence (0.8) so it remains re-confirmable. "
                f"_CALLBACK_WINDOW_RE has no hedge softening (cf. _TIMELINE_HEDGE_RE in timeline)."
            )

    def test_perhaps_friday_afternoon_ok_is_below_lock_confidence(self):
        """'perhaps Friday afternoon would be ok' — hedged; must not lock."""
        result = _extract_callback_window("perhaps Friday afternoon would be ok")
        if result is not None:
            _value, conf = result
            assert conf < 0.8, (
                f"NON-BLOCKING: 'perhaps Friday afternoon' extracted at conf={conf}; "
                f"hedged window must not lock at 0.9."
            )

    def test_thursday_might_be_fine_is_below_lock_confidence(self):
        """'Thursday might be fine I guess' — tentative; must not lock."""
        result = _extract_callback_window("Thursday might be fine I guess")
        if result is not None:
            _value, conf = result
            assert conf < 0.8, (
                f"NON-BLOCKING: tentative 'Thursday might be fine' extracted at conf={conf}; "
                f"must not be locked."
            )


# ==============================================================================
# GROUP D — confirm_request pre-empts escalation ("can you confirm a human will call me?")
# ==============================================================================

class TestConfirmRequestVsHumanRequest:
    """'can you confirm a human will call me?' — _HUMAN_REQUEST_RE does NOT match this phrase
    because the verb phrase is 'can you confirm' (not 'speak/talk/want a human'). So it falls
    through to _CONFIRM_REQUEST_RE and becomes confirm_request. This is a non-blocking policy
    question but should be documented: the agent confirms the callback time rather than escalating,
    which may be the right answer — but the human_request detection gap is real."""

    def test_can_you_confirm_human_will_call_is_confirm_not_human_request(self):
        """'can you confirm a human will call me?' falls through to confirm_request rather than
        human_request. This is the current behaviour; documenting it as known gap.
        Non-blocking since the downstream confirm_known is arguably correct (echoing the callback
        promise satisfies the intent), but it does mean the escalation path is never triggered."""
        act, obj, q = _classify_intent("can you confirm a human will call me?")
        # Currently: confirm_request. The test documents this known gap.
        # If this becomes human_request in a future fix, the test should pass trivially.
        known_gap = act == "confirm_request"
        # Not asserting a failure — documenting the gap so the orchestrator can decide.
        # The test FAILS only if it classifies as something OTHER than human_request or confirm_request.
        assert act in ("human_request", "confirm_request"), (
            f"NON-BLOCKING known gap: expected human_request or confirm_request for "
            f"'can you confirm a human will call me?', got {act!r}"
        )

    def test_please_confirm_human_advisor_will_follow_up(self):
        """'please confirm a human advisor will follow up' — confirm_request wins over human_request
        because _HUMAN_REQUEST_RE requires a stronger verb (speak/want/get me). Documents the gap."""
        act, obj, q = _classify_intent("please confirm a human advisor will follow up")
        assert act in ("human_request", "confirm_request"), (
            f"NON-BLOCKING gap: {act!r} for human-follow-up confirm phrase"
        )


# ==============================================================================
# GROUP E — gate-chain order: confirm_request fires BEFORE must_clear_objection
# ==============================================================================

class TestConfirmRequestGateChainOrdering:
    """In address_direct_input the confirm_request branch (line ~618) fires BEFORE the
    active_objection check (line ~633). This means if a turn is (mis)classified as
    confirm_request AND an objection is live, confirm_known is returned and the objection
    is never cleared. must_clear_objection (which runs later in apply_gates) only guards
    attempt_close, so it does not rescue the situation."""

    def test_confirm_request_with_active_price_objection_does_not_bypass_objection(self):
        """Caller: 'just confirm Thursday — also it's really too expensive.'
        If the price objection is live AND last_user_act is confirm_request, address_direct_input
        currently returns confirm_known — the price objection is ignored this turn.
        Correct: must_clear_objection or handle_objection should win when objection is live."""
        cfg = _make_config()
        b = BeliefState.fresh()
        b.set_slot("callback_window", "thursday after 3pm", 0.9)
        b.last_user_act = "confirm_request"
        b.active_objection = "price"
        b.open_question = "just confirm Thursday — also it's really too expensive"

        proposed = Decision(act="ask", target_slot="grade_level", rationale="discovery")
        out = gates.address_direct_input(proposed, b, cfg, history=[])
        # With the current implementation, confirm_request pre-empts the objection check.
        # Correct behaviour: when price objection is live, handle_objection must take priority.
        assert out.act == "handle_objection", (
            f"BLOCKING: with active_objection='price' AND confirm_request, address_direct_input "
            f"must route to handle_objection (not {out.act!r}). "
            f"The confirm_request branch pre-empts the objection check at gates.py ~618 vs ~633."
        )

    def test_confirm_request_with_active_objection_full_gate_chain(self):
        """Full apply_gates: confirm_request + active_objection='decision_maker'.
        must_clear_objection only blocks attempt_close; confirm_known passes through."""
        cfg = _make_config()
        b = BeliefState.fresh()
        b.set_slot("callback_window", "thursday after 3pm", 0.9)
        b.last_user_act = "confirm_request"
        b.active_objection = "decision_maker"
        b.open_question = "let me confirm Thursday — I also need to check with my wife"
        b.meta["learner_established"] = True

        proposed = Decision(act="ask", target_slot="grade_level", rationale="discovery")
        out = gates.apply_gates(proposed, b, cfg)
        # Current: confirm_known passes through (objection not cleared this turn by gate chain).
        # Desired: handle_objection wins when decision_maker is live.
        assert out.act == "handle_objection", (
            f"BLOCKING: full gate chain should prioritise handle_objection for open "
            f"decision_maker objection; got {out.act!r}."
        )


# ==============================================================================
# GROUP F — tutoring-day false positive
# ==============================================================================

class TestTutoringDayFalsePositive:
    """'she has tutoring Thursdays with her current tutor' — the current code returns None for
    this (the _SCHEDULING_CUES_RE cue isn't present for a bare day mention in the named-day
    branch). This test documents the current correct behaviour and guards against regression."""

    def test_tutoring_thursdays_with_current_tutor_not_callback_window(self):
        """'she has tutoring Thursdays with her current tutor' — existing tutoring schedule,
        NOT a proposed callback window. The extractor currently returns None (correct)."""
        result = _extract_callback_window("she has tutoring Thursdays with her current tutor")
        assert result is None, (
            f"'tutoring Thursdays' must NOT extract as callback_window; got {result!r}"
        )


# ==============================================================================
# GROUP G — confirm_request doesn't mask email/general confirm
# ==============================================================================

class TestConfirmRequestNonObjectionFalsePositives:
    """'I need to confirm my email is right' should not trigger confirm_known of callback_window.
    The intent classification itself fires confirm_request (the regex is too broad), but the
    downstream effect in the gate chain depends on whether callback_window is filled."""

    def test_confirm_email_intent_does_not_lock_callback_window(self):
        """If callback_window is NOT filled, address_direct_input's confirm_request branch
        sets target_slot=None (fallback). The NLG has nothing to echo. This is tolerable but
        the confirm_request label is still misleading for this case."""
        cfg = _make_config()
        b = BeliefState.fresh()
        # callback_window not filled
        b.last_user_act = "confirm_request"
        b.open_question = "I need to confirm my email is right"

        proposed = Decision(act="ask", target_slot="grade_level", rationale="discovery")
        out = gates.address_direct_input(proposed, b, cfg, history=[])
        # With no callback_window slot, target_slot=None — acceptable but confirm_request
        # is still wrong (this is an information question, not a time-confirmation).
        # Not asserting failure here; documenting the broad regex gap.
        assert out.target_slot is None or out.act in ("confirm_known", "answer_via_kb"), (
            f"NON-BLOCKING: 'confirm my email' with no callback_window slot should not "
            f"lock an unrelated slot; got act={out.act!r}, target_slot={out.target_slot!r}"
        )
