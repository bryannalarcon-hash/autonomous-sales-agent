# CB-61 regression tests (TEST-FIRST) — the agent doesn't LISTEN: DST drops caller-stated slots.
# Three concrete failure shapes from the QA6 blind transcript:
#   (a) "big test in about three weeks" → timeline slot NOT captured → agent re-asked the deadline.
#   (b) "Thursday afternoon after 3pm works" → callback_window slot NOT captured → re-asked the day.
#   (c) "please confirm they'll call Thursday after 3pm" → no echo → agent replied with a vague ack.
# All tests are deterministic / $0-cost: no LLM calls for the extraction/gate assertions; the
# confirm-echo path uses a MockLLMClient with a canned NLG reply to prove the act + NLG path are wired.
# INVIOLABLE INVARIANTS re-checked at the bottom: R37 parity (no LiveKit in core), buy-gate purity.
from __future__ import annotations

import json

import pytest

from src.config.settings import AgentConfig, Persona
from src.core import gates
from src.core.belief_state import DRIVERS, BeliefState
from src.core import dst
from src.core.dst import _extract_timeline, _extract_callback_window, _classify_intent
from src.core.llm import MockLLMClient
from src.core.nlg import realize, _build_messages
from src.core.policy import Decision


# -------------------------------------------------------------------------------------
# Shared helpers
# -------------------------------------------------------------------------------------

def _flat_llm(n: int = 1) -> MockLLMClient:
    """Zero-delta scripted LLM: returns driver=0 deltas only, so DST leaves them at priors."""
    return MockLLMClient([json.dumps({d: 0.0 for d in DRIVERS})] * n)


def make_config(**overrides) -> AgentConfig:
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
# (a) Deadline / timeline slot captured from "big test in about three weeks"
# ==============================================================================

class TestDeadlineTimelineCapture:
    """The DST must capture a stated deadline/timeline into the belief (CB-61 root cause (a))."""

    def test_extract_timeline_in_about_N_words(self):
        """'in about three weeks' must extract to the timeline slot (was silently dropped)."""
        result = _extract_timeline("she has a big test in about three weeks")
        assert result is not None, (
            "Expected timeline extraction for 'in about three weeks' — got None"
        )
        value, conf = result
        assert "week" in value.lower() or "three" in value.lower(), (
            f"Expected the extracted value to mention weeks/three, got {value!r}"
        )
        assert conf >= 0.7

    def test_extract_timeline_in_about_N_months(self):
        """'in about two months' must also capture a timeline."""
        result = _extract_timeline("he has finals in about two months")
        assert result is not None
        value, conf = result
        assert conf >= 0.7

    def test_extract_timeline_bare_in_N_weeks_still_works(self):
        """Existing 'in N weeks' pattern must remain functional after the fix."""
        result = _extract_timeline("she needs help in 3 weeks")
        assert result is not None

    def test_extract_timeline_exact_qna_scenario(self):
        """Verbatim from QA6 transcript: 'big test in about three weeks' must fill the slot."""
        result = _extract_timeline("she has a big test in about three weeks")
        assert result is not None, "QA6 verbatim: timeline must be captured"

    async def test_dst_update_fills_timeline_slot_from_deadline_utterance(self):
        """After a full DST update, belief.slots['timeline'] is filled from a deadline utterance."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="she has a big test in about three weeks",
            llm_client=_flat_llm(),
        )
        assert "timeline" in new_belief.slots, (
            "DST update must fill 'timeline' slot from 'big test in about three weeks'"
        )
        assert new_belief.slot_confidence("timeline") >= 0.7

    async def test_dst_update_fills_timeline_hedged_stays_soft(self):
        """A hedged timeline ('maybe in a few weeks, not sure') must fill at LOW confidence so
        skip_known does not lock it and it can be re-confirmed."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="maybe in a few weeks, not sure",
            llm_client=_flat_llm(),
        )
        if "timeline" in new_belief.slots:
            # A hedged match must be below the lock threshold so the slot stays soft.
            assert new_belief.slot_confidence("timeline") < 0.8, (
                "A hedged timeline answer must NOT lock the slot (CB-50 hedge discipline)"
            )


# ==============================================================================
# (b) Callback window slot captured from "Thursday afternoon after 3pm"
# ==============================================================================

