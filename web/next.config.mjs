// Next.js config for the auto-sales-agent web console (U13 demo + later U15/U16 operator UI).
// Minimal by design: strict React mode + a redirect so the bare "/" lands on the demo console.
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async redirects() {
    return [{ source: '/', destination: '/demo', permanent: false }];
  },
};

export default nextConfig;
