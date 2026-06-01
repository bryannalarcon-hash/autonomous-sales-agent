// Operate-mode layout (U15): loads the Cadence design system (cadence.css) + the three brand fonts
// (Inter body / Poppins display / JetBrains Mono numeric) and wraps every /operate/* page in the
// shared DashboardShell (nav rail + top bar). The shell is mode-agnostic, so U16's /improve/* gets
// its own thin layout that reuses the SAME DashboardShell + this CSS. Fonts load via a runtime
// Google Fonts <link> (matching the handoff, which links them) so the production BUILD never needs
// network for font binaries; the family names ("Inter"/"Poppins"/"JetBrains Mono") match cadence.css.
import type { Metadata } from 'next';
import '../cadence.css';
import { DashboardShell } from '@/components/cadence/DashboardShell';

const FONTS_HREF =
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Poppins:wght@500;600;700;800&display=swap';

export const metadata: Metadata = {
  title: 'Cadence — Operate',
  description: 'Operator console: live monitor, call review, KPIs, and escalation triage.',
};

export default function OperateLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link rel="stylesheet" href={FONTS_HREF} />
      <DashboardShell>{children}</DashboardShell>
    </>
  );
}
