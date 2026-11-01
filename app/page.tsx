import Closing from "@/components/Closing";
import Footer from "@/components/Footer";
import Hero from "@/components/Hero";
import Story from "@/components/Story";

export default function Home() {
  return (
    <main>
      <Hero />
      <div className="grid-texture">
        <Story />
        <Closing />
        <Footer />
      </div>
    </main>
  );
}
