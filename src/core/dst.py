# Hybrid Dialogue State Tracker (plan U3; docs/belief-state-schema.md "the belief update").
# `update(belief, last_agent_act, user_utterance, llm_client)` runs three factored sub-updates and
# returns a NEW BeliefState (pure: the input is not mutated):
#   1. Deterministic slot extraction — structured/regex pulls grade/subject/budget/timeline/etc.
#      with a per-slot confidence, merged under confidence arbitration so a garbled low-signal turn
#      cannot overwrite a high-confidence slot (near-monotonic accumulation, per the schema).
#   2. LLM latent-driver delta-update — a rubric-driven prompt asks the llm_client for a JSON of
#      *deltas* per driver; deltas are clamped into the six known drivers (hallucinated keys
#      ignored; unparseable JSON leaves drivers unchanged — no corruption).
#   3. Deterministically-DERIVED trends — `<driver>_velocity` = level(t) - level(t-1), computed from
#      the logged trajectory, NOT emitted by the LLM (Markov-preserving, zero extra latency).
#   4. EFSM stage derivation — advance belief.stage (greeting->discovery->pitch->close, or objection)
#      deterministically from the updated belief, so the policy isn't perpetually stuck at 'greeting'.
# Pure functions, NO LiveKit imports — both the text self-play and the voice adapter call this.
# MIXED-MODEL: the driver/intent call is mechanical strict-JSON that runs EVERY turn, so it can ride a
# cheaper/faster model than the policy & NLG calls. Set env DST_MODEL (e.g. openai/gpt-5-nano) to
# override JUST this call's model; unset -> the shared llm_client default (AGENT_MODEL). The robust
# fallback already tolerates a weaker model's bad JSON (drivers unchanged + regex intent fallback).
# CB-34/35/36/37 deterministic signals (all in belief.meta, carried across the call by copy()):
#   - grade extraction now accepts a BARE ordinal/number answer ("Third"->3, "8"->8 when grade was
#     just asked, sane 1-12 bound, "kindergarten"->K) so an answered grade is never re-asked (CB-35);
#     CB-50 (secondary): a HEDGED/disjunctive bare answer ("7th or 8th, not sure") extracts at REDUCED
#     confidence so an uncertain grade stays a soft, re-confirmable slot (not asserted back as fact);
#   - declined_slots: discovery slots the prospect declined to answer -> the no-repeat gate drops them;
#   - learner_established / learner_denied: whether WHO the tutoring is for has been established, so the
#     agent never presumes a learner exists before a who-is-this-for step (CB-34);
#   - disclosure + active_objection='concern': an emotional disclosure/complaint must be acknowledged
#     before any pitch (CB-36);
#   - hostility: a profane/abusive turn -> de-escalate, never pitch/re-ask (CB-37). Sticky for the call.
# CB-61 — the agent doesn't LISTEN: added two new deterministic slot extractors:
#   - _extract_timeline: widened to catch "in about N weeks/months" (was only "in N weeks"); covers the
#     QA6 "big test in about three weeks" miss where the agent re-asked the deadline it just heard.
#   - _extract_callback_window: new slot that captures a proposed callback day/time ("Thursday after
#     3pm") so skip_known/no_repeat_discovery protect it from re-asking. Also detects confirm requests
#     ("please confirm they'll call Thursday after 3pm") and routes them via _classify_intent ->
#     last_user_act='confirm_request' so the NLG path echoes the time instead of replying vaguely.
# CB-61 adversarial fixes (round 2):
#   - Defect 1: _extract_callback_window named-day branch now checks _DEADLINE_CONTEXT_RE — a day that
#     appears in a deadline context (test/exam/quiz) is NOT captured as a callback window. "her test is
#     on Thursday" / "the exam is Thursday morning" return None.
#   - Defect 2: _CONFIRM_REQUEST_RE tightened to require an agent-directed prefix (please/can you/just)
#     and explicitly excludes "confirm/double-check WITH my <person>" (a decision_maker deferral, not
#     an agent-directed confirmation request).
#   - Defect 4: _extract_callback_window applies _CALLBACK_HEDGE_RE to soften hedged windows below
#     the lock threshold (0.7 < 0.8) so "maybe Thursday could work?" stays re-confirmable.
# CB-76 — listening v2:
#   - (a) _extract_callback_window now captures PLURAL + DISJUNCTIVE day windows:
#     "Wednesdays or Fridays after 4 work best for us" → callback_window with whole phrase as value;
#     plural day names are normalised (Wednesdays→wednesday) before storage; the disjunction is kept
#     in the stored string; confidence at 0.9 (firmly volunteered); hedged forms still soften (existing
#     _CALLBACK_HEDGE_RE). The CB-67 deadline/context guard ("she has tutoring Thursdays") still blocks.
#   - (b) _TIMELINE_RE coverage extended: "end of the month", "end of month", "by the end of the
#     month", "next month", "this semester", "before finals" now fill the timeline slot.
#   - (c) NEVER-DENY RULE: _classify_intent now detects the 'memory_check' intent shape — a question
#     asking whether the caller already told us something ("did I already tell you when works?"). This
#     routes as last_user_act='memory_check' so NLG's _NEVER_DENY_NOTE instruction forbids asserting
#     the caller did NOT say something; if the slot is present in KNOWN_SO_FAR, echo it; if absent,
#     hedge honestly ("I may have missed it — when works best?"). Both layers are safe: extraction
#     catches the value; even when extraction misses, the NLG deny is forbidden.
#   - (d) Contact acknowledgment: when a turn captures name/phone/email the meta flag
#     'contact_just_captured' is set TRUE; the NLG _CONTACT_ACK_NOTE instruction then requires a
#     one-clause ack on ANY act ("Got it, Dana — I'll use this number.") so it rides every act.
# CB-85 (a) — Social-aside / non-sequitur detection: an off-topic social question (weather, sports,
#   casual chit-chat) that carries no product/scheduling/objection content routes as
#   'social_aside' so: (1) advance_to_close NEVER fires on it (the "storm" → "Sounds good — let
#   me get you scheduled" failure mode is blocked at the gate); (2) NLG's _SOCIAL_ASIDE_NOTE
#   instructs a brief one-clause human ack then a redirect, and explicitly forbids treating the
#   utterance as scheduling consent. Detection: a question matching _SOCIAL_ASIDE_CUES_RE topics AND
#   NOT matching _PRODUCT_KEYWORDS_RE (the AND-guard exemption: scheduling/policy/transaction terms
#   like reschedule/cancel/policy/refund/appointment/booking/cost) — so "what's the weather policy
#   if we need to reschedule?" stays a real product question, not small talk. Checked in
#   _classify_intent AFTER all priority intents (human_request/confirm_request/memory_check) and
#   BEFORE the generic "question" branch.
# CB-89 — Live-path social_aside override: the LLM intent vocabulary (question/price_inquiry/
#   human_request/objection/statement) never includes 'social_aside', so the regex fallback
#   _classify_intent fires social_aside only when the LLM call fails. Fix: after the LLM result is
#   accepted, run the same deterministic _SOCIAL_ASIDE_CUES_RE + _PRODUCT_KEYWORDS_RE check over the
#   raw utterance — if it hits AND the LLM didn't return a STRONGER intent (objection/human_request/
#   price_inquiry), override classified_act to 'social_aside'. This keeps the gate guard + NLG ack
#   working on the live path without depending on the LLM vocabulary. 'question'/'statement' from the
#   LLM are weaker than an explicit objection/escalation, so the social-aside override is safe there.
#   Genuine product/scheduling questions (incl. "weather policy if we need to reschedule") keep their
#   LLM classification because _PRODUCT_KEYWORDS_RE exempts them.
# CB-90 (a) — Guarantee/efficacy objection: _GUARANTEE_DEMAND_RE (pattern[0] of _OBJECTION_PATTERNS)
#   fires BEFORE diy_free so "Guarantee me a B or it's free" routes to efficacy_doubt, not diy_free
#   (misrouted because 'free' fired first). Round-2 TIGHTENING: the pattern requires a real grade/
#   result/outcome token after 'guarantee' (article-prefixed letter grade "a B", or grade/pass/score/
#   results/money-back/it-works), so "guarantee a tutor/time/slot" does NOT match; the bare standalone
#   'guarantee' was removed from the lower efficacy_doubt row so meta-uses ("guarantee we're on the
#   same page") no longer over-fire. The LIVE-PATH upgrade in update() is SCOPED to _GUARANTEE_DEMAND_RE
#   ONLY (not the full _OBJECTION_PATTERNS) — the LLM's 'statement' judgement is trusted for every
#   other objection type (no second-guessing "feel free"→diy_free / "worth it"→efficacy). The NLG
#   _NO_CONTACT_CLAIM_NOTE guard in nlg.py prevents claiming "I have your info" when KNOWN_SO_FAR holds
#   no contact (CB-83 tie-in: only assert captured contact when contact_just_captured / a phone-hash).
# CB-90 (b) — Caller availability acknowledgment: when the callback_window slot is NEWLY filled
#   this turn (absent in prior belief, present in updated), the meta flag
#   'callback_window_just_captured' is set TRUE; nlg.py's _CALLBACK_WINDOW_ACK_NOTE then instructs
#   a warm one-clause acknowledgment ("Got it — Wednesdays or Fridays after 4") and explicitly forbids
#   deflecting the caller's availability statement as a tutor-availability lookup ("I don't have
#   that in front of me"). The flag is ONE-SHOT: it fires on the turn after capture and resets.
from __future__ import annotations

import os
import re
from typing import Any, Optional

from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import LLMClient, Message

# Below this confidence a slot is "soft" and may be overwritten; at/above it the slot is "locked"
# and only a strictly-higher-confidence extraction can replace its value (confidence arbitration).
_LOCK_CONFIDENCE = 0.8

# --- Deterministic slot extractors ------------------------------------------------------------
# Each extractor returns (value, confidence) or None. Confidences are cold-start priors: explicit,
# unambiguous patterns score high; loose/inferred matches score low so they yield to firmer turns.

