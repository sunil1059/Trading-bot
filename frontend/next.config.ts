import type { NextConfig } from "next";

// Static export: the FastAPI backend serves frontend/out on the same origin,
// so cookies stay same-origin and no CORS is needed.
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default nextConfig;
