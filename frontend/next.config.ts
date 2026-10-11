import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // A self-contained server in .next/standalone, for the Docker image (frontend/Dockerfile).
  // Vercel ignores this and deploys the app its own way.
  output: "standalone",
};

export default nextConfig;
