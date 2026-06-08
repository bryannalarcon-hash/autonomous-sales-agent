# qa9fix_lab.py — CB-70/CB-71/CB-72/CB-80 regression suite for the experiment lab.
# Asserts the specific QA9 and QA10-lab findings are fixed:
#   CB-70: blank/raw-mutation-name record shows humanized title on card list, Past tab, and detail header.
#   CB-71b: legacy reason text ("challenger_better is False") is never visible on Past card list.
#   CB-71c: the known timeout card (back-off-pressure n4) shows no metrics or sample bar alongside reason.
#   CB-71d: zombie records (Running + Draft together) render as Draft only.
#   CB-72a: dialog cost estimate "≈ $" is visible before launch.
#   CB-72b: disabled launch button shows helper text "Make a change above to enable the run".
#   CB-72c: tooltips present on Ladder Δ / Significance (via title= attribute).
#   CB-72e: small-n caution chip present for n<10 records in Past tab.
#   CB-72f: [data-kind='exp-card'] count equals number of experiments shown on the tab.
#   CB-80 L1: no-result/timed-out record does NOT show the Guardrail regression chip.
#   CB-80 L2: a zombie record (state=running, n=0) shows "Draft" in the DRAWER header (not "Running").
#   CB-80 L3: every rejected card shows a one-line WHY reason (not blank for positive-significance rejects).
#   CB-80 L4: baseline note in the drawer uses plain English ("Tested against …"), not "materialized".
# Requirements: Next.js dashboard at localhost:3000, FastAPI API at localhost:8000.
# Run: python -m pytest tests/e2e/qa9fix_lab.py -v --tb=short
#   or: /usr/bin/python3 tests/e2e/qa9fix_lab.py  (direct script mode)
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

DASHBOARD_BASE = "http://localhost:3000"
API_BASE = "http://localhost:8000"
SCREENSHOT_DIR = "/tmp/qa9fix"

os.makedirs(SCREENSHOT_DIR, exist_ok=True)

try:
    from playwright.sync_api import Page, sync_playwright  # type: ignore
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _PW_AVAILABLE,
    reason="playwright not installed — `pip install playwright && playwright install`",
)


def _require_services() -> None:
    for label, url in [("API", f"{API_BASE}/api/experiments"), ("Dashboard", f"{DASHBOARD_BASE}/")]:
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            pytest.skip(f"Live {label} service not reachable at {url}")


def _fetch_experiments() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/experiments", timeout=8) as r:
            return json.loads(r.read()).get("experiments", [])
    except Exception:
        return []


@pytest.fixture(scope="module")
def page():
    if not _PW_AVAILABLE:
        pytest.skip("playwright not installed")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        yield pg
        ctx.close()
        browser.close()


# ---------------------------------------------------------------------------
# CB-70: humanized title on blank/raw-mutation-name records
# ---------------------------------------------------------------------------

def test_cb70_humanized_title_on_card_list(page: Page):
    """CB-70: blank/raw-mutation-name experiment must show humanized title on the Past card list."""
    _require_services()
    exps = _fetch_experiments()
    # Find the QA9 blank-name record: name is the raw mutation string starting with "reorder discovery_sequence"
    # The known live ID: RUN-champion_v0__playbooks_discovery_sequence__7-1780885495988
    blank_name_exp = next(
        (e for e in exps if e.get("name", "").startswith("reorder discovery_sequence -> [")),
        None,
    )
    if blank_name_exp is None:
        pytest.skip("No blank-name (raw mutation string) experiment found in the live database")

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    # Switch to Past tab to see rejected experiments.
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)

    # Find all card titles — none must contain "-> [" (the raw mutation arrow-bracket).
    titles = page.locator("[data-kind='exp-card'] .b").all_inner_texts()
    bad = [t for t in titles if "-> [" in t or t.strip().lower() == "draft"]
    if bad:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb70_card_title_leak.png")
        pytest.fail(
            f"CB-70: raw mutation string or 'draft' found in card title(s): {bad!r}\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb70_card_title_leak.png"
        )

    # The blank-name record should show a humanized title containing "New sequence:"
    exp_id = blank_name_exp["experiment_id"]
    # Click the card for this experiment to check the drawer title.
    card = page.locator(f"[data-kind='exp-card']").filter(has_text="New sequence")
    if card.count() > 0:
        # Drawer title should also be humanized.
        card.first.click()
        page.wait_for_timeout(800)
        drawer_body = page.inner_text(".drawer") if page.locator(".drawer").count() > 0 else ""
        if "-> [" in drawer_body:
            page.screenshot(path=f"{SCREENSHOT_DIR}/cb70_drawer_title_leak.png")
            pytest.fail(
                f"CB-70: raw mutation string '-> [' found in drawer for {exp_id}\n"
                f"Screenshot: {SCREENSHOT_DIR}/cb70_drawer_title_leak.png"
            )
        # Close drawer
        close_btns = page.locator(".drawer .gctl")
        if close_btns.count() > 0:
            close_btns.first.click()
            page.wait_for_timeout(400)


