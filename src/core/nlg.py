# Natural-language generation (plan U4): realize a GATED Decision into a persona-consistent,
# voice-shaped reply. realize(decision, belief, config, llm_client, *, retrieved_facts=None) builds
# a persona + act-specific prompt (warm consultative advisor, low-pressure) and asks the llm_client
# for one concise spoken-style turn. If retrieved_facts is given (KB grounding, wired in U5), the
# reply is constrained to those facts (state no fact absent from them). Decouples strategy (policy)
# from surface text (CraigslistBargain policy<->NLG split). Pure + async, NO LiveKit imports.
# CB-41 (stream NLG -> TTS): realize_stream(...) is the STREAMING twin — it builds the SAME prompt
# _build_messages() produces and yields the reply token-by-token from llm_client.complete_stream, so
# the voice worker can speak the first clause at NLG first-token (~0.5-1.6s) instead of after the
# whole reply. The blocking realize() is KEPT for the text/self-play path; both share _build_messages
# so the prompt (and thus the decided content) is identical. On a streaming failure / blank stream,
# realize_stream yields the SAME safe filler realize() falls back to (the turn is never empty).
# CB-29/CB-34 (no hallucinated slots, no presumed learner): the system prompt ALWAYS carries a hard
# grounding rule forbidding the agent from asserting any person/relationship/gender/grade/subject — or
# even the EXISTENCE of a learner — the DST has not confirmed; with no confirmed learner it uses neutral
# phrasing ("you") and finds out who the tutoring is for first, never "your daughter".
# CB-36 (no invented conversational context): a second hard rule forbids referencing any objection,
# topic, or concern the prospect did NOT raise, and requires acknowledging a disclosure/complaint
# before any pitch. Both rules are appended LAST so an authored config prompt can't override them.
# CB-48 (anti-repetition): _build_messages now also accepts own_last_lines — the agent's OWN last 1-2
# SPOKEN turns (threaded from respond/respond_stream, which hold the history). When present they are
# surfaced as YOUR_LAST_LINES with an instruction NOT to restate a point/offer already made (rephrase
# substantively or advance). ONLY the agent's own lines are passed — never the caller transcript — so
# the distillation is preserved (no new inventable facts). realize + realize_stream stay in lock-step.
# SAFEGUARD 2 (runtime grounding): the scattered soft grounding wording is CONSOLIDATED into ONE clear
# _GROUNDING_RULE (no invented figure/price/percentage/guarantee or student-outcome story) applied as
# the first-line constraint on a CLAIM turn (answer_via_kb/handle_objection/pitch) or whenever facts
# exist. realize() additionally accepts grounding_retry=True — the BLOCKING-path regenerate respond()
# triggers when retriever.grounded() flags the first reply as ungrounded; it hardens the rule
# (_GROUNDING_RETRY_RULE) so the second attempt states only supported claims. The STREAMING path keeps
# the first-line rule only — a streamed token can't be un-spoken, so respond_stream NEVER regenerates
# mid-stream (an ungrounded streamed reply is flagged in decision.meta by respond_stream for
# observability, not rewritten). realize + realize_stream still build the SAME prompt (lock-step).
# CB-61 (confirm-echo): when the gated decision is confirm_known targeting callback_window (or any
# other CB-61 slot), _build_messages surfaces the slot value in KNOWN_SO_FAR so the LLM has the exact
# time to echo. The confirm_known guidance was also made time-echo-aware: when TARGET_SLOT is
# callback_window, the guidance explicitly says to echo the booked time back verbatim.
# CB-62 (price-question policy): when the prospect asks a DIRECT price question, _build_messages
# injects a BUDGET_CONCERN_NOTE instruction: acknowledge any stated budget concern before pivoting
# (never "Perfect —" after "tight budget"), then use the KB-grounded ballpark from retrieved_facts
# (never hardcoded), then offer the exact-quote callback. The constraint stays pure — the number comes
# from the facts block, not from this instruction. realize/realize_stream stay in lock-step.
# CB-64 (repetition residual): _near_verbatim_repetition() detects a >8-content-word overlap between
# the new reply and own_last_lines — the shape of the QA6 reply-1-vs-3 verbatim pitch that the
# _NO_RESTATE_INSTRUCTION alone failed to prevent. On a hit the blocking path regenerates ONCE with
# _REPETITION_RETRY_RULE (a harder variant), mirroring the CB-53 grounding-retry pattern exactly;
# the streaming path flags decision.meta["repetition"] for observability only (can't un-speak a
# token). Deterministic: the >8-word threshold is a hard count, not an LLM judgment. Shares
# _CONTENT_WORD_RE so the definition of "content word" is the same as gates.py's CB-48 logic.
# CB-76 (c) NEVER-DENY RULE: when last_user_act == 'memory_check' (caller asks "did I already tell
# you X?"), _NEVER_DENY_NOTE is injected: the agent MUST NOT assert the caller did not say something.
# If the slot is in KNOWN_SO_FAR, echo it; if absent, hedge honestly ("I may have missed it — when
# works best?"). Two-layer safety: DST extraction captures the value when possible; even when it
# missed, the deny is unconditionally forbidden by this instruction.
# CB-76 (d) CONTACT ACK: when belief.meta['contact_just_captured'] is True, _CONTACT_ACK_NOTE is
# injected so the reply opens with a one-clause acknowledgment ("Got it, Dana — I'll use this
# number.") regardless of the gated act. The DST sets this flag one-shot (cleared on the next turn).
# CB-77 (f): _BUDGET_CONCERN_NOTE is STRENGTHENED to demand the floor–ceiling numeric range from the
# FACTS BLOCK when available. "a few hundred" is no longer acceptable when the facts carry "$X–$Y".
# CB-85 (a) SOCIAL ASIDE NOTE: when last_user_act == 'social_aside', _SOCIAL_ASIDE_NOTE is injected —
# the agent MUST briefly acknowledge the off-topic remark in ≤6 words then redirect to the sales goal;
# and must NEVER interpret the aside as consent or agreement to a close/callback/schedule.
# CB-85 (b) PRICE HONESTY NOTE: _BUDGET_CONCERN_NOTE is further strengthened with an explicit ban on
# "I don't have access to / I don't have the pricing" language when facts contain a price range —
# the agent must NEVER claim it lacks pricing it can ground from the facts block.
from __future__ import annotations

