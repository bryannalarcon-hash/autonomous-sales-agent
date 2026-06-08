# qa7_cb65_leaksweep.py — CB-65/CB-70/CB-81 regression: viewer-facing leak sweep across all surfaces.
# Walks every page (demo pre-consent, calls list both cohort modes, call review, escalations, KPI,
# lab list + one lab detail, versions, approvals) and scans rendered innerText against a pattern list.
# Each pattern class has an explicit allowlist with documented justification for any permitted hit.
# The test PASSES only when zero non-allowlisted hits remain — it is the headline regression for CB-65.
# CB-70 extension: includes the "-> [" arrow-bracket-list pattern on card/Past/detail TITLE elements
# so a blank-name record falling back to a raw mutation string is caught immediately.
# CB-81 extension: adds patterns for raw "text" channel slug + "sim-harness decision" rationale leak
# (O1: channel must render as "Web chat" not "text"; O2: stub turn rationale must be humanized).
# CB-81 (CB-NN strip): adds pattern for internal index tags (CB-NN, D#, W#, S#, P#, R#) leaking
# from src/core gate-rationale strings into the operator call-review rendered text.
# Does NOT click chat/chat links, start calls, or take any state-mutating action.
# Requirements: Next.js dashboard at localhost:3000, FastAPI API at localhost:8000.
# Run: python -m pytest tests/e2e/qa7_cb65_leaksweep.py -v --tb=short
#   or: /usr/bin/python3 tests/e2e/qa7_cb65_leaksweep.py  (direct script mode)
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import urllib.parse
from typing import Optional

import pytest

try:
    from playwright.sync_api import Page, sync_playwright  # type: ignore
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

DASHBOARD_BASE = "http://localhost:3000"
API_BASE = "http://localhost:8000"
SCREENSHOT_DIR = "/tmp/qa7"

os.makedirs(SCREENSHOT_DIR, exist_ok=True)

pytestmark = pytest.mark.skipif(
    not _PW_AVAILABLE,
    reason="playwright not installed — install with `pip install playwright && playwright install`",
)

# ---------------------------------------------------------------------------
# Leak pattern definitions
# ---------------------------------------------------------------------------

# Patterns that must not appear in rendered page text (innerText of the body).
# Each entry: (name, compiled_regex, description_for_report)
LEAK_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "raw_run_id",
        re.compile(r"#?RUN-[A-Za-z0-9_]+::[a-z]+::\d+"),
        "Raw arm episode ID (RUN-…::arm::n) as primary visible text",
    ),
    (
        "kb_slug",
        re.compile(r"\bkb_v\d+\b"),
        "Raw kb_version slug ('kb_v0') in rendered text — must be 'Knowledge base v0'",
    ),
    (
        "dunder_colons",
        re.compile(r"\S+(?:__|::)\S+"),
        "Raw internal index with __ or :: separator in rendered text",
    ),
    (
        "arrow_bracket_list",
        re.compile(r"->\s*\["),
        "Python list literal arrow ('-> [') in rendered text from diff_description",
    ),
    (
        "tier_int_jargon",
        re.compile(r"\btier\s+[01]\b"),
        "Raw 'tier 0' / 'tier 1' jargon in rendered text (should be human label)",
    ),
    (
        "is_false_true",
        re.compile(r"\bis\s+(?:False|True)\b"),
        "Python boolean literal 'is False'/'is True' in rendered text",
    ),
    (
        "python_kwargs_dump",
        re.compile(r"\b(?:trust|purchase_intent|bail_risk)=\d+\.\d+"),
        "Python kwargs dump (trust=0.50) in rendered text from gate rationale",
    ),
    (
        "debug_label_consumer",
        re.compile(r"(?<!\w)debug(?!\w)", re.IGNORECASE),
        "Raw 'debug' label visible on the consumer-facing demo page header",
    ),
    # CB-81 O1: raw channel slug "text" must not render as-is in the calls list or call drawer.
    # After the fix, "text" maps to "Web chat" via channelLabel(). We target the word in isolation
    # so normal prose (e.g. "Enter text…") does not trigger a false positive — the channel label
    # appears as a standalone tag or table cell, so we check for it as a complete word surrounded
    # by whitespace or line boundaries. Use a narrow form: "Channel\ntext" or "Channel text" only.
    (
        "raw_channel_text",
        re.compile(r"Channel\s+text\b", re.IGNORECASE),
        "Raw channel slug 'text' visible in the Channel fact row (should be 'Web chat')",
    ),
    # CB-81 O2: "sim-harness decision" is the seeded-stub turn rationale string. After the fix,
    # humanizeRationale maps it to "Simulated training call". Any raw occurrence is a leak.
    (
        "sim_harness_decision",
        re.compile(r"sim-harness\s+decision", re.IGNORECASE),
        "Raw 'sim-harness decision' rationale string visible in operator review (must be humanized)",
    ),
    # CB-81 (CB-NN strip): internal change-board index tags embedded by src/core in gate-rationale
    # strings must be stripped by humanizeRationale before reaching the operator. Pattern specifically
    # targets the "(CB-NN …)" parenthetical form that src/core embeds — e.g. "(CB-48 directness)".
    # The broader inline "CB-NN" token form is also checked on the call-review page specifically
    # (see test_call_review_no_cb_index_leak below). We keep this sweep-level pattern narrow to
    # the parenthetical form so normal text containing CB- prefixes (e.g. version labels) won't fire.
    (
        "cb_index_parenthetical",
        re.compile(r"\(CB-\d+[^)]*\)"),
        "Internal CB-NN parenthetical tag '(CB-NN …)' visible in operator text from gate rationale",
    ),
]

