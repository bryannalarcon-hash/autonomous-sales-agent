# qa7_cb57.py — CB-57 regression: the RunDrawer quotes the effective (floored/capped) n, not the
# raw requested value. Navigates to /improve/lab, opens "Run experiment", sets n=1, and asserts
# the cost note shows the floored value (>= 5 per arm, the minimum sample). Does NOT submit
# (no paid run launched). Screenshots saved to /tmp/qa7/. Run with:
#   /usr/bin/python3 tests/e2e/qa7_cb57.py
import os
import sys
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:3000"
SCREENSHOT_DIR = "/tmp/qa7"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# Backend constants — must match _MIN_RUN_N / _MAX_RUN_N in src/api/improve.py
N_MIN = 5
N_MAX = 24


def shot(page, name: str) -> None:
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    page.screenshot(path=path)
    print(f"  [screenshot] {path}")


def main() -> int:
    failures: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(15_000)

        # ---- Navigate to the Lab page ----
        print("[1] Navigate to /improve/lab")
        page.goto(f"{BASE}/improve/lab")
        page.wait_for_load_state("networkidle")
        shot(page, "cb57_01_lab_loaded")

        # ---- Open the Run Experiment drawer ----
        print("[2] Open RunDrawer via 'Run experiment' button")
        run_btn = page.locator("button", has_text="Run experiment").first
        run_btn.click()
        page.wait_for_timeout(800)  # let the drawer animate in
        shot(page, "cb57_02_drawer_open")

        # ---- Set n=1 in the "Calls per arm" input ----
        print("[3] Set n=1 in the calls-per-arm input")
        n_input = page.locator("input[type='number'][min='1'][max='24']").first
        n_input.fill("1")
        n_input.dispatch_event("change")
        page.wait_for_timeout(300)
        shot(page, "cb57_03_n1_entered")

        # ---- Assert the cost note shows the floored value, not n=1 ----
        print("[4] Assert cost note reflects floored n")

        # The cost note is the accent-soft box containing "real model calls".
        cost_area = page.locator("text=real model calls").first
        cost_text = cost_area.text_content() or ""
        print(f"    Cost note text: {cost_text!r}")

        # For n=1, the backend floors to N_MIN=5 per arm -> 10 total calls.
        expected_calls_total = N_MIN * 2  # 10
        if str(expected_calls_total) not in cost_text:
            failures.append(
                f"[FAIL] cost note shows raw n=1 (or wrong floor): expected {expected_calls_total}"
                f" total calls, got text: {cost_text!r}"
            )
        else:
            print(f"    [PASS] cost note shows {expected_calls_total} total calls (floor applied)")

        # The note must NOT show "2 real model calls" (raw n=1 × 2).
        if "2 real model calls" in cost_text:
            failures.append(
                f"[FAIL] cost note says '2 real model calls' — quoted raw n instead of floored n"
            )
        else:
            print("    [PASS] cost note does not quote the raw n=1 value")

        # Optionally: a floor note ("below the minimum") should appear.
        floor_note = page.locator("text=minimum sample").or_(page.locator("text=minimum")).first
        if floor_note.count() > 0:
            floor_text = floor_note.text_content() or ""
            print(f"    Floor note found: {floor_text!r}")
        else:
            print("    (no explicit floor note found — optional)")

        shot(page, "cb57_04_cost_note_checked")

        # ---- Assert submit button also shows the floored call count ----
        print("[5] Assert submit button shows floored call count")
        submit_btn = page.locator("button", has_text="calls").last
        submit_text = submit_btn.text_content() or ""
        print(f"    Submit button text: {submit_text!r}")
        if str(expected_calls_total) not in submit_text:
            failures.append(
                f"[FAIL] submit button shows raw call count instead of floored: {submit_text!r}"
            )
        else:
            print(f"    [PASS] submit button shows {expected_calls_total} calls")

        # DO NOT click submit — no paid run launched (CB instructions).
        print("[6] Close drawer WITHOUT submitting (no paid run)")
        close_btn = page.locator(".drawer .gctl").first
        close_btn.click()
        page.wait_for_timeout(300)
        shot(page, "cb57_05_drawer_closed")

        browser.close()

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)} failure(s).")
        return 1
    print("All CB-57 pre-launch dialog checks PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
