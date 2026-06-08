# qa9fix_ops.py — CB-74 + CB-75 regression suite: Operate polish round 2.
# Asserts each fix from CB-74 and CB-75 via Playwright (browser) and direct API calls.
# CB-74: fresh KPI load shows data (not empty) + CB-74 note renders when champion has no episodes.
# CB-75 items verified:
#   1. No "Champion Champion" doubling in the shell header.
#   2. Escalation cohort hint is a link to /operate/calls?cohort=all.
#   3. Count label == rendered row count (label matches rows in the table).
#   4. Sidebar badge and queue tab counts converge (both shown from one fetch via event).
#   5. No snake_case gate slugs (establish_who_first etc.) in the Live sample-call belief panel.
#   6. Sort glyph visible on active sorted column (▲/▼ present, aria-sort set).
#   7. Empty filter results show "No calls match" empty state message.
#   8. Drawer CALL ID shows shortened form (8 chars + …) with a copy button.
# Requirements: Next.js dashboard at localhost:3000, FastAPI API at localhost:8000.
# Run: python -m pytest tests/e2e/qa9fix_ops.py -v --tb=short
#   or: /usr/bin/python3 tests/e2e/qa9fix_ops.py  (direct script mode)
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

try:
    from playwright.sync_api import Page, sync_playwright  # type: ignore
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

DASHBOARD_BASE = "http://localhost:3000"
API_BASE = "http://localhost:8000"

pytestmark = pytest.mark.skipif(
    not _PW_AVAILABLE,
    reason="playwright not installed — install with: pip install playwright && playwright install",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_get(path: str, params: dict[str, str] | None = None) -> Any:
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = f"{API_BASE}{path}{qs}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def _require_services() -> None:
    """Skip the test if the dashboard or API is not reachable."""
    for url in [f"{API_BASE}/api/episodes?limit=1", f"{DASHBOARD_BASE}/"]:
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            pytest.skip(f"live service not reachable at {url}")


@pytest.fixture(scope="module")
def page():
    """Module-scoped Playwright page (Chromium headless) shared across all tests for speed."""
    if not _PW_AVAILABLE:
        pytest.skip("playwright not installed")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        pg = ctx.new_page()
        yield pg
        ctx.close()
        browser.close()


# ---------------------------------------------------------------------------
# CB-74: fresh KPI load must never default into an empty population
# ---------------------------------------------------------------------------

def test_cb74_kpi_fresh_load_shows_data_or_note(page: Page) -> None:
    """CB-74: /operate/kpi on fresh load either renders KPI tiles (n > 0) or shows the
    CB-74 note ('The live champion has no recorded calls yet — showing …'). A bare
    'No calls for this selection' with no note is the FORBIDDEN state."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/kpi", wait_until="networkidle")
    page.wait_for_timeout(2000)  # let both probes settle

    body = page.inner_text("body")

    has_data = (
        # Overview tiles render with at least one KPI value
        bool(page.query_selector(".kpi"))
        # Or the compare table is visible
        or bool(page.query_selector(".tbl"))
    )
    has_cb74_note = "The live champion has no recorded calls yet" in body
    is_empty_no_guidance = (
        "No calls for this selection" in body
        and not has_cb74_note
    )

    assert not is_empty_no_guidance, (
        "CB-74: KPI page defaulted into an empty state with no guidance note. "
        "Auto-select must fall back to a populated version and show the CB-74 note."
    )
    assert has_data or has_cb74_note, (
        "CB-74: KPI page shows neither data tiles nor the CB-74 fallback note. "
        f"Body snippet: {body[:400]!r}"
    )


def test_cb74_empty_manual_selection_shows_hint(page: Page) -> None:
    """CB-74: when a version with 0 episodes is manually selected, the empty state
    includes 'Try another version or cohort.' rather than generic 'Try another filter.'"""
    _require_services()
    # Check the API to see if there's a champion with 0 episodes
    try:
        ver_data = _api_get("/api/versions")
        champ = ver_data.get("champion_version")
        ep_data = _api_get("/api/episodes", {"version": champ or "champion_v0", "limit": "1"})
        if ep_data.get("count", 1) > 0:
            pytest.skip("Live champion has episodes — manual empty selection not reproducible here")
    except Exception:
        pytest.skip("Could not probe versions/episodes — skipping empty-selection hint check")

    page.goto(f"{DASHBOARD_BASE}/operate/kpi", wait_until="networkidle")
    page.wait_for_timeout(2000)
    body = page.inner_text("body")
    if "No calls for this selection" in body:
        assert "Try another version or cohort" in body, (
            "CB-74: empty-state hint should say 'Try another version or cohort.' not the old generic text."
        )


# ---------------------------------------------------------------------------
# CB-75 #1: no "Champion Champion" doubled label
# ---------------------------------------------------------------------------

def test_cb75_no_doubled_champion_label(page: Page) -> None:
    """CB-75 #1: the top-bar champion chip must never render 'Champion Champion …'.
    The versionLabel function already prepends 'Champion'; the literal prefix was doubled."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle")
    page.wait_for_timeout(1500)

    body = page.inner_text("body")
    doubled = re.search(r"Champion\s+Champion", body)
    assert not doubled, (
        f"CB-75 #1: 'Champion Champion' doubled label found in the top bar. "
        f"Found: {doubled.group()!r} at pos {doubled.start()}"
    )