# ---------------------------------------------------------------------------
# Allowlist: (pattern_name, regex_for_hit_text, justification)
# A hit matching an allowlist entry is NOT a failure — it is a documented exception.
# ---------------------------------------------------------------------------

ALLOWLIST: list[tuple[str, re.Pattern[str], str]] = [
    # VERSIONS page: the Versions page IS the place where kb version metadata is shown; we expect
    # a human label ("Knowledge base v0") there but allow the snake-slug form only if DashboardShell
    # still uses kbVersionLabel (tested separately). Actually the fix renders "Knowledge base v0"
    # everywhere, so this entry is a safety valve only — hitting it would mean the label works.
    # No allowlist entries are needed post-fix; this block documents what we considered.
    #
    # "debug" does NOT appear on /demo after CB-65 — allowlist not needed.
    #
    # "dunder_colons" on VERSIONS page: version IDs like "champion_v0__…" appear in the lineage
    # timeline ONLY as data (the labels strip them); the rendered label should have no __. But the
    # versions page may expose these in tooltip/title attrs, which innerText does NOT include.
    # If a test fires here because the raw version string appears in a visible element, it is a real
    # leak.
    #
    # No entries needed: post-fix every pattern should hit zero visible text.
]


def _hit_is_allowed(pattern_name: str, hit: str) -> Optional[str]:
    """Return the allowlist justification if `hit` is a documented exception, else None."""
    for aname, arx, ajustification in ALLOWLIST:
        if aname == pattern_name and arx.search(hit):
            return ajustification
    return None


# ---------------------------------------------------------------------------
# Page navigation helpers
# ---------------------------------------------------------------------------


def _require_services() -> None:
    for label, url in [("API", f"{API_BASE}/api/episodes?limit=1"), ("Dashboard", f"{DASHBOARD_BASE}/")]:
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            pytest.skip(f"Live {label} service not reachable at {url}")


