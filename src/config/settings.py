# Typed agent configuration loaded from a versioned YAML file (config/versions/*.yaml).
# An AgentConfig is the unit the improvement loop versions and promotes: prompts + playbooks +
# policy thresholds, identified by a stable `version` and a pinned `kb_version`. Every episode and
# live session is stamped with these so performance is attributable to the exact config that ran.
# Uses stdlib dataclasses (no third-party dep) so the foundational config layer stays robust.
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VERSIONS_DIR = Path(__file__).parent / "versions"
_REQUIRED = ("version", "kb_version", "persona")


@dataclass(frozen=True)
class Persona:
    name: str
    role: str
    style: str


@dataclass(frozen=True)
class AgentConfig:
    """The versioned, promotable agent configuration (plan R12)."""

    version: str
    kb_version: str
    persona: Persona
    prompts: dict[str, str] = field(default_factory=dict)
    playbooks: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)

    def stamp(self) -> dict[str, str]:
        """The version tags stamped onto every episode/session for attribution."""
        return {"version": self.version, "kb_version": self.kb_version}


def load_config(version: str = "champion_v0") -> AgentConfig:
    """Load and validate a versioned config from config/versions/<version>.yaml."""
    path = VERSIONS_DIR / f"{version}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No config version '{version}' at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise ValueError(f"Config '{version}' missing required keys: {missing}")
    return AgentConfig(
        version=str(data["version"]),
        kb_version=str(data["kb_version"]),
        persona=Persona(**data["persona"]),
        prompts=data.get("prompts", {}),
        playbooks=data.get("playbooks", {}),
        thresholds=data.get("thresholds", {}),
    )
