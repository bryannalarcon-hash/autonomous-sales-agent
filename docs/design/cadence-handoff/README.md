# Handoff: Cadence — AI Voice Sales-Agent Operator Dashboard

## Overview

**Cadence** is the operator console for an autonomous AI voice agent that runs outbound/inbound sales calls. A single operator (solo workspace) uses it to **(a) Operate** — watch live calls, review completed ones, track KPIs, and triage escalations — and **(b) Improve** — run champion/challenger experiments, approve guardrail-tripping changes, edit the playbook/knowledge base, and roll versions back and forth.

The product is organized as two **modes** (Operate / Improve) in a left rail, with **9 destinations** plus one detail screen (Call Review). It is a **data-dense, real-time, desktop-first** tool (~1440px target width).

This handoff documents the full design so it can be implemented in a production codebase.

---

## About the Design Files

The files in this bundle are **design references created in HTML/React-via-Babel** — runnable prototypes that show the intended look, layout, and behavior. **They are not production code to copy directly.**

Your task is to **recreate these designs in the target codebase's existing environment**, using its established framework, component library, state management, and styling conventions. If the project has a React + design-system setup, build Cadence with those components. If no environment exists yet, choose an appropriate stack (React + CSS Modules / Tailwind / styled-components are all fine) and implement there.

The prototype uses `React.createElement` (no JSX compile step) and a single hand-written CSS file. In production you would naturally use JSX and your own component primitives — treat the prototype's structure as the **specification**, not the implementation.

### What's in the bundle

```
design_handoff_cadence/
├── README.md                  ← you are here
├── Cadence App.html           ← the full 9-page app (open this in a browser)
├── app/
│   ├── cadence.css            ← THE design system: all tokens + component classes
│   ├── ui.jsx                 ← icon set (inline SVG paths), Spark, Ring helpers
│   ├── data.jsx               ← all mock data (window.DB)
│   ├── shell.jsx              ← nav rail, global top bar, hash router
│   ├── page-live.jsx          ← P1 Live Call Monitor
│   ├── page-review.jsx        ← Call Review (detail screen)
│   ├── page-calls.jsx         ← P3 Calls List
│   ├── page-kpi.jsx           ← P4 KPI Views
│   ├── page-escalations.jsx   ← P5 Escalation Queue
│   ├── page-lab.jsx           ← P6 Experiment Lab
│   ├── page-approvals.jsx     ← P7 Approval Queue
│   ├── page-kb.jsx            ← P8 KB / Playbook Editor
│   └── page-versions.jsx      ← P9 Version History & Rollback
└── reference/
    ├── Cadence Themes.html    ← the 7 theme explorations (we shipped "Nerdy")
    ├── cadence-*.jsx, design-canvas.jsx  ← support files for the themes page
    └── dashboard-ia-spec.md   ← the original product/IA spec this was built from
```

> **Start by opening `Cadence App.html` in a browser.** Use the left rail to walk all 9 pages. Click table rows / cards to open drawers, drag the scrubber on Call Review, open the rollback modal on Versions. The README below describes everything you'll see.

---

## Fidelity

**High-fidelity.** Final colors, typography, spacing, glass treatment, and interactions are all specified. Recreate the UI pixel-faithfully using the codebase's libraries. All design tokens (59 CSS custom properties) are listed at the bottom and defined in `app/cadence.css`.

The one intentional flexible dimension: this is a **desktop dashboard**. Layouts assume ~1280–1600px. Two of the detail pages (Call Review, KB Editor) are two-column grids that are meant for desktop width; they have `minmax()` guards but are not designed for mobile. Mobile/responsive is **out of scope** unless your team wants it — flag it if so.

---

## The "Nerdy" Visual Theme

A dark, premium console look:

- **Background:** a fixed multi-stop *aurora* — layered radial gradients (indigo, blue, violet) over a near-black base. Defined once as `--bg-stack` on `body`. It does **not** scroll; cards float over it.
- **Cards are frosted glass:** semi-transparent fill + `backdrop-filter: blur()` + a 1px light border + a top sheen highlight. This is the signature. See `.card` / `--glass*` tokens.
- **Accent:** cyan (`--accent: #2EE0DD`), often as a gradient (`--accent-grad`) on primary buttons and agent chat bubbles.
- **Display type:** Poppins (600/700) for headings, the brand wordmark, and big numbers. Body/UI is Inter. Numeric data uses tabular figures; code/IDs use JetBrains Mono.
- **Brand wordmark:** "Cadence" in Poppins with a pink→orange→cyan gradient text fill (`--word-grad`).
- **Status colors:** ok=mint, warn=amber, danger=pink-red, info=blue, violet=accentual. Each has a `-soft` (low-alpha fill) and `-border` variant for tags/badges.