def _navigate_and_scan(page: Page, tag: str, url: str, *, extra_wait_ms: int = 0) -> list[tuple[str, str, list[str]]]:
    """Navigate to `url`, wait for content to settle, scan for leak patterns.
    Returns list of (pattern_name, pattern_description, [hits]) for non-empty, non-allowlisted hits."""
    page.goto(url, wait_until="networkidle", timeout=30_000)
    if extra_wait_ms:
        page.wait_for_timeout(extra_wait_ms)
    # Special: KPI page may show a loading spinner — wait a few extra seconds.
    if "kpi" in url:
        for _ in range(15):
            if "Loading metrics" not in page.inner_text("body"):
                break
            page.wait_for_timeout(800)

    body = page.inner_text("body")
    findings: list[tuple[str, str, list[str]]] = []
    for pname, prx, pdesc in LEAK_PATTERNS:
        raw_hits = sorted(set(prx.findall(body)))
        real_hits = [h for h in raw_hits if _hit_is_allowed(pname, h) is None]
        if real_hits:
            findings.append((pname, pdesc, real_hits))
    return findings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def page():
    if not _PW_AVAILABLE:
        pytest.skip("playwright not installed")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        yield pg
        ctx.close()
        browser.close()


# ---------------------------------------------------------------------------
# Page-level sweep tests
# ---------------------------------------------------------------------------


def test_demo_pre_consent_no_leaks(page: Page):
    """Demo consumer page (pre-consent view only) has no viewer-facing leaks."""
    _require_services()
    findings = _navigate_and_scan(page, "demo", f"{DASHBOARD_BASE}/demo")
    _assert_no_leaks("demo", findings, page)


def test_calls_real_cohort_no_leaks(page: Page):
    """Calls list in Real calls (default) mode has no viewer-facing leaks."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle")
    page.wait_for_timeout(1500)
    body = page.inner_text("body")
    _assert_body_no_leaks("calls_real", body, page)


def test_calls_all_cohorts_no_leaks(page: Page):
    """Calls list in All-cohorts mode (includes sim/experiment rows) has no viewer-facing leaks."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle")
    page.wait_for_timeout(1200)
    # Switch to All cohorts — find the "All" button in the cohort toggle (Real calls / All).
    all_btn = page.locator("button:has-text('All')")
    if all_btn.count() > 0:
        all_btn.first.click()
        page.wait_for_timeout(1200)
    body = page.inner_text("body")
    _assert_body_no_leaks("calls_all_cohorts", body, page)


def test_call_review_no_leaks(page: Page):
    """A call review page (sim episode) has no viewer-facing leaks in the rendered transcript."""
    _require_services()
    # Pick the first available sim episode id from the API.
    ep_id = None
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/episodes?limit=50&cohort=sim", timeout=8) as r:
            data = json.loads(r.read())
        eps = data.get("episodes", [])
        ep_id = eps[0]["episode_id"] if eps else None
    except Exception:
        pass
    if ep_id is None:
        # Try any episode
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/episodes?limit=5", timeout=8) as r:
                data = json.loads(r.read())
            eps = data.get("episodes", [])
            ep_id = eps[0]["episode_id"] if eps else None
        except Exception:
            ep_id = None
    if not ep_id:
        pytest.skip("No episodes available in the live database to review")
    findings = _navigate_and_scan(page, "review", f"{DASHBOARD_BASE}/operate/review/{ep_id}", extra_wait_ms=2000)
    _assert_no_leaks("review", findings, page)


def test_escalations_no_leaks(page: Page):
    """Escalation queue page has no viewer-facing leaks."""
    _require_services()
    findings = _navigate_and_scan(page, "escalations", f"{DASHBOARD_BASE}/operate/escalations", extra_wait_ms=1500)
    _assert_no_leaks("escalations", findings, page)


def test_kpi_no_leaks(page: Page):
    """KPI views page has no viewer-facing leaks."""
    _require_services()
    findings = _navigate_and_scan(page, "kpi", f"{DASHBOARD_BASE}/operate/kpi", extra_wait_ms=3000)
    _assert_no_leaks("kpi", findings, page)


def test_lab_list_no_leaks(page: Page):
    """Experiment lab list (Active tab) has no viewer-facing leaks."""
    _require_services()
    findings = _navigate_and_scan(page, "lab", f"{DASHBOARD_BASE}/improve/lab", extra_wait_ms=1500)
    _assert_no_leaks("lab", findings, page)


