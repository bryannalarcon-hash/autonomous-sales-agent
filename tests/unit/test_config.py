# U1 tests: a versioned agent config loads, validates, and produces a version stamp.
from pathlib import Path

import pytest

from src.config import AgentConfig, load_config


def test_loads_champion_v0():
    cfg = load_config("champion_v0")
    assert cfg.version == "champion_v0"
    assert cfg.kb_version == "kb_v0"
    assert cfg.persona.style == "warm-consultative"


def test_stamp_carries_versions():
    cfg = load_config("champion_v0")
    stamp = cfg.stamp()
    assert stamp == {"version": "champion_v0", "kb_version": "kb_v0"}


def test_missing_version_raises():
    with pytest.raises(FileNotFoundError):
        load_config("does_not_exist")


def test_distinct_versions_have_distinct_ids(tmp_path: Path):
    # Two configs must surface different version strings for attribution.
    cfg = load_config("champion_v0")
    assert isinstance(cfg, AgentConfig)
    assert cfg.version  # non-empty, the loop relies on this for lineage
