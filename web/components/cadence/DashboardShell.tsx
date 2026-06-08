// The SHARED Cadence dashboard shell — left nav rail (the full Operate + Improve nav groups) + global
// top bar — that wraps BOTH areas. U15 (Operate) builds it; U16 (Improve) reuses it unchanged: the
// Improve pages live at /improve/{lab,approvals,kb,versions} and drop into this chrome. The route
// registry (NAV) is the single source of truth for label/title/icon/badge per destination and maps
// 1:1 to App Router paths. Active nav state derives from the pathname (the Call Review detail keeps
// "Calls" highlighted). Operator-facing titles are human-readable — NO internal P-id ever renders.
// CB-11: the redundant Operate/Improve mode toggle was removed (both full nav groups are always shown).
// The top bar carries a "Talk to the agent" link → /demo (CB-16, round-trips with /demo's dashboard
// link), the champion-version chip (REAL champion version humanized + kb_version from /api/versions,
// BOTH with their internal `__…`/`-hash` suffix stripped via versionLabel/kbVersionTag — never a raw
// index or fabricated id), the persona chip (the REAL agent persona from config — "Alex",
// warm-consultative), and the Sandbox/Live environment toggle (local UI state). The nav-rail badges
// (Escalations/Approvals counts) REVALIDATE without a refresh — a ~15s poll + focus/visibility refetch
// in useNavBadges (CB-17). The KB nav entry's title reads "KB / Playbook" (no "Editor") since that
// page is read-only. Pure chrome: pages render as {children} inside .main.
'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState, type ReactNode } from 'react';
import { fetchVersions, fetchApprovals } from '@/lib/improve-api';
import { fetchEscalations } from '@/lib/operate-api';
import { kbVersionLabel } from '@/lib/labels';
import { Icon, type IconName } from './Icon';

// The agent's REAL persona, from the champion config (src/config/versions/champion_v0.yaml:
// persona.name = "Alex", style = "warm-consultative"). The recorded demo names the agent "Alex"; the
// previous "Ava · Warm-Direct" was fabricated. No persona API endpoint exists, so these mirror config.
const PERSONA_NAME = 'Alex';
const PERSONA_STYLE = 'Warm-Consultative';

// Strip the internal suffixes from a raw version id for display: the experiment delta suffix
// ("champion_v0__playbooks_discovery_sequence__7" -> "champion_v0") and any short hash
// ("v1-f3798d7a" -> "v1"), then humanize the prefix ("champion_v0" -> "Champion v0"). The dropped
// parts are internal indices and must not render to the operator (the change is shown via the
// human-readable dimension_label tag elsewhere).
function versionLabel(raw: string): string {
  let base = raw.split('__')[0]; // drop the experiment dimension/seq suffix
  base = base.split('-')[0]; // drop a short hash suffix
  if (base.startsWith('champion_')) return `Champion ${base.slice('champion_'.length)}`;
  return base;
}

// CB-65: kbVersionTag is replaced by kbVersionLabel (from lib/labels) which translates the raw
// kb_version slug ("kb_v0") to a human-readable form ("Knowledge base v0"). Kept as a thin alias
// for any call-sites that haven't been updated yet.
function kbVersionTag(raw: string): string {
  return kbVersionLabel(raw);
}

interface NavDest {
  href: string; // the App Router path the rail item navigates to (1:1 with a page)
  label: string; // operator-facing rail label
  title: string; // operator-facing top-bar title
  icon: IconName;
  group: 'operate' | 'improve';
  live?: boolean; // pulsing live-dot
  badge?: number; // count badge
  amber?: boolean; // amber (warn) badge instead of danger-red
}

