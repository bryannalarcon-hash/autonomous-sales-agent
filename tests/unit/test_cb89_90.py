# CB-89/90 regression tests — live-path social_aside override, guarantee objection routing,
# and caller-availability acknowledgment. All deterministic / $0-cost: canned LLM responses.
# Test map:
#   CB-89 — live-path social_aside override:
#       - LLM returns 'question' for storm utterance -> classified as social_aside after override
#       - LLM returns 'statement' for storm utterance -> social_aside
#       - advance_to_close blocked on the live path after DST social_aside override
#       - full gate chain: no attempt_close after LLM-question + storm text
#       - reschedule-policy question (product keyword) -> NOT social_aside even with LLM=question
#       - genuine objection (LLM=objection/price) + social cue -> objection wins, NOT social_aside
#       - NLG _SOCIAL_ASIDE_NOTE injected after live-path DST update with social_aside
#   CB-90 (a) — guarantee/efficacy objection:
#       - regex path: "Guarantee me a B or it's free" -> efficacy_doubt (not diy_free)
#       - LLM=statement path: deterministic upgrade -> efficacy_doubt
#       - LLM=efficacy_doubt path: stays efficacy_doubt (not corrupted)
#       - gate: active_objection=efficacy_doubt -> handle_objection, NOT advance_to_close
#       - no false "I have your info" on close when no contact captured
#       - _NO_CONTACT_CLAIM_NOTE injected on attempt_close with no contact
#       - _NO_CONTACT_CLAIM_NOTE NOT injected when contact_just_captured is True
#   CB-90 (b) — caller availability acknowledgment:
#       - callback_window_just_captured flag set when slot newly filled
#       - flag cleared on the next turn (one-shot)
#       - _CALLBACK_WINDOW_ACK_NOTE injected in NLG prompt when flag is True
#       - _CALLBACK_WINDOW_ACK_NOTE NOT injected when flag is False
#       - note contains explicit prohibition on "I don't have that availability"
#       - stated value is echoed in the NLG prompt
#   Invariants — R37, buy-gate purity, CB-85 invariant still holds
from __future__ import annotations

import json
import pytest

from src.config.settings import AgentConfig, Persona
from src.core import dst, gates
from src.core.belief_state import DRIVERS, BeliefState
from src.core.dst import _classify_intent, _OBJECTION_PATTERNS
from src.core.gates import advance_to_close, apply_gates
from src.core.llm import MockLLMClient
from src.core.nlg import (
    _SOCIAL_ASIDE_NOTE,
    _CALLBACK_WINDOW_ACK_NOTE,
    _NO_CONTACT_CLAIM_NOTE,
    _build_messages,
)
from src.core.policy import Decision


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _flat_llm(intent: str = "question", obj: str | None = None, n: int = 6) -> MockLLMClient:
    """Canned LLM that returns a specific intent/objection (simulates live LLM path)."""
    payload = {
        **{d: 0.0 for d in DRIVERS},
        "user_act": intent,
        "open_question": "question text" if intent == "question" else None,
        "objection": obj,
        "affordability_refusal": False,
    }
    return MockLLMClient([json.dumps(payload)] * n)


def _fail_llm(n: int = 4) -> MockLLMClient:
    """LLM that always fails -> triggers the regex fallback path."""
    return MockLLMClient(["not-json"] * n)


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
        persona=Persona(name="Nerdy", role="learning advisor", style="warm-consultative"),
        prompts={"policy": "Propose next act as JSON.", "nlg": "Reply concisely."},
        playbooks={"discovery_sequence": ["grade_level", "subject", "goal", "timeline", "budget"]},
        thresholds=thresholds,
    )


def _decision(act: str = "pitch", **kw) -> Decision:
    return Decision(act=act, rationale="test", confidence=0.75, **kw)


def _warm_closeable_belief() -> BeliefState:
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.65
    b.drivers["purchase_intent"] = 0.55
    b.turn_count = 5
    return b


# ===========================================================================
# CB-89 — Live-path social_aside override
# ===========================================================================

