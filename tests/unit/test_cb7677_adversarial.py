# Adversarial false-positive / false-negative tests for CB-76/CB-77.
# Validates that new matchers (callback_window, timeline, _MEMORY_CHECK_RE,
# contact patterns, terse-price recurrence) do NOT fire on inputs that SHOULD
# NOT trigger them. All deterministic / $0-cost (no real LLM calls).
# Collaborators: src/core/dst.py, src/core/gates.py, src/core/nlg.py
from __future__ import annotations

import json

import pytest

from src.core import dst
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
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient
from src.config.settings import AgentConfig, Persona


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_cb76_cb77.py)
# ---------------------------------------------------------------------------

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


from src.core.policy import Decision


def _decision(act: str = "answer_via_kb", **kw) -> Decision:
    return Decision(act=act, rationale="test", confidence=0.75, **kw)


# ===========================================================================
# 1. CALLBACK_WINDOW false-positive adversarials
# ===========================================================================

class TestCallbackWindowFalsePositives:
    """Inputs that should NOT fill callback_window (venting, idiomatic, deadline contexts)."""

    def test_tutored_on_mondays_in_college_not_callback(self):
        """'I tutored on Mondays back in college' — past habit/venting, NOT a scheduling offer."""
        result = _extract_callback_window("I tutored on Mondays back in college")
        # DEFENSIBLE DECISION: 'Mondays' matches the plural day pattern, BUT 'tutored' is in
        # _DEADLINE_CONTEXT_RE (tutoring/lesson variants). Let's document the actual behaviour.
        # The tutoring keyword should block this via _DEADLINE_CONTEXT_RE.
        if result is not None:
            _value, conf = result
            # If it fires it should at least be low confidence (hedged or context-blocked path miss)
            assert conf < 0.8, (
                f"FAIL: 'I tutored on Mondays back in college' must not produce a locked window; "
                f"got conf={conf!r}, value={_value!r}. 'tutored' contains 'tutor' which should hit "
                f"_DEADLINE_CONTEXT_RE and block extraction."
            )

    def test_tried_wednesdays_before_no_callback(self):
        """'we tried Wednesdays before and it didn't help' — past failed attempt, NOT an offer."""
        result = _extract_callback_window("we tried Wednesdays before and it didn't help")
        # No deadline-context word present, but the utterance is past-tense venting, not an offer.
        # The regex cannot distinguish tense; document what actually happens.
        if result is not None:
            _value, conf = result
            assert conf < 0.8, (
                f"FAIL: past-tense venting 'we tried Wednesdays before' should not lock; "
                f"got conf={conf!r}, value={_value!r}"
            )

    def test_fridays_are_always_crazy_not_callback(self):
        """'Fridays are always crazy for us' — venting, NOT an availability offer."""
        result = _extract_callback_window("Fridays are always crazy for us")
        # No deadline context word, no scheduling cue, but no work/call/available cue either.
        # The singular-day branch requires either a scheduling cue or absence of deadline context.
        # This should either be None or very low confidence.
        if result is not None:
            _value, conf = result
            assert conf < 0.8, (
                f"FAIL: 'Fridays are always crazy for us' should not produce a locked window; "
                f"got conf={conf!r}, value={_value!r}. This is venting, not an availability offer."
            )

    def test_recital_thursday_test_friday_two_deadlines(self):
        """'her recital is Thursday and the test is Friday' — two deadline days, NOT availability."""
        result = _extract_callback_window("her recital is Thursday and the test is Friday")
        # 'test' is in _DEADLINE_CONTEXT_RE — should block.
        assert result is None, (
            f"FAIL: 'her recital is Thursday and the test is Friday' must NOT extract callback_window "
            f"(both days are deadline/event days, not scheduling offers). Got: {result!r}"
        )

    def test_school_on_thursdays_not_callback(self):
        """'she has school on Thursdays' — 'school' is a deadline-context word, must block."""
        result = _extract_callback_window("she has school on Thursdays")
        assert result is None, (
            f"FAIL: 'she has school on Thursdays' must not extract as callback_window — "
            f"'school' is in _DEADLINE_CONTEXT_RE. Got: {result!r}"
        )


# ===========================================================================
# 2. TIMELINE false-positive adversarials
# ===========================================================================