_GRADE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s*grade\b", re.I), 0.95),
    (re.compile(r"\bgrade\s*(\d{1,2})\b", re.I), 0.9),
]
_GRADE_WORDS = {
    "kindergarten": ("K", 0.9),
    "freshman": ("9", 0.7),
    "sophomore": ("10", 0.7),
    "junior": ("11", 0.7),
    "senior": ("12", 0.7),
}
# CB-35: ordinal/spelled-out words for a BARE grade answer ("Third", "he is in 5th", "fifth grade").
# A prospect answering "what grade?" with just an ordinal — no "grade" word — was being missed, so the
# agent re-asked the same question turn after turn. These map an ordinal token to its grade number.
_GRADE_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
    "kinder": "K",
}
# A bare ordinal ("3rd", "8th") or spelled ordinal anywhere; captured at MODERATE confidence (lower
# than the explicit "Nth grade" pattern) since context is thinner — it yields to a firmer later turn.
_GRADE_BARE_ORDINAL_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.I)
# A bare standalone number 1-12 ("8", "he is 5") — lowest confidence (could be anything), used ONLY
# when the agent JUST asked for the grade (see _extract_grade's `asked_grade` gate).
_GRADE_BARE_NUMBER_RE = re.compile(r"\b(\d{1,2})\b")
_GRADE_SPELLED_WORD_RE = re.compile(
    r"\b(" + "|".join(_GRADE_ORDINAL_WORDS) + r")(?:\s*grade)?\b", re.I
)
# A spelled ordinal directly followed by "grade" ("third grade", "fifth grade") is as explicit as the
# numeric "3rd grade" pattern, so it is ALWAYS trusted; the bare-ordinal/word/number forms below are
# trusted only as the direct answer to a just-asked grade question (CB-35 follow-up: a bare ordinal in
# a non-answer context — "1st time calling", "the 3rd of June", "my 2nd child" — must NOT lock a grade).
_GRADE_SPELLED_WITH_GRADE_RE = re.compile(
    r"\b(" + "|".join(_GRADE_ORDINAL_WORDS) + r")\s*grade\b", re.I
)
# Age context ("7 years old", "aged 9", "is 6 years"): a number here is an AGE, not a grade, so it must
# not be read as a grade even on the grade-answer turn (an age 7 != grade 7).
_AGE_CONTEXT_RE = re.compile(
    r"\b\d{1,2}\s*(?:years?|yrs?)\s*old\b|\bage[d]?\s+\d{1,2}\b|\bis\s+\d{1,2}\s+years?\b", re.I
)
# CB-50 (secondary): a HEDGED / disjunctive grade answer must NOT lock a confirmed grade slot. The
# eval showed the agent assert "Got it — eighth-grade math" when Pat said "7th or 8th, not sure" —
# an uncertain answer extracted as a high-confidence (lock-level) slot. A hedge is either explicit
# uncertainty ("not sure", "maybe", "I think", "around", "or so", "ish", "between") OR a disjunction
# of two candidate grades ("7th or 8th", "5 or 6"). When a BARE grade answer is hedged we drop its
# confidence below the lock threshold so the slot stays SOFT (re-confirmable next turn) instead of
# being asserted back as fact. Explicit "Nth grade" forms are still trusted (someone who says "8th
# grade" is committing) — only the thinner bare forms are softened.
_GRADE_HEDGE_RE = re.compile(
    r"\bnot\s+sure\b|\bnot\s+certain\b|\bmaybe\b|\bi\s+think\b|\bi\s+guess\b|\bprobably\b"
    r"|\baround\b|\bish\b|\bor\s+so\b|\bbetween\b|\bsomewhere\b|\bunsure\b|\bdon'?t\s+know\b",
    re.I,
)
# A disjunction of two grade-ish tokens ("7th or 8th", "5 or 6", "third or fourth") — the caller is
# offering two candidates, so neither is a confirmed grade.
_GRADE_DISJUNCTION_RE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+or\s+\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\b(?:" + "|".join(_GRADE_ORDINAL_WORDS) + r")\s+or\s+(?:"
    + "|".join(_GRADE_ORDINAL_WORDS) + r")\b",
    re.I,
)
# Lock-confidence ceiling for a HEDGED bare grade answer: kept strictly below dst._LOCK_CONFIDENCE so
# the slot stays soft (it can be confirmed on a firmer later turn rather than asserted back as fact).
_HEDGED_GRADE_CONFIDENCE = 0.6


def _is_hedged_grade(text: str) -> bool:
    """True when a grade answer expresses uncertainty or offers two candidate grades — so a BARE
    extraction from it must not lock a confirmed grade (CB-50 secondary: the '7th or 8th, not sure'
    false confirmation)."""
    return bool(_GRADE_HEDGE_RE.search(text) or _GRADE_DISJUNCTION_RE.search(text))


def _coerce_grade(raw: Any) -> Optional[str]:
    """Normalize a parsed grade to a sane 1-12 string or 'K'; reject out-of-range numbers (None)."""
    if raw == "K":
        return "K"
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return str(n) if 1 <= n <= 12 else None

# Subjects we recognize (controlled vocabulary — MultiWOZ-style ontology discipline, no free text).
_SUBJECT_PATTERNS = [
    (re.compile(r"\bSAT\s*math\b", re.I), "SAT math", 0.95),
    (re.compile(r"\bACT\s*math\b", re.I), "ACT math", 0.95),
    (re.compile(r"\bSAT\b", re.I), "SAT", 0.85),
    (re.compile(r"\bACT\b", re.I), "ACT", 0.85),
    (re.compile(r"\b(algebra|geometry|calculus|trigonometry|pre-?calculus)\b", re.I), None, 0.9),
    (re.compile(r"\b(math|reading|writing|english|science|chemistry|physics|biology)\b", re.I), None, 0.85),
]

# Budget: a dollar figure, optionally with a cadence. We capture the numeric amount.
# Two tiers: a $-/cadence-ANCHORED match (unambiguous money) is preferred over a bare number that
# merely appears alongside budget-context words — so "$300 a month" wins over the "11" in
# "11th grade" within the same sentence.
_BUDGET_ANCHORED_RE = re.compile(
    r"(?:\$\s*(\d{1,3}(?:,\d{3})*|\d+)"  # $300
    r"|(\d{1,3}(?:,\d{3})*|\d+)\s*(?:dollars?|bucks?)"  # 300 dollars
    r"|(\d{1,3}(?:,\d{3})*|\d+)\s*(?:/|per|a|each)\s*(?:month|week|hour|session))",  # 300 a month
    re.I,
)
_BUDGET_BARE_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})*|\d{2,})\b")
_BUDGET_CONTEXT_RE = re.compile(r"\b(budget|afford|spend|pay|cost|price)\b|\$", re.I)

# Timeline: a test/term date or a relative window.
# CB-61: expanded to capture "in about N weeks/months" (was only bare "in N weeks") — the QA6 miss
# was "big test in about three weeks" which the original regex silently dropped.  A hedge word
# ("about", "roughly", "approximately", "around") before the count drops confidence from 0.85 to
# 0.7 so the slot stays SOFT and can be confirmed, but it is at least captured (not dropped silently).
# CB-76 (b): extended to cover "end of the month", "end of month", "by the end of the month",
# "next month", "this semester", "before finals" — all common deadline forms that were silently dropped.
_TIMELINE_RE = re.compile(
    r"\b(?:by\s+)?("
    r"end\s+of\s+(?:the\s+)?month|"
    r"end\s+of\s+(?:the\s+)?(?:week|semester|term|year|quarter)|"
    r"this\s+(?:semester|term|month|week|year|quarter)|"
    r"before\s+(?:finals?|midterms?|exams?|testing|the\s+test|the\s+exam)|"
    r"next\s+(?:week|month|year|spring|fall|summer|semester|term)|"
    r"in\s+(?:about|roughly|approximately|around|just\s+(?:over|under))?\s*\d+\s+(?:weeks?|months?)|"
    r"in\s+(?:about|roughly|approximately|around)?\s*"
    r"(?:a\s+(?:couple(?:\s+of)?|few)|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:weeks?|months?)|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"|spring|fall|summer|finals|midterms"
    r")\b",
    re.I,
)
# The hedge words that should soften a timeline confidence below lock level.
_TIMELINE_HEDGE_RE = re.compile(
    r"\b(?:about|roughly|approximately|around|maybe|probably|i\s+think|not\s+sure|"
    r"somewhere|give\s+or\s+take|or\s+so|ish)\b",
    re.I,
)

# CB-61: Callback window — the prospect proposing a day/time for the callback ("Thursday after 3pm",
# "Monday morning works for me"). Captured as callback_window so skip_known/no_repeat_discovery can
# protect it from re-asking, and so NLG can echo it on a confirm-request turn.
# Tight: requires a named day or a time expression + a scheduling cue to avoid false positives.
# CB-76 (a): _DAY_NAMES now also matches plural day names (Wednesdays, Fridays) and DISJUNCTIVE
# multi-day offers ("Wednesdays or Fridays after 4"). The full disjunction is captured via
# _CALLBACK_DISJUNCTION_RE and preserved in the stored value; plural→singular is normalized.
_DAY_NAMES = (
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun)"
)
# CB-76 (a): plural day name, e.g. "Wednesdays", "Fridays". Normalised to singular on storage.
_DAY_NAMES_PLURAL = (
    r"(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays)"
)
# CB-76 (a): a single day name OR a plural day name (covers Wednesdays, Fridays, etc.)
_DAY_NAMES_ANY = (
    r"(?:mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|"
    r"mon|tue|wed|thu|fri|sat|sun)"
)
_TIME_OF_DAY = (
    r"(?:"
    r"(?:after|before|around|at|by)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)|"
    r"(?:morning|afternoon|evening|night|noon)"
    r")"
)
# CB-76 (a): a DISJUNCTIVE multi-day availability offer —
# "Wednesdays or Fridays after 4", "Tuesday or Thursday mornings".
# Captures the whole phrase so the stored value is the complete availability statement.
_CALLBACK_DISJUNCTION_RE = re.compile(
    r"\b("
    + _DAY_NAMES_ANY
    + r"(?:\s*(?:morning|afternoon|evening|night|noon))?"
    + r"(?:\s+(?:after|before|around|at|by)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?"
    + r"\s+or\s+"
    + _DAY_NAMES_ANY
    + r"(?:\s*(?:morning|afternoon|evening|night|noon))?"
    + r"(?:\s+(?:after|before|around|at|by)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?)"
    + r"(?:\s+(?:after|before|around|at|by)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?"
    r"\b",
    re.I,
)
# A day name (with optional time) OR a bare "after/before <time>" with no day (lower confidence).
_CALLBACK_WINDOW_RE = re.compile(
    # CB-76 (a): plural day name match (e.g. "Thursdays", "Wednesdays") — normalised on storage.
    r"\b(" + _DAY_NAMES_PLURAL + r"(?:\s*(?:morning|afternoon|evening|night|noon))?"
    r"(?:\s+(?:after|before|around|at)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?)\b"
    # Full singular: a named day, optionally followed by time qualifiers.
    r"|\b(" + _DAY_NAMES + r"(?:\s*(?:morning|afternoon|evening|night|noon))?"
    r"(?:\s+(?:after|before|around|at)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))?)\b"
    r"|\b(" + _TIME_OF_DAY + r")\b",
    re.I,
)
# A scheduling-intent cue required when there is NO named day — prevents capturing "after 3pm"
# in a completely unrelated context.
_SCHEDULING_CUES_RE = re.compile(
    r"\b(work[s]?(?:\s+for\s+(?:me|us))?|call|reach|callback|schedule|contact|"
    r"reach\s+(?:me|us)|call\s+(?:me|us)|available|confirm|please\s+confirm)\b",
    re.I,
)
# CB-61 adversarial fix (Defect 1): deadline-context words that, when found near a day name,
# indicate an EXAM/TEST DEADLINE rather than a scheduling offer. "her test is on Thursday" and
# "the exam is Thursday morning" must NOT become callback_window. Named-day branch requires a
# scheduling-intent cue OR the absence of deadline context; deadline context wins (blocks the
# named-day branch even when a scheduling cue is present in the same utterance).
# CB-76 (a): added "tutoring" to the context block — "she has tutoring Thursdays" is an EXISTING
# commitment, not a callback offer, so "Thursdays" there must not extract as callback_window.
# CB-76 round-2 (defense in depth): added PAST-TENSE activity words — "tutored"/"tried"/"used to"/
# "back in" mark a past habit or failed attempt, not a forward availability offer. "I tutored on
# Mondays back in college" and "we tried Wednesdays before" must NOT extract a callback window.
# (The plural-day scheduling-cue gate in _extract_callback_window is the primary guard; these
# words add a second, independent block so a future plural form with a stray cue still can't slip.)
_DEADLINE_CONTEXT_RE = re.compile(
    r"\b(test|exam|quiz|due|deadline|assignment|homework|midterm|finals?|"
    r"presentation|project\s+due|appointment\s+for|school|class(?:es)?|"
    r"tutor(?:ing|ed|s)?|lesson[s]?|practice[ds]?|"
    r"tried|used\s+to|back\s+in)\b",
    re.I,
)
# CB-61 adversarial fix (Defect 4): hedge words that soften a callback_window extraction below the
# lock threshold (0.8), mirroring the timeline hedge discipline. "maybe Thursday could work?" is
# not a firm commitment and must remain re-confirmable.
_CALLBACK_HEDGE_RE = re.compile(
    r"\b(?:maybe|perhaps|possibly|might|could\s+work|not\s+sure|i\s+think|"
    r"i\s+guess|probably|sort\s+of|kind\s+of|roughly|around|or\s+so|ish)\b",
    re.I,
)
# CB-90 (b) round-2: NEGATION / UNAVAILABILITY cue. A day disjunction stating when the caller is
# NOT free ("I'm NOT available Wednesdays or Fridays", "I can't do Wednesdays or Fridays",
# "Wednesdays or Fridays are tough") must NOT be captured as a callback window — acking those days
# as the caller's preferred slot would misrepresent stated UNAVAILABILITY as availability. Checked
# in a bounded window immediately around the day pair in the disjunction branch.
_AVAILABILITY_NEGATION_RE = re.compile(
    r"\b(?:not|never|cannot|can'?t|won'?t|don'?t|do\s+not|isn'?t|aren'?t|unable|unavailable|"
    r"tough|bad|busy|hard|difficult|avoid|except|no\s+good|terrible|rough)\b",
    re.I,
)