class TestCB89LivePathSocialAside:
    """The deterministic social_aside override in dst.update() must fire on the LLM path,
    not just when the LLM call fails and the regex fallback runs."""

    @pytest.mark.asyncio
    async def test_llm_question_storm_becomes_social_aside(self):
        """EXACT CB-89 defect: LLM returns 'question' for storm utterance -> social_aside override.

        Live demo: LLM said 'question', so last_user_act was 'question', advance_to_close fired,
        and agent said 'Sounds good — let me get you scheduled' after a weather aside."""
        belief = BeliefState.fresh()
        llm = _flat_llm("question")
        new_b = await dst.update(
            belief, "ask", "you guys see the storm coming tonight?", llm
        )
        assert new_b.last_user_act == "social_aside", (
            "CB-89 LIVE PATH: LLM returned 'question' for storm utterance but the deterministic "
            "social_aside override must set last_user_act='social_aside'. "
            f"Got {new_b.last_user_act!r}. This was the exact defect: LLM unaware of social_aside "
            "vocabulary -> 'question' -> advance_to_close fires -> scheduling push."
        )

    @pytest.mark.asyncio
    async def test_llm_statement_storm_becomes_social_aside(self):
        """LLM returns 'statement' for storm utterance -> social_aside override."""
        belief = BeliefState.fresh()
        llm = _flat_llm("statement")
        new_b = await dst.update(
            belief, "ask", "you guys see the storm coming tonight?", llm
        )
        assert new_b.last_user_act == "social_aside", (
            "LLM='statement' + storm text must produce last_user_act='social_aside' after override. "
            f"Got {new_b.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_reschedule_policy_not_social_aside_on_live_path(self):
        """INVARIANT: reschedule/policy question -> NOT social_aside even when LLM says 'question'.

        The _PRODUCT_KEYWORDS_RE exemption must protect genuine product questions."""
        belief = BeliefState.fresh()
        llm = _flat_llm("question")
        new_b = await dst.update(
            belief, "ask",
            "what's the weather policy if we need to reschedule?",
            llm,
        )
        assert new_b.last_user_act != "social_aside", (
            "INVARIANT (CB-85/CB-89): 'weather policy if we need to reschedule?' has a product "
            "keyword (reschedule/policy) and must NOT be social_aside on the live path. "
            f"Got {new_b.last_user_act!r}. The product-keyword exemption must hold end-to-end."
        )

    @pytest.mark.asyncio
    async def test_genuine_objection_not_overridden_to_social_aside(self):
        """A genuine objection (LLM=objection/price) is NOT downgraded to social_aside."""
        belief = BeliefState.fresh()
        llm = _flat_llm("objection", "price")
        new_b = await dst.update(
            belief, "ask",
            "This is too expensive — you guys see the storm coming tonight?",
            llm,
        )
        assert new_b.last_user_act != "social_aside", (
            "A genuine objection (LLM=objection/price) must NOT be overridden to social_aside "
            "even when the text also contains a social cue. "
            f"Got {new_b.last_user_act!r}"
        )
        assert new_b.active_objection == "price", (
            f"Price objection must be retained. Got {new_b.active_objection!r}"
        )

    @pytest.mark.asyncio
    async def test_human_request_not_overridden_to_social_aside(self):
        """A genuine human_request is NOT downgraded to social_aside."""
        belief = BeliefState.fresh()
        llm = _flat_llm("human_request")
        new_b = await dst.update(
            belief, "ask",
            "Can you connect me to a real person? Also, crazy storm out there!",
            llm,
        )
        # human_request is grounded via _HUMAN_REQUEST_CUES_RE; if grounded it stays human_request
        # (the storm text doesn't have _HUMAN_REQUEST_CUES_RE so if ungrounded it becomes question,
        # not social_aside — but the social_aside override won't fire on human_request intent)
        assert new_b.last_user_act not in ("social_aside",) or new_b.last_user_act == "human_request", (
            "human_request must NOT be overridden to social_aside."
        )

    @pytest.mark.asyncio
    async def test_advance_to_close_blocked_after_live_path_social_aside(self):
        """GATE INVARIANT: after DST sets social_aside on live path, advance_to_close must not fire."""
        prior = _warm_closeable_belief()
        llm = _flat_llm("question")
        new_b = await dst.update(
            prior, "pitch", "you guys see the storm coming tonight?", llm
        )
        assert new_b.last_user_act == "social_aside", (
            "Precondition: DST must set social_aside (precondition for gate test)"
        )
        cfg = _cfg(min_turns_before_close=4)
        dec = _decision("pitch")
        out = advance_to_close(dec, new_b, cfg, history=[])
        assert out.act != "attempt_close", (
            f"INVARIANT: advance_to_close must NOT fire after live-path social_aside. Got {out.act!r}"
        )

    @pytest.mark.asyncio
    async def test_full_gate_chain_no_close_after_live_path_social_aside(self):
        """apply_gates must not produce attempt_close after live-path DST social_aside override."""
        prior = _warm_closeable_belief()
        llm = _flat_llm("question")
        new_b = await dst.update(
            prior, "pitch", "you guys see the storm coming tonight?", llm
        )
        cfg = _cfg(min_turns_before_close=4)
        dec = _decision("pitch")
        out = apply_gates(dec, new_b, cfg, history=[])
        assert out.act != "attempt_close", (
            "INVARIANT: apply_gates must never produce attempt_close after live-path social_aside. "
            f"Got {out.act!r}. The 'storm tonight?' -> 'Sounds good — get you scheduled' defect "
            "must be blocked end-to-end on the live path (CB-89)."
        )

    @pytest.mark.asyncio
    async def test_social_aside_nlg_note_injected_after_live_path_update(self):
        """_SOCIAL_ASIDE_NOTE must appear in NLG prompt after DST live-path social_aside override."""
        prior = BeliefState.fresh()
        llm = _flat_llm("question")
        new_b = await dst.update(
            prior, "ask", "you guys see the storm coming tonight?", llm
        )
        assert new_b.last_user_act == "social_aside", "Precondition: social_aside set"
        cfg = _cfg()
        dec = _decision("pitch")
        msgs = _build_messages(dec, new_b, cfg, retrieved_facts=None)
        content = msgs[-1]["content"]
        assert "SOCIAL ASIDE RULE" in content, (
            "_SOCIAL_ASIDE_NOTE must be injected in NLG prompt after live-path social_aside. "
            "This is the second-layer protection preventing 'Sounds good — let me schedule you'."
        )

    @pytest.mark.asyncio
    async def test_regex_fallback_path_still_works(self):
        """Regression guard: regex fallback (LLM failure) must still classify social_aside."""
        belief = BeliefState.fresh()
        new_b = await dst.update(
            belief, "ask", "you guys see the storm coming tonight?", _fail_llm()
        )
        assert new_b.last_user_act == "social_aside", (
            "Regression: regex fallback path must still classify social_aside after CB-89 changes. "
            f"Got {new_b.last_user_act!r}"
        )


# ===========================================================================
# CB-90 (a) — Guarantee/efficacy objection routing
# ===========================================================================

class TestCB90aGuaranteeObjection:
    """'Guarantee me a B or it's free' must route to efficacy_doubt, not diy_free."""

    def test_guarantee_free_regex_is_efficacy_doubt_not_diy_free(self):
        """ORDERING FIX: guarantee+free pattern must fire before generic diy_free pattern.

        CB-90a root cause: 'free' fired the diy_free pattern before 'guarantee' could fire
        efficacy_doubt, so the objection was misrouted and swallowed by wrong handling."""
        text = "Guarantee me a B or it's free."
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key == "efficacy_doubt", (
            f"CB-90a: 'Guarantee me a B or it's free.' must match efficacy_doubt first "
            f"(guarantee-specific pattern), NOT diy_free ('free' alone). Got {matched_key!r}. "
            "Pattern priority fix: guarantee pattern must precede generic diy_free."
        )

    def test_guarantee_grade_demand_is_efficacy_doubt(self):
        """'Guarantee me a B' (no 'free') must route to efficacy_doubt."""
        text = "Can you guarantee me a B in this class?"
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key == "efficacy_doubt", (
            f"'Can you guarantee me a B?' must be efficacy_doubt. Got {matched_key!r}"
        )

    def test_guarantee_pass_is_efficacy_doubt(self):
        """'Guarantee me a pass' must route to efficacy_doubt."""
        text = "Can you guarantee me a pass on the exam?"
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key == "efficacy_doubt", (
            f"'Guarantee me a pass' must be efficacy_doubt. Got {matched_key!r}"
        )

    def test_diy_free_still_fires_when_no_guarantee(self):
        """Regression: 'free' alone (without guarantee context) still routes to diy_free."""
        text = "There are so many free resources online already."
        matched_key = None
        for pat, key in _OBJECTION_PATTERNS:
            if pat.search(text):
                matched_key = key
                break
        assert matched_key == "diy_free", (
            f"Regression: 'free resources online' must still be diy_free (no guarantee). Got {matched_key!r}"
        )

    def test_regex_path_guarantee_free_routes_efficacy_doubt(self):
        """On the regex fallback path, guarantee+free must produce efficacy_doubt active_objection."""
        act, obj, _ = _classify_intent("Guarantee me a B or it's free.")
        assert act == "objection", f"Expected 'objection', got {act!r}"
        assert obj == "efficacy_doubt", (
            f"Regex path: 'Guarantee me a B or it's free.' must produce efficacy_doubt. Got {obj!r}"
        )

    @pytest.mark.asyncio
    async def test_llm_statement_path_guarantee_upgrades_to_efficacy_doubt(self):
        """CB-90a: LLM returns 'statement' for guarantee demand -> deterministic upgrade fires."""
        belief = BeliefState.fresh()
        belief.turn_count = 5
        belief.drivers["trust"] = 0.65
        llm = _flat_llm("statement", None)  # LLM missed the objection
        new_b = await dst.update(
            belief, "pitch", "Guarantee me a B or it is free.", llm
        )
        assert new_b.active_objection == "efficacy_doubt", (
            "CB-90a: LLM='statement' but text has guarantee+free -> deterministic upgrade must set "
            f"active_objection='efficacy_doubt'. Got {new_b.active_objection!r}. "
            "Without this fix, the guarantee demand is silently dropped as a plain statement."
        )
        assert new_b.last_user_act == "objection", (
            f"last_user_act must be 'objection'. Got {new_b.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_llm_efficacy_doubt_path_stays_efficacy_doubt(self):
        """LLM correctly returns efficacy_doubt -> not corrupted by the upgrade logic."""
        belief = BeliefState.fresh()
        llm = _flat_llm("objection", "efficacy_doubt")
        new_b = await dst.update(
            belief, "ask", "Guarantee me a B or it is free.", llm
        )
        assert new_b.active_objection == "efficacy_doubt", (
            f"LLM=efficacy_doubt must be preserved. Got {new_b.active_objection!r}"
        )

    @pytest.mark.asyncio
    async def test_guarantee_routes_to_handle_objection_not_advance_close(self):
        """GATE INVARIANT: active_objection=efficacy_doubt -> handle_objection, NOT attempt_close.

        CB-90a failure: 'Guarantee me a B or it's free' -> scheduling push.
        The gate must redirect any pitch/close to handle_objection when an objection is live."""
        belief = _warm_closeable_belief()
        belief.active_objection = "efficacy_doubt"
        cfg = _cfg(min_turns_before_close=4)

        # A pitch or close proposed by the LLM must be redirected
        for act in ("pitch", "attempt_close"):
            dec = _decision(act, tier="consultation" if act == "attempt_close" else None)
            out = apply_gates(dec, belief, cfg, history=[])
            assert out.act == "handle_objection", (
                f"GATE: proposed '{act}' with efficacy_doubt objection must be redirected to "
                f"handle_objection. Got {out.act!r}. 'Guarantee me a B' must never produce a "
                "scheduling push (CB-90a)."
            )

    @pytest.mark.asyncio
    async def test_no_contact_claim_note_injected_on_close_without_contact(self):
        """_NO_CONTACT_CLAIM_NOTE must appear in NLG prompt on attempt_close with no contact."""
        b = BeliefState.fresh()
        b.meta["contact_just_captured"] = False
        cfg = _cfg()
        dec = _decision("attempt_close", tier="consultation")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        content = msgs[-1]["content"]
        assert "CONTACT CLAIM RULE" in content, (
            "CB-90a: _NO_CONTACT_CLAIM_NOTE must be injected on attempt_close when no contact has "
            "been captured. The agent must not claim 'I have your info' when none was given."
        )

    def test_no_contact_claim_note_not_injected_on_non_close_act(self):
        """_NO_CONTACT_CLAIM_NOTE must NOT appear on a non-close act (pitch, ask, etc.)."""
        b = BeliefState.fresh()
        b.meta["contact_just_captured"] = False
        cfg = _cfg()
        for act in ("pitch", "ask", "answer_via_kb", "handle_objection"):
            dec = _decision(act)
            msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
            content = msgs[-1]["content"]
            assert "CONTACT CLAIM RULE" not in content, (
                f"_NO_CONTACT_CLAIM_NOTE must NOT appear on act={act!r} (only on attempt_close)"
            )

    def test_no_contact_claim_note_not_injected_when_contact_captured(self):
        """_NO_CONTACT_CLAIM_NOTE must NOT fire when contact was just captured this turn."""
        b = BeliefState.fresh()
        b.meta["contact_just_captured"] = True
        b.meta["contact_name"] = "Dana"
        cfg = _cfg()
        dec = _decision("attempt_close", tier="consultation")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        content = msgs[-1]["content"]
        assert "CONTACT CLAIM RULE" not in content, (
            "_NO_CONTACT_CLAIM_NOTE must NOT fire when contact_just_captured=True "
            "(the _CONTACT_ACK_NOTE fires instead on the genuine capture turn)."
        )

    def test_no_contact_claim_note_content_forbids_fabricated_claim(self):
        """_NO_CONTACT_CLAIM_NOTE must explicitly forbid 'I have your info' / 'I've got your contact'."""
        note_lower = _NO_CONTACT_CLAIM_NOTE.lower()
        has_ban = (
            "have your info" in note_lower
            or "got your contact" in note_lower
            or "i've got" in note_lower
            or "do not say" in note_lower
            or "do not claim" in note_lower
        )
        assert has_ban, (
            "_NO_CONTACT_CLAIM_NOTE must explicitly forbid fabricated contact claims. "
            f"Note excerpt: {_NO_CONTACT_CLAIM_NOTE[:300]!r}"
        )


# ===========================================================================
# CB-90 (b) — Caller availability acknowledgment
# ===========================================================================

class TestCB90bCallerAvailabilityAck:
    """The caller STATING their availability must be captured + acknowledged, never deflected."""

    @pytest.mark.asyncio
    async def test_callback_window_just_captured_flag_set_on_new_slot(self):
        """callback_window_just_captured must be True when slot is newly filled this turn."""
        belief = BeliefState.fresh()
        llm = _flat_llm("statement", None)
        new_b = await dst.update(
            belief, "ask",
            "Wednesdays or Fridays after 4 work best for us",
            llm,
        )
        assert new_b.meta.get("callback_window_just_captured") is True, (
            "CB-90b: 'Wednesdays or Fridays after 4 work best' must set "
            "callback_window_just_captured=True in meta. This triggers the NLG ack instruction. "
            f"Got: {new_b.meta.get('callback_window_just_captured')!r}"
        )

    @pytest.mark.asyncio
    async def test_callback_window_value_stored_in_meta(self):
        """callback_window_value must store the normalized slot value for NLG to echo."""
        belief = BeliefState.fresh()
        llm = _flat_llm("statement", None)
        new_b = await dst.update(
            belief, "ask",
            "Wednesdays or Fridays after 4 work best for us",
            llm,
        )
        val = new_b.meta.get("callback_window_value", "")
        assert val, (
            f"callback_window_value must be non-empty after capture. Got: {val!r}"
        )
        # The stored value should reference the days (normalized)
        assert "wednesday" in val.lower() or "friday" in val.lower(), (
            f"callback_window_value must include the stated days. Got: {val!r}"
        )

    @pytest.mark.asyncio
    async def test_callback_window_just_captured_clears_next_turn(self):
        """callback_window_just_captured must clear on the NEXT turn (one-shot flag)."""
        belief = BeliefState.fresh()
        llm = _flat_llm("statement", None)
        new_b = await dst.update(
            belief, "ask",
            "Wednesdays or Fridays after 4 work best for us",
            llm,
        )
        assert new_b.meta.get("callback_window_just_captured") is True, "Precondition: flag set"

        # Second turn: different utterance, no new callback_window
        llm2 = _flat_llm("statement", None)
        new_b2 = await dst.update(
            new_b, "pitch", "That sounds helpful.", llm2
        )
        assert new_b2.meta.get("callback_window_just_captured") is False, (
            "CB-90b: callback_window_just_captured must clear on the next turn (one-shot). "
            f"Got: {new_b2.meta.get('callback_window_just_captured')!r}"
        )

    @pytest.mark.asyncio
    async def test_callback_window_not_captured_when_slot_already_filled(self):
        """callback_window_just_captured must NOT be True when the slot was already filled before."""
        belief = BeliefState.fresh()
        # Pre-fill the slot at lock confidence so it was already known
        belief.set_slot("callback_window", "thursday after 3pm", 0.9)
        llm = _flat_llm("statement", None)
        # Another availability statement that doesn't change the slot
        new_b = await dst.update(
            belief, "ask", "Thursday after 3pm still works.", llm
        )
        # If slot was already locked, callback_window_just_captured should be False
        assert new_b.meta.get("callback_window_just_captured") is False, (
            "callback_window_just_captured must NOT be True when slot was already filled. "
            f"Got: {new_b.meta.get('callback_window_just_captured')!r}"
        )

    def test_callback_window_ack_note_injected_when_just_captured(self):
        """_CALLBACK_WINDOW_ACK_NOTE must appear in NLG prompt when flag is True."""
        b = BeliefState.fresh()
        b.meta["callback_window_just_captured"] = True
        b.meta["callback_window_value"] = "wednesday or friday"
        cfg = _cfg()
        dec = _decision("ask")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        content = msgs[-1]["content"]
        assert "CALLER AVAILABILITY ACK" in content, (
            "CB-90b: _CALLBACK_WINDOW_ACK_NOTE must be injected when callback_window_just_captured=True. "
            "This prevents the 'I don't have that availability in front of me' deflection."
        )

    def test_callback_window_ack_note_echoes_stated_value(self):
        """_CALLBACK_WINDOW_ACK_NOTE prompt must include the stated availability for verbatim echo."""
        b = BeliefState.fresh()
        b.meta["callback_window_just_captured"] = True
        b.meta["callback_window_value"] = "wednesday or friday"
        cfg = _cfg()
        dec = _decision("ask")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        content = msgs[-1]["content"]
        assert "wednesday or friday" in content, (
            "CB-90b: the stated availability value must appear in the NLG prompt so the LLM can "
            "echo it back verbatim (e.g. 'Got it — Wednesdays or Fridays after 4'). "
            f"Content excerpt: {content[:600]!r}"
        )

    def test_callback_window_ack_note_not_injected_when_not_captured(self):
        """_CALLBACK_WINDOW_ACK_NOTE must NOT appear when flag is False."""
        b = BeliefState.fresh()
        b.meta["callback_window_just_captured"] = False
        cfg = _cfg()
        dec = _decision("pitch")
        msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
        content = msgs[-1]["content"]
        assert "CALLER AVAILABILITY ACK" not in content, (
            "_CALLBACK_WINDOW_ACK_NOTE must NOT appear when callback_window_just_captured=False."
        )

    def test_callback_window_ack_note_rides_any_act(self):
        """_CALLBACK_WINDOW_ACK_NOTE must be injected regardless of the gated act."""
        b = BeliefState.fresh()
        b.meta["callback_window_just_captured"] = True
        b.meta["callback_window_value"] = "thursday after 3"
        cfg = _cfg()
        for act in ("ask", "pitch", "answer_via_kb", "attempt_close", "handle_objection"):
            dec = _decision(act, tier="consultation" if act == "attempt_close" else None)
            msgs = _build_messages(dec, b, cfg, retrieved_facts=None)
            content = msgs[-1]["content"]
            assert "CALLER AVAILABILITY ACK" in content, (
                f"_CALLBACK_WINDOW_ACK_NOTE must be injected on act={act!r} (rides any act)"
            )

    def test_callback_window_ack_note_forbids_tutor_availability_deflection(self):
        """_CALLBACK_WINDOW_ACK_NOTE must explicitly forbid deflecting as a tutor-availability lookup."""
        note_lower = _CALLBACK_WINDOW_ACK_NOTE.lower()
        has_ban = (
            "don't have that availability" in note_lower
            or "don't have the" in note_lower
            or "not a question" in note_lower
            or "tutor" in note_lower
            or "their schedule" in note_lower
        )
        assert has_ban, (
            "_CALLBACK_WINDOW_ACK_NOTE must explicitly forbid the 'I don't have that availability "
            "in front of me' deflection. Note excerpt: "
            f"{_CALLBACK_WINDOW_ACK_NOTE[:400]!r}"
        )

    def test_callback_window_ack_note_instructs_to_acknowledge_caller_schedule(self):
        """_CALLBACK_WINDOW_ACK_NOTE must instruct acknowledging the CALLER's stated window."""
        note_lower = _CALLBACK_WINDOW_ACK_NOTE.lower()
        has_ack = (
            "acknowledge" in note_lower
            or "echo" in note_lower
            or "got it" in note_lower
        )
        assert has_ack, (
            "_CALLBACK_WINDOW_ACK_NOTE must instruct an acknowledgment of the caller's availability. "
            f"Note excerpt: {_CALLBACK_WINDOW_ACK_NOTE[:300]!r}"
        )

    @pytest.mark.asyncio
    async def test_exact_qa_scenario_availability_statement(self):
        """EXACT CB-90b scenario: 'Wednesdays or Fridays after 4 work best' -> ack, not lookup deflection.

        The defect: agent replied 'I don't have the specific Wednesday or Friday after-4
        availability in front of me' — treating caller's own schedule as a tutor lookup."""
        belief = BeliefState.fresh()
        llm = _flat_llm("statement", None)
        new_b = await dst.update(
            belief, "ask",
            "Wednesdays or Fridays after 4 work best for us",
            llm,
        )
        # Slot must be captured
        assert new_b.slots.get("callback_window"), (
            "callback_window slot must be filled from 'Wednesdays or Fridays after 4 work best'"
        )
        # Flag must be set for NLG ack
        assert new_b.meta.get("callback_window_just_captured") is True, (
            "callback_window_just_captured must be True so NLG injects the ack note"
        )
        # NLG must inject the ack note
        cfg = _cfg()
        dec = _decision("ask")
        msgs = _build_messages(dec, new_b, cfg, retrieved_facts=None)
        content = msgs[-1]["content"]
        assert "CALLER AVAILABILITY ACK" in content, (
            "EXACT CB-90b: NLG must inject _CALLBACK_WINDOW_ACK_NOTE after 'Wednesdays or Fridays "
            "after 4 work best'. Without this, agent deflects with 'I don't have that in front of me'."
        )


# ===========================================================================
# Invariants — R37, buy-gate purity, CB-85 invariant
# ===========================================================================

class TestCB8990Invariants:
    """CB-89/90 changes must not break R37, buy-gate purity, or the CB-85 gate invariant."""

    def test_r37_no_livekit_in_dst(self):
        """R37: src/core/dst.py must contain no LiveKit imports after CB-89/90 changes."""
        import ast
        import pathlib
        src_text = pathlib.Path("src/core/dst.py").read_text()
        tree = ast.parse(src_text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for name in names:
                    assert "livekit" not in (name or "").lower(), (
                        f"R37 violation: LiveKit import in dst.py after CB-89/90: {name!r}"
                    )

    def test_r37_no_livekit_in_nlg(self):
        """R37: src/core/nlg.py must contain no LiveKit imports after CB-89/90 changes."""
        import ast
        import pathlib
        src_text = pathlib.Path("src/core/nlg.py").read_text()
        tree = ast.parse(src_text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for name in names:
                    assert "livekit" not in (name or "").lower(), (
                        f"R37 violation: LiveKit import in nlg.py after CB-89/90: {name!r}"
                    )

    @pytest.mark.asyncio
    async def test_cb85_invariant_still_holds_via_live_path(self):
        """CB-85 invariant: advance_to_close must NOT fire on social_aside (live path)."""
        prior = _warm_closeable_belief()
        llm = _flat_llm("question")
        new_b = await dst.update(
            prior, "pitch", "you guys see the storm coming tonight?", llm
        )
        assert new_b.last_user_act == "social_aside", "Precondition"
        cfg = _cfg(min_turns_before_close=4)
        dec = _decision("pitch")
        out = advance_to_close(dec, new_b, cfg, history=[])
        assert out.act != "attempt_close", (
            "CB-85 INVARIANT must still hold after CB-89 changes: advance_to_close never fires on social_aside."
        )

    def test_buy_gate_purity_social_aside_no_tier_change(self):
        """Buy-gate purity: social_aside does not affect tier selection (drivers unchanged)."""
        from src.core.gates import _closeable_tier
        cfg = _cfg()
        b_normal = BeliefState.fresh()
        b_normal.drivers["trust"] = 0.65
        b_normal.turn_count = 5

        b_social = b_normal.copy()
        b_social.last_user_act = "social_aside"

        assert _closeable_tier(b_normal, cfg) == _closeable_tier(b_social, cfg), (
            "Buy-gate purity: social_aside last_user_act must not change the commitment tier."
        )

    def test_guarantee_objection_does_not_break_price_gate(self):
        """An efficacy_doubt objection from a guarantee demand must not open the price gate.

        With trust=0.0 (below threshold) and no price inquiry, price_gate redirects a budget ask
        to pitch — the efficacy_doubt objection does not bypass the trust-gate check."""
        b = BeliefState.fresh()
        b.active_objection = "efficacy_doubt"
        # trust at 0.0 (below the 0.5 open-price threshold), so price gate is closed
        b.drivers["trust"] = 0.0
        cfg = _cfg()
        dec = _decision("ask", target_slot="budget")
        from src.core.gates import price_gate
        out = price_gate(dec, b, cfg)
        # price_gate should redirect a budget ask to pitch (trust below threshold, no price inquiry)
        assert out.act == "pitch", (
            f"An efficacy_doubt objection must not open the price gate prematurely. "
            f"Expected price_gate to redirect to 'pitch', got {out.act!r}"
        )

    @pytest.mark.asyncio
    async def test_normal_discovery_turn_not_upgraded_to_objection(self):
        """Regression: a plain discovery answer must NOT be upgraded to an objection."""
        belief = BeliefState.fresh()
        llm = _flat_llm("statement", None)
        new_b = await dst.update(
            belief, "ask", "She is in third grade.", llm
        )
        assert new_b.active_objection is None, (
            f"Regression: 'She is in third grade.' must NOT produce an objection. "
            f"Got: {new_b.active_objection!r}"
        )
        assert new_b.last_user_act == "statement", (
            f"Plain statement must stay 'statement'. Got: {new_b.last_user_act!r}"
        )

    @pytest.mark.asyncio
    async def test_availability_statement_not_social_aside(self):
        """Regression: caller availability statement must NOT be misclassified as social_aside."""
        belief = BeliefState.fresh()
        llm = _flat_llm("statement", None)
        new_b = await dst.update(
            belief, "ask", "Wednesdays or Fridays after 4 work best for us", llm
        )
        assert new_b.last_user_act != "social_aside", (
            "Availability statement 'Wednesdays or Fridays after 4 work best for us' must NOT "
            f"be social_aside. Got: {new_b.last_user_act!r}"
        )
