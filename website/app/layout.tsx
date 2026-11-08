import type { Metadata, Viewport } from "next";
import { JetBrains_Mono, Schibsted_Grotesk } from "next/font/google";
import "./globals.css";

const sans = Schibsted_Grotesk({
  variable: "--font-sans",
  subsets: ["latin"],
  display: "swap",
});

const mono = JetBrains_Mono({
  variable: "--font-mono",
  subsets: ["latin"],
  display: "swap",
});

const description =
  "InterMux is a free, open-source network manager for Linux. Choose which connection each app uses: downloads on Ethernet, your browser on Wi-Fi, calls on your tethered phone.";

/** SITE_URL on a custom domain, or the URL Vercel gives the project. */
function siteUrl(): URL | undefined {
  const value =
    process.env.SITE_URL ??
    (process.env.VERCEL_PROJECT_PRODUCTION_URL
      ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}`
      : process.env.VERCEL_URL
        ? `https://${process.env.VERCEL_URL}`
        : undefined);
  if (!value) return undefined;
  try {
    return new URL(value.includes("://") ? value : `https://${value}`);
  } catch {
    return undefined;
  }
}

const baseUrl = siteUrl();

export const metadata: Metadata = {
  metadataBase: baseUrl,
  title: "InterMux: pick a network for every app on Linux",
  description,
  applicationName: "InterMux",
  keywords: [
    "InterMux",
    "Linux network manager",
    "per-app routing",
    "bind application to network interface",
    "split tunneling Linux",
    "network namespace",
    "policy routing",
    "use Wi-Fi and Ethernet at the same time",
  ],
  openGraph: {
    type: "website",
    siteName: "InterMux",
    title: "InterMux: pick a network for every app",
    description,
  },
  twitter: {
    card: "summary_large_image",
    title: "InterMux: pick a network for every app",
    description,
  },
};

export const viewport: Viewport = {
  themeColor: "#0c0d0e",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
