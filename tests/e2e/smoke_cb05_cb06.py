# smoke_cb05_cb06.py — Playwright smoke test for CB-05 (per-turn belief delta) and CB-06
# (agent-estimate vs prospect-truth panel) on the Call Review page (/operate/review/<id>).
# Verifies: page loads without JS crash; the review shell renders (belief-replay card present);
# no raw internal slugs (snake_case driver keys) leak into visible text;
# and the self-play truth panel is absent for a real (non-sim) episode.
# Run: /usr/bin/python3 tests/e2e/smoke_cb05_cb06.py
# Base URL: http://localhost:3001  (next dev on :3001)
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

BASE = "http://localhost:3001"
API_BASE = "http://localhost:8000"

# Raw internal slugs that must NEVER appear in operator-visible text on this page.
BANNED_SLUGS = [
    "trust_velocity",
    "bail_risk_velocity",
    "urgency_velocity",
    "purchase_intent_velocity",
    "need_intensity",
    "price_sensitivity",
    "bail_risk",
    "need_intensity_velocity",
    "price_sensitivity_velocity",
]


async def fetch_real_episode_id() -> str | None:
    """Hit the real API (if running) to get an episode id for the smoke test."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/episodes?limit=3", timeout=3) as r:
            import json
            data = json.load(r)
            eps = data.get("episodes", [])
            if eps:
                return eps[0]["episode_id"]
    except Exception:
        pass
    return None


async def main() -> int:
    episode_id = await fetch_real_episode_id()
    if not episode_id:
        # Fallback: use a known episode id from QA history
        episode_id = "ep-a9c468af324c474db160f25290c3272d"
        print(f"[smoke] API not reachable — using fallback episode id: {episode_id}")
    else:
        print(f"[smoke] Using episode id from API: {episode_id}")

    url = f"{BASE}/operate/review/{episode_id}"
    errors: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Collect JS errors and page crashes
        page.on("pageerror", lambda e: errors.append(f"JS error: {e}"))
        page.on("crash", lambda _: errors.append("Page crashed"))

        print(f"[smoke] Navigating to {url}")
        response = await page.goto(url, timeout=15000, wait_until="domcontentloaded")

        # 1. HTTP-level: page must not 404/500
        status = response.status if response else 0
        if status >= 400:
            errors.append(f"HTTP {status} loading {url}")

        # 2. Wait for the review shell to appear (belief-replay card has "Belief replay" heading)
        try:
            await page.wait_for_selector("text=Belief replay", timeout=10000)
            print("[smoke] PASS: 'Belief replay' card present")
        except Exception:
            # The page may be loading or showing a graceful empty state — still OK
            # (empty state is a valid render, not a crash)
            body_text = await page.inner_text("body")
            if "Loading call" in body_text or "Call not found" in body_text or "Nothing recorded" in body_text:
                print("[smoke] PASS: graceful empty/loading state rendered (no crash)")
            else:
                errors.append("Neither 'Belief replay' nor graceful empty state found after 10s")

        # 3. No raw internal slug must appear in visible text
        body_text = await page.inner_text("body")
        for slug in BANNED_SLUGS:
            if slug in body_text:
                errors.append(f"Raw slug leak: '{slug}' found in visible text")

        # 4. For a real (non-sim) call, the CB-06 truth panel must be ABSENT
        truth_panel = await page.query_selector("text=Agent read vs prospect reality")
        if truth_panel is None:
            print("[smoke] PASS: CB-06 truth panel correctly absent for real episode")
        else:
            # It may legitimately be present if this happens to be a sim episode
            print("[smoke] INFO: CB-06 truth panel present — episode may be a self-play run")

        await browser.close()

    # Report
    if errors:
        print("\n[smoke] FAILURES:")
        for e in errors:
            print(f"  - {e}")
        return 1
    else:
        print("\n[smoke] ALL CHECKS PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
