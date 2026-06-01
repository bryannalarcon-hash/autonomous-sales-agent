# The /demo call routes for the thin FastAPI surface (plan U13; R1/R3/R33/R40/R41/R42), extracted
# from src.api.server so that module stays a slim app-assembler. create_demo_router() builds an
# APIRouter carrying the EXACT, frozen JSON contract the Next.js demo depends on: consent/start +
# consent/respond drive the ConsentGate (AI disclosure, recording consent, minor->parental, with
# detect_minor run on available signals so a suspected minor is gated INDEPENDENT of a client
# is_minor flag, R40); /api/chat (GATED 409 by consent) runs one brain turn via VoiceSession — it
# detect_minors the user text first (a self-reported minor flips need_parental -> 409), buffers/persists
# an escalation with a PII-scrubbed moment, and reports `done` for terminal acts; /api/livekit/token
# (503 without creds) mints a token via the injected builder; /api/session/{id}/end persists the
# Episode (+escalations+lead) FK-safely and EVICTS the session from the bounded LRU store (503 over
# cap); GET /api/session/{id} + /api/health are introspection. Everything network/DB-bound is injected
# from create_app (LLM factory, embedder, retrieve/hydrate/escalation/call-end hooks, token builder) so
# tests run offline. PII is scrubbed in the VoiceSession before storage; the lead key is the phone-hash.
from __future__ import annotations

import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.kb.embeddings import EmbeddingModel
from src.voice.consent import ConsentGate, RefusalPolicy, assign_voice, detect_minor
from src.voice.session import ConsentError, RetrieveHook, VoiceSession

# A factory that yields a FRESH LLMClient per session (so call-state never collides across sessions).
LLMClientFactory = Callable[[], LLMClient]
# Optional lookup: phone_hash -> a stored sticky voice (U6 hydrate). Returns None for a new caller.
LeadVoiceLookup = Callable[[str], Optional[str]]
# Builds a LiveKit access token: (api_key, api_secret, identity, room) -> jwt str.
TokenBuilder = Callable[[str, str, str, str], str]
# Optional cross-call hydration: (raw_phone | phone_hash, config) -> a seeded BeliefState (U6).
HydrateHook = Callable[..., Awaitable[Any]]
# Optional escalation persist: (EscalationLog) -> awaitable (the U14 review-queue write).
EscalationPersistHook = Callable[..., Awaitable[Any]]
# Optional call-end persist: builds + saves the Episode (+ escalations + lead) from a finished session.
CallEndPersistHook = Callable[..., Awaitable[Any]]

# Hard cap on concurrently-tracked demo sessions. The store is a bounded LRU (OrderedDict): a /end
# evicts its session, and a fresh /consent/start over the cap is refused with 503 rather than letting
# the in-memory dict grow without bound. Overridable via MAX_DEMO_SESSIONS for load tests.
_DEFAULT_MAX_SESSIONS = 5000


# --- request/response DTOs (the EXACT contract the front-end depends on) -------------------------


class StartRequest(BaseModel):
    jurisdiction: str = ""
    channel: str = "voice"  # "voice" | "text"


class StartResponse(BaseModel):
    session_id: str
    disclosure_text: str
    consent_mode: str


class RespondRequest(BaseModel):
    session_id: str
    ai_acknowledged: bool = False
    recording_consent: bool = False
    is_minor: Optional[bool] = None
    parental_consent: Optional[bool] = None
    # Optional phone-hash to bind the caller to this session (R42: a HASH, never the raw phone).
    phone_hash: Optional[str] = None


class RespondResponse(BaseModel):
    state: str  # "ready" | "unrecorded" | "ended" | "need_parental"
    message: str


class ChatRequest(BaseModel):
    session_id: str
    text: str


class ChatResponse(BaseModel):
    reply: str
    decision_act: str
    done: bool


class TokenRequest(BaseModel):
    session_id: str
    identity: str


class TokenResponse(BaseModel):
    token: str
    url: str
    room: str


class EndRequest(BaseModel):
    session_id: str
    # Optional raw phone to remember the caller by (HASHED before any storage — never persisted raw).
    raw_phone: Optional[str] = None


