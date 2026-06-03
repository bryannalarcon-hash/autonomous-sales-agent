# The single LLM seam every unit calls through (plan KTD2/KTD5). Defines the LLMClient protocol
# (async complete() + a complete_json() helper that tolerates fenced ```json blocks + a STREAMING
# complete_stream() that yields content deltas), an OpenRouterClient (OpenAI-compatible: base_url
# https://openrouter.ai/api/v1, bearer auth from OPENROUTER_API_KEY, default model from AGENT_MODEL;
# async httpx, with a BOUNDED deterministic retry-with-backoff on transient 429/5xx/transport errors
# — FINDING 2), and a MockLLMClient (scripted list OR callable) for deterministic, network-free tests.
# CB-41 (stream NLG -> TTS): complete_stream() POSTs with "stream": true and parses the SSE
# `data:` lines from client.stream(), yielding each choices[].delta.content token as it arrives so the
# voice worker can speak the first clause at NLG first-token instead of after the whole reply. The
# blocking complete() is KEPT for the text/self-play path + the DST/policy JSON calls. Keeping every
# call behind this protocol lets the agent brain (Claude) and the sim/judge (a different family)
# swap by config without touching core logic, and lets tests inject scripted deltas.
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

import httpx
from dotenv import load_dotenv

# Messages are OpenAI-style chat dicts: {"role": "system"|"user"|"assistant", "content": str}.
Message = dict[str, str]

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT_S = 60.0
# The agent model used when neither a constructor `model=` nor env AGENT_MODEL is set. MUST be a real
# slug: a previous empty-string default made OpenRouterClient() POST `"model": ""`, which OpenRouter
# rejects with 400 on EVERY call — silently degrading the whole brain (DST/policy/NLG) to fallbacks
# (the /api/chat path gave the same canned line for every question). Kept in sync with src.voice.agent.
DEFAULT_AGENT_MODEL = "anthropic/claude-sonnet-4.5"