# CB-61: Confirm-request detection — the prospect explicitly asking the AGENT to confirm a fact
# (typically the callback time) they already stated. "please confirm they'll call Thursday after 3pm",
# "can you confirm <X>?". Deliberately tight: requires an explicit confirm/verify verb directed AT
# the agent, so a general question is NOT mislabeled as a confirm_request.
# CB-61 adversarial fix (Defect 2): "confirm/check WITH my wife/husband/partner" is a
# DECISION_MAKER DEFERRAL (the person is consulting a third party), NOT an agent-directed confirm.
# The verb "confirm/check with <person>" means "consult", not "please verify this for me".
# We exclude forms where the confirm/double-check/verify verb is immediately followed by "with my/
# our/his/her" (a third-party consultation phrase). Only agent-directed forms survive.
_CONFIRM_REQUEST_RE = re.compile(
    # Agent-directed forms: "please confirm", "can you confirm", "just to confirm",
    # "just confirm", "verify", "make sure" — but NOT followed by "with <person>".
    r"(?:"
    r"(?:please\s+|can\s+you\s+|could\s+you\s+|just\s+to\s+|just\s+)"
    r"(?:confirm|verify|double.?check|make\s+sure)"
    r")"
    r"(?!\s+with\s+(?:my|our|his|her|the|a)\b)",
    re.I,
)


def _extract_grade(text: str, *, asked_grade: bool = False) -> Optional[tuple[Any, float]]:
    """Extract a grade level. EXPLICIT forms — "Nth grade"/"grade N", grade words (kindergarten…), and a
    spelled ordinal + "grade" ("third grade") — are always trusted. BARE forms with no "grade" token —
    a spelled ordinal ("Third"), a numeric ordinal ("3rd"), or a standalone number ("8") — are trusted
    ONLY when the agent JUST asked for the grade (`asked_grade`); otherwise a stray ordinal/number in a
    non-answer context ("1st time calling", "the 3rd of June", "my 2nd child", an age "she is 7") would
    masquerade as and LOCK a grade. Out-of-range numbers (e.g. an age "16") and explicit age phrases are
    rejected (CB-35 + follow-up). CB-50 (secondary): a HEDGED / disjunctive bare answer ("7th or 8th,
    not sure") is extracted at REDUCED confidence (below the lock threshold) so an uncertain grade stays
    a SOFT, re-confirmable slot instead of being asserted back as fact."""
    for pat, conf in _GRADE_PATTERNS:
        m = pat.search(text)
        if m:
            g = _coerce_grade(m.group(1))
            if g is not None:
                return g, conf
    low = text.lower()
    for word, (val, conf) in _GRADE_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            return val, conf
    # A SPELLED ordinal followed by "grade" ("third grade", "fifth grade") is explicit — always trusted.
    swg = _GRADE_SPELLED_WITH_GRADE_RE.search(text)
    if swg:
        g = _coerce_grade(_GRADE_ORDINAL_WORDS[swg.group(1).lower()])
        if g is not None:
            return g, 0.9
    # Everything below is a BARE ordinal/spelled-word/number with NO "grade" token. These are only
    # trustworthy as the DIRECT answer to a just-asked grade question (asked_grade); otherwise a stray
    # ordinal/number — "1st time calling", "the 3rd of June", "my 2nd child", an age "she is 7" — would
    # masquerade as (and worse, LOCK) a grade. So when the grade was NOT just asked, extract nothing.
    if not asked_grade:
        return None
    # CB-50 (secondary): a HEDGED / disjunctive bare answer ("7th or 8th, not sure") must NOT lock a
    # confirmed grade. We cap a bare extraction's confidence below the lock threshold so the slot stays
    # soft (re-confirmable) instead of being asserted back as fact ("Got it — eighth-grade math").
    hedged = _is_hedged_grade(text)

    def _conf(c: float) -> float:
        return min(c, _HEDGED_GRADE_CONFIDENCE) if hedged else c

    # CB-35: a bare spelled ordinal ("Third", "fifth") answering "what grade?".
    sm = _GRADE_SPELLED_WORD_RE.search(text)
    if sm:
        g = _coerce_grade(_GRADE_ORDINAL_WORDS[sm.group(1).lower()])
        if g is not None:
            return g, _conf(0.85)
    # A bare numeric ordinal ("3rd", "8th") without the word "grade".
    om = _GRADE_BARE_ORDINAL_RE.search(text)
    if om:
        g = _coerce_grade(om.group(1))
        if g is not None:
            return g, _conf(0.8)
    # A bare standalone number ("8") — but NOT a number that is plainly an age ("she is 7 years old").
    if not _AGE_CONTEXT_RE.search(text):
        nm = _GRADE_BARE_NUMBER_RE.search(text)
        if nm:
            g = _coerce_grade(nm.group(1))
            if g is not None:
                return g, _conf(0.75)
    return None


def _extract_subject(text: str) -> Optional[tuple[Any, float]]:
    for pat, label, conf in _SUBJECT_PATTERNS:
        m = pat.search(text)
        if m:
            value = label if label is not None else m.group(1).lower()
            return value, conf
    return None


def _extract_budget(text: str) -> Optional[tuple[Any, float]]:
    # Prefer a $-/cadence-anchored amount (unambiguous money). This is what keeps "$300 a month"
    # from losing to the "11" in "11th grade" elsewhere in the same sentence.
    anchored = _BUDGET_ANCHORED_RE.search(text)
    if anchored:
        raw = next(g for g in anchored.groups() if g is not None).replace(",", "")
        try:
            return int(raw), 0.95
        except ValueError:
            return None
    # Fall back to a bare number ONLY when budget-context words are present, at lower confidence,
    # so it yields to any firmer extraction later.
    if not _BUDGET_CONTEXT_RE.search(text):
        return None
    bare = _BUDGET_BARE_RE.search(text)
    if not bare:
        return None
    try:
        return int(bare.group(1).replace(",", "")), 0.7
    except ValueError:
        return None


def _extract_timeline(text: str) -> Optional[tuple[Any, float]]:
    """Extract a deadline or relative test-prep timeline (CB-61 widened).

    A hedged expression ("in about three weeks") extracts at 0.7 — below the lock threshold — so
    the slot is soft and can be re-confirmed, but is at least captured (the QA6 miss: the original
    regex silently dropped "in about three weeks" and then re-asked the same question)."""
    m = _TIMELINE_RE.search(text)
    if m:
        value = m.group(1).lower()
        # Apply hedge softening: if the utterance contains uncertainty words, lower confidence so
        # the slot stays soft (re-confirmable) instead of locking an uncertain timeline.
        conf = 0.7 if _TIMELINE_HEDGE_RE.search(text) else 0.85
        return value, conf
    return None


def _normalize_plural_day(text: str) -> str:
    """Normalise plural day names to their singular form for slot storage.

    Strips the trailing 's' only on exact plural day-name tokens so "Wednesdays" → "wednesday"
    and "Fridays" → "friday", while not touching arbitrary trailing 's' on other words."""
    _PLURAL_TO_SINGULAR = {
        "mondays": "monday", "tuesdays": "tuesday", "wednesdays": "wednesday",
        "thursdays": "thursday", "fridays": "friday", "saturdays": "saturday",
        "sundays": "sunday",
    }
    words = text.lower().split()
    return " ".join(_PLURAL_TO_SINGULAR.get(w, w) for w in words)


def _extract_callback_window(text: str) -> Optional[tuple[Any, float]]:
    """Extract a proposed callback day/time into the callback_window slot (CB-61 new slot).

    High confidence (0.9) when a named day is present — the caller is committing a specific slot.
    Lower confidence (0.75) for a bare time-of-day with no day name but a scheduling cue present.
    Returns None when neither pattern matches or scheduling cues are absent for a bare time match.

    CB-61 adversarial fix (Defect 1): the named-day branch now requires a scheduling-intent cue
    OR the absence of deadline-context words. A deadline word (test/exam/quiz/due/deadline) in the
    same utterance marks the day as an academic deadline, not a proposed callback slot — e.g.
    "the exam is Thursday morning" and "she has a quiz on Friday morning" must NOT extract.

    CB-61 adversarial fix (Defect 4): hedge words ("maybe Thursday could work?", "perhaps Friday")
    lower the confidence to 0.7 — below the 0.8 lock threshold — so a tentative window stays SOFT
    and can be re-confirmed, mirroring the timeline hedge discipline.

    CB-76 (a): the DISJUNCTION branch fires FIRST — "Wednesdays or Fridays after 4" is captured as
    the whole phrase at confidence 0.9 (a firmly volunteered availability statement). Plural day
    names are normalised to singular before storage. The deadline/context guard still blocks forms
    like "she has tutoring Thursdays" (tutoring added to _DEADLINE_CONTEXT_RE).

    CB-76 round-2 (BLOCKING fix — plural-day over-trigger): a BARE plural day with NO scheduling
    intent is venting / a past habit, NOT a forward availability offer ("Fridays are always crazy
    for us", "we tried Wednesdays before", "I tutored on Mondays back in college"). The plural-day
    branch therefore now ALSO requires a _SCHEDULING_CUES_RE hit (work(s) for me / call / available
    / ...), mirroring the bare-time-of-day branch's existing cue gate. The SINGULAR-day branch keeps
    its CB-61 behaviour (a singular "Thursday after 3pm" is specific enough on its own), and the
    DISJUNCTION branch keeps firing without a cue word ("Wednesdays or Fridays after 4") because the
    two-day disjunction is itself the availability signal."""
    text_norm = _normalize_punct(text or "")

    # CB-76 (a): check for a disjunctive multi-day offer FIRST — it is the most specific shape and
    # must win over the single-day branch that would only capture the first day of the pair. The
    # two-day disjunction is itself the scheduling signal, so it needs no extra cue word.
    disj = _CALLBACK_DISJUNCTION_RE.search(text_norm)
    if disj:
        # CB-90 (b) round-2: NEGATION / UNAVAILABILITY guard — "I'm NOT available Wednesdays or
        # Fridays" / "I can't do Wednesdays or Fridays" / "Wednesdays or Fridays are tough" state
        # when the caller is UNAVAILABLE, not a preferred window. Acking those days as availability
        # would misrepresent the caller. Look in a bounded window immediately BEFORE (~30 chars,
        # catches "not available <days>") and AFTER (~20 chars, catches "<days> are tough") the day
        # pair for a negation/unavailability cue; if present, do NOT capture.
        _before = text_norm[: disj.start()][-30:]
        _after = text_norm[disj.end() :][:20]
        if _AVAILABILITY_NEGATION_RE.search(_before) or _AVAILABILITY_NEGATION_RE.search(_after):
            return None
        # Deadline/context guard: "she has tutoring Thursdays or Fridays" is an existing schedule,
        # not an availability offer. Block on deadline context the same as the single-day branch.
        if not _DEADLINE_CONTEXT_RE.search(text_norm):
            value = _normalize_plural_day(disj.group(1).strip())
            conf = 0.7 if _CALLBACK_HEDGE_RE.search(text_norm) else 0.9
            return value, conf

    m = _CALLBACK_WINDOW_RE.search(text_norm)
    if not m:
        return None
    plural_day_match = m.group(1)   # plural day name branch (CB-76a)
    day_match = m.group(2)          # singular named-day branch
    time_match = m.group(3)         # bare time-of-day branch
    if plural_day_match or day_match:
        matched = plural_day_match or day_match
        # CB-61 adversarial (Defect 1) + CB-76 (a): if deadline-context words are present, the
        # day is an academic/existing schedule, NOT a proposed callback window — return None.
        if _DEADLINE_CONTEXT_RE.search(text_norm):
            return None
        # CB-76 round-2 (BLOCKING fix): a bare PLURAL day must carry scheduling intent to count as
        # an offer. Without a _SCHEDULING_CUES_RE hit it's venting / a past habit, not availability
        # ("Fridays are always crazy", "we tried Wednesdays before") — do NOT fabricate a window.
        # The singular branch is exempt (CB-61: a single specific day is offer enough).
        if plural_day_match and not _SCHEDULING_CUES_RE.search(text_norm):
            return None
        # CB-61 adversarial (Defect 4): hedge softening — a tentative window must stay below lock.
        conf = 0.7 if _CALLBACK_HEDGE_RE.search(text_norm) else 0.9
        value = _normalize_plural_day(matched.strip())
        return value, conf
    # Bare time-of-day: only capture when a scheduling cue is nearby (avoids false positives).
    if time_match and _SCHEDULING_CUES_RE.search(text_norm):
        conf = 0.7 if _CALLBACK_HEDGE_RE.search(text_norm) else 0.75
        return time_match.lower().strip(), conf
    return None


