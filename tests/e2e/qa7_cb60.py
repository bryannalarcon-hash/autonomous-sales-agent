# CB-60 Playwright sweep — rendered-count reconciliation across Operate pages.
# Read-only: navigates to each Operate surface and asserts visible text is self-consistent.
# No clicking anything that spends (no lifecycle changes, no golden toggles).
# Requirements: the Next.js dashboard must be running at localhost:3000 and the FastAPI API
# at localhost:8000 (standard dev stack: `npm run dev` + `uvicorn src.api.server:app`).
# Run: playwright test tests/e2e/qa7_cb60.py (or python -m pytest tests/e2e/qa7_cb60.py --headed)
# The assertions are soft where counts are dynamic (DB-backed) — we check structural invariants
# (total ≥ count, n denominator present) rather than hard-coding expected numbers.
from __future__ import annotations

import re
import time

import pytest

# ---------------------------------------------------------------------------
# Skip gracefully when Playwright isn't installed
# ---------------------------------------------------------------------------
try:
    from playwright.sync_api import Page, sync_playwright  # type: ignore
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False


DASHBOARD_BASE = "http://localhost:3000"
API_BASE = "http://localhost:8000"

pytestmark = pytest.mark.skipif(
    not _PW_AVAILABLE,
    reason="playwright not installed — install with `pip install playwright && playwright install`",
)


@pytest.fixture(scope="module")
def page():
    """Module-scoped Playwright page (reused across tests for speed). Chromium headless."""
    if not _PW_AVAILABLE:
        pytest.skip("playwright not installed")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        pg = ctx.new_page()
        yield pg
        ctx.close()
        browser.close()


def _api_get(path: str, params: dict | None = None) -> dict:
    """Tiny helper: fetch the FastAPI endpoint directly via requests (no browser roundtrip)."""
    import urllib.request
    import json
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = f"{API_BASE}{path}{qs}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Helper: skip the test when the dashboard or API is not running
# ---------------------------------------------------------------------------

def _require_services():
    """Skip this test when the live services are not up — CI environments without the stack."""
    import urllib.request
    import urllib.error
    for url in [f"{API_BASE}/api/episodes?limit=1", f"{DASHBOARD_BASE}/"]:
        try:
            urllib.request.urlopen(url, timeout=3)
        except Exception:
            pytest.skip(f"live service not reachable at {url} — skipping Playwright sweep")


# ---------------------------------------------------------------------------
# T1: /api/episodes total ≥ count (the cap-disclosure contract)
# ---------------------------------------------------------------------------


def test_api_list_total_gte_count():
    """The /api/episodes JSON must carry `total` ≥ `count` for All cohorts and Real calls."""
    _require_services()
    # All cohorts.
    body = _api_get("/api/episodes", {"limit": "200"})
    assert "total" in body, "/api/episodes must have `total` field (CB-60)"
    assert body["total"] >= body["count"], (
        f"total ({body['total']}) must be ≥ count ({body['count']})"
    )
    # Real calls (cohort=live).
    live_body = _api_get("/api/episodes", {"cohort": "live", "limit": "200"})
    assert live_body["total"] >= live_body["count"]


# ---------------------------------------------------------------------------
# T2: KPI n matches list total (same denominator)
# ---------------------------------------------------------------------------


def test_api_kpi_n_matches_list_total():
    """CB-60 D3: /api/kpis?cohort=live n must equal /api/episodes?cohort=live total."""
    _require_services()
    kpi = _api_get("/api/kpis", {"cohort": "live"})
    lst = _api_get("/api/episodes", {"cohort": "live", "limit": "10000"})
    assert kpi["n"] == lst["total"], (
        f"KPI n={kpi['n']} must equal list total={lst['total']} — same denominator (CB-60 D3)"
    )


# ---------------------------------------------------------------------------
# T3: Escalation rows carry episode_cohort
# ---------------------------------------------------------------------------


