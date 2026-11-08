import Link from "next/link";
import { Download, Star } from "lucide-react";
import { GITHUB_URL, INSTALL_COMMAND, VERSION } from "@/lib/config";
import { getRepoData } from "@/lib/github";
import AmbientMesh from "./AmbientMesh";
import Globe from "./Globe";
import InstallCommand from "./InstallCommand";
import Logo from "./Logo";
import RotatingTitle from "./RotatingTitle";
import styles from "./Hero.module.css";

const NUMBER = new Intl.NumberFormat("en-US");

export default async function Hero() {
  const { stats } = await getRepoData();

  return (
    <section className={styles.hero}>
      <AmbientMesh />
      <Globe />

      <header className={styles.nav}>
        <Link href="/" className={styles.brand} aria-label="InterMux home">
          <Logo size={26} />
          <span>InterMux</span>
        </Link>
        <nav className={styles.links} aria-label="Primary">
          <a href="#features" className={styles.section}>Features</a>
          <a href="#install" className={styles.section}>Install</a>
          <Link href="/docs" className={styles.section}>Docs</Link>
          <a href="#faq" className={styles.section}>FAQ</a>
          <a href={GITHUB_URL} className={styles.github}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z" />
            </svg>
            GitHub
            <span className={styles.stars}>
              <Star size={12} strokeWidth={2.2} aria-hidden="true" />
              {NUMBER.format(stats.stars)}
            </span>
          </a>
        </nav>
      </header>

      <div className={styles.body}>
        <RotatingTitle />
        <p className={styles.lede}>
          Downloads on Ethernet, browser on Wi-Fi, calls on your phone. All at once, on Linux.
        </p>
        <div className={styles.ctas}>
          <a href="#install" className="btn btn-primary">
            <Download size={17} strokeWidth={2.2} aria-hidden="true" />
            Install for Linux
            <small>v{VERSION}</small>
          </a>
          <a href={GITHUB_URL} className="btn">
            <Star size={16} strokeWidth={2} aria-hidden="true" />
            Star on GitHub
            <span className="count">{NUMBER.format(stats.stars)}</span>
          </a>
        </div>
        <InstallCommand command={INSTALL_COMMAND} />
      </div>

      <footer className={styles.facts}>
        <span>For Linux</span>
        <span>Version {VERSION}, beta</span>
        <span>GUI and command line</span>
        <span>MIT license</span>
      </footer>
    </section>
  );
}
