"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import usePrefersReducedMotion from "@/hooks/usePrefersReducedMotion";
import Chip from "./Chip";
import TrafficRow, { type Tone } from "./TrafficRow";
import { ActivityIcon, BrowserIcon, CameraIcon, DownloadIcon, RouterIcon, UsbIcon, WifiIcon } from "./icons";
import panel from "./Panel.module.css";
import styles from "./Monitor.module.css";

// `max` is roughly what each connection can carry, in MB/s, so every graph has its own scale.
const interfaces: Array<{ id: string; label: string; max: number; icon: ReactNode }> = [
  { id: "eth0", label: "Ethernet", max: 120, icon: <RouterIcon size={24} strokeWidth={1.8} /> },
  { id: "wlan0", label: "Wi-Fi", max: 30, icon: <WifiIcon size={24} strokeWidth={1.8} /> },
  { id: "usb0", label: "Tethering", max: 5, icon: <UsbIcon size={24} strokeWidth={1.8} style={{ transform: "rotate(45deg)" }} /> },
];

// Which row each app sits on before and after InterMux.
const apps: Array<{ id: string; label: string; icon: ReactNode; color: string; before: number; after: number }> = [
  { id: "qbittorrent", label: "qbittorrent", icon: <DownloadIcon size={15} strokeWidth={2} />, color: "var(--amber)", before: 0, after: 0 },
  { id: "firefox", label: "firefox", icon: <BrowserIcon size={15} strokeWidth={1.9} />, color: "var(--cyan)", before: 0, after: 1 },
  { id: "zoom", label: "zoom", icon: <CameraIcon size={15} strokeWidth={1.9} />, color: "var(--violet)", before: 0, after: 2 },
];

// Before: Ethernet is the primary connection, so every app piles onto it and it maxes out.
const before: { mbps: number[]; tones: Tone[] } = { mbps: [120, 0, 0], tones: ["hot", "idle", "idle"] };
const after: { mbps: number[]; tones: Tone[] } = { mbps: [82, 14, 3], tones: ["ok", "ok", "ok"] };

/** The call, as a signal meter: one red bar when it lags, five green bars when it is clear. */
function CallQuality({ ms, bars, good }: { ms: number; bars: number; good: boolean }) {
  const reduced = usePrefersReducedMotion();
  const [value, setValue] = useState(ms);
  const from = useRef(ms);

  useEffect(() => {
    if (reduced) {
      from.current = ms;
      const id = requestAnimationFrame(() => setValue(ms));
      return () => cancelAnimationFrame(id);
    }
    const startValue = from.current;
    const t0 = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min((now - t0) / 900, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      const v = startValue + (ms - startValue) * eased;
      from.current = v;
      setValue(v);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [ms, reduced]);

  // A call never sits perfectly still: nudge the reading every so often.
  const [jitter, setJitter] = useState(0);
  useEffect(() => {
    if (reduced) return;
    const id = window.setInterval(() => setJitter((Math.random() - 0.5) * 0.11), 520);
    return () => window.clearInterval(id);
  }, [reduced]);

  return (
    <div className={styles.call} data-good={good || undefined}>
      <span className={styles.callIcon}>
        <CameraIcon size={19} strokeWidth={1.7} aria-hidden="true" />
        Video call
      </span>
      <span className={styles.bars} aria-hidden="true">
        {[1, 2, 3, 4, 5].map((n) => (
          <i key={n} data-on={n <= bars || undefined} style={{ height: 4 + n * 2.4 }} />
        ))}
      </span>
      <span className={styles.latency}>
        {Math.round(value * (1 + jitter))}
        <small>ms</small>
      </span>
    </div>
  );
}

export default function Monitor({ stage }: { stage: number }) {
  const routed = stage >= 1;
  const data = routed ? after : before;

  return (
    <div className={panel.view} data-monitor>
      <div className={panel.header}>
        <span className={styles.unit}>
          <ActivityIcon size={14} strokeWidth={2} />
          Network
        </span>
      </div>

      <div className={styles.rows}>
        {interfaces.map((iface, row) => (
          <TrafficRow
            key={iface.id}
            label={iface.label}
            icon={iface.icon}
            mbps={data.mbps[row]}
            max={iface.max}
            tone={data.tones[row]}
            chips={
              apps.filter((a) => (routed ? a.after : a.before) === row).length ? (
                apps
                  .filter((a) => (routed ? a.after : a.before) === row)
                  .map((a) => (
                    <Chip key={a.id} id={a.id} label={a.label} icon={a.icon} color={a.color} order={apps.indexOf(a)} />
                  ))
              ) : (
                <span className={styles.slot} aria-hidden="true" />
              )
            }
          />
        ))}
      </div>

      <div className={panel.footer}>
        <CallQuality ms={routed ? 24 : 480} bars={routed ? 5 : 1} good={routed} />
      </div>
    </div>
  );
}
