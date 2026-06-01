// Shared humanizers for operator-facing text. Internal indices/slugs (the experiment `__<dim>__<n>`
// suffix, short `-hash` suffixes, `DRAFT-`/`exp-` id prefixes, snake_case dimension/archetype/
// population slugs) must NEVER render in observable output — these turn them into human-readable
// labels (versionLabel / dimensionLabel / archetypeLabel / populationLabel). Mirrors the inlined
// versionLabel logic in DashboardShell.tsx / kpi / versions (kept in sync; do not import from those —
// they keep their own copy). Used by improve/lab, improve/approvals, operate/kpi pages.

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

// snake_case / kebab-case -> "Title case words" (first word capitalized, rest lower). Used by the
// dimension + archetype humanizers above.
function titleCase(token: string): string {
  const words = token.replace(/[_-]+/g, ' ').trim().split(/\s+/);
  if (words.length === 0 || words[0] === '') return '';
  return words
    .map((w, i) => (i === 0 ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w.toLowerCase()))
    .join(' ');
}
