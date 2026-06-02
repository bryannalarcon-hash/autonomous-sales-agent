# The /demo call routes for the thin FastAPI surface (plan U13; R1/R3/R33/R40/R41/R42), extracted
# from src.api.server so that module stays a slim app-assembler. create_demo_router() builds an
# APIRouter carrying the EXACT, frozen JSON contract the Next.js demo depends on: consent/start +
# consent/respond drive the ConsentGate (AI disclosure, recording consent, minor->parental, with
# detect_minor run on available signals so a suspected minor is gated INDEPENDENT of a client
# is_minor flag, R40); /api/chat (GATED 409 by consent) runs one brain turn via VoiceSession — it
# detect_minors the user text first (a self-reported minor flips need_parental -> 409), buffers/persists
# an escalation with a PII-scrubbed moment, and reports `done` for terminal acts; LIVE PERSISTENCE
# (Layer 2): after each /api/chat turn the live_upsert_hook (default: persist_call_live) upserts an
# in_progress Episode using a stable episode_id + created_at stamped at session creation, so the Live
# monitor can query the call WHILE it is happening; POST /api/demo/auto/start (CB-08) kicks off a
# SERVER-SIDE scripted demo call (consent pre-captured) that streams the real brain through that same
# per-turn live-upsert path as a background task — so the Live monitor populates without a phone AND
# the demo survives the operator navigating away (it is not browser-bound); /api/livekit/token (503 without creds) mints a
# token via the injected builder AND stamps the already-captured consent onto the room metadata
# (consent_state/recording_granted/jurisdiction/phone_hash/conversable — no secrets) so the live
# voice worker SEEDS its ConsentGate from it instead of deadlocking on a pending gate waiting for
# spoken consent; /api/session/{id}/end persists the Episode (+escalations+lead) FK-safely (using
# the same episode_id so the final save upserts the in_progress row) and EVICTS the session from
# the bounded LRU store (503 over cap); GET /api/session/{id} + /api/health are introspection.
# Everything network/DB-bound is injected from create_app (LLM factory, embedder,
# retrieve/hydrate/escalation/call-end/live-upsert hooks, token builder) so tests run offline. PII
# is scrubbed in the VoiceSession before storage; the lead key is the phone-hash.
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

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
# Builds a LiveKit access token: (api_key, api_secret, identity, room, room_metadata?) -> jwt str.
# room_metadata is an optional small JSON string stamped onto the room (carries the already-captured
# consent for the worker to seed its gate; NEVER secrets). The builder accepts it as a keyword so an
# existing 4-arg builder still type-checks; the default impl ignores None.
TokenBuilder = Callable[..., str]
# Optional cross-call hydration: (raw_phone | phone_hash, config) -> a seeded BeliefState (U6).
HydrateHook = Callable[..., Awaitable[Any]]
# Optional escalation persist: (EscalationLog) -> awaitable (the U14 review-queue write).
EscalationPersistHook = Callable[..., Awaitable[Any]]
# Optional call-end persist: builds + saves the Episode (+ escalations + lead) from a finished session.
CallEndPersistHook = Callable[..., Awaitable[Any]]
# Optional live upsert: called after each /api/chat turn to upsert an in_progress Episode so the
# Live monitor can query calls mid-call. Receives the same kwargs shape as call_end_persist_hook
# plus the stable episode_id and created_at (both stamped once at session creation). Default:
# src.api.persistence.persist_call_live. Injecting a no-op or a capture keeps tests DB-free.
LiveUpsertHook = Callable[..., Awaitable[Any]]

# Hard cap on concurrently-tracked demo sessions. The store is a bounded LRU (OrderedDict): a /end
# evicts its session, and a fresh /consent/start over the cap is refused with 503 rather than letting
# the in-memory dict grow without bound. Overridable via MAX_DEMO_SESSIONS for load tests.
_DEFAULT_MAX_SESSIONS = 5000