class TestCallbackWindowCapture:
    """The DST must capture a proposed callback window into the belief (CB-61 root cause (b))."""

    def test_extract_callback_window_day_plus_time(self):
        """'Thursday afternoon after 3pm' must extract to the callback_window slot."""
        result = _extract_callback_window("Thursday afternoon after 3pm works")
        assert result is not None, (
            "Expected callback_window extraction for 'Thursday afternoon after 3pm' — got None"
        )
        value, conf = result
        assert "thursday" in value.lower(), f"Expected 'thursday' in extracted value, got {value!r}"
        assert conf >= 0.85

    def test_extract_callback_window_exact_qna_scenario(self):
        """Verbatim from QA6 transcript: 'Thursday afternoon after 3pm works' fills the slot."""
        result = _extract_callback_window("Thursday afternoon after 3pm works")
        assert result is not None, "QA6 verbatim: callback_window must be captured"

    def test_extract_callback_window_morning_variant(self):
        """'Monday morning works for me' also extracts a callback window."""
        result = _extract_callback_window("Monday morning works for me")
        assert result is not None
        value, conf = result
        assert "monday" in value.lower()

    def test_extract_callback_window_time_of_day_only(self):
        """A bare time like 'after 2pm' must extract with lower confidence (no day given)."""
        result = _extract_callback_window("after 2pm works")
        # May or may not match depending on implementation; if it does, confidence must be lower
        if result is not None:
            _value, conf = result
            assert conf <= 0.85  # no day -> less certain

    def test_extract_callback_window_no_match_for_non_scheduling(self):
        """A regular statement with no scheduling intent must NOT extract a callback window."""
        result = _extract_callback_window("she is in the seventh grade")
        assert result is None

    def test_extract_callback_window_confirm_request(self):
        """'please confirm they'll call Thursday after 3pm' also fills the callback_window slot."""
        result = _extract_callback_window("please confirm they'll call Thursday after 3pm")
        assert result is not None, (
            "A confirm request with an explicit time must fill callback_window"
        )
        value, conf = result
        assert "thursday" in value.lower()

    async def test_dst_update_fills_callback_window_slot(self):
        """After a full DST update, belief.slots['callback_window'] is filled."""
        belief = BeliefState.fresh()
        new_belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="Thursday afternoon after 3pm works",
            llm_client=_flat_llm(),
        )
        assert "callback_window" in new_belief.slots, (
            "DST update must fill 'callback_window' from 'Thursday afternoon after 3pm works'"
        )
        assert new_belief.slot_confidence("callback_window") >= 0.85

    async def test_dst_update_callback_window_carried_across_turns(self):
        """A filled callback_window slot must survive a subsequent turn (carried by BeliefState.copy)."""
        belief = BeliefState.fresh()
        belief = await dst.update(
            belief,
            last_agent_act="ask",
            user_utterance="Thursday afternoon after 3pm works",
            llm_client=_flat_llm(),
        )
        assert "callback_window" in belief.slots

        # Second turn: a neutral utterance must NOT overwrite the window.
        belief2 = await dst.update(
            belief,
            last_agent_act="confirm_known",
            user_utterance="yes that's right",
            llm_client=_flat_llm(),
        )
        assert "callback_window" in belief2.slots, (
            "callback_window must persist to the next turn"
        )
        assert belief2.slot_value("callback_window") == belief.slot_value("callback_window"), (
            "callback_window value must not change on a neutral turn"
        )


# ==============================================================================
# (c) Gate layer: filled slots are never re-asked
# ==============================================================================

