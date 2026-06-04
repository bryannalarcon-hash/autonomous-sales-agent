# Regression tests (TEST-FIRST) for CB-44 — voice-call timing observability serialization.
# Validates the LOCKED API contract coder-dash builds against, WITHOUT needing livekit: build an
# Episode whose metrics carry the worker-stamped timing (metrics["turn_timings"] keyed by stringified
# agent turn_id, metrics["live_timing"] for the active turn) and assert operate serializes the three
# shapes exactly — per-turn episode_detail().turns[i]["timing"], episode_summary() averages, and
# live_snapshot()["live_timing"] — plus that a legacy turn with NO timing round-trips as all-nulls
# (no crash). Also covers persistence.episode_from_session stowing turn_timings onto metrics.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.api import operate
from src.memory.schema import Episode, Turn


# The 4 per-turn keys the contract LOCKS (operate emits all of them, int-or-null).
_TIMING_KEYS = {"audio_to_first_token_ms", "first_token_ms", "stream_duration_ms", "total_ms"}


def _timed_episode(active: bool = False) -> Episode:
    """An episode with two agent turns: turn_id=1 fully timed, turn_id=3 partially timed (no audio
    stamp — a text/legacy-ish turn), plus a never-timed prospect turn. metrics carry the worker shape."""
    now = datetime.now(timezone.utc)
    metrics = {
        "turn_count": 4,
        # Heartbeat fresh so _is_active is True when `active`; stale otherwise so it reads inactive.
        "live_heartbeat": (now if active else now - timedelta(hours=1)).isoformat(),
        # Stringified int turn_id keys (jsonb round-trip), as persistence writes them.
        "turn_timings": {
            "1": {"audio_to_first_token_ms": 1200, "first_token_ms": 800,
                  "stream_duration_ms": 1500, "total_ms": 2700},
            "3": {"audio_to_first_token_ms": None, "first_token_ms": 600,
                  "stream_duration_ms": 900, "total_ms": None},
        },
    }
    return Episode(
        episode_id="ep-cb44",
        channel="voice",
        cohort="live",
        outcome="in_progress" if active else "released",
        turns=[
            Turn(turn_id=0, speaker="prospect", text="Hi"),
            Turn(turn_id=1, speaker="agent", text="Hello, how can I help?", decision="ask"),
            Turn(turn_id=2, speaker="prospect", text="How much?"),
            Turn(turn_id=3, speaker="agent", text="Plans run about $300/month.", decision="answer_via_kb"),
        ],
        metrics=metrics,
    )


def test_cb44_episode_detail_per_turn_timing_contract():
    """episode_detail().turns[i]["timing"] carries all 4 keys; a timed agent turn has its numbers, a
    prospect/untimed turn is all-null — exactly the LOCKED shape coder-dash reads."""
    detail = operate.episode_detail(_timed_episode())
    turns = detail["turns"]

    # Every turn carries a full timing block with exactly the 4 contract keys.
    for t in turns:
        assert set(t["timing"].keys()) == _TIMING_KEYS, f"timing keys drift: {t['timing']}"

    # turn_id=1: fully timed.
    assert turns[1]["timing"] == {
        "audio_to_first_token_ms": 1200, "first_token_ms": 800,
        "stream_duration_ms": 1500, "total_ms": 2700,
    }
    # turn_id=3: partially timed (no audio stamp -> those two null).
    assert turns[3]["timing"] == {
        "audio_to_first_token_ms": None, "first_token_ms": 600,
        "stream_duration_ms": 900, "total_ms": None,
    }
    # prospect turn 0 (no timing entry): all-null, no crash.
    assert turns[0]["timing"] == {k: None for k in _TIMING_KEYS}


def test_cb44_legacy_episode_turns_serialize_all_null_no_crash():
    """An episode with NO metrics["turn_timings"] (a text/legacy call) serializes every turn's timing
    as all-null and the summary averages as null — never a KeyError / crash."""
    ep = Episode(
        episode_id="ep-legacy",
        channel="text",
        turns=[
            Turn(turn_id=0, speaker="prospect", text="Hi"),
            Turn(turn_id=1, speaker="agent", text="Hello!", decision="ask"),
        ],
        metrics={"turn_count": 2},
    )
    detail = operate.episode_detail(ep)
    for t in detail["turns"]:
        assert t["timing"] == {k: None for k in _TIMING_KEYS}
    assert detail["avg_first_token_ms"] is None
    assert detail["avg_stream_ms"] is None


def test_cb44_episode_summary_averages_over_timed_agent_turns():
    """episode_summary() carries avg_first_token_ms / avg_stream_ms = the mean over agent turns that
    have that number. first_token: mean(800, 600)=700; stream: mean(1500, 900)=1200."""
    summary = operate.episode_summary(_timed_episode())
    assert summary["avg_first_token_ms"] == 700
    assert summary["avg_stream_ms"] == 1200


def test_cb44_live_snapshot_live_timing_present_only_while_active():
    """live_snapshot()["live_timing"] = {first_token_ms, stream_elapsed_ms} for the ACTIVE streaming
    turn (from metrics["live_timing"]) while active; null on an inactive/completed call."""
    ep = _timed_episode(active=True)
    ep.metrics["live_timing"] = {"first_token_ms": 800, "stream_elapsed_ms": 450}
    snap = operate.live_snapshot(ep)
    assert snap["active"] is True
    assert snap["live_timing"] == {"first_token_ms": 800, "stream_elapsed_ms": 450}

    # An inactive (completed) call: live_timing is dropped (null) even if metrics still has it.
    done = _timed_episode(active=False)
    done.metrics["live_timing"] = {"first_token_ms": 800, "stream_elapsed_ms": 450}
    snap_done = operate.live_snapshot(done)
    # released + stale heartbeat -> inactive; live_timing must be null.
    assert snap_done.get("live_timing") is None


def test_cb44_episode_from_session_stows_turn_timings_on_metrics():
    """persistence.episode_from_session must write the worker's per-turn timings onto
    metrics["turn_timings"] (int turn_id stringified) so the round-trip surfaces them in operate."""
    from src.api import persistence
    from src.config.settings import load_config

    class _FakeState:
        def __init__(self) -> None:
            self.turns = [
                Turn(turn_id=0, speaker="prospect", text="Hi"),
                Turn(turn_id=1, speaker="agent", text="Hello!", decision="ask"),
            ]
            self.belief = None

    class _FakeSession:
        recorded = False
        state = _FakeState()

    config = load_config("champion_v0")
    timings = {1: {"audio_to_first_token_ms": 1000, "first_token_ms": 700,
                   "stream_duration_ms": 1400, "total_ms": 2400}}
    ep = persistence.episode_from_session(
        _FakeSession(), config=config, channel="voice", turn_timings=timings,
    )
    assert ep.metrics["turn_timings"] == {
        "1": {"audio_to_first_token_ms": 1000, "first_token_ms": 700,
              "stream_duration_ms": 1400, "total_ms": 2400}
    }
    # And it round-trips back through operate to the per-turn contract.
    detail = operate.episode_detail(ep)
    assert detail["turns"][1]["timing"]["first_token_ms"] == 700
    assert detail["avg_first_token_ms"] == 700