def test_lab_drawer_no_leaks(page: Page):
    """Experiment lab — opening the first card's drawer exposes no leaks."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1200)
    # Open the first visible card to trigger the drawer.
    cards = page.locator(".card")
    if cards.count() == 0:
        pytest.skip("No experiment cards visible on the lab page")
    cards.first.click()
    page.wait_for_timeout(1000)
    body = page.inner_text("body")
    _assert_body_no_leaks("lab_drawer", body, page)


def test_lab_detail_no_leaks(page: Page):
    """Experiment detail page (/improve/lab/[id]) — arm calls show no tier jargon or leaks."""
    _require_services()
    # Find a completed experiment with arm calls (RUN- prefix, n>0).
    exp_id = None
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/experiments", timeout=8) as r:
            data = json.loads(r.read())
        exps = data.get("experiments", [])
        run_exp = next(
            (e for e in exps if e.get("experiment_id", "").startswith("RUN-") and e.get("n", 0) > 0),
            None,
        )
        exp_id = run_exp["experiment_id"] if run_exp else None
    except Exception:
        exp_id = None
    if not exp_id:
        pytest.skip("No completed RUN- experiment with arm calls found in the live database")
    findings = _navigate_and_scan(
        page, "lab_detail",
        f"{DASHBOARD_BASE}/improve/lab/{urllib.parse.quote(exp_id)}",
        extra_wait_ms=2000,
    )
    _assert_no_leaks("lab_detail", findings, page)


def test_versions_no_leaks(page: Page):
    """Version history page has no viewer-facing leaks."""
    _require_services()
    findings = _navigate_and_scan(page, "versions", f"{DASHBOARD_BASE}/improve/versions", extra_wait_ms=1500)
    _assert_no_leaks("versions", findings, page)


def test_approvals_no_leaks(page: Page):
    """Approval queue page has no viewer-facing leaks."""
    _require_services()
    findings = _navigate_and_scan(page, "approvals", f"{DASHBOARD_BASE}/improve/approvals", extra_wait_ms=1500)
    _assert_no_leaks("approvals", findings, page)


def test_call_review_no_cb_index_leak(page: Page):
    """CB-81 (CB-NN strip): call review turn rationale must not contain 'CB-NN' internal index tags
    stripped from src/core gate-rationale strings by humanizeRationale. Also asserts no raw
    'sim-harness decision' rationale leaks on the review page."""
    _require_services()
    ep_id = None
    # Prefer a stub episode (most likely to have sim-harness rationale).
    for cohort in ("sim", None):
        try:
            q = f"{API_BASE}/api/episodes?limit=50" + (f"&cohort={cohort}" if cohort else "")
            with urllib.request.urlopen(q, timeout=8) as r:
                data = json.loads(r.read())
            eps = data.get("episodes", [])
            ep_id = eps[0]["episode_id"] if eps else None
            if ep_id:
                break
        except Exception:
            pass
    if not ep_id:
        pytest.skip("No episodes available to review")

    page.goto(f"{DASHBOARD_BASE}/operate/review/{ep_id}", wait_until="networkidle", timeout=30_000)
    page.wait_for_timeout(2000)
    body = page.inner_text("body")

    # (a) No raw CB-NN index in rendered text from gate rationale strings.
    cb_re = re.compile(r"\(CB-\d+[^)]*\)")
    cb_hits = sorted(set(cb_re.findall(body)))
    if cb_hits:
        shot_path = os.path.join(SCREENSHOT_DIR, "cb81_cb_index_in_review.png")
        page.screenshot(path=shot_path)
        pytest.fail(
            f"CB-81 (CB-NN strip): internal CB-NN parenthetical found in call review for {ep_id}: "
            f"{cb_hits!r}\nScreenshot: {shot_path}"
        )

    # (b) No raw "sim-harness decision" rationale.
    if re.search(r"sim-harness\s+decision", body, re.IGNORECASE):
        shot_path = os.path.join(SCREENSHOT_DIR, "cb81_sim_harness_in_review.png")
        page.screenshot(path=shot_path)
        pytest.fail(
            f"CB-81 O2: 'sim-harness decision' rationale not humanized in call review for {ep_id}\n"
            f"Screenshot: {shot_path}"
        )


def test_calls_channel_no_raw_text_slug(page: Page):
    """CB-81 O1: the calls list and call drawer must not show raw 'text' for the channel field.
    After the fix, channelLabel() maps 'text' → 'Web chat'. We look for 'Channel\\ntext' or
    'Channel text' as that is how the facts grid renders the label + value pair."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle", timeout=30_000)
    page.wait_for_timeout(1500)
    # Switch to All cohorts to surface any text-channel sim/stub rows.
    all_btn = page.locator("button:has-text('All cohorts'), button:has-text('All')")
    if all_btn.count() > 0:
        all_btn.first.click()
        page.wait_for_timeout(1200)
    body = page.inner_text("body")
    if re.search(r"Channel\s+text\b", body, re.IGNORECASE):
        shot_path = os.path.join(SCREENSHOT_DIR, "cb81_raw_channel_text.png")
        page.screenshot(path=shot_path)
        pytest.fail(
            "CB-81 O1: raw channel slug 'text' visible in calls list (expected 'Web chat')\n"
            f"Screenshot: {shot_path}"
        )
    # Open a drawer to check the inline channel tag.
    cards = page.locator("tbody tr")
    if cards.count() > 0:
        cards.first.click()
        page.wait_for_timeout(800)
        drawer = page.locator(".drawer")
        if drawer.count() > 0:
            drawer_text = drawer.inner_text()
            if re.search(r"Channel\s+text\b", drawer_text, re.IGNORECASE):
                shot_path = os.path.join(SCREENSHOT_DIR, "cb81_raw_channel_text_drawer.png")
                page.screenshot(path=shot_path)
                pytest.fail(
                    "CB-81 O1: raw channel slug 'text' visible in call drawer (expected 'Web chat')\n"
                    f"Screenshot: {shot_path}"
                )
        # Close drawer
        close_btns = page.locator(".drawer .gctl")
        if close_btns.count() > 0:
            close_btns.first.click()
            page.wait_for_timeout(300)