# Slot name -> extractor. Adding a slot is a one-line addition here.
_EXTRACTORS = {
    "grade_level": _extract_grade,
    "subject": _extract_subject,
    "budget": _extract_budget,
    "timeline": _extract_timeline,
    "callback_window": _extract_callback_window,  # CB-61: proposed callback day/time
}


def _extract_slots(belief: BeliefState, utterance: str, last_agent_act: Optional[str] = None) -> None:
    """Run each extractor and merge with confidence arbitration (mutates `belief` in place).

    Merge rule (near-monotonic accumulation): a new extraction replaces an existing slot only if
    the slot is not yet locked (existing confidence < _LOCK_CONFIDENCE) OR the new confidence is
    strictly higher than the locked one. This is what stops a garbled/contradictory turn from
    corrupting a high-confidence slot.

    CB-35: the grade extractor is told whether the agent JUST asked for the grade (`last_agent_act`
    'ask'/'confirm_known' with the grade as the pending question), so a bare standalone number ("8")
    is accepted as the grade answer only in that context.
    """
    asked_grade = last_agent_act in ("ask", "confirm_known") and (
        belief.meta.get("pending_ask_slot") == "grade_level"
    )
    for name, extractor in _EXTRACTORS.items():
        if name == "grade_level":
            result = extractor(utterance, asked_grade=asked_grade)
        else:
            result = extractor(utterance)
        if result is None:
            continue
        value, new_conf = result
        existing = belief.slots.get(name)
        if existing is None:
            belief.set_slot(name, value, new_conf)
            continue
        existing_conf = float(existing.get("confidence", 0.0))
        if existing_conf >= _LOCK_CONFIDENCE and new_conf <= existing_conf:
            # Locked and the new evidence is not stronger — keep the established value.
            continue
        if existing.get("value") == value:
            # Same value re-observed: take the higher confidence (reinforcement, never lower it).
            belief.set_slot(name, value, max(existing_conf, new_conf))
        elif new_conf > existing_conf:
            # Different value but stronger evidence — allowed to overwrite.
            belief.set_slot(name, value, new_conf)
        # else: weaker conflicting evidence — ignored.


# --- Deterministic intent / objection classification -----------------------------------------
# Why deterministic (no LLM): the policy is BLIND without these signals — it loops discovery and
# ignores direct questions/objections (a real prospect who asks "how much?" or "why not Khan
# Academy?" got re-asked "what grade?"). A keyword classifier is zero-latency, testable, and
# parity-clean (text + voice get the same signals). The objection taxonomy mirrors the config
# rebuttals + the kb_chunk `objections#*` corpus the agent grounds on.

# CB-90 (a) round-2 — a GUARANTEE-of-OUTCOME demand: the caller wants a PROMISED grade/result/refund
# ("guarantee me a B", "guarantee a pass", "guarantee results", "guarantee a better grade",
# "guarantee me a B or it's free"). This is the SINGLE source of truth for "is this a guarantee/
# efficacy DEMAND?" — pattern[0] of _OBJECTION_PATTERNS (the regex-classify path) AND the scoped
# live-path objection upgrade in update() both use it, so the two paths agree.
# TIGHT (round-2 fix for the verifier's false positives):
#   - a single LETTER grade is ONLY accepted when preceded by the article "a/an" ("a B", "an A") so
#     the standalone article "a" can never be mis-read AS the grade — this is what made the round-1
#     pattern fire on "guarantee a tutor"/"guarantee a slot" (the optional "(a\s+)?" group was
#     skipped and "[abcdf]" then matched the article "a"). The grade letter must also not be followed
#     by another letter ((?![a-z])) so "a Algebra"/"a session" don't match.
#   - otherwise an explicit RESULT word (grade/pass/score/results/improvement/money-back/it-works…)
#     is required after 'guarantee' — "guarantee a tutor"/"guarantee a time" carry NO result word, so
#     they do NOT match (scheduling/staffing, not an outcome demand).
#   - the "...or it's free" consequence shape is also an outcome-guarantee demand.
# Genuine: "Guarantee me a B or it's free" → efficacy_doubt. Rejected: "guarantee a tutor will be
# free Thursday" (→ falls through to diy_free on the bare 'free', harmless on the regex fallback),
# "I just want to guarantee we're on the same page" (→ no match).
_GUARANTEE_RESULT_WORDS = (
    r"grades?|letter\s+grade|pass(?:ing)?|scores?|results?|improvement|better\s+grade|"
    r"(?:my\s+)?money\s+back|it'?ll\s+work|it\s+will\s+work|it\s+works|(?:he|she|they)'?ll\s+pass"
)
_GUARANTEE_DEMAND_RE = re.compile(
    r"\bguarantee\s+(?:me\s+|us\s+)?(?:"
    r"an?\s+[abcdf][-+]?(?![a-z])"                          # article + grade letter: "a B", "an A", "a B+"
    r"|(?:an?\s+)?(?:" + _GUARANTEE_RESULT_WORDS + r")\b"   # an explicit result word
    r")"
    r"|\bguarantee\b.{0,30}?\bor\s+it'?s?\s+free\b",        # "...or it's free" consequence shape
    re.I,
)

# Objection cues -> the canonical objection key (priority order: first match wins). Word-boundaried.
# CB-90 (a): the GUARANTEE-demand pattern is FIRST so "Guarantee me a B or it's free" routes to
# efficacy_doubt (an outcome demand), NOT diy_free (which would fire on bare 'free' before this).
# CB-90 (a) round-2: the bare standalone 'guarantee' was REMOVED from the lower efficacy_doubt row
# (was pattern[3]) — it over-fired on meta-communication uses ("I just want to guarantee we're on
# the same page") and the live-path upgrade amplified that onto every weak-LLM-intent turn. Genuine
# guarantee DEMANDS are now caught by _GUARANTEE_DEMAND_RE (pattern[0]); the LLM-proposed
# efficacy_doubt grounding (_OBJECTION_GROUNDING) still keeps 'guarantee' for the LLM path.
_OBJECTION_PATTERNS: list[tuple[Any, str]] = [
    (_GUARANTEE_DEMAND_RE, "efficacy_doubt"),
    (re.compile(r"\b(khan|youtube|free|do it myself|on my own|google it|self[- ]?study)\b", re.I), "diy_free"),
    (re.compile(r"\b(my (husband|wife|spouse|partner)|other parent|check with|talk to my|discuss (it )?with|run it by)\b", re.I), "decision_maker"),
    (re.compile(r"\b(will it (really )?(work|help)|does it (really )?(work|help)|worth it|actually help|skeptic)\b", re.I), "efficacy_doubt"),
    (re.compile(r"\b(right time|too early|too late|not sure (if )?now|maybe later|wait (a|until)|down the road)\b", re.I), "timing"),
    (re.compile(r"\b(expensive|too much|can'?t afford|pricey|out of (our|my) budget|cost(s|ly)? too)\b", re.I), "price"),
]
# A FIRM affordability refusal — "we genuinely can't / don't have the budget" — distinct from a
# soft "that's pricey" (which is handleable). The agent reframes value on the first couple; once the
# CUMULATIVE count crosses the gate threshold the prospect is genuinely unqualified and the agent
# should gracefully RELEASE (disqualify) rather than loop acknowledgments. Deliberately BROAD (an LLM
# prospect rephrases the refusal every turn), and COUNTED CUMULATIVELY (not as a consecutive streak)
# so the count still climbs across a refusal loop even when a turn's phrasing slips past the regex.
_AFFORDABILITY_REFUSAL_RE = re.compile(
    r"(can'?t afford|cannot afford|no budget|don'?t have (the |a )?budget|do not have (the |a )?budget|"
    r"don'?t think (i|we) (can|have) (the )?budget|out of (our|my) budget|not in (a |our |my )?(position|place) to afford|"
    r"can'?t swing|can'?t make this work|can'?t justify|can'?t fit (this|it) (in|into)|"
    r"tight on (money|budget|cash)|just can'?t (do|swing|afford|justify) (this|it)|"
    r"too expensive for (us|me)|just don'?t have (the |a |it )?(budget|money|funds))",
    re.IGNORECASE,
)
# A bare price/cost question (not necessarily an objection) -> opens price talk via the gate.
_PRICE_INQUIRY_RE = re.compile(r"\b(how much|what(?:'s| is| does it) cost|price|pricing|per (month|hour|session)|monthly|fees?|rates?)\b", re.I)
# CB-85 (a): off-topic social / small-talk cues — weather, sports, news, weekend, general chit-chat.
# A question matching these AND NOT containing any product/scheduling keyword is a social aside that
# must NEVER be mistaken for scheduling consent or a product question.
_SOCIAL_ASIDE_CUES_RE = re.compile(
    r"\b(?:"
    r"storm|weather|rain|snow|hurricane|tornado|flood|temperature|forecast|"
    r"game|team|score|sports?|football|basketball|baseball|soccer|playoff|"
    r"news|election|politics?|weekend|holiday|vacation|traffic|commute|"
    r"movie|show|dinner|restaurant|party|kids?\s+school\b|"
    r"how\s+(?:are\s+you|is\s+(?:your|the)\s+day|have\s+you\s+been)|"
    r"did\s+you\s+(?:hear|see|watch|catch|follow)"
    r")\b",
    re.I,
)
# Anchors that confirm a social aside is actually a product/scheduling/transaction question — if ANY
# of these are in the utterance too, we do NOT classify it as social_aside (the question is product-
# related). MUST cover scheduling/policy/transaction terms: a "weather POLICY if we need to
# RESCHEDULE?" question carries a social cue ("weather") but is a real product question — the
# reschedule/policy keyword exempts it. Prefixes are written to catch their inflections (reschedul →
# reschedule/rescheduling; appoint → appointment; book → booking; cancel → cancellation) since the
# trailing \b otherwise blocks "appoint" from matching "appointment".
_PRODUCT_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"reschedul(?:e|ing)|schedul(?:e|ing)|cancel(?:lation|led|ing)?|polic(?:y|ies)|refund|"
    r"appoint(?:ment)?|book(?:ing)?|callback|call back|enroll|sign\s*up|start|"
    r"tutor(?:ing)?|session|consult|trial|lesson|subject|grade|slot|"
    r"cost|price|pricing|fee|pay(?:ment)?|monthly|hour(?:ly)?|"
    r"available|availabilit|meet(?:ing)?"
    r")\b",
    re.I,
)
# An explicit ask for a human — routes to escalate via the escalation gate. Two shapes: (1) a
# "speak/talk/connect me to/with a <human>" verb phrase, and (2) a "want/need/get me/give me/put me
# on with a <human>" desire phrase ("I want a human now", "get me a real person", "let me talk to
# someone"). Tight to an explicit human-noun (human/person/rep/agent/advisor/someone) so it never
# captures the decision_maker objection ("run it by my spouse") the rubric keeps distinct.
_HUMAN_NOUN = r"(?:human|person|people|representative|rep|agent|advisor|someone|somebody)"
_HUMAN_REQUEST_RE = re.compile(
    r"\b(?:speak|talk|connect me|put me through|transfer me|let me (?:speak|talk))\s+"
    r"(?:to|with|on)?\s*(?:a |an |the )?(?:real |actual |live )?" + _HUMAN_NOUN + r"\b"
    r"|\b(?:want|need|get me|give me|wanna (?:speak|talk))\s+(?:to\s+)?(?:speak\s+(?:to|with)\s+)?"
    r"(?:a |an |the )?(?:real |actual |live )?" + _HUMAN_NOUN + r"\b"
    r"|\breal (?:person|human)\b",
    re.I,
)
# A question at all: a trailing '?' or a leading interrogative/request-to-explain.
_QUESTION_RE = re.compile(r"\?|\b(what|how|when|where|why|who|which|can you|could you|do you|are there|is there|tell me about)\b", re.I)

