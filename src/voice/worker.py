# The RUNNABLE LiveKit voice worker (plan U12, Phase 3 — the last unwired piece). A worker process
# that registers with LiveKit Cloud, is dispatched into the caller's /demo room, and pipes
# ElevenLabs Scribe v2 STT -> OUR brain (src.voice.session.VoiceSession via build_voice_agent) ->
# ElevenLabs TTS, so a caller HEARS the agent and SEES their transcript. PARITY (R37): the reply the
# caller hears is the one respond() already computed in the VoiceSession — the LiveKit `llm_node` only
# SURFACES that buffered reply, it never re-decides. CRITICAL WIRING: AgentSession is given an inert
# pass-through LLM (_passthrough_llm) because livekit-agents SKIPS reply generation entirely when
# `AgentSession.llm is None` (agent_activity: `elif self.llm is None: return`) — without it the brain
# ran but llm_node was never called, so every answer was DEAD SILENCE on a live call. GROUNDING
# (U5/R43): the session threads the SAME build_live_retrieve_hook the text demo uses, so voice answers
# cite the ingested KB.
# COMPLIANCE (U13/R33/R40/R41): the entrypoint builds a ConsentGate (jurisdiction from room metadata,
# RefusalPolicy from env), threads it into the VoiceSession via build_voice_agent (so every turn is
# gated by VoiceSession._require_consent EXACTLY like /api/chat), and SPEAKS the AI+recording
# disclosure. It SEEDS that gate from the consent the BROWSER already captured + carried on the room
# metadata (_seed_gate_from_metadata): recording granted -> ready, refused-but-proceeding ->
# unrecorded, unresolved minor -> stays blocked — so can_converse is True from the first turn and the
# brain answers (the U13 deadlock fix).
# SIP / PHONE path (no browser to click consent): when _seed_gate_from_metadata returns False AND a
# remote participant is a SIP caller (_is_sip_call -> ParticipantKind.SIP), the worker arms an
# IVR-style SPOKEN consent (WorkerVoiceAgent.arm_spoken_consent): the FIRST user turn(s) are
# intercepted by handle_consent_turn (NOT run through the brain) and classified by
# _classify_consent_reply — "yes" grants (ready + recorded), "no" refuses (unrecorded or end per
# RefusalPolicy), a minor-indicating reply flags need_parental (blocked), "unclear" re-asks ONCE then
# politely ends. Once the gate reaches can_converse, subsequent turns route to the brain as normal —
# so a phone call works end-to-end without deadlocking on a `pending` gate, while still fail-closed
# (a turn before consent resolves never reaches the brain / is never recorded).
# PERSISTENCE (U2/U15/Layer2): at room close the finished VoiceSession is saved via
# src.api.persistence.persist_call_end (channel="voice") with the bound phone-hash, any escalations
# captured DURING the call, and the last close tier — so voice calls flow into /operate exactly like
# text calls. CB-09: a call that DISCONNECTS without ever reaching a terminal act (close/escalate/
# disqualify) used to persist outcome=in_progress FOREVER and vanish from the Calls list; the shutdown
# now maps such a call to a TERMINAL outcome via _terminal_outcome_on_disconnect ("abandoned" for a
# 0-turn hang-up, "released" for a substantive no-close call) and re-saves the same row, so a hung-up
# call is visible. LIVE PERSISTENCE (Layer 2): a stable live_episode_id is generated at call start; after
# each committed agent turn _upsert_live_voice writes an in_progress Episode via persist_call_live
# using that stable id so the Live monitor can query voice calls mid-call; _register_persistence
# finalizes with the same id so no orphan row is created.
#
# HEAVY-IMPORT ISOLATION: the livekit-agents CORE imports are import-guarded (so this module imports
# cleanly in the livekit-FREE test suite/CI, with Agent aliased to object and WorkerVoiceAgent still
# constructible); the heavy elevenlabs/silero PLUGIN imports live INSIDE entrypoint so installing the
# plugins never leaks into the offline suite. Nothing in src/core, src/voice/__init__, or the brain
# core imports this file. Run it with:
#     PYTHONPATH=. python3 -m src.voice.worker dev      # connects + registers to LIVEKIT_URL
# Built against livekit-agents 1.5.15 (AgentSession / WorkerOptions / cli.run_app / JobContext APIs
# were read from the installed package, not guessed): on_user_turn_completed(turn_ctx, new_message)
# runs the brain + buffers the reply, the AgentSession (with a non-None pass-through llm) then calls
# llm_node which returns that buffered str AND COMMITS the turn (the reliable state write — the
# SpeechHandle.add_done_callback path did not fire on live calls, so turns/belief never persisted;
# llm_node provably runs every turn). The done-callback is kept as a redundant barge-in backstop
# (commit_turn is idempotent). Committing in llm_node means a post-reply barge-in no longer discards.
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

from src.config.settings import AgentConfig, load_config
from src.core.llm import OpenRouterClient
from src.kb.embeddings import EmbeddingModel, SentenceTransformerEmbedder
from src.kb.live import build_live_retrieve_hook
from src.memory.schema import phone_hash as compute_phone_hash
from src.voice.agent import AGENT_MODEL, STT_MODEL_ID, build_voice_agent
from src.voice.consent import NEED_PARENTAL, ConsentGate, RefusalPolicy, detect_minor
from src.voice.session import VoiceSession

# --- Import guard for the livekit-agents CORE -----------------------------------------------------
# The worker ALWAYS needs these at runtime, but the offline test suite (which import-guards livekit)
# imports THIS module to cover WorkerVoiceAgent. So we guard the core imports exactly like
# src.voice.agent does: when livekit-agents is absent, Agent aliases to object (WorkerVoiceAgent stays
# constructible/unit-testable) and the worker-only symbols are None (entrypoint/_worker_options are
# never reached in that environment). The HEAVY elevenlabs/silero plugin imports stay inside
# entrypoint, so neither importing this module nor constructing the agent ever loads a media plugin.
try:  # pragma: no cover - exercised only when livekit-agents is installed
    from livekit.agents import (
        Agent,
        AgentSession,
        AutoSubscribe,
        JobContext,
        SpeechCreatedEvent,
        WorkerOptions,
        cli,
    )
    from livekit.agents import llm as lkllm
    from livekit.agents import DEFAULT_API_CONNECT_OPTIONS

    LIVEKIT_AVAILABLE = True
except ImportError:  # the offline-suite case: livekit-agents not installed
    Agent = object  # type: ignore[assignment,misc]
    AgentSession = Any  # type: ignore[assignment,misc]
    AutoSubscribe = None  # type: ignore[assignment]
    JobContext = Any  # type: ignore[assignment,misc]
    SpeechCreatedEvent = Any  # type: ignore[assignment,misc]
    WorkerOptions = None  # type: ignore[assignment]
    cli = None  # type: ignore[assignment]
    lkllm = None  # type: ignore[assignment]
    DEFAULT_API_CONNECT_OPTIONS = None  # type: ignore[assignment]
    LIVEKIT_AVAILABLE = False

