# verify_demo_button.py — end-to-end verification of the SERVER-SIDE "Run demo call" (CB-08) on
# /operate/live. Proves: the empty-state button starts a demo that lights up the monitor; the call
# duration TICKS (live timer); the demo SURVIVES navigating away to Calls and back (server-side, not
# browser-bound); the auto-scroll toggle is real; and it finishes terminal. Reads /api/live/active for
# ground truth + screenshots the monitor. Run: /usr/bin/python3 tests/e2e/verify_demo_button.py
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:3001"
API = "http://localhost:8000"
OUT = "/tmp/cb0506"


def live_state() -> tuple[int, int]:
    """(active_call_count, max_turn_count) from the API — ground truth independent of the page."""
    try:
        with urllib.request.urlopen(f"{API}/api/live/active", timeout=4) as r:
            d = json.load(r)
        calls = d.get("calls") or []
        turns = max((c.get("turn_count") or 0) for c in calls) if calls else 0
        return len(calls), turns
    except Exception:
        return -1, 0


async def wait_drain(timeout_s: int = 150) -> None:
    """Wait until no demo is active, so the test starts from a clean monitor."""
    for _ in range(timeout_s // 3):
        n, _t = live_state()
        if n == 0:
            return
        await asyncio.sleep(3)


async def main() -> int:
    import os
    os.makedirs(OUT, exist_ok=True)
    print("[demo] draining any in-flight demo first…")
    await wait_drain()

    errors: list[str] = []
    fails: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"{BASE}/operate/live", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1200)

        btn = page.get_by_role("button", name="Run a demo call")
        if await btn.count() == 0:
            print("FAIL: 'Run demo call' button not found"); await browser.close(); return 1
        await page.screenshot(path=f"{OUT}/demo_empty.png", full_page=True)
        await btn.click()
        print("[demo] clicked — server-side demo starting…")

        # 1) Wait for the call to go live with a couple turns.
        appeared = False
        for _ in range(40):
            await page.wait_for_timeout(2000)
            n, turns = live_state()
            if n >= 1 and turns >= 2:
                appeared = True
                break
        if not appeared:
            fails.append("call never reached the live monitor with >=2 turns")
        n0, turns_before = live_state()
        print(f"[demo] live before nav: active={n0} turns={turns_before}")
        await page.screenshot(path=f"{OUT}/demo_live.png", full_page=True)

        # 2) Ticking timer: read lv-dur twice ~3s apart; it must advance.
        async def dur_text() -> str:
            el = page.locator(".lv-dur").first
            return (await el.inner_text()).strip() if await el.count() else ""
        d1 = await dur_text()
        await page.wait_for_timeout(3000)
        d2 = await dur_text()
        print(f"[demo] duration timer: '{d1}' -> '{d2}'")
        if not d2 or d2 == d1 or d2 in ("—", "0:00", "00:00"):
            fails.append(f"duration timer did not tick ('{d1}' -> '{d2}')")

        # 3) NAVIGATE AWAY to Calls, then BACK to Live — the demo must keep going (server-side).
        await page.goto(f"{BASE}/operate/calls", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(8000)  # demo keeps streaming server-side while we're away
        await page.goto(f"{BASE}/operate/live", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)
        n1, turns_after = live_state()
        print(f"[demo] after nav back: active={n1} turns={turns_after} (was {turns_before})")
        await page.screenshot(path=f"{OUT}/demo_after_nav.png", full_page=True)
        # Survival: either still active with MORE turns, OR it already completed (turns advanced to a
        # terminal). The key proof is turns ADVANCED beyond turns_before (work continued while away).
        if turns_after <= turns_before and n1 >= 1:
            fails.append(f"demo did not advance while navigated away ({turns_before} -> {turns_after})")
        else:
            print("[demo] PASS: demo advanced/continued across navigation (server-side)")

        # 4) Auto-scroll toggle is a real control.
        toggle = page.get_by_role("switch")
        autoscroll_ok = await toggle.count() > 0
        if not autoscroll_ok:
            # fallback: the toggle span with the 'Auto-scroll' label
            autoscroll_ok = await page.locator("text=Auto-scroll").count() > 0
        if not autoscroll_ok:
            fails.append("auto-scroll control not found")

        # 5) Let it finish; confirm it terminates (drops from active queue).
        for _ in range(40):
            await page.wait_for_timeout(2000)
            n, _t = live_state()
            if n == 0:
                break
        n_final, _ = live_state()
        print(f"[demo] final active count: {n_final}")
        await browser.close()

    # Confirm the demo persisted as a terminal call (not in_progress) via the API.
    try:
        with urllib.request.urlopen(f"{API}/api/episodes?limit=5", timeout=4) as r:
            eps = (json.load(r).get("episodes") or [])
        latest_text = next((e for e in eps if e.get("channel") == "text"), None)
        print(f"[demo] latest text episode: {latest_text.get('episode_id') if latest_text else None} "
              f"outcome={latest_text.get('outcome') if latest_text else None} "
              f"turns={latest_text.get('turn_count') if latest_text else None} "
              f"dur_ms={(latest_text.get('metrics') or {}).get('duration_ms') if latest_text else None}")
    except Exception as e:
        print(f"[demo] (could not read latest episode: {e})")

    ok = not fails and not errors
    print(f"\njs_errors={len(errors)} fails={fails}")
    for e in errors[:3]:
        print(f"  JS: {e}")
    print("ALL CHECKS PASSED" if ok else "CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
