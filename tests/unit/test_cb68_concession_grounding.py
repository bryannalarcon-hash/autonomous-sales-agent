# test_cb68_concession_grounding.py — CB-68: an LLM-proposed price concession is GROUNDED in live
# price context before any gate acts on it. Caught live: the policy LLM proposed concession>band on
# a scheduling turn ("Thursday works — what do you need?") and escalation_triggers terminally
# escalated a healthy close. Outside price talk the phantom fraction is stripped; inside genuine
# price talk the over-band design (defer to a specialist) is unchanged.
from src.config.settings import load_config
from src.core.belief_state import BeliefState
from src.core.gates import _price_context_live, apply_gates
from src.core.policy import Decision


def _belief(**kw) -> BeliefState:
    b = BeliefState()
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _decision(**kw) -> Decision:
    base = dict(act="attempt_close", target_slot=None, tier="callback", rationale="t", confidence=0.9)
    base.update(kw)
    return Decision(**base)


CFG = load_config("champion_v0")


def test_phantom_concession_on_scheduling_turn_is_stripped_not_escalated():
    # The live failure shape: caller accepted a Thursday callback; LLM proposed a junk concession.
    b = _belief(last_user_act="statement")
    out = apply_gates(_decision(concession=0.30), b, CFG)
    assert out.act != "escalate"
    assert out.concession is None


def test_over_band_concession_in_live_price_talk_still_escalates():
    # The R10 design is unchanged INSIDE genuine price context: over-band -> defer to a human.
    b = _belief(last_user_act="price_inquiry")
    out = apply_gates(_decision(concession=0.30), b, CFG)
    assert out.act == "escalate"
    assert "concession" in (out.rationale or "")


def test_within_band_concession_in_price_talk_not_escalated():
    b = _belief(last_user_act="price_inquiry")
    out = apply_gates(_decision(concession=0.10), b, CFG)
    assert out.act != "escalate"


def test_price_objection_counts_as_live_price_context():
    assert _price_context_live(_belief(active_objection="price"))


def test_affordability_refusals_count_as_live_price_context():
    assert _price_context_live(_belief(meta={"affordability_refusals": 1}))


def test_no_price_signals_means_no_price_context():
    assert not _price_context_live(_belief(last_user_act="statement"))