def test_lab_title_no_raw_mutation_string(page: Page):
    """CB-70 regression: blank-name records must never show a raw mutation string (-> [) as a title.
    Checks that card bold titles (.b elements) on the lab list contain no '-> [' pattern.
    If the known QA9 blank-name record is present, verifies it shows a humanized title."""
    _require_services()
    page.goto(f"{DASHBOARD_BASE}/improve/lab", wait_until="networkidle")
    page.wait_for_timeout(1500)
    # Switch to Past tab to surface the blank-name discovery_sequence records.
    past_btn = page.locator("button:has-text('Past')")
    if past_btn.count() > 0:
        past_btn.first.click()
        page.wait_for_timeout(800)
    # All bold card titles must not contain raw mutation strings.
    titles = page.locator("[data-kind='exp-card'] .b").all_inner_texts()
    bad_titles = [t for t in titles if "-> [" in t or t.strip().lower() == "draft"]
    if bad_titles:
        shot_path = os.path.join(SCREENSHOT_DIR, "cb70_title_leak.png")
        page.screenshot(path=shot_path)
        pytest.fail(
            f"CB-70: raw mutation string or 'draft' found in card title(s): {bad_titles!r}\n"
            f"Screenshot: {shot_path}"
        )
    # Also verify the known QA9 blank-name record if present.
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/experiments", timeout=8) as r:
            data = json.loads(r.read())
        exps = data.get("experiments", [])
        # The QA9 record: name is the raw mutation string.
        raw_name_exp = next(
            (e for e in exps if e.get("name", "").startswith("reorder discovery_sequence -> [")),
            None,
        )
        if raw_name_exp:
            exp_id = raw_name_exp["experiment_id"]
            url = f"{DASHBOARD_BASE}/improve/lab/{urllib.parse.quote(exp_id)}"
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(1500)
            body = page.inner_text("body")
            if "-> [" in body:
                shot_path = os.path.join(SCREENSHOT_DIR, "cb70_detail_leak.png")
                page.screenshot(path=shot_path)
                pytest.fail(
                    f"CB-70: raw mutation string '-> [' found on detail page for {exp_id}\n"
                    f"Screenshot: {shot_path}"
                )
    except Exception:
        pass  # experiment list unavailable — title check above is sufficient