> If your codebase has an existing dark theme / design system, you may map Cadence onto it. But the frosted-glass-over-aurora identity and the cyan+gradient accent are the point — preserve them.

---

## Global Shell (`shell.jsx` + `.app`, `.nav`, `.topbar` in CSS)

Two-column app grid: **`256px` fixed nav rail** + fluid main column. Full viewport height, no page scroll — only inner regions scroll.

### Left Nav Rail (`.nav`)
Vertical flex, translucent panel with blur, 1px right border.
1. **Brand** (`.brand`): 34px rounded-square mark with the cyan gradient + a "pulse/waveform" glyph, then "Cadence" wordmark (gradient text, Poppins 700, 19px).
2. **Mode switch** (`.mode`): a 2-segment pill toggle — **Operate** / **Improve** (each with an icon). The active segment has a raised surface look. Switching mode navigates to that mode's first page (Operate→`live`, Improve→`lab`).
3. **Operate group** (label "OPERATE", uppercase 10.5px, letter-spacing): nav items **Live** (with a pulsing red live-dot), **Calls**, **KPI Views**, **Escalations** (red count badge "3").
4. **Improve group**: **Experiment Lab**, **Approvals** (amber count badge "2"), **KB / Playbook**, **Versions**.
5. **Footer** (`.nav-foot`): "OP" avatar + "Operator / Solo workspace".

Nav item states: default (muted), hover (surface fill + brighter text), **active** (`.on` — cyan soft-fill, cyan text, a 3px cyan bar on the far left edge via `::before`). Active page is derived from the route; `review` keeps **Calls** highlighted.

### Top Bar (`.topbar`, height 62px)
Translucent + blur, 1px bottom border. Left→right:
- **Breadcrumb** (only on Call Review): "← Calls", click returns to `calls`.
- **Page title** (`.top-title`, Poppins 18px). Live Monitor appends a blinking **LIVE** pill; Review appends the call ID as a sub.
- Spacer.
- **Global controls** (`GlobalControls`, always visible, right-aligned):
  - **Champion vX** chip — shield icon + "Champion **v12**" + "kb-37" sub + chevron. Click → Versions page.
  - **Persona** chip — mic icon + "**Ava** · Warm-Direct".
  - **Environment** toggle — a pill that flips between **SANDBOX** (amber) and **LIVE** (mint). Local state.
  - Vertical rule, then a small "OP" avatar.

### Routing
Hash-based: `#live`, `#calls`, `#kpi`, `#escalations`, `#review`, `#lab`, `#approvals`, `#kb`, `#versions`. `review` accepts a param: `#review?call=CALL-4820`. The router (`useRoute`) listens to `hashchange`, resets scroll on navigation, and renders the page component registered at `window.CadencePages[route]`. **In production, use your router** (React Router etc.); the page→component registry maps 1:1 to routes.

---

## Screens / Views

> Measurements below are the prototype's exact values. Components reference shared classes in `cadence.css` (`.card`, `.btn`, `.tag`, `.kpi`, `.tbl`, `.bar`, `.seg`, `.drawer`, `.modal`, etc.) — see **Design Tokens & Component Classes** at the end.

### P1 — Live Call Monitor (`page-live.jsx`, route `#live`)
**Purpose:** Watch the agent handle a call in real time; understand *why* it's saying what it's saying; take over if needed.

**Layout:** A column filling the main area: a content body (CSS grid **`1fr 388px`**, 16px gap, 18/20px padding) above a fixed **66px action bar**.

