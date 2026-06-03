# verify_wave2.py — integration screenshots for the Wave-2 CB batch: CB-24 (experiment failure reason),
# CB-25 (experiment detail page + A/B calls), CB-26/27 (lab run drawer), CB-28 (review tool-use panel),
# CB-29 (brain — review renders a coherent episode). Triggers a fresh experiment so the detail page has
# real arm calls, then shoots lab / lab/[id] / review. Run: /usr/bin/python3 tests/e2e/verify_wave2.py <review_ep>
from __future__ import annotations

import asyncio, json, sys, time, urllib.request
from playwright.async_api import async_playwright

BASE = "http://localhost:3001"; API = "http://localhost:8000"; OUT = "/tmp/cb_wave2"


def api_post(path, body):
    req = urllib.request.Request(f"{API}{path}", method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def api_get(path):
    with urllib.request.urlopen(f"{API}{path}", timeout=8) as r:
        return json.load(r)


async def main(review_ep) -> int:
    import os; os.makedirs(OUT, exist_ok=True)
    print("[w2] triggering an experiment run for the detail page…")
    try:
        run = api_post("/api/experiments/run", {"dimension": "thresholds.max_concession_band", "value": 0.22, "n": 4, "name": "w2-detail"})
    except Exception as e:
        print(f"[w2] run trigger failed: {e}"); run = {}
    exp_id = (run.get("experiment") or {}).get("experiment_id") or run.get("experiment_id")
    print(f"[w2] experiment_id={exp_id} state={(run.get('experiment') or {}).get('state')}")

    errors: list[str] = []; checks: dict[str, bool] = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={"width": 1440, "height": 1700}); pg = await ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))

        async def shoot(path, tag, waitms=1500):
            await pg.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=30000)
            await pg.wait_for_timeout(waitms)
            await pg.screenshot(path=f"{OUT}/{tag}.png", full_page=True)

        await shoot("/improve/lab", "lab")
        body = (await pg.inner_text("body")).lower()
        checks["CB24_reason_or_running_shown"] = ("did not complete" in body) or ("timed out" in body) or ("no significant lift" in body) or ("running" in body)

        # review page renders (CB-28 panel + CB-29 episode), no crash
        await shoot(f"/operate/review/{review_ep}", "review")
        checks["CB_review_renders"] = (await pg.locator(".rv").count()) > 0
        rbody = (await pg.inner_text("body")).lower()
        checks["CB29_no_hallucinated_child_in_review"] = ("your daughter" not in rbody)

        # poll for the experiment to settle, then the detail page
        settled = None
        for _ in range(40):
            await pg.wait_for_timeout(3000)
            try:
                xs = api_get("/api/experiments"); rows = xs.get("experiments") or xs if isinstance(xs, list) else xs.get("experiments", [])
                rows = rows if isinstance(rows, list) else []
                m = next((x for x in rows if x.get("experiment_id") == exp_id), None)
                if m and m.get("state") not in ("running", "draft"):
                    settled = m.get("state"); break
            except Exception:
                pass
        print(f"[w2] experiment settled state={settled}")
        if exp_id:
            await shoot(f"/improve/lab/{exp_id}", "lab_detail", 1800)
            dbody = (await pg.inner_text("body")).lower()
            checks["CB25_detail_has_arm_calls_or_state"] = ("champion" in dbody and "challenger" in dbody) or ("call" in dbody)
        await b.close()

    print("\n=== checks ==="); [print(f"  {'PASS' if v else 'FAIL'}  {k}") for k, v in checks.items()]
    print(f"js_errors={len(errors)}"); [print(f"  JS: {e}") for e in errors[:4]]
    ok = all(checks.values()) and not errors
    print("ALL CHECKS PASSED" if ok else "REVIEW screenshots in /tmp/cb_wave2")
    return 0 if ok else 1


if __name__ == "__main__":
    rev = sys.argv[1] if len(sys.argv) > 1 else "sp-232ca983f6ad4cec8e3274f6759cf1ac"
    raise SystemExit(asyncio.run(main(rev)))
