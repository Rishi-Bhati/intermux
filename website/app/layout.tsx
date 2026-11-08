import type { Metadata, Viewport } from "next";
import { JetBrains_Mono, Schibsted_Grotesk } from "next/font/google";
import { SITE_URL } from "@/lib/config";
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

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
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
