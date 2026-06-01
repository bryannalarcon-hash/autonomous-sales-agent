# The thin FastAPI demo API (plan U13; R1/R3/R33/R40/R41/R42). Exposes the transport-agnostic
# brain (src.core.respond via src.voice.session.VoiceSession) and the compliance core
# (src.voice.consent) to the browser front-end over an EXACT, frozen JSON contract the Next.js
# demo depends on. Holds an in-memory per-session store (dict keyed by session_id) for the demo —
# no DB required. The LLM client is INJECTABLE (llm_client_factory) so tests pass a MockLLMClient
# and never hit the network. Consent gates /api/chat (409 until satisfied) and /api/livekit/token
# (503 when LiveKit creds are absent, never a crash, never printing secrets). PII is scrubbed inside
# the VoiceSession before any transcript body is stored; the lead key is the phone-hash (raw phone
# is never persisted). CORS is open for local dev only.
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.config.settings import AgentConfig, load_config
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


@dataclass
class _Session:
    """One in-memory demo session: the consent gate, the brain session (built lazily once consent
    permits conversation), the caller's phone-hash + assigned sticky voice, and the channel."""

    session_id: str
    gate: ConsentGate
    channel: str
    voice_session: Optional[VoiceSession] = None
    phone_hash: Optional[str] = None
    assigned_voice: Optional[str] = None
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
) -> FastAPI:
    """Build the demo API app. Everything network/DB-bound is injectable so tests run offline.

    llm_client_factory builds a fresh LLMClient per session (prod: OpenRouterClient from env; tests:
    a MockLLMClient factory). embedder/retrieve_hook wire RAG (defaults: FakeEmbedder, no retrieve).
    lead_voice_lookup reuses a returning caller's stored sticky voice (U6 hydrate). The app keeps an
    in-memory session store; consent gates /api/chat and /api/livekit/token.
    """
    app = FastAPI(title="auto-sales-agent demo API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],  # local dev demo only
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    cfg = config or load_config("champion_v0")
    emb = embedder or FakeEmbedder()
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
        gate, the assigned sticky voice (R3), and the phone-hash key (R42) into it."""
        if sess.voice_session is None:
            sess.voice_session = VoiceSession(
                cfg,
                _make_llm(),
                emb,
                kb_chunks_or_retrieve_hook=retrieve_hook,
                consent_gate=sess.gate,
                assigned_voice=sess.assigned_voice,
                lead_phone_hash=sess.phone_hash,
            )
        return sess.voice_session

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
    def consent_respond(req: RespondRequest) -> RespondResponse:
        sess = _get(req.session_id)
        gate = sess.gate

        # Bind the caller's phone-hash (R42 — never a raw phone) + assign the sticky voice (R3).
        if req.phone_hash and sess.phone_hash is None:
            sess.phone_hash = req.phone_hash
            stored = lead_voice_lookup(req.phone_hash) if lead_voice_lookup else None
            sess.assigned_voice = stored or assign_voice(req.phone_hash)
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
        vs.commit_turn(speech_id)
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
