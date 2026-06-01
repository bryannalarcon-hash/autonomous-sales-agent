// Root layout for the shared web app (demo console now; operator /operate + /improve later in
// U15/U16). Imports global Tailwind styles and sets app-wide metadata. Every route renders inside
// this shell, so it stays neutral — route-level chrome lives in each page.
import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Sales Agent Console',
  description: 'Demo console for the autonomous voice sales agent (text + LiveKit web voice).',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