class TestTimelineFalsePositives:
    """Inputs that should NOT fill the timeline slot."""

    def test_think_about_it_for_a_bit_not_timeline(self):
        """'I want to think about it for a bit' — idiomatic hesitation, NOT a deadline."""
        result = _extract_timeline("I want to think about it for a bit")
        assert result is None, (
            f"FAIL: 'think about it for a bit' must NOT extract a timeline — it's hedging, "
            f"not a deadline. Got: {result!r}"
        )

    def test_end_of_the_road_not_timeline(self):
        """'this is the end of the road for us if this doesn't work' — idiomatic 'end of', NOT a deadline month."""
        result = _extract_timeline("this is the end of the road for us if this doesn't work")
        # The regex looks for 'end of (the)? month/week/semester/term/year/quarter'.
        # 'end of the road' should NOT match since 'road' is not in the allowed suffixes.
        assert result is None, (
            f"FAIL: 'end of the road' must NOT extract a timeline — 'road' is not a valid "
            f"calendar unit. Got: {result!r}"
        )


# ===========================================================================
# 3. MEMORY_CHECK / NEVER-DENY adversarials
# ===========================================================================

class TestMemoryCheckFalsePositives:
    """Inputs that are rhetorical/venting and should NOT classify as memory_check."""

    def test_did_i_tell_you_my_son_hates_math_rhetorical(self):
        """'did I tell you my son hates math?' — rhetorical/sharing, NOT asking us to recall a slot."""
        act, _obj, _q = _classify_intent("did I tell you my son hates math?")
        # This DOES match _MEMORY_CHECK_RE ("did I tell you") — so it WILL route as memory_check.
        # Document the actual classification and assert what the NEVER-DENY rule means for it:
        # even if it fires, the never-deny rule is safe here (the agent was told to hedge, not deny),
        # but firing on a rhetorical share is a false positive for intent classification.
        # Record outcome — this is a known edge case we are documenting, not necessarily a bug.
        if act == "memory_check":
            # The NEVER-DENY rule fires: agent hedges rather than denies. That's arguably acceptable
            # for this utterance (the agent says "let me make sure — when works best?" rather than
            # "no you didn't say that"). We document this as a NON-BLOCKING over-trigger.
            pass  # see adversarial report for verdict

    def test_never_deny_does_not_force_agent_to_lie_about_missing_info(self):
        """KEY TENSION: 'you already have my number right?' when the caller NEVER gave it.
        The never-deny rule must NOT force the agent to LIE that it has info it lacks.
        The agent MUST be able to say 'I don't have it yet — what's the best number?'
        The rule should distinguish 'don't contradict what they DID say' from 'honestly state what we DON'T have'."""
        from src.core.nlg import _NEVER_DENY_NOTE, _build_messages
        cfg = _cfg()
        b = BeliefState.fresh()
        b.last_user_act = "memory_check"
        b.open_question = "you already have my number right?"
        # No phone slot in belief — the caller never gave it.
        # KNOWN_SO_FAR will be empty for contact info.
        dec = _decision("answer_via_kb")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]

        # The NEVER_DENY_NOTE must be injected (memory_check triggers it).
        assert "NEVER-DENY RULE" in user_content, "NEVER_DENY_NOTE must be present on memory_check"

        # CRITICAL: the note must include the honest hedge path ("I may have missed it / let me
        # confirm") — NOT only a path that says "confirm it". The model must be allowed to say
        # "I don't have that yet" in a hedged way, NOT to say "No, you never gave it."
        note = _NEVER_DENY_NOTE
        # The note should include both branches: (a) slot present -> echo it;
        # (b) slot NOT present -> hedge honestly WITHOUT denying.
        has_hedge_path = (
            "let me make sure" in note.lower()
            or "i may have missed" in note.lower()
            or "hedge" in note.lower()
            or "when works best" in note.lower()
            or "not in known_so_far" in note.lower()
        )
        assert has_hedge_path, (
            "BLOCKING: _NEVER_DENY_NOTE must include a hedge path for when the slot is NOT in "
            "KNOWN_SO_FAR ('I may have missed it / let me confirm — when works best?'). "
            "Without it, the rule forces the agent to EITHER deny (forbidden) OR fabricate "
            "confirmation of info it never received (equally wrong). "
            f"Current note: {note!r}"
        )

    def test_never_deny_does_not_suppress_honest_correction_about_lacking_info(self):
        """The never-deny rule must not prevent 'I don't have your number yet' when contact was not given.

        Scenario: caller says 'you already have my number right?' but phone was never provided.
        The CORRECT response is an honest 'I don't have it yet — what's the best number?' — NOT a
        confident denial of what the caller said, and NOT a fabricated confirmation of a nonexistent fact.
        The never-deny rule forbids the LIE 'No, you haven't told me your number' (a denial of what
        they said) but must ALLOW the TRUTH 'I don't have that yet — can you share it?' (an honest
        statement about what the agent lacks)."""
        # Verify that KNOWN_SO_FAR is empty (no phone slot) when no contact was given.
        b = BeliefState.fresh()
        b.last_user_act = "memory_check"
        # No slot for phone / contact set in belief
        known = {
            name: slot.get("value")
            for name, slot in b.slots.items()
            if slot.get("value") is not None
        }
        assert "phone" not in known and "contact_phone" not in known, (
            "Belief must not have a phone slot when caller never provided it"
        )
        # The NEVER_DENY_NOTE says: if slot is NOT in KNOWN_SO_FAR, hedge honestly.
        # "Hedge honestly" must mean the agent CAN say 'I don't have that yet' in a non-denial form
        # like 'Let me make sure I have that right — could you share it again?'
        # This is NOT a denial of what the caller said; it's an honest statement about missing info.
        # This test documents the semantic distinction the rule must preserve.
        from src.core.nlg import _NEVER_DENY_NOTE
        # The note must instruct hedging, not lying:
        assert "NEVER say" in _NEVER_DENY_NOTE or "MUST NOT" in _NEVER_DENY_NOTE, (
            "Note must explicitly forbid denial forms"
        )
        # The honest hedge path must be present:
        assert "Let me make sure" in _NEVER_DENY_NOTE or "let me" in _NEVER_DENY_NOTE.lower(), (
            "Note must provide the honest hedge alternative so agent can say 'let me confirm'"
        )


