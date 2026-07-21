import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Allow preview subdomains for Keystone preview system
  allowedDevOrigins: [
    "preview.localhost",
    "*.preview.localhost",
    "preview.jboxai.com",
    "*.preview.jboxai.com",
  ],
  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "images.unsplash.com",
        pathname: "/**",
      },
    ],
  },
};

export default nextConfig;
