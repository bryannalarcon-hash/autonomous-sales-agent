// Shared humanizers for operator-facing text. Internal indices/slugs (the experiment `__<dim>__<n>`
// suffix, short `-hash` suffixes, `DRAFT-`/`exp-` id prefixes, snake_case dimension/archetype/
// population/threshold slugs, and the gate-rationale gate-names + driver enum slugs) must NEVER render
// in observable output — these turn them into human-readable labels (versionLabel / dimensionLabel /
// archetypeLabel / populationLabel / humanizeDiffDescription / humanizeRationale / ladderTierLabel /
// kbVersionLabel / humanizeGuardrailReason). Mirrors the inlined versionLabel logic in DashboardShell.tsx
// / kpi / versions (kept in sync; do not import from those — they keep their own copy). Used by
// improve/lab, improve/approvals, operate/kpi, operate/review pages, and the lab detail arm-call rows.
// CB-04: humanizeDiffDescription scrubs raw threshold keys (max_concession_band → "Pricing concession
// band") out of the approvals diff sentence, and humanizeRationale scrubs gate-names + driver enum
// slugs (pushiness_cap, bail_risk, …) out of the review-page `.rv-rat` rationale — display only; the
// backend logs/records keep the raw slug.
// CB-65: ladderTierLabel translates commitment-ladder tier ints to human labels (mirrors the Python
// LADDER_TIER_LABEL map in src/api/labels.py). kbVersionLabel translates a raw kb_version slug
// ("kb_v0") to a human-readable form ("Knowledge base v0") for header chips and inline tags.

// Strip the internal suffixes from a raw version id for display: the experiment dimension/seq suffix
// ("champion_v0__playbooks_discovery_sequence__7" -> "champion_v0") and any short hash
// ("v1-f3798d7a" -> "v1"), then humanize the prefix ("champion_v0" -> "Champion v0"). Never renders
// the raw internal index to the operator.
export function versionLabel(raw: string | null | undefined): string {
  if (!raw) return '—';
  let base = raw.split('__')[0]; // drop the experiment dimension/seq suffix
  base = base.split('-')[0]; // drop a short hash suffix
  if (base.startsWith('champion_')) return `Champion ${base.slice('champion_'.length)}`;
  return base;
}

// A few canonical dimension slugs we want to read cleanly; anything else falls through to Title Case.
// Keys are the dimension TOKEN (the segment between the `__…__` markers, or a `thresholds.`/`playbooks.`
// tail), e.g. "playbooks_discovery_sequence", "pricing", "playbooks_rebuttals".
const DIMENSION_LABELS: Record<string, string> = {
  playbooks_discovery_sequence: 'Discovery sequence',
  discovery_sequence: 'Discovery sequence',
  playbooks_rebuttals: 'Rebuttals',
  rebuttals: 'Rebuttals',
  pricing: 'Pricing',
};

// Turn a snake_case (or dotted) dimension slug into a human label. Prefers the canonical map above,
// else derives Title Case from the token ("max_concession_band" -> "Max concession band"). Accepts
// either the bare token ("pricing") or a dotted form ("thresholds.max_concession_band").
export function dimensionLabel(slug: string | null | undefined): string {
  if (!slug) return '';
  // Use the tail after any dotted namespace ("thresholds.max_concession_band" -> "max_concession_band").
  const token = slug.includes('.') ? slug.split('.').pop()! : slug;
  if (DIMENSION_LABELS[token]) return DIMENSION_LABELS[token];
  return titleCase(token);
}

// Turn an archetype slug into Title Case words ("anxious_parent" -> "Anxious parent"). Already-human
// strings (no underscore) pass through capitalized for safety.
export function archetypeLabel(slug: string | null | undefined): string {
  if (!slug) return '';
  return titleCase(slug);
}

// Humanize an experiment population/cohort value for display. Real runs already arrive humanized
// ("Held-out · 12 personas") and pass through untouched; seeded/legacy records carry a raw slug
// ("held_out", "mined_failures") that must NOT render as-is. Canonical slugs map to a curated label;
// anything else (already-human strings, other slugs) falls through to Title Case so no bare
// snake_case / `__` token ever reaches the operator.
const POPULATION_LABELS: Record<string, string> = {
  held_out: 'Held-out',
  mined_failures: 'Mined failures',
  training: 'Training',
  live: 'Live',
};
export function populationLabel(value: string | null | undefined): string {
  if (!value) return '';
  const trimmed = value.trim();
  // Already-humanized backend strings carry a space, a "·" separator, or an uppercase lead — leave
  // those intact (only the raw snake_case slugs need translating).
  if (/[\s·]/.test(trimmed) || /^[A-Z]/.test(trimmed)) {
    return POPULATION_LABELS[trimmed] ?? trimmed;
  }
  return POPULATION_LABELS[trimmed] ?? titleCase(trimmed);
}

