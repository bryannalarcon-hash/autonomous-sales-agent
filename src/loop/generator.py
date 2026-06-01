# Challenger generation for the U10 improvement loop (plan R16/R17/R21). A Challenger is the champion
# config plus a SINGLE declared minimal diff (containment): generation is failure-conditioned (cluster
# losing episodes to pick a weakness) + tactic-mined (winning episodes), with parametric search for
# numeric dimensions (perturb ONE threshold deterministically by seed) and LLM-propose for text dims
# (mockable; if llm is None only the network-free parametric/reorder path runs). The MUTATION SURFACE
# is prompts + playbooks + thresholds + persona ONLY (R21) — never code. EXTREME diffs (a pricing
# concession threshold or the persona, R19) are flagged for human approval. EVERY returned challenger
# satisfies is_minimal_diff (exactly one changed dimension) and carries a deterministic
# challenger_version. Pure stdlib + the src.core.llm seam; NO LiveKit / numpy / scipy / pandas; all
# randomness flows through a SEEDED random.Random (never the global random module).
from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.llm import LLMClient
from src.memory.schema import Episode

# Threshold keys whose change is a PRICING CONCESSION (extreme — needs human approval, R19). A diff
# touching any of these, OR the persona, is extreme.
_PRICING_THRESHOLD_KEYS: frozenset[str] = frozenset(
    {"max_concession_band", "trust_gate_open_price"}
)

# The two extreme dimension CLASSES (R19). A diff is extreme iff its single changed dimension is a
# pricing-concession threshold (labelled "thresholds.<key>") or the persona.
EXTREME_DIMENSIONS: frozenset[str] = frozenset({"pricing", "persona"})

# Numeric thresholds the parametric search may perturb (the SAFE, non-extreme numeric dims by
# default — the demo numeric search stays away from pricing unless explicitly asked).
_SAFE_NUMERIC_KEYS: tuple[str, ...] = (
    "pushiness_cap",
    "pushiness_pressure_count_cap",
    "discovery_slots_required",
    "low_confidence_level",
)

# Deterministic perturbation step for a numeric threshold (bounded so the diff stays sane). The
# direction is chosen by the seeded rng so a seed reproduces the exact challenger.
_NUMERIC_STEP = 0.1


@dataclass
class Challenger:
    """The champion config plus a SINGLE declared minimal diff (plan R17 containment).

    `dimension` is the one changed dimension label (e.g. "playbooks.discovery_sequence",
    "thresholds.pushiness_cap", or "persona"). `is_extreme` flags a pricing-concession or persona
    diff that blocks for human approval (R19). `challenger_version` is deterministic from
    parent+dimension+seed so the same generation reproduces the same id.
    """

    config: AgentConfig
    dimension: str
    diff_description: str
    parent_version: str
    challenger_version: str
    is_extreme: bool


# === Diff computation (R21 minimal-diff containment) ===========================================


def _diff_keys(prefix: str, champ_map: dict[str, Any], chal_map: dict[str, Any]) -> set[str]:
    """The per-key changed labels between two sub-maps (prompts/playbooks/thresholds).

    A key is changed if present in exactly one map, or present in both with differing values.
    Labels are namespaced ("playbooks.discovery_sequence", "thresholds.pushiness_cap", ...).
    """
    changed: set[str] = set()
    for key in set(champ_map) | set(chal_map):
        if champ_map.get(key) != chal_map.get(key):
            changed.add(f"{prefix}.{key}")
    return changed


def declared_diff(champion: AgentConfig, challenger: AgentConfig) -> set[str]:
    """The SET of changed dimensions between champion and challenger (plan R17/R21).

    Compares prompts, playbooks, and thresholds per-key (returning namespaced labels like
    "playbooks.discovery_sequence" or "thresholds.trust_gate_open_price") plus a "persona" label
    when the persona differs. Code is never part of the surface (R21), so it is never compared.
    """
    diff: set[str] = set()
    diff |= _diff_keys("prompts", champion.prompts, challenger.prompts)
    diff |= _diff_keys("playbooks", champion.playbooks, challenger.playbooks)
    diff |= _diff_keys("thresholds", champion.thresholds, challenger.thresholds)
    if champion.persona != challenger.persona:
        diff.add("persona")
    return diff


def is_minimal_diff(champion: AgentConfig, challenger: AgentConfig) -> bool:
    """True iff EXACTLY ONE dimension changed (plan R17 containment). Zero or >1 -> False."""
    return len(declared_diff(champion, challenger)) == 1


