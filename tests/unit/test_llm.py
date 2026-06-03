# U3 tests (TEST-FIRST) for the shared LLM seam (src/core/llm.py). Covers: the MockLLMClient
# returns scripted/callable output deterministically with NO network; complete_json parses a JSON
# reply (including fenced ```json blocks); and the OpenRouterClient constructs the correct
# OpenAI-compatible request (base_url, bearer auth, model, messages, response_format) WITHOUT
# hitting the network — httpx is monkeypatched so no real call is made.
# CB-41 (stream NLG -> TTS): also covers OpenRouterClient.complete_stream SSE parsing — a stubbed
# httpx client.stream() yields `data: {json}` lines and the client must yield each
# choices[0].delta.content token in order (skipping role-only deltas, keep-alive comments, and the
# terminal `data: [DONE]`), with the concatenation equal to the full reply (R37 parity at the seam);
# plus the MockLLMClient.complete_stream word-token stream (>1 token, rejoins to the exact reply).
from __future__ import annotations

import json

import httpx
import pytest

from src.core.llm import MockLLMClient, OpenRouterClient


# --- MockLLMClient: deterministic, no network -------------------------------------------------

async def test_mock_returns_scripted_list_in_order():
    """A scripted list is consumed in order, one reply per complete() call."""
    mock = MockLLMClient(["first", "second"])
    msgs = [{"role": "user", "content": "hi"}]
    assert await mock.complete(msgs) == "first"
    assert await mock.complete(msgs) == "second"
    # The recorded calls are inspectable for assertions in other tests.
    assert mock.calls == [msgs, msgs]


async def test_mock_exhausted_script_raises():
    """Running past the end of the script is a loud error, not silent reuse."""
    mock = MockLLMClient(["only"])
    await mock.complete([{"role": "user", "content": "x"}])
    with pytest.raises(IndexError):
        await mock.complete([{"role": "user", "content": "y"}])


async def test_mock_callable_receives_messages_and_opts():
    """A callable script can branch on the messages/opts it is handed (deterministic)."""

    def responder(messages, **opts):
        # Echo the model passed through so the test can assert opts propagate.
        return f"model={opts.get('model')}|last={messages[-1]['content']}"

    mock = MockLLMClient(responder)
    out = await mock.complete(
        [{"role": "user", "content": "ping"}], model="test-model"
    )
    assert out == "model=test-model|last=ping"


async def test_mock_complete_json_parses_plain_json():
    """complete_json parses a raw JSON object reply into a dict."""
    mock = MockLLMClient([json.dumps({"trust": 0.2, "bail_risk": -0.1})])
    out = await mock.complete_json([{"role": "user", "content": "deltas?"}])
    assert out == {"trust": 0.2, "bail_risk": -0.1}


async def test_mock_complete_json_parses_fenced_block():
    """complete_json tolerates a ```json fenced code block (common LLM formatting)."""
    fenced = "Here you go:\n```json\n{\"price_sensitivity\": 0.3}\n```\nThanks!"
    mock = MockLLMClient([fenced])
    out = await mock.complete_json([{"role": "user", "content": "deltas?"}])
    assert out == {"price_sensitivity": 0.3}


async def test_mock_complete_json_invalid_raises():
    """A non-JSON reply surfaces a clear ValueError rather than corrupt state."""
    mock = MockLLMClient(["not json at all"])
    with pytest.raises(ValueError):
        await mock.complete_json([{"role": "user", "content": "x"}])


# --- OpenRouterClient: correct request, NO network --------------------------------------------