class EndResponse(BaseModel):
    episode_id: str
    outcome: str
    escalations: int
    persisted: bool


@dataclass
class _Session:
    """One in-memory demo session: the consent gate, the brain session (built lazily once consent
    permits conversation), the caller's phone-hash + assigned sticky voice, the channel, and the
    escalation logs captured during the call (re-pointed onto the episode + persisted at call end)."""

    session_id: str
    gate: ConsentGate
    channel: str
    voice_session: Optional[VoiceSession] = None
    phone_hash: Optional[str] = None
    assigned_voice: Optional[str] = None
    raw_phone: Optional[str] = None
    last_close_tier: Optional[str] = None
    ended: bool = False
    # A returning caller's hydrated belief (U6), seeded into the VoiceSession when it is built.
    hydrated_belief: Optional[Any] = None
    escalations: list[Any] = field(default_factory=list)
    _speech_counter: int = field(default=0)


_OK_STATE_MESSAGES = {
    "ready": "Consent captured. The call may proceed and be recorded.",
    "unrecorded": "Recording declined. The call proceeds UNRECORDED.",
    "need_parental": "A parent or guardian must consent before we continue.",
    "ended": "The call has ended.",
    "pending": "Awaiting consent.",
    "ai_disclosed": "AI disclosure acknowledged; awaiting recording consent.",
}