# Server-side auto-demo (POST /api/demo/auto/start): a scripted, realistic QUALIFIED-parent arc the
# backend plays against the real brain so the Live monitor populates turn-by-turn WITHOUT a phone and,
# crucially, INDEPENDENT of the browser (it survives the operator navigating away + back). The arc
# warms toward an explicit move-forward so the close gate can fire (terminal "Consultation booked"
# rather than lingering in_progress). Agent replies are the real brain; only these lines are scripted.
_AUTO_DEMO_SCRIPT: tuple[str, ...] = (
    "Hi — my daughter's a high-school sophomore and she's really struggling with Algebra 2 right before finals.",
    "She does all the homework but still gets C's and D's on the tests, and her confidence is shot.",
    "We tried one of those free tutoring apps and she just didn't stick with it. What makes you different?",
    "That actually sounds like what she needs. How does the pricing work — is it worth it?",
    "Okay, that's reasonable. How soon could she actually start with someone?",
    "Great — let's go ahead and get her set up. What's the next step?",
    "Yes, I'm ready to move forward today — sign us up.",
    "Perfect. Please go ahead and book the first session for us.",
)
# Gap BETWEEN turns (on top of the ~6 s real-brain latency per turn) so the operator can watch each
# turn land in the monitor and the heartbeat stays fresh. Kept short — the brain is the real pacer.
_AUTO_DEMO_TURN_GAP_S = 1.5


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
    # session_id is ECHOED so the client has the live session id without threading its own state — the
    # /demo page reads it to unlock chat + voice. Omitting it left the page with sessionId=undefined,
    # so Send and "Start voice call" stayed permanently disabled after consent (the demo blocker).
    session_id: str
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


class AutoDemoResponse(BaseModel):
    # Returned by POST /api/demo/auto/start. episode_id is the stable live id the background demo will
    # stream into (the /api/live/active poll surfaces it); started is always True on a 200.
    episode_id: str
    started: bool


@dataclass
class _Session:
    """One in-memory demo session: the consent gate, the brain session (built lazily once consent
    permits conversation), the caller's phone-hash + assigned sticky voice, the channel, the
    escalation logs captured during the call (re-pointed onto the episode + persisted at call end),
    and the stable live-persistence identifiers (episode_id + created_at stamped once at session
    creation so every in_progress upsert AND the final /end upsert land on the same DB row)."""

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
    # Stable live-persistence identifiers: stamped ONCE at session creation and reused on every
    # in_progress upsert AND the final /end persist so both paths target the same episode row.
    live_episode_id: str = field(default_factory=lambda: f"ep-{uuid.uuid4().hex}")
    live_created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_OK_STATE_MESSAGES = {
    "ready": "Consent captured. The call may proceed and be recorded.",
    "unrecorded": "Recording declined. The call proceeds UNRECORDED.",
    "need_parental": "A parent or guardian must consent before we continue.",
    "ended": "The call has ended.",
    "pending": "Awaiting consent.",
    "ai_disclosed": "AI disclosure acknowledged; awaiting recording consent.",
}


