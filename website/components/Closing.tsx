import { Download, Star } from "lucide-react";
import { GITHUB_URL } from "@/lib/config";
import { getRepoData } from "@/lib/github";
import Logo from "./Logo";
import styles from "./Closing.module.css";

const NUMBER = new Intl.NumberFormat("en-US");

export default async function Closing() {
  const { stats } = await getRepoData();

  return (
    <section className={styles.closing} aria-labelledby="closing-title">
      <div className={styles.glow} aria-hidden="true" />
      <Logo size={56} />
      <h2 id="closing-title" className={styles.title}>
        Give every app its own lane.
      </h2>
      <div className={styles.ctas}>
        <a href="#install" className="btn btn-primary">
          <Download size={17} strokeWidth={2.2} aria-hidden="true" />
          Install InterMux
        </a>
        <a href={GITHUB_URL} className="btn">
          <Star size={16} strokeWidth={2} aria-hidden="true" />
          Star on GitHub
          <span className="count">{NUMBER.format(stats.stars)}</span>
        </a>
      </div>
    </section>
  );
}
