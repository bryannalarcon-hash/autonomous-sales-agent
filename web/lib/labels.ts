// Shared humanizers for operator-facing text. Internal indices/slugs (the experiment `__<dim>__<n>`
// suffix, short `-hash` suffixes, `DRAFT-`/`exp-` id prefixes, snake_case dimension/archetype/
// population/threshold slugs, and the gate-rationale gate-names + driver enum slugs) must NEVER render
// in observable output — these turn them into human-readable labels (versionLabel / dimensionLabel /
// archetypeLabel / populationLabel / humanizeDiffDescription / humanizeRationale). Mirrors the inlined
// versionLabel logic in DashboardShell.tsx / kpi / versions (kept in sync; do not import from those —
// they keep their own copy). Used by improve/lab, improve/approvals, operate/kpi, operate/review pages.
// CB-04: humanizeDiffDescription scrubs raw threshold keys (max_concession_band → "Pricing concession
// band") out of the approvals diff sentence, and humanizeRationale scrubs gate-names + driver enum
// slugs (pushiness_cap, bail_risk, …) out of the review-page `.rv-rat` rationale — display only; the
// backend logs/records keep the raw slug.

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

// Humanize the Approvals "diff_description" sentence for DISPLAY ONLY (the raw string stays in the
// experiment record / logs). It embeds a raw threshold key, e.g. "set max_concession_band -> 0.22 (was
// 0.15)" or "perturb max_concession_band: 0.15 -> 0.22"; we replace any known threshold key with its
// human label and drop the leading verb so it reads as a clean sentence ("Pricing concession band 0.15
// → 0.22"). Unknown / non-threshold descriptions (e.g. a reorder of discovery slots) are left intact
// after the same threshold-key scrub, so nothing snake_case leaks. Idempotent.
export function humanizeDiffDescription(desc: string | null | undefined): string {
  if (!desc) return '';
  let out = desc;
  // Replace each known raw threshold key (whole-word) with its human label, longest-first.
  for (const key of Object.keys(THRESHOLD_LABELS).sort((a, b) => b.length - a.length)) {
    out = out.replace(new RegExp(`\\b${key}\\b`, 'g'), THRESHOLD_LABELS[key]);
  }
  // Tidy the machine-generated phrasing into something readable: "set X -> a (was b)" → "X b → a";
  // "perturb X: b -> a" → "X b → a". ASCII "->" becomes a real arrow.
  out = out.replace(/^set\s+(.+?)\s*->\s*([^()]+?)\s*\(was\s*([^)]+)\)\s*$/i, '$1 $3 → $2');
  out = out.replace(/^perturb\s+(.+?):\s*(.+?)\s*->\s*(.+)$/i, '$1 $2 → $3');
  out = out.replace(/->/g, '→');
  // Catch-all: any remaining bare multi-word snake_case token (e.g. an unmapped dimension slug like
  //   "discovery_sequence" in a reorder diff) → spaced words, so no underscore-joined slug renders.
  out = out.replace(/\b[a-z]+(?:_[a-z0-9]+)+\b/g, (s) => s.replace(/_/g, ' '));
  // Capitalize the leading label so it reads as a sentence start.
  return out.charAt(0).toUpperCase() + out.slice(1);
}

// Humanize a gate RATIONALE string for DISPLAY ONLY (the raw rationale stays on the turn record / in
// logs). A rationale leads with an internal gate-name prefix and embeds raw driver enum slugs, e.g.
// "pushiness_cap: bail_risk over cap; backing off pressure…" → "Pushiness cap: walk-away risk over
// cap; backing off pressure…". We (1) translate the leading "<gate_name>:" prefix via GATE_LABELS, then
// (2) replace any remaining known driver slug via the slug map, then (3) catch-all any leftover bare
// snake_case token (Title Case) so NO raw slug or gate-name reaches the operator. Idempotent.
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
  // 3. Catch-all: any remaining bare multi-word snake_case token (e.g. an unmapped "<gate>_name" or a
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