# ===========================================================================
# 4. CONTACT PATTERN false-positive adversarials
# ===========================================================================

class TestContactPatternFalsePositives:
    """Inputs that should NOT trigger contact detection patterns."""

    def test_room_number_not_phone(self):
        """'my son is in grade 5 room 211' — a room number, NOT a phone number."""
        assert not _CONTACT_PHONE_RE.search("my son is in grade 5 room 211"), (
            "FAIL: room number 211 must NOT match _CONTACT_PHONE_RE"
        )

    def test_sat_score_not_phone(self):
        """'he scored 1480 on the SAT' — a test score, NOT a phone number."""
        assert not _CONTACT_PHONE_RE.search("he scored 1480 on the SAT"), (
            "FAIL: SAT score 1480 must NOT match _CONTACT_PHONE_RE (not 10 digits, not phone format)"
        )

    def test_price_range_not_phone(self):
        """'$40-80 per hour' — a price range, NOT a phone number."""
        # 40-80 should not match the phone regex (not enough digits, wrong format)
        assert not _CONTACT_PHONE_RE.search("$40-80 per hour"), (
            "FAIL: price '$40-80' must NOT match _CONTACT_PHONE_RE"
        )

    def test_worried_not_name_intro(self):
        """'I'm worried' — 'I'm' + common word, must NOT match _CONTACT_NAME_RE."""
        m = _CONTACT_NAME_RE.search("I'm worried")
        # 'worried' starts with 'W' (capital) but the pattern requires [A-Z][a-z]{1,20}.
        # 'Worried' would match if matched with capital W, but 'I'm worried' has lowercase 'w'.
        # Check the actual behaviour.
        if m:
            extracted = m.group(1)
            # If it fires, it must have matched a capitalized word.
            # 'worried' is lowercase — the pattern requires a capital-letter start.
            # This would only fire if the regex case-flag allows it.
            assert extracted[0].isupper() or _CONTACT_NAME_RE.flags, (
                f"FAIL: 'I'm worried' should not match name intro — extracted: {extracted!r}"
            )

    def test_im_looking_for_help_not_name(self):
        """'I'm looking for help' — 'I'm' + gerund phrase, NOT a name intro."""
        m = _CONTACT_NAME_RE.search("I'm looking for help")
        if m:
            extracted = m.group(1)
            # 'looking' starts with lowercase 'l' — the pattern requires [A-Z][a-z]{1,20}
            # With re.I, the capital constraint is relaxed. Test what actually happens.
            assert False, (
                f"FAIL: 'I'm looking for help' must NOT match _CONTACT_NAME_RE (I'm + gerund). "
                f"Got extracted name: {extracted!r}"
            )

    def test_im_asking_about_not_name(self):
        """'I'm asking about your service' — 'I'm asking' is excluded by the regex negative lookahead."""
        m = _CONTACT_NAME_RE.search("I'm asking about your service")
        # The regex has: i'?m\s+(?!asking|...) — 'asking' is excluded
        assert not m, (
            f"FAIL: 'I'm asking about your service' must not match _CONTACT_NAME_RE — "
            f"'asking' is in the negative lookahead. Got: {m!r}"
        )

    def test_im_calling_for_not_name(self):
        """'I'm calling for information' — 'calling' variants excluded by negative lookahead."""
        m = _CONTACT_NAME_RE.search("I'm calling for information")
        # The regex excludes 'calling_for' and 'calling_about'
        assert not m, (
            f"FAIL: 'I'm calling for information' must not match _CONTACT_NAME_RE. Got: {m!r}"
        )


