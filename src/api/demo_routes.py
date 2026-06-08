# The /demo call routes for the thin FastAPI surface (plan U13; R1/R3/R33/R40/R41/R42 + CB-87),
# extracted from src.api.server so that module stays a slim app-assembler. create_demo_router()
# builds an APIRouter carrying the EXACT, frozen JSON contract the Next.js demo depends on:
# consent/start + consent/respond drive the ConsentGate (AI disclosure, recording consent,
# minor->parental, with detect_minor run on available signals so a suspected minor is gated
# INDEPENDENT of a client is_minor flag, R40); /api/chat (GATED 409 by consent) runs one brain
# turn via VoiceSession — it detect_minors the user text first (a self-reported minor flips
# need_parental -> 409), buffers/persists an escalation with a PII-scrubbed moment, and reports
# `done` for terminal acts; CB-87 (corrects CB-82): `done` fires ONLY on genuinely terminal acts
# (enrollment close, escalate, disqualify) — sub-enrollment closes (callback/consultation/trial)
# do NOT set done (the conversation continues), but DO persist + appear in /operate via the live-
# upsert stamping the booked outcome (last_close_tier -> persist_call_live with outcome stamp) and
# the lead checkpoint (phone_hash upserted mid-call so an abandoned chat keeps the lead, CB-86);
# raw phone NEVER stored, sha256 hash only (R42); CB-63: /api/chat
# now STREAMS tokens as SSE when the client sends Accept: text/event-stream (progressive render +
# typing indicator, eliminating the 5.7–9.1 s silence). SSE events: `token` chunks during NLG
# generation, then a terminal `done` event carrying {reply, decision_act, done} — the same payload
# as the buffered JSON fallback (backward-compatible: existing clients that POST without Accept:
# text/event-stream get unchanged JSON). Streaming reuses handle_user_turn_stream (the VoiceSession
# streaming twin of handle_user_turn) then commit_turn + post-turn hooks (escalation, close-tier,
# live upsert) AFTER the stream completes — so consent gating, minor detection, timing stamps, cost
# accounting, and turn persistence are IDENTICAL to the buffered path. LIVE PERSISTENCE (Layer 2):
# after each /api/chat turn the live_upsert_hook (default: persist_call_live) upserts an in_progress
# Episode using a stable episode_id + created_at stamped at session creation, so the Live monitor
# can query the call WHILE it is happening; POST /api/demo/auto/start (CB-08 + CB-12) kicks off a
# SERVER-SIDE demo call (consent pre-captured) that runs a REAL self-play — the agent brain vs an
# LLM-GENERATED ProspectSimulator (src.sim, NOT a fixed script, so each demo varies) — streaming
# each committed turn through that same per-turn live-upsert path as a background task, so the Live
# monitor populates without a phone AND the demo survives the operator navigating away (it is not
# browser-bound); GET /api/demo/auto/stream?episode_id=<id> (CB-12) is an SSE stream that emits a
# per-turn `turn` event (and a terminal `end` event) so the live page appends turns in near-real-time
# instead of waiting on its 5 s poll (which stays a fallback); CB-21 adds a `generating` event
# emitted BEFORE each brain turn (so the monitor shows a "Generating…" spinner during the ~6 s
# compose window) and rides the committed reply text on the `turn` event so the monitor can REVEAL it
# word-by-word (a token-stream-like effect on the demo path); /api/livekit/token (503 without creds)
# mints a token via the injected builder AND stamps the already-captured consent onto the room
# metadata (consent_state/recording_granted/jurisdiction/phone_hash/conversable — no secrets) so the
# live voice worker SEEDS its ConsentGate from it instead of deadlocking on a pending gate waiting
# for spoken consent; /api/session/{id}/end persists the Episode (+escalations+lead) FK-safely
# (using the same episode_id so the final save upserts the in_progress row) and EVICTS the session
# from the bounded LRU store (503 over cap); GET /api/session/{id} + /api/health are introspection.
# Everything network/DB-bound is injected from create_app (LLM factory, embedder, retrieve/hydrate/
# escalation/call-end/live-upsert hooks, token builder) so tests run offline. PII is scrubbed in
# the VoiceSession before storage; the lead key is the phone-hash (raw phone never stored, R42).
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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
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