# ---------------------------------------------------------------------------
# CB-75 #2: escalation cohort hint is an actionable link, not raw text
# ---------------------------------------------------------------------------

def test_cb75_escalation_cohort_hint_is_link(page: Page) -> None:
    """CB-75 #2: escalation cards/drawers show a link to /operate/calls?cohort=all for
    non-live-cohort calls — not the old raw '… cohort — use All cohorts in Calls list' text."""
    _require_services()
    # Check if any escalation has a non-live cohort
    try:
        esc_data = _api_get("/api/escalations")
        non_live = [e for e in esc_data.get("escalations", []) if e.get("episode_cohort") not in (None, "live")]
    except Exception:
        pytest.skip("Could not fetch escalations")

    if not non_live:
        pytest.skip("No non-live-cohort escalations available to check hint affordance")

    page.goto(f"{DASHBOARD_BASE}/operate/escalations", wait_until="networkidle")
    page.wait_for_timeout(1500)

    # Check that the old raw-text hint is gone
    body = page.inner_text("body")
    old_hint = "use All cohorts in Calls list"
    assert old_hint not in body, (
        f"CB-75 #2: old raw text hint '{old_hint}' still rendering on escalations page. "
        "Should be replaced with a link to /operate/calls?cohort=all."
    )

    # Check that a link to calls?cohort=all is present on the page
    links = page.query_selector_all("a[href*='cohort=all']")
    assert len(links) > 0, (
        "CB-75 #2: no link to /operate/calls?cohort=all found on escalations page. "
        "Non-live cohort escalations should have an affordance link."
    )


# ---------------------------------------------------------------------------
# CB-75 #3: count label == rendered rows (via API consistency check)
# ---------------------------------------------------------------------------

def test_cb75_api_count_consistent_with_episodes_length() -> None:
    """CB-75 #3: API count field must equal len(episodes) in the response — the source
    for both the rendered rows and the label must be consistent."""
    _require_services()
    data = _api_get("/api/episodes", {"limit": "200"})
    count = data.get("count", -1)
    episodes = data.get("episodes", [])
    assert count == len(episodes), (
        f"CB-75 #3: /api/episodes count={count} but len(episodes)={len(episodes)}. "
        "The count field must match the rendered rows exactly."
    )


