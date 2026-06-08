# qa7_cb61_replay.py — paid behavior replay of the QA6 "Karen Mitchell" conversation that exposed
# CB-61/62/64. Re-runs the same 9 caller messages through the live streaming demo and dumps the
# full transcript to /tmp/qa7/cb61_replay_transcript.txt for orchestrator grading on: re-asks of
# volunteered facts (deadline/day), confirm-echo of the callback time, price ballpark + budget ack,
# and near-verbatim pitch repetition. ~9 paid turns (~$0.15).
import time

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"

MESSAGES = [
    "Hi, I saw your site while looking for tutoring help. My daughter is in 10th grade and she's really struggling in chemistry. Can you tell me how this works?",
    "She mostly gets lost with stoichiometry and balancing equations. Her grade dropped from a B to a D this semester and she has a big test in about three weeks. She gets discouraged easily, honestly.",
    "Like I said, she has a big test in three weeks, so that's the priority - getting her ready for that and pulling the grade back up. What would tutoring for that look like?",
    "Before I commit to anything - how much does this cost? We're on a pretty tight budget right now.",
    "Hold on - can you at least give me a ballpark number per hour or per month? I don't want to book a call only to find out it's way outside our budget.",
    "Okay, fine. Thursday afternoon after 3pm works for me. What do you need from me to set that up?",
    "I just told you - Thursday, after 3pm. Do you need my phone number or email or something?",
    "I'm Karen Mitchell, my number is 555-201-4477. Phone is better for me.",
    "Just make sure they know the test is in three weeks, and please confirm they'll call Thursday after 3pm like I asked. Thanks.",
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


with sync_playwright() as p:
    browser = p.chromium.launch()
    pg = browser.new_context().new_page()
    pg.goto(f"{BASE}/demo", wait_until="networkidle")
    pg.wait_for_timeout(2000)
    walk_consent(pg)
    pg.wait_for_selector("input[type=text]", timeout=15000)

    for i, msg in enumerate(MESSAGES, 1):
        # The conversation may legitimately TERMINATE early (close accepted / escalate) — the input
        # then stays disabled. Detect that and stop gracefully instead of dying on a timeout.
        try:
            pg.wait_for_selector("input[type=text]:enabled", timeout=45000)
        except Exception:
            print(f"  conversation ended before turn {i} could be sent (input stayed disabled)")
            break
        base = len(pg.inner_text("body"))
        pg.fill("input[type=text]", msg)
        pg.click("button:has-text('Send')")
        # wait for the reply stream to finish: growth then 6s of stability
        last, last_growth_t = base, time.time()
        deadline = time.time() + 60
        while time.time() < deadline:
            n = len(pg.inner_text("body"))
            if n > last:
                last, last_growth_t = n, time.time()
            elif n > base and time.time() - last_growth_t > 6:
                break
            time.sleep(0.2)
        print(f"  turn {i} done ({last - base} chars rendered)")

    transcript = pg.inner_text("body")
    with open("/tmp/qa7/cb61_replay_transcript.txt", "w") as f:
        f.write(transcript)
    pg.screenshot(path="/tmp/qa7/cb61_replay.png", full_page=True)
    browser.close()
    print("TRANSCRIPT SAVED: /tmp/qa7/cb61_replay_transcript.txt")
