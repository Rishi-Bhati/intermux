import { Plus } from "lucide-react";
import { GITHUB_URL } from "@/lib/config";
import styles from "./Faq.module.css";

export const faqs: Array<{ q: string; a: string }> = [
  {
    q: "Is it a VPN?",
    a: "No. Nothing is encrypted or tunnelled. It only picks which of your own connections each app uses.",
  },
  {
    q: "What do I need?",
    a: "Linux with systemd, and two or more connections: Ethernet, Wi-Fi, a tethered phone, a second adapter.",
  },
  {
    q: "Do my apps run as root?",
    a: "Never. A small polkit-gated helper sets up the network; your apps run as you, with your profile.",
  },
  {
    q: "Can I move an app that is already open?",
    a: "Yes, with intermux adopt. It keeps running; open connections reconnect on the new route.",
  },
  {
    q: "How do I undo it?",
    a: "intermux reset removes everything it set up. The installer with --uninstall removes InterMux too.",
  },
];

export default function Faq() {
  return (
    <section id="faq" className={styles.section} aria-labelledby="faq-title">
      <div className={styles.inner}>
        <div className={styles.head}>
          <span className="eyebrow">FAQ</span>
          <h2 id="faq-title" className="section-title">
            Questions, answered.
          </h2>
          <p className="section-lede">
            More? <a href={`${GITHUB_URL}/issues`}>Ask on GitHub</a>.
          </p>
        </div>

        <div className={styles.list}>
          {faqs.map((f, i) => (
            <details key={f.q} className={styles.item} open={i === 0}>
              <summary>
                <span>{f.q}</span>
                <Plus size={18} strokeWidth={2} aria-hidden="true" className={styles.plus} />
              </summary>
              <p>{f.a}</p>
            </details>
          ))}
        </div>
      </div>
    </section>
  );
}
