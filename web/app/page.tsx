// Root landing page ("/") — the front door for the autonomous voice AI sales agent built for online
// tutoring (Nerdy). Orients a first-time visitor (what Cadence is, what the agent does) and offers the
// two real entry points: "Try the demo" -> /demo (the prospect call console) and "Operator dashboard"
// -> /operate (live monitor, KPIs, improvement loop). Self-contained chrome: it loads the Cadence
// design system (cadence.css) + brand fonts the same way the Operate/Improve layouts do, and renders
// inside the `.cadence` scope so it reuses the existing tokens/components — no new visual language.
// Replaces the old "/" -> /demo redirect (removed from next.config.mjs).
import Link from 'next/link';
import './cadence.css';
import { Icon } from '@/components/cadence/Icon';

const FONTS_HREF =
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Poppins:wght@500;600;700;800&display=swap';

export const metadata = {
  title: 'Cadence — Autonomous voice sales agent',
  description:
    'An autonomous voice AI sales agent for online tutoring (Nerdy): it runs prospect calls end to ' +
    'end, and the Cadence operator console monitors, reviews, and improves it.',
};

// What the agent does, in plain terms — three short value points for the hero.
const CAPABILITIES: { icon: Parameters<typeof Icon>[0]['name']; title: string; body: string }[] = [
  {
    icon: 'phone',
    title: 'Runs the whole call',
    body: 'Greets the prospect, discovers needs, handles objections, and closes to a next commitment — by voice or text.',
  },
  {
    icon: 'shield',
    title: 'Knows when to hand off',
    body: 'A gated decision policy escalates pricing concessions and low-confidence moments to a human instead of guessing.',
  },
  {
    icon: 'seedling',
    title: 'Improves itself',
    body: 'Every call is graded and fed back into a champion-vs-challenger loop, so the playbook keeps getting better.',
  },
];

export default function LandingPage() {
  return (
    <>
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link rel="stylesheet" href={FONTS_HREF} />
      <div className="cadence">
        <main
          style={{
            minHeight: '100vh',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            padding: '48px 24px',
            background: 'var(--bg-stack)',
          }}
        >
          <div style={{ width: '100%', maxWidth: 920, display: 'flex', flexDirection: 'column', gap: 32 }}>
            {/* brand + headline */}
            <div className="col" style={{ gap: 18, alignItems: 'center', textAlign: 'center' }}>
              <div className="brand" style={{ padding: 0 }}>
                <div className="brand-mark">
                  <Icon name="pulse" size={19} sw={2.4} />
                </div>
                <div className="brand-word">Cadence</div>
              </div>
              <span className="tag accent dot tag-lg">Autonomous voice AI sales agent</span>
              <h1
                style={{
                  fontFamily: 'var(--font-display)',
                  fontWeight: 700,
                  fontSize: 'clamp(30px, 5vw, 46px)',
                  letterSpacing: '-0.03em',
                  lineHeight: 1.08,
                  maxWidth: 760,
                }}
              >
                An AI advisor that runs tutoring sales calls — end to end, on its own.
              </h1>
              <p
                className="muted"
                style={{ fontSize: 16, lineHeight: 1.6, maxWidth: 620, color: 'var(--text-2)' }}
              >
                Built for online tutoring (Nerdy), the agent talks to prospects by voice or text, qualifies
                and closes them, and escalates to a human when it should. Cadence is the operator console
                that watches it live, reviews every call, and runs the loop that keeps improving it.
              </p>
            </div>

            {/* two entry points */}
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
                gap: 16,
              }}
            >
              <Link
                href="/demo"
                className="card card-pad"
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 12,
                  textDecoration: 'none',
                  color: 'inherit',
                  borderColor: 'var(--accent-border)',
                }}
              >
                <div
                  className="avatar accent"
                  style={{ width: 42, height: 42, borderRadius: 12, fontSize: 0 }}
                >
                  <Icon name="phone" size={20} />
                </div>
                <div className="col" style={{ gap: 4 }}>
                  <h3 style={{ fontSize: 17 }}>Try the demo</h3>
                  <p className="muted" style={{ fontSize: 13.5, lineHeight: 1.5, color: 'var(--text-2)' }}>
                    Step into a prospect&apos;s seat and talk to the agent in the call console — text or
                    live voice, after a quick consent gate.
                  </p>
                </div>
                <span className="btn btn-primary" style={{ marginTop: 'auto', alignSelf: 'flex-start' }}>
                  Open the call console
                  <Icon name="arrowR" size={15} />
                </span>
              </Link>

              <Link
                href="/operate"
                className="card card-pad"
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 12,
                  textDecoration: 'none',
                  color: 'inherit',
                }}
              >
                <div
                  className="avatar"
                  style={{
                    width: 42,
                    height: 42,
                    borderRadius: 12,
                    fontSize: 0,
                    background: 'var(--surface-2)',
                    color: 'var(--text)',
                  }}
                >
                  <Icon name="grid" size={20} />
                </div>
                <div className="col" style={{ gap: 4 }}>
                  <h3 style={{ fontSize: 17 }}>Operator dashboard</h3>
                  <p className="muted" style={{ fontSize: 13.5, lineHeight: 1.5, color: 'var(--text-2)' }}>
                    Monitor live calls, review transcripts and KPIs, triage escalations, and run the
                    improvement loop in the Cadence console.
                  </p>
                </div>
                <span className="btn btn-ghost" style={{ marginTop: 'auto', alignSelf: 'flex-start' }}>
                  Open the dashboard
                  <Icon name="arrowR" size={15} />
                </span>
              </Link>
            </div>

            {/* capability strip */}
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
                gap: 14,
              }}
            >
              {CAPABILITIES.map((c) => (
                <div key={c.title} className="card solid card-pad" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <Icon name={c.icon} size={18} style={{ color: 'var(--accent)' }} />
                  <div className="b" style={{ fontSize: 14 }}>
                    {c.title}
                  </div>
                  <p className="faint" style={{ fontSize: 12.5, lineHeight: 1.5, color: 'var(--text-3)' }}>
                    {c.body}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </main>
      </div>
    </>
  );
}
