"use client";

import { useEffect, useState } from "react";
import usePrefersReducedMotion from "@/hooks/usePrefersReducedMotion";
import styles from "./RotatingTitle.module.css";

const TITLES: Array<[string, string]> = [
  ["Pick a network", "for every app."],
  ["Every network,", "in one place."],
  ["Route by app,", "not by machine."],
  ["Policy routing,", "without the homework."],
];

const INTERVAL_MS = 3000;

/** The hero headline. It cycles through a few lines; the heading text for screen readers stays fixed. */
export default function RotatingTitle({ align = "center" }: { align?: "center" | "start" }) {
  const reduced = usePrefersReducedMotion();
  const [{ index, prev }, setState] = useState({ index: 0, prev: -1 });

  const go = (next: number) => setState((s) => (s.index === next ? s : { index: next, prev: s.index }));

  useEffect(() => {
    if (reduced) return;
    const id = setTimeout(() => go((index + 1) % TITLES.length), INTERVAL_MS);
    return () => clearTimeout(id);
  }, [index, reduced]);

  return (
    <>
      <h1 className={styles.title} data-align={align}>
        <span className="sr-only">InterMux: pick a network for every app.</span>
        <span className={styles.stack} aria-hidden="true">
          {TITLES.map(([first, second], i) => (
            <span
              key={first}
              className={styles.item}
              data-state={i === index ? "in" : i === prev ? "out" : "idle"}
            >
              <span className={styles.line}>{first}</span>
              <span className={`${styles.line} ${styles.soft}`}>{second}</span>
            </span>
          ))}
        </span>
      </h1>

      <div className={styles.ticks} data-align={align} role="group" aria-label="Headline">
        {TITLES.map(([first], i) => (
          <button
            key={first}
            type="button"
            className={styles.tick}
            data-active={i === index || undefined}
            aria-label={`Show headline ${i + 1} of ${TITLES.length}`}
            aria-current={i === index}
            onClick={() => go(i)}
          >
            <span
              className={styles.fill}
              key={i === index ? `run-${index}` : "off"}
              style={reduced ? undefined : { animationDuration: `${INTERVAL_MS}ms` }}
            />
          </button>
        ))}
      </div>
    </>
  );
}
