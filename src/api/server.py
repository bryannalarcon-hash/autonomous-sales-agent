# The thin FastAPI app-assembler (plan U13/U15/U16). It wires three routers onto ONE app over the
# EXACT, frozen JSON contract the Next.js front-end depends on:
#   - the /demo call router (src.api.demo_routes.create_demo_router): consent, chat, livekit-token,
#     session end/view — the transport-agnostic brain (src.core.respond via VoiceSession) + the
#     compliance core (src.voice.consent) exposed to the browser.
#   - the Operate read router (src.api.operate): /api/episodes, /api/kpis, /api/escalations, /api/live.
#   - the Improve router (src.api.improve): experiments/approvals/kb/playbook/versions — GATED behind
#     require_operator (a write surface) at the include site, never inside the router.
# Everything network/DB-bound is INJECTABLE (llm_client_factory, embedder, retrieve/hydrate/escalation/
# call-end/live-upsert hooks, read_store/improve_store, token_builder) so tests run offline. LIVE
# GROUNDING (U5): by default create_app builds a real embedder + a kb.live retrieve hook so /api/chat
# grounds in the ingested KB; tests pass live_rag=False (or a FakeEmbedder + stub hook) to stay
# DB-free. LIVE PERSISTENCE (U2/U14/U15/Layer2): a finished call persists its Episode + escalations +
# per-lead memory at /end; live_upsert_hook upserts an in_progress Episode after each turn so the Live
# monitor can query calls mid-call. SECURITY/OPS: require_operator guards write endpoints (env
# OPERATOR_TOKEN; unset -> ALLOW with a warning for local demo); CORS reads ALLOWED_ORIGINS (never
# wildcard + credentials together); a lifespan warms the embedder when live_rag and closes the DB pool
# on shutdown. PII is scrubbed before any transcript is stored; the lead key is the phone-hash.
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from src.api.demo_routes import (
    CallEndPersistHook,
    EscalationPersistHook,
    HydrateHook,
    LeadVoiceLookup,
    LLMClientFactory,
    LiveUpsertHook,
    TokenBuilder,
    create_demo_router,
)
from src.api.improve import ImproveStore, create_improve_router
from src.api.operate import ReadStore, create_operate_router
from src.config.settings import AgentConfig, load_config
from src.core.llm import LLMClient
from src.kb.embeddings import EmbeddingModel, FakeEmbedder
from src.voice.consent import RefusalPolicy
from src.voice.session import RetrieveHook

_log = logging.getLogger(__name__)


def _resolve_cors(cors_origins: Optional[list[str]]) -> tuple[list[str], bool]:
    """Resolve (allow_origins, allow_credentials) safely. An explicit cors_origins arg wins; else read
    ALLOWED_ORIGINS (comma-separated) from the env. NEVER pair a wildcard with credentials (browsers
    reject it AND it is unsafe): wildcard -> credentials False; explicit origins -> credentials True.
    Default (nothing set) is the permissive local-dev pairing: ["*"] + credentials False."""
    origins = cors_origins
    if origins is None:
        raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
        origins = [o.strip() for o in raw.split(",") if o.strip()] if raw else ["*"]
    allow_credentials = "*" not in origins
    return origins, allow_credentials


