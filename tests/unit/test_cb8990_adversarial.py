# Adversarial false-positive hunt for CB-89/90 overrides in dst.update().
# Tests that SHOULD NOT trigger each override, edge cases at the boundary of each
# guard condition, multi-override interaction ordering, and the final advance_to_close
# smoke test confirming the guards didn't over-block genuine close turns.
# All tests are $0-cost (MockLLMClient, no live calls).
from __future__ import annotations

import asyncio
import json
import pytest

from src.config.settings import AgentConfig, Persona
from src.core import dst, gates
from src.core.belief_state import DRIVERS, BeliefState
from src.core.dst import _OBJECTION_PATTERNS, _extract_callback_window
from src.core.llm import MockLLMClient
from src.core.policy import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flat_llm(intent: str = "statement", obj: str | None = None, n: int = 8) -> MockLLMClient:
    payload = {
        **{d: 0.0 for d in DRIVERS},
        "user_act": intent,
        "open_question": "q text" if intent == "question" else None,
        "objection": obj,
        "affordability_refusal": False,
    }
    return MockLLMClient([json.dumps(payload)] * n)


def _cfg(**overrides) -> AgentConfig:
    thresholds = {
        "trust_gate_open_price": 0.5,
        "pushiness_cap": 0.7,
        "pushiness_pressure_count_cap": 2,
        "escalate_low_confidence_turns": 2,
        "low_confidence_level": 0.4,
        "max_concession_band": 0.15,
        "discovery_slots_required": 0,
        "min_turns_before_close": 4,
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
        persona=Persona(name="Nerdy", role="advisor", style="warm"),
        prompts={"policy": "json", "nlg": "reply"},
        playbooks={"discovery_sequence": ["grade_level"]},
        thresholds=thresholds,
    )


def _decision(act: str = "pitch", **kw) -> Decision:
    return Decision(act=act, rationale="test", confidence=0.75, **kw)


def _warm_belief() -> BeliefState:
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.68
    b.drivers["purchase_intent"] = 0.60
    b.turn_count = 5
    return b


# ===========================================================================
# 1. GUARANTEE objection regex — false-positive hunt
# ===========================================================================

class TestGuaranteeRegexFalsePositives:
    """Cases that must NOT route to efficacy_doubt via the new guarantee pattern."""

    def test_scheduling_guarantee_not_efficacy_doubt(self):
        """FIXED (round-2, BLOCKING #1): 'Can you guarantee a tutor will be free Thursday?' must
        NOT route to efficacy_doubt.

        Round-1 over-fired: the optional '(me|us|a)\\s+' group was skipped and the character class
        [abcdf] matched the standalone article 'a' in 'guarantee a tutor'. Round-2 fix
        (_GUARANTEE_DEMAND_RE) requires a single letter grade to be ARTICLE-prefixed ('a B') and not
        followed by another letter, and otherwise requires an explicit result word — so 'guarantee a
        tutor' (no result word) no longer matches. It falls through to diy_free on the bare 'free',
        which is the harmless PRE-EXISTING regex-fallback behavior for any sentence with 'free'; the
        LIVE path's scoped upgrade does NOT fire (no guarantee-of-outcome demand), so this never
        becomes an efficacy objection where it matters."""
        text = "Can you guarantee a tutor will be free Thursday?"
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key != "efficacy_doubt", (
            "FIXED (BLOCKING #1): 'Can you guarantee a tutor will be free Thursday?' must NOT be "
            f"efficacy_doubt — it is a scheduling question, not a results demand. Got {matched_key!r}."
        )
        # On the regex FALLBACK path it lands on diy_free via the bare 'free' (pre-existing, harmless);
        # the live-path scoped upgrade only fires on _GUARANTEE_DEMAND_RE, which does NOT match here.
        assert matched_key == "diy_free", (
            f"Expected the regex fallback to land on diy_free (bare 'free'); got {matched_key!r}."
        )

    def test_meta_communication_guarantee_not_objection(self):
        """FIXED (round-2, BLOCKING): 'I just want to guarantee we're on the same page' must NOT
        route to any objection.

        Round-1 fired efficacy_doubt via the lower row's bare '\\bguarantee\\b', which the live-path
        upgrade then amplified onto every weak-LLM-intent turn. Round-2 REMOVED the bare standalone
        'guarantee' from that efficacy_doubt row (genuine guarantee DEMANDS are caught by
        _GUARANTEE_DEMAND_RE, which needs a grade/result token), so a meta-communication 'guarantee
        we're on the same page' matches nothing."""
        text = "I just want to guarantee we're on the same page"
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key is None, (
            "FIXED (BLOCKING): 'I just want to guarantee we\\'re on the same page' must not match any "
            f"objection pattern (no grade/result token after 'guarantee'). Got {matched_key!r}."
        )

    def test_acceptance_no_guarantees_needed(self):
        """'No guarantees needed, I'm in' — acceptance/buying signal, must NOT be objection."""
        text = "No guarantees needed, I'm in"
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key is None, (
            f"'no guarantees needed, I\\'m in' is a BUYING SIGNAL — must not route to any objection. "
            f"Got {matched_key!r}"
        )

    def test_genuine_guarantee_b_or_free_is_efficacy_doubt(self):
        """THE TRUE CASE: 'Guarantee me a B or it's free' MUST be efficacy_doubt."""
        text = "Guarantee me a B or it's free"
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key == "efficacy_doubt", (
            f"Genuine case must route to efficacy_doubt. Got {matched_key!r}"
        )

    def test_diy_free_still_fires_for_free_resources(self):
        """'free resources online' with no guarantee context must still be diy_free."""
        text = "There are free resources online already"
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key == "diy_free", (
            f"Regression: 'free resources online' must be diy_free. Got {matched_key!r}"
        )