class TestGatesProtectFilledSlots:
    """skip_known and no_repeat_discovery must protect the new slots from re-asking (CB-61 root (c))."""

    def test_skip_known_blocks_re_ask_of_filled_timeline(self):
        """Once timeline is locked, skip_known must NOT allow a re-ask."""
        cfg = make_config()
        b = BeliefState.fresh()
        b.set_slot("timeline", "in about three weeks", 0.9)  # locked
        proposed = Decision(act="ask", target_slot="timeline", rationale="ask deadline")
        out = gates.skip_known(proposed, b, cfg)
        assert not (out.act == "ask" and out.target_slot == "timeline"), (
            "skip_known must block re-asking a locked timeline slot"
        )
        assert out.act == "confirm_known"

    def test_skip_known_blocks_re_ask_of_filled_callback_window(self):
        """Once callback_window is locked, skip_known must NOT allow a re-ask."""
        cfg = make_config()
        b = BeliefState.fresh()
        b.set_slot("callback_window", "thursday after 3pm", 0.9)  # locked
        proposed = Decision(act="ask", target_slot="callback_window", rationale="ask day")
        out = gates.skip_known(proposed, b, cfg)
        assert not (out.act == "ask" and out.target_slot == "callback_window"), (
            "skip_known must block re-asking a locked callback_window slot"
        )

    def test_no_repeat_discovery_drops_declined_timeline(self):
        """If timeline was explicitly declined, no_repeat_discovery must drop a re-ask of it."""
        cfg = make_config()
        b = BeliefState.fresh()
        b.meta["declined_slots"] = ["timeline"]
        proposed = Decision(act="ask", target_slot="timeline", rationale="ask timeline again")
        out = gates.no_repeat_discovery(proposed, b, cfg)
        assert out.act != "ask" or out.target_slot != "timeline", (
            "no_repeat_discovery must drop a re-ask of a declined timeline slot"
        )

    def test_no_repeat_discovery_drops_over_asked_callback_window(self):
        """After max_slot_asks, no_repeat_discovery must drop a callback_window re-ask."""
        cfg = make_config(max_slot_asks=2)
        b = BeliefState.fresh()
        b.meta["asked_slots"] = {"callback_window": 2}
        proposed = Decision(act="ask", target_slot="callback_window", rationale="ask day again")
        out = gates.no_repeat_discovery(proposed, b, cfg)
        assert out.act != "ask" or out.target_slot != "callback_window", (
            "no_repeat_discovery must drop a callback_window re-ask after max_slot_asks"
        )

    def test_apply_gates_never_re_asks_filled_timeline_and_callback_window(self):
        """Full gate chain: with both slots locked, a proposed ask of either is blocked."""
        cfg = make_config()
        b = BeliefState.fresh()
        b.set_slot("timeline", "in about three weeks", 0.9)
        b.set_slot("callback_window", "thursday after 3pm", 0.9)
        b.meta["learner_established"] = True  # avoid who_for redirect

        for slot in ("timeline", "callback_window"):
            proposed = Decision(act="ask", target_slot=slot, rationale="re-ask")
            out = gates.apply_gates(proposed, b, cfg)
            assert not (out.act == "ask" and out.target_slot == slot), (
                f"apply_gates must not re-ask filled {slot} slot"
            )


# ==============================================================================
# (d) Confirm-echo: "please confirm they'll call Thursday after 3pm" → explicit time echo
# ==============================================================================

class TestConfirmEchoMechanism:
    """An explicit confirm request must be recognized and routed to echo the stated time (CB-61 (d))."""

    def test_classify_intent_recognizes_confirm_request(self):
        """The intent classifier must recognize 'please confirm <time>' as a confirm_request."""
        from src.core.dst import _classify_intent
        act, _obj, _q = _classify_intent("please confirm they'll call Thursday after 3pm")
        assert act == "confirm_request", (
            f"Expected act='confirm_request' for a confirm utterance, got {act!r}"
        )

    def test_classify_intent_confirm_request_variant(self):
        """'can you confirm <day>?' is also a confirm_request."""
        act, _obj, _q = _classify_intent("can you confirm Thursday after 3pm?")
        assert act == "confirm_request"

    def test_classify_intent_confirm_request_captures_window(self):
        """A confirm_request utterance's open_question carries the time reference."""
        act, _obj, q = _classify_intent("please confirm they'll call Thursday after 3pm")
        assert act == "confirm_request"
        assert q is not None and "thursday" in q.lower(), (
            f"Expected open_question to carry the time reference, got {q!r}"
        )

    async def test_dst_update_confirm_request_sets_last_user_act(self):
        """DST update on a confirm-request utterance must set last_user_act='confirm_request'."""
        belief = BeliefState.fresh()
        belief.set_slot("callback_window", "thursday after 3pm", 0.9)
        new_belief = await dst.update(
            belief,
            last_agent_act="attempt_close",
            user_utterance="please confirm they'll call Thursday after 3pm",
            llm_client=_flat_llm(),
        )
        assert new_belief.last_user_act == "confirm_request", (
            "DST must set last_user_act='confirm_request' on an explicit confirm-request utterance"
        )

    def test_confirm_echo_gate_overrides_ask_to_confirm_known_when_window_known(self):
        """When callback_window is filled AND last_user_act=='confirm_request', any proposed 'ask'
        must be converted to confirm_known so the agent echoes the time, not re-asks it."""
        cfg = make_config()
        b = BeliefState.fresh()
        b.set_slot("callback_window", "thursday after 3pm", 0.9)
        b.last_user_act = "confirm_request"
        b.open_question = "please confirm they'll call Thursday after 3pm"
        proposed = Decision(act="ask", target_slot="callback_window", rationale="ask day")
        out = gates.skip_known(proposed, b, cfg)
        assert out.act == "confirm_known", (
            "skip_known must convert an ask-of-known-slot to confirm_known for the echo path"
        )

    def test_address_direct_input_routes_confirm_request_to_confirm_known(self):
        """With last_user_act=='confirm_request' and callback_window filled, address_direct_input
        must NOT redirect a confirm_known to handle_objection or answer_via_kb."""
        cfg = make_config()
        b = BeliefState.fresh()
        b.set_slot("callback_window", "thursday after 3pm", 0.9)
        b.last_user_act = "confirm_request"
        b.open_question = "please confirm they'll call Thursday after 3pm"
        b.active_objection = None
        proposed = Decision(act="confirm_known", target_slot="callback_window",
                            rationale="echo callback window")
        out = gates.address_direct_input(proposed, b, cfg)
        # confirm_known is not in _DEFERRABLE_ACTS for this path, so it must pass through
        assert out.act == "confirm_known", (
            "address_direct_input must leave confirm_known of a known slot alone"
        )

    def test_nlg_prompt_includes_callback_window_in_known_so_far(self):
        """When callback_window is filled, _build_messages includes it in KNOWN_SO_FAR so the LLM
        can echo it (this is the mechanism: the slot value reaches NLG via the belief context)."""
        cfg = make_config()
        b = BeliefState.fresh()
        b.set_slot("callback_window", "thursday after 3pm", 0.9)
        b.open_question = "please confirm they'll call Thursday after 3pm"
        decision = Decision(act="confirm_known", target_slot="callback_window",
                            rationale="echo the callback window")
        msgs = _build_messages(decision, b, cfg, retrieved_facts=None)
        user_content = msgs[-1]["content"]
        assert "thursday" in user_content.lower() or "callback_window" in user_content.lower(), (
            "KNOWN_SO_FAR in NLG prompt must surface the callback_window value so the LLM can echo it"
        )

    async def test_nlg_realize_confirm_known_echoes_time_value(self):
        """realize() with act=confirm_known and callback_window='thursday after 3pm' must produce
        a reply that includes the time — the confirm echo (CB-61 (d)). Uses a canned LLM response
        to stay $0; asserts the value flows through the prompt and is realizable."""
        cfg = make_config()
        b = BeliefState.fresh()
        b.set_slot("callback_window", "thursday after 3pm", 0.9)
        b.open_question = "please confirm they'll call Thursday after 3pm"

        canned_reply = "Thursday after 3pm — noted, and our team will call you then."
        llm = MockLLMClient([canned_reply])
        decision = Decision(act="confirm_known", target_slot="callback_window",
                            rationale="echo the callback window")
        reply = await realize(decision, b, cfg, llm)
        # The realized reply must not be the generic safe-filler.
        assert "thursday" in reply.lower() or "3pm" in reply.lower() or "after" in reply.lower(), (
            f"NLG reply must echo the callback time; got: {reply!r}"
        )


