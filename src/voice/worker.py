# The RUNNABLE LiveKit voice worker (plan U12, Phase 3 — the last unwired piece). A worker process
# that registers with LiveKit Cloud, is dispatched into the caller's /demo room, and pipes
# ElevenLabs Scribe v2 STT -> OUR brain (src.voice.session.VoiceSession via build_voice_agent) ->
# ElevenLabs TTS, so a caller HEARS the agent and SEES their transcript. PARITY (R37): the reply the
# caller hears is the one respond() already computed in the VoiceSession — the LiveKit `llm` node only
# SURFACES that buffered reply, it never re-decides. GROUNDING (U5/R43): the session threads the SAME
# build_live_retrieve_hook the text demo uses, so voice answers cite the ingested KB. PERSISTENCE
# (U2/U15): at room close the finished VoiceSession is saved via src.api.persistence.persist_call_end
# (channel="voice") so voice calls also flow into /operate.
#
# HEAVY-IMPORT ISOLATION: every livekit-agents / plugin import lives INSIDE this module (and mostly
# inside functions), and nothing in src/core, src/voice/__init__, or the test suite imports this file
# — so installing the plugins never leaks into the offline suite. Run it with:
#     PYTHONPATH=. python3 -m src.voice.worker dev      # connects + registers to LIVEKIT_URL
# Built against livekit-agents 1.5.15 (AgentSession / WorkerOptions / cli.run_app / JobContext APIs
# were read from the installed package, not guessed): on_user_turn_completed(turn_ctx, new_message),
# llm_node yields a str, SpeechHandle.add_done_callback gates commit-vs-discard on .interrupted.
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv

# livekit-agents core (the worker ALWAYS needs these; this module is never imported by the suite).
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

from src.config.settings import AgentConfig, load_config
from src.core.llm import OpenRouterClient
from src.kb.embeddings import SentenceTransformerEmbedder
from src.kb.live import build_live_retrieve_hook
from src.voice.agent import AGENT_MODEL, STT_MODEL_ID, build_voice_agent
from src.voice.session import VoiceSession

logger = logging.getLogger("voice.worker")

# Which config version the live worker runs (the brain's prompts/playbooks/thresholds + pinned
# kb_version). Overridable via env so a demo can pin a specific champion/challenger.
WORKER_CONFIG_VERSION = os.environ.get("WORKER_CONFIG_VERSION", "champion_v0")

# Adaptive barge-in / turn config (R5/R10): let the caller interrupt the agent's TTS naturally and
# recover. Tunable via env without code changes. These are passed to AgentSession below.
_MIN_INTERRUPTION_DURATION = float(os.environ.get("VOICE_MIN_INTERRUPTION_DURATION", "0.5"))
_MIN_ENDPOINTING_DELAY = float(os.environ.get("VOICE_MIN_ENDPOINTING_DELAY", "0.4"))
_MAX_ENDPOINTING_DELAY = float(os.environ.get("VOICE_MAX_ENDPOINTING_DELAY", "6.0"))