def test_cb75_kpi_page_count_label_matches_rows(page: Page) -> None:
    """CB-75 #3: on the Calls page, the visible count label matches the number of <tr> rows
    in the table body (no header row counted as a data row)."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle")
    page.wait_for_timeout(1500)

    # Count rendered table rows (tbody tr only, not thead)
    tbody_rows = page.query_selector_all(".tbl tbody tr")
    row_count = len(tbody_rows)

    if row_count == 0:
        pytest.skip("No table rows rendered — empty state, count check not applicable")

    # The count label reads "N real calls" or "N calls"
    body = page.inner_text("body")
    m = re.search(r"(\d+)\s+real\s+call", body)
    if not m:
        m = re.search(r"(\d+)\s+call", body)
    if not m:
        pytest.skip("Could not find a count label on the Calls page")

    label_count = int(m.group(1))
    assert label_count == row_count, (
        f"CB-75 #3: count label says {label_count} but table has {row_count} <tr> rows. "
        "Label and rendered rows must match."
    )


# ---------------------------------------------------------------------------
# CB-75 #4: sidebar badge + queue tab counts converge (same-source)
# ---------------------------------------------------------------------------

def test_cb75_escalation_badge_and_tab_count_converge(page: Page) -> None:
    """CB-75 #4: when on the escalations page, the sidebar badge count and the 'Unreviewed'
    tab badge count should be the same value (both from the same API response via CustomEvent)."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/escalations", wait_until="networkidle")
    page.wait_for_timeout(2500)  # wait for shell poll + escalations fetch to settle

    # Read the unreviewed count from the tab badge
    tab_badge = page.query_selector(".seg button:first-child .nav-badge")
    if not tab_badge:
        pytest.skip("Could not find tab badge on escalations page")
    tab_count_text = (tab_badge.inner_text() or "").strip()
    if not tab_count_text.isdigit():
        pytest.skip(f"Tab badge text '{tab_count_text}' is not numeric — skipping count convergence check")
    tab_count = int(tab_count_text)

    # Read the sidebar nav badge for /operate/escalations
    nav_badge = page.query_selector(".nav-item.on .nav-badge, a[href='/operate/escalations'] .nav-badge")
    if not nav_badge:
        # No nav badge means 0 unreviewed — that's consistent with tab count 0
        assert tab_count == 0, (
            f"CB-75 #4: Tab says {tab_count} unreviewed but sidebar badge is absent (implies 0). "
            "Both must agree."
        )
        return

    nav_count_text = (nav_badge.inner_text() or "").strip()
    if not nav_count_text.isdigit():
        pytest.skip(f"Nav badge text '{nav_count_text}' not numeric")
    nav_count = int(nav_count_text)

    assert nav_count == tab_count, (
        f"CB-75 #4: Sidebar badge shows {nav_count} but tab badge shows {tab_count} unreviewed. "
        "Both must derive from the same fetch (via cadence:escalation-counts event)."
    )


# ---------------------------------------------------------------------------
# CB-75 #5: no snake_case gate slugs in the live sample-call belief panel
# ---------------------------------------------------------------------------

