# The thin FastAPI demo API (plan U13; R1/R3/R33/R40/R41/R42) PLUS the operator-dashboard read API
# (plan U15 — Operate mode). Exposes the transport-agnostic brain (src.core.respond via
# src.voice.session.VoiceSession) and the compliance core (src.voice.consent) to the browser
# front-end over an EXACT, frozen JSON contract the Next.js demo depends on, and mounts the Operate
# read router (src.api.operate: /api/episodes, /api/kpis, /api/escalations, /api/live) the dashboard
# consumes. Holds an in-memory per-session store (dict keyed by session_id) for the demo. The LLM
# client is INJECTABLE (llm_client_factory) and so is the dashboard's read data layer (read_store)
# so tests pass a MockLLMClient + a seeded in-memory store and never hit the network or Postgres.
# LIVE GROUNDING (U5): by default create_app builds a real embedder + a kb.live retrieve hook so
# /api/chat does intent-gated PROACTIVE retrieval and replies GROUNDED in the ingested KB; tests
# turn this off (live_rag=False) or inject a FakeEmbedder + stub hook to stay DB-free. LIVE
# PERSISTENCE (U2/U14/U15): hydrate_belief seeds a returning caller's known slots at consent-start;
# an `escalate` decision persists an EscalationLog to the review queue; POST /api/session/{id}/end
# saves the Episode (so /demo calls flow into /operate) + persists per-lead memory. All DB writes go
# through an INJECTABLE persistence seam so the demo/consent tests run DB-free. Consent gates
# /api/chat (409) and /api/livekit/token (503 when creds absent — never a crash, never printing
# secrets). PII is scrubbed in the VoiceSession before any transcript is stored; the lead key is the
# phone-hash (raw phone never persisted). CORS is open for local dev only.
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.api.improve import ImproveStore, create_improve_router
from src.api.operate import ReadStore, create_operate_router
from src.config.settings import AgentConfig, load_config
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.kb.embeddings import EmbeddingModel, FakeEmbedder
from src.voice.consent import (
    ConsentGate,
    RefusalPolicy,
    assign_voice,
    consent_mode,
    detect_minor,
)
from src.voice.session import ConsentError, RetrieveHook, VoiceSession

# A factory that yields a FRESH LLMClient per session (so call-state never collides across
# sessions). In prod this builds an OpenRouterClient from env; tests inject a MockLLMClient factory.
LLMClientFactory = Callable[[], LLMClient]
# Optional lookup: phone_hash -> a stored sticky voice (U6 hydrate). Returns None for a new caller.
LeadVoiceLookup = Callable[[str], Optional[str]]
# Builds a LiveKit access token: (api_key, api_secret, identity, room) -> jwt str. Injectable so a
# test can verify the token path WITHOUT importing the livekit SDK (which would pollute the global
# sys.modules the core's livekit-leak guard tests assert on). Defaults to the real SDK builder.
TokenBuilder = Callable[[str, str, str, str], str]
# Optional cross-call hydration: (raw_phone | phone_hash) -> a seeded BeliefState (U6). When supplied,
# a returning caller's known slots seed the session at consent-start so skip-known fires. Injected so
# tests provide a DB-free stub; the prod default lazily calls src.memory.hydrate.hydrate_belief.
HydrateHook = Callable[..., Awaitable[Any]]
# Optional escalation persist: (EscalationLog) -> awaitable (the U14 review-queue write). Injected so
# tests capture the write DB-free; prod defaults to src.memory.store.save_escalation.
EscalationPersistHook = Callable[..., Awaitable[Any]]
# Optional call-end persist: builds + saves the Episode (+ escalations + lead) from a finished session
# (U2/U15). Injected so tests run DB-free; prod defaults to src.api.persistence.persist_call_end.
CallEndPersistHook = Callable[..., Awaitable[Any]]


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
    # The session already holds the phone-hash; this lets the caller pass the raw value at end so the
    # lead's learned slots round-trip without the server ever holding the raw phone beyond hashing.
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