import re
from typing import Any, AsyncIterator, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient, Message

# The safe filler an empty/failed realization degrades to — shared by realize() (returned) and
# realize_stream() (yielded) so both paths always produce a non-empty turn (FINDING 2).
_SAFE_FILLER = "Let me make sure I'm helping you the right way here."

# Per-act realization guidance: what the spoken turn should accomplish for each gated act. Keeps NLG
# faithful to the policy's decision while leaving phrasing to the LLM (persona-consistent).
_ACT_GUIDANCE: dict[str, str] = {
    "confirm_known": (
        "Briefly confirm what we already know (don't re-ask it) and move forward warmly. "
        "CB-61: if TARGET_SLOT is 'callback_window', echo the exact booked day/time from KNOWN_SO_FAR "
        "verbatim — say it back explicitly (e.g. 'Thursday after 3pm — noted, our team will call you then')."
    ),
    "ask": "Ask ONE discovery question (the target slot), conversationally — never interrogate.",
    "answer_via_kb": "Answer their question using ONLY the provided facts; if none cover it, say you'll follow up rather than guess.",
    "pitch": "Re-anchor on value: connect our approach to their stated goal/pain. Do NOT quote price.",
    "handle_objection": "Acknowledge the concern empathetically, then reframe with a grounded point. No pressure.",
    "attempt_close": "Invite the next step at the proposed commitment tier, low-pressure, easy to say yes to.",
    "escalate": "Gracefully defer in-persona: a specialist will follow up; secure a concrete next step.",
    "disqualify": "Release gracefully and warmly: acknowledge the fit isn't there now, leave the door open.",
}

# How each commitment-ladder tier should sound when realizing an attempt_close (R29).
_TIER_GUIDANCE: dict[str, str] = {
    "enrollment": "Invite them to enroll and get matched with a tutor.",
    "trial": "Offer a low-commitment trial/first session so they can see the fit.",
    "consultation": "Offer a free consultation/assessment to map out a plan.",
    "callback": "Offer a follow-up callback at a time that works for them.",
}

_PERSONA_FALLBACK = (
    "You are a warm, consultative learning advisor for a tutoring service. You are low-pressure, "
    "educational, and concise. You speak in short, natural spoken sentences (this may be read aloud)."
)

# CB-29/CB-34 (hallucinated slots + presumed learner): a HARD grounding rule that always applies. The
# agent invented a "daughter"/"student" the caller never mentioned (presumed a gender/grade/subject)
# AND presumed a learner EXISTS ("what grade is the learner in?") before discovery established one. It
# must speak ONLY from the CONFIRMED belief slots (KNOWN_SO_FAR) + retrieved facts — never asserting a
# person, relationship, gender, grade, subject, or the very EXISTENCE of a learner the DST has not
# confirmed. With no confirmed learner, use neutral phrasing ("you" / "the learner") and do not assume
# the caller even has a learner — find out WHO the tutoring is for first.
_NO_HALLUCINATED_SLOTS = (
    "GROUNDING RULE (do not break): speak ONLY from KNOWN_SO_FAR (the confirmed facts) and any "
    "retrieved facts below. Do NOT invent or presume ANY detail the prospect has not stated — never "
    "assert a child, son, daughter, student, their gender, their grade, their subject, or any "
    "relationship that is not in KNOWN_SO_FAR. Do NOT presume a learner even EXISTS: if no learner/"
    "student is confirmed in KNOWN_SO_FAR, do not ask about \"the learner\"/\"your student\" as if one "
    "is given — first find out who the tutoring is for (the caller themselves, their child, or someone "
    "else). Use \"you\", never \"your daughter\"/\"your son\"/\"your child\"/\"your student\". "
    "Ask, don't assume."
)

