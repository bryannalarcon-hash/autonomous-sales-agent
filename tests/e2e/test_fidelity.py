# test_fidelity.py — Playwright smoke test for the /improve/fidelity page.
# Asserts: (1) the page and trigger button render, (2) the idle/empty state shows
# correctly even when the backend API is not mounted (404 or network-unreachable).
# Uses base http://localhost:3001 (Next.js dev server must be running).
# Run: python3 tests/e2e/test_fidelity.py
from __future__ import annotations

import sys
import traceback


BASE = "http://localhost:3001"
PAGE_URL = f"{BASE}/improve/fidelity"


def run() -> bool:
    # Lazy import: playwright lives only in the /usr/bin/python3 e2e env, NOT .venv. Importing it at
    # module level breaks `.venv` pytest collection (the suite's default runner). Repo convention: these
    # browser scripts import playwright inside the function and run via `python3 tests/e2e/<file>.py`.
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout  # noqa: F401

    passed: list[str] = []
    failed: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Silence API errors — the backend may not be running; the page should not crash.
        page.on("console", lambda msg: None)

        try:
            page.goto(PAGE_URL, wait_until="networkidle", timeout=30_000)
        except PlaywrightTimeout:
            # If we can't even load the page, fail fast.
            print("FAIL: page timed out loading — is `next dev` running on :3001?")
            browser.close()
            return False
        except Exception as exc:
            print(f"FAIL: page navigation error — {exc}")
            browser.close()
            return False

        # --- 1. Trigger button is visible --------------------------------------------------------
        try:
            btn = page.locator("button", has_text="Build replay experiments")
            btn.wait_for(state="visible", timeout=10_000)
            passed.append("trigger button visible")
        except PlaywrightTimeout:
            failed.append("trigger button NOT found within 10s")

        # --- 2. Panel renders (the page title area shows 'Fidelity' in the nav or heading) ------
        try:
            # Either the top-bar title or the nav item label reads "Fidelity"
            page.locator("text=Fidelity").first.wait_for(state="visible", timeout=5_000)
            passed.append("'Fidelity' label visible")
        except PlaywrightTimeout:
            failed.append("'Fidelity' label NOT found")

        # --- 3. Empty / idle state shows (not a crash) ----------------------------------------
        # The explainer sentence must be present.
        try:
            page.locator("text=divergence 0 = the replay behaved identically").wait_for(
                state="visible", timeout=5_000
            )
            passed.append("explainer text visible")
        except PlaywrightTimeout:
            failed.append("explainer text NOT found")

        # --- 4. Empty state card visible (no results yet) -------------------------------------
        try:
            page.locator("text=No fidelity results yet").wait_for(state="visible", timeout=5_000)
            passed.append("empty state card visible")
        except PlaywrightTimeout:
            # The empty state shows only when status != running and no results — acceptable if the
            # backend returned data (unlikely in CI without the backend). Check for either.
            aggregate_visible = page.locator("text=Avg divergence").is_visible()
            if aggregate_visible:
                passed.append("aggregate card visible (backend returned data — empty state not shown)")
            else:
                failed.append("empty state card NOT found (and no aggregate card either)")

        # --- 5. No uncaught JS errors / crash rendering -----------------------------------------
        # The page URL should remain /improve/fidelity (no redirect to error page).
        current_url = page.url
        if "/improve/fidelity" in current_url:
            passed.append("page stayed on /improve/fidelity (no crash redirect)")
        else:
            failed.append(f"page redirected away from fidelity to {current_url}")

        browser.close()

    # --- report ---------------------------------------------------------------------------------
    print("\n=== Fidelity page smoke test ===")
    for p in passed:
        print(f"  PASS  {p}")
    for f in failed:
        print(f"  FAIL  {f}")
    print(f"\nResult: {len(passed)} passed, {len(failed)} failed")
    return len(failed) == 0


if __name__ == "__main__":
    ok = False
    try:
        ok = run()
    except Exception:
        traceback.print_exc()
    sys.exit(0 if ok else 1)
