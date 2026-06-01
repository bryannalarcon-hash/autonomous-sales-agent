#!/usr/bin/env python3
# One-time setup for INBOUND PSTN via LiveKit Phone Numbers: creates a SIP DISPATCH RULE that routes
# an inbound phone call into a per-call LiveKit room (room_prefix "phone-") with OUR voice worker
# dispatched into it, so a phone caller reaches the same brain the web/text path uses. Run ONCE after
# buying + linking a LiveKit Phone Number in the LiveKit Cloud dashboard. LiveKit Phone Numbers are
# INBOUND-only / US-only and need NO inbound trunk — the dispatch rule alone wires the number's calls
# to a room. Idempotent-ish: it skips creation if a rule with the same name already exists. Reads
# LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from the env (.env loaded if present); NEVER
# prints secrets. Verified against the installed livekit-api 1.1.0 SDK (CreateSIPDispatchRuleRequest
# + SIPDispatchRuleIndividual + RoomConfiguration.agents=[RoomAgentDispatch]; sip.create_dispatch_rule
# / list_dispatch_rule are the non-deprecated coroutines).
#
# Usage (either form, from the repo root):
#     PYTHONPATH=. python3 -m scripts.setup_sip_dispatch
#     python3 scripts/setup_sip_dispatch.py
# Optional env overrides:
#     SIP_DISPATCH_RULE_NAME   (default "auto-sales-inbound")  — the rule's name (idempotency key)
#     SIP_ROOM_PREFIX          (default "phone-")              — per-call rooms become phone-<call_id>
#     SIP_DISPATCH_AGENT_NAME  (default "")                    — agent_name to dispatch; "" = the
#                                                                worker's DEFAULT auto-dispatch (the
#                                                                worker runs with an empty agent_name)
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

# The whole SDK surface used here is verified present in livekit-api 1.1.0 (see header).
from livekit import api

RULE_NAME = os.environ.get("SIP_DISPATCH_RULE_NAME", "auto-sales-inbound")
ROOM_PREFIX = os.environ.get("SIP_ROOM_PREFIX", "phone-")
# The worker (src/voice/worker.py) registers with an EMPTY agent_name -> default auto-dispatch, so the
# default here is "" (no explicit agent name needed). Set this if you run the worker with a name.
AGENT_NAME = os.environ.get("SIP_DISPATCH_AGENT_NAME", "")


def _require_env() -> tuple[str, str, str]:
    """Return (url, api_key, api_secret) from the env or exit(2) with a clear message (no secrets).

    LiveKitAPI also reads LIVEKIT_* itself, but we check explicitly so a missing credential is an
    obvious, actionable error rather than a confusing connection failure deep in the SDK."""
    url = os.environ.get("LIVEKIT_URL", "").strip()
    key = os.environ.get("LIVEKIT_API_KEY", "").strip()
    secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    missing = [n for n, v in (("LIVEKIT_URL", url), ("LIVEKIT_API_KEY", key),
                              ("LIVEKIT_API_SECRET", secret)) if not v]
    if missing:
        print(f"ERROR: missing required env var(s): {', '.join(missing)}", file=sys.stderr)
        print("Set them in .env or the environment, then re-run. (Values are never printed.)",
              file=sys.stderr)
        raise SystemExit(2)
    return url, key, secret


async def _existing_rule_id(lk: "api.LiveKitAPI", name: str) -> str | None:
    """Return the id of an existing dispatch rule with this `name`, else None (idempotency check).

    Uses the non-deprecated sip.list_dispatch_rule; tolerant of an empty list. The rule `name` is our
    idempotency key — re-running the script with the same name will NOT create a duplicate."""
    resp = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
    for rule in resp.items:
        if rule.name == name:
            return rule.sip_dispatch_rule_id
    return None


async def setup() -> None:
    """Create (idempotently) the inbound SIP dispatch rule + agent dispatch. Prints what it did.

    The rule is an INDIVIDUAL rule (room_prefix) so each inbound call lands in its own
    phone-<call_id> room, and room_config.agents dispatches OUR worker into that room. If a rule with
    the same name already exists, we skip creation and report the existing id (no duplicate)."""
    url, key, secret = _require_env()

    lk = api.LiveKitAPI(url, key, secret)
    try:
        existing = await _existing_rule_id(lk, RULE_NAME)
        if existing is not None:
            print(f"Dispatch rule {RULE_NAME!r} already exists (id={existing}); nothing to do.")
            return

        # INDIVIDUAL rule: every inbound call gets its own room named "<prefix><call_id>".
        rule = api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix=ROOM_PREFIX)
        )
        # Dispatch OUR worker into the created room. agent_name="" matches the worker's DEFAULT
        # auto-dispatch (the worker runs with an empty agent_name); set SIP_DISPATCH_AGENT_NAME to
        # target a named worker instead.
        room_config = api.RoomConfiguration(
            agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]
        )
        request = api.CreateSIPDispatchRuleRequest(
            rule=rule,
            name=RULE_NAME,
            room_config=room_config,
        )
        info = await lk.sip.create_dispatch_rule(request)
        print(f"Created SIP dispatch rule {RULE_NAME!r} (id={info.sip_dispatch_rule_id}).")
        print(f"  - inbound calls route to individual rooms: {ROOM_PREFIX}<call_id>")
        print(f"  - agent dispatched: agent_name={AGENT_NAME!r} "
              f"({'DEFAULT auto-dispatch' if AGENT_NAME == '' else 'named worker'})")
        print("Next: buy + link a LiveKit Phone Number in the dashboard (if you haven't), start the "
              "worker (PYTHONPATH=. python3 -m src.voice.worker dev), then dial the number.")
    finally:
        await lk.aclose()


def main() -> None:
    load_dotenv()
    asyncio.run(setup())


if __name__ == "__main__":
    main()