- **Left column** (flex, 14px gap):
  - **Prospect header card** (`.lv-pros`, padding 15/17): 46px rounded avatar (cyan soft) "JA", name "Jordan Avery" (Poppins 16), company "Northwind Logistics", a tag row (`Skeptical Analyzer` [accent], `Inbound · SMB`, `Web-voice`). Right-aligned: live duration "04:12" (Poppins 22, tabular) + "v12 · kb-37".
  - **Transcript card** (`.lv-tcard`, grows, scrolls internally, pinned to newest): header "Transcript" + an "Auto-scroll" toggle (on). Stream of turns:
    - **Prospect turns** left-aligned: muted bubble, top-left corner squared.
    - **Agent turns** right-aligned: **cyan-gradient bubble**, dark ink text, top-right corner squared. Below each agent turn: a **decision chip** (`.lv-dchip` — spark icon + label like "Acknowledge · reframe-cost"), a latency value ("0.7s"), and an italic **rationale** quote ("Validate the objection before reframing to ROI.").
    - The newest agent turn shows an animated **typing indicator** (3 bouncing dots) + a pending decision chip ("Trial-close · pilot").
    - A top fade overlay masks scrolled-up content.
- **Right column — Belief State card** (`.lv-belief`, grows, scrolls):
  - Header: spark icon + "Belief State" + a stage tag ("Closing").
  - **Two gauges** side by side (`.lv-gg`): **Trust** "0.66" (↑ +.08, green) with a green progress bar; **Bail risk** "0.34" (↓ −.07, green delta = good) with an amber bar.
  - **Stage box**: "Stage → Closing" | "Last act → Trial-close".
  - **Escalation strip** (amber): warn icon tile + "Escalation · Armed" / "Watching concession pressure".
  - **Current decision card** (`.lv-dcard`, cyan-bordered soft panel): label "CURRENT DECISION", big "↻ Trial-close → pilot" (Poppins, cyan), a rationale sentence, and a **Confidence** mini-bar "0.83".
  - **Expandable "Full belief state"** (`.lv-exp`): a slot table — Budget 0.58, Authority 0.82, Need 0.90, Timeline 0.55, Team size 0.95 (each a labeled mini progress bar; <0.5 renders amber). Then **latent drivers** as chips with trend arrows: Price sensitivity ↑ (red=rising-bad), Skepticism ↓ easing (green), Urgency →, Rapport ↑.
- **Action bar** (`.lv-actions`, 66px): "● Recording · Turn 14 · synced 0.8s ago" (blinking red dot) · a **Talk/Listen** split meter (agent cyan 44% / prospect mint 56%) · right side: **Open review** (ghost) + **Take over** (primary, gradient, hand icon).

