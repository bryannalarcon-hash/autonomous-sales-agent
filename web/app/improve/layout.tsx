// Improve-mode layout (U16) — the thin layout that reuses the SAME shared DashboardShell + Cadence
// design system (cadence.css) the Operate layout uses, so the 4 Improve pages (Experiment Lab,
// Approval Queue, KB/Playbook Editor, Version History) drop into the identical nav-rail + top-bar
// chrome. Loads the three brand fonts (Inter body / Poppins display / JetBrains Mono numeric) the
// same way as web/app/operate/layout.tsx (runtime Google Fonts <link>; family names match
// cadence.css). The shell is mode-agnostic — its NAV registry already carries the Improve
// destinations — so this layout adds no nav logic of its own; pages render inside .main as children.
import type { Metadata } from 'next';
import '../cadence.css';
import { DashboardShell } from '@/components/cadence/DashboardShell';

const FONTS_HREF =
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Poppins:wght@500;600;700;800&display=swap';

export const metadata: Metadata = {
  title: 'Cadence — Improve',
  description: 'Operator console: experiment lab, approval queue, KB/playbook editor, version history.',
};

export default function ImproveLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link rel="stylesheet" href={FONTS_HREF} />
      <DashboardShell>{children}</DashboardShell>
    </>
  );
}
