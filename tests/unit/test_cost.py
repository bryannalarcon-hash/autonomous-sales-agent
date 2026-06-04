# Tests for src/core/cost.py (CB-52 per-call cost accounting): the in-process accumulator
# (record_usage/snapshot/reset/format_snapshot), best-effort no-raise on garbage, the optional
# JSONL ledger append gated by LLM_COST_LOG, and the rollup() of a log into per-model totals.
import json

from src.core import cost


def setup_function(_):
    cost.reset()


def test_record_and_snapshot_accumulates_cost_tokens_and_by_model():
    cost.record_usage("openai/gpt-5-nano", {"cost": 0.0001, "prompt_tokens": 100, "completion_tokens": 20})
    cost.record_usage("anthropic/claude-sonnet-4.5", {"cost": 0.012, "prompt_tokens": 800, "completion_tokens": 60})
    cost.record_usage("openai/gpt-5-nano", {"cost": 0.0002, "prompt_tokens": 50, "completion_tokens": 10})
    s = cost.snapshot()
    assert round(s["cost"], 6) == round(0.0001 + 0.012 + 0.0002, 6)
    assert s["calls"] == 3
    assert s["prompt_tokens"] == 950
    assert s["completion_tokens"] == 90
    assert s["by_model"]["openai/gpt-5-nano"]["calls"] == 2
    assert round(s["by_model"]["openai/gpt-5-nano"]["cost"], 6) == 0.0003


def test_reset_zeroes_everything():
    cost.record_usage("m", {"cost": 1.0})
    cost.reset()
    s = cost.snapshot()
    assert s["cost"] == 0.0 and s["calls"] == 0 and s["by_model"] == {}


def test_garbage_usage_is_a_silent_no_op():
    # None, non-dict, and a dict with a non-numeric/absent cost must NEVER raise and must not count cost.
    for bad in (None, "nope", 42, {"cost": "free"}, {}):
        cost.record_usage("m", bad)  # must not raise
    s = cost.snapshot()
    # {"cost":"free"} and {} still count as a call (a real call with no cost), but add $0.
    assert s["cost"] == 0.0


def test_jsonl_append_when_env_set_and_rollup(tmp_path, monkeypatch):
    log = tmp_path / "cost.jsonl"
    monkeypatch.setenv(cost.LOG_ENV, str(log))
    cost.record_usage("openai/gpt-4o", {"cost": 0.005, "prompt_tokens": 200, "completion_tokens": 40})
    cost.record_usage("openai/gpt-4o", {"cost": 0.003, "prompt_tokens": 100, "completion_tokens": 10})
    lines = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert lines[0]["model"] == "openai/gpt-4o" and lines[0]["cost"] == 0.005
    r = cost.rollup(str(log))
    assert round(r["cost"], 6) == 0.008
    assert r["calls"] == 2
    assert r["by_model"]["openai/gpt-4o"]["calls"] == 2


def test_no_file_written_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(cost.LOG_ENV, raising=False)
    cost.record_usage("m", {"cost": 0.001})
    assert not list(tmp_path.iterdir())  # nothing written anywhere we control


def test_format_snapshot_is_a_string():
    cost.record_usage("openai/gpt-5-mini", {"cost": 0.002, "prompt_tokens": 10, "completion_tokens": 5})
    out = cost.format_snapshot()
    assert "RUN COST" in out and "$0.0020" in out and "gpt-5-mini" in out