**Behavior:** Mostly presentational in the prototype (it's a snapshot of a live call). In production: transcript turns and belief values stream in over websocket; auto-scroll keeps newest in view; "Take over" hands the call to the human; "Open review" → `#review?call=CALL-4821`.

### Call Review (`page-review.jsx`, route `#review`)
**Purpose:** Post-call forensics — replay how the agent's belief state evolved turn by turn.

**Layout:** CSS grid `minmax(0,1fr) minmax(340px,416px)`, with a full-width **outcome strip** spanning the top.
- **Outcome strip:** avatar + name/company, then stat cells (Outcome "Enrolled · T3" in green, Duration 5:48, Turns 18, Version "v12 · kb-37"), then actions (Flag, Export, "Use in experiment").
- **Left — transcript with decision trace** (scrolls): each turn is a clickable row (`.rv-turn`) with a turn-number chip (agent numbers are cyan), speaker + timestamp + stage tag, the text, and for agent turns the decision chip + rationale + latency. **Clicking a turn selects it** (`.cur` — cyan ring).
- **Right — Belief Replay** (scrolls):
  - **Replay card**: "Belief replay" + "Turn N · mm:ss". A **scrubber** (`.rv-track`): gradient fill to current position, a draggable knob, and amber tick marks at key decision turns (pivot/close/de-risk/reframe). Click anywhere on the track to jump.
  - **Belief snapshot card**: Trust + Bail-risk gauges (values reflect the nearest snapshot at/below the selected turn), a Stage tag + the selected turn's decision tag, and a slot mini-table.
  - **Outcome card**: a 46px **Ring** gauge (0.93, green, "✓") + "Qualified correctly / Reached T3 same-call enroll · pilot booked".

**Behavior:** Selecting a turn (click row OR move scrubber) updates the belief snapshot to that moment. Verified: clicking an early turn drops Trust 0.66→0.42. In production, drive from the recorded per-turn belief log.

### P3 — Calls List (`page-calls.jsx`, route `#calls`)
**Purpose:** Browse/filter all calls; open one.

**Layout:** scrolling page, 22px padding.
- **Filter bar:** search input (with leading icon), an outcome **segmented control** (All / Enrolled / Booked / Escalated / Disqualified / No-interest), an "Escalated only" toggle button (turns amber when active), right side shows "N calls" + Export.
- **Table** (`.tbl` inside a card): columns Call (mono ID, + LIVE pill if live), Prospect (name + company), Archetype (tag), Outcome (status dot-tag), Ladder tier (muted), Duration (mono, right), Version (cyan tag), When (muted, right). Hover highlights the row and shows a cyan left edge.
- **Row click → detail drawer** (`.drawer`, 560px, slides from right over a scrim): header (avatar, name, company·archetype, close X), a tag row (outcome / tier / version·kb / channel / Escalated), a 3-col fact grid, a **"Belief at close"** mini-bar block (Trust 0.74, Bail 0.18, Qual conf. 0.93), and a primary **"Open full call review"** button → `#review?call=…`.

**Filtering logic:** outcome match AND (escalated-only ? escalated) AND text match on name/company/ID. Data: `DB.CALLS` (12 rows).

### P4 — KPI Views (`page-kpi.jsx`, route `#kpi`)
**Purpose:** Performance of the current champion; compare versions.

**Layout:** scrolling page. Top controls: **Overview / Compare versions** segmented control, a time-range segmented control (24h / 7d / 30d), and an archetype filter chip.

**Overview mode:**
- **4 primary KPI tiles** (`.kpi`, grid of 4): Weighted-ladder score 3.42/5 (+0.24 ↑), Same-call enrollment 61% (+6 pts ↑), Qualification accuracy 93% (+3 pts ↑), Guardrail status "Pass" (0 trips). Each tile: label+icon, big Poppins value + unit, colored delta, and a **Spark** sparkline in the corner. A 1px gradient sheen sits on the top edge.
- **Mid panels** (grid `1.1fr 1fr`): **Outcome ladder distribution** (T3 61% / T2 18% / T1 9% / T0 8% / DQ 4% — labeled bars in distinct colors) and **Objection recovery by type** (Price 72%, Trust/risk 81%, Timing 58%, Authority 66%, Competitor 69%, Feature gap 54% — bars, amber if <60%).
- **Two more panels** (grid `1fr 1fr`): **Conversion by archetype** (Warm Champion .78 / Busy Decider .64 / Skeptical Analyzer .49 / Price Hawk .31 — color by health) and **Avg dwell per stage** (Discovery 5.2 / Objection 6.1 / Closing 3.8 / Wrap 1.4 turns, violet bars).
- **Secondary metrics grid** (3 cols, 9 cards): Objection recovery 68%, Escalation rate 4.2%, Disqualification 7.1%, Talk/Listen 44/56, Avg turns 17.4, Avg duration 5:21, Discovery completeness 82%, Time-to-pivot 2.1, Abandon timing T-6.4.

**Compare mode:** a table comparing **v10 baseline vs v12 champion** across 7 metrics with a colored Δ tag per row.

Data: `DB.KPI`. The `BarRow` helper renders all labeled bars; `Spark` renders sparklines.

### P5 — Escalation Queue (`page-escalations.jsx`, route `#escalations`)
**Purpose:** Triage moments where the agent flagged for a human.

**Layout:** scrolling page. A **lifecycle segmented control** with counts — **Unreviewed / Reviewed / Resolved** — plus a "Reason" filter chip. Below, a list of escalation cards.
- **Escalation card:** a left **severity bar** (high=danger / med=warn / low=info color), then ID (mono) + severity tag + reason tag, the prospect name·company, the **trigger moment** description, and right-aligned the time + version tag + a chevron.
- **Card click → drawer:** severity-colored icon header (reason + ID + time), tags, a **"Trigger moment"** panel, a **"Belief at escalation"** mini-bar block (Bail 0.71, Concession pressure 0.64, Trust 0.38), and actions: **Open call** (→ review), **Assign callback**, **Mark reviewed** (only if unreviewed), **Resolve**, and **Turn into fix** (→ Lab).

Data: `DB.ESCALATIONS` (5 rows across states). Reasons include Pricing concession, Human requested, Compliance.

### P6 — Experiment Lab (`page-lab.jsx`, route `#lab`)
**Purpose:** Run champion vs challenger experiments and read results.

**Layout:** scrolling page. **Active / Past** segmented control + a **"New experiment"** primary button. A 2-col grid of experiment cards.
- **Experiment card:** ID + a **state tag** (Running=info, Result ready=ok, Guardrail blocked=danger, Failed/Retired=neutral) + optional "Auto-promote ready" badge; the name (Poppins); a "champ → challenger" tag + population. A 3-cell footer (Enroll Δ, Ladder Δ, Significance — green/red by sign). If blocked, a red guardrail reason banner. A **sample progress bar** ("n / target").
- **Card click → drawer:** **before/after** layout (Champion vXX → Challenger vYY card), a **"What changed"** mono diff line, a **lift block** (enroll, ladder, 95% CI, significance) with a guardrail pass/trip strip, and a state-dependent CTA: **Promote to champion** (result-ready), **Send to approval queue** (blocked), **Pause/Stop** (running), or **Clone & re-run** (past).

Data: `DB.EXPERIMENTS` (5). States: running, result-ready, blocked, failed, retired.

### P7 — Approval Queue (`page-approvals.jsx`, route `#approvals`)
**Purpose:** Sign off (or reject) changes that improve outcomes **but breach a guardrail** — promotion is paused until the operator decides.

**Layout:** scrolling page with a section header explaining the queue, then a list of approval cards.
- **Approval card:** ID + warn reason tag + experiment·challenger tag + time; name (Poppins); a detail paragraph; a 3-cell strip (Enroll lift, Ladder lift, **Guardrail breached**); footer actions: **Review detail** (→ drawer), **Reject** (danger), **Approve with override** (ok).
- **Drawer:** "Why it needs sign-off" panel, an amber **guardrail warning** panel ("Approving records an override on your account and ships vYY as champion."), enroll/ladder lift cards, and Reject / Approve-with-override buttons.
- Approving/rejecting removes the card from the queue and appends a logged-decision line ("… logged to version history").

Data: `DB.APPROVALS` (2). Local `decided` state map drives removal + the log line.

### P8 — KB / Playbook Editor (`page-kb.jsx`, route `#kb`)
**Purpose:** Edit the agent's playbook as a **draft challenger** (forks the KB; doesn't touch live calls until promoted via an experiment).