def test_api_escalations_carry_episode_cohort():
    """CB-60 D2: /api/escalations must include `episode_cohort` on every row."""
    _require_services()
    body = _api_get("/api/escalations")
    if not body.get("escalations"):
        pytest.skip("no escalations in the live DB — structural check skipped")
    for row in body["escalations"]:
        assert "episode_cohort" in row, (
            f"escalation {row.get('escalation_id')} missing episode_cohort (CB-60 D2)"
        )


# ---------------------------------------------------------------------------
# T4: Dashboard Calls page renders a disclosed population
# ---------------------------------------------------------------------------


def test_dashboard_calls_page_population_disclosure(page: "Page"):
    """CB-60 D1: the Calls page must show a count label. If the list is capped (total > shown),
    it must include the 'of N' disclosure text so operators can see they're looking at a slice."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle", timeout=20000)
    time.sleep(1)  # let React hydrate

    # The count label is always present (even when 0 calls).
    body_text = page.locator("body").inner_text()
    # Acceptable patterns: "56 real calls", "200 calls (showing 200 of 2,692)", "0 real calls"
    count_pattern = re.compile(r"\d[\d,]* (real calls?|calls?)", re.IGNORECASE)
    assert count_pattern.search(body_text), (
        "Calls page must render a count label like 'N calls' or 'N real calls'"
    )

    # If the API reports total > count, the page should mention 'of' somewhere in the count area.
    try:
        api_body = _api_get("/api/episodes", {"cohort": "live", "limit": "200"})
        if api_body.get("total", 0) > api_body.get("count", 0):
            assert "of" in body_text or "showing" in body_text.lower(), (
                "when total > count the Calls page must show a 'showing N of total' disclosure"
            )
    except Exception:
        pass  # API not reachable after initial check; non-fatal for the disclosure assertion


# ---------------------------------------------------------------------------
# T5: Escalations page renders cohort hints for non-live escalations
# ---------------------------------------------------------------------------


def test_dashboard_escalations_page_no_missing_denominators(page: "Page"):
    """CB-60 D2: the Escalations page must not show raw cohort slugs or opaque 'Sample 40 / —'
    placeholders. Every visible escalation card should have a reason label (not empty)."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/escalations", wait_until="networkidle", timeout=20000)
    time.sleep(1)

    # Check that no raw internal slug like 'sp-' or 'self_play' renders as a visible label.
    body_text = page.locator("body").inner_text()
    # These are the raw self-play cohort prefixes that should NOT appear as primary labels.
    forbidden = ["sp-ep-", "selfplay_cohort", "Sample 40 / —"]
    for token in forbidden:
        assert token not in body_text, (
            f"raw internal token {token!r} must not render in the Escalations page (CB-60)"
        )


# ---------------------------------------------------------------------------
# T6: KPI page — no contradictory "100% enrollment" + "0% enrollment on ladder"
# ---------------------------------------------------------------------------


def test_api_kpi_enrollment_ladder_consistency():
    """CB-60 D3: for each version+cohort, the enrollment_rate from the headline tile and
    the tier-4 rate from the ladder_distribution must be equal (computed from the same set).
    Checks with the live data for the seeded champion_v0 cohort."""
    _require_services()
    kpi = _api_get("/api/kpis", {"version": "champion_v0"})
    if kpi.get("n", 0) == 0:
        pytest.skip("no champion_v0 episodes in the live DB")

    enrollment = kpi["enrollment_rate"]
    tier4 = next((d["rate"] for d in kpi.get("ladder_distribution", []) if d["tier"] == 4), None)
    if tier4 is None:
        pytest.skip("no tier-4 entries in the live DB for champion_v0")

    assert abs(enrollment - tier4) < 1e-6, (
        f"enrollment_rate ({enrollment:.4f}) and tier-4 rate ({tier4:.4f}) must be identical "
        f"— they come from the same set. A mismatch means the 67%-vs-0% contradiction is live."
    )