def create_app(
    *,
    llm_client_factory: Optional[LLMClientFactory] = None,
    embedder: Optional[EmbeddingModel] = None,
    retrieve_hook: Optional[RetrieveHook] = None,
    lead_voice_lookup: Optional[LeadVoiceLookup] = None,
    config: Optional[AgentConfig] = None,
    refusal_policy: RefusalPolicy = RefusalPolicy.PROCEED_UNRECORDED,
    cors_origins: Optional[list[str]] = None,
    token_builder: Optional[TokenBuilder] = None,
    read_store: Optional[ReadStore] = None,
    improve_store: Optional[ImproveStore] = None,
    experiment_runner: Optional[Callable[..., Any]] = None,
    live_rag: bool = True,
    hydrate_hook: Optional[HydrateHook] = None,
    escalation_persist_hook: Optional[EscalationPersistHook] = None,
    call_end_persist_hook: Optional[CallEndPersistHook] = None,
) -> FastAPI:
    """Build the demo + operator API app. Everything network/DB-bound is injectable so tests run
    offline.

    llm_client_factory builds a fresh LLMClient per session (prod: OpenRouterClient from env; tests:
    a MockLLMClient factory). LIVE RAG (U5): when live_rag is True and no retrieve_hook is injected,
    create_app builds a REAL SentenceTransformerEmbedder + a kb.live retrieve hook so /api/chat does
    intent-gated PROACTIVE retrieval and replies GROUNDED in the ingested KB; tests pass live_rag=False
    (and/or embedder=FakeEmbedder() + a stub retrieve_hook) to stay DB-free. lead_voice_lookup reuses a
    returning caller's stored sticky voice (U6). LIVE PERSISTENCE: hydrate_hook seeds a returning
    caller's known slots at consent-start (default: src.memory.hydrate.hydrate_belief);
    escalation_persist_hook writes an `escalate` decision to the review queue (default:
    store.save_escalation); call_end_persist_hook saves the Episode + escalations + lead at
    /api/session/{id}/end (default: src.api.persistence.persist_call_end). read_store / improve_store
    are the dashboard READ layers (default Postgres store; tests fake them). experiment_runner is the
    Improve run-an-A/B (POST /api/experiments/run) self-play seam — tests inject a deterministic runner
    (free + reproducible); prod leaves it None so a run does REAL self-play with the injected LLM client
    (which SPENDS model credit). The app keeps an in-memory session store; consent gates /api/chat and
    /api/livekit/token.
    """
    app = FastAPI(title="auto-sales-agent demo + operator API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],  # local dev demo only
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    cfg = config or load_config("champion_v0")
    # Embedder: a REAL SentenceTransformerEmbedder by default (lazy model load on first embed), unless
    # a test injects a FakeEmbedder. The real model only loads if the live retrieve hook actually runs.
    if embedder is not None:
        emb: EmbeddingModel = embedder
    elif live_rag:
        from src.kb.embeddings import SentenceTransformerEmbedder

        emb = SentenceTransformerEmbedder()
    else:
        emb = FakeEmbedder()
    # Retrieve hook: use the injected one; else, when live_rag is on, build the shared kb.live hook so
    # /api/chat grounds against the ingested KB (the SAME hook the voice worker imports). When live_rag
    # is off and no hook is injected, retrieval stays None (the prior DB-free demo behavior).
    active_retrieve_hook = retrieve_hook
    if active_retrieve_hook is None and live_rag:
        from src.kb.live import build_live_retrieve_hook

        active_retrieve_hook = build_live_retrieve_hook(emb, kb_version=cfg.kb_version)
    build_token = token_builder or _build_livekit_token
    sessions: dict[str, _Session] = {}

    def _make_llm() -> LLMClient:
        if llm_client_factory is not None:
            return llm_client_factory()
        # Lazy import keeps OpenRouter (httpx/env) off the test path; only built when truly needed.
        from src.core.llm import OpenRouterClient

        return OpenRouterClient()

    def _get(session_id: str) -> _Session:
        sess = sessions.get(session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail=f"unknown session_id {session_id!r}")
        return sess

    def _ensure_voice_session(sess: _Session) -> VoiceSession:
        """Build the per-session VoiceSession lazily once consent permits conversation, wiring the
        gate, the live RAG retrieve hook (U5 grounding), the assigned sticky voice (R3), and the
        phone-hash key (R42) into it. If the session hydrated a returning caller's belief at
        consent-start, that seeded belief is restored so skip-known fires on the first turn."""
        if sess.voice_session is None:
            vs = VoiceSession(
                cfg,
                _make_llm(),
                emb,
                kb_chunks_or_retrieve_hook=active_retrieve_hook,
                consent_gate=sess.gate,
                assigned_voice=sess.assigned_voice,
                lead_phone_hash=sess.phone_hash,
            )
            # Seed the hydrated cross-call belief (returning caller) so the first turn sees known slots.
            if sess.hydrated_belief is not None:
                vs.state.belief = sess.hydrated_belief
                vs.state.committed = sess.hydrated_belief
            sess.voice_session = vs
        return sess.voice_session

    async def _hydrate(phone_hash_value: str) -> Optional[Any]:
        """Hydrate a returning caller's belief by phone-hash (U6). Uses the injected hydrate_hook when
        present; else, ONLY when live_rag is on, defaults to the store-backed lookup (a returning
        caller's known slots seed the session so skip-known fires). When live_rag is off and no hook is
        injected, returns None — the prior DB-free demo behavior, so no Postgres pool is created on the
        TestClient's event loop. Any failure (unknown caller) degrades to None so a fresh call proceeds."""
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
        """No-op store_hook used DURING a call on the real DB path: the escalation_log FKs to an
        episode that does not exist yet, so the actual DB write is deferred to call end (where
        persist_call_end saves the episode first, then re-points + writes the buffered escalations)."""
        return None

    # --- POST /api/consent/start ----------------------------------------------------------------

    @app.post("/api/consent/start", response_model=StartResponse)
    def consent_start(req: StartRequest) -> StartResponse:
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

    @app.post("/api/consent/respond", response_model=RespondResponse)
    async def consent_respond(req: RespondRequest) -> RespondResponse:
        sess = _get(req.session_id)
        gate = sess.gate

        # Bind the caller's phone-hash (R42 — never a raw phone) + assign the sticky voice (R3) and
        # HYDRATE cross-call memory (U6): a returning caller's known slots seed the session so
        # skip-known fires. Hydration runs once per session at first phone-hash bind.
        if req.phone_hash and sess.phone_hash is None:
            sess.phone_hash = req.phone_hash
            stored = lead_voice_lookup(req.phone_hash) if lead_voice_lookup else None
            try:
                sess.hydrated_belief = await _hydrate(req.phone_hash)
            except Exception:
                # Never let a memory lookup (e.g. DB down) block consent; degrade to a fresh call.
                sess.hydrated_belief = None
            # Reuse a stored sticky voice if hydration surfaced one (U6), else the deterministic pick.
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

        # Minor gate (R40): an explicit is_minor OR detection from the bound signals forces parental.
        if req.is_minor is True:
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

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        sess = _get(req.session_id)
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

        # ESCALATION (U14/R10): a gated `escalate` decision is handled at runtime — an in-persona
        # async deferral is produced + an EscalationLog is built and queued for review. The log is
        # buffered on the session and re-pointed onto the episode at call end (FK requires the episode
        # to exist first); when a persist hook is injected (tests) it is also written immediately so
        # the review queue reflects the live call without waiting for /end.
        if committed is not None and committed.decision.act == "escalate":
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
                moment=committed.reply_text,
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

    @app.post("/api/livekit/token", response_model=TokenResponse)
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

    # --- POST /api/session/{session_id}/end (persist the finished call) -------------------------

    @app.post("/api/session/{session_id}/end", response_model=EndResponse)
    async def session_end(session_id: str, req: EndRequest) -> EndResponse:
        """End a demo call and PERSIST it (U2/U14/U15) so /demo flows into Operate + remembers callers.

        Builds an Episode from the committed session (channel "text"/"voice", version+kb_version
        stamped, persona, the turns + belief trajectory, the terminal outcome derived from the last
        committed agent decision, metrics["recorded"]=session.recorded), saves it, persists any
        escalations captured during the call onto it (review queue P5), and upserts per-lead memory by
        phone-hash so the caller is remembered next time (raw phone NEVER stored). Idempotent: a
        second /end on the same session no-ops and returns the prior result. DB writes go through the
        injectable call_end_persist_hook (tests pass a capture; prod uses persist_call_end).
        """
        sess = _get(session_id)
        raw_phone = req.raw_phone or sess.raw_phone
        # Idempotent: a second /end on an already-ended session does not re-persist (no duplicate row).
        if sess.ended:
            return EndResponse(
                episode_id="", outcome="already_ended", escalations=len(sess.escalations),
                persisted=False,
            )
        # Nothing to persist if the call never reached conversation (no brain session built).
        if sess.voice_session is None:
            return EndResponse(
                episode_id="", outcome="no_conversation", escalations=0, persisted=False
            )
        # Persist the episode BEFORE ending the gate: session.recorded reads the gate's can_record,
        # which flips to False once the state is ENDED — so the metrics["recorded"] flag must be
        # captured while the gate still reflects the call's recording status.
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
        return EndResponse(
            episode_id=episode.episode_id,
            outcome=episode.outcome or "",
            escalations=len(sess.escalations),
            persisted=True,
        )

    # --- GET /api/session/{session_id} (demo introspection / test seam) -------------------------

    @app.get("/api/session/{session_id}")
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

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Operate-mode read endpoints (U15 dashboard): /api/episodes, /api/episodes/{id}, /api/kpis,
    # /api/escalations, /api/live. The read_store is injected here (default Postgres; tests fake it).
    app.include_router(create_operate_router(read_store=read_store))

    # Improve-mode endpoints (U16 dashboard): /api/experiments(+run), /api/approvals(+approve/reject),
    # /api/kb, /api/playbook, /api/versions(+rollback). improve_store + the champion config provider
    # are injected (default Postgres store + load_config; tests fake them) so this runs DB-free too. The
    # run-an-A/B endpoint reuses the app's LLM factory (_make_llm: prod OpenRouterClient = paid; tests a
    # MockLLMClient) and the injectable experiment_runner so tests stay free + deterministic.
    app.include_router(
        create_improve_router(
            improve_store=improve_store,
            champion_config_provider=(lambda: cfg),
            llm_client_factory=_make_llm,
            experiment_runner=experiment_runner,
        )
    )

    return app


def _build_livekit_token(api_key: str, api_secret: str, identity: str, room: str) -> str:
    """Build a LiveKit access token from creds (never logged / printed). Imports livekit.api lazily
    so the API module imports cleanly even when livekit is not installed (the token path is the only
    place that needs it, and it is already guarded by the 503 above when creds are absent)."""
    from livekit import api as lk_api

    grants = lk_api.VideoGrants(room_join=True, room=room)
    token = (
        lk_api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
    )
    return token.to_jwt()


# A module-level app for `uvicorn src.api.server:app` (prod uses env-built OpenRouterClient).
app = create_app()
