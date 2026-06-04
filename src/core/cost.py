# cost.py (CB-52) — per-call LLM cost accounting. The OpenRouterClient reports each call's
# usage.cost (real OpenRouter credits, ~USD) here; we keep an in-process accumulator (grand total +
# per-model + token counts) so a run can print its OWN spend, and — when env LLM_COST_LOG names a
# file — append one JSONL line per call for a durable, cross-process ledger that feeds
# docs/eval-budget-ledger.md. Best-effort and NEVER raises into the LLM call path (cost tracking must
# never break a turn). Pure stdlib: no network, no DB, no LiveKit. `python -m src.core.cost <jsonl>`
# rolls a log up into per-model totals + a grand total.
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

# Env var naming the JSONL ledger file. Unset -> only the in-memory accumulator is kept (no file I/O),
# so tests and the API/voice paths never write a file unless explicitly opted in.
LOG_ENV = "LLM_COST_LOG"

_LOCK = threading.Lock()
_total_cost = 0.0
_calls = 0
_prompt_tokens = 0
_completion_tokens = 0
_by_model: dict[str, dict[str, float]] = {}


def record_usage(model: str, usage: Optional[dict[str, Any]]) -> None:
    """Record ONE LLM call's usage. Best-effort — NEVER raises (a cost-tracking hiccup must not break
    a turn). `usage` is the OpenRouter response `usage` object; when usage.include was requested it
    carries `cost` (credits ~ USD) + prompt/completion token counts. Accumulates the in-process totals
    and, if LLM_COST_LOG is set, appends one JSONL line {ts, model, prompt_tokens, completion_tokens,
    cost}. A None/garbage usage (e.g. the mock client, or a response without usage) is a silent no-op."""
    try:
        if not isinstance(usage, dict):
            return
        raw = usage.get("cost")
        cost = float(raw) if isinstance(raw, (int, float)) else 0.0
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        key = model or "?"
        global _total_cost, _calls, _prompt_tokens, _completion_tokens
        with _LOCK:
            _total_cost += cost
            _calls += 1
            _prompt_tokens += pt
            _completion_tokens += ct
            m = _by_model.setdefault(key, {"cost": 0.0, "calls": 0})
            m["cost"] += cost
            m["calls"] += 1
            path = os.environ.get(LOG_ENV)
            if path:
                line = json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "model": key,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "cost": cost,
                })
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
    except Exception:
        return  # cost tracking must never break the LLM call path


def snapshot() -> dict[str, Any]:
    """The in-process running totals since the last reset() (one process = one eval run)."""
    with _LOCK:
        return {
            "cost": round(_total_cost, 6),
            "calls": _calls,
            "prompt_tokens": _prompt_tokens,
            "completion_tokens": _completion_tokens,
            "by_model": {
                k: {"cost": round(v["cost"], 6), "calls": int(v["calls"])}
                for k, v in _by_model.items()
            },
        }


def reset() -> None:
    """Zero the in-process accumulator (call at the start of a run / test for a clean per-run total)."""
    global _total_cost, _calls, _prompt_tokens, _completion_tokens
    with _LOCK:
        _total_cost = 0.0
        _calls = 0
        _prompt_tokens = 0
        _completion_tokens = 0
        _by_model.clear()


def format_snapshot() -> str:
    """One-line human summary of the current run's spend (for a runner to print at the end)."""
    s = snapshot()
    by = "; ".join(
        f"{m}=${d['cost']:.4f}/{d['calls']}c" for m, d in sorted(s["by_model"].items())
    )
    return (
        f"RUN COST: ${s['cost']:.4f} over {s['calls']} LLM calls "
        f"({s['prompt_tokens']}+{s['completion_tokens']} tok)" + (f" — {by}" if by else "")
    )


def rollup(path: str) -> dict[str, Any]:
    """Roll a JSONL cost log up into {cost, calls, by_model} (cross-process durable totals)."""
    total = 0.0
    calls = 0
    by: dict[str, dict[str, float]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                c = float(row.get("cost") or 0.0)
                total += c
                calls += 1
                m = by.setdefault(str(row.get("model") or "?"), {"cost": 0.0, "calls": 0})
                m["cost"] += c
                m["calls"] += 1
    except FileNotFoundError:
        pass
    return {
        "cost": round(total, 6),
        "calls": calls,
        "by_model": {k: {"cost": round(v["cost"], 6), "calls": int(v["calls"])} for k, v in by.items()},
    }


def main(argv: Optional[list[str]] = None) -> int:
    """`python -m src.core.cost [path.jsonl]` — print a per-model rollup of a cost log."""
    import sys

    args = argv if argv is not None else sys.argv[1:]
    path = args[0] if args else os.environ.get(LOG_ENV, "")
    if not path:
        print("usage: python -m src.core.cost <cost-log.jsonl>  (or set LLM_COST_LOG)")
        return 2
    r = rollup(path)
    print(f"{path}: ${r['cost']:.4f} over {r['calls']} calls")
    for m, d in sorted(r["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
        print(f"  {m:35} ${d['cost']:.4f}  ({d['calls']} calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
