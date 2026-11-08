import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { ArrowRight, BookOpen, Monitor, Terminal } from "lucide-react";
import Logo from "@/components/Logo";
import { GITHUB_URL, VERSION } from "@/lib/config";
import styles from "./docs.module.css";

export const metadata: Metadata = {
  title: "Docs · InterMux",
  description:
    "A short guide to InterMux: bind an application to a network interface from the command line, then manage the same bindings in the window.",
};

const toc = [
  { id: "start", label: "Before you start" },
  { id: "first-binding", label: "Your first binding" },
  { id: "status", label: "Check what is running" },
  { id: "everyday", label: "Everyday commands" },
  { id: "adopt", label: "Apps already open" },
  { id: "reference", label: "Command reference" },
  { id: "gui", label: "The window" },
  { id: "cleanup", label: "Clean up and undo" },
  { id: "troubleshooting", label: "Troubleshooting" },
];

const commands: Array<{ cmd: string; desc: string }> = [
  { cmd: "intermux --help", desc: "List every command, or get help for one command." },
  { cmd: "intermux check", desc: "Report missing system tools and how to install them." },
  { cmd: "intermux interfaces [--all]", desc: "List bindable interfaces. --all includes the others." },
  { cmd: "intermux apps [--search TEXT]", desc: "List desktop applications, optionally filtered." },
  { cmd: "intermux bind --app APP --iface IF", desc: "Start an app inside a namespace bound to an interface." },
  { cmd: "intermux status", desc: "Show bindings, DNS, limits and traffic per namespace." },
  { cmd: "intermux verify --iface IF", desc: "Print the public IP seen from that interface." },
  { cmd: "intermux restart --app APP --iface IF", desc: "Restart a bound app in the same namespace." },
  { cmd: "intermux move --app APP --iface IF --to IF", desc: "Move a bound app to another interface (it restarts)." },
  { cmd: "intermux stop --app APP --iface IF", desc: "Stop the app bound to an interface." },
  { cmd: "intermux kill --app APP [--force]", desc: "Stop every host instance of an app." },
  { cmd: "intermux dns --iface IF SERVERS", desc: "Change the DNS servers of an interface namespace." },
  { cmd: "intermux limit --iface IF [--down M] [--up M]", desc: "Set or remove a bandwidth limit in Mbit/s." },
  { cmd: "intermux adopt --app APP --iface IF", desc: "Route an app that is already running." },
  { cmd: "intermux release ID", desc: "Stop routing an adopted app. The ID comes from adopt or status." },
  { cmd: "intermux reconnect", desc: "Re-add routes for interfaces that are back." },
  { cmd: "intermux teardown-idle", desc: "Remove plumbing for bindings with no running apps." },
  { cmd: "intermux remove --iface IF [--yes]", desc: "Stop everything on an interface and remove its namespace." },
  { cmd: "intermux reset [--kill-apps] [--yes]", desc: "Remove everything InterMux created." },
  { cmd: "intermux report [--web] [--out DIR]", desc: "Save a bug report zip, or open the reporting portal." },
];

function Code({ children, label = "Terminal" }: { children: ReactNode; label?: string }) {
  return (
    <div className={styles.code}>
      <div className={styles.codeBar}>
        <Terminal size={13} strokeWidth={2} aria-hidden="true" />
        <span>{label}</span>
      </div>
      <pre>
        <code>{children}</code>
      </pre>
    </div>
  );
}

function Section({ id, children }: { id: string; children: ReactNode }) {
  return (
    <section id={id} className={styles.section}>
      {children}
    </section>
  );
}

