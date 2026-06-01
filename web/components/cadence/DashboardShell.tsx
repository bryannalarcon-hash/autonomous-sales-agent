// The SHARED Cadence dashboard shell — left nav rail (two modes: Operate / Improve + their
// destinations) + global top bar — that wraps BOTH modes. U15 (Operate) builds it; U16 (Improve)
// reuses it unchanged: add Improve pages under /improve/* and they drop into this chrome. The route
// registry (NAV) is the single source of truth for label/title/icon/badge per destination and maps
// 1:1 to App Router paths. Active nav state derives from the pathname (the Call Review detail keeps
// "Calls" highlighted). Operator-facing titles are human-readable — NO internal P-id ever renders.
// The top bar carries the champion-version chip, persona chip, and the Sandbox/Live environment
// toggle (local UI state). Pure chrome: pages render as {children} inside .main.
'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useState, type ReactNode } from 'react';
import { Icon, type IconName } from './Icon';

interface NavDest {
  href: string; // where the rail item navigates TODAY (real page, or the /improve placeholder)
  target?: string; // the dedicated route U16 will split this into (Improve items only)
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
  { href: '/operate/escalations', label: 'Escalations', title: 'Escalation Queue', icon: 'alert', group: 'operate', badge: 3 },
  // Improve destinations live in the rail NOW so the two-mode IA is complete, but their dedicated
  // pages are U16's deliverable. Until then every Improve item points at the existing /improve
  // placeholder (no broken links); `target` records the route U16 will split it into. When U16 adds
  // /improve/{lab,approvals,kb,versions}, swap each `href` to its `target` — the rest of the shell
  // (active-state, mode switch, badges) already keys off these entries unchanged.
  { href: '/improve', target: '/improve/lab', label: 'Experiment Lab', title: 'Experiment Lab', icon: 'flask', group: 'improve' },
  { href: '/improve', target: '/improve/approvals', label: 'Approvals', title: 'Approval Queue', icon: 'badge', group: 'improve', badge: 2, amber: true },
  { href: '/improve', target: '/improve/kb', label: 'KB / Playbook', title: 'KB / Playbook Editor', icon: 'book', group: 'improve' },
  { href: '/improve', target: '/improve/versions', label: 'Versions', title: 'Version History', icon: 'branch', group: 'improve' },
];

// The Call Review detail screen (no rail entry of its own) keeps "Calls" highlighted + shows a
// breadcrumb back to the list, per the handoff §86/§90.
const REVIEW_PREFIX = '/operate/review';

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
  return (
    <>
      <Link href="/improve" className="gctl" title="Champion version — open Version History (U16)">
        <Icon name="shield" size={15} />
        <span>
          Champion <b>v12</b>
        </span>
        <span className="muted">kb-37</span>
        <Icon name="chevDown" className="gctl-chev" />
      </Link>
      <div className="gctl" title="Active persona / voice">
        <Icon name="mic" size={15} />
        <span>
          <b>Ava</b> · Warm-Direct
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
  const router = useRouter();
  const isReview = pathname.startsWith(REVIEW_PREFIX);

  // Active destination: the longest NAV href that prefixes the path; review maps to Calls.
  const activeHref = isReview
    ? '/operate/calls'
    : NAV.map((n) => n.href)
        .filter((h) => pathname === h || pathname.startsWith(`${h}/`))
        .sort((a, b) => b.length - a.length)[0] ?? '';

  const current = NAV.find((n) => n.href === activeHref);
  const mode: 'operate' | 'improve' = current?.group ?? (pathname.startsWith('/improve') ? 'improve' : 'operate');
  const title = isReview ? 'Call Review' : current?.title ?? 'Operate';
  const showLivePill = !isReview && current?.live;

  const operate = NAV.filter((n) => n.group === 'operate');
  const improve = NAV.filter((n) => n.group === 'improve');

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

          {/* Mode switch: the active segment is raised; switching mode jumps to that mode's first
              page (Operate→Live, Improve→Experiment Lab), per the handoff §81. */}
          <div className="mode">
            <button className={mode === 'operate' ? 'on' : ''} onClick={() => router.push('/operate/live')}>
              <Icon name="broadcast" size={15} />
              Operate
            </button>
            <button className={mode === 'improve' ? 'on' : ''} onClick={() => router.push('/improve')}>
              <Icon name="flask" size={15} />
              Improve
            </button>
          </div>

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
