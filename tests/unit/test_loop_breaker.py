# Regression tests (TEST-FIRST) for SAFEGUARD 1 — the consolidated progress-based loop-breaker gate
# (src.core.gates.break_no_progress_loop) and the recent-acts / progress bookkeeping in respond.py.
# These pin the behavior the scattered guards (no_repeat_discovery on tracked slots only,
# _recent_objection_handles on objections only, NLG re-wording) did NOT cover: a free-form reactive
# stall (Marcus re-asking an UNtracked "SAT score?" ~7x; Dana re-offering "judge for yourself" 5x).
#   1. A stalled no-progress loop (K reactive turns, no slot/objection/intent progress) FORCES a commit.
#   2. A call making REAL progress each turn is NOT cut short by the breaker.
#   3. The objection-breakthrough behavior STILL holds, now routed through the unified gate.
#   4. STICKINESS (ISSUE 2): one soft forced commit, then a re-stall ESCALATES to a firm terminal —
#      never re-offers the same soft close (the dana_11 death-march fix). Flag latched in respond().
# Network-free: scripted/no LLM; asserts the DETERMINISTIC gate + meta bookkeeping only.
from __future__ import annotations

from src.config.settings import AgentConfig, Persona
from src.core import gates
from src.core.belief_state import BeliefState
from src.core.gates import _LOOP_BREAK_K, apply_gates, break_no_progress_loop
from src.core.policy import Decision


def make_config(**threshold_overrides) -> AgentConfig:
    thresholds = {
        "trust_gate_open_price": 0.5,
        "pushiness_cap": 0.7,
        "pushiness_pressure_count_cap": 99,  # disable the count-based pushiness back-off for these tests
        "escalate_low_confidence_turns": 2,
        "low_confidence_level": 0.4,
        "max_concession_band": 0.15,
        "discovery_slots_required": 0,
        "min_turns_before_close": 4,
        "discovery_slots_before_close": 1,
        "extreme_bail_deescalate": 0.9,
        "max_slot_asks": 99,  # don't let no_repeat_discovery interfere — we test the GENERAL breaker
    }
    thresholds.update(threshold_overrides)
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Alex", role="learning advisor", style="warm-consultative"),
        prompts={"policy": "Propose the next act as JSON.", "nlg": "Reply warmly."},
        playbooks={"discovery_sequence": ["grade_level", "subject", "goal", "timeline", "budget"]},
        thresholds=thresholds,
    )


def _stalled_meta(acts: list[str]) -> dict:
    """A belief.meta snapshot encoding the last K gated acts as REACTIVE with NO progress recorded —
    the prior-turn snapshot equals the current state so the progress delta is zero."""
    return {
        "recent_acts": list(acts),
        # prior snapshot identical to current -> no slot/objection/intent progress this turn
        "progress_snapshot": {"locked_slots": 0, "active_objection": None, "purchase_intent": 0.5},
    }


# --- 1. a stalled no-progress reactive loop forces a commit ---------------------------------------

def test_stalled_reactive_loop_forces_commit_not_a_fourth_reactive_act():
    """K consecutive REACTIVE acts (the free-form re-ask Marcus did 7x) with NO progress must NOT be
    allowed to become yet another identical reactive act — the breaker forces a COMMIT (a close on a
    closeable belief). This is the case no existing guard caught (the slot is untracked)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 7
    b.drivers["trust"] = 0.7  # closeable (warm, no objection)
    b.meta = _stalled_meta(["answer_via_kb"] * _LOOP_BREAK_K)
    # The agent keeps proposing the same reactive answer (re-asking "SAT score?" framed as an answer).
    out = break_no_progress_loop(Decision(act="answer_via_kb"), b, cfg, history=[])
    assert out.act == "attempt_close", "a stalled reactive loop must be forced to commit (close)"


def test_stalled_loop_with_no_closeable_belief_gives_honest_bottom_line():
    """When the belief is NOT closeable (cold, no readiness) and there's no genuine escalation reason,
    a stalled loop still must not loop — it produces an honest bottom-line answer_via_kb (a real
    next-step turn), NOT a re-ask. The act may stay answer_via_kb but the gate TAGS it as the
    bottom-line so it's no longer a loop turn (the breaker fired)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 5
    b.drivers["trust"] = 0.5  # NOT warm enough to close
    b.meta = _stalled_meta(["answer_via_kb"] * _LOOP_BREAK_K)
    out = break_no_progress_loop(Decision(act="answer_via_kb"), b, cfg, history=[])
    # Not closeable + no escalation reason -> honest bottom-line, flagged so it's a deliberate break.
    assert out.act in ("answer_via_kb", "attempt_close")
    assert out.meta.get("loop_break") is True, "the gate must mark the bottom-line break for observability"


