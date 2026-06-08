# qa7_round4_confirm.py — paid live confirmation of the round-4 brain/persistence fixes against the
# real streaming demo. Two scripted conversations exercising the EXACT round-3 failure utterances:
#  Conv A (skeptical dad): guarantee "or it's free" (CB-83 — must NOT be called "Free"), terse price
#    x3 (CB-77/85 — must get a range, never "I don't have access"), storm non-sequitur (CB-85 — brief
#    ack + redirect, never "sounds good let me schedule").
#  Conv B (mom): books a callback + gives a phone (CB-82 — episode must persist to /operate w/ lead).
# Dumps both transcripts to /tmp/qa7/round4_*.txt for orchestrator grading. ~15 paid turns (~$0.25).
import time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"

CONV_A = [
    "My son is in 11th grade and failing physics. What do I get for the money?",
    "How much per month? Just a number.",
    "you guys see the storm coming in tonight?",
    "Anyway — price. Just the number.",
    "Guarantee me a B or it's free. Yes or no.",
    "Third time: dollars per month, one number.",
]
CONV_B = [
    "Hi, my daughter is in 6th grade and needs help with reading comprehension.",
    "Wednesdays or Fridays after 4 work best, and state testing is end of the month.",
    "That sounds good — let's set up a callback.",
    "I'm Dana Reyes, 555-014-9921. Phone is best.",
]


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


def run_conv(pg, msgs, tag):
    walk_consent(pg)
    pg.wait_for_selector("input[type=text]", timeout=15000)
    for i, m in enumerate(msgs, 1):
        try:
            pg.wait_for_selector("input[type=text]:enabled", timeout=45000)
        except Exception:
            print(f"  {tag}: ended before turn {i}")
            break
        base = len(pg.inner_text("body"))
        pg.fill("input[type=text]", m)
        pg.click("button:has-text('Send')")
        last, t = base, time.time()
        while time.time() < t + 55:
            n = len(pg.inner_text("body"))
            if n > last:
                last, t = n, time.time()
            elif n > base and time.time() - t > 6:
                break
            time.sleep(0.2)
        print(f"  {tag} turn {i} done")
    txt = pg.inner_text("body")
    with open(f"/tmp/qa7/round4_{tag}.txt", "w") as f:
        f.write(txt)


with sync_playwright() as p:
    b = p.chromium.launch()
    a = b.new_context().new_page()
    a.goto(f"{BASE}/demo", wait_until="networkidle"); a.wait_for_timeout(2000)
    run_conv(a, CONV_A, "dad")
    a.context.close()
    m = b.new_context().new_page()
    m.goto(f"{BASE}/demo", wait_until="networkidle"); m.wait_for_timeout(2000)
    run_conv(m, CONV_B, "mom")
    m.context.close()
    b.close()
print("TRANSCRIPTS: /tmp/qa7/round4_dad.txt /tmp/qa7/round4_mom.txt")
