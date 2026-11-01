import Closing from "@/components/Closing";
import Faq, { faqs } from "@/components/Faq";
import Features from "@/components/Features";
import Footer from "@/components/Footer";
import Hero from "@/components/Hero";
import Install from "@/components/Install";
import Story from "@/components/Story";
import Switchboard from "@/components/Switchboard";
import { GITHUB_URL, TARBALL_URL, VERSION } from "@/lib/config";

// Structured data, so search engines can show InterMux as a free Linux app with its FAQ.
const jsonLd = [
  {
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    name: "InterMux",
    description:
      "A network manager for Linux that binds each application to the network interface you choose: Ethernet, Wi-Fi or a tethered phone.",
    applicationCategory: "UtilitiesApplication",
    operatingSystem: "Linux",
    softwareVersion: VERSION,
    license: "https://opensource.org/licenses/MIT",
    downloadUrl: TARBALL_URL,
    codeRepository: GITHUB_URL,
    offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
  },
  {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: faqs.map((f) => ({
      "@type": "Question",
      name: f.q,
      acceptedAnswer: { "@type": "Answer", text: f.a },
    })),
  },
];

export default function Home() {
  return (
    <main>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c") }}
      />
      <Hero />
      <div className="grid-texture">
        <Story />
        <Switchboard />
        <Features />
        <Install />
        <Faq />
        <Closing />
        <Footer />
      </div>
    </main>
  );
}
