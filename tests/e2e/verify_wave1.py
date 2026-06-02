# verify_wave1.py — integration screenshot-verification for the Wave-1 CB batch (CB-11/14/16/17 shell,
# CB-12/13 demo, CB-15/19/20/02 experiments). Triggers a server-side demo, then shoots the live
# monitor (generated prospect streaming), the version lineage (CB-02 dimension), and the lab (CB-15
# running cards), with structural checks: the Operate/Improve mode toggle is GONE (CB-11), the
# "Talk to the agent" demo link is present (CB-16), no JS errors. Run: /usr/bin/python3 tests/e2e/verify_wave1.py
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from playwright.async_api import async_playwright

BASE = "http://localhost:3001"
API = "http://localhost:8000"
OUT = "/tmp/cb_wave1"


def post(path):
    try:
        req = urllib.request.Request(f"{API}{path}", method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)
    except Exception as e:
        return {"err": str(e)}


async def shoot(page, path, tag, errors):
    await page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1200)
    await page.screenshot(path=f"{OUT}/{tag}.png", full_page=True)


async def main() -> int:
    import os
    os.makedirs(OUT, exist_ok=True)
    print("[wave1] starting a server-side demo for the live screenshot…")
    started = post("/api/demo/auto/start")
    print(f"[wave1] auto/start -> {started.get('episode_id', started)}")

    errors: list[str] = []
    checks: dict[str, bool] = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={"width": 1440, "height": 1600})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))

        # --- Dashboard shell checks on /improve/versions (any dash page has the shell) ---
        await page.goto(f"{BASE}/improve/versions", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1200)
        checks["CB11_mode_toggle_gone"] = (await page.locator(".mode").count()) == 0
        checks["CB16_talk_link_present"] = (await page.locator('a[href="/demo"]').count()) > 0
        body = (await page.inner_text("body"))
        checks["CB02_no_CHANGE_dash_placeholder"] = "CHANGE —" not in body
        checks["CB02_dimension_rendered"] = ("Discovery sequencing" in body) or ("sequenc" in body.lower())
        await page.screenshot(path=f"{OUT}/versions.png", full_page=True)

        # --- Lab (CB-15 running cards, no freeze) ---
        await shoot(page, "/improve/lab", "lab", errors)

        # --- Live monitor: let the demo stream a few turns, then shoot ---
        for _ in range(8):
            await page.wait_for_timeout(2500)
            try:
                with urllib.request.urlopen(f"{API}/api/live/active", timeout=4) as r:
                    d = json.load(r)
                calls = d.get("calls") or []
                if calls and max((c.get("turn_count") or 0) for c in calls) >= 2:
                    break
            except Exception:
                pass
        await shoot(page, "/operate/live", "live", errors)

        await b.close()

    print("\n=== checks ===")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print(f"js_errors={len(errors)}")
    for e in errors[:4]:
        print(f"  JS: {e}")
    ok = all(checks.values()) and not errors
    print("ALL CHECKS PASSED" if ok else "CHECKS FAILED (review screenshots in /tmp/cb_wave1)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
