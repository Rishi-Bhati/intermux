"use client";

import styles from "./ModeSwitch.module.css";

type Props = {
  on: boolean;
  onChange: (next: boolean) => void;
};

/** The command that does it. Grey while InterMux is off, blue once it is on. */
const COMMAND = "intermux adopt --app zoom --iface usb0";

export default function ModeSwitch({ on, onChange }: Props) {
  return (
    <div className={styles.wrap}>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={`InterMux: ${COMMAND}`}
        className={styles.bar}
        onClick={() => onChange(!on)}
      >
        <span className={styles.track} aria-hidden="true">
          <span className={styles.thumb} />
        </span>
        <span className={styles.text}>
          <code className={styles.cmd}>
            <span className={styles.prompt}>$</span> {COMMAND}
          </code>
          <span className={styles.caption}>
            {on ? "On: each app has its own connection" : "Off: every app shares one connection"}
          </span>
        </span>
      </button>
    </div>
  );
}
