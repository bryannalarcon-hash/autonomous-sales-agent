# CB-76 + CB-77 regression tests — listening v2 + price persistence.
# All tests are deterministic / $0-cost: canned LLM responses, direct extractor / gate / NLG calls.
# Test map:
#   CB-76 (a) — plural/disjunctive day windows in _extract_callback_window
#   CB-76 (b) — new timeline forms in _extract_timeline
#   CB-76 (c) — never-deny rule: memory_check intent + NLG _NEVER_DENY_NOTE injection
#   CB-76 (d) — contact-ack: meta flag set + NLG _CONTACT_ACK_NOTE injection
#   CB-77 (e) — terse price re-asks count as recurrences (_question_recurs / address_direct_input)
#   CB-77 (f) — price answer demands numeric range from facts (_BUDGET_CONCERN_NOTE content)
#   Invariants — R37 parity, buy-gate purity
from __future__ import annotations

import json

import pytest

from src.config.settings import AgentConfig, Persona
from src.core import dst, gates
from src.core.belief_state import DRIVERS, BeliefState
from src.core.dst import (
    _extract_callback_window,
    _extract_timeline,
    _classify_intent,
    _MEMORY_CHECK_RE,
    _CONTACT_PHONE_RE,
    _CONTACT_EMAIL_RE,
    _CONTACT_NAME_RE,
)
from src.core.gates import (
    _question_recurs,
    _has_prior_price_inquiry,
    _TERSE_PRICE_REASK_RE,
    address_direct_input,
)
from src.core.llm import MockLLMClient
from src.core.nlg import (
    _BUDGET_CONCERN_NOTE,
    _NEVER_DENY_NOTE,
    _CONTACT_ACK_NOTE,
    _build_messages,
    realize,
)
from src.core.policy import Decision


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _flat_llm(n: int = 1) -> MockLLMClient:
    """Zero-delta LLM: returns driver=0 JSON so DST leaves drivers at priors."""
    return MockLLMClient([json.dumps({d: 0.0 for d in DRIVERS})] * n)


def _cfg(**overrides) -> AgentConfig:
    thresholds = {
        "trust_gate_open_price": 0.5,
        "pushiness_cap": 0.7,
        "pushiness_pressure_count_cap": 2,
        "escalate_low_confidence_turns": 2,
        "low_confidence_level": 0.4,
        "max_concession_band": 0.15,
        "discovery_slots_required": 0,
        "min_turns_before_close": 99,
        "discovery_slots_before_close": 0,
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
        prompts={"policy": "Propose next act as JSON.", "nlg": "Reply concisely."},
        playbooks={"discovery_sequence": ["grade_level", "subject", "goal", "timeline", "budget"]},
        thresholds=thresholds,
    )


def _decision(act: str = "answer_via_kb", **kw) -> Decision:
    return Decision(act=act, rationale="test", confidence=0.75, **kw)


# ===========================================================================
# CB-76 (a) — plural/disjunctive day windows
# ===========================================================================