def test_stalled_loop_escalates_on_a_genuine_reason():
    """If a genuine escalation reason fires (explicit human request) AND the belief isn't closeable,
    the forced commit is a real escalation, not a re-ask."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 5
    b.drivers["trust"] = 0.5
    b.last_user_act = "human_request"
    b.meta = _stalled_meta(["answer_via_kb"] * _LOOP_BREAK_K)
    out = break_no_progress_loop(Decision(act="handle_objection"), b, cfg, history=[])
    assert out.act == "escalate"


# --- 2. a call making REAL progress each turn is NOT cut short ------------------------------------

def test_progress_each_turn_is_not_cut_short():
    """A normal multi-turn call where SOMETHING advanced recently (a slot locked / objection cleared /
    intent rose) must NOT trip the breaker even after several reactive acts — only a genuine stall does.
    Here the prior snapshot shows fewer locked slots than now (a slot just locked) -> progress."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 7
    b.drivers["trust"] = 0.7
    b.set_slot("grade_level", "8th", 0.9)  # one slot locked now
    b.meta = {
        "recent_acts": ["answer_via_kb"] * _LOOP_BREAK_K,
        # prior snapshot had ZERO locked slots -> a slot locked since -> PROGRESS this window
        "progress_snapshot": {"locked_slots": 0, "active_objection": None, "purchase_intent": 0.5},
    }
    out = break_no_progress_loop(Decision(act="answer_via_kb"), b, cfg, history=[])
    assert out.act == "answer_via_kb", "real progress must not be cut short by the loop breaker"


def test_fewer_than_K_reactive_acts_does_not_trip():
    """Below K consecutive reactive acts the breaker never fires (conservative K so a normal multi-
    objection call is not cut short)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 7
    b.drivers["trust"] = 0.7
    b.meta = _stalled_meta(["answer_via_kb"] * (_LOOP_BREAK_K - 1))
    out = break_no_progress_loop(Decision(act="answer_via_kb"), b, cfg, history=[])
    assert out.act == "answer_via_kb"


def test_a_non_reactive_act_in_the_window_resets_the_stall():
    """If a COMMIT (attempt_close) already happened within the recent window, the run of reactive acts
    is broken — the breaker doesn't fire (we're not actually stuck looping)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 7
    b.drivers["trust"] = 0.7
    acts = ["answer_via_kb"] * (_LOOP_BREAK_K - 1) + ["attempt_close"]
    b.meta = _stalled_meta(acts)
    out = break_no_progress_loop(Decision(act="answer_via_kb"), b, cfg, history=[])
    assert out.act == "answer_via_kb"


# --- 3. the objection-breakthrough behavior STILL holds (now via the unified gate) ----------------