# CB-36 (no invented conversational context): the agent referenced things the caller never raised — it
# invented "timing matters" / "think it through", answered a "free resources" objection nobody raised,
# and pitched right after the caller disclosed being mistreated by a past tutor. NLG must respond ONLY
# to what the caller actually said this call: never introduce an objection, concern, or topic the
# prospect did not raise (ACTIVE_OBJECTION/PROSPECT_ASKED carry what they DID raise), and when they
# disclose a concern or a bad experience, ACKNOWLEDGE it — do not pivot to a pitch or sales line.
_NO_INVENTED_CONTEXT = (
    "CONTEXT RULE (do not break): respond ONLY to what the prospect actually said. Do NOT introduce an "
    "objection, worry, or topic they did not raise (e.g. do not bring up \"timing\", \"thinking it "
    "over\", or free resources unless THEY did), and do NOT answer a concern nobody voiced. If they "
    "disclosed a problem or a bad past experience, acknowledge THAT first — never follow a disclosure "
    "with a pitch or a sales push."
)


def _persona_system(config: AgentConfig) -> str:
    """The persona system prompt: prefer the authored NLG prompt + persona, else a safe fallback.

    Always appends the CB-29/CB-34 no-hallucinated-slots/no-presumed-learner grounding rule AND the
    CB-36 no-invented-context rule LAST so an authored prompt can't override them — the agent must
    never assert a person/relationship/grade/learner the DST hasn't confirmed, and must respond only
    to what the prospect actually raised (no invented objections/topics; acknowledge disclosures).
    """
    persona = config.persona
    persona_line = (
        f"Your name is {persona.name}, a {persona.role}. Style: {persona.style}."
        if persona
        else ""
    )
    authored = config.prompts.get("nlg") or ""
    base = "\n".join(
        p
        for p in (
            _PERSONA_FALLBACK,
            persona_line,
            authored,
            _NO_HALLUCINATED_SLOTS,
            _NO_INVENTED_CONTEXT,
        )
        if p
    ).strip()
    return base


# SAFEGUARD 2 (runtime grounding) — ONE consolidated grounding rule, replacing the scattered soft
# wording. It is the first-line constraint on BOTH paths (blocking + streaming) so the model is told
# up front to invent no specific (a figure or a student-outcome story) not in the facts. The blocking
# path additionally re-runs NLG with the HARDENED variant below when a reply still slips an unsupported
# specific through; the streaming path cannot un-speak a token, so this first-line rule is its only
# guard (an ungrounded streamed reply is flagged for observability, never regenerated mid-stream).
_GROUNDING_RULE = (
    "GROUNDING RULE (do not break): state ONLY claims supported by the facts provided below. Do NOT "
    "invent a specific figure, price, percentage, guarantee, or a student-outcome story (e.g. \"went "
    "from a D to a B+\", \"scored an A on the final\") that is not in those facts. If you lack a "
    "specific number or story, say so plainly — do NOT make one up."
)

# The HARDENED re-instruction the blocking path uses on a regenerate after grounded() flagged the
# first reply as ungrounded. Stronger + explicit about the honest fallback so the second attempt does
# not repeat the fabrication. Only used by realize(grounding_retry=True); never on the stream path.
_GROUNDING_RETRY_RULE = (
    "Your previous reply asserted a specific the provided facts do NOT support. REWRITE it: state ONLY "
    "claims directly supported by the facts below. If you do not have a specific figure or a real "
    "student story, say plainly that you don't have that exact number/example and offer a concrete "
    "next step (a consultation, a callback, or connecting them with the details) — do NOT invent one."
)


# SAFEGUARD 2: acts that ASSERT a product fact/proof to the prospect — the turns the runtime grounding
# guard checks (mirrors loop/grading._FACTUAL_ACTS minus confirm_known, which only echoes known slots).
# A bare ask/confirm/escalate asserts nothing, so it is NOT grounding-checked and gets no facts-claim
# nudge (its prompt is unchanged from before SAFEGUARD 2).
_CLAIM_ACTS = frozenset({"answer_via_kb", "handle_objection", "pitch"})


