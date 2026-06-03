# The TESTABLE voice-session orchestrator (plan U12/U13) — the per-call state + per-turn
# orchestration the LiveKit hooks (src/voice/agent.py) delegate to. Imports the brain
# (src.core.respond), the KB retriever seam, config, the memory schema, and the compliance core
# (src.voice.consent), but NEVER livekit — so it is fully unit-testable and CANNOT leak a LiveKit
# type into src/core. PARITY (R37) is achieved by calling the WHOLE respond() as ONE unit (never
# re-splitting dst/decide/realize) and building history dicts in the EXACT shape src.sim.selfplay
# builds — so a scripted voice session and the equivalent text session produce the same decisions.
# SIDE-EFFECT SAFETY under speculative/preemptive generation: each turn is buffered in
# `pending[speech_id]` and commit_turn() is the ONLY path that writes session state;
# discard_speculative()/on_barge_in() drop a pending turn with NO state write (R5/R10 self-recover).
# CONSENT/PII (U13): an optional ConsentGate gates handle_user_turn/commit_turn (no record/log
# until consent allows); captured Turn.text is PII-scrubbed (scrub_pii) BEFORE it is stored; the
# session carries the assigned sticky TTS voice (R3).
# CB-41 (stream NLG -> TTS): handle_user_turn_stream() is the STREAMING twin of handle_user_turn — it
# runs the consent gate + RAG retrieve + the SHARED DST/gated-Policy path via respond_stream (so the
# gated decision is IDENTICAL to the blocking path), yields the NLG reply token-by-token (the worker
# forwards each to TTS at generation pace), and once the stream ends buffers the SAME PendingTurn under
# speech_id (reply_text == concat of the yielded tokens) so commit_turn() is unchanged. R37 parity is
# preserved end-to-end: the brain decides every word; streaming only changes WHEN each token surfaces.
# BELIEF PERSISTENCE (CB-23): every committed AGENT Turn carries its belief snapshot
# (pending.new_belief.to_snapshot() — the SAME projection sim/text log per turn), so Call Review can
# replay a voice call's trust/walk-away/stage trajectory. This is load-bearing: a turn committed with
# an empty/None belief blanks the Review trajectory for voice calls, so commit_turn always attaches it.
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.core.policy import Decision
from src.core.respond import StreamResult, respond, respond_stream
from src.kb.embeddings import EmbeddingModel
from src.memory.schema import Turn
from src.voice.consent import ConsentGate, scrub_pii

# A retrieve hook: (query, *, kb_version, k) -> a sequence of grounding facts (Chunks or strings).
# Injected so the session is DB-free in tests (a local FakeEmbedder hook) yet wires to the real
# kb.retriever.retrieve in production. The session threads the result straight into respond().
RetrieveHook = Callable[..., Sequence[Any]]