# CB-76 (c): NEVER-DENY RULE — the caller asking whether they already told us something. The agent
# must NEVER assert the caller did NOT say something in response to this intent. Detection shape:
# "did I already tell you [X]?" / "have I told you [X]?" / "did I mention [X]?" / "do you have [X]?"
# broad enough to catch paraphrases; the NLG instruction is the enforcement layer.
# CB-76 round-2 (NON-BLOCKING tightening): the "(did|have) I tell/mention/say..." form now requires
# a RECALL REFERENT within ~40 chars after the verb — an "already" marker, a scheduling/time word
# (when / what time), or a slot referent ("my number/availability/budget/..."). This keeps the
# genuine recall questions ("did I already tell you when works for me?", "have I mentioned what time
# works?") while NO LONGER firing on a rhetorical SHARE ("did I tell you my son hates math?") that
# carries no recall referent — so the never-deny note doesn't inject on a pure rhetorical share.
_RECALL_REFERENT = (
    r"(?:already|when|what\s+time|what\s+times?|time\s+works?|"
    r"my\s+(?:number|phone|email|availabilit\w*|schedule|budget|grade|name|time|preference)|"
    r"availabilit\w*|good\s+(?:time|for))"
)
_MEMORY_CHECK_RE = re.compile(
    r"\b(?:"
    r"(?:did|have)\s+i\s+(?:already\s+)?(?:tell|told|mention|mentioned|say|said|give|gave|share|shared|"
    r"let\s+you\s+know)\b(?=.{0,40}?" + _RECALL_REFERENT + r")|"
    r"did\s+you\s+(?:get|catch|hear|note|write\s+down)\b.{0,30}?\bfrom\s+me\b|"
    r"do\s+you\s+(?:have|know|remember)\b.{0,30}?\b(?:already|from\s+(?:me|what\s+i\s+said))\b|"
    r"you\s+(?:already\s+)?(?:have|know|got)\b.{0,25}?\b(?:that|it|this|my\s+\w+)\b|"
    r"i\s+(?:already|just|previously)\s+(?:told|mentioned|said|gave|shared)\b"
    r")",
    re.I,
)
# Contact fields that, when captured, should trigger a one-line acknowledgment on the next reply.
# These are the raw-text cues for whether a turn delivers contact info. The actual extraction of
# name/phone/email lives in the demo/voice flow (not DST), so we detect here via simple heuristics
# that fire on the same turns without re-implementing full contact extraction.
_CONTACT_PHONE_RE = re.compile(
    r"\b(?:\d{3}[\s\-\.]\d{3}[\s\-\.]\d{4}|\d{10}|\(\d{3}\)\s*\d{3}[\s\-\.]\d{4})\b"
)
_CONTACT_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")
# CB-76 round-3 (CRITICAL live-QA regression): words that look like a name after an intro verb but
# are NOT names — kept as a stopword negative-lookahead for defense in depth even though the capture
# group is now case-sensitive. The regression: "Guarantee me a B or it's free. Yes or no." captured
# name="free" (re.I relaxed the [A-Z] guard) and the agent replied "Got it, Free — I've noted that
# down.", swallowing a guarantee objection at the highest-stakes moment. Broad on hedges / feelings /
# fillers that an LLM might capitalize at a sentence start.
_NAME_STOPWORDS = (
    r"free|fine|worried|looking|asking|calling|trying|sorry|okay|ok|yes|no|sure|maybe|"
    r"just|here|there|not|good|great|interested|ready|done|confused|frustrated|busy|"
    r"available|hoping|wondering|thinking|afraid|glad|happy|curious|concerned|unsure|"
    r"urgent|nope|yeah|nah|hi|hey|hello|the|a|an|so|well|still|already|"
    r"gonna|going|guarantee|cost|price"
)
# A name introduction — EXPLICIT intro forms ONLY: "my name is X", "I'm X", "this is X",
# "call me X", "name's X". The "it's <X>" form is DROPPED entirely (CB-76 round-3): "it's
# free/fine/urgent/no problem" is essentially never a name and was the source of the regression.
# The captured name group is CASE-SENSITIVE — `(?-i:([A-Z][a-z]+))` — even though the surrounding
# intro prefix matches case-insensitively (re.I). This means "Dana"/"Karen" match but "free",
# "worried", "looking" (lowercase) do NOT, killing the over-match at the regex level. The stopword
# lookahead is a second guard (rejects a capitalized "Free"/"Worried" too).
_CONTACT_NAME_RE = re.compile(
    r"\b(?:my\s+name\s+is|i'?m|this\s+is|call\s+me|name'?s)\s+"
    r"(?!(?:" + _NAME_STOPWORDS + r")\b)"
    r"(?-i:([A-Z][a-z]+))",
    re.I,
)

# CB-37: profanity / abuse / hostility markers. A hostile or abusive turn (or extreme bail) must
# DE-ESCALATE — never pitch or re-ask. Deliberately broad on directed profanity + frustration markers;
# the agent reacts to TONE, not just literal slurs. Used to set belief.meta["hostility"] = True.
_HOSTILITY_RE = re.compile(
    r"\b(f+u+c+k|f\*+k|fuck(ing|er|ed)?|shit|bullshit|asshole|bastard|"
    r"piss off|screw (you|this|off)|go to hell|shut up|"
    r"stop (calling|bothering|harass)|leave me alone|stop wasting my time|"
    r"you('| a)?re (an? )?(idiot|moron|stupid|useless|scam|fraud|liar|jerk)|"
    r"scammer|i'?m done|wasting my time|sick of (this|you)|"
    r"how dare you|don'?t (you )?(ever )?call)\b",
    re.I,
)

# CB-34: the prospect EXPLICITLY denies having a learner / corrects a presumed learner. When this
# fires, the agent presumed a learner that does not (yet) exist — drop learner-specific discovery and
# go back to establishing WHO the tutoring is for. Sets belief.meta["learner_denied"] = True.
_LEARNER_DENIAL_RE = re.compile(
    r"\b(i (did ?n'?t|did not|never) sa(id|y) .{0,30}?(learner|student|kid|child|son|daughter)|"
    r"i (don'?t|do not) have (a |an )?(learner|student|kid|child|son|daughter|one)|"
    r"there('?s| is| was) no (learner|student|kid|child)|"
    r"no (learner|student|kid|child|one) (here|involved)|"
    r"who said (anything about )?(a |an )?(learner|student|kid|child))\b",
    re.I,
)
# CB-34 follow-up: the caller ESTABLISHES who the tutoring is for — themselves ("it's for me",
# "I'm the student") OR a specific child/relative ("it's for my son", "my daughter needs help"). In
# either case a learner now EXISTS, so learner-specific discovery is legitimate. This must NOT be
# lumped with a denial: "it's for me" answers (not refuses) the who-is-this-for question. Sets
# belief.meta["learner_established"] = True and clears learner_denied.
_LEARNER_ESTABLISH_RE = re.compile(
    r"\b((it'?s|this is|i'?m asking) (for|about) (me|myself)|"
    r"i'?m the (one|student|learner)|(for|about) myself|"
    r"(it'?s|this is) for my (son|daughter|child|kid|grandson|granddaughter|grandchild|niece|nephew|student)|"
    r"my (son|daughter|child|kid|grandson|granddaughter|grandchild|niece|nephew|student) "
    r"(needs?|is|wants?|could use|has)|"
    r"(for|helping|tutoring) my (son|daughter|child|kid|grandson|granddaughter|grandchild|niece|nephew|student))\b",
    re.I,
)

# CB-36: an emotional disclosure / complaint / grievance the agent must ACKNOWLEDGE before any pitch —
# e.g. the caller disclosing they were mistreated/burned by a past tutor or service. Broad on
# negative-experience language. Sets belief.meta["disclosure"] = True (an emotional concern is live).
_DISCLOSURE_RE = re.compile(
    r"mistreat|"
    r"treated (me|us|him|her|my \w+) (badly|poorly|terribl|awful|like)|"
    r"(bad|terrible|awful|horrible|nightmare|negative|rough) (experience|time)|"
    r"(burned|burnt|let down|ripped off|ghosted|scammed)|"
    r"(last|previous|past|former) (tutor|tutors|company|service|agency|provider)s?\b.{0,20}?"
    r"(was|were|treated|did|never|didn'?t|wasted|messed|ruined|let|bad|terrible|awful)|"
    r"did ?n'?t (help|work|show up)|never (showed|helped|worked)|"
    r"wasted (our|my|his|her) (time|money)|"
    r"frustrat|disappoint|fed up|"
    r"\b(upset|hurt|angry)\b",
    re.I,
)


# CB-35: a decline/deflect cue — the prospect declining to answer the pending discovery question
# ("I'd rather not say", "why do you need that", "none of your business", "prefer not to"). A turn
# that both fails to fill the asked slot AND matches this is a DECLINE -> the slot is dropped.
_DECLINE_RE = re.compile(
    r"\b(rather not|prefer not|won'?t say|not say(ing)?|not (gonna|going to) (tell|say|answer)|"
    r"none of your (business|concern)|why (do|would) you (need|want|ask)|"
    r"that'?s (private|personal)|not (relevant|important)|"
    r"no comment|skip (that|this)|move on|next question|"
    r"i'?d rather (you )?not|don'?t want to (say|answer|share)|i'?m not telling)\b",
    re.I,
)