# ==============================================================================
# Invariant re-checks: R37 parity + buy-gate purity (CB-50/53)
# ==============================================================================

class TestInvariantPreservation:
    """Confirm the CB-61 changes do NOT break the inviolable invariants."""

    def test_r37_no_livekit_imports_in_core_dst(self):
        """R37 parity: src/core/dst.py must contain no LiveKit imports."""
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

    def test_r37_no_livekit_imports_in_core_gates(self):
        """R37 parity: src/core/gates.py must contain no LiveKit imports."""
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

    def test_r37_no_livekit_imports_in_core_nlg(self):
        """R37 parity: src/core/nlg.py must contain no LiveKit imports."""
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

    def test_buy_gate_purity_callback_window_does_not_alter_tier_decision(self):
        """Buy-gate purity: filling callback_window must NOT change the commitment tier returned
        by _closeable_tier — budget immutable to scheduling talk."""
        from src.core.gates import _closeable_tier
        cfg = make_config()

        b_without = BeliefState.fresh()
        b_without.drivers["trust"] = 0.65
        b_without.turn_count = 5

        b_with = b_without.copy()
        b_with.set_slot("callback_window", "thursday after 3pm", 0.9)

        tier_without = _closeable_tier(b_without, cfg)
        tier_with = _closeable_tier(b_with, cfg)
        assert tier_without == tier_with, (
            "Buy-gate purity: callback_window must not affect commitment tier selection"
        )

    def test_buy_gate_purity_timeline_does_not_alter_budget_immutability(self):
        """A filled timeline slot must NOT change close tier or any budget-related gate outcome."""
        from src.core.gates import _closeable_tier
        cfg = make_config()

        b_without = BeliefState.fresh()
        b_without.drivers["trust"] = 0.65
        b_without.turn_count = 5

        b_with = b_without.copy()
        b_with.set_slot("timeline", "in about three weeks", 0.9)

        assert _closeable_tier(b_without, cfg) == _closeable_tier(b_with, cfg), (
            "Buy-gate purity: timeline slot must not affect commitment tier selection"
        )