logger = logging.getLogger("voice.worker")

# Which config version the live worker runs (the brain's prompts/playbooks/thresholds + pinned
# kb_version). Overridable via env so a demo can pin a specific champion/challenger.
WORKER_CONFIG_VERSION = os.environ.get("WORKER_CONFIG_VERSION", "champion_v0")

# Default consent jurisdiction when the room carries none (R33). A sane default keeps recording GATED
# regardless of jurisdiction (recording only happens after explicit consent); the jurisdiction only
# tunes the disclosure emphasis (all-party vs one-party). Overridable via env.
DEFAULT_JURISDICTION = os.environ.get("VOICE_DEFAULT_JURISDICTION", "")
# What to do if the caller REFUSES recording consent (R41): proceed UNRECORDED (default) or END.
_REFUSAL_POLICY = (
    RefusalPolicy.END
    if os.environ.get("VOICE_REFUSAL_POLICY", "").strip().lower() == "end"
    else RefusalPolicy.PROCEED_UNRECORDED
)

# Adaptive barge-in / turn config (R5/R10): let the caller interrupt the agent's TTS naturally and
# recover. Tunable via env without code changes. These are passed to AgentSession below.
_MIN_INTERRUPTION_DURATION = float(os.environ.get("VOICE_MIN_INTERRUPTION_DURATION", "0.5"))
_MIN_ENDPOINTING_DELAY = float(os.environ.get("VOICE_MIN_ENDPOINTING_DELAY", "0.4"))
_MAX_ENDPOINTING_DELAY = float(os.environ.get("VOICE_MAX_ENDPOINTING_DELAY", "6.0"))

# --- SIP spoken-consent: the IVR prompts + the deterministic reply classifier --------------------
# A PSTN caller has NO browser to click consent, so the worker captures recording consent BY VOICE.
# The first user turn(s) are routed to handle_consent_turn and classified here (no LLM): an
# affirmation -> "yes", a negation -> "no", anything ambiguous -> "unclear" (re-asked once).

# The recording-consent ask the agent SPEAKS at the top of a SIP call (after the AI disclosure).
# SELF-CONTAINED (AI disclosure + recording + the ask) so the SIP path speaks it ONCE — the
# entrypoint does NOT also say gate.disclosure_text on the SIP branch (that double-disclosed before).
SIP_CONSENT_PROMPT = (
    "Hi, you're speaking with an AI learning advisor for Nerdy. This call may be recorded for "
    "quality and training. Do you consent to this call being recorded? Please say yes or no."
)
# The single re-ask on an unclear first answer (then we end politely if still unclear).
SIP_CONSENT_REASK = (
    "Sorry, I didn't catch that. Do you consent to this call being recorded? "
    "Please say yes or no."
)
# Spoken hand-offs once consent resolves (recorded vs not) and the polite endings.
SIP_CONSENT_RECORDED = "Thank you. This call will be recorded. How can I help you today?"
SIP_CONSENT_UNRECORDED = "Thank you. This call will not be recorded. How can I help you today?"
SIP_CONSENT_ENDED = (
    "No problem — we won't proceed without your consent to record. Thanks for calling, goodbye."
)
SIP_CONSENT_MINOR = (
    "Thanks for calling. Because this may involve a minor, we'll need a parent or guardian's "
    "consent before we can continue. Please have them join or call back. Goodbye."
)

# Affirmation / negation cues for the consent reply. Negation is checked FIRST so "no thanks" and
# "I do not consent" never read as the affirmative "consent". Word-boundaried so "nope" matches but
# "another" does not falsely hit "no".
_CONSENT_NO_RE = re.compile(
    r"\b(no|nope|nah|don'?t|do not|never|stop|refuse|decline|rather not|"
    r"negative|disagree|reject|won'?t)\b",
    re.IGNORECASE,
)
_CONSENT_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|consent|agree|agreed|fine|"
    r"go ahead|of course|absolutely|definitely|please do|that'?s fine|sounds good|affirmative)\b",
    re.IGNORECASE,
)

# The livekit.rtc ParticipantKind value for a SIP (PSTN) caller (verified against installed
# livekit-rtc: PARTICIPANT_KIND_SIP == 3). Kept as a literal so this module stays livekit-free /
# import-guard-clean; the worker only reads the numeric `.kind` off a remote participant.
PARTICIPANT_KIND_SIP = 3


def _classify_consent_reply(text: str) -> str:
    """Classify a spoken recording-consent reply as "yes" | "no" | "unclear" (deterministic, no LLM).

    Negation is matched FIRST (so "no thanks" / "I do not consent" / "I'd rather not" are firmly
    "no") before the affirmation pass, so a reply that contains BOTH (e.g. "no") never reads as yes.
    Empty / question / pure-filler replies are "unclear" so the worker re-asks once rather than
    guessing. Robust to punctuation/case; word-boundaried to avoid false hits (e.g. "another" != no).
    """
    t = (text or "").strip()
    if not t:
        return "unclear"
    if _CONSENT_NO_RE.search(t):
        return "no"
    if _CONSENT_YES_RE.search(t):
        return "yes"
    return "unclear"


# --- Terminal outcome on participant-disconnect (CB-09) --------------------------------------------
# A real inbound call that disconnects mid-call (LiveKit `participant disconnect`, reason
# CLIENT_INITIATED) was persisted with outcome=in_progress FOREVER, because no terminal agent act
# (attempt_close / escalate / disqualify) ever fired. The Calls list (/api/episodes via
# operate._is_completed) filters OUT in_progress episodes, so a hung-up call was INVISIBLE. This pure,
# livekit-free mapping assigns a TERMINAL outcome when the call ended WITHOUT a terminal act, using the
# SAME canonical outcome vocabulary the sim already emits (selfplay._RELEASED) plus "abandoned" (a
# 0-turn hang-up). A hang-up is NEVER a positive outcome — that is the whole point: it only maps the
# NON-terminal case; a call that DID reach a terminal act keeps the outcome derive_outcome computed.
#
# - "abandoned": the caller dropped with ~no conversation (no committed turns). Renders as "Abandoned"
#   via src.api.labels.OUTCOME_LABEL (added there so no bare slug leaks to the operator).
# - "released": a substantive conversation happened (>=1 committed turn) but no close/escalate/dq —
#   the same terminal the self-play loop uses for "ran out of runway without a commitment".

# A LAST agent decision act that already means a TERMINAL outcome (persistence.derive_outcome maps each
# to enrolled/booked/escalated/disqualified). When the last act is one of these, the call ALREADY has a
# terminal outcome, so the disconnect mapping must NOT override it.
_TERMINAL_ACTS = frozenset({"attempt_close", "escalate", "disqualify"})


