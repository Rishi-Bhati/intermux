import {
  BUNDLE_STEPS,
  INSTALL_COMMAND,
  INSTALL_OPTIONS,
  INSTALLER_SOURCE_URL,
  SH_URL,
  SOURCE_STEPS,
  TARBALL,
  TARBALL_URL,
  WGET_COMMAND,
  WINDOWS_COMMAND,
  WINDOWS_URL,
} from "@/lib/config";
import InstallCommand from "./InstallCommand";
import InstallTabs from "./InstallTabs";
import styles from "./Install.module.css";


const distros = ["Ubuntu", "Debian", "Fedora", "Arch", "openSUSE", "Alpine"];

const oneLinerHint = (
  <>
    Installs what is missing, verifies the download. Run again to update. <a href={INSTALLER_SOURCE_URL}>Read the script</a>
  </>
);

export default function Install() {
  return (
    <section id="install" className={styles.section} aria-labelledby="install-title">
      <div className={styles.inner}>
        <div className={styles.left}>
          <span className="eyebrow">Install</span>
          <h2 id="install-title" className="section-title">
            One line. That&apos;s the install.
          </h2>

          <InstallTabs
            tabs={[
              { id: "curl", label: "curl", badge: "Recommended", hint: oneLinerHint, lines: [INSTALL_COMMAND], options: INSTALL_OPTIONS },
              { id: "wget", label: "wget", hint: oneLinerHint, lines: [WGET_COMMAND], options: INSTALL_OPTIONS },
              {
                id: "bundle",
                label: "By hand",
                hint: (
                  <>
                    <a href={TARBALL_URL}>{TARBALL}</a>, checked and installed. x86-64, glibc 2.35+.
                  </>
                ),
                lines: BUNDLE_STEPS,
              },
              {
                id: "source",
                label: "From source",
                hint: <>Needs python3 with venv and tkinter.</>,
                lines: SOURCE_STEPS,
              },
            ]}
            optionsExample={`curl -fsSL ${SH_URL} | sh -s -- --uninstall`}
          />

        </div>

        <aside className={styles.right} aria-label="Requirements and other platforms">
          <div className={styles.card}>
            <h3>Works on</h3>
            <ul className={styles.chips} aria-label="Distributions">
              {distros.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
            <p className={styles.distros}>Needs systemd and two or more network connections.</p>
          </div>

          <div className={styles.card}>
            <h3>
              On Windows? <span className={styles.pre}>Preview</span>
            </h3>
            <div className={styles.winCmd}>
              <InstallCommand command={WINDOWS_COMMAND} label="Copy Windows install command" />
            </div>
            <a href={WINDOWS_URL} className={styles.link}>
              About the Windows edition →
            </a>
          </div>
        </aside>
      </div>
    </section>
  );
}