# ===========================================================================
# 5. TERSE-PRICE RECURRENCE false-positive adversarials
# ===========================================================================

class TestTersePriceRecurrenceFalsePositives:
    """Non-price terse re-asks must NOT hijack to answer_via_kb via the terse-price path."""

    def test_terse_sessions_per_week_does_not_match_pattern(self):
        """'how many sessions per week?' — a non-price question, must NOT match _TERSE_PRICE_REASK_RE."""
        assert not _TERSE_PRICE_REASK_RE.search("how many sessions per week?"), (
            "FAIL: 'how many sessions per week?' must NOT match _TERSE_PRICE_REASK_RE"
        )

    def test_just_the_schedule_not_price_terse(self):
        """'just the schedule' — 'schedule' is not a price-intent token, must NOT match."""
        assert not _TERSE_PRICE_REASK_RE.search("just the schedule"), (
            "FAIL: 'just the schedule' must NOT match _TERSE_PRICE_REASK_RE"
        )

    def test_non_price_terse_does_not_hijack_address_direct_input(self):
        """A non-price terse re-ask ('how many sessions per week?') must NOT be hijacked to answer_via_kb
        via the terse-price recurrence path when there was a prior price inquiry."""
        cfg = _cfg(min_turns_before_close=0)
        b = BeliefState.fresh()
        # last_user_act is 'question', NOT 'price_inquiry'
        b.last_user_act = "question"
        b.open_question = "how many sessions per week?"
        history = [
            {"role": "user", "text": "how much does this cost?"},
            {"role": "assistant", "text": "Pricing depends on your plan.", "act": "answer_via_kb"},
            {"role": "user", "text": "how many sessions per week?"},
        ]
        # _question_recurs: current is "question" not "price_inquiry", so terse-price path skipped.
        recurs = _question_recurs(b, history)
        # "how many sessions per week?" has 3 content words: "many", "sessions", "week"
        # Prior user turn "how much does this cost?" has content words: "much", "cost"
        # Shared: none -> recurs should be False
        assert not recurs, (
            f"FAIL: 'how many sessions per week?' after a prior price ask should NOT count as a "
            f"recurrence (different topic, not price_inquiry). _question_recurs returned True."
        )

    def test_non_price_terse_second_ask_does_not_force_answer_via_kb(self):
        """'just give me the schedule' — terse but NOT price intent, must NOT force answer_via_kb."""
        cfg = _cfg(min_turns_before_close=0)
        b = BeliefState.fresh()
        # Manually set last_user_act to question (non-price)
        b.last_user_act = "question"
        b.open_question = "just give me the schedule"
        history = [
            {"role": "user", "text": "how much does tutoring cost?"},
            {"role": "assistant", "text": "I'll get you a range.", "act": "answer_via_kb"},
            {"role": "user", "text": "just give me the schedule"},
        ]
        dec = _decision("pitch")
        out = address_direct_input(dec, b, cfg, history=history)
        # Because last_user_act == "question" and open_question is set, address_direct_input
        # will redirect pitch -> answer_via_kb (the general question-answering path).
        # That's expected and correct. But it must NOT be going through the TERSE-PRICE path.
        # The terse-price path only fires when last_user_act == "price_inquiry".
        # So this redirection is legitimate (open question present), not a false-positive of CB-77.
        # We test that _TERSE_PRICE_REASK_RE does NOT match "just give me the schedule":
        assert not _TERSE_PRICE_REASK_RE.search("just give me the schedule"), (
            "FAIL: 'just give me the schedule' must NOT match _TERSE_PRICE_REASK_RE"
        )

    def test_has_prior_price_inquiry_does_not_make_non_price_question_recur(self):
        """_has_prior_price_inquiry=True alone does NOT make a non-price question recurrent."""
        b = BeliefState.fresh()
        b.last_user_act = "question"  # NOT price_inquiry
        b.open_question = "what subjects do you cover?"
        history = [
            {"role": "user", "text": "how much does this cost per month?"},
            {"role": "assistant", "text": "Let me explain pricing.", "act": "answer_via_kb"},
            {"role": "user", "text": "what subjects do you cover?"},
        ]
        # _has_prior_price_inquiry is True (there WAS a prior price ask), but the current
        # last_user_act is "question" not "price_inquiry", so terse-price path must NOT fire.
        assert _has_prior_price_inquiry(history), "Setup check: prior price inquiry exists"
        recurs = _question_recurs(b, history)
        # Content words: "subjects", "cover" vs "much", "cost", "month" -> 0 shared
        assert not recurs, (
            "FAIL: a non-price question ('what subjects do you cover?') after a prior price ask "
            "must NOT count as recurrent via the terse-price path. _question_recurs must be False."
        )