// Gate THRESHOLD keys (config knobs an experiment can tune) → operator-facing labels. These keys leak
// raw into the Approvals "diff_description" sentence (e.g. "set max_concession_band -> 0.22 (was
// 0.15)") and into gate rationales. The pricing-concession band is the headline extreme one CB-04
// calls out; siblings here are the rest of the gate thresholds (src/core/gates.py `_DEFAULTS`). A key
// not in this map falls through to Title Case, so no bare snake_case threshold key ever renders.
const THRESHOLD_LABELS: Record<string, string> = {
  max_concession_band: 'Pricing concession band',
  trust_gate_open_price: 'Trust to open pricing',
  pushiness_cap: 'Pushiness cap',
  pushiness_pressure_count_cap: 'Repeated-pressure cap',
  escalate_low_confidence_turns: 'Low-confidence turns before escalating',
  low_confidence_level: 'Low-confidence level',
  discovery_slots_required: 'Discovery slots required',
  budget_refusals_before_callback: 'Budget refusals before callback',
  close_ready_trust: 'Trust to offer a consultation',
  close_ready_trial_trust: 'Trust to offer a trial',
  close_ready_trial_intent: 'Purchase intent to offer a trial',
  min_turns_before_close: 'Minimum turns before closing',
  callback_price_sensitivity: 'Price sensitivity for a free callback',
};

// Humanize a threshold key for display ("max_concession_band" -> "Pricing concession band"). Accepts
// either a bare key or a dotted "thresholds.<key>" form; unknown keys fall through to Title Case.
export function thresholdLabel(key: string | null | undefined): string {
  if (!key) return '';
  const token = key.includes('.') ? key.split('.').pop()! : key;
  return THRESHOLD_LABELS[token] ?? titleCase(token);
}

// Driver/belief enum slugs (the hidden-state signals a gate reasons over) → human phrases. These leak
// verbatim inside gate rationales ("bail_risk over cap", "purchase_intent=0.55"). Mirrors the operator
// language used elsewhere in the UI (e.g. Walk-away risk gauge). Unmapped slugs fall through to Title
// Case via humanizeRationale, so no bare snake_case driver slug renders.
const DRIVER_LABELS: Record<string, string> = {
  bail_risk: 'walk-away risk',
  need_intensity: 'need intensity',
  price_sensitivity: 'price sensitivity',
  purchase_intent: 'purchase intent',
  budget_constrained: 'budget constrained',
  trust_velocity: 'trust momentum',
  bail_risk_velocity: 'walk-away momentum',
};

// Gate-name prefixes (the internal policy-gate identifiers a rationale leads with, e.g.
// "pushiness_cap: …") → human gate labels. These are NOT thresholds — they name the override gate that
// fired. Unmapped gate names fall through to Title Case.
const GATE_LABELS: Record<string, string> = {
  pushiness_cap: 'Pushiness cap',
  advance_to_close: 'Advance to close',
  must_clear_objection: 'Clear objection first',
  price_gate: 'Price gate',
  escalation_triggers: 'Escalation trigger',
  skip_known: 'Skip known slot',
  address_direct_input: 'Address direct input',
  offer_low_commitment_on_budget: 'Offer free callback',
};

// Build the union of every multi-word snake_case slug we humanize, LONGEST-first so a compound slug
// (bail_risk_velocity) matches before its prefix (bail_risk). The trailing `(?![a-z])` stops a partial
// match inside a longer token. Word-boundary-anchored so we never rewrite mid-word.
const RATIONALE_SLUG_MAP: Record<string, string> = { ...DRIVER_LABELS };
const RATIONALE_SLUG_RE = new RegExp(
  `\\b(${Object.keys(RATIONALE_SLUG_MAP)
    .sort((a, b) => b.length - a.length)
    .join('|')})(?![a-z_])`,
  'g',
);

