# test_cb67_human_request_grounding.py — CB-67: an LLM-hallucinated 'human_request' label must be
# GROUNDED in a lexical person/speak cue before it can reach escalation_triggers (which treats an
# explicit human request as a TERMINAL escalate). Caught live: gpt-5-nano labeled "Can you tell me
# how this works?" human_request and the agent ended a brand-new call on turn 1.
from src.core.dst import _ground_human_request


# The exact live hallucination: a how-does-this-work opener with ZERO person cues.
KAREN_OPENER = (
    "Hi, I saw your site while looking for tutoring help. My daughter is in 10th grade and "
    "she's really struggling in chemistry. Can you tell me how this works?"
)


def test_hallucinated_human_request_on_plain_question_degrades_to_question():
    assert _ground_human_request("human_request", KAREN_OPENER) == "question"


def test_hallucinated_human_request_on_plain_statement_degrades_to_statement():
    assert _ground_human_request("human_request", "We're just getting started with tutoring.") == "statement"


def test_genuine_explicit_request_passes():
    assert _ground_human_request("human_request", "Can I just talk to a real person about this?") == "human_request"


def test_genuine_paraphrase_with_person_cue_passes():
    # Looser than the strict _HUMAN_REQUEST_RE: a paraphrase naming a person survives grounding.
    assert _ground_human_request("human_request", "Is there someone I could speak with?") == "human_request"
    assert _ground_human_request("human_request", "Put a representative on please.") == "human_request"


def test_non_human_request_acts_pass_through_unchanged():
    for act in ("question", "price_inquiry", "objection", "statement", "confirm_request", None):
        assert _ground_human_request(act, KAREN_OPENER) == act


def test_empty_utterance_with_human_request_label_degrades():
    assert _ground_human_request("human_request", "") == "statement"