# Bounded retry policy for transient OpenRouter failures (FINDING 2). A 429 (rate limit) or any 5xx
# server error, and transport-level errors (timeout/connection), are retried with a DETERMINISTIC
# exponential backoff (base * 2**attempt) so the behavior is testable — no random jitter. A 4xx
# client error (bad request / auth) is NOT retried. After the budget is spent the error re-raises.
_DEFAULT_MAX_RETRIES = 2  # total attempts = 1 + _DEFAULT_MAX_RETRIES (<=3)
_DEFAULT_RETRY_BACKOFF_BASE_S = 0.5
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Pulls a JSON object out of a reply that may be wrapped in a ```json ... ``` fence or surrounded
# by prose. We match the first balanced-looking object; complete_json validates by json.loads.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_BRACE_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Parse the first JSON value in `text`, tolerating ```json fences and surrounding prose.

    Raises ValueError (not a bare JSONDecodeError) so callers can treat 'no parseable JSON' as a
    single, explicit failure mode and decide to leave state unchanged.
    """
    if text is None:
        raise ValueError("cannot parse JSON from a None reply")
    candidates: list[str] = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    stripped = text.strip()
    candidates.append(stripped)
    braced = _BRACE_RE.search(text)
    if braced:
        candidates.append(braced.group(1))
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
    raise ValueError(f"no parseable JSON in LLM reply: {text!r}")


def _sse_content_delta(data_line: str) -> Optional[str]:
    """Parse ONE OpenAI/OpenRouter SSE `data:` payload into its content delta, or None.

    A streaming chat-completion sends `data: {json}` lines; the JSON carries
    choices[0].delta.content (the incremental token) or, at the end, choices[0].finish_reason. The
    terminal sentinel is the literal `data: [DONE]`. Returns the content token string (possibly "")
    when present, or None for a line that carries no content (DONE, role-only first delta, a keep-
    alive comment, or unparseable JSON) so the caller can simply skip it. CB-41 — used by
    OpenRouterClient.complete_stream to turn the raw SSE stream into a token iterator.
    """
    line = (data_line or "").strip()
    if not line:
        return None
    # SSE comment / keep-alive (": OPENROUTER PROCESSING") — not a data event.
    if line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line[len("data:"):].strip()
    if not line or line == "[DONE]":
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    try:
        choices = obj.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
    except (AttributeError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


@runtime_checkable
class LLMClient(Protocol):
    """The seam every LLM call goes through. Implementations are async.

    complete() returns the whole assistant text; complete_json() is a thin helper that parses that
    text as JSON (used by the DST driver-delta update and the policy proposal). complete_stream()
    (CB-41) yields the assistant text in incremental content tokens as they arrive — the voice NLG
    path consumes it so TTS speaks the first clause at first-token instead of after the full reply.
    """

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        **opts: Any,
    ) -> str: ...

    async def complete_json(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        **opts: Any,
    ) -> Any: ...

    def complete_stream(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        **opts: Any,
    ) -> AsyncIterator[str]: ...


class _JsonHelperMixin:
    """Shares one complete_json implementation: call complete() then parse the reply as JSON."""

    async def complete_json(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        **opts: Any,
    ) -> Any:
        # Nudge OpenAI-compatible backends toward valid JSON; harmless for the mock.
        opts.setdefault("response_format", {"type": "json_object"})
        text = await self.complete(messages, model=model, **opts)  # type: ignore[attr-defined]
        return _extract_json(text)


class OpenRouterClient(_JsonHelperMixin):
    """OpenAI-compatible chat client pointed at OpenRouter (one key -> many model families).

    Reads OPENROUTER_API_KEY + AGENT_MODEL from the environment (.env via python-dotenv) unless
    overridden in the constructor. Non-streaming: one POST to /chat/completions per complete().
    The API key lives only on the instance and in the Authorization header — never logged.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: str = OPENROUTER_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT_S,
        extra_headers: Optional[dict[str, str]] = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        retry_backoff_base: float = _DEFAULT_RETRY_BACKOFF_BASE_S,
        _load_env: bool = True,
    ) -> None:
        if _load_env:
            load_dotenv()
        self.api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "OpenRouterClient requires an API key: pass api_key= or set OPENROUTER_API_KEY"
            )
        # Resolve the model: explicit arg > env AGENT_MODEL > DEFAULT_AGENT_MODEL. NEVER "" — an empty
        # model makes OpenRouter 400 every request, silently breaking the brain (see DEFAULT_AGENT_MODEL).
        self.model = (model or os.environ.get("AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        # Bounded retry budget + deterministic backoff base (seconds). max_retries is the number of
        # RETRIES after the first attempt, so total attempts = 1 + max_retries.
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_base = max(0.0, float(retry_backoff_base))

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional but recommended).
            "HTTP-Referer": "https://github.com/nerdy/auto-sales-agent",
            "X-Title": "auto-sales-agent",
        }
        headers.update(self.extra_headers)
        return headers

    def _backoff_seconds(self, attempt: int, retry_after: Optional[str]) -> float:
        """Deterministic backoff for a given retry attempt (0-based). A server-sent Retry-After
        header (seconds) wins; otherwise base * 2**attempt. No random jitter — kept testable."""
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass  # malformed header -> fall back to computed backoff
        return self.retry_backoff_base * (2 ** attempt)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        **opts: Any,
    ) -> str:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            **opts,
        }
        if response_format is not None:
            body["response_format"] = response_format

        # Bounded retry-with-backoff (FINDING 2): retry transient 429/5xx + transport errors with a
        # deterministic exponential backoff; do NOT retry 4xx client errors; re-raise after the
        # budget is spent so a persistent outage surfaces (the callers degrade gracefully on it).
        last_exc: Exception
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url, timeout=self.timeout
                ) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                return data["choices"][0]["message"]["content"]
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUS or attempt >= self.max_retries:
                    raise  # non-retryable status, or budget exhausted -> surface it
                last_exc = exc
                retry_after = exc.response.headers.get("Retry-After")
                await asyncio.sleep(self._backoff_seconds(attempt, retry_after))
            except httpx.RequestError as exc:
                # Transport-level failure (timeout / connection reset) — retry until budget spent.
                if attempt >= self.max_retries:
                    raise
                last_exc = exc
                await asyncio.sleep(self._backoff_seconds(attempt, None))
        # Defensive: the loop either returns or raises; this only runs if the range is empty.
        raise last_exc  # pragma: no cover

    async def complete_stream(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        **opts: Any,
    ) -> AsyncIterator[str]:
        """STREAM a chat completion: yield each content token (choices[0].delta.content) as it arrives.

        CB-41 — the streaming twin of complete(). POSTs the SAME OpenAI-compatible body but with
        "stream": true and consumes the SSE response via httpx's client.stream(...), parsing each
        `data:` line with _sse_content_delta and yielding the non-empty content tokens in order. The
        concatenation of every yielded token equals what complete() would have returned for the same
        request (R37 parity is preserved by the caller asserting concat == reply). Bounded retry +
        error handling mirror complete(): a transient 429/5xx (before any token is yielded) or a
        transport error is retried with the SAME deterministic backoff; a non-retryable 4xx re-raises;
        the budget exhausting re-raises so the NLG layer can degrade to its safe filler. Once tokens
        have started flowing we do NOT retry (the partial output is already committed to the stream).
        The API key lives only in the Authorization header — never logged.
        """
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "stream": True,
            **opts,
        }
        if response_format is not None:
            body["response_format"] = response_format

        last_exc: Exception
        for attempt in range(self.max_retries + 1):
            yielded_any = False
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url, timeout=self.timeout
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers=self._headers(),
                    ) as resp:
                        resp.raise_for_status()
                        async for raw_line in resp.aiter_lines():
                            token = _sse_content_delta(raw_line)
                            if token:
                                yielded_any = True
                                yield token
                return
            except httpx.HTTPStatusError as exc:
                # If a status surfaced we have not started yielding (raise_for_status runs before the
                # first token), so retrying is safe. Honor the same retryable-status / budget rules.
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUS or attempt >= self.max_retries:
                    raise
                last_exc = exc
                retry_after = exc.response.headers.get("Retry-After")
                await asyncio.sleep(self._backoff_seconds(attempt, retry_after))
            except httpx.RequestError as exc:
                # A transport error AFTER tokens started would truncate the reply mid-stream; do not
                # retry then (the partial is already out). Only retry a connect/read error that hit
                # before any token was yielded, until the budget is spent.
                if yielded_any or attempt >= self.max_retries:
                    raise
                last_exc = exc
                await asyncio.sleep(self._backoff_seconds(attempt, None))
        raise last_exc  # pragma: no cover