// Humanize the Approvals / lab drawer "diff_description" sentence for DISPLAY ONLY (the raw string
// stays in the experiment record / logs). It embeds raw threshold keys and/or Python list literals,
// e.g. "set max_concession_band -> 0.22 (was 0.15)", "perturb max_concession_band: 0.15 -> 0.22",
// or "reorder discovery_sequence -> ['grade_level', 'goal', …]". We:
//   1. Replace known threshold keys with human labels.
//   2. Clean machine-generated phrasing: "set X -> a (was b)" → "X b → a"; "perturb X: b -> a" →
//      "X b → a"; "reorder X -> [a, b, …]" → "New sequence: A, B, …" (title-cased, no brackets).
//   3. Normalize ASCII "->" to "→".
//   4. Catch-all: any remaining snake_case token → spaced words.
// CB-65: handles the Python-list reorder form so no `-> [` / snake_case slot names render raw.
// Idempotent.
export function humanizeDiffDescription(desc: string | null | undefined): string {
  if (!desc) return '';
  let out = desc;
  // 1. Replace each known raw threshold key (whole-word) with its human label, longest-first.
  for (const key of Object.keys(THRESHOLD_LABELS).sort((a, b) => b.length - a.length)) {
    out = out.replace(new RegExp(`\\b${key}\\b`, 'g'), THRESHOLD_LABELS[key]);
  }
  // 2a. Reorder form: "reorder <dim> -> ['a', 'b', …]" → "New sequence: A, B, …"
  const reorderMatch = /^reorder\s+[a-z_]+\s*->\s*(\[.+\])\s*$/i.exec(out);
  if (reorderMatch) {
    // Parse the Python list literal: extract quoted strings.
    const items = [...reorderMatch[1].matchAll(/['"]([^'"]+)['"]/g)].map((m) =>
      m[1].replace(/_/g, ' ')
    );
    if (items.length > 0) {
      const labelled = items.map((s) => s.charAt(0).toUpperCase() + s.slice(1));
      return `New sequence: ${labelled.join(', ')}`;
    }
  }
  // 2b. "set X -> a (was b)" → "X b → a"; "perturb X: b -> a" → "X b → a". ASCII "->" → real arrow.
  out = out.replace(/^set\s+(.+?)\s*->\s*([^()]+?)\s*\(was\s*([^)]+)\)\s*$/i, '$1 $3 → $2');
  out = out.replace(/^perturb\s+(.+?):\s*(.+?)\s*->\s*(.+)$/i, '$1 $2 → $3');
  out = out.replace(/->/g, '→');
  // 3. Catch-all: any remaining bare multi-word snake_case token (e.g. an unmapped dimension slug like
  //   "discovery_sequence" in a reorder diff) → spaced words, so no underscore-joined slug renders.
  out = out.replace(/\b[a-z]+(?:_[a-z0-9]+)+\b/g, (s) => s.replace(/_/g, ' '));
  // Capitalize the leading label so it reads as a sentence start.
  return out.charAt(0).toUpperCase() + out.slice(1);
}

// Humanize a gate RATIONALE string for DISPLAY ONLY (the raw rationale stays on the turn record / in
// logs). A rationale leads with an internal gate-name prefix and embeds raw driver enum slugs, e.g.
// "pushiness_cap: bail_risk over cap; backing off pressure…" → "Pushiness cap: walk-away risk over
// cap; backing off pressure…". We (1) translate the leading "<gate_name>:" prefix via GATE_LABELS, then
// (2) replace any remaining known driver slug via the slug map, then (3) drop the Python-style decision
// parenthetical "(tier='callback', trust=0.50, …)" that some gate rationales embed — it is machine-
// readable metadata, not operator text; (4) catch-all any leftover bare snake_case token (Title Case)
// so NO raw slug or gate-name reaches the operator. Idempotent.
// CB-65: step 3 added to strip Python-style kwargs like "tier='trial' (trust=0.70, purchase_intent=0.62)"
// that gates.py embeds in the advance_to_close rationale string — display-only scrub; raw string kept.
export function humanizeRationale(rationale: string | null | undefined): string {
  if (!rationale) return '';
  let out = rationale;
  // 1. Leading gate-name prefix "<gate>: …" → human gate label.
  const m = out.match(/^([a-z][a-z0-9_]*?):\s*/);
  if (m && GATE_LABELS[m[1]]) {
    out = `${GATE_LABELS[m[1]]}: ${out.slice(m[0].length)}`;
  }
  // 2. Known driver enum slugs anywhere in the body → human phrases.
  out = out.replace(RATIONALE_SLUG_RE, (s) => RATIONALE_SLUG_MAP[s] ?? s);
  // 3. CB-65: strip Python-style decision kwargs that gates.py embeds in some rationale strings.
  //    Patterns scrubbed (display only — raw string unchanged on the Turn record / in logs):
  //      - "tier='callback'" / tier="enrollment"  → the tier name in plain English
  //      - "(trust=0.70, purchase_intent=0.62)" parentheticals → dropped (noise to the operator)
  //    The tier name is extracted and surfaced as a plain suffix so the sentence stays coherent.
  //    Example: "advance to close: tier='trial' (trust=0.70, …) — taking initiative"
  //          →  "Advance to close: trial — taking initiative"
  const TIER_NAMES: Record<string, string> = {
    callback: 'callback',
    consultation: 'consultation',
    trial: 'trial',
    enrollment: 'enrollment',
  };
  out = out.replace(/\btier=['"]([a-z]+)['"]/gi, (_, t) => TIER_NAMES[t.toLowerCase()] ?? t);
  // Drop the parenthetical kwargs block (trust=N, purchase_intent=N, …) that follows the tier token.
  out = out.replace(/\s*\([^)]*(?:trust|purchase_intent|urgency|bail_risk|budget_constrained|breakthrough)[^)]*\)/g, '');
  // 4. Catch-all: any remaining bare multi-word snake_case token (e.g. an unmapped "<gate>_name" or a
  //    new driver slug) → spaced words, so no underscore-joined slug ever renders to the operator.
  out = out.replace(/\b[a-z]+(?:_[a-z0-9]+)+\b/g, (s) => s.replace(/_/g, ' '));
  return out;
}

