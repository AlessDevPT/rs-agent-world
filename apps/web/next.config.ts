import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The frontend is a pure API consumer: no database driver, no server-side
  // coupling to apps/api beyond HTTP (goal G7 in docs/01-ARCHITECTURE.md).
  env: {},
  eslint: {
    // Linting runs as its own CI job (`npm run lint`), so a build does not
    // silently become a second, differently-configured lint pass.
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
