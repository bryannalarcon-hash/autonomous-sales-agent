// Demo-mode layout — brings the prospect-facing /demo surface into the SAME dark "Cadence" design
// system the operator routes use (web/app/cadence.css + the three brand fonts: Inter body / Poppins
// display / JetBrains Mono numeric), so /demo stops rendering in a plain light Tailwind theme.
// Unlike /operate + /improve it does NOT mount the full DashboardShell (nav rail + top bar is
// operator chrome, not a prospect experience): it only loads cadence.css + the fonts the same way
// (runtime Google Fonts <link>; family names match cadence.css) and sets prospect metadata. The
// page itself renders inside the `.cadence` scope so the dark aurora + tokens apply.
import type { Metadata } from 'next';
import '../cadence.css';

const FONTS_HREF =
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Poppins:wght@500;600;700;800&display=swap';

export const metadata: Metadata = {
  title: 'Cadence — Talk to our tutoring advisor',
  description: 'Speak with our AI tutoring advisor by text or voice.',
};

export default function DemoLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link rel="stylesheet" href={FONTS_HREF} />
      {children}
    </>
  );
}
