# tests/unit/test_token_dispatch.py — regression for NAMED LiveKit agent dispatch ("prioritize the
# deployed worker"). _build_livekit_token must attach a RoomAgentDispatch(agent_name=...) to the
# token's room config when agent_name is set, so LiveKit dispatches the demo room to THAT specific
# worker (the Railway one) instead of load-balancing across every registered worker. When agent_name
# is empty it must stay on default auto-dispatch (local parity, backward-compatible). Requires
# livekit-api, so it skips under the KB-blind .venv and runs under /usr/bin/python3 (grounded env).
import base64
import json

import pytest

pytest.importorskip("livekit.api")  # skip where livekit isn't installed (the .venv test run)
from src.api.server import _build_livekit_token  # noqa: E402

_KEY = "devkey"
_SECRET = "devsecretdevsecretdevsecretdevsecret0123"  # length-safe dummy HMAC secret


def _jwt_payload(tok: str) -> dict:
    """Decode (no verify) the JWT payload segment so we can inspect the embedded room config."""
    seg = tok.split(".")[1]
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def test_named_agent_dispatch_present_when_agent_name_set():
    tok = _build_livekit_token(
        _KEY, _SECRET, "probe", "demo-x", room_metadata=None, agent_name="cadence-prod"
    )
    blob = json.dumps(_jwt_payload(tok))
    assert "cadence-prod" in blob, "named agent dispatch missing from token claims"


def test_auto_dispatch_when_agent_name_empty():
    tok = _build_livekit_token(
        _KEY, _SECRET, "probe", "demo-x", room_metadata=None, agent_name=""
    )
    assert "cadence-prod" not in json.dumps(_jwt_payload(tok))


def test_consent_metadata_still_carried_with_named_dispatch():
    # Named dispatch must NOT drop the room metadata the worker seeds its ConsentGate from.
    tok = _build_livekit_token(
        _KEY, _SECRET, "probe", "demo-x", room_metadata='{"ai":true}', agent_name="cadence-prod"
    )
    blob = json.dumps(_jwt_payload(tok))
    assert "cadence-prod" in blob and "ai" in blob