# A scripted entry is either a precomputed string reply or a callable(messages, **opts) -> str.
ScriptItem = Union[str, Callable[..., Union[str, Awaitable[str]]]]
Script = Union[Sequence[ScriptItem], Callable[..., Union[str, Awaitable[str]]]]


# SSE-delta token splitter for the mock stream: chunk a resolved reply into word-ish tokens that
# PRESERVE whitespace so ''.join(tokens) == reply (parity), giving tests a genuine multi-token stream
# without a network. Mirrors how a real model emits sub-word/word deltas (the exact split is opaque to
# callers — only the reassembled string is load-bearing).
def _mock_stream_tokens(text: str) -> list[str]:
    """Split `text` into ordered tokens whose concatenation is EXACTLY `text` (whitespace kept)."""
    if not text:
        return []
    tokens = re.findall(r"\S+\s*", text)
    return tokens or [text]


class MockLLMClient(_JsonHelperMixin):
    """Deterministic, network-free LLM stand-in for tests (plan: MockLLMClient(scripted)).

    `scripted` is either a list consumed one entry per complete()/complete_stream() call (a str reply
    or a callable(messages, **opts) -> str), or a single callable applied to every call. Each call's
    messages are recorded on .calls so tests can assert what the DST/policy actually sent.
    Exhausting a list raises IndexError (loud), never silently reuses the last reply. complete_stream()
    (CB-41) resolves the SAME scripted reply complete() would and yields it as whitespace-preserving
    word tokens, so a streamed turn produces >1 chunk and the tokens rejoin to the exact reply.
    """

    def __init__(self, scripted: Script) -> None:
        self._scripted = scripted
        self._is_callable = callable(scripted)
        self._i = 0
        self.calls: list[list[Message]] = []
        # CB-41: separate counters so a streamed call and a blocking call both advance a list script
        # consistently (the streaming path calls _resolve, which bumps _i exactly like complete()).
        self.stream_calls: list[list[Message]] = []

    def _resolve(
        self,
        messages: Sequence[Message],
        model: Optional[str],
        response_format: Optional[dict[str, Any]],
        opts: dict[str, Any],
    ) -> str:
        """Resolve the next scripted reply (str or callable) — shared by complete + complete_stream."""
        call_opts = dict(opts)
        if model is not None:
            call_opts["model"] = model
        if response_format is not None:
            call_opts["response_format"] = response_format

        if self._is_callable:
            item: ScriptItem = self._scripted  # type: ignore[assignment]
        else:
            seq = self._scripted  # type: ignore[assignment]
            item = seq[self._i]  # IndexError if exhausted — intentional
            self._i += 1

        if callable(item):
            result = item(messages, **call_opts)
            return result  # may be awaitable; the caller awaits it
        return str(item)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        **opts: Any,
    ) -> str:
        self.calls.append(list(messages))
        result = self._resolve(messages, model, response_format, opts)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        return str(result)

    async def complete_stream(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        **opts: Any,
    ) -> AsyncIterator[str]:
        """Yield the next scripted reply as whitespace-preserving word tokens (CB-41, no network).

        Resolves the reply EXACTLY like complete() (advancing a list script by one), then chunks it so
        the consumer sees a genuine multi-token stream whose concatenation is the verbatim reply.
        """
        self.calls.append(list(messages))
        self.stream_calls.append(list(messages))
        result = self._resolve(messages, model, response_format, opts)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        for token in _mock_stream_tokens(str(result)):
            yield token