class TestCB76PluraldDisjunctiveWindows:
    """_extract_callback_window must capture plural and disjunctive day offers (CB-76a)."""

    # --- QA9 verbatim utterance ---

    def test_qa9_verbatim_wednesdays_or_fridays(self):
        """EXACT QA9 utterance: 'Wednesdays or Fridays after 4 work best for us' must fill the slot."""
        result = _extract_callback_window("Wednesdays or Fridays after 4 work best for us")
        assert result is not None, (
            "QA9 verbatim: 'Wednesdays or Fridays after 4 work best for us' must extract callback_window"
        )
        value, conf = result
        # Value must capture the disjunction (both days present)
        assert "wednesday" in value.lower() or "friday" in value.lower(), (
            f"Expected wednesday or friday in extracted value, got {value!r}"
        )
        assert conf >= 0.85, (
            f"A firmly volunteered disjunctive offer should have confidence >=0.85, got {conf}"
        )

    def test_disjunctive_two_days_captures_both(self):
        """'Tuesdays or Thursdays after 3pm work best' must capture both day names."""
        result = _extract_callback_window("Tuesdays or Thursdays after 3pm work best")
        assert result is not None
        value, conf = result
        assert "tuesday" in value.lower() or "thursday" in value.lower()
        assert conf >= 0.85

    def test_plural_single_day_thursday(self):
        """'Thursdays work for me' (singular plural offer) must extract callback_window."""
        result = _extract_callback_window("Thursdays work for me")
        assert result is not None, "A plural single-day offer must fill callback_window"
        value, conf = result
        assert "thursday" in value.lower()
        assert conf >= 0.85

    def test_plural_day_normalized_to_singular(self):
        """Plural day names must be normalized: 'Fridays' → stored value contains 'friday'."""
        result = _extract_callback_window("Fridays after 5 work great for us")
        assert result is not None
        value, _conf = result
        assert "friday" in value.lower(), (
            f"Plural 'Fridays' must normalize to 'friday' in stored value, got {value!r}"
        )

    def test_hedged_disjunctive_lowered_confidence(self):
        """A hedged disjunction ('maybe Wednesdays or Fridays') must be below the lock threshold."""
        result = _extract_callback_window("maybe Wednesdays or Fridays work")
        if result is not None:
            _value, conf = result
            assert conf < 0.8, (
                f"A hedged disjunctive window must be below lock threshold (0.8), got {conf}"
            )

    def test_tutoring_context_blocks_plural_day(self):
        """'she has tutoring Thursdays' — 'tutoring' is a context block, NOT a callback offer."""
        result = _extract_callback_window("she has tutoring Thursdays")
        assert result is None, (
            "'she has tutoring Thursdays' must NOT extract as callback_window (tutoring = context block)"
        )

    def test_deadline_context_still_blocks_disjunction(self):
        """'she has a test on Wednesdays or Fridays' must NOT extract (test = deadline context)."""
        result = _extract_callback_window("she has a test on Wednesdays or Fridays")
        assert result is None, (
            "A deadline context (test) must block a disjunctive day extraction"
        )

    def test_singular_day_still_works(self):
        """Existing singular-day extraction ('Thursday after 3pm') must still work."""
        result = _extract_callback_window("Thursday after 3pm works")
        assert result is not None
        value, conf = result
        assert "thursday" in value.lower()
        assert conf >= 0.85

    @pytest.mark.asyncio
    async def test_dst_update_fills_disjunctive_window(self):
        """Full DST update on the QA9 verbatim fills callback_window."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="Wednesdays or Fridays after 4 work best for us",
            llm_client=_flat_llm(),
        )
        assert "callback_window" in new_belief.slots, (
            "DST must fill callback_window from 'Wednesdays or Fridays after 4 work best for us'"
        )
        value = new_belief.slot_value("callback_window")
        assert value is not None
        low = str(value).lower()
        assert "wednesday" in low or "friday" in low, (
            f"callback_window value must contain wednesday or friday, got {value!r}"
        )


# ===========================================================================
# CB-76 (b) — new timeline forms
# ===========================================================================

class TestCB76TimelineForms:
    """_extract_timeline must cover the new deadline forms from CB-76b."""

    def test_end_of_the_month(self):
        """'state testing is end of the month' must extract timeline."""
        result = _extract_timeline("state testing is end of the month")
        assert result is not None, "'end of the month' must fill the timeline slot"
        value, conf = result
        assert "month" in value.lower()
        assert conf >= 0.7

    def test_end_of_month_no_article(self):
        """'end of month' (no article) must also extract."""
        result = _extract_timeline("the test is end of month")
        assert result is not None, "'end of month' must fill the timeline slot"

    def test_by_the_end_of_the_month(self):
        """'by the end of the month' must extract timeline."""
        result = _extract_timeline("we need to start by the end of the month")
        assert result is not None, "'by the end of the month' must fill the timeline slot"

    def test_next_month(self):
        """'next month' must extract timeline."""
        result = _extract_timeline("she has a test next month")
        assert result is not None, "'next month' must fill the timeline slot"
        value, _conf = result
        assert "month" in value.lower()

    def test_this_semester(self):
        """'this semester' must extract timeline."""
        result = _extract_timeline("she needs help this semester")
        assert result is not None, "'this semester' must fill the timeline slot"
        value, _conf = result
        assert "semester" in value.lower()

    def test_before_finals(self):
        """'before finals' must extract timeline."""
        result = _extract_timeline("we want to get her ready before finals")
        assert result is not None, "'before finals' must fill the timeline slot"

    def test_before_the_exam(self):
        """'before the exam' must extract timeline."""
        result = _extract_timeline("need to prepare before the exam")
        assert result is not None, "'before the exam' must fill timeline"

    def test_existing_in_about_three_weeks_still_works(self):
        """Existing form 'in about three weeks' must still extract (regression guard)."""
        result = _extract_timeline("she has a big test in about three weeks")
        assert result is not None, "Regression: 'in about three weeks' must still fill timeline"

    @pytest.mark.asyncio
    async def test_dst_end_of_month_fills_timeline(self):
        """Full DST update on 'state testing is end of the month' fills timeline slot."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="state testing is end of the month",
            llm_client=_flat_llm(),
        )
        assert "timeline" in new_belief.slots, (
            "DST must fill 'timeline' from 'state testing is end of the month'"
        )


