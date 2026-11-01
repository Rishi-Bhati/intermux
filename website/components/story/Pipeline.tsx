import { AppWindow, Lock, Route, SquareDashed, Wifi } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import panel from "./Panel.module.css";
import styles from "./Pipeline.module.css";

const nodes: Array<{ title: string; caption: string; code: string; color: string; icon: ReactNode }> = [
  {
    title: "Your app",
    caption: "Runs as you, with your files and settings",
    code: "firefox",
    color: "var(--cyan)",
    icon: <AppWindow size={19} strokeWidth={1.7} />,
  },
  {
    title: "Its own network",
    caption: "Separate routes and DNS",
    code: "ns_3fa9c2d1",
    color: "var(--violet)",
    icon: <SquareDashed size={19} strokeWidth={1.7} />,
  },
  {
    title: "A route you choose",
    caption: "Its traffic is steered to one interface",
    code: "10.200.3.0/24 → table 1003",
    color: "var(--amber)",
    icon: <Route size={19} strokeWidth={1.7} />,
  },
  {
    title: "Your chosen connection",
    caption: "Everything leaves through wlan0",
    code: "wlan0 · 198.51.100.24",
    color: "var(--green)",
    icon: <Wifi size={19} strokeWidth={1.7} />,
  },
];

export default function Pipeline() {
  return (
    <div className={panel.view}>
      <div className={panel.header}>
        <span>
          <span className={panel.prompt}>$</span> <strong>intermux bind --app firefox --iface wlan0</strong>
        </span>
      </div>

      <div className={styles.flow} data-active>
        <div className={styles.rail} aria-hidden="true">
          <span className={styles.packet} />
        </div>
        {nodes.map((node, i) => (
          <div key={node.title} className={styles.node} style={{ "--i": i, "--c": node.color } as CSSProperties}>
            <span className={styles.icon}>{node.icon}</span>
            <div className={styles.text}>
              <span className={styles.title}>{node.title}</span>
              <span className={styles.caption}>{node.caption}</span>
            </div>
            <code className={styles.code}>{node.code}</code>
          </div>
        ))}
      </div>

      <div className={panel.footer}>
        <span className={styles.lock}>
          <Lock size={15} strokeWidth={1.9} aria-hidden="true" />
          One password prompt sets it up. The app never runs as root.
        </span>
      </div>
    </div>
  );
}
