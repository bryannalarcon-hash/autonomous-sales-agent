# verify_demo_button.py — end-to-end verification of the "Run demo call" button on /operate/live.
# Loads the live page (empty state), screenshots the button, clicks it, then polls the live API +
# the rendered page while the scripted demo call streams through the real consent/chat path, and
# screenshots the monitor mid-call. Proves the button lights up the live monitor with no phone.
# Run: /usr/bin/python3 tests/e2e/verify_demo_button.py
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:3001"
API = "http://localhost:8000"
OUT = "/tmp/cb0506"


def active_count() -> tuple[int, int]:
    try:
        with urllib.request.urlopen(f"{API}/api/live/active", timeout=4) as r:
            d = json.load(r)
        calls = d.get("calls") or []
        turns = max((c.get("turn_count") or 0) for c in calls) if calls else 0
        return len(calls), turns
    except Exception:
        return -1, 0


async def main() -> int:
    import os
    os.makedirs(OUT, exist_ok=True)
    errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"{BASE}/operate/live", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)

        btn = page.get_by_role("button", name="Run a demo call")
        present = await btn.count() > 0
        print(f"[demo] empty-state button present: {present}")
        await page.screenshot(path=f"{OUT}/demo_empty.png", full_page=True)
        if not present:
            print("FAIL: 'Run demo call' button not found on the empty state")
            await browser.close()
            return 1

        await btn.click()
        print("[demo] clicked — streaming a scripted call (real brain, ~6s/turn)…")

        # Poll until the call appears in the live monitor (queue + turns), up to ~80s.
        appeared = False
        peak_turns = 0
        shot_taken = False
        for i in range(40):  # 40 * 2s = 80s
            await page.wait_for_timeout(2000)
            n, turns = active_count()
            peak_turns = max(peak_turns, turns)
            if n >= 1 and not appeared:
                appeared = True
                print(f"[demo] call is LIVE in the monitor (active={n}) at ~{(i+1)*2}s")
            # Once we have a couple of turns, grab the mid-call monitor screenshot.
            if turns >= 2 and not shot_taken:
                await page.wait_for_timeout(800)
                await page.screenshot(path=f"{OUT}/demo_live.png", full_page=True)
                shot_taken = True
                print(f"[demo] mid-call screenshot at turns={turns}")
            # Stop polling once the demo finished (status text) and the call left the active queue.
            body = (await page.inner_text("body")).lower()
            if "demo call complete" in body:
                print(f"[demo] demo reported complete (peak turns={peak_turns})")
                break

        if not shot_taken:
            await page.screenshot(path=f"{OUT}/demo_live.png", full_page=True)
        await browser.close()

    ok = appeared and peak_turns >= 2 and not errors
    print(f"\nappeared={appeared} peak_turns={peak_turns} js_errors={len(errors)}")
    for e in errors[:3]:
        print(f"  JS: {e}")
    print("ALL CHECKS PASSED" if ok else "CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