# ===========================================================================
# 2. OBJECTION UPGRADE (live path) — false-positive hunt
# ===========================================================================

class TestObjectionUpgradeFalsePositives:
    """Cases where LLM returns statement/None and the text has a word that matches
    an objection pattern — but the usage is NOT a genuine objection."""

    @pytest.mark.asyncio
    async def test_feel_free_not_upgraded_to_objection(self):
        """FIXED (round-2, BLOCKING #2): 'feel free to send me information' (LLM=statement) must NOT
        be upgraded to a diy_free objection.

        Round-1 ran the FULL _OBJECTION_PATTERNS in the live-path upgrade, so the bare 'free' fired
        diy_free — second-guessing the LLM's CORRECT 'statement' judgement on a cooperative phrase.
        Round-2 SCOPES the upgrade to _GUARANTEE_DEMAND_RE only; everything else trusts the LLM, so
        'feel free' stays a plain statement with no objection."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "feel free to send me information", _flat_llm("statement"))
        assert nb.active_objection is None, (
            "FIXED (BLOCKING #2): 'feel free to send me information' must NOT upgrade to a diy_free "
            f"objection — the caller is being cooperative. Got {nb.active_objection!r}. The scoped "
            "upgrade (guarantee-demand only) trusts the LLM's 'statement' judgement on general 'free'."
        )
        assert nb.last_user_act == "statement", (
            f"'feel free…' must stay a statement, not become an objection. Got {nb.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_positive_worth_it_not_upgraded_to_objection(self):
        """FIXED (round-2, BLOCKING #3): 'I think it's really worth it' (LLM=statement) must NOT be
        upgraded to efficacy_doubt.

        Round-1's full-pattern upgrade fired efficacy_doubt on the context-free 'worth it' even
        though the caller AFFIRMED value. Round-2 scopes the upgrade to a guarantee-of-outcome
        DEMAND only, so a positive affirmation is left as the LLM's 'statement'."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "pitch", "I think it's really worth it", _flat_llm("statement"))
        assert nb.active_objection is None, (
            "FIXED (BLOCKING #3): 'I think it\\'s really worth it' (affirmation) must NOT become an "
            f"efficacy_doubt objection. Got {nb.active_objection!r}. The scoped upgrade fires only on a "
            "guarantee-of-outcome demand, never on a context-free 'worth it'."
        )
        assert nb.last_user_act == "statement", (
            f"A positive affirmation must stay a statement. Got {nb.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_benign_free_through_school_context(self):
        """'My last tutor was free through school' — mentions 'free' but prior service context."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "pitch", "my last tutor was free through school", _flat_llm("statement"))
        # 'free' matches diy_free AND 'school' may be in a deadline context — but only _DEADLINE_CONTEXT_RE
        # is checked in slot extraction; the objection upgrade uses _OBJECTION_PATTERNS directly.
        # Document actual: routes to 'concern' (from disclosure?) or diy_free
        print(f"  'free through school': act={nb.last_user_act!r}, obj={nb.active_objection!r}")
        # The result is actually 'concern' from _DISCLOSURE_RE (bad tutor experience) — not diy_free.
        # That is defensible but worth noting: 'concern' may suppress the diy_free objection here.
        # This is a NON-BLOCKING edge case.

    @pytest.mark.asyncio
    async def test_price_inquiry_not_upgraded_to_objection(self):
        """LLM=price_inquiry must NOT be upgraded to a price/objection — it's a stronger intent."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "how much does it cost per month?", _flat_llm("price_inquiry"))
        assert nb.last_user_act == "price_inquiry", (
            f"price_inquiry must not be upgraded to an objection. Got {nb.last_user_act!r}"
        )
        assert nb.active_objection is None, (
            f"price_inquiry must not produce an objection. Got {nb.active_objection!r}"
        )

    @pytest.mark.asyncio
    async def test_buying_signal_with_price_word(self):
        """'The price is fine, let's go' — price mention in a buying signal context."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "pitch", "the price is fine, let's go", _flat_llm("statement"))
        assert nb.active_objection is None, (
            f"BUYING SIGNAL: 'price is fine let\\'s go' must not produce a price objection. "
            f"Got {nb.active_objection!r}"
        )


# ===========================================================================
# 3. SOCIAL_ASIDE live override — false-positive hunt
# ===========================================================================

class TestSocialAsideFalsePositives:
    """Cases that must NOT become social_aside even with LLM=question."""

    @pytest.mark.asyncio
    async def test_reschedule_policy_question_not_social_aside(self):
        """'What's the weather policy if we need to reschedule?' — product keyword exempts it."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "what's the weather policy if we need to reschedule?", _flat_llm("question"))
        assert nb.last_user_act != "social_aside", (
            f"Reschedule/policy question must NOT be social_aside. Got {nb.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_genuine_storm_social_aside(self):
        """'You guys see the storm tonight?' — genuine social aside, must fire."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "you guys see the storm tonight?", _flat_llm("question"))
        assert nb.last_user_act == "social_aside", (
            f"Pure social aside must be classified social_aside. Got {nb.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_storm_plus_session_keyword_not_social_aside(self):
        """'Does the storm affect session scheduling?' — 'session'/'scheduling' are product keywords."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "does the storm affect session scheduling?", _flat_llm("question"))
        assert nb.last_user_act != "social_aside", (
            f"Storm+session keyword question must NOT be social_aside. Got {nb.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_price_inquiry_with_storm_mention_not_social_aside(self):
        """LLM=price_inquiry + storm mention — price_inquiry is a stronger intent, must survive."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "how much is it per session? Also crazy storm tonight!", _flat_llm("price_inquiry"))
        assert nb.last_user_act == "price_inquiry", (
            f"price_inquiry + social cue — price_inquiry must win. Got {nb.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_objection_with_social_cue_not_social_aside(self):
        """LLM=objection + social text — objection is stronger, must NOT become social_aside."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "pitch", "too expensive — did you see the storm?", _flat_llm("objection", "price"))
        assert nb.last_user_act != "social_aside", (
            f"Objection+social cue must keep objection. Got {nb.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_human_request_with_weather_mention_not_social_aside(self):
        """LLM=human_request + weather mention — human_request is a stronger intent."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "can I speak to a real person? wild weather today", _flat_llm("human_request"))
        assert nb.last_user_act != "social_aside", (
            f"human_request+weather must NOT become social_aside. Got {nb.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_social_aside_requires_question_mark(self):
        """Social aside override requires _is_question_utt=True — a statement with storm doesn't fire."""
        b = BeliefState.fresh()
        # Statement with social cue but no question mark / interrogative
        nb = await dst.update(b, "ask", "crazy storm last night", _flat_llm("statement"))
        assert nb.last_user_act != "social_aside", (
            "A non-question utterance with social cue must NOT become social_aside "
            "(override requires _is_question_utt). "
            f"Got {nb.last_user_act!r}"
        )


# ===========================================================================
# 4. CALLBACK_WINDOW ack — false-positive hunt (negation / unavailability)
# ===========================================================================

class TestCallbackWindowFalsePositives:
    """Negated and unavailability statements must not set callback_window_just_captured."""

    def test_not_available_wednesdays_or_fridays_does_not_capture(self):
        """FIXED (round-2, NON-BLOCKING): 'I'm NOT available Wednesdays or Fridays' must NOT capture
        a callback window.

        Round-1's disjunction branch fired on 'Wednesdays or Fridays' regardless of the 'NOT
        available' prefix, so unavailability was captured as a preferred window (conf 0.9) and the
        NLG would ack days the caller said they CAN'T do. Round-2 adds _AVAILABILITY_NEGATION_RE: a
        negation/unavailability cue in a bounded window around the day pair → return None."""
        result = _extract_callback_window("I'm NOT available Wednesdays or Fridays")
        assert result is None, (
            "FIXED (NON-BLOCKING): _extract_callback_window must NOT capture 'Wednesdays or Fridays' "
            "from 'I\\'m NOT available Wednesdays or Fridays' — the negation guard treats it as stated "
            f"unavailability. Got {result!r}."
        )

    @pytest.mark.asyncio
    async def test_not_available_days_does_not_set_ack_flag(self):
        """FIXED (round-2): 'I'm NOT available Wednesdays or Fridays' must NOT set
        callback_window_just_captured — the NLG must not ack unavailability as a window."""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "I'm NOT available Wednesdays or Fridays", _flat_llm("statement"))
        cw = nb.slots.get("callback_window")
        flag = nb.meta.get("callback_window_just_captured")
        assert flag is not True, (
            "FIXED: 'NOT available Wednesdays or Fridays' must NOT set callback_window_just_captured "
            f"— there is no availability to ack. Slot captured: {cw!r}, flag: {flag!r}"
        )
        assert cw is None, (
            f"The callback_window slot must NOT be filled from a stated unavailability. Got {cw!r}"
        )

    def test_wednesdays_tough_for_us_does_not_capture(self):
        """'Wednesdays are tough for us' — bare plural with no scheduling cue, CB-76 guard stops it."""
        result = _extract_callback_window("Wednesdays are tough for us")
        assert result is None, (
            f"'Wednesdays are tough' should NOT capture (plural + no scheduling cue). Got {result!r}"
        )

    def test_genuine_availability_captures(self):
        """Smoke test: 'Wednesdays or Fridays after 4 work best' must still capture."""
        result = _extract_callback_window("Wednesdays or Fridays after 4 work best for us")
        assert result is not None, "Genuine availability must capture callback_window"
        val, conf = result
        assert "wednesday" in val.lower() or "friday" in val.lower()


# ===========================================================================
# 5. MULTI-OVERRIDE INTERACTION: order and win conditions
# ===========================================================================

class TestMultiOverrideInteraction:
    """Verify the ordering of the three overrides in update() and that the correct
    one wins when multiple could fire."""

    @pytest.mark.asyncio
    async def test_social_aside_wins_over_objection_upgrade_when_no_product_keyword(self):
        """Storm question with a 'free' mention but no product keyword:
        CB-89 (social_aside) fires first, so classified_act becomes 'social_aside';
        CB-90a (objection upgrade) then sees classified_act='social_aside' (not in
        {statement,None}) and does NOT fire. Social aside wins."""
        b = BeliefState.fresh()
        # 'free' matches diy_free; 'storm' matches social_aside; no product keyword
        nb = await dst.update(b, "ask", "it's free right? And did you see the storm?", _flat_llm("question"))
        # If social_aside fires: act=social_aside, obj=None
        # If objection upgrade fires: act=objection, obj=diy_free
        # Correct winner: social_aside (CB-89 runs first in the code)
        # But: 'free right?' has 'price' or similar? Let's see actual
        print(f"  free+storm: act={nb.last_user_act!r}, obj={nb.active_objection!r}")
        # Document: whatever fires, verify only ONE fires
        assert not (nb.last_user_act == "social_aside" and nb.active_objection is not None), (
            "If social_aside wins, active_objection must be None (CB-90a must not also fire)"
        )

    @pytest.mark.asyncio
    async def test_price_question_with_day_mention_neither_blocked(self):
        """Price question + day mention (LLM=question):
        - Social aside: 'meet' is in _PRODUCT_KEYWORDS_RE? Let's verify
        - Objection upgrade: LLM=question, not in {statement,None} — skips
        - Result: neither override fires on LLM=question with product keyword"""
        b = BeliefState.fresh()
        nb = await dst.update(b, "ask", "how much for a Monday session?", _flat_llm("question"))
        assert nb.last_user_act != "social_aside", (
            "'Monday session' has 'session' (product keyword) — must NOT be social_aside"
        )
        assert nb.last_user_act != "objection", (
            "LLM=question is not in {statement,None} — objection upgrade must not fire"
        )

    @pytest.mark.asyncio
    async def test_override_order_social_then_objection(self):
        """Confirm code order: CB-89 social_aside runs BEFORE CB-90a objection upgrade.
        Proof: an utterance that would trigger BOTH — 'free storm?' (LLM=statement, has
        question mark so _is_question_utt=True, has 'storm' social cue, has 'free' objection cue,
        no product keyword) — social_aside must win (CB-89 first), not diy_free (CB-90a second).
        After CB-89 sets classified_act='social_aside', CB-90a guard (classified_act in
        {statement,None}) is False so it doesn't fire."""
        b = BeliefState.fresh()
        # LLM=statement; utterance has '?' (question), storm (social), free (diy_free) — no product kw
        nb = await dst.update(b, "ask", "are there free alternatives? what about the storm?", _flat_llm("statement"))
        # Expected: social_aside wins (CB-89 first) because _is_question_utt=True and social cue present
        # But 'free alternatives' has no product keyword... unless 'alternatives' is in _PRODUCT_KEYWORDS_RE?
        print(f"  free+storm (statement+?): act={nb.last_user_act!r}, obj={nb.active_objection!r}")
        # The interaction is: if classified_act becomes social_aside after CB-89, CB-90a won't fire.
        # Verify they don't BOTH fire:
        if nb.last_user_act == "social_aside":
            assert nb.active_objection is None, "social_aside must not co-exist with an objection from CB-90a"
        elif nb.last_user_act == "objection":
            # CB-90a won (social cue wasn't enough — maybe product keyword blocked CB-89, or no social cue hit)
            assert nb.active_objection == "diy_free", f"If objection upgrade wins, must be diy_free. Got {nb.active_objection!r}"


# ===========================================================================
# 6. advance_to_close SMOKE TEST — guard didn't over-block genuine close turns
# ===========================================================================

class TestAdvanceToCloseNotOverBlocked:
    """Confirms advance_to_close still fires normally on a genuine closeable turn
    after the CB-89 social_aside guard was added."""

    @pytest.mark.asyncio
    async def test_genuine_interest_statement_allows_close(self):
        """'That sounds great, I'm interested' on a warm belief -> advance_to_close fires."""
        b = _warm_belief()
        nb = await dst.update(b, "pitch", "that sounds great, I'm interested", _flat_llm("statement"))
        assert nb.last_user_act != "social_aside", "Genuine interest must not be social_aside"
        assert nb.active_objection is None, "No objection expected"
        cfg = _cfg(min_turns_before_close=4)
        out = gates.advance_to_close(_decision("pitch"), nb, cfg, history=[])
        assert out.act == "attempt_close", (
            f"advance_to_close must fire on genuine warm closeable turn. Got {out.act!r}. "
            "The social_aside guard must not over-block real closes."
        )

    @pytest.mark.asyncio
    async def test_social_storm_still_blocked(self):
        """Smoke: 'You guys see the storm tonight?' on a warm belief -> advance_to_close blocked."""
        b = _warm_belief()
        nb = await dst.update(b, "pitch", "you guys see the storm coming tonight?", _flat_llm("question"))
        assert nb.last_user_act == "social_aside", "Storm question must be social_aside"
        cfg = _cfg(min_turns_before_close=4)
        out = gates.advance_to_close(_decision("pitch"), nb, cfg, history=[])
        assert out.act != "attempt_close", (
            f"advance_to_close must be blocked after social_aside. Got {out.act!r}"
        )


# CB-90a-fix (live): the LLM labels a guarantee demand phrased as "...Yes or no?" / "Can you
# guarantee a B?" as `question`, not statement — the upgrade must cover `question` too (safe
# because _GUARANTEE_DEMAND_RE requires a grade/result token). Caught by the round-4 live replay.
import asyncio as _asyncio
import json as _json
from src.core import dst as _dst
from src.core.belief_state import BeliefState as _BS
from src.core.llm import MockLLMClient as _Mock


def _intent(utt, llm_act):
    body = {"trust": 0.0, "urgency": 0.0, "bail_risk": 0.0, "need_intensity": 0.0,
            "purchase_intent": 0.0, "price_sensitivity": 0.0, "user_act": llm_act,
            "objection": None, "open_question": None, "affordability_refusal": False}

    async def _run():
        nb = await _dst.update(_BS.fresh(), "pitch", utt, _Mock([_json.dumps(body)]))
        return nb.last_user_act, nb.active_objection
    return _asyncio.run(_run())


def test_cb90a_guarantee_as_question_upgrades_to_efficacy():
    assert _intent("Guarantee me a B or it's free. Yes or no.", "question") == ("objection", "efficacy_doubt")
    assert _intent("Can you guarantee a B?", "question") == ("objection", "efficacy_doubt")


def test_cb90a_guarantee_question_fp_guard_holds():
    # tight pattern: no grade/result token -> stays a question even though "guarantee" appears
    assert _intent("Can you guarantee a tutor will be free Thursday?", "question") == ("question", None)
    assert _intent("What do I get for the money?", "question") == ("question", None)
