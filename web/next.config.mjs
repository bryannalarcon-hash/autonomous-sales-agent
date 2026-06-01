// Next.js config for the auto-sales-agent web console (U13 demo + U15/U16 operator UI).
// Minimal by design: strict React mode only. The bare "/" now renders a real landing page
// (web/app/page.tsx) that orients a visitor and routes them to the demo or the operator dashboard —
// it is NO LONGER redirected to /demo.
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
};

export default nextConfig;
