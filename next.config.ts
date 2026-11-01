import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // Short install URLs: `curl -fsSL https://<site>/install | sh`. The scripts live in the main repo
  // under get/; these are temporary redirects so the target can move later.
  async redirects() {
    const raw = "https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get";
    return [
      { source: "/install", destination: `${raw}/install.sh`, permanent: false },
      { source: "/install.sh", destination: `${raw}/install.sh`, permanent: false },
      { source: "/install.ps1", destination: `${raw}/install.ps1`, permanent: false },
    ];
  },
};

export default nextConfig;