def test_objection_breakthrough_still_breaks_through_via_unified_gate():
    """The OLD _recent_objection_handles / _BREAKTHROUGH_OBJECTION_HANDLES special case is subsumed by
    the general no-progress breaker: a relentless objector handled repeatedly with NO progress (the
    objection never clears) must still break through to a close. apply_gates is the contract."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 8
    b.drivers["trust"] = 0.6
    b.active_objection = "efficacy_doubt"  # still open (handled repeatedly, never cleared)
    # Recent gated acts were all handle_objection (reactive) with no progress recorded.
    b.meta = _stalled_meta(["handle_objection"] * _LOOP_BREAK_K)
    history = [
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "still not sure"},
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "really?"},
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "hmm"},
    ]
    out = apply_gates(Decision(act="handle_objection"), b, cfg, history=history)
    # Must break the deadlock with a low-pressure close (the breakthrough), not handle it a 4th time.
    assert out.act == "attempt_close", f"objection deadlock must break through, got {out.act}"


def test_warm_no_discovery_bounce_loop_is_forced_to_a_callback_commit():
    """The silent-loop the reorder fixes: a WARM prospect (trust above the close bar) with NO discovery
    and neutral intent. advance_to_close converts a reactive act to a paid close, but close_floor (blind
    close) backs it RIGHT back to a pitch — a reactive act — so the call bounces forever. After K such
    no-progress reactive turns the breaker (running AFTER close_floor/pushiness) forces a COMMIT, down-
    tiered to the FREE callback rung (close_floor won't veto it). This is Marcus re-asking an untracked
    'SAT score?' / Dana re-offering 'judge for yourself' on a warm-but-thin call."""
    cfg = make_config(close_ready_trust=0.6)
    b = BeliefState.fresh()
    b.turn_count = 7
    b.drivers["trust"] = 0.7  # warm enough that advance_to_close WANTS to close (but it'd be blind)
    b.meta = _stalled_meta(["answer_via_kb"] * _LOOP_BREAK_K)
    out = apply_gates(Decision(act="answer_via_kb", rationale="re-ask SAT score"), b, cfg, history=[])
    assert out.act == "attempt_close" and out.tier == "callback", (out.act, out.tier)
    assert out.meta.get("loop_break") is True


def test_healthy_warm_call_with_discovery_closes_normally_not_forced_callback():
    """A HEALTHY warm prospect with real discovery (a slot locked) closes at the proper PAID tier via
    advance_to_close — the breaker must NOT down-tier it to callback or otherwise interfere (only a
    genuine no-progress stall is cut short). The locked slot also makes this turn show PROGRESS vs a
    zero-slot prior snapshot, so the breaker stands down."""
    cfg = make_config(close_ready_trust=0.6)
    b = BeliefState.fresh()
    b.turn_count = 7
    b.drivers["trust"] = 0.7
    b.set_slot("grade_level", "8th", 0.9)  # real discovery -> a paid close is not blind
    b.meta = {
        "recent_acts": ["answer_via_kb"] * _LOOP_BREAK_K,
        "progress_snapshot": {"locked_slots": 0, "active_objection": None, "purchase_intent": 0.5},
    }
    out = apply_gates(Decision(act="answer_via_kb"), b, cfg, history=[])
    assert out.act == "attempt_close" and out.tier == "consultation"
    assert out.meta.get("loop_break") is None  # closed via advance_to_close, NOT the loop breaker


def test_objection_handled_with_progress_is_not_cut_short():
    """A multi-objection call where each handled objection actually CLEARS (progress) is the normal
    healthy path — it must NOT be force-closed by the unified breaker. Here the prior snapshot shows an
    active objection that is now None (the objection cleared) -> progress."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 8
    b.drivers["trust"] = 0.6
    b.active_objection = None  # the latest objection cleared
    b.meta = {
        "recent_acts": ["handle_objection"] * _LOOP_BREAK_K,
        # prior snapshot had an objection set -> it cleared -> PROGRESS
        "progress_snapshot": {"locked_slots": 0, "active_objection": "price", "purchase_intent": 0.5},
    }
    out = break_no_progress_loop(Decision(act="handle_objection"), b, cfg, history=[])
    assert out.act == "handle_objection", "a cleared objection is progress; don't force a close"


# --- 4. STICKINESS (ISSUE 2 — the dana_11 death-march): one soft commit, then escalate-and-stop ----

def test_first_stall_soft_commits_then_second_stall_escalates_terminal():
    """ISSUE 2: the FIRST no-progress stall gets a soft forced commit (a close); but a SECOND stall
    after that (loop_break_fired latched — the prospect declined and the agent relapsed for K more
    reactive no-progress turns) must ESCALATE to a firm terminal, NOT re-offer the same soft close."""
    cfg = make_config(close_ready_trust=0.6)
    # First stall, no prior loop_break -> soft commit (down-tiered to callback, blind).
    b1 = BeliefState.fresh(); b1.turn_count = 7; b1.drivers["trust"] = 0.7
    b1.meta = _stalled_meta(["answer_via_kb"] * _LOOP_BREAK_K)
    first = apply_gates(Decision(act="answer_via_kb"), b1, cfg, history=[])
    assert first.act == "attempt_close" and first.meta.get("loop_break") is True
    assert first.meta.get("loop_terminal") is not True  # the FIRST break is a soft commit, not terminal

    # Second stall, loop_break_fired latched -> firm terminal escalate.
    b2 = BeliefState.fresh(); b2.turn_count = 12; b2.drivers["trust"] = 0.7
    b2.meta = _stalled_meta(["answer_via_kb"] * _LOOP_BREAK_K)
    b2.meta["loop_break_fired"] = True
    second = apply_gates(Decision(act="answer_via_kb"), b2, cfg, history=[])
    assert second.act == "escalate", f"a re-stall after a soft commit must escalate, got {second.act}"
    assert second.meta.get("loop_terminal") is True
    assert b2.escalation_imminent is True


def test_sticky_terminal_escalate_survives_escalate_only_if_warranted():
    """The sticky terminal escalate is marked loop_terminal so escalate_only_if_warranted (which would
    otherwise redirect a triggerless escalate back into a selling/answer turn) leaves it alone — without
    this the death-march would resume. Verify via apply_gates (the full chain) and the gate directly."""
    cfg = make_config(close_ready_trust=0.6)
    b = BeliefState.fresh(); b.turn_count = 12; b.drivers["trust"] = 0.7
    b.meta = _stalled_meta(["pitch"] * _LOOP_BREAK_K)
    b.meta["loop_break_fired"] = True
    out = apply_gates(Decision(act="pitch"), b, cfg, history=[])
    assert out.act == "escalate", "the sticky terminal must NOT be redirected back into selling"
    # Direct: escalate_only_if_warranted passes a loop_terminal escalate through unchanged.
    term = Decision(act="escalate"); term.meta["loop_terminal"] = True
    assert gates.escalate_only_if_warranted(term, BeliefState.fresh(), cfg, history=[]).act == "escalate"


def test_no_stickiness_without_a_prior_loop_break():
    """Stickiness only kicks in AFTER a soft commit fired — a first-ever stall (no loop_break_fired)
    must still get the soft commit, never jump straight to a terminal escalate."""
    cfg = make_config(close_ready_trust=0.6)
    b = BeliefState.fresh(); b.turn_count = 7; b.drivers["trust"] = 0.7
    b.meta = _stalled_meta(["answer_via_kb"] * _LOOP_BREAK_K)  # NO loop_break_fired
    out = break_no_progress_loop(Decision(act="answer_via_kb"), b, cfg, history=[])
    assert out.act == "attempt_close"  # soft commit, not escalate
    assert out.meta.get("loop_terminal") is not True


async def test_respond_endtoend_one_soft_commit_then_escalate_no_deathmarch():
    """End-to-end via respond() (MockLLMClient): a relentlessly stalling prospect gets ONE soft forced
    commit, and once the loop_break_fired flag latches and the call re-stalls, the breaker ESCALATES —
    proving the dana_11 death-march (relapse into 4+ reactive turns after a declined commit) is gone.
    The flag persists across turns via BeliefState.copy()."""
    import json
    from src.core.belief_state import DRIVERS
    from src.core.llm import MockLLMClient
    from src.core.respond import respond

    cfg = make_config(close_ready_trust=0.6)
    facts = ["Our tutoring is personalized and most families report steady improvement over a few months."]
    # A grounded, fabrication-free answer so Safeguard 2 never interferes — we isolate Safeguard 1.
    answer = "Most families report steady improvement over a few months with personalized tutoring."

    def _turn_script(act: str) -> list:
        return [json.dumps({d: 0.0 for d in DRIVERS}),
                json.dumps({"act": act, "rationale": "r", "confidence": 0.6}),
                answer]

    b = BeliefState.fresh(); b.drivers["trust"] = 0.7
    history: list = []
    acts: list[str] = []
    # Drive enough reactive no-progress turns to trip the breaker twice (each LLM proposes answer_via_kb;
    # the breaker overrides). turn_count advances via DST; trust stays warm; no slot ever locks.
    for i in range(9):
        llm = MockLLMClient(_turn_script("answer_via_kb"))
        d, _reply, b = await respond(
            b, history, "is this really worth it?", llm, cfg,
            retrieved_facts=facts, last_user_act="objection",
        )
        history.append({"role": "user", "text": "is this really worth it?"})
        history.append({"role": "agent", "act": d.act, "text": _reply})
        acts.append(d.act)

    # Exactly one soft commit (a close), then a terminal escalate later — NOT repeated soft closes.
    assert "attempt_close" in acts, f"expected one forced soft commit, got {acts}"
    assert "escalate" in acts, f"expected a sticky terminal escalate after re-stall, got {acts}"
    assert acts.index("attempt_close") < acts.index("escalate"), acts
    # No relapse into a long reactive death-march AFTER the escalate (escalation latched).
    after = acts[acts.index("escalate") + 1:]
    assert all(a == "escalate" for a in after), f"death-march resumed after escalate: {acts}"