**Layout:** two-column grid `262px minmax(0,1fr)`.
- **Left — section tree** (scrolls): "Playbook · kb-37" + add button. Section nav items: Persona & tone, Discovery flow, **Objection rebuttals** (active, shows an amber unsaved dot), Pricing & concessions (lock icon), Closing triggers, Escalation rules, Compliance guardrails (lock). Below, a **"Draft challenger"** info card.
- **Right — editor / diff:** a toolbar (section title + meta, an **Edit / Diff vs champion** segmented control, Revert, **"Test as experiment"** primary → Lab). Then either:
  - **Edit view:** a rule card ("Rule 3 · Price objection", price tag, "recovery 72%") with a **Trigger condition** (mono code box), a **Response strategy** textarea, and two select fields (Next stage / Escalate if). Plus an "Add rebuttal rule" button.
  - **Diff view:** "Champion kb-37 → Draft kb-37-d1" + "+3 / −2 lines", then a unified **diff** (`.diff` rows: added=green/left-border, deleted=red/strikethrough, context=muted, mono font).

Data: hardcoded `SECTIONS` and `DIFF` arrays in the file.

### P9 — Version History & Rollback (`page-versions.jsx`, route `#versions`)
**Purpose:** See the lineage of agent versions; inspect any; roll back.