// snake_case / kebab-case -> "Title case words" (first word capitalized, rest lower). Used by the
// dimension + archetype humanizers above.
function titleCase(token: string): string {
  const words = token.replace(/[_-]+/g, ' ').trim().split(/\s+/);
  if (words.length === 0 || words[0] === '') return '';
  return words
    .map((w, i) => (i === 0 ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w.toLowerCase()))
    .join(' ');
}

// CB-65: Commitment-ladder tier int -> human label. Mirrors src/api/labels.LADDER_TIER_LABEL.
// Used by the experiment detail page arm-call rows so "tier 0" / "tier 1" never renders.
const LADDER_TIER_LABELS: Record<number, string> = {
  0: 'No commitment',
  1: 'Callback booked',
  2: 'Consultation booked',
  3: 'Trial booked',
  4: 'Same-call enrollment',
};
export function ladderTierLabel(tier: number | null | undefined): string {
  if (tier == null) return '—';
  return LADDER_TIER_LABELS[tier] ?? `Tier ${tier}`;
}

// CB-65: Translate a raw kb_version slug ("kb_v0", "kb_v1") to a human-readable label ("Knowledge
// base v0"). Strips internal suffixes (__…/-hash) first, then replaces the "kb_v" prefix. Returns the
// cleaned raw string if the pattern does not match (so a future non-standard kb version still renders
// sensibly). Display only — raw kb_version values stay in data fields / tooltips for automation.
export function kbVersionLabel(raw: string | null | undefined): string {
  if (!raw) return '—';
  // Strip any internal experiment suffix (__…) or hash disambiguator (-xxxx).
  const clean = raw.split('__')[0].split('-')[0];
  // "kb_v0" -> "Knowledge base v0", "kb_v1" -> "Knowledge base v1", etc.
  const m = /^kb_v(\d+)$/.exec(clean);
  if (m) return `Knowledge base v${m[1]}`;
  // Fallback: titleize the cleaned slug so no bare snake_case leaks.
  return titleCase(clean) || clean;
}

// CB-65: Humanize the guardrail_reason string from the experiment record for DISPLAY ONLY.
// The raw field embeds Python-style boolean tokens ("challenger_better is False") and internal
// variable names ("challenger_better"). These are scrubbed so no Python identifier or "is False"
// ever renders in the operator UI. The raw field is preserved in the experiment record for logs.
// Examples:
//   "no significant lift: challenger_better is False (delta CI includes 0 — not statistically …)"
//     → "No significant lift — delta CI includes 0, not statistically separated from noise."
//   "the run timed out after 180s — no result recorded" → unchanged (no Python tokens)
export function humanizeGuardrailReason(reason: string | null | undefined): string {
  if (!reason) return '';
  let out = reason;
  // "challenger_better is False" → "no significant lift detected"
  out = out.replace(/challenger_better\s+is\s+False\b/gi, 'no significant lift detected');
  // "challenger_better is True" → "challenger is better"
  out = out.replace(/challenger_better\s+is\s+True\b/gi, 'challenger is better');
  // Clean up any remaining bare "is False" / "is True" Python booleans
  out = out.replace(/\bis\s+False\b/g, 'is not the case');
  out = out.replace(/\bis\s+True\b/g, 'is confirmed');
  // "no significant lift: <detail>" → rewrite as a clean sentence
  out = out.replace(/^no significant lift:\s*/i, 'No significant lift — ');
  // Strip the parenthetical "(delta CI includes 0 — …)" by unwrapping its content
  out = out.replace(/\(([^)]+)\)/g, '$1');
  // Normalize spacing and punctuation artifacts
  out = out.replace(/\s{2,}/g, ' ').replace(/\s+—\s+/g, ' — ').trim();
  // Capitalize
  return out.charAt(0).toUpperCase() + out.slice(1);
}