def _terminal_outcome_on_disconnect(
    *,
    turn_count: int,
    escalated: bool,
    committed_tier: Optional[str],
    last_act: Optional[str],
) -> Optional[str]:
    """Pick the TERMINAL outcome for a call that ended via participant-disconnect (CB-09).

    Returns None when the call ALREADY reached a terminal outcome (a terminal last act fired, an
    escalation was captured, or a close tier was committed) — the caller then keeps whatever
    persistence.derive_outcome produced (enrolled / *_booked / escalated / disqualified). For a
    NON-terminal disconnect it returns a terminal slug so the episode stops being in_progress:
      - 0 committed turns  -> "abandoned" (the caller dropped during the opening, ~no conversation).
      - >=1 committed turn  -> "released" (a real exchange happened but no close — the same canonical
        terminal selfplay._RELEASED uses).
    A hang-up is NEVER mapped to a positive outcome (enrolled / consult_booked); those only arise from
    an actual terminal close act, which short-circuits this fn to None. Pure / stdlib-only so its unit
    test runs in the livekit-free .venv; both returned slugs have human labels in src.api.labels."""
    if last_act in _TERMINAL_ACTS or escalated or committed_tier:
        return None
    if turn_count <= 0:
        return "abandoned"
    return "released"


def _last_committed_act(turns: list[Any]) -> Optional[str]:
    """The act of the most recent committed AGENT turn (its `decision` string), or None.

    Mirrors persistence._last_agent_decision but kept livekit-free + local so the worker can feed
    _terminal_outcome_on_disconnect without importing a private helper across the persistence seam.
    A committed agent Turn carries the system act on `.decision` (schema.Turn); user turns have None."""
    for turn in reversed(turns or []):
        if getattr(turn, "speaker", None) == "agent" and getattr(turn, "decision", None):
            return turn.decision
    return None


def _is_sip_call(ctx: Any) -> bool:
    """True when a remote participant is a SIP (PSTN) caller — the no-browser path that needs SPOKEN
    consent. Reads the numeric `.kind` off each remote participant (livekit.rtc.RemoteParticipant.kind
    == ParticipantKind.PARTICIPANT_KIND_SIP, 3). Livekit-free: only the int is compared, tolerating
    a missing kind / empty participant map (-> False, the browser/web default)."""
    room = getattr(ctx, "room", None)
    participants = getattr(room, "remote_participants", None) or {}
    try:
        values = participants.values()
    except AttributeError:
        values = participants
    for p in values:
        kind = getattr(p, "kind", None)
        try:
            if int(kind) == PARTICIPANT_KIND_SIP:
                return True
        except (TypeError, ValueError):
            continue
    return False


