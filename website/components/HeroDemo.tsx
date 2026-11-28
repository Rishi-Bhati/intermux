"use client";

import Image from "next/image";
import { useState } from "react";
import { Maximize2 } from "lucide-react";
import styles from "./HeroDemo.module.css";

const screenshots = [
  {
    label: "Routes",
    src: "/app-preview/dashboard.webp",
    alt: "The routes dashboard groups running apps by connection and shows live traffic and actions for the selected route.",
  },
  {
    label: "Route an app",
    src: "/app-preview/composer.webp",
    alt: "The routing form lets you choose an application and the connection it should use.",
  },
  {
    label: "VPN",
    src: "/app-preview/vpn.webp",
    alt: "The VPN connections screen lists saved profiles and their connection status.",
  },
];

export default function HeroDemo() {
  const [selected, setSelected] = useState(screenshots[0]);

  return (
    <figure className={styles.demo}>
      <div className={styles.frame}>
        <Image
          src={selected.src}
          alt={selected.alt}
          fill
          unoptimized
          loading="eager"
          className={styles.capture}
        />
      </div>
      <figcaption className={styles.bar}>
        <div className={styles.caption}>
          <span className={styles.label}>3.0 preview</span>
          <span className={styles.meta}>Sample data</span>
        </div>
        <div className={styles.controls} role="group" aria-label="App screenshots">
          {screenshots.map((screenshot) => (
            <button
              type="button"
              key={screenshot.src}
              aria-pressed={selected === screenshot}
              className={styles.button}
              onClick={() => setSelected(screenshot)}
            >
              {screenshot.label}
            </button>
          ))}
          <a
            className={styles.expand}
            href={selected.src}
            aria-label={`Open full-size ${selected.label} screenshot`}
          >
            <Maximize2 size={14} aria-hidden="true" />
          </a>
        </div>
      </figcaption>
    </figure>
  );
}