class _FakeResponse:
    """Minimal stand-in for httpx.Response capturing what a real call would parse."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeAsyncClient:
    """Records the single POST a real httpx.AsyncClient would make; returns a canned reply."""

    last_init_kwargs: dict = {}
    last_post: dict = {}

    def __init__(self, **kwargs):
        _FakeAsyncClient.last_init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, json=None, headers=None):
        _FakeAsyncClient.last_post = {"url": url, "json": json, "headers": headers}
        return _FakeResponse(
            {"choices": [{"message": {"content": "ASSISTANT_REPLY"}}]}
        )


async def test_openrouter_builds_correct_request(monkeypatch):
    """OpenRouterClient posts to the OpenAI-compatible endpoint with bearer auth + model + messages,
    and returns the assistant content — all WITHOUT a real network call (httpx is faked)."""
    import src.core.llm as llm_mod

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeAsyncClient)

    client = OpenRouterClient(api_key="sk-test-123", model="anthropic/claude-sonnet-4.5")
    msgs = [{"role": "user", "content": "hello"}]
    reply = await client.complete(msgs, temperature=0.0)

    assert reply == "ASSISTANT_REPLY"

    post = _FakeAsyncClient.last_post
    # Endpoint is the OpenAI-compatible chat/completions under the OpenRouter base_url.
    assert post["url"] == "https://openrouter.ai/api/v1/chat/completions"
    # Bearer auth from the provided key — never echoed elsewhere.
    assert post["headers"]["Authorization"] == "Bearer sk-test-123"
    # Body carries the model, the messages verbatim, and the passthrough opt.
    body = post["json"]
    assert body["model"] == "anthropic/claude-sonnet-4.5"
    assert body["messages"] == msgs
    assert body["temperature"] == 0.0
    # Default base_url is wired onto the AsyncClient (or used to build the absolute URL).
    assert post["url"].startswith("https://openrouter.ai/api/v1")


async def test_openrouter_passes_response_format(monkeypatch):
    """response_format is forwarded into the request body for JSON-mode calls."""
    import src.core.llm as llm_mod

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeAsyncClient)

    client = OpenRouterClient(api_key="sk-test-123", model="m")
    await client.complete(
        [{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
    )
    body = _FakeAsyncClient.last_post["json"]
    assert body["response_format"] == {"type": "json_object"}


async def test_openrouter_model_override_per_call(monkeypatch):
    """A per-call model= overrides the client default without mutating the client."""
    import src.core.llm as llm_mod

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeAsyncClient)

    client = OpenRouterClient(api_key="sk-test-123", model="default-model")
    await client.complete([{"role": "user", "content": "x"}], model="override-model")
    assert _FakeAsyncClient.last_post["json"]["model"] == "override-model"
    assert client.model == "default-model"  # unchanged


async def test_openrouter_requires_api_key():
    """Constructing without any key (env empty) fails fast rather than at first call."""
    with pytest.raises(ValueError):
        OpenRouterClient(api_key="", model="m", _load_env=False)


async def test_openrouter_never_defaults_to_empty_model(monkeypatch):
    """REGRESSION: with no model= and no AGENT_MODEL env, the client must fall back to a REAL model,
    never "". An empty model made OpenRouter 400 every request, silently degrading the whole brain
    (DST/policy/NLG) to canned fallbacks — the /api/chat path answered every question identically."""
    import src.core.llm as llm_mod

    monkeypatch.delenv("AGENT_MODEL", raising=False)
    client = OpenRouterClient(api_key="sk-test-123", _load_env=False)
    assert client.model == llm_mod.DEFAULT_AGENT_MODEL
    assert client.model  # non-empty
    # And env AGENT_MODEL still wins when present.
    monkeypatch.setenv("AGENT_MODEL", "anthropic/claude-3.5-sonnet")
    assert OpenRouterClient(api_key="sk-test-123", _load_env=False).model == "anthropic/claude-3.5-sonnet"


async def test_openrouter_complete_json_parses(monkeypatch):
    """complete_json on the real client parses the assistant content as JSON (network faked)."""
    import src.core.llm as llm_mod

    class _JsonClient(_FakeAsyncClient):
        async def post(self, url, *, json=None, headers=None):
            _FakeAsyncClient.last_post = {"url": url, "json": json, "headers": headers}
            return _FakeResponse(
                {"choices": [{"message": {"content": "{\"trust\": 0.5}"}}]}
            )

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _JsonClient)
    client = OpenRouterClient(api_key="sk-test-123", model="m")
    out = await client.complete_json([{"role": "user", "content": "x"}])
    assert out == {"trust": 0.5}


# --- FINDING 2: bounded retry-with-backoff on transient httpx errors --------------------------

class _ErrorResponse(_FakeResponse):
    """An httpx.Response stand-in whose raise_for_status() raises an HTTPStatusError (429/5xx)."""

    def __init__(self, status_code: int, *, headers: dict | None = None):
        super().__init__({})
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        raise httpx.HTTPStatusError(
            f"server error {self.status_code}", request=request, response=self  # type: ignore[arg-type]
        )


def _scripted_client(responses):
    """Build a fake AsyncClient class whose successive .post() calls pop from `responses`
    (each entry is either a callable() -> response, or a response object). Records attempt count."""

    state = {"i": 0, "attempts": 0}

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, json=None, headers=None):
            state["attempts"] += 1
            item = responses[state["i"]]
            state["i"] += 1
            if callable(item):
                return item()
            return item

    _Client.state = state  # type: ignore[attr-defined]
    return _Client


async def test_openrouter_retries_then_succeeds_on_transient_5xx(monkeypatch):
    """A transient 503 is retried (bounded), and a subsequent 200 returns the assistant reply.
    No real sleeping: asyncio.sleep is patched to record the deterministic backoff durations."""
    import asyncio as _asyncio

    import src.core.llm as llm_mod

    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep)

    client_cls = _scripted_client(
        [
            _ErrorResponse(503),  # first attempt: transient server error
            _FakeResponse({"choices": [{"message": {"content": "RECOVERED"}}]}),  # retry succeeds
        ]
    )
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", client_cls)

    client = OpenRouterClient(api_key="sk-test-123", model="m", retry_backoff_base=0.5)
    reply = await client.complete([{"role": "user", "content": "x"}])

    assert reply == "RECOVERED"
    assert client_cls.state["attempts"] == 2  # one failure + one success
    # Deterministic backoff (NOT random): exactly one sleep before the single retry.
    assert slept == [0.5]


async def test_openrouter_raises_after_exhausting_retries_on_429(monkeypatch):
    """Persistent 429s exhaust the bounded retry budget (<=3 attempts) and re-raise, never hang."""
    import src.core.llm as llm_mod

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep)

    client_cls = _scripted_client([_ErrorResponse(429) for _ in range(5)])
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", client_cls)

    client = OpenRouterClient(api_key="sk-test-123", model="m", retry_backoff_base=0.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.complete([{"role": "user", "content": "x"}])
    # Bounded: at most 3 total attempts (1 + 2 retries), never an unbounded loop.
    assert client_cls.state["attempts"] == 3


async def test_openrouter_retries_on_request_error_timeout(monkeypatch):
    """A transport-level RequestError (timeout/connection) is also retried, then succeeds."""
    import src.core.llm as llm_mod

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep)

    def _raise_timeout():
        raise httpx.ConnectTimeout("timed out")

    client_cls = _scripted_client(
        [
            _raise_timeout,  # first attempt: transport error raised inside post()
            _FakeResponse({"choices": [{"message": {"content": "OK"}}]}),
        ]
    )
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", client_cls)

    client = OpenRouterClient(api_key="sk-test-123", model="m", retry_backoff_base=0.0)
    reply = await client.complete([{"role": "user", "content": "x"}])
    assert reply == "OK"
    assert client_cls.state["attempts"] == 2


async def test_openrouter_honors_retry_after_header(monkeypatch):
    """A Retry-After header (seconds) overrides the computed backoff for that retry (deterministic)."""
    import src.core.llm as llm_mod

    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep)

    client_cls = _scripted_client(
        [
            _ErrorResponse(429, headers={"Retry-After": "2"}),
            _FakeResponse({"choices": [{"message": {"content": "DONE"}}]}),
        ]
    )
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", client_cls)

    client = OpenRouterClient(api_key="sk-test-123", model="m", retry_backoff_base=0.5)
    reply = await client.complete([{"role": "user", "content": "x"}])
    assert reply == "DONE"
    # Honored Retry-After (2s), NOT the computed 0.5s base backoff.
    assert slept == [2.0]


async def test_openrouter_does_not_retry_on_4xx_client_error(monkeypatch):
    """A non-retryable 4xx (e.g. 400/401) is NOT retried — it raises immediately (one attempt)."""
    import src.core.llm as llm_mod

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep)

    client_cls = _scripted_client([_ErrorResponse(400) for _ in range(3)])
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", client_cls)

    client = OpenRouterClient(api_key="sk-test-123", model="m", retry_backoff_base=0.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.complete([{"role": "user", "content": "x"}])
    assert client_cls.state["attempts"] == 1  # 400 is a client error; do not retry


# --- CB-41: complete_stream SSE parsing (stubbed httpx stream, NO network) --------------------


def _sse_lines(reply_tokens: list[str], *, include_role_delta: bool = True) -> list[str]:
    """Build the raw SSE `data:` lines an OpenRouter streamed chat-completion sends for `reply_tokens`.

    Mirrors the real wire shape: an optional role-only first delta (no content), then one
    content-delta line per token, a finish_reason line, and the terminal `data: [DONE]`. A keep-alive
    SSE comment is interleaved to prove the parser skips it."""
    lines: list[str] = []
    if include_role_delta:
        lines.append('data: {"choices":[{"delta":{"role":"assistant"},"index":0}]}')
    lines.append(": OPENROUTER PROCESSING")  # keep-alive comment — must be skipped
    for tok in reply_tokens:
        lines.append('data: ' + json.dumps({"choices": [{"delta": {"content": tok}, "index": 0}]}))
    lines.append('data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}')
    lines.append("data: [DONE]")
    return lines


class _FakeStreamResponse:
    """A stand-in for the httpx streaming Response context manager (client.stream(...) result).

    Supports `async with`, raise_for_status(), and aiter_lines() yielding the scripted SSE lines."""

    def __init__(self, lines: list[str], *, status_code: int = 200,
                 headers: dict | None = None) -> None:
        self._lines = lines
        self.status_code = status_code
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=request, response=self  # type: ignore[arg-type]
            )
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamingFakeClient:
    """A fake httpx.AsyncClient whose .stream(...) returns a scripted _FakeStreamResponse.

    Records the POST body so a test can assert "stream": true was sent. Constructed per attempt."""

    last_body: dict = {}
    response_factory = None  # set per test to a callable() -> _FakeStreamResponse

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, *, json=None, headers=None):
        _StreamingFakeClient.last_body = json or {}
        assert _StreamingFakeClient.response_factory is not None
        return _StreamingFakeClient.response_factory()


async def test_complete_stream_yields_content_tokens_in_order(monkeypatch):
    """complete_stream POSTs stream=true and yields each choices[0].delta.content token in order,
    skipping the role-only first delta, the keep-alive comment, the finish_reason line, and [DONE].
    The concatenation of the yielded tokens equals the full reply (R37 parity at the LLM seam)."""
    import src.core.llm as llm_mod

    tokens = ["Sure", ", let", " me", " help", " you", "."]
    _StreamingFakeClient.response_factory = lambda: _FakeStreamResponse(_sse_lines(tokens))
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _StreamingFakeClient)

    client = OpenRouterClient(api_key="sk-test-123", model="anthropic/claude-sonnet-4.5")
    got = [t async for t in client.complete_stream([{"role": "user", "content": "hi"}])]

    assert got == tokens  # only the content tokens, in order (role/comment/DONE skipped)
    assert "".join(got) == "Sure, let me help you."  # parity: tokens rejoin to the full reply
    # The request body carried stream=true (and the model + messages verbatim).
    body = _StreamingFakeClient.last_body
    assert body["stream"] is True
    assert body["model"] == "anthropic/claude-sonnet-4.5"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


async def test_complete_stream_handles_no_role_delta_and_empty_content(monkeypatch):
    """A stream with NO role-only first delta and an interleaved empty-content delta still yields only
    the real content tokens (the empty-content delta contributes nothing); parity holds."""
    import src.core.llm as llm_mod

    lines = [
        'data: {"choices":[{"delta":{"content":"Hello"},"index":0}]}',
        'data: {"choices":[{"delta":{"content":""},"index":0}]}',   # empty content -> skipped
        'data: {"choices":[{"delta":{"content":" world"},"index":0}]}',
        "data: [DONE]",
    ]
    _StreamingFakeClient.response_factory = lambda: _FakeStreamResponse(lines)
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _StreamingFakeClient)

    client = OpenRouterClient(api_key="sk-test-123", model="m")
    got = [t async for t in client.complete_stream([{"role": "user", "content": "x"}])]
    assert got == ["Hello", " world"]
    assert "".join(got) == "Hello world"


async def test_complete_stream_retries_then_streams_on_transient_5xx(monkeypatch):
    """A transient 5xx surfaced by raise_for_status (BEFORE any token) is retried with the bounded,
    deterministic backoff; the retry then streams the tokens. Mirrors complete()'s retry policy."""
    import src.core.llm as llm_mod

    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep)

    tokens = ["ok", " then"]
    responses = [
        _FakeStreamResponse([], status_code=503),               # first attempt: transient 5xx
        _FakeStreamResponse(_sse_lines(tokens, include_role_delta=False)),  # retry succeeds
    ]
    state = {"i": 0}

    def _factory():
        resp = responses[state["i"]]
        state["i"] += 1
        return resp

    _StreamingFakeClient.response_factory = _factory
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _StreamingFakeClient)

    client = OpenRouterClient(api_key="sk-test-123", model="m", retry_backoff_base=0.5)
    got = [t async for t in client.complete_stream([{"role": "user", "content": "x"}])]
    assert got == tokens
    assert slept == [0.5]  # exactly one deterministic backoff before the single retry


