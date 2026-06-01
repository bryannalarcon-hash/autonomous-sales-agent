# The single LLM seam every unit calls through (plan KTD2/KTD5). Defines the LLMClient protocol
# (async complete() + a complete_json() helper that tolerates fenced ```json blocks), an
# OpenRouterClient (OpenAI-compatible: base_url https://openrouter.ai/api/v1, bearer auth from
# OPENROUTER_API_KEY, default model from AGENT_MODEL; async httpx, non-streaming, with a BOUNDED
# deterministic retry-with-backoff on transient 429/5xx/transport errors — FINDING 2), and a
# MockLLMClient (scripted list OR callable) for deterministic, network-free tests. Keeping every
# call behind this protocol lets the agent brain (Claude) and the sim/judge (a different family)
# swap by config without touching core logic, and lets tests inject scripted deltas.
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence, Union, runtime_checkable

import httpx
from dotenv import load_dotenv

# Messages are OpenAI-style chat dicts: {"role": "system"|"user"|"assistant", "content": str}.
Message = dict[str, str]

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT_S = 60.0

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


@runtime_checkable
class LLMClient(Protocol):
    """The seam every LLM call goes through. Implementations must be async and non-streaming here.

    complete() returns the assistant text; complete_json() is a thin helper that parses that text
    as JSON (used by the DST driver-delta update and, later, the policy proposal).
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
        self.model = model or os.environ.get("AGENT_MODEL", "")
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


# A scripted entry is either a precomputed string reply or a callable(messages, **opts) -> str.
ScriptItem = Union[str, Callable[..., Union[str, Awaitable[str]]]]
Script = Union[Sequence[ScriptItem], Callable[..., Union[str, Awaitable[str]]]]


class MockLLMClient(_JsonHelperMixin):
    """Deterministic, network-free LLM stand-in for tests (plan: MockLLMClient(scripted)).

    `scripted` is either a list consumed one entry per complete() call (a str reply or a
    callable(messages, **opts) -> str), or a single callable applied to every call. Each call's
    messages are recorded on .calls so tests can assert what the DST/policy actually sent.
    Exhausting a list raises IndexError (loud), never silently reuses the last reply.
    """

    def __init__(self, scripted: Script) -> None:
        self._scripted = scripted
        self._is_callable = callable(scripted)
        self._i = 0
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        **opts: Any,
    ) -> str:
        self.calls.append(list(messages))
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
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]
            return str(result)
        return str(item)