**Layout:** two-column grid `1fr 400px`.
- **Left — lineage timeline** (scrolls): a vertical connector line; each version is a node (champion node = cyan with glow; selected = ring) + a card showing label (mono) + **Champion** tag (if live) + kb tag + guardrail tag + date; a one-line note; and **mini metrics** (Ladder / Enroll / Qual as tiny labeled bars) + call count. Click selects.
- **Right — detail** (scrolls): big version label + Champion/Archived tag, note, a 2-col fact grid (KB, persona, parent, created, calls, guardrail), a **Performance** block (weighted ladder / enroll / qualification bars), a **"Change from parent"** mono line, and either a "live in production" confirmation (champion) or **Diff vs champion** + **"Roll back to vXX"** (primary) actions.
- **Rollback → modal** (`.modal`, 460px, centered over scrim): rollback icon, "Roll back to vXX?", an explanation, an "Expected ladder change" row (red negative delta), and Cancel / **Confirm rollback**.

Data: `DB.VERSIONS` (7: v12 champion … v1 genesis, with parent links forming a tree).

---

## Interactions & Behavior (summary)

- **Navigation:** hash router; nav rail + mode switch + in-page CTAs all navigate. Cross-links: Live→Review, Calls row→Review, Escalation→Review/Lab, Lab(blocked)→Approvals, KB→Lab, top-bar Champion chip→Versions.
- **Drawers** (Calls, Escalations, Lab, Approvals): right-side slide-in over a scrim; close via X or scrim click. One open at a time (local `sel` state).
- **Modal** (Versions rollback): centered pop over scrim.
- **Selection state:** Review (selected turn drives belief replay), Versions (selected version drives detail), KB (active section + Edit/Diff toggle), KPI (Overview/Compare + range), Escalations (lifecycle tab), Lab (Active/Past tab).
- **Toggles:** environment Sandbox/Live, auto-scroll, "Escalated only".
- **Animations:** nav/hover transitions ~140ms; drawer slide ~220ms `cubic-bezier(.2,.8,.2,1)`; modal pop ~200ms; live indicators — `pulse` (live dot ring, 1.8s), `blink` (recording/LIVE, 1.4s), typing dots `lvd` (1.2s). Keep these.
- **Empty states** (`.empty`): Escalations and Approvals show an icon + heading + copy when a list/queue is empty.

## State Management

The prototype uses local component state only (`useState`) and a shared `window.DB` for mock data. For production:

- **Server data:** calls, transcripts + per-turn belief logs, KPI aggregates, escalations, experiments, approvals, KB sections/rules, version lineage. Most screens are read-heavy; wire to your data layer (REST/GraphQL/websocket). The Live Monitor and the LIVE row in Calls need **realtime** (websocket/SSE).
- **Mutations:** resolve/review escalation; approve/reject change (records an override + writes to version history); promote challenger; roll back version; save KB draft + "test as experiment". These should be optimistic where safe and reflected in the relevant lists.
- **Per-screen UI state** (selected turn/version/section, open drawer/modal, active tab, filters, toggles) is local.
- **Global UI state:** current route, environment (Sandbox/Live), champion version + persona shown in the top bar.

## Design Tokens & Component Classes

**All tokens live in `app/cadence.css` under `:root`.** Port these verbatim. Key values:

**Surfaces / aurora**
- `--bg-stack`: fixed layered radial+linear gradient (indigo `#3A2566`, blue `#1C3478`, violet `#2A1A52`, base `#12132C`→`#0B0C1C`). Applied to `body`, `background-attachment: fixed`.
- `--panel #14152E`, `--panel-2 #171833`, `--surface #191A38`, `--surface-2 #212350`, `--surface-3 #2B2E60`, `--border #2C2E59`, `--border-soft #23244A`.

**Glass** (the signature card treatment)
- `--glass: linear-gradient(158deg, rgba(46,48,96,.62), rgba(20,21,46,.5))`
- `--glass-2: linear-gradient(158deg, rgba(54,56,108,.5), rgba(26,27,54,.42))`
- `--glass-blur: saturate(150%) blur(22px)` · `--glass-border: rgba(255,255,255,.10)`
- `--glass-sheen: 0 1px 0 rgba(255,255,255,.10) inset` · `--glass-shadow: 0 18px 46px rgba(6,7,22,.5), 0 2px 8px rgba(6,7,22,.35)`
- `.card` = glass + blur + glass border + sheen/shadow. `.card.solid` opts out (opaque `--surface`) for nested panels inside a glass card.

**Text:** `--text #F3F2FC`, `--text-2 #B0ACD4`, `--text-3 #7C77A6`, `--text-faint #5B5783`.