def _make_require_operator() -> Callable[..., None]:
    """Build the require_operator dependency. Compares the X-Operator-Token header (or a Bearer
    Authorization) against env OPERATOR_TOKEN. When OPERATOR_TOKEN is UNSET, ALLOW every request (and
    log a one-time warning) so the local demo works without config; when SET, a missing/wrong token is
    401. The check is constant-shape (no token value ever logged or echoed in the error detail)."""

    def require_operator(
        x_operator_token: Optional[str] = Header(default=None),
        authorization: Optional[str] = Header(default=None),
    ) -> None:
        expected = os.environ.get("OPERATOR_TOKEN", "").strip()
        if not expected:
            _log.warning(
                "OPERATOR_TOKEN is unset — operator write endpoints are UNAUTHENTICATED "
                "(acceptable for local demo only; set OPERATOR_TOKEN before deploying)."
            )
            return
        presented = x_operator_token
        if not presented and authorization and authorization.lower().startswith("bearer "):
            presented = authorization[len("bearer "):].strip()
        if presented != expected:
            raise HTTPException(status_code=401, detail="operator authentication required")

    return require_operator


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
    live_upsert_hook: Optional[LiveUpsertHook] = None,
) -> FastAPI:
    """Assemble the demo + operator API app. Everything network/DB-bound is injectable so tests run
    offline.

    llm_client_factory builds a fresh LLMClient per session (prod: OpenRouterClient from env; tests: a
    MockLLMClient factory). LIVE RAG (U5): when live_rag is True and no retrieve_hook is injected,
    create_app builds a REAL SentenceTransformerEmbedder + a kb.live retrieve hook so /api/chat does
    intent-gated PROACTIVE retrieval and replies GROUNDED in the ingested KB; tests pass live_rag=False
    (and/or embedder=FakeEmbedder() + a stub retrieve_hook) to stay DB-free. lead_voice_lookup reuses a
    returning caller's stored sticky voice (U6). LIVE PERSISTENCE: hydrate_hook seeds a returning
    caller's slots at consent-start (default: src.memory.hydrate.hydrate_belief); escalation_persist_hook
    writes an `escalate` to the review queue (default: store.save_escalation); call_end_persist_hook saves
    the Episode + escalations + lead at /end (default: src.api.persistence.persist_call_end);
    live_upsert_hook upserts an in_progress Episode after each /api/chat turn so the Live monitor can
    query calls mid-call (default: src.api.persistence.persist_call_live). read_store / improve_store are
    the dashboard READ layers (default Postgres; tests fake them). experiment_runner is the Improve A/B
    (POST /api/experiments/run) self-play seam — tests inject a deterministic runner; prod leaves it None
    so a run does REAL self-play with the injected LLM client (which SPENDS credit).
    """
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
    # /api/chat grounds against the ingested KB. When live_rag is off and no hook is injected, retrieval
    # stays None (the prior DB-free demo behavior).
    active_retrieve_hook = retrieve_hook
    if active_retrieve_hook is None and live_rag:
        from src.kb.live import build_live_retrieve_hook

        active_retrieve_hook = build_live_retrieve_hook(emb, kb_version=cfg.kb_version)
    build_token = token_builder or _build_livekit_token

    # Default live_upsert_hook: the real persist_call_live (lazy import keeps DB off the test path).
    # Tests inject a no-op or capture hook; prod gets this default which upserts in_progress Episodes
    # so the Live monitor can query calls mid-call. Only wired when no explicit hook is provided.
    if live_upsert_hook is None and live_rag:
        from src.api.persistence import persist_call_live

        async def _default_live_upsert(session: Any, **kw: Any) -> Any:
            return await persist_call_live(
                session,
                config=kw["config"],
                channel=kw["channel"],
                created_at=kw["created_at"],
                episode_id=kw["episode_id"],
            )

        live_upsert_hook = _default_live_upsert

    def _make_llm() -> LLMClient:
        if llm_client_factory is not None:
            return llm_client_factory()
        # Lazy import keeps OpenRouter (httpx/env) off the test path; only built when truly needed.
        from src.core.llm import OpenRouterClient

        return OpenRouterClient()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Warm the embedder on startup (when live_rag, so the first grounded turn isn't slow) and close
        the shared DB pool on shutdown. Both are best-effort: a warm-up / pool failure must not crash
        the app (tests build the app with live_rag off and never touch Postgres)."""
        if live_rag:
            try:
                emb.embed(["warmup"])
            except Exception:  # pragma: no cover - warm-up is best-effort
                _log.warning("embedder warm-up failed (continuing; first turn will load lazily)")
        try:
            yield
        finally:
            try:
                from src.memory import store

                await store.close_pool()
            except Exception:  # pragma: no cover - shutdown is best-effort
                _log.warning("DB pool close on shutdown failed (already closed?)")

    app = FastAPI(
        title="auto-sales-agent demo + operator API", version="0.1.0", lifespan=lifespan
    )
    origins, allow_credentials = _resolve_cors(cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # /demo call routes (consent, chat, livekit-token, session end/view) — their own bounded session
    # store lives inside the router.
    app.include_router(
        create_demo_router(
            cfg=cfg,
            emb=emb,
            active_retrieve_hook=active_retrieve_hook,
            build_token=build_token,
            make_llm=_make_llm,
            refusal_policy=refusal_policy,
            live_rag=live_rag,
            lead_voice_lookup=lead_voice_lookup,
            hydrate_hook=hydrate_hook,
            escalation_persist_hook=escalation_persist_hook,
            call_end_persist_hook=call_end_persist_hook,
            live_upsert_hook=live_upsert_hook,
        )
    )

    # Operate-mode read endpoints (U15 dashboard) — read-only, no auth gate.
    app.include_router(create_operate_router(read_store=read_store))

    # Improve-mode endpoints (U16 dashboard) — a WRITE surface, so the whole router is GATED behind
    # require_operator at the INCLUDE site (not inside the router). The run-an-A/B endpoint reuses the
    # app's LLM factory + the injectable experiment_runner so tests stay free + deterministic.
    app.include_router(
        create_improve_router(
            improve_store=improve_store,
            champion_config_provider=(lambda: cfg),
            llm_client_factory=_make_llm,
            experiment_runner=experiment_runner,
        ),
        dependencies=[Depends(_make_require_operator())],
    )

    return app


def _build_livekit_token(api_key: str, api_secret: str, identity: str, room: str,
                         room_metadata: Optional[str] = None) -> str:
    """Build a LiveKit access token from creds (never logged / printed). Imports livekit.api lazily so
    the API module imports cleanly even when livekit is not installed (the token path is the only place
    that needs it, and it is already guarded by the 503 in the demo router when creds are absent).

    When `room_metadata` is given (the already-captured consent JSON the voice worker seeds its gate
    from — never secrets), it is attached via the token's room-config metadata (RoomConfiguration):
    LiveKit stamps that metadata onto the room when the room is created from this token, so the worker
    reads it off ctx.room.metadata and opens its ConsentGate (the U13 deadlock fix). A None metadata
    keeps the prior plain-token behavior."""
    from livekit import api as lk_api

    grants = lk_api.VideoGrants(room_join=True, room=room)
    token = (
        lk_api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
    )
    if room_metadata:
        # Carry consent on the room config so it lands on ctx.room.metadata for the worker to seed.
        token = token.with_room_config(lk_api.RoomConfiguration(metadata=room_metadata))
    return token.to_jwt()


# A module-level app for `uvicorn src.api.server:app` (prod uses env-built OpenRouterClient).
app = create_app()
