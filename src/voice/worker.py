# The RUNNABLE LiveKit voice worker (plan U12, Phase 3 — the last unwired piece). A worker process
# that registers with LiveKit Cloud, is dispatched into the caller's /demo room, and pipes
# ElevenLabs Scribe v2 STT -> OUR brain (src.voice.session.VoiceSession via build_voice_agent) ->
# ElevenLabs TTS, so a caller HEARS the agent and SEES their transcript. PARITY (R37): the reply the
# caller hears is the one respond() already computed in the VoiceSession — the LiveKit `llm` node only
# SURFACES that buffered reply, it never re-decides. GROUNDING (U5/R43): the session threads the SAME
# build_live_retrieve_hook the text demo uses, so voice answers cite the ingested KB.
# COMPLIANCE (U13/R33/R40/R41): the entrypoint builds a ConsentGate (jurisdiction from room metadata,
# RefusalPolicy from env), threads it into the VoiceSession via build_voice_agent (so every turn is
# gated by VoiceSession._require_consent EXACTLY like /api/chat), and SPEAKS the AI+recording
# disclosure BEFORE the brain answers — a not-yet-can_converse turn raises ConsentError and is never
# recorded. PERSISTENCE (U2/U15): at room close the finished VoiceSession is saved via
# src.api.persistence.persist_call_end (channel="voice") with the bound phone-hash, any escalations
# captured DURING the call, and the last close tier — so voice calls flow into /operate exactly like
# text calls.
#
# HEAVY-IMPORT ISOLATION: the livekit-agents CORE imports are import-guarded (so this module imports
# cleanly in the livekit-FREE test suite/CI, with Agent aliased to object and WorkerVoiceAgent still
# constructible); the heavy elevenlabs/silero PLUGIN imports live INSIDE entrypoint so installing the
# plugins never leaks into the offline suite. Nothing in src/core, src/voice/__init__, or the brain
# core imports this file. Run it with:
#     PYTHONPATH=. python3 -m src.voice.worker dev      # connects + registers to LIVEKIT_URL
# Built against livekit-agents 1.5.15 (AgentSession / WorkerOptions / cli.run_app / JobContext APIs
# were read from the installed package, not guessed): on_user_turn_completed(turn_ctx, new_message),
# llm_node yields a str, SpeechHandle.add_done_callback gates commit-vs-discard on .interrupted.
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv

from src.config.settings import AgentConfig, load_config
from src.core.llm import OpenRouterClient
from src.kb.embeddings import SentenceTransformerEmbedder
from src.kb.live import build_live_retrieve_hook
from src.memory.schema import phone_hash as compute_phone_hash
from src.voice.agent import AGENT_MODEL, STT_MODEL_ID, build_voice_agent
from src.voice.consent import ConsentGate, RefusalPolicy
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
        # Escalations captured DURING the call (re-pointed onto the episode at persist) + the last
        # committed close tier (the schema Turn carries no tier, so we track it for outcome mapping).
        self.escalations: list[Any] = []
        self.last_close_tier: Optional[str] = None
        # In-flight async escalation-handling tasks, awaited at shutdown before persisting.
        self._escalation_tasks: list[Any] = []

    async def on_user_turn_completed(
        self, turn_ctx: Any, new_message: Any
    ) -> None:
        """User finished speaking (final STT): run the brain speculatively and buffer the reply.

        Generates a fresh speech_id, runs respond() through the session (consent-gated + RAG-grounded),
        and remembers the speech_id so llm_node can surface the buffered reply. No state write here."""
        user_text = (new_message.text_content or "").strip()
        if not user_text:
            return
        self._turn_counter += 1
        speech_id = f"t-{self._turn_counter}"
        self._pending_speech_id = speech_id
        await self.voice_session.handle_user_turn(user_text, speech_id)

    async def llm_node(self, chat_ctx: Any, tools: Any, model_settings: Any) -> str:
        """Surface the brain's pre-computed reply for the current turn (PARITY — never re-decide).

        Returns the buffered reply_text the VoiceSession already produced in on_user_turn_completed.
        The AgentSession streams this string straight to TTS; no LLM is invoked in this node. Returns
        "" on the defensive None branches (no pending speech / its buffer already drained)."""
        sid = self._pending_speech_id
        if sid is None:
            return ""
        pending = self.voice_session.state.pending.get(sid)
        return pending.reply_text if pending is not None else ""


def _build_brain(config: AgentConfig, *, consent_gate: ConsentGate,
                 lead_phone_hash: Optional[str] = None) -> WorkerVoiceAgent:
    """Build the worker's Agent around OUR brain — REUSING build_voice_agent so the VoiceSession is
    wired EXACTLY like the text path (parity), GROUNDED via build_live_retrieve_hook (U5), and
    CONSENT-GATED via the supplied ConsentGate (U13 — the blocking finding's fix).

    The real OpenRouterClient(model=AGENT_MODEL) is the brain; a real SentenceTransformerEmbedder +
    the live retrieve hook (pinned to the config's kb_version) give voice answers the same KB
    grounding the /api/chat text path uses. The consent_gate is threaded INTO the VoiceSession so
    handle_user_turn is gated by VoiceSession._require_consent (no record/answer before consent). We
    grab the VoiceSession build_voice_agent produced and re-wrap it in the 1.5.x-correct
    WorkerVoiceAgent (build_voice_agent's own hook signatures predate the installed livekit-agents)."""
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
    return WorkerVoiceAgent(base)


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