def create_demo_router(
    *,
    cfg: AgentConfig,
    emb: EmbeddingModel,
    active_retrieve_hook: Optional[RetrieveHook],
    build_token: TokenBuilder,
    make_llm: Callable[[], LLMClient],
    refusal_policy: RefusalPolicy,
    live_rag: bool,
    lead_voice_lookup: Optional[LeadVoiceLookup] = None,
    hydrate_hook: Optional[HydrateHook] = None,
    escalation_persist_hook: Optional[EscalationPersistHook] = None,
    call_end_persist_hook: Optional[CallEndPersistHook] = None,
) -> APIRouter:
    """Build the /demo APIRouter with its own bounded in-memory session store.

    All network/DB seams are injected by create_app: make_llm builds a fresh LLMClient per session;
    active_retrieve_hook grounds /api/chat against the KB; hydrate_hook seeds a returning caller's slots
    at consent-start; escalation_persist_hook writes an `escalate` to the review queue;
    call_end_persist_hook saves the Episode at /end (defaults to persist_call_end). The router owns a
    bounded LRU `sessions` dict — /end evicts its session and a start over MAX_DEMO_SESSIONS returns 503.
    """
    router = APIRouter()
    sessions: "OrderedDict[str, _Session]" = OrderedDict()
    # A small FIFO cache of the EndResponse for sessions already ended + evicted, so a repeat /end
    # stays idempotent (returns already_ended) even though the heavy live session was popped. Bounded
    # the same way as `sessions` so it never grows unbounded either.
    ended_results: "OrderedDict[str, EndResponse]" = OrderedDict()
    try:
        max_sessions = int(os.environ.get("MAX_DEMO_SESSIONS", str(_DEFAULT_MAX_SESSIONS)))
    except (TypeError, ValueError):
        max_sessions = _DEFAULT_MAX_SESSIONS

    def _get(session_id: str) -> _Session:
        sess = sessions.get(session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail=f"unknown session_id {session_id!r}")
        sessions.move_to_end(session_id)  # LRU touch
        return sess

    def _remember_ended(session_id: str, result: EndResponse) -> None:
        """Record the ended-session result for idempotent re-/end, bounding the cache by the same cap."""
        ended_results[session_id] = result
        ended_results.move_to_end(session_id)
        while len(ended_results) > max_sessions:
            ended_results.popitem(last=False)

    def _ensure_voice_session(sess: _Session) -> VoiceSession:
        """Build the per-session VoiceSession lazily once consent permits conversation, wiring the
        gate, the live RAG retrieve hook (U5 grounding), the assigned sticky voice (R3), and the
        phone-hash key (R42) into it. A hydrated returning-caller belief seeds skip-known on turn 1."""
        if sess.voice_session is None:
            vs = VoiceSession(
                cfg,
                make_llm(),
                emb,
                kb_chunks_or_retrieve_hook=active_retrieve_hook,
                consent_gate=sess.gate,
                assigned_voice=sess.assigned_voice,
                lead_phone_hash=sess.phone_hash,
            )
            if sess.hydrated_belief is not None:
                vs.state.belief = sess.hydrated_belief
                vs.state.committed = sess.hydrated_belief
            sess.voice_session = vs
        return sess.voice_session

    async def _hydrate(phone_hash_value: str) -> Optional[Any]:
        """Hydrate a returning caller's belief by phone-hash (U6). Uses the injected hydrate_hook when
        present; else, ONLY when live_rag is on, defaults to the store-backed lookup. When live_rag is
        off and no hook is injected, returns None (the prior DB-free demo behavior). Any failure
        degrades to None so a fresh call proceeds."""
        if hydrate_hook is not None:
            return await hydrate_hook(phone_hash_value, cfg)
        if not live_rag:
            return None
        from src.memory import hydrate as hydrate_mod
        from src.memory import store as store_mod

        lead = await store_mod.get_lead_by_phone(phone_hash_value)
        if lead is None:
            return None
        belief = BeliefState.fresh()
        hydrate_mod.seed_slots(belief, lead.slots)
        belief.meta["returning_caller"] = True
        belief.meta["lead_phone_hash"] = phone_hash_value
        belief.meta["prior_objections"] = list(lead.prior_objections or [])
        belief.meta["assigned_voice"] = lead.assigned_voice
        return belief

    async def _persist_escalation(log: Any) -> None:
        """Write an EscalationLog to the review queue (U14) via the INJECTED hook (tests capture it)."""
        if escalation_persist_hook is not None:
            await escalation_persist_hook(log)

    async def _noop_store(log: Any) -> None:
        """No-op store_hook used DURING a call on the real DB path: the escalation FKs to an episode
        that does not exist yet, so the write is deferred to call end (persist_call_end saves the
        episode first, then re-points + writes the buffered escalations)."""
        return None

    # --- POST /api/consent/start ----------------------------------------------------------------

    @router.post("/api/consent/start", response_model=StartResponse)
    def consent_start(req: StartRequest) -> StartResponse:
        # Bound the in-memory store: refuse a NEW session over the cap rather than grow unbounded.
        if len(sessions) >= max_sessions:
            raise HTTPException(
                status_code=503,
                detail="demo session capacity reached; please retry shortly.",
            )
        session_id = uuid.uuid4().hex
        gate = ConsentGate(jurisdiction=req.jurisdiction, refusal_policy=refusal_policy)
        channel = req.channel if req.channel in ("voice", "text") else "voice"
        sessions[session_id] = _Session(session_id=session_id, gate=gate, channel=channel)
        return StartResponse(
            session_id=session_id,
            disclosure_text=gate.disclosure_text,
            consent_mode=gate.mode,
        )

    # --- POST /api/consent/respond --------------------------------------------------------------

    @router.post("/api/consent/respond", response_model=RespondResponse)
    async def consent_respond(req: RespondRequest) -> RespondResponse:
        sess = _get(req.session_id)
        gate = sess.gate

        # Bind the caller's phone-hash (R42) + assign the sticky voice (R3) + HYDRATE cross-call
        # memory (U6). Hydration runs once per session at first phone-hash bind.
        if req.phone_hash and sess.phone_hash is None:
            sess.phone_hash = req.phone_hash
            stored = lead_voice_lookup(req.phone_hash) if lead_voice_lookup else None
            try:
                sess.hydrated_belief = await _hydrate(req.phone_hash)
            except Exception:
                # Never let a memory lookup (e.g. DB down) block consent; degrade to a fresh call.
                sess.hydrated_belief = None
            hydrated_voice = (
                getattr(sess.hydrated_belief, "meta", {}).get("assigned_voice")
                if sess.hydrated_belief is not None
                else None
            )
            sess.assigned_voice = stored or hydrated_voice or assign_voice(req.phone_hash)
            if sess.voice_session is not None:
                sess.voice_session.assigned_voice = sess.assigned_voice
                sess.voice_session.lead_phone_hash = sess.phone_hash

        if req.ai_acknowledged:
            gate.acknowledge_ai()
        # Recording consent: grant -> ready; refuse -> unrecorded/ended per policy (R41).
        if req.recording_consent:
            gate.grant_recording()
        else:
            gate.refuse_recording()

        # Minor gate (R40): explicit is_minor OR detect_minor on signals (independent of is_minor).
        minor_signals: dict[str, Any] = {}
        if req.is_minor is not None:
            minor_signals["caller_is_student"] = req.is_minor
        is_minor = req.is_minor is True or detect_minor(minor_signals)
        if is_minor:
            gate.flag_minor()
            if req.parental_consent is True:
                gate.grant_parental()
            elif req.parental_consent is False:
                gate.deny_parental()

        return RespondResponse(
            state=gate.state,
            message=_OK_STATE_MESSAGES.get(gate.state, gate.state),
        )

    # --- POST /api/chat (GATED by consent) ------------------------------------------------------

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        sess = _get(req.session_id)
        # Suspected-minor detection on the user's text (R40), INDEPENDENT of any is_minor flag: a
        # self-reported school-aged caller flips the gate to need_parental and the turn is GATED (409),
        # so the brain never runs for an un-consented minor.
        if sess.gate.can_converse and detect_minor({"text": req.text}):
            sess.gate.flag_minor()
        if not sess.gate.can_converse:
            # 409: consent not satisfied (pending / need_parental / ended).
            raise HTTPException(
                status_code=409,
                detail={"reason": "consent_not_satisfied", "state": sess.gate.state},
            )
        vs = _ensure_voice_session(sess)
        sess._speech_counter += 1
        speech_id = f"s-{sess._speech_counter}"
        try:
            pending = await vs.handle_user_turn(req.text, speech_id)
        except ConsentError as exc:
            raise HTTPException(
                status_code=409,
                detail={"reason": "consent_not_satisfied", "state": exc.state},
            ) from exc
        committed = vs.commit_turn(speech_id)

        # ESCALATION (U14/R10): a gated `escalate` decision is handled at runtime — an in-persona async
        # deferral is produced + an EscalationLog is built and queued for review. The log is buffered on
        # the session and re-pointed onto the episode at call end (FK requires the episode first); when a
        # persist hook is injected (tests) it is also written immediately so the queue reflects the live
        # call without waiting for /end. The recorded `moment` is PII-SCRUBBED before storage (R42).
        if committed is not None and committed.decision.act == "escalate":
            from src.voice.consent import scrub_pii
            from src.voice.escalation import handle_escalation

            agent_turn = vs.turn_for_speech_id(speech_id)
            turn_id = agent_turn.turn_id if agent_turn is not None else 0
            outcome = await handle_escalation(
                committed.decision,
                vs.state.belief,
                episode_id="",  # re-pointed onto the real episode_id at call end (FK-safe)
                turn_id=turn_id,
                config=cfg,
                store_hook=_persist_escalation if escalation_persist_hook is not None else _noop_store,
                moment=scrub_pii(committed.reply_text),
            )
            sess.escalations.append(outcome.escalation_log)

        # Track the latest close tier (the schema Turn carries no tier; we need it to map the outcome).
        if committed is not None and committed.decision.act == "attempt_close":
            sess.last_close_tier = committed.decision.tier

        done = pending.decision.act in ("disqualify", "escalate") or (
            pending.decision.act == "attempt_close" and pending.decision.tier == "enrollment"
        )
        return ChatResponse(
            reply=pending.reply_text,
            decision_act=pending.decision.act,
            done=bool(done),
        )

    # --- POST /api/livekit/token (only when consent allows recording/voice) ---------------------

    @router.post("/api/livekit/token", response_model=TokenResponse)
    def livekit_token(req: TokenRequest) -> TokenResponse:
        sess = _get(req.session_id)
        if not sess.gate.can_converse:
            raise HTTPException(
                status_code=409,
                detail={"reason": "consent_not_satisfied", "state": sess.gate.state},
            )
        api_key = os.environ.get("LIVEKIT_API_KEY", "")
        api_secret = os.environ.get("LIVEKIT_API_SECRET", "")
        url = os.environ.get("LIVEKIT_URL", "")
        if not (api_key and api_secret and url):
            # Clear 503 instead of crashing; we never echo which secret is missing in detail.
            raise HTTPException(
                status_code=503,
                detail="LiveKit not configured (set LIVEKIT_API_KEY / LIVEKIT_API_SECRET / LIVEKIT_URL).",
            )
        room = f"demo-{req.session_id}"
        token = build_token(api_key, api_secret, req.identity, room)
        return TokenResponse(token=token, url=url, room=room)

    # --- POST /api/session/{session_id}/end (persist the finished call + evict) -----------------

    @router.post("/api/session/{session_id}/end", response_model=EndResponse)
    async def session_end(session_id: str, req: EndRequest) -> EndResponse:
        """End a demo call and PERSIST it (U2/U14/U15) so /demo flows into Operate + remembers callers.

        Builds an Episode, saves it FK-safely (lead -> episode -> escalations), upserts per-lead memory
        by phone-hash (raw phone NEVER stored), then EVICTS the session so the dict shrinks. Idempotent:
        a second /end returns the prior already_ended result from a small ended-results cache without
        re-persisting. DB writes go through call_end_persist_hook (tests capture; prod persist_call_end).
        """
        # Idempotent: a second /end on an already-ended (and evicted) session returns already_ended
        # from the cache without re-persisting — no duplicate row, no 404.
        if session_id in ended_results and session_id not in sessions:
            prior = ended_results[session_id]
            return EndResponse(
                episode_id="", outcome="already_ended",
                escalations=prior.escalations, persisted=False,
            )
        sess = _get(session_id)
        raw_phone = req.raw_phone or sess.raw_phone
        # Nothing to persist if the call never reached conversation (no brain session built). Evict it.
        if sess.voice_session is None:
            result = EndResponse(
                episode_id="", outcome="no_conversation", escalations=0, persisted=False
            )
            sessions.pop(session_id, None)
            _remember_ended(session_id, result)
            return result
        # Persist the episode BEFORE ending the gate: session.recorded reads the gate's can_record,
        # which flips False once ENDED — so metrics["recorded"] must be captured while the gate still
        # reflects the call's recording status.
        if call_end_persist_hook is not None:
            episode = await call_end_persist_hook(
                sess.voice_session,
                config=cfg,
                channel=sess.channel,
                phone_hash=sess.phone_hash,
                raw_phone=raw_phone,
                escalation_logs=list(sess.escalations),
                last_tier=sess.last_close_tier,
            )
        else:
            from src.api.persistence import persist_call_end

            episode = await persist_call_end(
                sess.voice_session,
                config=cfg,
                channel=sess.channel,
                phone_hash=sess.phone_hash,
                raw_phone=raw_phone,
                escalation_logs=list(sess.escalations),
                last_tier=sess.last_close_tier,
            )
        # Now end the gate (recorded already captured into the episode metrics above).
        sess.gate.end()
        sess.ended = True
        result = EndResponse(
            episode_id=episode.episode_id,
            outcome=episode.outcome or "",
            escalations=len(sess.escalations),
            persisted=True,
        )
        # EVICT: the finished call is durable in the store now; drop it from the in-memory dict so the
        # session store stays bounded, and cache the result so a repeat /end stays idempotent.
        sessions.pop(session_id, None)
        _remember_ended(session_id, result)
        return result

    # --- GET /api/session/{session_id} (demo introspection / test seam) -------------------------

    @router.get("/api/session/{session_id}")
    def session_view(session_id: str) -> dict[str, Any]:
        sess = _get(session_id)
        turns = (
            [t.to_dict() for t in sess.voice_session.state.turns]
            if sess.voice_session is not None
            else []
        )
        return {
            "session_id": sess.session_id,
            "channel": sess.channel,
            "state": sess.gate.state,
            "consent_mode": sess.gate.mode,
            "recorded": sess.gate.recorded,
            "can_converse": sess.gate.can_converse,
            "assigned_voice": sess.assigned_voice,
            "lead_phone_hash": sess.phone_hash,
            "turns": turns,
        }

    @router.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return router
