"use client";

import { useEffect, useRef, useState } from "react";
import styles from "./InstallCommand.module.css";

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for insecure contexts or blocked clipboard access.
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

export default function InstallCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => () => clearTimeout(timer.current), []);

  const onCopy = async () => {
    if (await copyText(command)) {
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className={styles.box}>
      <span className={styles.prompt} aria-hidden="true">
        $
      </span>
      <code className={styles.command}>{command}</code>
      <button
        type="button"
        className={styles.copy}
        onClick={onCopy}
        data-copied={copied || undefined}
        aria-label="Copy install command"
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          {copied ? (
            <path d="M3 8.5l3.2 3.2L13 4.8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          ) : (
            <>
              <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
              <path d="M10.5 3.5v-.3A1.2 1.2 0 009.3 2H3.7A1.2 1.2 0 002.5 3.2v5.6A1.2 1.2 0 003.7 10h.3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </>
          )}
        </svg>
        <span>{copied ? "Copied" : "Copy"}</span>
      </button>
      <span className="sr-only" role="status">
        {copied ? "Install command copied to clipboard" : ""}
      </span>
    </div>
  );
}