def _is_decline(text: str) -> bool:
    """True if the (already normalized) utterance declines/deflects answering — used with a failed
    slot extraction to mark the pending discovery slot as DECLINED (CB-35)."""
    return bool(_DECLINE_RE.search(text or ""))


def _normalize_punct(text: str) -> str:
    """Fold smart quotes/apostrophes to ASCII so the keyword regexes (which use straight ' ) match
    LLM output. LLMs emit curly apostrophes (don't, can't) constantly; without this fold EVERY
    intent/objection/refusal pattern silently misses them — which left objections unhandled and the
    affordability-refusal count stuck at 0 (the agent looped instead of releasing)."""
    if not text:
        return text
    return (
        text.replace("’", "'").replace("‘", "'")
        .replace("“", '"').replace("”", '"')
    )


def _classify_intent(utterance: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Classify a prospect utterance into (last_user_act, active_objection, open_question).

    Deterministic + zero-latency. `active_objection` is one of the canonical keys (price /
    efficacy_doubt / diy_free / timing / decision_maker) when an objection cue fires. `open_question`
    carries the trimmed utterance when the prospect asked something (so NLG knows WHAT to answer and
    the price_gate's substring check can fire). `last_user_act` is the coarse intent the gates read:
    human_request > confirm_request > memory_check > social_aside > objection > price_inquiry >
    question > statement (None when nothing fires).

    CB-85 (a): 'social_aside' is an off-topic social/weather/sports question with no product keyword.
    It sets open_question so NLG can acknowledge it, but advance_to_close is blocked (never consent).

    CB-61: detect 'confirm_request' (highest priority after human_request) — the prospect explicitly
    asking the agent to confirm a fact they already stated, typically the callback time ("please
    confirm they'll call Thursday after 3pm"). This routes through skip_known -> confirm_known so
    the NLG path echoes the stated time instead of replying vaguely. The confirm verb ("please
    confirm / verify / double-check") must be present; a general question is NOT a confirm_request.

    CB-76 (c): detect 'memory_check' — the caller asking whether they already told us something
    ("did I already tell you when works for me?", "have I mentioned what time I prefer?"). The
    agent MUST NOT assert they did not — this is the NEVER-DENY shape. Routes as 'memory_check'
    so the NLG _NEVER_DENY_NOTE instruction can enforce the rule; if the slot is in KNOWN_SO_FAR,
    confirm it; if absent, hedge ("I may have missed it — when works best?"), never deny."""
    text = _normalize_punct((utterance or "").strip())
    if not text:
        return None, None, None
    is_question = bool(_QUESTION_RE.search(text))
    open_question = text[:300] if is_question else None

    if _HUMAN_REQUEST_RE.search(text):
        return "human_request", None, open_question

    # CB-61: explicit confirm/verify request — routes to confirm_known so the time is echoed.
    if _CONFIRM_REQUEST_RE.search(text):
        return "confirm_request", None, text[:300]

    # CB-76 (c): memory-check — caller asking if they already told us something. Routes as
    # 'memory_check' so the NLG never-deny rule applies. Checked AFTER confirm_request (an
    # explicit "please confirm" is more specific) but BEFORE generic question classification so
    # the NLG guard triggers; treat the text as an open_question so NLG can reference it.
    if _MEMORY_CHECK_RE.search(text):
        return "memory_check", None, text[:300]

    # CB-85 (a): social aside / non-sequitur — an off-topic social question (weather, sports,
    # chit-chat) with NO product/scheduling keyword. Routes as 'social_aside' so that:
    #   (1) advance_to_close is BLOCKED (a social question is NOT scheduling consent);
    #   (2) open_question is set to None so address_direct_input cannot mistake it for a product
    #       question that demands answer_via_kb (the agent acknowledges and redirects instead).
    # Checked AFTER all priority intents and BEFORE the generic question/objection paths.
    # Conservative: requires BOTH a social cue AND the absence of any product keyword — a product
    # question that incidentally mentions the weather is NOT classified as a social aside.
    if is_question and _SOCIAL_ASIDE_CUES_RE.search(text) and not _PRODUCT_KEYWORDS_RE.search(text):
        return "social_aside", None, text[:300]

    objection: Optional[str] = None
    for pat, key in _OBJECTION_PATTERNS:
        if pat.search(text):
            objection = key
            break
    # A bare price question with no complaint cue is a price INQUIRY, not a price objection.
    if objection is None and _PRICE_INQUIRY_RE.search(text):
        return "price_inquiry", None, open_question or text[:300]
    if objection is not None:
        # A price objection still counts as price talk the gate should open.
        return ("price_inquiry" if objection == "price" else "objection"), objection, open_question
    if is_question:
        return "question", None, open_question
    return None, None, None


# --- LLM latent-driver delta-update -----------------------------------------------------------

_DRIVER_RUBRIC = (
    "You are the latent-state tracker + intent classifier for a tutoring sales agent. Given the "
    "agent's last act and the prospect's latest utterance, return ONLY a JSON object with BOTH: "
    "(A) the six driver DELTAS, each a top-level key -> signed delta in [-1, 1] (0 = no change): "
    "trust (credibility/rapport; rises on acknowledgment, falls on pushback/price-shock), "
    "need_intensity (felt problem severity; rises with pain/stakes language), "
    "price_sensitivity (reaction to cost; rises on sticker-shock/comparison-shopping), "
    "urgency (felt time pressure; rises with deadline cues), "
    "purchase_intent (buying signals; MEASURED from explicit intent language), "
    "bail_risk (disengagement/walk-away; rises with terseness, dodges, or frustration); "
    "deltas are LEVEL changes only — no trends/velocities. "
    "(B) these classification fields about the prospect's LATEST utterance: "
    '"user_act": one of "question" | "price_inquiry" | "human_request" | "objection" | "statement"; '
    '"objection": the objection RAISED, one of "price" | "efficacy_doubt" | "diy_free" | "timing" | '
    '"decision_maker", or null if none; '
    '"open_question": the question the prospect is asking (short text) or null if they are not asking one; '
    '"affordability_refusal": true ONLY when the prospect is FIRMLY declining on cost/budget '
    '(e.g. "we just can\'t afford it", "there\'s no budget for this") — NOT merely asking the price '
    "or noting it's pricey; false otherwise. "
    "IMPORTANT: human_request means ONLY an explicit ask to speak with a human, person, "
    "representative, or advisor. Wanting to check with a spouse or partner, run it by someone, or "
    "think it over is NOT a human_request — classify that as the decision_maker objection. General "
    "wariness, skepticism, or hesitation is an objection or statement, NEVER human_request "
    "(mislabeling it forces a premature escalation). "
    "Judge meaning, not keywords (handle paraphrase, typos, and curly apostrophes)."
)


def _build_driver_messages(
    belief: BeliefState, last_agent_act: Optional[str], utterance: str
) -> list[Message]:
    """Assemble the rubric-driven chat messages for the driver delta-update call."""
    prior = {d: round(belief.drivers.get(d, 0.5), 3) for d in DRIVERS}
    user = (
        f"PRIOR_DRIVER_LEVELS: {prior}\n"
        f"LAST_AGENT_ACT: {last_agent_act or 'none'}\n"
        f"PROSPECT_UTTERANCE: {utterance!r}\n\n"
        "Return the JSON delta object now."
    )
    return [
        {"role": "system", "content": _DRIVER_RUBRIC},
        {"role": "user", "content": user},
    ]


# The intent/classification keys the LLM returns alongside the driver deltas. Their PRESENCE in the
# reply is how we know the LLM classified intent (vs a driver-only reply from a test mock / old prompt),
# which decides LLM-primary vs the regex fallback in update().
_INTENT_KEYS = ("user_act", "objection", "open_question", "affordability_refusal")
_VALID_OBJECTIONS = frozenset({"price", "efficacy_doubt", "diy_free", "timing", "decision_maker"})


def dst_model() -> Optional[str]:
    """The model slug for JUST the DST belief/intent call, or None to use the client default.

    Read PER CALL (not at import) so a .env loaded after import still takes effect. Set env DST_MODEL
    to ride a cheaper/faster model here (e.g. openai/gpt-5-nano) while policy/NLG stay on the agent
    default — the mixed-model latency/cost lever. Empty/unset -> None -> llm_client's default model."""
    return os.environ.get("DST_MODEL") or None


def _dst_call_opts() -> dict[str, Any]:
    """Per-call opts for the DST model JSON call.

    When a DST_MODEL override is set we ALSO pin a low reasoning effort: GPT-5-family slugs are
    REASONING models that, left at default, 'think' for ~18s on this rubric (measured) — far slower
    than Sonnet's ~3s; at minimal effort the SAME call is ~1.6s and still returns valid JSON. So the
    override is only a win with reasoning held low. Effort is tunable via DST_REASONING_EFFORT
    (default 'minimal'); set it to 'none' to omit the param. When NO override is set we pass nothing,
    leaving the default (Sonnet) path byte-for-byte unchanged."""
    model = dst_model()
    if not model:
        return {}
    opts: dict[str, Any] = {"model": model}
    effort = os.environ.get("DST_REASONING_EFFORT", "minimal").strip()
    if effort and effort.lower() != "none":
        opts["reasoning"] = {"effort": effort}
    return opts


async def _update_drivers(
    belief: BeliefState,
    last_agent_act: Optional[str],
    utterance: str,
    llm_client: LLMClient,
) -> Optional[dict[str, Any]]:
    """Ask the LLM for per-driver deltas + an intent classification; apply the deltas clamped (mutates
    `belief` in place) and RETURN the parsed intent fields (so update() routes objections/questions/
    refusals by MEANING, not brittle keywords).

    Returns the intent dict {user_act, objection, open_question, affordability_refusal} when the LLM
    actually classified (any _INTENT_KEY present), else None -> caller falls back to the deterministic
    keyword classifier. Robustness contract unchanged: an unparseable/non-dict reply or a call failure
    leaves drivers at their priors AND returns None (regex fallback); hallucinated/garbled values are
    dropped, never corrupting state.
    """
    messages = _build_driver_messages(belief, last_agent_act, utterance)
    try:
        deltas = await llm_client.complete_json(messages, **_dst_call_opts())
    except Exception:
        # FINDING 2: degrade on ANY driver-call failure, not just bad JSON. ValueError is the
        # malformed-JSON path; a real OpenRouter outage raises httpx.HTTPStatusError/RequestError
        # (outliving the client's bounded retry). Either way leave drivers at their priors — the
        # deterministic slot extraction + trend derivation still run — so the turn never crashes.
        return None
    if not isinstance(deltas, dict):
        return None
    for name, delta in deltas.items():
        if name not in belief.drivers:
            continue  # ignore hallucinated/intent keys; keep the six-driver set intact
        try:
            belief.apply_driver_delta(name, float(delta))
        except (TypeError, ValueError):
            continue  # skip a non-numeric delta rather than corrupt the level

    # The LLM intent classification (only when present — else the caller uses the regex fallback).
    if not any(k in deltas for k in _INTENT_KEYS):
        return None
    objection = deltas.get("objection")
    objection = objection if objection in _VALID_OBJECTIONS else None
    open_q = deltas.get("open_question")
    open_q = str(open_q)[:300] if isinstance(open_q, str) and open_q.strip() else None
    user_act = deltas.get("user_act")
    user_act = str(user_act) if isinstance(user_act, str) and user_act.strip() else None
    return {
        "user_act": user_act,
        "objection": objection,
        "open_question": open_q,
        "affordability_refusal": bool(deltas.get("affordability_refusal")),
    }


# --- Deterministically-derived trends ---------------------------------------------------------

def _derive_trends(prior: BeliefState, updated: BeliefState) -> None:
    """Compute `<driver>_velocity` = level(t) - level(t-1) for each driver (mutates `updated`).

    Velocity is a DETERMINISTIC function of the logged trajectory (the prior level vs the new
    level) — never emitted by the LLM. This keeps the state Markov while letting the policy see
    direction-of-travel (frame-stacking pattern, docs/belief-state-schema.md).
    """
    for d in DRIVERS:
        prev = float(prior.drivers.get(d, 0.5))
        now = float(updated.drivers.get(d, 0.5))
        updated.trends[f"{d}_velocity"] = round(now - prev, 6)


# --- EFSM stage derivation (Layer C) ----------------------------------------------------------
# The stage was previously FROZEN at 'greeting' (nothing ever assigned belief.stage), so the policy
# perpetually believed the call was in its opening and never entered a closing frame — a load-bearing
# bug behind the "agent answers questions forever, never closes" failure. Derive it deterministically
# from the belief each turn: greeting -> discovery -> pitch -> close, with a live objection surfacing
# as 'objection'. Heuristic framing for the policy prompt (the deterministic close ACTION is the
# advance_to_close gate); thresholds sit ABOVE the 0.5 neutral prior so a fresh belief stays in
# discovery rather than jumping straight to pitch/close.
_STAGE_PITCH_TRUST = 0.55
_STAGE_CLOSE_TRUST = 0.6
_STAGE_CLOSE_INTENT = 0.45
_STAGE_PITCH_SLOTS = 2


def _derive_stage(belief: BeliefState) -> str:
    """Map the current belief to an EFSM stage label (greeting/discovery/pitch/close/objection)."""
    if belief.turn_count <= 0:
        return "greeting"
    if belief.active_objection:
        return "objection"
    trust = float(belief.drivers.get("trust", 0.0))
    intent = float(belief.drivers.get("purchase_intent", 0.0))
    if trust >= _STAGE_CLOSE_TRUST and intent >= _STAGE_CLOSE_INTENT:
        return "close"
    known = sum(1 for s in belief.slots.values() if float(s.get("confidence", 0.0)) >= _LOCK_CONFIDENCE)
    if trust >= _STAGE_PITCH_TRUST or known >= _STAGE_PITCH_SLOTS:
        return "pitch"
    return "discovery"


# --- Objection grounding (CB-36 follow-up) ----------------------------------------------------
# The LLM intent classifier sometimes HALLUCINATES an objection — e.g. it labeled "I didn't say I had
# a learner" as a 'timing' objection, which then latched and contaminated four turns of invented
# scheduling prose (the deterministic _OBJECTION_PATTERNS regex never matched it). Neuro-symbolic rule:
# the LLM may PROPOSE an objection, but we only ACCEPT one of the five canonical objections when the
# utterance carries a lexical cue for that category; otherwise the label is dropped (None) so the agent
# responds to what was actually said. Broad cue sets — missing a subtle paraphrase only costs us one
# turn of objection-handling (recoverable), whereas a hallucinated objection derails the whole call.
_OBJECTION_GROUNDING: dict[str, Any] = {
    "price": re.compile(
        r"\b(price[ds]?|cost(s|ly)?|expensive|afford|affordab\w*|money|cheap|budget|pay(ing|ment)?|"
        r"dollar|fee[s]?|pricey|spend|\$\d)", re.I),
    "timing": re.compile(
        r"\b(tim(e|ing)|schedul\w*|busy|rush\w*|later|wait\w*|soon|right now|hour[s]?|week[s]?|day[s]?|"
        r"semester|month[s]?|when|avail\w*|fit it in|too soon|not ready|ready|no rush)\b", re.I),
    "efficacy_doubt": re.compile(
        r"\b(work[s]?|working|help[s]?|result[s]?|doubt|prove|proof|guarantee[ds]?|effective\w*|"
        r"difference|skeptic\w*|scam|waste[d]?|legit|sure (this|it)|really (work|help))\b", re.I),
    "diy_free": re.compile(
        r"\b(free|myself|ourselves|our own|on my own|youtube|khan|online|internet|library|google|diy|"
        r"do it (myself|ourselves)|self.?study|video[s]?|neighbou?r|getting by|on our own|managing|"
        r"figur(e|ing) it out)\b", re.I),
    "decision_maker": re.compile(
        r"\b(spouse|husband|wife|partner|think (it over|about it)|talk (to|it over)|discuss|decid\w*|"
        r"ask my|check with|run it by|sleep on it|not my call)\b", re.I),
}


def _ground_objection(objection: Optional[str], utterance: str) -> Optional[str]:
    """Keep an LLM-proposed canonical objection ONLY when the utterance carries a cue for it; else drop
    it to None (CB-36 anti-hallucination). Non-canonical keys (e.g. the deterministic 'concern') and a
    None objection pass through unchanged."""
    if not objection:
        return objection
    pat = _OBJECTION_GROUNDING.get(objection)
    if pat is None:
        return objection
    return objection if pat.search(_normalize_punct(utterance or "")) else None


# CB-67: broad lexical cue set for grounding an LLM-proposed human_request (looser than the strict
# _HUMAN_REQUEST_RE so genuine paraphrases survive, but it must at least NAME a person to talk to or
# a speak/talk/transfer intent). "Can you tell me how this works?" has zero cues and must NOT pass —
# the DST LLM hallucinated human_request on exactly that, and escalation_triggers treats an explicit
# human request as a TERMINAL escalate, ending a brand-new call on turn 1 (caught by the CB-61 replay).
_HUMAN_REQUEST_CUES_RE = re.compile(
    r"\b(?:human|person|people|someone|somebody|representative|rep|agent|advisor|operator|manager|"
    r"specialist|staff|speak|talk|transfer|call\s+me\s+back|real\s+person)\b",
    re.IGNORECASE,
)


def _ground_human_request(act: Optional[str], utterance: str) -> Optional[str]:
    """Keep an LLM-proposed 'human_request' ONLY when the utterance carries a person/speak cue
    (CB-67 — the human_request analogue of CB-36's objection grounding). human_request is the one
    label that terminally ends a call via escalation_triggers, so a hallucinated label is the
    costliest possible misread: an ungrounded one degrades to a plain question/statement turn
    (recoverable), never a terminal escalate."""
    if act != "human_request":
        return act
    if _HUMAN_REQUEST_CUES_RE.search(_normalize_punct(utterance or "")):
        return act
    return "question" if _QUESTION_RE.search(_normalize_punct(utterance or "")) else "statement"


# --- The per-turn hybrid update ---------------------------------------------------------------

async def update(
    belief: BeliefState,
    last_agent_act: Optional[str],
    user_utterance: str,
    llm_client: LLMClient,
    *,
    last_user_act: Optional[str] = None,
) -> BeliefState:
    """Run one hybrid belief update and return a NEW BeliefState (the input is not mutated).

    Order: deterministic slot extraction -> ONE LLM call (driver deltas + intent/objection/refusal
    classification) -> deterministic trend derivation -> meta bookkeeping (turn_count++, intent
    signals + the firm-refusal count). The LLM classifies intent by MEANING; a keyword classifier is
    the fallback when the call fails. The trend step reads the PRIOR levels (captured before the
    driver update), so velocity reflects this turn's actual change.
    """
    prior = belief  # keep a reference to the pre-update levels for trend derivation
    updated = belief.copy()

    # 1. Deterministic slots (confidence-arbitrated merge). Pass last_agent_act so the grade extractor
    #    knows whether the agent JUST asked the grade (accept a bare number "8" only in that context).
    _extract_slots(updated, user_utterance, last_agent_act)

    # 2. LLM latent-driver deltas + intent classification in ONE call (clamped, robust to bad output).
    #    The LLM reads the nuance (objection / question / firm-budget-refusal — paraphrase, typos,
    #    curly apostrophes and all); the deterministic gates downstream still DECIDE. llm_intent is
    #    None on a call failure / driver-only reply -> we fall back to the keyword classifier so the
    #    turn always degrades gracefully (regex was the brittle PRIMARY before; it's now the fallback).
    llm_intent = await _update_drivers(updated, last_agent_act, user_utterance, llm_client)

    # 3. Deterministically-derived trends from the logged trajectory (prior -> updated levels).
    _derive_trends(prior, updated)

    # 4. Meta bookkeeping + intent/objection signals (LLM-primary, regex fallback). These fill the
    #    signals the policy/gates/NLG need to ADDRESS the prospect (objection, open question,
    #    price-inquiry, firm refusal); an explicit last_user_act passed in (an upstream NLU label) wins.
    updated.turn_count = prior.turn_count + 1
    if llm_intent is not None:
        # CB-67: ground the LLM's human_request label BEFORE anything reads it — an unsupported
        # human_request is the costliest hallucination (terminal escalate on a normal question).
        classified_act = _ground_human_request(llm_intent["user_act"], user_utterance)
        # CB-36 anti-hallucination: GROUND the LLM-proposed objection — keep it only when the utterance
        # carries a cue for that category. On real calls the LLM intent classifier hallucinated objections
        # ('timing' on "Why do you keep asking? I said third", 'diy_free' on "this feels like a scam.
        # Fuck you", 'timing' on "I didn't say I had a learner") that latched and invented context
        # ("we can start whenever works for you", "some great free resources"). The deterministic gate
        # drops a label with zero lexical support; the cue sets are broad (looser than the strict
        # _OBJECTION_PATTERNS), so a genuine paraphrased objection still grounds and is kept — only a
        # truly unsupported label is dropped (degrading to one un-handled turn, recoverable).
        objection = _ground_objection(llm_intent["objection"], user_utterance)
        if classified_act == "objection" and objection is None:
            classified_act = "statement"  # a phantom objection is not an objection turn
        open_question = llm_intent["open_question"]
        refused = llm_intent["affordability_refusal"]

        # CB-89: LIVE-PATH social_aside override — the LLM intent vocabulary never includes
        # 'social_aside', so the gate guard + NLG ack only fired on the regex fallback path (when the
        # LLM call failed). Fix: after accepting the LLM result, run the deterministic social-aside
        # check over the raw utterance regardless. If the text matches _SOCIAL_ASIDE_CUES_RE AND
        # NOT _PRODUCT_KEYWORDS_RE (same AND-guard as _classify_intent), AND the LLM returned only a
        # WEAK intent ('question'/'statement'/None — not a stronger objection/human_request/
        # price_inquiry that must be preserved), override to 'social_aside'. This keeps
        # advance_to_close blocked + NLG _SOCIAL_ASIDE_NOTE injected on the live path. Conservative:
        # a genuine product/scheduling question ("weather policy if we need to reschedule?") is NOT
        # overridden because _PRODUCT_KEYWORDS_RE exempts it; a real objection/human_request/
        # price_inquiry is not overridden because the stronger-intent guard protects it.
        _SOCIAL_ASIDE_WEAK_ACTS = frozenset({"question", "statement", None})
        _norm_utt = _normalize_punct(user_utterance or "")
        _is_question_utt = bool(_QUESTION_RE.search(_norm_utt))
        if (
            classified_act in _SOCIAL_ASIDE_WEAK_ACTS
            and _is_question_utt
            and _SOCIAL_ASIDE_CUES_RE.search(_norm_utt)
            and not _PRODUCT_KEYWORDS_RE.search(_norm_utt)
        ):
            classified_act = "social_aside"
            # Carry the question text as open_question so NLG can acknowledge it
            if open_question is None:
                open_question = _norm_utt[:300]

        # CB-91: LIVE-PATH memory_check override — the SAME gap CB-89 fixed for social_aside, now for
        # the never-deny rule. _MEMORY_CHECK_RE ("did I already tell you …?") lives only in the regex
        # fallback `_classify_intent`; the live LLM never returns 'memory_check', so last_user_act was
        # never 'memory_check' on a real call → NLG's _NEVER_DENY_NOTE never injected → the agent could
        # DENY the caller said something they did ("No, you haven't yet" — the gaslighting CB-76 meant
        # to kill, found live again in QA11). Fix: after the LLM result, if the text matches
        # _MEMORY_CHECK_RE and the LLM returned a weak intent (question/statement/None — a "did I tell
        # you" is a question), override to 'memory_check' so the never-deny note fires live. Runs AFTER
        # social_aside (a memory check is never a weather aside) and is mutually exclusive in practice.
        _MEMORY_CHECK_WEAK_ACTS = frozenset({"question", "statement", None})
        if (
            classified_act in _MEMORY_CHECK_WEAK_ACTS
            and _MEMORY_CHECK_RE.search(_norm_utt)
        ):
            classified_act = "memory_check"
            if open_question is None:
                open_question = _norm_utt[:300]

        # CB-90 (a) round-2: LIVE-PATH guarantee/efficacy upgrade — SCOPED to a guarantee-of-outcome
        # DEMAND ONLY. The ONLY documented live-path failure is a guarantee demand ("Guarantee me a
        # B or it's free") the LLM mislabels as 'statement' so the objection is silently dropped.
        # The round-1 version ran the FULL _OBJECTION_PATTERNS scan here, which AMPLIFIED the regex's
        # context-free over-matches onto the live path — second-guessing the LLM's CORRECT 'statement'
        # judgement on cooperative phrases ("feel free to send me information" → diy_free; "I think
        # it's really worth it" → efficacy_doubt). For every objection type OTHER than a guarantee
        # demand we now TRUST the LLM (it judges by meaning; the regex words are context-free). So the
        # upgrade fires ONLY when the NARROW _GUARANTEE_DEMAND_RE matches AND the LLM returned a weak
        # intent (statement/question/None, no objection). Genuine guarantee demand → efficacy_doubt;
        # everything else keeps the LLM's call. (The pattern-ORDERING fix stays on the regex fallback.)
        # CB-90a-fix: "question" is a WEAK act here too — live, the LLM labels a guarantee demand
        # phrased as "...Yes or no?" / "Can you guarantee a B?" as `question`, which left the guarantee
        # unhandled (answer_via_kb instead of handle_objection). Upgrading a `question` is safe ONLY
        # because _GUARANTEE_DEMAND_RE is tight (requires a grade/result token) — a normal question
        # never matches it, so this cannot steal genuine questions (proven by the adversarial suite).
        _OBJECTION_WEAK_ACTS = frozenset({"statement", "question", None})
        if (
            classified_act in _OBJECTION_WEAK_ACTS
            and objection is None
            and _GUARANTEE_DEMAND_RE.search(_norm_utt)
        ):
            classified_act = "objection"
            objection = "efficacy_doubt"
    else:
        classified_act, objection, open_question = _classify_intent(user_utterance)
        refused = bool(_AFFORDABILITY_REFUSAL_RE.search(_normalize_punct(user_utterance or "")))
    updated.last_user_act = last_user_act if last_user_act is not None else classified_act
    updated.active_objection = objection
    updated.open_question = open_question

    # Track the CUMULATIVE firm-affordability-refusal count in meta (carried across turns by copy()).
    # A refusal also counts as a live price objection so the FIRST ones get a value reframe
    # (handle_objection); once the count crosses the gate threshold the reframe has demonstrably not
    # landed and the agent disqualifies/releases. Cumulative (not reset each turn) so it survives the
    # prospect rephrasing the same refusal differently turn to turn.
    prior_refusals = int(prior.meta.get("affordability_refusal_count", 0))
    if refused:  # LLM-judged firm refusal (or the regex fallback when the LLM call failed)
        updated.meta["affordability_refusal_count"] = prior_refusals + 1
        if updated.active_objection is None:
            updated.active_objection = "price"
    else:
        updated.meta["affordability_refusal_count"] = prior_refusals  # cumulative — never reset down

    # CB-37: hostility / abuse detection — a profane or abusive turn sets a sticky hostility flag the
    # pushiness gate reads to STOP pitching and de-escalate (it never auto-clears within the call; once
    # a caller turns hostile the agent stays in de-escalation mode rather than re-pitching). Deterministic
    # so text + voice share it (R37). Detected on the normalized utterance (curly-apostrophe-safe).
    norm = _normalize_punct(user_utterance or "")
    if _HOSTILITY_RE.search(norm):
        updated.meta["hostility"] = True

    # CB-34: track whether a learner has been ESTABLISHED (who the tutoring is for). A learner is
    # established once we know a learner-specific slot OR the caller said it's for them / a child. An
    # explicit denial ("I didn't say I had a learner") DROPS the presumption and forces a who-is-this-
    # for step before any learner-specific question.
    if _LEARNER_ESTABLISH_RE.search(norm):
        # The caller said who it's for (themselves or a specific child) -> a learner now EXISTS. This
        # ANSWERS the who-is-this-for question, so it overrides a prior denial and unblocks discovery.
        updated.meta["learner_established"] = True
        updated.meta["learner_denied"] = False
        updated.meta["who_for"] = True
    elif _LEARNER_DENIAL_RE.search(norm):
        updated.meta["learner_denied"] = True
        updated.meta["learner_established"] = False
    elif (
        updated.slot_confidence("grade_level") >= _LOCK_CONFIDENCE
        or updated.slot_confidence("subject") >= _LOCK_CONFIDENCE
        or bool(updated.meta.get("who_for"))
    ):
        # A learner-specific fact (or an explicit who-for answer) was given -> a learner exists.
        updated.meta["learner_established"] = True

    # CB-36: an emotional disclosure / complaint (mistreated by a past tutor, bad experience) sets a
    # live-concern flag the policy reads to ACKNOWLEDGE before any pitch. Like an objection, it should
    # be addressed; surface it as the active concern so address_direct_input routes to handle it.
    if _DISCLOSURE_RE.search(norm):
        updated.meta["disclosure"] = True
        # A mistreatment / bad-experience disclosure is an emotional CONCERN to ACKNOWLEDGE first — it
        # OVERRIDES whatever objection the LLM guessed (on a real call it mislabeled "they wasted our
        # money" as an 'efficacy_doubt' to REBUT, instead of a concern to acknowledge). 'concern' routes
        # through address_direct_input -> handle_objection. Exception: a firm affordability refusal keeps
        # its 'price' label so the value-reframe / release path (affordability_refusal_count) still fires.
        if not refused:
            updated.active_objection = "concern"
    else:
        # The disclosure is live only on the turn it surfaces; clear it so the agent moves on AFTER it
        # has acknowledged (the concern does not pin the agent in handle_objection forever).
        updated.meta["disclosure"] = False

    # CB-35: track ASKED and DECLINED discovery slots ACROSS the call (carried in meta by copy()). When
    # the agent just asked a slot and the prospect DECLINES to answer it (no extraction, and a decline/
    # deflect cue), record it as declined so the no-repeat gate drops it. asked_slots is incremented in
    # respond() (which knows the gated decision's target_slot); here we only handle declines.
    pending = prior.meta.get("pending_ask_slot")
    if pending and last_agent_act in ("ask", "confirm_known"):
        answered = updated.slot_confidence(pending) >= _LOCK_CONFIDENCE
        declined = _LEARNER_DENIAL_RE.search(norm) or _is_decline(norm)
        if not answered and declined:
            dl = list(updated.meta.get("declined_slots", []))
            if pending not in dl:
                dl.append(pending)
            updated.meta["declined_slots"] = dl

    # CB-76 (d): contact acknowledgment — when a turn delivers name/phone/email, set the
    # 'contact_just_captured' flag TRUE so NLG's _CONTACT_ACK_NOTE instruction fires on the NEXT
    # reply (ANY act), producing a one-clause ack ("Got it, Dana — I'll use this number."). The flag
    # is ONE-SHOT: it fires on the turn after capture and resets, so the ack is never repeated.
    # Detection is deterministic via heuristics (phone number pattern, email pattern, name intro) —
    # the full contact extraction lives in the demo/voice flow; this is the DST-side signal.
    # CB-76 round-3 (CRITICAL gate): a phone OR email OR an EXPLICIT-INTRO name (_CONTACT_NAME_RE now
    # matches only "my name is / I'm <Cap>" forms with a case-sensitive name group) sets the flag.
    # A bare capitalized word with no intro and no phone/email never triggers — the regression case
    # "...or it's free. Yes or no." carries no contact context, so the guarantee objection is NOT
    # swallowed by a spurious "Got it, Free —" ack.
    phone_or_email = bool(_CONTACT_PHONE_RE.search(norm) or _CONTACT_EMAIL_RE.search(norm))
    name_match = _CONTACT_NAME_RE.search(norm)  # explicit-intro name only (case-sensitive group)
    if phone_or_email or name_match:
        updated.meta["contact_just_captured"] = True
        # Name for the ack ("Got it, Dana") — only from the explicit-intro pattern when present.
        if name_match:
            updated.meta["contact_name"] = name_match.group(1).capitalize()
    else:
        # Clear the one-shot flag so the ack fires ONLY on the turn after the capture turn.
        updated.meta["contact_just_captured"] = False

    # CB-90 (b): callback_window acknowledgment — when the callback_window slot is NEWLY filled
    # this turn (absent or unlocked in the prior belief, now present at confidence >= lock), set
    # the 'callback_window_just_captured' flag TRUE so NLG's _CALLBACK_WINDOW_ACK_NOTE instruction
    # fires, producing a warm one-clause ack ("Got it — Wednesdays or Fridays after 4") and
    # explicitly forbidding the "I don't have that availability in front of me" deflection. ONE-SHOT:
    # cleared on the next turn so the ack never repeats. The prior belief comparison uses the slot
    # confidence: if the prior confidence was below lock and the updated confidence is at/above lock,
    # OR if the slot was absent (None) and is now present, the window was just volunteered this turn.
    prior_cw_conf = float(prior.slots.get("callback_window", {}).get("confidence", 0.0)) if prior.slots.get("callback_window") else 0.0
    updated_cw_conf = float(updated.slots.get("callback_window", {}).get("confidence", 0.0)) if updated.slots.get("callback_window") else 0.0
    cb_newly_captured = (
        updated_cw_conf >= _LOCK_CONFIDENCE
        and prior_cw_conf < _LOCK_CONFIDENCE
    )
    if cb_newly_captured:
        updated.meta["callback_window_just_captured"] = True
        # Store the human-readable value for the NLG ack (the stored normalized string).
        cw_val = (updated.slots.get("callback_window") or {}).get("value", "")
        updated.meta["callback_window_value"] = str(cw_val) if cw_val else ""
    else:
        # Clear the one-shot flag on any turn that didn't just capture the window.
        updated.meta["callback_window_just_captured"] = False

    # Advance the EFSM stage from the (now-updated) belief so the policy knows WHERE the call is
    # (greeting -> discovery -> pitch -> close / objection) instead of being stuck at 'greeting'.
    updated.stage = _derive_stage(updated)

    return updated