class ConsentError(RuntimeError):
    """Raised when a turn is attempted before the ConsentGate permits conversation (U13/R33/R41).

    The API layer maps this to HTTP 409 (consent not satisfied). Carries the gate's current state
    so the caller can tell pending/need_parental/ended apart without re-reading the gate.
    """

    def __init__(self, state: str, message: Optional[str] = None) -> None:
        self.state = state
        super().__init__(message or f"consent not satisfied (state={state!r})")

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
        consent_gate: Optional[ConsentGate] = None,
        assigned_voice: Optional[str] = None,
        lead_phone_hash: Optional[str] = None,
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        self.embedder = embedder
        self.seed = seed
        self._retrieve_hook = kb_chunks_or_retrieve_hook
        # U13 compliance: an optional ConsentGate gates recording/logging; the assigned sticky voice
        # (R3) and the lead phone-hash key (R42 — raw phone NEVER stored) ride on the session.
        self.consent_gate = consent_gate
        self.assigned_voice = assigned_voice
        self.lead_phone_hash = lead_phone_hash
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

    # --- consent gating (U13) -------------------------------------------------------------------

    def _require_consent(self) -> None:
        """Raise ConsentError unless the attached ConsentGate permits conversation. No gate attached
        (e.g. the headless parity tests) means consent is not in scope -> allow."""
        gate = self.consent_gate
        if gate is not None and not gate.can_converse:
            raise ConsentError(gate.state)

    @property
    def recorded(self) -> bool:
        """Whether this call is being recorded — False when no gate is attached or consent refused."""
        return bool(self.consent_gate and self.consent_gate.recorded)

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

        CONSENT GATE (U13): if a ConsentGate is attached and does not yet permit conversation
        (state not in ready/unrecorded), raise ConsentError WITHOUT generating — the brain never
        runs and nothing is recorded until consent allows.
        """
        self._require_consent()
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

    async def handle_user_turn_stream(
        self,
        user_text: str,
        speech_id: str,
        *,
        last_user_act: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """STREAM the agent's response to one user turn, yielding NLG tokens, then buffer the turn.

        CB-41 — the streaming twin of handle_user_turn the worker's llm_node consumes. (1) consent gate
        + intent-gated RAG retrieve BEFORE generation, (2) run the WHOLE brain via respond_stream (DST
        -> gated Policy -> streamed NLG) — the gated decision is IDENTICAL to handle_user_turn's
        because both go through the same brain code, (3) yield each NLG token as it generates so the
        worker forwards it to TTS at generation pace, (4) once the stream ends, buffer the SAME
        PendingTurn under speech_id with reply_text == ''.join(the yielded tokens) — NO state write
        until commit_turn (the single writer). R37 PARITY: the buffered reply IS the concatenation of
        the streamed tokens, so what TTS spoke equals what commits.

        CONSENT GATE (U13): if a ConsentGate is attached and does not yet permit conversation, raise
        ConsentError WITHOUT generating — the brain never runs and nothing is recorded.
        """
        self._require_consent()
        facts = await self._maybe_retrieve(user_text)
        result = StreamResult()
        async for token in respond_stream(
            self.state.belief,
            self.state.history,
            user_text,
            self.llm_client,
            self.config,
            retrieved_facts=facts,
            last_user_act=last_user_act,
            result=result,
        ):
            yield token
        # The stream finished: the holder now carries the gated decision, the assembled reply (==
        # concat of the yielded tokens), and the new belief. Buffer the PendingTurn so commit_turn is
        # unchanged. Defensive: respond_stream always finalizes the holder, but guard a None decision
        # (a degenerate stream) so a bad turn can't poison commit.
        if result.decision is None or result.new_belief is None:  # pragma: no cover - defensive
            return
        pending = PendingTurn(
            speech_id=speech_id,
            user_text=user_text,
            decision=result.decision,
            reply_text=result.reply,
            new_belief=result.new_belief,
            retrieved_facts=facts,
            committed=False,
        )
        self.state.pending[speech_id] = pending

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

        # 2. Capture the user turn (transcript) + history dict (selfplay shape). The TRANSCRIPT body
        #    is PII-scrubbed BEFORE it is stored (U13/R42); the history dict (a parity artifact fed
        #    back into respond()) keeps the verbatim text so decisions stay identical to text mode.
        user_turn_id = self._next_turn_id
        self._next_turn_id += 1
        self.state.turns.append(
            Turn(turn_id=user_turn_id, speaker="prospect", text=scrub_pii(pending.user_text))
        )
        self.state.history.append({"role": "user", "text": pending.user_text})

        # 3. Capture the agent turn (decision + rationale + belief snapshot), keyed by speech_id,
        #    stamped with the running kb_version; append the agent history dict (selfplay shape).
        agent_turn_id = self._next_turn_id
        self._next_turn_id += 1
        _timings = pending.decision.meta.get("timings") or {}
        agent_turn = Turn(
            turn_id=agent_turn_id,
            speaker="agent",
            text=scrub_pii(pending.reply_text),  # scrub any echoed PII before storing (U13/R42)
            decision=pending.decision.act,
            rationale=pending.decision.rationale,
            # CB-23: persist the per-turn belief snapshot (drivers + trends + stage) on every committed
            # agent turn — the SAME to_snapshot() projection the sim/text path logs — so Call Review can
            # replay the voice call's trust/walk-away trajectory (an empty belief here blanks Review).
            belief=pending.new_belief.to_snapshot(),
            kb_version=self.state.kb_version,
            # CB-28: carry the KB facts that grounded a tool answer (answer_via_kb) so Call Review can
            # show what was pulled; None on non-tool turns. Stamped on decision.meta by respond().
            retrieved=(pending.decision.meta or {}).get("retrieved"),
            # Real measured turn latency from respond()'s stage timing (perf_counter) — feeds the live
            # monitor's per-turn latency, which was previously unpopulated for real calls.
            latency_ms=int(round(_timings["total_ms"])) if "total_ms" in _timings else None,
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
