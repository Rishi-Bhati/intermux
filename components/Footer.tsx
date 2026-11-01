import { Download, GitCommitHorizontal, GitFork, Star } from "lucide-react";
import type { ReactNode } from "react";
import { DOCS_URL, GITHUB_URL } from "@/lib/config";
import { getRepoData } from "@/lib/github";
import HistoryChart from "./HistoryChart";
import Logo from "./Logo";
import styles from "./Footer.module.css";

const REPORT_URL =
  "https://reportary.onrender.com/p/nWQbq2dDTlsy89u6XBWG2UV0pz9-vQuR_WnRuU7U7bWUMVi5rqRlwDxhs8j_w6r9/";

const DATE = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const NUMBER = new Intl.NumberFormat("en-US");

function Tile({ href, icon, value, label }: { href: string; icon: ReactNode; value: number; label: string }) {
  return (
    <a href={href} className={styles.tile}>
      <span className={styles.tileIcon}>{icon}</span>
      <span className={styles.tileValue}>{NUMBER.format(value)}</span>
      <span className={styles.tileLabel}>{label}</span>
    </a>
  );
}

export default async function Footer() {
  const data = await getRepoData();
  const file = (path: string) => `${GITHUB_URL}/blob/${data.branch}/${path}`;

  // A real star history when GitHub lets us read it; otherwise the growth of the project in commits.
  const useStars = Boolean(data.starTimes);
  const times = data.starTimes ?? data.commitTimes;

  return (
    <footer className={styles.footer}>
      <div className={styles.inner}>
        <div className={styles.left}>
          <div className={styles.brand}>
            <Logo size={40} />
            <span>InterMux</span>
          </div>
          <p className={styles.tagline}>A network manager that works per app. Free, open source, and beta.</p>

          <a href={GITHUB_URL} className={styles.star}>
            <Star size={16} strokeWidth={2} aria-hidden="true" />
            Star on GitHub
            <span className={styles.starCount}>{NUMBER.format(data.stats.stars)}</span>
          </a>

          <nav className={styles.columns} aria-label="Footer">
            <div className={styles.column}>
              <h3>Project</h3>
              <ul>
                <li>
                  <a href={GITHUB_URL}>GitHub</a>
                </li>
                <li>
                  <a href={`${GITHUB_URL}/releases`}>Releases</a>
                </li>
                <li>
                  <a href={file("CHANGELOG.md")}>Changelog</a>
                </li>
                <li>
                  <a href={DOCS_URL}>Documentation</a>
                </li>
              </ul>
            </div>
            <div className={styles.column}>
              <h3>Help</h3>
              <ul>
                <li>
                  <a href={REPORT_URL}>Report an issue</a>
                </li>
                <li>
                  <a href={file("SECURITY.md")}>Security policy</a>
                </li>
                <li>
                  <a href={file("CONTRIBUTING.md")}>Contributing</a>
                </li>
              </ul>
            </div>
            <div className={styles.column}>
              <h3>More</h3>
              <ul>
                <li>
                  <a href={file("windows/README.md")}>Windows edition</a>
                </li>
                <li>
                  <a href={file("license")}>MIT license</a>
                </li>
              </ul>
            </div>
          </nav>
        </div>

        <div className={styles.right}>
          <div className={styles.panel}>
            <div className={styles.tiles}>
              <Tile href={`${GITHUB_URL}/stargazers`} icon={<Star size={15} strokeWidth={2} />} value={data.stats.stars} label="Stars" />
              <Tile href={`${GITHUB_URL}/forks`} icon={<GitFork size={15} strokeWidth={2} />} value={data.stats.forks} label="Forks" />
              <Tile href={`${GITHUB_URL}/releases`} icon={<Download size={15} strokeWidth={2} />} value={data.stats.downloads} label="Downloads" />
              <Tile href={`${GITHUB_URL}/commits`} icon={<GitCommitHorizontal size={15} strokeWidth={2} />} value={data.stats.commits} label="Commits" />
            </div>

            <div className={styles.chartHead}>
              <span className={styles.chartTitle}>{useStars ? "Stars over time" : "Commits over time"}</span>
              <span className={styles.legend}>
                <i aria-hidden="true" />
                Release
              </span>
            </div>

            <HistoryChart
              times={times}
              noun={useStars ? "star" : "commit"}
              releases={data.releases}
              end={data.end}
            />

            <p className={styles.source}>From GitHub, as of {DATE.format(data.end)}</p>
          </div>
        </div>
      </div>

      <div className={styles.base}>
        <span>© 2026 InterMux contributors</span>
        <span>Open source under the MIT license</span>
      </div>
    </footer>
  );
}
