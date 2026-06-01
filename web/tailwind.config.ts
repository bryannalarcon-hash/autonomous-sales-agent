// Tailwind config for the demo/operator console. Scans the App Router tree + components for
// class usage. Styling intent is clean + minimal (this is the demo surface, not the polished
// operator dashboard, which gets its own design handoff in U15/U16) — so the theme stays default.
import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './hooks/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {},
  },
  plugins: [],
};

export default config;