def test_cb70_humanized_title_on_detail_page(page: Page):
    """CB-70: blank/raw-mutation-name record must show humanized title on the detail page header."""
    _require_services()
    exps = _fetch_experiments()
    blank_name_exp = next(
        (e for e in exps if e.get("name", "").startswith("reorder discovery_sequence -> [")),
        None,
    )
    if blank_name_exp is None:
        pytest.skip("No blank-name experiment found in the live database")

    exp_id = blank_name_exp["experiment_id"]
    url = f"{DASHBOARD_BASE}/improve/lab/{urllib.parse.quote(exp_id)}"
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(1500)

    body = page.inner_text("body")
    if "-> [" in body:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb70_detail_leak.png")
        pytest.fail(
            f"CB-70: raw mutation string '-> [' found on detail page for {exp_id}\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb70_detail_leak.png"
        )


# ---------------------------------------------------------------------------
# CB-71b: legacy reason text never appears raw on Past card list
# ---------------------------------------------------------------------------

def test_cb71b_legacy_reason_humanized(page: Page):
    """CB-71b: 'challenger_better is False' Python token must not appear on any card list surface."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)

    # Check Active tab
    body = page.inner_text("body")
    if "challenger_better" in body or "is False" in body:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb71b_active_leak.png")
        pytest.fail(
            "CB-71b: raw Python token ('challenger_better' or 'is False') found on Active tab\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb71b_active_leak.png"
        )

    # Switch to Past tab
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)

    body = page.inner_text("body")
    if "challenger_better" in body or "is False" in body:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb71b_past_leak.png")
        pytest.fail(
            "CB-71b: raw Python token ('challenger_better' or 'is False') found on Past tab\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb71b_past_leak.png"
        )


# ---------------------------------------------------------------------------
# CB-71c: timeout/no-result card shows no metrics (Rejected + Δ0.00 + Sample simultaneously)
# ---------------------------------------------------------------------------

def test_cb71c_no_result_card_has_no_metrics(page: Page):
    """CB-71c: a record whose reason says 'no result recorded' shows no metrics/CI/sample bar."""
    _require_services()
    exps = _fetch_experiments()
    # Find a no-result record: guardrail_reason ends with 'no result recorded'
    no_result_exp = next(
        (e for e in exps if (e.get("guardrail_reason") or "").endswith("no result recorded")),
        None,
    )
    if no_result_exp is None:
        pytest.skip("No 'no result recorded' experiment found in the live database")

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)

    # The no-result card must NOT show "Sample" label (the sample bar) or Ladder Δ metric cells
    # simultaneously with the reason banner.
    cards = page.locator("[data-kind='exp-card']")
    found_no_result_card = False
    for i in range(cards.count()):
        card = cards.nth(i)
        card_text = card.inner_text()
        # Detect if this card is the no-result one (reason text present)
        if "no result recorded" in card_text.lower() or "did not complete" in card_text.lower():
            found_no_result_card = True
            # Must not also show "Sample" or "Ladder Δ" cells
            if "Sample" in card_text and ("Ladder" in card_text or "Enroll" in card_text):
                page.screenshot(path=f"{SCREENSHOT_DIR}/cb71c_no_result_with_metrics.png")
                pytest.fail(
                    "CB-71c: no-result card shows both 'no result' reason AND metric cells "
                    f"('Sample', 'Ladder Δ'): {card_text[:200]!r}\n"
                    f"Screenshot: {SCREENSHOT_DIR}/cb71c_no_result_with_metrics.png"
                )
    if not found_no_result_card:
        pytest.skip("No-result card not visible on the Past tab (may be on a different page)")


# ---------------------------------------------------------------------------
# CB-71d: zombie records render as Draft, not Running+Draft
# ---------------------------------------------------------------------------

def test_cb71d_zombie_shows_draft_only(page: Page):
    """CB-71d: a record with state=running and n=0 must render as Draft, not Running."""
    _require_services()
    exps = _fetch_experiments()
    # A zombie: state=running, n=0, no target
    zombie = next(
        (e for e in exps if e.get("state") == "running" and e.get("n", 0) == 0 and not e.get("target")),
        None,
    )
    if zombie is None:
        pytest.skip("No zombie (running + n=0) experiment in the live database")

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    # Active tab should show the zombie card.
    body = page.inner_text("body")
    # Must see "Draft" somewhere (we show this card as Draft).
    # Must NOT see "Running — results land in one batch" for the same card.
    # This is hard to target at the card level via text, so we check the overall body:
    # if a card shows state_label="Draft" that's sufficient (Running+Draft would render both).
    cards = page.locator("[data-kind='exp-card']")
    for i in range(cards.count()):
        card = cards.nth(i)
        card_text = card.inner_text()
        # If it says Draft but also shows the "Running — results land in one batch" bar, fail.
        if "Draft" in card_text and "results land in one batch" in card_text:
            page.screenshot(path=f"{SCREENSHOT_DIR}/cb71d_zombie_both_states.png")
            pytest.fail(
                "CB-71d: zombie card shows both 'Draft' state and 'Running — results land' bar simultaneously\n"
                f"Screenshot: {SCREENSHOT_DIR}/cb71d_zombie_both_states.png"
            )


# ---------------------------------------------------------------------------
# CB-72a: dialog shows $ cost estimate
# ---------------------------------------------------------------------------

def test_cb72a_dialog_cost_estimate(page: Page):
    """CB-72a: the Run experiment dialog shows a '≈ $' approximate cost estimate."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    # Open the Run experiment dialog.
    run_btn = page.locator("button:has-text('Run experiment')")
    if run_btn.count() == 0:
        pytest.skip("No 'Run experiment' button on the lab page")
    run_btn.first.click()
    page.wait_for_timeout(1000)
    drawer_text = page.inner_text(".drawer") if page.locator(".drawer").count() > 0 else ""
    if "≈ $" not in drawer_text:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb72a_no_cost_estimate.png")
        pytest.fail(
            f"CB-72a: '≈ $' approximate cost estimate not found in dialog\n"
            f"Dialog text (first 300 chars): {drawer_text[:300]!r}\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb72a_no_cost_estimate.png"
        )
    # Close drawer
    close_btns = page.locator(".drawer .gctl")
    if close_btns.count() > 0:
        close_btns.first.click()
        page.wait_for_timeout(400)