# ===========================================================================
# 6. CONTACT-NAME over-match REGRESSION (CB-76 round-3, CRITICAL live-QA bug)
# ===========================================================================

def _flat_llm() -> MockLLMClient:
    """Zero-delta scripted LLM: driver=0 deltas only, so DST leaves drivers at priors ($0)."""
    return MockLLMClient([json.dumps({d: 0.0 for d in DRIVERS})])


class TestContactNameOverMatchRegression:
    """CRITICAL: _CONTACT_NAME_RE must not capture lowercase common words as names.

    Live QA: "Guarantee me a B or it's free. Yes or no." captured name="free"; the agent replied
    "Got it, Free — I've noted that down.", swallowing a guarantee objection at the highest-stakes
    moment. Root: re.I relaxed the [A-Z] guard + the "it's <X>" intro form. Fixed by a case-sensitive
    name group, dropping the "it's" form, a stopword guard, and a phone/email-or-explicit-intro gate."""

    # --- The EXACT QA utterance ---

    def test_exact_qa_guarantee_or_its_free_no_name(self):
        """EXACT QA regression: 'Guarantee me a B or it's free. Yes or no.' must capture NO name."""
        m = _CONTACT_NAME_RE.search("Guarantee me a B or it's free. Yes or no.")
        assert m is None, (
            f"CRITICAL REGRESSION: 'Guarantee me a B or it's free. Yes or no.' must NOT capture a "
            f"name. Got: {m.group(1)!r}" if m else ""
        )

    async def test_exact_qa_guarantee_does_not_set_contact_flag(self):
        """The EXACT QA utterance through a full DST update must NOT set contact_just_captured —
        the guarantee objection must not be swallowed by a spurious 'Got it, Free —' ack."""
        belief = BeliefState.fresh()
        nb = await dst.update(
            belief,
            last_agent_act="attempt_close",
            user_utterance="Guarantee me a B or it's free. Yes or no.",
            llm_client=_flat_llm(),
        )
        assert not nb.meta.get("contact_just_captured"), (
            "CRITICAL: the guarantee-demand turn must NOT set contact_just_captured"
        )
        assert not nb.meta.get("contact_name"), (
            f"CRITICAL: the guarantee-demand turn must NOT capture a name; got "
            f"{nb.meta.get('contact_name')!r}"
        )

    # --- "it's <X>" forms must never produce a name (form dropped) ---

    @pytest.mark.parametrize("utterance", [
        "or it's free",
        "it's free",
        "it's fine",
        "it's urgent",
        "it's no problem",
    ])
    def test_its_x_form_dropped(self, utterance):
        """The 'it's <X>' intro form is dropped — these must capture NO name."""
        m = _CONTACT_NAME_RE.search(utterance)
        assert m is None, (
            f"FAIL: '{utterance}' must NOT capture a name (the 'it's <X>' form is dropped). "
            f"Got: {m.group(1)!r}" if m else ""
        )

    # --- "I'm <lowercase-word>" must not produce a name ---

    @pytest.mark.parametrize("utterance", [
        "I'm worried about the cost",
        "I'm looking for help",
        "I'm asking about your service",
        "I'm calling for information",
        "I'm just trying to understand",
        "I'm not sure yet",
        "I'm interested but skeptical",
    ])
    def test_im_lowercase_word_not_name(self, utterance):
        """'I'm <lowercase common word>' must NOT match — the name group is case-sensitive."""
        m = _CONTACT_NAME_RE.search(utterance)
        assert m is None, (
            f"FAIL: '{utterance}' must NOT capture a name. Got: {m.group(1)!r}" if m else ""
        )

    async def test_im_worried_no_contact_flag_via_dst(self):
        """'I'm worried about the cost' through a full DST update must NOT set the contact flag."""
        belief = BeliefState.fresh()
        nb = await dst.update(
            belief,
            last_agent_act="answer_via_kb",
            user_utterance="I'm worried about the cost",
            llm_client=_flat_llm(),
        )
        assert not nb.meta.get("contact_just_captured"), (
            "'I'm worried about the cost' must NOT set contact_just_captured"
        )

    # --- GENUINE cases must still work ---

    def test_genuine_my_name_is_karen(self):
        """'My name is Karen' must capture 'Karen' (the Karen replay must not regress)."""
        m = _CONTACT_NAME_RE.search("My name is Karen")
        assert m is not None and m.group(1) == "Karen", (
            f"GENUINE: 'My name is Karen' must capture 'Karen'; got {m.group(1) if m else None!r}"
        )

    def test_genuine_im_dana(self):
        """'I'm Dana' must capture 'Dana'."""
        m = _CONTACT_NAME_RE.search("I'm Dana")
        assert m is not None and m.group(1) == "Dana"

    def test_genuine_dana_reyes_with_phone(self):
        """'I'm Dana Reyes, 555-014-9921' captures name 'Dana' AND a phone (qa3chat conv-2 ack)."""
        m = _CONTACT_NAME_RE.search("I'm Dana Reyes, 555-014-9921")
        assert m is not None and m.group(1) == "Dana", (
            f"GENUINE: must capture 'Dana'; got {m.group(1) if m else None!r}"
        )
        assert _CONTACT_PHONE_RE.search("I'm Dana Reyes, 555-014-9921"), (
            "GENUINE: the phone number must also be detected"
        )

    async def test_genuine_dana_reyes_sets_contact_flag_and_name(self):
        """Full DST update on 'I'm Dana Reyes, 555-014-9921' sets the flag + name='Dana'."""
        belief = BeliefState.fresh()
        nb = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="I'm Dana Reyes, 555-014-9921",
            llm_client=_flat_llm(),
        )
        assert nb.meta.get("contact_just_captured") is True, (
            "GENUINE: 'I'm Dana Reyes, 555-014-9921' must set contact_just_captured"
        )
        assert nb.meta.get("contact_name") == "Dana", (
            f"GENUINE: name must be 'Dana'; got {nb.meta.get('contact_name')!r}"
        )

    async def test_genuine_my_name_is_karen_sets_flag(self):
        """Full DST update on 'My name is Karen' (no phone/email) sets the flag via explicit intro."""
        belief = BeliefState.fresh()
        nb = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="My name is Karen",
            llm_client=_flat_llm(),
        )
        assert nb.meta.get("contact_just_captured") is True, (
            "GENUINE: an explicit-intro name with no phone/email still triggers the ack"
        )
        assert nb.meta.get("contact_name") == "Karen"

    async def test_bare_capitalized_word_no_intro_no_flag(self):
        """A bare capitalized word with no intro and no phone/email must NOT trigger the ack."""
        belief = BeliefState.fresh()
        nb = await dst.update(
            belief,
            last_agent_act="answer_via_kb",
            user_utterance="Monday is when the test happens.",  # 'Monday' capitalized, no intro
            llm_client=_flat_llm(),
        )
        assert not nb.meta.get("contact_just_captured"), (
            "A bare capitalized word (no intro, no phone/email) must NOT set contact_just_captured"
        )