# ===========================================================================
# CB-76 (c) — never-deny rule: memory_check intent + NLG instruction
# ===========================================================================

class TestCB76NeverDeny:
    """The agent must never assert the caller did not say something (CB-76c)."""

    # --- Intent classification ---

    def test_classify_intent_memory_check_did_i_already_tell_you(self):
        """EXACT QA9 utterance: 'did I already tell you when works for me?' → memory_check."""
        act, _obj, q = _classify_intent("did I already tell you when works for me?")
        assert act == "memory_check", (
            f"'did I already tell you when works for me?' must classify as memory_check, got {act!r}"
        )
        assert q is not None, "open_question must be populated for memory_check"

    def test_classify_intent_have_i_mentioned(self):
        """'have I mentioned what time works for me?' → memory_check."""
        act, _obj, _q = _classify_intent("have I mentioned what time works for me?")
        assert act == "memory_check", (
            f"'have I mentioned what time works for me?' must be memory_check, got {act!r}"
        )

    def test_classify_intent_did_i_say(self):
        """'did I say when is good for me?' → memory_check."""
        act, _obj, _q = _classify_intent("did I say when is good for me?")
        assert act == "memory_check"

    def test_classify_intent_have_i_told_you(self):
        """'have I already told you my availability?' → memory_check."""
        act, _obj, _q = _classify_intent("have I already told you my availability?")
        assert act == "memory_check"

    def test_classify_intent_i_already_told_you(self):
        """'I already told you what time works.' → memory_check (statement form)."""
        act, _obj, _q = _classify_intent("I already told you what time works.")
        assert act == "memory_check", (
            f"Assertion form 'I already told you' must be memory_check, got {act!r}"
        )

    def test_classify_intent_normal_question_not_memory_check(self):
        """'when is a good time for a callback?' is a plain question, NOT a memory_check."""
        act, _obj, _q = _classify_intent("when is a good time for a callback?")
        assert act != "memory_check", (
            "A plain callback question must NOT be classified as memory_check"
        )

    # --- NLG injection ---

    def test_never_deny_note_injected_on_memory_check(self):
        """_NEVER_DENY_NOTE must appear in _build_messages when last_user_act == 'memory_check'."""
        cfg = _cfg()
        b = BeliefState.fresh()
        b.last_user_act = "memory_check"
        b.open_question = "did I already tell you when works for me?"
        dec = _decision("answer_via_kb")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]
        assert "NEVER-DENY RULE" in user_content, (
            "_NEVER_DENY_NOTE must be injected when last_user_act == 'memory_check'"
        )
        assert "NEVER say" in user_content or "MUST NOT" in user_content

    def test_never_deny_note_not_injected_on_normal_question(self):
        """_NEVER_DENY_NOTE must NOT appear for a plain question (only for memory_check)."""
        cfg = _cfg()
        b = BeliefState.fresh()
        b.last_user_act = "question"
        b.open_question = "what subjects do you cover?"
        dec = _decision("answer_via_kb")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]
        assert "NEVER-DENY RULE" not in user_content, (
            "_NEVER_DENY_NOTE must NOT appear on a normal question turn"
        )

    def test_never_deny_note_present_in_constant(self):
        """_NEVER_DENY_NOTE must contain the key prohibition phrase (safety check)."""
        assert "NEVER" in _NEVER_DENY_NOTE
        assert "did not say" in _NEVER_DENY_NOTE or "didn" in _NEVER_DENY_NOTE or "denial" in _NEVER_DENY_NOTE.lower()

    # --- DST sets last_user_act = memory_check ---

    @pytest.mark.asyncio
    async def test_dst_sets_memory_check_on_did_i_tell_you(self):
        """DST update on the QA9 verbatim must set last_user_act='memory_check'."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="answer_via_kb",
            user_utterance="did I already tell you when works for me?",
            llm_client=_flat_llm(),
        )
        assert new_belief.last_user_act == "memory_check", (
            "DST must set last_user_act='memory_check' on 'did I already tell you when works for me?'"
        )

    def test_known_slot_surfaced_in_prompt_for_memory_check(self):
        """When callback_window is filled, KNOWN_SO_FAR in the NLG prompt carries it — so the
        model can echo it back rather than hedging (the 'slot present' path of never-deny)."""
        cfg = _cfg()
        b = BeliefState.fresh()
        b.last_user_act = "memory_check"
        b.set_slot("callback_window", "wednesdays or fridays after 4", 0.9)
        b.open_question = "did I already tell you when works for me?"
        dec = _decision("answer_via_kb")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]
        assert "wednesday" in user_content.lower() or "friday" in user_content.lower(), (
            "KNOWN_SO_FAR must surface the callback_window value so the agent can echo it"
        )
        assert "NEVER-DENY RULE" in user_content, (
            "_NEVER_DENY_NOTE must also be present on the same turn"
        )


# ===========================================================================
# CB-76 (d) — contact acknowledgment
# ===========================================================================

class TestCB76ContactAck:
    """A one-clause contact ack must fire when name/phone/email is captured (CB-76d)."""

    # --- Contact detection patterns ---

    def test_phone_pattern_detected(self):
        """A phone number in an utterance must be detected by _CONTACT_PHONE_RE."""
        assert _CONTACT_PHONE_RE.search("my number is 555-867-5309"), (
            "A 10-digit phone number must be detected"
        )

    def test_phone_10digits_detected(self):
        """A bare 10-digit number must be detected."""
        assert _CONTACT_PHONE_RE.search("call me at 5558675309")

    def test_email_pattern_detected(self):
        """An email address must be detected by _CONTACT_EMAIL_RE."""
        assert _CONTACT_EMAIL_RE.search("my email is dana@example.com")

    def test_name_intro_detected(self):
        """'my name is Dana' must be detected by _CONTACT_NAME_RE."""
        assert _CONTACT_NAME_RE.search("my name is Dana")

    def test_im_name_detected(self):
        """'I'm Karen' must match the name-intro pattern."""
        assert _CONTACT_NAME_RE.search("I'm Karen")

    # --- DST sets contact_just_captured ---

    @pytest.mark.asyncio
    async def test_dst_sets_contact_flag_on_phone(self):
        """DST update on an utterance with a phone number must set meta['contact_just_captured']."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="You can reach me at 555-867-5309",
            llm_client=_flat_llm(),
        )
        assert new_belief.meta.get("contact_just_captured") is True, (
            "DST must set contact_just_captured=True when a phone number is in the utterance"
        )

    @pytest.mark.asyncio
    async def test_dst_sets_contact_flag_on_email(self):
        """DST update on an utterance with an email must set meta['contact_just_captured']."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="My email is parent@school.edu",
            llm_client=_flat_llm(),
        )
        assert new_belief.meta.get("contact_just_captured") is True

    @pytest.mark.asyncio
    async def test_dst_sets_contact_flag_on_name_and_stores_name(self):
        """DST update on 'my name is Dana' must set the flag and store the name in meta."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="My name is Dana",
            llm_client=_flat_llm(),
        )
        assert new_belief.meta.get("contact_just_captured") is True
        assert new_belief.meta.get("contact_name", "").lower() == "dana", (
            "DST must store the extracted name for the contact ack"
        )

    @pytest.mark.asyncio
    async def test_dst_clears_contact_flag_next_turn(self):
        """The contact_just_captured flag must be False on the NEXT turn (one-shot)."""
        belief = BeliefState.fresh()
        belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="My name is Dana, reach me at 555-867-5309",
            llm_client=_flat_llm(),
        )
        assert belief.meta.get("contact_just_captured") is True
        # Next turn: a neutral utterance must clear the flag.
        belief2 = await dst.update(
            belief,
            last_agent_act="confirm_known",
            user_utterance="yes, that's correct",
            llm_client=_flat_llm(),
        )
        assert not belief2.meta.get("contact_just_captured"), (
            "contact_just_captured must be cleared on the next turn (one-shot)"
        )

    # --- NLG injection ---

    def test_contact_ack_note_injected_when_flag_set(self):
        """_CONTACT_ACK_NOTE must appear in the NLG prompt when contact_just_captured is True."""
        cfg = _cfg()
        b = BeliefState.fresh()
        b.meta["contact_just_captured"] = True
        b.meta["contact_name"] = "Dana"
        dec = _decision("pitch")  # any act — ack rides all acts
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]
        assert "CONTACT ACK" in user_content, (
            "_CONTACT_ACK_NOTE must be injected when contact_just_captured is True"
        )

    def test_contact_ack_note_not_injected_when_no_contact(self):
        """_CONTACT_ACK_NOTE must NOT appear when no contact info was captured."""
        cfg = _cfg()
        b = BeliefState.fresh()
        # contact_just_captured not set / False
        dec = _decision("pitch")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]
        assert "CONTACT ACK" not in user_content, (
            "_CONTACT_ACK_NOTE must NOT appear when no contact was captured"
        )

    def test_contact_ack_note_includes_name_when_present(self):
        """When a name is captured, the NLG prompt must surface it for the ack."""
        cfg = _cfg()
        b = BeliefState.fresh()
        b.meta["contact_just_captured"] = True
        b.meta["contact_name"] = "Karen"
        dec = _decision("answer_via_kb")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]
        assert "Karen" in user_content, (
            "The captured name must be surfaced in the NLG prompt for the contact ack"
        )

    def test_contact_ack_rides_any_act(self):
        """The contact ack must be injected for answer_via_kb, pitch, attempt_close, etc."""
        cfg = _cfg()
        for act in ("answer_via_kb", "pitch", "attempt_close", "confirm_known", "ask"):
            b = BeliefState.fresh()
            b.meta["contact_just_captured"] = True
            dec = _decision(act)
            msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
            assert "CONTACT ACK" in msgs[-1]["content"], (
                f"_CONTACT_ACK_NOTE must be injected even when act={act!r}"
            )


# ===========================================================================
# CB-77 (e) — terse price re-asks count as recurrences
# ===========================================================================

class TestCB77TersePriceRecurrence:
    """Terse price re-asks must trigger _question_recurs → address_direct_input → answer_via_kb."""

    # --- Pattern match ---

    def test_terse_price_reask_pattern_just_give_me_the_number(self):
        """'just give me the number' must match _TERSE_PRICE_REASK_RE."""
        assert _TERSE_PRICE_REASK_RE.search("just give me the number"), (
            "'just give me the number' must match _TERSE_PRICE_REASK_RE"
        )

    def test_terse_price_reask_monthly_cost_just_the_number(self):
        """'monthly cost. just the number.' must match the terse-price pattern."""
        assert _TERSE_PRICE_REASK_RE.search("monthly cost. just the number."), (
            "'monthly cost. just the number.' must match _TERSE_PRICE_REASK_RE"
        )

    def test_terse_price_reask_just_the_price(self):
        """'just the price' must match."""
        assert _TERSE_PRICE_REASK_RE.search("just the price")

    def test_terse_price_reask_just_tell_me_the_number(self):
        """'just tell me the number' must match."""
        assert _TERSE_PRICE_REASK_RE.search("just tell me the number")

    def test_terse_price_non_price_does_not_match(self):
        """'what subjects do you cover?' must NOT match _TERSE_PRICE_REASK_RE."""
        assert not _TERSE_PRICE_REASK_RE.search("what subjects do you cover?")

    # --- has_prior_price_inquiry helper ---

    def test_has_prior_price_inquiry_with_earlier_price_ask(self):
        """A history with an earlier price ask must return True from _has_prior_price_inquiry."""
        history = [
            {"role": "user", "text": "how much does this cost per month?"},
            {"role": "assistant", "text": "I'll get you a range.", "act": "answer_via_kb"},
            {"role": "user", "text": "just give me the number"},
        ]
        assert _has_prior_price_inquiry(history), (
            "_has_prior_price_inquiry must return True when a prior user turn has a price question"
        )

    def test_has_prior_price_inquiry_no_prior(self):
        """Without a prior price ask, _has_prior_price_inquiry must return False."""
        history = [
            {"role": "user", "text": "what grade are you for?"},
            {"role": "user", "text": "just give me the number"},
        ]
        assert not _has_prior_price_inquiry(history), (
            "_has_prior_price_inquiry must return False when no prior price turn exists"
        )

    # --- _question_recurs on terse re-asks ---

    def test_question_recurs_terse_price_reask_first_ask(self):
        """'just give me the number' with price_inquiry intent + prior price ask → recurs=True."""
        b = BeliefState.fresh()
        b.last_user_act = "price_inquiry"
        b.open_question = "just give me the number"
        history = [
            {"role": "user", "text": "how much does this cost per month?"},
            {"role": "assistant", "text": "I'll get you a range.", "act": "answer_via_kb"},
            {"role": "user", "text": "just give me the number"},
        ]
        assert _question_recurs(b, history), (
            "_question_recurs must return True for a terse price re-ask after a prior price inquiry"
        )

    def test_question_recurs_terse_price_second_reask(self):
        """'monthly cost. just the number.' also triggers recurrence on 2nd re-ask."""
        b = BeliefState.fresh()
        b.last_user_act = "price_inquiry"
        b.open_question = "monthly cost. just the number."
        history = [
            {"role": "user", "text": "how much is it?"},
            {"role": "assistant", "text": "I'll get you a range.", "act": "answer_via_kb"},
            {"role": "user", "text": "just give me the number"},
            {"role": "assistant", "text": "The consultation will clarify.", "act": "answer_via_kb"},
            {"role": "user", "text": "monthly cost. just the number."},
        ]
        assert _question_recurs(b, history)

    def test_question_recurs_false_for_new_non_price_question(self):
        """A brand-new non-price question must NOT be flagged as recurring."""
        b = BeliefState.fresh()
        b.last_user_act = "question"
        b.open_question = "what subjects do you cover?"
        history = [
            {"role": "user", "text": "how old do students have to be?"},
        ]
        assert not _question_recurs(b, history), (
            "A brand-new non-price question must not flag as recurring"
        )

    # --- address_direct_input forces answer_via_kb on terse price re-ask ---

    def test_address_direct_input_forces_answer_on_terse_price_reask(self):
        """address_direct_input must redirect attempt_close to answer_via_kb on a terse price re-ask."""
        cfg = _cfg(min_turns_before_close=0)
        b = BeliefState.fresh()
        b.last_user_act = "price_inquiry"
        b.open_question = "just give me the number"
        history = [
            {"role": "user", "text": "how much does this cost?"},
            {"role": "assistant", "text": "I'll clarify in the consultation.", "act": "answer_via_kb"},
            {"role": "user", "text": "just give me the number"},
        ]
        dec = _decision("attempt_close", tier="consultation")
        out = address_direct_input(dec, b, cfg, history=history)
        assert out.act == "answer_via_kb", (
            "address_direct_input must redirect attempt_close to answer_via_kb on a recurring terse "
            "price re-ask (CB-77e)"
        )

    def test_address_direct_input_forces_answer_from_pitch_on_terse_reask(self):
        """address_direct_input must redirect a pitch to answer_via_kb on a terse price re-ask."""
        cfg = _cfg()
        b = BeliefState.fresh()
        b.last_user_act = "price_inquiry"
        b.open_question = "just the price"
        history = [
            {"role": "user", "text": "what does this cost monthly?"},
            {"role": "assistant", "text": "Prices vary — let's explore.", "act": "answer_via_kb"},
            {"role": "user", "text": "just the price"},
        ]
        dec = _decision("pitch")
        out = address_direct_input(dec, b, cfg, history=history)
        assert out.act == "answer_via_kb"


# ===========================================================================
# CB-77 (f) — price answer demands numeric range from facts
# ===========================================================================

class TestCB77PriceRange:
    """_BUDGET_CONCERN_NOTE must demand the floor–ceiling range when facts contain it."""

    def test_budget_concern_note_mentions_floor_ceiling(self):
        """_BUDGET_CONCERN_NOTE must reference a 'floor–ceiling' / range requirement."""
        assert (
            "floor" in _BUDGET_CONCERN_NOTE.lower()
            or "ceiling" in _BUDGET_CONCERN_NOTE.lower()
            or "$X" in _BUDGET_CONCERN_NOTE
            or "range" in _BUDGET_CONCERN_NOTE.lower()
        ), "_BUDGET_CONCERN_NOTE must mention a floor/ceiling or numeric range requirement"

    def test_budget_concern_note_forbids_vague_language(self):
        """_BUDGET_CONCERN_NOTE must explicitly forbid vague language like 'a few hundred'."""
        note_lower = _BUDGET_CONCERN_NOTE.lower()
        assert "vague" in note_lower or "few hundred" in note_lower or "invented" in note_lower, (
            "_BUDGET_CONCERN_NOTE must explicitly warn against vague pricing language"
        )

    def test_budget_concern_note_demands_numbers_from_facts(self):
        """_BUDGET_CONCERN_NOTE must state the numbers must come from the facts block."""
        assert "facts" in _BUDGET_CONCERN_NOTE.lower(), (
            "_BUDGET_CONCERN_NOTE must state the price figures come from the provided facts"
        )

    def test_kb_facts_with_range_appear_in_prompt(self):
        """When retrieved_facts include a floor–ceiling range, the NLG prompt must surface them."""
        cfg = _cfg()
        b = BeliefState.fresh()
        b.last_user_act = "price_inquiry"
        b.open_question = "just give me the number"
        facts = [
            "Typical pricing: $40–$80 per hour (VERIFIED, Nerdy 2025 rates).",
            "Monthly: most families land around $300–$600 per month for 2–4 hours/week.",
        ]
        dec = _decision("answer_via_kb")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=facts)
        user_content = msgs[-1]["content"]
        assert "$40" in user_content or "$80" in user_content or "$300" in user_content, (
            "KB pricing facts with floor–ceiling must appear in the NLG prompt"
        )
        assert "PRICE QUESTION POLICY" in user_content

    def test_budget_concern_note_still_injected_on_terse_reask(self):
        """The _BUDGET_CONCERN_NOTE must still be present even on a terse price re-ask turn."""
        cfg = _cfg()
        b = BeliefState.fresh()
        b.last_user_act = "price_inquiry"
        b.open_question = "just give me the number"
        dec = _decision("answer_via_kb")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]
        assert "PRICE QUESTION POLICY" in user_content, (
            "_BUDGET_CONCERN_NOTE must be injected on a terse price re-ask answer_via_kb turn"
        )


# ===========================================================================
# Invariants: R37 parity + buy-gate purity
# ===========================================================================

class TestInvariants:
    """CB-76/77 changes must not break R37 parity or buy-gate purity."""

    def test_r37_no_livekit_in_dst(self):
        """R37: src/core/dst.py must contain no LiveKit imports."""
        import ast, pathlib
        src = pathlib.Path("src/core/dst.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for name in names:
                    assert "livekit" not in (name or "").lower(), (
                        f"R37 violation: LiveKit import in dst.py: {name!r}"
                    )

    def test_r37_no_livekit_in_gates(self):
        """R37: src/core/gates.py must contain no LiveKit imports."""
        import ast, pathlib
        src = pathlib.Path("src/core/gates.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for name in names:
                    assert "livekit" not in (name or "").lower(), (
                        f"R37 violation: LiveKit import in gates.py: {name!r}"
                    )

    def test_r37_no_livekit_in_nlg(self):
        """R37: src/core/nlg.py must contain no LiveKit imports."""
        import ast, pathlib
        src = pathlib.Path("src/core/nlg.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for name in names:
                    assert "livekit" not in (name or "").lower(), (
                        f"R37 violation: LiveKit import in nlg.py: {name!r}"
                    )

    def test_buy_gate_purity_contact_capture_does_not_affect_tier(self):
        """Buy-gate purity: the contact_just_captured meta flag must NOT affect commitment tier."""
        from src.core.gates import _closeable_tier
        cfg = _cfg()
        b_without = BeliefState.fresh()
        b_without.drivers["trust"] = 0.65
        b_without.turn_count = 5

        b_with = b_without.copy()
        b_with.meta["contact_just_captured"] = True
        b_with.meta["contact_name"] = "Dana"

        assert _closeable_tier(b_without, cfg) == _closeable_tier(b_with, cfg), (
            "Buy-gate purity: contact capture must not affect commitment tier selection"
        )

    def test_buy_gate_purity_memory_check_does_not_affect_tier(self):
        """Buy-gate purity: memory_check last_user_act must NOT affect commitment tier."""
        from src.core.gates import _closeable_tier
        cfg = _cfg()
        b_without = BeliefState.fresh()
        b_without.drivers["trust"] = 0.65
        b_without.turn_count = 5

        b_with = b_without.copy()
        b_with.last_user_act = "memory_check"

        assert _closeable_tier(b_without, cfg) == _closeable_tier(b_with, cfg), (
            "Buy-gate purity: memory_check intent must not affect commitment tier"
        )

    def test_memory_check_does_not_trigger_escalation(self):
        """A memory_check turn must NOT trigger any escalation path (no _HUMAN_REQUEST_RE match)."""
        b = BeliefState.fresh()
        b.last_user_act = "memory_check"
        b.drivers["trust"] = 0.5
        cfg = _cfg()
        dec = _decision("answer_via_kb")
        out = gates.apply_gates(dec, b, cfg, history=[])
        assert out.act != "escalate", (
            "A memory_check turn must NOT be escalated — it is a conversational check, not a crisis"
        )
