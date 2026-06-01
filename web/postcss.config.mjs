// PostCSS pipeline for Tailwind — drives the utility-class styling used across the console.
// Tailwind first, then autoprefixer for vendor-prefix coverage on the production build.
const config = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};

export default config;
