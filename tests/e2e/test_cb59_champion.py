# CB-59 — One champion: championship resolution tests (round 2 — honest-fallback semantics).
# All tests use TestClient + fakes (no Postgres, no real LLM, no experiment launches).
# Test groups:
#   1. resolve_champion_config() — a MATERIALIZED store champion wins (its real content loads);
#      empty store falls back to the yaml bootstrap; corrupt store degrades; an UNMATERIALIZABLE
#      champion degrades to the bootstrap UNDER ITS TRUE LABEL (champion_v0) — content is never
#      relabeled (round-2 orchestrator-approved spec correction: the round-1 behavior that patched
#      the champion's version onto bootstrap content was a substance lie).
#   2. ROOT FIX: promote() materializes the new champion's config yaml + stamps config_ref, so
#      resolve_champion_config returns the promoted champion's ACTUAL content (changed field value).
#   3. server.py wiring (defect-1 regression): create_app WITHOUT an explicit config passes
#      champion_config_provider=None, so the improve router resolves from the store in prod.
#   4. Label coherence: /api/experiments champion_version == /api/versions champion_version
#      (lab drawer label and Versions page label share one source).
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from src.api.improve import resolve_champion_config
from src.api.server import create_app
from src.config import settings as settings_mod
from src.config.settings import load_config
from src.loop.experiment import ExperimentResult
from src.loop.generator import build_challenger, mutate_threshold
from src.loop.grading import ComparisonResult, GuardrailReport
from src.loop.promotion import promote
from src.memory.schema import ExperimentRecord, VersionLineage


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class FakeImproveStore:
    """Thin in-memory ImproveStore for CB-59 tests — only the surfaces tested here are wired.
    Also satisfies promotion.VersionRecorder (record_version) so promote() can run DB-free."""

    def __init__(
        self,
        lineage: list[VersionLineage],
        experiments: Optional[list[ExperimentRecord]] = None,
        fail_get_champion: bool = False,
    ) -> None:
        self._lineage = {n.version: n for n in lineage}
        self._experiments: dict[str, ExperimentRecord] = {
            e.experiment_id: e for e in (experiments or [])
        }
        self._fail_get_champion = fail_get_champion

    async def get_champion(self) -> Optional[VersionLineage]:
        if self._fail_get_champion:
            raise RuntimeError("injected store failure")
        return next((n for n in self._lineage.values() if n.is_champion), None)

    async def get_lineage(self, version: str) -> Optional[VersionLineage]:
        return self._lineage.get(version)

    async def list_lineage(self, *, limit: int = 200) -> list[VersionLineage]:
        rows = sorted(self._lineage.values(), key=lambda n: n.created_at, reverse=True)
        return rows[:limit]

    async def list_experiments(
        self, *, state: Optional[str] = None, limit: int = 200
    ) -> list[ExperimentRecord]:
        rows = [e for e in self._experiments.values() if state is None or e.state == state]
        return rows[:limit]

    async def get_experiment(self, experiment_id: str) -> Optional[ExperimentRecord]:
        return self._experiments.get(experiment_id)

    async def save_experiment(self, experiment: ExperimentRecord) -> str:
        self._experiments[experiment.experiment_id] = experiment
        return experiment.experiment_id

    async def record_version(self, lineage: VersionLineage) -> str:
        if lineage.is_champion:
            for v, node in self._lineage.items():
                if v != lineage.version:
                    node.is_champion = False
        self._lineage[lineage.version] = lineage
        return lineage.version