def _grounding_block(
    retrieved_facts: Optional[Sequence[Any]],
    *,
    is_claim: bool = False,
    retry: bool = False,
) -> str:
    """Render the SAFEGUARD 2 grounding constraint + any retrieved KB facts (U5), or "" when nothing
    applies. Behavior:
      • facts present -> the consolidated _GROUNDING_RULE + the facts (was: a softer ad-hoc block);
      • NO facts but a CLAIM turn -> the rule + an explicit "no facts retrieved, don't invent a
        specific" nudge so a fact-asserting turn is pushed toward the honest line rather than inventing;
      • NO facts and NOT a claim turn -> "" (unchanged from before — a bare ask carries no grounding
        block, so the opening/discovery prompt is identical to pre-SAFEGUARD-2).
    `retry` (blocking-path regenerate after grounded() flagged the first reply) prepends the stronger
    _GROUNDING_RETRY_RULE."""
    if not retrieved_facts and not is_claim:
        return ""
    lines: list[str] = []
    if retry:
        lines.append(_GROUNDING_RETRY_RULE)
    lines.append(_GROUNDING_RULE)
    if retrieved_facts:
        facts = "\n".join(f"- {str(f)}" for f in retrieved_facts)
        lines.append("Facts you may rely on:\n" + facts)
    else:
        lines.append("(No supporting facts were retrieved — do not state any specific figure or story.)")
    return "\n".join(lines)


# CB-48 (anti-repetition): the instruction that accompanies YOUR_LAST_LINES — do NOT restate a point
# or offer already made; if the decided act would repeat the last line, rephrase substantively or move
# the conversation forward. Kept as a named constant so realize/realize_stream share the exact wording.
_NO_RESTATE_INSTRUCTION = (
    "Do NOT repeat a statistic, fact, claim, or offer you already gave in YOUR_LAST_LINES — re-citing "
    "the same number (e.g. a satisfaction percentage) or re-offering the same thing reads as evasive. "
    "If you have already made a point, do NOT say it again in different words; instead add genuinely "
    "NEW substance (a different, concrete proof or detail) or move the conversation forward with a "
    "concrete next step. If the prospect keeps pressing for something you honestly cannot give, say so "
    "plainly once and pivot to the most useful next step — never loop."
)

# CB-62 (price-question policy): the ack instruction injected into the NLG prompt whenever the
# prospect has asked a DIRECT price question (last_user_act == "price_inquiry"). It instructs the
# model to (1) acknowledge any stated budget concern before pivoting — never a chipper "Perfect —"
# after "tight budget" — and (2) offer the KB-grounded ballpark from the facts block (the number
# MUST come from the retrieved facts, never invented here) with the "depends on needs/plan" qualifier,
# then (3) bridge to the exact-quote callback. The instruction carries no price figure itself — that
# grounding constraint from CB-53 is inviolable. Both realize() and realize_stream() share it because
# they both build from _build_messages().
# CB-77 (f): STRENGTHENED — the model must extract and state the numeric floor–ceiling from the FACTS
# BLOCK when one is present (e.g. "$40–80/hr" or "$300–600/month"). "a few hundred" is explicitly
# forbidden when facts carry real numbers. The CB-53 grounding guard still prevents inventing figures.
_BUDGET_CONCERN_NOTE = (
    "PRICE QUESTION POLICY (do not break): the prospect asked directly about cost. "
    "Step 1 — acknowledge any stated budget concern with genuine empathy FIRST (e.g. if they said "
    "'tight budget', say so explicitly: 'That makes sense — budget is a real consideration here'); "
    "NEVER reply with a chipper opener like 'Perfect!' or 'Great!' after someone mentions financial "
    "constraints. "
    "Step 2 — give the SPECIFIC KB-grounded floor–ceiling range from the facts provided below "
    "(e.g. '$40–80 per hour' or '$300–$600 per month' — use the EXACT numbers from the facts); "
    "do NOT use vague language like 'a few hundred' or 'it varies' when the facts carry real numbers; "
    "frame it as 'most families land around $X–$Y depending on the plan and goals'. "
    "CRITICAL: if the facts below contain a price range, you MUST state it — do NOT say "
    "'I don't have access to the exact pricing' or 'I don't have that number' or 'pricing is not "
    "available to me'; those claims are FALSE when facts are present. The range is in the facts. "
    "Step 3 — offer the free consultation as the place to get an exact personalized quote. "
    "If the facts do NOT include a price range, say plainly that price varies and the exact quote "
    "comes from the free consultation — do NOT invent a number."
)

