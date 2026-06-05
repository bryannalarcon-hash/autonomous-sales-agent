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
from __future__ import annotations

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
    "confirm_known": "Briefly confirm what we already know (don't re-ask it) and move forward warmly.",
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
) -> list[Message]:
    """Assemble persona + act-specific + grounding messages for the realization call.

    own_last_lines (CB-48) are the agent's OWN last 1-2 spoken turns (threaded from respond/
    respond_stream). When present they are surfaced as YOUR_LAST_LINES plus a do-not-restate
    instruction so NLG stops repeating the same hedge/offer; only the agent's own lines are passed
    (never the caller transcript), preserving the distillation.

    SAFEGUARD 2: the grounding block is included for a CLAIM act (answer_via_kb/handle_objection/pitch)
    OR whenever facts are present; `grounding_retry` (the blocking-path regenerate after grounded()
    flagged the first reply) hardens it. A bare ask/confirm/escalate with no facts gets no grounding
    block (its prompt is unchanged)."""
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
    parts = [
        f"NEXT_ACT: {decision.act}",
        f"TARGET_SLOT: {decision.target_slot}" if decision.target_slot else "",
        f"COMMITMENT_TIER: {decision.tier}" if decision.tier else "",
        f"WHAT_TO_DO: {guidance}",
        f"PROSPECT_ASKED: {open_q!r}" if open_q else "",
        f"KNOWN_SO_FAR: {known}" if known else "",
        f"ACTIVE_OBJECTION: {belief.active_objection}" if belief.active_objection else "",
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
) -> str:
    """Realize a gated Decision into one persona-consistent, voice-shaped reply string.

    The decision has ALREADY passed the gates (policy returns the gated decision), so NLG only has
    to phrase it. When retrieved_facts is supplied (U5 KB grounding) the prompt constrains the reply
    to those facts. own_last_lines (CB-48) carries the agent's own last 1-2 spoken turns so the prompt
    can forbid restating them. SAFEGUARD 2: `grounding_retry=True` is the blocking-path REGENERATE
    after retriever.grounded() flagged the first reply as ungrounded — it hardens the grounding rule so
    the second attempt states only supported claims (or says it lacks the figure). The reply is
    whitespace-trimmed; a blank LLM reply — OR any call failure (a real OpenRouter 429/5xx/timeout that
    outlived the client's bounded retry, FINDING 2) — degrades to a safe filler so the caller always
    gets a non-empty turn instead of a crash.
    """
    messages = _build_messages(
        decision, belief, config, retrieved_facts,
        own_last_lines=own_last_lines, grounding_retry=grounding_retry,
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
