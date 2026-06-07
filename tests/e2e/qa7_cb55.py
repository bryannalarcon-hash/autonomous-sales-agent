# qa7_cb55.py — CB-55 regression: lab->review handoff for composite episode IDs (RUN-...::arm::n).
# Navigates from the experiment detail page (/improve/lab/[run-id]) to a champion arm call and a
# challenger arm call and asserts that a full transcript renders (not a "Call not found" error).
# Designed to FAIL before the CB-55 fix (double-encoding of :: separators) and PASS after.
# Screenshots saved to /tmp/qa7/. Run with: /usr/bin/python3 tests/e2e/qa7_cb55.py
import os
import sys
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:3000"
RUN_ID = "RUN-champion_v0__playbooks_discovery_sequence__7"
SCREENSHOT_DIR = "/tmp/qa7"

os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def shot(page, name: str) -> None:
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    page.screenshot(path=path)
    print(f"  [screenshot] {path}")


def assert_transcript_renders(page, arm: str) -> None:
    """Assert a review page shows transcript turns, not an error."""
    # Error state: "Call not found" heading is present
    error_heading = page.locator("h3", has_text="Call not found")
    # Transcript turn: .rv-turn elements in the transcript panel
    turn_elements = page.locator(".rv-turn")

    shot(page, f"review_{arm}_loaded")

    # Check for the error case first (pre-fix: this will be present)
    if error_heading.count() > 0:
        shot(page, f"review_{arm}_ERROR")
        # Capture the error text
        err_p = page.locator(".empty p").first
        err_text = err_p.inner_text() if err_p.count() > 0 else "(no error text found)"
        print(f"  [FAIL] {arm}: 'Call not found' error shown: {err_text!r}")
        return False

    # Check that at least one transcript turn rendered
    try:
        expect(turn_elements.first).to_be_visible(timeout=8000)
        count = turn_elements.count()
        print(f"  [PASS] {arm}: transcript renders ({count} turns)")
        return True
    except Exception as exc:
        shot(page, f"review_{arm}_no_turns")
        print(f"  [FAIL] {arm}: no transcript turns visible — {exc}")
        return False


def run() -> int:
    """Return exit code: 0 = all pass, 1 = any fail."""
    failures = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        # ── Step 1: Load the experiment detail page ──────────────────────────────
        detail_url = f"{BASE}/improve/lab/{RUN_ID}"
        print(f"\n[1] Loading experiment detail page: {detail_url}")
        page.goto(detail_url, wait_until="networkidle")
        shot(page, "01_lab_detail_page")

        # Verify the page loaded (not a 404 / blank)
        title = page.title()
        print(f"  Page title: {title!r}")

        # Wait for the arm call buttons to appear
        call_buttons = page.locator("button[title='Open this call in Review']")
        try:
            expect(call_buttons.first).to_be_visible(timeout=10000)
        except Exception as exc:
            shot(page, "01_lab_detail_NO_CALLS")
            print(f"  [FAIL] No arm call buttons found on detail page: {exc}")
            failures.append("detail_page_no_calls")
            browser.close()
            return 1

        total_buttons = call_buttons.count()
        print(f"  Found {total_buttons} arm call buttons")
        shot(page, "02_lab_detail_calls_visible")

        # ── Step 2: Click the first CHAMPION arm call ─────────────────────────────
        print(f"\n[2] Clicking champion arm call (button 1)…")
        # The champion column is the first ArmColumn (accent=false)
        champion_btn = call_buttons.nth(0)
        champion_btn.click()

        # Wait for navigation to /operate/review/...
        page.wait_for_url("**/operate/review/**", timeout=8000)
        review_url = page.url
        print(f"  Navigated to: {review_url}")

        # Check for double-encoding in the URL (pre-fix symptom)
        if "%253A" in review_url:
            print(f"  [BUG] Double-encoding detected in URL: %253A present")
        elif "%3A" in review_url:
            print(f"  [NOTE] Single-encoding in URL: %3A present (expected pre-fix)")
        else:
            print(f"  [NOTE] No encoding in URL (post-fix with plain :: OK too)")

        # Wait for the review page to settle
        page.wait_for_load_state("networkidle")
        ok = assert_transcript_renders(page, "champion")
        if not ok:
            failures.append("champion_arm_no_transcript")

        # ── Step 3: Go back and click a CHALLENGER arm call ───────────────────────
        print(f"\n[3] Going back to detail page for challenger arm…")
        page.goto(detail_url, wait_until="networkidle")
        shot(page, "03_back_to_detail")

        call_buttons = page.locator("button[title='Open this call in Review']")
        expect(call_buttons.first).to_be_visible(timeout=10000)

        # The challenger column comes second — find its first button
        # ArmColumn renders two separate divs; challenger buttons come after champion buttons
        # The total count / 2 boundary finds the challenger section
        total_buttons = call_buttons.count()
        challenger_idx = total_buttons // 2  # first button in the second (challenger) column
        print(f"  Clicking challenger arm call (button {challenger_idx + 1} of {total_buttons})…")
        challenger_btn = call_buttons.nth(challenger_idx)
        challenger_btn.click()

        page.wait_for_url("**/operate/review/**", timeout=8000)
        review_url = page.url
        print(f"  Navigated to: {review_url}")

        page.wait_for_load_state("networkidle")
        ok = assert_transcript_renders(page, "challenger")
        if not ok:
            failures.append("challenger_arm_no_transcript")

        # ── Step 4: Verify error page shows clean message (not raw ID) ────────────
        print(f"\n[4] Verifying error page shows clean message for a bogus ID…")
        bogus_url = f"{BASE}/operate/review/nonexistent-call-99"
        page.goto(bogus_url, wait_until="networkidle")
        page.wait_for_load_state("networkidle")
        shot(page, "04_error_page_bogus_id")

        error_h3 = page.locator("h3", has_text="Call not found")
        if error_h3.count() > 0:
            err_p = page.locator(".empty p").first
            err_text = err_p.inner_text() if err_p.count() > 0 else ""
            print(f"  Error text: {err_text!r}")
            # Should NOT contain the raw encoded ID blob
            if "%3A%3A" in err_text or "%253A" in err_text:
                print(f"  [FAIL] Error page leaks raw encoded ID: {err_text!r}")
                failures.append("error_page_leaks_raw_id")
            else:
                print(f"  [PASS] Error page shows clean message")
        else:
            print(f"  [NOTE] No 'Call not found' heading on bogus-ID page")

        browser.close()

    # ── Summary ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if failures:
        print(f"RESULT: FAIL — {len(failures)} assertion(s) failed: {failures}")
        return 1
    else:
        print(f"RESULT: PASS — all assertions passed")
        return 0


if __name__ == "__main__":
    sys.exit(run())
