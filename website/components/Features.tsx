import { ArrowRightLeft, Eraser, Globe, PanelsTopLeft, Radar, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";
import styles from "./Features.module.css";

const features: Array<{ icon: ReactNode; title: string; tag: string }> = [
  { icon: <ArrowRightLeft size={20} strokeWidth={1.8} />, title: "Move apps that are already open", tag: "intermux adopt" },
  { icon: <Radar size={20} strokeWidth={1.8} />, title: "See the public IP of each link", tag: "intermux verify" },
  { icon: <Globe size={20} strokeWidth={1.8} />, title: "Keeps your browser profile", tag: "--separate-profile" },
  { icon: <PanelsTopLeft size={20} strokeWidth={1.8} />, title: "A window, or the terminal", tag: "intermux --help" },
  { icon: <Eraser size={20} strokeWidth={1.8} />, title: "Cleans up when the app closes", tag: "intermux reset" },
  { icon: <ShieldCheck size={20} strokeWidth={1.8} />, title: "Your apps never run as root", tag: "polkit" },
];

export default function Features() {
  return (
    <section id="features" className={styles.section} aria-labelledby="features-title">
      <span className="eyebrow">Features</span>
      <h2 id="features-title" className="section-title">
        Small tool. Careful details.
      </h2>

      <ul className={styles.grid}>
        {features.map((f) => (
          <li key={f.title} className={styles.item}>
            <span className={styles.icon} aria-hidden="true">
              {f.icon}
            </span>
            <h3>{f.title}</h3>
            <code className={styles.tag}>{f.tag}</code>
          </li>
        ))}
      </ul>
    </section>
  );
}