class WorkerVoiceAgent(Agent):  # type: ignore[misc,valid-type]
    """The live LiveKit Agent for the worker — delegates EVERY brain decision to a VoiceSession.

    It wraps the SAME VoiceSession that build_voice_agent wires (so RAG grounding + respond() parity
    + the CONSENT GATE are identical to the text path); this subclass only re-expresses the
    delegation against the livekit-agents 1.5.x hook signatures (which differ from the older shape
    sketched in agent.py):

      on_user_turn_completed(turn_ctx, new_message) -> pull the user text from new_message, run the
        WHOLE respond() via voice_session.handle_user_turn (RAG + DST + decide + realize), buffer the
        speculative PendingTurn under a fresh speech_id. We do NOT commit here. The consent gate fires
        FIRST inside handle_user_turn: a not-yet-can_converse turn raises ConsentError (caught by the
        worker and turned into a disclosure ask), so the brain never runs / nothing is recorded.
      llm_node(...) -> yield the buffered reply_text for the pending speech_id. The brain ALREADY
        generated it, so this node SURFACES it (parity) and never calls an LLM itself.
      commit/discard -> wired by the worker via SpeechHandle.add_done_callback: when the agent's TTS
        finishes uninterrupted we commit_turn (the only state write) + capture escalations/close-tier;
        a barge-in (.interrupted) drops the speculative turn with NO committed-state write (R5/R10).
    """

    def __init__(self, base: Any) -> None:
        # `base` is the VoiceSalesAgent that build_voice_agent constructed (with persona instructions
        # + the wired VoiceSession + any ConsentGate). Re-init the real Agent base with those same
        # instructions and adopt its VoiceSession so the brain wiring (config/llm/embedder/retrieve-
        # hook/consent_gate) is reused verbatim — this subclass only swaps in the 1.5.x lifecycle
        # hooks. When livekit is absent Agent is object, so this is still constructible/unit-testable.
        if LIVEKIT_AVAILABLE:
            super().__init__(instructions=base.instructions)
        else:  # pragma: no cover - structural (object base takes no kwargs)
            super().__init__()
        self.voice_session: VoiceSession = base.voice_session
        self._pending_speech_id: Optional[str] = None
        self._turn_counter = 0
        # SIP spoken-consent state (U13 phone path): when armed (arm_spoken_consent), the FIRST user
        # turn(s) are intercepted by handle_consent_turn — NOT run through the brain — until recording
        # consent resolves the gate to can_converse (or ends it). False on the browser path (consent
        # was seeded from room metadata), so a web call proceeds to the brain on the first turn.
        self._awaiting_consent = False
        # Whether the unclear-reply re-ask has already been spent (we re-ask ONCE, then end).
        self._consent_reasked = False
        # Escalations captured DURING the call (re-pointed onto the episode at persist) + the last
        # committed close tier (the schema Turn carries no tier, so we track it for outcome mapping).
        self.escalations: list[Any] = []
        self.last_close_tier: Optional[str] = None
        # In-flight async escalation-handling tasks, awaited at shutdown before persisting.
        self._escalation_tasks: list[Any] = []
        # Stable live-persistence identifiers (Layer 2): generated ONCE at construction and reused on
        # every in_progress upsert AND the final _register_persistence call so both target the same row.
        self.live_episode_id: str = f"ep-{uuid.uuid4().hex}"
        self.live_created_at: datetime = datetime.now(timezone.utc)
        # Live AgentConfig, set by _build_brain. llm_node commits the turn (the reliable state write)
        # and needs config for _capture_post_commit (escalation handling + the live in_progress upsert).
        self._config: Optional[AgentConfig] = None

    # --- SIP spoken-consent sub-flow (the no-browser phone path) --------------------------------

    def arm_spoken_consent(self) -> str:
        """Arm the IVR-style spoken-consent capture for a SIP/phone call and return the prompt to SPEAK.

        Called by entrypoint ONLY when there was NO browser-captured consent (the gate is still
        `pending`) AND the caller is a SIP participant. While armed, on_user_turn_completed routes the
        first user turn(s) to handle_consent_turn instead of the brain. The browser/web path never
        calls this, so its first turn flows straight to the brain (the web-voice path is unchanged)."""
        self._awaiting_consent = True
        self._consent_reasked = False
        return SIP_CONSENT_PROMPT

    async def handle_consent_turn(self, text: str) -> Optional[str]:
        """Advance the ConsentGate from ONE spoken consent reply and return the next line to SPEAK.

        This is the SIP equivalent of the browser's consent click — the reply is NEVER run through the
        brain (no record/answer before consent). Classifies via _classify_consent_reply, also runs
        detect_minor on the reply so a school-aged self-referring caller is gated (COPPA/FERPA):
          - minor detected -> flag_minor() -> need_parental (BLOCKED, even on a "yes"); stop awaiting.
          - "yes" -> acknowledge_ai() + grant_recording() -> ready (recorded); stop awaiting.
          - "no"  -> refuse_recording() -> unrecorded or ended per RefusalPolicy; stop awaiting.
          - "unclear" -> re-ask ONCE (stay awaiting); a second unclear -> end() (no deadlock).
        Once this returns with _awaiting_consent False AND the gate can_converse, subsequent turns
        route to the brain. Returns the spoken hand-off / re-ask / ending line (None never expected)."""
        gate = self.voice_session.consent_gate
        if gate is None:  # pragma: no cover - entrypoint always arms with a gate attached
            self._awaiting_consent = False
            return None

        # Suspected-minor check on the captured reply FIRST (R40): a minor may not proceed unsupervised
        # regardless of what they answered, so this overrides a "yes".
        if detect_minor({"text": text}):
            gate.flag_minor()
            self._awaiting_consent = False
            return SIP_CONSENT_MINOR

        verdict = _classify_consent_reply(text)
        if verdict == "yes":
            gate.acknowledge_ai()
            gate.grant_recording()
            self._awaiting_consent = False
            return SIP_CONSENT_RECORDED
        if verdict == "no":
            gate.acknowledge_ai()
            gate.refuse_recording()  # -> unrecorded or ended per the gate's RefusalPolicy
            self._awaiting_consent = False
            return SIP_CONSENT_UNRECORDED if gate.can_converse else SIP_CONSENT_ENDED
        # Unclear: re-ask exactly once, then end politely (never loop forever / deadlock).
        if not self._consent_reasked:
            self._consent_reasked = True
            return SIP_CONSENT_REASK
        gate.end()
        self._awaiting_consent = False
        return SIP_CONSENT_ENDED

    async def on_user_turn_completed(
        self, turn_ctx: Any, new_message: Any
    ) -> None:
        """User finished speaking (final STT): capture spoken consent (SIP path) or run the brain.

        While _awaiting_consent (SIP/phone path with no browser-captured consent), the turn is the
        consent answer: route it to handle_consent_turn and SPEAK the result — the brain NEVER runs on
        it. Otherwise (consent already resolved, or the browser path), generate a fresh speech_id, run
        respond() through the session (consent-gated + RAG-grounded), and remember the speech_id so
        llm_node can surface the buffered reply. No committed-state write here."""
        user_text = (new_message.text_content or "").strip()
        if not user_text:
            return
        if self._awaiting_consent:
            # Intercept as the consent answer — do NOT run the brain or record this turn.
            reply = await self.handle_consent_turn(user_text)
            if reply is not None:
                await self._say_consent_line(reply)
            return
        self._turn_counter += 1
        speech_id = f"t-{self._turn_counter}"
        self._pending_speech_id = speech_id
        await self.voice_session.handle_user_turn(user_text, speech_id)

    async def _say_consent_line(self, text: str) -> None:
        """SPEAK a consent prompt/hand-off line via the running AgentSession (livekit), if available.

        The agent's real `session` is the running AgentSession; with no live session attached (unit
        tests, or before the AgentSession starts) the real livekit Agent's `session` PROPERTY RAISES
        (no activity), and with the object base there is none — both are treated as "no session, no-op"
        (the tests drive handle_consent_turn directly). Kept tiny + defensive so a missing/erroring
        session or a TTS hiccup never crashes the consent flow."""
        try:
            session = self.session  # real livekit Agent: property; raises if not running
        except Exception:  # no running AgentSession (tests / pre-start) -> nothing to speak through
            return
        say = getattr(session, "say", None)
        if say is None:  # pragma: no cover - exercised only with a live AgentSession attached
            return
        try:
            await say(text, allow_interruptions=False)
        except Exception:  # pragma: no cover - defensive: a TTS hiccup must not crash the call
            logger.warning("failed to speak consent line")

    async def llm_node(self, chat_ctx: Any, tools: Any, model_settings: Any) -> str:
        """Surface the brain's pre-computed reply for the current turn (PARITY — never re-decide).

        Returns the buffered reply_text the VoiceSession already produced in on_user_turn_completed.
        The AgentSession streams this string straight to TTS; no LLM is invoked in this node. Returns
        "" on the defensive None branches (no pending speech / its buffer already drained)."""
        sid = self._pending_speech_id
        if sid is None:
            return ""
        pending = self.voice_session.state.pending.get(sid)
        if pending is None:
            return ""
        reply = pending.reply_text
        # COMMIT HERE — the RELIABLE state write. The intended commit-on-uninterrupted-playout path
        # (_wire_commit_on_speech's SpeechHandle done-callback) did NOT fire on live calls, so turns
        # were never saved and the committed belief never advanced (every brain turn logged turn=1, and
        # calls saved an empty in_progress shell that never reached the Calls list / Live monitor).
        # llm_node provably runs every turn, so committing once the reply is surfaced guarantees the
        # turn is recorded + the Live monitor tracks it. commit_turn is idempotent (pops the pending),
        # so the done-callback's later commit is a harmless no-op. Trade-off: a barge-in AFTER the reply
        # starts no longer discards the turn — acceptable (keeping the transcript beats dropping it).
        committed = self.voice_session.commit_turn(sid)
        if committed is not None:
            logger.info("turn %s committed (llm_node)", sid)
            if self._config is not None:
                _capture_post_commit(self, committed, sid, self._config)
        return reply


def _passthrough_llm() -> Any:  # pragma: no cover - requires livekit-agents installed
    """Build the INERT pass-through LLM the AgentSession needs so its reply pipeline actually RUNS.

    THE live-call "dead silence" fix. livekit-agents SKIPS response generation entirely when
    `AgentSession.llm is None` — in agent_activity, right after awaiting on_user_turn_completed:
    `elif self.llm is None: return  # skip response if no llm is set`. Our brain runs INSIDE
    on_user_turn_completed and buffers the reply, but with no llm the pipeline returned BEFORE calling
    llm_node, so the buffered reply was never surfaced to TTS: the caller heard the consent line (an
    explicit session.say) and then nothing on every actual answer. This LLM exists ONLY to make
    `self.llm is not None` true so the pipeline proceeds to WorkerVoiceAgent.llm_node (which returns
    the brain's pre-computed reply). Its chat() is NEVER used for inference — R37 parity holds: the
    brain decides every word, this LLM contributes none. Defined lazily (lkllm is None offline)."""
    class _PassthroughStream(lkllm.LLMStream):  # type: ignore[misc,valid-type]
        async def _run(self) -> None:
            return  # emits no chunks; never reached — llm_node fully overrides generation.

    class _PassthroughLLM(lkllm.LLM):  # type: ignore[misc,valid-type]
        def chat(self, *, chat_ctx: Any, tools: Any = None,
                 conn_options: Any = DEFAULT_API_CONNECT_OPTIONS, **_: Any) -> Any:
            return _PassthroughStream(self, chat_ctx=chat_ctx, tools=tools or [],
                                      conn_options=conn_options)

    return _PassthroughLLM()


