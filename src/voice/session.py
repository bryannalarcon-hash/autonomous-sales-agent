# The TESTABLE voice-session orchestrator (plan U12) — the per-call state + per-turn orchestration
# the LiveKit hooks (src/voice/agent.py) delegate to. Imports the brain (src.core.respond), the KB
# retriever seam, config, and the memory schema, but NEVER livekit — so it is fully unit-testable
# and CANNOT leak a LiveKit type into src/core. PARITY (R37) is achieved by calling the WHOLE
# respond() as ONE unit (never re-splitting dst/decide/realize) and building history dicts in the
# EXACT shape src.sim.selfplay builds — so a scripted voice session and the equivalent text session
# produce the same decisions. SIDE-EFFECT SAFETY under speculative/preemptive generation: each turn
# is buffered in `pending[speech_id]` and commit_turn() is the ONLY path that writes session state;
# discard_speculative()/on_barge_in() drop a pending turn with NO state write (R5/R10 self-recover).
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.core.policy import Decision
from src.core.respond import respond
from src.kb.embeddings import EmbeddingModel
from src.memory.schema import Turn

# A retrieve hook: (query, *, kb_version, k) -> a sequence of grounding facts (Chunks or strings).
# Injected so the session is DB-free in tests (a local FakeEmbedder hook) yet wires to the real
# kb.retriever.retrieve in production. The session threads the result straight into respond().
RetrieveHook = Callable[..., Sequence[Any]]

# Intent gating for PROACTIVE retrieval (R43): retrieve BEFORE generation only when the user's turn
# is factual (a KB lookup will help). Non-factual acknowledgements / chit-chat skip retrieval so the
# latency-sensitive path stays fast. Cheap, deterministic keyword/shape heuristics — no LLM call.
_FACTUAL_CUES = (
    "how much", "cost", "price", "pricing", "fee", "charge", "expensive", "afford",
    "how long", "how many", "how often", "when", "where", "what time", "schedule",
    "result", "results", "guarantee", "refund", "work", "proof", "evidence",
    "qualif", "discount", "plan", "package", "subject", "grade", "tutor",
)
_QUESTION_RE = re.compile(r"\?\s*$")
# Short acknowledgements that should NEVER trigger a lookup even if they brush a cue word.
_ACK_RE = re.compile(
    r"^\s*(ok(ay)?|sure|yeah?|yep|got it|sounds good|that makes sense|alright|thanks?"
    r"|i see|cool|right|fine|mm-?hm+)\b",
    re.IGNORECASE,
)


def should_retrieve(user_text: str, belief: BeliefState) -> bool:
    """Predicate: will a KB lookup help this turn? True for factual/price questions, False for
    acknowledgements / pure chit-chat. Intent-gated so non-factual turns skip retrieval (R43).

    Deterministic + LLM-free: a turn is "factual" when it asks a question OR brushes a factual cue
    word, AND is not a bare acknowledgement. `belief` is accepted so a future version can gate on
    dialogue stage without changing the call sites.
    """
    text = (user_text or "").strip()
    if not text:
        return False
    if _ACK_RE.match(text):
        return False
    lowered = text.lower()
    if _QUESTION_RE.search(text):
        return True
    return any(cue in lowered for cue in _FACTUAL_CUES)


def should_play_filler(user_text: str, belief: BeliefState) -> bool:
    """Latency cover (R43): play a cached filler exactly when a KB lookup will run (it's the slow
    step before generation). Mirrors should_retrieve so the adapter can mask the lookup latency."""
    return should_retrieve(user_text, belief)


@dataclass
class PendingTurn:
    """A speculatively-generated turn buffered under its speech_id BEFORE commit (side-effect
    safety). Holds the full respond() result — the gated decision, the realized reply, the NEW
    belief (the session belief is NOT advanced until commit), and the facts that grounded it.
    `committed` flips True only when commit_turn() promotes it."""

    speech_id: str
    user_text: str
    decision: Decision
    reply_text: str
    new_belief: BeliefState
    retrieved_facts: Optional[Sequence[Any]] = None
    committed: bool = False