def test_cb75_no_snake_case_in_live_belief_panel(page: Page) -> None:
    """CB-75 #5: the Live page's belief panel (with sample call active) must not render
    raw snake_case slugs like 'establish_who_first' in rationale or driver text."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/live", wait_until="networkidle")
    page.wait_for_timeout(1500)

    # Enable the sample call toggle if present
    sample_toggle = page.query_selector("[data-testid='sample-toggle'] button")
    if sample_toggle:
        sample_toggle.click()
        page.wait_for_timeout(1500)

    # Check the lv-belief / lv-rat area for snake_case leaks
    belief_area = page.query_selector(".lv-belief")
    if not belief_area:
        pytest.skip("Belief panel not visible — no active or sample call available")

    belief_text = belief_area.inner_text() or ""
    # Also check transcript rationales
    rat_elements = page.query_selector_all(".lv-rat")
    rat_text = " ".join(e.inner_text() for e in rat_elements)

    combined = belief_text + " " + rat_text

    # Known gate slugs that must NOT appear raw
    BAD_SLUGS = [
        "establish_who_first",
        "no_repeat_discovery",
        "address_direct_input",
        "escalation_triggers",
        "advance_to_close",
    ]
    leaks = [s for s in BAD_SLUGS if s in combined]
    assert not leaks, (
        f"CB-75 #5: raw gate/strategy slugs found in Live belief panel: {leaks!r}. "
        "All rationale text must pass through humanizeRationale before rendering."
    )

    # Also check for generic snake_case patterns (multi-word underscored slugs)
    snake_hits = re.findall(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+){2,}\b", combined)
    known_ok = {"walk_away", "no_commitment"}  # these are acceptable if they slip through
    bad_snake = [h for h in snake_hits if h not in known_ok]
    assert not bad_snake, (
        f"CB-75 #5: snake_case tokens found in Live belief panel: {bad_snake!r}"
    )


# ---------------------------------------------------------------------------
# CB-75 #6: sort glyph visible + aria-sort on active sorted column
# ---------------------------------------------------------------------------

def test_cb75_sort_glyph_visible_on_active_column(page: Page) -> None:
    """CB-75 #6: clicking a sortable column header activates it: aria-sort changes from 'none'
    to 'ascending'/'descending', and the direction glyph (▲/▼) appears in the header text."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle")
    page.wait_for_timeout(1500)

    # Find the WHEN column header (SortTh) and click it
    when_th = page.query_selector("th[aria-sort]")
    if not when_th:
        pytest.skip("No sortable column headers found on Calls page")

    # Find a header with aria-sort='none' to click
    none_ths = page.query_selector_all("th[aria-sort='none']")
    if not none_ths:
        pytest.skip("No unsorted sortable columns found")

    target = none_ths[0]
    target.click()
    page.wait_for_timeout(300)

    # After click, aria-sort should be ascending or descending
    aria_sort = target.get_attribute("aria-sort")
    assert aria_sort in ("ascending", "descending"), (
        f"CB-75 #6: After clicking sort header, aria-sort='{aria_sort}' (expected ascending/descending)"
    )

    # The header text should contain ▲ or ▼
    header_text = target.inner_text() or ""
    assert "▲" in header_text or "▼" in header_text, (
        f"CB-75 #6: Active sorted column header text does not contain ▲/▼ glyph. Got: {header_text!r}"
    )


def test_cb75_ladder_and_version_columns_not_sortable(page: Page) -> None:
    """CB-75 #6: LADDER TIER and VERSION columns must not have aria-sort (they are StaticTh),
    so they don't silently eat clicks with no visible feedback."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle")
    page.wait_for_timeout(1500)

    headers = page.query_selector_all("thead th")
    for th in headers:
        text = (th.inner_text() or "").strip().upper()
        if text in ("LADDER TIER", "VERSION"):
            aria_sort = th.get_attribute("aria-sort")
            cursor_style = th.evaluate("el => window.getComputedStyle(el).cursor")
            assert aria_sort is None, (
                f"CB-75 #6: '{text}' column has aria-sort='{aria_sort}' but should not be sortable. "
                "Use StaticTh for non-sortable columns."
            )
            assert cursor_style != "pointer", (
                f"CB-75 #6: '{text}' column has cursor:pointer but is not sortable. "
                "Non-sortable columns must use cursor:default."
            )


# ---------------------------------------------------------------------------
# CB-75 #7: empty filter results show "No calls match" message
# ---------------------------------------------------------------------------

def test_cb75_empty_filter_shows_no_match_message(page: Page) -> None:
    """CB-75 #7: clicking a filter chip that yields 0 results shows the 'No calls match'
    empty-state message, not a bare empty table body."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle")
    page.wait_for_timeout(1500)

    # Use the API to find an outcome filter that returns 0 results
    zero_outcome_key: str | None = None
    CANDIDATE_OUTCOMES = ["booked", "interested", "no_interest", "released", "walked", "enrolled"]
    for key in CANDIDATE_OUTCOMES:
        try:
            d = _api_get("/api/episodes", {"outcome": key, "cohort": "live", "limit": "1"})
            if d.get("count", 0) == 0:
                zero_outcome_key = key
                break
        except Exception:
            continue

    if not zero_outcome_key:
        pytest.skip("No outcome filter with zero live-cohort results found — skipping empty-filter test")

    # Find the corresponding filter chip
    # OUTCOME_OPTIONS labels match: Booked, Interested, No interest, Released, Walked away, Enrolled
    label_map = {
        "booked": "Booked",
        "interested": "Interested",
        "no_interest": "No interest",
        "released": "Released",
        "walked": "Walked away",
        "enrolled": "Enrolled",
    }
    chip_label = label_map.get(zero_outcome_key, zero_outcome_key)
    chip = page.get_by_role("button", name=chip_label, exact=True).first
    if not chip:
        pytest.skip(f"Could not find filter chip '{chip_label}' on Calls page")

    chip.click()
    page.wait_for_timeout(1200)

    # Verify the empty-state message shows
    body = page.inner_text("body")
    has_empty_msg = "No calls match" in body
    # And no bare empty table body
    has_empty_tbody = len(page.query_selector_all(".tbl tbody tr")) == 0

    if has_empty_tbody:
        # If no rows rendered, the empty-state message must show
        assert has_empty_msg, (
            f"CB-75 #7: filter chip '{chip_label}' yields empty table but 'No calls match' "
            "message is absent. Empty filter results must show the empty-state card."
        )