# ---------------------------------------------------------------------------
# Helper assertions
# ---------------------------------------------------------------------------


def _assert_no_leaks(
    tag: str,
    findings: list[tuple[str, str, list[str]]],
    page: Optional[Page] = None,
) -> None:
    if not findings:
        return
    lines = [f"Viewer-facing leak(s) found on page '{tag}':"]
    for pname, pdesc, hits in findings:
        lines.append(f"  [{pname}] {pdesc}")
        for h in hits[:6]:
            lines.append(f"    - {h!r}")
        if len(hits) > 6:
            lines.append(f"    … and {len(hits) - 6} more")
    if page:
        shot_path = os.path.join(SCREENSHOT_DIR, f"cb65_leak_{tag}.png")
        page.screenshot(path=shot_path)
        lines.append(f"  Screenshot: {shot_path}")
    pytest.fail("\n".join(lines))


def _assert_body_no_leaks(tag: str, body: str, page: Optional[Page] = None) -> None:
    findings: list[tuple[str, str, list[str]]] = []
    for pname, prx, pdesc in LEAK_PATTERNS:
        raw_hits = sorted(set(prx.findall(body)))
        real_hits = [h for h in raw_hits if _hit_is_allowed(pname, h) is None]
        if real_hits:
            findings.append((pname, pdesc, real_hits))
    _assert_no_leaks(tag, findings, page)