@dataclass
class VoiceSessionState:
    """The per-call state. `belief`/`history`/`turns` are the COMMITTED state (only commit_turn
    writes them); `pending` is the speculative buffer (speech_id -> PendingTurn) that holds turns
    until they are committed or discarded. `version`/`kb_version` stamp every captured turn for
    attribution; `committed` is the last committed belief (== `belief`)."""

    belief: BeliefState
    version: str
    kb_version: str
    history: list[dict[str, Any]] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    pending: dict[str, PendingTurn] = field(default_factory=dict)
    committed: Optional[BeliefState] = None
    last_interrupted_speech_id: Optional[str] = None


class VoiceSession:
    """Per-call orchestrator the LiveKit hooks delegate to (livekit-FREE, fully unit-testable).

    Lifecycle per user turn:
      handle_user_turn(text, speech_id) -> intent-gated RAG retrieve (BEFORE generation) -> call the
        WHOLE respond() (parity: one unit, never re-split) -> buffer the result in pending[speech_id]
        with NO state write -> return the PendingTurn (so llm_node can emit the reply).
      commit_turn(speech_id) -> the ONLY writer: advance belief to new_belief, append the user turn +
        the captured agent Turn (keyed by speech_id, stamped version+kb_version), append history dicts
        in the SAME shape selfplay builds.
      discard_speculative(speech_id) / on_barge_in(speech_id) -> drop pending[speech_id] with NO
        state write (a discarded speculative turn / an interrupted TTS leaves committed state intact).
    """

    def __init__(
        self,
        config: AgentConfig,
        llm_client: LLMClient,
        embedder: EmbeddingModel,
        *,
        kb_chunks_or_retrieve_hook: Optional[RetrieveHook] = None,
        seed: int = 0,
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        self.embedder = embedder
        self.seed = seed
        self._retrieve_hook = kb_chunks_or_retrieve_hook
        stamp = config.stamp()
        belief = BeliefState.fresh()
        self.state = VoiceSessionState(
            belief=belief,
            version=stamp.get("version", ""),
            kb_version=stamp.get("kb_version", ""),
            committed=belief,
        )
        # speech_id <-> captured-Turn join (the schema Turn has no speech_id field and core stays
        # unchanged, so the linkage lives here): speech_id -> turn_id and the inverse.
        self._speech_to_turn: dict[str, int] = {}
        self._turn_to_speech: dict[int, str] = {}
        self._next_turn_id = 0

    # --- retrieval (intent-gated, BEFORE generation) -------------------------------------------

    async def _maybe_retrieve(self, user_text: str) -> Optional[Sequence[Any]]:
        """Run the injected retrieve hook ONLY when should_retrieve fires (R43). Returns the facts
        (threaded into respond) or None. The hook is sync or async; both are awaited correctly."""
        if self._retrieve_hook is None or not should_retrieve(user_text, self.state.belief):
            return None
        result = self._retrieve_hook(user_text, kb_version=self.state.kb_version, k=4)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        return result

    # --- the per-turn orchestration -------------------------------------------------------------

    async def handle_user_turn(
        self,
        user_text: str,
        speech_id: str,
        *,
        last_user_act: Optional[str] = None,
    ) -> PendingTurn:
        """Generate (speculatively) the agent's response to one user turn WITHOUT committing it.

        (1) intent-gated RAG retrieve BEFORE generation, (2) call the WHOLE respond() (parity: one
        unit) against the CURRENT committed belief/history, (3) buffer (decision, reply, new_belief,
        facts) in pending[speech_id] — NO state write. Returns the PendingTurn so the LiveKit
        llm_node can emit reply_text for this speech_id.
        """
        facts = await self._maybe_retrieve(user_text)
        decision, reply_text, new_belief = await respond(
            self.state.belief,
            self.state.history,
            user_text,
            self.llm_client,
            self.config,
            retrieved_facts=facts,
            last_user_act=last_user_act,
        )
        pending = PendingTurn(
            speech_id=speech_id,
            user_text=user_text,
            decision=decision,
            reply_text=reply_text,
            new_belief=new_belief,
            retrieved_facts=facts,
            committed=False,
        )
        # Buffer ONLY — commit_turn is the single writer of session state.
        self.state.pending[speech_id] = pending
        return pending

    def commit_turn(self, speech_id: str) -> Optional[PendingTurn]:
        """Promote pending[speech_id] into committed state — the ONLY path that writes state.

        Advances belief to the pending new_belief, appends the user turn + the captured agent Turn
        (keyed by speech_id via the session's join map, stamped version+kb_version), and appends the
        user + agent history dicts in the EXACT selfplay shape so decisions stay parity-identical.
        No-op (returns None) if there is no pending turn for this speech_id.
        """
        pending = self.state.pending.pop(speech_id, None)
        if pending is None:
            return None

        # 1. Advance the committed belief (respond never mutated the input belief).
        self.state.belief = pending.new_belief
        self.state.committed = pending.new_belief

        # 2. Capture the user turn (transcript) + history dict (selfplay shape).
        user_turn_id = self._next_turn_id
        self._next_turn_id += 1
        self.state.turns.append(
            Turn(turn_id=user_turn_id, speaker="prospect", text=pending.user_text)
        )
        self.state.history.append({"role": "user", "text": pending.user_text})

        # 3. Capture the agent turn (decision + rationale + belief snapshot), keyed by speech_id,
        #    stamped with the running kb_version; append the agent history dict (selfplay shape).
        agent_turn_id = self._next_turn_id
        self._next_turn_id += 1
        agent_turn = Turn(
            turn_id=agent_turn_id,
            speaker="agent",
            text=pending.reply_text,
            decision=pending.decision.act,
            rationale=pending.decision.rationale,
            belief=pending.new_belief.to_snapshot(),
            kb_version=self.state.kb_version,
        )
        self.state.turns.append(agent_turn)
        self.state.history.append(
            {
                "role": "agent",
                "act": pending.decision.act,
                "confidence": pending.decision.confidence,
                "text": pending.reply_text,
            }
        )

        # 4. Record the speech_id <-> turn join (transcript+decision+version join by speech_id).
        self._speech_to_turn[speech_id] = agent_turn_id
        self._turn_to_speech[agent_turn_id] = speech_id

        pending.committed = True
        return pending

    def discard_speculative(self, speech_id: str) -> None:
        """Drop a speculatively-generated turn with NO state write (the side-effect-safety
        guarantee): a discarded speculative turn leaves committed belief/history/turns untouched."""
        self.state.pending.pop(speech_id, None)

    def on_barge_in(self, speech_id: str) -> None:
        """Handle a barge-in / interrupted TTS: drop the (still-uncommitted) pending turn with NO
        committed-state write (R5/R10 self-recover) and mark the interruption on the state so the
        SessionReport can record it."""
        self.state.pending.pop(speech_id, None)
        self.state.last_interrupted_speech_id = speech_id

    # --- speech_id <-> captured-Turn join lookups -----------------------------------------------

    def turn_for_speech_id(self, speech_id: str) -> Optional[Turn]:
        """The captured agent Turn for a committed speech_id (transcript+decision+version join)."""
        turn_id = self._speech_to_turn.get(speech_id)
        if turn_id is None:
            return None
        for turn in self.state.turns:
            if turn.turn_id == turn_id:
                return turn
        return None

    def speech_id_for_turn(self, turn: Turn) -> Optional[str]:
        """The speech_id a captured agent Turn was committed under (the inverse join)."""
        return self._turn_to_speech.get(turn.turn_id)