# CB-85 (a) SOCIAL ASIDE NOTE: injected when last_user_act == 'social_aside'. The agent must give a
# brief human acknowledgment (≤6 words) of the off-topic remark FIRST, then redirect warmly to the
# goal — and must NEVER interpret the remark as agreement, consent, or scheduling intent. A question
# about the weather / a sports score / weekend plans is NOT a "yes" to anything. This prevents the
# live defect: "you guys see the storm coming tonight?" → "Sounds good — let me get you scheduled."
_SOCIAL_ASIDE_NOTE = (
    "SOCIAL ASIDE RULE (do not break): the caller just made an off-topic social remark (weather, "
    "sports, chit-chat, or similar). "
    "Step 1 — briefly acknowledge it in ≤6 words (e.g. 'Ha, yes — quite a storm out there!' "
    "or 'Sounds like a busy day!') — one short human touch only. "
    "Step 2 — smoothly redirect back to the goal. "
    "NEVER interpret the off-topic remark as agreement, scheduling consent, or a 'yes' to anything. "
    "Do NOT say 'Sounds good — let me get you scheduled' or any equivalent after an off-topic aside."
)

# CB-76 (c) NEVER-DENY RULE: injected when last_user_act == 'memory_check'. The agent must NEVER
# assert the caller did not say something. If the relevant slot is in KNOWN_SO_FAR, echo it back
# verbatim. If the slot is absent from KNOWN_SO_FAR, hedge honestly without denying ("I want to
# make sure I have this right — when does work best?") — do NOT say "No, you haven't told me" or
# any equivalent confident denial. This is the QA9 failure mode: slot was empty (the disjunctive
# window was missed) AND the agent confidently denied the caller stated it.
_NEVER_DENY_NOTE = (
    "NEVER-DENY RULE (do not break): the caller is asking whether they already told you something. "
    "You MUST NOT assert they did not say it. "
    "If the slot or answer is in KNOWN_SO_FAR, confirm it: echo the value back ('Yes — you mentioned "
    "Wednesdays or Fridays after 4; I've noted that.'). "
    "If it is NOT in KNOWN_SO_FAR, hedge honestly without denying: "
    "'Let me make sure I have that right — when works best for you?' "
    "NEVER say 'No, you haven't told me' or 'You didn't share that' or any equivalent denial."
)

# CB-76 (d) CONTACT ACK: injected when belief.meta['contact_just_captured'] is True (the DST sets
# this one-shot flag when the turn delivers name/phone/email). The reply MUST open with a short,
# warm acknowledgment that names the person if known and references the contact info captured, then
# proceeds with the decided act. This rides ANY act — confirm, answer, pitch, close — so the
# acknowledgment is never skipped because the routing chose a non-confirm act.
_CONTACT_ACK_NOTE = (
    "CONTACT ACK (do not skip): the caller JUST gave their contact info (name, phone, or email). "
    "Open your reply with a SHORT, warm acknowledgment — e.g. 'Got it{name_clause} — I've noted "
    "that down.' (one short clause, not a full sentence). Then continue with your response. "
    "Do NOT skip or defer this acknowledgment regardless of the decided act."
)

# CB-64 (repetition residual): stop-words stripped before comparing a new reply against own_last_lines
# to detect near-verbatim repetition. Mirrors _RECUR_STOPWORDS in gates.py (CB-48 recurrence logic).
_REPETITION_STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are", "do", "does", "did",
    "can", "could", "you", "your", "i", "we", "he", "she", "it", "they", "me", "my", "him", "her",
    "this", "that", "what", "how", "when", "why", "who", "which", "will", "would", "so", "just",
    "not", "or", "really", "actually", "tell", "give", "with", "at", "be", "have", "has", "out",
    "ll", "re", "ve", "s", "t", "let", "get",
})

# The minimum shared-content-word count that constitutes near-verbatim repetition (CB-64). Chosen
# at >8 words so: (a) the QA6 defect pairs (reply-1 vs reply-3, "connect you with my team…exact
# pricing…fifteen minutes" re-pitched verbatim) are reliably caught (they share 10+ content words);
# (b) short legitimately-similar acks like "That makes sense" / "I understand" (≤3 content words
# shared) are NOT flagged — the threshold is well above their overlap.
_REPETITION_WORD_THRESHOLD = 8

# Regex for tokenizing content words from a reply string (lowercase alphanum+apostrophe).
_CONTENT_WORD_RE = re.compile(r"[a-z0-9']+")


def _content_words_nlg(text: str) -> set[str]:
    """Lower-cased content words of `text`, stripped of stop-words (CB-64 near-verbatim detection)."""
    words = _CONTENT_WORD_RE.findall(text.lower())
    return {w for w in words if w not in _REPETITION_STOPWORDS and len(w) > 1}