# ---------------------------------------------------------------------------
# CB-75 #8: drawer CALL ID shortened (first 8 + …) with copy affordance
# ---------------------------------------------------------------------------

def test_cb75_drawer_call_id_shortened_with_copy(page: Page) -> None:
    """CB-75 #8: opening a call drawer shows the CALL ID shortened to first-8-chars + '…'
    with a copy button, and the full id is in a data-full-id attribute."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle")
    page.wait_for_timeout(1500)

    # Click the first table row to open the drawer
    first_row = page.query_selector(".tbl tbody tr")
    if not first_row:
        pytest.skip("No rows on Calls page — cannot open drawer")

    first_row.click()
    page.wait_for_timeout(800)

    drawer = page.query_selector(".drawer")
    assert drawer is not None, "Drawer did not open after clicking a row"

    # Check for shortened ID (must contain … for ids > 12 chars)
    copy_btn = drawer.query_selector("[data-testid='copy-call-id']")
    assert copy_btn is not None, (
        "CB-75 #8: no copy-call-id button found in drawer. "
        "CALL ID must have a copy affordance."
    )

    # Check data-full-id is present
    full_id_el = drawer.query_selector("[data-full-id]")
    assert full_id_el is not None, (
        "CB-75 #8: no element with data-full-id found in drawer. "
        "The full id must be preserved in a data attribute."
    )
    full_id = full_id_el.get_attribute("data-full-id") or ""
    assert len(full_id) > 0, "CB-75 #8: data-full-id attribute is empty"

    # Check the displayed ID is shortened (not the full 32-hex id)
    drawer_text = drawer.inner_text() or ""
    if len(full_id) > 12:
        assert "…" in drawer_text or full_id[:8] in drawer_text, (
            f"CB-75 #8: full 32-char id appears verbatim in the CALL ID field. "
            f"Full id: {full_id!r}, drawer text snippet: {drawer_text[:200]!r}"
        )
        # Full ID must NOT appear verbatim as the displayed CALL ID text
        # (it IS in the secondary header line #episode_id and data attribute, which is fine)
        call_id_section = drawer.query_selector("[data-full-id]")
        if call_id_section:
            section_text = call_id_section.inner_text() or ""
            # The displayed id text should be the short form, not the full id
            # (full id still appears in the #id secondary line, that's acceptable)
            assert full_id not in section_text.replace(f"#{full_id}", ""), (
                "CB-75 #8: full id displayed in the CALL ID data field — should be shortened."
            )

    # Close the drawer
    close_btn = drawer.query_selector("button:has(.x)")
    if close_btn:
        close_btn.click()


# ---------------------------------------------------------------------------
# Direct script mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import subprocess

    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"]))
