# Natural-language generation (plan U4): realize a GATED Decision into a persona-consistent,
# voice-shaped reply. realize(decision, belief, config, llm_client, *, retrieved_facts=None) builds
# a persona + act-specific prompt (warm consultative advisor, low-pressure) and asks the llm_client
# for one concise spoken-style turn. If retrieved_facts is given (KB grounding, wired in U5), the
# reply is constrained to those facts (state no fact absent from them). Decouples strategy (policy)
# from surface text (CraigslistBargain policy<->NLG split). Pure + async, NO LiveKit imports.
from __future__ import annotations

from typing import Any, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient, Message

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


def _persona_system(config: AgentConfig) -> str:
    """The persona system prompt: prefer the authored NLG prompt + persona, else a safe fallback."""
    persona = config.persona
    persona_line = (
        f"Your name is {persona.name}, a {persona.role}. Style: {persona.style}."
        if persona
        else ""
    )
    authored = config.prompts.get("nlg") or ""
    base = "\n".join(p for p in (_PERSONA_FALLBACK, persona_line, authored) if p).strip()
    return base


def _grounding_block(retrieved_facts: Optional[Sequence[Any]]) -> str:
    """Render retrieved KB facts (U5) into a grounding constraint, or an empty string if none."""
    if not retrieved_facts:
        return ""
    facts = "\n".join(f"- {str(f)}" for f in retrieved_facts)
    return (
        "GROUNDING — you may ONLY state facts supported by these retrieved KB snippets; "
        "do not invent specifics (prices, guarantees, claims) absent here:\n" + facts
    )


def _build_messages(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    retrieved_facts: Optional[Sequence[Any]],
) -> list[Message]:
    """Assemble persona + act-specific + grounding messages for the realization call."""
    guidance = _ACT_GUIDANCE.get(decision.act, "Respond helpfully and warmly.")
    if decision.act == "attempt_close" and decision.tier in _TIER_GUIDANCE:
        guidance = f"{guidance} {_TIER_GUIDANCE[decision.tier]}"

    known = {
        name: slot.get("value")
        for name, slot in belief.slots.items()
        if slot.get("value") is not None
    }
    parts = [
        f"NEXT_ACT: {decision.act}",
        f"TARGET_SLOT: {decision.target_slot}" if decision.target_slot else "",
        f"COMMITMENT_TIER: {decision.tier}" if decision.tier else "",
        f"WHAT_TO_DO: {guidance}",
        f"KNOWN_SO_FAR: {known}" if known else "",
        f"ACTIVE_OBJECTION: {belief.active_objection}" if belief.active_objection else "",
        _grounding_block(retrieved_facts),
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
) -> str:
    """Realize a gated Decision into one persona-consistent, voice-shaped reply string.

    The decision has ALREADY passed the gates (policy returns the gated decision), so NLG only has
    to phrase it. When retrieved_facts is supplied (U5 KB grounding) the prompt constrains the reply
    to those facts. The reply is whitespace-trimmed; a blank LLM reply — OR any call failure (a real
    OpenRouter 429/5xx/timeout that outlived the client's bounded retry, FINDING 2) — degrades to a
    safe filler so the caller always gets a non-empty turn instead of a crash.
    """
    messages = _build_messages(decision, belief, config, retrieved_facts)
    try:
        text = await llm_client.complete(messages)
    except Exception:
        # FINDING 2: a failed realization call must not crash the turn — fall back to safe filler.
        text = ""
    reply = (text or "").strip()
    if not reply:
        return "Let me make sure I'm helping you the right way here."
    return reply