def _near_verbatim_repetition(reply: str, own_last_lines: Optional[Sequence[Any]]) -> bool:
    """True when `reply` shares >_REPETITION_WORD_THRESHOLD content words with ANY of own_last_lines.

    CB-64: the QA6 defect was a near-verbatim re-pitch delivered on turn 3 even though turns 1 and 3
    were both inside the YOUR_LAST_LINES n=4 window and the _NO_RESTATE_INSTRUCTION was present —
    the instruction alone was ignored. This deterministic post-NLG check catches what the prompt
    instruction missed: >8 shared content words between the new reply and any prior agent line is an
    objective near-verbatim hit, triggering a blocking-path regenerate (or a stream-path flag).
    Short acks (≤3 shared words) are never flagged; the threshold is calibrated to catch pitch-level
    repetition only. Returns False when own_last_lines is empty/absent (turn 1 has nothing to compare)
    or the reply itself is short (fewer than _REPETITION_WORD_THRESHOLD+1 content words)."""
    if not own_last_lines:
        return False
    reply_words = _content_words_nlg(reply)
    if len(reply_words) <= _REPETITION_WORD_THRESHOLD:
        return False  # too short to flag — a short reply cannot be a verbatim re-pitch
    for prior in own_last_lines:
        prior_words = _content_words_nlg(str(prior))
        shared = reply_words & prior_words
        if len(shared) > _REPETITION_WORD_THRESHOLD:
            return True
    return False


# CB-64: the hardened no-repetition re-instruction used on the blocking-path regenerate when
# _near_verbatim_repetition() fires. Stronger and more direct than _NO_RESTATE_INSTRUCTION so the
# second attempt genuinely advances instead of re-pitching. Mirrors _GROUNDING_RETRY_RULE's pattern
# (explain what went wrong + give the corrected directive) so both safeguards share a consistent
# shape. Never used on the streaming path (can't un-speak a token — that path flags instead).
_REPETITION_RETRY_RULE = (
    "Your previous reply repeated an offer or point you already made. REWRITE it: do NOT re-pitch "
    "the same callback offer, team connection, or fifteen-minute framing you already said. Instead, "
    "either (a) add a NEW, concrete piece of information (a different benefit, a specific detail the "
    "prospect hasn't heard) or (b) advance the conversation with a direct next-step question. "
    "Abbreviate the earlier offer if you must reference it ('like I mentioned, the team call —') "
    "rather than repeating it in full. One sentence that moves forward is better than a re-pitch."
)


def _own_lines_block(own_last_lines: Optional[Sequence[Any]]) -> str:
    """Render the agent's OWN last 1-2 spoken lines into a YOUR_LAST_LINES block, or "" if none (CB-48).

    ONLY the agent's own lines are surfaced (the caller transcript is intentionally never passed in) so
    the distillation is preserved — no new inventable facts enter the prompt, only what the agent itself
    already said, so the model can avoid restating it. Empty/blank lines are dropped; nothing is shown
    when there is no prior agent line (turn 1), keeping the opening turn's prompt unchanged."""
    if not own_last_lines:
        return ""
    lines = [str(s).strip() for s in own_last_lines if str(s).strip()]
    if not lines:
        return ""
    rendered = "\n".join(f"- {ln}" for ln in lines)
    return "YOUR_LAST_LINES (your own recent turns — do not repeat them):\n" + rendered


