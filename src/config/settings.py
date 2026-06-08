# Typed agent configuration loaded from a versioned YAML file (config/versions/*.yaml).
# An AgentConfig is the unit the improvement loop versions and promotes: prompts + playbooks +
# policy thresholds, identified by a stable `version` and a pinned `kb_version`. Every episode and
# live session is stamped with these so performance is attributable to the exact config that ran.
# CB-59: save_config() is the write-side counterpart of load_config() — promotion
# (src.loop.promotion.promote) materializes every NEW champion's config to versions/<version>.yaml
# so resolve_champion_config can re-load its REAL content later (lineage config_ref points here).
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


def save_config(config: AgentConfig) -> Path:
    """Serialize an AgentConfig to config/versions/<config.version>.yaml (CB-59 root fix).

    The write-side counterpart of load_config: promotion materializes every NEW champion's config
    here so its content is re-loadable later (load_config(config.version) round-trips). The yaml
    carries the SAME field set load_config validates (version/kb_version/persona + prompts/
    playbooks/thresholds), so a saved config always re-loads. Returns the written path. Raises on
    I/O failure — the caller (promote) decides whether that is fatal (it is not: promotion still
    records the lineage; config_ref just stays None for that champion)."""
    target = VERSIONS_DIR / f"{config.version}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": config.version,
        "kb_version": config.kb_version,
        "persona": {
            "name": config.persona.name,
            "role": config.persona.role,
            "style": config.persona.style,
        },
        "prompts": dict(config.prompts),
        "playbooks": dict(config.playbooks),
        "thresholds": dict(config.thresholds),
    }
    target.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return target