export default function DocsPage() {
  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <Link href="/" className={styles.brand} aria-label="InterMux home">
          <Logo size={26} />
          <span>InterMux</span>
        </Link>
        <nav className={styles.nav} aria-label="Primary">
          <Link href="/" className={styles.navLink}>
            Home
          </Link>
          <Link href="/#install" className={styles.navLink}>
            Install
          </Link>
          <Link href="/docs" className={styles.navLink} aria-current="page">
            Docs
          </Link>
          <a href={GITHUB_URL} className={styles.github}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z" />
            </svg>
            GitHub
          </a>
        </nav>
      </header>

      <div className={styles.hero}>
        <span className="eyebrow">Documentation</span>
        <h1 className={styles.title}>Using InterMux</h1>
        <p className={styles.lede}>
          Pick a network for each app. This short guide covers the command line first, then the same
          workflow in the window.
        </p>
        <div className={styles.meta}>
          <span>Linux</span>
          <span>Version {VERSION}, beta</span>
          <span>CLI and GUI</span>
        </div>
      </div>

      <div className={styles.layout}>
        <aside className={styles.sidebar} aria-label="On this page">
          <p className={styles.sidebarTitle}>On this page</p>
          <nav className={styles.toc}>
            {toc.map((item, i) => (
              <a key={item.id} href={`#${item.id}`}>
                <span>{String(i + 1).padStart(2, "0")}</span>
                {item.label}
              </a>
            ))}
          </nav>
          <div className={styles.sidebarFoot}>
            <a href={`${GITHUB_URL}#readme`}>
              <BookOpen size={14} strokeWidth={2} aria-hidden="true" />
              Full README
            </a>
          </div>
        </aside>

        <div className={styles.content}>
          <Section id="start">
            <h2>
              <span>01</span> Before you start
            </h2>
            <p>
              InterMux runs on Linux with systemd and network namespaces. Run it as your normal user,
              never with <code>sudo</code>. Network changes go through a small polkit-gated helper, so a
              password prompt appears when root work is needed.
            </p>
            <p>First, see what is available on the machine:</p>
            <Code>{`intermux check\nintermux interfaces\nintermux apps --search firefox`}</Code>
            <p>
              <code>check</code> reports missing tools. <code>interfaces</code> lists the connections you
              can bind, and <code>apps</code> searches the installed desktop applications. Flatpak apps
              cannot be bound because they run in their own sandbox.
            </p>
          </Section>

          <Section id="first-binding">
            <h2>
              <span>02</span> Your first binding
            </h2>
            <p>
              A binding is one interface plus one application. Use <code>bind</code> to start the app
              inside a namespace routed through that interface:
            </p>
            <Code>{`intermux bind --app firefox --iface wlan0`}</Code>
            <p>
              The app starts as you, with your profile and settings. Everything it sends leaves through{" "}
              <code>wlan0</code>. When the last process of the app exits, a background watcher removes the
              binding.
            </p>
            <p>DNS can be a preset or a comma-separated list of addresses:</p>
            <Code>{`intermux bind --app firefox --iface wlan0 --dns quad9\nintermux bind --app qbittorrent --iface eth0 --dns 1.1.1.1,9.9.9.9`}</Code>
            <p>
              Presets are <code>auto</code>, <code>cloudflare</code>, <code>google</code> and{" "}
              <code>quad9</code>. If the app is already running, add <code>--separate-profile</code> to
              open a second copy, or <code>--close-existing</code> to close and reopen it.
            </p>
          </Section>

          <Section id="status">
            <h2>
              <span>03</span> Check what is running
            </h2>
            <Code>{`intermux status`}</Code>
            <p>
              <code>status</code> shows every binding and every routed running app. For each namespace it
              also reports the DNS servers, the bandwidth limit and the traffic counters. To confirm which
              address the outside world sees:
            </p>
            <Code>{`intermux verify --iface wlan0`}</Code>
          </Section>

          <Section id="everyday">
            <h2>
              <span>04</span> Everyday commands
            </h2>
            <div className={styles.cards}>
              <div className={styles.card}>
                <h3>Move and restart</h3>
                <Code label="Shell">{`intermux move --app firefox \\\n  --iface wlan0 --to wg0\n\nintermux restart --app firefox --iface wlan0`}</Code>
              </div>
              <div className={styles.card}>
                <h3>DNS and limits</h3>
                <Code label="Shell">{`intermux dns --iface wlan0 1.1.1.1,9.9.9.9\n\nintermux limit --iface wlan0 \\\n  --down 20 --up 5`}</Code>
              </div>
            </div>
            <p>
              Running apps pick up DNS changes without a restart. A bandwidth limit covers every app on
              that interface together; run <code>limit</code> without values to remove it. Moving an app
              restarts it, because routing belongs to the namespace.
            </p>
            <p>
              Use <code>stop</code> for one binding and <code>kill</code> for every host instance of an
              app. <code>reconnect</code> re-adds routes after a link comes back.
            </p>
            <Code>{`intermux stop --app firefox --iface wlan0\nintermux kill --app firefox\nintermux reconnect`}</Code>
          </Section>

          <Section id="adopt">
            <h2>
              <span>05</span> Apps that are already open
            </h2>
            <p>
              <code>adopt</code> routes a running app through an interface without closing it. InterMux
              moves its processes into a cgroup and marks its packets, so new connections use the chosen
              link:
            </p>
            <Code>{`intermux adopt --app firefox --iface eth0`}</Code>
            <p>
              The command prints an ID. Give that ID to <code>release</code> to stop routing the app:
            </p>
            <Code>{`intermux release <ID>`}</Code>
            <p>
              Connections the app already had open keep their old route until they reconnect. Name lookups
              for an adopted app use the normal system route, not the chosen interface, and IPv6 for the
              adopted group is rejected so traffic cannot leak around the interface. This needs cgroup v2
              and <code>nft</code>.
            </p>
          </Section>

          <Section id="reference">
            <h2>
              <span>06</span> Command reference
            </h2>
            <p>
              Run <code>intermux &lt;command&gt; --help</code> for flags and details. <code>helper</code>{" "}
              and <code>watch</code> are internal and are not meant to be run directly.
            </p>
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th scope="col">Command</th>
                    <th scope="col">What it does</th>
                  </tr>
                </thead>
                <tbody>
                  {commands.map((row) => (
                    <tr key={row.cmd}>
                      <td>
                        <code>{row.cmd}</code>
                      </td>
                      <td>{row.desc}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section id="gui">
            <h2>
              <span>07</span> The window
            </h2>
            <p>
              Run <code>intermux</code> with no command to open the GUI, or start it from your application
              menu. Do not run it as root. The window has three sections: <strong>New binding</strong>,{" "}
              <strong>Queue</strong> and <strong>Bindings</strong>.
            </p>
            <div className={styles.guiGrid}>
              <div className={styles.guiItem}>
                <Monitor size={18} strokeWidth={1.8} aria-hidden="true" />
                <h3>New binding</h3>
                <p>
                  Choose an interface, then an application. Pick DNS and a speed limit if you need them,
                  then add it to the queue. <strong>Start all</strong> starts the queue and asks for your
                  password once.
                </p>
              </div>
              <div className={styles.guiItem}>
                <Terminal size={18} strokeWidth={1.8} aria-hidden="true" />
                <h3>Bindings</h3>
                <p>
                  One row per interface, with its apps underneath. See status, PID, download and upload
                  speed, a one-minute graph, speed limit, DNS and public IP.
                </p>
              </div>
              <div className={styles.guiItem}>
                <ArrowRight size={18} strokeWidth={1.8} aria-hidden="true" />
                <h3>Actions</h3>
                <p>
                  Buttons follow the selected row: check public IP, restart, stop, open log, move, force
                  quit or release. Right-click a row for the same actions.
                </p>
              </div>
            </div>
            <p>
              If an app is already running when its queue starts, InterMux asks what to do: close and
              reopen it, open a separate profile, <strong>Shift current session</strong> to move the running
              copy without restarting it, or cancel. <strong>VPN connections…</strong> connects or imports
              NetworkManager VPNs. <strong>Refresh</strong>, <strong>Re-add routes</strong>,{" "}
              <strong>Remove stopped</strong> and <strong>Reset everything</strong> are in the same toolbar.
            </p>
          </Section>

          <Section id="cleanup">
            <h2>
              <span>08</span> Clean up and undo
            </h2>
            <p>
              Bindings normally disappear when their apps exit. To tidy up the rest, or to start over:
            </p>
            <Code>{`intermux teardown-idle\nintermux reset --kill-apps --yes`}</Code>
            <p>
              <code>reset</code> removes everything InterMux created. Add <code>--kill-apps</code> to also
              stop the applications it started, and <code>--yes</code> to skip the confirmation. To remove
              the program itself:
            </p>
            <Code>{`curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh -s -- --uninstall`}</Code>
          </Section>

          <Section id="troubleshooting">
            <h2>
              <span>09</span> Troubleshooting
            </h2>
            <p>
              Each binding writes a log under <code>~/.local/state/intermux/logs</code>. In the GUI,{" "}
              <strong>Open log</strong> opens the selected app&apos;s log. For verbose output on stderr:
            </p>
            <Code>{`INTERMUX_DEBUG=1 intermux status`}</Code>
            <p>
              <code>intermux report</code> writes a zip of diagnostics to <code>~/intermux-reports</code>{" "}
              (change the folder with <code>--out DIR</code>). Nothing is uploaded automatically. If idle
              bindings are not cleaned up, check the watcher:
            </p>
            <Code>{`sudo systemctl status intermux-watch.service\nsudo systemctl enable --now intermux-watch.service`}</Code>
            <p>
              InterMux is beta software that changes system networking. Try it first on a machine you can
              recover from a mistake.
            </p>
          </Section>

          <div className={styles.end}>
            <p>That covers the common path. The README has the full details.</p>
            <div className={styles.endLinks}>
              <a href={`${GITHUB_URL}#readme`} className="btn">
                <BookOpen size={16} strokeWidth={2} aria-hidden="true" />
                Read the README
              </a>
              <Link href="/#install" className="btn btn-primary">
                Install InterMux
                <ArrowRight size={16} strokeWidth={2} aria-hidden="true" />
              </Link>
            </div>
          </div>
        </div>
      </div>

      <footer className={styles.footer}>
        <span>© 2026 InterMux contributors</span>
        <span>Open source under the MIT license</span>
      </footer>
    </main>
  );
}