**Accent (cyan):** `--accent #2EE0DD`, `--accent-2 #23C7D8`, `--accent-ink #06262C`, `--accent-soft #0F3038`, `--accent-border #1E5059`, `--accent-strong #57EDE9`, `--accent-strong-bg #0E2C36`, `--accent-glow rgba(46,224,221,.40)`, `--accent-grad linear-gradient(135deg,#39E9E1,#23C7D8)`.

**Brand gradient:** `--word-grad linear-gradient(95deg,#FF5C9A 0%,#FF9E57 46%,#3DE0DD 100%)` (used as `background-clip:text` for the wordmark / `.glow-text`).

**Status (each with `-soft` fill + `-border`):** `--ok #3DD6A0`, `--warn #F4A23C` (+`--warn-strong #F6B65F`, `--warn-ink #241704`), `--danger #FF5C7A`, `--info #6AA6FF`, `--violet #9C7BFF`.

**Type:**
- `--font`: Inter (400/500/550/600/650/700)
- `--font-display`: Poppins (500/600/700/800) — headings, wordmark, big numbers
- `--font-mono`: JetBrains Mono — IDs, code, diffs, numeric detail
- Headings use `letter-spacing: -0.02em`. Body `-0.006em`. Numeric uses `font-variant-numeric: tabular-nums` (`.tnum` / `.mono`).
- **Minimum sizes used:** body 13–14px, labels 10.5–12px (uppercase micro-labels are 10.5px/700/0.06em tracking). Don't go smaller.

**Radii:** `--r 18px` (cards), `--r-md 14px`, `--r-sm 11px`, `--r-xs 8px`, `--r-pill 999px`. Buttons 11px, tags 7px, inputs/chips 10px.

**Shadows:** `--shadow`, `--shadow-sm`, `--shadow-pop` (drawer/modal), plus the glass shadows above.

**Layout constants:** `--nav-w 256px`, `--top-h 62px`.

**Reusable component classes** (study them in `cadence.css`, recreate as components):
`.card`/`.card.solid`/`.card-head`/`.card-pad` · `.btn` + `.btn-primary|ghost|danger|ok` + `.btn-sm|lg` · `.tag` + status/`.dot`/`.tag-lg` · `.bar`>`i` (progress) · `.toggle` · `.tbl` (table) · `.seg` (segmented control) · `.field`/`.input` (form) · `.kpi` (metric tile) · `.drawer`/`.modal`/`.scrim` · `.diff-add`/`.diff-del` · `.empty` (empty state) · `.sec-head` · plus layout utilities (`.row`,`.col`,`.grow`,`.gapN`,`.muted`,`.faint`,`.mono`,`.tnum`,`.b`).

**Helpers in `ui.jsx`:**
- `Icon({d, size, sw})` — inline-SVG icon by name from a path table (`CIcons`). ~50 icons (broadcast, list, chart, alert, flask, badge, book, branch, mic, spark, pivot, hand, shield, etc.). Replace with your icon library, matching stroke weight ~1.7 and the named glyph meanings.
- `Spark({data, w, h, color})` — sparkline (gradient area + line).
- `Ring({value, size, stroke, color, label})` — circular progress gauge.

## Assets

- **No raster images/logos.** The brand mark and all icons are inline SVG (vector) — see `ui.jsx` and the `.brand-mark` glyph in `shell.jsx`. Re-draw or map to your icon set.
- **Fonts:** Inter, Poppins, JetBrains Mono — loaded from Google Fonts in `Cadence App.html`. Self-host or use your font pipeline in production.
- **Avatars** are text initials in colored squares (no images).

## Files to reference

- `app/cadence.css` — **the source of truth for all tokens and component styling.**
- `app/shell.jsx` — nav, top bar, routing, route↔page map.
- `app/ui.jsx` — icons + Spark + Ring.
- `app/data.jsx` — exact mock data shapes (`window.DB`) for every screen — use as the schema/contract for your real data.
- `app/page-*.jsx` — one file per screen, each documents structure + interactions for that view.
- `reference/dashboard-ia-spec.md` — the original product/IA spec (rationale, modes, page intents).
- `reference/Cadence Themes.html` — the 7 explored themes (we shipped **Nerdy**, the dark cyan/aurora one); useful if you ever want the light variants.
