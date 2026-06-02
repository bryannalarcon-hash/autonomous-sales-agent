# verify_cb05_cb06_screens.py — screenshot-verification gate for CB-05 (per-turn belief delta) +
# CB-06 (agent-estimate vs prospect-truth panel) on the Call Review page (/operate/review/<id>).
# Captures full-page PNGs for a SIM episode (panel + deltas MUST render) and a VOICE episode (panel
# MUST be absent), checks no raw driver slugs leak, and prints a verdict. PNGs are read back by the
# orchestrator so a human/agent confirms the UI isn't broken. Run: /usr/bin/python3 tests/e2e/verify_cb05_cb06_screens.py <sim_id> <voice_id>
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

BASE = "http://localhost:3001"
OUT_DIR = "/tmp/cb0506"

PANEL_MARKER = "true state"  # substring of the CB-06 explainer line (case-insensitive)
BANNED_SLUGS = [
    "trust_velocity", "bail_risk_velocity", "urgency_velocity", "purchase_intent_velocity",
    "need_intensity", "price_sensitivity", "bail_risk", "prospect_trajectory",
]


async def shoot(page, ep_id: str, tag: str) -> dict:
    url = f"{BASE}/operate/review/{ep_id}"
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(1500)
    png = f"{OUT_DIR}/{tag}_{ep_id[:12]}.png"
    await page.screenshot(path=png, full_page=True)
    body = (await page.inner_text("body")).lower()
    panel_present = PANEL_MARKER in body
    leaked = [s for s in BANNED_SLUGS if s in body]
    return {"tag": tag, "id": ep_id, "png": png, "panel_present": panel_present,
            "leaked": leaked, "js_errors": errors}


async def main(sim_id: str, voice_id: str) -> int:
    import os
    os.makedirs(OUT_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 2400})
        page = await ctx.new_page()
        sim = await shoot(page, sim_id, "sim")
        voice = await shoot(page, voice_id, "voice")
        await browser.close()

    ok = True
    for r in (sim, voice):
        print(f"\n[{r['tag']}] {r['id']}\n  png={r['png']}\n  panel_present={r['panel_present']}"
              f"  leaked_slugs={r['leaked']}  js_errors={len(r['js_errors'])}")
        for e in r["js_errors"][:3]:
            print(f"    JS: {e}")

    if not sim["panel_present"]:
        print("FAIL: sim episode is MISSING the agent-vs-prospect panel"); ok = False
    if voice["panel_present"]:
        print("FAIL: voice episode WRONGLY shows the agent-vs-prospect panel"); ok = False
    if sim["leaked"] or voice["leaked"]:
        print("FAIL: raw driver slugs leaked into visible text"); ok = False
    if sim["js_errors"] or voice["js_errors"]:
        print("FAIL: JS errors on page"); ok = False
    print("\n" + ("ALL CHECKS PASSED" if ok else "CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sim_id = sys.argv[1] if len(sys.argv) > 1 else "sp-a0494cd61f6542eba51fc2d36a9ec2ed"
    voice_id = sys.argv[2] if len(sys.argv) > 2 else "ep-94aabde44273495798cfee392fcc75cd"
    raise SystemExit(asyncio.run(main(sim_id, voice_id)))
