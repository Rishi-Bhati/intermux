"use client";

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import styles from "./Install.module.css";

type Option = { flag: string; desc: string };
type Tab = { id: string; label: string; badge?: string; hint: ReactNode; lines: string[]; options?: Option[] };

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/** Install methods as tabs, each a block of shell lines with one button that copies them all. */
export default function InstallTabs({ tabs, optionsExample }: { tabs: Tab[]; optionsExample?: string }) {
  const [active, setActive] = useState(0);
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => () => clearTimeout(timer.current), []);

  const tab = tabs[active];

  const select = (i: number) => {
    setActive(i);
    setCopied(false);
  };

  const onKey = (e: KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = (active + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
    select(next);
    tabRefs.current[next]?.focus();
  };

  const onCopy = async () => {
    if (await copyText(tab.lines.join("\n"))) {
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className={styles.tabs}>
      <div className={styles.tabBar}>
        <div role="tablist" aria-label="Install method" className={styles.tabList} onKeyDown={onKey}>
          {tabs.map((t, i) => (
            <button
              key={t.id}
              ref={(el) => {
                tabRefs.current[i] = el;
              }}
              type="button"
              role="tab"
              id={`tab-${t.id}`}
              aria-selected={i === active}
              aria-controls={`panel-${t.id}`}
              tabIndex={i === active ? 0 : -1}
              className={styles.tab}
              onClick={() => select(i)}
            >
              {t.label}
              {t.badge && <span className={styles.badge}>{t.badge}</span>}
            </button>
          ))}
        </div>
        <button type="button" className={styles.copyAll} onClick={onCopy} data-copied={copied || undefined}>
          {copied ? "Copied" : tab.lines.length > 1 ? "Copy all" : "Copy"}
        </button>
      </div>

      <div role="tabpanel" id={`panel-${tab.id}`} aria-labelledby={`tab-${tab.id}`} className={styles.panel}>
        <pre className={styles.code}>
          {tab.lines.map((line, i) => (
            <span key={line} className={styles.line} data-single={tab.lines.length === 1 || undefined}>
              {tab.lines.length > 1 && (
                <span className={styles.num} aria-hidden="true">
                  {i + 1}
                </span>
              )}
              <span className={styles.dollar} aria-hidden="true">
                $
              </span>
              {line}
            </span>
          ))}
        </pre>
        <p className={styles.hint}>{tab.hint}</p>
        {tab.options && (
          <details className={styles.options}>
            <summary>Options</summary>
            <dl>
              {tab.options.map((o) => (
                <div key={o.flag}>
                  <dt>
                    <code>{o.flag}</code>
                  </dt>
                  <dd>{o.desc}</dd>
                </div>
              ))}
            </dl>
            {optionsExample && (
              <p>
                Pass them after <code>sh -s --</code>, for example <code>{optionsExample}</code>
              </p>
            )}
          </details>
        )}
      </div>
      <span className="sr-only" role="status">
        {copied ? "Install commands copied to clipboard" : ""}
      </span>
    </div>
  );
}