// Destinations, in rail order. Operate is built in U15; Improve points at the U16 placeholder pages.
const NAV: NavDest[] = [
  { href: '/operate/live', label: 'Live', title: 'Live Call Monitor', icon: 'broadcast', group: 'operate', live: true },
  { href: '/operate/calls', label: 'Calls', title: 'Calls List', icon: 'list', group: 'operate' },
  { href: '/operate/kpi', label: 'KPI Views', title: 'KPI Views', icon: 'chart', group: 'operate' },
  // The escalations/approvals badges are bound to REAL counts at runtime (see useNavBadges) — no
  // hardcoded literal (the old `badge: 3` was stale; the queue actually held ~116 unreviewed).
  { href: '/operate/escalations', label: 'Escalations', title: 'Escalation Queue', icon: 'alert', group: 'operate' },
  // Improve destinations now point at their dedicated U16 pages under /improve/* (the `target` the
  // U15 scaffold recorded is now the live `href`). The rest of the shell (active-state, mode switch,
  // badges) keys off these entries unchanged.
  { href: '/improve/lab', label: 'Experiment Lab', title: 'Experiment Lab', icon: 'flask', group: 'improve' },
  { href: '/improve/approvals', label: 'Approvals', title: 'Approval Queue', icon: 'badge', group: 'improve', amber: true },
  // m4 FIX: the KB page is read-only, so the top-bar title drops "Editor" (label was already "KB / Playbook").
  { href: '/improve/kb', label: 'KB / Playbook', title: 'KB / Playbook', icon: 'book', group: 'improve' },
  { href: '/improve/versions', label: 'Versions', title: 'Version History', icon: 'branch', group: 'improve' },
  { href: '/improve/fidelity', label: 'Fidelity', title: 'Replay Fidelity', icon: 'layers', group: 'improve' },
];

// The Call Review detail screen (no rail entry of its own) keeps "Calls" highlighted + shows a
// breadcrumb back to the list, per the handoff §86/§90.
const REVIEW_PREFIX = '/operate/review';

// How often to re-poll the badge counts so they don't go stale between explicit refreshes (CB-17).
const BADGE_POLL_MS = 15000;