class WorkerVoiceAgent(Agent):
    """The live LiveKit Agent for the worker — delegates EVERY brain decision to a VoiceSession.

    It wraps the SAME VoiceSession that build_voice_agent wires (so RAG grounding + respond() parity
    are identical to the text path); this subclass only re-expresses the delegation against the
    livekit-agents 1.5.x hook signatures (which differ from the older shape sketched in agent.py):

      on_user_turn_completed(turn_ctx, new_message) -> pull the user text from new_message, run the
        WHOLE respond() via voice_session.handle_user_turn (RAG + DST + decide + realize), buffer the
        speculative PendingTurn under a fresh speech_id. We do NOT commit here.
      llm_node(...) -> yield the buffered reply_text for the pending speech_id. The brain ALREADY
        generated it, so this node SURFACES it (parity) and never calls an LLM itself.
      commit/discard -> wired by the worker via SpeechHandle.add_done_callback: when the agent's TTS
        finishes uninterrupted we commit_turn (the only state write); a barge-in (.interrupted) drops
        the speculative turn with NO committed-state write (R5/R10 self-recover).
    """

    def __init__(self, base: Any) -> None:
        # `base` is the VoiceSalesAgent that build_voice_agent constructed (with persona instructions
        # + the wired VoiceSession). Re-init the real Agent base with those same instructions and
        # adopt its VoiceSession so the brain wiring (config/llm/embedder/retrieve-hook) is reused
        # verbatim — this subclass only swaps in the 1.5.x-correct lifecycle hooks.
        super().__init__(instructions=base.instructions)
        self.voice_session: VoiceSession = base.voice_session
        self._pending_speech_id: Optional[str] = None
        self._turn_counter = 0

    async def on_user_turn_completed(
        self, turn_ctx: "lkllm.ChatContext", new_message: "lkllm.ChatMessage"
    ) -> None:
        """User finished speaking (final STT): run the brain speculatively and buffer the reply.

        Generates a fresh speech_id, runs respond() through the session (RAG-grounded), and remembers
        the speech_id so llm_node can surface the buffered reply. No state write happens here."""
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
        The AgentSession streams this string straight to TTS; no LLM is invoked in this node."""
        sid = self._pending_speech_id
        if sid is None:
            return ""
        pending = self.voice_session.state.pending.get(sid)
        return pending.reply_text if pending is not None else ""


def _build_brain(config: AgentConfig) -> WorkerVoiceAgent:
    """Build the worker's Agent around OUR brain — REUSING build_voice_agent so the VoiceSession is
    wired EXACTLY like the text path (parity) and GROUNDED via build_live_retrieve_hook (U5).

    The real OpenRouterClient(model=AGENT_MODEL) is the brain; a real SentenceTransformerEmbedder +
    the live retrieve hook (pinned to the config's kb_version) give voice answers the same KB
    grounding the /api/chat text path uses. We grab the VoiceSession build_voice_agent produced and
    re-wrap it in the 1.5.x-correct WorkerVoiceAgent (build_voice_agent's own hook signatures predate
    the installed livekit-agents)."""
    embedder = SentenceTransformerEmbedder()
    retrieve_hook = build_live_retrieve_hook(embedder, kb_version=config.kb_version)
    llm_client = OpenRouterClient(model=AGENT_MODEL)
    base = build_voice_agent(
        config,
        llm_client,
        embedder,
        kb_chunks_or_retrieve_hook=retrieve_hook,
    )
    return WorkerVoiceAgent(base)


def _wire_commit_on_speech(session: AgentSession, agent: WorkerVoiceAgent) -> None:
    """Gate commit-vs-discard on the agent's TTS playout (the only path that writes session state).

    When the AgentSession creates an agent-initiated speech (the reply), attach a done-callback to its
    SpeechHandle: on completion, commit_turn(speech_id) if the speech was NOT interrupted, else
    on_barge_in(speech_id) to drop the speculative turn with no committed-state write (R5/R10)."""

    def _on_speech_created(ev: SpeechCreatedEvent) -> None:
        # Only agent replies carry a buffered brain turn; ignore user-initiated speech.
        if ev.user_initiated:
            return
        speech_id = agent._pending_speech_id
        if speech_id is None:
            return
        handle = ev.speech_handle

        def _on_done(_h: Any) -> None:
            if handle.interrupted:
                agent.voice_session.on_barge_in(speech_id)
                logger.info("turn %s interrupted (barge-in) — speculative turn discarded", speech_id)
            else:
                agent.voice_session.commit_turn(speech_id)
                logger.info("turn %s committed", speech_id)

        handle.add_done_callback(_on_done)

    session.on("speech_created", _on_speech_created)


def _register_persistence(ctx: JobContext, session: AgentSession, agent: WorkerVoiceAgent,
                          config: AgentConfig) -> None:
    """At room close, persist the finished call via the SAME path the text demo uses (U2/U15).

    Reuses src.api.persistence.persist_call_end (channel="voice") so the voice Episode + any
    escalations + phone-hash memory land in /operate exactly like a text call — no duplicated mapping.
    Imported lazily inside the callback so the asyncpg/store deps never load unless a call actually
    ends. Failures are logged, never raised, so a persistence hiccup can't crash the worker."""

    async def _on_shutdown(*_: Any, **__: Any) -> None:
        try:
            from src.api.persistence import persist_call_end

            episode = await persist_call_end(
                agent.voice_session,
                config=config,
                channel="voice",
            )
            logger.info("persisted voice episode %s (outcome=%s)", episode.episode_id, episode.outcome)
        except Exception as exc:  # pragma: no cover - prod-only path (needs a real call + DB)
            logger.warning("voice episode persistence skipped/failed: %s", type(exc).__name__)

    ctx.add_shutdown_callback(_on_shutdown)


async def entrypoint(ctx: JobContext) -> None:
    """The worker job entrypoint: connect to the caller's room, build the brain-backed AgentSession,
    and run it. Dispatched automatically into each /demo room (the token mints room demo-<session_id>;
    a default-named worker auto-dispatches to every room, so the worker joins the caller's room)."""
    load_dotenv()
    config = load_config(WORKER_CONFIG_VERSION)

    # Connect FIRST (audio-only: the caller publishes mic, the agent publishes TTS — no video).
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info("connected to room %r", ctx.room.name)

    agent = _build_brain(config)

    # The live media stack: ElevenLabs Scribe v2 realtime STT, ElevenLabs TTS, Silero VAD. Imported
    # here (not at module top) so the plugin deps load only when a worker job actually starts.
    from livekit.plugins import elevenlabs, silero

    stt = elevenlabs.STT(model_id=STT_MODEL_ID, use_realtime=True)
    tts = elevenlabs.TTS()
    vad = silero.VAD.load()

    # Stamp version/kb_version so an operator can attribute a live call to the exact config that ran
    # (mirrors the Episode stamp + the userdata the brain session carries).
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

    # Wire commit-on-uninterrupted-playout (the only state writer) + call persistence at room close.
    _wire_commit_on_speech(session, agent)
    _register_persistence(ctx, session, agent, config)

    await session.start(agent=agent, room=ctx.room)
    logger.info("voice session started for room %r (version=%s kb_version=%s)",
                ctx.room.name, stamp.get("version"), stamp.get("kb_version"))


def _worker_options() -> WorkerOptions:
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


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    # cli.run_app parses the subcommand (dev|start|connect) from argv, so:
    #   PYTHONPATH=. python3 -m src.voice.worker dev
    cli.run_app(_worker_options())