def _make_session(stt: Any, tts: Any, vad: Any, *, version: str = "",
                  kb_version: str = "") -> Any:  # pragma: no cover - real AgentSession needs livekit
    """Construct the AgentSession with the media stack + the REQUIRED non-None pass-through llm.

    Extracted as a seam so the wiring that matters most — `llm=_passthrough_llm()` is present — is
    unit-asserted (test_worker): livekit-agents SILENTLY skips reply generation when llm is None, so
    dropping the llm here would make the agent stop speaking on every turn. Barge-in / endpointing
    knobs (R5/R10) are applied here too. The brain still authors every reply (R37 parity); the llm is
    inert (never invoked for inference)."""
    return AgentSession(
        stt=stt,
        tts=tts,
        vad=vad,
        llm=_passthrough_llm(),
        userdata={"version": version, "kb_version": kb_version},
        allow_interruptions=True,
        min_interruption_duration=_MIN_INTERRUPTION_DURATION,
        min_endpointing_delay=_MIN_ENDPOINTING_DELAY,
        max_endpointing_delay=_MAX_ENDPOINTING_DELAY,
        # MUST stay off: with preemptive generation, livekit creates the reply SpeechHandle BEFORE
        # on_user_turn_completed sets _pending_speech_id, so _wire_commit_on_speech sees None and never
        # attaches the done-callback -> commit_turn never fires -> NO turns persist and the committed
        # belief never advances (every brain turn logged turn=1, and a real call saved an empty
        # in_progress shell that never reached the Calls list). Sequential generation fixes that.
        preemptive_generation=False,
    )


def _build_brain(config: AgentConfig, *, consent_gate: ConsentGate,
                 lead_phone_hash: Optional[str] = None,
                 embedder: Optional[EmbeddingModel] = None) -> WorkerVoiceAgent:
    """Build the worker's Agent around OUR brain — REUSING build_voice_agent so the VoiceSession is
    wired EXACTLY like the text path (parity), GROUNDED via build_live_retrieve_hook (U5), and
    CONSENT-GATED via the supplied ConsentGate (U13 — the blocking finding's fix).

    The real OpenRouterClient(model=AGENT_MODEL) is the brain; a real SentenceTransformerEmbedder +
    the live retrieve hook (pinned to the config's kb_version) give voice answers the same KB
    grounding the /api/chat text path uses. The consent_gate is threaded INTO the VoiceSession so
    handle_user_turn is gated by VoiceSession._require_consent (no record/answer before consent). We
    grab the VoiceSession build_voice_agent produced and re-wrap it in the 1.5.x-correct
    WorkerVoiceAgent (build_voice_agent's own hook signatures predate the installed livekit-agents)."""
    # Reuse the PREWARMED embedder (loaded once at process start) so the model never loads mid-call —
    # a lazy first-turn load added a multi-second stall that read as the agent "never replying". Only
    # build a fresh one if prewarm didn't supply it (e.g. a dev hot-reload edge).
    if embedder is None:
        embedder = SentenceTransformerEmbedder()
    retrieve_hook = build_live_retrieve_hook(embedder, kb_version=config.kb_version)
    llm_client = OpenRouterClient(model=AGENT_MODEL)
    base = build_voice_agent(
        config,
        llm_client,
        embedder,
        kb_chunks_or_retrieve_hook=retrieve_hook,
        consent_gate=consent_gate,
        lead_phone_hash=lead_phone_hash,
    )
    agent = WorkerVoiceAgent(base)
    agent._config = config  # llm_node's commit -> _capture_post_commit needs the live config
    return agent


# --- Room metadata parsing (pure, livekit-free) ---------------------------------------------------


