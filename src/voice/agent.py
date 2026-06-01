# The THIN LiveKit glue for the voice sales agent (plan U12). All brain orchestration lives in the
# livekit-FREE src/voice/session.VoiceSession; this file only wires the LiveKit hooks to it so NO
# LiveKit type ever reaches src/core. The livekit imports are IMPORT-GUARDED: when livekit-agents is
# absent (e.g. the test suite, CI) the module STILL imports cleanly with LIVEKIT_AVAILABLE=False and
# Agent aliased to object, so importing src.voice.agent never fails. When livekit IS present the
# glue is real: on_user_turn_completed -> session.handle_user_turn (RAG+DST+respond), llm_node ->
# emit the pending reply for the current speech_id, turn-commit -> session.commit_turn, interruption
# -> session.on_barge_in; STT/TTS/VAD + adaptive barge-in/turn config + version/kb_version stamping
# (room metadata + session.userdata + a SessionReport hook) are all behind LIVEKIT_AVAILABLE.
from __future__ import annotations

import os
from typing import Any, Optional

from src.config.settings import AgentConfig
from src.core.llm import LLMClient
from src.kb.embeddings import EmbeddingModel
from src.voice.session import RetrieveHook, VoiceSession

# Default model/component ids for the live LiveKit LLM-node wiring (only used when livekit IS
# installed). The BRAIN's decisions run on the `llm_client` passed to build_voice_agent (the env-
# driven OpenRouterClient), NOT this constant — so it is read from AGENT_MODEL with the SAME default
# the text path uses (claude-sonnet-4.5), keeping text/voice on one model family. Overridable via env.
AGENT_MODEL = os.environ.get("AGENT_MODEL", "anthropic/claude-sonnet-4.5")
STT_MODEL_ID = "scribe_v2_realtime"

# --- Import guard: the suite (and CI) run WITHOUT livekit-agents installed --------------------
try:  # pragma: no cover - exercised only when livekit-agents is installed
    from livekit.agents import Agent  # type: ignore

    LIVEKIT_AVAILABLE = True
except ImportError:  # the common case here: livekit-agents not installed
    Agent = object  # type: ignore[assignment,misc]
    LIVEKIT_AVAILABLE = False


def _persona_instructions(config: AgentConfig) -> str:
    """Default system instructions for the real LiveKit Agent, derived from the config persona.

    The brain (respond()) already drives every decision; these instructions only shape the STT/TTS
    persona surface (name/role/style) so the live Agent constructs cleanly when the caller passes
    none. They never affect the gated decision — that stays in src.core (parity preserved).
    """
    p = getattr(config, "persona", None)
    name = getattr(p, "name", None) or "Alex"
    role = getattr(p, "role", None) or "tutoring advisor"
    style = getattr(p, "style", None) or "warm, low-pressure, consultative"
    return (
        f"You are {name}, a {role} for Nerdy / Varsity Tutors. Speak in a {style} manner. "
        "Follow the structured decisions provided by the policy brain; never invent facts."
    )


class VoiceSalesAgent(Agent):  # type: ignore[misc,valid-type]
    """The LiveKit Agent that delegates every brain decision to a VoiceSession.

    Holds NO conversation logic of its own — it routes LiveKit lifecycle hooks to `voice_session`
    (named so to avoid the real Agent's read-only `session` property):
      on_user_turn_completed(stt_text, speech_id) -> voice_session.handle_user_turn (RAG+DST+respond
        run here, per the U12 spec; the pending reply is buffered, NOT committed).
      llm_node(...) -> returns/emits the pending reply_text for the current speech_id.
      commit current speech_id -> voice_session.commit_turn (the only state write).
      on interruption -> voice_session.on_barge_in (drop the speculative turn, committed state intact).
    When livekit is absent Agent is `object`, so this class is still constructible + unit-mockable.
    """

    def __init__(self, session: VoiceSession, **kwargs: Any) -> None:
        # Only forward to the real Agent base when livekit is present; otherwise object.__init__.
        if LIVEKIT_AVAILABLE:
            # The real livekit Agent requires `instructions=`; default it from the config persona so
            # the factory yields a working Agent even when the caller passes none. STT/TTS/VAD/turn
            # detection can be supplied via kwargs at wiring time (left to the worker entrypoint).
            kwargs.setdefault("instructions", _persona_instructions(session.config))
            super().__init__(**kwargs)  # type: ignore[misc]
        else:  # pragma: no cover - structural (object base takes no kwargs)
            super().__init__()
        # Stored as `voice_session` (NOT `session`): the real livekit Agent already exposes a
        # read-only `session` property (the running AgentSession), so shadowing it would collide.
        self.voice_session = session
        self._current_speech_id: Optional[str] = None

    # --- LiveKit lifecycle hooks (delegate to the session) -------------------------------------

    async def on_user_turn_completed(self, stt_text: str, speech_id: str, **_: Any) -> Any:
        """User finished speaking: run RAG+DST+respond via the session (speculative, uncommitted)
        and remember the speech_id so llm_node can emit its reply."""
        self._current_speech_id = speech_id
        return await self.voice_session.handle_user_turn(stt_text, speech_id)

    async def llm_node(self, *_: Any, **__: Any) -> Optional[str]:
        """Emit the pending reply_text for the current speech_id (the brain already generated it in
        on_user_turn_completed). Returns None if there is no pending turn (defensive)."""
        sid = self._current_speech_id
        if sid is None:
            return None
        pending = self.voice_session.state.pending.get(sid)
        return pending.reply_text if pending is not None else None

    def on_turn_committed(self, speech_id: Optional[str] = None, **_: Any) -> None:
        """The agent's TTS finished uninterrupted -> commit the speculative turn (state write)."""
        sid = speech_id or self._current_speech_id
        if sid is not None:
            self.voice_session.commit_turn(sid)

    def on_interrupted(self, speech_id: Optional[str] = None, **_: Any) -> None:
        """A barge-in interrupted TTS -> drop the speculative turn, committed state untouched."""
        sid = speech_id or self._current_speech_id
        if sid is not None:
            self.voice_session.on_barge_in(sid)


def build_voice_agent(
    config: AgentConfig,
    llm_client: LLMClient,
    embedder: EmbeddingModel,
    *,
    kb_chunks_or_retrieve_hook: Optional[RetrieveHook] = None,
    seed: int = 0,
    **agent_kwargs: Any,
) -> VoiceSalesAgent:
    """Factory: build a VoiceSession (the brain orchestrator) and wrap it in a VoiceSalesAgent.

    Constructs the livekit-FREE session first (so the brain wiring is identical whether or not
    livekit is installed), then the thin Agent glue around it. The caller supplies the version/
    kb_version-stamped config, the agent LLM client, the KB embedder, and an optional retrieve hook
    (defaults to None -> retrieval is skipped, which keeps the session DB-free for demos/tests).
    """
    session = VoiceSession(
        config,
        llm_client,
        embedder,
        kb_chunks_or_retrieve_hook=kb_chunks_or_retrieve_hook,
        seed=seed,
    )
    return VoiceSalesAgent(session, **agent_kwargs)