def _build_messages(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    retrieved_facts: Optional[Sequence[Any]],
    *,
    own_last_lines: Optional[Sequence[Any]] = None,
    grounding_retry: bool = False,
    repetition_retry: bool = False,
) -> list[Message]:
    """Assemble persona + act-specific + grounding messages for the realization call.

    own_last_lines (CB-48) are the agent's OWN last 1-2 spoken turns (threaded from respond/
    respond_stream). When present they are surfaced as YOUR_LAST_LINES plus a do-not-restate
    instruction so NLG stops repeating the same hedge/offer; only the agent's own lines are passed
    (never the caller transcript), preserving the distillation.

    SAFEGUARD 2: the grounding block is included for a CLAIM act (answer_via_kb/handle_objection/pitch)
    OR whenever facts are present; `grounding_retry` (the blocking-path regenerate after grounded()
    flagged the first reply) hardens it. A bare ask/confirm/escalate with no facts gets no grounding
    block (its prompt is unchanged).

    CB-62: when the prospect asked a direct price question (last_user_act == "price_inquiry"), the
    _BUDGET_CONCERN_NOTE instruction is injected so the model acknowledges any stated budget concern
    before pivoting and uses the KB-grounded ballpark from the facts block (no hardcoded price).

    CB-64: `repetition_retry=True` (the blocking-path regenerate after _near_verbatim_repetition()
    fired) prepends _REPETITION_RETRY_RULE so the second attempt genuinely advances instead of
    re-pitching. Never used on the streaming path.

    CB-76 (c): when last_user_act == 'memory_check', _NEVER_DENY_NOTE is injected — the agent must
    never assert the caller did not say something; confirm from KNOWN_SO_FAR or hedge honestly.

    CB-76 (d): when belief.meta['contact_just_captured'] is True, _CONTACT_ACK_NOTE is injected so
    the reply opens with a warm one-clause contact acknowledgment, regardless of the gated act.

    CB-85 (a): when last_user_act == 'social_aside', _SOCIAL_ASIDE_NOTE is injected — the agent must
    give a brief ≤6-word human acknowledgment then redirect; NEVER interpret the aside as consent.

    CB-85 (b): _BUDGET_CONCERN_NOTE explicitly bans "I don't have access to pricing" when facts carry
    a range — the agent must state the KB-grounded numbers, never claim it lacks them."""
    guidance = _ACT_GUIDANCE.get(decision.act, "Respond helpfully and warmly.")
    if decision.act == "attempt_close" and decision.tier in _TIER_GUIDANCE:
        guidance = f"{guidance} {_TIER_GUIDANCE[decision.tier]}"

    known = {
        name: slot.get("value")
        for name, slot in belief.slots.items()
        if slot.get("value") is not None
    }
    # Surface what the prospect actually asked so answer_via_kb answers THAT (not "remind me what you
    # wanted to know") and handle_objection addresses the specific objection.
    open_q = getattr(belief, "open_question", None)
    own_block = _own_lines_block(own_last_lines)

    # CB-62: inject the budget-concern ack + KB-ballpark instruction whenever the prospect has asked
    # a direct price question. The price figure itself MUST come from retrieved_facts (grounding
    # constraint preserved); this instruction only sets the framing and tone. Applies to answer_via_kb
    # on a price_inquiry (the routing address_direct_input produces); also fires on handle_objection
    # (a price objection deserves the same honest ack). Does NOT fire on a bare pitch or close — those
    # are agent-initiated, not a direct price question.
    is_price_inquiry = (
        getattr(belief, "last_user_act", None) == "price_inquiry"
        or "price" in str(getattr(belief, "open_question", "") or "").lower()
        or "cost" in str(getattr(belief, "open_question", "") or "").lower()
        or "how much" in str(getattr(belief, "open_question", "") or "").lower()
    )
    price_acts = frozenset({"answer_via_kb", "handle_objection"})
    budget_concern_block = (
        _BUDGET_CONCERN_NOTE
        if (is_price_inquiry and decision.act in price_acts)
        else ""
    )

    # CB-64 repetition retry: prepend the hard no-repetition re-instruction FIRST so the regenerate
    # attempt sees it before any other guidance. Placed ahead of the grounding block so both safeguards
    # can fire on the same turn without conflicting (grounding checks facts; repetition checks phrasing).
    repetition_block = _REPETITION_RETRY_RULE if repetition_retry else ""

    # CB-85 (a): SOCIAL ASIDE RULE — inject when the caller's turn is an off-topic social remark.
    # The instruction enforces the acknowledge-then-redirect shape and explicitly forbids treating the
    # aside as scheduling consent. Runs on ANY act (including attempt_close, which the DST-level gate
    # already blocks, but the NLG guard is a second layer).
    last_act = getattr(belief, "last_user_act", None)
    social_aside_block = _SOCIAL_ASIDE_NOTE if last_act == "social_aside" else ""

    # CB-76 (c): NEVER-DENY RULE — inject when the caller asked if they already told us something.
    never_deny_block = _NEVER_DENY_NOTE if last_act == "memory_check" else ""

    # CB-76 (d): CONTACT ACK — inject when the DST flagged that this turn captured contact info.
    contact_captured = bool((belief.meta or {}).get("contact_just_captured"))
    if contact_captured:
        contact_name = (belief.meta or {}).get("contact_name", "")
        name_clause = f", {contact_name}" if contact_name else ""
        contact_ack_block = _CONTACT_ACK_NOTE.replace("{name_clause}", name_clause)
    else:
        contact_ack_block = ""

    parts = [
        # CB-64: repetition retry instruction (only on the second attempt — empty string otherwise).
        repetition_block,
        f"NEXT_ACT: {decision.act}",
        f"TARGET_SLOT: {decision.target_slot}" if decision.target_slot else "",
        f"COMMITMENT_TIER: {decision.tier}" if decision.tier else "",
        f"WHAT_TO_DO: {guidance}",
        f"PROSPECT_ASKED: {open_q!r}" if open_q else "",
        f"KNOWN_SO_FAR: {known}" if known else "",
        f"ACTIVE_OBJECTION: {belief.active_objection}" if belief.active_objection else "",
        # CB-62: budget-concern ack instruction (only on a direct price question — empty otherwise).
        budget_concern_block,
        # CB-85 (a): social aside rule (only on a social_aside turn — empty otherwise).
        social_aside_block,
        # CB-76 (c): never-deny rule (only on a memory_check turn — empty otherwise).
        never_deny_block,
        # CB-76 (d): contact acknowledgment instruction (only when contact was just captured).
        contact_ack_block,
        # CB-48: the agent's own prior lines + a no-restate instruction (only when there are lines, so
        # the opening turn's prompt is unchanged).
        own_block,
        _NO_RESTATE_INSTRUCTION if own_block else "",
        _grounding_block(
            retrieved_facts, is_claim=decision.act in _CLAIM_ACTS, retry=grounding_retry
        ),
        "Write ONLY the agent's next spoken line (1-3 short sentences). No prefaces, no quotes.",
    ]
    user = "\n".join(p for p in parts if p)
    return [
        {"role": "system", "content": _persona_system(config)},
        {"role": "user", "content": user},
    ]