def _room_metadata(ctx: Any) -> dict[str, Any]:
    """Best-effort parse of the room's metadata JSON into a dict (empty on missing/garbage).

    The /demo flow stamps the room metadata (or a SIP trunk carries it) with the call's jurisdiction
    and caller phone. We tolerate None / non-JSON / non-dict metadata so a malformed stamp never
    crashes the worker — it degrades to the sane defaults (gated recording, anonymous caller)."""
    raw = getattr(getattr(ctx, "room", None), "metadata", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _room_jurisdiction(ctx: Any) -> str:
    """The consent jurisdiction for this call: room metadata `jurisdiction`/`region`/`state`, else the
    env DEFAULT_JURISDICTION. Recording stays GATED regardless; this only tunes disclosure emphasis."""
    meta = _room_metadata(ctx)
    for key in ("jurisdiction", "region", "state"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return DEFAULT_JURISDICTION


def _room_raw_phone(ctx: Any) -> Optional[str]:
    """The caller's RAW phone if the room metadata or a SIP participant exposes it (None if unknown).

    Checked sources (first hit wins): room metadata `phone`/`caller`/`from`, then any remote
    participant's SIP attribute (`sip.phoneNumber`/`sip.from`). The raw phone is used ONLY to derive
    the phone-hash (R42 — the raw number is NEVER persisted) and to upsert per-lead memory at end."""
    meta = _room_metadata(ctx)
    for key in ("phone", "caller", "from", "from_number"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    room = getattr(ctx, "room", None)
    participants = getattr(room, "remote_participants", None) or {}
    try:
        values = participants.values()
    except AttributeError:
        values = participants
    for p in values:
        attrs = getattr(p, "attributes", None) or {}
        for key in ("sip.phoneNumber", "sip.from", "sip.trunkPhoneNumber"):
            val = attrs.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _seed_gate_from_metadata(gate: ConsentGate, ctx: Any) -> bool:
    """Seed a FRESH worker ConsentGate from the consent the BROWSER already captured (the U13 deadlock
    fix). Returns True if consent metadata was present + applied, False if none was found.

    The /api/livekit/token route stamps the already-captured consent onto the room metadata (shape:
    consent_state/recording_granted/jurisdiction/phone_hash/conversable — see
    src.api.demo_routes._consent_room_metadata). Here we drive the worker's gate to the SAME outcome
    using ONLY public ConsentGate transitions (never private fields): acknowledge the AI disclosure,
    then GRANT recording (-> ready, can_record) when it was granted, else REFUSE (-> unrecorded:
    conversable but NOT recorded — recording stays honest). A captured-but-not-conversable consent
    (e.g. an unresolved suspected minor -> need_parental) is mirrored with flag_minor() so the gate
    stays BLOCKED. When NO consent metadata is present (a future direct SIP call), we DO NOTHING and
    return False — the gate stays `pending` and the worker keeps its fail-closed behavior, so the
    safety property never regresses.

    We treat metadata as "consent metadata" only when it carries the consent keys (consent_state /
    conversable / recording_granted); a metadata blob that only carries jurisdiction/phone (the SIP
    shape) is NOT treated as captured consent."""
    meta = _room_metadata(ctx)
    has_consent_keys = any(k in meta for k in ("consent_state", "conversable", "recording_granted"))
    if not has_consent_keys:
        return False

    conversable = bool(meta.get("conversable"))
    recording_granted = bool(meta.get("recording_granted"))
    consent_state = str(meta.get("consent_state") or "")

    # Always acknowledge the AI disclosure the browser already surfaced (idempotent, harmless).
    gate.acknowledge_ai()

    if conversable:
        # The browser captured consent AND the call may proceed: open the gate to the matching
        # state. Recording granted -> ready (can_record); refused-but-proceeding -> unrecorded.
        if recording_granted:
            gate.grant_recording()
        else:
            gate.refuse_recording()
    elif consent_state == NEED_PARENTAL:
        # A suspected minor was flagged and parental consent is UNRESOLVED: keep the gate BLOCKED
        # (need_parental) — a minor may not proceed unsupervised. flag_minor() drives it there.
        gate.flag_minor()
    # else: consent metadata present but not conversable and not a minor gate (e.g. ended) -> leave
    # the gate as-is (pending/blocked); the worker stays fail-closed for that turn.
    return True


def _wire_commit_on_speech(session: Any, agent: WorkerVoiceAgent, config: AgentConfig) -> None:
    """Gate commit-vs-discard on the agent's TTS playout (the only path that writes session state).

    When the AgentSession creates an agent-initiated speech (the reply), attach a done-callback to its
    SpeechHandle: on completion, if the speech was NOT interrupted commit_turn(speech_id) AND capture
    any escalation / close-tier from the committed decision (parity with the /api/chat text path); a
    barge-in (.interrupted) -> on_barge_in(speech_id) to drop the speculative turn with no
    committed-state write (R5/R10)."""

    def _on_speech_created(ev: Any) -> None:
        # DIAGNOSTIC (commit-path debugging): record the real event shape so we can see WHY the
        # done-callback commit wasn't firing on live calls — source + user_initiated decide whether we
        # process it. (The reliable commit now happens in llm_node; this path is a redundant backstop.)
        logger.info(
            "speech_created: source=%s user_initiated=%s pending=%s",
            getattr(ev, "source", None),
            getattr(ev, "user_initiated", None),
            agent._pending_speech_id,
        )
        # Only agent replies carry a buffered brain turn; ignore user-initiated speech.
        if getattr(ev, "user_initiated", False):
            return
        speech_id = agent._pending_speech_id
        if speech_id is None:
            return
        handle = ev.speech_handle

        def _on_done(_h: Any) -> None:
            if handle.interrupted:
                agent.voice_session.on_barge_in(speech_id)
                logger.info("turn %s interrupted (barge-in) — speculative turn discarded", speech_id)
                return
            committed = agent.voice_session.commit_turn(speech_id)
            logger.info("turn %s committed", speech_id)
            _capture_post_commit(agent, committed, speech_id, config)

        handle.add_done_callback(_on_done)

    session.on("speech_created", _on_speech_created)


def _capture_post_commit(agent: WorkerVoiceAgent, committed: Any, speech_id: str,
                         config: AgentConfig) -> None:
    """After a turn commits, mirror the text path's post-commit bookkeeping (server.py /api/chat):

      - act == "escalate": HANDLE the escalation (graceful async deferral, NO live human) and BUFFER
        the EscalationLog on the agent (re-pointed onto the episode at persist). handle_escalation is
        a coroutine, so we schedule it with a no-op store_hook (no mid-call DB write) and track the
        task so the shutdown callback can await it before persisting.
      - act == "attempt_close": track the close tier (the schema Turn carries no tier; persist needs
        it to map the outcome to the right ladder rung).
      - ALWAYS: schedule a live in_progress upsert (Layer 2) so the Live monitor can query the call
        mid-call. Failures are swallowed inside _upsert_live_voice."""
    if committed is None:
        return
    decision = committed.decision
    if decision.act == "escalate":
        agent._escalation_tasks.append(asyncio.ensure_future(_handle_escalation_async(
            agent, committed, speech_id, config
        )))
    elif decision.act == "attempt_close":
        agent.last_close_tier = decision.tier
    # Live in-progress upsert after every committed turn (Layer 2).
    asyncio.ensure_future(_upsert_live_voice(agent, config))


async def _handle_escalation_async(agent: WorkerVoiceAgent, committed: Any, speech_id: str,
                                   config: AgentConfig) -> None:
    """Build + buffer the EscalationLog for a committed `escalate` turn (parity with /api/chat).

    Uses src.voice.escalation.handle_escalation with a NO-OP store_hook so nothing is written to the
    DB mid-call; the log is buffered on the agent and persisted in FK-safe order at room close (the
    episode must exist before the escalation_log FKs to it). Failures are logged, never raised."""
    try:
        from src.voice.escalation import handle_escalation

        agent_turn = agent.voice_session.turn_for_speech_id(speech_id)
        turn_id = agent_turn.turn_id if agent_turn is not None else 0

        async def _noop_store(_log: Any) -> None:
            return None

        outcome = await handle_escalation(
            committed.decision,
            agent.voice_session.state.belief,
            episode_id="",  # re-pointed onto the real episode_id at persist (FK-safe)
            turn_id=turn_id,
            config=config,
            store_hook=_noop_store,
            moment=committed.reply_text,
        )
        agent.escalations.append(outcome.escalation_log)
        logger.info("escalation captured on turn %s", speech_id)
    except Exception as exc:  # pragma: no cover - defensive (escalation must never crash the call)
        logger.warning("escalation capture failed on turn %s: %s", speech_id, type(exc).__name__)


async def _upsert_live_voice(agent: WorkerVoiceAgent, config: AgentConfig) -> None:
    """Upsert an in_progress Episode after each committed agent turn (Layer 2 live persistence).

    Calls persist_call_live with the agent's stable live_episode_id + live_created_at so every
    upsert AND the final _register_persistence call target the same DB row. Imported lazily so the
    asyncpg/store deps only load on a live call. Failures are swallowed — a hiccup must never
    interrupt the voice call path."""
    try:
        from src.api.persistence import persist_call_live

        await persist_call_live(
            agent.voice_session,
            config=config,
            channel="voice",
            created_at=agent.live_created_at,
            episode_id=agent.live_episode_id,
        )
    except Exception as exc:  # pragma: no cover - prod-only path (needs a real call + DB)
        logger.warning("voice live upsert failed (call continues): %s", type(exc).__name__)


def _register_persistence(ctx: Any, agent: WorkerVoiceAgent, config: AgentConfig,
                          *, phone_hash_value: Optional[str], raw_phone: Optional[str]) -> None:
    """At room close, persist the finished call via the SAME path the text demo uses (U2/U15).

    Reuses src.api.persistence.persist_call_end (channel="voice") with the bound phone-hash, the
    escalations captured DURING the call, and the last close tier — so the voice Episode + escalations
    + phone-hash memory land in /operate exactly like a text call (no duplicated mapping). Awaits any
    in-flight escalation-handling tasks FIRST so their logs are buffered before persist. Imported
    lazily inside the callback so the asyncpg/store deps never load unless a call actually ends.
    Failures are logged, never raised, so a persistence hiccup can't crash the worker. Registered
    BEFORE session.start so an early STT/TTS/connect failure still attempts to persist committed turns.
    """

    async def _on_shutdown(*_: Any, **__: Any) -> None:
        try:
            # Drain in-flight escalation handlers so their logs are buffered before persist.
            if agent._escalation_tasks:
                await asyncio.gather(*agent._escalation_tasks, return_exceptions=True)

            from src.api.persistence import persist_call_end

            # Pass live_episode_id so the final save upserts the same row as all in_progress
            # live upserts (no orphan in_progress row, no duplicate episode).
            episode = await persist_call_end(
                agent.voice_session,
                config=config,
                channel="voice",
                phone_hash=phone_hash_value,
                raw_phone=raw_phone,
                escalation_logs=list(agent.escalations),
                last_tier=agent.last_close_tier,
                episode_id=agent.live_episode_id,
            )
            # CB-09: a disconnect WITHOUT a terminal act persisted as in_progress forever (invisible in
            # the Calls list, which filters in_progress out). When the call ended non-terminal, map it
            # to a TERMINAL outcome (abandoned / released) and RE-SAVE the same row (save_episode
            # upserts ON CONFLICT). persist_call_end / persistence.py are intentionally untouched; the
            # override lives here because only the worker knows the call ended on a participant-
            # disconnect. derive_outcome already produced terminal outcomes for close/escalate/dq, so
            # the mapping returns None for those and we leave the persisted outcome as-is.
            terminal = _terminal_outcome_on_disconnect(
                turn_count=len(agent.voice_session.state.turns),
                escalated=bool(agent.escalations),
                committed_tier=agent.last_close_tier,
                last_act=_last_committed_act(agent.voice_session.state.turns),
            )
            if terminal is not None and episode.outcome in (None, "", "in_progress"):
                episode.outcome = terminal
                episode.ladder_tier = 0
                episode.qualified = False
                from src.memory import store

                await store.save_episode(episode)
                logger.info("CB-09 disconnect: re-saved episode %s as terminal outcome=%s",
                            episode.episode_id, terminal)
            logger.info("persisted voice episode %s (outcome=%s)", episode.episode_id, episode.outcome)
        except Exception as exc:  # pragma: no cover - prod-only path (needs a real call + DB)
            logger.warning("voice episode persistence skipped/failed: %s", type(exc).__name__)

    ctx.add_shutdown_callback(_on_shutdown)


async def entrypoint(ctx: Any) -> None:  # pragma: no cover - requires live livekit + media plugins
    """The worker job entrypoint: connect to the caller's room, build the CONSENT-GATED brain-backed
    AgentSession, SPEAK the disclosure, and run it. Dispatched automatically into each /demo room (the
    token mints room demo-<session_id>; a default-named worker auto-dispatches to every room).

    COMPLIANCE ORDER (U13/R33/R41): (1) connect, (2) build the ConsentGate (jurisdiction from room
    metadata, RefusalPolicy from env) + bind the caller's phone-hash, (3) build the brain with that
    gate threaded into the VoiceSession (so every turn is gated by _require_consent), (4) register
    persistence BEFORE start (so an early failure still persists committed turns), (5) start the
    session, (6) SPEAK the AI+recording disclosure so the opening turn discloses BEFORE any user turn
    is answered. The gate must reach can_converse (recording granted -> ready, or refused ->
    unrecorded) before handle_user_turn answers; until then a user turn raises ConsentError and is
    deflected to the disclosure/consent ask, never run through the brain or recorded. The live audio
    loop (STT->brain->TTS) and the actual spoken consent capture are a MANUAL audio check."""
    load_dotenv()
    config = load_config(WORKER_CONFIG_VERSION)

    # Connect FIRST (audio-only: the caller publishes mic, the agent publishes TTS — no video).
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info("connected to room %r", ctx.room.name)

    # Build the consent gate from the room's jurisdiction + bind the caller's phone-hash (R42 — the
    # raw phone is used only to derive the hash + upsert per-lead memory; it is NEVER persisted).
    jurisdiction = _room_jurisdiction(ctx)
    raw_phone = _room_raw_phone(ctx)
    phone_hash_value: Optional[str] = None
    if raw_phone:
        try:
            phone_hash_value = compute_phone_hash(raw_phone)
        except ValueError:
            phone_hash_value = None
    gate = ConsentGate(jurisdiction=jurisdiction, refusal_policy=_REFUSAL_POLICY)
    # SEED the gate from the consent the BROWSER already captured + carried on the room metadata (the
    # U13 deadlock fix): if present, the gate opens to ready/unrecorded (or stays blocked for an
    # unresolved minor) so can_converse is True from the first turn and the brain answers — no
    # waiting forever for spoken consent. If NO consent metadata is present (a future direct SIP
    # call), the gate stays `pending` and the worker keeps its fail-closed behavior.
    seeded = _seed_gate_from_metadata(gate, ctx)
    # SIP / PHONE path: no browser captured consent (seeded is False) AND the caller is a SIP
    # participant -> capture consent BY VOICE (the browser path has no SIP participant, so this stays
    # False there and the seeded gate proceeds straight to the brain). When neither seeded nor SIP
    # (an unexpected no-consent web call), the gate stays fail-closed exactly as before.
    is_sip = (not seeded) and _is_sip_call(ctx)
    logger.info("consent gate: jurisdiction=%r mode=%s phone_bound=%s seeded=%s sip=%s state=%s can_converse=%s",
                jurisdiction, gate.mode, phone_hash_value is not None, seeded, is_sip, gate.state,
                gate.can_converse)

    # Reuse the embedder warmed at process start (prewarm) so RAG retrieval never triggers a mid-call
    # model load. Defensive getattr: fall back to a per-job build if prewarm didn't populate userdata.
    warm_embedder = getattr(getattr(ctx, "proc", None), "userdata", {}).get("embedder")
    agent = _build_brain(config, consent_gate=gate, lead_phone_hash=phone_hash_value,
                         embedder=warm_embedder)

    # The live media stack: ElevenLabs Scribe v2 realtime STT, ElevenLabs TTS, Silero VAD. Imported
    # here (not at module top) so the plugin deps load only when a worker job actually starts.
    from livekit.plugins import elevenlabs, silero

    # The ElevenLabs plugin defaults to the ELEVEN_API_KEY env var, but this project standardizes on
    # ELEVENLABS_API_KEY (.env / .env.example) — pass it EXPLICITLY so the worker finds the key instead
    # of crashing the job on "API key is required". Fall back to the plugin's own var name if that's
    # what's set. (This bug only surfaces on a live call — the entrypoint is not unit-tested.)
    _eleven_key = os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_API_KEY")
    stt = elevenlabs.STT(model_id=STT_MODEL_ID, use_realtime=True, api_key=_eleven_key)
    tts = elevenlabs.TTS(api_key=_eleven_key)
    # Reuse the VAD warmed at process start (prewarm); only load fresh if prewarm didn't supply it.
    vad = getattr(getattr(ctx, "proc", None), "userdata", {}).get("vad") or silero.VAD.load()

    # Stamp version/kb_version so an operator can attribute a live call to the exact config that ran.
    stamp = config.stamp()
    session = _make_session(stt, tts, vad, version=stamp.get("version", ""),
                            kb_version=stamp.get("kb_version", ""))

    # Wire commit-on-uninterrupted-playout (the only state writer + escalation/tier capture). Register
    # call persistence BEFORE session.start so an early STT/TTS/connect failure still persists what was
    # committed. Wrap construction/start in try/except so a media error exits the job cleanly.
    _wire_commit_on_speech(session, agent, config)
    _register_persistence(ctx, agent, config, phone_hash_value=phone_hash_value, raw_phone=raw_phone)

    try:
        await session.start(agent=agent, room=ctx.room)
        # DISCLOSURE BEFORE the brain answers (R33): the opening turn re-states the AI + recording
        # disclosure (good practice — the caller hears it on the call too). When the gate was SEEDED
        # from the browser-captured consent it is ALREADY can_converse, so the first user turn flows
        # straight to the brain (no deadlock).
        if is_sip:
            # SIP / PHONE path (no browser to click consent): speak ONE self-contained disclosure +
            # spoken-consent ask (SIP_CONSENT_PROMPT discloses AI + recording AND asks) and ARM the
            # capture. We deliberately do NOT also say gate.disclosure_text here — doing both is what
            # disclosed + asked TWICE on the live call. The FIRST user turn(s) are intercepted by
            # WorkerVoiceAgent.handle_consent_turn (NOT the brain): "yes" -> ready (recorded), "no" ->
            # unrecorded/ended per policy, a minor reply -> need_parental (blocked), unclear -> re-ask
            # once then end. Once consent resolves to can_converse, subsequent turns hit the brain.
            await session.say(agent.arm_spoken_consent(), allow_interruptions=False)
            logger.info("SIP spoken-consent armed for room %r", ctx.room.name)
        else:
            # Browser/seeded path: consent was already captured in the UI -> just re-state the
            # disclosure once (good practice); the first turn flows straight to the brain.
            await session.say(gate.disclosure_text, allow_interruptions=True)
        logger.info("voice session started + disclosure spoken for room %r (version=%s kb_version=%s)",
                    ctx.room.name, stamp.get("version"), stamp.get("kb_version"))
    except Exception as exc:
        # Clean exit on an STT/TTS/connect error — the shutdown callback (already registered) still
        # attempts to persist any committed turns. Re-raise so livekit ends the job.
        logger.error("voice session start failed: %s", type(exc).__name__)
        raise


def prewarm(proc: Any) -> None:  # pragma: no cover - runs once per job subprocess before any call
    """Load + WARM the per-call HEAVY assets ONCE per worker subprocess (before LiveKit assigns it a
    call) and stash them on proc.userdata, so call SETUP is fast and the first factual turn's RAG
    retrieval reuses the already-loaded model. Combined with num_idle_processes>=1, this moves the
    ~10s of cold model/plugin loading OFF the call path (a cold first call rang ~20s before the caller
    heard anything, so they hung up). Warms two things:
      - the BGE embedder (RAG): without it the sentence-transformers model loaded LAZILY mid-call.
      - the Silero VAD: VAD.load() is several hundred ms+ of model load; reused across calls.
    Every warm step is best-effort — a failure never crashes the subprocess; the entrypoint falls back
    to a per-job build for whatever is missing."""
    log = logging.getLogger("voice.worker")
    emb = SentenceTransformerEmbedder()
    try:
        emb.embed(["warm up the embedding model"])  # forces the lazy model load + encode NOW
    except Exception as exc:  # noqa: BLE001 - warm is best-effort; don't crash the worker
        log.warning("embedder prewarm failed: %s", type(exc).__name__)
    proc.userdata["embedder"] = emb
    try:
        from livekit.plugins import silero

        proc.userdata["vad"] = silero.VAD.load()  # reused per call (entrypoint falls back if absent)
    except Exception as exc:  # noqa: BLE001 - VAD warm is best-effort
        log.warning("vad prewarm failed: %s", type(exc).__name__)


def _worker_options() -> Any:  # pragma: no cover - requires livekit installed + a live registration
    """Build WorkerOptions from env. ws_url/api_key/api_secret default to LIVEKIT_* from the env when
    unset (livekit-agents reads them); we pass them explicitly so a misconfig is a clear error, not a
    silent localhost attempt. agent_name is left empty -> DEFAULT auto-dispatch: the worker joins
    every room (incl. the token's demo-<session_id> room), which is what the /demo flow needs.
    prewarm_fnc loads the embedder before any call so RAG never stalls mid-call."""
    load_dotenv()
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        # prewarm_fnc loads the embedder + VAD in the job subprocess BEFORE the entrypoint/first turn,
        # so RAG never triggers a model load mid-conversation and call setup is fast.
        prewarm_fnc=prewarm,
        # KEEP A WARM PROCESS READY. The dev default is 0 idle processes, so EVERY call cold-started a
        # subprocess (embedder + plugin load ~10s) — the caller heard ~20s of dead ring and hung up
        # before the agent ever spoke ("no warmed process available" in the logs). One warm process
        # pays that cost up front. Run the worker in `start` mode (production) so idle processes are
        # stable — `dev` mode's hot-reload conflicts with them. ~700MB/process, so default to 1.
        num_idle_processes=int(os.environ.get("VOICE_IDLE_PROCESSES", "1")),
        # The cold prewarm (SentenceTransformer load ~5.5s + a HuggingFace hub check) EXCEEDED
        # livekit's 10s default initialize_process_timeout on the first call, so the first job's
        # process was KILLED and re-initialized — adding ~20s to call setup before the caller heard
        # anything. Give the subprocess room to warm. Overridable via env for slower/faster hosts.
        initialize_process_timeout=float(os.environ.get("VOICE_INIT_TIMEOUT", "45")),
        # The BGE embedder + torch put steady-state RSS ~700MB; the 500MB default flooded the log with
        # "process memory usage is high" every 5s, drowning the real turn events. Raise the warn line.
        job_memory_warn_mb=float(os.environ.get("VOICE_MEM_WARN_MB", "1500")),
        ws_url=os.environ.get("LIVEKIT_URL") or None,
        api_key=os.environ.get("LIVEKIT_API_KEY") or None,
        api_secret=os.environ.get("LIVEKIT_API_SECRET") or None,
    )


if __name__ == "__main__":  # pragma: no cover - the live entrypoint
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    # cli.run_app parses the subcommand (dev|start|connect) from argv, so:
    #   PYTHONPATH=. python3 -m src.voice.worker dev
    cli.run_app(_worker_options())