# Server-side auto-demo (POST /api/demo/auto/start): a REAL self-play the backend plays against the
# brain so the Live monitor populates turn-by-turn WITHOUT a phone and, crucially, INDEPENDENT of the
# browser (it survives the operator navigating away + back). CB-12: the prospect side is now
# LLM-GENERATED (src.sim.ProspectSimulator) rather than a fixed script — so each demo is a varied
# self-play (a fresh qualified persona + a different sim-model run each time), and the agent's close
# is honestly earned through the deterministic buy_gate. The CB-08 outcome_override still applies: if
# the agent closed at any point the demo lands terminal (e.g. "Consultation booked"), never in_progress.
#
# Gap BETWEEN turns (on top of the ~6 s real-brain latency per turn) so the operator can watch each
# turn land in the monitor and the heartbeat stays fresh. Kept short — the brain is the real pacer.
_AUTO_DEMO_TURN_GAP_S = 1.5
# Hard turn budget for a generated demo so a non-closing run can't stream forever. A qualified
# prospect typically closes well within this; reaching it ends the demo "released" (terminal).
_AUTO_DEMO_MAX_TURNS = 20
# The agent's canned opener (turn 0 — no brain call yet, mirroring src.sim.selfplay._opener). The
# generated prospect responds to THIS first.
_AUTO_DEMO_OPENER = "Hi, this is Alex from Nerdy. How can I help you today?"


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
    # CB-12: the terminal outcome a GENERATED demo reached on the PROSPECT side when no agent close
    # tier applies — "walked" (patience exhausted) or "released" (turn budget hit / no terminal act).
    # Used as the outcome_override at finalize so a non-closing demo still lands terminal (never
    # in_progress). A real close (last_close_tier) takes precedence over this.
    demo_terminal_override: Optional[str] = None
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
        """Upsert an Episode after each successful turn (Layer 2 live persistence — CB-87 extended).

        Uses the session's stable live_episode_id + live_created_at so every upsert lands on the
        same row. The final /end persist uses the same episode_id so it finalizes that same row.
        Only fires when live_upsert_hook is set (mirrors the escalation_persist_hook pattern —
        tests inject a capture hook; prod create_app wires persist_call_live). Skips silently when
        no hook is provided so /api/chat stays DB-free when not opted in. Failures are swallowed.

        CB-87: passes last_close_tier (the most-recently-offered close tier, if any) and phone_hash
        (the session's bound lead hash, if any) to the hook so persist_call_live can (a) stamp the
        booked outcome instead of "in_progress" once a close was offered (the episode appears in
        /operate with its outcome while the conversation continues), and (b) upsert a minimal Lead row
        so an abandoned-after-booking chat keeps the lead even if /end never fires (absorbs CB-86)."""
        if sess.voice_session is None or live_upsert_hook is None:
            return
        try:
            await live_upsert_hook(
                sess.voice_session,
                config=cfg,
                channel=sess.channel,
                episode_id=sess.live_episode_id,
                created_at=sess.live_created_at,
                last_close_tier=sess.last_close_tier,
                phone_hash=sess.phone_hash,
            )
        except Exception:
            pass  # swallow — persist_call_live already logs; never crash the call path

    # --- Server-side auto-demo (POST /api/demo/auto/start) -------------------------------------
    # Holds strong refs to in-flight demo tasks (asyncio only weakly references tasks, so without
    # this a demo could be garbage-collected mid-run). Each task discards itself on completion.
    _demo_tasks: "set[asyncio.Task[Any]]" = set()

    # CB-12 real-time streaming: per-episode fan-out of SSE subscriber queues. _run_auto_demo PUSHES a
    # small event ("turn"/"end") after each committed turn / at finalize; each open SSE connection for
    # that episode_id has a bounded queue here that it drains and forwards to the browser. A dict of
    # LISTS so multiple monitors can watch the same demo. Queues are bounded so a slow/abandoned client
    # can't grow memory without bound (overflow events are dropped — the 5 s poll reconciles).
    _demo_streams: "dict[str, list[asyncio.Queue[dict[str, Any]]]]" = {}
    _DEMO_STREAM_QUEUE_MAX = 64

    def _publish_demo_event(episode_id: str, event: dict[str, Any]) -> None:
        """Fan an SSE event out to every subscriber queue for this episode (non-blocking). A full
        queue (slow client) drops the event rather than blocking the demo runner — the per-turn live
        upsert + the page's 5 s poll are the durable source of truth, SSE is only the low-latency nudge."""
        for q in _demo_streams.get(episode_id, ()):  # iterate a snapshot-safe view
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow/abandoned subscriber — drop; the poll will reconcile

    async def _finalize_auto_demo(sess: _Session) -> None:
        """Persist the finished auto-demo terminal + evict it — mirrors /end's persist branch but runs
        server-side. Passes the stable created_at so duration_ms is recorded. Idempotent on `ended`;
        swallows failures so a finalize hiccup never leaks the background task. Emits the SSE "end"
        event so a subscribed monitor refreshes once more (the page then freezes the ended call, CB-13)."""
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
                # the LAST turn wasn't the close — so the demo lands in the Calls list as e.g.
                # "Consultation booked", not in_progress (CB-08 override; episode_from_session ignores
                # the override once a real close is on the transcript). With CB-12's generated prospect
                # a non-closing run instead ends "walked"/"released" (demo_terminal_override) so it is
                # ALWAYS terminal, never in_progress.
                if sess.last_close_tier:
                    override = derive_outcome("attempt_close", sess.last_close_tier)[0]
                else:
                    override = sess.demo_terminal_override
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
            # Tell any SSE subscriber the demo is over (last refresh + close), then drop the gate/session.
            _publish_demo_event(sess.live_episode_id, {"event": "end"})
            try:
                sess.gate.end()
            except Exception:
                pass
            sessions.pop(sess.session_id, None)

    def _make_demo_prospect() -> Any:
        """Build the LLM-GENERATED prospect for one demo (CB-12). A fresh QUALIFIED persona (so the
        agent can realistically earn a close) sampled with a per-run seed so each demo VARIES; the
        sim's commit/walk is still the deterministic buy_gate, so honesty holds. Imported lazily so
        the demo router stays cheap to import (the sim package is pure: no LiveKit/DB)."""
        from src.sim.personas import sample_population
        from src.sim.prospect import ProspectSimulator

        seed = uuid.uuid4().int & 0xFFFFFFFF  # vary the persona + tie-breaking run to run
        persona = sample_population(1, seed=seed, unqualified_fraction=0.0)[0]
        return ProspectSimulator(persona, seed=seed)

    async def _run_auto_demo(sess: _Session) -> None:
        """Background task: a REAL self-play (CB-12) — the agent BRAIN alternates with an LLM-GENERATED
        ProspectSimulator, streaming each committed turn through the SAME per-turn live-upsert path
        /api/chat uses AND an SSE per-turn event, paced so the operator watches the monitor populate.
        Runs server-side so it SURVIVES the operator navigating away. The prospect's commit/walk is the
        deterministic buy_gate (honesty preserved); the agent's close sets last_close_tier so the CB-08
        outcome_override forces a terminal outcome. ALWAYS finalizes (terminal persist + evict) in a
        finally so no in_progress shell dangles."""
        from src.sim.prospect import SIM_MODEL

        ep_id = sess.live_episode_id
        prospect = _make_demo_prospect()
        prospect_llm = make_llm()  # the prospect side uses SIM_MODEL on the same injected client/seam
        try:
            vs = _ensure_voice_session(sess)
            # Turn 0: the agent's canned opener (no brain call yet — mirrors selfplay._opener). The
            # generated prospect responds to this first.
            agent_text = _AUTO_DEMO_OPENER
            agent_act: Optional[str] = "greeting"
            agent_tier: Optional[str] = None

            reached_terminal = False
            for _turn in range(_AUTO_DEMO_MAX_TURNS):
                if sess.ended:
                    return
                # 1. PROSPECT generates its turn (LLM talk + clamped soft deltas; commit/walk via the
                #    pure buy_gate against the agent's last offered tier).
                try:
                    pt = await prospect.respond(
                        agent_text, agent_act, prospect_llm, agent_tier=agent_tier, model=SIM_MODEL
                    )
                except Exception:
                    logger.warning("auto-demo prospect turn failed for session %s", sess.session_id)
                    break
                prospect_text = pt.text or "…"
                # A WALK is terminal on the prospect side (no close tier applies) — remember it so the
                # finalize forces a terminal "walked" outcome (never in_progress).
                if pt.walked:
                    sess.demo_terminal_override = "walked"

                # 2. Feed the prospect's turn to the agent brain (commit writes the transcript: a
                #    prospect turn + the agent's closing/confirming reply). The brain is REAL (no mock).
                sess._speech_counter += 1
                speech_id = f"s-{sess._speech_counter}"
                # CB-21(a): signal that the agent is now COMPOSING this turn, BEFORE the ~6 s brain
                # round-trip. The monitor shows a "Generating…" spinner on the active turn for the whole
                # compose window (cleared by the matching "turn" event below) so there is never a
                # multi-second blank gap that reads as the agent being stuck.
                _publish_demo_event(ep_id, {"event": "generating", "turn": sess._speech_counter})
                try:
                    pending = await vs.handle_user_turn(prospect_text, speech_id)
                except ConsentError:
                    break
                committed = vs.commit_turn(speech_id)
                decided = committed if committed is not None else pending

                # Surface the call in the Live monitor (heartbeat upsert) + nudge SSE subscribers so
                # the prospect+agent bubbles appear in near-real-time, not on the 5 s poll. CB-21(b):
                # the agent's just-composed reply rides on the "turn" event so the monitor can REVEAL it
                # word-by-word (a token-stream-like effect on the demo SSE path) instead of one batch.
                # This is the REAL committed reply text (no fabrication); real-voice token streaming is
                # deferred (it needs core LLM + LiveKit TTS streaming, outside this path).
                await _live_upsert(sess)
                _publish_demo_event(
                    ep_id,
                    {"event": "turn", "turn": sess._speech_counter, "text": decided.reply_text},
                )

                # 3. The prospect COMMITTED (the deterministic buy_gate fired in response to the agent's
                #    close) or WALKED -> terminal. The agent's close already set last_close_tier (commit)
                #    or demo_terminal_override is "walked", so the finalize persists a terminal outcome.
                if pt.committed_tier is not None or pt.walked:
                    reached_terminal = True
                    break

                # 4. Record the agent's move for the NEXT prospect turn; track a close tier so the
                #    finalize can force a terminal outcome (CB-08).
                agent_text = decided.reply_text
                agent_act = decided.decision.act
                agent_tier = decided.decision.tier
                if decided.decision.act == "attempt_close":
                    sess.last_close_tier = decided.decision.tier
                # 5. A hard terminal agent act (disqualify / escalate) ends the call — its own decision
                #    is terminal (derive_outcome handles it), so no override is needed.
                if decided.decision.act in ("disqualify", "escalate"):
                    reached_terminal = True
                    break

                await asyncio.sleep(_AUTO_DEMO_TURN_GAP_S)

            # Hit the turn budget without any terminal -> "released" (a real ended call, never a
            # lingering in_progress), unless the agent had closed (last_close_tier wins at finalize).
            if not reached_terminal and not sess.last_close_tier:
                sess.demo_terminal_override = sess.demo_terminal_override or "released"
        except Exception:
            logger.warning("auto-demo runner crashed for session %s", sess.session_id)
        finally:
            await _finalize_auto_demo(sess)

    @router.post("/api/demo/auto/start", response_model=AutoDemoResponse)
    async def demo_auto_start() -> AutoDemoResponse:
        """Kick off a SERVER-SIDE demo call — a REAL self-play vs an LLM-GENERATED prospect (CB-08 +
        CB-12). Returns immediately with the stable episode_id; a background task streams the call into
        the Live monitor (the /api/live/active poll surfaces it; /api/demo/auto/stream pushes per-turn
        events) and it survives the operator navigating away. 503 when the real brain is unavailable
        (no OPENROUTER_API_KEY) or the bounded session store is at capacity."""
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

    @router.get("/api/demo/auto/stream")
    async def demo_auto_stream(episode_id: str) -> StreamingResponse:
        """Server-Sent-Events stream of per-turn nudges for ONE demo episode (CB-12 real-time).

        The page subscribes for the SELECTED call and, on each event, re-fetches the live snapshot —
        so prospect/agent bubbles appear within sub-second of the server committing them instead of
        only on the 5 s poll (which stays as a fallback). Events are NAMED:
          - `event: turn`  data: {"turn": <n>}   — a turn was committed + live-upserted.
          - `event: end`   data: {}              — the demo went terminal; the client refreshes once
                                                    more, then the page freezes the ended call (CB-13).
        A periodic `: keep-alive` comment keeps intermediaries from closing an idle connection.
        Disconnect-safe: the subscriber queue is always removed in `finally`, and the runner DROPS
        events for a full/slow queue rather than blocking, so a dead client never wedges a demo.
        """
        queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue(maxsize=_DEMO_STREAM_QUEUE_MAX)
        _demo_streams.setdefault(episode_id, []).append(queue)

        async def _event_gen() -> Any:
            try:
                # An initial comment opens the stream immediately (some clients wait for first bytes).
                yield ": stream-open\n\n"
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"  # idle heartbeat; keeps proxies from dropping us
                        continue
                    name = str(evt.get("event", "turn"))
                    data = {k: v for k, v in evt.items() if k != "event"}
                    yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
                    if name == "end":
                        break  # terminal — close the stream cleanly
            finally:
                # Always detach this subscriber so _demo_streams doesn't leak queues per connection.
                subs = _demo_streams.get(episode_id)
                if subs is not None and queue in subs:
                    subs.remove(queue)
                    if not subs:
                        _demo_streams.pop(episode_id, None)

        return StreamingResponse(
            _event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",  # disable nginx proxy buffering so events flush promptly
                "Connection": "keep-alive",
            },
        )

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

    # --- POST /api/chat (GATED by consent) -------------------------------------------------------
    #
    # CB-63 dual-mode: when the client sends Accept: text/event-stream the response streams tokens
    # as SSE (progressive render; first visible text within ~1–2 s on a warm path). When the client
    # sends a plain JSON Accept (or none), the original buffered JSON response is returned unchanged
    # — existing tests and non-streaming clients keep working.
    #
    # SSE event shape (CB-63 streaming path):
    #   event: token   data: "word "          — one NLG delta as it generates
    #   event: done    data: {"reply":…,"decision_act":…,"done":…}  — final payload after all tokens
    #
    # CONSENT GATING fires BEFORE any tokens flow (same as buffered path). R40 minor detection also
    # runs before the brain starts. Both paths commit state, run escalation, record the close tier,
    # and fire the live upsert via _post_turn_hooks() so semantics are identical.

    async def _post_turn_hooks(sess: "_Session", vs: Any, speech_id: str) -> tuple[Any, bool]:
        """Run the post-turn hooks that are identical for both the buffered and streaming paths.

        Calls commit_turn (the single writer of session state), handles an escalate decision (U14),
        tracks the close tier (CB-08), fires the live upsert (Layer 2), and returns (committed, done).
        Extracted to a helper so the streaming and buffered /api/chat handlers share the SAME logic
        without duplication. `committed` is the PendingTurn returned by commit_turn (it is the same
        object that was in the pending buffer, now promoted to committed state)."""
        committed = vs.commit_turn(speech_id)
        # `committed` IS the promoted PendingTurn (or None if the speech_id was not pending). It
        # carries the gated decision + assembled reply — use it for all downstream checks.

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

        # CB-87 (corrects CB-82): `done` ENDS the interactive conversation, so it must reflect the
        # HUMAN accepting — not merely the AGENT offering. In interactive /api/chat, `committed` is the
        # AGENT's gated turn: act == "attempt_close" means the agent OFFERED a close (e.g. "would you
        # like me to book that?"), NOT that the prospect accepted. CB-82 ended the chat on that offer,
        # cutting the human off mid-booking (live QA: both convos died on the agent's "which day?"
        # question). So `done` fires only on genuinely terminal states: an enrollment close (the
        # strongest, accepted commit), escalate, or disqualify. Sub-enrollment bookings (callback /
        # consultation / trial) DO persist + appear in /operate — but via the live-upsert stamping the
        # booked outcome (not in_progress) and the lead checkpoint on contact-capture (CB-86), NOT by
        # ending the conversation. This decouples PERSISTENCE from CONVERSATION-END (CB-82's error).
        done = committed is not None and (
            committed.decision.act in ("disqualify", "escalate")
            or (committed.decision.act == "attempt_close" and committed.decision.tier == "enrollment")
        )
        return committed, bool(done)

    def _wants_sse(request: Request) -> bool:
        """True when the client signals it accepts SSE (Accept: text/event-stream)."""
        accept = request.headers.get("accept", "")
        return "text/event-stream" in accept

    @router.post("/api/chat")
    async def chat(req: ChatRequest, request: Request) -> Any:
        sess = _get(req.session_id)
        # Suspected-minor detection on the user's text (R40), INDEPENDENT of any is_minor flag: a
        # self-reported school-aged caller flips the gate to need_parental and the turn is GATED (409),
        # so the brain never runs for an un-consented minor. Runs BEFORE the brain on BOTH paths.
        if sess.gate.can_converse and detect_minor({"text": req.text}):
            sess.gate.flag_minor()
        if not sess.gate.can_converse:
            # 409: consent not satisfied (pending / need_parental / ended). Fires BEFORE any token
            # flows on BOTH paths — the SSE path does NOT open the stream then 409 partway through.
            raise HTTPException(
                status_code=409,
                detail={"reason": "consent_not_satisfied", "state": sess.gate.state},
            )
        vs = _ensure_voice_session(sess)
        sess._speech_counter += 1
        speech_id = f"s-{sess._speech_counter}"

        # --- CB-63 STREAMING PATH (Accept: text/event-stream) ------------------------------------
        if _wants_sse(request):
            async def _sse_gen() -> Any:
                """Yield SSE `token` events during NLG, then a terminal `done` event.

                The consent gate + minor detection already fired above (before the stream opens) so
                no token ever flows for an un-consented session. After the token stream ends, commit_turn
                and post-turn hooks run INSIDE the generator so they complete before the `done` event
                closes the connection — ensuring timing stamps, cost accounting, and live upsert are
                persisted synchronously before the client fires /end."""
                try:
                    async for token in vs.handle_user_turn_stream(req.text, speech_id):
                        # Each NLG delta forwarded to the client as a `token` SSE event. JSON-encode
                        # the token string so the client can safely parse it (handles quotes/newlines).
                        yield f"event: token\ndata: {json.dumps(token)}\n\n"
                except ConsentError as exc:
                    # Consent flipped mid-stream (extremely rare; handle_user_turn_stream raises
                    # ConsentError if the gate blocks BEFORE generation). Emit an error event so
                    # the client knows this is a 409, then close cleanly.
                    err = {"error": "consent_not_satisfied", "state": exc.state}
                    yield f"event: error\ndata: {json.dumps(err)}\n\n"
                    return
                except Exception as exc:
                    err = {"error": "stream_failed", "detail": str(exc)}
                    yield f"event: error\ndata: {json.dumps(err)}\n\n"
                    return

                # Stream complete — run post-turn hooks (commit, escalation, live upsert).
                try:
                    decided, done = await _post_turn_hooks(sess, vs, speech_id)
                except Exception:
                    # A hook failure must not swallow the reply; emit what we have.
                    decided, done = None, False

                reply = decided.reply_text if decided is not None else ""
                act = decided.decision.act if decided is not None else ""
                payload = json.dumps({"reply": reply, "decision_act": act, "done": done})
                yield f"event: done\ndata: {payload}\n\n"

            return StreamingResponse(
                _sse_gen(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        # --- BUFFERED JSON FALLBACK (original path, unchanged) -----------------------------------
        try:
            pending = await vs.handle_user_turn(req.text, speech_id)
        except ConsentError as exc:
            raise HTTPException(
                status_code=409,
                detail={"reason": "consent_not_satisfied", "state": exc.state},
            ) from exc

        decided, done = await _post_turn_hooks(sess, vs, speech_id)
        # `pending` carries the decision / reply from handle_user_turn; `decided` is the same turn
        # promoted by commit_turn inside _post_turn_hooks. Use pending for the reply (it was computed
        # first and is identical — R37 parity), decided for done-flag (commit may mutate tier bookkeeping).
        return ChatResponse(
            reply=pending.reply_text,
            decision_act=pending.decision.act,
            done=done,
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