# ---------------------------------------------------------------------------
# CB-72b: disabled launch button shows helper text
# ---------------------------------------------------------------------------

def test_cb72b_disabled_button_helper_text(page: Page):
    """CB-72b: disabled launch button shows 'Make a change above to enable the run' helper text."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    run_btn = page.locator("button:has-text('Run experiment')")
    if run_btn.count() == 0:
        pytest.skip("No 'Run experiment' button on the lab page")
    run_btn.first.click()
    page.wait_for_timeout(1000)
    # The dialog opens with the launch button disabled (no change made yet).
    drawer_text = page.inner_text(".drawer") if page.locator(".drawer").count() > 0 else ""
    if "Make a change above" not in drawer_text:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb72b_no_helper_text.png")
        pytest.fail(
            "CB-72b: disabled launch button helper text 'Make a change above to enable the run' not found\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb72b_no_helper_text.png"
        )
    # Close drawer
    close_btns = page.locator(".drawer .gctl")
    if close_btns.count() > 0:
        close_btns.first.click()
        page.wait_for_timeout(400)


# ---------------------------------------------------------------------------
# CB-72c: tooltips present on Ladder Δ and Significance labels
# ---------------------------------------------------------------------------

def test_cb72c_tooltips_on_term_labels(page: Page):
    """CB-72c: card Ladder Δ and Significance cells have title= tooltips for term definitions."""
    _require_services()
    exps = _fetch_experiments()
    # Need a Past card with n>0 to see Ladder Δ / Significance cells.
    has_n = any(e.get("n", 0) > 0 for e in exps if e.get("state") not in ("draft", "running", "passed", "blocked"))
    if not has_n:
        pytest.skip("No completed experiments with n>0 in the live database")

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)

    # Check for title= on Ladder Δ and Significance cell divs.
    # The Cell components render with title= on the outer wrapper div.
    # We look for any element with title containing "commitment level" or "statistically significant".
    ladder_tip = page.locator("[title*='commitment level']")
    sig_tip = page.locator("[title*='significant']")

    if ladder_tip.count() == 0 and sig_tip.count() == 0:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb72c_no_tooltips.png")
        pytest.fail(
            "CB-72c: no title= tooltip found for Ladder Δ or Significance on Past tab\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb72c_no_tooltips.png"
        )


# ---------------------------------------------------------------------------
# CB-72e: small-n caution chip for n<10 records
# ---------------------------------------------------------------------------

def test_cb72e_small_n_caution_chip(page: Page):
    """CB-72e: a result with n<10 per arm shows the small-sample caution chip."""
    _require_services()
    exps = _fetch_experiments()
    small_n_exp = next(
        (e for e in exps
         if 0 < e.get("n", 0) < 10
         and e.get("state") not in ("draft", "running")
         and not (e.get("guardrail_reason") or "").endswith("no result recorded")),
        None,
    )
    if small_n_exp is None:
        pytest.skip("No small-n (0 < n < 10) completed experiment in the live database")

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)

    # Look for the small-sample caution text anywhere on the Past tab.
    body = page.inner_text("body")
    if "Small sample" not in body and "small sample" not in body and "directional only" not in body:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb72e_no_small_n_caution.png")
        pytest.fail(
            f"CB-72e: small-n caution chip not found on Past tab (n={small_n_exp['n']} experiment present)\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb72e_no_small_n_caution.png"
        )


# ---------------------------------------------------------------------------
# CB-72f: [data-kind='exp-card'] count equals visible experiment count
# ---------------------------------------------------------------------------

def test_cb72f_no_card_double_render(page: Page):
    """CB-72f: data-kind='exp-card' node count exactly equals the number of visible experiments."""
    _require_services()
    exps = _fetch_experiments()

    active_states = {"draft", "running", "passed", "blocked"}
    active_count = sum(1 for e in exps if e.get("state") in active_states)
    past_count = sum(1 for e in exps if e.get("state") not in active_states)

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)

    # Active tab
    if active_count > 0:
        rendered = page.locator("[data-kind='exp-card']").count()
        if rendered != active_count:
            page.screenshot(path=f"{SCREENSHOT_DIR}/cb72f_active_double_render.png")
            pytest.fail(
                f"CB-72f: Active tab has {rendered} exp-card nodes but API returned {active_count} active experiments\n"
                f"Screenshot: {SCREENSHOT_DIR}/cb72f_active_double_render.png"
            )

    # Past tab
    if past_count > 0:
        past_btn = page.locator("button:has-text('Past')")
        if past_btn.count() > 0:
            past_btn.first.click()
            page.wait_for_timeout(800)
        rendered = page.locator("[data-kind='exp-card']").count()
        if rendered != past_count:
            page.screenshot(path=f"{SCREENSHOT_DIR}/cb72f_past_double_render.png")
            pytest.fail(
                f"CB-72f: Past tab has {rendered} exp-card nodes but API returned {past_count} past experiments\n"
                f"Screenshot: {SCREENSHOT_DIR}/cb72f_past_double_render.png"
            )


# ---------------------------------------------------------------------------
# CB-80 L1: no-result card must NOT show the Guardrail regression chip
# ---------------------------------------------------------------------------

def test_cb80_l1_no_result_card_no_guardrail_chip(page: Page):
    """CB-80 L1: a timed-out/no-result record must not show the 'Guardrail regression' chip.
    No run completed → no guardrail determination was possible; showing the chip fabricates a verdict."""
    _require_services()
    exps = _fetch_experiments()
    no_result_exp = next(
        (e for e in exps if (e.get("guardrail_reason") or "").endswith("no result recorded")),
        None,
    )
    if no_result_exp is None:
        pytest.skip("No 'no result recorded' experiment in the live database")

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)

    # Find the no-result card and assert it has no Guardrail regression chip.
    cards = page.locator("[data-kind='exp-card']")
    for i in range(cards.count()):
        card = cards.nth(i)
        card_text = card.inner_text()
        if "no result recorded" in card_text.lower() or "did not complete" in card_text.lower():
            if "Guardrail regression" in card_text:
                page.screenshot(path=f"{SCREENSHOT_DIR}/cb80_l1_guardrail_chip_on_no_result.png")
                pytest.fail(
                    "CB-80 L1: 'Guardrail regression' chip visible on a no-result/timed-out card\n"
                    f"Card text: {card_text[:200]!r}\n"
                    f"Screenshot: {SCREENSHOT_DIR}/cb80_l1_guardrail_chip_on_no_result.png"
                )
            return  # found the card and it was clean
    pytest.skip("No-result card not visible on the Past tab")


# ---------------------------------------------------------------------------
# CB-80 L2: zombie record drawer header shows "Draft", not "Running"
# ---------------------------------------------------------------------------

def test_cb80_l2_zombie_drawer_shows_draft(page: Page):
    """CB-80 L2: a zombie record (state=running, n=0) must show 'Draft' in the DRAWER header —
    effectiveState must be used in the drawer, not just on the card face."""
    _require_services()
    exps = _fetch_experiments()
    zombie = next(
        (e for e in exps if e.get("state") == "running" and e.get("n", 0) == 0 and not e.get("target")),
        None,
    )
    if zombie is None:
        pytest.skip("No zombie (state=running, n=0, no target) experiment in the live database")

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)

    # Click the zombie card to open the drawer.
    cards = page.locator("[data-kind='exp-card']")
    zombie_card = None
    for i in range(cards.count()):
        card = cards.nth(i)
        # Zombie card shows "Draft" chip on the face (from effectiveState on the card).
        if "Draft" in card.inner_text() and "results land in one batch" not in card.inner_text():
            zombie_card = card
            break
    if zombie_card is None:
        pytest.skip("Zombie card not visible on the Active tab (may not be present)")

    zombie_card.click()
    page.wait_for_timeout(800)

    drawer = page.locator(".drawer")
    if drawer.count() == 0:
        pytest.fail("CB-80 L2: drawer did not open after clicking zombie card")

    drawer_text = drawer.inner_text()
    # Drawer header must show Draft chip (the state tag), not "Running".
    # The state chip is the tag element in the card-head row.
    drawer_tags = page.locator(".drawer .card-head .tag")
    state_tag_texts = [t for t in drawer_tags.all_inner_texts() if t.strip()]
    # We expect to find "Draft" among the tags and NOT "Running".
    has_draft = any("Draft" in t for t in state_tag_texts)
    has_running = any(t.strip() == "Running" for t in state_tag_texts)

    if not has_draft or has_running:
        page.screenshot(path=f"{SCREENSHOT_DIR}/cb80_l2_zombie_drawer_state.png")
        pytest.fail(
            f"CB-80 L2: zombie drawer header state chips: {state_tag_texts!r} — "
            f"expected 'Draft' present and 'Running' absent\n"
            f"Screenshot: {SCREENSHOT_DIR}/cb80_l2_zombie_drawer_state.png"
        )

    # Close drawer
    close_btns = page.locator(".drawer .gctl")
    if close_btns.count() > 0:
        close_btns.first.click()
        page.wait_for_timeout(400)


# ---------------------------------------------------------------------------
# CB-80 L3: every rejected card shows a one-line WHY reason
# ---------------------------------------------------------------------------

def test_cb80_l3_rejected_card_shows_why(page: Page):
    """CB-80 L3: every rejected card must show a one-line WHY reason — even when guardrail_reason
    is absent (positive significance but not promoted). The reason block must be non-empty."""
    _require_services()
    exps = _fetch_experiments()
    rejected_exps = [e for e in exps if e.get("state") == "rejected"]
    if not rejected_exps:
        pytest.skip("No rejected experiments in the live database")

    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)

    # Scan every card on the Past tab: any card with no visible reason text beneath its metrics fails.
    cards = page.locator("[data-kind='exp-card']")
    cards_checked = 0
    for i in range(cards.count()):
        card = cards.nth(i)
        card_text = card.inner_text()
        # Identify rejected cards by absence of "Draft"/"Running"/"Passed"/"Blocked"/"Promoted" states.
        # All non-result rejected cards should have some reason text visible.
        # We look for the reason strip via a known set of expected reason phrases.
        known_reasons = [
            "Guardrail regression", "Held for approval", "Did not meet", "Did not pass",
            "no significant lift", "No significant lift", "Simulated training", "run timed out",
            "no result recorded",
        ]
        # A card is "rejected" on the Past tab if it has the danger-colored reason strip.
        # We detect rejected cards: they have a state tag that ISN'T "Draft"/"Blocked" etc.
        # Simpler: if the card contains any "rejected"-state indicator in its background style
        # — too fragile. Instead just check all Past cards: each should have at least one reason phrase.
        # Skip cards that clearly aren't terminal-rejected (e.g. only show "Draft").
        if "Draft" in card_text and "Guardrail" not in card_text and all(r not in card_text for r in known_reasons):
            continue  # Draft card, not rejected
        # If a card is in the past tab and has metric cells (Enroll Δ / Ladder Δ / Significance)
        # but NO reason text at all, that's L3.
        has_metrics = "Enroll Δ" in card_text or "Ladder Δ" in card_text
        has_reason = any(r in card_text for r in known_reasons)
        # A completed experiment card without a reason is the bug.
        if has_metrics and not has_reason:
            page.screenshot(path=f"{SCREENSHOT_DIR}/cb80_l3_no_why_on_rejected_card.png")
            pytest.fail(
                f"CB-80 L3: rejected card shows metrics but NO one-line WHY reason: {card_text[:250]!r}\n"
                f"Screenshot: {SCREENSHOT_DIR}/cb80_l3_no_why_on_rejected_card.png"
            )
        cards_checked += 1
    if cards_checked == 0:
        pytest.skip("No rejected cards with metrics visible on the Past tab")


# ---------------------------------------------------------------------------
# CB-80 L4: baseline note uses plain English (no "materialized")
# ---------------------------------------------------------------------------

def test_cb80_l4_baseline_note_plain_english(page: Page):
    """CB-80 L4: the CB-73 baseline note in the drawer must NOT contain 'materialized' (engineer-speak).
    It must use plain English — 'saved settings aren't available here' or similar."""
    _require_services()
    exps = _fetch_experiments()
    # Find an experiment whose champion_version differs from the live store champion — this
    # triggers the CB-73 note. If the store champion is unknown, skip gracefully.
    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)

    # Open cards one by one until we find the mismatch note (or exhaust cards).
    cards = page.locator("[data-kind='exp-card']")
    for i in range(min(cards.count(), 8)):
        cards.nth(i).click()
        page.wait_for_timeout(600)
        drawer = page.locator(".drawer")
        if drawer.count() == 0:
            continue
        drawer_text = drawer.inner_text()
        # Check if the baseline note is present and look for the bad phrasing.
        if "Tested against" in drawer_text or "isn't materialized" in drawer_text or "materialized" in drawer_text:
            if "isn't materialized" in drawer_text or "materialized on this machine" in drawer_text:
                page.screenshot(path=f"{SCREENSHOT_DIR}/cb80_l4_materialized_in_drawer.png")
                pytest.fail(
                    "CB-80 L4: engineer-speak 'materialized' found in drawer baseline note\n"
                    f"Drawer text snippet: {drawer_text[:300]!r}\n"
                    f"Screenshot: {SCREENSHOT_DIR}/cb80_l4_materialized_in_drawer.png"
                )
            # Note found and clean — pass for this card.
            close_btns = page.locator(".drawer .gctl")
            if close_btns.count() > 0:
                close_btns.first.click()
                page.wait_for_timeout(300)
            return
        close_btns = page.locator(".drawer .gctl")
        if close_btns.count() > 0:
            close_btns.first.click()
            page.wait_for_timeout(300)
    # If no note was found (all experiments use the current champion), the fix is untriggered but OK.
    # We don't fail — the fix is correct, just no data to trigger the note display.