// Bind the nav rail's count badges to REAL backend counts (escalations: unreviewed; approvals:
// pending) rather than hardcoded literals. Non-fatal: a failed/empty fetch just shows no badge.
// CB-17: the counts REVALIDATE without a full refresh — a ~15s interval poll PLUS a refetch whenever
// the tab regains focus / becomes visible. So after an operator rejects an approval or reviews an
// escalation (on its own page), the badge here catches up within a poll / on the next focus, instead
// of staying stale until the shell remounts. All timers/listeners are cleaned up on unmount.
function useNavBadges(): Record<string, number> {
  const [badges, setBadges] = useState<Record<string, number>>({});
  useEffect(() => {
    let cancelled = false;

    const refetch = () => {
      Promise.allSettled([fetchEscalations(), fetchApprovals()]).then(([esc, app]) => {
        if (cancelled) return;
        const next: Record<string, number> = {};
        if (esc.status === 'fulfilled') {
          next['/operate/escalations'] = esc.value.counts?.unreviewed ?? esc.value.count ?? 0;
        }
        if (app.status === 'fulfilled') next['/improve/approvals'] = app.value.count ?? 0;
        setBadges(next);
      });
    };

    refetch(); // initial load on mount
    const interval = setInterval(refetch, BADGE_POLL_MS);

    // Refetch when the operator returns to this tab (after acting on the approvals/escalations page,
    // or a background change) — visibilitychange covers tab switches, focus covers window refocus.
    const onVisible = () => {
      if (document.visibilityState === 'visible') refetch();
    };
    window.addEventListener('focus', refetch);
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      cancelled = true;
      clearInterval(interval);
      window.removeEventListener('focus', refetch);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, []);
  return badges;
}

function NavItem({ dest, active }: { dest: NavDest; active: boolean }) {
  return (
    <Link href={dest.href} className={`nav-item${active ? ' on' : ''}`}>
      <Icon name={dest.icon} size={18} />
      <span>{dest.label}</span>
      {dest.live ? (
        <span className="live-dot" />
      ) : dest.badge ? (
        <span className={`nav-badge${dest.amber ? ' amber' : ''}`}>{dest.badge}</span>
      ) : null}
    </Link>
  );
}

function GlobalControls() {
  const [live, setLive] = useState(false);
  // Real champion version + kb_version from the lineage API (was a hardcoded "v12 / kb-37").
  const [champion, setChampion] = useState<{ version: string; kbVersion: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchVersions()
      .then((res) => {
        if (cancelled) return;
        const champ =
          res.versions.find((v) => v.version === res.champion_version) ??
          res.versions.find((v) => v.is_champion) ??
          null;
        if (champ) setChampion({ version: champ.version, kbVersion: champ.kb_version });
      })
      .catch(() => {
        // Non-fatal: if the lineage can't load, the chip simply omits the version rather than faking one.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      {/* CB-16: round-trip to the speaking/demo console. Mirrors the /demo page's "Operator dashboard"
          .gctl link (chart icon + arrowR chev) so the two surfaces link to each other. Lives in the
          shared shell → present on every dashboard page. */}
      <Link href="/demo" className="gctl" title="Talk to the agent — open the call console">
        <Icon name="phone" size={15} />
        <span>Talk to the agent</span>
        <Icon name="arrowR" className="gctl-chev" />
      </Link>
      <Link href="/improve/versions" className="gctl" title="Champion version — open Version History">
        <Icon name="shield" size={15} />
        <span>
          Champion {champion ? <b>{versionLabel(champion.version)}</b> : <b>—</b>}
        </span>
        {champion ? <span className="muted">{kbVersionTag(champion.kbVersion)}</span> : null}
        <Icon name="chevDown" className="gctl-chev" />
      </Link>
      <div className="gctl" title="Active agent persona / voice">
        <Icon name="mic" size={15} />
        <span>
          <b>{PERSONA_NAME}</b> · {PERSONA_STYLE}
        </span>
      </div>
      <button className={`env${live ? ' live' : ''}`} onClick={() => setLive((v) => !v)} title="Toggle environment">
        <i />
        {live ? 'LIVE' : 'SANDBOX'}
      </button>
    </>
  );
}

export function DashboardShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? '';
  const isReview = pathname.startsWith(REVIEW_PREFIX);

  // Active destination: the longest NAV href that prefixes the path; review maps to Calls.
  const activeHref = isReview
    ? '/operate/calls'
    : NAV.map((n) => n.href)
        .filter((h) => pathname === h || pathname.startsWith(`${h}/`))
        .sort((a, b) => b.length - a.length)[0] ?? '';

  const current = NAV.find((n) => n.href === activeHref);
  const title = isReview ? 'Call Review' : current?.title ?? 'Operate';
  const showLivePill = !isReview && current?.live;

  // Merge real runtime counts onto the static NAV entries (escalations/approvals badges).
  const navBadges = useNavBadges();
  const withBadge = (n: NavDest): NavDest =>
    n.href in navBadges ? { ...n, badge: navBadges[n.href] } : n;
  const operate = NAV.filter((n) => n.group === 'operate').map(withBadge);
  const improve = NAV.filter((n) => n.group === 'improve').map(withBadge);

  return (
    <div className="cadence">
      <div className="app">
        {/* NAV RAIL */}
        <aside className="nav">
          <div className="brand">
            <div className="brand-mark">
              <Icon name="pulse" size={19} sw={2.4} />
            </div>
            <div className="brand-word">Cadence</div>
          </div>

          {/* CB-11: the redundant Operate/Improve mode toggle was removed — both full nav groups
              render below, so the toggle added no reach. The group headers stay as section labels. */}
          <div className="nav-group">Operate</div>
          <div className="nav-list">
            {operate.map((dest) => (
              <NavItem key={dest.href} dest={dest} active={dest.href === activeHref} />
            ))}
          </div>

          <div className="nav-group">Improve</div>
          <div className="nav-list">
            {improve.map((dest) => (
              <NavItem key={dest.href} dest={dest} active={dest.href === activeHref} />
            ))}
          </div>

          <div className="nav-foot">
            <div className="avatar accent">OP</div>
            <div style={{ lineHeight: 1.3 }}>
              <div style={{ fontWeight: 650, fontSize: '13px' }}>Operator</div>
              <div style={{ fontSize: '11px', color: 'var(--text-3)' }}>Solo workspace</div>
            </div>
          </div>
        </aside>

        {/* MAIN */}
        <div className="main">
          <header className="topbar">
            {isReview ? (
              <Link href="/operate/calls" className="crumb">
                <Icon name="arrowR" size={15} style={{ transform: 'rotate(180deg)' }} />
                Calls
              </Link>
            ) : null}
            <div className="top-title">
              {title}
              {showLivePill ? (
                <span className="live-pill">
                  <i />
                  LIVE
                </span>
              ) : null}
            </div>
            <div className="top-spacer" />
            <GlobalControls />
            <div className="vrule" />
            <div className="avatar sm">OP</div>
          </header>
          {children}
        </div>
      </div>
    </div>
  );
}