def _wire_commit_on_speech(session: Any, agent: WorkerVoiceAgent, config: AgentConfig) -> None:
    """Gate commit-vs-discard on the agent's TTS playout (the only path that writes session state).

    When the AgentSession creates an agent-initiated speech (the reply), attach a done-callback to its
    SpeechHandle: on completion, if the speech was NOT interrupted commit_turn(speech_id) AND capture
    any escalation / close-tier from the committed decision (parity with the /api/chat text path); a
    barge-in (.interrupted) -> on_barge_in(speech_id) to drop the speculative turn with no
    committed-state write (R5/R10)."""

    def _on_speech_created(ev: Any) -> None:
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
        it to map the outcome to the right ladder rung)."""
    if committed is None:
        return
    decision = committed.decision
    if decision.act == "escalate":
        agent._escalation_tasks.append(asyncio.ensure_future(_handle_escalation_async(
            agent, committed, speech_id, config
        )))
    elif decision.act == "attempt_close":
        agent.last_close_tier = decision.tier


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

            episode = await persist_call_end(
                agent.voice_session,
                config=config,
                channel="voice",
                phone_hash=phone_hash_value,
                raw_phone=raw_phone,
                escalation_logs=list(agent.escalations),
                last_tier=agent.last_close_tier,
            )
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
    logger.info("consent gate: jurisdiction=%r mode=%s phone_bound=%s",
                jurisdiction, gate.mode, phone_hash_value is not None)

    agent = _build_brain(config, consent_gate=gate, lead_phone_hash=phone_hash_value)

    # The live media stack: ElevenLabs Scribe v2 realtime STT, ElevenLabs TTS, Silero VAD. Imported
    # here (not at module top) so the plugin deps load only when a worker job actually starts.
    from livekit.plugins import elevenlabs, silero

    stt = elevenlabs.STT(model_id=STT_MODEL_ID, use_realtime=True)
    tts = elevenlabs.TTS()
    vad = silero.VAD.load()

    # Stamp version/kb_version so an operator can attribute a live call to the exact config that ran.
    stamp = config.stamp()
    session = AgentSession(
        stt=stt,
        tts=tts,
        vad=vad,
        userdata={"version": stamp.get("version", ""), "kb_version": stamp.get("kb_version", "")},
        # Adaptive barge-in / turn-taking (R5/R10): natural interruption + endpointing windows.
        allow_interruptions=True,
        min_interruption_duration=_MIN_INTERRUPTION_DURATION,
        min_endpointing_delay=_MIN_ENDPOINTING_DELAY,
        max_endpointing_delay=_MAX_ENDPOINTING_DELAY,
    )

    # Wire commit-on-uninterrupted-playout (the only state writer + escalation/tier capture). Register
    # call persistence BEFORE session.start so an early STT/TTS/connect failure still persists what was
    # committed. Wrap construction/start in try/except so a media error exits the job cleanly.
    _wire_commit_on_speech(session, agent, config)
    _register_persistence(ctx, agent, config, phone_hash_value=phone_hash_value, raw_phone=raw_phone)

    try:
        await session.start(agent=agent, room=ctx.room)
        # DISCLOSURE BEFORE the brain answers (R33): the opening turn states the AI + recording
        # disclosure. The gate is still `pending` (not can_converse), so any user turn raises
        # ConsentError until recording consent is captured (recording granted -> ready, refused ->
        # unrecorded per policy). Spoken consent capture is part of the MANUAL audio check.
        await session.say(gate.disclosure_text, allow_interruptions=True)
        logger.info("voice session started + disclosure spoken for room %r (version=%s kb_version=%s)",
                    ctx.room.name, stamp.get("version"), stamp.get("kb_version"))
    except Exception as exc:
        # Clean exit on an STT/TTS/connect error — the shutdown callback (already registered) still
        # attempts to persist any committed turns. Re-raise so livekit ends the job.
        logger.error("voice session start failed: %s", type(exc).__name__)
        raise


def _worker_options() -> Any:  # pragma: no cover - requires livekit installed + a live registration
    """Build WorkerOptions from env. ws_url/api_key/api_secret default to LIVEKIT_* from the env when
    unset (livekit-agents reads them); we pass them explicitly so a misconfig is a clear error, not a
    silent localhost attempt. agent_name is left empty -> DEFAULT auto-dispatch: the worker joins
    every room (incl. the token's demo-<session_id> room), which is what the /demo flow needs."""
    load_dotenv()
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        ws_url=os.environ.get("LIVEKIT_URL") or None,
        api_key=os.environ.get("LIVEKIT_API_KEY") or None,
        api_secret=os.environ.get("LIVEKIT_API_SECRET") or None,
    )


if __name__ == "__main__":  # pragma: no cover - the live entrypoint
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    # cli.run_app parses the subcommand (dev|start|connect) from argv, so:
    #   PYTHONPATH=. python3 -m src.voice.worker dev
    cli.run_app(_worker_options())