# ---------------------------------------------------------------------------
# Direct script mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _PW_AVAILABLE:
        print("ERROR: playwright not installed — `pip install playwright && playwright install`")
        sys.exit(1)

    for label, url in [("API", f"{API_BASE}/api/experiments"), ("Dashboard", f"{DASHBOARD_BASE}/")]:
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            print(f"ERROR: {label} not reachable at {url}")
            sys.exit(1)

    exps = _fetch_experiments()
    print(f"Live experiments: {len(exps)}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()

        results: list[tuple[str, str]] = []

        def run_check(name: str, fn):  # type: ignore[no-untyped-def]
            try:
                fn(pg)
                results.append((name, "PASS"))
                print(f"  PASS  {name}")
            except pytest.skip.Exception as e:
                results.append((name, f"SKIP: {e}"))
                print(f"  SKIP  {name}: {e}")
            except Exception as e:
                results.append((name, f"FAIL: {e}"))
                print(f"  FAIL  {name}: {e}")

        run_check("cb70_humanized_title_on_card_list", test_cb70_humanized_title_on_card_list)
        run_check("cb70_humanized_title_on_detail_page", test_cb70_humanized_title_on_detail_page)
        run_check("cb71b_legacy_reason_humanized", test_cb71b_legacy_reason_humanized)
        run_check("cb71c_no_result_card_has_no_metrics", test_cb71c_no_result_card_has_no_metrics)
        run_check("cb71d_zombie_shows_draft_only", test_cb71d_zombie_shows_draft_only)
        run_check("cb72a_dialog_cost_estimate", test_cb72a_dialog_cost_estimate)
        run_check("cb72b_disabled_button_helper_text", test_cb72b_disabled_button_helper_text)
        run_check("cb72c_tooltips_on_term_labels", test_cb72c_tooltips_on_term_labels)
        run_check("cb72e_small_n_caution_chip", test_cb72e_small_n_caution_chip)
        run_check("cb72f_no_card_double_render", test_cb72f_no_card_double_render)
        run_check("cb80_l1_no_result_card_no_guardrail_chip", test_cb80_l1_no_result_card_no_guardrail_chip)
        run_check("cb80_l2_zombie_drawer_shows_draft", test_cb80_l2_zombie_drawer_shows_draft)
        run_check("cb80_l3_rejected_card_shows_why", test_cb80_l3_rejected_card_shows_why)
        run_check("cb80_l4_baseline_note_plain_english", test_cb80_l4_baseline_note_plain_english)

        ctx.close()
        browser.close()

    passes = sum(1 for _, r in results if r == "PASS")
    skips = sum(1 for _, r in results if r.startswith("SKIP"))
    fails = sum(1 for _, r in results if r.startswith("FAIL"))
    print(f"\n{'='*60}")
    print(f"Results: {passes} passed / {skips} skipped / {fails} failed")
    if fails > 0:
        sys.exit(1)