async def realize(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    llm_client: LLMClient,
    *,
    retrieved_facts: Optional[Sequence[Any]] = None,
    own_last_lines: Optional[Sequence[Any]] = None,
    grounding_retry: bool = False,
    repetition_retry: bool = False,
) -> str:
    """Realize a gated Decision into one persona-consistent, voice-shaped reply string.

    The decision has ALREADY passed the gates (policy returns the gated decision), so NLG only has
    to phrase it. When retrieved_facts is supplied (U5 KB grounding) the prompt constrains the reply
    to those facts. own_last_lines (CB-48) carries the agent's own last 1-2 spoken turns so the prompt
    can forbid restating them. SAFEGUARD 2: `grounding_retry=True` is the blocking-path REGENERATE
    after retriever.grounded() flagged the first reply as ungrounded — it hardens the grounding rule so
    the second attempt states only supported claims (or says it lacks the figure). CB-64:
    `repetition_retry=True` is the blocking-path REGENERATE after _near_verbatim_repetition() fired
    — it prepends _REPETITION_RETRY_RULE so the second attempt genuinely advances instead of re-pitching.
    The reply is whitespace-trimmed; a blank LLM reply — OR any call failure (a real OpenRouter
    429/5xx/timeout that outlived the client's bounded retry, FINDING 2) — degrades to a safe filler so
    the caller always gets a non-empty turn instead of a crash.
    """
    messages = _build_messages(
        decision, belief, config, retrieved_facts,
        own_last_lines=own_last_lines, grounding_retry=grounding_retry,
        repetition_retry=repetition_retry,
    )
    try:
        text = await llm_client.complete(messages)
    except Exception:
        # FINDING 2: a failed realization call must not crash the turn — fall back to safe filler.
        text = ""
    reply = (text or "").strip()
    if not reply:
        return _SAFE_FILLER
    return reply


async def realize_stream(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    llm_client: LLMClient,
    *,
    retrieved_facts: Optional[Sequence[Any]] = None,
    own_last_lines: Optional[Sequence[Any]] = None,
) -> AsyncIterator[str]:
    """STREAM a gated Decision into a persona-consistent reply, yielding tokens as they generate.

    CB-41 — the streaming twin of realize(). It builds the EXACT SAME prompt _build_messages()
    produces (so the decided content is identical to the blocking path) and yields each content token
    from llm_client.complete_stream in order. The concatenation of every yielded token is the reply
    (the voice worker accumulates them and the committed Turn.text is that assembly — R37 parity).
    own_last_lines (CB-48) is threaded identically to realize() so the streamed prompt forbids the same
    restatement (R37: respond + respond_stream pass the SAME own-lines).

    Robustness mirrors realize(): if the stream errors before ANY token is produced — or produces only
    whitespace — we yield the SAME safe filler the blocking path falls back to, so the caller always
    gets a non-empty turn. A failure AFTER tokens have flowed is swallowed (the partial reply already
    streamed to TTS); we do not inject filler mid-reply. Leading whitespace on the very first token is
    trimmed so the reply does not start with stray spaces (matching realize()'s .strip() on the head),
    while interior/trailing whitespace is preserved so the assembled text reads naturally.
    """
    messages = _build_messages(
        decision, belief, config, retrieved_facts, own_last_lines=own_last_lines
    )
    produced_any = False
    seen_nonspace = False
    try:
        async for token in llm_client.complete_stream(messages):
            if not token:
                continue
            chunk = token
            if not seen_nonspace:
                # Trim leading whitespace until the first non-space token (head .strip() parity).
                chunk = chunk.lstrip()
                if not chunk:
                    continue
                seen_nonspace = True
            produced_any = True
            yield chunk
    except Exception:
        # A failure BEFORE any token -> degrade to the safe filler (parity with realize()'s except).
        # A failure AFTER tokens flowed -> swallow (the partial already streamed); do not add filler.
        if produced_any:
            return
    if not produced_any:
        # Blank/failed stream: emit the safe filler so the turn is never empty (FINDING 2 parity).
        yield _SAFE_FILLER