def _consent_room_metadata(gate: ConsentGate, *, phone_hash_value: Optional[str]) -> str:
    """Serialize the already-captured consent into the small JSON the LiveKit room carries (no
    secrets), so the live voice worker can SEED its fresh ConsentGate from it instead of deadlocking
    on a pending gate waiting for spoken consent (the U13 deadlock fix).

    Shape (read by src.voice.worker._seed_gate_from_metadata): consent_state (the gate's state),
    recording_granted (bool — only True when recording was actually granted, so recording stays
    honest), jurisdiction (tunes the worker's disclosure emphasis), phone_hash (the lead key, a HASH
    not the raw phone — R42), and conversable (whether the brain may proceed). NEVER includes the
    LiveKit creds or any secret — only consent facts the worker needs to open its gate."""
    return json.dumps({
        "consent_state": gate.state,
        "recording_granted": gate.can_record,
        "jurisdiction": gate.jurisdiction,
        "phone_hash": phone_hash_value,
        "conversable": gate.can_converse,
    })


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
    live_upsert_hook: Optional[LiveUpsertHook] = None,
) -> APIRouter:
    """Build the /demo APIRouter with its own bounded in-memory session store.

    All network/DB seams are injected by create_app: make_llm builds a fresh LLMClient per session;
    active_retrieve_hook grounds /api/chat against the KB; hydrate_hook seeds a returning caller's slots
    at consent-start; escalation_persist_hook writes an `escalate` to the review queue;
    call_end_persist_hook saves the Episode at /end (defaults to persist_call_end);
    live_upsert_hook upserts an in_progress Episode after each /api/chat turn (defaults to
    persist_call_live) so the Live monitor can query calls mid-call. The router owns a bounded LRU
    `sessions` dict — /end evicts its session and a start over MAX_DEMO_SESSIONS returns 503.
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

    async def _live_upsert(sess: "_Session") -> None:
        """Upsert an in_progress Episode after each successful turn (Layer 2 live persistence).

        Uses the session's stable live_episode_id + live_created_at so every upsert lands on the
        same row. The final /end persist uses the same episode_id so it finalizes that same row.
        Only fires when live_upsert_hook is set (mirrors the escalation_persist_hook pattern —
        tests inject a capture hook; prod create_app wires persist_call_live). Skips silently when
        no hook is provided so /api/chat stays DB-free when not opted in. Failures are swallowed."""
        if sess.voice_session is None or live_upsert_hook is None:
            return
        try:
            await live_upsert_hook(
                sess.voice_session,
                config=cfg,
                channel=sess.channel,
                episode_id=sess.live_episode_id,
                created_at=sess.live_created_at,
            )
        except Exception:
            pass  # swallow — persist_call_live already logs; never crash the call path

    # --- Server-side auto-demo (POST /api/demo/auto/start) -------------------------------------
    # Holds strong refs to in-flight demo tasks (asyncio only weakly references tasks, so without
    # this a demo could be garbage-collected mid-run). Each task discards itself on completion.
    _demo_tasks: "set[asyncio.Task[Any]]" = set()

    async def _finalize_auto_demo(sess: _Session) -> None:
        """Persist the finished auto-demo terminal + evict it — mirrors /end's persist branch but runs
        server-side. Passes the stable created_at so duration_ms is recorded. Idempotent on `ended`;
        swallows failures so a finalize hiccup never leaks the background task."""
        if sess.ended:
            return
        sess.ended = True
        try:
            if sess.voice_session is not None:
                from src.api.persistence import derive_outcome

                persist = call_end_persist_hook
                if persist is None:
                    from src.api.persistence import persist_call_end as _pce

                    persist = _pce
                # If the agent closed at ANY point in the call, force that terminal outcome even when
                # the LAST turn wasn't the close (the script keeps talking past a mid-call close) — so
                # the demo lands in the Calls list as e.g. "Consultation booked", not in_progress.
                override = (
                    derive_outcome("attempt_close", sess.last_close_tier)[0]
                    if sess.last_close_tier
                    else None
                )
                await persist(
                    sess.voice_session,
                    config=cfg,
                    channel=sess.channel,
                    escalation_logs=list(sess.escalations),
                    last_tier=sess.last_close_tier,
                    episode_id=sess.live_episode_id,
                    created_at=sess.live_created_at,
                    outcome_override=override,
                )
        except Exception:
            logger.warning("auto-demo finalize failed for session %s", sess.session_id)
        finally:
            try:
                sess.gate.end()
            except Exception:
                pass
            sessions.pop(sess.session_id, None)

    async def _run_auto_demo(sess: _Session) -> None:
        """Background task: stream _AUTO_DEMO_SCRIPT through the REAL brain + the SAME per-turn
        live-upsert path /api/chat uses, paced so the operator watches the monitor populate. Runs
        server-side so it SURVIVES the operator navigating away. ALWAYS finalizes (terminal persist +
        evict) in a finally so no in_progress shell dangles."""
        try:
            vs = _ensure_voice_session(sess)
            for i, line in enumerate(_AUTO_DEMO_SCRIPT):
                if sess.ended:
                    return
                sess._speech_counter += 1
                speech_id = f"s-{sess._speech_counter}"
                try:
                    pending = await vs.handle_user_turn(line, speech_id)
                except ConsentError:
                    break
                committed = vs.commit_turn(speech_id)
                # Record that the agent closed (at whatever tier) — used by _finalize_auto_demo to force
                # a terminal outcome even if a LATER turn isn't a close. We run the WHOLE script (rather
                # than stopping on the first close) so the demo always shows a full, watchable arc: the
                # agent sometimes closes early (LLM variance) and a 2-turn dud would be a poor demo.
                decided = committed if committed is not None else pending
                if decided.decision.act == "attempt_close":
                    sess.last_close_tier = decided.decision.tier
                # Same heartbeat upsert /api/chat fires — surfaces the call in the Live monitor.
                await _live_upsert(sess)
                # A hard terminal act (disqualify / escalate) genuinely ends the call — stop there.
                if decided.decision.act in ("disqualify", "escalate"):
                    break
                if i < len(_AUTO_DEMO_SCRIPT) - 1:
                    await asyncio.sleep(_AUTO_DEMO_TURN_GAP_S)
        except Exception:
            logger.warning("auto-demo runner crashed for session %s", sess.session_id)
        finally:
            await _finalize_auto_demo(sess)

    @router.post("/api/demo/auto/start", response_model=AutoDemoResponse)
    async def demo_auto_start() -> AutoDemoResponse:
        """Kick off a SERVER-SIDE scripted demo call (CB-08). Returns immediately with the stable
        episode_id; a background task streams the call into the Live monitor (the /api/live/active
        poll surfaces it) and it survives the operator navigating away. 503 when the real brain is
        unavailable (no OPENROUTER_API_KEY) or the bounded session store is at capacity."""
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise HTTPException(
                status_code=503,
                detail="demo unavailable: the language-model key is not configured.",
            )
        if len(sessions) >= max_sessions:
            raise HTTPException(
                status_code=503,
                detail="demo session capacity reached; please retry shortly.",
            )
        # Consent PRE-CAPTURED (server-driven demo — no human consent UI): a non-minor accept, i.e.
        # AI disclosure acknowledged + recording granted -> the gate permits conversation.
        session_id = uuid.uuid4().hex
        gate = ConsentGate(jurisdiction="", refusal_policy=refusal_policy)
        gate.acknowledge_ai()
        gate.grant_recording()
        sess = _Session(session_id=session_id, gate=gate, channel="text")
        sessions[session_id] = sess
        task = asyncio.create_task(_run_auto_demo(sess))
        _demo_tasks.add(task)
        task.add_done_callback(_demo_tasks.discard)
        return AutoDemoResponse(episode_id=sess.live_episode_id, started=True)

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
            session_id=sess.session_id,
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

        # Live in-progress upsert (Layer 2): after each committed turn, upsert the in_progress
        # Episode so the Live monitor can query this call while it is happening. The stable
        # live_episode_id ensures all upserts + the final /end persist target the same row.
        await _live_upsert(sess)

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
        # Stamp the ALREADY-captured consent onto the room metadata so the voice worker seeds its
        # ConsentGate from it (no spoken-consent deadlock). The browser captured consent via
        # /api/consent/respond before this call, so sess.gate already reflects it. No secrets ride
        # along — only consent facts (R33/R41/R42). The builder applies it via the LiveKit token's
        # room-config metadata, which LiveKit stamps on the room the worker reads.
        room_metadata = _consent_room_metadata(sess.gate, phone_hash_value=sess.phone_hash)
        token = build_token(api_key, api_secret, req.identity, room, room_metadata=room_metadata)
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
        # reflects the call's recording status. Pass live_episode_id so the final save UPSERTS the
        # same row as all the in_progress live upserts (no duplicate, no orphan in_progress row).
        if call_end_persist_hook is not None:
            episode = await call_end_persist_hook(
                sess.voice_session,
                config=cfg,
                channel=sess.channel,
                phone_hash=sess.phone_hash,
                raw_phone=raw_phone,
                escalation_logs=list(sess.escalations),
                last_tier=sess.last_close_tier,
                episode_id=sess.live_episode_id,
                created_at=sess.live_created_at,
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
                episode_id=sess.live_episode_id,
                created_at=sess.live_created_at,
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
