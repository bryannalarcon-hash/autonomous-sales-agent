// Shared humanizers for operator-facing text. Internal indices/slugs (the experiment `__<dim>__<n>`
// suffix, short `-hash` suffixes, `DRAFT-`/`exp-` id prefixes, snake_case dimension + archetype slugs)
// must NEVER render in observable output — these turn them into human-readable labels. Mirrors the
// inlined versionLabel logic in DashboardShell.tsx / kpi / versions (kept in sync; do not import from
// those — they keep their own copy). Used by improve/lab, improve/approvals, operate/kpi pages.

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

// snake_case / kebab-case -> "Title case words" (first word capitalized, rest lower). Used by the
// dimension + archetype humanizers above.
function titleCase(token: string): string {
  const words = token.replace(/[_-]+/g, ' ').trim().split(/\s+/);
  if (words.length === 0 || words[0] === '') return '';
  return words
    .map((w, i) => (i === 0 ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w.toLowerCase()))
    .join(' ');
}