def _lineage(
    version: str,
    *,
    parent: Optional[str],
    kb: str,
    champion: bool,
    days_ago: int = 0,
    config_ref: Optional[str] = None,
) -> VersionLineage:
    return VersionLineage(
        version=version,
        parent_version=parent,
        config_ref=config_ref,
        kb_version=kb,
        is_champion=champion,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def _experiment_result(dimension: str) -> ExperimentResult:
    """A minimal clean-pass ExperimentResult so promote() can record a lineage KPI snapshot."""
    guardrails = GuardrailReport(
        pushiness_rate=0.0,
        pushiness_violation_turns=[],
        false_promise_flags=0,
        escalation_appropriate=True,
        pushiness_over_cap=False,
        agent_turns=5,
    )
    comparison = ComparisonResult(
        champion_kpi=3.0,
        challenger_kpi=3.2,
        delta=0.2,
        champion_ci=(2.9, 3.1),
        challenger_ci=(3.1, 3.3),
        delta_ci=(0.05, 0.35),
        challenger_better=True,
        n_champion=6,
        n_challenger=6,
    )
    return ExperimentResult(
        dimension=dimension,
        comparison=comparison,
        champion_guardrails=guardrails,
        challenger_guardrails=guardrails,
        champion_qual_acc=0.9,
        challenger_qual_acc=0.9,
        champion_eps=[],
        challenger_eps=[],
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture()
def tmp_versions_dir(tmp_path, monkeypatch):
    """Redirect the config versions dir to a tmp copy so promotion-persistence tests never write
    into the repo's src/config/versions/. The bootstrap champion_v0.yaml is copied in so
    load_config('champion_v0') (the fallback + create_app default) still works."""
    shutil.copy(settings_mod.VERSIONS_DIR / "champion_v0.yaml", tmp_path / "champion_v0.yaml")
    monkeypatch.setattr(settings_mod, "VERSIONS_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. resolve_champion_config() — championship resolution logic
# ---------------------------------------------------------------------------


def test_resolve_champion_materialized_store_champion_wins(tmp_versions_dir):
    """CB-59: when the store's promoted champion has a MATERIALIZED config yaml, resolve returns
    that champion's REAL content — version label AND substance — not the bootstrap."""
    champion = load_config("champion_v0")
    new_cap = float(champion.thresholds["pushiness_cap"]) + 0.07
    chal_cfg = mutate_threshold(champion, "pushiness_cap", new_cap)
    challenger = build_challenger(champion, chal_cfg, dimension_hint=None, seed=11)

    store = FakeImproveStore(lineage=[])
    _run(promote(challenger, _experiment_result(challenger.dimension), seed=11, version_store=store))

    cfg = _run(resolve_champion_config(store))
    assert cfg.version == challenger.challenger_version, (
        f"Expected the promoted champion {challenger.challenger_version!r}, got {cfg.version!r}"
    )
    # SUBSTANCE: the changed threshold value is the challenger's, proving real content loaded.
    assert cfg.thresholds["pushiness_cap"] == pytest.approx(new_cap)


def test_resolve_champion_kb_version_comes_from_materialized_yaml(tmp_versions_dir):
    """CB-59: the resolved config's kb_version is the MATERIALIZED yaml's value (the challenger
    config carried it through promotion) — read from loaded content, never patched on."""
    champion = load_config("champion_v0")
    chal_cfg = mutate_threshold(champion, "pushiness_cap", 0.77)
    challenger = build_challenger(champion, chal_cfg, dimension_hint=None, seed=12)

    store = FakeImproveStore(lineage=[])
    _run(promote(challenger, _experiment_result(challenger.dimension), seed=12, version_store=store))

    cfg = _run(resolve_champion_config(store))
    assert cfg.kb_version == challenger.config.kb_version


def test_resolve_champion_empty_store_falls_back_to_yaml():
    """CB-59: when the store has NO champion record (fresh install), resolve_champion_config
    falls back to load_config('champion_v0') explicitly — not a crash."""
    store = FakeImproveStore(lineage=[])  # no champion
    cfg = _run(resolve_champion_config(store))
    assert cfg.version == "champion_v0", (
        f"Expected bootstrap 'champion_v0', got {cfg.version!r}"
    )


def test_resolve_champion_corrupt_store_degrades_to_yaml():
    """CB-59: a store that raises on get_champion() must NOT crash the run route — falls back to
    the bootstrap yaml so the system remains operational."""
    store = FakeImproveStore(lineage=[], fail_get_champion=True)
    cfg = _run(resolve_champion_config(store))
    assert cfg.version == "champion_v0", (
        f"Expected bootstrap fallback 'champion_v0', got {cfg.version!r}"
    )


def test_resolve_champion_unmaterializable_returns_true_bootstrap_label():
    """CB-59 round 2 (HONEST FALLBACK — orchestrator-approved spec correction): a legacy champion
    whose config content is unavailable (config_ref=None, no versions/<version>.yaml — e.g. the
    live store's v1-d762f480, promoted before the root fix) resolves to the bootstrap config under
    its TRUE label 'champion_v0'. The round-1 behavior (patching the champion's version onto
    bootstrap content) stamped v0 substance as 'v1' — a fabricated baseline; records must say what
    they actually baselined."""
    store = FakeImproveStore(
        lineage=[
            _lineage("v1-d762f480", parent="champion_v0", kb="kb_v0", champion=True),
        ]
    )
    cfg = _run(resolve_champion_config(store))
    assert cfg.version == "champion_v0", (
        f"Unmaterializable champion must degrade to the TRUE bootstrap label, got {cfg.version!r} — "
        "never relabel content that wasn't loaded"
    )


def test_resolve_champion_bad_config_ref_degrades_honestly():
    """CB-59: a champion whose config_ref points to a non-existent yaml (and whose version has no
    yaml either) degrades to the bootstrap under its TRUE label — no crash, no relabel."""
    node = VersionLineage(
        version="v99-zzzz",
        parent_version="champion_v0",
        config_ref="non_existent_v99",
        kb_version="kb_v0",
        is_champion=True,
    )
    store = FakeImproveStore(lineage=[node])
    cfg = _run(resolve_champion_config(store))
    assert cfg.version == "champion_v0"  # honest fallback — NOT "v99-zzzz" (round-2 correction)


# ---------------------------------------------------------------------------
# 2. ROOT FIX — promotion materializes the champion config (CB-59 round 2)
# ---------------------------------------------------------------------------


def test_promote_writes_config_yaml_and_stamps_config_ref(tmp_versions_dir):
    """CB-59(b): promote() persists the challenger config to versions/<challenger_version>.yaml and
    sets lineage.config_ref to it, so the new champion is materializable going forward."""
    champion = load_config("champion_v0")
    chal_cfg = mutate_threshold(champion, "pushiness_cap", 0.77)
    challenger = build_challenger(champion, chal_cfg, dimension_hint=None, seed=13)

    store = FakeImproveStore(lineage=[])
    lineage = _run(
        promote(challenger, _experiment_result(challenger.dimension), seed=13, version_store=store)
    )

    assert lineage.config_ref == challenger.challenger_version
    yaml_path = tmp_versions_dir / f"{challenger.challenger_version}.yaml"
    assert yaml_path.exists(), f"promotion must materialize {yaml_path}"
    # The materialized yaml round-trips through load_config with the challenger's REAL content.
    reloaded = load_config(challenger.challenger_version)
    assert reloaded.version == challenger.challenger_version
    assert reloaded.thresholds["pushiness_cap"] == pytest.approx(0.77)


def test_promoted_challenger_resolves_with_actual_content(tmp_versions_dir):
    """CB-59(b) acceptance: fake-store promotion of a generator-built challenger ->
    resolve_champion_config returns that challenger's ACTUAL content — the changed field's VALUE is
    asserted (substance), not just the version label."""
    champion = load_config("champion_v0")
    old_cap = float(champion.thresholds["pushiness_cap"])
    new_cap = old_cap + 0.05
    chal_cfg = mutate_threshold(champion, "pushiness_cap", new_cap)
    challenger = build_challenger(champion, chal_cfg, dimension_hint=None, seed=14)

    store = FakeImproveStore(lineage=[])
    _run(promote(challenger, _experiment_result(challenger.dimension), seed=14, version_store=store))

    cfg = _run(resolve_champion_config(store))
    assert cfg.version == challenger.challenger_version
    assert cfg.thresholds["pushiness_cap"] == pytest.approx(new_cap), (
        "resolve must return the promoted challenger's ACTUAL content (the mutated threshold), "
        "not the bootstrap's"
    )
    assert cfg.thresholds["pushiness_cap"] != pytest.approx(old_cap)


def test_promote_yaml_write_failure_still_promotes_with_null_config_ref(tmp_versions_dir, monkeypatch):
    """CB-59(b): a config-yaml write failure must NOT fail the promotion — the lineage records with
    config_ref=None and resolve degrades to the honest bootstrap fallback."""
    champion = load_config("champion_v0")
    chal_cfg = mutate_threshold(champion, "pushiness_cap", 0.78)
    challenger = build_challenger(champion, chal_cfg, dimension_hint=None, seed=15)

    def _boom(_cfg):
        raise OSError("disk full (injected)")

    monkeypatch.setattr("src.loop.promotion.save_config", _boom)
    store = FakeImproveStore(lineage=[])
    lineage = _run(
        promote(challenger, _experiment_result(challenger.dimension), seed=15, version_store=store)
    )
    assert lineage.is_champion is True  # promotion succeeded
    assert lineage.config_ref is None  # but the config could not be materialized
    # resolve degrades to the honest bootstrap (no yaml exists for the challenger version).
    cfg = _run(resolve_champion_config(store))
    assert cfg.version == "champion_v0"


# ---------------------------------------------------------------------------
# 3. server.py wiring (defect-1 regression) — prod resolves from the store
# ---------------------------------------------------------------------------


def test_create_app_without_config_resolves_champion_from_store(tmp_versions_dir):
    """DEFECT-1 regression (round 2): create_app called WITHOUT an explicit `config` (the prod
    path) must pass champion_config_provider=None so the improve router resolves the champion from
    the VERSION STORE. Verified end-to-end: a fake store holding a promoted+materialized champion
    -> GET /api/playbook returns the promoted champion's version AND substance."""
    champion = load_config("champion_v0")
    new_cap = float(champion.thresholds["pushiness_cap"]) + 0.06
    chal_cfg = mutate_threshold(champion, "pushiness_cap", new_cap)
    challenger = build_challenger(champion, chal_cfg, dimension_hint=None, seed=16)

    store = FakeImproveStore(lineage=[])
    _run(promote(challenger, _experiment_result(challenger.dimension), seed=16, version_store=store))

    # NO `config=` argument — the prod wiring path. live_rag=False keeps the test offline.
    app = create_app(improve_store=store, live_rag=False)
    client = TestClient(app)

    r = client.get("/api/playbook")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == challenger.challenger_version, (
        f"prod wiring must resolve the STORE champion; got {body['version']!r} — "
        "if this is 'champion_v0' the provider injection is shadowing the store again (defect 1)"
    )
    assert body["thresholds"]["pushiness_cap"] == pytest.approx(new_cap)


def test_create_app_without_config_scaffold_stamps_store_champion(tmp_versions_dir):
    """DEFECT-1 + deliverable 2: through the PROD wiring (no config arg), a new experiment record's
    champion_version is stamped from the store's resolved champion — the lab drawer will label the
    true baseline."""
    champion = load_config("champion_v0")
    chal_cfg = mutate_threshold(champion, "pushiness_cap", 0.79)
    challenger = build_challenger(champion, chal_cfg, dimension_hint=None, seed=17)

    store = FakeImproveStore(lineage=[])
    _run(promote(challenger, _experiment_result(challenger.dimension), seed=17, version_store=store))

    app = create_app(improve_store=store, live_rag=False)
    client = TestClient(app)

    r = client.post(
        "/api/experiments/scaffold",
        json={"episode_id": "ep-cb59-prod-wiring", "name": "prod-wiring scaffold"},
    )
    assert r.status_code == 200, r.text
    exp = r.json()["experiment"]
    assert exp["champion_version"] == challenger.challenger_version, (
        f"Expected champion_version={challenger.challenger_version!r}, got "
        f"{exp['champion_version']!r} — the run/scaffold route must baseline the store's champion"
    )


def test_create_app_with_explicit_config_keeps_provider_seam():
    """The test seam is preserved: an explicit `config` override on create_app pins the champion
    config deterministically (existing tests rely on this)."""
    store = FakeImproveStore(
        lineage=[_lineage("v1-should-be-ignored", parent=None, kb="kb_v0", champion=True)]
    )
    app = create_app(improve_store=store, config=load_config("champion_v0"), live_rag=False)
    client = TestClient(app)
    r = client.get("/api/playbook")
    assert r.status_code == 200, r.text
    # The explicit config wins over the store (the deterministic test seam).
    assert r.json()["version"] == "champion_v0"


# ---------------------------------------------------------------------------
# 4. Label coherence: lab drawer champion_version == Versions page champion_version
# ---------------------------------------------------------------------------


def test_versions_endpoint_champion_version_matches_store():
    """CB-59: /api/versions champion_version comes from the store's is_champion record (the same
    source resolve_champion_config reads), so the Versions page and the lab drawer label agree."""
    champion_version = "v1-d762f480"
    store = FakeImproveStore(
        lineage=[
            _lineage(champion_version, parent="champion_v0", kb="kb_v0", champion=True, days_ago=0),
            _lineage("champion_v0", parent=None, kb="kb_v0", champion=False, days_ago=10),
        ]
    )
    app = create_app(improve_store=store, config=load_config("champion_v0"), live_rag=False)
    client = TestClient(app)

    r = client.get("/api/versions")
    assert r.status_code == 200, r.text
    assert r.json()["champion_version"] == champion_version


def test_versions_endpoint_flags_champion_in_versions_list():
    """CB-59: /api/versions returns the champion with is_champion=True, so the Versions page can
    mark it — and the KPI page can identify the live champion by reading this endpoint."""
    champion_version = "v1-d762f480"
    store = FakeImproveStore(
        lineage=[
            _lineage(champion_version, parent="champion_v0", kb="kb_v0", champion=True, days_ago=0),
            _lineage("champion_v0", parent=None, kb="kb_v0", champion=False, days_ago=10),
        ]
    )
    app = create_app(improve_store=store, config=load_config("champion_v0"), live_rag=False)
    client = TestClient(app)

    body = client.get("/api/versions").json()
    champ_row = next((v for v in body["versions"] if v["version"] == champion_version), None)
    assert champ_row is not None
    assert champ_row["is_champion"] is True


def test_lab_drawer_and_versions_page_share_champion_source(tmp_versions_dir):
    """CB-59: through the PROD wiring (store-resolved champion), the champion_version stamped on a
    new experiment record (lab drawer) equals /api/versions champion_version (Versions page) —
    single source of truth, verified on the SAME app."""
    champion = load_config("champion_v0")
    chal_cfg = mutate_threshold(champion, "pushiness_cap", 0.76)
    challenger = build_challenger(champion, chal_cfg, dimension_hint=None, seed=18)

    store = FakeImproveStore(lineage=[])
    _run(promote(challenger, _experiment_result(challenger.dimension), seed=18, version_store=store))

    app = create_app(improve_store=store, live_rag=False)  # prod wiring — no config arg
    client = TestClient(app)

    exp = client.post(
        "/api/experiments/scaffold",
        json={"episode_id": "ep-cb59-label", "name": "label source test"},
    ).json()["experiment"]
    versions_champ = client.get("/api/versions").json()["champion_version"]

    assert exp["champion_version"] == versions_champ, (
        f"Lab drawer champion_version {exp['champion_version']!r} != Versions page "
        f"champion_version {versions_champ!r} — single source of truth violated"
    )
    assert versions_champ == challenger.challenger_version


def test_fresh_install_both_endpoints_agree_on_bootstrap():
    """CB-59: when the store is empty (no champion), the scaffold record baselines champion_v0 (the
    explicit yaml bootstrap) and /api/versions reports no champion — internally consistent."""
    store = FakeImproveStore(lineage=[])  # empty — no champion in store
    app = create_app(improve_store=store, live_rag=False)  # prod wiring
    client = TestClient(app)

    exp = client.post(
        "/api/experiments/scaffold",
        json={"episode_id": "ep-fresh", "name": "fresh install"},
    ).json()["experiment"]
    versions = client.get("/api/versions").json()

    assert exp["champion_version"] == "champion_v0"  # the explicit bootstrap fallback
    assert versions["champion_version"] is None  # store truthfully has no champion yet


def test_legacy_unmaterializable_champion_scaffold_baselines_bootstrap_honestly(tmp_versions_dir):
    """CB-59 round 2: with the LIVE store's actual shape (champion v1-d762f480, config_ref=None, no
    yaml), a new experiment record says champion_version='champion_v0' — the TRUE baseline that ran
    — while /api/versions still reports v1 as the champion. The record is honest about the
    substance mismatch instead of fabricating a v1 baseline."""
    store = FakeImproveStore(
        lineage=[_lineage("v1-d762f480", parent="champion_v0", kb="kb_v0", champion=True)]
    )
    app = create_app(improve_store=store, live_rag=False)  # prod wiring
    client = TestClient(app)

    exp = client.post(
        "/api/experiments/scaffold",
        json={"episode_id": "ep-legacy", "name": "legacy champion"},
    ).json()["experiment"]

    # The baseline that ACTUALLY ran is the bootstrap — the record must say so.
    assert exp["champion_version"] == "champion_v0", (
        "an unmaterializable champion must yield an honestly-labeled bootstrap baseline, "
        f"got {exp['champion_version']!r}"
    )