def _is_extreme_dimension(dimension: str) -> bool:
    """A single changed dimension is EXTREME (R19) when it is the persona or a pricing-concession
    threshold. Other prompts/playbooks/threshold edits are non-extreme (auto-promotable)."""
    if dimension == "persona":
        return True
    if dimension.startswith("thresholds."):
        key = dimension.split(".", 1)[1]
        return key in _PRICING_THRESHOLD_KEYS
    return False


# === Pure config-copy mutators (deep-copy; never mutate the input) =============================


def _clone(config: AgentConfig, *, prompts=None, playbooks=None, thresholds=None, persona=None) -> AgentConfig:
    """Deep-copy `config` overriding only the named sub-maps. The input is NEVER mutated."""
    return AgentConfig(
        version=config.version,
        kb_version=config.kb_version,
        persona=persona if persona is not None else config.persona,
        prompts=copy.deepcopy(config.prompts) if prompts is None else prompts,
        playbooks=copy.deepcopy(config.playbooks) if playbooks is None else playbooks,
        thresholds=copy.deepcopy(config.thresholds) if thresholds is None else thresholds,
    )


def mutate_threshold(config: AgentConfig, key: str, new_value: float) -> AgentConfig:
    """Return a NEW config with thresholds[key] set to new_value (deep-copy; input untouched)."""
    thresholds = copy.deepcopy(config.thresholds)
    thresholds[key] = new_value
    return _clone(config, thresholds=thresholds)


def reorder_discovery(config: AgentConfig, new_sequence: Sequence[str]) -> AgentConfig:
    """Return a NEW config with playbooks.discovery_sequence replaced by new_sequence (deep-copy;
    input untouched). The demo dimension — discovery sequencing."""
    playbooks = copy.deepcopy(config.playbooks)
    playbooks["discovery_sequence"] = list(new_sequence)
    return _clone(config, playbooks=playbooks)


# === Challenger assembly =======================================================================


def _challenger_version(parent: str, dimension: str, seed: int) -> str:
    """A deterministic challenger version id from parent+dimension+seed (plan: f-string id)."""
    safe_dim = dimension.replace(".", "_")
    return f"{parent}__{safe_dim}__{seed}"


def _build_challenger(
    champion: AgentConfig,
    challenger_config: AgentConfig,
    *,
    dimension_hint: Optional[str],
    seed: int,
    diff_description: str = "",
) -> Challenger:
    """Wrap a mutated config as a Challenger: derive its single dimension from the declared diff,
    set is_extreme from that dimension's class, mint a deterministic version, and STAMP that version
    onto the returned config so its episodes are attributable to the challenger (R12). Raises if the
    config is not a minimal one-dimension diff (containment is enforced at construction, R17).

    The diff is computed BEFORE re-stamping the version (declared_diff ignores `version`, comparing
    only prompts/playbooks/thresholds/persona — the mutation surface), so re-versioning never adds a
    spurious dimension and containment still sees exactly one changed dimension.
    """
    diff = declared_diff(champion, challenger_config)
    if len(diff) != 1:
        raise ValueError(
            f"challenger must be a minimal one-dimension diff (R17); got {sorted(diff)}"
        )
    dimension = next(iter(diff))
    is_extreme = _is_extreme_dimension(dimension)
    version = _challenger_version(champion.version, dimension, seed)
    # Re-stamp the config's version so self-play episodes carry the challenger version (attribution).
    versioned_config = AgentConfig(
        version=version,
        kb_version=challenger_config.kb_version,
        persona=challenger_config.persona,
        prompts=challenger_config.prompts,
        playbooks=challenger_config.playbooks,
        thresholds=challenger_config.thresholds,
    )
    desc = diff_description or f"changed {dimension}"
    return Challenger(
        config=versioned_config,
        dimension=dimension,
        diff_description=desc,
        parent_version=champion.version,
        challenger_version=version,
        is_extreme=is_extreme,
    )


# === Generation: failure-conditioned + tactic-mined ============================================


def _cluster_losses(episodes: Sequence[Episode]) -> list[Episode]:
    """The 'losing' episodes that signal a weakness: walked or low ladder_tier (< 2, i.e. no
    substantive commitment). These cluster the failure the generator targets (R17)."""
    return [
        ep for ep in episodes
        if ep.outcome == "walked" or int(ep.ladder_tier) < 2
    ]


def _winning_episodes(episodes: Sequence[Episode]) -> list[Episode]:
    """The 'winning' episodes whose tactics are mined (high ladder_tier >= 3)."""
    return [ep for ep in episodes if int(ep.ladder_tier) >= 3]