async def test_complete_stream_does_not_retry_on_4xx(monkeypatch):
    """A non-retryable 4xx (surfaced by raise_for_status before any token) re-raises immediately —
    the NLG layer's try/except then degrades to its safe filler. No retry, no hang."""
    import src.core.llm as llm_mod

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _fake_sleep)

    _StreamingFakeClient.response_factory = lambda: _FakeStreamResponse([], status_code=400)
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _StreamingFakeClient)

    client = OpenRouterClient(api_key="sk-test-123", model="m", retry_backoff_base=0.0)
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in client.complete_stream([{"role": "user", "content": "x"}]):
            pass


# --- CB-41: MockLLMClient.complete_stream (deterministic word tokens, no network) -------------


async def test_mock_complete_stream_yields_word_tokens_rejoining_to_reply():
    """MockLLMClient.complete_stream yields the scripted reply as >1 whitespace-preserving token whose
    concatenation is the EXACT reply (so a streamed turn produces a genuine multi-token stream)."""
    reply = "Sure, let me get a few details about what your child is working on."
    mock = MockLLMClient([reply])
    tokens = [t async for t in mock.complete_stream([{"role": "user", "content": "x"}])]
    assert len(tokens) > 1
    assert "".join(tokens) == reply


async def test_mock_complete_stream_advances_a_list_script_like_complete():
    """complete_stream consumes one list entry (advancing the script), just like complete(), so mixing
    the two in a test stays deterministic."""
    mock = MockLLMClient(["first reply here", "second reply here"])
    first = "".join([t async for t in mock.complete_stream([{"role": "user", "content": "a"}])])
    second = await mock.complete([{"role": "user", "content": "b"}])
    assert first == "first reply here"
    assert second == "second reply here"


def test_sse_content_delta_parses_and_skips():
    """The pure _sse_content_delta helper: extracts content from a data line, returns None for the
    role-only delta / [DONE] / a keep-alive comment / unparseable JSON (so the caller simply skips)."""
    import src.core.llm as llm_mod

    f = llm_mod._sse_content_delta
    assert f('data: {"choices":[{"delta":{"content":"hi"}}]}') == "hi"
    assert f('{"choices":[{"delta":{"content":" there"}}]}') == " there"  # no `data:` prefix tolerated
    assert f('data: {"choices":[{"delta":{"role":"assistant"}}]}') is None  # role-only -> no content
    assert f("data: [DONE]") is None
    assert f(": OPENROUTER PROCESSING") is None  # keep-alive comment
    assert f("data: not json") is None
    assert f("") is None