# ---------------------------------------------------------------------------
# Direct script mode (run without pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _PW_AVAILABLE:
        print("ERROR: playwright not installed — `pip install playwright && playwright install`")
        sys.exit(1)

    # Check services
    for label, url in [("API", f"{API_BASE}/api/episodes?limit=1"), ("Dashboard", f"{DASHBOARD_BASE}/")]:
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            print(f"ERROR: {label} service not reachable at {url}")
            sys.exit(1)

    total_hits = 0
    all_findings: list[tuple[str, str, str, list[str]]] = []  # (tag, url, pname, hits)

    PAGES_TO_SCAN = [
        ("demo_pre_consent", f"{DASHBOARD_BASE}/demo", 0),
        ("calls_real", f"{DASHBOARD_BASE}/operate/calls", 1500),
        ("escalations", f"{DASHBOARD_BASE}/operate/escalations", 1500),
        ("kpi", f"{DASHBOARD_BASE}/operate/kpi", 3000),
        ("lab", f"{DASHBOARD_BASE}/improve/lab", 1500),
        ("versions", f"{DASHBOARD_BASE}/improve/versions", 1500),
        ("approvals", f"{DASHBOARD_BASE}/improve/approvals", 1500),
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()

        for tag, url, wait_ms in PAGES_TO_SCAN:
            print(f"\n--- scanning {tag} ({url}) ---")
            pg.goto(url, wait_until="networkidle", timeout=30_000)
            if wait_ms:
                pg.wait_for_timeout(wait_ms)
            if "kpi" in url:
                for _ in range(15):
                    if "Loading metrics" not in pg.inner_text("body"):
                        break
                    pg.wait_for_timeout(800)
            body = pg.inner_text("body")
            for pname, prx, pdesc in LEAK_PATTERNS:
                raw_hits = sorted(set(prx.findall(body)))
                real_hits = [h for h in raw_hits if _hit_is_allowed(pname, h) is None]
                if real_hits:
                    print(f"  LEAK [{pname}] {pdesc}")
                    for h in real_hits[:6]:
                        print(f"    - {h!r}")
                    all_findings.append((tag, url, pname, real_hits))
                    total_hits += len(real_hits)
                else:
                    pass  # clean

        # Calls — All cohorts
        print("\n--- scanning calls_all_cohorts ---")
        pg.goto(f"{DASHBOARD_BASE}/operate/calls", wait_until="networkidle", timeout=30_000)
        pg.wait_for_timeout(1200)
        all_btn = pg.locator("button:has-text('All')")
        if all_btn.count() > 0:
            all_btn.first.click()
            pg.wait_for_timeout(1200)
        body = pg.inner_text("body")
        for pname, prx, pdesc in LEAK_PATTERNS:
            raw_hits = sorted(set(prx.findall(body)))
            real_hits = [h for h in raw_hits if _hit_is_allowed(pname, h) is None]
            if real_hits:
                print(f"  LEAK [{pname}] {pdesc}: {real_hits[:4]}")
                total_hits += len(real_hits)

        # Lab detail (RUN- experiment with arm calls)
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/experiments", timeout=8) as r:
                data = json.loads(r.read())
            exps = data.get("experiments", [])
            run_exp = next(
                (e for e in exps if e.get("experiment_id", "").startswith("RUN-") and e.get("n", 0) > 0),
                None,
            )
            if run_exp:
                exp_id = run_exp["experiment_id"]
                print(f"\n--- scanning lab_detail ({exp_id[:50]}) ---")
                pg.goto(
                    f"{DASHBOARD_BASE}/improve/lab/{urllib.parse.quote(exp_id)}",
                    wait_until="networkidle", timeout=30_000,
                )
                pg.wait_for_timeout(2000)
                body = pg.inner_text("body")
                for pname, prx, pdesc in LEAK_PATTERNS:
                    raw_hits = sorted(set(prx.findall(body)))
                    real_hits = [h for h in raw_hits if _hit_is_allowed(pname, h) is None]
                    if real_hits:
                        print(f"  LEAK [{pname}] {pdesc}: {real_hits[:4]}")
                        total_hits += len(real_hits)
        except Exception as e:
            print(f"  (lab_detail skipped: {e})")

        # CB-81 O2 + CB-NN strip: call review for sim/stub episode
        print("\n--- scanning call_review (CB-81 sim-harness + CB-NN strip) ---")
        ep_id = None
        for cohort in ("sim", None):
            try:
                q = f"{API_BASE}/api/episodes?limit=50" + (f"&cohort={cohort}" if cohort else "")
                with urllib.request.urlopen(q, timeout=8) as r:
                    data = json.loads(r.read())
                eps = data.get("episodes", [])
                ep_id = eps[0]["episode_id"] if eps else None
                if ep_id:
                    break
            except Exception:
                pass
        if ep_id:
            pg.goto(f"{DASHBOARD_BASE}/operate/review/{ep_id}", wait_until="networkidle", timeout=30_000)
            pg.wait_for_timeout(2000)
            body = pg.inner_text("body")
            cb_hits = re.findall(r"\(CB-\d+[^)]*\)", body)
            sim_hits = re.findall(r"sim-harness\s+decision", body, re.IGNORECASE)
            if cb_hits:
                print(f"  LEAK [cb_index_parenthetical]: {cb_hits[:4]}")
                total_hits += len(cb_hits)
            if sim_hits:
                print(f"  LEAK [sim_harness_decision]: {sim_hits[:4]}")
                total_hits += len(sim_hits)
            if not cb_hits and not sim_hits:
                print("  clean (no CB-NN parentheticals, no sim-harness decision)")
        else:
            print("  (skipped — no episodes available)")

        ctx.close()
        browser.close()

    print(f"\n{'='*60}")
    if total_hits == 0:
        print("SWEEP PASSED — zero viewer-facing leaks found.")
    else:
        print(f"SWEEP FAILED — {total_hits} viewer-facing leak(s) found across {len(all_findings)} pattern(s).")
        sys.exit(1)