def _reorder_candidate(champion: AgentConfig, rng: random.Random, seed: int) -> Optional[Challenger]:
    """A discovery-sequencing challenger (the demo text dim that needs NO LLM): deterministically
    reorder playbooks.discovery_sequence by swapping two slots chosen by the seeded rng. Returns
    None if there is no reorderable sequence."""
    seq = list(champion.playbooks.get("discovery_sequence") or [])
    if len(seq) < 2:
        return None
    i = rng.randrange(len(seq))
    j = rng.randrange(len(seq))
    if i == j:
        j = (j + 1) % len(seq)
    new_seq = list(seq)
    new_seq[i], new_seq[j] = new_seq[j], new_seq[i]
    if new_seq == seq:
        return None
    chal_config = reorder_discovery(champion, new_seq)
    return _build_challenger(
        champion, chal_config, dimension_hint="playbooks.discovery_sequence", seed=seed,
        diff_description=f"reorder discovery_sequence: swap slots {i}<->{j}",
    )


def _threshold_candidate(champion: AgentConfig, rng: random.Random, seed: int) -> Optional[Challenger]:
    """A numeric parametric-search challenger: perturb ONE safe threshold deterministically by the
    seeded rng (key + direction). Stays away from pricing keys (those would be extreme). Returns
    None if no safe threshold is present."""
    keys = [k for k in _SAFE_NUMERIC_KEYS if k in champion.thresholds]
    if not keys:
        return None
    key = rng.choice(keys)
    current = float(champion.thresholds[key])
    direction = 1.0 if rng.random() < 0.5 else -1.0
    # Integer-valued count thresholds step by 1; bounded fractions step by _NUMERIC_STEP in [0,1].
    if key in ("pushiness_pressure_count_cap", "discovery_slots_required"):
        new_value: float = max(1.0, current + direction)
    else:
        new_value = round(min(1.0, max(0.0, current + direction * _NUMERIC_STEP)), 3)
    if new_value == current:
        return None
    chal_config = mutate_threshold(champion, key, new_value)
    return _build_challenger(
        champion, chal_config, dimension_hint=f"thresholds.{key}", seed=seed,
        diff_description=f"perturb {key}: {current} -> {new_value}",
    )


def generate_challengers(
    champion: AgentConfig,
    episodes: Sequence[Episode],
    *,
    llm: Optional[LLMClient] = None,
    seed: int,
    n: int = 1,
) -> list[Challenger]:
    """Generate up to `n` minimal-diff challengers from the champion (plan R16/R17).

    Strategy: failure-conditioned (cluster losing episodes — walked / low ladder_tier — to confirm a
    weakness exists) + tactic-mined (winning episodes). Numeric dims use a deterministic parametric
    search (perturb ONE safe threshold by seed); the demo text dim (discovery sequencing) uses a
    deterministic reorder. `llm` is the LLM-propose seam for richer text dims; when it is None ONLY
    the network-free parametric/reorder path runs (so generation works without a network — the LLM
    path is reserved for future text-prompt proposals).

    EVERY returned challenger satisfies is_minimal_diff (enforced in _build_challenger) and sets
    is_extreme correctly. Each candidate uses a distinct seed derived from `seed` so the versions are
    deterministic and unique. Returns at most `n`; may return fewer if no distinct candidate exists.
    """
    _ = llm  # LLM-propose seam reserved for text-prompt dims; parametric/reorder needs no network.
    losses = _cluster_losses(episodes)
    wins = _winning_episodes(episodes)
    # The presence of losses biases toward the discovery-sequencing fix (a discovery weakness); the
    # presence of wins biases toward mining a numeric tactic. With neither signal we still try both.
    prefer_reorder = len(losses) >= len(wins)

    out: list[Challenger] = []
    seen_versions: set[str] = set()
    # Try candidates with distinct derived seeds until we have n (or run out of attempts).
    for attempt in range(max(n * 4, 4)):
        cseed = seed + attempt
        rng = random.Random(cseed)
        builders = (
            (_reorder_candidate, _threshold_candidate)
            if (prefer_reorder if attempt % 2 == 0 else not prefer_reorder)
            else (_threshold_candidate, _reorder_candidate)
        )
        candidate: Optional[Challenger] = None
        for build in builders:
            candidate = build(champion, rng, cseed)
            if candidate is not None:
                break
        if candidate is None:
            continue
        if candidate.challenger_version in seen_versions:
            continue
        # Defensive: containment must hold (it does by construction, but never ship a non-minimal one).
        if not is_minimal_diff(champion, candidate.config):
            continue
        seen_versions.add(candidate.challenger_version)
        out.append(candidate)
        if len(out) >= n:
            break
    return out
