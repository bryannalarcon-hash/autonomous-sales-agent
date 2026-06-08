# qa7_streaming_stress.py — orchestrator stress test for CB-63's streamed demo chat.
# Polls body innerText length: echo must render instantly; a real token stream produces
# MANY distinct growth increments (buffered pop-in = 1-2). Also stresses mid-stream abandon
# -> fresh-session recovery (the CB-54 edge). ~5 paid turns.
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
fails = []
notes = []


def walk_consent(pg):
    pg.wait_for_selector("input[type=checkbox]", timeout=15000)
    for box in pg.query_selector_all("input[type=checkbox]"):
        box.check()
    pg.click("button:has-text('Allow recording')", timeout=5000)
    pg.wait_for_selector("button:has-text('Continue'):enabled", timeout=5000)
    pg.click("button:has-text('Continue')")
    try:
        pg.click("button:has-text('I am the parent/guardian')", timeout=4000)
    except Exception:
        pass


def streamed_turn(pg, text, tag, watch_s=45):
    """Send a message; poll body innerText length every 120ms. Signals:
    - echo_at: first growth (user bubble renders — must be near-instant)
    - reply growth INCREMENTS after the echo: buffered = 1-2 big jumps;
      a real token stream = many (>4) distinct increases."""
    base = len(pg.inner_text("body"))
    pg.fill("input[type=text]", text)
    t0 = time.time()
    pg.click("button:has-text('Send')")
    echo_at = None
    increases = []  # (t_seconds, new_len)
    last = base
    deadline = t0 + watch_s
    while time.time() < deadline:
        n = len(pg.inner_text("body"))
        if n > last:
            now = time.time() - t0
            if echo_at is None:
                echo_at = now
            else:
                increases.append((now, n))
            last = n
        elif increases and (time.time() - t0) > increases[-1][0] + 5:
            break  # no growth for 5s after streaming began -> turn done
        time.sleep(0.12)
    first_token_at = increases[0][0] if increases else None
    growth = len(increases)
    notes.append(
        f"{tag}: echo {echo_at and f'{echo_at:.1f}s'}, first-token {first_token_at and f'{first_token_at:.1f}s'}, "
        f"stream-increments {growth}"
    )
    if echo_at is None or echo_at > 1.5:
        fails.append(f"{tag}: user echo not instant (echo_at={echo_at})")
    if first_token_at is None:
        fails.append(f"{tag}: no agent reply growth observed in {watch_s}s")
    elif growth <= 4:
        fails.append(f"{tag}: only {growth} stream increments — reply effectively popped in whole")
    body = pg.inner_text("body")
    if "Consent required" in body or "no longer valid" in body:
        fails.append(f"{tag}: consent wall appeared")
    return first_token_at


with sync_playwright() as p:
    browser = p.chromium.launch()

    # --- Session 1: two streamed turns back-to-back ---
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(f"{BASE}/demo", wait_until="networkidle")
    pg.wait_for_timeout(2000)
    walk_consent(pg)
    pg.wait_for_selector("input[type=text]", timeout=15000)
    streamed_turn(pg, "Hi, my son is in 8th grade and struggling with algebra. How does this work?", "turn 1")
    pg.wait_for_selector("input[type=text]:enabled", timeout=20000)  # input must re-enable
    streamed_turn(pg, "What does it roughly cost per month? Money is tight for us right now.", "turn 2 (price+budget)")
    pg.screenshot(path="/tmp/qa7/stream_session1.png", full_page=True)

    # --- Mid-stream abandon: send, kill the page ~1.5s in ---
    pg.wait_for_selector("input[type=text]:enabled", timeout=20000)
    pg.fill("input[type=text]", "And do you offer anything for test prep specifically?")
    pg.click("button:has-text('Send')")
    pg.wait_for_timeout(1500)
    ctx.close()  # abandon mid-stream
    notes.append("abandon: context closed ~1.5s into a streamed reply")

    # --- Session 2 (fresh): must work after the abandoned stream ---
    ctx2 = browser.new_context()
    pg2 = ctx2.new_page()
    pg2.goto(f"{BASE}/demo", wait_until="networkidle")
    pg2.wait_for_timeout(2000)
    walk_consent(pg2)
    pg2.wait_for_selector("input[type=text]", timeout=15000)
    streamed_turn(pg2, "Hello, I'm looking for chemistry tutoring for my daughter.", "post-abandon fresh session")
    pg2.screenshot(path="/tmp/qa7/stream_session2.png", full_page=True)
    ctx2.close()
    browser.close()

print("=" * 60)
for n in notes:
    print(" ", n)
print("=" * 60)
if fails:
    print("RESULT: FAIL —", " | ".join(fails))
    sys.exit(1)
print("RESULT: PASS — instant echo, progressive token stream, abandon-recovery clean")
