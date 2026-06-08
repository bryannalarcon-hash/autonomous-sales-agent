# test_cb78_llm_client_reuse.py — CB-78: OpenRouterClient reuses ONE httpx client per event loop
# (was: a fresh AsyncClient + TLS handshake per LLM call — 3+ handshakes per conversation turn).
# Loop-affinity is the trap: a client cached from a dead test loop must be replaced, not reused.
import asyncio

import pytest

from src.core.llm import OpenRouterClient


def _client_obj(orc: OpenRouterClient):
    async def grab():
        return orc._client()

    return asyncio.run(grab())


def test_same_loop_reuses_one_client():
    orc = OpenRouterClient(api_key="test-key", model="test/model", _load_env=False)

    async def two_grabs():
        return orc._client(), orc._client()

    a, b = asyncio.run(two_grabs())
    assert a is b


def test_new_loop_gets_fresh_client():
    orc = OpenRouterClient(api_key="test-key", model="test/model", _load_env=False)
    first = _client_obj(orc)   # loop 1 (closed when asyncio.run returns)
    second = _client_obj(orc)  # loop 2 — must NOT reuse loop 1's client
    assert first is not second  # cache holds only the latest loop's client


def test_closed_client_is_replaced_within_same_loop():
    orc = OpenRouterClient(api_key="test-key", model="test/model", _load_env=False)

    async def grab_close_grab():
        a = orc._client()
        await a.aclose()
        b = orc._client()
        return a, b

    a, b = asyncio.run(grab_close_grab())
    assert a is not b
    assert b.is_closed is False


def test_client_carries_base_url_and_timeout():
    orc = OpenRouterClient(
        api_key="test-key", model="test/model", _load_env=False, timeout=12.5
    )
    c = _client_obj(orc)
    assert str(c.base_url).startswith(orc.base_url)
    assert c.timeout.read == pytest.approx(12.5)
